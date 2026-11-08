"""Runs TCP checks, stores results and emits events on status changes.

Network I/O happens in a thread pool; every database write happens on the
calling (job) thread, which keeps this safe on SQLite as well as MySQL/PostgreSQL.
"""
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from django.conf import settings
from django.db import DatabaseError, transaction
from django.utils import timezone

from core.access import PERMS, RANK, can, role_for, scope_by_site
from core.events import emit

from .models import Status, TcpCheck, TcpResult
from .probe import tcp_probe

log = logging.getLogger("linkedeye.tcpcheck")

MAX_PER_PASS = 500          # bound one scheduler pass; the rest run on the next tick
SEVERITY = {Status.DOWN: "critical", Status.DEGRADED: "warning", Status.ONLINE: "ok"}


def tenant():
    return settings.LINKEDEYE_TENANT


# ------------------------------------------------------------------ access helpers
def checks_for(user):
    return scope_by_site(TcpCheck.objects.filter(tenant_key=tenant()), user)


def can_manage(user, perm, site):
    """`perm` on the record's site. Records for "All Sites" need the permission tenant-wide."""
    if site and site != "All Sites":
        return can(user, perm, site_name=site)
    if not can(user, perm):
        return False
    if user.is_superuser:
        return True
    from core.access import _memberships
    ms = _memberships(user)
    if not ms:  # users without memberships hold their default role everywhere
        return True
    return any(m.site is None and RANK[m.role] >= PERMS[perm] for m in ms)


def role_label(user):
    return (role_for(user) or "viewer").title()


# ------------------------------------------------------------------ evaluation
def fmt_ms(v):
    """Connect time for messages; matches the connect_ms template filter ("<1 ms" below half a millisecond)."""
    return "<1 ms" if v < 0.5 else f"{v:.0f} ms"


def evaluate(check, r):
    """Map a raw probe result to (status, message)."""
    if not r.ok:
        return Status.DOWN, r.error or "Connection failed"
    if check.expect_banner and not r.banner_matched:
        if r.error:
            return Status.DOWN, r.error
        got = f" (received “{r.banner[:60]}”)" if r.banner else " (nothing received)"
        return Status.DOWN, f"Banner did not contain “{check.expect_banner}”{got}"
    if r.response_ms is not None and r.response_ms > check.degraded_ms:
        return Status.DEGRADED, f"Slow connect ({fmt_ms(r.response_ms)} > {check.degraded_ms} ms)"
    msg = f"Connected in {fmt_ms(r.response_ms or 0)}"
    if check.expect_banner:
        msg += "; banner matched"
    return Status.ONLINE, msg


def _probe(check):
    try:
        return tcp_probe(check.host, check.port, check.timeout_seconds, check.expect_banner)
    except Exception as e:  # never let one bad target kill the pool
        log.exception("tcp probe crashed for %s", check.target)
        from .probe import ProbeResult
        return ProbeResult(error=f"Probe error: {type(e).__name__}: {e}"[:500])


def save_result(check, r, now):
    """Store the result; returns the new status, or None if the check was deleted while it was probed."""
    from probes.runner import Gone, store_status
    status, message = evaluate(check, r)
    fields = {"last_status": status, "last_checked_at": now, "last_response_ms": r.response_ms if r.ok else None,
              "last_error": "" if status == Status.ONLINE else message[:500], "last_ip": r.ip or None}
    if r.banner or not check.expect_banner:
        fields["last_banner"] = r.banner[:200]
    try:
        with transaction.atomic():
            TcpResult.objects.create(tcp_check=check, checked_at=now, status=status,
                                     response_ms=r.response_ms if r.ok else None, ip=r.ip or None,
                                     banner=r.banner[:200], message=message[:500])
            # conditional on the status this copy last saw: a stale copy (a scheduler pass racing an interactive
            # "Run now") must not emit the same transition twice
            previous = store_status(TcpCheck, check.pk, "last_status", status, check.last_status, fields)
    except (Gone, DatabaseError):
        if TcpCheck.objects.filter(pk=check.pk).exists():
            raise
        log.warning("tcp check %s (pk=%s) is gone; result discarded", check.name, check.pk)
        return None
    for k, v in fields.items():
        setattr(check, k, v)
    if previous != status:
        log.info("tcp %s (%s): %s -> %s (%s)", check.name, check.target, previous, status, message)
        # the first result of a new check is only news when it is a problem
        if previous != Status.UNKNOWN or status != Status.ONLINE:
            label = Status(status).label
            emit("tcpcheck", f"{check.name} ({check.target}) is {label}", severity=SEVERITY.get(status, "info"),
                 message=message, site=check.site, obj=check,
                 fingerprint=f"tcp:{check.pk}", previous=str(previous), status=str(status), host=check.host,
                 port=check.port, response_ms=r.response_ms)
    return status


def run_checks(items, workers=None, now=None):
    """Probe the checks in parallel, then store every result on this thread. Returns statuses in order.
    `workers` overrides settings.PROBE_WORKERS (interactive bulk runs use more, to bound the request time).
    `now` is the time the results are stamped with: the scheduler passes the time of its pass, so last_checked_at
    is not pushed forward by the batch's own duration (the next pass one interval later would then find the
    check not quite due and run it a tick late, every time)."""
    items = list(items)
    if not items:
        return []
    now = now or timezone.now()   # before the probes run, not after the slowest one timed out
    with ThreadPoolExecutor(max_workers=max(1, min(workers or settings.PROBE_WORKERS, len(items)))) as pool:
        raw = list(pool.map(_probe, items))
    out = []
    for c, r in zip(items, raw):
        try:
            out.append(save_result(c, r, now) or Status.UNKNOWN)   # None: deleted while probed
        except Exception:
            log.exception("could not store tcp result for %s", c.pk)
            out.append(Status.UNKNOWN)
    return out


# ------------------------------------------------------------------ jobs
def run_due(now=None):
    """Job: run every enabled check whose interval elapsed. Never raises."""
    try:
        now = now or timezone.now()
        due = [c for c in TcpCheck.objects.filter(tenant_key=tenant(), enabled=True) if c.is_due(now)]
        due.sort(key=lambda c: c.last_checked_at or now - timedelta(days=3650))
        due = due[:MAX_PER_PASS]
        statuses = run_checks(due, now=now)
        if not due:
            return None
        down = sum(1 for s in statuses if s == Status.DOWN)
        return f"checked {len(due)} TCP target(s), {down} down"
    except Exception:
        log.exception("tcpcheck.run_due failed")
        return "error"


def prune(now=None):
    """Job: delete results older than PROBE_RETENTION_DAYS. Never raises."""
    try:
        cutoff = (now or timezone.now()) - timedelta(days=settings.PROBE_RETENTION_DAYS)
        # this tenant's history only: other tenants in a shared database follow their own retention setting
        n, _ = TcpResult.objects.filter(tcp_check__tenant_key=tenant(), checked_at__lt=cutoff).delete()
        return f"pruned {n} TCP result(s)" if n else None
    except Exception:
        log.exception("tcpcheck.prune failed")
        return "error"
