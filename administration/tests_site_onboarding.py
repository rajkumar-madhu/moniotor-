from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from core.models import Membership, Site, SiteLesiteProfile

from administration.lesites_defaults import default_profile_fields, port_connection_rows

User = get_user_model()


class SiteOnboardingTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("admin", "admin@finspot.in", "pass")
        self.site = Site.objects.create(name="FinSpot DR", code="fs-dr-le")
        self.client = Client()
        self.client.force_login(self.admin)

    def test_default_profile_fs_dr_le(self):
        d = default_profile_fields(self.site, "fs-dr-le")
        self.assertEqual(d["entity_host"], "domain-fs-le-dr.finspot.in")
        self.assertEqual(d["analytics_prefix_url"], "https://grafana-fs-le-dr.finspot.in")
        self.assertEqual(d["prometheus_url"], "https://prometheus-fs-le-dr.finspot.in")
        self.assertEqual(d["websocket_url"], "wss://fs-le-dr.finspot.in/websocket/ws/")
        self.assertEqual(d["redis_host"], "")

    def test_port_connections_include_grafana_prefix(self):
        rows = {r["service"]: r for r in port_connection_rows("fs-dr-le")}
        self.assertIn("grafana-fs-le-dr.finspot.in", rows["grafana"]["host"])

    def test_onboarding_save_creates_profile(self):
        url = reverse("administration:site_onboarding", args=[self.site.pk])
        self.client.get(url)  # create empty profile row
        data = default_profile_fields(self.site, "fs-dr-le")
        data.update(
            {
                "enabled": "on",
                "is_url_secured": "on",
                "users": [str(self.admin.pk)],
            }
        )
        resp = self.client.post(url, data)
        if resp.status_code != 302 and hasattr(resp, "context") and resp.context:
            self.fail(resp.context["form"].errors.as_json())
        self.assertEqual(resp.status_code, 302)
        profile = SiteLesiteProfile.objects.get(site=self.site)
        self.assertEqual(profile.entity_host, "domain-fs-le-dr.finspot.in")
        self.assertTrue(Membership.objects.filter(site=self.site, user=self.admin).exists())

    def test_onboarding_list_requires_admin(self):
        url = reverse("administration:site_onboarding_list")
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(url).status_code, 200)
