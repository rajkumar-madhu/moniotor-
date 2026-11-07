import csv
import io
from unittest import mock

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse

from cmdb.models import ConfigurationItem
from core import jobs, prometheus
from core.models import AuditEvent, Event, Membership, Site
from network.models import Device

from . import services
from .forms import CoolingUnitForm, PowerFeedForm, RackForm, RackItemForm, RoomForm
from .models import CoolingUnit, PowerFeed, Rack, RackItem, Room, faces_conflict
from .views import csv_safe, elevation

PROM = override_settings(PROMETHEUS_URL="http://prometheus.test:9090")


def series(*values):
    return [{"metric": {}, "value": v} for v in values]


class Fixture(TestCase):
    """Two sites; users: admin, global viewer, global operator, Mumbai-only operator, Mumbai-only viewer."""

    @classmethod
    def setUpTestData(cls):
        cls.mum = Site.objects.create(name="Mumbai", code="mum")
        cls.lon = Site.objects.create(name="London", code="lon")
        cls.admin = User.objects.create_superuser("root", "r@example.com", "x")
        cls.viewer = User.objects.create_user("viewer", password="x")
        cls.operator = User.objects.create_user("op", password="x", is_staff=True)
        cls.mum_op = User.objects.create_user("mumop", password="x")
        Membership.objects.create(user=cls.mum_op, site=cls.mum, role="operator")
        cls.mum_viewer = User.objects.create_user("mumview", password="x")
        Membership.objects.create(user=cls.mum_viewer, site=cls.mum, role="viewer")

        cls.room_m = Room.objects.create(site="Mumbai", name="Hall M", floor="G")
        cls.room_l = Room.objects.create(site="London", name="Hall L")
        cls.rack_m = Rack.objects.create(room=cls.room_m, name="M01", u_height=42, max_power_kw=4.0, max_cooling_kw=5.0)
        cls.rack_l = Rack.objects.create(room=cls.room_l, name="L01", u_height=42, max_power_kw=4.0)
        cls.item_m = RackItem.objects.create(rack=cls.rack_m, name="srv-m", u_position=10, u_height=2, face="front",
                                             power_draw_w=500)
        cls.item_l = RackItem.objects.create(rack=cls.rack_l, name="srv-l", u_position=10, u_height=1, face="both")
        cls.feed_m = PowerFeed.objects.create(rack=cls.rack_m, name="PDU-A", capacity_kw=5.0, measured_kw=1.2)
        cls.feed_l = PowerFeed.objects.create(rack=cls.rack_l, name="PDU-A", capacity_kw=5.0)
        cls.unit_m = CoolingUnit.objects.create(room=cls.room_m, name="CRAH-1", capacity_kw=10, supply_temp_c=18,
                                                return_temp_c=30)
        cls.unit_l = CoolingUnit.objects.create(room=cls.room_l, name="CRAH-L", capacity_kw=10)

    def login(self, user):
        self.client.force_login(user)


# ------------------------------------------------------------------ models
class ModelTests(Fixture):
    def item(self, **kw):
        defaults = {"rack": self.rack_m, "name": "x", "u_position": 1, "u_height": 1, "face": "front"}
        defaults.update(kw)
        return RackItem(**defaults)

    def test_site_is_derived_from_room(self):
        self.assertEqual(self.rack_m.site, "Mumbai")
        self.assertEqual(self.unit_l.site, "London")

    def test_tenant_key_default(self):
        self.assertEqual(self.rack_m.tenant_key, "default")
        self.assertEqual(self.item_m.tenant_key, "default")

    def test_item_must_fit(self):
        with self.assertRaisesRegex(ValidationError, "Does not fit"):
            self.item(u_position=41, u_height=3).full_clean()
        self.item(u_position=41, u_height=2).full_clean()   # U41-U42 fits exactly

    def test_overlap_same_face_rejected(self):
        with self.assertRaisesRegex(ValidationError, "Overlaps"):
            self.item(u_position=11, u_height=1, face="front").full_clean()
        with self.assertRaisesRegex(ValidationError, "Overlaps"):
            self.item(u_position=8, u_height=3, face="front").full_clean()   # U8-U10 hits U10

    def test_overlap_other_face_allowed_but_full_depth_conflicts(self):
        self.item(u_position=10, u_height=2, face="rear").full_clean()
        with self.assertRaisesRegex(ValidationError, "Overlaps"):
            self.item(u_position=11, u_height=1, face="both").full_clean()

    def test_adjacent_and_self_do_not_overlap(self):
        self.item(u_position=12, u_height=1).full_clean()
        self.item(u_position=9, u_height=1).full_clean()
        self.item_m.full_clean()   # editing itself

    def test_faces_conflict(self):
        self.assertTrue(faces_conflict("front", "front"))
        self.assertTrue(faces_conflict("rear", "both"))
        self.assertFalse(faces_conflict("front", "rear"))

    def test_u_label(self):
        self.assertEqual(self.item_m.u_label, "U10–U11")
        self.assertEqual(self.item_l.u_label, "U10")

    def test_feed_load_prefers_live_value(self):
        f = PowerFeed(capacity_kw=4, measured_kw=1.0)
        self.assertEqual((f.load_kw, f.source), (1.0, "manual"))
        f.prometheus_query, f.last_value = "x", 2.0
        self.assertEqual((f.load_kw, f.source), (2.0, "live"))
        self.assertEqual(f.headroom_kw, 2.0)
        self.assertEqual(f.load_pct, 50.0)
        f.last_value = None                     # failed read: falls back to the manual value
        self.assertEqual(f.source, "manual")
        f.measured_kw = None
        self.assertIsNone(f.load_kw)
        self.assertEqual(f.source, "")

    def test_cooling_delta_t(self):
        self.assertEqual(self.unit_m.delta_t, 12)
        self.assertIsNone(self.unit_l.delta_t)
        u = CoolingUnit(supply_temp_c=18, return_temp_c=30, supply_query="q", last_supply_c=20)
        self.assertEqual(u.supply_c, 20)
        self.assertTrue(u.supply_live)
        self.assertEqual(u.delta_t, 10)

    def test_unique_constraints_include_tenant(self):
        with transaction.atomic(), self.assertRaises(IntegrityError):
            Room.objects.create(site="Mumbai", name="Hall M")
        Room.objects.create(site="Mumbai", name="Hall M", tenant_key="other")
        with transaction.atomic(), self.assertRaises(IntegrityError):
            Rack.objects.create(room=self.room_m, name="M01")
        with transaction.atomic(), self.assertRaises(IntegrityError):
            PowerFeed.objects.create(rack=self.rack_m, name="PDU-A", capacity_kw=1)

    def test_room_protects_racks(self):
        from django.db.models import ProtectedError
        with self.assertRaises(ProtectedError):
            self.room_m.delete()


# ------------------------------------------------------------------ capacity maths
class CapacityTests(Fixture):
    def test_rack_summary_measured(self):
        r = services.summarize_rack(Rack.objects.get(pk=self.rack_m.pk))
        self.assertEqual(r.u_used, 2)
        self.assertEqual(r.u_free, 40)
        self.assertAlmostEqual(r.load_kw, 1.2)
        self.assertEqual(r.load_source, "manual")
        self.assertAlmostEqual(r.power_pct, 30.0)
        self.assertAlmostEqual(r.estimated_kw, 0.5)
        self.assertFalse(r.is_hot)

    def test_rack_summary_estimated_and_none(self):
        self.feed_m.measured_kw = None
        self.feed_m.save()
        r = services.summarize_rack(Rack.objects.get(pk=self.rack_m.pk))
        self.assertEqual(r.load_source, "estimated")
        self.assertAlmostEqual(r.load_kw, 0.5)
        r = services.summarize_rack(Rack.objects.get(pk=self.rack_l.pk))
        self.assertIsNone(r.load_kw)
        self.assertIsNone(r.power_pct)

    def test_space_counts_each_unit_once(self):
        RackItem.objects.create(rack=self.rack_m, name="rear", u_position=10, u_height=2, face="rear")
        r = services.summarize_rack(Rack.objects.get(pk=self.rack_m.pk))
        self.assertEqual(r.u_used, 2)

    def test_hot_rack(self):
        self.feed_m.measured_kw = 3.4
        self.feed_m.save()
        r = services.summarize_rack(Rack.objects.get(pk=self.rack_m.pk))
        self.assertTrue(r.is_hot)

    def test_room_summary_and_totals(self):
        rooms = services.room_summaries(Room.objects.all())
        m = next(r for r in rooms if r.pk == self.room_m.pk)
        self.assertAlmostEqual(m.cooling_capacity_kw, 10)
        self.assertAlmostEqual(m.headroom_kw, 8.8)
        CoolingUnit.objects.filter(pk=self.unit_m.pk).update(status="standby")
        m = services.room_summaries(Room.objects.filter(pk=self.room_m.pk))[0]
        self.assertIsNone(m.headroom_kw)        # no active units: unknown, never negative
        self.assertEqual(m.standby_kw, 10)
        t = services.totals(services.room_summaries(Room.objects.all()))
        self.assertEqual(t["rooms"], 2)
        self.assertEqual(t["racks"], 2)
        self.assertEqual(t["u_total"], 84)
        self.assertEqual(t["u_used"], 3)

    def test_decommissioned_rack_excluded_from_capacity(self):
        Rack.objects.filter(pk=self.rack_l.pk).update(status="decommissioned")
        t = services.totals(services.room_summaries(Room.objects.all()))
        self.assertEqual(t["u_total"], 42)
        self.assertEqual(t["active_racks"], 1)

    def test_elevation_slots_cover_every_unit(self):
        rack = Rack.objects.get(pk=self.rack_m.pk)
        RackItem.objects.create(rack=rack, name="top", u_position=42, u_height=1, face="both")
        items = list(rack.items.all())
        front = elevation(rack, items, "front")
        self.assertEqual(sum(s["span"] for s in front), 42)
        blocks = [s for s in front if s["item"]]
        self.assertEqual([b["item"].name for b in blocks], ["top", "srv-m"])
        self.assertEqual((blocks[1]["top"], blocks[1]["bottom"], blocks[1]["span"], blocks[1]["row"]), (11, 10, 2, 32))
        rear = elevation(rack, items, "rear")
        self.assertEqual([s["item"].name for s in rear if s["item"]], ["top"])


# ------------------------------------------------------------------ readings and alerts
class ReadingTests(Fixture):
    def setUp(self):
        PowerFeed.objects.filter(pk=self.feed_m.pk).update(prometheus_query="pdu_kw")
        CoolingUnit.objects.filter(pk=self.unit_m.pk).update(supply_query="sup", return_query="ret")

    def test_not_configured_reads_nothing(self):
        with mock.patch("core.prometheus.query") as q:
            self.assertEqual(services.read_all(), (0, 0))
            q.assert_not_called()
        self.assertIn("not configured", services.run_readings())

    @PROM
    def test_reads_and_stores_values(self):
        def fake(promql):
            return {"pdu_kw": series(1.5, 0.5), "sup": series(17.5), "ret": series(29.0)}[promql]
        with mock.patch("core.prometheus.query", side_effect=fake):
            self.assertEqual(services.read_all(), (2, 0))
        f = PowerFeed.objects.get(pk=self.feed_m.pk)
        self.assertEqual(f.last_value, 2.0)          # several series are summed
        self.assertEqual(f.source, "live")
        self.assertIsNotNone(f.last_read_at)
        u = CoolingUnit.objects.get(pk=self.unit_m.pk)
        self.assertEqual((u.supply_c, u.return_c, u.delta_t), (17.5, 29.0, 11.5))

    @PROM
    def test_errors_clear_stale_value(self):
        PowerFeed.objects.filter(pk=self.feed_m.pk).update(last_value=3.0)
        with mock.patch("core.prometheus.query", side_effect=prometheus.PrometheusError("Prometheus unreachable: refused")):
            self.assertEqual(services.read_all(), (0, 2))
        f = PowerFeed.objects.get(pk=self.feed_m.pk)
        self.assertIsNone(f.last_value)
        self.assertIn("unreachable", f.last_error)
        self.assertEqual(f.source, "manual")
        self.assertIn("Supply", CoolingUnit.objects.get(pk=self.unit_m.pk).last_error)

    @PROM
    def test_empty_nan_and_out_of_range(self):
        for result, msg in ((series(), "no data"), (series(None), "no data"), (series(-1.0), "outside"),
                            (series(float("inf")), "no data")):
            with mock.patch("core.prometheus.query", return_value=result):
                self.assertFalse(services.read_feed(PowerFeed.objects.get(pk=self.feed_m.pk)))
            self.assertIn(msg, PowerFeed.objects.get(pk=self.feed_m.pk).last_error)

    def test_power_alert_emits_on_state_change_only(self):
        PowerFeed.objects.filter(pk=self.feed_m.pk).update(prometheus_query="", measured_kw=3.8)  # 95 % of 4 kW
        self.assertEqual(services.evaluate_alerts(), 1)
        ev = Event.objects.get(fingerprint=f"dcim:rack:{self.rack_m.pk}:power")
        self.assertEqual((ev.severity, ev.source, ev.site), ("warning", "dcim", "Mumbai"))
        self.assertEqual(Rack.objects.get(pk=self.rack_m.pk).power_state, "high")
        self.assertEqual(services.evaluate_alerts(), 0)       # no spam
        PowerFeed.objects.filter(pk=self.feed_m.pk).update(measured_kw=1.0)
        self.assertEqual(services.evaluate_alerts(), 1)
        self.assertEqual(Event.objects.filter(fingerprint=f"dcim:rack:{self.rack_m.pk}:power", severity="ok").count(), 1)
        self.assertEqual(services.evaluate_alerts(), 0)

    def test_cooling_alert(self):
        PowerFeed.objects.filter(pk=self.feed_m.pk).update(prometheus_query="", measured_kw=3.0)
        CoolingUnit.objects.filter(pk=self.unit_m.pk).update(capacity_kw=2.0)
        services.evaluate_alerts()
        ev = Event.objects.get(fingerprint=f"dcim:room:{self.room_m.pk}:cooling")
        self.assertEqual(ev.severity, "warning")
        self.assertEqual(Room.objects.get(pk=self.room_m.pk).cooling_state, "negative")
        self.assertEqual(services.evaluate_alerts(), 0)
        CoolingUnit.objects.filter(pk=self.unit_m.pk).update(capacity_kw=20.0)
        self.assertEqual(services.evaluate_alerts(), 1)

    def test_job_registered_and_never_raises(self):
        self.assertEqual(jobs.JOBS["dcim.readings"]["interval"], 300)
        with mock.patch("dcim.services.evaluate_alerts", side_effect=RuntimeError("boom")), \
                self.assertLogs("linkedeye.dcim", "ERROR"):
            self.assertIn("0 event", services.run_readings())


# ------------------------------------------------------------------ forms
class FormTests(Fixture):
    def test_room_site_choices_follow_permissions(self):
        f = RoomForm(user=self.mum_op)
        self.assertEqual([c for c, _ in f.fields["site"].choices], ["Mumbai"])
        f = RoomForm({"site": "London", "name": "X"}, user=self.mum_op)
        self.assertFalse(f.is_valid())
        self.assertIn("site", f.errors)
        f = RoomForm(user=self.operator)
        self.assertEqual([c for c, _ in f.fields["site"].choices], ["All Sites", "London", "Mumbai"])

    def test_room_duplicate_name_case_insensitive(self):
        f = RoomForm({"site": "Mumbai", "name": "  hall m "}, user=self.admin)
        self.assertFalse(f.is_valid())
        self.assertIn("already exists", str(f.errors))
        self.assertTrue(RoomForm({"site": "London", "name": "Hall M"}, user=self.admin).is_valid())

    def rack_data(self, **kw):
        d = {"room": self.room_m.pk, "name": "M02", "u_height": 42, "status": "active", "max_power_kw": "",
             "max_cooling_kw": "", "notes": ""}
        d.update(kw)
        return d

    def test_rack_ranges(self):
        for bad in ({"u_height": 0}, {"u_height": 61}, {"max_power_kw": -1}, {"max_power_kw": "nan"},
                    {"max_power_kw": "inf"}, {"max_cooling_kw": 0}, {"name": "   "}, {"name": "M01"}):
            f = RackForm(self.rack_data(**bad), user=self.admin)
            self.assertFalse(f.is_valid(), bad)
        self.assertTrue(RackForm(self.rack_data(max_power_kw="7.5"), user=self.admin).is_valid())

    def test_rack_cannot_shrink_below_items(self):
        f = RackForm(self.rack_data(name="M01", u_height=10), instance=self.rack_m, user=self.admin)
        self.assertFalse(f.is_valid())
        self.assertIn("U11", str(f.errors["u_height"]))
        self.assertTrue(RackForm(self.rack_data(name="M01", u_height=11), instance=self.rack_m, user=self.admin).is_valid())

    def test_rack_room_choices_scoped(self):
        f = RackForm(self.rack_data(room=self.room_l.pk), user=self.mum_op)
        self.assertFalse(f.is_valid())
        self.assertIn("room", f.errors)

    def item_data(self, **kw):
        d = {"name": "new", "kind": "server", "u_position": 20, "u_height": 1, "face": "front", "device": "", "ci": "",
             "power_draw_w": "", "notes": ""}
        d.update(kw)
        return d

    def test_item_form_overlap_and_fit(self):
        f = RackItemForm(self.item_data(u_position=11), rack=self.rack_m, user=self.admin)
        self.assertFalse(f.is_valid())
        self.assertIn("Overlaps “srv-m”", str(f.errors))
        f = RackItemForm(self.item_data(u_position=42, u_height=2), rack=self.rack_m, user=self.admin)
        self.assertFalse(f.is_valid())
        self.assertIn("Does not fit", str(f.errors))
        for bad in ({"u_position": 0}, {"u_height": 0}, {"power_draw_w": -5}, {"face": "side"}, {"kind": "toaster"}):
            self.assertFalse(RackItemForm(self.item_data(**bad), rack=self.rack_m, user=self.admin).is_valid(), bad)
        self.assertTrue(RackItemForm(self.item_data(u_position=11, face="rear"), rack=self.rack_m, user=self.admin).is_valid())

    def test_item_device_rules(self):
        dev_m = Device.objects.create(name="sw-m", mgmt_ip="10.0.0.1", site="Mumbai")
        dev_l = Device.objects.create(name="sw-l", mgmt_ip="2001:db8::1", site="London")
        f = RackItemForm(self.item_data(device=dev_l.pk), rack=self.rack_m, user=self.mum_op)
        self.assertFalse(f.is_valid())                    # not visible to a Mumbai-only operator
        self.assertIn("device", f.errors)
        f = RackItemForm(self.item_data(device=dev_l.pk), rack=self.rack_m, user=self.admin)
        self.assertFalse(f.is_valid())                    # visible, but on another site
        self.assertIn("belongs to site", str(f.errors))
        RackItem.objects.filter(pk=self.item_l.pk).update(device=dev_m)
        f = RackItemForm(self.item_data(device=dev_m.pk), rack=self.rack_m, user=self.admin)
        self.assertFalse(f.is_valid())
        self.assertIn("already mounted", str(f.errors))
        RackItem.objects.filter(pk=self.item_l.pk).update(device=None)
        self.assertTrue(RackItemForm(self.item_data(device=dev_m.pk), rack=self.rack_m, user=self.admin).is_valid())

    def test_item_ci_rules(self):
        ci = ConfigurationItem.objects.create(name="db-01", site="All Sites")
        RackItem.objects.filter(pk=self.item_m.pk).update(ci=ci)
        f = RackItemForm(self.item_data(ci=ci.pk), rack=self.rack_l, user=self.admin)
        self.assertFalse(f.is_valid())
        self.assertIn("already mounted", str(f.errors))

    def feed_data(self, **kw):
        d = {"rack": self.rack_m.pk, "name": "PDU-B", "kind": "pdu", "capacity_kw": "5", "measured_kw": "",
             "prometheus_query": "", "notes": ""}
        d.update(kw)
        return d

    def test_feed_validation(self):
        for bad in ({"capacity_kw": 0}, {"capacity_kw": "nan"}, {"measured_kw": -1}, {"name": "pdu-a"},
                    {"prometheus_query": "sum(x)\nor y"}, {"prometheus_query": "x" * 501}, {"rack": self.rack_l.pk}):
            self.assertFalse(PowerFeedForm(self.feed_data(**bad), user=self.mum_op).is_valid(), bad)
        f = PowerFeedForm(self.feed_data(prometheus_query='  sum(pdu_watts{pdu="a"}) / 1000 '), user=self.mum_op)
        self.assertTrue(f.is_valid(), f.errors)
        self.assertEqual(f.cleaned_data["prometheus_query"], 'sum(pdu_watts{pdu="a"}) / 1000')

    def test_cooling_validation(self):
        base = {"room": self.room_m.pk, "name": "CRAH-2", "kind": "crah", "status": "active", "capacity_kw": "20",
                "supply_temp_c": "", "return_temp_c": "", "supply_query": "", "return_query": "", "notes": ""}
        for bad in ({"supply_temp_c": 30, "return_temp_c": 18}, {"supply_temp_c": 120}, {"return_temp_c": -40},
                    {"capacity_kw": -2}, {"kind": "fan"}, {"name": "crah-1"}):
            self.assertFalse(CoolingUnitForm({**base, **bad}, user=self.admin).is_valid(), bad)
        self.assertTrue(CoolingUnitForm({**base, "supply_temp_c": 18, "return_temp_c": 30}, user=self.admin).is_valid())


# ------------------------------------------------------------------ views
class ViewAccessTests(Fixture):
    def pages(self):
        return [reverse("dcim:overview"), reverse("dcim:racks"), reverse("dcim:power"), reverse("dcim:cooling"),
                reverse("dcim:rack_detail", args=[self.rack_m.pk]), reverse("dcim:racks_export"),
                reverse("dcim:items_export"), reverse("dcim:power_export")]

    def edit_pages(self):
        return [reverse("dcim:room_create"), reverse("dcim:rack_create"), reverse("dcim:feed_create"),
                reverse("dcim:cooling_create"), reverse("dcim:room_edit", args=[self.room_m.pk]),
                reverse("dcim:rack_edit", args=[self.rack_m.pk]), reverse("dcim:item_create", args=[self.rack_m.pk]),
                reverse("dcim:item_edit", args=[self.item_m.pk]), reverse("dcim:feed_edit", args=[self.feed_m.pk]),
                reverse("dcim:cooling_edit", args=[self.unit_m.pk])]

    def test_anonymous_redirected(self):
        self.assertEqual(self.client.get(reverse("dcim:overview")).status_code, 302)

    def test_sidebar_names_resolve(self):
        for name in ("dcim:overview", "dcim:racks", "dcim:power", "dcim:cooling"):
            self.assertTrue(reverse(name).startswith("/dcim/"))

    def test_viewer_reads_everything_but_cannot_edit(self):
        self.login(self.viewer)
        for url in self.pages():
            self.assertEqual(self.client.get(url).status_code, 200, url)
        for url in self.edit_pages():
            self.assertEqual(self.client.get(url).status_code, 403, url)
        r = self.client.post(reverse("dcim:room_create"), {"site": "Mumbai", "name": "New"})
        self.assertEqual(r.status_code, 403)
        self.assertFalse(Room.objects.filter(name="New").exists())
        self.assertEqual(self.client.post(reverse("dcim:readings_run")).status_code, 403)

    def test_operator_and_admin_open_every_page(self):
        for user in (self.operator, self.admin):
            self.login(user)
            for url in self.pages() + self.edit_pages():
                self.assertEqual(self.client.get(url).status_code, 200, (user, url))

    def test_foreign_site_user_gets_404(self):
        self.login(self.mum_op)
        for url in (reverse("dcim:rack_detail", args=[self.rack_l.pk]), reverse("dcim:rack_edit", args=[self.rack_l.pk]),
                    reverse("dcim:room_edit", args=[self.room_l.pk]), reverse("dcim:item_edit", args=[self.item_l.pk]),
                    reverse("dcim:item_create", args=[self.rack_l.pk]), reverse("dcim:feed_edit", args=[self.feed_l.pk]),
                    reverse("dcim:cooling_edit", args=[self.unit_l.pk])):
            self.assertEqual(self.client.get(url).status_code, 404, url)
        self.assertEqual(self.client.get(reverse("dcim:rack_detail", args=[self.rack_m.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("dcim:rack_detail", args=[99999])).status_code, 404)

    def test_lists_are_site_scoped(self):
        self.login(self.mum_op)
        for url in (reverse("dcim:racks"), reverse("dcim:power"), reverse("dcim:cooling"), reverse("dcim:overview")):
            body = self.client.get(url).content.decode()
            self.assertIn("Hall M", body, url)
            self.assertNotIn("Hall L", body, url)

    def test_site_viewer_cannot_edit_own_site(self):
        self.login(self.mum_viewer)
        self.assertEqual(self.client.get(reverse("dcim:rack_edit", args=[self.rack_m.pk])).status_code, 403)

    def test_operator_cannot_delete(self):
        self.login(self.operator)
        for url in (reverse("dcim:rack_delete", args=[self.rack_m.pk]), reverse("dcim:item_delete", args=[self.item_m.pk]),
                    reverse("dcim:feed_delete", args=[self.feed_m.pk]), reverse("dcim:cooling_delete", args=[self.unit_m.pk]),
                    reverse("dcim:room_delete", args=[self.room_m.pk])):
            self.assertEqual(self.client.post(url).status_code, 403, url)
        self.assertTrue(Rack.objects.filter(pk=self.rack_m.pk).exists())

    def test_delete_requires_post(self):
        self.login(self.admin)
        self.assertEqual(self.client.get(reverse("dcim:rack_delete", args=[self.rack_m.pk])).status_code, 405)

    def test_get_never_mutates(self):
        self.login(self.admin)
        audits, events = AuditEvent.objects.count(), Event.objects.count()
        PowerFeed.objects.filter(pk=self.feed_m.pk).update(measured_kw=3.9)    # would raise a power alert
        for url in self.pages() + self.edit_pages():
            self.client.get(url)
        self.assertEqual(AuditEvent.objects.count(), audits)
        self.assertEqual(Event.objects.count(), events)
        self.assertEqual(Rack.objects.get(pk=self.rack_m.pk).power_state, "")


class ViewMutationTests(Fixture):
    def test_room_create_edit_delete(self):
        self.login(self.mum_op)
        r = self.client.post(reverse("dcim:room_create"), {"site": "Mumbai", "name": "Cage 7", "floor": "B1"})
        self.assertEqual(r.status_code, 302)
        room = Room.objects.get(name="Cage 7")
        self.assertEqual((room.site, room.tenant_key), ("Mumbai", "default"))
        self.assertTrue(AuditEvent.objects.filter(action="create", object_id=str(room.pk), object_type="dcim.room").exists())
        r = self.client.post(reverse("dcim:room_create"), {"site": "London", "name": "Nope"})
        self.assertEqual(r.status_code, 400)
        r = self.client.post(reverse("dcim:room_create"), {"site": "Mumbai", "name": ""})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.client.post(reverse("dcim:room_delete", args=[room.pk])).status_code, 403)
        self.login(self.admin)
        r = self.client.post(reverse("dcim:room_delete", args=[room.pk]))
        self.assertEqual(r.status_code, 302)
        self.assertFalse(Room.objects.filter(pk=room.pk).exists())
        self.assertTrue(AuditEvent.objects.filter(action="delete", object_type="dcim.room").exists())

    def test_room_with_racks_cannot_be_deleted(self):
        self.login(self.admin)
        self.client.post(reverse("dcim:room_delete", args=[self.room_m.pk]))
        self.assertTrue(Room.objects.filter(pk=self.room_m.pk).exists())

    def test_room_move_updates_racks_and_units(self):
        self.login(self.admin)
        r = self.client.post(reverse("dcim:room_edit", args=[self.room_m.pk]),
                             {"site": "London", "name": "Hall M", "floor": "G", "description": ""})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Rack.objects.get(pk=self.rack_m.pk).site, "London")
        self.assertEqual(CoolingUnit.objects.get(pk=self.unit_m.pk).site, "London")
        audit = AuditEvent.objects.filter(action="update", object_type="dcim.room").latest("pk")
        self.assertEqual(audit.details["changed"]["site"], ["Mumbai", "London"])

    def test_rack_create_and_move_needs_target_site(self):
        self.login(self.mum_op)
        r = self.client.post(reverse("dcim:rack_create"), {"room": self.room_m.pk, "name": "M02", "u_height": 45,
                                                           "status": "active", "max_power_kw": "6"})
        self.assertEqual(r.status_code, 302)
        rack = Rack.objects.get(name="M02")
        self.assertEqual((rack.site, rack.u_height), ("Mumbai", 45))
        r = self.client.post(reverse("dcim:rack_edit", args=[rack.pk]), {"room": self.room_l.pk, "name": "M02",
                                                                         "u_height": 45, "status": "active"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(Rack.objects.get(pk=rack.pk).room_id, self.room_m.pk)

    def test_item_create_at_unit_and_overlap(self):
        self.login(self.mum_op)
        url = reverse("dcim:item_create", args=[self.rack_m.pk])
        body = self.client.get(url + "?u=20&face=rear").content.decode()
        self.assertIn('value="20"', body)
        data = {"name": "web-1", "kind": "server", "u_position": 20, "u_height": 2, "face": "rear", "power_draw_w": 300}
        self.assertEqual(self.client.post(url, data).status_code, 302)
        item = RackItem.objects.get(name="web-1")
        self.assertEqual((item.tenant_key, item.rack_id, item.u_top), ("default", self.rack_m.pk, 21))
        self.assertTrue(AuditEvent.objects.filter(action="create", object_type="dcim.rackitem").exists())
        r = self.client.post(url, {**data, "name": "web-2", "u_position": 21})
        self.assertEqual(r.status_code, 400)
        self.assertContains(r, "Overlaps", status_code=400)
        self.assertFalse(RackItem.objects.filter(name="web-2").exists())

    def test_item_edit_and_delete(self):
        self.login(self.operator)
        url = reverse("dcim:item_edit", args=[self.item_m.pk])
        r = self.client.post(url, {"name": "srv-m", "kind": "storage", "u_position": 30, "u_height": 4, "face": "both"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(RackItem.objects.get(pk=self.item_m.pk).u_position, 30)
        self.login(self.admin)
        self.assertEqual(self.client.post(reverse("dcim:item_delete", args=[self.item_m.pk])).status_code, 302)
        self.assertFalse(RackItem.objects.filter(pk=self.item_m.pk).exists())

    def test_feed_create_safe_next_and_alert(self):
        self.login(self.mum_op)
        r = self.client.post(reverse("dcim:feed_create"), {"rack": self.rack_m.pk, "name": "PDU-B", "kind": "pdu",
                                                           "capacity_kw": "5", "measured_kw": "2.6",
                                                           "next": "https://evil.example/"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r["Location"], reverse("dcim:power"))
        self.assertTrue(PowerFeed.objects.filter(name="PDU-B").exists())
        # 1.2 + 2.6 = 3.8 kW of 4 kW: the save re-evaluates the rack and raises one warning
        self.assertTrue(Event.objects.filter(fingerprint=f"dcim:rack:{self.rack_m.pk}:power", severity="warning").exists())
        r = self.client.get(reverse("dcim:feed_create") + "?next=javascript:alert(1)")
        self.assertNotContains(r, "javascript:")

    @PROM
    def test_feed_save_reads_query_once(self):
        self.login(self.admin)
        with mock.patch("core.prometheus.query", return_value=series(0.75)) as q:
            r = self.client.post(reverse("dcim:feed_edit", args=[self.feed_m.pk]),
                                 {"rack": self.rack_m.pk, "name": "PDU-A", "kind": "pdu", "capacity_kw": "5",
                                  "measured_kw": "1.2", "prometheus_query": "pdu_kw"}, follow=True)
        q.assert_called_once_with("pdu_kw")
        self.assertContains(r, "Live reading: 0.750 kW")
        self.assertEqual(PowerFeed.objects.get(pk=self.feed_m.pk).last_value, 0.75)

    def test_cooling_crud(self):
        self.login(self.mum_op)
        r = self.client.post(reverse("dcim:cooling_create"), {"room": self.room_m.pk, "name": "In-row 2",
                                                              "kind": "in_row", "status": "active", "capacity_kw": "8",
                                                              "supply_temp_c": "19", "return_temp_c": "31"})
        self.assertEqual(r.status_code, 302)
        unit = CoolingUnit.objects.get(name="In-row 2")
        self.assertEqual(unit.site, "Mumbai")
        r = self.client.post(reverse("dcim:cooling_edit", args=[unit.pk]), {
            "room": self.room_m.pk, "name": "In-row 2", "kind": "in_row", "status": "offline", "capacity_kw": "8"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(CoolingUnit.objects.get(pk=unit.pk).status, "offline")
        self.login(self.admin)
        self.assertEqual(self.client.post(reverse("dcim:cooling_delete", args=[unit.pk])).status_code, 302)
        self.assertTrue(AuditEvent.objects.filter(action="delete", object_type="dcim.coolingunit").exists())

    def test_rack_delete_cascades(self):
        self.login(self.admin)
        self.assertEqual(self.client.post(reverse("dcim:rack_delete", args=[self.rack_m.pk])).status_code, 302)
        self.assertFalse(RackItem.objects.filter(pk=self.item_m.pk).exists())
        self.assertFalse(PowerFeed.objects.filter(pk=self.feed_m.pk).exists())

    def test_readings_run_not_configured(self):
        self.login(self.operator)
        r = self.client.post(reverse("dcim:readings_run"), follow=True)
        self.assertContains(r, "Prometheus is not connected")

    @PROM
    def test_readings_run_scoped_to_operable_sites(self):
        PowerFeed.objects.update(prometheus_query="pdu_kw")
        self.login(self.mum_op)
        with mock.patch("core.prometheus.query", return_value=series(1.0)):
            self.client.post(reverse("dcim:readings_run"))
        self.assertEqual(PowerFeed.objects.get(pk=self.feed_m.pk).last_value, 1.0)
        self.assertIsNone(PowerFeed.objects.get(pk=self.feed_l.pk).last_read_at)
        self.assertTrue(AuditEvent.objects.filter(action="run", object_type="dcim.readings").exists())

    @PROM
    def test_power_page_shows_prometheus_error(self):
        PowerFeed.objects.filter(pk=self.feed_m.pk).update(prometheus_query="pdu_kw", last_error="Prometheus unreachable: refused")
        self.login(self.admin)
        self.assertContains(self.client.get(reverse("dcim:power")), "Prometheus unreachable: refused")


class PageContentTests(Fixture):
    def test_rack_detail_elevation(self):
        self.login(self.mum_op)
        r = self.client.get(reverse("dcim:rack_detail", args=[self.rack_m.pk]))
        self.assertContains(r, "U10 to U11, Server")
        self.assertContains(r, reverse("dcim:item_create", args=[self.rack_m.pk]) + "?u=42&amp;face=front")
        self.assertContains(r, "U42 free – add an item here")
        self.login(self.mum_viewer)
        r = self.client.get(reverse("dcim:rack_detail", args=[self.rack_m.pk]))
        self.assertNotContains(r, "add an item here")
        self.assertContains(r, "U42 free")

    def test_user_text_is_escaped(self):
        RackItem.objects.filter(pk=self.item_m.pk).update(name="<script>x</script>")
        self.login(self.admin)
        r = self.client.get(reverse("dcim:rack_detail", args=[self.rack_m.pk]))
        self.assertNotContains(r, "<script>x</script>")
        self.assertContains(r, "&lt;script&gt;x&lt;/script&gt;")

    def test_empty_states(self):
        Rack.objects.all().delete()
        Room.objects.all().delete()
        self.login(self.viewer)
        self.assertContains(self.client.get(reverse("dcim:overview")), "No rooms or racks")
        self.assertContains(self.client.get(reverse("dcim:racks")), "No rooms yet")
        self.assertContains(self.client.get(reverse("dcim:power")), "No rooms or racks yet")
        self.assertContains(self.client.get(reverse("dcim:cooling")), "No rooms")
        self.assertContains(self.client.get(reverse("dcim:power")), "LINKEDEYE_PROMETHEUS_URL")

    def test_filters(self):
        self.login(self.admin)
        body = self.client.get(reverse("dcim:racks") + "?q=L01").content.decode()
        self.assertIn("Hall L", body)
        self.assertNotIn("Hall M", body)
        body = self.client.get(reverse("dcim:racks") + "?site=Mumbai").content.decode()
        self.assertNotIn("Hall L", body)
        self.assertEqual(self.client.get(reverse("dcim:racks") + "?per=abc&page=x&site=<b>").status_code, 200)


class CsvTests(Fixture):
    def rows(self, url, user):
        self.login(user)
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "text/csv; charset=utf-8")
        return list(csv.reader(io.StringIO(r.content.decode())))

    def test_csv_safe(self):
        self.assertEqual(csv_safe("=1+1"), "'=1+1")
        self.assertEqual(csv_safe("@SUM(A1)"), "'@SUM(A1)")
        self.assertEqual(csv_safe("-x"), "'-x")
        self.assertEqual(csv_safe(-1.5), -1.5)
        self.assertEqual(csv_safe(None), "")

    def test_racks_csv_formula_safe_and_scoped(self):
        Rack.objects.filter(pk=self.rack_m.pk).update(notes='=HYPERLINK("http://x")')
        rows = self.rows(reverse("dcim:racks_export"), self.mum_op)
        self.assertEqual(rows[0][:4], ["site", "room", "floor", "rack"])
        self.assertEqual([r[3] for r in rows[1:]], ["M01"])
        self.assertTrue(rows[1][-1].startswith("'="))
        self.assertEqual(rows[1][10], "1.2")          # load_kw
        rows = self.rows(reverse("dcim:racks_export"), self.admin)
        self.assertEqual(len(rows), 3)

    def test_items_csv(self):
        RackItem.objects.filter(pk=self.item_m.pk).update(name="+cmd")
        rows = self.rows(reverse("dcim:items_export"), self.mum_op)
        self.assertEqual([r[3] for r in rows[1:]], ["'+cmd"])
        rows = self.rows(reverse("dcim:items_export") + f"?rack={self.rack_l.pk}", self.admin)
        self.assertEqual([r[3] for r in rows[1:]], ["srv-l"])

    def test_power_csv(self):
        rows = self.rows(reverse("dcim:power_export"), self.viewer)
        self.assertEqual(len(rows), 3)
        self.assertIn("headroom_kw", rows[0])


class SeedTests(TestCase):
    def test_seed_idempotent_and_removable(self):
        out = io.StringIO()
        call_command("seed_dcim", stdout=out)
        counts = (Room.objects.count(), Rack.objects.count(), RackItem.objects.count(), PowerFeed.objects.count(),
                  CoolingUnit.objects.count())
        self.assertTrue(all(counts))
        call_command("seed_dcim", stdout=out)
        self.assertEqual(counts, (Room.objects.count(), Rack.objects.count(), RackItem.objects.count(),
                                  PowerFeed.objects.count(), CoolingUnit.objects.count()))
        self.assertTrue(all(r.is_example for r in Room.objects.all()))
        self.assertTrue(all(r.is_example for r in Rack.objects.all()))
        for rack in Rack.objects.all():     # every example item fits and none overlap
            for it in rack.items.all():
                it.full_clean()
        call_command("seed_dcim", "--remove", stdout=out)
        self.assertEqual(Room.objects.count(), 0)
        self.assertEqual(RackItem.objects.count(), 0)
