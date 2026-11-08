"""The per-port page (network:interface), its live JSON and CSV, the port facts stored by the metrics job, and the links
into the page. Prometheus is always mocked."""
import csv
import io
import re
import time
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from core import prometheus
from core.models import Event, Membership, Site

from . import jobs, metrics, portmetrics, portviews
from .live import port_data
from .models import Device, Interface
from .tests import T, dev, fake_query, prom_row, users

PROM = "http://prometheus.test:9090"
INSTANT = "core.prometheus.query"
RANGE = "core.prometheus.query_range"


def grid(seconds, step, n=6):
    end = (int(time.time()) // step) * step
    return [end - (n - 1 - i) * step for i in range(n)]


def fake_range(promql, seconds=3600, step=60, octets=1.25e6):
    """snmp_exporter-shaped range answers keyed by the label_replace tags / metric names the page asks for."""
    ts = grid(seconds, step)
    if "label_replace" in promql:
        out = []
        for tag in re.findall(r'"m","(\w+)"', promql):
            v = octets if tag in ("in", "out") else 10.0 if tag.endswith(("uni", "multi", "bcast")) else 0.5
            out.append({"metric": {"m": tag, "ifIndex": "1"}, "values": [(t, v) for t in ts]})
        return out
    if promql.startswith("ifOperStatus"):
        return [{"metric": {"ifIndex": "1"}, "values": [(t, 1.0 if i not in (2, 3) else 2.0) for i, t in enumerate(ts)]}]
    if "entSensorValue" in promql:
        return [{"metric": {"__name__": "entSensorValue", "entPhysicalName": "Gi1/0/1 Transceiver Rx Power Sensor",
                            "entSensorType": "dBm", "entSensorPrecision": "2"}, "values": [(t, -231.0) for t in ts]}]
    return []


def sensors(q):
    name = "Gi1/0/1"
    rows = [("Transceiver Rx Power Sensor", "dBm", "units", "2", -231), ("Transceiver Tx Power Sensor", "dBm", "units", "2", -134),
            ("Transceiver Temperature Sensor", "celsius", "units", "2", 3850), ("Transceiver Bias Current Sensor", "amperes", "milli", "0", 62),
            ("Transceiver Voltage Sensor", "voltsDC", "milli", "0", 3290)]
    return [({"__name__": "entSensorValue", "instance": "sw1", "entPhysicalName": f"{name} {label}", "entPhysicalIndex": str(900 + i),
              "entSensorType": typ, "entSensorScale": scale, "entSensorPrecision": prec}, raw) for i, (label, typ, scale, prec, raw) in enumerate(rows)]


def fake_instant(q):
    """Full-featured device: every optional card has data for ifIndex 1."""
    rows = []
    if "ipAdEntIfIndex" in q:
        rows = [({"ipAdEntAddr": "10.1.0.1"}, 1), ({"ipAdEntAddr": "10.1.0.9"}, 1)]
    elif "dot3adAggPortAttachedAggID" in q and 'dot3adAggPortIndex="1"' in q:
        rows = [({"dot3adAggPortIndex": "1"}, 2)]
    elif "dot1dBasePortIfIndex" in q:
        rows = [({"dot1dBasePort": "1"}, 1)]
    elif "dot1qPvid" in q:
        rows = [({}, 100)]
    elif "dot1dStpPortState" in q:
        rows = [({}, 5)]
    elif "vlanTrunkPortNativeVlan" in q:
        rows = [({"vlanTrunkPortIfIndex": "1"}, 7)]
    elif "lldpRemSysName" in q:
        base = {"lldpRemIndex": "1"}
        rows = [({**base, "__name__": "lldpRemSysName", "lldpRemSysName": "dist-b.example.net"}, 1),
                ({**base, "__name__": "lldpRemPortId", "lldpRemPortId": "Ethernet1/12"}, 1),
                ({**base, "__name__": "lldpRemChassisId", "lldpRemChassisId": "0x001B54C0FF21"}, 1),
                ({**base, "__name__": "lldpRemManAddrIfSubtype", "lldpRemManAddr": "0xC0A80164"}, 1)]
    elif "entSensorThresholdValue" in q:
        rows = [({"entPhysicalIndex": "900", "entSensorThresholdSeverity": "major", "entSensorThresholdRelation": "lessThan"}, -1400)]
    elif "entSensorValue" in q:
        rows = sensors(q)
    elif "pethPse" in q:
        rows = [({"__name__": "pethPsePortAdminEnable"}, 1), ({"__name__": "pethPsePortDetectionStatus"}, 3),
                ({"__name__": "pethPsePortPowerClassifications"}, 4), ({"__name__": "cpeExtPsePortPwrConsumption"}, 6800)]
    return [{"metric": {"instance": "sw1", **m}, "value": float(v)} for m, v in rows]


def empty_instant(q):
    return []


def calls(*mocks):
    return sum(m.call_count for m in mocks)


@override_settings(LINKEDEYE_TENANT=T, PROMETHEUS_URL=PROM)
class PortCase(TestCase):
    def setUp(self):
        cache.clear()
        self.viewer, self.operator, self.admin = users()
        self.d = dev("core-sw-01", site="Mumbai (NSE)", prometheus_instance="sw1")
        now = timezone.now()
        self.i1 = Interface.objects.create(
            device=self.d, if_index=1, name="Gi1/0/1", alias="Uplink", if_type="Ethernet", admin_status="up", oper_status="up",
            speed_mbps=1000, in_mbps=680, out_mbps=520, utilization=68, errors_per_s=0.5, discards_per_s=0, flaps_24h=2,
            mtu=9216, mac="00:1a:2b:3c:4d:5e", duplex="full", last_change_at=now - timedelta(hours=3), updated_at=now)
        self.i2 = Interface.objects.create(device=self.d, if_index=2, name="Gi1/0/2", if_type="Ethernet", admin_status="up",
                                           oper_status="down", speed_mbps=1000, utilization=None, updated_at=now)
        self.i3 = Interface.objects.create(device=self.d, if_index=3, name="Gi1/0/3", if_type="Ethernet", admin_status="up",
                                           oper_status="up", speed_mbps=1000, in_mbps=900, out_mbps=100, utilization=90, updated_at=now)
        self.c = Client()
        self.c.force_login(self.viewer)
        self.url = reverse("network:interface", args=[self.d.pk, 1])

    def get(self, url=None, instant=empty_instant, rng=fake_range, **params):
        with mock.patch(INSTANT, side_effect=instant) as q, mock.patch(RANGE, side_effect=rng) as qr:
            r = self.c.get(url or self.url, params)
        self.q, self.qr = q, qr
        return r

    def all_promql(self):
        return [c.args[0] for c in self.q.call_args_list] + [c.args[0] for c in self.qr.call_args_list]


class PortFactsCollectionTests(PortCase):
    def collect(self, query):
        with mock.patch(INSTANT, side_effect=query):
            return jobs.collect_metrics()

    @staticmethod
    def facts_query(q):
        base = {"instance": "sw1"}
        if "ifMtu" in q:
            return [prom_row(9216, ifIndex="1", **base), prom_row(1500, ifIndex="2", **base), prom_row(1500, ifIndex="77", **base)]
        if "ifPhysAddress" in q:
            return [prom_row(1, ifIndex="1", ifPhysAddress="0x001A2B3C4D5E", **base),
                    prom_row(1, ifIndex="2", ifPhysAddress="00:00:00:00:00:00", **base)]
        if "dot3StatsDuplexStatus" in q:
            return [prom_row(3, dot3StatsIndex="1", **base), prom_row(1, dot3StatsIndex="2", **base)]
        if "ifLastChange" in q:
            return [prom_row(7444800 * 100 - 360000, ifIndex="1", **base)]
        return fake_query(q)

    def test_facts_are_stored_and_never_create_an_interface(self):
        d = dev("sw-facts", prometheus_instance="sw1", mgmt_ip="192.0.2.50")
        self.collect(self.facts_query)
        g1, g2 = d.interfaces.order_by("if_index")
        self.assertFalse(d.interfaces.filter(if_index=77).exists())
        self.assertEqual((g1.mtu, g1.mac, g1.duplex), (9216, "00:1a:2b:3c:4d:5e", "full"))
        self.assertAlmostEqual((timezone.now() - g1.last_change_at).total_seconds(), 3600, delta=120)
        # an all-zero MAC, an unknown duplex and a missing ifLastChange are "not reported", not values
        self.assertEqual((g2.mtu, g2.mac, g2.duplex, g2.last_change_at), (1500, "", "", None))

    def test_failed_query_keeps_stored_facts_and_absent_series_clear_them(self):
        d = dev("sw-facts2", prometheus_instance="sw1", mgmt_ip="192.0.2.51")
        self.collect(self.facts_query)

        def broken(q):
            if "ifMtu" in q or "ifLastChange" in q:
                raise prometheus.PrometheusError("timeout")
            return self.facts_query(q)
        self.collect(broken)
        g1 = d.interfaces.get(if_index=1)
        self.assertEqual(g1.mtu, 9216)                  # the query failed: nothing is known to have changed
        self.assertIsNotNone(g1.last_change_at)
        self.collect(fake_query)                        # the series are simply gone
        g1.refresh_from_db()
        self.assertEqual((g1.mtu, g1.mac, g1.duplex, g1.last_change_at), (None, "", "", None))

    def test_last_change_beyond_uptime_is_not_reported(self):
        d = dev("sw-facts3", prometheus_instance="sw1", mgmt_ip="192.0.2.52")

        def q(promql):
            return [prom_row(7444800 * 100 + 10_000_000, ifIndex="1", instance="sw1")] if "ifLastChange" in promql else fake_query(promql)
        self.collect(q)
        self.assertIsNone(d.interfaces.get(if_index=1).last_change_at)

    def test_parsers(self):
        for raw, want in (("0x001A2B3C4D5E", "00:1a:2b:3c:4d:5e"), ("00-1A-2B-3C-4D-5E", "00:1a:2b:3c:4d:5e"),
                          ("001a.2b3c.4d5e", "00:1a:2b:3c:4d:5e"), ("00:00:00:00:00:00", ""), ("0x1234", ""), ("zz:zz", ""), (None, ""),
                          ("<script>", "")):
            self.assertEqual(metrics.parse_mac(raw), want, raw)
        self.assertEqual(metrics.parse_duplex({}, 3), "full")
        self.assertEqual(metrics.parse_duplex({}, 2), "half")
        self.assertEqual(metrics.parse_duplex({}, 1), "")
        self.assertEqual(metrics.parse_duplex({"dot3StatsDuplexStatus": "fullDuplex"}, 1), "full")
        self.assertEqual(metrics.parse_duplex({"dot3StatsDuplexStatus": "fullDuplex"}, 0), "")


class SelectorTests(PortCase):
    def test_selector_is_built_from_the_instance_label_and_an_integer(self):
        self.assertEqual(portmetrics.selector(self.d, 7), 'instance="sw1",ifIndex="7"')
        hostile = Device(name="x", prometheus_instance='sw"1\\')
        self.assertEqual(portmetrics.selector(hostile, 7), 'instance="sw\\"1\\\\",ifIndex="7"')
        for bad in ("sw1\nup", "a\x00b", "x" * 300, "   "):
            self.assertIsNone(portmetrics.selector(Device(name="x", prometheus_instance=bad), 1), repr(bad))

    def test_name_regex_is_anchored_and_escaped(self):
        body = portmetrics.name_regex('Gi1/0/1" or vector(1) #\n.*')
        self.assertNotIn('"', body.replace('\\"', ""))
        self.assertNotIn("\n", body)
        self.assertTrue(body.endswith("( .*)?"))
        self.assertEqual(portmetrics.name_regex("\x00\x01"), "")

    def test_clean_strips_controls_and_caps(self):
        self.assertEqual(portmetrics.clean("a\x00b\x1bc\nd"), "a b c d")
        self.assertEqual(len(portmetrics.clean("x" * 5000)), portmetrics.TEXT_MAX)

    def test_chart_queries_only_contain_the_selector_and_fixed_text(self):
        sel = portmetrics.selector(self.d, 1)
        for q in (portmetrics.traffic_query(sel), portmetrics.packets_query(sel), portmetrics.errors_query(sel)):
            self.assertEqual(q.count(f"{{{sel}}}"), q.count("{"))


class PortPageTests(PortCase):
    def test_renders_for_a_viewer_with_full_data(self):
        r = self.get(instant=fake_instant)
        self.assertEqual(r.status_code, 200)
        for text in ("Gi1/0/1", "Uplink", "9,216 bytes", "00:1a:2b:3c:4d:5e", "Full duplex", "Trunk, native VLAN 7", "10.1.0.1", "10.1.0.9",
                     "Member of Gi1/0/2", "forwarding", "dist-b.example.net", "Ethernet1/12", "00:1b:54:c0:ff:21", "192.168.1.100",
                     "Transceiver Rx Power Sensor", "-2.31 dBm", "-1.34 dBm", "38.5 °C", "62.0 mA", "3.29 V", "&lt; -14 dBm (major)",
                     "Delivering power", "Class 3", "6.8 W", "Port 1 of 3", "Compare with other ports", "Gi1/0/3"):
            self.assertContains(r, text)
        self.assertContains(r, "pd-traffic")
        self.assertContains(r, "Optical power")

    def test_breadcrumb_prev_next_and_picker(self):
        r = self.get(reverse("network:interface", args=[self.d.pk, 2]))
        self.assertContains(r, reverse("network:interface", args=[self.d.pk, 1]) + "?range=24h")
        self.assertContains(r, reverse("network:interface", args=[self.d.pk, 3]) + "?range=24h")
        self.assertContains(r, 'name="goto"')
        self.assertContains(r, f'{reverse("network:detail", args=[self.d.pk])}?tab=interfaces')
        r = self.c.get(self.url, {"goto": "3", "range": "7d"})
        self.assertRedirects(r, reverse("network:interface", args=[self.d.pk, 3]) + "?range=7d", fetch_redirect_response=False)
        r = self.c.get(self.url, {"goto": "99"})           # unknown port: just the page
        self.assertEqual(r.status_code, 200)
        r = self.c.get(self.url, {"goto": "1}or vector(1)"})
        self.assertEqual(r.status_code, 200)

    def test_anonymous_is_sent_to_login_and_every_role_may_view(self):
        self.assertEqual(Client().get(self.url).status_code, 302)
        for u in (self.viewer, self.operator, self.admin):
            c = Client()
            c.force_login(u)
            with mock.patch(INSTANT, return_value=[]), mock.patch(RANGE, return_value=[]):
                self.assertEqual(c.get(self.url).status_code, 200, u.username)

    def test_unknown_ifindex_other_devices_port_and_vm_are_404(self):
        other = dev("other-sw", mgmt_ip="192.0.2.77", prometheus_instance="sw2")
        Interface.objects.create(device=other, if_index=500, name="Eth500")
        vm = dev("vm1", device_type="virtual_machine", mgmt_ip="192.0.2.78")
        Interface.objects.create(device=vm, if_index=1, name="eth0")
        for path in (reverse("network:interface", args=[self.d.pk, 500]),          # another device's ifIndex (IDOR)
                     reverse("network:interface", args=[self.d.pk, 4]),
                     reverse("network:interface", args=[self.d.pk, 2 ** 31]),
                     reverse("network:interface", args=[self.d.pk, 10 ** 40]),
                     reverse("network:interface", args=[10 ** 40, 1]),
                     reverse("network:interface", args=[vm.pk, 1]),
                     reverse("network:interface", args=[999999, 1]),
                     reverse("network:interface_live", args=[self.d.pk, 500]),
                     reverse("network:interface_export", args=[self.d.pk, 500])):
            r = self.get(path)
            self.assertEqual(r.status_code, 404, path)
        self.assertEqual(calls(self.q, self.qr), 0)
        self.assertEqual(self.c.get(reverse("network:interface", args=[other.pk, 500])).status_code, 200)

    def test_site_scoping(self):
        dr = Site.objects.create(tenant_key=T, name="DR Site", code="dr")
        Site.objects.create(tenant_key=T, name="Mumbai (NSE)", code="mum")
        u = User.objects.create_user("dr-viewer", password="pw")
        Membership.objects.create(user=u, site=dr, role="viewer")
        c = Client()
        c.force_login(u)
        with mock.patch(INSTANT, return_value=[]) as q, mock.patch(RANGE, return_value=[]) as qr:
            for name in ("interface", "interface_live", "interface_export"):
                self.assertEqual(c.get(reverse(f"network:{name}", args=[self.d.pk, 1])).status_code, 404, name)
        self.assertEqual(calls(q, qr), 0)
        mum = Site.objects.get(code="mum")
        v = User.objects.create_user("mum-viewer", password="pw")
        Membership.objects.create(user=v, site=mum, role="viewer")
        c2 = Client()
        c2.force_login(v)
        with mock.patch(INSTANT, return_value=[]), mock.patch(RANGE, return_value=[]):
            self.assertEqual(c2.get(self.url).status_code, 200)

    def test_range_is_allow_listed_and_nothing_else_reaches_promql(self):
        for rng in ("1h", "6h", "24h", "7d"):
            r = self.get(range=rng)
            self.assertContains(r, f'aria-current="true">{rng}<')
        r = self.get(range='24h"} or vector(1) #')
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'aria-current="true">24h<')
        for q in self.all_promql():
            self.assertNotIn("vector(1)", q)
            self.assertIn('instance="sw1"', q)
        r = self.c.get(f"/network/devices/{self.d.pk}/interfaces/1%7D%20or%20vector(1)/")
        self.assertEqual(r.status_code, 404)

    def test_hostile_names_are_escaped_and_never_reach_promql_raw(self):
        evil = '<script>alert(1)</script>"\'`${x}'
        self.i1.name = 'Gi1/0/1" or vector(1) # ' + evil
        self.i1.alias = evil + "\n\r" + "A" * 5000
        self.i1.save()
        Interface.objects.filter(pk=self.i3.pk).update(name=evil, alias=evil)
        r = self.get(instant=fake_instant)
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertNotIn("<script>alert(1)</script>", body)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", body)
        for q in self.all_promql():
            self.assertNotIn('" or vector(1)', q.replace('\\"', ""), q)
        # hostile device text from Prometheus (LLDP / sensor names) is escaped too
        def hostile(q):
            rows = fake_instant(q)
            for row in rows:
                for k, v in row["metric"].items():
                    if k.startswith("lldpRem") and k not in ("lldpRemIndex",):
                        row["metric"][k] = evil if k != "lldpRemManAddr" else v
            return rows
        r = self.get(instant=hostile)
        self.assertNotContains(r, "<script>alert(1)</script>")

    def test_prometheus_unconfigured(self):
        with override_settings(PROMETHEUS_URL=""):
            r = self.get()
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "No metrics source")
        self.assertEqual(calls(self.q, self.qr), 0)
        self.assertContains(r, "not reported")

    def test_device_without_instance(self):
        Device.objects.filter(pk=self.d.pk).update(prometheus_instance="")
        r = self.get()
        self.assertContains(r, "set the device")
        self.assertEqual(calls(self.q, self.qr), 0)

    def test_prometheus_down_stops_after_one_call(self):
        with mock.patch(INSTANT, side_effect=prometheus.PrometheusError("Prometheus unreachable: refused")) as q, \
                mock.patch(RANGE, side_effect=prometheus.PrometheusError("Prometheus unreachable: refused")) as qr:
            r = self.c.get(self.url)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(calls(q, qr), 1)
        self.assertContains(r, "Prometheus did not answer every query")
        self.assertContains(r, "Gi1/0/1")

    def test_prometheus_error_on_one_query_keeps_the_rest(self):
        def flaky(promql, **kw):
            if "ifOperStatus" in promql:
                raise prometheus.PrometheusError("HTTP 500")
            return fake_range(promql, **kw)
        r = self.get(rng=flaky, instant=fake_instant)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Prometheus query failed: HTTP 500")
        self.assertContains(r, "pd-traffic")

    def test_disabled_device_asks_nothing_and_shows_no_traffic(self):
        Device.objects.filter(pk=self.d.pk).update(enabled=False)
        r = self.get()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(calls(self.q, self.qr), 0)
        self.assertContains(r, "Monitoring is disabled")
        self.assertNotContains(r, "680 Mbps")
        self.assertEqual(r.context["kpis"][0]["value"], "–")
        self.assertEqual(r.context["kpis"][2]["value"], "–")

    def test_every_card_degrades_to_not_reported(self):
        r = self.get(rng=lambda *a, **k: [])
        self.assertEqual(r.status_code, 200)
        for text in ("No LLDP or CDP neighbour", "No transceiver sensors", "No PoE data", "No interface counters in Prometheus for this port",
                     "No ifOperStatus samples", "No packet counters", "No error or discard counters"):
            self.assertContains(r, text)
        self.assertNotContains(r, "Optical power")
        rows = {x["k"]: x for x in r.context["info"]}
        for key in ("IP addresses", "LAG membership", "Spanning tree state", "Port role / VLAN"):
            self.assertFalse(rows[key]["v"] or rows[key]["items"] or rows[key]["links"], key)
        bare = Interface.objects.create(device=self.d, if_index=9, name="Gi1/0/9")
        r = self.get(reverse("network:interface", args=[self.d.pk, 9]), rng=lambda *a, **k: [])
        rows = {x["k"]: x for x in r.context["info"]}
        for key in ("Description (ifAlias)", "MTU (ifMtu)", "MAC (ifPhysAddress)", "Duplex", "Last change", "Speed"):
            self.assertIsNone(rows[key]["v"], key)
        self.assertContains(r, "not reported")
        self.assertEqual(bare.mtu, None)

    def test_missing_one_family_only_blanks_that_card(self):
        def no_packets(promql, **kw):
            return [] if "InUcastPkts" in promql else fake_range(promql, **kw)
        r = self.get(rng=no_packets)
        self.assertContains(r, "No packet counters")
        self.assertContains(r, "pd-traffic")
        self.assertContains(r, "Utilisation")

    def test_call_budget_and_cache(self):
        r = self.get(instant=fake_instant, range="6h")
        used = calls(self.q, self.qr)
        self.assertLessEqual(used, portmetrics.MAX_PROM_CALLS)
        self.assertEqual(r.context["prom_calls"], used)
        self.assertGreaterEqual(used, 15)
        # a reload (any viewer) inside the cache window asks nothing
        r = self.get(instant=fake_instant, range="6h")
        self.assertEqual(calls(self.q, self.qr), 0)
        self.assertContains(r, "Transceiver Rx Power Sensor")
        # the 24 h range reuses its own charts for the sparklines: three calls fewer
        cache.clear()
        self.get(instant=fake_instant, range="24h")
        self.assertEqual(calls(self.q, self.qr), used - 3)

    def test_failed_answers_are_not_cached_for_long(self):
        with mock.patch(INSTANT, side_effect=prometheus.PrometheusError("HTTP 500")), mock.patch(RANGE, return_value=[]):
            self.c.get(self.url)
        self.assertEqual(cache.get(f"nwport:x:{self.d.pk}:1")["errors"], ["HTTP 500"])
        self.get(instant=fake_instant)   # cached for the short failure window: not asked again, no crash
        self.assertEqual(self.q.call_count, 0)

    def test_utilisation_chart_needs_speed(self):
        Interface.objects.filter(pk=self.i1.pk).update(speed_mbps=None)
        r = self.get()
        self.assertContains(r, "Utilisation needs the port speed")
        self.assertContains(r, "pd-traffic")

    def test_traffic_chart_switches_to_gbps_on_fast_ports(self):
        r = self.get(rng=lambda p, **k: fake_range(p, octets=3e8, **k))
        chart = next(p["chart"] for p in r.context["panels"] if p["key"] == "traffic")
        self.assertEqual(chart["unit"], "Gbps")
        self.assertAlmostEqual(chart["series"][0]["values"][-1], 2.4, places=2)
        self.assertEqual(chart["series"][0]["name"], "Inbound")
        self.assertEqual(len(chart["ts"]), len(chart["labels"]))

    def test_oper_timeline(self):
        r = self.get()
        oper = r.context["oper"]
        self.assertEqual([s["cls"] for s in oper["segments"] if s["cls"] != "nodata"], ["up", "down", "up"])
        self.assertEqual(oper["segments"][0]["cls"], "nodata")      # the samples start after the window does: a gap, not "up"
        self.assertEqual(oper["changes"], 2)
        self.assertIn("2 changes", oper["summary"])
        self.assertContains(r, 'class="pd-seg down"')

    def test_state_set_oper_status(self):
        ts = grid(3600, 60)
        rows = [{"metric": {"ifOperStatus": "up"}, "values": [(t, 1.0 if i < 3 else 0.0) for i, t in enumerate(ts)]},
                {"metric": {"ifOperStatus": "down"}, "values": [(t, 0.0 if i < 3 else 1.0) for i, t in enumerate(ts)]}]
        self.assertEqual([s for _t, s in portmetrics.oper_points(rows)], ["up"] * 3 + ["down"] * 3)

    def test_kpis_and_gauge(self):
        r = self.get()
        by = {c["key"]: c for c in r.context["kpis"]}
        self.assertEqual((by["in"]["value"], by["out"]["value"], by["util"]["value"]), ("680 Mbps", "520 Mbps", "68%"))
        self.assertEqual((by["errors"]["value"], by["discards"]["value"], by["flaps"]["value"]), ("0.50/s", "0/s", "2"))
        self.assertEqual(by["errors"]["tone"], "warn")
        self.assertEqual(r.context["gauge"]["value"], 68)
        self.assertContains(r, 'data-chart="gauge"')
        self.assertContains(r, 'data-chart="spark"')

    def test_stale_banner(self):
        Interface.objects.filter(pk=self.i1.pk).update(updated_at=timezone.now() - timedelta(minutes=30))
        r = self.get()
        self.assertTrue(r.context["state"]["stale"])
        self.assertContains(r, "30 min ago")

    def test_recent_events_and_flaps(self):
        Event.objects.create(tenant_key=T, source="network", title="Interface core-sw-01 Gi1/0/1 is down", severity="warning",
                             object_type="network.device", object_id=str(self.d.pk), fingerprint=f"network:{self.d.pk}:if1")
        Event.objects.create(tenant_key=T, source="network", title="Interface core-sw-01 Gi1/0/2 is down", severity="warning",
                             object_type="network.device", object_id=str(self.d.pk), fingerprint=f"network:{self.d.pk}:if2")
        r = self.get()
        self.assertContains(r, "Gi1/0/1 is down")
        self.assertNotContains(r, "Gi1/0/2 is down")
        self.assertEqual(len(r.context["events"]), 1)

    def test_compare_table_is_top_five_by_utilisation_without_self(self):
        for n in range(10, 18):
            Interface.objects.create(device=self.d, if_index=n, name=f"Gi1/0/{n}", utilization=float(n), oper_status="up", updated_at=timezone.now())
        r = self.get()
        names = [c["name"] for c in r.context["compare"]]
        self.assertEqual(names, ["Gi1/0/3", "Gi1/0/17", "Gi1/0/16", "Gi1/0/15", "Gi1/0/14"])
        self.assertNotIn("Gi1/0/1", names)

    def test_explorer_link_only_for_all_site_users(self):
        r = self.get()
        self.assertIn("/monitoring/metrics-explorer/?expr=", r.context["explorer_url"])
        self.assertIn("ifHCInOctets", r.context["explorer_url"])
        dr = Site.objects.create(tenant_key=T, name="Mumbai (NSE)", code="mum")
        v = User.objects.create_user("scoped", password="pw")
        Membership.objects.create(user=v, site=dr, role="viewer")
        c = Client()
        c.force_login(v)
        with mock.patch(INSTANT, return_value=[]), mock.patch(RANGE, return_value=[]):
            r = c.get(self.url)
        self.assertEqual(r.context["explorer_url"], "")


class LinksIntoThePortPageTests(PortCase):
    def test_interfaces_tab_links_each_port(self):
        with mock.patch(RANGE, return_value=[]):
            r = self.c.get(reverse("network:detail", args=[self.d.pk]), {"tab": "interfaces"})
        for i in (1, 2, 3):
            self.assertContains(r, f'href="{reverse("network:interface", args=[self.d.pk, i])}"')

    def test_front_panel_data_carries_the_port_url(self):
        ports = port_data(metrics.current_interfaces(self.d))
        self.assertEqual(ports[0]["url"], reverse("network:interface", args=[self.d.pk, 1]))
        with mock.patch(RANGE, return_value=[]):
            r = self.c.get(reverse("network:detail", args=[self.d.pk]))
        self.assertContains(r, "fp-d-detail")
        self.assertContains(r, f'"url": "{reverse("network:interface", args=[self.d.pk, 1])}"')

    def test_alerts_tab_links_interface_events_only(self):
        Event.objects.create(tenant_key=T, source="network", title="Interface x Gi1/0/1 is down", severity="warning",
                             object_type="network.device", object_id=str(self.d.pk), fingerprint=f"network:{self.d.pk}:if1")
        Event.objects.create(tenant_key=T, source="network", title="Interface x Gi9/9/9 is down", severity="warning",
                             object_type="network.device", object_id=str(self.d.pk), fingerprint=f"network:{self.d.pk}:if999")
        Event.objects.create(tenant_key=T, source="network", title="core-sw-01 is down", severity="critical",
                             object_type="network.device", object_id=str(self.d.pk), fingerprint=f"network:{self.d.pk}")
        with mock.patch(RANGE, return_value=[]):
            r = self.c.get(reverse("network:detail", args=[self.d.pk]), {"tab": "events"})
        link = reverse("network:interface", args=[self.d.pk, 1])
        self.assertContains(r, f'<a href="{link}"')
        self.assertNotContains(r, "/interfaces/999/")
        self.assertEqual(r.content.decode().count("/interfaces/1/"), 1)


class ArchitectureLinkTests(PortCase):
    def setUp(self):
        super().setUp()
        from architecture.models import Link
        self.Link = Link
        self.mum = Site.objects.create(tenant_key=T, name="Mumbai (NSE)", code="mum")
        self.dr = Site.objects.create(tenant_key=T, name="DR Site", code="dr")
        self.far = dev("dr-rtr", site="DR Site", mgmt_ip="192.0.2.90")
        self.near = dev("mum-dist", site="Mumbai (NSE)", mgmt_ip="192.0.2.91")
        self.l_far = Link.objects.create(tenant_key=T, a_device=self.d, a_port="gi1/0/1", b_device=self.far, b_port="Eth9", kind="uplink")
        self.l_near = Link.objects.create(tenant_key=T, a_device=self.d, a_port="Gi1/0/3", b_device=self.near, b_port="Eth4")

    def test_visible_far_end_is_named_and_hidden_far_end_is_redacted(self):
        v = User.objects.create_user("mum-only", password="pw")
        Membership.objects.create(user=v, site=self.mum, role="viewer")
        c = Client()
        c.force_login(v)
        with mock.patch(INSTANT, return_value=[]), mock.patch(RANGE, return_value=[]):
            r = c.get(self.url)
            self.assertContains(r, "A device outside your sites")
            self.assertNotContains(r, "dr-rtr")
            self.assertNotContains(r, "Eth9")
            r = c.get(reverse("network:interface", args=[self.d.pk, 3]))
            self.assertContains(r, "mum-dist")
            self.assertContains(r, "Eth4")
            r = c.get(reverse("network:detail", args=[self.d.pk]), {"tab": "neighbours"})
        self.assertContains(r, "Documented links")
        self.assertContains(r, "a device outside your sites")
        self.assertNotContains(r, "dr-rtr")
        self.assertContains(r, reverse("network:interface", args=[self.d.pk, 1]))

    def test_admin_sees_everything_and_edit_link_needs_operate(self):
        with mock.patch(INSTANT, return_value=[]), mock.patch(RANGE, return_value=[]):
            r = self.c.get(self.url)
            self.assertContains(r, "dr-rtr")
            self.assertNotContains(r, reverse("architecture:link_edit", args=[self.l_far.pk]))
            c = Client()
            c.force_login(self.operator)
            self.assertContains(c.get(self.url), reverse("architecture:link_edit", args=[self.l_far.pk]))

    def test_lldp_neighbour_matched_only_to_a_visible_device(self):
        v = User.objects.create_user("mum-only2", password="pw")
        Membership.objects.create(user=v, site=self.mum, role="viewer")

        def lldp(q):
            rows = fake_instant(q)
            for r in rows:
                if r["metric"].get("lldpRemSysName"):
                    r["metric"]["lldpRemSysName"] = "DR-RTR.example.net"
            return rows
        c = Client()
        c.force_login(v)
        with mock.patch(INSTANT, side_effect=lldp), mock.patch(RANGE, return_value=[]):
            r = c.get(self.url)
            self.assertContains(r, "no matching device you can see")
            admin = Client()
            admin.force_login(self.admin)
            r = admin.get(self.url)
        self.assertContains(r, reverse("network:detail", args=[self.far.pk]))

    def test_no_site_field_on_interface_or_link(self):
        # neither model carries a site name of its own, so administration.SITE_NAME_FIELDS needs no entry for them:
        # visibility always comes from the owning device(s)
        from administration.services import SITE_NAME_FIELDS
        self.assertFalse(any(f.name in ("site", "site_key") for f in Interface._meta.get_fields()))
        self.assertFalse(any(f.name in ("site", "site_key") for f in self.Link._meta.get_fields()))
        listed = {(a, m) for a, m, _f, _l in SITE_NAME_FIELDS}
        self.assertNotIn(("network", "Interface"), listed)
        self.assertIn(("network", "Device"), listed)


class QueryCountTests(PortCase):
    def make(self, name, ip, n):
        d = dev(name, mgmt_ip=ip, site="Mumbai (NSE)", prometheus_instance="sw1")
        Interface.objects.bulk_create([Interface(device=d, if_index=i, name=f"Gi1/0/{i}", utilization=float(i % 100), oper_status="up",
                                                 if_type="Ethernet", updated_at=timezone.now()) for i in range(1, n + 1)])
        return d

    def count(self, d):
        cache.clear()
        with mock.patch(INSTANT, side_effect=fake_instant), mock.patch(RANGE, side_effect=fake_range):
            with CaptureQueriesContext(connection) as ctx:
                r = self.c.get(reverse("network:interface", args=[d.pk, 1]))
        self.assertEqual(r.status_code, 200)
        return len(ctx)

    def test_database_queries_do_not_grow_with_the_number_of_ports(self):
        small, big = self.make("small", "192.0.2.101", 4), self.make("big", "192.0.2.102", 400)
        self.assertEqual(self.count(small), self.count(big))

    def test_with_documented_links_the_count_is_still_constant(self):
        from architecture.models import Link
        small, big = self.make("small2", "192.0.2.103", 4), self.make("big2", "192.0.2.104", 400)
        for d in (small, big):
            peer = dev(f"peer-{d.name}", mgmt_ip=f"192.0.2.{150 + d.pk % 50}", site="Mumbai (NSE)")
            Link.objects.create(tenant_key=T, a_device=d, a_port="Gi1/0/1", b_device=peer, b_port="Eth1")
        self.assertEqual(self.count(small), self.count(big))


class LiveAndCsvTests(PortCase):
    def live(self, **params):
        url = reverse("network:interface_live", args=[self.d.pk, 1])
        with mock.patch(INSTANT, side_effect=fake_instant), mock.patch(RANGE, side_effect=fake_range):
            return self.c.get(url, params, HTTP_ACCEPT="application/json")

    def test_live_shape(self):
        self.get(instant=fake_instant)       # the page found the optical sensors
        r = self.live(range="24h")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Cache-Control"], "no-store")
        data = r.json()
        self.assertEqual(set(data), {"generated_at", "stale", "oper_status", "values", "tones", "charts", "legends", "sparks", "gauge",
                                     "oper", "notes"})
        v = data["values"]
        self.assertEqual((v["kpi_in"], v["kpi_out"], v["kpi_util"], v["kpi_errors"], v["kpi_flaps"]), ("680 Mbps", "520 Mbps", "68%", "0.50/s", "2"))
        self.assertEqual((v["state_admin"], v["state_oper"]), ("Up", "Up"))
        self.assertEqual(data["tones"]["oper"], "ok")
        self.assertFalse(data["stale"])
        self.assertEqual(set(data["charts"]), {"pd-traffic", "pd-util", "pd-packets", "pd-errors", "pd-optics"})
        self.assertEqual(data["legends"]["pd-traffic"][0]["name"], "Inbound")
        self.assertIn("pd-spark-in", data["sparks"])
        self.assertEqual(data["gauge"]["value"], 68)
        self.assertEqual([x["cls"] for x in data["oper"]["segments"] if x["cls"] != "nodata"], ["up", "down", "up"])
        self.assertEqual(data["charts"]["pd-traffic"]["ts"][0], data["charts"]["pd-traffic"]["ts"][0] // 1)

    def test_live_without_data_or_prometheus(self):
        with override_settings(PROMETHEUS_URL=""):
            r = self.live()
        data = r.json()
        self.assertEqual(data["charts"], {})
        self.assertIn("No metrics source", data["notes"][0])
        self.assertEqual(data["values"]["kpi_in"], "680 Mbps")

    def test_live_stale_flag_and_login(self):
        Interface.objects.filter(pk=self.i1.pk).update(updated_at=timezone.now() - timedelta(hours=1))
        self.assertTrue(self.live().json()["stale"])
        self.assertEqual(Client().get(reverse("network:interface_live", args=[self.d.pk, 1])).status_code, 302)

    def test_live_is_rate_limited_per_user(self):
        with mock.patch(INSTANT, return_value=[]), mock.patch(RANGE, return_value=[]):
            url = reverse("network:interface_live", args=[self.d.pk, 1])
            codes = [self.c.get(url).status_code for _ in range(portviews.LIVE_PER_MINUTE + 3)]
            self.assertEqual(codes[:portviews.LIVE_PER_MINUTE], [200] * portviews.LIVE_PER_MINUTE)
            self.assertEqual(codes[portviews.LIVE_PER_MINUTE:], [429] * 3)
            r = self.c.get(url)
            self.assertEqual(r["Retry-After"], "30")
            other = Client()
            other.force_login(self.operator)
            self.assertEqual(other.get(url).status_code, 200)

    def csv(self, **params):
        url = reverse("network:interface_export", args=[self.d.pk, 1])
        with mock.patch(INSTANT, return_value=[]), mock.patch(RANGE, side_effect=params.pop("rng", fake_range)):
            return self.c.get(url, params)

    def test_csv_content(self):
        r = self.csv(range="6h")
        self.assertEqual(r["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("attachment; filename=\"core-sw-01-Gi1-0-1-6h.csv\"", r["Content-Disposition"])
        rows = list(csv.reader(io.StringIO(r.content.decode())))
        self.assertEqual(rows[0], portviews.CSV_COLUMNS)
        self.assertEqual(len(rows) - 1, 6)
        first = dict(zip(rows[0], rows[1]))
        self.assertEqual((first["device"], first["interface"], first["alias"]), ("core-sw-01", "Gi1/0/1", "Uplink"))
        self.assertEqual(float(first["in_mbps"]), 10.0)
        self.assertEqual(float(first["utilization_pct"]), 1.0)
        self.assertTrue(first["timestamp_utc"].endswith("Z"))
        states = [dict(zip(rows[0], x))["oper_state"] for x in rows[1:]]
        self.assertEqual(states, ["up", "up", "down", "down", "up", "up"])

    def test_csv_is_always_mbps(self):
        rows = list(csv.reader(io.StringIO(self.csv(rng=lambda p, **k: fake_range(p, octets=3e8, **k)).content.decode())))
        self.assertEqual(float(dict(zip(rows[0], rows[1]))["in_mbps"]), 2400.0)

    def test_csv_is_formula_safe(self):
        Interface.objects.filter(pk=self.i1.pk).update(name='=HYPERLINK("http://x","y")', alias="@SUM(1+1)")
        Device.objects.filter(pk=self.d.pk).update(name="+cmd|' /C calc'!A0")
        r = self.csv()
        rows = list(csv.reader(io.StringIO(r.content.decode())))
        row = dict(zip(rows[0], rows[1]))
        self.assertTrue(row["interface"].startswith("'="))
        self.assertTrue(row["alias"].startswith("'@"))
        self.assertTrue(row["device"].startswith("'+"))
        self.assertNotIn("\n", r["Content-Disposition"])
        self.assertRegex(r["Content-Disposition"], r'^attachment; filename="[A-Za-z0-9._-]+\.csv"$')

    def test_csv_rows_are_bounded(self):
        def huge(promql, seconds=3600, step=60):
            end = (int(time.time()) // step) * step
            ts = [end - (5000 - i) * step for i in range(5000)]
            return [{"metric": {"m": "in"}, "values": [(t, 1e6) for t in ts]}] if "label_replace" in promql else []
        rows = list(csv.reader(io.StringIO(self.csv(rng=huge).content.decode())))
        self.assertLessEqual(len(rows) - 1, portmetrics.MAX_POINTS_CSV)
        self.assertGreater(len(rows) - 1, 100)

    def test_csv_without_data_is_just_the_header_and_throttled(self):
        r = self.csv(rng=lambda *a, **k: [])
        self.assertEqual(r.content.decode().strip().splitlines(), [",".join(portviews.CSV_COLUMNS)])
        codes = [self.csv().status_code for _ in range(portviews.CSV_PER_MINUTE + 1)]
        self.assertEqual(codes[-1], 429)


class PortLayoutTests(PortCase):
    """Small presentation rules found in the browser review."""

    def zero_range(self, promql, **kw):
        rows = fake_range(promql, **kw)
        if "label_replace" in promql:
            for r in rows:
                if r["metric"]["m"] not in ("in", "out"):
                    r["values"] = [(t, 0.0) for t, _v in r["values"]]
        return rows

    def test_all_zero_error_and_packet_charts_get_a_fixed_axis(self):
        r = self.get(rng=self.zero_range)
        errors = next(p["chart"] for p in r.context["panels"] if p["key"] == "errors")
        self.assertEqual((errors["yMin"], errors["yMax"]), (0, 1))
        cache.clear()
        busy = self.get()    # the default stub has errors above zero: the chart keeps its own axis
        self.assertNotIn("yMax", next(p["chart"] for p in busy.context["panels"] if p["key"] == "errors"))

    def test_state_bands_fill_the_open_half_row(self):
        r = self.get()    # util, packets, errors: three half-width graphs
        self.assertTrue(r.context["oper_half"])
        self.assertContains(r, "pd-panel-oper half")
        cache.clear()
        full = self.get(instant=fake_instant)    # plus optics: even again
        self.assertFalse(full.context["oper_half"])
        self.assertNotContains(full, "pd-panel-oper half")

    def test_sensor_thresholds_sit_with_their_sensor(self):
        r = self.get(instant=fake_instant)
        rx = next(s for s in r.context["sensors"] if s["label"] == "Transceiver Rx Power Sensor")
        self.assertEqual(rx["thresholds"], ["< -14 dBm (major)"])
        self.assertContains(r, "Thresholds &lt; -14 dBm (major)")

    def test_one_banner_per_prometheus_failure(self):
        err = prometheus.PrometheusError("Prometheus unreachable: refused")
        with mock.patch(INSTANT, side_effect=err), mock.patch(RANGE, side_effect=err):
            r = self.c.get(self.url)
        self.assertEqual(r.content.decode().count("Prometheus unreachable: refused</span></div>"), 1)
        self.assertNotContains(r, "Some live details could not be read")

    def test_stale_banner_reads_as_a_sentence_and_speed_gap_is_said(self):
        Interface.objects.filter(pk=self.i1.pk).update(updated_at=timezone.now() - timedelta(minutes=30), speed_mbps=None)
        r = self.get()
        self.assertContains(r, "were last updated 30 min ago")
        self.assertContains(r, "Speed not reported")


class PortMigrationTests(TransactionTestCase):
    def test_0007_is_reversible_and_additive(self):
        columns = lambda: {c.name for c in connection.introspection.get_table_description(connection.cursor(), "network_interface")}  # noqa: E731
        try:
            executor = MigrationExecutor(connection)
            executor.migrate([("network", "0006_datasheet_photo_candidates")])
            self.assertTrue({"mtu", "mac", "duplex", "last_change_at"}.isdisjoint(columns()))
            self.assertIn("flaps_24h", columns())
            executor = MigrationExecutor(connection)
            executor.migrate([("network", "0007_interface_port_details")])
            self.assertTrue({"mtu", "mac", "duplex", "last_change_at"} <= columns())
        finally:
            executor = MigrationExecutor(connection)
            executor.migrate(executor.loader.graph.leaf_nodes())
