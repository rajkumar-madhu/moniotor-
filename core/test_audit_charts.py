"""Regression tests for the audit source IP (X-Forwarded-For trust) and the chart tooltip (no HTML sink)."""
import json
import shutil
import subprocess
from pathlib import Path
from unittest import mock, skipUnless

from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from core.audit import audit
from core.models import AuditEvent


class AuditSourceIpTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("a")

    def _ip(self, **meta):
        req = RequestFactory().get("/", **meta)
        req.user = self.user
        audit(req, "update", object_type="thing")
        return AuditEvent.objects.latest("pk").source_ip

    @override_settings(TRUSTED_PROXIES=[])
    def test_client_supplied_forwarded_for_is_ignored_without_a_trusted_proxy(self):
        self.assertEqual(self._ip(REMOTE_ADDR="10.0.0.5", HTTP_X_FORWARDED_FOR="1.2.3.4, 10.0.0.5"), "10.0.0.5")
        self.assertEqual(self._ip(REMOTE_ADDR="10.0.0.5", HTTP_X_FORWARDED_FOR="not-an-ip"), "10.0.0.5")

    @override_settings(TRUSTED_PROXIES=["10.0.0.5"])
    def test_trusted_proxy_uses_right_most_untrusted_hop(self):
        # nginx's $proxy_add_x_forwarded_for appends the real peer to whatever the client sent
        self.assertEqual(self._ip(REMOTE_ADDR="10.0.0.5", HTTP_X_FORWARDED_FOR="1.2.3.4, 203.0.113.7"), "203.0.113.7")
        self.assertEqual(self._ip(REMOTE_ADDR="10.0.0.5", HTTP_X_FORWARDED_FOR="203.0.113.7"), "203.0.113.7")
        # a peer that is not a trusted proxy cannot forge the header
        self.assertEqual(self._ip(REMOTE_ADDR="198.51.100.9", HTTP_X_FORWARDED_FOR="1.2.3.4"), "198.51.100.9")

    @override_settings(TRUSTED_PROXIES=["10.0.0.0/8", "::1"])
    def test_trusted_cidrs_are_skipped_and_invalid_values_store_none(self):
        self.assertEqual(self._ip(REMOTE_ADDR="10.0.0.5", HTTP_X_FORWARDED_FOR="203.0.113.7, 10.1.2.3"), "203.0.113.7")
        # every hop trusted: the left-most one is the best we know
        self.assertEqual(self._ip(REMOTE_ADDR="10.0.0.5", HTTP_X_FORWARDED_FOR="10.9.9.9"), "10.9.9.9")
        self.assertIsNone(self._ip(REMOTE_ADDR="10.0.0.5", HTTP_X_FORWARDED_FOR="not-an-ip; DROP"))
        self.assertIsNone(self._ip(REMOTE_ADDR="10.0.0.5", HTTP_X_FORWARDED_FOR="x" * 80))
        self.assertEqual(self._ip(REMOTE_ADDR="10.0.0.5", HTTP_X_FORWARDED_FOR=""), "10.0.0.5")

    @override_settings(TRUSTED_PROXIES=["10.0.0.0/8"])
    def test_ipv6_zone_ids_are_dropped_and_mapped_ipv4_peers_match(self):
        # a zone id is free text: "::1%aaaa…" passes ip_address() but not PostgreSQL inet or MySQL char(39)
        self.assertEqual(self._ip(REMOTE_ADDR="10.0.0.5", HTTP_X_FORWARDED_FOR="::1%" + "a" * 100 + ", 10.1.1.1"), "::1")
        self.assertEqual(self._ip(REMOTE_ADDR="fe80::1%" + "e" * 60), "fe80::1")
        # a dual-stack socket reports the IPv4 proxy as ::ffff:10.0.0.5
        self.assertEqual(self._ip(REMOTE_ADDR="::ffff:10.0.0.5", HTTP_X_FORWARDED_FOR="203.0.113.7"), "203.0.113.7")
        self.assertEqual(self._ip(REMOTE_ADDR="::ffff:198.51.100.9", HTTP_X_FORWARDED_FOR="1.2.3.4"), "198.51.100.9")

    def test_invalid_remote_addr_stores_none(self):
        self.assertIsNone(self._ip(REMOTE_ADDR="garbage"))
        req = mock.Mock(user=self.user, META={})
        audit(req, "update", object_type="thing")
        self.assertIsNone(AuditEvent.objects.latest("pk").source_ip)

    def test_failed_login_records_peer_not_forged_header(self):
        self.client.post(reverse("login"), {"username": "a", "password": "wrong"},
                         REMOTE_ADDR="192.0.2.10", HTTP_X_FORWARDED_FOR="8.8.8.8")
        self.assertEqual(AuditEvent.objects.get(action="login_failed").source_ip, "192.0.2.10")


CHARTS_JS = Path(__file__).resolve().parent / "static" / "linkedeye" / "charts.js"

# A tiny DOM stand-in, just enough for charts.js: records every innerHTML assignment so the test can tell
# whether data ever reaches an HTML parser. It is deliberately this thin — no getAttribute, no classList, no
# closest, no getComputedStyle / matchMedia / ResizeObserver / sessionStorage / CustomEvent — so charts.js has to
# treat every one of those as optional. charts.js v2 drives hover from pointer events (they also cover touch), so
# the harness dispatches `pointermove`.
_DOM = r"""
const fs = require('fs'), vm = require('vm');
const htmlSinks = [];
class N {
  constructor(tag) { this.tagName = tag; this.children = []; this.attrs = {}; this.listeners = {}; this.style = {};
    this.dataset = {}; this._text = ''; this.className = ''; this.hidden = false; this.clientWidth = 600; this.clientHeight = 200; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  appendChild(c) { this.children.push(c); return c; }
  append(...cs) { cs.forEach(c => this.appendChild(typeof c === 'string' ? text(c) : c)); }
  replaceChildren(...cs) { this.children = []; this._text = ''; this.append(...cs); }
  set textContent(v) { this.children = []; this._text = String(v); }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
  set innerHTML(v) { htmlSinks.push(String(v)); this.children = []; this._text = String(v); }
  querySelector(sel) { return sel === '.tip' ? (this.children.find(c => c.className === 'tip') || null) : null; }
  addEventListener(t, f) { this.listeners[t] = f; }
  getBoundingClientRect() { return { left: 0, top: 0, width: 600, height: 200 }; }
  walk(f) { f(this); this.children.forEach(c => c.walk(f)); }
}
function text(s) { const n = new N('#text'); n._text = String(s); return n; }
const host = new N('div'); host.className = 'chart'; host.dataset = { chart: process.argv[3] || 'line', src: 'c' };
const src = new N('script'); src._text = process.argv[2];
globalThis.window = globalThis;
globalThis.document = {
  readyState: 'complete', createElementNS: (ns, t) => new N(t), createElement: t => new N(t), createTextNode: text,
  getElementById: id => (id === 'c' ? src : null), querySelectorAll: () => [host],
};
vm.runInThisContext(fs.readFileSync(process.argv[1], 'utf8'));
"""

HARNESS = _DOM + r"""
const svg = host.children[0];
svg.listeners.pointermove({ clientX: 300, clientY: 50 });
const tip = host.querySelector('.tip');
const tags = [], values = [], names = [];
tip.walk(n => { tags.push(n.tagName); if (n.className === 'v') values.push(n.textContent); if (n.className === 'n') names.push(n.textContent); });
console.log(JSON.stringify({ htmlSinks, tipText: tip.textContent, tags, values, names, hidden: tip.hidden }));
"""

# Reports every attribute of every node the renderer produced, so a test can assert that no coordinate came out
# NaN (an attribute of "MNaN,30" makes the browser drop the whole path and log an SVG error).
GEOMETRY = _DOM + r"""
const nodes = [];
host.walk(n => { if (n !== host) nodes.push({ tag: n.tagName, attrs: n.attrs, text: n.children.length ? '' : n.textContent }); });
console.log(JSON.stringify({ htmlSinks, nodes }));
"""


@skipUnless(shutil.which("node"), "node is needed to run charts.js")
class ChartGeometryTests(TestCase):
    """Degenerate series must not put NaN into an SVG attribute.

    telemetry.hosts.cpu_sparklines hands the infrastructure table whatever finite samples a host had in the last
    hour, which for a host that just came up (or whose scrapes mostly fail) is one sample or none."""

    def _render(self, data, kind):
        out = subprocess.run(["node", "-e", GEOMETRY, str(CHARTS_JS), json.dumps(data), kind],
                             capture_output=True, text=True, timeout=30, check=True)
        return json.loads(out.stdout)

    def _nans(self, res):
        return [(n["tag"], k, v) for n in res["nodes"] for k, v in n["attrs"].items()
                if "NaN" in v or "Infinity" in v or "undefined" in v]

    def test_sparkline_survives_one_point_no_points_and_gaps(self):
        for values in ([7.5], [], [None, None], [1, None, 3], [0, 0, 0], [1.5, 2.5, 3.5]):
            with self.subTest(values=values):
                res = self._render({"values": values, "color": "#1565f0"}, "spark")
                self.assertFalse(self._nans(res), (values, res["nodes"]))
                self.assertFalse(res["htmlSinks"])
        # one finite sample is still shown (as a dot), and a null is a gap rather than a plotted zero
        self.assertEqual([n["tag"] for n in self._render({"values": [7.5]}, "spark")["nodes"]], ["svg", "circle"])
        gapped = self._render({"values": [1, 2, None, 4, 5]}, "spark")["nodes"]
        paths = [n["attrs"]["d"] for n in gapped if n["tag"] == "path"]
        self.assertEqual(len(paths), 1)
        self.assertEqual(paths[0].count("M"), 2, paths)          # the gap breaks the line in two
        # points with a gap on both sides are drawn as dots: a path of two moveTo commands would draw nothing
        lonely = self._render({"values": [1, None, 3]}, "spark")["nodes"]
        self.assertEqual([n["tag"] for n in lonely], ["svg", "circle", "circle"])
        self.assertNotIn("path", [n["tag"] for n in self._render({"values": []}, "spark")["nodes"]])

    def test_axes_chart_survives_a_single_point_and_an_all_gap_series(self):
        for series in ([{"name": "a", "color": "#111", "values": [1]}],
                       [{"name": "a", "color": "#111", "values": [None, None, None]}],
                       [{"name": "a", "color": "#111", "values": [0, 0]}]):
            with self.subTest(series=series):
                n = len(series[0]["values"])
                res = self._render({"labels": ["x"] * n, "series": series}, "line")
                self.assertFalse(self._nans(res), res["nodes"])

    def test_gauge_survives_a_value_over_max_and_a_non_numeric_value(self):
        for d in ({"value": 250, "max": 100, "unit": "%"}, {"value": None, "max": 100},
                  {"value": "x", "max": 0}, {"value": 42, "max": 100, "thresholds": []}):
            with self.subTest(d=d):
                self.assertFalse(self._nans(self._render(d, "gauge")), d)

    def test_axes_chart_with_every_series_hidden_keeps_a_readable_axis(self):
        """Hiding the last visible series (legend click) must not collapse the y axis to 0.00 on every tick."""
        for kind in ("line", "bars"):
            with self.subTest(kind=kind):
                data = {"labels": ["a", "b", "c"], "series": [{"name": "s", "color": "#111", "values": [5, 7, 9], "hidden": True}]}
                res = self._render(data, kind)
                self.assertFalse(self._nans(res), res["nodes"])
                ticks = [n["text"] for n in res["nodes"] if n["tag"] == "text" and n["attrs"].get("text-anchor") == "end"]
                self.assertEqual(ticks, ["0", "0.25", "0.50", "0.75", "1"])
                self.assertFalse([n for n in res["nodes"] if n["attrs"].get("stroke") == "#111"])   # the hidden series draws nothing


@skipUnless(shutil.which("node"), "node is needed to run charts.js")
class ChartTooltipTests(TestCase):
    PAYLOAD = "/<img src=x onerror=window.__xss=1>"

    def _hover(self, data):
        out = subprocess.run(["node", "-e", HARNESS, str(CHARTS_JS), json.dumps(data)],
                             capture_output=True, text=True, timeout=30, check=True)
        return json.loads(out.stdout)

    def test_tooltip_shows_series_names_and_labels_as_text(self):
        res = self._hover({
            "labels": ["a", "<b onmouseover=x>lbl", "c"],
            "series": [{"name": 'http_requests_total{path="%s"}' % self.PAYLOAD, "color": "#1565f0",
                        "values": [1, 2, 3]}],
        })
        # nothing from the data may pass through an HTML parser
        self.assertFalse([s for s in res["htmlSinks"] if "<img" in s or "<b onmouseover" in s], res["htmlSinks"])
        self.assertNotIn("img", res["tags"])
        self.assertFalse(res["hidden"])
        self.assertIn(self.PAYLOAD, res["tipText"])          # shown literally
        self.assertIn("<b onmouseover=x>lbl", res["tipText"])
        # the hovered point's value and the series name are text nodes of the tooltip rows
        self.assertEqual(res["values"], ["2"])
        self.assertEqual(res["names"], ['http_requests_total{path="%s"}' % self.PAYLOAD])
