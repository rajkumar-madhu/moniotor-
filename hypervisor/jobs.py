"""Periodic jobs registered in ``HypervisorConfig.ready()`` (run by ``manage.py run_probes``). Jobs never raise."""
import logging

log = logging.getLogger("linkedeye.hypervisor")

SYNC_INTERVAL = 60
RETENTION_INTERVAL = 6 * 3600


def sync():
    from . import sync as sync_mod
    try:
        return sync_mod.sync_due()
    except Exception:                               # noqa: BLE001
        log.exception("hypervisor.sync failed")
        return "error"


def retention():
    from . import sync as sync_mod
    try:
        out = sync_mod.retention()
        return ", ".join(f"{k} {v}" for k, v in out.items())
    except Exception:                               # noqa: BLE001
        log.exception("hypervisor.retention failed")
        return "error"
