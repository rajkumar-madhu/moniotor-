"""F26: a discovery run without a site scans for every site, so starting one needs operate on every site."""
from unittest import mock

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from core.models import Membership, Site

from .models import DiscoveryRun


@override_settings(PROBE_ALLOWED_PRIVATE_CIDRS=["192.0.2.0/24"])
class DiscoverySiteTests(TestCase):
    def setUp(self):
        self.mum = Site.objects.create(name="Mumbai", code="mum")
        Site.objects.create(name="DR", code="dr")

    def client_for(self, name, site):
        u = User.objects.create_user(name, password="x")
        Membership.objects.create(user=u, site=site, role="operator")
        c = Client()
        c.force_login(u)
        return c

    def test_single_site_operator_cannot_start_siteless_discovery(self):
        c = self.client_for("mop", self.mum)
        with mock.patch("network.discovery.launch") as launch:
            r = c.post(reverse("network:discovery"), {"cidr": "192.0.2.0/29", "ports": "22", "site": ""})
            self.assertEqual(r.status_code, 200)
            self.assertContains(r, "needs the operator role on every site")
            self.assertFalse(DiscoveryRun.objects.exists())
            launch.assert_not_called()
            r = c.post(reverse("network:discovery"), {"cidr": "192.0.2.0/29", "ports": "22", "site": "Mumbai"})
        self.assertEqual(DiscoveryRun.objects.get().site, "Mumbai")
        launch.assert_called_once()

    def test_tenant_wide_operator_can_start_siteless_discovery(self):
        c = self.client_for("gop", None)
        with mock.patch("network.discovery.launch"):
            c.post(reverse("network:discovery"), {"cidr": "192.0.2.0/29", "ports": "22", "site": ""})
        self.assertEqual(DiscoveryRun.objects.get().site, "")
