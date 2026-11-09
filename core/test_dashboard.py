"""The operations dashboard: banner, KPI cards, needs-attention queue, fleet panel (cache only), heat-map, empty
install, per-role and per-site scoping, the JSON refresh, and its query budget."""
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from core import dashboard
from core.events import emit
from core.models import Membership, Site
from network.models import Device
from tcpcheck.models import TcpCheck
from telemetry.models import Alert
from urlchecker.models import UrlCheck, UrlResult

PW = "Corr3ct-horse-battery"


class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.mum = Site.objects.create(name="Mumbai", code="mum")
        self.dr = Site.objects.create(name="DR Site", code="dr")
        self.root = User.objects.create_superuser("root", "r@example.com", PW)
        self.viewer = User.objects.create_user("viewer", "v@example.com", PW)
        Membership.objects.create(user=self.viewer, site=self.mum, role="viewer")
        self.mum_op = User.objects.create_user("mumop", "o@example.com", PW)
        Membership.objects.create(user=self.mum_op, site=self.mum, role="operator")
        UrlCheck.objects.create(name="Portal", url="https://portal.example.com/", site="Mumbai", last_status="down")
        UrlCheck.objects.create(name="Wiki", url="https://wiki.example.com/", site="Mumbai", last_status="online")
        Device.objects.create(name="dr-fw-01", device_type="firewall", mgmt_ip="10.9.0.1", site="DR Site", status="down")
        TcpCheck.objects.create(name="Oracle", host="db.example.com", port=1521, site="Mumbai", last_status="degraded")
        self.alert_m = Alert.objects.create(fingerprint="m1", alertname="DiskFullMumbai", severity="critical", site="Mumbai",
                                            starts_at=timezone.now() - timedelta(minutes=30), summary="disk")
        self.alert_d = Alert.objects.create(fingerprint="d1", alertname="DiskFullDR", severity="critical", site="DR Site",
                                            starts_at=timezone.now() - timedelta(minutes=20))
        emit("bodeod", "BOD ok-ish Mumbai", severity="warning", site="Mumbai")
        emit("bodeod", "EOD late DR", severity="warning", site="DR Site")

    def tearDown(self):
        cache.clear()

    def get(self, user, **params):
        self.client.force_login(user)
        return self.client.get(reverse("dashboard"), params)


class PageTests(Base):
    def test_renders_for_every_role(self):
        for user in (self.root, self.mum_op, self.viewer):
            r = self.get(user)
            self.assertEqual(r.status_code, 200, user)
            for part in ("data-banner", "dash-kpis", "data-problems", "data-events", "dash-heat-table"):
                self.assertContains(r, part)

    def test_banner_states(self):
        crit = dashboard.banner([{"status": "down"}, {"status": "degraded"}], {"p12": 2}, "", timezone.now())
        self.assertEqual((crit["tone"], crit["crit"], crit["warn"]), ("crit", 1, 1))
        self.assertIn("2 open incidents at P1 or P2", crit["detail"])
        warn = dashboard.banner([{"status": "Expiring"}], {}, "", timezone.now())
        self.assertEqual(warn["tone"], "warn")
        self.assertEqual(warn["title"], "1 item needs attention")
        ok = dashboard.banner([], {}, "", timezone.now())
        self.assertEqual((ok["tone"], ok["title"]), ("ok", "All systems operational"))

    def test_kpi_cards_and_their_links(self):
        r = self.get(self.root)
        keys = [k["key"] for k in r.context["kpis"]]
        self.assertEqual(keys[:3], ["attention", "incidents", "alerts"])
        self.assertIn("health", keys)
        att = next(k for k in r.context["kpis"] if k["key"] == "attention")
        self.assertEqual(att["value"], str(len(r.context["problems"])))
        self.assertEqual(att["href"], "#attention")
        alerts = next(k for k in r.context["kpis"] if k["key"] == "alerts")
        self.assertEqual((alerts["value"], alerts["sub"]), ("2", "2 critical"))
        self.assertTrue(alerts["spark"])
        health = next(k for k in r.context["kpis"] if k["key"] == "health")
        self.assertEqual(health["value"], "33%")        # URL and TCP checks only: Wiki is the one healthy of three
        self.assertContains(r, 'data-kpi="health"')

    def test_needs_attention_rows_have_severity_source_and_actions(self):
        r = self.get(self.root)
        rows = {p["name"]: p for p in r.context["problems"]}
        self.assertEqual(rows["Portal"]["tone"], "crit")
        self.assertEqual(rows["Portal"]["icon"], "link")
        self.assertEqual(rows["Oracle (db.example.com:1521)"]["tone"], "warn")
        self.assertEqual(rows["DiskFullMumbai"]["ack_url"], reverse("telemetry:alert_ack", args=[self.alert_m.pk]))
        self.assertContains(r, 'data-post="%s"' % reverse("telemetry:alert_ack", args=[self.alert_m.pk]))

    def test_acknowledge_button_needs_operate_on_the_alert_site(self):
        r = self.get(self.viewer)
        self.assertNotContains(r, "Acknowledge</button>")
        r = self.get(self.mum_op)
        self.assertContains(r, reverse("telemetry:alert_ack", args=[self.alert_m.pk]))
        self.assertNotContains(r, reverse("telemetry:alert_ack", args=[self.alert_d.pk]))
        self.alert_m.acknowledged_at = timezone.now()
        self.alert_m.save()
        self.assertNotContains(self.get(self.mum_op), reverse("telemetry:alert_ack", args=[self.alert_m.pk]))

    def test_range_switch_validates_and_marks_the_active_range(self):
        self.assertEqual(self.get(self.root, range="24h").context["range"], "24h")
        self.assertEqual(self.get(self.root, range="9999").context["range"], "1h")
        self.assertEqual(self.get(self.root, range="<script>").context["range"], "1h")
        r = self.get(self.root, range="6h", site="Mumbai")
        self.assertContains(r, 'site=Mumbai&amp;range=15m')   # the other links keep the site filter

    def test_heatmap_collapses_sites_without_data(self):
        r = self.get(self.root)
        self.assertEqual(r.context["heat"]["empty"], [])
        Site.objects.create(name="Empty Site", code="e1")
        r = self.get(self.root)
        self.assertEqual(r.context["heat"]["empty"], ["Empty Site"])
        self.assertContains(r, "1 site without data")

    def test_legacy_backend_is_one_muted_line_unless_connected(self):
        r = self.get(self.root)
        self.assertContains(r, "Legacy backend: not connected")
        self.assertNotContains(r, "data-backend-summary")
        self.assertNotContains(r, "could not be loaded")
        with mock.patch("core.dashboard.legacy_backend_configured", return_value=True):
            r = self.get(self.root)
        self.assertContains(r, "data-backend-summary")
        self.assertNotContains(r, "Legacy backend: not connected")

    def test_the_legacy_card_loads_through_the_cached_summary_api_not_the_page(self):
        with mock.patch("core.backend_summary._fetch", side_effect=AssertionError("page must not fetch")):
            self.assertEqual(self.get(self.root).status_code, 200)


class ScopingTests(Base):
    def test_a_site_limited_user_sees_only_their_site_everywhere(self):
        r = self.get(self.viewer)
        text = r.content.decode()
        self.assertIn("Portal", text)
        for other in ("dr-fw-01", "DiskFullDR", "EOD late DR"):
            self.assertNotIn(other, text, other)
        self.assertEqual({k["key"]: k["value"] for k in r.context["kpis"]}["alerts"], "1")
        self.assertEqual([x["site"] for x in r.context["heat"]["rows"]], ["Mumbai"])

    def test_the_site_picker_narrows_problems_kpis_and_events(self):
        r = self.get(self.root, site="DR Site")
        text = r.content.decode()
        self.assertIn("dr-fw-01", text)
        self.assertNotIn("Portal", text)
        self.assertNotIn("BOD ok-ish Mumbai", text)
        self.assertEqual({k["key"]: k["value"] for k in r.context["kpis"]}["alerts"], "1")
        self.assertEqual(r.context["banner"]["scope"], "DR Site")

    def test_a_site_the_user_cannot_see_shows_nothing_not_everything(self):
        r = self.get(self.viewer, site="DR Site")
        self.assertEqual(r.context["problems"], [])
        self.assertEqual(r.context["problem_total"], 0)
        self.assertNotContains(r, "dr-fw-01")
        self.assertEqual(r.context["banner"]["tone"], "ok")

    def test_json_refresh_is_scoped_like_the_page(self):
        self.client.force_login(self.viewer)
        d = self.client.get(reverse("dashboard_data")).json()
        text = str(d)
        self.assertIn("Portal", text)
        for other in ("dr-fw-01", "DiskFullDR"):
            self.assertNotIn(other, text)

    def test_json_refresh_requires_sign_in(self):
        self.client.logout()
        r = self.client.get(reverse("dashboard_data"))
        self.assertEqual(r.status_code, 302)

    def test_spark_cache_is_per_user(self):
        self.client.force_login(self.root)
        self.client.get(reverse("dashboard_data"))
        self.client.force_login(self.viewer)
        d = self.client.get(reverse("dashboard_data")).json()
        self.assertEqual(d["spark"]["alerts"][-1], 1)   # not the admin's 2


class RefreshTests(Base):
    def test_payload_shape_and_sparkline_history(self):
        from servicedesk import services
        services.create_ticket(user=self.root, kind="incident", title="Outage", priority="P1", site="Mumbai")
        UrlResult.objects.create(url_check=UrlCheck.objects.get(name="Wiki"), checked_at=timezone.now() - timedelta(minutes=5), status="online")
        UrlResult.objects.create(url_check=UrlCheck.objects.get(name="Portal"), checked_at=timezone.now() - timedelta(minutes=5), status="down")
        self.client.force_login(self.root)
        d = self.client.get(reverse("dashboard_data"), {"range": "1h"}).json()
        for key in ("range", "updated", "banner", "kpis", "problems", "problem_total", "events", "fleet", "spark"):
            self.assertIn(key, d)
        spark = d["spark"]
        self.assertEqual(len(spark["alerts"]), dashboard.SPARK_POINTS)
        self.assertEqual(spark["alerts"][-1], 2)
        self.assertEqual(spark["alerts"][0], 0)
        self.assertEqual(spark["incidents"][-1], 1)
        self.assertIn(50.0, spark["health"])                 # one of two checks online in the bucket

    def test_a_resolved_alert_stops_counting_after_it_ended(self):
        self.alert_m.status = "resolved"
        self.alert_m.ends_at = timezone.now() - timedelta(minutes=10)
        self.alert_m.save()
        self.client.force_login(self.root)
        d = self.client.get(reverse("dashboard_data"), {"range": "1h"}).json()
        series = d["spark"]["alerts"]
        self.assertEqual(series[-1], 1)                      # only the DR alert still fires
        self.assertGreaterEqual(max(series), 2)              # but two were open earlier in the hour

    def test_series_without_history_are_left_out(self):
        UrlCheck.objects.all().delete()
        TcpCheck.objects.all().delete()
        self.client.force_login(self.root)
        d = self.client.get(reverse("dashboard_data")).json()
        self.assertNotIn("health", d["spark"])

    def test_no_store(self):
        self.client.force_login(self.root)
        self.assertEqual(self.client.get(reverse("dashboard_data"))["Cache-Control"], "no-store")
        self.assertEqual(self.client.get(reverse("dashboard_data"), {"only": "spark"})["Cache-Control"], "no-store")

    def test_only_spark_returns_the_history_alone_and_costs_less_than_a_full_build(self):
        """The page calls this right after it renders (it already holds every figure): no second build of the page."""
        self.client.force_login(self.root)
        url = reverse("dashboard_data")
        self.client.get(url)                                  # warm session and content types
        cache.clear()
        with CaptureQueriesContext(connection) as full:
            self.client.get(url, {"range": "1h"})
        cache.clear()
        with CaptureQueriesContext(connection) as light:
            d = self.client.get(url, {"range": "1h", "only": "spark"}).json()
        self.assertEqual(set(d), {"range", "updated", "spark"})
        self.assertEqual(d["range"], "1h")
        self.assertEqual(len(d["spark"]["alerts"]), dashboard.SPARK_POINTS)
        self.assertLess(len(light), len(full) - 10)

    def test_only_spark_is_scoped_like_the_page(self):
        self.client.force_login(self.viewer)
        d = self.client.get(reverse("dashboard_data"), {"only": "spark"}).json()
        self.assertEqual(d["spark"]["alerts"][-1], 1)         # Mumbai's alert, not DR's
        self.client.force_login(self.root)
        d = self.client.get(reverse("dashboard_data"), {"only": "spark", "site": "DR Site"}).json()
        self.assertEqual(d["spark"]["alerts"][-1], 1)         # the site picker narrows it too
        d = self.client.get(reverse("dashboard_data"), {"only": "spark", "site": "Nowhere"}).json()
        self.assertEqual(d["spark"]["alerts"][-1], 0)         # a site that does not exist shows nothing, not everything

    def test_only_spark_still_needs_a_sign_in_and_validates_the_range(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("dashboard_data"), {"only": "spark"}).status_code, 302)
        self.client.force_login(self.root)
        self.assertEqual(self.client.get(reverse("dashboard_data"), {"only": "spark", "range": "9y"}).json()["range"], "1h")

    def test_the_page_asks_for_history_only_on_first_load_and_a_range_switch(self):
        js = (Path(__file__).resolve().parent / "static" / "linkedeye" / "pages" / "dashboard.js").read_text()
        self.assertIn("searchParams.set('only', 'spark')", js)
        self.assertIn("refresh(true);\n  schedule();", js)            # first load: history only
        self.assertIn("refreshBtn.addEventListener('click', () => refresh(false))", js)   # never the click event as the flag


class FleetPanelTests(Base):
    def _warm(self, hosts):
        for k in dashboard.FLEET_CACHE_KEYS:
            cache.set(k, [] if "os" not in k else ({}, ""), 60)
        return mock.patch("telemetry.fleet.host_states", return_value={"hosts": hosts, "errors": {}})

    def test_cold_cache_never_calls_prometheus(self):
        with mock.patch("core.prometheus._get", side_effect=AssertionError("no Prometheus call on a cold dashboard")), \
                mock.patch("telemetry.hosts.fetch_hosts", side_effect=AssertionError("no fetch")):
            r = self.get(self.root)
            self.assertEqual(r.status_code, 200)
            d = self.client.get(reverse("dashboard_data")).json()
        self.assertFalse(r.context["fleet"]["loaded"])
        self.assertFalse(d["fleet"]["loaded"])
        hosts = next(k for k in r.context["kpis"] if k["key"] == "hosts")
        self.assertEqual((hosts["value"], hosts["sub"]), ("–", "Open Fleet to load"))
        self.assertContains(r, "Open Fleet")

    def test_warm_cache_shows_states_and_top_hosts(self):
        hosts = [
            {"instance": "a:9100", "name": "alpha", "state": "live", "cpu": 91.0, "memory": 40.0, "url": "/monitoring/infrastructure/a:9100/"},
            {"instance": "b:9100", "name": "bravo", "state": "live", "cpu": 20.0, "memory": 85.0, "url": "/monitoring/infrastructure/b:9100/"},
            {"instance": "c:9100", "name": "charlie", "state": "down", "cpu": None, "memory": None, "url": ""},
        ]
        with self._warm(hosts), mock.patch("telemetry.fleet.scope_hosts", side_effect=lambda u, rows: rows):
            r = self.get(self.root)
        f = r.context["fleet"]
        self.assertTrue(f["loaded"])
        self.assertEqual((f["total"], f["live"]), (3, 2))
        self.assertEqual([h["name"] for h in f["top_cpu"]], ["alpha", "bravo"])
        self.assertEqual(f["top_cpu"][0]["tone"], "crit")
        self.assertEqual(f["top_memory"][0]["name"], "bravo")
        self.assertEqual(next(s for s in f["segments"] if s["key"] == "down")["href"], reverse("telemetry:fleet") + "?state=down")
        hostcard = next(k for k in r.context["kpis"] if k["key"] == "hosts")
        self.assertEqual((hostcard["value"], hostcard["tone"]), ("2/3", "crit"))
        self.assertContains(r, "dash-statebar")

    def test_the_fleet_panel_links_to_the_overview_charts_once_prometheus_is_connected(self):
        overview_url = reverse("telemetry:overview")
        with mock.patch("core.prometheus.configured", return_value=True):
            r = self.get(self.root)
        self.assertEqual(r.context["fleet"]["overview_href"], overview_url)
        self.assertContains(r, f'<a class="view-all" href="{overview_url}">Charts</a>', html=False)
        with mock.patch("core.prometheus.configured", return_value=False):
            r = self.get(self.root)
        self.assertNotContains(r, f'href="{overview_url}">Charts</a>')   # nothing to chart: the panel offers setup instead


class EmptyInstallTests(TestCase):
    def setUp(self):
        cache.clear()
        self.root = User.objects.create_superuser("root", "r@example.com", PW)
        self.viewer = User.objects.create_user("viewer", "v@example.com", PW)

    def test_new_install_shows_the_get_started_checklist_to_admins(self):
        self.client.force_login(self.root)
        r = self.client.get(reverse("dashboard"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Get started")
        steps = [s["label"] for s in r.context["start"]["steps"]]
        self.assertIn("Add a site", steps)
        self.assertIn("Onboard servers and devices", steps)
        self.assertIn("Import your inventory", steps)
        self.assertTrue(any("Prometheus" in s for s in steps))
        self.assertContains(r, reverse("onboarding:index"))
        self.assertContains(r, "Nothing is monitored yet")        # not a false "all clear"
        self.assertEqual(r.context["banner"]["tone"], "info")
        self.assertNotContains(r, "dash-heat-table")              # nothing to chart: the checklist leads
        self.assertNotContains(r, "dash-svc")
        self.assertNotContains(r, "Everything monitored is healthy")   # nothing is monitored: no all-clear in the table either
        self.assertContains(r, "nothing can need attention")

    def test_non_admins_do_not_get_the_checklist(self):
        self.client.force_login(self.viewer)
        r = self.client.get(reverse("dashboard"))
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.context["start"])
        self.assertNotContains(r, "Get started")

    def test_checklist_goes_away_once_something_is_monitored(self):
        UrlCheck.objects.create(name="Portal", url="https://portal.example.com/", site="", last_status="online")
        self.client.force_login(self.root)
        self.assertIsNone(self.client.get(reverse("dashboard")).context["start"])


class QueryBudgetTests(Base):
    """The dashboard's query count must not grow with the amount of data (or with the number of problems)."""

    def count(self, user):
        cache.clear()
        self.client.force_login(user)
        self.client.get(reverse("dashboard"))      # warm session, content types, template caches
        cache.clear()
        with CaptureQueriesContext(connection) as q:
            self.client.get(reverse("dashboard"))
        return len(q)

    def test_count_is_flat_in_the_amount_of_data(self):
        before = self.count(self.root)
        for i in range(25):
            UrlCheck.objects.create(name=f"bulk{i}", url=f"https://b{i}.example.com/", site="Mumbai", last_status="down")
            Alert.objects.create(fingerprint=f"bulk{i}", alertname=f"A{i}", severity="critical", site="DR Site", starts_at=timezone.now())
        self.assertEqual(self.count(self.root), before)
        self.assertLessEqual(before, 45)           # the demo database measured 39 before the redesign

    def test_no_prometheus_and_no_network_on_render(self):
        with mock.patch("core.prometheus._get", side_effect=AssertionError("Prometheus")), \
                mock.patch("core.backend_summary._fetch", side_effect=AssertionError("backend")):
            self.assertEqual(self.get(self.root).status_code, 200)

    def test_the_sidebar_badges_reuse_the_dashboards_numbers(self):
        self.client.force_login(self.root)
        r = self.client.get(reverse("dashboard"))
        badges = {e["label"]: e["badge"] for e in r.context["nav"] if e["badge"]}
        self.assertEqual(badges["Alert Manager"], 2)
        self.assertEqual(badges["Checks"], 1)        # Portal; the degraded TCP check is not "down"


class FigureCardStyleTests(SimpleTestCase):
    """Art-direction regressions in dashboard.css: a thick left stripe on a rounded card draws a curved wedge."""

    css = (Path(__file__).resolve().parent / "static" / "linkedeye" / "pages" / "dashboard.css").read_text()

    def test_cards_and_banner_have_no_left_stripe(self):
        self.assertNotIn("border-left: 4px", self.css)
        self.assertNotIn("inset 3px 0 0", self.css)

    def test_the_now_marker_never_covers_a_validation_label(self):
        # labels sit above the marker line, the "now" time chip above both
        self.assertRegex(self.css, r"\.dash-win \{ z-index: 1; \}")
        self.assertRegex(self.css, r"\.dash-now span \{ z-index: 2;")

    def test_validation_labels_are_never_clipped(self):
        self.assertIn("min-width: max-content", self.css)
        self.assertNotIn("min-width: 9%", self.css)

    def test_a_cold_fleet_card_does_not_stretch_to_the_heat_map(self):
        self.assertIn(".dash-row-fleet:has(.dash-cold) { align-items: start; }", self.css)
