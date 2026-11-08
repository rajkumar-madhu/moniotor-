"""Code / security / test lens review: adversarial probes that were not covered elsewhere."""
import json
import re

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from architecture import csvio
from architecture.models import Link, Placement
from network.models import Device

from .fixtures import Base, dev, link, make_fabric

HOSTILE = "<script>alert(1)</script>\"'&‮\x07"
NEEDLE = "<script>alert(1)</script>"


class HostileTextEverywhere(Base):
    """A device name, port, note and kind full of markup must be escaped on every page that shows them."""

    def setUp(self):
        super().setUp()
        self.evil = dev("evil-" + HOSTILE.replace("\x07", ""), "switch", site="Mumbai", vendor=NEEDLE, model="<b>x</b>" + NEEDLE,
                        role=NEEDLE, tags=NEEDLE)
        self.l = link(self.evil, self.spine, "Eth<script>1", "Eth\"2'", note=NEEDLE, kind="<i>k</i>")

    def test_no_page_echoes_raw_markup(self):
        self.login(self.admin)
        urls = [reverse("architecture:overview"), reverse("architecture:overview") + "?view=list",
                reverse("architecture:overview") + "?present=1&lc=util", reverse("architecture:links"),
                reverse("architecture:link_edit", args=[self.l.pk]), reverse("architecture:link_create"),
                reverse("architecture:placement_edit", args=[self.evil.pk]), reverse("architecture:links_import"),
                reverse("architecture:suggest")]
        for url in urls:
            body = self.client.get(url).content.decode()
            self.assertNotIn(NEEDLE, body, url)
        # the JSON blocks are json_script: "<" is written as <
        body = self.client.get(reverse("architecture:overview")).content.decode()
        blocks = re.findall(r'<script id="ar-(?:data|conf)" type="application/json">(.*?)</script>', body, re.S)
        self.assertEqual(len(blocks), 2)
        for b in blocks:
            self.assertNotIn("<", b)
            json.loads(b)

    def test_live_json_is_json_and_carries_only_ids_and_states(self):
        self.login(self.admin)
        r = self.client.get(reverse("architecture:live"))
        self.assertEqual(r["Content-Type"], "application/json")
        self.assertNotIn(b"script", r.content.lower())

    def test_import_error_echo_is_escaped(self):
        self.login(self.admin)
        csv = ("a_device,b_device\n\"" + NEEDLE + "\",dc1-spine-01\n").encode()
        r = self.client.post(reverse("architecture:links_import"), {"file": SimpleUploadedFile("l.csv", csv, "text/csv")})
        self.assertNotIn(NEEDLE, r.content.decode())

    def test_flash_message_with_hostile_names_is_escaped(self):
        self.login(self.admin)
        r = self.client.post(reverse("architecture:link_create"), {
            "a_device": self.evil.pk, "b_device": self.leaf.pk, "a_port": "p1", "b_port": "p2", "medium": "fibre", "confirmed": "on"},
            follow=True)
        self.assertNotIn(NEEDLE, r.content.decode())

    def test_export_neutralises_formulas_in_every_text_column(self):
        for v in ("=1+1", "+1+1", "-2+3", "@SUM(A1)", "\tcmd", "\rcmd"):
            self.assertTrue(csvio.csv_safe(v).startswith("'"), v)
        self.assertEqual(csvio.csv_safe("-5"), "-5")
        Link.objects.filter(pk=self.l.pk).update(note="=HYPERLINK(\"http://x\")", kind="@x")
        Device.objects.filter(pk=self.evil.pk).update(name="=cmd|' /C calc'!A0")
        self.login(self.admin)
        body = self.client.get(reverse("architecture:links_export")).content.decode()
        for line in body.splitlines()[1:]:
            for cell in line.split(","):
                self.assertFalse(bool(cell) and cell[0] in "=+-@" and not re.fullmatch(r"-?[\d.]+", cell), cell)


class Limits(Base):
    def test_a_huge_expand_parameter_does_not_blow_up_the_page(self):
        make_fabric(2, 4, 2)
        self.login(self.admin)
        expand = ",".join(f"group-{i}" for i in range(3000))
        r = self.client.get(reverse("architecture:overview"), {"expand": expand})
        self.assertEqual(r.status_code, 200)
        self.assertLess(len(r.content), 600_000)

    def test_unclassified_devices_do_not_cost_a_query_each(self):
        Device.objects.bulk_create([Device(name=f"ap-{i}", device_type="wireless", site="Mumbai", mgmt_ip="192.0.2.9") for i in range(5)])
        self.login(self.mum_op)
        with CaptureQueriesContext(connection) as small:
            self.client.get(reverse("architecture:overview"))
        Device.objects.bulk_create([Device(name=f"ap2-{i}", device_type="wireless", site="Mumbai", mgmt_ip="192.0.2.9") for i in range(150)])
        with CaptureQueriesContext(connection) as big:
            r = self.client.get(reverse("architecture:overview"))
        self.assertEqual(r.status_code, 200)
        self.assertLessEqual(len(big), len(small) + 2)

    def test_list_view_and_live_do_not_grow_for_a_site_operator(self):
        link(self.spine, self.leaf, "1", "2")
        self.login(self.mum_op)
        small = {}
        for name, params in (("overview", {"view": "list"}), ("live", {}), ("links", {}), ("suggest", {"find": "1"})):
            with CaptureQueriesContext(connection) as ctx:
                self.client.get(reverse(f"architecture:{name}"), params)
            small[name] = len(ctx)
        make_fabric(6, 30, 6)
        for name, params in (("overview", {"view": "list"}), ("live", {}), ("links", {}), ("suggest", {"find": "1"})):
            with CaptureQueriesContext(connection) as ctx:
                r = self.client.get(reverse(f"architecture:{name}"), params)
            self.assertEqual(r.status_code, 200)
            self.assertLessEqual(len(ctx), small[name] + 2, name)


class DataFixes(Base):
    def test_page_survives_links_whose_tenant_or_ends_were_edited_in_the_database(self):
        l = link(self.spine, self.leaf, "1", "2")
        Link.objects.filter(pk=l.pk).update(tenant_key="other-tenant")
        self.login(self.admin)
        r = self.client.get(reverse("architecture:overview"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["kpis"]["links"], 0)
        self.assertEqual(self.client.get(reverse("architecture:link_edit", args=[l.pk])).status_code, 404)

    def test_other_tenant_placement_and_link_are_ignored(self):
        Placement.objects.create(tenant_key="other-tenant", device=self.srv, tier="spine")
        self.login(self.admin)
        r = self.client.get(reverse("architecture:overview"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["kpis"]["tiers"]["spine"], 1)      # dc1-spine-01 only: the foreign placement did not move web-01
        self.assertEqual(r.context["kpis"]["tiers"]["server"], 1)


class VisibleButNotManageable(Base):
    """A site operator can SEE a link to a global ("All Sites") device but may not change it: the object-level check,
    not only the role decorator, has to refuse."""

    def setUp(self):
        super().setUp()
        Device.objects.filter(pk=self.leaf.pk).update(site="All Sites")
        self.l = link(self.spine, self.leaf, "p1", "p2")

    def test_edit_delete_and_bulk_are_refused_and_audit_nothing(self):
        from core.models import AuditEvent as AuditLog
        self.login(self.mum_op)
        before = AuditLog.objects.count()
        self.assertEqual(self.client.get(reverse("architecture:link_edit", args=[self.l.pk])).status_code, 403)
        self.assertEqual(self.client.post(reverse("architecture:link_edit", args=[self.l.pk]), {
            "a_device": self.spine.pk, "b_device": self.leaf.pk, "a_port": "p1", "b_port": "p2", "medium": "copper"}).status_code, 403)
        self.assertEqual(self.client.post(reverse("architecture:link_delete", args=[self.l.pk])).status_code, 403)
        self.client.post(reverse("architecture:links_bulk"), {"action": "delete", "ids": [self.l.pk]})
        self.client.post(reverse("architecture:links_bulk"), {"action": "confirm", "ids": [self.l.pk]})
        self.assertTrue(Link.objects.filter(pk=self.l.pk, medium="fibre").exists())
        self.assertEqual(AuditLog.objects.count(), before)

    def test_a_tenant_wide_operator_can(self):
        self.login(self.operator)
        self.assertEqual(self.client.post(reverse("architecture:link_delete", args=[self.l.pk])).status_code, 302)
        self.assertFalse(Link.objects.filter(pk=self.l.pk).exists())


class BigImport(Base):
    def test_a_5000_row_file_validates_and_commits_with_few_queries_per_row(self):
        make_fabric(2, 2, 1)
        names = [f"imp-{i:04d}" for i in range(5000)]
        Device.objects.bulk_create([Device(name=n, device_type="physical_server", site="Mumbai", mgmt_ip="192.0.2.5") for n in names])
        rows = "\n".join(f"spine-00,{n},Eth1/{i % 60},eth0" for i, n in enumerate(names))
        text = "a_device,b_device,a_port,b_port\n" + rows + "\n"
        with CaptureQueriesContext(connection) as ctx:
            plan = csvio.validate(text, self.admin)
        self.assertTrue(plan.ok, plan.errors[:3])
        self.assertEqual(plan.create_count, 5000)
        self.assertLess(len(ctx), 50, "validation should not run a query per row")
        created, _ = csvio.commit(plan)
        self.assertEqual(len(created), 5000)
        again = csvio.validate(text, self.admin, update_existing=True)
        self.assertEqual(again.same_count, 5000)
