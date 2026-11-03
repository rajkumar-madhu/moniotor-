"""Periodic job registry used by `manage.py run_probes`.

Apps register jobs in AppConfig.ready():
    from core.jobs import register
    register("network.poll", 60, poll_devices)
Each job is a callable taking no arguments and returning a short summary (or None).

Scheduling (run_due):
* due jobs run most-overdue first (never-run jobs first), so a job skipped by the pass budget runs early next pass;
* pass budget: once the pass (measured from `started`, normally the start of run_probes' pass including the URL/
  domain probes) has used settings.SCHEDULER_PASS_BUDGET seconds, the remaining non-critical jobs are skipped to the
  next pass. A job may declare `budget` (its expected run time, seconds): it is skipped when it would not fit in
  what is left. Critical jobs (critical=True or named in settings.SCHEDULER_CRITICAL_JOBS) always run, and every
  pass runs at least one non-critical job, so nothing starves;
* last-run times are kept in core.JobState (wall clock), so a restarted scheduler or `run_probes --once` from cron
  does not re-run hourly jobs on every start. When the database is unavailable the in-memory times are used.
"""
import hashlib
import logging
import os
import socket
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone as dt_timezone

from django.conf import settings

log = logging.getLogger("linkedeye.jobs")

JOBS = {}


def register(name, interval_seconds, func, critical=False, budget=None):
    JOBS[name] = {"interval": interval_seconds, "func": func, "last": None, "critical": critical, "budget": budget}


def _is_critical(name, job):
    return bool(job.get("critical")) or name in getattr(settings, "SCHEDULER_CRITICAL_JOBS", ())


def _wall():
    return time.time()


def _load_state():
    """{name: last run (epoch seconds)} from core.JobState, or None when it cannot be read."""
    try:
        from .models import JobState
        rows = JobState.objects.filter(tenant_key=settings.LINKEDEYE_TENANT, name__in=list(JOBS)).values_list(
            "name", "last_run_at")
        return {n: at.timestamp() for n, at in rows if at is not None}
    except Exception:
        log.warning("could not read job state; using in-memory last-run times", exc_info=True)
        return None


def _save_state(name, started, duration):
    try:
        from .models import JobState
        at = datetime.fromtimestamp(started, tz=dt_timezone.utc)
        n = JobState.objects.filter(tenant_key=settings.LINKEDEYE_TENANT, name=name).update(
            last_run_at=at, last_duration_ms=round(duration * 1000, 1))
        if not n:
            JobState.objects.get_or_create(tenant_key=settings.LINKEDEYE_TENANT, name=name,
                                           defaults={"last_run_at": at, "last_duration_ms": round(duration * 1000, 1)})
    except Exception:
        log.warning("could not store job state for %s", name, exc_info=True)


def due_jobs(now, state=None):
    """[(overdue seconds, name, job)] of due jobs, most overdue first (never-run jobs count as infinitely overdue)."""
    out = []
    for name, job in JOBS.items():
        last = job["last"]
        if state is not None and state.get(name) is not None:
            last = state[name] if last is None else max(last, state[name])
        if last is not None and last > now + 60:   # the wall clock went backwards: do not stall until it catches up
            last = None
        if last is not None and now - last < job["interval"]:
            continue
        overdue = float("inf") if last is None else now - last - job["interval"]
        out.append((overdue, name, job))
    out.sort(key=lambda x: (-x[0], x[1]))
    return out


def run_due(now=None, started=None, budget=None, persist=True):
    """Run registered jobs whose interval elapsed. Returns {name: summary}.

    now: wall-clock seconds (epoch), defaults to time.time(). started: time.monotonic() at the start of the pass
    (defaults to now). budget: seconds (defaults to settings.SCHEDULER_PASS_BUDGET). persist: read/write JobState."""
    now = _wall() if now is None else now
    started = time.monotonic() if started is None else started
    budget = getattr(settings, "SCHEDULER_PASS_BUDGET", 60) if budget is None else budget
    state = _load_state() if persist else None
    done, skipped, ran_noncritical = {}, [], False
    for _overdue, name, job in due_jobs(now, state):
        critical = _is_critical(name, job)
        elapsed = time.monotonic() - started
        need = job.get("budget") or 0
        if not critical and ran_noncritical and budget and elapsed + need > budget:
            skipped.append(name)
            continue
        t0 = time.monotonic()
        job["last"] = now
        try:
            done[name] = job["func"]()
        except Exception:
            log.exception("job %s failed", name)
            done[name] = "error"
        duration = time.monotonic() - t0
        if not critical:
            ran_noncritical = True
        if job.get("budget") and duration > job["budget"]:
            log.warning("job %s took %.1fs (budget %.1fs)", name, duration, job["budget"])
        if persist:
            _save_state(name, now, duration)
    if skipped:
        log.warning("scheduler pass over budget (%.0fs of %.0fs): deferred %s to the next pass",
                    time.monotonic() - started, budget, ", ".join(skipped))
    return done


# ------------------------------------------------------------------ singleton guard
class Lease:
    """Only one `run_probes` per database (and tenant) runs scheduler passes.

    PostgreSQL: pg_try_advisory_lock on a dedicated connection held for the life of the process (the lock goes away
    with the session, so a crashed holder frees it at once; needs a session-mode pooler, not PgBouncer transaction
    mode). Other databases: a core.SchedulerLease row taken with a conditional UPDATE once it has expired, renewed by
    the caller (run_probes' heartbeat thread) every SCHEDULER_HEARTBEAT_SECONDS; a dead holder's lease expires after
    SCHEDULER_LEASE_TTL seconds. acquire() both takes and renews; it never raises. Thread-safe."""

    def __init__(self, name=None, ttl=None, owner=None):
        self.name = (name or f"scheduler:{settings.LINKEDEYE_TENANT}")[:100]
        self.ttl = ttl or getattr(settings, "SCHEDULER_LEASE_TTL", 90)
        self.owner = owner or f"{socket.gethostname()[:150]}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.held = False
        self.holder = ""            # current holder when we do not hold it (lease row only)
        self._lock = threading.Lock()
        self._conn = None           # PostgreSQL: dedicated connection that owns the advisory lock
        self._valid_until = 0.0     # lease row: monotonic time our lease is known valid until

    @property
    def advisory_key(self):
        return int.from_bytes(hashlib.sha256(self.name.encode()).digest()[:8], "big", signed=True)

    @staticmethod
    def _vendor():
        from django.db import connection
        return connection.vendor

    def acquire(self):
        with self._lock:
            try:
                held = self._acquire_pg() if self._vendor() == "postgresql" else self._acquire_row()
            except Exception:
                log.warning("scheduler lease check failed", exc_info=True)
                # keep a row lease only while it is still valid; an advisory lock is gone with its connection
                held = self._conn is None and self.held and time.monotonic() < self._valid_until
                if self._conn is not None:
                    self._drop_conn()
            if held != self.held:
                log.info("scheduler lease %s %s by %s", self.name, "acquired" if held else "lost", self.owner)
            self.held = held
            return held

    # ---- PostgreSQL
    def _drop_conn(self):
        try:
            self._conn.close()
        except Exception:
            pass
        self._conn = None

    def _acquire_pg(self):
        from django.db import DEFAULT_DB_ALIAS, connections
        if self._conn is not None:
            with self._conn.cursor() as cur:   # still connected = still holding the session lock
                cur.execute("SELECT 1")
            return True
        conn = connections.create_connection(DEFAULT_DB_ALIAS)
        conn.inc_thread_sharing()   # renewed from the heartbeat thread, released from the main thread
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(%s)", [self.advisory_key])
                ok = bool(cur.fetchone()[0])
        except Exception:
            conn.close()
            raise
        if ok:
            self._conn = conn
        else:
            conn.close()
            self.holder = "another session (PostgreSQL advisory lock)"
        return ok

    # ---- lease row
    def _acquire_row(self):
        from django.db import IntegrityError, transaction
        from django.db.models import Q
        from django.utils import timezone

        from .models import SchedulerLease
        now = timezone.now()
        expires = now + timedelta(seconds=self.ttl)
        mine = SchedulerLease.objects.filter(name=self.name, owner=self.owner).update(expires_at=expires)
        if not mine:
            mine = SchedulerLease.objects.filter(name=self.name).filter(Q(expires_at__lt=now) | Q(owner="")).update(
                owner=self.owner, acquired_at=now, expires_at=expires)
        if not mine:
            try:
                with transaction.atomic():
                    SchedulerLease.objects.create(name=self.name, owner=self.owner, acquired_at=now, expires_at=expires)
                mine = 1
            except IntegrityError:
                row = SchedulerLease.objects.filter(name=self.name).values("owner", "expires_at").first() or {}
                self.holder = f"{row.get('owner', '?')} (lease until {row.get('expires_at')})"
        if mine:
            self._valid_until = time.monotonic() + self.ttl
        return bool(mine)

    def release(self):
        with self._lock:
            try:
                if self._conn is not None:
                    with self._conn.cursor() as cur:
                        cur.execute("SELECT pg_advisory_unlock(%s)", [self.advisory_key])
                    self._drop_conn()
                elif self.held:
                    from django.utils import timezone

                    from .models import SchedulerLease
                    SchedulerLease.objects.filter(name=self.name, owner=self.owner).update(
                        owner="", expires_at=timezone.now())
            except Exception:
                log.warning("could not release scheduler lease", exc_info=True)
            self.held = False
