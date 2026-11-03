"""Regression tests: duplicate transition events from stale copies (F11), the run_probes singleton lease and heartbeat
thread (F04c, F23), and the probes container healthcheck that goes with them."""
import importlib.util
import threading
import time
from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from administration.jobs import standby_key
from administration.models import Heartbeat
from core import jobs
from core.events import emit
from core.models import Event, SchedulerLease
from domainmonitor.models import Domain
from probes import checks, runner
from probes.management.commands import run_probes
from urlchecker.models import UrlCheck

NO_INCIDENTS = override_settings(INCIDENT_AUTO_CREATE=False, NOTIFY_WEBHOOK_URLS=[], NOTIFY_EMAILS=[])


@NO_INCIDENTS
class StaleTransitionTests(TestCase):
    def test_url_transition_is_emitted_once_by_two_writers(self):
        c = UrlCheck.objects.create(name="svc", url="https://example.com/", site="All Sites", last_status="online",
                                    last_checked_at=timezone.now() - timedelta(hours=1))
        worker_copy = UrlCheck.objects.get(pk=c.pk)   # loaded by the scheduler pass
        web_copy = UrlCheck.objects.get(pk=c.pk)      # operator clicks "Run now" meanwhile
        down = checks.HttpResult(ok=False, error="Connection refused", response_ms=1.0)
        runner.save_url_result(web_copy, down, timezone.now())
        self.assertEqual(runner.save_url_result(worker_copy, down, timezone.now()), "down")
        evs = Event.objects.filter(fingerprint=f"url:{c.pk}")
        self.assertEqual([(e.data["previous"], e.data["status"]) for e in evs], [("online", "down")])
        self.assertEqual(c.results.count(), 2)          # both results are stored
        self.assertEqual(worker_copy.last_status, "down")

    def test_stale_copy_still_reports_a_real_transition(self):
        c = UrlCheck.objects.create(name="svc", url="https://example.com/", last_status="online")
        stale = UrlCheck.objects.get(pk=c.pk)
        UrlCheck.objects.filter(pk=c.pk).update(last_status="down")   # someone else stored "down"
        ok = checks.HttpResult(ok=True, status=200, response_ms=5.0)
        runner.save_url_result(stale, ok, timezone.now())
        ev = Event.objects.get(fingerprint=f"url:{c.pk}")
        self.assertEqual((ev.data["previous"], ev.data["status"]), ("down", "online"))

    def test_domain_transition_is_emitted_once_by_two_writers(self):
        d = Domain.objects.create(hostname="example.com", status="up")
        a, b = Domain.objects.get(pk=d.pk), Domain.objects.get(pk=d.pk)
        probe = (checks.DnsResult(ok=False, ips=[], error="NXDOMAIN"), None, None)
        runner.save_domain_result(a, probe, timezone.now())
        runner.save_domain_result(b, probe, timezone.now())
        self.assertEqual(Event.objects.filter(fingerprint=f"domain:{d.pk}").count(), 1)
        self.assertEqual(d.checks.count(), 2)
        d.refresh_from_db()
        self.assertEqual(d.status, "down")

    def test_deleted_check_result_is_discarded(self):
        c = UrlCheck.objects.create(name="gone", url="https://example.com/")
        copy = UrlCheck.objects.get(pk=c.pk)
        c.delete()
        with self.assertLogs("linkedeye.probes", "WARNING"):
            self.assertIsNone(runner.save_url_result(copy, checks.HttpResult(ok=False, error="x"), timezone.now()))


class RunProbesLeaseTests(TestCase):
    def test_second_scheduler_stands_by(self):
        SchedulerLease.objects.create(name=f"scheduler:{settings.LINKEDEYE_TENANT}", owner="other-host:1",
                                      expires_at=timezone.now() + timedelta(seconds=60))
        out = StringIO()
        with mock.patch.object(runner, "run_due") as probes_due, mock.patch("core.jobs.run_due") as jobs_due, \
                self.assertLogs("linkedeye.probes", "WARNING"):
            call_command("run_probes", "--once", stdout=out)
        probes_due.assert_not_called()
        jobs_due.assert_not_called()
        self.assertIn("another scheduler holds the lease", out.getvalue())
        self.assertIn("other-host:1", out.getvalue())

    def test_once_takes_and_releases_the_lease_and_beats(self):
        with mock.patch.object(runner, "run_due", return_value=(0, 0)), \
                mock.patch("core.jobs.run_due", return_value={}), mock.patch.object(runner, "prune", return_value=0):
            call_command("run_probes", "--once", stdout=StringIO())
        lease = SchedulerLease.objects.get()
        self.assertEqual(lease.owner, "")              # released for the next cron run
        hb = Heartbeat.objects.get(key="scheduler")
        self.assertEqual(hb.data["passes"], 1)
        self.assertEqual(hb.data["lease"], "held")

    def test_standby_writes_its_own_heartbeat(self):
        lease = mock.Mock(held=False)
        run_probes.HeartbeatThread(lease, run_probes.Loop(), 30).tick()
        lease.acquire.assert_called_once()
        self.assertTrue(Heartbeat.objects.filter(key=standby_key()).exists())
        self.assertFalse(Heartbeat.objects.filter(key="scheduler").exists())

    def test_heartbeat_is_independent_of_pass_duration(self):
        beats = []
        with mock.patch.object(run_probes, "record_beat", side_effect=lambda *a: beats.append(time.monotonic())), \
                mock.patch.object(run_probes, "close_old_connections"), \
                mock.patch.object(run_probes, "connections"):
            hb = run_probes.HeartbeatThread(None, run_probes.Loop(), 0.05)
            hb.start()
            time.sleep(0.4)   # the main thread is "stuck" in a long pass
            hb.stop()
            hb.join(2)
        self.assertGreaterEqual(len(beats), 3)

    def test_heartbeat_job_registration_stays_harmless(self):
        self.assertIn("administration.heartbeat", jobs.JOBS)


@override_settings(NOTIFY_WEBHOOK_URLS=["http://hook.example/x"], NOTIFY_EMAILS=[], INCIDENT_AUTO_CREATE=False,
                   NOTIFY_ASYNC=True, NOTIFY_DIGEST_THRESHOLD=0)
class OnceFlushesNotificationsTests(TestCase):
    def test_once_waits_for_queued_notifications(self):
        sent = threading.Event()

        def pass_emits():
            emit("test", "Down", severity="critical")
            return (1, 0)

        from core import notify
        notify.reset()
        self.addCleanup(notify.reset)
        # the _post mock must outlive the pool's delivery, so flush() runs inside its block
        with mock.patch.object(runner, "run_due", side_effect=pass_emits), \
                mock.patch("core.jobs.run_due", return_value={}), mock.patch.object(runner, "prune", return_value=0), \
                mock.patch("core.notify._post", side_effect=lambda *a, **k: (time.sleep(0.2), sent.set())):
            with self.captureOnCommitCallbacks(execute=True):
                call_command("run_probes", "--once", stdout=StringIO())
                self.assertFalse(sent.is_set())    # delivered after commit, i.e. after the pass
            self.assertEqual(notify.flush(timeout=5), 0)
        self.assertTrue(sent.is_set())


def _load_healthcheck():
    path = Path(__file__).resolve().parents[1] / "deploy" / "docker" / "healthcheck.py"
    spec = importlib.util.spec_from_file_location("le_healthcheck_pipeline", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class HealthcheckTests(SimpleTestCase):
    def setUp(self):
        self.hc = _load_healthcheck()
        self.now = timezone.now()

    def beat(self, host="me", age=10, loop_age=20):
        return {"seen_at": self.now - timedelta(seconds=age), "host": host,
                "data": {"loop_at": (self.now - timedelta(seconds=loop_age)).isoformat()}}

    def check(self, beat, standby=None):
        return self.hc.evaluate_probes(beat, standby, "me", self.now, 300, 900)

    def test_active_scheduler(self):
        self.assertTrue(self.check(self.beat())[0])

    def test_hung_loop_with_live_heartbeat_thread_is_unhealthy(self):
        ok, msg = self.check(self.beat(loop_age=1200))
        self.assertFalse(ok)
        self.assertIn("hung", msg)

    def test_stale_heartbeat_is_unhealthy(self):
        self.assertFalse(self.check(self.beat(age=400))[0])

    def test_standby_is_healthy_while_its_own_heartbeat_is_fresh(self):
        ok, msg = self.check(self.beat(host="other"), {"seen_at": self.now - timedelta(seconds=5), "host": "me", "data": {}})
        self.assertTrue(ok)
        self.assertIn("standby", msg)
        self.assertFalse(self.check(self.beat(host="other"))[0])   # neither active nor a standby

    def test_old_rows_without_loop_data(self):
        self.assertTrue(self.check({"seen_at": self.now, "host": "me", "data": {}})[0])
