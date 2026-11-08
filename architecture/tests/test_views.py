import json
from unittest import mock

from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from architecture import services
from architecture.models import Link, Placement
from core import prometheus
from core.models import AuditEvent
from network.models import Device, Interface

from .fixtures import Base, dev, link, make_fabric

PROM = override_settings(PROMETHEUS_URL="http://prometheus.test:9090")


class PageTests(Base):
    def setUp(self):
        super().setUp()
        link(self.spine, self.leaf, "Eth1/1", "Eth1/49", speed_mbps=100000)
        link(self.leaf, self.srv, "Eth1/1", "eth0", speed_mbps=25000)
        link(self.spine, self.lon_sw, "Eth1/9", "Eth1/9")        # one end in another site

    def get(self, user, path="overview", **params):
        self.login(user)
        return self.client.get(reverse(f"architecture:{path}"), params)

    def test_renders_for_a_viewer_with_tiers_nodes_and_links(self):
        r = self.get(self.viewer)
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn("Data Center Architecture", html)
        for name in ("dc1-spine-01", "dc1-leaf-01", "web-01"):
            self.assertIn(f'aria-label="{name}, ', html)
        self.assertIn('class="band band-spine', html)
        self.assertIn("<title", html)
        self.assertNotIn("Add link", html)          # a viewer cannot edit
        self.assertEqual(r.context["kpis"]["links"], 3)

    def test_tier_chips_read_servers_not_server_groups_and_oversubscription_is_worded_cleanly(self):
        r = self.get(self.viewer)
        labels = [c["label"] for c in r.context["chips"]]
        self.assertIn("Servers", labels)
        self.assertNotIn("Server groups", labels)
        html = r.content.decode()
        self.assertNotIn("leafves", html)
        self.assertRegex(html, r'data-kpi="over_sub"[^>]*>\s*1 leaf · worst ')

    def test_links_page_default_page_size_is_one_the_pager_offers(self):
        self.login(self.admin)
        html = self.client.get(reverse("architecture:links")).content.decode()
        self.assertIn('<option value="20" selected>', html)

    def test_other_site_user_neither_sees_nor_counts_the_cross_site_link(self):
        r = self.get(self.mum_viewer)
        self.assertNotIn("lon-core-01", r.content.decode())
        self.assertEqual(r.context["kpis"]["links"], 2)
        self.assertEqual(r.context["kpis"]["devices"], 4)
        r = self.get(self.lon_op)
        self.assertEqual(r.context["kpis"]["links"], 0)
        self.assertEqual(r.context["kpis"]["devices"], 1)
        self.assertNotIn("dc1-spine-01", r.content.decode())

    def test_site_and_environment_filters(self):
        r = self.get(self.admin, site="London")
        self.assertEqual(r.context["kpis"]["devices"], 1)
        Device.objects.filter(pk=self.srv.pk).update(environment="dr")
        r = self.get(self.admin, env="dr")
        self.assertEqual(r.context["kpis"]["devices"], 1)
        r = self.get(self.admin, site="<script>", env="bogus")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("<script>alert", r.content.decode())

    def test_site_outside_the_users_scope_is_ignored(self):
        r = self.get(self.mum_viewer, site="London")
        self.assertEqual(r.context["f"]["site"], "")

    def test_list_view(self):
        r = self.get(self.viewer, view="list")
        html = r.content.decode()
        self.assertIn("ar-list", html)
        self.assertIn("dc1-spine-01", html)
        self.assertNotIn("<svg id=\"ar-svg\"", html)

    def test_empty_state_offers_the_example_only_to_operators(self):
        Device.objects.all().delete()
        r = self.get(self.operator)
        self.assertTrue(r.context["empty"])
        self.assertIn("Load example fabric", r.content.decode())
        r = self.get(self.viewer)
        self.assertNotIn("Load example fabric", r.content.decode())

    def test_hidden_devices_are_not_drawn(self):
        Placement.objects.create(device=self.leaf2, hidden=True)
        Placement.objects.create(device=self.srv, hidden=True)
        r = self.get(self.viewer)
        self.assertNotIn('aria-label="dc1-leaf-02, ', r.content.decode())
        self.assertEqual(r.context["kpis"]["hidden"], 2)
        self.assertEqual(r.context["kpis"]["links"], 2)        # the server link is not drawn nor counted

    def test_prometheus_off_banner(self):
        r = self.get(self.viewer)
        self.assertEqual(r.context["prom"]["state"], "off")
        self.assertIn("Prometheus is not connected", r.content.decode())

    @PROM
    def test_prometheus_down_banner_still_renders(self):
        with mock.patch.object(prometheus, "query", side_effect=prometheus.PrometheusError("boom")):
            r = self.get(self.viewer)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["prom"]["state"], "down")
        self.assertIn("not answering", r.content.decode())

    @PROM
    def test_prometheus_ok_has_no_banner_and_is_cached(self):
        with mock.patch.object(prometheus, "query", return_value=[{"metric": {}, "value": 1.0}]) as q:
            r = self.get(self.viewer)
            self.get(self.viewer)
        self.assertEqual(r.context["prom"]["state"], "ok")
        self.assertNotIn("data-prom", r.content.decode().split('id="ar-prom"')[1].split("</div>")[0])
        self.assertEqual(q.call_count, 1)

    def test_photo_is_the_stored_one_and_never_a_hotlink(self):
        from network.images import store_image
        import io, struct, zlib
        def png():
            raw = b"\x00\xff\xff\xff" * 1
            chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
            return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
        img, _ = store_image(png(), source="upload")
        Device.objects.filter(pk=self.spine.pk).update(image=img, product_image_url="https://vendor.example/x.png")
        html = self.get(self.viewer).content.decode()
        self.assertIn(img.url, html)
        self.assertNotIn("vendor.example", html)
        self.assertIn('class="nd-face" href="#fc-f-switch-', html)        # the other devices draw their generated face

    def test_login_required(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("architecture:overview")).status_code, 302)
        self.assertEqual(self.client.get(reverse("architecture:live")).status_code, 302)

    def test_query_count_does_not_grow_with_the_fabric(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        self.login(self.admin)
        with CaptureQueriesContext(connection) as small:
            self.client.get(reverse("architecture:overview"))
        make_fabric(8, 24, 3)
        with CaptureQueriesContext(connection) as big:
            r = self.client.get(reverse("architecture:overview"))
        self.assertGreater(r.context["kpis"]["links"], 190)
        self.assertLessEqual(len(big), len(small) + 2, [q["sql"][:80] for q in big])


class LargePageTests(Base):
    def test_large_fabric_renders_collapsed_and_quickly(self):
        import time
        make_fabric(16, 48, 2)
        self.login(self.admin)
        t = time.perf_counter()
        r = self.client.get(reverse("architecture:overview"))
        elapsed = time.perf_counter() - t
        self.assertEqual(r.status_code, 200)
        self.assertLess(elapsed, 5.0)
        self.assertEqual(r.context["kpis"]["links"], 16 * 48 + 96)
        self.assertTrue(r.context["aggs"])
        self.assertTrue(any("compact chips" in n["text"] for n in r.context["notes"]))
        r2 = self.client.get(reverse("architecture:overview"), {"expand": "leaf"})
        self.assertEqual(r2.status_code, 200)


class LiveTests(Base):
    def setUp(self):
        super().setUp()
        self.l1 = link(self.spine, self.leaf, "Eth1/1", "Eth1/49", speed_mbps=100000)
        self.l2 = link(self.spine, self.lon_sw, "Eth1/9", "Eth1/9")

    def live(self, user, **params):
        self.login(user)
        return self.client.get(reverse("architecture:live"), params)

    def test_shape(self):
        Device.objects.filter(pk__in=[self.spine.pk, self.leaf.pk]).update(status="up")
        r = self.live(self.viewer)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Cache-Control"], "no-store")
        j = r.json()
        self.assertEqual(set(j), {"generated_at", "prometheus", "devices", "links", "summary"})
        self.assertEqual(j["devices"][str(self.spine.pk)], {"status": "up", "state": "up", "state_label": "Up"})
        self.assertEqual(set(j["links"][str(self.l1.pk)]), {"state", "util", "tone", "oper_a", "oper_b", "rate_a", "rate_b"})
        self.assertEqual(j["links"][str(self.l1.pk)]["state"], "up")
        self.assertEqual(j["links"][str(self.l2.pk)]["state"], "unknown")
        self.assertEqual(set(j["summary"]), {"devices_down", "devices_up", "devices_unknown", "links_up", "links_down", "links_unknown", "links", "devices", "over"})
        self.assertEqual(j["prometheus"]["state"], "off")

    def test_scoped_to_the_user(self):
        j = self.live(self.mum_viewer).json()
        self.assertNotIn(str(self.lon_sw.pk), j["devices"])
        self.assertNotIn(str(self.l2.pk), j["links"])
        j = self.live(self.lon_op).json()
        self.assertEqual(list(j["devices"]), [str(self.lon_sw.pk)])
        self.assertEqual(j["links"], {})

    def test_state_changes_show_up(self):
        Device.objects.filter(pk=self.spine.pk).update(status="up")
        Device.objects.filter(pk=self.leaf.pk).update(status="down")
        j = self.live(self.viewer).json()
        self.assertEqual(j["links"][str(self.l1.pk)]["state"], "down")
        self.assertEqual(j["summary"]["devices_down"], 1)
        Device.objects.filter(pk=self.leaf.pk).update(enabled=False, status="unknown")
        j = self.live(self.viewer).json()
        self.assertEqual(j["devices"][str(self.leaf.pk)]["state"], "disabled")
        self.assertEqual(j["links"][str(self.l1.pk)]["state"], "unknown")

    def test_interface_state_and_utilisation(self):
        Device.objects.filter(pk__in=[self.spine.pk, self.leaf.pk]).update(status="up")
        now = timezone.now()
        Interface.objects.create(device=self.spine, if_index=1, name="Ethernet1/1", oper_status="up", utilization=72.0, speed_mbps=100000, updated_at=now)
        Interface.objects.create(device=self.leaf, if_index=49, name="Eth1/49", oper_status="up", utilization=10.0, updated_at=now)
        j = self.live(self.viewer).json()["links"][str(self.l1.pk)]
        self.assertEqual((j["state"], j["util"], j["tone"]), ("up", 72.0, "warn"))
        Interface.objects.filter(device=self.leaf).update(oper_status="down")
        self.assertEqual(self.live(self.viewer).json()["links"][str(self.l1.pk)]["state"], "down")
        # old figures are no data, not "current"
        Interface.objects.update(updated_at=now - timezone.timedelta(hours=2))
        j = self.live(self.viewer).json()["links"][str(self.l1.pk)]
        self.assertEqual((j["state"], j["util"]), ("up", None))

    def test_disabled_device_interface_rates_are_not_current(self):
        Interface.objects.create(device=self.spine, if_index=1, name="Eth1/1", oper_status="up", utilization=90.0, updated_at=timezone.now())
        Device.objects.filter(pk=self.spine.pk).update(enabled=False)
        self.assertIsNone(self.live(self.viewer).json()["links"][str(self.l1.pk)]["util"])

    def test_oversubscription(self):
        sp, lf, sv = make_fabric(2, 2, 2)
        self.login(self.admin)
        j = self.client.get(reverse("architecture:live")).json()
        over = j["summary"]["over"]
        self.assertIsNotNone(over)
        self.assertEqual(over["ratio"], round(2 * 25000 / (2 * 100000), 2))

    @PROM
    def test_prometheus_error_is_reported_not_raised(self):
        with mock.patch.object(prometheus, "query", side_effect=prometheus.PrometheusError("down")):
            j = self.live(self.viewer).json()
        self.assertEqual(j["prometheus"]["state"], "down")


class LinkCrudTests(Base):
    def setUp(self):
        super().setUp()
        self.l1 = link(self.spine, self.leaf, "Eth1/1", "Eth1/49")
        self.l_cross = link(self.spine, self.lon_sw, "Eth1/9", "Eth1/9")

    def data(self, **kw):
        base = {"a_device": self.leaf.pk, "b_device": self.leaf2.pk, "a_port": "Eth1/2", "b_port": "Eth1/2", "medium": "dac",
                "speed_mbps": "40000", "kind": "interconnect", "note": "mlag", "confirmed": "on"}
        base.update(kw)
        return base

    def test_viewer_cannot_add_edit_delete_or_import(self):
        self.login(self.viewer)
        # viewers are the no-membership default: they hold "viewer", so every operate view is refused
        for name, args in (("link_create", []), ("link_edit", [self.l1.pk]), ("links_import", []), ("suggest", [])):
            self.assertEqual(self.client.get(reverse(f"architecture:{name}", args=args)).status_code, 403, name)
        for name, args in (("link_delete", [self.l1.pk]), ("links_bulk", []), ("example_load", [])):
            r = self.client.post(reverse(f"architecture:{name}", args=args), {"ids": [self.l1.pk], "action": "delete"})
            self.assertEqual(r.status_code, 302, name)
        self.assertTrue(Link.objects.filter(pk=self.l1.pk).exists())
        self.client.post(reverse("architecture:link_create"), self.data())
        self.assertEqual(Link.objects.count(), 2)

    def test_create_audits_and_normalises(self):
        self.login(self.operator)
        r = self.client.post(reverse("architecture:link_create"), self.data(a_device=self.leaf2.pk, b_device=self.leaf.pk))
        self.assertEqual(r.status_code, 302)
        l = Link.objects.exclude(pk__in=[self.l1.pk, self.l_cross.pk]).get()
        self.assertLess(l.a_device_id, l.b_device_id)
        self.assertEqual(l.origin, "manual")
        a = AuditEvent.objects.get(action="create", object_type="architecture.link")
        self.assertEqual(a.object_id, str(l.pk))

    def test_edit_and_delete_audit(self):
        self.login(self.operator)
        r = self.client.post(reverse("architecture:link_edit", args=[self.l1.pk]),
                             self.data(a_device=self.l1.a_device_id, b_device=self.l1.b_device_id, a_port=self.l1.a_port, b_port=self.l1.b_port, speed_mbps="100000"))
        self.assertEqual(r.status_code, 302)
        self.l1.refresh_from_db()
        self.assertEqual(self.l1.speed_mbps, 100000)
        self.assertTrue(AuditEvent.objects.filter(action="update", object_type="architecture.link").exists())
        self.client.post(reverse("architecture:link_delete", args=[self.l1.pk]))
        self.assertFalse(Link.objects.filter(pk=self.l1.pk).exists())
        self.assertTrue(AuditEvent.objects.filter(action="delete", object_type="architecture.link").exists())

    def test_get_is_not_a_mutation(self):
        self.login(self.operator)
        self.client.get(reverse("architecture:link_delete", args=[self.l1.pk]))     # 405
        self.assertTrue(Link.objects.filter(pk=self.l1.pk).exists())

    def test_csrf_is_enforced(self):
        from django.test import Client
        c = Client(enforce_csrf_checks=True)
        c.force_login(self.operator)
        self.assertEqual(c.post(reverse("architecture:link_delete", args=[self.l1.pk])).status_code, 403)
        self.assertTrue(Link.objects.filter(pk=self.l1.pk).exists())

    def test_idor_cross_site_link_is_invisible_and_untouchable(self):
        self.login(self.mum_op)
        for name in ("link_edit",):
            self.assertEqual(self.client.get(reverse(f"architecture:{name}", args=[self.l_cross.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("architecture:link_delete", args=[self.l_cross.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("architecture:links_bulk"), {"action": "delete", "ids": [self.l_cross.pk]}).status_code, 302)
        self.assertTrue(Link.objects.filter(pk=self.l_cross.pk).exists())
        self.assertEqual(self.client.get(reverse("architecture:link_edit", args=[self.l1.pk])).status_code, 200)

    def test_viewer_on_site_sees_but_cannot_change_the_link(self):
        self.login(self.mum_viewer)
        self.assertEqual(self.client.get(reverse("architecture:links")).status_code, 200)
        self.assertEqual(self.client.get(reverse("architecture:link_edit", args=[self.l1.pk])).status_code, 403)
        self.client.post(reverse("architecture:link_delete", args=[self.l1.pk]))
        self.assertTrue(Link.objects.filter(pk=self.l1.pk).exists())

    def test_operator_of_one_site_cannot_link_to_a_device_of_another(self):
        self.login(self.mum_op)
        r = self.client.post(reverse("architecture:link_create"), self.data(b_device=self.lon_sw.pk))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Link.objects.count(), 2)

    def test_global_operator_can_link_across_sites(self):
        self.login(self.operator)
        r = self.client.post(reverse("architecture:link_create"), self.data(b_device=self.lon_sw.pk))
        self.assertEqual(r.status_code, 302)

    def test_bulk_delete_and_confirm(self):
        l3 = link(self.leaf, self.leaf2, "Eth1/3", "Eth1/3", confirmed=False)
        self.login(self.mum_op)
        self.client.post(reverse("architecture:links_bulk"), {"action": "confirm", "ids": [l3.pk, self.l_cross.pk]})
        l3.refresh_from_db()
        self.assertTrue(l3.confirmed)
        self.client.post(reverse("architecture:links_bulk"), {"action": "delete", "ids": [l3.pk, self.l_cross.pk, "x"]})
        self.assertFalse(Link.objects.filter(pk=l3.pk).exists())
        self.assertTrue(Link.objects.filter(pk=self.l_cross.pk).exists())
        self.assertEqual(AuditEvent.objects.filter(object_type="architecture.link", action="delete").count(), 1)

    def test_open_redirect_is_refused(self):
        self.login(self.operator)
        r = self.client.post(reverse("architecture:link_delete", args=[self.l1.pk]), {"next": "https://evil.example/"})
        self.assertEqual(r["Location"], reverse("architecture:links"))

    def test_list_filters_and_scoping(self):
        self.login(self.mum_viewer)
        r = self.client.get(reverse("architecture:links"))
        self.assertEqual(r.context["total"], 1)
        self.assertNotContains(r, "lon-core-01")
        self.login(self.admin)
        self.assertEqual(self.client.get(reverse("architecture:links"), {"q": "lon-core"}).context["total"], 1)
        self.assertEqual(self.client.get(reverse("architecture:links"), {"medium": "dac"}).context["total"], 0)


class PlacementViewTests(Base):
    def test_set_and_reset(self):
        self.login(self.operator)
        url = reverse("architecture:placement_edit", args=[self.srv.pk])
        r = self.client.post(url, {"tier": "server", "group": "Edge Cache", "colour": "teal", "sort_order": 0})
        self.assertEqual(r.status_code, 302)
        p = Placement.objects.get(device=self.srv)
        self.assertEqual((p.group, p.colour), ("Edge Cache", "teal"))
        self.assertTrue(AuditEvent.objects.filter(object_type="architecture.placement", action="create").exists())
        self.client.post(url, {"tier": "", "group": "", "colour": "", "sort_order": 0})
        self.assertFalse(Placement.objects.filter(device=self.srv).exists())

    def test_bad_values(self):
        self.login(self.operator)
        url = reverse("architecture:placement_edit", args=[self.srv.pk])
        self.assertEqual(self.client.post(url, {"tier": "nope", "sort_order": 0}).status_code, 200)
        self.assertEqual(self.client.post(url, {"colour": "hotpink", "sort_order": 0}).status_code, 200)
        self.assertEqual(self.client.post(url, {"group": "a\x00b", "sort_order": 0}).status_code, 200)
        self.assertFalse(Placement.objects.exists())

    def test_permissions_and_idor(self):
        for user, code in ((self.viewer, 403), (self.mum_viewer, 403)):
            self.login(user)
            self.assertEqual(self.client.get(reverse("architecture:placement_edit", args=[self.srv.pk])).status_code, code)
        self.login(self.mum_op)
        self.assertEqual(self.client.get(reverse("architecture:placement_edit", args=[self.lon_sw.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("architecture:placement_reset", args=[self.lon_sw.pk])).status_code, 404)

    def test_override_moves_the_device_in_the_page(self):
        Placement.objects.create(device=self.leaf, tier="spine")
        self.login(self.admin)
        r = self.client.get(reverse("architecture:overview"))
        self.assertEqual(r.context["kpis"]["tiers"]["spine"], 2)

    def test_explain(self):
        self.login(self.operator)
        r = self.client.get(reverse("architecture:placement_edit", args=[self.leaf.pk]))
        self.assertEqual(r.context["explain"].tier, "leaf")


class EscapingTests(Base):
    def test_hostile_names_are_escaped_in_the_svg_and_the_json(self):
        evil = '"><script>alert(1)</script>'
        Device.objects.filter(pk=self.leaf.pk).update(name=evil, model="<img src=x onerror=alert(2)>", role='"><svg onload=1>')
        link(self.spine, self.leaf, "<b>p</b>", "q")
        self.login(self.admin)
        html = self.client.get(reverse("architecture:overview")).content.decode()
        self.assertNotIn("<script>alert(1)", html)
        self.assertNotIn("<img src=x", html)
        self.assertNotIn("<b>p</b>", html)
        self.assertIn("\\u003Cscript\\u003Ealert(1)", html)           # json_script escaping
        self.login(self.admin)
        html = self.client.get(reverse("architecture:overview"), {"view": "list"}).content.decode()
        self.assertNotIn("<script>alert(1)", html)
