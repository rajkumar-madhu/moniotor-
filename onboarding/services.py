"""Onboarding services: scoping, the run/target state machine, the step log and target parsing.

Every queryset here is tenant- and site-scoped. Every mutating helper is called from a POST view
that has already checked `admin` on the run's site.
"""
import csv
import io

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from core.access import can_on, scope_by_site, visible_sites
from core.audit import audit

from .models import (DEFAULT_EXPORTERS, DEFAULT_PORTS, SD_STATES, STEP_SLUGS, STEP_STATUS, Credential,
                     OnboardingRun, OnboardingTarget, RunKind, RunStatus, StepLog, TargetState, TargetType)

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


def enabled_sd_targets(tenant_key=None):
    """Every enabled onboarded target of the tenant that is ready to be scraped."""
    return (OnboardingTarget.objects
            .filter(run__tenant_key=tenant_key or tenant(), enabled=True, state__in=SD_STATES)
            .exclude(run__status=RunStatus.FAILED)
            .select_related("run", "run__client")
            .order_by("hostname", "id"))


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
    """Move the run onto `step` (idempotent; never moves a finished run backwards in status)."""
    if step not in STEP_SLUGS:
        return run
    fields = []
    if run.step != step:
        run.step = step
        fields.append("step")
    status = STEP_STATUS.get(step, run.status)
    if run.status != RunStatus.FAILED and run.status != status:
        # a run that is already complete stays complete when the operator revisits an earlier step
        if not (run.status == RunStatus.COMPLETE and status != RunStatus.COMPLETE):
            run.status = status
            fields.append("status")
    if run.started_at is None and step != "scope":
        run.started_at = timezone.now()
        fields.append("started_at")
    if status == RunStatus.COMPLETE and run.finished_at is None:
        run.finished_at = timezone.now()
        fields.append("finished_at")
    if fields:
        run.save(update_fields=fields + ["updated_at"])
    return run


def fail_run(run, detail=""):
    run.status = RunStatus.FAILED
    run.finished_at = run.finished_at or timezone.now()
    run.save(update_fields=["status", "finished_at", "updated_at"])
    log_step(run, run.step, "error", detail or "Run marked failed")
    return run


def reopen_run(run):
    """A failed run can be picked up again where it stopped."""
    run.status = STEP_STATUS.get(run.step, RunStatus.DRAFT)
    run.finished_at = None
    run.save(update_fields=["status", "finished_at", "updated_at"])
    log_step(run, run.step, "info", "Run reopened")
    return run


def set_target_state(target, state, error="", facts=None, first_sample_at=None):
    """Write one target's outcome. `error` is operator-facing text and never holds a secret."""
    fields = ["state", "last_error", "updated_at"]
    target.state = state
    target.last_error = (error or "")[:500]
    if facts is not None:
        target.facts = {**(target.facts or {}), **facts}
        fields.append("facts")
    if first_sample_at is not None:
        target.first_sample_at = first_sample_at
        fields.append("first_sample_at")
    target.save(update_fields=fields)
    return target


# ----------------------------------------------------------------------------- runs and targets
def create_run(user, kind, site, client=None, environment="prod", notes="", request=None):
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


def _clean_host(value):
    host = (value or "").strip().strip(",;")
    # accept "host:port", "http://host:port" and bare hosts
    if "://" in host:
        host = host.split("://", 1)[1]
    host = host.split("/", 1)[0]
    port = None
    if host.count(":") == 1:
        host, _, raw = host.partition(":")
        try:
            port = int(raw)
        except ValueError:
            port = None
    if not host or len(host) > 255:
        return "", None
    if any(c.isspace() for c in host):
        return "", None
    return host.lower(), port


def parse_target_list(text, default_type=TargetType.LINUX, default_port=None):
    """Parse a pasted list or a CSV into [{hostname, address, target_type, port}], plus the lines
    that could not be read. Accepts "host", "host:port", "host,type", "host,type,port" and a CSV
    with a header row (hostname/address/type/port in any order)."""
    rows, bad = [], []
    text = (text or "")[:MAX_PASTE_BYTES]
    lines = [ln for ln in (text.replace("\r\n", "\n").replace("\r", "\n")).split("\n") if ln.strip()]
    if not lines:
        return rows, bad
    header = None
    first = [c.strip().lower() for c in next(csv.reader([lines[0]]))]
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
            address = ""
            type_cell = (cells[1].lower() if len(cells) > 1 else "")
            port_cell = cells[2] if len(cells) > 2 else ""
        host, inline_port = _clean_host(host_cell)
        if not host:
            bad.append(raw.strip()[:120])
            continue
        if host in seen:
            continue
        seen.add(host)
        ttype = types.get(type_cell, default_type)
        port = None
        try:
            port = int(port_cell) if str(port_cell).strip() else None
        except ValueError:
            port = None
        port = port or inline_port or default_port or DEFAULT_PORTS.get(ttype, 9100)
        if not 1 <= int(port) <= 65535:
            bad.append(raw.strip()[:120])
            continue
        addr, _ = _clean_host(address)
        rows.append({"hostname": host, "address": addr or "", "target_type": ttype, "port": int(port)})
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
                                 ("target_type", ttype), ("port", port)):
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
        target = OnboardingTarget.objects.create(
            run=run, hostname=host, address=row.get("address") or "", target_type=ttype, port=port,
            exporter_kind=DEFAULT_EXPORTERS.get(ttype, ""), state=TargetState.NEW,
        )
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


def attach_credential(run, credential, targets=None, request=None):
    """Point the given targets (or every target of the run) at `credential`. Idempotent."""
    qs = run.targets.all() if targets is None else run.targets.filter(pk__in=[t.pk for t in targets])
    n = qs.update(credential=credential)
    log_step(run, "credentials", "ok",
             f"{credential.get_kind_display()} “{credential.name}” attached to {n} target(s)")
    audit(request, "onboarding.credential.attach", run, site=run.site, credential=credential.pk, targets=n)
    return n


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
    """Decoded text of an uploaded CSV, bounded in size. Returns "" when there is nothing usable."""
    if not uploaded:
        return ""
    data = uploaded.read(MAX_PASTE_BYTES + 1)[:MAX_PASTE_BYTES]
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
