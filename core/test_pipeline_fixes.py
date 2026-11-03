"""Regression tests: notifications off the caller's thread (F04b), job pass budget / persisted job state (F04d,
F23) and the scheduler lease (F23)."""
import threading
import time
from datetime import timedelta
from unittest import mock

from django.conf import settings
import unittest

from django.db import connection, transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from core import jobs, notify
from core.events import emit
from core.models import JobState, SchedulerLease

HOOK = override_settings(NOTIFY_WEBHOOK_URLS=["http://hook.example/x"], NOTIFY_EMAILS=[], INCIDENT_AUTO_CREATE=False,
                         NOTIFY_SEVERITIES=["critical", "warning"])


class EmailTimeoutTests(TestCase):
    def test_smtp_has_a_timeout(self):
        self.assertEqual(settings.EMAIL_TIMEOUT, 10)


@HOOK
class NotifyTests(TestCase):
    def setUp(self):
        notify.reset()
        self.addCleanup(notify.reset)

    @override_settings(NOTIFY_ASYNC=True, NOTIFY_DIGEST_THRESHOLD=0)
    def test_slow_webhook_does_not_block_the_emitter(self):
        release = threading.Event()
        calls = []

        def slow_post(url, payload, timeout=5):
            calls.append(payload["title"])
            release.wait(5)
            return 200

        with mock.patch("core.notify._post", side_effect=slow_post):
            t0 = time.monotonic()
            with self.captureOnCommitCallbacks(execute=True):
                emit("test", "Down", severity="critical")
            self.assertLess(time.monotonic() - t0, 1.0)   # the webhook still hangs, the emitter is already back
            release.set()
            self.assertEqual(notify.flush(timeout=5), 0)
        self.assertEqual(calls, ["Down"])

    @override_settings(NOTIFY_ASYNC=False)
    def test_rolled_back_event_never_notifies(self):
        with mock.patch("core.notify._post") as post, self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    emit("test", "Phantom", severity="critical")
                    raise RuntimeError("probe transaction failed")
            except RuntimeError:
                pass
            emit("test", "Real", severity="critical")
        self.assertEqual([c.args[1]["title"] for c in post.call_args_list], ["Real"])

    @override_settings(NOTIFY_ASYNC=False, NOTIFY_DIGEST_THRESHOLD=3, NOTIFY_DIGEST_WINDOW=60)
    def test_flood_is_collapsed_into_one_digest(self):
        clock = [1000.0]
        with mock.patch("core.notify._clock", side_effect=lambda: clock[0]), mock.patch("core.notify._post") as post:
            with self.captureOnCommitCallbacks(execute=True):
                for i in range(8):
                    emit("test", f"Down {i}", severity="critical")
                emit("test", "Slow", severity="warning")       # other severities have their own window
            self.assertEqual([c.args[1]["title"] for c in post.call_args_list],
                             ["Down 0", "Down 1", "Down 2", "Slow"])
            clock[0] += 61
            self.assertEqual(notify.flush(timeout=1), 0)
        titles = [c.args[1]["title"] for c in post.call_args_list]
        self.assertEqual(len(titles), 5)
        self.assertEqual(titles[-1], "5 more critical events in the last 60 s")
        self.assertEqual(post.call_args_list[-1].args[1]["titles"], [f"Down {i}" for i in range(3, 8)])

    @override_settings(NOTIFY_ASYNC=True, NOTIFY_QUEUE_SIZE=1, NOTIFY_WORKERS=1, NOTIFY_DIGEST_THRESHOLD=0)
    def test_full_queue_drops_with_warning(self):
        release = threading.Event()
        ev = mock.Mock(severity="critical", title="x", message="", source="t", site="", kind="k", object_type="",
                       object_id="", occurred_at=timezone.now())
        with mock.patch("core.notify._post", side_effect=lambda *a, **k: release.wait(5)) as post, \
                self.assertLogs("linkedeye.notify", "WARNING") as logs:
            notify.dispatch(ev)
            notify.dispatch(ev)
            notify.dispatch(ev)
            release.set()
            notify.flush(timeout=5)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(notify.stats()["dropped"], 2)
        self.assertIn("notification queue full", "\n".join(logs.output))


class JobBudgetTests(TestCase):
    def setUp(self):
        p = mock.patch.dict(jobs.JOBS, clear=True)
        p.start()
        self.addCleanup(p.stop)
        self.ran = []

    def job(self, name, interval=60, **kw):
        jobs.register(name, interval, lambda: self.ran.append(name) or "ok", **kw)

    def test_most_overdue_first(self):
        self.job("a.fast", 15)
        self.job("b.hourly", 3600)
        jobs.run_due(now=10_000.0)
        self.ran.clear()
        # a.fast is 3585 s overdue, b.hourly 0 s
        jobs.run_due(now=10_000.0 + 3600)
        self.assertEqual(self.ran, ["a.fast", "b.hourly"])

    @override_settings(SCHEDULER_PASS_BUDGET=60, SCHEDULER_CRITICAL_JOBS=["c.critical"])
    def test_over_budget_pass_defers_noncritical_jobs_to_the_next_pass(self):
        for n in ("a.one", "b.two", "c.critical", "d.three"):
            self.job(n)
        long_ago = time.monotonic() - 120   # the probe pass already used 120 s
        with self.assertLogs("linkedeye.jobs", "WARNING") as logs:
            done = jobs.run_due(now=5000.0, started=long_ago)
        # one non-critical job always runs (no starvation), critical jobs always run, the rest wait
        self.assertEqual(self.ran, ["a.one", "c.critical"])
        self.assertEqual(set(done), {"a.one", "c.critical"})
        self.assertIn("b.two, d.three", "\n".join(logs.output))
        self.ran.clear()
        jobs.run_due(now=5015.0)   # within budget: the deferred jobs run, most overdue first
        self.assertEqual(self.ran, ["b.two", "d.three"])

    def test_job_budget_skips_a_job_that_would_not_fit(self):
        self.job("a.one")
        self.job("b.slow", budget=50)
        with override_settings(SCHEDULER_PASS_BUDGET=60), self.assertLogs("linkedeye.jobs", "WARNING"):
            jobs.run_due(now=100.0, started=time.monotonic() - 20)
        self.assertEqual(self.ran, ["a.one"])

    def test_last_run_survives_a_restart(self):
        self.job("x.hourly", 3600)
        jobs.run_due(now=1_800_000_000.0)
        self.assertTrue(JobState.objects.filter(name="x.hourly", tenant_key=settings.LINKEDEYE_TENANT).exists())
        jobs.JOBS["x.hourly"]["last"] = None    # a new process (e.g. run_probes --once from cron)
        jobs.run_due(now=1_800_000_000.0 + 600)
        self.assertEqual(self.ran, ["x.hourly"])
        jobs.run_due(now=1_800_000_000.0 + 3601)
        self.assertEqual(self.ran, ["x.hourly", "x.hourly"])


def force_lease_row(test):
    """Lease picks the advisory lock on PostgreSQL; tests of the lease-row path pin it to the row path."""
    p = mock.patch.object(jobs.Lease, "_vendor", return_value="sqlite")
    p.start()
    test.addCleanup(p.stop)


class LeaseRowTests(TestCase):
    def setUp(self):
        force_lease_row(self)

    def test_second_instance_waits_until_the_lease_expires(self):
        a, b = jobs.Lease(owner="host-a:1"), jobs.Lease(owner="host-b:2")
        self.assertTrue(a.acquire())
        self.assertFalse(b.acquire())
        self.assertIn("host-a:1", b.holder)
        self.assertTrue(a.acquire())   # renewal
        SchedulerLease.objects.update(expires_at=timezone.now() - timedelta(seconds=1))   # a stopped renewing
        self.assertTrue(b.acquire())
        self.assertFalse(a.acquire())
        self.assertFalse(a.held)

    def test_release_hands_over_at_once(self):
        a, b = jobs.Lease(owner="host-a:1"), jobs.Lease(owner="host-b:2")
        a.acquire()
        a.release()
        self.assertTrue(b.acquire())

    def test_database_error_keeps_a_still_valid_lease(self):
        a = jobs.Lease(owner="host-a:1")
        a.acquire()
        with mock.patch.object(jobs.Lease, "_acquire_row", side_effect=RuntimeError("db down")), \
                self.assertLogs("linkedeye.jobs", "WARNING"):
            self.assertTrue(a.acquire())
            a._valid_until = time.monotonic() - 1
            self.assertFalse(a.acquire())


class LeaseAdvisoryTests(TestCase):
    """PostgreSQL path, with a fake dedicated connection."""

    def fake_conn(self, granted):
        conn = mock.MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (granted,)
        return conn, cur

    def test_advisory_lock_on_a_dedicated_connection(self):
        conn, cur = self.fake_conn(True)
        with mock.patch.object(jobs.Lease, "_vendor", return_value="postgresql"), \
                mock.patch("django.db.connections.create_connection", return_value=conn):
            lease = jobs.Lease()
            self.assertTrue(lease.acquire())
            cur.execute.assert_called_with("SELECT pg_try_advisory_lock(%s)", [lease.advisory_key])
            self.assertTrue(lease.acquire())          # renewal = the session is still alive
            cur.execute.assert_called_with("SELECT 1")
            lease.release()
        cur.execute.assert_called_with("SELECT pg_advisory_unlock(%s)", [lease.advisory_key])
        conn.close.assert_called()

    def test_lock_held_elsewhere(self):
        conn, _cur = self.fake_conn(False)
        with mock.patch.object(jobs.Lease, "_vendor", return_value="postgresql"), \
                mock.patch("django.db.connections.create_connection", return_value=conn):
            lease = jobs.Lease()
            self.assertFalse(lease.acquire())
        conn.close.assert_called()
        self.assertIn("advisory lock", lease.holder)

    def test_lost_session_drops_the_lease(self):
        conn, cur = self.fake_conn(True)
        with mock.patch.object(jobs.Lease, "_vendor", return_value="postgresql"), \
                mock.patch("django.db.connections.create_connection", return_value=conn):
            lease = jobs.Lease()
            lease.acquire()
            cur.execute.side_effect = OSError("server closed the connection")
            with self.assertLogs("linkedeye.jobs", "WARNING"):
                self.assertFalse(lease.acquire())
        self.assertFalse(lease.held)


@unittest.skipUnless(connection.vendor == "postgresql", "PostgreSQL advisory locks")
class PostgresAdvisoryLockTests(TestCase):
    """The real PostgreSQL path: each Lease holds pg_try_advisory_lock on its own dedicated connection, so this works
    inside the test transaction."""

    def test_only_one_holder_and_release_hands_over(self):
        a, b = jobs.Lease(name="test:advisory", owner="a"), jobs.Lease(name="test:advisory", owner="b")
        self.addCleanup(a.release)
        self.addCleanup(b.release)
        self.assertTrue(a.acquire())
        self.assertFalse(b.acquire())
        self.assertTrue(a.acquire())       # renewal: the session is still alive
        a.release()
        self.assertTrue(b.acquire())
        self.assertFalse(a.acquire())

    def test_a_dead_holder_frees_the_lock_at_once(self):
        a, b = jobs.Lease(name="test:advisory-crash", owner="a"), jobs.Lease(name="test:advisory-crash", owner="b")
        self.addCleanup(b.release)
        self.assertTrue(a.acquire())
        a._conn.close()                    # the holder's process died: its session (and lock) is gone
        a._conn = None
        self.assertTrue(b.acquire())
