"""Regression tests for F20 (URL availability p95 over the whole window, batched) and F21 (analytics tenant filter)."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from tcpcheck.models import TcpCheck
from urlchecker.models import UrlCheck, UrlResult

from . import analytics, registry

COLUMNS_P95 = 9


class UrlAvailabilityP95Tests(TestCase):
    def rows(self):
        start, end = registry.period(1)
        rows, error = registry.generate(registry.REPORTS["url_availability"], registry.Ctx(start, end))
        self.assertEqual(error, "")
        return rows

    def test_p95_covers_the_whole_window(self):
        c = UrlCheck.objects.create(name="busy", url="https://busy.example/", site="All Sites")
        now = timezone.now()
        # 30,000 samples in the last 20 h: the oldest third is slow (1000 ms), the newest 20,000 are fast (10 ms).
        # p95 over the whole window is 1000 ms; the newest-20,000 shortcut reported 10 ms.
        UrlResult.objects.bulk_create([
            UrlResult(url_check=c, checked_at=now - timedelta(seconds=2 * i + 1), status="online",
                      response_ms=10.0 if i < 20000 else 1000.0) for i in range(30000)], batch_size=2000)
        row = self.rows()[0]
        self.assertEqual(row[4], 30000)
        self.assertEqual(row[COLUMNS_P95], 1000.0)
        self.assertEqual(len(row), len(registry.REPORTS["url_availability"].columns))

    def test_small_check_p95_matches_exact_value_and_queries_are_batched(self):
        now = timezone.now()

        def make(n):
            for k in range(n):
                c = UrlCheck.objects.create(name=f"c{UrlCheck.objects.count()}", url=f"https://c{k}.example/", site="All Sites")
                UrlResult.objects.bulk_create([UrlResult(url_check=c, checked_at=now - timedelta(minutes=i + 1),
                                                         status="online", response_ms=float(i)) for i in range(100)])
        make(2)
        with CaptureQueriesContext(connection) as q2:
            rows = self.rows()
        for r in rows:   # nearest rank (portable path) 94.0; percentile_cont on PostgreSQL 94.05 -> 94.0/94.1
            self.assertAlmostEqual(r[COLUMNS_P95], 94.0, delta=0.11)
        make(6)
        with CaptureQueriesContext(connection) as q8:
            self.assertEqual(len(self.rows()), 8)
        self.assertEqual(len(q8.captured_queries), len(q2.captured_queries))   # not one query per check


class AnalyticsTenantTests(TestCase):
    def test_inventory_tcp_counts_only_this_tenant(self):
        TcpCheck.objects.create(name="mine", host="a.example", port=22)
        TcpCheck.objects.create(name="theirs", host="b.example", port=22, tenant_key="other-tenant")
        admin = get_user_model().objects.create_superuser("root", password="x")
        out = analytics.inventory(admin)
        self.assertEqual(out["tcp"], 1)
