"""Periodic jobs registered in ``ApmConfig.ready()`` (run by ``manage.py run_probes``). Jobs never raise."""
import logging

log = logging.getLogger("linkedeye.apm")

REDIS_INTERVAL = 60
STALE_INTERVAL = 60
RETENTION_INTERVAL = 6 * 3600


def redis_poll():
    from . import redis_reader
    try:
        return redis_reader.poll_all()
    except Exception:                               # noqa: BLE001
        log.exception("apm.redis_poll failed")
        return "error"


def stale():
    from . import services
    try:
        out = services.detect_stale()
        services.expire_retries()
        return f"{out['silent']} source(s) newly silent"
    except Exception:                               # noqa: BLE001
        log.exception("apm.stale failed")
        return "error"


def retention():
    from . import services
    try:
        out = services.retention()
        return ", ".join(f"{k} {v}" for k, v in out.items())
    except Exception:                               # noqa: BLE001
        log.exception("apm.retention failed")
        return "error"
