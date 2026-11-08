"""Re-entry, racing requests, hostile input and a misbehaving Prometheus.

Races are simulated deterministically (a stale in-memory object, a create that loses to another
request) because the suite must pass on SQLite and PostgreSQL alike. Nothing here opens a socket: the
base class refuses them and every Prometheus answer is faked."""
import io
import os
import subprocess
import sys
from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError
from django.urls import reverse
from django.utils import timezone

from cmdb.models import ConfigurationItem
from core import prometheus
from core.models import Event
from observe.models import Exporter
from onboarding import activate, jobs, services
from onboarding.models import OnboardingRun, OnboardingTarget, RunStatus, StepLog, TargetState

from .base import OnboardingTestCase, with_key


def url(run, name):
    return reverse("onboarding:step", args=[run.pk, name])


class HostileInputTests(OnboardingTestCase):
    def test_an_unreadable_first_line_is_reported_not_a_500(self):
        rows, bad = services.parse_target_list("a" * 200000 + ",linux\ndb-01")
        self.assertEqual([r["hostname"] for r in rows], ["db-01"])
        self.assertEqual(len(bad), 1)
        self.assertLessEqual(len(bad[0]), 120)

    def test_a_list_over_the_limit_never_acts_on_a_line_cut_in_half(self):
        line = "host-%05d\n"
        text = "".join(line % i for i in range(30000))
        self.assertGreater(len(text), services.MAX_PASTE_BYTES)
        rows, _bad = services.parse_target_list(text)
        names = {r["hostname"] for r in rows}
        self.assertTrue(all(len(n) == len("host-00000") for n in names))      # no "host-0" or "host-001"
        self.assertLess(len(names), 30000)

    def test_host_forms(self):
        clean = services._clean_host
        self.assertEqual(clean("[::1]:9100"), ("::1", 9100))
        self.assertEqual(clean("http://DB-01.Example.com:9100/metrics"), ("db-01.example.com", 9100))
        self.assertEqual(clean("db-01:abc"), ("", None))                       # a typo, not a host
        self.assertEqual(clean("a" * 256), ("", None))
        self.assertEqual(clean("db 01"), ("", None))
        self.assertEqual(clean("db-01;rm -rf /"), ("", None))
        self.assertEqual(clean('db"01'), ("", None))
        self.assertEqual(clean("$(id)"), ("", None))
        self.assertEqual(clean("db-01\nweb-01"), ("", None))
        self.assertEqual(clean("bücher.example"), ("xn--bcher-kva.example", None))   # unicode -> punycode
        self.assertEqual(clean("exa\u0000mple.com"), ("", None))
        self.assertEqual(clean("a" * 70 + ".example"), ("", None))                # label longer than 63

    def test_unicode_lines_are_listed_as_unreadable_when_they_cannot_be_encoded(self):
        rows, bad = services.parse_target_list("\u202eevil\nbad host\nok")
        self.assertEqual([r["hostname"] for r in rows], ["ok"])
        self.assertEqual(len(bad), 2)

    def test_port_edges(self):
        rows, bad = services.parse_target_list("a,linux,0\nb,linux,65536\nc,linux,65535\nd,linux,9" + "9" * 5000)
        self.assertEqual([(r["hostname"], r["port"]) for r in rows], [("c", 65535)])
        self.assertEqual(len(bad), 3)

    def test_the_longest_legal_host_fits_the_column(self):
        run = self.make_run()
        name = ".".join(["a" * 63] * 3 + ["b" * 60])                              # 252 characters
        rows, bad = services.parse_target_list(name)
        self.assertEqual(bad, [])
        self.assertEqual(services.add_targets(run, rows)[0], 1)
        self.assertEqual(run.targets.get().hostname, name)

    def test_an_oversized_csv_is_refused_by_the_add_action_not_truncated(self):
        run = self.make_run()
        self.login(self.admin_a)
        big = SimpleUploadedFile("hosts.csv", b"db-01\n" * (services.MAX_PASTE_BYTES // 6 + 10))
        resp = self.c.post(url(run, "targets"), {"action": "add", "csv_file": big, "target_type": "linux"},
                           follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(run.targets.count(), 0)
        self.assertIn("larger than", resp.content.decode())

    def test_csv_upload_text_limits(self):
        with self.assertRaises(ValueError):
            services.csv_upload_text(io.BytesIO(b"x" * (services.MAX_PASTE_BYTES + 1)))
        self.assertEqual(services.csv_upload_text(io.BytesIO(b"x" * services.MAX_PASTE_BYTES)),
                         "x" * services.MAX_PASTE_BYTES)
        self.assertEqual(services.csv_upload_text(None), "")
        self.assertEqual(services.csv_upload_text(io.BytesIO("hé".encode("latin-1"))), "hé")

    def test_the_per_run_limit_holds(self):
        run = self.make_run()
        rows = [{"hostname": f"h{i}", "address": "", "target_type": "linux", "port": 9100}
                for i in range(services.MAX_TARGETS_PER_RUN + 5)]
        created, _updated, skipped = services.add_targets(run, rows)
        self.assertEqual((created, skipped), (services.MAX_TARGETS_PER_RUN, 5))
        self.assertEqual(run.targets.count(), services.MAX_TARGETS_PER_RUN)


class StateMachineRaceTests(OnboardingTestCase):
    def test_a_stale_run_object_cannot_move_the_run_backwards(self):
        run = self.make_run(step="scope")
        stale = OnboardingRun.objects.get(pk=run.pk)
        services.enter_step(run, "enable")                         # another request got further
        services.enter_step(stale, "targets")                      # this one still holds step = scope
        self.assertEqual(OnboardingRun.objects.get(pk=run.pk).step, "enable")
        self.assertEqual(stale.step, "enable")                     # and now knows it
        self.assertEqual(OnboardingRun.objects.get(pk=run.pk).status, RunStatus.INSTALLING)

    def test_a_stale_run_cannot_reopen_a_finished_one(self):
        run = self.make_run(step="confirm", status=RunStatus.WAITING_FOR_DATA)
        stale = OnboardingRun.objects.get(pk=run.pk)
        services.complete_run(run)
        services.enter_step(stale, "verify")
        self.assertEqual(OnboardingRun.objects.get(pk=run.pk).status, RunStatus.COMPLETE)

    def test_entering_a_step_of_a_deleted_run_is_harmless(self):
        run = self.make_run()
        OnboardingRun.objects.filter(pk=run.pk).delete()
        self.assertIs(services.enter_step(run, "verify"), run)

    def test_completing_twice_logs_once(self):
        run = self.make_run(step="confirm", status=RunStatus.WAITING_FOR_DATA)
        services.complete_run(run)
        first = OnboardingRun.objects.get(pk=run.pk).finished_at
        services.complete_run(OnboardingRun.objects.get(pk=run.pk))
        services.complete_run(run)
        self.assertEqual(StepLog.objects.filter(run=run, step="done").count(), 1)
        self.assertEqual(OnboardingRun.objects.get(pk=run.pk).finished_at, first)

    def test_two_pollers_complete_a_run_and_announce_it_once(self):
        run = self.make_run(step="confirm", status=RunStatus.WAITING_FOR_DATA)
        self.make_target(run, "db-01", state=TargetState.LIVE)
        a, b = OnboardingRun.objects.get(pk=run.pk), OnboardingRun.objects.get(pk=run.pk)   # both see "waiting"
        self.assertTrue(services.finish_if_all_live(a))
        self.assertFalse(services.finish_if_all_live(b))
        self.assertEqual(Event.objects.filter(source="onboarding", title__contains="complete").count(), 1)
        self.assertEqual(StepLog.objects.filter(run=run, step="done").count(), 1)

    def test_a_poller_with_a_stale_view_never_completes_a_run_marked_failed(self):
        run = self.make_run(step="confirm", status=RunStatus.WAITING_FOR_DATA)
        self.make_target(run, "db-01", state=TargetState.LIVE)
        stale = OnboardingRun.objects.get(pk=run.pk)
        services.fail_run(run, "abandoned")
        self.assertFalse(services.finish_if_all_live(stale))
        self.assertEqual(OnboardingRun.objects.get(pk=run.pk).status, RunStatus.FAILED)

    def test_reopening_a_failed_run_that_had_reached_done_does_not_call_it_complete(self):
        run = self.make_run(step="done", status=RunStatus.FAILED)
        services.reopen_run(run)
        self.assertNotEqual(run.status, RunStatus.COMPLETE)
        self.assertIsNone(run.finished_at)

    def test_a_target_added_by_a_racing_request_is_skipped_not_a_500(self):
        run = self.make_run()
        row = {"hostname": "db-01", "address": "", "target_type": "linux", "port": 9100}
        with mock.patch.object(OnboardingTarget.objects, "create", side_effect=IntegrityError("duplicate")):
            created, updated, skipped = services.add_targets(run, [row, dict(row, hostname="web-01")])
        self.assertEqual((created, updated, skipped), (0, 0, 2))
        # and the transaction is still usable afterwards
        self.assertEqual(services.add_targets(run, [row])[0], 1)

    def test_adding_the_same_host_twice_is_idempotent(self):
        run = self.make_run()
        row = {"hostname": "db-01", "address": "", "target_type": "linux", "port": 9100}
        self.assertEqual(services.add_targets(run, [row, row])[0], 1)
        self.assertEqual(services.add_targets(run, [row])[:2], (0, 0))
        self.assertEqual(run.targets.count(), 1)


@with_key
class ActivateRaceTests(OnboardingTestCase):
    def test_a_name_taken_between_the_lookup_and_the_create_links_instead_of_duplicating(self):
        run = self.make_run()
        target = self.make_target(run, "db-01", address="10.0.3.14", exporter_kind="node_exporter")
        original = activate._unique_name

        def lose_the_race(model, base, site):
            name = original(model, base, site)
            model.objects.get_or_create(tenant_key=settings.LINKEDEYE_TENANT, name=name,
                                        defaults=_defaults(model))      # the other request wins here
            return name

        with mock.patch.object(activate, "_unique_name", side_effect=lose_the_race):
            out = activate.activate(target)
        self.assertEqual(ConfigurationItem.objects.count(), 1)
        self.assertEqual(Exporter.objects.count(), 1)
        again = activate.activate(OnboardingTarget.objects.get(pk=target.pk))
        self.assertEqual((again["ci"], again["exporter"]), (out["ci"], out["exporter"]))
        self.assertEqual(ConfigurationItem.objects.count(), 1)
        self.assertEqual(Exporter.objects.count(), 1)

    def test_two_runs_for_the_same_host_share_one_record(self):
        first = self.make_target(self.make_run(), "db-01", address="10.0.3.14", exporter_kind="node_exporter")
        second = self.make_target(self.make_run(), "db-01", address="10.0.3.14", exporter_kind="node_exporter")
        a, b = activate.activate(first), activate.activate(second)
        self.assertEqual((a["ci"], a["exporter"]), (b["ci"], b["exporter"]))
        self.assertEqual(ConfigurationItem.objects.count(), 1)
        self.assertEqual(Exporter.objects.count(), 1)


def _defaults(model):
    if model is ConfigurationItem:
        return {"ci_type": "server", "site": "Mumbai"}
    return {"exporter_type": "Node Exporter", "category": "Servers", "site": "Mumbai"}


class _Body:
    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return io.BytesIO(self.data)

    def __exit__(self, *exc):
        return False


@with_key
class MisbehavingPrometheusTests(OnboardingTestCase):
    def setUp(self):
        super().setUp()
        self.run_ = self.make_run(step="confirm", status=RunStatus.WAITING_FOR_DATA)
        self.t = self.make_target(self.run_, "db-01", address="10.0.3.14", state=TargetState.SCRAPING)
        for patcher in (mock.patch.object(prometheus, "configured", return_value=True),
                        mock.patch("administration.integration_config.prometheus_url",
                                   return_value="http://prom.invalid:9090"),
                        mock.patch("administration.integration_config.prometheus_credential_id",
                                   return_value=None)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def answer(self, body):
        return mock.patch("urllib.request.urlopen", return_value=_Body(body))

    def test_a_login_page_answering_200_is_a_prometheus_error_not_a_crash(self):
        for body in (b"<html>please sign in</html>", b"", b"[1, 2]", b"null", b'{"status": "success"}'):
            with self.answer(body), self.assertRaises(prometheus.PrometheusError):
                prometheus.query("up")

    def test_the_page_poll_survives_every_kind_of_bad_answer(self):
        self.login(self.admin_a)
        for body in (b"<html>", b"[]", b'{"status":"success","data":{"result":[{"metric":{},"value":[0,"x"]}]}}'):
            with self.answer(body):
                resp = self.c.post(reverse("onboarding:poll", args=[self.run_.pk]))
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["targets"][0]["state"], "scraping")

    def test_the_job_survives_and_leaves_the_target_waiting(self):
        for body in (b"<html>", b"[]", b'{"status":"error","error":"bad query"}'):
            with self.answer(body):
                self.assertIsInstance(jobs.liveness(), str)       # a result, never an exception
        self.t.refresh_from_db()
        self.assertEqual(self.t.state, TargetState.SCRAPING)

    def test_a_connection_cut_mid_body_is_a_prometheus_error(self):
        import http.client
        with mock.patch("urllib.request.urlopen", side_effect=http.client.IncompleteRead(b"x")):
            with self.assertRaises(prometheus.PrometheusError):
                prometheus.query("up")


@with_key
class PollingBoundsTests(OnboardingTestCase):
    up = [{"metric": {"instance": "10.0.3.14:9100"}, "value": 1.0}]

    def test_a_failed_run_is_not_polled(self):
        run = self.make_run(step="confirm", status=RunStatus.FAILED)
        self.make_target(run, "db-01", address="10.0.3.14", state=TargetState.SCRAPING)
        with mock.patch.object(prometheus, "configured", return_value=True), \
                mock.patch.object(prometheus, "query", return_value=self.up) as q:
            out = activate.poll()
        self.assertEqual((out["checked"], out["live"]), (0, 0))
        q.assert_not_called()

    def test_a_target_that_never_came_up_is_given_up_on_by_the_job_but_not_by_check_now(self):
        run = self.make_run(step="confirm", status=RunStatus.WAITING_FOR_DATA)
        dead = self.make_target(run, "db-01", address="10.0.3.14", state=TargetState.SCRAPING)
        OnboardingTarget.objects.filter(pk=dead.pk).update(
            updated_at=timezone.now() - services.GIVE_UP_AFTER - timedelta(days=1))
        with mock.patch.object(prometheus, "configured", return_value=True), \
                mock.patch.object(prometheus, "query", return_value=self.up) as q:
            self.assertEqual(activate.poll()["checked"], 0)
            q.assert_not_called()
            dead.refresh_from_db()
            self.assertEqual(services.refresh_liveness([dead]), 1)       # the operator's Check now still works

    def test_dead_targets_cannot_starve_fresh_ones_out_of_the_batch(self):
        run = self.make_run(step="confirm", status=RunStatus.WAITING_FOR_DATA)
        old = [OnboardingTarget(run=run, hostname=f"old-{i}", state=TargetState.SCRAPING) for i in range(5)]
        OnboardingTarget.objects.bulk_create(old)
        fresh = self.make_target(run, "db-01", address="10.0.3.14", state=TargetState.SCRAPING)
        OnboardingTarget.objects.filter(hostname__startswith="old-").update(
            updated_at=timezone.now() - timedelta(days=3))
        with mock.patch.object(services, "POLL_LIMIT", 1), \
                mock.patch.object(prometheus, "configured", return_value=True), \
                mock.patch.object(prometheus, "query", return_value=[
                    {"metric": {"instance": fresh.instance}, "value": 1.0}]):
            out = activate.poll()
        self.assertEqual((out["checked"], out["live"]), (1, 1))
        fresh.refresh_from_db()
        self.assertEqual(fresh.state, TargetState.LIVE)


class ImportWithoutOptionalLibrariesTests(OnboardingTestCase):
    def test_check_passes_and_the_vault_refuses_when_cryptography_and_paramiko_are_missing(self):
        code = (
            "import sys\n"
            "for m in ('cryptography', 'cryptography.fernet', 'paramiko'):\n"
            "    sys.modules[m] = None\n"
            "import django\n"
            "django.setup()\n"
            "from django.core.management import call_command\n"
            "call_command('check')\n"
            "from core import secrets\n"
            "assert not secrets.available()\n"
            "try:\n"
            "    secrets.encrypt('x')\n"
            "except secrets.VaultUnavailable as exc:\n"
            "    assert 'cryptography' in str(exc)\n"
            "else:\n"
            "    raise SystemExit('stored a secret without the library')\n"
            "import onboarding.views, onboarding.ssh, onboarding.servers, onboarding.install\n"
            "print('ok')\n"
        )
        env = dict(os.environ, DJANGO_SETTINGS_MODULE="linkedeye.settings", LINKEDEYE_DEBUG="true",
                   LINKEDEYE_CREDENTIAL_KEY="")
        done = subprocess.run([sys.executable, "-c", code], cwd=str(settings.BASE_DIR), env=env,
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr[-800:])
        self.assertIn("ok", done.stdout)
