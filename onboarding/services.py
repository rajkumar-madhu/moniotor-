"""Onboarding services: scoping, the run/target state machine, the step log and target parsing.

Every queryset here is tenant- and site-scoped. Every mutating helper is called from a POST view
that has already checked `admin` on the run's site.
"""
import csv
import io
import logging
import re
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction
from django.utils import timezone

from core import secrets
from core.access import can_on, scope_by_site, visible_sites
from core.audit import audit

from .models import (DEFAULT_EXPORTERS, DEFAULT_PORTS, SD_STATES, STEP_SLUGS, STEP_STATUS, Credential,
                     CredentialKind, OnboardingRun, OnboardingTarget, RunKind, RunStatus, StepLog, TargetState,
                     TargetType)

log = logging.getLogger("linkedeye.onboarding")

MAX_TARGETS_PER_RUN = 500
MAX_PASTE_BYTES = 256 * 1024


def tenant():
    return settings.LINKEDEYE_TENANT


# ----------------------------------------------------------------------------- access
def can_onboard(user, site):
    """Only an admin for that site may onboard into it (viewers and operators cannot)."""
    return can_on(user, "admin", site)


def onboardable_sites(user):
    """Site names the user may onboard into, in display order."""
    from core.models import Site
    names = list(Site.objects.filter(tenant_key=tenant()).order_by("name").values_list("name", flat=True))
    allowed = visible_sites(user)
    if allowed is not None:
        names = [n for n in names if n in allowed]
    return [n for n in names if can_onboard(user, n)]


def runs_for(user):
    return scope_by_site(OnboardingRun.objects.filter(tenant_key=tenant()), user).select_related("client")


def credentials_for(user):
    return scope_by_site(Credential.objects.filter(tenant_key=tenant()), user)


def targets_for(user):
    return OnboardingTarget.objects.filter(run__in=runs_for(user).values("pk")).select_related("run")


def enabled_sd_targets(tenant_key=None, user=None):
    """Every enabled onboarded target of the tenant that is ready to be scraped. With `user`, only
    those on sites the user may see (the endpoint itself passes none: it serves the whole tenant)."""
    qs = (OnboardingTarget.objects
          .filter(run__tenant_key=tenant_key or tenant(), enabled=True, state__in=SD_STATES)
          .exclude(run__status=RunStatus.FAILED)
          .select_related("run", "run__client", "credential")
          .order_by("hostname", "id"))
    if user is not None:
        allowed = visible_sites(user)
        if allowed is not None:
            qs = qs.filter(run__site__in=list(allowed) + ["", "All Sites"])
    return qs


def can_manage_discovery(user):
    """The service-discovery token and fetch record are tenant-wide: only an admin on every site."""
    return can_on(user, "admin", None)


# ----------------------------------------------------------------------------- step log
def log_step(run, step, status="info", detail="", target=None, duration_ms=None):
    """Append one line to the run's step log. `detail` must never contain a secret."""
    return StepLog.objects.create(run=run, target=target, step=step, status=status,
                                  detail=(detail or "")[:500], duration_ms=duration_ms)


# ----------------------------------------------------------------------------- state machine
def next_step(step):
    i = STEP_SLUGS.index(step) if step in STEP_SLUGS else 0
    return STEP_SLUGS[min(i + 1, len(STEP_SLUGS) - 1)]


def previous_step(step):
    i = STEP_SLUGS.index(step) if step in STEP_SLUGS else 0
    return STEP_SLUGS[max(i - 1, 0)]


def enter_step(run, step):
    """Record that the operator acted on `step`. Idempotent and monotonic: `run.step` is the furthest
    step reached, so closing the browser and coming back resumes there, and revisiting an earlier
    step never moves the run (or its status) backwards. A finished run keeps its status.

    The run row is locked while it is read and written, so two concurrent POSTs (a double click, two
    tabs) cannot overwrite each other with a stale step."""
    if step not in STEP_SLUGS:
        return run
    with transaction.atomic():
        cur = OnboardingRun.objects.select_for_update().filter(pk=run.pk).first()
        if cur is None:                      # deleted under us
            return run
        fields = []
        if STEP_SLUGS.index(step) > cur.step_index:
            cur.step = step
            fields.append("step")
        if cur.status not in (RunStatus.FAILED, RunStatus.COMPLETE):
            status = STEP_STATUS.get(cur.step, cur.status)
            if status == RunStatus.COMPLETE:
                status = cur.status             # only complete_run() completes a run
            if cur.status != status:
                cur.status = status
                fields.append("status")
        if cur.started_at is None and step != "scope":
            cur.started_at = timezone.now()
            fields.append("started_at")
        if fields:
            cur.save(update_fields=fields + ["updated_at"])
    for name in ("step", "status", "started_at", "updated_at"):
        setattr(run, name, getattr(cur, name))
    return run


def _complete(run, detail, auto=False):
    """Complete the run with one conditional UPDATE, so that of two racing callers (the 60 s job and a
    page poll) exactly one wins. Returns True for the winner, who alone logs the completion. `auto`
    callers never complete a run an operator has marked failed."""
    now = timezone.now()
    blocked = [RunStatus.COMPLETE] + ([RunStatus.FAILED] if auto else [])
    won = OnboardingRun.objects.filter(pk=run.pk).exclude(status__in=blocked).update(
        status=RunStatus.COMPLETE, step="done", finished_at=now, updated_at=now)
    if won:
        run.status, run.step, run.finished_at = RunStatus.COMPLETE, "done", now
        log_step(run, "done", "ok", detail)
    return bool(won)


def complete_run(run, detail="Run completed"):
    """Mark the run complete (idempotent: the second call changes and logs nothing)."""
    _complete(run, detail)
    return run


def fail_run(run, detail=""):
    run.status = RunStatus.FAILED
    run.finished_at = run.finished_at or timezone.now()
    run.save(update_fields=["status", "finished_at", "updated_at"])
    log_step(run, run.step, "error", detail or "Run marked failed")
    return run


def reopen_run(run):
    """A failed run can be picked up again where it stopped."""
    status = STEP_STATUS.get(run.step, RunStatus.DRAFT)
    run.status = RunStatus.WAITING_FOR_DATA if status == RunStatus.COMPLETE else status
    run.finished_at = None
    run.save(update_fields=["status", "finished_at", "updated_at"])
    log_step(run, run.step, "info", "Run reopened")
    return run


#: a facts key containing one of these is dropped before anything is stored or rendered
_SECRETISH = ("password", "passwd", "secret", "token", "private_key", "privatekey", "passphrase",
              "api_key", "apikey", "authorization", "community", "auth_key", "priv_key")


#: no single stored fact string may be longer than this: a hostile or buggy exporter must not be able to put
#: megabytes into a JSON column that every run page then renders (the longest legitimate value is a shipper config)
MAX_FACT_CHARS = 12000


def scrub(value, _depth=0):
    """A copy of a facts structure without secret-looking keys and with every string bounded. Defence in depth:
    the hooks are told never to return a secret, and this makes sure one cannot reach the database or a page."""
    if _depth > 6:
        return None
    if isinstance(value, dict):
        return {k: scrub(v, _depth + 1) for k, v in value.items()
                if not any(w in str(k).lower() for w in _SECRETISH)}
    if isinstance(value, (list, tuple)):
        return [scrub(v, _depth + 1) for v in value][:200]
    if isinstance(value, str) and len(value) > MAX_FACT_CHARS:
        return value[:MAX_FACT_CHARS - 1] + "…"
    return value


def hook_outcome(result, current_state, error_state, default_state=None):
    """Split a hook's result into (state, error, facts).

    A hook reports its verdict with the keys ``state`` (a TargetState value) and ``error``; every
    other key is a fact. Without a ``state`` the verdict is `error_state` when there is an error,
    else `default_state`, else the target's current state (a hook that only describes something,
    such as an install command, does not move the target)."""
    facts = scrub(dict(result or {}))
    state = facts.pop("state", None)
    error = str(facts.pop("error", "") or "")
    valid = set(TargetState.values)
    if state not in valid:
        state = error_state if error else (default_state or current_state)
    return state, error, facts


def verify_default_state(facts):
    """Verdict for a verify result that did not name a state."""
    if facts.get("error") or facts.get("tcp") is False or facts.get("dns") is False:
        return TargetState.UNREACHABLE
    if facts.get("exporter"):
        return TargetState.EXPORTER_FOUND
    return TargetState.REACHABLE


def set_target_state(target, state, error="", facts=None, first_sample_at=None):
    """Write one target's outcome. `error` is operator-facing text and never holds a secret."""
    fields = ["state", "last_error", "updated_at"]
    target.state = state
    target.last_error = (error or "")[:500]
    if facts is not None:
        target.facts = {**(target.facts or {}), **scrub(facts)}
        fields.append("facts")
    if first_sample_at is not None:
        target.first_sample_at = first_sample_at
        fields.append("first_sample_at")
    target.save(update_fields=fields)
    return target


#: states a target reaches only when the operator activated it: a probe or an install never moves it out of them
ACTIVATED_STATES = (TargetState.SCRAPING, TargetState.LIVE)


def probe_state(current, new):
    """The state to store after a verify or enable probe said `new`. A target that is already registered for
    scraping or live keeps its state whatever the probe found (a DNS blip must not drop a healthy production
    host from service discovery); the result is still recorded in last_error and facts."""
    return current if current in ACTIVATED_STATES else new


#: an install that has not released its lock after this long is taken for dead (its request was killed)
INSTALL_LOCK_FOR = timedelta(minutes=10)


def claim_install(target):
    """Take the target's install lock with one conditional UPDATE, so that two requests cannot run an install on the
    same host at once. True when this caller holds the lock (and must call release_install), False when another
    install is running. The lock expires by itself, so a killed request cannot block the host for ever."""
    now = timezone.now()
    won = (OnboardingTarget.objects.filter(pk=target.pk)
           .filter(models.Q(install_lock_until__isnull=True) | models.Q(install_lock_until__lt=now))
           .update(install_lock_until=now + INSTALL_LOCK_FOR))
    return bool(won)


def release_install(target):
    OnboardingTarget.objects.filter(pk=target.pk).update(install_lock_until=None)


# ----------------------------------------------------------------------------- runs and targets
def site_client(site):
    """The client that owns the named site (or None)."""
    from core.models import Site
    row = Site.objects.filter(tenant_key=tenant(), name=site).select_related("client").first()
    return row.client if row is not None else None


def create_run(user, kind, site, client=None, environment="prod", notes="", request=None):
    client = client or site_client(site)
    run = OnboardingRun.objects.create(
        tenant_key=tenant(), kind=kind or RunKind.SERVER, site=site or "", client=client,
        environment=environment or "prod", notes=notes or "", step="scope", status=RunStatus.DRAFT,
        created_by=user if getattr(user, "is_authenticated", False) else None,
    )
    log_step(run, "scope", "ok", f"Run created for {run.site or 'All Sites'} ({run.get_kind_display()})")
    audit(request, "onboarding.run.create", run, site=run.site, kind=run.kind)
    return run


def default_target_type(run_kind):
    return {RunKind.DEVICE: TargetType.DEVICE, RunKind.PROMETHEUS: TargetType.PROMETHEUS}.get(
        run_kind, TargetType.LINUX)


_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9._\-]*[a-z0-9])?$|^[0-9a-f]*:[0-9a-f:.]*$")


def _clean_host(value):
    host = (value or "").strip().strip(",;")
    # accept "host:port", "http://host:port", "[v6]:port" and bare hosts
    if "://" in host:
        host = host.split("://", 1)[1]
    host = host.split("/", 1)[0]
    port = None
    if host.startswith("[") and "]" in host:
        host, _, rest = host[1:].partition("]")
        if rest.startswith(":"):
            try:
                port = int(rest[1:])
            except ValueError:
                return "", None
        elif rest:
            return "", None
    elif host.count(":") == 1:
        host, _, raw = host.partition(":")
        try:
            port = int(raw)
        except ValueError:
            return "", None                   # "db-01:abc" is a typo, not a host called db-01
    if not host or len(host) > 255:
        return "", None
    host = host.strip("[]").lower()
    if not host.isascii():
        try:                                  # an internationalised name is stored as its punycode form
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            return "", None
    if len(host) > 255:
        return "", None
    # letters, digits, dot, dash, underscore, or an IPv6 literal: nothing that could break a PromQL
    # selector, a YAML scrape block or a shell command later
    if not _HOST_RE.match(host.replace("_", "-")):
        return "", None
    if ":" not in host and any(len(label) > 63 for label in host.split(".")):
        return "", None                       # no resolver accepts a label over 63 characters
    return host, port


def parse_target_list(text, default_type=TargetType.LINUX, default_port=None):
    """Parse a pasted list or a CSV into [{hostname, address, target_type, port}], plus the lines
    that could not be read. Accepts "host", "host:port", "host,type", "host,type,port", a four-column
    "hostname,address,type,port" row and a CSV with a header row (hostname/address/type/port in any order)."""
    rows, bad = [], []
    text = text or ""
    if len(text) > MAX_PASTE_BYTES:
        # never act on a line cut in half: "db-0" would be taken for a real host
        text = text[:MAX_PASTE_BYTES].rsplit("\n", 1)[0]
    lines = [ln for ln in (text.replace("\r\n", "\n").replace("\r", "\n")).split("\n") if ln.strip()]
    if not lines:
        return rows, bad
    header = None
    try:
        first = [c.strip().lower() for c in next(csv.reader([lines[0]]))]
    except (csv.Error, StopIteration):
        first = []                       # an unreadable first line is reported with the others below
    if any(c in ("hostname", "host", "address", "ip") for c in first):
        header, lines = first, lines[1:]
    types = {t.value: t.value for t in TargetType}
    types.update({t.label.lower(): t.value for t in TargetType})
    types.update({"linux server": "linux", "windows server": "windows", "snmp": "device",
                  "network device": "device", "kubernetes": "k8s_node", "k8s": "k8s_node",
                  "service": "url"})
    seen = set()
    for raw in lines:
        try:
            cells = [c.strip() for c in next(csv.reader([raw]))]
        except (csv.Error, StopIteration):
            bad.append(raw.strip()[:120])
            continue
        data = {}
        if header:
            for key, cell in zip(header, cells):
                data[key] = cell
            host_cell = data.get("hostname") or data.get("host") or ""
            address = data.get("address") or data.get("ip") or ""
            type_cell = (data.get("type") or data.get("target_type") or "").lower()
            port_cell = data.get("port") or ""
        else:
            host_cell = cells[0] if cells else ""
            if len(cells) >= 4:                       # no header row, the documented column order
                address = cells[1]
                type_cell = cells[2].lower()
                port_cell = cells[3]
            else:
                address = ""
                type_cell = (cells[1].lower() if len(cells) > 1 else "")
                port_cell = cells[2] if len(cells) > 2 else ""
        host, inline_port = _clean_host(host_cell)
        if not host:
            bad.append(raw.strip()[:120])
            continue
        scheme = host_cell.strip().lower().split("://", 1)[0] if "://" in host_cell else ""
        scheme = scheme if scheme in ("http", "https") else ""
        if host in seen:
            continue
        seen.add(host)
        ttype = types.get(type_cell, default_type)
        port = None
        if str(port_cell).strip():
            try:
                port = int(port_cell)
            except ValueError:
                port = -1
        elif inline_port is not None:
            port = inline_port
        if port is None:
            port = default_port or DEFAULT_PORTS.get(ttype, 9100)
        if not 1 <= int(port) <= 65535:
            bad.append(raw.strip()[:120])
            continue
        addr, _ = _clean_host(address)
        rows.append({"hostname": host, "address": addr or "", "target_type": ttype, "port": int(port),
                     "scheme": scheme})
    return rows, bad


@transaction.atomic
def add_targets(run, rows, request=None):
    """Add targets to the run, idempotently: a hostname already on the run is updated, not
    duplicated. Returns (created, updated, skipped)."""
    existing = {t.hostname: t for t in run.targets.all()}
    room = MAX_TARGETS_PER_RUN - len(existing)
    created = updated = skipped = 0
    for row in rows:
        host = row["hostname"]
        ttype = row.get("target_type") or default_target_type(run.kind)
        port = int(row.get("port") or DEFAULT_PORTS.get(ttype, 9100))
        target = existing.get(host)
        if target is not None:
            changed = []
            for field, value in (("address", row.get("address") or target.address),
                                 ("target_type", ttype), ("port", port),
                                 ("scheme", row.get("scheme") or target.scheme)):
                if getattr(target, field) != value:
                    setattr(target, field, value)
                    changed.append(field)
            if not target.exporter_kind:
                target.exporter_kind = DEFAULT_EXPORTERS.get(ttype, "")
                changed.append("exporter_kind")
            if changed:
                target.save(update_fields=changed + ["updated_at"])
                updated += 1
            else:
                skipped += 1
            continue
        if room <= 0:
            skipped += 1
            continue
        try:
            with transaction.atomic():           # a savepoint: a lost race must not poison the outer transaction
                target = OnboardingTarget.objects.create(
                    run=run, hostname=host, address=row.get("address") or "", target_type=ttype, port=port,
                    scheme=row.get("scheme") or "", exporter_kind=DEFAULT_EXPORTERS.get(ttype, ""), state=TargetState.NEW,
                )
        except IntegrityError:                   # a concurrent POST added the same host first
            skipped += 1
            continue
        existing[host] = target
        room -= 1
        created += 1
    detail = f"{created} added, {updated} updated, {skipped} unchanged"
    log_step(run, "targets", "ok" if created or updated else "info", detail)
    audit(request, "onboarding.targets.add", run, site=run.site, created=created, updated=updated)
    return created, updated, skipped


def remove_target(run, target, request=None):
    host = target.hostname
    target.delete()
    log_step(run, "targets", "info", f"Removed {host}")
    audit(request, "onboarding.targets.remove", run, site=run.site, hostname=host)


#: which credential kinds make sense for which target types. A secret is only ever attached to, and used for,
#: a target that can legitimately receive it: an SSH login belongs to a server, an SNMP community to a device,
#: an HTTP login to something that serves /metrics. Everything else is skipped, never "attached anyway".
_SERVERS = (TargetType.LINUX, TargetType.WINDOWS)
CREDENTIAL_FITS = {
    CredentialKind.SSH_PASSWORD: _SERVERS,
    CredentialKind.SSH_KEY: _SERVERS,
    CredentialKind.SNMP_V2C: (TargetType.DEVICE,),
    CredentialKind.SNMP_V3: (TargetType.DEVICE,),
    CredentialKind.BASIC_AUTH: _SERVERS + (TargetType.K8S_NODE, TargetType.URL, TargetType.PROMETHEUS),
    CredentialKind.BEARER: _SERVERS + (TargetType.K8S_NODE, TargetType.URL),
}


def credential_fits(kind, target_type):
    """True when a credential of `kind` may be attached to, and used for, a target of `target_type`."""
    return str(target_type) in {str(t) for t in CREDENTIAL_FITS.get(str(kind), ())}


def attach_credential(run, credential, targets=None, request=None):
    """Point the given targets (or every target of the run) at `credential`. Idempotent. Targets whose type
    cannot take this kind of credential are skipped (and said so in the run log). Returns how many were attached."""
    qs = run.targets.all() if targets is None else run.targets.filter(pk__in=[t.pk for t in targets])
    fitting = [t.pk for t in qs if credential_fits(credential.kind, t.target_type)]
    skipped = qs.count() - len(fitting)
    n = run.targets.filter(pk__in=fitting).update(credential=credential)
    log_step(run, "credentials", "ok",
             f"{credential.get_kind_display()} “{credential.name}” attached to {n} target(s)")
    if skipped:
        log_step(run, "credentials", "warn",
                 f"{skipped} target(s) skipped: a {credential.get_kind_display()} does not apply to their type")
    audit(request, "onboarding.credential.attach", run, site=run.site, credential=credential.pk, targets=n,
          skipped=skipped)
    return n


#: labels the product itself uses for its tenant-wide vault rows; a site admin may not store a row under them
_RESERVED_LABELS = ("prometheus service discovery",)
_RESERVED_PREFIXES = ("prometheus http",)


def reserved_label(name):
    """True for a vault label that belongs to the product (the service-discovery token, the Prometheus login)."""
    norm = " ".join(str(name or "").split()).lower()
    return norm in _RESERVED_LABELS or norm.startswith(_RESERVED_PREFIXES)


def cleartext_allowed():
    """True when the operator opted in to sending an exporter login over plain http (a network they trust)."""
    return bool(getattr(settings, "LINKEDEYE_ONBOARDING_ALLOW_CLEARTEXT_CREDENTIALS", False))


CLEARTEXT_MESSAGE = ("The attached HTTP login was not sent: this target is reached over plain http, which would put "
                     "it on the network in clear text. Paste the target as https://host:port, or set "
                     "LINKEDEYE_ONBOARDING_ALLOW_CLEARTEXT_CREDENTIALS=true for a network you trust.")


def acting_request():
    """The request on whose behalf a hook runs (set by ``secrets.acting_as`` around a POST), or None. Hooks get no
    request of their own; this keeps their audit rows attributed to the operator instead of "system"."""
    return secrets._actor.get()


def clear_credential(run, target, request=None):
    target.credential = None
    target.save(update_fields=["credential", "updated_at"])
    log_step(run, "credentials", "info", f"Credential detached from {target.hostname}")
    audit(request, "onboarding.credential.detach", run, site=run.site, hostname=target.hostname)


def validate_hostname(value):
    host, _ = _clean_host(value)
    if not host:
        raise ValidationError("Enter a hostname or IP address.")
    return host


# ----------------------------------------------------------------------------- summaries
def run_summary(run):
    counts = {}
    for t in run.targets.all():
        counts[t.state] = counts.get(t.state, 0) + 1
    total = sum(counts.values())
    return {
        "total": total,
        "counts": counts,
        "live": counts.get(TargetState.LIVE, 0),
        "failed": counts.get(TargetState.FAILED, 0) + counts.get(TargetState.UNREACHABLE, 0),
        "scraping": sum(counts.get(s, 0) for s in SD_STATES),
        "pending": counts.get(TargetState.NEW, 0),
    }


def csv_upload_text(uploaded):
    """Decoded text of an uploaded CSV, bounded in size. Returns "" when there is nothing usable and
    raises ValueError for a file over the limit: truncating it would cut a line in half."""
    if not uploaded:
        return ""
    data = uploaded.read(MAX_PASTE_BYTES + 1)
    if len(data) > MAX_PASTE_BYTES:
        raise ValueError(f"That file is larger than {MAX_PASTE_BYTES // 1024} KB. Split it into smaller files.")
    if isinstance(data, bytes):
        for encoding in ("utf-8-sig", "utf-8", "latin-1"):
            try:
                return data.decode(encoding)
            except UnicodeDecodeError:
                continue
        return ""
    return str(data)


def csv_safe(value):
    """Defuse spreadsheet formula injection in exported cells."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t") else text


def read_csv(stream):
    return csv.reader(io.StringIO(stream))


# ----------------------------------------------------------------------------- liveness
#: only a target the operator activated waits for its first samples: a merely verified or installed one is not
#: live, whatever Prometheus says about it (activation is what creates its CMDB item, device and exporter)
WAITING_STATES = (TargetState.SCRAPING,)
#: the background job stops polling a target that has not changed for this long (it never came up);
#: it stays in service discovery and the operator's "Check now" still promotes it if it ever does
GIVE_UP_AFTER = timedelta(days=30)
POLL_LIMIT = 2000


def waiting_targets(tenant_key=None):
    """Targets the 60 s job should look at: waiting, enabled, on a run that is not failed, and not given up
    on. Newest first, so a pile of dead targets can never starve the fresh ones out of the batch."""
    return (OnboardingTarget.objects
            .filter(run__tenant_key=tenant_key or tenant(), enabled=True, state__in=WAITING_STATES,
                    updated_at__gte=timezone.now() - GIVE_UP_AFTER)
            .exclude(run__status=RunStatus.FAILED).select_related("run", "run__client")
            .order_by("-updated_at", "-id"))


def _promql_str(value):
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def refresh_liveness(targets, run=None):
    """Mark targets live once Prometheus reports ``up == 1`` for them, and complete a run whose
    targets are all live. One instant query covers every target (matched by the `le_target` label
    the service-discovery endpoint adds, or by `instance`). Returns the number newly live.

    Prometheus is the operator-configured server (not a probed target). When it is not connected
    or does not answer, nothing changes and the wizard keeps waiting."""
    from core import prometheus

    waiting = [t for t in targets if t.state in WAITING_STATES and t.enabled]
    if not waiting or not prometheus.configured():
        return 0
    try:
        rows = prometheus.query("up == 1")
    except prometheus.PrometheusError as exc:
        log.info("onboarding liveness check skipped: %s", exc)
        return 0
    except Exception as exc:              # noqa: BLE001 - an odd answer must not break the page poll or the job
        log.info("onboarding liveness check skipped: %s", type(exc).__name__)
        return 0
    up_ids = {r["metric"].get("le_target") for r in rows if r["metric"].get("le_target")}
    up_instances = {r["metric"].get("instance") for r in rows if r["metric"].get("instance")}
    now = timezone.now()
    live = 0
    touched_runs = {}
    for target in waiting:
        if str(target.pk) in up_ids or target.instance in up_instances:
            set_target_state(target, TargetState.LIVE, first_sample_at=target.first_sample_at or now)
            log_step(target.run, "confirm", "ok", f"{target.hostname}: up == 1, first samples landed",
                     target=target)
            live += 1
            touched_runs[target.run_id] = target.run
    for run_obj in touched_runs.values():
        finish_if_all_live(run_obj)
    return live


def finish_if_all_live(run):
    """Complete the run (and say so on the event stream) when every target of it is live. Returns True
    only for the call that completed it."""
    if run.status in (RunStatus.COMPLETE, RunStatus.FAILED):
        return False
    states = list(run.targets.values_list("state", flat=True))
    if not states or any(s != TargetState.LIVE for s in states):
        return False
    if not _complete(run, "Every target reports up == 1; run complete", auto=True):
        return False
    try:
        from core.events import emit
        emit("onboarding", f"Onboarding run #{run.pk} complete: {len(states)} target(s) live",
             severity="info", site=run.site, kind="onboarding", obj=run)
    except Exception:                     # an event listener must never break the check
        log.exception("could not emit the onboarding completion event")
    return True


def status_payload(run):
    """What the confirm page polls: the run and each target's state (no secrets, no facts)."""
    return {
        "run": {"status": run.status, "status_label": run.get_status_display(), "tone": run.tone,
                "step": run.step},
        "summary": run_summary(run),
        "targets": [{"id": t.pk, "hostname": t.hostname, "instance": t.instance, "state": t.state,
                     "state_label": t.get_state_display(), "tone": t.tone,
                     "first_sample": (timezone.localtime(t.first_sample_at).strftime("%d %b %H:%M")
                                          if t.first_sample_at else "")}
                    for t in run.targets.all()],
    }


def poll_waiting_targets():
    """Scheduled job: check every waiting target of the tenant. Never raises."""
    try:
        targets = list(waiting_targets()[:POLL_LIMIT])
        return {"live": refresh_liveness(targets), "checked": len(targets)}
    except Exception:
        log.exception("onboarding liveness job failed")
        return {"live": 0, "checked": 0, "error": True}


def prune_fetches(days=30):
    """Scheduled housekeeping: drop service-discovery fetch records older than `days`."""
    from .models import SDFetch
    cutoff = timezone.now() - timedelta(days=days)
    n, _ = SDFetch.objects.filter(fetched_at__lt=cutoff).delete()
    return n
