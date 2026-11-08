"""Hostile input against the per-port page: URL values, PromQL selector construction, odd or oversized Prometheus answers,
labels with markup, CSV formulas, the rate limits and cache keys. Prometheus is always mocked."""
import csv
import io
import json
import time
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from . import jobs, metrics, portmetrics, portviews
from .models import Device, Interface
from .test_port_detail import INSTANT, RANGE, PortCase, calls, empty_instant, fake_instant, fake_range
from .tests import dev, prom_row

NAN, INF = float("nan"), float("inf")
EVIL = '<img src=x onerror=alert(1)>"\'`${x}\n\r\t'


def strict(response):
    """Parse a JSON body the way a browser does: NaN / Infinity are errors."""
    def bad(token):
        raise ValueError(f"invalid JSON constant {token}")
    return json.loads(response.content.decode(), parse_constant=bad)


class UrlValueTests(PortCase):
    def test_odd_ifindex_values_are_404_on_all_three_urls(self):
        base = f"/network/devices/{self.d.pk}/interfaces/"
        for bad in ("-1", "+1", "1.5", "1e3", "%D9%A1", "%C2%B2", "1%0A", "1%20", "1%00", "0x1", "99999999999999999999999",
                    "9" * 5000, "2147483648", "2147483647", "0", "4294967296"):
            for tail in ("/", "/live/", "/export.csv"):
                r = self.c.get(base + bad + tail)
                self.assertEqual(r.status_code, 404, (bad, tail, r.status_code))
        self.assertEqual(self.q.call_count if hasattr(self, "q") else 0, 0)

    def test_odd_device_ids_are_404(self):
        for bad in ("99999999999999999999999", "9" * 5000, "2147483648", "0", "-1", "%D9%A1"):
            for tail in ("/", "/live/", "/export.csv"):
                r = self.c.get(f"/network/devices/{bad}/interfaces/1{tail}")
                self.assertEqual(r.status_code, 404, (bad, tail, r.status_code))

    def test_leading_zeros_do_not_make_a_second_cache_entry(self):
        self.get(instant=fake_instant)
        first = calls(self.q, self.qr)
        r = self.get(url=f"/network/devices/{self.d.pk}/interfaces/0001/", instant=fake_instant)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(calls(self.q, self.qr), 0, first)

    def test_goto_only_follows_a_port_of_this_device(self):
        other = dev("other-sw", mgmt_ip="192.0.2.77", prometheus_instance="sw9")
        Interface.objects.create(device=other, if_index=77, name="Eth77", updated_at=timezone.now())
        for goto in ("77", "-1", "٣", "2" * 400, "1e1", "2%0d%0aLocation:http://evil.example/", "http://evil.example/", "//evil.example",
                     "", " 2", "2 "):
            r = self.get(goto=goto)
            self.assertIn(r.status_code, (200,), goto)
        r = self.get(goto="3")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r["Location"], f"/network/devices/{self.d.pk}/interfaces/3/?range=24h")
        r = self.get(goto="2", range='"}<script>')
        self.assertEqual(r["Location"], f"/network/devices/{self.d.pk}/interfaces/2/?range=24h")

    def test_repeated_and_array_range_parameters(self):
        for qs in ("?range=1h&range=7d", "?range[]=1h", "?range=1h%00", "?range=%E2%80%AE7d"):
            r = self.get(url=self.url + qs, instant=fake_instant)
            self.assertEqual(r.status_code, 200, qs)
            for q in self.all_promql():
                self.assertNotIn("\x00", q)

    def test_hostile_query_string_is_escaped_in_the_range_links(self):
        r = self.get(url=self.url + '?tab="><script>alert(1)</script>&x=%27onmouseover%3D1')
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, "<script>alert(1)</script>")


class SelectorTests(PortCase):
    def test_hostile_instance_labels_stay_inside_the_string_literal(self):
        for inst in ('sw1"} or vector(1) #', 'sw\\', 'sw1\\"', '}{', 'a' * 200, '{__name__=~".+"}', 'sw1" ,ifIndex="2'):
            Device.objects.filter(pk=self.d.pk).update(prometheus_instance=inst)
            cache.clear()
            r = self.get(instant=fake_instant)
            self.assertEqual(r.status_code, 200, inst)
            escaped = metrics.promql_string(inst)
            for q in self.all_promql():
                self.assertIn(f'instance="{escaped}"', q, (inst, q))
            # the same text unescaped never appears outside of its quotes: removing every escaped literal leaves no quote of ours
            for q in self.all_promql():
                rest = q.replace(f'"{escaped}"', "").replace(f"{escaped}", "")
                self.assertNotIn("vector(1)", rest)

    def test_instance_with_control_characters_or_too_long_is_no_instance(self):
        # PostgreSQL enforces the 200-character column and refuses NUL bytes, so neither can be stored at all
        for inst in ("sw1\nup", "sw1\u202e", "s\x85w1"):
            Device.objects.filter(pk=self.d.pk).update(prometheus_instance=inst)
            cache.clear()
            r = self.get(instant=fake_instant)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(calls(self.q, self.qr), 0, repr(inst))
            self.assertContains(r, "Prometheus instance")

    def test_hostile_port_names_cannot_break_out_of_the_sensor_regex(self):
        for name in ('Gi1/0/1" or vector(1) #', 'Gi\\', 'Gi.*', 'Gi(1|2)', 'a' * 150, 'Gi1\nGi2', '"', '{}', '[a-z]+', '$^'):
            Interface.objects.filter(pk=self.i1.pk).update(name=name)
            cache.clear()
            r = self.get(instant=fake_instant)
            self.assertEqual(r.status_code, 200, name)
            for q in self.all_promql():
                if "entPhysicalName" in q:
                    body = q.split('entPhysicalName=~"', 1)[1]
                    # the regex string ends at the first unescaped quote and what follows is the closing of the selector
                    i, out = 0, []
                    while i < len(body) and body[i] != '"':
                        if body[i] == "\\":
                            i += 1
                        i += 1
                    self.assertIn(body[i:], ('"}', '"}'), (name, q))

    def test_regex_metacharacters_in_a_port_name_match_only_themselves(self):
        regex = portmetrics.name_regex("Gi1/0/1")
        self.assertEqual(regex, "Gi1/0/1( .*)?")
        self.assertEqual(portmetrics.name_regex("Gi.*"), "Gi\\\\.\\\\*( .*)?")
        self.assertEqual(portmetrics.name_regex("   "), "")


def weird_instant(rows):
    return lambda q: rows


class OddPrometheusAnswerTests(PortCase):
    SHAPES = [
        [None], ["x"], [1], [[]], [{}], [{"metric": None}], [{"metric": "x", "value": 1}], [{"metric": {"a": None}, "value": "1"}],
        [{"metric": {"__name__": None, "ifIndex": None}, "value": None}], [{"metric": [], "value": []}],
        [{"metric": {"__name__": "entSensorValue", "entPhysicalName": "Gi1/0/1 Rx Power", "entSensorType": "dBm"}, "value": INF}],
        [{"metric": {"__name__": "entSensorValue", "entPhysicalName": "Gi1/0/1 Rx Power", "entSensorType": "dBm"}, "value": NAN}],
        [{"metric": {"__name__": "entSensorValue", "entPhysicalName": "Gi1/0/1 Rx Power", "entSensorType": "dBm",
                     "entSensorScale": "\x00", "entSensorPrecision": "9" * 400}, "value": 1e308}],
        [{"metric": {"__name__": "lldpRemSysName", "lldpRemIndex": "1", "lldpRemSysName": 5}, "value": 1}],
        [{"metric": {"__name__": "lldpRemManAddrIfSubtype", "lldpRemManAddr": "0x" + "F" * 5000}, "value": 1}],
        [{"metric": {"__name__": "pethPsePortDetectionStatus"}, "value": 1e308},
         {"metric": {"__name__": "pethPsePortAdminEnable"}, "value": -INF}, {"metric": {"__name__": "cpeExtPsePortPwrConsumption"}, "value": INF}],
        [{"metric": {"ipAdEntAddr": "999.1.1.1"}, "value": 1}, {"metric": {"ipAdEntAddr": "<b>"}, "value": 1}],
        [{"metric": {"dot3adAggPortIndex": "١٢"}, "value": 1e30}, {"metric": {"dot3adAggPortIndex": "9" * 5000}, "value": NAN}],
        [{"metric": {"dot1dBasePort": "9" * 5000}, "value": 1}],
        [{"metric": {"dot1dStpPortState": EVIL}, "value": 1}],
    ]

    def test_instant_shapes_never_crash_the_page_or_the_json(self):
        for rows in self.SHAPES:
            cache.clear()
            r = self.get(instant=weird_instant(rows))
            self.assertEqual(r.status_code, 200, rows)
            self.assertNotContains(r, "<img src=x")
            url = reverse("network:interface_live", args=[self.d.pk, 1])
            with mock.patch(INSTANT, side_effect=weird_instant(rows)), mock.patch(RANGE, side_effect=fake_range):
                live = self.c.get(url)
            self.assertEqual(live.status_code, 200, rows)
            strict(live)
            cache.delete_many([k for k in ("x",)])

    def test_instant_value_is_not_a_float(self):
        for rows in ([{"metric": {"ipAdEntAddr": "10.0.0.1"}, "value": "abc"}], [{"metric": {}, "value": [1, 2]}],
                     [{"metric": {}, "value": {"a": 1}}]):
            cache.clear()
            self.assertEqual(self.get(instant=weird_instant(rows)).status_code, 200)

    def test_range_shapes_never_crash(self):
        t = int(time.time())
        shapes = [
            [None], ["x"], [{}], [{"metric": None, "values": None}], [{"metric": {"m": "in"}, "values": "abc"}],
            [{"metric": {"m": "in"}, "values": [(t, "x")]}], [{"metric": {"m": "in"}, "values": [None, (1,), (t, 1, 2)]}],
            [{"metric": {"m": "in"}, "values": [(NAN, 1.0), (INF, 1.0), (-INF, 1.0), (t, 1.0)]}],
            [{"metric": {"m": "in"}, "values": [(t - 60, 1e308), (t, INF)]}, {"metric": {"m": "out"}, "values": [(t, -INF)]}],
            [{"metric": {"m": "in"}, "values": [(t - 60, 1e308), (t, 1e308)]}, {"metric": {"m": "out"}, "values": [(t, 1e308)]}],
            [{"metric": {"m": "in_uni"}, "values": [(t, 1e300)]}, {"metric": {"m": "in_err"}, "values": [(t, -5.0)]}],
            [{"metric": {"ifOperStatus": EVIL}, "values": [(t, 1.0)]}, {"metric": {"ifOperStatus": "up"}, "values": [(t, 1e9)]}],
            [{"metric": {}, "values": [(t, 1.0)]}], [{"metric": {"m": ["in"]}, "values": [(t, 1.0)]}],
            [{"metric": {"m": "in"}, "values": [(t, 1.0)] * 5000}],
            [{"metric": {"m": "in"}, "values": [(t - i, 1.0) for i in range(5000)]}],
        ]
        for rows in shapes:
            cache.clear()
            r = self.get(rng=lambda *a, **k: rows)
            self.assertEqual(r.status_code, 200, rows[:1])
            self.assertNotContains(r, "<img src=x")
            live = reverse("network:interface_live", args=[self.d.pk, 1])
            with mock.patch(INSTANT, side_effect=empty_instant), mock.patch(RANGE, side_effect=lambda *a, **k: rows):
                cache.clear()
                resp = self.c.get(live)
            self.assertEqual(resp.status_code, 200)
            strict(resp)
            cache.clear()
            with mock.patch(INSTANT, side_effect=empty_instant), mock.patch(RANGE, side_effect=lambda *a, **k: rows):
                resp = self.c.get(reverse("network:interface_export", args=[self.d.pk, 1]))
            self.assertEqual(resp.status_code, 200)
            self.assertLessEqual(len(list(csv.reader(io.StringIO(resp.content.decode())))), portmetrics.MAX_POINTS_CSV + 1)
            cache.clear()
            self.c.get(reverse("network:interface_export", args=[self.d.pk, 1]))   # not rate-limited out of the next case

    def test_huge_and_infinite_numbers_give_valid_json(self):
        t = int(time.time())
        rows = [{"metric": {"m": "in"}, "values": [(t - 60, 1e308), (t, INF)]}, {"metric": {"m": "out"}, "values": [(t, 5.0)]}]
        with mock.patch(INSTANT, side_effect=empty_instant), mock.patch(RANGE, side_effect=lambda *a, **k: rows):
            r = self.c.get(reverse("network:interface_live", args=[self.d.pk, 1]))
        strict(r)
        self.assertEqual(r.status_code, 200)

    def test_nonfinite_stored_figures_give_valid_json(self):
        Interface.objects.filter(pk=self.i1.pk).update(utilization=1e308, in_mbps=1e308)
        with mock.patch(INSTANT, side_effect=empty_instant), mock.patch(RANGE, side_effect=fake_range):
            r = self.c.get(reverse("network:interface_live", args=[self.d.pk, 1]))
        strict(r)

    def test_many_series_and_huge_answers_are_bounded(self):
        t = int(time.time())
        many = [{"metric": {"m": f"x{i}"}, "values": [(t, 1.0)]} for i in range(20000)]
        r = self.get(rng=lambda *a, **k: many)
        self.assertEqual(r.status_code, 200)
        rows = [prom_row(-10, __name__="entSensorValue", entPhysicalName=f"Gi1/0/1 Rx lane {i}", entPhysicalIndex=str(i),
                         entSensorType="dBm") for i in range(5000)]
        cache.clear()
        r = self.get(instant=weird_instant(rows))
        self.assertEqual(r.status_code, 200)
        self.assertLess(len(r.content), 600_000)

    def test_labels_with_markup_and_newlines_are_text_everywhere(self):
        base = {"lldpRemIndex": "1"}
        rows = [prom_row(1, __name__="lldpRemSysName", lldpRemSysName=EVIL * 30, **base),
                prom_row(1, __name__="lldpRemPortId", lldpRemPortId=EVIL, **base),
                prom_row(1, __name__="lldpRemPortDesc", lldpRemPortDesc=EVIL, **base),
                prom_row(1, __name__="lldpRemChassisId", lldpRemChassisId=EVIL, **base),
                prom_row(1, __name__="lldpRemSysDesc", lldpRemSysDesc=EVIL * 50, **base),
                prom_row(1, __name__="entSensorValue", entPhysicalName="Gi1/0/1 " + EVIL, entSensorType="dBm"),
                prom_row(1, __name__="entSensorValue", entPhysicalName="Gi1/0/1 Rx", entSensorType="dBm",
                         entSensorScale=EVIL, entSensorPrecision=EVIL)]
        r = self.get(instant=weird_instant(rows))
        body = r.content.decode()
        self.assertNotIn("<img src=x", body)
        self.assertNotIn("onerror=alert(1)>", body.replace("&gt;", ""))


class CacheKeyTests(PortCase):
    def test_two_devices_with_the_same_ifindex_do_not_share_answers(self):
        other = dev("other-sw", mgmt_ip="192.0.2.77", prometheus_instance="sw9", site="Mumbai (NSE)")
        o1 = Interface.objects.create(device=other, if_index=1, name="Eth1", updated_at=timezone.now())
        self.get(instant=fake_instant)
        n = calls(self.q, self.qr)
        r = self.get(url=reverse("network:interface", args=[other.pk, 1]), instant=empty_instant)
        self.assertEqual(r.status_code, 200)
        self.assertGreater(calls(self.q, self.qr), 0, n)
        self.assertNotContains(r, "dist-b.example.net")
        self.assertNotContains(r, "Transceiver Rx")
        self.assertEqual(o1.if_index, 1)
        for q in self.all_promql():
            self.assertIn('instance="sw9"', q)

    def test_ranges_do_not_share_chart_answers(self):
        seen = []

        def rng(promql, seconds=3600, step=60):
            seen.append(seconds)
            return fake_range(promql, seconds, step)
        for key in ("1h", "6h", "7d"):
            self.get(rng=rng, range=key)
        self.assertIn(3600, seen)
        self.assertIn(6 * 3600, seen)
        self.assertIn(7 * 86400, seen)
        r = self.get(range="1h")
        self.assertEqual(calls(self.q, self.qr), 0)
        self.assertContains(r, "aria-current")

    def test_instance_change_is_not_served_from_the_old_instance_cache_after_disable(self):
        self.get(instant=fake_instant)
        Device.objects.filter(pk=self.d.pk).update(enabled=False)
        r = self.get(instant=fake_instant)
        self.assertEqual(calls(self.q, self.qr), 0)
        self.assertNotContains(r, "dist-b.example.net")
        self.assertNotContains(r, "Transceiver Rx")


class DisabledVanishedAndLimitTests(PortCase):
    def test_disabled_device_live_and_csv_show_nothing_current(self):
        Device.objects.filter(pk=self.d.pk).update(enabled=False)
        with mock.patch(INSTANT, side_effect=fake_instant) as q, mock.patch(RANGE, side_effect=fake_range) as qr:
            live = self.c.get(reverse("network:interface_live", args=[self.d.pk, 1]))
            export = self.c.get(reverse("network:interface_export", args=[self.d.pk, 1]))
        self.assertEqual(calls(q, qr), 0)
        data = strict(live)
        self.assertEqual(data["charts"], {})
        self.assertEqual(data["values"]["kpi_in"], "–")
        self.assertEqual(export.content.decode().strip().splitlines(), [",".join(portviews.CSV_COLUMNS)])

    def test_port_that_vanishes_between_polls_is_a_json_404(self):
        url = reverse("network:interface_live", args=[self.d.pk, 2])
        with mock.patch(INSTANT, side_effect=empty_instant), mock.patch(RANGE, side_effect=fake_range):
            self.assertEqual(self.c.get(url).status_code, 200)
            Interface.objects.filter(pk=self.i2.pk).delete()
            r = self.c.get(url)
        self.assertEqual(r.status_code, 404)
        self.assertIn("error", r.json())

    def test_device_with_no_interfaces_and_deleted_device(self):
        Interface.objects.filter(device=self.d).delete()
        self.assertEqual(self.get().status_code, 404)
        Device.objects.filter(pk=self.d.pk).delete()
        self.assertEqual(self.c.get(self.url).status_code, 404)

    def test_live_and_csv_limits_are_per_user_and_per_kind(self):
        live = reverse("network:interface_live", args=[self.d.pk, 1])
        exp = reverse("network:interface_export", args=[self.d.pk, 1])
        with mock.patch(INSTANT, return_value=[]), mock.patch(RANGE, return_value=[]):
            for _ in range(portviews.LIVE_PER_MINUTE + 1):
                self.c.get(live)
            self.assertEqual(self.c.get(live).status_code, 429)
            self.assertEqual(self.c.get(exp).status_code, 200)      # another budget
            self.assertEqual(self.c.get(self.url).status_code, 200)  # the page itself is not on the live budget
            # a different port of the same user shares the budget (it is per user, not per URL)
            self.assertEqual(self.c.get(reverse("network:interface_live", args=[self.d.pk, 3])).status_code, 429)

    def test_a_broken_cache_never_blocks_the_page(self):
        with mock.patch("network.portviews.cache.add", side_effect=RuntimeError("down")):
            with mock.patch(INSTANT, return_value=[]), mock.patch(RANGE, return_value=[]):
                self.assertEqual(self.c.get(reverse("network:interface_live", args=[self.d.pk, 1])).status_code, 200)

    def test_429s_never_reach_prometheus(self):
        live = reverse("network:interface_live", args=[self.d.pk, 1])
        with mock.patch(INSTANT, return_value=[]), mock.patch(RANGE, return_value=[]):
            for _ in range(portviews.LIVE_PER_MINUTE):
                self.c.get(live)
        with mock.patch(INSTANT, return_value=[]) as q, mock.patch(RANGE, return_value=[]) as qr:
            cache.delete_many([k for k in ("nwport:x",)])
            self.assertEqual(self.c.get(live).status_code, 429)
        self.assertEqual(calls(q, qr), 0)


class CsvInjectionTests(PortCase):
    def csv(self, rng=fake_range):
        with mock.patch(INSTANT, return_value=[]), mock.patch(RANGE, side_effect=rng):
            return self.c.get(reverse("network:interface_export", args=[self.d.pk, 1]), {"range": "6h"})

    def test_every_text_cell_is_formula_safe_including_the_state_label(self):
        t = int(time.time() // 360 * 360)

        def rng(promql, seconds=3600, step=60):
            if promql.startswith("ifOperStatus"):
                return [{"metric": {"ifOperStatus": '=cmd|" /c calc"!A0'}, "values": [(t, 1.0)]}]
            return fake_range(promql, seconds, step)
        for text in ("=1+1", "+1", "-1", "@SUM(1)", "\t=1", "\r=1", "=HYPERLINK(\"http://evil\")"):
            Interface.objects.filter(pk=self.i1.pk).update(alias=text)
            r = self.csv(rng)
            rows = list(csv.reader(io.StringIO(r.content.decode())))
            for row in rows[1:]:
                for cell in row:
                    self.assertFalse(cell[:1] in ("=", "+", "@", "\t", "\r"), (text, cell))
                    if cell.startswith("-"):
                        float(cell)       # only a real negative number may start with a minus
            cache.clear()

    def test_filename_cannot_inject_headers(self):
        Device.objects.filter(pk=self.d.pk).update(name='a"; filename*=UTF-8\'\'evil.exe\r\nX-Evil: 1')
        Interface.objects.filter(pk=self.i1.pk).update(name="../../etc/passwd\n")
        r = self.csv()
        self.assertEqual(r.status_code, 200)
        self.assertRegex(r["Content-Disposition"], r'^attachment; filename="[A-Za-z0-9._-]+\.csv"$')
        self.assertNotIn("X-Evil", r)


class DuplicateNamesTests(PortCase):
    def test_two_ports_with_the_same_name_keep_their_own_series_and_pages(self):
        Interface.objects.filter(pk=self.i3.pk).update(name="Gi1/0/1")
        for idx in (1, 3):
            r = self.get(url=reverse("network:interface", args=[self.d.pk, idx]), instant=fake_instant)
            self.assertEqual(r.status_code, 200)
            for q in self.all_promql():
                if "ifIndex" in q:
                    self.assertIn(f'ifIndex="{idx}"', q)
                    self.assertNotIn(f'ifIndex="{4 - idx}"', q)
        picker = self.get().content.decode()
        self.assertEqual(picker.count('<option value="1"'), 1)
        self.assertEqual(picker.count('<option value="3"'), 1)


class HugeIfIndexCollectionTests(PortCase):
    def test_label_ifindex_beyond_the_column_is_ignored_not_fatal(self):
        base = {"instance": "sw1", "job": "snmp"}

        def q(promql):
            if "ifHCInOctets" in promql:
                return [prom_row(10.0, ifIndex="1", ifName="Gi1/0/1", **base),
                        prom_row(10.0, ifIndex="4294967296", ifName="Huge", **base),
                        prom_row(10.0, ifIndex="9" * 5000, ifName="Absurd", **base),
                        prom_row(10.0, ifIndex="2147483648", ifName="Edge", **base)]
            if "ifMtu" in promql:
                return [prom_row(1500, ifIndex="4294967296", **base), prom_row(1500, ifIndex="1", **base)]
            return []
        with mock.patch(INSTANT, side_effect=q):
            jobs.collect_metrics()
        self.assertEqual(sorted(Interface.objects.filter(device=self.d).values_list("if_index", flat=True)), [1])
        self.assertEqual(Interface.objects.get(pk=self.i1.pk).mtu, 1500)


class RawApiShapeTests(PortCase):
    """Odd bodies from core.prometheus._get (a proxy or a broken Prometheus), below the query() helpers."""

    def test_unexpected_api_bodies_are_a_banner_not_a_500(self):
        for body in ([], "x", None, {"result": "abc"}, {"result": [1, 2]}, {"result": [{"metric": {}, "value": 5}]},
                     {"result": [{"metric": {}, "value": [1]}]}, {"resultType": "vector"}, {"result": [{"value": [1, {"a": 1}]}]},
                     {"result": [{"metric": {}, "values": [[1, 2, 3], "x", None]}]}):
            cache.clear()
            with mock.patch("core.prometheus._get", return_value=body):
                r = self.c.get(self.url)
                live = self.c.get(reverse("network:interface_live", args=[self.d.pk, 1]))
            self.assertEqual(r.status_code, 200, body)
            self.assertEqual(live.status_code, 200, body)
            strict(live)
            self.c.get(reverse("network:interface_export", args=[self.d.pk, 1]))
            cache.delete(f"nwport:rl:csv:{self.viewer.pk}")


class DuplicateCableNameTests(PortCase):
    def test_a_cable_documented_by_a_name_two_ports_share_belongs_to_neither(self):
        from architecture.models import Link
        far = dev("far-rtr", mgmt_ip="192.0.2.95")
        Interface.objects.filter(pk=self.i3.pk).update(name="Gi1/0/1")
        Link.objects.create(tenant_key="default", a_device=self.d, a_port="Gi1/0/1", b_device=far, b_port="Eth1")
        for idx in (1, 3):
            r = self.get(url=reverse("network:interface", args=[self.d.pk, idx]))
            self.assertNotContains(r, "far-rtr")
        r = self.get(url=reverse("network:detail", args=[self.d.pk]) + "?tab=neighbours")
        self.assertContains(r, "far-rtr")
        self.assertNotContains(r, reverse("network:interface", args=[self.d.pk, 3]) + '"')
        Interface.objects.filter(pk=self.i3.pk).update(name="Gi1/0/3")
        self.assertContains(self.get(), "far-rtr")


class LegendTests(PortCase):
    def test_absurd_readings_keep_the_legend_short(self):
        self.assertLess(len(portmetrics.fmt_value(1e300, "Mbps")), 20)
        self.assertEqual(portmetrics.fmt_value(float("inf"), "Mbps"), "–")
        self.assertEqual(portmetrics.fmt_value(1500, "Mbps"), "1,500 Mbps")

    def test_clean_removes_bidi_overrides_but_keeps_indic_joiners(self):
        self.assertEqual(portmetrics.clean("a‮b​c﻿d"), "a b c d")
        self.assertEqual(portmetrics.clean("क्‍ष"), "क्‍ष")


class HostileFingerprintTests(PortCase):
    def test_odd_event_fingerprints_never_break_the_alerts_tab_or_link_anywhere(self):
        from core.models import Event
        for fp in ("network:" + "9" * 5000 + ":if1", f"network:{self.d.pk}:if" + "9" * 5000, "network:١:if١", f"network:{self.d.pk}:if1\n",
                   f"network:{self.d.pk}:if-1", f"x network:{self.d.pk}:if1", ""):
            Event.objects.create(tenant_key="default", source="network", title="odd", severity="warning",
                                 object_type="network.device", object_id=str(self.d.pk), fingerprint=fp[:190])
        with mock.patch(RANGE, return_value=[]):
            r = self.c.get(reverse("network:detail", args=[self.d.pk]), {"tab": "events"})
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, "/interfaces/")
