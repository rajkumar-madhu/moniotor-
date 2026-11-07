"""Periodic jobs registered with core.jobs (run by `manage.py run_probes`). Jobs never raise."""
import logging

log = logging.getLogger("linkedeye.telemetry")

DISCOVERY_INTERVAL = 300    # telemetry.discovery runs every 5 minutes


def prune_alerts():
    """Delete resolved Alertmanager alerts older than TELEMETRY_ALERT_RETENTION_DAYS (default 90)."""
    from .alerts import prune
    try:
        n = prune()
    except Exception:
        log.exception("telemetry.prune_alerts failed")
        return "error"
    return f"pruned {n} resolved alert(s)"


def discover_targets():
    """Gather Prometheus scrape targets and what they report about themselves (telemetry.discovery.run)."""
    from .discovery import run
    try:
        row = run(trigger="job")
    except Exception:
        log.exception("telemetry.discovery failed")
        return "error"
    return row.summary
