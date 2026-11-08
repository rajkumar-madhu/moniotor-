"""Review fixes for the administration area: console-saved integration settings reach their consumers,
onboarding defaults/handoff are safe and audited, the profile page does not leak site names, the webhook form
validates its input, and the Clients page does not issue a query per client."""
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from administration import integration_config as ic
from core.models import AuditEvent, Membership, Site

PW = "Corr3ct-Horse-Battery-77"
FAST = override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])


@FAST
class ConsoleSettingsReachConsumersTests(TestCase):
    """Console-saved values win over the environment; the environment is the fallback."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.admin = User.objects.create_user("boss", "boss@example.com", PW)
        Membership.objects.create(user=self.admin, site=None, role="admin")
        self.client.force_login(self.admin)

    def save(self, slug, **values):
        ic.save(slug, values, self.admin)

    # -- Alertmanager webhook
    @override_settings(ALERTMANAGER_WEBHOOK_TOKEN="")
    def test_alertmanager_webhook_accepts_console_token(self):
        token = "t" * 30
        self.save("alertmanager", token=token)
        self.client.logout()
        r = self.client.post("/api/v1/alertmanager/", data="{}", content_type="application/json",
                             HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertNotEqual(r.status_code, 503)
        self.assertNotEqual(r.status_code, 401)
        r = self.client.post("/api/v1/alertmanager/", data="{}", content_type="application/json",
                             HTTP_AUTHORIZATION="Bearer wrong")
        self.assertEqual(r.status_code, 401)

    @override_settings(ALERTMANAGER_WEBHOOK_TOKEN="env-token-" + "e" * 20)
    def test_alertmanager_webhook_env_fallback_and_console_wins(self):
        self.client.logout()
        env = "env-token-" + "e" * 20
        r = self.client.post("/api/v1/alertmanager/", data="{}", content_type="application/json",
                             HTTP_AUTHORIZATION=f"Bearer {env}")
        self.assertNotIn(r.status_code, (401, 503))
        self.save("alertmanager", token="c" * 30)
        r = self.client.post("/api/v1/alertmanager/", data="{}", content_type="application/json",
                             HTTP_AUTHORIZATION=f"Bearer {env}")
        self.assertEqual(r.status_code, 401)

    @override_settings(ALERTMANAGER_WEBHOOK_TOKEN="")
    def test_alertmanager_webhook_without_any_token_stays_closed(self):
        self.client.logout()
        r = self.client.post("/api/v1/alertmanager/", data="{}", content_type="application/json")
        self.assertEqual(r.status_code, 503)

    # -- log ingest
    @override_settings(LOG_INGEST_TOKEN="")
    def test_log_ingest_accepts_console_token(self):
        token = "l" * 30
        self.save("logs", token=token)
        self.client.logout()
        r = self.client.post("/api/v1/logs/", data="[]", content_type="application/json",
                             HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertNotIn(r.status_code, (401, 503))
        r = self.client.post("/api/v1/logs/", data="[]", content_type="application/json",
                             HTTP_AUTHORIZATION="Bearer nope")
        self.assertEqual(r.status_code, 401)

    @override_settings(LOG_INGEST_TOKEN="env-log-" + "e" * 20)
    def test_log_ingest_env_fallback(self):
        self.client.logout()
        r = self.client.post("/api/v1/logs/", data="[]", content_type="application/json",
                             HTTP_AUTHORIZATION="Bearer env-log-" + "e" * 20)
        self.assertNotIn(r.status_code, (401, 503))

    @override_settings(LOG_INGEST_TOKEN="")
    def test_onboarding_log_token_follows_console(self):
        from onboarding import logs
        self.assertFalse(logs.ingest_enabled())
        self.save("logs", token="l" * 30)
        self.assertTrue(logs.ingest_enabled())
        self.assertEqual(logs.ingest_token(), "l" * 30)

    # -- Nagios
    @override_settings(NAGIOS_URL="", NAGIOS_USER="", NAGIOS_PASSWORD="", NAGIOS_TIMEOUT=10.0)
    def test_nagios_reads_console_values(self):
        from telemetry import nagios
        self.assertFalse(nagios.configured())
        self.save("nagios", url="https://nagios.example/nagios", user="nagiosadmin", password="pw", timeout=20)
        self.assertTrue(nagios.configured())
        self.assertEqual(nagios.base_url(), "https://nagios.example/nagios")
        self.assertEqual(nagios.timeout(), 20.0)
        seen = {}

        class Resp:
            def read(self, n=-1):
                return b'{"data": {}}'
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
            headers = {}
            status = 200

        def fake(req, timeout):
            seen["auth"] = req.get_header("Authorization")
            seen["url"] = req.full_url
            return Resp()

        with mock.patch.object(nagios, "_urlopen", fake):
            try:
                nagios._get({"query": "hostlist"})
            except Exception:
                pass
        import base64
        self.assertEqual(seen["auth"], "Basic " + base64.b64encode(b"nagiosadmin:pw").decode())
        self.assertTrue(seen["url"].startswith("https://nagios.example/nagios/cgi-bin/statusjson.cgi"))

    @override_settings(NAGIOS_URL="https://env.example/nagios", NAGIOS_USER="envuser", NAGIOS_PASSWORD="")
    def test_nagios_env_fallback_and_console_wins(self):
        from telemetry import nagios
        self.assertEqual(nagios.base_url(), "https://env.example/nagios")
        self.save("nagios", url="https://console.example/nagios")
        self.assertEqual(nagios.base_url(), "https://console.example/nagios")

    # -- StackStorm
    @override_settings(STACKSTORM_URL="", STACKSTORM_API_KEY="")
    def test_stackstorm_uses_console_url_and_key(self):
        from administration import services as admin_services
        from automation import services as auto
        self.assertFalse(auto.stackstorm_configured())
        self.save("stackstorm", url="https://st2.example/api/v1/webhooks/", api_key="k-secret")
        self.assertTrue(auto.stackstorm_configured())
        self.assertEqual(auto.stackstorm_endpoint("restart"), "https://st2.example/api/v1/webhooks/restart")
        seen = {}

        class Resp:
            status = 200
            def read(self, n=-1):
                return b"{}"
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        def fake_open(req, timeout=None):
            seen["key"] = req.get_header("St2-api-key")
            seen["url"] = req.full_url
            return Resp()

        with mock.patch.object(auto, "_st2_open", fake_open):
            ex = auto.trigger_stackstorm("restart", {"a": 1})
        self.assertEqual(ex.status, "success")
        self.assertEqual(seen["key"], "k-secret")
        self.assertEqual(seen["url"], "https://st2.example/api/v1/webhooks/restart")

        opener = mock.Mock()
        opener.open.return_value = Resp()
        with mock.patch("administration.services.urllib.request.build_opener", return_value=opener):
            ok, msg = admin_services.test_stackstorm()
        self.assertTrue(ok, msg)
        self.assertEqual(opener.open.call_args[0][0].full_url, "https://st2.example/api/v1/webhooks/")

    @override_settings(STACKSTORM_URL="https://env-st2.example/hooks/", STACKSTORM_API_KEY="envkey")
    def test_stackstorm_env_fallback(self):
        from automation import services as auto
        self.assertTrue(auto.stackstorm_configured())
        self.assertEqual(auto.stackstorm_endpoint("x"), "https://env-st2.example/hooks/x")


@FAST
class OnboardingDefaultsTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("boss", "boss@example.com", PW)
        Membership.objects.create(user=self.admin, site=None, role="admin")
        self.client.force_login(self.admin)
        self.site = Site.objects.create(name="R Site", code="rs")
        self.url = reverse("administration:site_onboarding", args=[self.site.pk])

    def test_get_never_writes(self):
        from core.models import SiteLesiteProfile
        before = AuditEvent.objects.count()
        self.assertEqual(self.client.get(self.url + "?defaults=1").status_code, 200)
        self.assertEqual(self.client.get(self.url).status_code, 200)
        self.assertFalse(SiteLesiteProfile.objects.filter(site=self.site).exists())
        self.assertEqual(AuditEvent.objects.count(), before)

    def test_get_defaults_does_not_overwrite_saved_profile(self):
        from core.models import SiteLesiteProfile
        SiteLesiteProfile.objects.create(site=self.site, prometheus_url="https://mine.example", legacy_sitename="rs")
        self.client.get(self.url + "?defaults=1")
        self.assertEqual(SiteLesiteProfile.objects.get(site=self.site).prometheus_url, "https://mine.example")

    def test_apply_defaults_is_a_post_that_fills_the_unsaved_form(self):
        from core.models import SiteLesiteProfile
        SiteLesiteProfile.objects.create(site=self.site, prometheus_url="https://mine.example", legacy_sitename="rs")
        r = self.client.post(self.url, {"apply_defaults": "1"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "prometheus-rs.finspot.in")
        self.assertContains(r, "not saved until you submit")
        self.assertEqual(SiteLesiteProfile.objects.get(site=self.site).prometheus_url, "https://mine.example")

    def test_templates_post_the_apply_button(self):
        r = self.client.get(self.url)
        html = r.content.decode()
        self.assertNotIn("?defaults=1", html)
        self.assertIn('name="apply_defaults"', html)
        r = self.client.get(reverse("administration:site_domain", args=[self.site.pk]))
        self.assertNotIn("?defaults=1", r.content.decode())


@FAST
class OnboardingHandoffTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("boss", "boss@example.com", PW)
        Membership.objects.create(user=self.admin, site=None, role="admin")
        self.client.force_login(self.admin)
        self.site = Site.objects.create(name="R Site", code="rs")
        self.other = Site.objects.create(name="Other Site", code="os")
        self.url = reverse("administration:site_onboarding", args=[self.site.pk])
        self.v_only = User.objects.create_user("v_only", "a@example.com", PW)
        Membership.objects.create(user=self.v_only, site=self.site, role="viewer")
        self.v_both = User.objects.create_user("v_both", "b@example.com", PW)
        Membership.objects.create(user=self.v_both, site=self.site, role="viewer")
        Membership.objects.create(user=self.v_both, site=self.other, role="viewer")
        self.newbie = User.objects.create_user("newbie", "c@example.com", PW)

    def post(self, user_ids):
        from administration.lesites_defaults import default_profile_fields
        data = default_profile_fields(self.site, "rs")
        data["enabled"] = "on"
        data["is_url_secured"] = "on"
        data["users"] = [str(i) for i in user_ids]
        return self.client.post(self.url, data, follow=True)

    def test_last_membership_is_not_removed_so_the_user_is_not_widened(self):
        from core.access import visible_sites
        r = self.post([self.admin.pk])
        self.assertTrue(Membership.objects.filter(user=self.v_only, site=self.site).exists())
        u = User.objects.get(pk=self.v_only.pk)
        self.assertEqual(visible_sites(u), {"R Site"})
        self.assertContains(r, "v_only")                       # the warning names the user
        self.assertFalse(AuditEvent.objects.filter(action="membership_remove", object_repr__icontains="v_only").exists())

    def test_removing_a_non_last_membership_is_audited(self):
        self.post([self.v_only.pk])
        self.assertFalse(Membership.objects.filter(user=self.v_both, site=self.site).exists())
        self.assertTrue(Membership.objects.filter(user=self.v_both, site=self.other).exists())
        ev = AuditEvent.objects.filter(action="membership_remove").get()
        self.assertEqual(ev.details.get("site"), "R Site")
        self.assertEqual(ev.details.get("role"), "viewer")

    def test_adding_a_member_is_audited(self):
        self.post([self.v_only.pk, self.v_both.pk, self.newbie.pk])
        self.assertTrue(Membership.objects.filter(user=self.newbie, site=self.site, role="viewer").exists())
        ev = AuditEvent.objects.filter(action="membership_add").get()
        self.assertEqual(ev.details.get("site"), "R Site")

    def test_profile_and_memberships_are_one_transaction(self):
        from core.models import SiteLesiteProfile
        with mock.patch("administration.views.audit", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.post([self.v_only.pk, self.newbie.pk])
        self.assertFalse(Membership.objects.filter(user=self.newbie, site=self.site).exists())
        self.assertFalse(SiteLesiteProfile.objects.filter(site=self.site).exists())


@FAST
class ProfileSiteLeakTests(TestCase):
    def test_scoped_user_sees_only_their_own_sites(self):
        mine = Site.objects.create(name="R Site", code="rs")
        Site.objects.create(name="Secret Client Site", code="sc")
        u = User.objects.create_user("scoped", "s@example.com", PW)
        Membership.objects.create(user=u, site=mine, role="viewer")
        self.client.force_login(u)
        r = self.client.get(reverse("administration:profile"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "R Site")
        self.assertNotContains(r, "Secret Client Site")

    def test_tenant_wide_user_still_sees_every_site(self):
        Site.objects.create(name="R Site", code="rs")
        Site.objects.create(name="Other Site", code="os")
        u = User.objects.create_user("wide", "w@example.com", PW)
        self.client.force_login(u)
        r = self.client.get(reverse("administration:profile"))
        self.assertContains(r, "R Site")
        self.assertContains(r, "Other Site")


@FAST
class WebhookFormValidationTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.admin = User.objects.create_user("boss", "boss@example.com", PW)
        Membership.objects.create(user=self.admin, site=None, role="admin")
        self.client.force_login(self.admin)
        self.url = reverse("administration:integration_save", args=["webhooks"])

    def post(self, urls, severities):
        return self.client.post(self.url, {"urls": urls, "severities": severities}, follow=True)

    def test_bad_urls_and_severities_are_rejected_and_not_stored(self):
        r = self.post("file:///etc/hostname\nnot a url", "critcal")
        self.assertEqual(ic.notify_webhook_urls(), [])
        self.assertContains(r, "Could not save webhooks")

    def test_each_rule(self):
        from administration.forms import WebhooksIntegrationForm as F
        bad = ["file:///etc/hostname", "ftp://x.example/h", "not a url", "https://", "https://u:p@x.example/h",
               "https://x.example:99999999/h"]
        for u in bad:
            self.assertFalse(F(data={"urls": u}).is_valid(), u)
        self.assertFalse(F(data={"urls": "\n".join(f"https://x{i}.example/h" for i in range(21))}).is_valid())
        self.assertFalse(F(data={"severities": "critical, critcal"}).is_valid())

    def test_valid_input_is_normalised_and_stored(self):
        self.post("https://hooks.example/a\n\n  https://hooks.example/a \nhttp://10.0.0.1:8080/b", "Critical, warning critical")
        self.assertEqual(ic.notify_webhook_urls(), ["https://hooks.example/a", "http://10.0.0.1:8080/b"])
        self.assertEqual(ic.notify_severities(), ["critical", "warning"])

    def test_blank_clears_to_the_environment_defaults(self):
        self.post("https://hooks.example/a", "critical")
        self.post("", "")
        self.assertEqual(ic.notify_webhook_urls(), list(settings_urls()))


def settings_urls():
    from django.conf import settings
    return settings.NOTIFY_WEBHOOK_URLS or []


@FAST
class ClientRowsQueryTests(TestCase):
    def setUp(self):
        from core.models import Client as LeClient
        self.LeClient = LeClient
        self.admin = User.objects.create_user("boss", "boss@example.com", PW)
        Membership.objects.create(user=self.admin, site=None, role="admin")
        self.client.force_login(self.admin)

    def make_client(self, name, devices=1):
        from domainmonitor.models import Domain
        from network.models import Device
        from servicedesk.models import Ticket
        from urlchecker.models import UrlCheck
        c = self.LeClient.objects.create(name=name, code=name.lower(), status="active")
        s = Site.objects.create(name=f"{name} HQ", code=f"{name.lower()}-hq", client=c)
        UrlCheck.objects.create(name=f"{name}-u", url="https://u.example/", site=s.name, last_status="down")
        UrlCheck.objects.create(name=f"{name}-u2", url="https://u2.example/", site=s.name, last_status="up")
        for i in range(devices):
            Device.objects.create(name=f"{name}-sw{i}", mgmt_ip=f"10.{sum(map(ord, name)) % 250}.{len(name)}.{i + 1}", site=s.name,
                                  device_type="switch", status="down" if i == 0 else "up")
        Domain.objects.create(tenant_key="default", site_key=s.name, hostname=f"{name.lower()}.example",
                              status="down", ssl_status="Valid")
        Ticket.objects.create(kind="incident", title="t", number=f"INC-{name}", priority="P1", site=s.name)
        # matches the client by name AND by site: counted once
        Ticket.objects.create(kind="incident", title="t2", number=f"INC-{name}-2", priority="P3", site=s.name, client=name)
        # matches only by client name
        Ticket.objects.create(kind="incident", title="t3", number=f"INC-{name}-3", priority="P2", client=name)
        return c

    def test_roll_up_numbers(self):
        c = self.make_client("Acme", devices=3)
        Site.objects.create(name="Loose", code="loose")
        from servicedesk.models import Ticket
        Ticket.objects.create(kind="incident", title="x", number="INC-L", priority="P2", site="Loose")
        from administration import services
        rows = services.client_wise_rows()
        row = next(r for r in rows if r["client"] and r["client"].pk == c.pk)
        self.assertEqual(row["sites"], 1)
        self.assertEqual(row["open_incidents"], 3)
        self.assertEqual(row["open_major"], 2)
        self.assertEqual((row["urls"], row["urls_down"]), (2, 1))
        self.assertEqual((row["devices"], row["devices_down"]), (3, 1))
        self.assertEqual(row["devices_by_type"], {"switch": 3})
        self.assertEqual((row["domains"], row["domains_issue"]), (1, 1))
        loose = next(r for r in rows if r["client"] is None)
        self.assertEqual((loose["sites"], loose["open_incidents"], loose["open_major"]), (1, 1, 1))

    def test_query_count_does_not_grow_with_clients(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from administration import services
        self.make_client("One")
        with CaptureQueriesContext(connection) as few:
            services.client_wise_rows()
        for n in ("Two", "Three", "Four", "Five", "Six"):
            self.make_client(n)
        with CaptureQueriesContext(connection) as many:
            services.client_wise_rows()
        self.assertEqual(len(few), len(many))
        self.assertLess(len(many), 15)
