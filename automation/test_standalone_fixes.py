"""Regression tests for F07 (webhook secrets shown to viewers) and F18 (StackStorm URL with a query string)."""
import urllib.error
from io import BytesIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import Event, Membership, Site

from . import services
from .models import Execution, Rule

User = get_user_model()
HOOK = "https://hooks.slack.com/services/T000/B000/SECRETTOKEN"
SECRET = "SECRETTOKEN"


class WebhookRedactionTests(TestCase):
    def setUp(self):
        self.a = Site.objects.create(name="A", code="a")
        self.rule = Rule.objects.create(name="slack", enabled=True, action="webhook", min_severity="critical",
                                        action_params={"url": HOOK})
        self.ex = Execution.objects.create(kind="webhook", trigger=HOOK, status="success", http_status=200,
                                           rule=self.rule, site="A")
        self.viewer = User.objects.create_user("v", password="x")
        Membership.objects.create(user=self.viewer, site=self.a, role="viewer")
        self.site_op = User.objects.create_user("siteop", password="x")
        Membership.objects.create(user=self.site_op, site=self.a, role="operator")
        self.op = User.objects.create_user("op", password="x")
        Membership.objects.create(user=self.op, site=None, role="operator")

    def get(self, user, url):
        self.client.force_login(user)
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200, url)
        return r.content.decode()

    def test_redact_url(self):
        self.assertEqual(services.redact_url(HOOK), "https://hooks.slack.com/…")
        self.assertEqual(services.redact_url("https://u:p@h.example:8443/x?k=v"), "https://h.example:8443/…")
        self.assertEqual(services.redact_url("http://h.example/"), "http://h.example/")
        self.assertEqual(services.redact_url(""), "")

    def test_viewer_never_sees_the_secret(self):
        pages = [reverse("automation:rule_detail", args=[self.rule.pk]), reverse("automation:workflows"),
                 reverse("automation:execution_detail", args=[self.ex.pk]), reverse("automation:execution_export")]
        for user in (self.viewer, self.site_op):   # the rule applies to any site: a site operator may not see it
            for url in pages:
                body = self.get(user, url)
                self.assertNotIn(SECRET, body, f"{user} {url}")
            self.assertIn("hooks.slack.com/…", self.get(user, reverse("automation:rule_detail", args=[self.rule.pk])))

    def test_search_cannot_probe_the_hidden_url(self):
        self.client.force_login(self.viewer)
        body = self.client.get(reverse("automation:execution_export") + "?q=SECRET").content.decode()
        self.assertEqual(len(body.strip().splitlines()), 1)   # header only

    def test_operator_of_the_site_sees_the_full_url(self):
        self.assertIn(SECRET, self.get(self.op, reverse("automation:rule_detail", args=[self.rule.pk])))
        self.assertIn(SECRET, self.get(self.op, reverse("automation:execution_detail", args=[self.ex.pk])))
        self.assertIn(SECRET, self.get(self.op, reverse("automation:execution_export")))
        self.assertIn(SECRET, self.get(self.op, reverse("automation:rule_edit", args=[self.rule.pk])))

    def test_summary_event_and_str_are_redacted(self):
        self.assertNotIn(SECRET, self.rule.action_summary())
        self.assertIn(SECRET, self.rule.action_summary(full=True))
        self.assertNotIn(SECRET, str(self.ex))
        ev = Event(source="urlchecker", severity="critical", title="x down", site="A", fingerprint="f", data={})
        done = Execution(kind="webhook", trigger=HOOK, status="success")
        with mock.patch("automation.services.post_webhook", return_value=done):
            services.fire(self.rule, ev, triggered_by="test")
        titles = list(Event.objects.filter(source="automation").values_list("title", flat=True))
        self.assertTrue(titles)
        self.assertFalse(any(SECRET in t for t in titles), titles)


class StackStormQueryStringTests(TestCase):
    @override_settings(STACKSTORM_URL="https://st2.example/api/v1/webhooks/?st2-api-key=KEY")
    def test_trigger_goes_into_the_path_and_query_is_kept(self):
        self.assertEqual(services.stackstorm_endpoint("linkedeye_restart"),
                         "https://st2.example/api/v1/webhooks/linkedeye_restart?st2-api-key=KEY")
        self.assertEqual(services.stackstorm_endpoint("/a/b"), "https://st2.example/api/v1/webhooks/a/b?st2-api-key=KEY")
        self.assertEqual(services.stackstorm_endpoint(), "https://st2.example/api/v1/webhooks/?st2-api-key=KEY")
        self.assertEqual(services.stackstorm_display_url(), "https://st2.example/api/v1/webhooks/")

    @override_settings(STACKSTORM_URL="https://st2.example/api/v1/webhooks")
    def test_plain_url_unchanged(self):
        self.assertEqual(services.stackstorm_endpoint("restart"), "https://st2.example/api/v1/webhooks/restart")

    @override_settings(STACKSTORM_URL="https://[2001:db8::1]:8443/api/v1/webhooks/?st2-api-key=KEY")
    def test_ipv6_display_url_keeps_brackets_and_drops_query(self):
        self.assertEqual(services.stackstorm_display_url(),
                         "https://[2001:db8::1]:8443/api/v1/webhooks/")

    @override_settings(STACKSTORM_URL="https://st2.example/api/v1/webhooks/?st2-api-key=KEY", STACKSTORM_API_KEY="")
    def test_connection_tests_use_the_same_builder(self):
        from administration import services as admin_services
        seen = []

        def fake_open(req, timeout=None):
            seen.append(req.full_url)
            raise urllib.error.HTTPError(req.full_url, 405, "Method Not Allowed", {}, BytesIO(b""))

        with mock.patch("automation.services._st2_open", side_effect=fake_open):
            self.assertTrue(services.test_stackstorm()[0])
        with mock.patch("automation.services.stackstorm_endpoint", wraps=services.stackstorm_endpoint) as ep, \
                mock.patch("urllib.request.OpenerDirector.open", side_effect=fake_open):
            self.assertTrue(admin_services.test_stackstorm()[0])
        ep.assert_called()
        self.assertEqual(seen, ["https://st2.example/api/v1/webhooks/?st2-api-key=KEY"] * 2)
