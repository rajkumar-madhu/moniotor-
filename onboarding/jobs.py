"""Periodic jobs registered with core.jobs (run by `manage.py run_probes`). Jobs never raise."""
import logging

log = logging.getLogger("linkedeye.onboarding")

LIVENESS_INTERVAL = 60       # onboarding.liveness: promote waiting targets once up == 1
HOUSEKEEPING_INTERVAL = 6 * 3600


def liveness():
    """Promote scraping targets to live once Prometheus reports up == 1 with samples (stamping
    first_sample_at), and complete (and announce) every run whose targets are all live."""
    from .activate import poll
    out = poll()
    if out.get("note") == "error":
        return "error"
    text = f"checked {out['checked']}, {out['live']} newly live, {out['completed']} run(s) complete"
    return f"{text} ({out['note']})" if out.get("note") else text


def housekeeping():
    """Drop old service-discovery fetch records."""
    from .services import prune_fetches
    try:
        n = prune_fetches()
    except Exception:
        log.exception("onboarding.housekeeping failed")
        return "error"
    return f"pruned {n} fetch record(s)"
