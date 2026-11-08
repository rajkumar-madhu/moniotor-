"""Regression tests for the second audit round of the automation module:

* a rule test-run may not reuse a user-supplied fingerprint (it would merge into another site's open incident);
* webhook and StackStorm calls have a wall-clock deadline, so a trickling peer cannot stall the probe loop;
* applying a URL/TCP-check template validates through the owning module's form;
* non-ASCII digits in ?rule= / ?from= / ?event= are ignored instead of raising.
"""
import socket
import time
from unittest import mock

from django.apps import apps
from django.contrib.auth.models import User
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from core.models import Membership, Site
from probes.tests.test_audit_fixes import _TrickleServer

from . import services
from .models import Execution, Rule, Template
from .tests import LOCAL, NO_AUTO_INCIDENT, ViewTestBase, make_rule, users

BODY_TRICKLE = b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n"
HEADER_TRICKLE = b"HTTP/1.1 200 OK\r\nX-Slow: "


# ------------------------------------------------------------ 1. test-run fingerprint
@NO_AUTO_INCIDENT
class TestRunFingerprintTests(TestCase):
    def setUp(self):
        self.mumbai = Site.objects.create(name="Mumbai", code="mum")
        Site.objects.create(name="Delhi", code="del")
        self.attacker = User.objects.create_user("mum-op", password="x")
        Membership.objects.create(user=self.attacker, site=self.mumbai, role="operator")
        from servicedesk import services as sd
        self.sd = sd
        self.delhi = sd.create_ticket(kind="incident", title="Delhi core switch down", priority="P4", site="Delhi",
                                      source="alert", fingerprint="tcp:77")
        self.delhi.status = "mitigated"
        self.delhi.save(update_fields=["status"])
        self.rule = Rule.objects.create(name="Mumbai P1", enabled=True, site="Mumbai", match_source="", min_severity="warning",
                                        action="open_incident", action_params={"priority": "P1"})

    def post_test_run(self, **extra):
        data = {"source": "urlchecker", "severity": "critical", "title": "INJECTED", "message": "attacker text",
                "site": "Mumbai", "fingerprint": "tcp:77", "run": "on"}
        data.update(extra)
        self.client.force_login(self.attacker)
        return self.client.post(reverse("automation:rule_test", args=[self.rule.pk]), data)

    def test_test_run_cannot_touch_another_sites_incident(self):
        from servicedesk.models import TimelineEntry
        r = self.post_test_run()
        self.assertEqual(r.status_code, 302)
        self.delhi.refresh_from_db()
        self.assertEqual((self.delhi.priority, self.delhi.status), ("P4", "mitigated"))
        self.assertFalse(TimelineEntry.objects.filter(ticket=self.delhi, body__icontains="INJECTED").exists())
        ex = Execution.objects.get(rule=self.rule)
        self.assertEqual(ex.status, "success", ex.error)
        self.assertNotEqual(ex.result_label, self.delhi.number)
        self.assertNotEqual(ex.fingerprint, "tcp:77")
        self.assertTrue(ex.fingerprint.startswith("test:"), ex.fingerprint)
        # the run opened its own incident, for the rule's site
        Ticket = apps.get_model("servicedesk", "Ticket")
        new = Ticket.objects.exclude(pk=self.delhi.pk).get()
        self.assertEqual((new.site, new.title, new.priority), ("Mumbai", "INJECTED", "P1"))

    def test_dry_run_still_uses_the_given_fingerprint_for_the_cooldown_check(self):
        Execution.objects.create(kind="rule", trigger="open_incident", status="success", rule=self.rule, fingerprint="tcp:77",
                                 site="Mumbai")
        r = self.post_test_run(run="")
        self.assertContains(r, "would be <b>skipped</b>")


# ------------------------------------------------------------ 2. wall-clock deadline for webhooks / StackStorm
@LOCAL
class WebhookDeadlineTests(SimpleTestCase):
    def run_against(self, prefix, fn):
        srv = _TrickleServer(prefix)
        try:
            started = time.monotonic()
            try:
                return fn(srv.port), time.monotonic() - started
            except OSError as e:  # socket.timeout is the expected outcome
                return e, time.monotonic() - started
        finally:
            srv.close()

    def post(self, port):
        parts, port, ip = services.check_webhook_url(f"http://127.0.0.1:{port}/hook")
        return services._post_pinned(parts, port, ip, b"{}", 1)

    def test_trickled_body_hits_overall_deadline(self):
        out, elapsed = self.run_against(BODY_TRICKLE, self.post)
        self.assertLess(elapsed, 3.0)
        self.assertIsInstance(out, socket.timeout)

    def test_trickled_headers_hit_overall_deadline(self):
        out, elapsed = self.run_against(HEADER_TRICKLE, self.post)
        self.assertLess(elapsed, 3.0)
        self.assertIsInstance(out, socket.timeout)

    def test_fast_response_unaffected(self):
        srv = _TrickleServer(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok", limit=0)
        try:
            code, raw = self.post(srv.port)
        finally:
            srv.close()
        self.assertEqual((code, raw), (200, b"ok"))


@LOCAL
class ExecutionDeadlineTests(TestCase):
    """The Execution-recording wrappers report a timeout instead of hanging."""

    def test_post_webhook_times_out(self):
        srv = _TrickleServer(BODY_TRICKLE)
        try:
            started = time.monotonic()
            with mock.patch.object(services, "WEBHOOK_TIMEOUT", 1):
                ex = services.post_webhook(f"http://127.0.0.1:{srv.port}/hook", {"a": 1})
            elapsed = time.monotonic() - started
        finally:
            srv.close()
        self.assertLess(elapsed, 3.0)
        self.assertEqual(ex.status, "failed")
        self.assertIn("imed out", ex.error)

    def test_stackstorm_trigger_times_out(self):
        srv = _TrickleServer(BODY_TRICKLE)
        try:
            started = time.monotonic()
            with override_settings(STACKSTORM_URL=f"http://127.0.0.1:{srv.port}/api/v1/webhooks/", STACKSTORM_API_KEY="k"), \
                    mock.patch.object(services, "ST2_TIMEOUT", 1):
                ex = services.trigger_stackstorm("restart", {"a": 1})
            elapsed = time.monotonic() - started
        finally:
            srv.close()
        self.assertLess(elapsed, 3.0)
        self.assertEqual(ex.status, "failed")
        self.assertIn("unreachable", ex.error)

    def test_stackstorm_trickled_headers_time_out(self):
        srv = _TrickleServer(HEADER_TRICKLE)
        try:
            started = time.monotonic()
            with override_settings(STACKSTORM_URL=f"http://127.0.0.1:{srv.port}/api/v1/webhooks/", STACKSTORM_API_KEY="k"), \
                    mock.patch.object(services, "ST2_TIMEOUT", 1):
                ex = services.trigger_stackstorm("restart", {"a": 1})
            elapsed = time.monotonic() - started
        finally:
            srv.close()
        self.assertLess(elapsed, 3.0)
        self.assertEqual(ex.status, "failed")


# ------------------------------------------------------------ 3. template apply validates like the owning module
@NO_AUTO_INCIDENT
class TemplateApplyValidationTests(TestCase):
    def test_tcp_template_uses_the_tcp_check_form_rules(self):
        TcpCheck = apps.get_model("tcpcheck", "TcpCheck")
        t = Template.objects.create(kind="tcp_check", name="DB")
        base = {"name": "DB", "port": 5432, "site": "All Sites"}
        for host in ("db01.example.com:5432", "http://db01/", "0x7f.1"):
            with self.assertRaises(services.ApplyError, msg=host) as cm:
                services.apply_template(t, {**base, "host": host})
            self.assertIn("host", cm.exception.errors, host)
        with self.assertRaises(services.ApplyError) as cm:
            services.apply_template(t, {**base, "host": "db01", "timeout_seconds": 5, "degraded_ms": 60000})
        self.assertIn("degraded_ms", cm.exception.errors)
        self.assertFalse(TcpCheck.objects.exists())
        obj, url, label = services.apply_template(t, {**base, "host": "DB01.Example.COM."})
        self.assertEqual((obj.host, obj.site, obj.enabled, obj.timeout_seconds), ("db01.example.com", "All Sites", True, 5))
        with self.assertRaises(services.ApplyError) as cm:   # the module's name check is case-insensitive
            services.apply_template(t, {**base, "name": "db", "host": "db02"})
        self.assertIn("name", cm.exception.errors)
        self.assertEqual(TcpCheck.objects.count(), 1)

    def test_url_template_uses_the_url_check_form_rules(self):
        UrlCheck = apps.get_model("urlchecker", "UrlCheck")
        t = Template.objects.create(kind="url_check", name="Files")
        with self.assertRaises(services.ApplyError) as cm:
            services.apply_template(t, {"name": "Files", "url": "ftp://files.example.com/", "site": "All Sites"})
        self.assertIn("url", cm.exception.errors)
        self.assertFalse(UrlCheck.objects.exists())
        obj, url, label = services.apply_template(t, {"name": "Files", "url": "https://files.example.com/", "site": "All Sites",
                                                      "interval_seconds": 60})
        self.assertEqual((obj.enabled, obj.follow_redirects, obj.interval_seconds, obj.expected_status), (True, True, 60, 200))
        self.assertEqual(url, reverse("urlchecker:detail", args=[obj.pk]))

    def test_apply_view_shows_the_modules_error(self):
        TcpCheck = apps.get_model("tcpcheck", "TcpCheck")
        _viewer, operator, _admin = users()
        t = Template.objects.create(kind="tcp_check", name="DB")
        self.client.force_login(operator)
        r = self.client.post(reverse("automation:template_apply", args=[t.pk]),
                             {"name": "DB", "host": "db01.example.com:5432", "port": "5432", "site": "All Sites",
                              "interval_seconds": "60", "timeout_seconds": "5", "degraded_ms": "500"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Put the port in the Port field")
        self.assertFalse(TcpCheck.objects.exists())


# ------------------------------------------------------------ 4. non-ASCII digits in query parameters
class UnicodeDigitParamTests(ViewTestBase):
    def test_superscript_digits_are_ignored(self):
        rule = make_rule()
        c = Client(raise_request_exception=False)
        c.force_login(self.viewer)
        for url in ("/automation/workflows/?rule=%C2%B2", "/automation/stackstorm/?from=%C2%B2",
                    f"/automation/workflows/{rule.pk}/?event=%C2%B2", "/automation/executions/export.csv?rule=%C2%B2",
                    "/automation/workflows/?rule=99999999999999999999999", f"/automation/workflows/?rule={rule.pk}"):
            self.assertEqual(c.get(url).status_code, 200, url)
