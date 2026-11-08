"""Regression tests for the code-review findings of the automation module:

* StackStorm is a tenant-wide capability: a one-site operator cannot create/edit/test-run/re-run a StackStorm rule;
* the Administration > Integrations StackStorm values are what the module uses;
* a webhook error never carries the (secret) URL path;
* a failed action does not start the rule cooldown;
* deeply nested JSON in a payload field is a form error, not a 500.
"""
import json
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import Event, Membership, Site

from . import services
from .models import Execution, Rule
from .tests import LOCAL, NO_AUTO_INCIDENT, ST2, ViewTestBase, fake_urlopen, make_rule, users


@NO_AUTO_INCIDENT
class StackStormTenantWideTests(TestCase):
    def setUp(self):
        self.mumbai = Site.objects.create(name="Mumbai", code="mumbai")
        self.site_op = User.objects.create_user("local", password="x")
        Membership.objects.create(user=self.site_op, site=self.mumbai, role="operator")
        self.tenant_op = User.objects.create_user("tenantop", password="x")
        Membership.objects.create(user=self.tenant_op, site=None, role="operator")
        self.c = Client()

    def data(self, **kw):
        d = {"name": "r", "enabled": "on", "match_source": "", "min_severity": "warning", "title_contains": "",
             "site": "Mumbai", "cooldown_minutes": "0", "action": "stackstorm", "trigger": "wipe_all",
             "payload": '{"target": "all"}', "priority": "P3", "incident_title": "", "webhook_url": "", "runbook": ""}
        d.update(kw)
        return d

    @ST2
    def test_site_operator_cannot_create_stackstorm_rule(self):
        self.c.force_login(self.site_op)
        r = self.c.post(reverse("automation:rule_create"), self.data())
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "StackStorm")
        self.assertFalse(Rule.objects.exists())

    @ST2
    def test_site_operator_can_still_create_other_rules(self):
        self.c.force_login(self.site_op)
        r = self.c.post(reverse("automation:rule_create"), self.data(action="open_incident"))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Rule.objects.get().action, "open_incident")

    @ST2
    def test_tenant_operator_can_create_site_scoped_stackstorm_rule(self):
        self.c.force_login(self.tenant_op)
        r = self.c.post(reverse("automation:rule_create"), self.data())
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Rule.objects.get().action, "stackstorm")

    @ST2
    def test_site_operator_cannot_edit_stackstorm_rule(self):
        rule = make_rule(site="Mumbai", action_params={"trigger": "restart"})
        self.c.force_login(self.site_op)
        r = self.c.post(reverse("automation:rule_edit", args=[rule.pk]), self.data(trigger="wipe_all"))
        self.assertEqual(r.status_code, 200)
        rule.refresh_from_db()
        self.assertEqual(rule.action_params, {"trigger": "restart"})
        # nor may they turn it into something else (that would drop the tenant-wide automation)
        r = self.c.post(reverse("automation:rule_edit", args=[rule.pk]), self.data(action="open_incident"))
        self.assertEqual(r.status_code, 200)
        rule.refresh_from_db()
        self.assertEqual(rule.action, "stackstorm")

    @ST2
    def test_site_operator_cannot_test_run_stackstorm_rule(self):
        rule = make_rule(site="Mumbai", action_params={"trigger": "wipe_all", "payload": {"target": "all"}})
        self.c.force_login(self.site_op)
        sample = {"source": "urlchecker", "severity": "critical", "title": "t", "site": "Mumbai", "run": "on"}
        with mock.patch("automation.services._st2_open", side_effect=fake_urlopen()) as op:
            self.c.post(reverse("automation:rule_test", args=[rule.pk]), sample)
            op.assert_not_called()
        self.assertFalse(Execution.objects.exists())
        # a dry run (no 'run') is still allowed
        r = self.c.post(reverse("automation:rule_test", args=[rule.pk]), {k: v for k, v in sample.items() if k != "run"})
        self.assertEqual(r.status_code, 200)

    @ST2
    def test_tenant_operator_can_test_run_stackstorm_rule(self):
        rule = make_rule(site="Mumbai", action_params={"trigger": "restart"})
        self.c.force_login(self.tenant_op)
        sample = {"source": "urlchecker", "severity": "critical", "title": "t", "site": "Mumbai", "run": "on"}
        with mock.patch("automation.services._st2_open", side_effect=fake_urlopen()) as op:
            self.c.post(reverse("automation:rule_test", args=[rule.pk]), sample)
            self.assertEqual(op.call_count, 1)

    @ST2
    def test_site_operator_cannot_rerun_stackstorm_execution(self):
        ex = Execution.objects.create(kind="stackstorm", trigger="restart", site="Mumbai", status="failed")
        self.c.force_login(self.site_op)
        with mock.patch("automation.services._st2_open", side_effect=fake_urlopen()) as op:
            self.c.post(reverse("automation:execution_rerun", args=[ex.pk]))
            op.assert_not_called()
        self.assertEqual(Execution.objects.count(), 1)
        self.c.force_login(self.tenant_op)
        with mock.patch("automation.services._st2_open", side_effect=fake_urlopen()) as op:
            self.c.post(reverse("automation:execution_rerun", args=[ex.pk]))
            self.assertEqual(op.call_count, 1)

    @ST2
    def test_rerun_button_hidden_for_site_operator(self):
        ex = Execution.objects.create(kind="stackstorm", trigger="restart", site="Mumbai", status="failed")
        self.c.force_login(self.site_op)
        self.assertNotContains(self.c.get(reverse("automation:execution_detail", args=[ex.pk])), "Re-run")


@NO_AUTO_INCIDENT
class ConsoleStackStormSettingsTests(TestCase):
    """Administration > Integrations values win over the environment (shared integration_config accessor)."""

    @override_settings(STACKSTORM_URL="", STACKSTORM_API_KEY="")
    def test_console_values_are_used(self):
        from administration import integration_config as ic
        from administration.models import IntegrationSetting
        ic.invalidate_cache()
        self.addCleanup(ic.invalidate_cache)
        self.assertFalse(services.stackstorm_configured())
        IntegrationSetting.objects.update_or_create(
            slug="stackstorm", defaults={"values": {"url": "https://st2.console.example/api/v1/webhooks/", "api_key": "console-key"}})
        ic.invalidate_cache()
        self.assertTrue(services.stackstorm_configured())
        calls = []
        with mock.patch("automation.services._st2_open", side_effect=fake_urlopen(calls=calls)):
            ex = services.trigger_stackstorm("restart", {"a": 1})
        self.assertEqual(ex.status, "success")
        req = calls[0][0]
        self.assertEqual(req.full_url, "https://st2.console.example/api/v1/webhooks/restart")
        self.assertEqual(req.get_header("St2-api-key"), "console-key")


@NO_AUTO_INCIDENT
@LOCAL
class WebhookErrorRedactionTests(TestCase):
    def test_url_with_whitespace_is_rejected(self):
        with self.assertRaises(services.WebhookError):
            services.check_webhook_url("http://127.0.0.1:9/services/T0/B0/SECRET TOKEN?k=v")
        with self.assertRaises(services.WebhookError):
            services.check_webhook_url("http://127.0.0.1:9/services/T0/B0/SECRET\tTOKEN")

    def test_invalid_url_error_never_carries_the_path(self):
        import http.client
        err = http.client.InvalidURL("URL can't contain control characters. '/services/T0/B0/SECRET TOKEN?k=v'")
        with mock.patch("automation.services.check_webhook_url", return_value=(mock.Mock(hostname="x", scheme="http"), 80, "127.0.0.1")), \
                mock.patch("automation.services._post_pinned", side_effect=err):
            ex = services.post_webhook("http://127.0.0.1:9/services/T0/B0/SECRET TOKEN?k=v", {})
        self.assertEqual(ex.status, "failed")
        self.assertNotIn("SECRET", ex.error)
        self.assertNotIn("TOKEN", ex.error)
        self.assertIn("InvalidURL", ex.error)


@NO_AUTO_INCIDENT
class FailedActionCooldownTests(TestCase):
    def event(self, fp="fp"):
        return Event(source="urlchecker", severity="critical", title="x", fingerprint=fp)

    def test_failed_execution_does_not_start_cooldown(self):
        rule = make_rule(cooldown_minutes=15)
        Execution.objects.create(kind="stackstorm", trigger="restart", rule=rule, fingerprint="fp", status="failed")
        self.assertFalse(services.in_cooldown(rule, self.event()))

    def test_successful_and_pending_executions_start_cooldown(self):
        rule = make_rule(cooldown_minutes=15)
        Execution.objects.create(kind="stackstorm", trigger="restart", rule=rule, fingerprint="fp", status="success")
        self.assertTrue(services.in_cooldown(rule, self.event()))
        rule2 = make_rule(name="Other", cooldown_minutes=15)
        Execution.objects.create(kind="stackstorm", trigger="restart", rule=rule2, fingerprint="fp", status="pending")
        self.assertTrue(services.in_cooldown(rule2, self.event()))

    def test_second_alert_after_failed_run_fires_again(self):
        rule = make_rule(cooldown_minutes=15)
        with mock.patch("automation.services._st2_open", side_effect=fake_urlopen()):
            with override_settings(STACKSTORM_URL="", STACKSTORM_API_KEY=""):
                first = services.evaluate_event(self.event())
            self.assertEqual([e.status for e in first], ["failed"])
            with override_settings(STACKSTORM_URL="https://st2.example.net/api/v1/webhooks/", STACKSTORM_API_KEY="k"):
                second = services.evaluate_event(self.event())
        self.assertEqual([e.status for e in second], ["success"])


@NO_AUTO_INCIDENT
class DeepJsonPayloadTests(ViewTestBase):
    @ST2
    def test_deeply_nested_payload_is_a_form_error(self):
        bomb = "[" * 20000 + "]" * 20000
        with mock.patch("automation.services._st2_open") as op:
            r = self.login(self.admin).post(reverse("automation:stackstorm_trigger"), {"trigger": "x", "payload": bomb})
        op.assert_not_called()
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "not valid JSON")

    def test_deeply_nested_rule_payload_is_a_form_error(self):
        bomb = "[" * 20000 + "]" * 20000
        d = {"name": "r", "enabled": "on", "min_severity": "warning", "site": "", "cooldown_minutes": "0",
             "action": "stackstorm", "trigger": "x", "payload": bomb}
        r = self.login(self.admin).post(reverse("automation:rule_create"), d)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "not valid JSON")
        self.assertFalse(Rule.objects.exists())

    @ST2
    def test_oversized_payload_is_rejected(self):
        big = json.dumps({"k": "x" * 70000})
        r = self.login(self.admin).post(reverse("automation:stackstorm_trigger"), {"trigger": "x", "payload": big})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "too large")
