"""Regression tests for F22: the performance page must not compare against a window beyond probe retention."""
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

NOTE = "Not enough history to compare"


class PerformanceComparisonTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser("root", password="x"))

    def page(self, window):
        r = self.client.get(reverse("domains:performance"), {"window": window})
        self.assertEqual(r.status_code, 200)
        return r

    @override_settings(PROBE_RETENTION_DAYS=30)
    def test_last_30_days_has_no_previous_window(self):
        r = self.page("720")
        self.assertFalse(r.context["compare"])
        self.assertContains(r, NOTE)
        self.assertIsNone(r.context["k"]["regressed"])
        self.assertNotIn("p95 (previous window)", [s["name"] for s in r.context["chart"]["series"]])

    @override_settings(PROBE_RETENTION_DAYS=30)
    def test_short_windows_still_compare(self):
        r = self.page("24")
        self.assertTrue(r.context["compare"])
        self.assertNotContains(r, NOTE)
        self.assertIn("p95 (previous window)", [s["name"] for s in r.context["chart"]["series"]])

    @override_settings(PROBE_RETENTION_DAYS=90)
    def test_long_retention_allows_the_30_day_comparison(self):
        self.assertTrue(self.page("720").context["compare"])
