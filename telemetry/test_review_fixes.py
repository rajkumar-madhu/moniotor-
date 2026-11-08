"""Regression tests for the telemetry-probes review findings. Prometheus, Nagios and DNS are always mocked."""
import importlib
import json
import time
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import OperationalError, connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from core.models import AuditEvent, Membership, Site

from . import alerts as alert_svc, discovery, explorer, fleet, hosts as hosts_svc, nagios
from .models import Alert, DiscoveredTarget

PROM = "http://prom.test:9090"
TOKEN = "k" * 32


def _site_user(name, **roles):
    """User with one membership per {site name: role}."""
    u = User.objects.create_user(name, password="pw")
    for site_name, role in roles.items():
        site, _ = Site.objects.get_or_create(name=site_name, defaults={"code": site_name.lower()[:10]})
        Membership.objects.create(user=u, site=site, role=role)
    return u


def _host(instance, job="node"):
    return {"instance": instance, "job": job, "up": 1.0, "cpu": 10.0, "memory": 20.0, "disk": 30.0, "load1": 0.1,
            "cores": 2.0, "rx": 1.0, "tx": 1.0, "uptime": 1000.0, "health": "healthy"}


# ----------------------------------------------------------------------------- 1: settings saved in the console
class ConsoleSettingsTests(TestCase):
    def setUp(self):
        from administration import integration_config as ic
        from administration.models import IntegrationSetting
        self.ic, self.Setting = ic, IntegrationSetting
        cache.clear()
        self.addCleanup(cache.clear)

    def save(self, slug, **values):
        self.Setting.objects.update_or_create(slug=slug, defaults={"values": values})
        self.ic.invalidate_cache()

    @override_settings(ALERTMANAGER_WEBHOOK_TOKEN="")
    def test_webhook_accepts_the_token_saved_in_the_console(self):
        self.save("alertmanager", token=TOKEN)
        body = json.dumps({"version": "4", "alerts": []})
        r = self.client.post("/api/v1/alertmanager/", body, content_type="application/json",
                             HTTP_AUTHORIZATION="Bearer " + TOKEN)
        self.assertEqual(r.status_code, 200, r.content)
        r = self.client.post("/api/v1/alertmanager/", body, content_type="application/json",
                             HTTP_AUTHORIZATION="Bearer wrong")
        self.assertEqual(r.status_code, 401)

    @override_settings(ALERTMANAGER_WEBHOOK_TOKEN="")
    def test_webhook_is_still_disabled_without_any_token(self):
        r = self.client.post("/api/v1/alertmanager/", "{}", content_type="application/json")
        self.assertEqual(r.status_code, 503)

    @override_settings(NAGIOS_URL="", NAGIOS_USER="", NAGIOS_PASSWORD="", NAGIOS_TIMEOUT=10.0)
    def test_nagios_reads_url_credentials_and_timeout_from_the_console(self):
        self.assertFalse(nagios.configured())
        self.save("nagios", url="https://nagios.example/nagios", user="nagiosadmin", password="s3cret", timeout="25")
        self.assertTrue(nagios.configured())
        self.assertEqual(nagios.base_url(), "https://nagios.example/nagios")
        self.assertEqual(nagios.timeout(), 25.0)
        sent = {}

        def fake_urlopen(req, timeout):
            sent["auth"], sent["timeout"], sent["url"] = req.get_header("Authorization"), timeout, req.full_url
            raise OSError("stop here")
        with mock.patch.object(nagios, "_urlopen", fake_urlopen):
            with self.assertRaises(nagios.NagiosError):
                nagios.fetch_status(use_cache=False)
        self.assertTrue(sent["auth"].startswith("Basic "))
        self.assertEqual(sent["timeout"], 25.0)
        self.assertTrue(sent["url"].startswith("https://nagios.example/nagios/cgi-bin/statusjson.cgi"))

    @override_settings(ALERTMANAGER_WEBHOOK_TOKEN="")
    def test_alert_page_knows_the_webhook_token_is_set(self):
        self.save("alertmanager", token=TOKEN)
        self.client.force_login(User.objects.create_superuser("root", "r@example.com", "pw"))
        r = self.client.get(reverse("telemetry:alertmanager"))
        self.assertTrue(r.context["token_set"])


# ----------------------------------------------------------------------------- 2, AZ-02, 13: host site scoping
@override_settings(PROMETHEUS_URL=PROM)
class HostSiteScopeTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.viewer_a = _site_user("va", A="viewer")
        self.viewer_b = _site_user("vb", B="viewer")
        self.admin = User.objects.create_superuser("root", "r@example.com", "pw")
        DiscoveredTarget.objects.create(instance="bhost:9100", job="node", kind="node", site="B")
        DiscoveredTarget.objects.create(instance="ahost:9100", job="node", kind="node", site="A")
        DiscoveredTarget.objects.create(instance="gglobal:9100", job="node", kind="node", site="All Sites")
        self.rows = [_host(i) for i in ("bhost:9100", "ahost:9100", "gglobal:9100", "unknown:9100")]

    def get(self, user, url):
        self.client.force_login(user)
        with mock.patch("telemetry.hosts.fetch_hosts", return_value=self.rows), \
                mock.patch("telemetry.hosts.cpu_sparklines", return_value={}), \
                mock.patch("telemetry.fleet.node_target_map", return_value=({}, "")), \
                mock.patch("telemetry.hosts.fetch_host", return_value=dict(_host("bhost:9100"), series={})), \
                mock.patch("telemetry.hosts_full.fetch_host_full", return_value={}), \
                mock.patch("telemetry.targets.fetch_targets", return_value=[
                    {"instance": r["instance"], "job": "node", "url": "", "health": "up", "last_scrape": None,
                     "duration": None, "error": "", "labels": []} for r in self.rows]), \
                mock.patch("telemetry.targets.server_stats", return_value=None):
            return self.client.get(url)

    def test_infrastructure_hides_ci_less_hosts_of_other_sites(self):
        r = self.get(self.viewer_a, "/monitoring/infrastructure/?refresh=1")
        shown = {h["instance"] for h in r.context["page"]}
        self.assertEqual(shown, {"ahost:9100", "gglobal:9100", "unknown:9100"})
        csv = self.get(self.viewer_a, "/monitoring/infrastructure/?format=csv").content.decode()
        self.assertNotIn("bhost", csv)
        self.assertIn("ahost", csv)

    def test_host_pages_404_for_ci_less_host_of_another_site(self):
        for name in ("host_detail", "host_metrics", "host_metrics_json"):
            r = self.get(self.viewer_a, reverse(f"telemetry:{name}", args=["bhost:9100"]))
            self.assertEqual(r.status_code, 404, name)
        self.assertEqual(self.get(self.viewer_b, reverse("telemetry:host_detail", args=["bhost:9100"])).status_code, 200)
        self.assertEqual(self.get(self.admin, reverse("telemetry:host_detail", args=["bhost:9100"])).status_code, 200)

    def test_prometheus_targets_page_hides_it_too(self):
        r = self.get(self.viewer_a, "/monitoring/prometheus/?refresh=1")
        self.assertNotContains(r, "bhost:9100")
        self.assertContains(r, "ahost:9100")

    def test_fleet_scoping_uses_the_same_rule_for_hosts_and_targets(self):
        targets = [{"instance": r["instance"]} for r in self.rows]
        got = {t["instance"] for t in fleet.scope_targets(self.viewer_a, targets)}
        self.assertEqual(got, {"ahost:9100", "gglobal:9100", "unknown:9100"})
        hosts = [{"instance": r["instance"], "site": ""} for r in self.rows]   # a stale/absent h["site"] changes nothing
        got = {h["instance"] for h in fleet.scope_hosts(self.viewer_a, hosts)}
        self.assertEqual(got, {"ahost:9100", "gglobal:9100", "unknown:9100"})
        self.assertEqual(len(fleet.scope_targets(self.admin, targets)), 4)

    def test_network_device_site_is_used_when_there_is_no_ci_or_discovery_row(self):
        from network.models import Device
        Device.objects.create(name="sw-b", device_type="switch", mgmt_ip="10.9.0.1", site="B",
                              prometheus_instance="10.9.0.1")
        self.assertEqual(hosts_svc.instance_sites(["10.9.0.1"]), {"10.9.0.1": "B"})
        self.assertEqual(fleet.scope_targets(self.viewer_a, [{"instance": "10.9.0.1"}]), [])

    def test_ci_site_wins_over_discovery(self):
        from cmdb.models import ConfigurationItem
        ConfigurationItem.objects.create(name="bhost", site="A", prometheus_instance="bhost:9100")
        self.assertEqual(hosts_svc.instance_sites(["bhost:9100"]), {"bhost:9100": "A"})

    def test_lookup_failure_fails_closed_for_site_limited_users(self):
        with mock.patch.object(hosts_svc, "_ci_sites", side_effect=OperationalError("no such table")):
            r = self.get(self.viewer_a, "/monitoring/infrastructure/?refresh=1")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(list(r.context["page"]), [])
            self.assertEqual(self.get(self.viewer_a, reverse("telemetry:host_detail", args=["ahost:9100"])).status_code, 404)
            self.assertEqual(fleet.scope_targets(self.viewer_a, [{"instance": "ahost:9100"}]), [])
            self.assertEqual(fleet.scope_hosts(self.viewer_a, [{"instance": "ahost:9100", "site": "A"}]), [])
            # a tenant-wide user needs no scoping and keeps working
            r = self.get(self.admin, "/monitoring/infrastructure/?refresh=1")
            self.assertEqual(len(list(r.context["page"])), 4)


# ----------------------------------------------------------------------------- 3: Nagios page scoping
NAGIOS_STATUS = {
    "hosts": [
        {"name": "siteA-host", "state": "up", "output": "OK 10.1.1.1", "last_check": None, "last_change": None,
         "acknowledged": False, "downtime": False, "flapping": False, "attempt": 1, "max_attempts": 3, "hard": True},
        {"name": "siteB-host", "state": "down", "output": "secret 10.9.9.9", "last_check": None, "last_change": None,
         "acknowledged": False, "downtime": False, "flapping": False, "attempt": 1, "max_attempts": 3, "hard": True},
        {"name": "orphan-host", "state": "down", "output": "orphan 10.7.7.7", "last_check": None, "last_change": None,
         "acknowledged": False, "downtime": False, "flapping": False, "attempt": 1, "max_attempts": 3, "hard": True},
    ],
    "services": [
        {"host": "siteA-host", "service": "PING", "state": "ok", "output": "fine", "last_check": None,
         "last_change": None, "acknowledged": False, "downtime": False, "flapping": False, "attempt": 1,
         "max_attempts": 3, "hard": True},
        {"host": "siteB-host", "service": "DISK", "state": "critical", "output": "B-disk-secret", "last_check": None,
         "last_change": None, "acknowledged": False, "downtime": False, "flapping": False, "attempt": 1,
         "max_attempts": 3, "hard": True},
    ],
    "query_time": None, "program": "4.5",
}


class NagiosSiteScopeTests(TestCase):
    def setUp(self):
        from cmdb.models import ConfigurationItem
        cache.clear()
        ConfigurationItem.objects.create(name="siteA-host", site="A")
        ConfigurationItem.objects.create(name="siteB-host", site="B")
        self.viewer_a = _site_user("va", A="viewer")
        self.admin = User.objects.create_superuser("root", "r@example.com", "pw")

    def get(self, user, qs):
        self.client.force_login(user)
        with mock.patch.object(nagios, "configured", return_value=True), \
                mock.patch.object(nagios, "base_url", return_value="https://n.example/nagios"), \
                mock.patch.object(nagios, "fetch_status", return_value=NAGIOS_STATUS):
            return self.client.get("/monitoring/nagios/" + qs)

    def test_site_limited_viewer_sees_only_mapped_hosts_of_their_sites(self):
        csv = self.get(self.viewer_a, "?tab=hosts&format=csv").content.decode()
        self.assertIn("siteA-host", csv)
        for secret in ("siteB-host", "10.9.9.9", "orphan-host"):
            self.assertNotIn(secret, csv)
        csv = self.get(self.viewer_a, "?tab=services&format=csv").content.decode()
        self.assertIn("PING", csv)
        self.assertNotIn("B-disk-secret", csv)

    def test_page_summary_counts_only_visible_objects(self):
        r = self.get(self.viewer_a, "?tab=hosts")
        self.assertEqual(r.context["summary"]["hosts"], 1)
        self.assertEqual(r.context["summary"]["services"], 1)
        self.assertNotContains(r, "10.9.9.9")
        self.assertNotContains(r, "B-disk-secret")

    def test_tenant_wide_user_sees_everything(self):
        csv = self.get(self.admin, "?tab=hosts&format=csv").content.decode()
        for name in ("siteA-host", "siteB-host", "orphan-host"):
            self.assertIn(name, csv)


# ----------------------------------------------------------------------------- 4: acknowledging global alerts
class GlobalAlertAckTests(TestCase):
    def setUp(self):
        self.user = _site_user("mixed", A="viewer", B="operator")
        self.client.force_login(self.user)
        self.global_alert = Alert.objects.create(fingerprint="g1", alertname="Everywhere", site="")
        self.b_alert = Alert.objects.create(fingerprint="b1", alertname="OnlyB", site="B")
        self.a_alert = Alert.objects.create(fingerprint="a1", alertname="OnlyA", site="A")

    def test_one_site_operator_cannot_acknowledge_a_global_alert(self):
        for site in ("", "All Sites"):
            Alert.objects.filter(pk=self.global_alert.pk).update(site=site)
            r = self.client.post(reverse("telemetry:alert_ack", args=[self.global_alert.pk]))
            self.assertEqual(r.status_code, 302)
            self.global_alert.refresh_from_db()
            self.assertIsNone(self.global_alert.acknowledged_at, site)
        self.assertFalse(AuditEvent.objects.filter(action="acknowledge").exists())

    def test_site_operator_still_acknowledges_alerts_of_their_site_only(self):
        self.client.post(reverse("telemetry:alert_ack", args=[self.b_alert.pk]))
        self.client.post(reverse("telemetry:alert_ack", args=[self.a_alert.pk]))
        self.b_alert.refresh_from_db()
        self.a_alert.refresh_from_db()
        self.assertIsNotNone(self.b_alert.acknowledged_at)
        self.assertIsNone(self.a_alert.acknowledged_at)       # viewer on A only

    def test_tenant_wide_operator_acknowledges_global_alert(self):
        self.client.force_login(User.objects.create_user("wide", password="pw", is_staff=True))
        # no memberships: the default role applies everywhere; make that user an operator through a global membership
        Membership.objects.create(user=User.objects.get(username="wide"), site=None, role="operator")
        self.client.post(reverse("telemetry:alert_ack", args=[self.global_alert.pk]))
        self.global_alert.refresh_from_db()
        self.assertIsNotNone(self.global_alert.acknowledged_at)


# ----------------------------------------------------------------------------- 5: NUL characters
@override_settings(INCIDENT_AUTO_CREATE=False, NOTIFY_WEBHOOK_URLS=[], NOTIFY_EMAILS=[])
class NulCharacterTests(TestCase):
    def test_nul_is_stripped_everywhere_before_storing(self):
        raw = {"status": "firing", "fingerprint": "fp\x00x",
               "labels": {"alertname": "X\x00Y", "severity": "crit\x00ical", "instance": "h\x00:9100", "job": "j\x00",
                          "k\x00ey": "v\x00al"},
               "annotations": {"summary": "bad\x00byte", "description": "d\x00esc"},
               "generatorURL": "http://x/\x00"}
        payload = {"receiver": "r\x00", "alerts": [raw]}
        alert_svc.ingest(payload, [raw])
        a = Alert.objects.get()
        self.assertEqual(a.fingerprint, "fpx")
        blob = json.dumps([a.alertname, a.severity, a.instance, a.job, a.summary, a.description, a.receiver,
                           a.generator_url, a.labels, a.annotations])
        self.assertNotIn("\\u0000", blob)
        self.assertNotIn("\x00", blob)
        self.assertEqual(a.alertname, "XY")
        self.assertEqual(a.labels["key"], "val")
        self.assertEqual(a.summary, "badbyte")

    def test_derived_fingerprint_ignores_nul(self):
        a = alert_svc.fingerprint_of({}, {"alertname": "X\x00"})
        b = alert_svc.fingerprint_of({}, {"alertname": "X"})
        self.assertEqual(a, b)


# ----------------------------------------------------------------------------- 7: huge step
class ExplorerStepTests(TestCase):
    def test_parse_duration_rejects_absurd_numbers(self):
        self.assertIsNone(explorer.parse_duration("9" * 5000))
        self.assertIsNone(explorer.parse_duration("9" * 30 + "s"))
        self.assertEqual(explorer.parse_duration("90"), 90)
        self.assertEqual(explorer.parse_duration("5m"), 300)

    @override_settings(PROMETHEUS_URL=PROM)
    def test_view_answers_with_a_message_not_a_500(self):
        self.client.force_login(User.objects.create_superuser("root", "r@example.com", "pw"))
        r = self.client.get("/monitoring/metrics-explorer/", {"expr": "up", "step": "9" * 5000})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Step must be a duration")


# ----------------------------------------------------------------------------- 8: discovery cost per pass
def _targets(n):
    return [{"labels": {"instance": f"h{i:04d}:9100", "job": "node"}, "discoveredLabels": {}, "health": "up",
             "scrapePool": "node", "lastError": "", "scrapeUrl": f"http://h{i:04d}:9100/metrics",
             "lastScrape": "2026-11-07T08:00:00Z"} for i in range(n)]


@override_settings(PROMETHEUS_URL=PROM)
class DiscoveryQueryCountTests(TestCase):
    def run_pass(self, raw):
        with mock.patch("telemetry.promapi.targets", return_value=raw), \
                mock.patch("telemetry.promapi.query", return_value=[]):
            with CaptureQueriesContext(connection) as ctx:
                row = discovery.run()
        return row, len(ctx)

    def test_unchanged_second_pass_does_not_cost_queries_per_target(self):
        raw = _targets(60)
        first, _n = self.run_pass(raw)
        self.assertEqual(first.targets, 60)
        second, queries = self.run_pass(raw)
        self.assertTrue(second.ok, second.error)
        self.assertEqual(DiscoveredTarget.objects.count(), 60)
        self.assertLess(queries, 60, f"{queries} queries for 60 unchanged targets")

    def test_links_and_state_are_still_applied(self):
        from cmdb.models import ConfigurationItem
        raw = _targets(3)
        self.run_pass(raw)
        ci = ConfigurationItem.objects.create(name="h0001", prometheus_instance="h0001:9100", site="All Sites")
        self.run_pass(raw)
        row = DiscoveredTarget.objects.get(instance="h0001:9100")
        self.assertEqual(row.ci_id, ci.pk)
        self.assertEqual(row.state, DiscoveredTarget.LINKED)
        ci.delete()
        self.run_pass(raw)
        row.refresh_from_db()
        self.assertIsNone(row.ci_id)
        self.assertEqual(row.state, DiscoveredTarget.NEW)
        self.assertEqual(DiscoveredTarget.objects.get(instance="h0002:9100").state, DiscoveredTarget.NEW)

    def test_target_that_disappears_is_marked_missing(self):
        self.run_pass(_targets(3))
        row, _n = self.run_pass(_targets(2))
        self.assertEqual(row.missing, 1)
        self.assertIsNotNone(DiscoveredTarget.objects.get(instance="h0002:9100").missing_since)


# ----------------------------------------------------------------------------- 10: fleet alert counts
class FleetOccurrenceTests(TestCase):
    def test_a_flapping_alert_counts_once_inside_the_window(self):
        now = timezone.now()
        a = Alert.objects.create(fingerprint="f", alertname="Flap", instance="db:9100", severity="warning",
                                 fire_count=40, last_received_at=now, starts_at=now - timedelta(minutes=5))
        start = now - timedelta(hours=1)
        self.assertEqual(fleet.top_alerts([a], start, now=now)[0]["occurrences"], 1)
        self.assertEqual(fleet.host_alert_leaders([a], [], start, now=now)[0]["total"], 1)
        self.assertEqual(fleet.top_alerts([a], now - timedelta(days=7), now=now)[0]["occurrences"], 1)


# ----------------------------------------------------------------------------- 11: lost resolved notification
@override_settings(INCIDENT_AUTO_CREATE=False, NOTIFY_WEBHOOK_URLS=[], NOTIFY_EMAILS=[], TELEMETRY_ALERT_STALE_HOURS=48)
class StaleFiringTests(TestCase):
    def make(self, fp, hours_ago, **kw):
        now = timezone.now()
        return Alert.objects.create(fingerprint=fp, alertname="Lost", instance="h:9100", severity="critical",
                                    status=Alert.FIRING, starts_at=now - timedelta(hours=hours_ago + 1),
                                    last_received_at=now - timedelta(hours=hours_ago), fire_count=1, **kw)

    def test_firing_alert_without_renotification_is_resolved_as_stale(self):
        from core.models import Event
        old, fresh = self.make("old", 72), self.make("fresh", 2)
        n = alert_svc.expire_stale()
        self.assertEqual(n, 1)
        old.refresh_from_db()
        fresh.refresh_from_db()
        self.assertEqual(old.status, Alert.RESOLVED)
        self.assertEqual(old.ends_at, old.last_received_at)
        self.assertEqual(fresh.status, Alert.FIRING)
        ev = Event.objects.get(fingerprint="am:old")
        self.assertEqual(ev.severity, "ok")
        self.assertEqual(ev.data["reason"], "stale")

    @override_settings(TELEMETRY_ALERT_STALE_HOURS=0)
    def test_zero_disables_expiry(self):
        self.make("old", 500)
        self.assertEqual(alert_svc.expire_stale(), 0)
        self.assertEqual(Alert.objects.firing().count(), 1)

    def test_prune_job_runs_the_expiry(self):
        from . import jobs
        self.make("old", 72)
        self.assertIn("1 stale", jobs.prune_alerts())

    def test_alert_that_is_still_firing_reopens_on_its_next_notification(self):
        old = self.make("old", 72)
        alert_svc.expire_stale()
        raw = {"status": "firing", "fingerprint": "old", "startsAt": old.starts_at.isoformat(),
               "labels": {"alertname": "Lost", "severity": "critical", "instance": "h:9100"}}
        alert_svc.ingest({"alerts": [raw]}, [raw])
        old.refresh_from_db()
        self.assertEqual(old.status, Alert.FIRING)


# ----------------------------------------------------------------------------- 12: ?refresh=1 needs operate
@override_settings(PROMETHEUS_URL=PROM)
class RefreshGateTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.viewer = User.objects.create_user("viewer", password="pw")
        self.operator = User.objects.create_user("operator", password="pw", is_staff=True)
        Membership.objects.create(user=self.viewer, site=None, role="viewer")
        Membership.objects.create(user=self.operator, site=None, role="operator")

    def test_viewer_refresh_is_served_from_the_cache(self):
        self.client.force_login(self.viewer)
        with mock.patch("telemetry.hosts.fetch_hosts", return_value=[]) as fh, \
                mock.patch("telemetry.fleet.node_target_map", return_value=({}, "")) as nm, \
                mock.patch("telemetry.hosts.cpu_sparklines", return_value={}):
            self.client.get("/monitoring/infrastructure/?refresh=1")
        fh.assert_called_with(use_cache=True)
        nm.assert_called_with(use_cache=True)

    def test_operator_refresh_bypasses_the_cache(self):
        self.client.force_login(self.operator)
        with mock.patch("telemetry.hosts.fetch_hosts", return_value=[]) as fh, \
                mock.patch("telemetry.fleet.node_target_map", return_value=({}, "")), \
                mock.patch("telemetry.hosts.cpu_sparklines", return_value={}):
            self.client.get("/monitoring/infrastructure/?refresh=1")
        fh.assert_called_with(use_cache=False)

    def test_fleet_never_bypasses_the_series_count_cache(self):
        self.client.force_login(self.operator)
        with mock.patch("telemetry.fleet.host_states", return_value={"hosts": [], "errors": {}}) as hs, \
                mock.patch("telemetry.fleet.all_targets", return_value=([], "")), \
                mock.patch("telemetry.fleet.series_count", return_value=(5, "")) as sc:
            self.client.get("/monitoring/fleet/?refresh=1")
        hs.assert_called_with(use_cache=False, with_prometheus=True)
        sc.assert_called_with()

    def test_refresh_button_is_only_offered_to_operators(self):
        with mock.patch("telemetry.fleet.host_states", return_value={"hosts": [], "errors": {}}), \
                mock.patch("telemetry.fleet.all_targets", return_value=([], "")), \
                mock.patch("telemetry.fleet.series_count", return_value=(5, "")):
            self.client.force_login(self.viewer)
            self.assertNotContains(self.client.get("/monitoring/fleet/"), "refresh=1")
            self.client.force_login(self.operator)
            self.assertContains(self.client.get("/monitoring/fleet/"), "refresh=1")


# ----------------------------------------------------------------------------- 14: discovery list
class DiscoveryListTests(TestCase):
    def setUp(self):
        for i in range(45):
            DiscoveredTarget.objects.create(instance=f"n{i:02d}:9100", job="node", kind="node", site="A",
                                            details={"hostname": f"host-{i:02d}", "os": "Debian 12"})
        DiscoveredTarget.objects.create(instance="z:9100", job="node", kind="node", site="B",
                                        details={"hostname": "needle-b"})
        DiscoveredTarget.objects.create(instance="y:9100", job="node", kind="node", site="A",
                                        details={"hostname": "needle-a", "os": "Debian 12"})
        self.admin = User.objects.create_superuser("root", "r@example.com", "pw")
        self.viewer_a = _site_user("va", A="viewer")

    def test_only_the_visible_page_is_post_processed(self):
        view = importlib.import_module("telemetry.views.discovery")   # `telemetry.views.discovery` the name is the view function
        self.client.force_login(self.admin)
        with mock.patch.object(view, "hide_foreign_links", wraps=view.hide_foreign_links) as hide:
            r = self.client.get("/monitoring/discovery/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(sum(len(c.args[1]) for c in hide.call_args_list), 20)
        self.assertEqual(r.context["page"].paginator.count, 47)

    def test_search_runs_in_sql_and_matches_what_it_used_to(self):
        self.client.force_login(self.admin)
        for text, expected in (("needle", {"z:9100", "y:9100"}), ("HOST-07", {"n07:9100"}), ("debian", None),
                               ("y:91", {"y:9100"}), ("nothing-matches", set())):
            r = self.client.get("/monitoring/discovery/", {"q": text, "per": 100})
            got = {t.instance for t in r.context["page"]}
            if expected is not None:
                self.assertEqual(got, expected, text)
            else:
                self.assertEqual(len(got), 46, text)     # os is part of the searched summary

    def test_site_limited_search_does_not_reach_other_sites(self):
        self.client.force_login(self.viewer_a)
        r = self.client.get("/monitoring/discovery/", {"q": "needle"})
        self.assertEqual({t.instance for t in r.context["page"]}, {"y:9100"})

    def test_foreign_linked_record_names_are_not_searchable(self):
        from cmdb.models import ConfigurationItem
        ci = ConfigurationItem.objects.create(name="secret-ci-in-b", site="B")
        DiscoveredTarget.objects.filter(instance="y:9100").update(ci=ci)
        self.client.force_login(self.viewer_a)
        self.assertEqual(list(self.client.get("/monitoring/discovery/", {"q": "secret-ci"}).context["page"]), [])
        self.client.force_login(self.admin)
        self.assertEqual(len(list(self.client.get("/monitoring/discovery/", {"q": "secret-ci"}).context["page"])), 1)

    def test_csv_still_exports_every_matching_row(self):
        self.client.force_login(self.admin)
        body = self.client.get("/monitoring/discovery/", {"format": "csv"}).content.decode()
        self.assertEqual(len(body.strip().splitlines()), 48)
