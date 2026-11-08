"""APM services: ingest (one code path for the push API, the Redis reader and the seed command), tokens, retry requests,
stale detection and retention.

``ingest_matrix`` / ``ingest_table`` / ``ingest_logs`` take the producer's data, validate it with ``apm.payloads`` (a
structural problem raises ``PayloadError`` before anything is written), apply it in ONE transaction per payload (cells,
history, run row and bookkeeping commit together or not at all) and only then emit events, so a failing listener or a
rolled-back payload can never lose or invent state.
"""
import hashlib
import hmac
import logging
import re
import secrets as pysecrets
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from core.access import scope_by_site

from . import payloads
from .models import (FAILED, HEALTHY, WARNING, AdapterCell, AdapterRun, CellEvent, ExecLog, ProcessState,
                     RetryRequest, Source)

log = logging.getLogger("linkedeye.apm")

# ----------------------------------------------------------------------------- settings (overridable in settings.py)
RETENTION_DAYS = 30            # adapter runs and cell history
LOG_RETENTION_DAYS = 7
CAP_RUNS = 5000                # rows kept per source
CAP_CELL_EVENTS = 20000
CAP_LOGS = 20000
RETRY_TTL = timedelta(hours=1)  # a retry nobody picked up expires
MAX_EVENTS_PER_INGEST = 25
RATE_LIMIT = 60
RATE_WINDOW = 60
CANON_SEGMENTS = ["NSE", "NFO", "CDS", "BSE", "BFO", "SLBM", "MSE", "NIPO", "BSTAR", "NCOM"]


def _cfg(name, default):
    return getattr(settings, f"APM_{name}", default)


def tenant():
    return settings.LINKEDEYE_TENANT


# ----------------------------------------------------------------------------- sources and scoping
def sources_for(user):
    """Sources of this tenant the user may see (site scoped)."""
    return scope_by_site(Source.objects.filter(tenant_key=tenant()), user, field="site")


def stale_after(source):
    """Seconds after which data is stale: the expected interval plus a short grace (so a cron that is a few seconds late
    does not flicker)."""
    interval = max(30, int(source.expected_interval_s or 300))
    return interval + min(30, interval // 5)


def age_seconds(then, now=None):
    return None if then is None else max(0.0, ((now or timezone.now()) - then).total_seconds())


def is_stale(source, at, now=None):
    """True when ``at`` (the last time data of one kind arrived) is missing or older than the stale threshold."""
    age = age_seconds(at, now)
    return age is None or age > stale_after(source)


# ----------------------------------------------------------------------------- tokens
TOKEN_RE = re.compile(r"^lea_([0-9a-f]{8})_([A-Za-z0-9_-]{32,64})$")
_DUMMY_SALT = "0" * 32
_DUMMY_HASH = hashlib.sha256((_DUMMY_SALT + ":dummy").encode()).hexdigest()


def _hash_token(salt, token):
    return hashlib.sha256(f"{salt}:{token}".encode()).hexdigest()


def issue_token(source):
    """Generate a new ingest token for ``source`` (replacing any old one) and return the plaintext, shown to the operator
    exactly once. Only the prefix, a per-source salt and a salted SHA-256 are stored. The caller saves the source."""
    prefix = pysecrets.token_hex(4)
    token = f"lea_{prefix}_{pysecrets.token_urlsafe(32)}"
    source.token_prefix = prefix
    source.token_salt = pysecrets.token_hex(16)
    source.token_hash = _hash_token(source.token_salt, token)
    source.token_rotated_at = timezone.now()
    return token


@sensitive_variables()
def authenticate(header):
    """The enabled Source an ``Authorization: Bearer <token>`` header belongs to, or None.

    Every failure takes the same path: a (dummy) hash is always computed and compared in constant time, so neither the
    response nor its timing says whether a prefix exists, whether the source is disabled or whether the secret was wrong."""
    scheme, _, given = (header or "").partition(" ")
    given = given.strip()
    m = TOKEN_RE.match(given) if scheme.lower() == "bearer" else None
    # the same query runs for a malformed header (prefix "-" matches nothing), so its cost does not tell the two apart
    candidates = list(Source.objects.filter(tenant_key=tenant(), token_prefix=m.group(1) if m else "-").exclude(token_hash="")[:5])
    found = None
    if not candidates:
        hmac.compare_digest(_hash_token(_DUMMY_SALT, given or "x"), _DUMMY_HASH)
    for src in candidates:
        if hmac.compare_digest(_hash_token(src.token_salt, given), src.token_hash) and src.enabled:
            found = src
    return found


# ----------------------------------------------------------------------------- ingest helpers
class _Pending:
    """Events collected inside the transaction and emitted after it commits."""

    def __init__(self):
        self.items = []

    def add(self, **kw):
        self.items.append(kw)


def _emit_all(pending, limit=MAX_EVENTS_PER_INGEST):
    from core.events import emit
    shown = pending.items[:limit]
    for kw in shown:
        try:
            emit("apm", **kw)
        except Exception:                           # noqa: BLE001 - an event must never break ingest
            log.exception("apm could not emit an event")
    rest = pending.items[limit:]
    if rest:
        sev = "critical" if any(i["severity"] == "critical" for i in rest) else rest[0]["severity"]
        try:
            emit("apm", title=f"APM: {len(rest)} more state change(s) in the same report", severity="warning" if sev == "critical" else "ok",
                 message="Open the APM dashboard for the full list.", site=rest[0].get("site", ""))
        except Exception:                           # noqa: BLE001
            log.exception("apm could not emit the summary event")


def _touch(src, now, ip):
    fields = {"last_seen": now}
    if ip:
        fields["last_ip"] = ip
    return fields


def _source_recovered(src, pending):
    if src.silent_since:
        pending.add(title=f"APM source reporting again: {src.name}", severity="ok", site=src.site,
                    message=f"{src.name} sent data after being silent since {src.silent_since:%Y-%m-%d %H:%M:%S %Z}.",
                    fingerprint=f"apm:source:{src.pk}", kind="state_change", object_type="apm.source", object_id=src.pk)
        src.silent_since = None


def _run_row(src, kind, key, digest, executed_at, now, **extra):
    """(run, status) where status is 'new', 'duplicate' (same key, same content) or 'update' (same key, other content)."""
    run = AdapterRun.objects.filter(source=src, kind=kind, executed_key=key).first()
    if run is None:
        try:
            with transaction.atomic():
                run = AdapterRun.objects.create(
                    tenant_key=src.tenant_key, source=src, kind=kind, executed_key=key, payload_hash=digest,
                    executed_at=executed_at, received_at=now, **extra)
            # bounded at write time too: a flapping or hostile producer must not outrun the 6-hourly retention job
            _trim(AdapterRun.objects.filter(source=src), "-executed_at", _cfg("CAP_RUNS", CAP_RUNS))
            return run, "new"
        except IntegrityError:
            run = AdapterRun.objects.get(source=src, kind=kind, executed_key=key)
    if run.payload_hash == digest:
        return run, "duplicate"
    for k, v in {"payload_hash": digest, "executed_at": executed_at, "received_at": now, **extra}.items():
        setattr(run, k, v)
    run.save()
    return run, "update"


def _result(run, status, **kw):
    out = {"status": status, "duplicate": status == "duplicate", "run_id": run.pk if run else None,
           "warnings": [], "warnings_total": 0}
    out.update(kw)
    return out


# ----------------------------------------------------------------------------- the matrix
def ingest_matrix(source, payload, *, host="", script="", duration_s=None, now=None, ip=None, emit_events=True,
                  completed=()):
    """Apply one adapter-matrix payload (the toolkit's dict). Returns a summary dict; raises PayloadError (nothing written)."""
    now = now or timezone.now()
    m = payloads.parse_matrix(payload, now=now)
    pending = _Pending()
    removed = 0
    with transaction.atomic():
        src = Source.objects.select_for_update().get(pk=source.pk)
        if src.tenant_key != tenant():
            raise payloads.PayloadError("unknown source", "unknown", 404)
        before = AdapterRun.objects.filter(source=src, kind="matrix").order_by("-executed_at").first()
        success = sum(1 for c in m.cells if c.status == HEALTHY)
        failure = sum(1 for c in m.cells if c.status == FAILED)
        run, status = _run_row(src, "matrix", m.executed_key, m.payload_hash, m.executed_at, now, total=len(m.cells),
                               success=success, failure=failure, overall=m.overall, duration_s=duration_s,
                               host=host[:120], script=script[:120])
        for k, v in _touch(src, now, ip).items():
            setattr(src, k, v)
        warnings, wtotal = list(m.warnings), m.warnings_total
        if status == "duplicate":
            src.save(update_fields=["last_seen", "last_ip", "updated_at"])
            return _result(run, status, warnings=warnings, warnings_total=wtotal, total=len(m.cells))
        # An OLDER report must never overwrite newer state, whether it is new or a replay of an old run with other content
        older = before is not None and before.pk != run.pk and m.executed_at < before.executed_at
        if older:
            src.save(update_fields=["last_seen", "last_ip", "updated_at"])
            warnings.append("this report is older than the current state; it was recorded in the history only")
            return _result(run, "older", warnings=warnings, warnings_total=wtotal + 1, total=len(m.cells))
        if not m.cells and AdapterCell.objects.filter(source=src).exists():
            src.save(update_fields=["last_seen", "last_ip", "updated_at"])
            warnings.append("the matrix is empty; the existing adapters were kept")
            return _result(run, "empty", warnings=warnings, warnings_total=wtotal + 1, total=0)
        prev_at = before.executed_at if (before is not None and status == "new") else None
        changed = _apply_cells(src, m, run, status == "new", prev_at, now, pending)
        removed = changed["removed"]
        _complete_retries(src, m, completed, now)
        _source_recovered(src, pending)
        src.last_matrix_at = src.last_data_at = now
        src.save(update_fields=["last_seen", "last_ip", "last_matrix_at", "last_data_at", "silent_since", "updated_at"])
    if emit_events:
        _emit_all(pending)
    return _result(run, status, warnings=warnings, warnings_total=wtotal, total=len(m.cells), success=success,
                   failure=failure, created=changed["created"], updated=changed["updated"], removed=removed,
                   transitions=changed["transitions"])


def _apply_cells(src, m, run, new_run, prev_at, now, pending):
    existing = {(c.client, c.segment): c for c in AdapterCell.objects.filter(source=src)}
    seen, create, update, history = set(), [], [], []
    for c in m.cells:
        key = (c.client, c.segment)
        seen.add(key)
        cur = existing.get(key)
        error = c.error if c.status in (FAILED, WARNING) else ""
        if cur is None:
            cell = AdapterCell(
                tenant_key=src.tenant_key, source=src, client=c.client, segment=c.segment, ctcl_id=c.ctcl_id, value=c.value,
                status=c.status, last_error=error, exit_code=c.exit_code, updated_at=now,
                last_success_at=m.executed_at if c.status == HEALTHY else None,
                last_failure_at=m.executed_at if c.status == FAILED else None,
                failure_streak=1 if c.status == FAILED else 0, failure_total=1 if c.status == FAILED else 0)
            create.append(cell)
            history.append(CellEvent(tenant_key=src.tenant_key, source=src, client=c.client, segment=c.segment, ts=m.executed_at,
                                     old_status=None, new_status=c.status, ctcl_id=c.ctcl_id, error=error))
            if c.status == FAILED:
                _cell_event(pending, src, c, None)
            continue
        old = cur.status
        dirty = False
        for attr, val in (("ctcl_id", c.ctcl_id), ("value", c.value), ("last_error", error), ("exit_code", c.exit_code)):
            if getattr(cur, attr) != val:
                setattr(cur, attr, val)
                dirty = True
        if c.status == FAILED:
            cur.last_failure_at = m.executed_at
            if new_run or old != FAILED:        # a re-sent report counts once, but a change INTO failure always counts
                cur.failure_streak += 1
                cur.failure_total += 1
            dirty = True
        elif cur.failure_streak:
            cur.failure_streak = 0
            dirty = True
        if old != c.status:
            if old == HEALTHY and prev_at is not None:
                cur.last_success_at = prev_at            # the last run that saw it healthy
            if c.status == HEALTHY:
                cur.last_success_at = m.executed_at
            cur.status = c.status
            history.append(CellEvent(tenant_key=src.tenant_key, source=src, client=c.client, segment=c.segment, ts=m.executed_at,
                                     old_status=old, new_status=c.status, ctcl_id=c.ctcl_id, error=error))
            if c.status == FAILED or old == FAILED:
                _cell_event(pending, src, c, old)
            dirty = True
        if dirty:
            cur.updated_at = now
            update.append(cur)
    gone = [cell.pk for key, cell in existing.items() if key not in seen]
    for key, cell in existing.items():
        if key not in seen and cell.status == FAILED:
            _gone_event(pending, src, f"{cell.client} / {cell.segment}" + (f" (adapter {cell.ctcl_id})" if cell.ctcl_id else ""),
                        f"apm:cell:{src.pk}:{cell.client}:{cell.segment}", "adapter", "apm.adaptercell")
    if create:
        AdapterCell.objects.bulk_create(create, batch_size=500)
    if update:
        AdapterCell.objects.bulk_update(
            update, ["ctcl_id", "value", "last_error", "exit_code", "status", "last_success_at", "last_failure_at",
                     "failure_streak", "failure_total", "updated_at"], batch_size=300)
    if gone:
        for i in range(0, len(gone), 500):
            AdapterCell.objects.filter(pk__in=gone[i:i + 500]).delete()
    if history:
        CellEvent.objects.bulk_create(history, batch_size=500)
        _trim(CellEvent.objects.filter(source=src), "-ts", _cfg("CAP_CELL_EVENTS", CAP_CELL_EVENTS))
    return {"created": len(create), "updated": len(update), "removed": len(gone),
            "transitions": sum(1 for h in history if h.old_status is not None or h.new_status == FAILED)}


def _gone_event(pending, src, where, fingerprint, noun, object_type):
    """A failing adapter or process that the producer stopped reporting is no longer monitored: close its alert / incident
    with the same fingerprint instead of leaving it open for ever."""
    pending.add(title=f"APM {noun} no longer reported: {where}", severity="ok", site=src.site, fingerprint=fingerprint,
                message=f"The {noun} was failing and is no longer part of the report (removed from the producer's configuration).",
                kind="state_change", object_type=object_type, category="APM", source_name=src.name)


def _cell_event(pending, src, c, old):
    fp = f"apm:cell:{src.pk}:{c.client}:{c.segment}"
    where = f"{c.client} / {c.segment}" + (f" (adapter {c.ctcl_id})" if c.ctcl_id else "")
    if c.status == FAILED:
        pending.add(title=f"APM adapter down: {where}", severity="critical", site=src.site, fingerprint=fp,
                    message=(c.error or "The adapter reported a failure."), kind="state_change",
                    object_type="apm.adaptercell", client=c.client, category="APM", source_name=src.name)
    else:
        pending.add(title=f"APM adapter recovered: {where}", severity="ok", site=src.site, fingerprint=fp,
                    message="The adapter is no longer failing.", kind="state_change", object_type="apm.adaptercell",
                    client=c.client, category="APM", source_name=src.name)


# ----------------------------------------------------------------------------- the process table
def ingest_table(source, payload, *, host="", script="", duration_s=None, now=None, ip=None, emit_events=True,
                 completed=()):
    now = now or timezone.now()
    t = payloads.parse_table(payload, host=host, now=now)
    pending = _Pending()
    with transaction.atomic():
        src = Source.objects.select_for_update().get(pk=source.pk)
        if src.tenant_key != tenant():
            raise payloads.PayloadError("unknown source", "unknown", 404)
        before = AdapterRun.objects.filter(source=src, kind="table").order_by("-executed_at").first()
        success = sum(1 for r in t.rows if r.status == HEALTHY)
        failure = sum(1 for r in t.rows if r.status == FAILED)
        run, status = _run_row(src, "table", t.executed_key, t.payload_hash, t.executed_at, now, total=len(t.rows),
                               success=success, failure=failure, overall=t.overall, duration_s=duration_s,
                               host=host[:120], script=script[:120])
        for k, v in _touch(src, now, ip).items():
            setattr(src, k, v)
        warnings, wtotal = list(t.warnings), t.warnings_total
        if status == "duplicate":
            src.save(update_fields=["last_seen", "last_ip", "updated_at"])
            return _result(run, status, warnings=warnings, warnings_total=wtotal, total=len(t.rows))
        if before is not None and before.pk != run.pk and t.executed_at < before.executed_at:
            src.save(update_fields=["last_seen", "last_ip", "updated_at"])
            warnings.append("this report is older than the current state; it was recorded in the history only")
            return _result(run, "older", warnings=warnings, warnings_total=wtotal + 1, total=len(t.rows))
        hosts = {r.host for r in t.rows}
        existing = {(p.host, p.name): p for p in ProcessState.objects.filter(source=src, host__in=hosts)} if hosts else {}
        create, update = [], []
        for r in t.rows:
            cur = existing.get((r.host, r.name))
            fields = dict(pid=r.pid, status=r.status, cpu_pct=r.cpu_pct, mem_pct=r.mem_pct, uptime_s=r.uptime_s,
                          last_heartbeat=r.last_heartbeat or t.executed_at, last_execution=r.last_execution or t.executed_at,
                          next_execution=r.next_execution, response_ms=r.response_ms, exit_code=r.exit_code,
                          remarks=r.remarks, updated_at=now)
            if cur is None:
                create.append(ProcessState(tenant_key=src.tenant_key, source=src, host=r.host, name=r.name, **fields))
                if r.status == FAILED:
                    _process_event(pending, src, r, None)
                continue
            old = cur.status
            for k, v in fields.items():
                setattr(cur, k, v)
            update.append(cur)
            if old != r.status and (r.status == FAILED or old == FAILED):
                _process_event(pending, src, r, old)
        listed = {(r.host, r.name) for r in t.rows}
        gone = [p.pk for key, p in existing.items() if key not in listed]
        for key, p in existing.items():
            if key not in listed and p.status == FAILED:
                _gone_event(pending, src, p.name + (f" on {p.host}" if p.host else ""), f"apm:process:{src.pk}:{p.host}:{p.name}",
                            "process", "apm.processstate")
        if create:
            ProcessState.objects.bulk_create(create, batch_size=500)
        if update:
            ProcessState.objects.bulk_update(
                update, ["pid", "status", "cpu_pct", "mem_pct", "uptime_s", "last_heartbeat", "last_execution",
                         "next_execution", "response_ms", "exit_code", "remarks", "updated_at"], batch_size=300)
        if gone:
            ProcessState.objects.filter(pk__in=gone).delete()
        _source_recovered(src, pending)
        src.last_table_at = src.last_data_at = now
        src.save(update_fields=["last_seen", "last_ip", "last_table_at", "last_data_at", "silent_since", "updated_at"])
    if emit_events:
        _emit_all(pending)
    return _result(run, status, warnings=warnings, warnings_total=wtotal, total=len(t.rows), success=success,
                   failure=failure, created=len(create), updated=len(update), removed=len(gone))


def _process_event(pending, src, r, old):
    fp = f"apm:process:{src.pk}:{r.host}:{r.name}"
    where = f"{r.name}" + (f" on {r.host}" if r.host else "")
    if r.status == FAILED:
        pending.add(title=f"APM process down: {where}", severity="critical", site=src.site, fingerprint=fp,
                    message=r.remarks or "The process is not running.", kind="state_change",
                    object_type="apm.processstate", host=r.host, category="APM", source_name=src.name)
    else:
        pending.add(title=f"APM process recovered: {where}", severity="ok", site=src.site, fingerprint=fp,
                    message="The process is running again.", kind="state_change", object_type="apm.processstate",
                    host=r.host, category="APM", source_name=src.name)


# ----------------------------------------------------------------------------- logs
def ingest_logs(source, items, *, now=None, ip=None):
    """Store execution log lines. Duplicates (a client retrying a request) are dropped by content fingerprint."""
    now = now or timezone.now()
    lines, warnings, wtotal = payloads.parse_logs(items, now=now)
    with transaction.atomic():
        src = Source.objects.select_for_update().get(pk=source.pk)
        if src.tenant_key != tenant():
            raise payloads.PayloadError("unknown source", "unknown", 404)
        unique = {}
        for ln in lines:
            unique.setdefault(ln.fp, ln)
        known = set(ExecLog.objects.filter(source=src, fp__in=list(unique)).values_list("fp", flat=True)) if unique else set()
        fresh = [ln for fp, ln in unique.items() if fp not in known]
        if fresh:
            ExecLog.objects.bulk_create(
                [ExecLog(tenant_key=src.tenant_key, source=src, ts=ln.ts, level=ln.level, process=ln.process,
                         message=ln.message, client=ln.client, segment=ln.segment, fp=ln.fp) for ln in fresh],
                batch_size=500, ignore_conflicts=True)
            _cap_logs(src)
        src.last_seen = now
        if ip:
            src.last_ip = ip
        src.last_log_at = now
        src.save(update_fields=["last_seen", "last_ip", "last_log_at", "updated_at"])
    return {"status": "ok", "duplicate": not fresh and bool(lines), "accepted": len(fresh), "duplicates": len(lines) - len(fresh),
            "warnings": warnings, "warnings_total": wtotal, "run_id": None}


def _cap_logs(src, cap=None):
    _trim(ExecLog.objects.filter(source=src), "-ts", cap or _cfg("CAP_LOGS", CAP_LOGS))


# ----------------------------------------------------------------------------- retry requests
def expire_retries(source=None, now=None):
    now = now or timezone.now()
    qs = RetryRequest.objects.filter(tenant_key=tenant(), state__in=[RetryRequest.PENDING, RetryRequest.PICKED],
                                     requested_at__lt=now - RETRY_TTL)
    if source is not None:
        qs = qs.filter(source=source)
    return qs.update(state=RetryRequest.EXPIRED)


def request_retry(cell, user=None, note=""):
    """(RetryRequest, created). One open request per adapter: asking again returns the one already waiting. The source row
    is locked for the check-and-insert, so two operators clicking at once cannot create two requests."""
    expire_retries(cell.source)
    name = (user.get_username() if user is not None and getattr(user, "is_authenticated", False) else "system")
    with transaction.atomic():
        Source.objects.select_for_update().get(pk=cell.source_id)
        open_ = RetryRequest.objects.filter(source=cell.source, client=cell.client, segment=cell.segment,
                                            state__in=[RetryRequest.PENDING, RetryRequest.PICKED]).first()
        if open_:
            return open_, False
        req = RetryRequest.objects.create(
            tenant_key=cell.tenant_key, source=cell.source, client=cell.client, segment=cell.segment,
            requested_by=user if user is not None and getattr(user, "is_authenticated", False) else None,
            requested_by_name=name[:150], note=payloads.clean_text(note, 200))
    return req, True


def pick_commands(source, now=None):
    """Pending retry requests for a source, marked picked. What GET /api/apm/commands/ returns."""
    now = now or timezone.now()
    expire_retries(source, now)
    with transaction.atomic():
        rows = list(RetryRequest.objects.select_for_update().filter(source=source, state=RetryRequest.PENDING)
                    .order_by("requested_at", "id")[:100])
        if rows:
            RetryRequest.objects.filter(pk__in=[r.pk for r in rows]).update(state=RetryRequest.PICKED, picked_at=now)
    return rows


def complete_command(source, pk, now=None):
    """Mark one request of this source done. False when it is not this source's, or already finished."""
    now = now or timezone.now()
    n = RetryRequest.objects.filter(source=source, pk=pk, state__in=[RetryRequest.PENDING, RetryRequest.PICKED]).update(
        state=RetryRequest.DONE, done_at=now)
    return bool(n)


def _complete_retries(src, m, completed, now):
    ids = [i for i in completed if isinstance(i, int)]
    if ids:
        RetryRequest.objects.filter(source=src, pk__in=ids, state__in=[RetryRequest.PENDING, RetryRequest.PICKED]).update(
            state=RetryRequest.DONE, done_at=now)
    picked = list(RetryRequest.objects.filter(source=src, state=RetryRequest.PICKED, picked_at__lte=m.executed_at))
    if picked:
        present = {(c.client, c.segment) for c in m.cells}
        done = [r.pk for r in picked if (r.client, r.segment) in present]
        if done:
            RetryRequest.objects.filter(pk__in=done).update(state=RetryRequest.DONE, done_at=now)


# ----------------------------------------------------------------------------- stale detection and retention
def detect_stale(now=None):
    """One event when a source goes silent, one when it reports again (the latter from the ingest path). Returns a summary."""
    from core.events import emit
    now = now or timezone.now()
    marked = 0
    for src in Source.objects.filter(tenant_key=tenant(), enabled=True, silent_since__isnull=True, last_data_at__isnull=False):
        latest = max([t for t in (src.last_matrix_at, src.last_table_at) if t] or [src.last_data_at])
        if not is_stale(src, latest, now):
            continue
        if not Source.objects.filter(pk=src.pk, silent_since__isnull=True).update(silent_since=now):
            continue
        marked += 1
        try:
            emit("apm", title=f"APM source silent: {src.name}", severity="warning", site=src.site,
                 message=f"No new data from {src.name} for {int(age_seconds(latest, now) // 60)} min "
                         f"(expected every {src.expected_interval_s // 60 or 1} min). Is the cron job still running?",
                 fingerprint=f"apm:source:{src.pk}", kind="state_change", object_type="apm.source", object_id=src.pk)
        except Exception:                           # noqa: BLE001
            log.exception("apm could not emit the silent-source event")
    return {"silent": marked}


def retention(now=None):
    """Delete history older than the retention window and trim each source to its cap. Returns row counts."""
    now = now or timezone.now()
    days = _cfg("RETENTION_DAYS", RETENTION_DAYS)
    log_days = _cfg("LOG_RETENTION_DAYS", LOG_RETENTION_DAYS)
    out = {"runs": 0, "events": 0, "logs": 0, "retries": 0}
    t = tenant()
    out["runs"] += AdapterRun.objects.filter(tenant_key=t, executed_at__lt=now - timedelta(days=days)).delete()[0]
    out["events"] += CellEvent.objects.filter(tenant_key=t, ts__lt=now - timedelta(days=days)).delete()[0]
    out["logs"] += ExecLog.objects.filter(tenant_key=t, ts__lt=now - timedelta(days=log_days)).delete()[0]
    out["retries"] += RetryRequest.objects.filter(tenant_key=t, requested_at__lt=now - timedelta(days=days)).delete()[0]
    for src in Source.objects.filter(tenant_key=t):
        out["runs"] += _trim(AdapterRun.objects.filter(source=src), "-executed_at", _cfg("CAP_RUNS", CAP_RUNS))
        out["events"] += _trim(CellEvent.objects.filter(source=src), "-ts", _cfg("CAP_CELL_EVENTS", CAP_CELL_EVENTS))
        out["logs"] += _trim(ExecLog.objects.filter(source=src), "-ts", _cfg("CAP_LOGS", CAP_LOGS))
    return out


def _trim(qs, order, cap):
    """Delete everything beyond the newest ``cap`` rows of ``qs`` (ordered by ``order`` then pk descending)."""
    if qs.count() <= cap:
        return 0
    cut = list(qs.order_by(order, "-pk").values_list("pk", flat=True)[cap:cap + 1])
    if not cut:
        return 0
    boundary = qs.model.objects.get(pk=cut[0])
    field = order.lstrip("-")
    older = qs.filter(**{f"{field}__lt": getattr(boundary, field)}) | qs.filter(
        **{field: getattr(boundary, field)}, pk__lte=boundary.pk)
    return older.delete()[0]
