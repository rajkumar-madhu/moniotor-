from io import StringIO

from django.core.management import call_command
from django.urls import reverse

from architecture import example
from architecture.models import Link
from core.models import AuditEvent
from network.models import Device

from .fixtures import Base, dev


class SeedTests(Base):
    def setUp(self):
        super().setUp()
        Device.objects.filter(name__startswith="dc1-").delete()      # the shared fixture reuses a few of the seed's names
    def run_cmd(self, *args):
        out = StringIO()
        call_command("seed_architecture", *args, stdout=out)
        return out.getvalue()

    def test_creates_the_fabric_and_is_idempotent(self):
        self.run_cmd("--site", "Mumbai")
        n_dev, n_link = Device.objects.filter(tags__icontains="arch-example").count(), Link.objects.count()
        self.assertEqual(n_dev, 2 + 2 + 2 + 4 + 4 + 19)
        self.assertEqual(n_link, 2 + 1 + 4 + 1 + 8 + 16 + 19)
        out = self.run_cmd("--site", "Mumbai")
        self.assertIn("0 devices, 0 links, 0 racks, 0 rack items", out)
        self.assertEqual((Device.objects.filter(tags__icontains="arch-example").count(), Link.objects.count()), (n_dev, n_link))

    def test_devices_are_examples_on_documentation_addresses(self):
        import ipaddress
        self.run_cmd("--site", "Mumbai")
        docs = [ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")]
        for d in Device.objects.filter(tags__icontains="arch-example"):
            self.assertTrue(d.is_example)
            self.assertTrue(any(ipaddress.ip_address(d.mgmt_ip) in n for n in docs), d.mgmt_ip)
            self.assertEqual(d.status, "unknown")

    def test_every_tier_is_inferred_as_in_the_drawing(self):
        self.run_cmd("--site", "Mumbai")
        self.login(self.admin)
        r = self.client.get(reverse("architecture:overview"), {"site": "Mumbai"})
        tiers = r.context["kpis"]["tiers"]
        self.assertEqual(tiers, {"wan": 2, "firewall": 2, "core": 2, "spine": 4, "leaf": 4, "server": 20, "other": 0})
        groups = {c["label"]: c["count"] for c in r.context["cards"]}
        self.assertEqual(groups["Web Servers"], 6)       # five seeded plus the fixture's web-01
        self.assertEqual(groups["Storage (SAN)"], 4)

    def test_racks_are_created_and_shown(self):
        from dcim.models import Rack, RackItem
        self.run_cmd("--site", "Mumbai")
        self.assertEqual(Rack.objects.filter(name__startswith="ARCH-").count(), 4)
        self.assertEqual(RackItem.objects.filter(device__isnull=False, rack__name__startswith="ARCH-").count(), 23)
        self.login(self.admin)
        r = self.client.get(reverse("architecture:overview"))
        self.assertContains(r, "ARCH-WEB")
        self.assertContains(r, reverse("dcim:rack_detail", args=[Rack.objects.get(name="ARCH-WEB").pk]))

    def test_remove_deletes_only_what_it_created(self):
        from dcim.models import Rack, Room
        mine = dev("web-01-mine", "physical_server")
        real = dev("dc1-spine-01x", site="Mumbai")        # a real device that merely looks similar
        self.run_cmd("--site", "Mumbai")
        self.run_cmd("--remove")
        self.assertEqual(Device.objects.filter(tags__icontains="arch-example").count(), 0)
        self.assertEqual(Device.objects.filter(pk__in=[mine.pk, real.pk]).count(), 2)
        self.assertEqual(Link.objects.count(), 0)
        self.assertFalse(Rack.objects.filter(name__startswith="ARCH-").exists())
        self.assertFalse(Room.objects.filter(name=example.ROOM).exists())
        self.run_cmd("--remove")

    def test_a_real_device_with_a_seed_name_is_never_overwritten_or_removed(self):
        Device.objects.create(name="dc1-fw-01", device_type="firewall", mgmt_ip="192.0.2.99", site="Mumbai", vendor="Real")
        out = self.run_cmd("--site", "Mumbai")
        self.assertIn("real device", out)
        self.assertEqual(Device.objects.get(name="dc1-fw-01").vendor, "Real")
        self.run_cmd("--remove")
        self.assertTrue(Device.objects.filter(name="dc1-fw-01").exists())

    def test_unknown_site_is_an_error(self):
        from django.core.management import CommandError
        with self.assertRaises(CommandError):
            self.run_cmd("--site", "Atlantis")

    def test_button_runs_the_same_code_and_is_permission_gated(self):
        self.login(self.viewer)
        self.client.post(reverse("architecture:example_load"))
        self.assertEqual(Device.objects.filter(tags__icontains="arch-example").count(), 0)
        self.login(self.mum_op)
        r = self.client.post(reverse("architecture:example_load"))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Device.objects.filter(tags__icontains="arch-example", site="Mumbai").count(), 33)
        self.assertTrue(AuditEvent.objects.filter(object_type="architecture.example").exists())
        self.assertEqual(Link.objects.count(), 51)

    def test_global_operator_loads_on_the_first_site(self):
        self.login(self.operator)
        self.client.post(reverse("architecture:example_load"))
        self.assertEqual(Device.objects.filter(tags__icontains="arch-example", site="London").count(), 33)   # first by name
