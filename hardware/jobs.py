"""Periodic jobs registered in ``HardwareConfig.ready()`` (run by ``manage.py run_probes``). Jobs never raise."""
import logging

log = logging.getLogger("linkedeye.hardware")

POLL_INTERVAL = 30          # the scheduler looks for due endpoints this often; each endpoint has its own poll interval
POLL_BUDGET = 30            # expected seconds of one pass (network reads run in a few worker threads)
RETENTION_INTERVAL = 3600


def poll():
    from . import services
    try:
        return services.poll_due()
    except Exception:                               # noqa: BLE001
        log.exception("hardware.poll failed")
        return "error"


def retention():
    from . import services
    try:
        out = services.retention()
        return ", ".join(f"{k} {v}" for k, v in out.items())
    except Exception:                               # noqa: BLE001
        log.exception("hardware.retention failed")
        return "error"
