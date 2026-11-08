from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from architecture.forms import LinkForm
from architecture.models import Link, Placement
from architecture.ports import clean_port, port_key
from network.models import Device

from .fixtures import Base, dev, link


class PortTests(Base):
    def test_clean_port(self):
        self.assertEqual(clean_port("  Eth1/1\x00\x07  "), "Eth1/1")
        self.assertEqual(clean_port("Eth 1/1\n\t x"), "Eth 1/1 x")
        with self.assertRaises(ValueError):
            clean_port("x" * 61)

    def test_port_key_spellings(self):
        self.assertEqual(port_key("Ethernet1/49"), port_key("eth 1/49"))
        self.assertEqual(port_key("Et1/49"), "eth1/49")
        self.assertEqual(port_key("TenGigabitEthernet1/0/1"), port_key("Te1/0/1"))
        self.assertNotEqual(port_key("Eth1/1"), port_key("Eth1/2"))


class LinkModelTests(Base):
    def test_ends_are_normalised_so_ab_equals_ba(self):
        a, b = sorted([self.spine, self.leaf], key=lambda d: d.pk)
        l1 = Link(a_device=b, a_port="Eth1/1", b_device=a, b_port="Eth1/49")
        l1.save()
        self.assertEqual((l1.a_device_id, l1.a_port, l1.b_device_id, l1.b_port), (a.pk, "Eth1/49", b.pk, "Eth1/1"))
        with self.assertRaises(IntegrityError), transaction.atomic():
            Link(a_device=a, a_port="Eth1/49", b_device=b, b_port="Eth1/1").save()

    def test_self_link_is_refused(self):
        with self.assertRaisesMessage(ValidationError, "itself"):
            Link(a_device=self.leaf, b_device=self.leaf).save()

    def test_database_enforces_the_order(self):
        a, b = sorted([self.spine, self.leaf], key=lambda d: d.pk)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Link.objects.bulk_create([Link(a_device=b, b_device=a)])
        with self.assertRaises(IntegrityError), transaction.atomic():
            Link.objects.bulk_create([Link(a_device=a, b_device=a)])

    def test_parallel_links_with_different_ports_are_fine_and_empty_ports_are_unique(self):
        link(self.spine, self.leaf, "Eth1/1", "Eth1/49")
        link(self.spine, self.leaf, "Eth1/2", "Eth1/50")
        link(self.spine, self.leaf)
        with self.assertRaises(IntegrityError), transaction.atomic():
            link(self.spine, self.leaf)

    def test_cascade_with_the_device(self):
        link(self.spine, self.leaf)
        self.spine.delete()
        self.assertEqual(Link.objects.count(), 0)

    def test_clean_checks_tenant(self):
        other = Device.objects.create(name="other-tenant", tenant_key="other", mgmt_ip="192.0.2.9")
        with self.assertRaisesMessage(ValidationError, "same tenant"):
            Link(a_device=self.leaf, b_device=other).full_clean()

    def test_kind_is_tidied_and_str(self):
        l = link(self.spine, self.leaf, "Eth1/1", "Eth1/49", kind="  up   link ")
        l.refresh_from_db()
        self.assertEqual(l.kind, "up link")
        self.assertIn("↔", str(l))


class LinkFormTests(Base):
    def form(self, user, **data):
        base = {"a_device": self.spine.pk, "b_device": self.leaf.pk, "a_port": "Eth1/1", "b_port": "Eth1/49", "medium": "fibre",
                "confirmed": "on"}
        base.update(data)
        return LinkForm(base, user=user)

    def test_valid(self):
        self.assertTrue(self.form(self.operator).is_valid())

    def test_self_link(self):
        f = self.form(self.operator, b_device=self.spine.pk)
        self.assertFalse(f.is_valid())
        self.assertIn("itself", str(f.errors))

    def test_port_length_and_control_characters(self):
        self.assertIn("60 characters", str(self.form(self.operator, a_port="x" * 61).errors))
        self.assertIn("a_port", self.form(self.operator, a_port="Eth\x001").errors)
        self.assertIn("control", str(self.form(self.operator, a_port="Eth\x071").errors))
        self.assertIn("control", str(self.form(self.operator, note="a\x07b").errors))

    def test_speed_bounds(self):
        self.assertFalse(self.form(self.operator, speed_mbps="0").is_valid())
        self.assertFalse(self.form(self.operator, speed_mbps="99999999999").is_valid())
        self.assertTrue(self.form(self.operator, speed_mbps="100000").is_valid())

    def test_duplicate_is_spelling_independent_and_order_independent(self):
        link(self.spine, self.leaf, "Ethernet1/1", "Eth1/49")
        f = self.form(self.operator, a_device=self.leaf.pk, b_device=self.spine.pk, a_port="eth1/49", b_port="Eth 1/1")
        self.assertFalse(f.is_valid())
        self.assertIn("already linked", str(f.errors))

    def test_other_site_devices_are_not_even_choices(self):
        f = self.form(self.mum_op, b_device=self.lon_sw.pk)
        self.assertFalse(f.is_valid())
        self.assertIn("b_device", f.errors)

    def test_viewer_cannot_pick_devices(self):
        f = self.form(self.viewer)
        self.assertFalse(f.is_valid())


class PlacementTests(Base):
    def test_is_empty(self):
        self.assertTrue(Placement(device=self.leaf).is_empty)
        self.assertFalse(Placement(device=self.leaf, hidden=True).is_empty)
        self.assertFalse(Placement(device=self.leaf, sort_order=3).is_empty)

    def test_one_per_device(self):
        Placement.objects.create(device=self.leaf, tier="spine")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Placement.objects.create(device=self.leaf, tier="leaf")
