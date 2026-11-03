"""F08: Prometheus targets are site-scoped like Infrastructure; Metrics Explorer needs a view of every site."""
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import Membership, Site


@override_settings(PROMETHEUS_URL="http://prom:9090")
class TelemetrySiteScopeTests(TestCase):
    def setUp(self):
        from cmdb.models import ConfigurationItem
        self.a = Site.objects.create(name="A", code="a")
        Site.objects.create(name="B", code="b")
        ConfigurationItem.objects.create(name="hostA", site="A", prometheus_instance="10.0.0.1:9100")
        ConfigurationItem.objects.create(name="hostB", site="B", prometheus_instance="10.0.0.2:9100")

    def viewer_of_a(self):
        v = User.objects.create_user("v", password="x")
        Membership.objects.create(user=v, site=self.a, role="viewer")
        self.client.force_login(v)
        return v

    def get_targets(self, url):
        tg = [{"labels": {"job": "node", "instance": i}, "scrapeUrl": f"http://{i}/metrics", "health": "up"}
              for i in ("10.0.0.1:9100", "10.0.0.2:9100", "10.0.0.3:9100")]
        with mock.patch("telemetry.promapi.targets", return_value=tg), \
             mock.patch("telemetry.targets.server_stats", return_value=None):
            return self.client.get(url)

    def test_targets_hide_hosts_of_other_sites(self):
        self.viewer_of_a()
        r = self.get_targets("/monitoring/prometheus/?refresh=1")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "10.0.0.1:9100")
        self.assertContains(r, "10.0.0.3:9100")       # not in the CMDB: no site, stays visible (as on Infrastructure)
        self.assertNotContains(r, "10.0.0.2")
        self.assertEqual(r.context["kpis"]["total"], 2)
        csv = self.get_targets("/monitoring/prometheus/?refresh=1&format=csv").content.decode()
        self.assertNotIn("10.0.0.2", csv)

    def test_targets_unfiltered_for_tenant_wide_viewer(self):
        self.client.force_login(User.objects.create_user("all", password="x"))   # no memberships: every site
        r = self.get_targets("/monitoring/prometheus/?refresh=1")
        self.assertContains(r, "10.0.0.2:9100")

    def test_metrics_explorer_needs_all_sites(self):
        self.viewer_of_a()
        with mock.patch("telemetry.explorer.run") as run:
            r = self.client.get(reverse("telemetry:explorer") + "?expr=up%7Binstance%3D%2210.0.0.2%3A9100%22%7D")
        self.assertEqual(r.status_code, 403)
        self.assertContains(r, "Metrics Explorer needs access to all sites", status_code=403)
        run.assert_not_called()

    def test_metrics_explorer_open_to_tenant_wide_viewer(self):
        self.client.force_login(User.objects.create_user("all", password="x"))
        r = self.client.get(reverse("telemetry:explorer"))
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, "needs access to all sites")
