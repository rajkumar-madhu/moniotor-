"""The run and target state machine, resumability, idempotency and the hook seam."""
from unittest import mock

from django.urls import reverse

from core import prometheus
from core.models import AuditEvent, Event
from onboarding import services, steps
from onboarding.models import (OnboardingRun, OnboardingTarget, RunStatus, StepLog, TargetState, TargetType)

from .base import SECRET, OnboardingTestCase, with_key


def url(run, name):
    return reverse("onboarding:step", args=[run.pk, name])


@with_key
class WalkTests(OnboardingTestCase):
    """The wizard walks end to end through the unimplemented hooks without a socket."""

    def test_new_run_scope_to_done_with_the_stubbed_hooks(self):
        self.login(self.admin_a)
        resp = self.c.post(reverse("onboarding:new"), {"kind": "server", "site": "Mumbai", "client": self.client_row.pk,
                                                       "environment": "prod", "notes": "rack 4"})
        run = OnboardingRun.objects.get()
        self.assertRedirects(resp, url(run, "targets"))
        self.assertEqual((run.site, run.status, run.tenant_key), ("Mumbai", RunStatus.DRAFT, "default"))
        self.c.post(url(run, "targets"), {"action": "add", "hosts": "db-01\nweb-01:9100\napp-02, linux, 9100",
                                          "target_type": "linux"})
        self.assertEqual(run.targets.count(), 3)
        self.c.post(url(run, "credentials"), {"action": "save", "name": "m-ssh", "kind": "ssh_password",
                                              "username": "svc", "secret": SECRET, "apply_to": "all"})
        self.assertEqual(run.targets.exclude(credential=None).count(), 3)
        pending = mock.Mock(side_effect=steps.StepPending("not wired"))
        with mock.patch.object(steps, "verify_target", pending), mock.patch.object(steps, "enable_collection", pending), \
                mock.patch.object(steps, "logs_config", pending), mock.patch.object(steps, "activate", pending):
            for name, action in (("verify", "verify"), ("enable", "enable"), ("logs", "logs"), ("confirm", "activate")):
                resp = self.c.post(url(run, name), {"action": action}, follow=True)
                self.assertEqual(resp.status_code, 200, name)
                self.assertTrue(StepLog.objects.filter(run=run, step="confirm" if name == "confirm" else name,
                                                       status="pending").exists(), name)
        resp = self.c.post(url(run, "done"), {"action": "finish"})
        run.refresh_from_db()
        self.assertEqual(run.status, RunStatus.COMPLETE)
        self.assertIsNotNone(run.finished_at)
        self.assertTrue(AuditEvent.objects.filter(action="onboarding.run.complete").exists())
        self.assertTrue(Event.objects.filter(source="onboarding").exists())
        # the targets never moved: nothing was verified, so nothing is served to Prometheus
        self.assertFalse(run.targets.exclude(state=TargetState.NEW).exists())
        page = self.c.get(url(run, "done")).content.decode()
        self.assertIn("db-01", page)

    def test_every_step_page_renders_for_the_admin(self):
        run = self.make_run()
        self.make_target(run, "db-01")
        self.login(self.admin_a)
        for name in ("scope", "targets", "credentials", "verify", "enable", "logs", "confirm", "done"):
            self.assertEqual(self.c.get(url(run, name)).status_code, 200, name)
        self.assertEqual(self.c.get(url(run, "nope")).status_code, 404)
        self.assertEqual(self.c.get(reverse("onboarding:log", args=[run.pk])).status_code, 200)

    def test_hooks_keep_their_contract_documented(self):
        # every hook is wired to its module now; a hook that is not yet raises StepPending (a NotImplementedError)
        self.assertTrue(issubclass(steps.StepPending, NotImplementedError))
        for fn in (steps.verify_target, steps.enable_collection, steps.logs_config, steps.activate):
            self.assertTrue(fn.__doc__)


@with_key
class HookResultTests(OnboardingTestCase):
    def setUp(self):
        super().setUp()
        self.run_ = self.make_run()
        # a "url" target: Linux and Windows servers have real hooks now (test_servers.py)
        self.t = self.make_target(self.run_, "db-01", address="10.0.3.14", target_type=TargetType.URL)
        self.login(self.admin_a)

    def test_verify_states_follow_the_facts(self):
        cases = [({"tcp": True, "http_status": 200, "exporter": "node_exporter", "exporter_version": "1.8.1"},
                  TargetState.EXPORTER_FOUND),
                 ({"tcp": True}, TargetState.REACHABLE),
                 ({"tcp": False, "error": "connection refused"}, TargetState.UNREACHABLE),
                 ({"state": "failed", "error": "no route"}, TargetState.FAILED)]
        for facts, expected in cases:
            with mock.patch.object(steps, "verify_target", return_value=dict(facts)):
                self.c.post(url(self.run_, "verify"), {"action": "verify"})
            self.t.refresh_from_db()
            self.assertEqual(self.t.state, expected, facts)
        self.assertEqual(self.t.last_error, "no route")

    def test_enable_that_only_describes_does_not_move_the_target(self):
        services.set_target_state(self.t, TargetState.REACHABLE)
        with mock.patch.object(steps, "enable_collection", return_value={"install_command": "curl x | sh"}):
            self.c.post(url(self.run_, "enable"), {"action": "enable"})
        self.t.refresh_from_db()
        self.assertEqual(self.t.state, TargetState.REACHABLE)
        self.assertEqual(self.t.facts["install_command"], "curl x | sh")
        self.assertIn("curl x | sh", self.c.get(url(self.run_, "enable")).content.decode())

    def test_enable_passes_only_the_ticked_options(self):
        seen = {}

        def fake(target, options=None):
            seen.update(options)
            return {"state": "installed"}
        with mock.patch.object(steps, "enable_collection", side_effect=fake):
            self.c.post(url(self.run_, "enable"), {"action": "enable"})
            self.assertFalse(seen["install_over_ssh"])
            self.c.post(url(self.run_, "enable"), {"action": "enable", "install_over_ssh": "1"})
            self.assertTrue(seen["install_over_ssh"])
        self.t.refresh_from_db()
        self.assertEqual(self.t.state, TargetState.INSTALLED)

    def test_activate_defaults_to_scraping_then_prometheus_makes_it_live(self):
        with mock.patch.object(steps, "activate", return_value={"created": ["ci"]}):
            self.c.post(url(self.run_, "confirm"), {"action": "activate"})
        self.t.refresh_from_db()
        self.assertEqual(self.t.state, TargetState.SCRAPING)
        up = [{"metric": {"instance": "10.0.3.14:9100", "job": "node"}, "value": 1.0}]
        with mock.patch.object(prometheus, "configured", return_value=True), \
                mock.patch.object(prometheus, "query", return_value=up):
            resp = self.c.post(url(self.run_, "confirm"), {"action": "check"})
        self.t.refresh_from_db()
        self.run_.refresh_from_db()
        self.assertEqual(self.t.state, TargetState.LIVE)
        self.assertIsNotNone(self.t.first_sample_at)
        self.assertEqual(self.run_.status, RunStatus.COMPLETE)
        self.assertRedirects(resp, url(self.run_, "done"), fetch_redirect_response=False)
        self.assertTrue(Event.objects.filter(source="onboarding", title__contains="complete").exists())

    def test_liveness_matches_le_target_and_ignores_down_series(self):
        services.set_target_state(self.t, TargetState.SCRAPING)
        other = self.make_target(self.run_, "web-01", state=TargetState.SCRAPING)
        rows = [{"metric": {"le_target": str(self.t.pk)}, "value": 1.0}]
        with mock.patch.object(prometheus, "configured", return_value=True), \
                mock.patch.object(prometheus, "query", return_value=rows):
            n = services.refresh_liveness(list(self.run_.targets.select_related("run")))
        self.assertEqual(n, 1)
        self.t.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual((self.t.state, other.state), (TargetState.LIVE, TargetState.SCRAPING))
        self.run_.refresh_from_db()
        self.assertNotEqual(self.run_.status, RunStatus.COMPLETE)     # one is still waiting

    def test_liveness_does_nothing_when_prometheus_is_unreachable(self):
        services.set_target_state(self.t, TargetState.SCRAPING)
        with mock.patch.object(prometheus, "configured", return_value=True), \
                mock.patch.object(prometheus, "query", side_effect=prometheus.PrometheusError("down")):
            self.assertEqual(services.refresh_liveness([self.t]), 0)
        with mock.patch.object(prometheus, "configured", return_value=False):
            self.assertEqual(services.refresh_liveness([self.t]), 0)
        self.t.refresh_from_db()
        self.assertEqual(self.t.state, TargetState.SCRAPING)

    def test_poll_endpoint_returns_state_and_no_secret(self):
        services.set_target_state(self.t, TargetState.SCRAPING, facts={"os": "Linux"})
        with mock.patch.object(prometheus, "configured", return_value=False):
            resp = self.c.post(reverse("onboarding:poll", args=[self.run_.pk]))
        data = resp.json()
        self.assertEqual(data["targets"][0]["state"], "scraping")
        self.assertNotIn("facts", str(data))
        self.assertEqual(self.c.get(reverse("onboarding:poll", args=[self.run_.pk])).status_code, 405)

    def test_the_liveness_job_checks_every_waiting_target_and_never_raises(self):
        from onboarding import jobs
        services.set_target_state(self.t, TargetState.SCRAPING)
        with mock.patch.object(prometheus, "configured", return_value=True), \
                mock.patch.object(prometheus, "query", return_value=[{"metric": {"instance": self.t.instance},
                                                                      "value": 1.0}]):
            self.assertIn("1 newly live", jobs.liveness())
        services.set_target_state(self.t, TargetState.SCRAPING)
        with mock.patch.object(prometheus, "configured", return_value=True), \
                mock.patch.object(prometheus, "query", return_value=[{"metric": {"instance": self.t.instance},
                                                                      "value": 1.0}]), \
                mock.patch.object(services, "refresh_liveness", side_effect=RuntimeError):
            self.assertEqual(jobs.liveness(), "error")
        from core.jobs import JOBS
        self.assertIn("onboarding.liveness", JOBS)


@with_key
class ResumabilityTests(OnboardingTestCase):
    def test_get_changes_nothing_and_the_furthest_step_is_kept(self):
        run = self.make_run()
        self.login(self.admin_a)
        before = OnboardingRun.objects.values().get(pk=run.pk)
        for name in ("targets", "verify", "done"):
            self.c.get(url(run, name))
        self.assertEqual(OnboardingRun.objects.values().get(pk=run.pk), before)
        self.assertFalse(AuditEvent.objects.filter(action__startswith="onboarding").exists())

    def test_posting_an_earlier_step_does_not_move_the_run_backwards(self):
        run = self.make_run(step="enable", status=RunStatus.INSTALLING)
        self.login(self.admin_a)
        self.c.post(url(run, "targets"), {"action": "add", "hosts": "db-01", "target_type": "linux"})
        run.refresh_from_db()
        self.assertEqual((run.step, run.status), ("enable", RunStatus.INSTALLING))
        self.assertEqual(self.c.get(reverse("onboarding:run", args=[run.pk])).headers["Location"], url(run, "enable"))

    def test_continue_advances_and_the_list_resumes_there(self):
        run = self.make_run()
        self.make_target(run, "db-01")
        self.login(self.admin_a)
        self.c.post(url(run, "targets"), {"action": "next"})
        run.refresh_from_db()
        self.assertEqual(run.step, "credentials")
        self.assertIn(url(run, "credentials"), self.c.get(reverse("onboarding:index")).content.decode())

    def test_continue_without_targets_is_refused(self):
        run = self.make_run()
        self.login(self.admin_a)
        resp = self.c.post(url(run, "targets"), {"action": "next"})
        self.assertRedirects(resp, url(run, "targets"))
        run.refresh_from_db()
        self.assertEqual(run.step, "scope")

    def test_a_completed_run_stays_complete_when_an_earlier_step_is_revisited(self):
        run = self.make_run(step="done", status=RunStatus.COMPLETE)
        self.make_target(run, "db-01")
        self.login(self.admin_a)
        self.c.post(url(run, "targets"), {"action": "add", "hosts": "web-01", "target_type": "linux"})
        run.refresh_from_db()
        self.assertEqual(run.status, RunStatus.COMPLETE)

    def test_fail_and_reopen(self):
        run = self.make_run(step="confirm", status=RunStatus.WAITING_FOR_DATA)
        self.login(self.admin_a)
        self.c.post(url(run, "confirm"), {"action": "fail", "reason": "host decommissioned"})
        run.refresh_from_db()
        self.assertEqual(run.status, RunStatus.FAILED)
        self.assertTrue(run.is_finished)
        self.c.post(url(run, "confirm"), {"action": "reopen"})
        run.refresh_from_db()
        self.assertEqual(run.status, RunStatus.WAITING_FOR_DATA)
        self.assertIsNone(run.finished_at)


@with_key
class IdempotencyTests(OnboardingTestCase):
    def test_re_adding_a_host_never_duplicates_it(self):
        run = self.make_run()
        self.login(self.admin_a)
        for _ in range(3):
            self.c.post(url(run, "targets"), {"action": "add", "hosts": "db-01\nDB-01:9100\ndb-01", "target_type": "linux"})
        self.assertEqual(run.targets.count(), 1)
        self.c.post(url(run, "targets"), {"action": "add", "hosts": "db-01:9200", "target_type": "linux"})
        self.assertEqual(run.targets.get().port, 9200)
        self.assertEqual(run.targets.count(), 1)

    def test_the_database_enforces_one_target_per_host_per_run(self):
        from django.db import IntegrityError, transaction
        run = self.make_run()
        self.make_target(run, "db-01")
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_target(run, "db-01")
        self.make_target(self.make_run(), "db-01")          # another run may hold the same host

    def test_the_same_host_in_a_second_run_is_its_own_row(self):
        a, b = self.make_run(), self.make_run()
        services.add_targets(a, [{"hostname": "db-01", "target_type": "linux", "port": 9100}])
        services.add_targets(b, [{"hostname": "db-01", "target_type": "linux", "port": 9100}])
        self.assertEqual(OnboardingTarget.objects.filter(hostname="db-01").count(), 2)

    def test_the_target_cap(self):
        run = self.make_run()
        rows = [{"hostname": f"h{i}", "target_type": "linux", "port": 9100} for i in range(services.MAX_TARGETS_PER_RUN + 5)]
        created, _u, skipped = services.add_targets(run, rows)
        self.assertEqual((created, skipped), (services.MAX_TARGETS_PER_RUN, 5))


class ParseTests(OnboardingTestCase):
    def test_paste_and_csv_forms(self):
        rows, bad = services.parse_target_list("db-01\nweb-01:9101\n10.0.3.14, device\napp-02,linux,9200\n\n")
        self.assertEqual([(r["hostname"], r["target_type"], r["port"]) for r in rows],
                         [("db-01", "linux", 9100), ("web-01", "linux", 9101), ("10.0.3.14", "device", 9116),
                          ("app-02", "linux", 9200)])
        self.assertEqual(bad, [])
        rows, bad = services.parse_target_list("hostname,address,type,port\nsw-1,10.1.1.1,snmp,\nk8s-1,,k8s,8081\n")
        self.assertEqual([(r["hostname"], r["address"], r["target_type"], r["port"]) for r in rows],
                         [("sw-1", "10.1.1.1", "device", 9116), ("k8s-1", "", "k8s_node", 8081)])

    def test_hostile_hosts_are_rejected(self):
        text = 'a"b\nup{x}\nhost name\n$(rm -rf)\n`id`\nok-host\nhttp://good.example:9100/metrics\n..\n-x\nhost;ls'
        rows, bad = services.parse_target_list(text)
        self.assertEqual({r["hostname"] for r in rows}, {"ok-host", "good.example"})
        self.assertGreaterEqual(len(bad), 7)

    def test_port_range_and_default_type(self):
        rows, bad = services.parse_target_list("a:70000\nb:0\nc", TargetType.DEVICE)
        self.assertEqual([r["hostname"] for r in rows], ["c"])
        self.assertEqual(rows[0]["port"], 9116)
        self.assertEqual(len(bad), 2)

    def test_csv_formula_cells_are_defused_in_the_export(self):
        self.assertEqual(services.csv_safe("=1+1"), "'=1+1")
        self.assertEqual(services.csv_safe("db-01"), "db-01")

    def test_ipv6_instances_are_bracketed(self):
        run = self.make_run()
        t = self.make_target(run, "fd00::1", port=9100)
        self.assertEqual(t.instance, "[fd00::1]:9100")
        self.assertEqual(t.metrics_url, "http://[fd00::1]:9100/metrics")
