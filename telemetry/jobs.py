"""Periodic jobs registered with core.jobs (run by `manage.py run_probes`). Jobs never raise."""
import logging

log = logging.getLogger("linkedeye.telemetry")

DISCOVERY_INTERVAL = 300    # telemetry.discovery runs every 5 minutes


def prune_alerts():
    """Resolve firing alerts nobody has re-notified for TELEMETRY_ALERT_STALE_HOURS (default 48; their resolved
    notification was lost), then delete resolved Alertmanager alerts older than TELEMETRY_ALERT_RETENTION_DAYS
    (default 90)."""
    from .alerts import expire_stale, prune
    try:
        stale = expire_stale()
    except Exception:
        log.exception("telemetry.expire_stale failed")
        stale = 0
    try:
        n = prune()
    except Exception:
        log.exception("telemetry.prune_alerts failed")
        return "error"
    return f"pruned {n} resolved alert(s)" + (f", resolved {stale} stale firing alert(s)" if stale else "")


def discover_targets():
    """Gather Prometheus scrape targets and what they report about themselves (telemetry.discovery.run)."""
    from .discovery import run
    try:
        row = run(trigger="job")
    except Exception:
        log.exception("telemetry.discovery failed")
        return "error"
    return row.summary
