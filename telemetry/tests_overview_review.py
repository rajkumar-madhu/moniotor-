"""Review of the Fleet Overview: PromQL built from hostile labels, scoping across cached views of different users,
odd Prometheus answers (NaN, infinity, wrong shapes) and markup/CSV built from scraped names. Prometheus is mocked."""
import json
import re
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, SimpleTestCase, override_settings
from django.urls import reverse

from core.models import Membership, Site

from . import overview as ov
from . import tests_fleet as tf
from .tests_overview import PROM, OverviewCase, OverviewProm

HOSTILE_IDS = [
    'a"b:9100', "a\\b:9100", "a|b:9100", "a.b:9100", "a b:9100", "(x)+*?[]{}^$:9100", "a\nb:9100", "tab\there:9100",
    "ünï:9100", "web-01:9100", "=cmd|' /c calc:9100", "}{job=\"x\"}:9100", "a,b=~\".*\":9100",
]


def promql_unquote(literal):
    """The value of a PromQL double-quoted string literal (what Prometheus compiles the regex from)."""
    assert literal[0] == literal[-1] == '"'
    out, i, body = [], 0, literal[1:-1]
    while i < len(body):
        c = body[i]
        if c == "\\":
            n = body[i + 1]
            out.append({"n": "\n", "t": "\t", '"': '"', "\\": "\\"}.get(n, n))
            i += 2
        else:
            assert c != '"' and c != "\n", "unescaped quote or newline inside the literal"
            out.append(c)
            i += 1
    return "".join(out)


class MatcherRoundTripTests(SimpleTestCase):
    def test_the_matcher_selects_exactly_the_listed_instances(self):
        """Compile what Prometheus would: unquote the literal, anchor it, and match instance ids against it."""
        m = ov.matcher_for(HOSTILE_IDS)
        self.assertTrue(m.startswith('instance=~"'))
        pattern = re.compile(promql_unquote(m[len("instance=~"):]), re.S)
        for inst in HOSTILE_IDS:
            self.assertIsNotNone(pattern.fullmatch(inst), inst)
        for other in ("axb:9100", "a:9100", "b:9100", "a", "web-01:9100x", "xweb-01:9100", "", "aXb:9100", "ab:9100",
                      "a\\\\b:9100", "x" * 50):
            self.assertIsNone(pattern.fullmatch(other), other)

    def test_a_single_hostile_instance_cannot_break_out_of_its_literal(self):
        for inst in HOSTILE_IDS:
            m = ov.matcher_for([inst])
            self.assertTrue(m.startswith("instance="))
            self.assertEqual(promql_unquote(m[len("instance="):]), inst)

    def test_every_chart_query_keeps_the_matcher_inside_one_selector(self):
        sel = ov.matcher_for(HOSTILE_IDS)
        for off in ("", " offset 900s"):
            for spec in ov.chart_specs(sel, "300s", off):
                for q in [g["q"] for g in spec["dims"]] + [spec["node"], spec["total"]]:
                    # the quoted matcher appears wherever the selector does, and parentheses/braces balance outside strings
                    bare = re.sub(r'"(?:[^"\\]|\\.)*"', '""', q)
                    self.assertEqual(bare.count("{"), bare.count("}"), (spec["key"], q))
                    self.assertEqual(bare.count("("), bare.count(")"), (spec["key"], q))
                    self.assertEqual(bare.count("["), bare.count("]"), (spec["key"], q))


class OddRangesTests(SimpleTestCase):
    def test_hostile_range_parameters_fall_back_to_the_defaults(self):
        from django.http import QueryDict
        for raw in ("range=custom&last=%00&unit=h", "range=custom&last=5%20&unit=h", "range=custom&last=1_000&unit=d",
                    "range=%E0%A4%A4&last=9", "range=custom&last=10&unit=y", "range=custom&last=١٢&unit=h",
                    "range=custom&last=99999999&unit=d", "by=node%27%29&live=15%3B", "live=-15", "live=015"):
            p = ov.Params(QueryDict(raw))
            self.assertIn(p.range_key, list(ov.RANGES) + ["custom"], raw)
            self.assertTrue(ov.CUSTOM_MIN <= p.seconds <= ov.CUSTOM_MAX or p.seconds in {s for _l, s in ov.RANGES.values()}, raw)
            self.assertIn(p.by, ("",) + ov.BY_KEYS, raw)
            self.assertIn(p.live, (0,) + ov.LIVE_CHOICES, raw)
            self.assertRegex(p.last, r"^[0-9]{0,6}$", raw)
            self.assertIn(p.unit, ("", "m", "h", "d"), raw)

    def test_node_list_is_bounded_and_unique(self):
        from django.utils.datastructures import MultiValueDict
        q = MultiValueDict({"nodes": ["n%d" % (i % 3000) for i in range(9000)] + ["", ""]})
        nodes = ov.Params(q).nodes
        self.assertLessEqual(len(nodes), 2000)
        self.assertEqual(len(nodes), len(set(nodes)))
        self.assertNotIn("", nodes)


@override_settings(PROMETHEUS_URL=PROM)
class OverviewCacheScopeTests(OverviewCase):
    """The chart cache is keyed by matcher: a limited user's view must never be served from, or into, an admin's."""

    def setUp(self):
        super().setUp()
        mumbai = Site.objects.create(name="Mumbai", code="mumbai")
        Site.objects.create(name="Chennai", code="chennai")
        self.one = User.objects.create_user("single", password="pw")
        Membership.objects.create(user=self.one, site=mumbai, role="viewer")
        tf.ci("Database", "db-01:9100", site="Mumbai")
        tf.ci("Web", "web-01:9100", site="Chennai")
        tf.alert(name="OtherSiteNoInstance", instance="", severity="critical", site="Chennai")
        tf.alert(name="Mine", instance="db-01:9100", severity="critical", site="Mumbai")
        self.admin = Client()
        self.admin.force_login(User.objects.create_superuser("root", "root@example.com", "pw"))
        self.limited = Client()
        self.limited.force_login(self.one)

    def test_admin_first_then_limited_user_gets_nothing_of_the_admins(self):
        self.start()
        for params in ({}, {"by": "node"}, {"by": "site"}, {"compare": "1"}, {"range": "1h"}, {"by": "env", "range": "6h"}):
            admin = self.admin.get(self.url, params)
            self.assertContains(admin, "web-01")
            mine = self.limited.get(self.url, params)
            body = mine.content.decode()
            self.assertEqual(mine.status_code, 200, params)
            self.assertNotIn("web-01", body, params)
            self.assertNotIn("Chennai", body, params)
            self.assertNotIn("OtherSiteNoInstance", body, params)
            seen = {h["instance"] for h in mine.context["sel"].hosts}
            self.assertIn("db-01:9100", seen, params)
            self.assertNotIn("web-01:9100", seen, params)

    def test_limited_user_first_then_admin_still_sees_the_whole_fleet(self):
        self.start()
        self.limited.get(self.url)
        r = self.admin.get(self.url)
        self.assertEqual({h["instance"] for h in r.context["sel"].hosts} >= {"db-01:9100", "web-01:9100"}, True)

    def test_json_and_csv_never_serve_the_admins_cached_series(self):
        self.start()
        self.admin.get(self.url, {"by": "node"})
        j = self.limited.get(reverse("telemetry:overview_json"), {"by": "node"})
        self.assertNotIn("web", json.dumps(j.json()["legends"]).lower())
        csv = self.limited.get(self.url, {"by": "node", "format": "csv", "chart": "cpu"}).content.decode()
        self.assertNotIn("Web", csv)
        self.assertIn("Database", csv)

    def test_alert_drawer_hides_other_sites_even_without_an_instance_label(self):
        self.start()
        r = self.limited.get(self.url)
        names = [a.alertname for a in r.context["alerts"]]
        self.assertEqual(names, ["Mine"])
        self.assertEqual(r.context["alerts_total"], 1)

    def test_a_site_filter_naming_another_site_is_ignored_for_alerts_too(self):
        self.start()
        r = self.limited.get(self.url, {"site": "Chennai"})
        self.assertEqual(list(r.context["alerts"]), [])
        self.assertNotContains(r, "OtherSiteNoInstance")

    def test_query_strings_for_a_limited_user_never_name_other_hosts(self):
        prom = self.start()
        self.limited.get(self.url, {"nodes": ["web-01:9100", "db-01:9100"], "by": "node", "compare": "1"})
        every = " ".join(prom.range_queries)
        self.assertIn("db-01", every)
        self.assertNotIn("web-01", every)
        self.assertNotIn('job=~".*node.*"', every)


class OddPrometheus(OverviewProm):
    """Answers every range query with the awkward shapes a real or half-broken Prometheus can produce."""

    def __init__(self, shape, **kw):
        super().__init__(**kw)
        self.shape = shape

    def query_range(self, q, seconds=3600, step=None):
        with self.lock:
            self.range_queries.append(q)
        t0 = 1_700_000_000
        nan, inf = float("nan"), float("inf")
        shapes = {
            "nan": [{"metric": {"mode": "user", "instance": "db-01:9100", "__name__": "node_load1"},
                     "values": [(t0, nan), (t0 + 15, 1.0), (t0 + 30, nan)]}],
            "inf": [{"metric": {"mode": "user", "instance": "db-01:9100"}, "values": [(t0, inf), (t0 + 15, -inf), (t0 + 30, 2.0)]}],
            "all_nan": [{"metric": {"mode": "user", "instance": "db-01:9100"}, "values": [(t0, nan), (t0 + 15, nan)]}],
            "empty_values": [{"metric": {"mode": "user", "instance": "db-01:9100"}, "values": []}],
            "no_metric": [{"metric": None, "values": [(t0, 1.0)]}, {"values": [(t0, 2.0)]}],
            "no_instance": [{"metric": {"job": "node"}, "values": [(t0, 1.0), (t0 + 15, 2.0)]}],
            "unknown_instance": [{"metric": {"instance": "ghost:1", "mode": "user"}, "values": [(t0, 1.0), (t0 + 15, 2.0)]}],
            "negative": [{"metric": {"mode": "user", "instance": "db-01:9100"}, "values": [(t0, -5.0), (t0 + 15, -1e300)]}],
            "unsorted": [{"metric": {"mode": "user", "instance": "db-01:9100"}, "values": [(t0 + 30, 1.0), (t0, 2.0), (t0 + 15, 3.0)]}],
            "dupes": [{"metric": {"mode": "user", "instance": "db-01:9100"}, "values": [(t0, 1.0), (t0, 2.0)]}] * 3,
            "many_series": [{"metric": {"mode": f"m{i}", "instance": f"i{i}:1", "__name__": f"n{i}"}, "values": [(t0, float(i))]}
                            for i in range(300)],
            "html_labels": [{"metric": {"mode": "<b>x</b>", "instance": "<script>alert(1)</script>", "__name__": '"><svg onload=alert(1)>'},
                             "values": [(t0, 1.0), (t0 + 15, 2.0)]}],
            "float_ts": [{"metric": {"mode": "user", "instance": "db-01:9100"}, "values": [(t0 + 0.5, 1.0), (t0 + 15.25, 2.0)]}],
        }
        return shapes[self.shape]


@override_settings(PROMETHEUS_URL=PROM)
class OddShapeTests(OverviewCase):
    SHAPES = ["nan", "inf", "all_nan", "empty_values", "no_metric", "no_instance", "unknown_instance", "negative",
              "unsorted", "dupes", "many_series", "html_labels", "float_ts"]

    def setUp(self):
        super().setUp()
        self.login_admin()
        tf.ci("Database", "db-01:9100", site="Mumbai")
        tf.ci("Web", "web-01:9100", site="Chennai")

    def run_shape(self, shape):
        cache.clear()
        prom = OddPrometheus(shape)
        patches = prom.patches()
        for p in patches:
            p.start()
        try:
            out = {}
            for by in ("", "node", "site"):
                for compare in ("", "1"):
                    page = self.client.get(self.url, {"by": by, "compare": compare})
                    live = self.client.get(reverse("telemetry:overview_json"), {"by": by, "compare": compare})
                    out[(by, compare)] = (page, live)
            return out
        finally:
            for p in patches:
                p.stop()

    def test_no_shape_breaks_a_page_the_json_or_the_csv(self):
        for shape in self.SHAPES:
            for (by, compare), (page, live) in self.run_shape(shape).items():
                where = (shape, by, compare)
                self.assertEqual(page.status_code, 200, where)
                self.assertEqual(live.status_code, 200, where)
                # strict JSON: no NaN / Infinity tokens that a browser's JSON.parse would reject
                json.loads(live.content.decode(), parse_constant=lambda c: self.fail(f"{c} in json {where}"))
                body = page.content.decode()
                for blob in re.findall(r'<script id="ov-[^"]+" type="application/json">(.*?)</script>', body, re.S):
                    json.loads(blob, parse_constant=lambda c: self.fail(f"{c} in json_script {where}"))
                self.assertNotIn("<script>alert(1)</script>", body, where)
                self.assertNotIn("onload=alert(1)>", body.replace("&quot;&gt;&lt;svg onload=alert(1)&gt;", ""), where)

    def test_csv_of_odd_shapes_is_well_formed_and_numeric(self):
        import csv
        import io
        for shape in ("nan", "inf", "unsorted", "dupes", "float_ts"):
            cache.clear()
            prom = OddPrometheus(shape)
            patches = prom.patches()
            for p in patches:
                p.start()
            try:
                r = self.client.get(self.url, {"format": "csv", "chart": "cpu"})
            finally:
                for p in patches:
                    p.stop()
            self.assertIn(r.status_code, (200, 404), shape)
            if r.status_code == 200:
                rows = list(csv.reader(io.StringIO(r.content.decode())))
                self.assertEqual(rows[0][0], "time_utc")
                for row in rows[1:]:
                    self.assertEqual(len(row), len(rows[0]), shape)
                    for cell in row[1:]:
                        if cell:
                            self.assertTrue(re.fullmatch(r"-?[0-9.]+(e[+-]?[0-9]+)?", cell), (shape, cell))

    def test_the_series_cap_holds_for_hundreds_of_series(self):
        cache.clear()
        prom = OddPrometheus("many_series")
        patches = prom.patches()
        for p in patches:
            p.start()
        try:
            r = self.client.get(self.url, {"by": "node"})
        finally:
            for p in patches:
                p.stop()
        for sec in r.context["sections"]:
            for c in sec["charts"]:
                if c["has"]:
                    self.assertLessEqual(len(c["data"]["series"]), ov.MAX_SERIES + 1, c["key"])


@override_settings(PROMETHEUS_URL=PROM)
class HostileHostNameTests(OverviewCase):
    def test_names_and_labels_print_escaped_everywhere(self):
        self.login_admin()
        evil = '"><img src=x onerror=alert(1)>'
        tf.ci(evil, "db-01:9100", site=evil)
        self.start()
        for params in ({}, {"by": "node"}, {"by": "site"}, {"site": evil}, {"nodes": evil}, {"client": evil, "env": evil}):
            body = self.client.get(self.url, params).content.decode()
            self.assertNotIn("<img src=x onerror", body, params)
        csv = self.client.get(self.url, {"by": "site", "format": "csv", "chart": "cpu"}).content.decode()
        self.assertNotIn(",=", csv)

    def test_csv_cells_with_a_leading_tab_or_return_are_defused(self):
        from .views.common import _cell
        for raw in ("\t=1+1", "\r=1+1", "=1+1", "+1+1", "-1+1", "@SUM(A1)"):
            self.assertTrue(_cell(raw).startswith("'"), raw)
        for ok in ("12", "-3.5", "1e-05", "name", ""):
            self.assertEqual(_cell(ok), ok)


@override_settings(PROMETHEUS_URL=PROM)
class ClientParameterTests(OverviewCase):
    """``client`` is the sidebar picker's client id; the Overview's own client-name filter is ``client_name``."""

    def setUp(self):
        super().setUp()
        from core.models import Client as ClientModel
        self.acme = ClientModel.objects.create(name="Acme")
        Site.objects.create(name="Mumbai", code="mumbai", client=self.acme)
        Site.objects.create(name="Chennai", code="chennai")
        tf.ci("Database", "db-01:9100", site="Mumbai")
        tf.ci("Web", "web-01:9100", site="Chennai")
        self.login_admin()

    def test_the_sidebar_pickers_params_do_not_empty_the_page(self):
        self.start()
        r = self.client.get(self.url, {"site": "Mumbai", "client": str(self.acme.pk)})
        self.assertEqual([h["instance"] for h in r.context["sel"].hosts], ["db-01:9100"])
        self.assertFalse(r.context["params"].client)
        r = self.client.get(self.url, {"client": str(self.acme.pk)})
        self.assertGreaterEqual(len(r.context["sel"].hosts), 2)

    def test_the_overview_has_its_own_client_name_filter(self):
        self.start()
        r = self.client.get(self.url, {"client_name": "Acme"})
        self.assertEqual([h["instance"] for h in r.context["sel"].hosts], ["db-01:9100"])
        self.assertContains(r, 'name="client_name"')
        self.assertNotContains(r, 'select name="client"')


@override_settings(PROMETHEUS_URL=PROM)
class RoleTests(OverviewCase):
    def test_every_role_gets_the_page_the_json_and_the_csv_but_only_operators_force_a_refresh(self):
        site = Site.objects.create(name="Mumbai", code="mumbai")
        tf.ci("Database", "db-01:9100", site="Mumbai")
        prom = self.start()
        for role in ("viewer", "operator", "admin"):
            user = User.objects.create_user(role, password="pw")
            Membership.objects.create(user=user, site=site, role=role)
            c = Client()
            c.force_login(user)
            self.assertEqual(c.get(self.url).status_code, 200, role)
            self.assertEqual(c.get(reverse("telemetry:overview_json")).status_code, 200, role)
            self.assertEqual(c.get(self.url, {"format": "csv", "chart": "cpu"}).status_code, 200, role)
            before = len(prom.range_queries)
            c.get(self.url, {"refresh": "1"})
            if role == "viewer":
                self.assertEqual(len(prom.range_queries), before, "a viewer's refresh=1 is served from the cache")

    def test_anonymous_users_get_no_json_and_no_csv(self):
        self.start()
        c = Client()
        for url in (reverse("telemetry:overview_json"), self.url + "?format=csv&chart=cpu"):
            r = c.get(url)
            self.assertEqual(r.status_code, 302, url)
            self.assertIn("login", r["Location"])
