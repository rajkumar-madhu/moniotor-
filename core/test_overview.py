"""Operations overview: market-day strip, site matrix and site scoping."""
from datetime import datetime, time, timezone as dt_tz
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase

from bodeod.models import Mode, Run
from core import overview
from core.models import Membership, Site
from tcpcheck.models import TcpCheck
from urlchecker.models import UrlCheck

MIDDAY = datetime(2026, 11, 3, 7, 50, tzinfo=dt_tz.utc)   # Tue 13:20 IST
SATURDAY = datetime(2026, 11, 7, 7, 50, tzinfo=dt_tz.utc)


class OverviewTests(TestCase):
    def setUp(self):
        self.mum = Site.objects.create(name="Mumbai (NSE)", code="MUMNSE")
        self.dr = Site.objects.create(name="DR Site", code="DR")
        UrlCheck.objects.create(name="oms", url="https://oms.example.com/", site="Mumbai (NSE)", last_status="down")
        UrlCheck.objects.create(name="web", url="https://web.example.com/", site="Mumbai (NSE)", last_status="online")
        UrlCheck.objects.create(name="dr", url="https://dr.example.com/", site="DR Site", last_status="online")
        TcpCheck.objects.create(name="gw", host="gw.example.com", port=443, site="DR Site", last_status="degraded")
        self.admin = User.objects.create_superuser("root", "r@example.com", "pw-Strong-12345")
        self.mum_viewer = User.objects.create_user("mv")
        Membership.objects.create(user=self.mum_viewer, site=self.mum, role="viewer")

    def test_site_matrix_counts_and_worst_first(self):
        m = overview.site_matrix(self.admin)
        rows = {r["site"]: r for r in m["rows"]}
        url = next(c for c in rows["Mumbai (NSE)"]["cells"] if c["kind"] == "url")
        self.assertEqual(url["c"], {"total": 2, "ok": 1, "bad": 1, "other": 0})
        self.assertEqual(rows["Mumbai (NSE)"]["tone"], "crit")
        self.assertEqual(rows["DR Site"]["tone"], "warn")  # degraded TCP is not down
        self.assertEqual(m["rows"][0]["site"], "Mumbai (NSE)")

    def test_site_matrix_is_site_scoped(self):
        sites = [r["site"] for r in overview.site_matrix(self.mum_viewer)["rows"]]
        self.assertIn("Mumbai (NSE)", sites)
        self.assertNotIn("DR Site", sites)

    def test_site_matrix_selected_site_narrows_rows_and_totals(self):
        m = overview.site_matrix(self.admin, "DR Site")
        self.assertEqual([r["site"] for r in m["rows"]], ["DR Site"])
        self.assertEqual(next(t for t in m["totals"] if t["key"] == "url")["total"], 1)

    def test_site_matrix_unknown_or_hidden_site_returns_no_rows(self):
        # Same behaviour as backend_summary: a site outside the user's scope shows nothing, not every site.
        self.assertEqual(overview.site_matrix(self.admin, "Typo")["rows"], [])
        m = overview.site_matrix(self.mum_viewer, "DR Site")
        self.assertEqual(m["rows"], [])
        self.assertTrue(all(t["total"] == 0 for t in m["totals"]))

    def test_heatmap_page_renders_site_heatmap_without_overview_css(self):
        self.client.force_login(self.admin)
        r = self.client.get("/monitoring/heatmap/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "legacy-heatmap-card")
        self.assertContains(r, "site-heatmap.css")
        self.assertNotContains(r, "pages/overview.css")

    def test_site_map_points_require_coordinates_and_respect_site_scope(self):
        self.mum.latitude, self.mum.longitude = 19.076, 72.8777
        self.mum.save(update_fields=["latitude", "longitude"])
        self.dr.latitude, self.dr.longitude = 12.9716, 77.5946
        self.dr.save(update_fields=["latitude", "longitude"])
        matrix = overview.site_matrix(self.mum_viewer)
        points = overview.site_map_points(self.mum_viewer, matrix["rows"])
        self.assertEqual([point["name"] for point in points], ["Mumbai (NSE)"])
        self.assertEqual(points[0]["latLng"], [19.076, 72.8777])
        # With a site filter only that site is plotted (others would otherwise show as 'none').
        matrix = overview.site_matrix(self.admin, "DR Site")
        points = overview.site_map_points(self.admin, matrix["rows"], "DR Site")
        self.assertEqual([point["name"] for point in points], ["DR Site"])

    def test_market_day_midday_places_now_and_missed_bod(self):
        Mode.objects.create(code="BOD", name="BOD", window_start=time(7), window_end=time(9))
        eod = Mode.objects.create(code="EOD", name="EOD", window_start=time(15, 30), window_end=time(16))
        md = overview.market_day(self.admin, now=MIDDAY)
        self.assertTrue(md["trading"])
        self.assertEqual(md["phase"]["key"], "trading")
        self.assertTrue(md["line"].startswith("Trading hours until 15:30"))
        self.assertAlmostEqual(md["now_left"], (13 + 20 / 60 - 6) * 100 / 18, places=1)
        by = {m["code"]: m for m in md["modes"]}
        self.assertEqual(by["BOD"]["status"], "missed")      # window over, no run today
        self.assertEqual(by["EOD"]["status"], "pending")
        Run.objects.create(mode=eod, trading_date=md["now"].date(), status="pass")
        md = overview.market_day(self.admin, now=MIDDAY)
        self.assertEqual({m["code"]: m["status"] for m in md["modes"]}["EOD"], "pass")

    def test_market_day_weekend_says_next_trading_day(self):
        md = overview.market_day(self.admin, now=SATURDAY)
        self.assertFalse(md["trading"])
        self.assertIn("Next trading day: Monday 9 November", md["line"])

    def test_lanes_do_not_overlap(self):
        for code, s, e in [("BOD", 7, 9), ("EOD", 7, 8), ("ADP", 16, 20)]:
            Mode.objects.create(code=code, name=code, window_start=time(s), window_end=time(e))
        md = overview.market_day(self.admin, now=MIDDAY)
        lanes = {}
        for m in md["modes"]:
            for other in lanes.get(m["lane"], []):
                self.assertTrue(m["left"] >= other["left"] + max(other["width"], 9.0) or
                                other["left"] >= m["left"] + max(m["width"], 9.0))
            lanes.setdefault(m["lane"], []).append(m)
        self.assertEqual(md["lanes"], 2)

    def test_dashboard_renders(self):
        self.client.force_login(self.admin)
        with mock.patch("django.utils.timezone.now", return_value=MIDDAY):
            r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Trading hours until 15:30 IST.")
        self.assertContains(r, 'class="ov-strip"')
        self.assertContains(r, "Mumbai (NSE)")
        self.assertContains(r, "1 down")

    def test_dashboard_hides_other_sites_from_scoped_viewer(self):
        self.client.force_login(self.mum_viewer)
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, "dr.example.com")
        self.assertNotContains(r, ">DR Site<")
