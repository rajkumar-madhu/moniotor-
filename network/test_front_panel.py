"""Device detail front panel, port drawer data, ?port= chart scoping, interface filters and the errors chart."""
import json
import re
from unittest import mock

from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import metrics
from .models import Interface
from .tests import T, dev, users
from .views import FRONT_PANEL_MAX, _front_panel


def port(device, idx, **kw):
    kw.setdefault("name", f"Ethernet{idx}")
    kw.setdefault("if_type", "Ethernet")
    kw.setdefault("admin_status", "up")
    kw.setdefault("oper_status", "up")
    kw.setdefault("speed_mbps", 25000)
    return Interface.objects.create(device=device, if_index=idx, **kw)


def series(*values):
    now = timezone.now().timestamp()
    return [{"metric": {}, "values": [(now - 60 * (len(values) - k), v) for k, v in enumerate(values)]}]


class PortToneTests(TestCase):
    def tone(self, **kw):
        return Interface(name="x", if_index=1, **kw).port_tone

    def test_states(self):
        self.assertEqual(self.tone(admin_status="down", oper_status="down"), "unused")
        self.assertEqual(self.tone(admin_status="up", oper_status="notPresent"), "unused")
        self.assertEqual(self.tone(admin_status="up", oper_status="lowerLayerDown"), "down")
        self.assertEqual(self.tone(admin_status="unknown", oper_status="down"), "down")
        self.assertEqual(self.tone(admin_status="up", oper_status="up", utilization=79.9, errors_per_s=0), "up")
        self.assertEqual(self.tone(admin_status="up", oper_status="up", utilization=80), "busy")
        self.assertEqual(self.tone(admin_status="up", oper_status="up", errors_per_s=0.001), "busy")
        self.assertEqual(self.tone(admin_status="up", oper_status="dormant"), "unknown")
        self.assertEqual(Interface(admin_status="up", oper_status="up", utilization=90).port_tone_label, "busy or errors")

    def test_logical_interfaces_are_not_on_the_panel(self):
        for t in ("VLAN", "Loopback", "LAG", "Tunnel", "Virtual", "Other"):
            self.assertFalse(Interface(if_type=t).is_physical, t)
        for t in ("Ethernet", "Wireless", "", "fibreChannel"):
            self.assertTrue(Interface(if_type=t).is_physical, t)


@override_settings(LINKEDEYE_TENANT=T)
class FrontPanelLayoutTests(TestCase):
    def test_runs_of_one_speed_split_into_groups_of_twelve(self):
        d = dev(prometheus_instance="sw1")
        ps = [port(d, i) for i in range(1, 49)] + [port(d, i, speed_mbps=100000) for i in range(49, 53)]
        blocks = _front_panel(ps)
        self.assertEqual([b["label"] for b in blocks], ["48 × 25G", "4 × 100G"])
        self.assertEqual([len(c) for c in blocks[0]["chunks"]], [12, 12, 12, 12])
        self.assertEqual(blocks[1]["chunks"][0][0][0], 49)   # panel positions continue across blocks

    def test_ports_without_a_speed_join_the_current_run(self):
        d = dev()
        ps = [port(d, 1, speed_mbps=None), port(d, 2), port(d, 3, speed_mbps=0, oper_status="down"), port(d, 4)]
        self.assertEqual([b["label"] for b in _front_panel(ps)], ["4 × 25G"])
        self.assertEqual([b["label"] for b in _front_panel([port(d, 9, speed_mbps=None)])], ["1 ports"])


@override_settings(LINKEDEYE_TENANT=T, PROMETHEUS_URL="")
class DeviceDetailFrontPanelTests(TestCase):
    def setUp(self):
        self.viewer = users()[0]
        self.d = dev("core-sw-01", vendor="Arista", prometheus_instance="sw1")
        for i in range(1, 49):
            port(self.d, i, alias=f"srv-{i:02d}", utilization=88 if i == 7 else 20,
                 oper_status="down" if i in (13, 40) else "up", admin_status="down" if i == 40 else "up",
                 errors_per_s=0.5 if i == 21 else 0, discards_per_s=1.25 if i == 21 else 0, flaps_24h=3 if i == 21 else 0)
        for i in range(49, 53):
            port(self.d, i, speed_mbps=100000)
        Interface.objects.create(device=self.d, if_index=1000, name="Vlan10", if_type="VLAN", admin_status="up", oper_status="up")
        self.c = Client()
        self.c.force_login(self.viewer)
        self.url = reverse("network:detail", args=[self.d.pk])

    def test_overview_draws_physical_ports_and_kpis(self):
        r = self.c.get(self.url)
        self.assertContains(r, 'id="front-panel"')
        self.assertContains(r, "48 × 25G · 4 × 100G")
        html = r.content.decode()
        self.assertEqual(html.count('class="fp-port '), 52)            # the VLAN interface is not a port
        self.assertIn('class="fp-port busy" data-port="6"', html)      # port 7: 88 % utilised
        self.assertIn('class="fp-port busy" data-port="20"', html)     # port 21: errors
        self.assertIn('class="fp-port down" data-port="12"', html)
        self.assertIn('class="fp-port unused" data-port="39"', html)
        self.assertContains(r, 'aria-label="Port 21, Ethernet21, busy or errors"')
        self.assertContains(r, "network/front-panel.js")
        self.assertContains(r, "Ports up / down")
        self.assertContains(r, "50 / ")            # 52 ports, 13 down and 40 admin down
        self.assertContains(r, "1 unused or admin down")
        self.assertContains(r, "0.50/s")           # errors KPI
        data = json.loads(re.search(r'<script id="nw-ports" type="application/json">(.*?)</script>', html, re.S).group(1))
        self.assertEqual(len(data), 52)
        self.assertEqual({k: data[20][k] for k in ("n", "ifIndex", "tone", "errors", "discards", "flaps")},
                         {"n": 21, "ifIndex": 21, "tone": "busy", "errors": "0.50/s", "discards": "1.25/s", "flaps": "3"})

    def test_device_reported_names_are_escaped(self):
        Interface.objects.filter(device=self.d, if_index=1).update(name='</script><img src=x onerror=alert(1)>')
        html = self.c.get(self.url).content.decode()
        self.assertNotIn("<img src=x", html)
        self.assertIn("\\u003C/script\\u003E\\u003Cimg", html)   # json_script escapes the drawer data

    def test_no_panel_without_physical_interfaces(self):
        empty = dev("vm-01", mgmt_ip="192.0.2.20", device_type="virtual_machine")
        r = self.c.get(reverse("network:detail", args=[empty.pk]))
        self.assertNotContains(r, 'id="front-panel"')
        self.assertNotContains(r, "front-panel.js")

    def test_panel_is_capped(self):
        big = dev("big-sw", mgmt_ip="192.0.2.30")
        Interface.objects.bulk_create([Interface(device=big, if_index=i, name=f"Gi{i}", if_type="Ethernet")
                                       for i in range(1, FRONT_PANEL_MAX + 6)])
        r = self.c.get(reverse("network:detail", args=[big.pk]))
        self.assertEqual(r.content.decode().count('class="fp-port '), FRONT_PANEL_MAX)
        self.assertContains(r, f"Showing the first {FRONT_PANEL_MAX} of {FRONT_PANEL_MAX + 5} ports")

    @override_settings(PROMETHEUS_URL="http://prometheus.test:9090")
    def test_port_filter_scopes_the_charts(self):
        with mock.patch("core.prometheus.query_range", return_value=series(1.0, 2.0)) as qr:
            r = self.c.get(self.url + "?tab=overview&port=21")
        self.assertContains(r, "Charts filtered to <b>Ethernet21</b>")
        self.assertContains(r, 'data-selected="20"')
        self.assertContains(r, "Errors + discards · Ethernet21")
        queries = [c.args[0] for c in qr.call_args_list]
        self.assertEqual(len(queries), 4)   # traffic in/out, errors, discards
        self.assertTrue(all('instance="sw1",ifIndex="21"' in q for q in queries), queries)
        self.assertContains(r, 'id="nw-errors"')
        for bad in ("999", "abc", "-1"):    # unknown or malformed: the whole device
            with mock.patch("core.prometheus.query_range", return_value=series(1.0)) as qr:
                r = self.c.get(self.url + f"?tab=overview&port={bad}")
            self.assertNotContains(r, "Charts filtered to")
            self.assertTrue(all("ifIndex" not in c.args[0] for c in qr.call_args_list))

    def test_interfaces_filters_and_search(self):
        r = self.c.get(self.url + "?tab=interfaces")
        for label, count in (("All", 53), ("Up", 51), ("Busy or errors", 2), ("Down", 1), ("Unused or admin down", 1)):
            self.assertContains(r, f"{label} <small>{count}</small>")
        self.assertContains(r, "Err/s")
        self.assertContains(r, "Flaps (24h)")
        r = self.c.get(self.url + "?tab=interfaces&state=down")
        self.assertContains(r, "Ethernet13")
        self.assertNotContains(r, "Ethernet14<")
        r = self.c.get(self.url + "?tab=interfaces&state=busy")
        self.assertContains(r, "Ethernet7")
        self.assertContains(r, "Ethernet21")
        self.assertNotContains(r, "Ethernet8 ")
        r = self.c.get(self.url + "?tab=interfaces&q=SRV-2")   # case-insensitive, name or description
        self.assertContains(r, "srv-21")
        self.assertNotContains(r, "srv-31")
        r = self.c.get(self.url + "?tab=interfaces&state=bogus&q=nothing-matches")
        self.assertContains(r, "No interfaces match")
        self.assertContains(r, 'class="on" aria-current="true">All')

    def test_csv_has_the_counter_columns(self):
        rows = self.c.get(reverse("network:interfaces_export", args=[self.d.pk])).content.decode().splitlines()
        self.assertTrue(rows[0].endswith("errors_per_s,discards_per_s,flaps_24h,updated_at"))
        self.assertIn(",0.5,1.25,3,", rows[21])


@override_settings(LINKEDEYE_TENANT=T, PROMETHEUS_URL="http://prometheus.test:9090")
class ErrorsRangeTests(TestCase):
    def test_errors_and_discards_series(self):
        d = dev(prometheus_instance="sw1")
        with mock.patch("core.prometheus.query_range", return_value=series(0.0, 2.5)) as qr:
            chart, msg = metrics.errors_range(d, if_index=7)
        self.assertEqual(msg, "")
        self.assertEqual([s["name"] for s in chart["series"]], ["Errors", "Discards"])
        self.assertEqual(chart["series"][0]["values"], [0.0, 2.5])
        self.assertEqual(chart["unit"], "/s")
        first, second = (c.args[0] for c in qr.call_args_list)
        self.assertIn('__name__=~"ifInErrors|ifOutErrors",instance="sw1",ifIndex="7"', first)
        self.assertIn("ifInDiscards|ifOutDiscards", second)
        with mock.patch("core.prometheus.query_range", return_value=[]):
            self.assertIn("No error or discard counters", metrics.errors_range(d)[1])

    @override_settings(PROMETHEUS_URL="")
    def test_not_configured(self):
        self.assertIn("LINKEDEYE_PROMETHEUS_URL", metrics.errors_range(dev(prometheus_instance="sw1"))[1])
