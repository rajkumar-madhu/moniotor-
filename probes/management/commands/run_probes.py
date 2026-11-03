"""manage.py run_probes — the LinkedEye scheduler.

Every --tick seconds a pass runs the due URL/domain probes, then the registered periodic jobs (core.jobs, with a
per-pass time budget) and hourly pruning. Alongside the passes a daemon heartbeat thread, independent of how long a
pass takes, renews the scheduler lease and records the scheduler heartbeat every SCHEDULER_HEARTBEAT_SECONDS.

Only one instance per database runs passes (core.jobs.Lease). A second instance idles as a standby, logging that
another scheduler holds the lease, and takes over when the lease is released or expires.
"""
import logging
import signal
import threading
import time

from django.apps import apps
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections, connections
from django.utils import timezone

from core import jobs, notify
from probes import runner

log = logging.getLogger("linkedeye.probes")


class Loop:
    """State shared between the main loop and the heartbeat thread."""

    def __init__(self):
        self.started_at = timezone.now()
        self.loop_at = self.started_at        # last time the main loop finished a pass (or started)
        self.pass_started_at = None           # set while a pass is running
        self.passes = 0

    def data(self, lease):
        iso = lambda v: v.isoformat() if v else None  # noqa: E731
        return {"started_at": iso(self.started_at), "loop_at": iso(self.loop_at),
                "pass_started_at": iso(self.pass_started_at), "passes": self.passes,
                "lease": "disabled" if lease is None else ("held" if lease.held else "standby"),
                "notify": notify.stats()}


def record_beat(lease, loop):
    if not apps.is_installed("administration"):
        return False
    from administration.jobs import beat
    return beat(active=lease is None or lease.held, data=loop.data(lease))


class HeartbeatThread(threading.Thread):
    """Renews the lease (or tries to take it) and writes the heartbeat every `interval` seconds."""

    def __init__(self, lease, loop, interval):
        super().__init__(name="le-heartbeat", daemon=True)
        self.lease, self.loop, self.interval = lease, loop, interval
        self.stopped = threading.Event()

    def tick(self):
        try:
            close_old_connections()
            if self.lease is not None:
                self.lease.acquire()
            record_beat(self.lease, self.loop)
        except Exception:  # the heartbeat thread must survive anything
            log.exception("scheduler heartbeat failed")

    def run(self):
        try:
            while not self.stopped.wait(self.interval):
                self.tick()
        finally:
            connections.close_all()

    def stop(self):
        self.stopped.set()


class Command(BaseCommand):
    help = "Run URL and domain checks that are due. Loops until stopped unless --once is given."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Run due checks once and exit.")
        parser.add_argument("--tick", type=int, default=15, help="Seconds between scheduler passes (default 15).")

    def run_pass(self, loop, prune_state):
        close_old_connections()
        started = time.monotonic()
        loop.pass_started_at = timezone.now()
        try:
            urls, domains = runner.run_due()
            if urls or domains:
                self.stdout.write(f"checked {urls} URL(s), {domains} domain(s) in {time.monotonic() - started:.1f}s")
        except Exception:  # a failed probe pass must not skip the jobs and pruning below
            log.exception("probe pass failed")
        try:
            for name, summary in jobs.run_due(started=started).items():
                if summary:
                    self.stdout.write(f"{name}: {summary}")
            if time.monotonic() - prune_state["last"] > 3600:
                removed = runner.prune()
                prune_state["last"] = time.monotonic()
                if removed:
                    self.stdout.write(f"pruned {removed} old result(s)")
        except Exception:  # keep the scheduler alive; the error is logged with traceback
            log.exception("jobs/prune pass failed")
        loop.pass_started_at = None
        loop.loop_at = timezone.now()
        loop.passes += 1
        return started

    def standby_message(self, lease):
        msg = f"another scheduler holds the lease ({lease.holder or 'unknown holder'}); standing by"
        log.warning(msg)
        self.stdout.write(msg)

    def handle(self, *args, once=False, tick=15, **opts):
        stop = threading.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())
        lease = jobs.Lease() if getattr(settings, "SCHEDULER_LEASE", True) else None
        loop = Loop()
        prune_state = {"last": 0.0}
        if once:
            try:
                if lease is not None and not lease.acquire():
                    self.standby_message(lease)
                    return
                self.run_pass(loop, prune_state)
                record_beat(lease, loop)
            finally:
                if lease is not None:
                    lease.release()
                pending = notify.flush(timeout=30)   # --once exits right away: deliver what this pass queued
                if pending:
                    log.warning("%s notification(s) were still pending when run_probes --once exited", pending)
            return

        heart = HeartbeatThread(lease, loop, getattr(settings, "SCHEDULER_HEARTBEAT_SECONDS", 30))
        heart.tick()      # take the lease (or learn who has it) and beat before the first pass
        heart.start()
        standby = False
        try:
            while not stop.is_set():
                if lease is not None and not lease.held:
                    if not standby:
                        self.standby_message(lease)
                        standby = True
                    stop.wait(min(tick, heart.interval))
                    continue
                if standby:
                    log.info("scheduler lease acquired; running passes")
                    self.stdout.write("scheduler lease acquired; running passes")
                    standby = False
                started = self.run_pass(loop, prune_state)
                while time.monotonic() - started < tick and not stop.is_set():
                    stop.wait(0.5)
        finally:
            heart.stop()
            heart.join(timeout=10)   # so it cannot renew the lease after we released it
            if lease is not None:
                lease.release()
            notify.shutdown(timeout=10)
