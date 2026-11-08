"""Regression tests for the code-review findings onboarding-1 .. onboarding-14. Each class names its finding.

Nothing here opens a socket: DNS, TCP, HTTP, SSH and Prometheus are all faked."""
import csv
import io
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from core import prometheus, secrets
from core.models import AuditEvent
from core.models import Client as LeClient
from onboarding import activate, devices, prometheus_step, servers, sd, services, steps
from onboarding import views as ob_views
from onboarding.models import (Credential, OnboardingRun, OnboardingTarget, RunStatus, StepLog, TargetState,
                               TargetType)

from .base import PASSWORD, SECRET, OnboardingTestCase, with_key
from .test_servers import IP, ServersCase


def url(run, name):
    return reverse("onboarding:step", args=[run.pk, name])


def up_rows(*pairs):
    return [{"metric": {"instance": inst, "job": "node"}, "value": 1.0} for inst in pairs]


# ----------------------------------------------------------------------------- onboarding-1
@with_key
@override_settings(PROBE_ALLOWED_PRIVATE_CIDRS=["10.0.0.0/8"])
class CredentialKindMatchesTargetTypeTests(ServersCase):
    """A credential is only ever used for a target type it can belong to."""

    def setUp(self):
        super().setUp()
        self.run_obj.kind = "site"
        self.run_obj.save()
        self.linux = self.host("srv-01")
        self.url = self.host("app.partner.example.com", target_type=TargetType.URL, port=443)
        self.admin_a.is_staff = False
        self.login(self.admin_a)

    def save_credential(self, kind="ssh_password", **extra):
        data = {"action": "save", "name": "svc", "kind": kind, "username": "svc-le", "secret": SECRET,
                "apply_to": "all", **extra}
        return self.c.post(url(self.run_obj, "credentials"), data)

    def test_apply_to_all_attaches_an_ssh_credential_to_servers_only(self):
        self.save_credential("ssh_password")
        self.linux.refresh_from_db()
        self.url.refresh_from_db()
        self.assertIsNotNone(self.linux.credential_id)
        self.assertIsNone(self.url.credential_id)

    def test_ssh_is_never_opened_to_a_url_service_even_with_a_bad_attachment(self):
        cred = self.cred()
        OnboardingTarget.objects.filter(pk=self.url.pk).update(credential=cred)   # old data, or a direct write
        self.url.refresh_from_db()
        with mock.patch("onboarding.ssh.open_session", side_effect=AssertionError("ssh to a url service")) as op:
            out = steps.verify_target(self.url)
        op.assert_not_called()
        self.assertEqual(self.world.connects, [])
        self.assertNotIn((IP, 22), self.net.connects)
        self.assertEqual(out["ssh"], "refused")
        self.assertNotIn("Authorization", self.net.requests[0]["headers"])

    def test_ssh_is_never_opened_to_a_kubernetes_node(self):
        node = self.host("k8s-1", target_type=TargetType.K8S_NODE, port=8080, credential=self.cred())
        steps.verify_target(node)
        self.assertEqual(self.world.connects, [])
        self.assertNotIn((IP, 22), self.net.connects)

    def test_the_wizard_verify_sends_no_ssh_password_to_the_url_service(self):
        self.save_credential("ssh_password")
        OnboardingTarget.objects.filter(pk=self.url.pk).update(credential=Credential.objects.get())
        self.c.post(url(self.run_obj, "verify"), {"action": "verify"})
        self.assertEqual([c["hostname"] for c in self.world.connects], ["srv-01"])

    def test_install_over_ssh_ignores_a_ssh_credential_on_a_url_service(self):
        OnboardingTarget.objects.filter(pk=self.url.pk).update(credential=self.cred())
        self.net.exporter_up = False
        self.net.status = 404
        self.url.refresh_from_db()
        out = steps.enable_collection(self.url, {"install_over_ssh": True})
        self.assertEqual(self.world.connects, [])
        self.assertEqual(self.world.installs, [])
        self.assertNotEqual(out.get("state"), TargetState.INSTALLED)

    def test_apply_to_all_with_an_http_token_skips_snmp_devices(self):
        dev = self.host("sw-1", target_type=TargetType.DEVICE, port=9116)
        self.save_credential("bearer", username="")
        dev.refresh_from_db()
        self.assertIsNone(dev.credential_id)

    def test_an_http_credential_is_not_sent_to_a_device(self):
        cred = self.cred("bearer", username="", secret="tok-xyz-123")
        dev = self.host("sw-1", target_type=TargetType.DEVICE, port=9116, credential=cred)
        _cred, problem = servers._credential(dev)
        self.assertIsNone(_cred)
        self.assertTrue(problem)

    def test_snmp_credentials_only_fit_devices(self):
        self.assertTrue(services.credential_fits("snmp_v2c", TargetType.DEVICE))
        self.assertFalse(services.credential_fits("snmp_v2c", TargetType.LINUX))
        self.assertFalse(services.credential_fits("ssh_key", TargetType.DEVICE))
        self.assertFalse(services.credential_fits("ssh_password", TargetType.URL))
        self.assertTrue(services.credential_fits("ssh_password", TargetType.WINDOWS))
        self.assertTrue(services.credential_fits("bearer", TargetType.URL))

    def test_attach_credential_reports_and_logs_what_it_skipped(self):
        cred = self.cred()
        n = services.attach_credential(self.run_obj, cred)
        self.assertEqual(n, 1)
        self.assertTrue(StepLog.objects.filter(run=self.run_obj, status="warn", detail__contains="skipped").exists())


# ----------------------------------------------------------------------------- onboarding-2
@with_key
class PlantedDiscoveryTokenTests(OnboardingTestCase):
    def setUp(self):
        super().setUp()
        self.root = get_user_model().objects.create_superuser("root-sd", "sd@example.com", "root-pass-123456")
        run_b = self.make_run(site="Chennai")
        self.make_target(run_b, "secret-host-b", state="scraping")
        self.run_a = self.make_run(site="Mumbai")

    def plant(self, name=sd.TOKEN_NAME):
        self.login(self.admin_a)
        return self.c.post(url(self.run_a, "credentials"), {
            "action": "save", "name": name, "kind": "bearer", "secret": "attacker-known-token", "apply_to": "none"})

    def fetch(self, token):
        return Client().get("/api/sd/targets.json", HTTP_AUTHORIZATION=f"Bearer {token}")

    def test_a_site_admin_cannot_store_a_row_with_the_reserved_label(self):
        for label in (sd.TOKEN_NAME, sd.TOKEN_NAME.lower(), "  Prometheus service discovery ", "Prometheus https://p.example"):
            resp = self.plant(label)
            self.assertEqual(resp.status_code, 200, label)          # the form is shown again with an error
            self.assertContains(resp, "reserved", status_code=200)
        self.assertEqual(Credential.objects.count(), 0)
        self.assertEqual(self.fetch("attacker-known-token").status_code, 401)

    def test_a_same_named_site_row_never_authenticates_even_if_it_exists(self):
        secrets.store("bearer", "", "attacker-known-token", "Mumbai", self.admin_a, name=sd.TOKEN_NAME)
        self.assertIsNone(sd.token_row())
        self.assertFalse(sd.configured())
        self.assertEqual(self.fetch("attacker-known-token").status_code, 401)

    def test_issue_token_never_rotates_a_site_row_and_creates_a_tenant_wide_one(self):
        planted = secrets.store("bearer", "", "attacker-known-token", "Mumbai", self.admin_a, name=sd.TOKEN_NAME)
        row, token = sd.issue_token(self.root)
        self.assertNotEqual(row.pk, planted.pk)
        self.assertEqual(row.site, "")
        self.assertEqual(self.fetch("attacker-known-token").status_code, 401)
        self.assertEqual(self.fetch(token).status_code, 200)
        with secrets.use(Credential.objects.get(pk=planted.pk)) as plain:
            self.assertEqual(plain, "attacker-known-token")           # untouched

    def test_the_real_token_lives_in_a_tenant_wide_row_a_site_admin_cannot_reach(self):
        row, token = sd.issue_token(self.root)
        self.assertEqual(row.site, "")
        self.login(self.admin_a)
        for action in ("rotate", "delete", "attach"):
            resp = self.c.post(url(self.run_a, "credentials"), {"action": action, "credential": row.pk, "secret": "new-x"})
            self.assertEqual(resp.status_code, 404, action)
        self.assertEqual(self.fetch(token).status_code, 200)

    def test_issue_token_twice_rotates_the_one_tenant_wide_row(self):
        first, old = sd.issue_token(self.root)
        second, new = sd.issue_token(self.root)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Credential.objects.filter(name=sd.TOKEN_NAME).count(), 1)
        self.assertEqual(self.fetch(old).status_code, 401)
        self.assertEqual(self.fetch(new).status_code, 200)

    def test_the_snmp_form_rejects_the_reserved_label_too(self):
        self.login(self.admin_a)
        dev_run = self.make_run(site="Mumbai", kind="device")
        self.make_target(dev_run, "sw-1", target_type="device", port=9116)
        resp = self.c.post(reverse("onboarding:snmp_save", args=[dev_run.pk]), {
            "name": sd.TOKEN_NAME, "version": "v2c", "community": "public-xyz", "module": "if_mib",
            "apply_to": "devices"})
        self.assertEqual(Credential.objects.count(), 0, resp.status_code)


# ----------------------------------------------------------------------------- onboarding-3
@with_key
class VerifiedIsNotLiveTests(OnboardingTestCase):
    def poll(self, rows):
        with mock.patch.object(prometheus, "configured", return_value=True), \
                mock.patch.object(prometheus, "query", return_value=rows):
            return activate.poll()

    def test_an_exporter_found_target_is_not_promoted_before_activation(self):
        run = self.make_run(step="verify", status="verifying")
        t = self.make_target(run, "db-01", state=TargetState.EXPORTER_FOUND)
        out = self.poll(up_rows("db-01:9100"))
        self.assertEqual(out["live"], 0)
        run.refresh_from_db()
        t.refresh_from_db()
        self.assertEqual(t.state, TargetState.EXPORTER_FOUND)
        self.assertNotEqual(run.status, RunStatus.COMPLETE)
        self.assertEqual(run.step, "verify")

    def test_an_installed_target_is_not_promoted_either(self):
        run = self.make_run(step="enable", status="installing")
        t = self.make_target(run, "db-02", state=TargetState.INSTALLED)
        self.assertEqual(self.poll(up_rows("db-02:9100"))["live"], 0)
        t.refresh_from_db()
        self.assertEqual(t.state, TargetState.INSTALLED)

    def test_an_activated_target_still_goes_live_and_completes_the_run(self):
        run = self.make_run(step="confirm", status="waiting_for_data")
        t = self.make_target(run, "db-03", state=TargetState.SCRAPING)
        out = self.poll(up_rows("db-03:9100"))
        self.assertEqual((out["live"], out["completed"]), (1, 1))
        t.refresh_from_db()
        run.refresh_from_db()
        self.assertEqual(t.state, TargetState.LIVE)
        self.assertEqual(run.status, RunStatus.COMPLETE)

    def test_the_liveness_job_query_skips_unactivated_targets(self):
        run = self.make_run()
        self.make_target(run, "a", state=TargetState.EXPORTER_FOUND)
        self.make_target(run, "b", state=TargetState.INSTALLED)
        s = self.make_target(run, "c", state=TargetState.SCRAPING)
        self.assertEqual(list(services.waiting_targets()), [s])


# ----------------------------------------------------------------------------- onboarding-4
@with_key
class ReverifyKeepsALiveTargetServedTests(OnboardingTestCase):
    def setUp(self):
        super().setUp()
        self.run_ = self.make_run(step="done", status="complete")
        self.t = self.make_target(self.run_, "prod-db", state=TargetState.LIVE)
        self.login(self.admin_a)

    def served(self):
        return any("prod-db:9100" in e["targets"] for e in sd.targets_payload())

    def test_a_failing_reverify_does_not_remove_a_live_target_from_discovery(self):
        self.assertTrue(self.served())
        failed = {"state": TargetState.UNREACHABLE, "error": "DNS resolution failed", "dns": False}
        with mock.patch.object(steps, "verify_target", return_value=failed):
            resp = self.c.post(url(self.run_, "verify"), {"action": "verify"})
        self.assertEqual(resp.status_code, 302)
        self.t.refresh_from_db()
        self.assertEqual(self.t.state, TargetState.LIVE)
        self.assertEqual(self.t.last_error, "DNS resolution failed")      # the probe result is still recorded
        self.assertIs(self.t.facts.get("dns"), False)
        self.assertTrue(self.served())

    def test_a_failed_ssh_hostkey_check_on_a_scraping_target_keeps_it_served(self):
        self.t.state = TargetState.SCRAPING
        self.t.save()
        with mock.patch.object(steps, "verify_target",
                               return_value={"state": TargetState.FAILED, "error": "SSH host key changed"}):
            self.c.post(url(self.run_, "verify"), {"action": "verify"})
        self.t.refresh_from_db()
        self.assertEqual(self.t.state, TargetState.SCRAPING)
        self.assertTrue(self.served())

    def test_enable_does_not_demote_it_either(self):
        with mock.patch.object(steps, "enable_collection",
                               return_value={"state": TargetState.FAILED, "error": "install failed"}):
            self.c.post(url(self.run_, "enable"), {"action": "enable"})
        self.t.refresh_from_db()
        self.assertEqual(self.t.state, TargetState.LIVE)
        self.assertTrue(self.served())

    def test_a_target_that_is_not_activated_still_follows_the_probe(self):
        self.t.state = TargetState.EXPORTER_FOUND
        self.t.save()
        with mock.patch.object(steps, "verify_target", return_value={"state": TargetState.UNREACHABLE, "error": "x"}):
            self.c.post(url(self.run_, "verify"), {"action": "verify"})
        self.t.refresh_from_db()
        self.assertEqual(self.t.state, TargetState.UNREACHABLE)

    def test_a_good_reverify_clears_the_recorded_problem(self):
        self.t.last_error = "old problem"
        self.t.save()
        with mock.patch.object(steps, "verify_target", return_value={"state": TargetState.EXPORTER_FOUND}):
            self.c.post(url(self.run_, "verify"), {"action": "verify"})
        self.t.refresh_from_db()
        self.assertEqual((self.t.state, self.t.last_error), (TargetState.LIVE, ""))


# ----------------------------------------------------------------------------- onboarding-5
@with_key
class BoundedStepWorkTests(OnboardingTestCase):
    def setUp(self):
        super().setUp()
        self.run_ = self.make_run(step="verify")
        self.targets = [self.make_target(self.run_, f"h-{i}") for i in range(4)]
        self.login(self.admin_a)

    def test_verify_stops_at_the_time_budget_and_says_what_is_left(self):
        calls = []

        def hook(target):
            calls.append(target.hostname)
            return {"state": TargetState.EXPORTER_FOUND}
        with mock.patch.object(ob_views, "STEP_BUDGET_SECONDS", 0.0), \
                mock.patch.object(steps, "verify_target", side_effect=hook):
            resp = self.c.post(url(self.run_, "verify"), {"action": "verify"}, follow=True)
        self.assertEqual(calls, ["h-0"])                              # always one; the rest wait for the next press
        self.assertContains(resp, "time budget")
        states = list(OnboardingTarget.objects.order_by("hostname").values_list("state", flat=True))
        self.assertEqual(states, ["exporter_found", "new", "new", "new"])

    def test_the_audit_row_is_written_before_the_work_starts(self):
        seen = []

        def hook(target):
            seen.append(AuditEvent.objects.filter(action="onboarding.verify.start").count())
            return {"state": TargetState.EXPORTER_FOUND}
        with mock.patch.object(steps, "verify_target", side_effect=hook):
            self.c.post(url(self.run_, "verify"), {"action": "verify"})
        self.assertEqual(seen, [1] * 4)
        self.assertTrue(AuditEvent.objects.filter(action="onboarding.verify").exists())

    def test_enable_stops_at_the_time_budget_too(self):
        calls = []
        with mock.patch.object(ob_views, "STEP_BUDGET_SECONDS", 0.0), \
                mock.patch.object(steps, "enable_collection", side_effect=lambda t, options=None: calls.append(t) or {}):
            resp = self.c.post(url(self.run_, "enable"), {"action": "enable"}, follow=True)
        self.assertEqual(len(calls), 1)
        self.assertContains(resp, "time budget")

    def test_two_installs_cannot_overlap_on_one_host(self):
        t = self.targets[0]
        OnboardingTarget.objects.filter(pk=t.pk).update(install_lock_until=timezone.now() + timedelta(minutes=5))
        calls = []
        with mock.patch.object(steps, "enable_collection", side_effect=lambda t, options=None: calls.append(t.hostname) or {}):
            self.c.post(url(self.run_, "enable"), {"action": "enable", "install_over_ssh": "1"})
        self.assertNotIn("h-0", calls)
        self.assertEqual(len(calls), 3)
        self.assertTrue(StepLog.objects.filter(run=self.run_, target=t, detail__contains="already running").exists())

    def test_the_install_lock_is_released_after_the_work(self):
        with mock.patch.object(steps, "enable_collection", return_value={}):
            self.c.post(url(self.run_, "enable"), {"action": "enable", "install_over_ssh": "1"})
        self.assertFalse(OnboardingTarget.objects.filter(install_lock_until__isnull=False).exists())

    def test_a_stale_install_lock_does_not_block_forever(self):
        t = self.targets[0]
        OnboardingTarget.objects.filter(pk=t.pk).update(install_lock_until=timezone.now() - timedelta(hours=2))
        calls = []
        with mock.patch.object(steps, "enable_collection", side_effect=lambda t, options=None: calls.append(t.hostname) or {}):
            self.c.post(url(self.run_, "enable"), {"action": "enable", "install_over_ssh": "1"})
        self.assertIn("h-0", calls)

    def test_the_state_before_an_install_is_restored_when_nothing_is_decided(self):
        t = self.targets[0]
        OnboardingTarget.objects.filter(pk=t.pk).update(state=TargetState.REACHABLE)
        with mock.patch.object(steps, "enable_collection", return_value={"install_command": "x"}):
            self.c.post(url(self.run_, "enable"), {"action": "enable", "install_over_ssh": "1"})
        t.refresh_from_db()
        self.assertEqual(t.state, TargetState.REACHABLE)


# ----------------------------------------------------------------------------- onboarding-6
@with_key
class ActivationAuditAttributionTests(OnboardingTestCase):
    def test_the_per_target_activation_audit_names_the_operator_and_address(self):
        run = self.make_run(step="confirm")
        self.make_target(run, "db-01", state=TargetState.EXPORTER_FOUND, address="10.0.3.14")
        self.login(self.admin_a)
        self.c.post(url(run, "confirm"), {"action": "activate"}, REMOTE_ADDR="10.1.2.3")
        row = AuditEvent.objects.get(action="onboarding.activate.target")
        self.assertEqual((row.actor_name, row.source_ip), ("admin-a", "10.1.2.3"))

    def test_the_records_activation_creates_are_audited(self):
        run = self.make_run(step="confirm")
        self.make_target(run, "db-01", state=TargetState.EXPORTER_FOUND, address="10.0.3.14")
        self.login(self.admin_a)
        self.c.post(url(run, "confirm"), {"action": "activate"}, REMOTE_ADDR="10.1.2.3")
        for label in ("cmdb.configurationitem", "observe.exporter"):
            rows = AuditEvent.objects.filter(object_type=label)
            self.assertTrue(rows.exists(), label)
            self.assertEqual({(r.actor_name, r.source_ip) for r in rows}, {("admin-a", "10.1.2.3")}, label)

    def test_without_a_request_context_it_is_still_system(self):
        run = self.make_run(step="confirm")
        t = self.make_target(run, "db-02", state=TargetState.EXPORTER_FOUND)
        activate.activate(t)
        self.assertEqual(AuditEvent.objects.get(action="onboarding.activate.target").actor_name, "system")


# ----------------------------------------------------------------------------- onboarding-7
@with_key
@override_settings(PROBE_ALLOWED_PRIVATE_CIDRS=["10.0.0.0/8"])
class CleartextCredentialTests(ServersCase):
    def test_a_bearer_token_is_not_sent_over_plain_http(self):
        cred = self.cred("bearer", username="", secret="tok-clear-1")
        t = self.host("db-01", credential=cred, port=9100)
        out = steps.verify_target(t)
        for req in self.net.requests:
            self.assertNotIn("Authorization", req["headers"])
        self.assertIn("clear text", out["error"])
        self.assertEqual(out["state"], TargetState.REACHABLE)

    def test_basic_auth_over_plain_http_is_refused_the_same_way(self):
        cred = self.cred("basic_auth", username="scraper", secret="pw-clear-1")
        t = self.host("db-01", credential=cred, port=9182, target_type=TargetType.WINDOWS)
        steps.verify_target(t)
        for req in self.net.requests:
            self.assertNotIn("Authorization", req["headers"])

    def test_an_https_target_still_gets_its_credential(self):
        cred = self.cred("bearer", username="", secret="tok-tls-1")
        t = self.host("api.example.com", credential=cred, port=443, target_type=TargetType.URL)
        steps.verify_target(t)
        self.assertEqual(self.net.requests[0]["headers"]["Authorization"], "Bearer tok-tls-1")
        self.assertEqual(self.net.requests[0]["scheme"], "https")

    def test_a_pasted_https_url_on_a_custom_port_means_tls(self):
        rows, bad = services.parse_target_list("https://node-1:9100")
        self.assertEqual(rows[0]["scheme"], "https")
        t = self.host("node-1", scheme="https", credential=self.cred("bearer", username="", secret="tok-tls-2"))
        steps.verify_target(t)
        self.assertEqual(self.net.requests[0]["scheme"], "https")
        self.assertEqual(self.net.requests[0]["headers"]["Authorization"], "Bearer tok-tls-2")

    def test_the_operator_can_allow_clear_text_on_a_trusted_network(self):
        cred = self.cred("bearer", username="", secret="tok-clear-2")
        t = self.host("db-01", credential=cred, port=9100)
        with override_settings(LINKEDEYE_ONBOARDING_ALLOW_CLEARTEXT_CREDENTIALS=True):
            steps.verify_target(t)
        self.assertEqual(self.net.requests[0]["headers"]["Authorization"], "Bearer tok-clear-2")
        self.assertEqual(self.net.requests[0]["scheme"], "http")

    def test_a_prometheus_basic_login_is_not_sent_over_plain_http(self):
        run = self.make_run(kind="prometheus")
        cred = secrets.store("basic_auth", "u", SECRET, "Mumbai", self.admin_a, name="p-login")
        t = self.make_target(run, "prom.example.test", target_type="prometheus", port=9090, credential=cred)
        with mock.patch("onboarding.probing.get") as get, \
                mock.patch("administration.integration_config.prometheus_url", return_value=""), \
                mock.patch("administration.integration_config.prometheus_credential_id", return_value=None):
            out = prometheus_step.verify_prometheus(t)
        get.assert_not_called()                       # not even an unauthenticated request: nothing leaves
        self.assertEqual(out["state"], TargetState.UNREACHABLE)
        self.assertIn("clear text", out["error"])

    def test_a_prometheus_login_over_https_is_still_sent(self):
        run = self.make_run(kind="prometheus")
        cred = secrets.store("basic_auth", "u", SECRET, "Mumbai", self.admin_a, name="p-login")
        t = self.make_target(run, "prom.example.test", target_type="prometheus", port=443, credential=cred)
        with mock.patch("onboarding.probing.get") as get, \
                mock.patch("administration.integration_config.prometheus_url", return_value=""), \
                mock.patch("administration.integration_config.prometheus_credential_id", return_value=None):
            get.return_value = mock.Mock(success=False, ok=False, status=None, error="x", duration_ms=1)
            prometheus_step.verify_prometheus(t)
        self.assertIn("Authorization", get.call_args.kwargs["headers"])


# ----------------------------------------------------------------------------- onboarding-8
@with_key
class SdPayloadQueryCountTests(OnboardingTestCase):
    def test_the_payload_does_not_query_per_credentialed_device(self):
        run = self.make_run(kind="device")
        cred = secrets.store("snmp_v2c", "", "community-xyz", "Mumbai", self.admin_a, name="snmp")
        for i in range(12):
            self.make_target(run, f"sw-{i}", target_type="device", port=9116, state="scraping", credential=cred)
        with mock.patch.object(devices, "exporter_url", return_value="http://snmp-exp:9116"), \
                CaptureQueriesContext(connection) as ctx:
            payload = sd.targets_payload()
        self.assertEqual(len(payload), 12)
        self.assertLessEqual(len(ctx), 3)

    def test_the_sample_on_the_page_is_built_from_a_limited_queryset(self):
        run = self.make_run()
        for i in range(30):
            self.make_target(run, f"n-{i:02d}", state="scraping")
        self.assertEqual(len(sd.targets_payload(limit=3)), 3)


# ----------------------------------------------------------------------------- onboarding-9
@with_key
class CsvExportQueryCountTests(OnboardingTestCase):
    def test_export_is_a_constant_number_of_queries(self):
        for i in range(15):
            run = self.make_run()
            self.make_target(run, f"h-{i}", state="live")
            self.make_target(run, f"g-{i}", state="unreachable")
        self.login(self.admin_a)
        with CaptureQueriesContext(connection) as ctx:
            resp = self.c.get(reverse("onboarding:export"))
        rows = list(csv.reader(io.StringIO(resp.content.decode())))
        self.assertEqual(len(rows), 16)
        self.assertEqual(rows[1][8:11], ["2", "1", "1"])
        self.assertEqual(rows[1][11], "admin-a")
        self.assertLessEqual(len(ctx), 14)


# ----------------------------------------------------------------------------- onboarding-10
@with_key
class RunClientMatchesSiteTests(OnboardingTestCase):
    def setUp(self):
        super().setUp()
        self.other = LeClient.objects.create(name="OtherCust", code="other")

    def post(self, **extra):
        data = {"kind": "server", "site": "Mumbai", "environment": "prod", "client": self.other.pk, **extra}
        return self.c.post(reverse("onboarding:new"), data)

    def test_a_site_admin_cannot_stamp_another_customers_client_on_a_run(self):
        self.login(self.admin_a)
        resp = self.post()
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(OnboardingRun.objects.exists())

    def test_the_dropdown_offers_only_the_sites_own_client(self):
        self.login(self.admin_a)
        body = self.c.get(reverse("onboarding:new")).content.decode()
        self.assertNotIn("OtherCust", body)
        self.assertIn("NSE", body)

    def test_the_site_own_client_or_none_is_accepted(self):
        self.login(self.admin_a)
        self.assertEqual(self.post(client=self.client_row.pk).status_code, 302)
        self.assertEqual(self.post(client="").status_code, 302)
        self.assertEqual({r.client_id for r in OnboardingRun.objects.all()}, {self.client_row.pk})

    def test_editing_the_scope_of_a_run_is_checked_the_same_way(self):
        run = self.make_run()
        self.login(self.admin_a)
        self.c.post(url(run, "scope"), {"action": "save", "kind": "server", "site": "Mumbai", "environment": "prod",
                                        "client": self.other.pk})
        run.refresh_from_db()
        self.assertEqual(run.client_id, self.client_row.pk)

    def test_an_admin_of_every_site_may_choose_any_client(self):
        root = get_user_model().objects.create_superuser("root-x", "x@example.com", PASSWORD)
        self.c = Client()
        self.assertTrue(self.c.login(username="root-x", password=PASSWORD))
        self.assertEqual(self.post().status_code, 302)
        self.assertEqual(OnboardingRun.objects.get().client_id, self.other.pk)
        self.assertIn("OtherCust", self.c.get(reverse("onboarding:new")).content.decode())


# ----------------------------------------------------------------------------- onboarding-11
@with_key
class CredentialLabelHiddenFromViewersTests(OnboardingTestCase):
    def setUp(self):
        super().setUp()
        self.run_ = self.make_run(step="verify")
        cred = secrets.store("ssh_password", "u", SECRET, "Mumbai", self.admin_a, name="PROD-ROOT-LABEL-xyz")
        self.make_target(self.run_, "db-01", credential=cred)

    def test_viewer_and_operator_see_no_label_on_any_step(self):
        for user in (self.viewer, self.operator):
            self.login(user)
            for step in ("targets", "credentials", "verify", "enable", "confirm"):
                body = self.c.get(url(self.run_, step)).content.decode()
                self.assertNotIn("PROD-ROOT-LABEL-xyz", body, (user.username, step))

    def test_an_admin_still_sees_it(self):
        self.login(self.admin_a)
        self.assertIn("PROD-ROOT-LABEL-xyz", self.c.get(url(self.run_, "targets")).content.decode())
        self.assertIn("PROD-ROOT-LABEL-xyz", self.c.get(url(self.run_, "credentials")).content.decode())


# ----------------------------------------------------------------------------- onboarding-12
@with_key
class BoundedFactsTests(OnboardingTestCase):
    def test_parse_exposition_truncates_label_values(self):
        big = "x" * 1_500_000
        body = f'node_uname_info{{nodename="{big}",release="{big}",machine="{big}"}} 1\n' \
               f'node_os_info{{pretty_name="{big}"}} 1\n'
        out = servers.parse_exposition(body)
        for key in ("hostname", "kernel", "arch", "os"):
            self.assertLessEqual(len(out[key]), 255, key)

    def test_a_windows_exposition_is_truncated_too(self):
        big = "w" * 100_000
        out = servers.parse_exposition(f'windows_cs_hostname{{hostname="{big}"}} 1\n'
                                       f'windows_os_info{{product="{big}",version="{big}"}} 1\n')
        for key in ("hostname", "os", "kernel"):
            self.assertLessEqual(len(out[key]), 255, key)

    def test_scrub_caps_any_long_string(self):
        run = self.make_run()
        t = self.make_target(run, "db-01")
        services.set_target_state(t, TargetState.REACHABLE, facts={"anything": "z" * 3_000_000, "n": 3})
        t.refresh_from_db()
        self.assertLess(len(t.facts["anything"]), 20_000)
        self.assertEqual(t.facts["n"], 3)


# ----------------------------------------------------------------------------- onboarding-13
@with_key
class Ipv6PrometheusTargetTests(OnboardingTestCase):
    def test_the_probe_url_brackets_an_ipv6_literal(self):
        run = self.make_run(kind="prometheus")
        t = self.make_target(run, "2001:db8::5", target_type="prometheus", port=9090)
        with mock.patch.object(prometheus_step, "fetch_overview") as fo, \
                mock.patch("administration.integration_config.prometheus_url", return_value=""), \
                mock.patch("administration.integration_config.prometheus_credential_id", return_value=None):
            fo.return_value = {"ok": True, "errors": [], "version": "2.5", "duration_ms": 1}
            out = prometheus_step.verify_prometheus(t)
        self.assertEqual(fo.call_args.args[0], "http://[2001:db8::5]:9090")
        self.assertEqual(out["state"], TargetState.EXPORTER_FOUND)

    def test_the_url_parses_back_to_the_same_port(self):
        from urllib.parse import urlsplit
        run = self.make_run(kind="prometheus")
        t = self.make_target(run, "2001:db8::5", target_type="prometheus", port=9090)
        with mock.patch.object(prometheus_step, "fetch_overview") as fo, \
                mock.patch("administration.integration_config.prometheus_url", return_value=""), \
                mock.patch("administration.integration_config.prometheus_credential_id", return_value=None):
            fo.return_value = {"ok": True, "errors": [], "version": "", "duration_ms": 1}
            prometheus_step.verify_prometheus(t)
        self.assertEqual(urlsplit(fo.call_args.args[0]).port, 9090)


# ----------------------------------------------------------------------------- onboarding-14
@with_key
class ActivateKeepsHeldBackTargetsOutTests(OnboardingTestCase):
    def test_a_held_back_target_stays_out_of_discovery_after_activate(self):
        run = self.make_run(step="confirm")
        held = self.make_target(run, "held-01", state=TargetState.EXPORTER_FOUND, enabled=False)
        joined = self.make_target(run, "join-01", state=TargetState.EXPORTER_FOUND)
        self.login(self.admin_a)
        self.c.post(url(run, "confirm"), {"action": "activate"})
        held.refresh_from_db()
        joined.refresh_from_db()
        self.assertFalse(held.enabled)
        self.assertTrue(joined.enabled)
        served = {e["labels"]["le_hostname"] for e in sd.targets_payload()}
        self.assertEqual(served, {"join-01"})
        self.assertFalse(StepLog.objects.filter(target=held, detail__contains="joined service discovery").exists())
        self.assertTrue(StepLog.objects.filter(target=held, detail__contains="held back").exists())
        self.assertTrue(held.ci_id)                                  # the records are still wired
