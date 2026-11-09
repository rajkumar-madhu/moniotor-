"""Regression tests from the code / security review: odd inputs that must never be a 500, example data that must stay
inside the user's scope, and the layout on degenerate graphs."""
import json

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from architecture import csvio, example, layout as lay
from architecture.models import Link
from core.models import AuditEvent
from network.models import Device

from .fixtures import Base, dev, link, make_fabric


class OddInputTests(Base):
    def setUp(self):
        super().setUp()
        self.l1 = link(self.spine, self.leaf, "Eth1/1", "Eth1/49")
        self.login(self.admin)

    def test_unicode_digits_in_ids_are_not_a_server_error(self):
        # "²".isdigit() is True but int("²") raises
        r = self.client.get(reverse("architecture:link_create"), {"a": "²", "b": "٣"})
        self.assertEqual(r.status_code, 200)
        r = self.client.post(reverse("architecture:links_bulk"), {"action": "delete", "ids": ["²", str(10 ** 40)]})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(Link.objects.filter(pk=self.l1.pk).exists())

    def test_huge_ids_in_bulk_and_filters(self):
        r = self.client.post(reverse("architecture:links_bulk"), {"action": "confirm", "ids": [str(2 ** 70)]})
        self.assertEqual(r.status_code, 302)
        for params in ({"per": "9" * 40}, {"page": "9" * 40}, {"q": "x" * 5000}, {"site": "x" * 500}):
            self.assertEqual(self.client.get(reverse("architecture:links"), params).status_code, 200)
            self.assertEqual(self.client.get(reverse("architecture:overview"), params).status_code, 200)

    def test_csv_superscript_digit_speed_is_a_row_error(self):
        p = csvio.validate("a_device,b_device,speed_mbps\ndc1-spine-01,dc1-leaf-02,²\n", self.admin)
        self.assertFalse(p.ok)

    def test_csv_oversized_cell_is_a_clean_error(self):
        text = "a_device,b_device,note\ndc1-spine-01,dc1-leaf-02," + "x" * 200_000 + "\n"
        p = csvio.validate(text, self.admin)
        self.assertFalse(p.ok)
        r = self.client.post(reverse("architecture:links_import"),
                             {"file": SimpleUploadedFile("l.csv", text.encode(), "text/csv")})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Link.objects.count(), 1)

    def test_csv_binary_and_nul_content(self):
        for raw in (b"\x00\x01\x02\xff\xfe", b"a_device,b_device\n\x00,\x00\n", b'a_device,b_device\n"unterminated,x\n'):
            r = self.client.post(reverse("architecture:links_import"), {"file": SimpleUploadedFile("l.csv", raw, "text/csv")})
            self.assertEqual(r.status_code, 200)
        self.assertEqual(Link.objects.count(), 1)

    def test_csv_names_that_differ_only_in_case_are_not_guessed(self):
        Device.objects.create(name="SW-Case", device_type="switch", site="Mumbai", mgmt_ip="192.0.2.77")
        Device.objects.create(name="sw-case", device_type="switch", site="Mumbai", mgmt_ip="192.0.2.78")
        p = csvio.validate("a_device,b_device\nSW-Case,dc1-leaf-02\nSW-CASE,dc1-leaf-02\n", self.admin)
        self.assertEqual(len(p.rows), 1)                     # the exact spelling works
        self.assertEqual(p.rows[0]["a"].name, "SW-Case")
        self.assertEqual(len(p.errors), 1)                   # the ambiguous one is refused, not guessed
        self.assertEqual(p.errors[0][0], 3)


class ExampleScopeTests(Base):
    """The example loader runs for operators of one site: it must not reach into other sites or real devices."""

    def test_a_real_device_with_a_seed_name_gets_no_links_and_no_rack_item(self):
        Device.objects.filter(pk=self.spine.pk).update(name="mumbai-spine")     # free the seed name
        real = Device.objects.create(name="dc1-spine-01", device_type="switch", site="London", mgmt_ip="192.0.2.252")
        n = example.load("Mumbai", user=self.mum_op)
        self.assertGreater(n["skipped"], 0)
        self.assertFalse(Link.objects.filter(a_device=real).exists() or Link.objects.filter(b_device=real).exists())
        real.refresh_from_db()
        self.assertEqual(real.site, "London")
        self.assertNotIn("arch-example", real.tags)

    def test_example_devices_on_another_site_are_not_moved(self):
        example.load("London")
        before = set(Device.objects.filter(tags__icontains="arch-example").values_list("pk", "site"))
        self.assertTrue(before)
        example.load("Mumbai", user=self.mum_op)
        after = set(Device.objects.filter(tags__icontains="arch-example", site="London").values_list("pk", "site"))
        self.assertEqual(after, before)


class DegenerateLayoutTests(Base):
    def test_layout_tolerates_self_edges_unknown_ids_cycles_and_parallel_links(self):
        nodes = [lay.LNode("1", "spine", "a"), lay.LNode("2", "spine", "b"), lay.LNode("3", "leaf", "c")]
        edges = [lay.LEdge("e1", "1", "1"), lay.LEdge("e2", "1", "99"), lay.LEdge("e3", "1", "2"), lay.LEdge("e4", "2", "1"),
                 lay.LEdge("e5", "2", "3"), lay.LEdge("e6", "3", "1"), lay.LEdge("e7", "1", "3"), lay.LEdge("e8", "1", "3")]
        res = lay.layout(nodes, edges, [], lay.Options())
        self.assertEqual(set(res.nodes), {"1", "2", "3"})
        for p in res.paths:
            self.assertNotIn("nan", p.d.lower())

    def test_empty_and_single_tier(self):
        res = lay.layout([], [], [], lay.Options())
        self.assertEqual(len(res.nodes), 0)
        res = lay.layout([lay.LNode(str(i), "leaf", f"l{i}") for i in range(3)], [lay.LEdge("x", "0", "1")], [], lay.Options())
        self.assertEqual(len(res.nodes), 3)

    def test_page_with_only_links_to_a_hidden_device_and_a_removed_device(self):
        a, b = dev("only-a"), dev("only-b")
        link(a, b, "1", "2")
        from architecture.models import Placement
        Placement.objects.create(device=b, hidden=True)
        self.login(self.admin)
        r = self.client.get(reverse("architecture:overview"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["kpis"]["links"], 0)
        b.delete()
        self.assertEqual(self.client.get(reverse("architecture:overview")).status_code, 200)

    def test_database_refuses_a_self_link_and_unordered_ends(self):
        from django.db import IntegrityError, transaction
        with self.assertRaises(IntegrityError), transaction.atomic():
            Link.objects.bulk_create([Link(a_device=self.spine, b_device=self.spine, a_port="x")])
        with self.assertRaises(IntegrityError), transaction.atomic():
            hi, lo = sorted([self.spine, self.leaf], key=lambda d: -d.pk)
            Link.objects.bulk_create([Link(a_device=hi, b_device=lo)])


class QueryCountTests(Base):
    def count(self, name):
        self.login(self.admin)
        with CaptureQueriesContext(connection) as ctx:
            r = self.client.get(reverse(name))
        self.assertEqual(r.status_code, 200)
        return len(ctx)

    def test_live_json_and_links_list_do_not_grow_with_the_fabric(self):
        link(self.spine, self.leaf, "Eth1/1", "Eth1/49")
        small = {n: self.count(f"architecture:{n}") for n in ("live", "links", "overview")}
        make_fabric(6, 30, 5)
        for n, base in small.items():
            self.assertLessEqual(self.count(f"architecture:{n}"), base + 2, n)


class LeakTests(Base):
    """A link with an invisible end must never surface, whatever the route."""

    def setUp(self):
        super().setUp()
        self.secret = dev("secret-london-fw", "firewall", site="London")
        link(self.mum_dev(), self.secret, "Eth9", "Eth9", note="private-note-zzz")

    def mum_dev(self):
        return self.spine

    def test_no_route_names_the_invisible_end(self):
        self.login(self.mum_op)
        pages = [reverse("architecture:overview"), reverse("architecture:live"), reverse("architecture:links"),
                 reverse("architecture:links_export"), reverse("architecture:suggest") + "?find=1",
                 reverse("architecture:overview") + "?view=list", reverse("architecture:links") + "?q=secret"]
        for url in pages:
            r = self.client.get(url)
            body = r.content.decode()
            self.assertNotIn("secret-london-fw", body, url)
            self.assertNotIn("private-note-zzz", body, url)

    def test_csv_import_error_does_not_confirm_the_invisible_device_exists(self):
        self.login(self.mum_op)
        r = self.client.post(reverse("architecture:links_import"), {"file": SimpleUploadedFile(
            "l.csv", b"a_device,b_device\ndc1-spine-01,secret-london-fw\ndc1-spine-01,does-not-exist\n", "text/csv")})
        text = r.content.decode()
        self.assertIn("No device named", text)
        # both look identical to the user: the same message for a hidden and a missing device
        self.assertEqual(text.count("that you can see"), 2)


class LldpScopeTests(Base):
    def test_rows_between_devices_the_user_cannot_see_are_not_counted(self):
        from architecture import lldp
        from .test_lldp import series
        Device.objects.filter(pk=self.spine.pk).update(prometheus_instance="192.0.2.251", mgmt_ip="192.0.2.251")
        Device.objects.filter(pk=self.lon_sw.pk).update(prometheus_instance="192.0.2.252", mgmt_ip="192.0.2.252")
        data = series([("192.0.2.252", "1", "1", "some-other-london-box", "x", ""),     # invisible to a Mumbai user
                       ("192.0.2.251", "2", "1", "stranger", "y", "")])                 # a visible device, unknown neighbour
        visible = list(Device.objects.filter(site="Mumbai"))
        res = lldp.parse(data, visible)
        self.assertEqual((res.rows, res.unknown, len(res.suggestions)), (1, 1, 0))
