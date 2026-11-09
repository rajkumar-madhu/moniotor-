"""Fleet Overview tests (telemetry:overview). Prometheus is always mocked; nothing here touches the network."""
import re
import threading
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import resolve, reverse

from core import prometheus
from core.models import Membership, Site

from . import overview as ov
from . import tests_fleet as tf

PROM = tf.PROM
STATIC = Path(__file__).resolve().parent / "static" / "telemetry"


class OverviewProm:
    """Records every Prometheus request of a test and answers them: the fleet's host queries come from
    tests_fleet, the probe lists ``present`` metrics, range queries return one tidy series per row asked for."""

    def __init__(self, present=None, fail=False):
        self.present = set(ov.ALL_METRICS if present is None else present)
        self.fail = fail
        self.lock = threading.Lock()
        self.range_queries, self.instant_queries = [], []

    def query(self, q, at=None):
        with self.lock:
            self.instant_queries.append(q)
        if self.fail:
            raise prometheus.PrometheusError("prom exploded")
        if q == ov.PROBE_QUERY:
            return [{"metric": {"__name__": n}, "value": 3.0} for n in sorted(self.present)]
        return tf.fake_query(q, at)

    def query_range(self, q, seconds=3600, step=None):
        with self.lock:
            self.range_queries.append(q)
        if self.fail:
            raise prometheus.PrometheusError("prom exploded")
        t0 = 1_700_000_000
        pts = lambda base: [(t0 + i * 15, base + i) for i in range(8)]  # noqa: E731
        if "by (mode)" in q:
            return [{"metric": {"mode": m}, "values": pts(2.0 + i)} for i, m in enumerate(ov.CPU_MODES)]
        if "by (__name__)" in q:
            names = re.findall(r"node_[A-Za-z_0-9]+", q)
            if "node_memory_(Cached|Buffers|MemFree)_bytes" in q:
                names = ["node_memory_Cached_bytes", "node_memory_Buffers_bytes", "node_memory_MemFree_bytes"]
            return [{"metric": {"__name__": n}, "values": pts(10.0 * (i + 1))} for i, n in enumerate(dict.fromkeys(names))][:4]
        if "by (instance)" in q:
            insts = re.findall(r'instance="([^"]+)"', q)
            m = re.search(r'instance=~"((?:[^"\\]|\\.)*)"', q)
            if m:
                insts = [re.sub(r"\\\\(.)", r"\1", p) for p in re.split(r"(?<!\\)\|", m.group(1))]
            if not insts:
                insts = list(tf.HOSTS)
            return [{"metric": {"instance": i}, "values": pts(5.0 + k)} for k, i in enumerate(insts)]
        return [{"metric": {}, "values": pts(20.0)}]

    def patches(self):
        return [mock.patch("core.prometheus.query", side_effect=self.query),
                mock.patch("core.prometheus.query_range", side_effect=self.query_range),
                mock.patch("core.prometheus.targets", side_effect=tf.fake_targets)]


class OverviewCase(TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.url = reverse("telemetry:overview")

    def start(self, **kw):
        self.prom = OverviewProm(**kw)
        for p in self.prom.patches():
            p.start()
            self.addCleanup(p.stop)
        return self.prom

    def login_admin(self):
        self.client = Client()
        self.client.force_login(User.objects.create_superuser("root", "root@example.com", "pw"))


# ============================================================================= parameters and PromQL building
class ParamsTests(SimpleTestCase):
    def test_range_allow_list_and_default(self):
        from django.http import QueryDict
        for key, secs in (("15m", 900), ("1h", 3600), ("6h", 21600), ("24h", 86400), ("7d", 604800)):
            self.assertEqual(ov.parse_range(QueryDict(f"range={key}"))[::2], (key, secs))
        for junk in ("", "5y", "1h; drop", "custom"):
            self.assertEqual(ov.parse_range(QueryDict("range=" + junk))[0], "15m")

    def test_custom_range_is_bounded(self):
        from django.http import QueryDict
        key, label, secs = ov.parse_range(QueryDict("range=custom&last=90&unit=m"))
        self.assertEqual((key, secs, label), ("custom", 5400, "Last 90 minutes"))
        self.assertEqual(ov.parse_range(QueryDict("range=custom&last=1&unit=m"))[2], ov.CUSTOM_MIN)
        self.assertEqual(ov.parse_range(QueryDict("range=custom&last=999999&unit=d"))[2], ov.CUSTOM_MAX)
        for bad in ("-5", "1e9", "ab", "9" * 12, "0", "²"):
            self.assertEqual(ov.parse_range(QueryDict(f"range=custom&last={bad}&unit=h"))[0], "15m", bad)

    def test_params_ignore_unknown_values(self):
        from django.http import QueryDict
        p = ov.Params(QueryDict("by=drop&compare=yes&live=7&nodes=a&nodes=a&nodes=b"))
        self.assertEqual((p.by, p.compare, p.live, p.nodes), ("", False, 0, ["a", "b"]))
        p = ov.Params(QueryDict("by=site&compare=1&live=15"))
        self.assertEqual((p.by, p.compare, p.live), ("site", True, 15))


class MatcherTests(SimpleTestCase):
    def test_single_host_uses_host_selector(self):
        self.assertEqual(ov.matcher_for(["web-01:9100"]), 'instance="web-01:9100"')

    def test_regex_is_escaped_and_quoted(self):
        m = ov.matcher_for(['a.b:9100', 'c"}|.*:1', "x\\y"])
        self.assertTrue(m.startswith('instance=~"'))
        self.assertEqual(m.count('"'), 2 + m.count('\\"'))      # one quoted literal, quotes inside are escaped
        self.assertNotIn('"}', m.replace('\\"}', ""))
        self.assertIn("a\\\\.b\\\\:9100", m)                       # '.' and ':' are literal in the regex, once PromQL unescapes

    def test_whole_fleet_uses_the_node_job(self):
        self.assertEqual(ov.matcher_for(["a", "b"], whole_fleet=True), 'job=~".*node.*"')

    def test_all_specs_only_contain_the_matcher_and_integers(self):
        sel = 'instance=~"a|b"'
        for off in ("", " offset 900s"):
            for c in ov.chart_specs(sel, "300s", off):
                for q in [g["q"] for g in c["dims"]] + [c["node"], c["total"]]:
                    self.assertTrue(q is None or sel in q, q)
                    self.assertEqual(q.count("offset") if q else 0, q.count("offset 900s") if q else 0)


# ============================================================================= the page
@override_settings(PROMETHEUS_URL=PROM)
class OverviewPageTests(OverviewCase):
    def setUp(self):
        super().setUp()
        self.login_admin()
        tf.ci("Database", "db-01:9100", site="Mumbai", environment="prod")
        tf.ci("Web", "web-01:9100", site="Chennai", environment="uat")

    def test_route_and_nav_name(self):
        self.assertEqual(self.url, "/monitoring/overview/")
        self.assertEqual(resolve(self.url).url_name, "overview")

    def test_anonymous_is_sent_to_login(self):
        r = Client().get(self.url)
        self.assertEqual(r.status_code, 302)
        self.assertIn("login", r["Location"])

    def test_renders_sections_charts_and_controls(self):
        self.start()
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 200)
        for text in ("Overview", "CPU utilisation", "Memory usage", "Disk throughput", "Network traffic",
                     "Uptime distribution", "Time range", "Group by", "Compare to previous period", "Nodes: all",
                     "Alerts &amp; nodes", "ov-drawer", "telemetry/overview.js"):
            self.assertContains(r, text)
        self.assertEqual(r.context["params"].range_key, "15m")
        self.assertEqual([s["id"] for s in r.context["sections"]], ["compute", "memory", "storage", "network", "system"])
        self.assertEqual(r.context["shown_total"], 15)

    def test_range_selector_drives_step_and_window(self):
        prom = self.start()
        r = self.client.get(self.url, {"range": "24h"})
        self.assertEqual(r.context["page"]["charts"]["step"], 720)
        self.assertTrue(any("[720s]" in q for q in prom.range_queries))      # the rate window follows the step
        cache.clear()
        prom.range_queries.clear()
        r = self.client.get(self.url, {"range": "1h"})
        self.assertEqual(r.context["page"]["charts"]["step"], 30)
        self.assertTrue(all("[720s]" not in q for q in prom.range_queries))
        self.assertTrue(any("[300s]" in q for q in prom.range_queries))

    def test_custom_range_label(self):
        self.start()
        r = self.client.get(self.url, {"range": "custom", "last": "90", "unit": "m"})
        self.assertContains(r, "Last 90 minutes")

    def test_not_reported_metrics_are_listed_not_drawn(self):
        self.start(present={"node_load1", "node_cpu_seconds_total"})
        r = self.client.get(self.url)
        self.assertEqual(r.context["shown_total"], 2)
        self.assertContains(r, "not reported by any node")
        self.assertContains(r, "node_hwmon_temp_celsius")
        self.assertNotContains(r, 'data-src="ov-disk_bw"')

    def test_no_metrics_at_all_says_so(self):
        self.start(present=set())
        r = self.client.get(self.url)
        self.assertContains(r, "No node_exporter metrics for this selection")

    def test_prometheus_unconfigured(self):
        with override_settings(PROMETHEUS_URL=""):
            r = self.client.get(self.url)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Prometheus not connected")
        self.assertFalse(r.context["charts_ready"])
        self.assertNotContains(r, "hm-kpi-gauge")          # no empty gauges or zeroed network figures without Prometheus
        self.assertNotContains(r, "Avg root disk")

    def test_prometheus_down_shows_a_banner_not_a_500(self):
        self.start(fail=True)
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "prom exploded")
        self.assertFalse(r.context["charts_ready"])

    def test_one_failing_query_only_blanks_its_chart(self):
        prom = self.start()
        real = prom.query_range

        def flaky(q, seconds=3600, step=None):
            if "node_filefd_allocated" in q:
                raise prometheus.PrometheusError("filefd boom")
            return real(q, seconds, step)
        with mock.patch("core.prometheus.query_range", side_effect=flaky):
            r = self.client.get(self.url)
        self.assertContains(r, "filefd boom")
        self.assertEqual(r.context["shown_total"], 14)

    def test_series_are_capped_with_an_other_series(self):
        self.start()
        extra = {f"bulk-{i:02d}:9100": (1, 10.0, 20.0, 30.0, 0.5, 4) for i in range(25)}
        with mock.patch.dict(tf.HOSTS, extra):
            r = self.client.get(self.url, {"by": "node"})
        cpu = next(c for s in r.context["sections"] for c in s["charts"] if c["key"] == "cpu")
        names = [s["name"] for s in cpu["data"]["series"]]
        self.assertEqual(len(names), ov.MAX_SERIES + 1)
        self.assertTrue(names[-1].startswith("Other ("))
        self.assertTrue(cpu["truncated"])
        self.assertContains(r, "biggest")

    def test_per_node_ratio_charts_are_not_stacked_but_additive_ones_are(self):
        self.start()
        r = self.client.get(self.url, {"by": "node"})
        charts = {c["key"]: c for s in r.context["sections"] for c in s["charts"]}
        self.assertEqual((charts["cpu"]["kind"], charts["cpu"]["stacked"]), ("line", False))    # averages do not add up
        self.assertEqual((charts["mem"]["kind"], charts["mem"]["stacked"]), ("bars", True))     # bytes do
        r = self.client.get(self.url)
        charts = {c["key"]: c for s in r.context["sections"] for c in s["charts"]}
        self.assertEqual((charts["cpu"]["kind"], charts["cpu"]["stacked"]), ("bars", True))     # modes do

    def test_group_by_site_uses_site_names(self):
        self.start()
        r = self.client.get(self.url, {"by": "site"})
        cpu = next(c for s in r.context["sections"] for c in s["charts"] if c["key"] == "cpu")
        names = {s["name"] for s in cpu["data"]["series"]}
        self.assertTrue({"Mumbai", "Chennai"} <= names, names)

    def test_compare_adds_a_previous_period_series_and_one_query_per_chart(self):
        prom = self.start()
        self.client.get(self.url)
        base = len(prom.range_queries)
        cache.clear()
        prom.range_queries.clear()
        r = self.client.get(self.url, {"compare": "1", "range": "1h"})
        cpu = next(c for s in r.context["sections"] for c in s["charts"] if c["key"] == "cpu")
        self.assertIn(ov.PREVIOUS, [s["name"] for s in cpu["data"]["series"]])
        self.assertEqual(cpu["kind"], "line")                      # a stacked chart cannot carry the dashed overlay
        self.assertEqual(sum("offset 3600s" in q for q in prom.range_queries), 15)
        self.assertEqual(len(prom.range_queries), base + 15)

    def test_node_selection_builds_the_matcher_from_the_chosen_hosts(self):
        prom = self.start()
        r = self.client.get(self.url, {"nodes": ["db-01:9100", "web-01:9100"]})
        self.assertEqual(r.context["sel"].hosts[0]["instance"], "db-01:9100")
        self.assertEqual(len(r.context["sel"].hosts), 2)
        joined = " ".join(prom.range_queries)
        self.assertIn('instance=~"db\\\\-01\\\\:9100|web\\\\-01\\\\:9100"', joined)
        self.assertNotIn("app-02", joined)

    def test_unknown_or_hostile_node_ids_are_dropped_never_queried(self):
        prom = self.start()
        evil = 'x"} or vector(1) # '
        r = self.client.get(self.url, {"nodes": [evil, "db-01:9100"]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["sel"].ignored, [evil])
        self.assertNotIn("vector(1)", " ".join(prom.range_queries + prom.instant_queries))
        self.assertContains(r, "left out")

    def test_json_poll(self):
        self.start()
        r = self.client.get(reverse("telemetry:overview_json"), {"range": "15m"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Cache-Control"], "no-store")
        j = r.json()
        self.assertIn("ov-cpu", j["charts"])
        self.assertIn("ov-g-cpu", j["charts"])
        self.assertEqual(j["legends"]["ov-cpu"][0]["name"], "User")
        self.assertEqual(j["counts"]["live"], 2)

    def test_json_poll_with_prometheus_down(self):
        self.start(fail=True)
        j = self.client.get(reverse("telemetry:overview_json")).json()
        self.assertIn("prom exploded", j["error"])
        self.assertNotIn("ov-cpu", j["charts"])

    def test_drawer_lists_alerts(self):
        self.start()
        tf.alert(name="DiskFull", instance="db-01:9100", severity="critical")
        r = self.client.get(self.url)
        self.assertContains(r, "DiskFull")
        self.assertEqual(r.context["alert_split"]["critical"], 1)


# ============================================================================= call budget
@override_settings(PROMETHEUS_URL=PROM)
class OverviewBudgetTests(OverviewCase):
    def setUp(self):
        super().setUp()
        self.login_admin()

    def run_view(self, hosts, **params):
        cache.clear()
        prom = OverviewProm()
        for p in prom.patches():
            p.start()
            self.addCleanup(p.stop)
        with mock.patch.dict(tf.HOSTS, {f"n-{i:03d}:9100": (1, 10.0, 20.0, 30.0, 0.5, 4) for i in range(hosts)}):
            r = self.client.get(self.url, params)
        self.assertEqual(r.status_code, 200)
        return len(prom.range_queries) + sum(1 for q in prom.instant_queries if q == ov.PROBE_QUERY)

    def test_query_count_is_independent_of_the_number_of_hosts(self):
        for params in ({}, {"by": "node"}, {"by": "site"}, {"compare": "1"}):
            small = self.run_view(3, **params)
            large = self.run_view(60, **params)
            self.assertEqual(small, large, params)
            self.assertLessEqual(large, ov.MAX_QUERIES, params)

    def test_documented_budget_figures(self):
        self.assertEqual(self.run_view(3), 22 + 1)                      # dimension view: 22 range queries + the probe
        self.assertEqual(self.run_view(3, by="node"), 15 + 1)
        self.assertEqual(self.run_view(3, compare="1"), 22 + 15 + 1)

    def test_second_view_is_served_from_the_cache(self):
        prom = OverviewProm()
        for p in prom.patches():
            p.start()
            self.addCleanup(p.stop)
        self.client.get(self.url)
        n = len(prom.range_queries)
        self.client.get(self.url)
        self.assertEqual(len(prom.range_queries), n)

    def test_the_budget_guard_trips_instead_of_overrunning(self):
        with mock.patch.object(ov, "MAX_QUERIES", 5), self.assertRaises(RuntimeError):
            ov.build_charts('instance="a"', 900, "", False, set(ov.ALL_METRICS), [])


# ============================================================================= site scoping
@override_settings(PROMETHEUS_URL=PROM)
class OverviewScopingTests(OverviewCase):
    def setUp(self):
        super().setUp()
        mumbai = Site.objects.create(name="Mumbai", code="mumbai")
        Site.objects.create(name="Chennai", code="chennai")
        self.user = User.objects.create_user("single", password="pw")
        Membership.objects.create(user=self.user, site=mumbai, role="viewer")
        tf.ci("Database", "db-01:9100", site="Mumbai")
        tf.ci("Web", "web-01:9100", site="Chennai")
        tf.alert(name="Mine", instance="db-01:9100", severity="critical", site="Mumbai")
        tf.alert(name="Theirs", instance="web-01:9100", severity="critical", site="Chennai")
        self.client = Client()
        self.client.force_login(self.user)

    def test_site_limited_user_never_queries_or_sees_another_sites_hosts(self):
        prom = self.start()
        r = self.client.get(self.url)
        joined = " ".join(prom.range_queries + prom.instant_queries[1:])
        selected = {h["instance"] for h in r.context["sel"].hosts}
        self.assertIn("db-01:9100", selected)
        self.assertNotIn("web-01:9100", selected)
        self.assertNotContains(r, "web-01:9100")
        self.assertNotContains(r, "Theirs")
        self.assertContains(r, "Mine")
        chart_q = " ".join(prom.range_queries)
        self.assertNotIn("web\\\\-01", chart_q)
        self.assertIn("instance=~", chart_q)                       # a limited user never gets the whole-fleet job matcher
        self.assertNotIn('job=~".*node.*"', chart_q)
        self.assertTrue(joined)

    def test_node_and_site_parameters_cannot_reach_another_site(self):
        prom = self.start()
        r = self.client.get(self.url, {"nodes": "web-01:9100", "site": "Chennai"})
        self.assertEqual(r.context["sel"].hosts, [])
        self.assertEqual(prom.range_queries, [])
        self.assertContains(r, "No nodes match the filters")
        self.assertNotContains(r, "web-01:9100")

    def test_json_and_csv_are_scoped(self):
        prom = self.start()
        self.client.get(reverse("telemetry:overview_json"), {"nodes": "web-01:9100"})
        self.assertEqual(prom.range_queries, [])
        r = self.client.get(self.url, {"by": "node", "format": "csv", "chart": "cpu"})
        self.assertNotIn("Web", r.content.decode())

    def test_superuser_whole_fleet_uses_the_job_matcher(self):
        prom = self.start()
        c = Client()
        c.force_login(User.objects.create_superuser("root2", "r2@example.com", "pw"))
        c.get(self.url)
        self.assertTrue(any('job=~".*node.*"' in q for q in prom.range_queries))


# ============================================================================= CSV
@override_settings(PROMETHEUS_URL=PROM)
class OverviewCsvTests(OverviewCase):
    def setUp(self):
        super().setUp()
        self.login_admin()

    def test_csv_of_one_chart(self):
        self.start()
        r = self.client.get(self.url, {"format": "csv", "chart": "cpu"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn('filename="fleet-overview-cpu.csv"', r["Content-Disposition"])
        lines = r.content.decode().strip().splitlines()
        self.assertEqual(lines[0].split(",")[:3], ["time_utc", "User [%]", "System [%]"])
        self.assertEqual(len(lines), 1 + 8)
        self.assertRegex(lines[1], r"^2023-11-14T22:13:20Z,")

    def test_csv_is_bounded(self):
        data = {"ts": list(range(5000)), "series": [{"name": "a", "values": [1] * 5000}]}
        header, rows = ov.csv_rows({"data": data})
        self.assertEqual(len(rows), ov.CSV_MAX_ROWS)

    def test_unknown_chart_is_404(self):
        self.start()
        self.assertEqual(self.client.get(self.url, {"format": "csv", "chart": "nope"}).status_code, 404)
        self.assertEqual(self.client.get(self.url, {"format": "csv", "chart": "../../etc"}).status_code, 404)

    def test_series_names_cannot_inject_formulas(self):
        prom = self.start()
        extra = {"=HYPERLINK(1)|:9100": (1, 10.0, 20.0, 30.0, 0.5, 4)}
        tf.ci("=cmd|' /c calc", "=HYPERLINK(1)|:9100", site="Mumbai")
        with mock.patch.dict(tf.HOSTS, extra):
            body = self.client.get(self.url, {"by": "node", "format": "csv", "chart": "cpu"}).content.decode()
        header = body.splitlines()[0]
        self.assertNotIn(",=", header)
        self.assertIn("'=cmd", header)
        self.assertTrue(prom.range_queries)


# ============================================================================= front end contract
class OverviewAssetTests(SimpleTestCase):
    def test_js_writes_text_only_and_has_no_inline_handlers(self):
        js = (STATIC / "overview.js").read_text()
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            self.assertNotIn(banned, js)
        self.assertIn("textContent", js)
        self.assertIn("document.hidden", js)                  # live polling pauses with the tab
        self.assertIn("localStorage", js)

    def test_template_has_no_inline_handlers_or_unsafe_output(self):
        html = (Path(__file__).resolve().parent / "templates" / "telemetry" / "overview.html").read_text()
        self.assertIsNone(re.search(r"\son[a-z]+\s*=", html))
        self.assertNotIn("|safe", html)
        self.assertNotIn("mark_safe", html)

    def test_css_uses_tokens_only(self):
        css = (STATIC / "overview.css").read_text()
        self.assertIsNone(re.search(r"#[0-9a-fA-F]{3,8}\b", css))
        self.assertIsNone(re.search(r"rgba?\(", css))
