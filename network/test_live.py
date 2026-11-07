"""Live monitoring: the network:live JSON snapshot, graph ranges, port bars and VM uptime from node_exporter."""
import json
import time
from unittest import mock

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from core import prometheus
from core.models import Membership, Site

from . import jobs, metrics, services
from .models import Device, DevicePoll, Interface
from .tests import T, dev, fail, ok, prom_row, users

PROM = "http://prometheus.test:9090"


def series(*values, step=60):
    start = time.time() - step * len(values)
    return [{"metric": {}, "values": [(start + i * step, v) for i, v in enumerate(values)]}]


def guest_query(promql):
    """node_exporter-shaped instant answers for instance vm1:9100."""
    if "node_cpu_seconds_total" in promql:
        return [prom_row(12.5)]
    if "MemAvailable" in promql:
        return [prom_row(4.0e9)]
    if "MemTotal" in promql:
        return [prom_row(16.0e9)]
    if "node_filesystem" in promql:
        return [prom_row(40.0)]
    if "node_network" in promql:
        return [prom_row(8.25)]
    if "node_boot_time_seconds" in promql:
        return [prom_row(3 * 86400 + 7200.0, instance="vm1:9100")]
    return []


@override_settings(LINKEDEYE_TENANT=T, PROMETHEUS_URL="")
class LiveEndpointTests(TestCase):
    def setUp(self):
        self.viewer, self.operator, self.admin = users()
        self.d = dev("core-sw-01", site="Mumbai (NSE)", prometheus_instance="sw1")
        Interface.objects.create(device=self.d, if_index=1, name="Gi1/0/1", alias="Uplink", oper_status="up",
                                 admin_status="up", if_type="Ethernet", speed_mbps=1000, in_mbps=680, out_mbps=520,
                                 utilization=68, errors_per_s=0.5, discards_per_s=0)
        Interface.objects.create(device=self.d, if_index=2, name="Gi1/0/2", oper_status="down", admin_status="up",
                                 if_type="Ethernet", speed_mbps=1000)
        self.c = Client()
        self.url = reverse("network:live", args=[self.d.pk])

    def test_login_required(self):
        r = self.c.get(self.url)
        self.assertEqual(r.status_code, 302)

    def test_snapshot_values_tones_and_ports(self):
        jobs.record(self.d, ok(3.0))
        self.c.force_login(self.viewer)
        r = self.c.get(self.url, HTTP_ACCEPT="application/json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Cache-Control"], "no-store")
        data = r.json()
        self.assertEqual(data["status"], "up")
        v = data["values"]
        self.assertEqual(v["status"], "Up")
        self.assertEqual(v["latency"], "3.0 ms")
        self.assertEqual(v["avail24"], "100.00%")
        self.assertEqual((v["ports_up"], v["ports_down"], v["ports_unused"]), ("1", "1", "0"))
        self.assertEqual((v["traffic_in"], v["traffic_out"]), ("680 Mbps", "520 Mbps"))
        self.assertEqual(v["errors"], "0.50/s")
        self.assertEqual(data["tones"], {"status": "ok", "avail24": "ok", "errors": "warn", "ports_down": "crit"})
        self.assertEqual([p["tone"] for p in data["ports"]], ["busy", "down"])
        self.assertEqual(data["charts"]["nw-port-bars"]["labels"], ["Gi1/0/1"])
        self.assertIsNotNone(data["charts"]["nw-avail"])
        self.assertIsNone(data["charts"]["nw-traffic-strip"])   # Prometheus not configured: no invented series
        self.assertEqual(data["notes"], [])                     # the page already says it is not connected
        self.assertIsNone(data["latest_event"])

    def test_status_change_and_alert_are_reported(self):
        self.c.force_login(self.viewer)
        jobs.record(self.d, fail())
        data = self.c.get(self.url).json()
        self.assertEqual(data["status"], "down")
        self.assertEqual(data["tones"]["status"], "crit")
        self.assertEqual(data["latest_event"]["title"], "core-sw-01 is down")

    def test_port_filter_drives_legends(self):
        self.c.force_login(self.viewer)
        v = self.c.get(self.url + "?port=2").json()["values"]
        self.assertEqual((v["legend_in"], v["legend_out"]), ("–", "–"))    # Gi1/0/2 has no rate
        self.assertEqual(v["traffic_in"], "680 Mbps")                        # KPIs stay whole-device
        v = self.c.get(self.url + "?port=99").json()["values"]                 # not on this device: whole device
        self.assertEqual(v["legend_in"], "680 Mbps")

    def test_out_of_scope_device_is_404_json(self):
        Site.objects.create(tenant_key=T, name="Mumbai (NSE)", code="mum")
        other = Site.objects.create(tenant_key=T, name="DR Site", code="dr")
        u = User.objects.create_user("dr-only", password="pw")
        Membership.objects.create(user=u, site=other, role="viewer")
        self.c.force_login(u)
        r = self.c.get(self.url, HTTP_ACCEPT="application/json")
        self.assertEqual(r.status_code, 404)
        self.assertIn("error", r.json())

    def test_post_not_allowed(self):
        self.c.force_login(self.operator)
        self.assertEqual(self.c.post(self.url).status_code, 405)

    def test_detail_page_wires_live_polling(self):
        self.c.force_login(self.viewer)
        r = self.c.get(reverse("network:detail", args=[self.d.pk]) + "?range=1h&port=1")
        self.assertContains(r, 'id="nw-live"')
        self.assertContains(r, f'data-url="{self.url}?range=1h&amp;port=1"')
        self.assertContains(r, "network/live.js")
        self.assertContains(r, 'data-live="traffic_in"')
        self.assertContains(r, "Graphs · Last hour")
        r = self.c.get(reverse("network:detail", args=[self.d.pk]) + "?range=bogus")
        self.assertContains(r, "Graphs · Last 24 hours")


@override_settings(LINKEDEYE_TENANT=T, PROMETHEUS_URL=PROM)
class LiveGraphTests(TestCase):
    def setUp(self):
        self.viewer = users()[0]
        self.d = dev("core-sw-01", prometheus_instance="sw1")
        self.c = Client()
        self.c.force_login(self.viewer)

    def test_range_controls_query_window(self):
        with mock.patch("core.prometheus.query_range", return_value=series(10.0, 20.0)) as qr:
            r = self.c.get(reverse("network:detail", args=[self.d.pk]) + "?tab=traffic&range=7d")
        self.assertEqual(r.status_code, 200)
        self.assertEqual({c.kwargs["seconds"] for c in qr.call_args_list}, {7 * 86400})
        self.assertContains(r, 'id="nw-traffic-strip"')
        self.assertContains(r, "Last 7 days")
        self.assertContains(r, "range=7d")   # tab links keep the range
        with mock.patch("core.prometheus.query_range", return_value=series(1.0, 2.0)) as qr:
            data = self.c.get(reverse("network:live", args=[self.d.pk]) + "?range=1h").json()
        self.assertEqual({c.kwargs["seconds"] for c in qr.call_args_list}, {3600})
        self.assertEqual(data["charts"]["nw-traffic-strip"]["series"][0]["values"], [1.0, 2.0])

    def test_series_from_separate_queries_share_x_positions(self):
        # each range query ends at its own time.time(): answers arrive a few ms apart, as with real Prometheus
        calls = []

        def staggered(promql, seconds=3600, step=None):
            calls.append(promql)
            start = 1_700_000_000.0 + 0.037 * len(calls)
            return [{"metric": {}, "values": [(start + k * step, 10.0 * len(calls) + k) for k in range(5)]}]
        with mock.patch("core.prometheus.query_range", side_effect=staggered):
            chart, _msg = metrics.traffic_range(self.d, 3600)
        self.assertEqual(len(chart["labels"]), 5)
        for s in chart["series"]:
            self.assertNotIn(None, s["values"])   # interleaved gaps used to draw the lines as dots
        self.assertEqual(chart["series"][1]["values"], [20.0, 21.0, 22.0, 23.0, 24.0])

    def test_prometheus_failure_is_a_note_not_a_crash(self):
        with mock.patch("core.prometheus.query_range", side_effect=prometheus.PrometheusError("HTTP 500")):
            data = self.c.get(reverse("network:live", args=[self.d.pk])).json()
        self.assertIsNone(data["charts"]["nw-traffic-strip"])
        self.assertTrue(any("HTTP 500" in n for n in data["notes"]))


@override_settings(LINKEDEYE_TENANT=T, PROMETHEUS_URL=PROM)
class VmLiveTests(TestCase):
    def setUp(self):
        self.viewer = users()[0]
        self.vm = dev("VM-APP-01", device_type="virtual_machine", mgmt_ip="192.0.2.40", prometheus_instance="vm1:9100",
                      status="up")
        self.c = Client()
        self.c.force_login(self.viewer)

    def test_guest_metrics_and_uptime_are_live(self):
        with mock.patch("core.prometheus.query", side_effect=guest_query), \
                mock.patch("core.prometheus.query_range", return_value=series(11.0, 12.5)):
            r = self.c.get(reverse("network:detail", args=[self.vm.pk]))
            data = self.c.get(reverse("network:live", args=[self.vm.pk])).json()
        self.assertContains(r, "12.5%")                 # CPU
        self.assertContains(r, "75.0%")                 # memory: 1 - 4/16
        self.assertContains(r, "3 days, 2 hours")       # node_boot_time_seconds
        self.assertContains(r, "node_exporter instance vm1:9100")
        self.assertNotContains(r, "Guest metrics not connected")
        v = data["values"]
        self.assertEqual((v["cpu"], v["memory"], v["disk"], v["network"]), ("12.5%", "75.0%", "40.0%", "8.2 Mb/s"))
        self.assertEqual(v["uptime_short"], "3 days")
        self.assertEqual(data["tones"]["cpu"], "")
        self.assertIsNotNone(data["charts"]["nw-vm-cpu"])
        self.assertEqual(data["ports"], [])

    def test_missing_node_exporter_series_says_so(self):
        with mock.patch("core.prometheus.query", return_value=[]), mock.patch("core.prometheus.query_range", return_value=[]):
            r = self.c.get(reverse("network:detail", args=[self.vm.pk]))
            data = self.c.get(reverse("network:live", args=[self.vm.pk])).json()
        self.assertContains(r, "No node_exporter series in Prometheus for instance")
        self.assertEqual(data["values"]["cpu"], "–")
        self.assertEqual(data["tones"]["cpu"], "mute")
        self.assertTrue(data["notes"])

    def test_metrics_job_takes_vm_uptime_from_node_exporter(self):
        sw = dev("sw-snmp", mgmt_ip="192.0.2.41", prometheus_instance="sw9")
        with mock.patch("core.prometheus.query", side_effect=guest_query):
            jobs.update_metrics([self.vm, sw])
        self.vm.refresh_from_db()
        sw.refresh_from_db()
        self.assertEqual(self.vm.uptime_seconds, 3 * 86400 + 7200)
        self.assertEqual(self.vm.metrics_error, "")
        self.assertIsNotNone(self.vm.metrics_updated_at)
        self.assertIn("No snmp_exporter series", sw.metrics_error)   # switches still report the missing SNMP data
        self.assertIsNone(sw.uptime_seconds)


class HelperTests(TestCase):
    @override_settings(LINKEDEYE_TENANT=T)
    def test_interface_summary_and_port_bars(self):
        d = dev()
        self.assertIsNone(metrics.port_bars([]))
        empty = services.interface_summary([])
        self.assertIsNone(empty["total_in"])
        self.assertEqual(empty["ports_total"], 0)
        for i, (inn, out) in enumerate([(5.0, 1.0), (None, None), (50.0, 70.0)], 1):
            Interface.objects.create(device=d, if_index=i, name=f"p{i}", oper_status="up", admin_status="up",
                                     if_type="Ethernet", in_mbps=inn, out_mbps=out)
        Interface.objects.create(device=d, if_index=9, name="Vlan1", if_type="VLAN", oper_status="up", admin_status="up",
                                 in_mbps=1.0)
        ifs = list(d.interfaces.all())
        s = services.interface_summary(ifs)
        self.assertEqual((s["if_total"], s["ports_total"], s["total_in"], s["total_out"]), (4, 3, 56.0, 71.0))
        bars = metrics.port_bars(ifs)
        self.assertEqual(bars["labels"], ["p3", "p1", "Vlan1"])
        self.assertEqual(bars["series"][1]["values"], [70.0, 1.0, None])

    def test_range_key(self):
        self.assertEqual(metrics.range_key("6h"), "6h")
        self.assertEqual(metrics.range_key("1y"), metrics.DEFAULT_RANGE)
        self.assertEqual(metrics.range_key(None), metrics.DEFAULT_RANGE)
