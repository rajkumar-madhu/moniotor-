"""Regression tests for F14: TCP daily history and response trend are aggregated by the database."""
from datetime import timedelta

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from . import stats
from .models import TcpCheck, TcpResult


class SqlAggregationTests(TestCase):
    def setUp(self):
        self.a = TcpCheck.objects.create(name="a", host="a.example", port=22)
        self.b = TcpCheck.objects.create(name="b", host="b.example", port=22)
        now = timezone.now()
        rows = []
        # check a: today 1 down of 10 (>5% -> Down); check b: today all online
        for i in range(10):
            at = now - timedelta(seconds=i + 1)
            rows.append(TcpResult(tcp_check=self.a, checked_at=at, status="down" if i == 0 else "online", response_ms=10 + i))
            rows.append(TcpResult(tcp_check=self.b, checked_at=at, status="online", response_ms=100))
        TcpResult.objects.bulk_create(rows)

    def test_daily_history_counts_in_sql(self):
        with CaptureQueriesContext(connection) as q:
            data = stats.daily_history(TcpCheck.objects.all())
        self.assertEqual(len(q.captured_queries), 1)
        sql = q.captured_queries[0]["sql"].upper()
        self.assertIn("COUNT(", sql)
        self.assertIn("GROUP BY", sql)
        series = {s["name"]: s["values"] for s in data["series"]}
        self.assertEqual((series["Down"][-1], series["Online"][-1], series["Degraded"][-1]), (1, 1, None))
        self.assertEqual(len(data["labels"]), 7)
        self.assertEqual(data["labels"][-1], timezone.localdate().strftime("%b %d"))

    def test_response_trend_aggregates_in_sql(self):
        with CaptureQueriesContext(connection) as q:
            data = stats.response_trend(TcpCheck.objects.all(), hours=1, buckets=4)
        self.assertEqual(len(q.captured_queries), 1)
        self.assertIn("AVG(", q.captured_queries[0]["sql"].upper())
        avg, mn, mx = (s["values"] for s in data["series"])
        self.assertEqual(len(avg), 4)
        self.assertEqual(mn[-1], 10)
        self.assertEqual(mx[-1], 100)
        self.assertEqual(avg[-1], round((sum(range(10, 20)) + 1000) / 20, 1))
        self.assertEqual(avg[:3], [None, None, None])
