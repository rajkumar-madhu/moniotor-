"""Second audit round (domainmonitor): statistics are aggregated in the database instead of loading every check;
duplicated security headers still pass; renaming a domain drops the old host's results; the overview donut does
not compare model instances pairwise; a Unicode-digit ?selected= never raises."""
import re
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from . import analytics, security
from .models import Domain, DomainCheck, DomainSecurityScan
from .test_security import GOOD_HEADERS, facts

AGGREGATED = re.compile(r"GROUP BY|\b(AVG|COUNT|MAX|MIN|SUM)\(", re.I)


def check_queries(captured):
    """SQL of the captured queries that read the DomainCheck table."""
    return [q["sql"] for q in captured
            if "domainmonitor_domaincheck" in q["sql"] and q["sql"].lstrip().upper().startswith("SELECT")]


class AggregatedStatsTests(TestCase):
    """F2: the performance, overview and DNS pages must not materialise every stored check of the window."""

    def setUp(self):
        self.client.force_login(User.objects.create_superuser("root", password="x" * 14))
        now = timezone.now()
        self.a = Domain.objects.create(tenant_key="default", hostname="a.example.com", site_key="SiteA", status="up",
                                       dns_status="Resolved", ssl_status="Valid")
        self.b = Domain.objects.create(tenant_key="default", hostname="b.example.com", site_key="SiteA", status="up",
                                       dns_status="Resolved", ssl_status="Valid")
        rows = []
        for h in range(1, 13):
            rows.append(DomainCheck(domain=self.a, checked_at=now - timedelta(hours=h), up=True, response_ms=90 + 10 * h,
                                    ip_address="203.0.113.1", dns_status="Resolved", ssl_status="Valid"))
            rows.append(DomainCheck(domain=self.a, checked_at=now - timedelta(hours=24 + h), up=True, response_ms=400,
                                    ip_address="203.0.113.2", dns_status="Resolved", ssl_status="Valid"))
        for h, up in ((5, True), (4, False), (3, False), (2, True), (1, False)):
            rows.append(DomainCheck(domain=self.b, checked_at=now - timedelta(hours=h), up=up, response_ms=500 if up else None,
                                    ip_address="203.0.113.9" if up else None, dns_status="Resolved" if up else "Failed",
                                    ssl_status="Valid"))
        DomainCheck.objects.bulk_create(rows)

    def assert_aggregated(self, url):
        with CaptureQueriesContext(connection) as ctx:
            r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        reads = check_queries(ctx.captured_queries)
        self.assertTrue(reads, "the page reads DomainCheck rows")
        for sql in reads:
            self.assertRegex(sql, AGGREGATED, f"raw check rows loaded into Python by: {sql}")
        return r

    def test_performance_page_is_aggregated_and_still_exact(self):
        r = self.assert_aggregated(reverse("domains:performance") + "?window=24")
        rows = {d.hostname: d for d in r.context["page"]}
        a = rows["a.example.com"]
        self.assertEqual((a.cur["n"], a.cur["max"], a.cur["p50"]), (12, 210, 155))
        self.assertAlmostEqual(a.cur["avg"], 155.0)
        self.assertAlmostEqual(a.cur["p95"], 204.5)
        self.assertEqual(a.prev["p95"], 400)
        self.assertEqual(a.trend[0], "Improved")
        self.assertEqual(rows["b.example.com"].cur["n"], 2)
        self.assertEqual(r.context["k"]["cur"]["n"], 14)
        self.assertEqual(r.context["k"]["cur"]["max"], 500)
        series = {s["name"]: s["values"] for s in r.context["chart"]["series"]}
        self.assertEqual(sum(1 for v in series["p95"] if v is not None), 12)
        self.assertEqual(sum(1 for v in series["p95 (previous window)"] if v is not None), 12)
        self.assertIn(482.0, series["p95"])   # the bucket holding a 140 ms and a 500 ms check: 140 + 0.95 * 360
        for window in ("6", "168", "720"):
            self.assert_aggregated(reverse("domains:performance") + f"?window={window}")
        self.assert_aggregated(reverse("domains:performance") + "?window=24&format=csv")

    def test_overview_charts_are_aggregated(self):
        for rng in ("6", "24", "168"):
            r = self.assert_aggregated(reverse("domains:overview") + f"?resp_range={rng}")
        values = r.context["resp"]["series"][0]["values"]
        self.assertTrue(any(v is not None for v in values))
        self.assertIsNotNone(r.context["trend"]["resp"])

    def test_dns_page_is_aggregated(self):
        r = self.assert_aggregated(reverse("domains:dns"))
        rows = {d.hostname: d for d in r.context["rows"]}
        a, b = rows["a.example.com"], rows["b.example.com"]
        self.assertEqual((a.checks_n, a.fail_n), (24, 0))
        self.assertEqual([x["ip"] for x in a.records], ["203.0.113.1", "203.0.113.2"])
        self.assertEqual(a.records[0]["n"], 12)
        self.assertEqual((b.checks_n, b.fail_n), (5, 3))
        self.assertEqual(r.context["k"]["ips"], 3)
        self.assertEqual(r.context["k"]["changed"], 1)

    def test_histogram_percentiles_match_the_sample_percentiles(self):
        samples = [120, 95, 95, 300, 101, 99, 250, 95, 180, 101]
        bins = {}
        for s in samples:
            n, total, mx = bins.get(s, (0, 0, s))
            bins[s] = (n + 1, total + s, max(mx, s))
        hist = [(ms, n, total, mx) for ms, (n, total, mx) in bins.items()]
        got, want = analytics.describe_bins(hist), analytics.describe(samples)
        self.assertEqual(got["n"], want["n"])
        self.assertEqual(got["max"], want["max"])
        for key in ("p50", "p95", "avg"):
            self.assertAlmostEqual(got[key], want[key], msg=key)
        self.assertEqual(analytics.describe_bins([]), analytics.describe([]))


class DuplicatedHeaderTests(TestCase):
    """F3: a proxy and the app both sending X-Frame-Options / X-Content-Type-Options must not fail the check."""

    def failed(self, headers):
        grade, score, findings = security.evaluate(facts(headers=headers))
        return {f["key"] for f in findings if not f["passed"]}, grade, score

    def test_same_value_twice_still_passes(self):
        h = dict(GOOD_HEADERS, **{"content-security-policy": "default-src 'self'",   # no frame-ancestors fallback
                                  "x-frame-options": "SAMEORIGIN, SAMEORIGIN", "x-content-type-options": "nosniff, nosniff"})
        self.assertEqual(self.failed(h), (set(), "A", 100))
        h["x-frame-options"] = "DENY,deny"
        self.assertEqual(self.failed(h), (set(), "A", 100))

    def test_bad_tokens_still_fail(self):
        h = dict(GOOD_HEADERS)
        del h["content-security-policy"]   # no frame-ancestors fallback
        h["x-frame-options"] = "SAMEORIGIN, ALLOW-FROM https://x.example"
        h["x-content-type-options"] = "none, nosniff"
        keys, _grade, _score = self.failed(h)
        self.assertEqual(keys, {"csp", "x_frame_options", "x_content_type_options"})


class RenameResetsResultsTests(TestCase):
    """F4: a renamed domain must not keep the previous host's checks, scans, certificate and status."""

    def setUp(self):
        self.client.force_login(User.objects.create_superuser("root", password="x" * 14))
        now = timezone.now()
        self.d = Domain.objects.create(tenant_key="default", hostname="old.example.com", site_key="SiteA", status="up",
                                       dns_status="Resolved", ssl_status="Valid", ip_address="203.0.113.1", http_status=200,
                                       response_ms=120.0, ssl_expires_at=now + timedelta(days=300), last_checked_at=now,
                                       last_error="")
        DomainCheck.objects.create(domain=self.d, checked_at=now - timedelta(hours=1), up=True, response_ms=120,
                                   dns_status="Resolved", ssl_status="Valid")
        DomainSecurityScan.objects.create(domain=self.d, scanned_at=now - timedelta(hours=1), grade="A", score=100, findings=[])

    def form(self, hostname):
        return {"pk": self.d.pk, "hostname": hostname, "domain_type": "Production", "site_key": "SiteA",
                "check_interval_seconds": 300, "monitor_enabled": "on"}

    @mock.patch("probes.runner.run_domain_checks", return_value=[])
    def test_rename_drops_old_results(self, run):
        r = self.client.post(reverse("domains:list"), self.form("new.example.com"))
        self.assertEqual(r.status_code, 302)
        self.d.refresh_from_db()
        self.assertEqual(self.d.hostname, "new.example.com")
        self.assertEqual(self.d.checks.count(), 0)
        self.assertEqual(self.d.security_scans.count(), 0)
        self.assertEqual((self.d.status, self.d.dns_status, self.d.ssl_status), ("unknown", "Unknown", "Unknown"))
        for f in ("ip_address", "http_status", "response_ms", "ssl_expires_at", "last_checked_at"):
            self.assertIsNone(getattr(self.d, f), f)
        run.assert_called_once()
        self.assertEqual(security.latest_scans([self.d.pk]), {})

    @mock.patch("probes.runner.run_domain_checks", return_value=[])
    def test_edit_without_rename_keeps_history(self, run):
        self.client.post(reverse("domains:list"), dict(self.form("OLD.example.com."), description="same host"))
        self.d.refresh_from_db()
        self.assertEqual((self.d.hostname, self.d.description), ("old.example.com", "same host"))
        self.assertEqual(self.d.checks.count(), 1)
        self.assertEqual(self.d.security_scans.count(), 1)
        self.assertEqual(self.d.status, "up")
        self.assertIsNotNone(self.d.ssl_expires_at)


class OverviewDonutTests(TestCase):
    """F5: the donut must not test `d in healthy` (a linear scan with Model.__eq__ per domain)."""

    def setUp(self):
        self.client.force_login(User.objects.create_user("v", password="x" * 14))
        Domain.objects.bulk_create([
            Domain(tenant_key="default", hostname=f"d{i:03}.example.com", site_key="All Sites", status="up",
                   dns_status="Resolved", ssl_status="Valid") for i in range(40)]
            + [Domain(tenant_key="default", hostname="down.example.com", site_key="All Sites", status="down"),
               Domain(tenant_key="default", hostname="exp.example.com", site_key="All Sites", status="up",
                      dns_status="Resolved", ssl_status="Expiring"),
               Domain(tenant_key="default", hostname="dns.example.com", site_key="All Sites", status="up",
                      dns_status="Failed", ssl_status="Valid"),
               Domain(tenant_key="default", hostname="paused.example.com", site_key="All Sites", monitor_enabled=False)])

    def test_slices_without_pairwise_model_comparison(self):
        calls = [0]
        original = Domain.__eq__

        def counting(self, other):
            calls[0] += 1
            return original(self, other)

        with mock.patch.object(Domain, "__eq__", counting):
            r = self.client.get(reverse("domains:overview"))
        self.assertEqual(r.status_code, 200)
        slices = {x["label"]: x["n"] for x in r.context["legend"]}
        self.assertEqual(slices, {"Healthy": 40, "Down": 1, "SSL Expiring": 1, "Paused": 1, "Other issues": 1})
        self.assertEqual(r.context["n"]["healthy"], 40)
        self.assertLess(calls[0], 44, "the donut compares model instances pairwise")


class SecuritySelectedParamTests(TestCase):
    """F6: ?selected= with a Unicode digit (isdigit() is True, int() fails) must not be a server error."""

    def setUp(self):
        self.client.force_login(User.objects.create_user("v", password="x" * 14))
        self.d = Domain.objects.create(tenant_key="default", hostname="a.example.com", site_key="All Sites")

    def test_unicode_digit_is_ignored(self):
        for sel in ("%C2%B2", "%C2%B3", "abc", "-1", ""):   # superscripts: isdigit() is True, int() raises
            r = self.client.get(reverse("domains:security") + f"?selected={sel}")
            self.assertEqual(r.status_code, 200, sel)
            self.assertIsNone(r.context["selected"], sel)
        r = self.client.get(reverse("domains:security") + f"?selected={self.d.pk}")
        self.assertEqual(r.context["selected"].pk, self.d.pk)
