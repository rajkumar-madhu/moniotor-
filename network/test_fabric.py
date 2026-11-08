from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse

from core.models import Site

from network.fabric import build_fabric, fabric_layer
from network.models import Device, DeviceType
from network.tests import dev


class FabricLayerTests(TestCase):
    def test_switch_and_tiers(self):
        self.assertEqual(fabric_layer(Device(device_type=DeviceType.FIREWALL, name="FW-1")), "firewall")
        self.assertEqual(fabric_layer(Device(device_type=DeviceType.SWITCH, name="spine-01")), "spine")
        self.assertEqual(fabric_layer(Device(device_type=DeviceType.VIRTUAL_MACHINE, name="WEB-PORTAL")), "tier_web")
        self.assertEqual(fabric_layer(Device(device_type=DeviceType.VIRTUAL_MACHINE, name="CORE-NS")), "tier_app")


class FabricViewTests(TestCase):
    def setUp(self):
        self.site = Site.objects.create(name="FinSpot DR", code="finspot-dr")
        self.admin = User.objects.create_superuser("admin", "a@example.com", "pw")
        dev("GM-SW", device_type="switch", mgmt_ip="10.0.0.1", site="FinSpot DR")
        dev("100F-FW", device_type="firewall", mgmt_ip="10.0.0.2", site="FinSpot DR")
        self.c = Client()
        self.c.login(username="admin", password="pw")

    def test_fabric_page(self):
        r = self.c.get(reverse("network:fabric") + "?site=FinSpot+DR")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Spine–leaf")
        self.assertContains(r, "GM-SW")

    def test_fabric_live_json(self):
        r = self.c.get(reverse("network:fabric_live") + "?site=FinSpot+DR")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["totals"]["total"], 2)

    def test_build_fabric_groups(self):
        qs = Device.objects.filter(site="FinSpot DR")
        data = build_fabric(qs)
        self.assertTrue(any(row["spec"].key == "leaf" for row in data["layers"]))
