"""Prometheus discovery tests. Every Prometheus call is mocked – nothing touches the network."""
from unittest import mock

from django.apps import apps
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core import prometheus
from core.models import AuditEvent, Event, Membership, Site

from . import discovery as svc
from .models import DiscoveredTarget, DiscoveryRun, TargetKind

PROM = "http://prom.test:9090"
MUMBAI, CHENNAI = "Mumbai", "Chennai"


def _has(app_label, model):
    try:
        apps.get_model(app_label, model)
        return True
    except LookupError:
        return False


# ----------------------------------------------------------------------------- fake Prometheus
def target(instance, job, health="up", site=None, pool=None, url=None, error=""):
    labels = {"instance": instance, "job": job}
    if site:
        labels["site"] = site
    return {"labels": labels, "discoveredLabels": {"__address__": instance, "job": job},
            "health": health, "scrapePool": pool or job, "lastError": error,
            "scrapeUrl": url or f"http://{instance}/metrics", "lastScrape": "2026-11-07T08:00:00Z"}


TARGETS = [
    target("db-01:9100", "node", site="Mumbai"),
    target("web-01:9100", "node", site="Mumbai"),
    target("app-02:9100", "node", health="down", site="Chennai", error="connection refused"),
    target("10.0.0.1", "snmp", site="Chennai"),
    target("https://example.com", "blackbox", site="Chennai",
           url="http://blackbox:9115/probe?target=https://example.com"),
    target("kube-state:8080", "kube-state-metrics"),
]
NODES = ["db-01:9100", "web-01:9100", "app-02:9100"]


def _rows(pairs):
    return [{"metric": m, "value": v} for m, v in pairs]


def fake_query(q):
    """Answers only the metrics a real node_exporter would publish for NODES; everything else is empty."""
    def each(extra, value=1.0):
        return _rows([({"instance": i, "job": "node", **extra(i)}, value) for i in NODES])
    if q == "node_uname_info":
        return each(lambda i: {"nodename": i.split(":")[0], "sysname": "Linux",
                               "release": "6.1.0-18-amd64", "machine": "x86_64"})
    if q == "node_os_info":
        return each(lambda i: {"pretty_name": "Debian GNU/Linux 12 (bookworm)", "id": "debian"})
    if q == "node_exporter_build_info":
        return each(lambda i: {"version": "1.8.2"})
    if q == "node_network_info":
        return _rows([({"instance": i, "device": d, "address": f"aa:bb:cc:00:00:0{n}", "operstate": "up"}, 1.0)
                      for i in NODES for n, d in enumerate(["eth0", "eth1"])])
    if q == "node_network_speed_bytes":
        return _rows([({"instance": i, "device": "eth0"}, 125000000.0) for i in NODES])
    if q == "node_memory_MemTotal_bytes":
        return _rows([({"instance": i}, 8589934592.0) for i in NODES])
    if q == "node_boot_time_seconds":
        return _rows([({"instance": i}, 1762000000.0) for i in NODES])
    if q.startswith("count by (instance)"):
        return _rows([({"instance": i}, 4.0) for i in NODES])
    if q.startswith("node_filesystem_size_bytes"):
        return _rows([({"instance": i, "mountpoint": "/", "device": "/dev/sda1", "fstype": "ext4"}, 5.0e10)
                      for i in NODES])
    if q.startswith("node_filesystem_avail_bytes"):
        return _rows([({"instance": i, "mountpoint": "/", "device": "/dev/sda1", "fstype": "ext4"}, 2.0e10)
                      for i in NODES])
    return []


def fake_windows_query(q):
    if q == "windows_cs_hostname":
        return _rows([({"instance": "win-01:9182", "hostname": "WIN-01", "domain": "corp"}, 1.0)])
    if q == "windows_os_info":
        return _rows([({"instance": "win-01:9182", "product": "Windows Server 2022", "version": "10.0.20348"}, 1.0)])
    if q == "windows_cs_logical_processors":
        return _rows([({"instance": "win-01:9182"}, 8.0)])
    if q == "windows_cs_physical_memory_bytes":
        return _rows([({"instance": "win-01:9182"}, 17179869184.0)])
    if q.startswith("windows_logical_disk_size_bytes"):
        return _rows([({"instance": "win-01:9182", "volume": "C:"}, 1.0e11)])
    if q.startswith("windows_logical_disk_free_bytes"):
        return _rows([({"instance": "win-01:9182", "volume": "C:"}, 4.0e10)])
    return []


def fake_snmp_query(q):
    if q == "sysName":
        return _rows([({"instance": "10.0.0.1", "sysName": "core-sw-01"}, 1.0)])
    if q == "sysDescr":
        return _rows([({"instance": "10.0.0.1", "sysDescr": "Cisco IOS Software, C2960X"}, 1.0)])
    if q == "sysLocation":
        return _rows([({"instance": "10.0.0.1", "sysLocation": "Rack 4"}, 1.0)])
    return []


RULES = [{
    "name": "node.rules", "file": "/etc/prometheus/rules/node.yml", "interval": 30,
    "lastEvaluation": "2026-11-07T08:00:00Z",
    "rules": [
        {"type": "alerting", "name": "HostDown", "query": "up == 0", "duration": 300, "health": "ok",
         "state": "firing", "labels": {"severity": "critical"}, "annotations": {"summary": "Host down"},
         "lastEvaluation": "2026-11-07T08:00:00Z", "evaluationTime": 0.002,
         "alerts": [{"labels": {"instance": "app-02:9100"}, "state": "firing"},
                    {"labels": {"instance": "x"}, "state": "pending"}]},
        {"type": "alerting", "name": "HighCPU", "query": "cpu > 90", "duration": 600, "health": "ok",
         "state": "inactive", "labels": {"severity": "warning"}, "alerts": []},
        {"type": "recording", "name": "instance:node_cpu:rate5m", "query": "rate(node_cpu_seconds_total[5m])",
         "health": "err", "lastError": "boom"},
        "not a dict",
    ]}]


class PromMixin:
    """Patch telemetry.promapi so discovery sees TARGETS and the fake metrics."""
    targets_data = TARGETS
    query_fns = (fake_query,)

    def setUp(self):
        super().setUp()
        cache.clear()
        DiscoveryRun.objects.all().delete()
        patches = [
            mock.patch("telemetry.promapi.targets", side_effect=lambda: list(self.targets_data)),
            mock.patch("telemetry.promapi.query", side_effect=self._query),
            mock.patch("core.prometheus.configured", return_value=True),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _query(self, q):
        for fn in self.query_fns:
            rows = fn(q)
            if rows:
                return rows
        return []


@override_settings(PROMETHEUS_URL=PROM)
class KindDetectionTests(TestCase):
    def test_metric_evidence_wins_over_the_job_name(self):
        self.assertEqual(svc.detect_kind("whatever", "whatever", has_node=True), TargetKind.NODE)
        self.assertEqual(svc.detect_kind("node", "node", has_windows=True), TargetKind.WINDOWS)
        self.assertEqual(svc.detect_kind("anything", "", has_snmp=True), TargetKind.SNMP)

    def test_job_name_decides_between_two_claims(self):
        # both families answered: the job name breaks the tie
        self.assertEqual(svc.detect_kind("windows-prod", "", has_node=True, has_windows=True), TargetKind.WINDOWS)
        self.assertEqual(svc.detect_kind("node-exporter", "", has_node=True, has_windows=True), TargetKind.NODE)
        self.assertEqual(svc.detect_kind("", "", has_node=True, has_windows=True), TargetKind.NODE)

    def test_names_without_metric_evidence(self):
        cases = {"node": TargetKind.NODE, "linux-hosts": TargetKind.NODE, "windows": TargetKind.WINDOWS,
                 "wmi_exporter": TargetKind.WINDOWS, "snmp-switches": TargetKind.SNMP,
                 "blackbox_http_2xx": TargetKind.BLACKBOX, "icmp": TargetKind.BLACKBOX,
                 "kube-state-metrics": TargetKind.KUBE_STATE, "cadvisor": TargetKind.CADVISOR,
                 "mysqld": TargetKind.MYSQL, "mariadb": TargetKind.MYSQL, "postgres-exporter": TargetKind.POSTGRES,
                 "redis": TargetKind.REDIS, "nginx-ingress": TargetKind.NGINX, "weird-thing": TargetKind.OTHER}
        for job, expected in cases.items():
            self.assertEqual(svc.detect_kind(job), expected, job)

    def test_scrape_pool_is_used_when_the_job_says_nothing(self):
        self.assertEqual(svc.detect_kind("", "snmp-pool"), TargetKind.SNMP)

    def test_address_of(self):
        self.assertEqual(svc.address_of("db-01:9100"), "db-01")
        self.assertEqual(svc.address_of("10.0.0.1"), "10.0.0.1")
        self.assertEqual(svc.address_of("[fd00::1]:9100"), "fd00::1")
        self.assertEqual(svc.address_of("https://example.com/x"), "example.com")
        self.assertEqual(svc.address_of(""), "")


@override_settings(PROMETHEUS_URL=PROM)
class SiteResolutionTests(TestCase):
    def test_reads_the_configured_label(self):
        self.assertEqual(svc.resolve_site({"site": "Mumbai"}, {}, "site"), "Mumbai")
        self.assertEqual(svc.resolve_site({"dc": "Pune"}, {}, "dc"), "Pune")

    def test_discovered_labels_are_a_fallback(self):
        self.assertEqual(svc.resolve_site({}, {"site": "Chennai"}, "site"), "Chennai")

    def test_missing_or_blank_label_means_all_sites(self):
        self.assertEqual(svc.resolve_site({}, {}, "site"), "All Sites")
        self.assertEqual(svc.resolve_site({"site": "   "}, {}, "site"), "All Sites")

    def test_label_name_comes_from_the_integration_settings(self):
        with mock.patch("administration.integration_config.prometheus_site_label", return_value="dc"):
            self.assertEqual(svc.site_label(), "dc")
            self.assertEqual(svc.resolve_site({"dc": "Pune", "site": "x"}, {}), "Pune")


@override_settings(PROMETHEUS_URL=PROM)
class RunTests(PromMixin, TestCase):
    def test_upsert_creates_one_row_per_instance_and_job(self):
        run = svc.run()
        self.assertTrue(run.ok)
        self.assertEqual(run.targets, 6)
        self.assertEqual(run.new, 6)
        self.assertEqual(run.up, 5)
        self.assertEqual(run.down, 1)
        self.assertEqual(DiscoveredTarget.objects.for_tenant().count(), 6)
        kinds = dict(DiscoveredTarget.objects.for_tenant().values_list("instance", "kind"))
        self.assertEqual(kinds["db-01:9100"], TargetKind.NODE)
        self.assertEqual(kinds["10.0.0.1"], TargetKind.SNMP)
        self.assertEqual(kinds["https://example.com"], TargetKind.BLACKBOX)
        self.assertEqual(kinds["kube-state:8080"], TargetKind.KUBE_STATE)

    def test_second_run_updates_in_place(self):
        svc.run()
        first = DiscoveredTarget.objects.for_tenant().get(instance="db-01:9100")
        run = svc.run()
        self.assertEqual(run.new, 0)
        self.assertEqual(DiscoveredTarget.objects.for_tenant().count(), 6)
        again = DiscoveredTarget.objects.for_tenant().get(instance="db-01:9100")
        self.assertEqual(again.pk, first.pk)
        self.assertEqual(again.first_seen, first.first_seen)
        self.assertGreaterEqual(again.last_seen, first.last_seen)

    def test_details_are_gathered_for_node_hosts(self):
        svc.run()
        d = DiscoveredTarget.objects.for_tenant().get(instance="db-01:9100").details
        self.assertEqual(d["hostname"], "db-01")
        self.assertEqual(d["os"], "Debian GNU/Linux 12 (bookworm)")
        self.assertEqual(d["kernel"], "Linux 6.1.0-18-amd64")
        self.assertEqual(d["arch"], "x86_64")
        self.assertEqual(d["cores"], 4)
        self.assertEqual(d["memory_bytes"], 8589934592)
        self.assertEqual(d["exporter_version"], "1.8.2")
        self.assertEqual(d["boot_time"], 1762000000)
        self.assertEqual([i["name"] for i in d["interfaces"]], ["eth0", "eth1"])
        self.assertEqual(d["interfaces"][0]["mac"], "aa:bb:cc:00:00:00")
        self.assertEqual(d["interfaces"][0]["speed"], 125000000)
        self.assertEqual(d["filesystems"][0]["mountpoint"], "/")
        self.assertEqual(d["filesystems"][0]["size_bytes"], 50000000000)
        self.assertEqual(d["filesystems"][0]["avail_bytes"], 20000000000)

    def test_non_host_targets_get_only_their_address(self):
        svc.run()
        d = DiscoveredTarget.objects.for_tenant().get(instance="kube-state:8080").details
        self.assertEqual(d, {"address": "kube-state"})

    def test_site_comes_from_the_target_label(self):
        svc.run()
        sites = dict(DiscoveredTarget.objects.for_tenant().values_list("instance", "site"))
        self.assertEqual(sites["db-01:9100"], "Mumbai")
        self.assertEqual(sites["10.0.0.1"], "Chennai")
        self.assertEqual(sites["kube-state:8080"], "All Sites")   # no site label

    def test_missing_since_is_set_and_cleared(self):
        svc.run()
        self.targets_data = [t for t in TARGETS if t["labels"]["instance"] != "web-01:9100"]
        run = svc.run()
        self.assertEqual(run.missing, 1)
        gone = DiscoveredTarget.objects.for_tenant().get(instance="web-01:9100")
        self.assertIsNotNone(gone.missing_since)
        self.assertEqual(gone.health, DiscoveredTarget.UNKNOWN)
        self.assertEqual(DiscoveredTarget.objects.for_tenant().present().count(), 5)
        self.targets_data = TARGETS
        svc.run()
        gone.refresh_from_db()
        self.assertIsNone(gone.missing_since)

    def test_query_budget_is_independent_of_host_count(self):
        run = svc.run()
        # 3 identity probes + the Linux detail queries + the kube-state-metrics ones (TARGETS has a
        # kube-state-metrics job; no Windows or SNMP answered)
        self.assertEqual(run.queries, len(svc.IDENTITY_QUERIES) + len(svc.NODE_QUERIES) + len(svc.KSM_QUERIES))
        self.assertEqual(run.queries, 3 + 10 + 7)

    def test_a_family_nothing_answers_for_is_never_asked_for_details(self):
        """Nothing answers the identity probes: the pass stops at the 3 probes instead of asking every
        detail query, and still records every target."""
        with mock.patch("telemetry.promapi.query", return_value=[]):
            run = svc.run()
        self.assertTrue(run.ok, run.error)
        # only the kube-state-metrics queries remain, because TARGETS has a job named like one
        self.assertEqual(run.queries, len(svc.IDENTITY_QUERIES) + len(svc.KSM_QUERIES))
        self.assertEqual(DiscoveredTarget.objects.for_tenant().count(), 6)

    def test_a_pass_that_overlaps_another_pass_does_not_fail(self):
        """The 5-minute job and "Discover now" / the management command can run at the same time: the loser of
        the (tenant_key, instance, job) unique constraint must update the winner's row, not abort the pass."""
        real, state = svc._details_for, {"done": False}

        def competing(inst, kind, data):
            if not state["done"]:                       # the other pass inserted this target a moment ago
                state["done"] = True
                DiscoveredTarget.objects.create(tenant_key=svc.tenant(), instance=inst, job="node",
                                                site="All Sites")
            return real(inst, kind, data)

        with mock.patch("telemetry.discovery._details_for", side_effect=competing):
            run = svc.run()
        self.assertTrue(run.ok, run.error)
        self.assertEqual(DiscoveredTarget.objects.for_tenant().count(), 6)
        self.assertEqual(DiscoveredTarget.objects.for_tenant().filter(instance="db-01:9100").count(), 1)
        # the row the other pass inserted is updated in place, not left half-written
        row = DiscoveredTarget.objects.for_tenant().get(instance="db-01:9100")
        self.assertEqual(row.kind, TargetKind.NODE)
        self.assertEqual(row.site, MUMBAI)
        self.assertEqual(row.health, DiscoveredTarget.UP)
        self.assertEqual((row.details or {}).get("hostname"), "db-01")

    def test_run_summary_is_stored(self):
        svc.run(trigger="manual")
        row = DiscoveryRun.objects.filter(tenant_key=svc.tenant()).first()
        self.assertTrue(row.ok)
        self.assertEqual(row.trigger, "manual")
        self.assertEqual(row.targets, 6)
        self.assertIsNotNone(row.duration_ms)
        self.assertIn("6 target(s)", row.summary)

    def test_events_are_emitted_for_new_targets_only(self):
        svc.run()
        per_target = Event.objects.filter(source="telemetry", kind="discovery").exclude(title__contains="found")
        self.assertEqual(per_target.count(), 6)
        self.assertEqual(Event.objects.filter(title__contains="found 6 new target").count(), 1)
        Event.objects.all().delete()
        svc.run()
        self.assertEqual(Event.objects.count(), 0)

    def test_per_target_events_are_capped(self):
        self.targets_data = [target(f"host-{n}:9100", "node") for n in range(30)]
        svc.run()
        per_target = Event.objects.filter(kind="discovery").exclude(title__contains="found")
        self.assertEqual(per_target.count(), svc.NEW_TARGET_EVENT_CAP)
        self.assertEqual(Event.objects.filter(title__contains="found 30 new target").count(), 1)

    def test_discovery_writes_no_audit_events(self):
        svc.run()
        self.assertEqual(AuditEvent.objects.count(), 0)


@override_settings(PROMETHEUS_URL=PROM)
class WindowsAndSnmpTests(PromMixin, TestCase):
    targets_data = [target("win-01:9182", "windows"), target("10.0.0.1", "snmp")]
    query_fns = (fake_windows_query, fake_snmp_query)

    def test_windows_details(self):
        svc.run()
        row = DiscoveredTarget.objects.for_tenant().get(instance="win-01:9182")
        self.assertEqual(row.kind, TargetKind.WINDOWS)
        self.assertEqual(row.details["hostname"], "WIN-01")
        self.assertEqual(row.details["os"], "Windows Server 2022")
        self.assertEqual(row.details["cores"], 8)
        self.assertEqual(row.details["memory_bytes"], 17179869184)
        self.assertEqual(row.details["filesystems"][0]["mountpoint"], "C:")

    def test_the_linux_detail_queries_are_not_run_when_no_linux_host_answered(self):
        run = svc.run()
        # 3 identity probes + 7 Windows + 2 SNMP: the 9 node_exporter queries are never sent
        self.assertEqual(run.queries,
                         len(svc.IDENTITY_QUERIES) + len(svc.WINDOWS_QUERIES) + len(svc.SNMP_QUERIES))

    def test_snmp_details(self):
        svc.run()
        row = DiscoveredTarget.objects.for_tenant().get(instance="10.0.0.1")
        self.assertEqual(row.kind, TargetKind.SNMP)
        self.assertEqual(row.details["sysName"], "core-sw-01")
        self.assertEqual(row.details["sysDescr"], "Cisco IOS Software, C2960X")
        self.assertEqual(row.details["sysLocation"], "Rack 4")
        self.assertEqual(row.details["ip"], "10.0.0.1")


@override_settings(PROMETHEUS_URL=PROM)
class ErrorHandlingTests(PromMixin, TestCase):
    def test_prometheus_error_does_not_raise_and_is_recorded(self):
        with mock.patch("telemetry.promapi.targets", side_effect=prometheus.PrometheusError("boom")):
            run = svc.run()
        self.assertFalse(run.ok)
        self.assertEqual(run.error, "boom")
        self.assertEqual(DiscoveredTarget.objects.count(), 0)
        self.assertTrue(DiscoveryRun.objects.filter(ok=False, error="boom").exists())

    def test_unexpected_response_does_not_raise(self):
        with mock.patch("telemetry.promapi.targets", side_effect=TypeError("nope")):
            run = svc.run()
        self.assertFalse(run.ok)
        self.assertIn("Unexpected", run.error)

    def test_detail_query_failure_still_upserts_targets(self):
        def boom(q):
            if q == "node_os_info":
                raise prometheus.PrometheusError("detail boom")
            return fake_query(q)
        with mock.patch("telemetry.promapi.query", side_effect=boom):
            run = svc.run()
        self.assertTrue(run.ok)
        self.assertEqual(run.targets, 6)

    def test_not_configured(self):
        with mock.patch("core.prometheus.configured", return_value=False):
            run = svc.run()
        self.assertFalse(run.ok)
        self.assertIn("not connected", run.error)

    def test_the_job_never_raises(self):
        from . import jobs
        with mock.patch("telemetry.discovery.run", side_effect=RuntimeError("kaboom")):
            self.assertEqual(jobs.discover_targets(), "error")

    def test_targets_without_an_instance_label_are_skipped(self):
        self.targets_data = [{"labels": {"job": "x"}, "health": "up"}, "junk", target("ok:9100", "node")]
        run = svc.run()
        self.assertEqual(run.targets, 1)


@override_settings(PROMETHEUS_URL=PROM)
class LinkingTests(PromMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.CI = apps.get_model("cmdb", "ConfigurationItem")
        self.Device = apps.get_model("network", "Device")
        self.Exporter = apps.get_model("observe", "Exporter")

    def test_links_by_prometheus_instance(self):
        ci = self.CI.objects.create(name="db-01", site="Mumbai", prometheus_instance="db-01:9100")
        svc.run()
        row = DiscoveredTarget.objects.for_tenant().get(instance="db-01:9100")
        self.assertEqual(row.ci_id, ci.pk)
        self.assertEqual(row.state, DiscoveredTarget.LINKED)

    def test_links_an_item_added_by_hand_after_the_first_run(self):
        svc.run()
        row = DiscoveredTarget.objects.for_tenant().get(instance="web-01:9100")
        self.assertIsNone(row.ci_id)
        self.assertEqual(row.state, DiscoveredTarget.NEW)
        ci = self.CI.objects.create(name="web-01-by-hand", site="Mumbai", prometheus_instance="web-01:9100")
        svc.run()
        row.refresh_from_db()
        self.assertEqual(row.ci_id, ci.pk)
        self.assertEqual(row.state, DiscoveredTarget.LINKED)

    def test_links_by_name_and_by_ip(self):
        self.CI.objects.create(name="db-01", site="Mumbai")
        self.CI.objects.create(name="switch", site="Chennai", ip_address="10.0.0.1")
        svc.run()
        self.assertEqual(DiscoveredTarget.objects.for_tenant().get(instance="db-01:9100").ci.name, "db-01")
        self.assertEqual(DiscoveredTarget.objects.for_tenant().get(instance="10.0.0.1").ci.name, "switch")

    def test_links_a_device_by_management_address(self):
        dev = self.Device.objects.create(name="core-sw", mgmt_ip="10.0.0.1", site="Chennai")
        svc.run()
        self.assertEqual(DiscoveredTarget.objects.for_tenant().get(instance="10.0.0.1").device_id, dev.pk)

    def test_links_an_exporter_by_scrape_url(self):
        exp = self.Exporter.objects.create(name="db-01 node", exporter_type="Node Exporter",
                                           metrics_url="http://db-01:9100/metrics")
        svc.run()
        self.assertEqual(DiscoveredTarget.objects.for_tenant().get(instance="db-01:9100").exporter_id, exp.pk)

    def test_another_tenants_item_is_never_linked(self):
        self.CI.objects.create(tenant_key="other-tenant", name="db-01", site="Mumbai",
                               prometheus_instance="db-01:9100")
        self.Device.objects.create(tenant_key="other-tenant", name="other-sw", mgmt_ip="10.0.0.1")
        self.Exporter.objects.create(tenant_key="other-tenant", name="other-exp", exporter_type="Node Exporter",
                                     metrics_url="http://db-01:9100/metrics")
        svc.run()
        row = DiscoveredTarget.objects.for_tenant().get(instance="db-01:9100")
        self.assertIsNone(row.ci_id)
        self.assertIsNone(row.exporter_id)
        self.assertIsNone(DiscoveredTarget.objects.for_tenant().get(instance="10.0.0.1").device_id)
        self.assertEqual(row.state, DiscoveredTarget.NEW)


@override_settings(PROMETHEUS_URL=PROM)
class AutoCreateTests(PromMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.CI = apps.get_model("cmdb", "ConfigurationItem")
        self.Device = apps.get_model("network", "Device")
        self.Exporter = apps.get_model("observe", "Exporter")

    def _flags(self, ci=False, device=False, exporter=False):
        return mock.patch("telemetry.discovery._flags",
                          return_value={"ci": ci, "device": device, "exporter": exporter})

    def test_off_by_default_nothing_is_created(self):
        svc.run()
        self.assertEqual(self.CI.objects.count(), 0)
        self.assertEqual(self.Device.objects.count(), 0)
        self.assertEqual(self.Exporter.objects.count(), 0)

    def test_creates_cmdb_items_for_hosts_only(self):
        with self._flags(ci=True):
            run = svc.run()
        self.assertEqual(self.CI.objects.count(), 3)            # the three node hosts, not snmp/blackbox/ksm
        self.assertEqual(run.created, 3)
        ci = self.CI.objects.get(name="db-01")
        self.assertEqual(ci.ci_type, "server")
        self.assertEqual(ci.site, "Mumbai")
        self.assertEqual(ci.prometheus_instance, "db-01:9100")
        self.assertEqual(ci.attributes["os"], "Debian GNU/Linux 12 (bookworm)")
        self.assertEqual(ci.attributes["cores"], 4)
        self.assertNotIn("interfaces", ci.attributes)           # bulky lists stay on the discovered target
        self.assertIn("Discovered from Prometheus job node", ci.description)
        self.assertIsNone(ci.ip_address)                        # "db-01" is not an IP address

    def test_ip_address_is_filled_when_the_instance_is_an_ip(self):
        self.targets_data = [target("10.1.2.3:9100", "node", site="Mumbai")]
        with self._flags(ci=True):
            svc.run()
        self.assertEqual(str(self.CI.objects.get().ip_address), "10.1.2.3")

    def test_a_host_scraped_by_two_jobs_gets_one_cmdb_item(self):
        """Two scrape jobs on the same exporter are two targets but one host: the second target is linked to
        the item the first one created (never a "db-01-2" twin)."""
        self.targets_data = [target("db-01:9100", "node", site=MUMBAI),
                             target("db-01:9100", "node-extra", site=MUMBAI)]
        with self._flags(ci=True):
            run = svc.run()
        self.assertEqual(run.targets, 2)
        self.assertEqual(DiscoveredTarget.objects.for_tenant().count(), 2)
        self.assertEqual(self.CI.objects.count(), 1)
        self.assertEqual(run.created, 1)
        ci_ids = set(DiscoveredTarget.objects.for_tenant().values_list("ci_id", flat=True))
        self.assertEqual(ci_ids, {self.CI.objects.get().pk})

    def test_creating_is_idempotent_across_runs(self):
        with self._flags(ci=True):
            svc.run()
            svc.run()
            svc.run()
        self.assertEqual(self.CI.objects.count(), 3)

    def test_an_already_linked_target_is_never_duplicated(self):
        self.CI.objects.create(name="db-01", site="Mumbai", prometheus_instance="db-01:9100")
        with self._flags(ci=True):
            svc.run()
        self.assertEqual(self.CI.objects.filter(prometheus_instance="db-01:9100").count(), 1)
        self.assertEqual(self.CI.objects.count(), 3)            # db-01 linked + web-01 and app-02 created

    def test_name_clash_gets_a_suffix(self):
        self.CI.objects.create(name="db-01", site="Chennai")    # same name, no prometheus_instance, other site
        with self._flags(ci=True):
            svc.run()
        # the existing item is linked by name, so nothing is created for db-01
        self.assertEqual(self.CI.objects.filter(name="db-01").count(), 1)
        row = DiscoveredTarget.objects.for_tenant().get(instance="db-01:9100")
        self.assertEqual(row.ci.name, "db-01")

    def test_creates_devices_for_snmp_targets_only(self):
        with self._flags(device=True):
            svc.run()
        self.assertEqual(self.Device.objects.count(), 1)
        dev = self.Device.objects.get()
        self.assertEqual(dev.mgmt_ip, "10.0.0.1")
        self.assertEqual(dev.prometheus_instance, "10.0.0.1")
        self.assertEqual(dev.site, "Chennai")
        self.assertEqual(dev.vendor, "")
        self.assertEqual(dev.model, "")

    def test_registers_exporters_for_every_target_with_a_scrape_url(self):
        with self._flags(exporter=True):
            svc.run()
        self.assertEqual(self.Exporter.objects.count(), 6)
        exp = self.Exporter.objects.get(target="db-01:9100")
        self.assertEqual(exp.exporter_type, "Node Exporter")
        self.assertEqual(exp.category, "Servers")
        self.assertEqual(exp.metrics_url, "http://db-01:9100/metrics")

    def test_a_target_without_a_scrape_url_registers_no_exporter(self):
        self.targets_data = [{**target("x:9100", "node"), "scrapeUrl": ""}]
        with self._flags(exporter=True):
            svc.run()
        self.assertEqual(self.Exporter.objects.count(), 0)

    def test_ignored_targets_are_never_auto_created(self):
        svc.run()
        DiscoveredTarget.objects.for_tenant().update(state=DiscoveredTarget.IGNORED)
        with self._flags(ci=True, device=True, exporter=True):
            svc.run()
        self.assertEqual(self.CI.objects.count(), 0)
        self.assertEqual(self.Device.objects.count(), 0)
        self.assertEqual(self.Exporter.objects.count(), 0)

    def test_ignore_survives_later_runs(self):
        svc.run()
        row = DiscoveredTarget.objects.for_tenant().get(instance="db-01:9100")
        row.state = DiscoveredTarget.IGNORED
        row.save()
        svc.run()
        svc.run()
        row.refresh_from_db()
        self.assertEqual(row.state, DiscoveredTarget.IGNORED)


@override_settings(PROMETHEUS_URL=PROM)
class RulesTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_parsing(self):
        with mock.patch("telemetry.promapi.rules", return_value=RULES):
            groups = svc.rule_groups(use_cache=False)
        self.assertEqual(len(groups), 1)
        g = groups[0]
        self.assertEqual(g["name"], "node.rules")
        self.assertEqual(g["file"], "/etc/prometheus/rules/node.yml")
        self.assertEqual(g["interval"], 30)
        self.assertEqual(len(g["rules"]), 3)            # the "not a dict" entry is dropped
        self.assertEqual(g["alerting"], 2)
        self.assertEqual(g["recording"], 1)
        self.assertEqual(g["firing"], 1)
        host_down = g["rules"][0]
        self.assertEqual(host_down["state"], "firing")
        self.assertEqual(host_down["severity"], "critical")
        self.assertEqual(host_down["active"], 2)        # firing + pending
        self.assertEqual(host_down["firing"], 1)
        self.assertEqual(host_down["duration"], 300)
        self.assertEqual(host_down["summary"], "Host down")
        self.assertEqual(g["rules"][2]["type"], "recording")
        self.assertEqual(g["rules"][2]["error"], "boom")

    def test_kpis(self):
        with mock.patch("telemetry.promapi.rules", return_value=RULES):
            k = svc.rule_kpis(svc.rule_groups(use_cache=False))
        self.assertEqual(k, {"groups": 1, "rules": 3, "alerting": 2, "recording": 1,
                             "firing": 1, "pending": 1, "errors": 1})

    def test_results_are_cached(self):
        with mock.patch("telemetry.promapi.rules", return_value=RULES) as m:
            svc.rule_groups()
            svc.rule_groups()
        self.assertEqual(m.call_count, 1)

    def test_empty(self):
        with mock.patch("telemetry.promapi.rules", return_value=[]):
            self.assertEqual(svc.rule_groups(use_cache=False), [])


@override_settings(PROMETHEUS_URL=PROM)
class CommandTests(PromMixin, TestCase):
    def test_discover_targets_command(self):
        from io import StringIO
        out = StringIO()
        call_command("discover_targets", "--now", "--list", stdout=out)
        text = out.getvalue()
        self.assertIn("6 target(s)", text)
        self.assertIn("db-01:9100", text)
        self.assertEqual(DiscoveredTarget.objects.for_tenant().count(), 6)
        self.assertEqual(DiscoveryRun.objects.filter(trigger="command").count(), 1)

    def test_command_reports_a_failure(self):
        from io import StringIO
        out, err = StringIO(), StringIO()
        with mock.patch("telemetry.promapi.targets", side_effect=prometheus.PrometheusError("down")):
            call_command("discover_targets", stdout=out, stderr=err)
        self.assertIn("Discovery failed", err.getvalue())


# ----------------------------------------------------------------------------- pages and actions
def _user(name, role=None, site=None, superuser=False):
    u = User.objects.create_user(name, password="pw-for-tests-123", is_staff=False)
    if superuser:
        u.is_superuser = u.is_staff = True
        u.save()
    elif role:
        s = Site.objects.get_or_create(name=site, defaults={"code": site.lower()})[0] if site else None
        Membership.objects.create(user=u, site=s, role=role)
    return u


@override_settings(PROMETHEUS_URL=PROM)
class PageTests(PromMixin, TestCase):
    def setUp(self):
        super().setUp()
        svc.run()
        self.admin = _user("disc-admin", superuser=True)
        self.client = Client()
        self.client.force_login(self.admin)

    def test_list_page(self):
        r = self.client.get(reverse("telemetry:discovery"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "db-01:9100")
        self.assertContains(r, "Debian GNU/Linux 12")
        self.assertContains(r, "Discover now")
        self.assertEqual(r.context["kpis"]["targets"], 6)
        self.assertEqual(r.context["kpis"]["hosts"], 3)
        self.assertEqual(r.context["kpis"]["unlinked"], 3)

    def test_page_without_prometheus(self):
        with mock.patch("core.prometheus.configured", return_value=False):
            r = self.client.get(reverse("telemetry:discovery"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "not connected")

    def test_filters(self):
        url = reverse("telemetry:discovery")
        self.assertEqual(len(self.client.get(url, {"kind": "node"}).context["page"].paginator.object_list), 3)
        self.assertEqual(len(self.client.get(url, {"health": "down"}).context["page"].paginator.object_list), 1)
        self.assertEqual(len(self.client.get(url, {"site": "Mumbai"}).context["page"].paginator.object_list), 2)
        self.assertEqual(len(self.client.get(url, {"q": "kube"}).context["page"].paginator.object_list), 1)
        self.assertEqual(len(self.client.get(url, {"state": "ignored"}).context["page"].paginator.object_list), 0)

    def test_show_gone_toggle_keeps_the_other_filters(self):
        """The "Show/Hide gone" link must not throw away the kind/health/site/search the user set."""
        url = reverse("telemetry:discovery")
        r = self.client.get(url, {"kind": "node", "q": "db"})
        self.assertEqual(r.context["gone_query"].urlencode(), "kind=node&q=db")
        self.assertContains(r, "?kind=node&amp;q=db&amp;missing=1")
        # and back again: the toggle drops only `missing`
        r = self.client.get(url, {"kind": "node", "q": "db", "missing": "1"})
        self.assertEqual(r.context["gone_query"].urlencode(), "kind=node&q=db")
        self.assertNotContains(r, "missing=1\" class=\"on\"")

    def test_csv_export(self):
        r = self.client.get(reverse("telemetry:discovery"), {"format": "csv"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "text/csv; charset=utf-8")
        body = r.content.decode()
        self.assertIn("instance,hostname,kind", body)
        self.assertIn("db-01:9100", body)
        self.assertIn("Debian GNU/Linux 12 (bookworm)", body)

    def test_detail_page(self):
        row = DiscoveredTarget.objects.for_tenant().get(instance="db-01:9100")
        r = self.client.get(row.get_absolute_url())
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "eth0")
        self.assertContains(r, "/dev/sda1")
        self.assertContains(r, "Debian GNU/Linux 12")
        self.assertContains(r, "site")           # the target labels table

    def test_prometheus_page_shows_rules(self):
        with mock.patch("telemetry.promapi.rules", return_value=RULES), \
                mock.patch("telemetry.promapi.alerts", return_value=[]):
            r = self.client.get(reverse("telemetry:prometheus"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Alert &amp; Recording Rules")
        self.assertContains(r, "HostDown")
        self.assertContains(r, "node.rules")
        self.assertEqual(r.context["rule_kpis"]["firing"], 1)

    def test_infrastructure_banner(self):
        with mock.patch("telemetry.hosts.fetch_hosts", return_value=[]), \
                mock.patch("telemetry.hosts.cpu_sparklines", return_value={}):
            r = self.client.get(reverse("telemetry:infrastructure"))
        self.assertEqual(r.context["undiscovered"], 3)
        self.assertContains(r, "not in the CMDB yet")

    def test_cmdb_items_banner(self):
        r = self.client.get(reverse("cmdb:items"))
        self.assertEqual(r.context["undiscovered"], 3)
        self.assertContains(r, "not in the CMDB yet")

    def test_discover_now_runs_a_pass(self):
        DiscoveryRun.objects.all().delete()
        r = self.client.post(reverse("telemetry:discovery_run"), {})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(DiscoveryRun.objects.filter(trigger="manual").count(), 1)
        self.assertTrue(AuditEvent.objects.filter(action="discover").exists())

    def test_discover_now_rejects_get(self):
        self.assertEqual(self.client.get(reverse("telemetry:discovery_run")).status_code, 405)

    def test_open_redirect_is_refused(self):
        r = self.client.post(reverse("telemetry:discovery_run"), {"next": "https://evil.example/x"})
        self.assertEqual(r["Location"], reverse("telemetry:discovery"))


@override_settings(PROMETHEUS_URL=PROM)
class ActionTests(PromMixin, TestCase):
    def setUp(self):
        super().setUp()
        svc.run()
        self.CI = apps.get_model("cmdb", "ConfigurationItem")
        self.Device = apps.get_model("network", "Device")
        self.admin = _user("act-admin", superuser=True)
        self.client = Client()
        self.client.force_login(self.admin)
        self.db01 = DiscoveredTarget.objects.for_tenant().get(instance="db-01:9100")
        self.snmp = DiscoveredTarget.objects.for_tenant().get(instance="10.0.0.1")
        self.url = reverse("telemetry:discovery_action")

    def test_add_to_cmdb(self):
        r = self.client.post(self.url, {"action": "add_to_cmdb", "target": [self.db01.pk]})
        self.assertEqual(r.status_code, 302)
        self.db01.refresh_from_db()
        self.assertIsNotNone(self.db01.ci_id)
        self.assertEqual(self.db01.state, DiscoveredTarget.LINKED)
        self.assertEqual(self.db01.ci.site, "Mumbai")
        self.assertTrue(AuditEvent.objects.filter(action="create").exists())

    def test_add_to_cmdb_twice_creates_one_item(self):
        for _ in range(2):
            self.client.post(self.url, {"action": "add_to_cmdb", "target": [self.db01.pk]})
        self.assertEqual(self.CI.objects.filter(prometheus_instance="db-01:9100").count(), 1)

    def test_add_to_cmdb_refuses_a_non_host(self):
        self.client.post(self.url, {"action": "add_to_cmdb", "target": [self.snmp.pk]})
        self.assertEqual(self.CI.objects.count(), 0)

    def test_bulk_add_to_cmdb(self):
        ids = list(DiscoveredTarget.objects.for_tenant().hosts().values_list("pk", flat=True))
        self.client.post(self.url, {"action": "add_to_cmdb", "target": ids})
        self.assertEqual(self.CI.objects.count(), 3)

    def test_create_device(self):
        self.client.post(self.url, {"action": "create_device", "target": [self.snmp.pk]})
        self.snmp.refresh_from_db()
        self.assertIsNotNone(self.snmp.device_id)
        self.assertEqual(self.snmp.device.mgmt_ip, "10.0.0.1")

    def test_ignore_and_unignore(self):
        self.client.post(self.url, {"action": "ignore", "target": [self.db01.pk]})
        self.db01.refresh_from_db()
        self.assertEqual(self.db01.state, DiscoveredTarget.IGNORED)
        self.client.post(self.url, {"action": "unignore", "target": [self.db01.pk]})
        self.db01.refresh_from_db()
        self.assertEqual(self.db01.state, DiscoveredTarget.NEW)

    def test_set_site_is_kept_by_later_runs(self):
        Site.objects.get_or_create(name="Pune", defaults={"code": "pune"})
        self.client.post(self.url, {"action": "set_site", "target": [self.db01.pk], "site": "Pune"})
        self.db01.refresh_from_db()
        self.assertEqual(self.db01.site, "Pune")
        self.assertTrue(self.db01.site_manual)
        svc.run()
        self.db01.refresh_from_db()
        self.assertEqual(self.db01.site, "Pune")       # the "site" target label does not win it back

    def test_set_site_refuses_an_unknown_site(self):
        self.client.post(self.url, {"action": "set_site", "target": [self.db01.pk], "site": "Nowhere"})
        self.db01.refresh_from_db()
        self.assertEqual(self.db01.site, "Mumbai")

    def test_link_to_an_existing_item(self):
        ci = self.CI.objects.create(name="db-01-existing", site="Mumbai")
        self.client.post(self.url, {"action": "link", "target": [self.db01.pk], "ci": ci.pk})
        self.db01.refresh_from_db()
        ci.refresh_from_db()
        self.assertEqual(self.db01.ci_id, ci.pk)
        self.assertEqual(ci.prometheus_instance, "db-01:9100")   # written back for the live pages

    def test_link_refuses_a_record_on_another_site(self):
        ci = self.CI.objects.create(name="chennai-ci", site="Chennai")
        self.client.post(self.url, {"action": "link", "target": [self.db01.pk], "ci": ci.pk})
        self.db01.refresh_from_db()
        self.assertIsNone(self.db01.ci_id)

    def test_unknown_action(self):
        r = self.client.post(self.url, {"action": "drop_tables", "target": [self.db01.pk]})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.CI.objects.count(), 0)

    def test_actions_reject_get(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)


@override_settings(PROMETHEUS_URL=PROM)
class RbacTests(PromMixin, TestCase):
    def setUp(self):
        super().setUp()
        svc.run()
        self.CI = apps.get_model("cmdb", "ConfigurationItem")
        self.db01 = DiscoveredTarget.objects.for_tenant().get(instance="db-01:9100")    # Mumbai
        self.app02 = DiscoveredTarget.objects.for_tenant().get(instance="app-02:9100")  # Chennai
        self.ksm = DiscoveredTarget.objects.for_tenant().get(instance="kube-state:8080")  # All Sites
        Site.objects.get_or_create(name=MUMBAI, defaults={"code": "mumbai"})
        Site.objects.get_or_create(name=CHENNAI, defaults={"code": "chennai"})
        self.op = _user("mumbai-op", role="operator", site=MUMBAI)
        self.viewer = _user("mumbai-viewer", role="viewer", site=MUMBAI)
        self.url = reverse("telemetry:discovery_action")

    def _as(self, user):
        c = Client()
        c.force_login(user)
        return c

    def test_an_operator_sees_only_their_sites(self):
        r = self._as(self.op).get(reverse("telemetry:discovery"))
        rows = list(r.context["page"].paginator.object_list)
        instances = {row.instance for row in rows}
        self.assertIn("db-01:9100", instances)
        self.assertIn("kube-state:8080", instances)     # "All Sites" rows stay visible
        self.assertNotIn("app-02:9100", instances)      # Chennai

    def test_a_chennai_target_is_not_reachable_by_a_mumbai_operator(self):
        r = self._as(self.op).get(self.app02.get_absolute_url())
        self.assertEqual(r.status_code, 404)

    def test_mumbai_operator_can_add_a_mumbai_target(self):
        self._as(self.op).post(self.url, {"action": "add_to_cmdb", "target": [self.db01.pk]})
        self.db01.refresh_from_db()
        self.assertIsNotNone(self.db01.ci_id)

    def test_mumbai_operator_cannot_add_a_chennai_target(self):
        self._as(self.op).post(self.url, {"action": "add_to_cmdb", "target": [self.app02.pk]})
        self.app02.refresh_from_db()
        self.assertIsNone(self.app02.ci_id)
        self.assertEqual(self.CI.objects.count(), 0)

    def test_mumbai_operator_cannot_add_an_all_sites_target(self):
        """"All Sites" needs the role on every site, as core.access.can_on requires everywhere else."""
        self._as(self.op).post(self.url, {"action": "add_to_cmdb", "target": [self.ksm.pk]})
        self.ksm.refresh_from_db()
        self.assertIsNone(self.ksm.ci_id)

    def test_a_mixed_selection_only_changes_the_allowed_rows(self):
        self._as(self.op).post(self.url, {"action": "ignore", "target": [self.db01.pk, self.app02.pk]})
        self.db01.refresh_from_db()
        self.app02.refresh_from_db()
        self.assertEqual(self.db01.state, DiscoveredTarget.IGNORED)
        self.assertEqual(self.app02.state, DiscoveredTarget.NEW)

    def test_a_mumbai_operator_cannot_move_a_target_to_chennai(self):
        self._as(self.op).post(self.url, {"action": "set_site", "target": [self.db01.pk], "site": CHENNAI})
        self.db01.refresh_from_db()
        self.assertEqual(self.db01.site, MUMBAI)
        self.assertFalse(self.db01.site_manual)

    def test_site_choices_hold_the_users_sites_only(self):
        r = self._as(self.op).get(reverse("telemetry:discovery"))
        self.assertIn(MUMBAI, r.context["site_choices"])       # the target's own site can be chosen again
        self.assertIn("All Sites", r.context["site_choices"])
        self.assertNotIn(CHENNAI, r.context["site_choices"])

    def test_a_viewer_cannot_act(self):
        r = self._as(self.viewer).post(self.url, {"action": "ignore", "target": [self.db01.pk]})
        self.db01.refresh_from_db()
        self.assertEqual(self.db01.state, DiscoveredTarget.NEW)
        self.assertIn(r.status_code, (302, 403))

    def test_a_viewer_cannot_discover(self):
        r = self._as(self.viewer).post(reverse("telemetry:discovery_run"), {})
        self.assertIn(r.status_code, (302, 403))

    def test_an_operator_cannot_discover(self):
        """"Discover now" talks to the integration and may create records: admin only."""
        DiscoveryRun.objects.all().delete()
        self._as(self.op).post(reverse("telemetry:discovery_run"), {})
        self.assertEqual(DiscoveryRun.objects.count(), 0)

    def test_an_anonymous_visitor_is_sent_to_the_sign_in_page(self):
        r = Client().get(reverse("telemetry:discovery"))
        self.assertEqual(r.status_code, 302)
        self.assertIn("/accounts/login/", r["Location"])


@override_settings(PROMETHEUS_URL=PROM)
class JobRegistrationTests(TestCase):
    def test_the_job_is_registered_every_300_seconds(self):
        from core import jobs as core_jobs
        self.assertIn("telemetry.discovery", core_jobs.JOBS)
        self.assertEqual(core_jobs.JOBS["telemetry.discovery"]["interval"], 300)


class TenantIsolationTests(PromMixin, TestCase):
    @override_settings(PROMETHEUS_URL=PROM)
    def test_rows_of_another_tenant_are_invisible(self):
        svc.run()
        DiscoveredTarget.objects.create(tenant_key="other-tenant", instance="hidden:9100", job="node")
        self.assertEqual(DiscoveredTarget.objects.for_tenant().count(), 6)
        self.assertEqual(DiscoveredTarget.objects.count(), 7)
        admin = _user("tenant-admin", superuser=True)
        c = Client()
        c.force_login(admin)
        r = c.get(reverse("telemetry:discovery"))
        self.assertNotContains(r, "hidden:9100")


class ModelTests(TestCase):
    def test_address_and_display_name(self):
        t = DiscoveredTarget(instance="db-01:9100", details={"hostname": "db-01"})
        self.assertEqual(t.address, "db-01")
        self.assertEqual(t.display_name, "db-01")
        t2 = DiscoveredTarget(instance="https://example.com")
        self.assertEqual(t2.address, "example.com")
        self.assertEqual(t2.display_name, "example.com")
        t3 = DiscoveredTarget(instance="10.0.0.1")
        self.assertEqual(t3.address, "10.0.0.1")

    def test_summary_line(self):
        node = DiscoveredTarget(instance="a:1", kind=TargetKind.NODE,
                                details={"os": "Debian 12", "kernel": "Linux 6.1"})
        self.assertEqual(node.summary_line, "Debian 12 · Linux 6.1")
        snmp = DiscoveredTarget(instance="a", kind=TargetKind.SNMP, details={"sysDescr": "Cisco IOS"})
        self.assertEqual(snmp.summary_line, "Cisco IOS")

    def test_unlinked_hosts_queryset(self):
        DiscoveredTarget.objects.create(instance="a:9100", job="node", kind=TargetKind.NODE)
        DiscoveredTarget.objects.create(instance="b:9100", job="node", kind=TargetKind.NODE,
                                        state=DiscoveredTarget.IGNORED)
        DiscoveredTarget.objects.create(instance="c:9100", job="node", kind=TargetKind.NODE,
                                        missing_since=timezone.now())
        DiscoveredTarget.objects.create(instance="d", job="snmp", kind=TargetKind.SNMP)
        self.assertEqual([t.instance for t in DiscoveredTarget.objects.for_tenant().unlinked_hosts()], ["a:9100"])

    def test_unique_per_instance_and_job(self):
        from django.db import IntegrityError, transaction
        DiscoveredTarget.objects.create(instance="a:9100", job="node")
        DiscoveredTarget.objects.create(instance="a:9100", job="other")      # a second job may scrape it
        with self.assertRaises(IntegrityError), transaction.atomic():
            DiscoveredTarget.objects.create(instance="a:9100", job="node")


# ----------------------------------------------------------------------------- scrape settings, Windows, kube-state
def _target_with_scrape(instance, job, **extra):
    return {**target(instance, job), **extra}


@override_settings(PROMETHEUS_URL=PROM)
class ScrapeSettingsTests(PromMixin, TestCase):
    targets_data = [_target_with_scrape("db-01:9100", "node", scrapeInterval="1m30s", scrapeTimeout="10s",
                                        lastScrapeDuration=0.0123),
                    _target_with_scrape("web-01:9100", "node", scrapeInterval="15s", scrapeTimeout="banana",
                                        lastScrapeDuration="x"),
                    target("app-02:9100", "node")]

    def test_seconds_parses_prometheus_durations(self):
        self.assertEqual(svc._seconds("15s"), 15)
        self.assertEqual(svc._seconds("1m30s"), 90)
        self.assertEqual(svc._seconds("500ms"), 0.5)
        self.assertEqual(svc._seconds("2h"), 7200)
        self.assertEqual(svc._seconds(0.25), 0.25)
        for bad in ("", None, "abc", "15", "-5s", "1x", True, float("nan"), float("inf"), 1e12, "99999999999999s"):
            self.assertIsNone(svc._seconds(bad), bad)

    def test_interval_timeout_and_duration_are_stored_in_details(self):
        svc.run()
        d = DiscoveredTarget.objects.for_tenant().get(instance="db-01:9100").details
        self.assertEqual(d["scrape"], {"interval": 90.0, "timeout": 10.0, "duration": 0.0123})

    def test_unusable_values_are_dropped_and_a_target_without_any_has_no_scrape_key(self):
        svc.run()
        self.assertEqual(DiscoveredTarget.objects.for_tenant().get(instance="web-01:9100").details["scrape"],
                         {"interval": 15.0})
        self.assertNotIn("scrape", DiscoveredTarget.objects.for_tenant().get(instance="app-02:9100").details)

    def test_scrape_settings_follow_a_config_change_on_the_next_pass(self):
        svc.run()
        self.targets_data = [_target_with_scrape("db-01:9100", "node", scrapeInterval="30s")] + self.targets_data[1:]
        svc.run()
        d = DiscoveredTarget.objects.for_tenant().get(instance="db-01:9100").details
        self.assertEqual(d["scrape"], {"interval": 30.0})

    def test_scrape_settings_are_not_copied_into_a_cmdb_items_attributes(self):
        row = DiscoveredTarget(instance="db-01:9100", job="node", site="Mumbai",
                               details={"hostname": "db-01", "scrape": {"interval": 15.0}, "cluster": {"nodes": 1},
                                        "os": "Debian"})
        ci = svc.create_ci(row)
        if ci is not None:          # the cmdb app is installed in this project
            self.assertEqual(ci.attributes["os"], "Debian")
            self.assertNotIn("scrape", ci.attributes)
            self.assertNotIn("cluster", ci.attributes)

    def test_the_detail_page_shows_the_scrape_settings(self):
        svc.run()
        row = DiscoveredTarget.objects.for_tenant().get(instance="db-01:9100")
        admin = User.objects.create_superuser("scrapeadmin", "a@x.test", "pw-123456-xyz")
        c = Client()
        c.force_login(admin)
        html = c.get(reverse("telemetry:discovery_detail", args=[row.pk])).content.decode()
        self.assertIn("Scrape interval", html)
        self.assertIn("90 s", html)
        self.assertIn("Last scrape took", html)


def fake_dmi_query(q):
    if q == "node_dmi_info":
        return _rows([({"instance": i, "system_vendor": "Dell Inc.", "product_name": "PowerEdge R740"}, 1.0)
                      for i in NODES])
    return []


@override_settings(PROMETHEUS_URL=PROM)
class HardwareTests(PromMixin, TestCase):
    targets_data = TARGETS[:1]
    query_fns = (fake_dmi_query, fake_query)

    def test_vendor_and_model_come_from_node_dmi_info(self):
        svc.run()
        d = DiscoveredTarget.objects.for_tenant().get(instance="db-01:9100").details
        self.assertEqual((d["vendor"], d["model"]), ("Dell Inc.", "PowerEdge R740"))


def fake_windows_extra_query(q):
    if q == "windows_exporter_build_info":
        return _rows([({"instance": "win-01:9182", "version": "0.30.4"}, 1.0)])
    if q.startswith("windows_system_boot_time_timestamp"):
        return _rows([({"instance": "win-01:9182"}, 1762000000.0)])
    return fake_windows_query(q)


@override_settings(PROMETHEUS_URL=PROM)
class WindowsExtraTests(PromMixin, TestCase):
    targets_data = [target("win-01:9182", "windows")]
    query_fns = (fake_windows_extra_query,)

    def test_windows_exporter_version_and_boot_time(self):
        run = svc.run()
        d = DiscoveredTarget.objects.for_tenant().get(instance="win-01:9182").details
        self.assertEqual(d["exporter_version"], "0.30.4")
        self.assertEqual(d["boot_time"], 1762000000)
        self.assertEqual(run.queries, len(svc.IDENTITY_QUERIES) + len(svc.WINDOWS_QUERIES))

    def test_windows_is_detected_by_metric_even_when_the_job_is_called_something_else(self):
        self.targets_data = [target("win-01:9182", "prod-estate")]
        svc.run()
        self.assertEqual(DiscoveredTarget.objects.for_tenant().get(instance="win-01:9182").kind, TargetKind.WINDOWS)


def fake_ksm_query(q):
    inst = "kube-state:8080"
    if q == "kube_state_metrics_build_info":
        return _rows([({"instance": inst, "version": "v2.13.0"}, 1.0)])
    answers = {"count by (instance) (kube_node_info)": 5.0,
               "count by (instance) (kube_pod_info)": 120.0,
               "count by (instance) (kube_namespace_created)": 14.0,
               "count by (instance) (kube_deployment_created)": 33.0}
    if q in answers:
        return _rows([({"instance": inst}, answers[q])])
    if "kube_node_status_condition" in q:
        return _rows([({"instance": inst}, 4.0)])
    if "kube_pod_status_phase" in q:
        return _rows([({"instance": inst}, 110.0)])
    return []


@override_settings(PROMETHEUS_URL=PROM)
class KubeStateTests(PromMixin, TestCase):
    targets_data = [target("kube-state:8080", "kube-state-metrics"), target("db-01:9100", "node")]
    query_fns = (fake_ksm_query, fake_query)

    def test_cluster_counts_are_stored_on_the_kube_state_target(self):
        run = svc.run()
        row = DiscoveredTarget.objects.for_tenant().get(instance="kube-state:8080")
        self.assertEqual(row.kind, TargetKind.KUBE_STATE)
        self.assertEqual(row.details["cluster"], {"nodes": 5, "nodes_ready": 4, "pods": 120, "pods_running": 110,
                                                  "namespaces": 14, "deployments": 33})
        self.assertEqual(row.details["exporter_version"], "v2.13.0")
        self.assertEqual(run.queries, len(svc.IDENTITY_QUERIES) + len(svc.NODE_QUERIES) + len(svc.KSM_QUERIES))

    def test_the_kube_state_queries_are_not_asked_when_no_target_looks_like_kube_state_metrics(self):
        self.targets_data = [target("db-01:9100", "node")]
        run = svc.run()
        self.assertEqual(run.queries, len(svc.IDENTITY_QUERIES) + len(svc.NODE_QUERIES))

    def test_kube_state_counts_never_land_on_a_node_host(self):
        svc.run()
        self.assertNotIn("cluster", DiscoveredTarget.objects.for_tenant().get(instance="db-01:9100").details)

    def test_a_partial_answer_stores_only_what_was_reported(self):
        def partial(q):
            if q == "count by (instance) (kube_node_info)":
                return _rows([({"instance": "kube-state:8080"}, 3.0)])
            return []
        self.query_fns = (partial,)
        svc.run()
        row = DiscoveredTarget.objects.for_tenant().get(instance="kube-state:8080")
        self.assertEqual(row.details["cluster"], {"nodes": 3})

    def test_the_detail_page_shows_the_cluster_size(self):
        svc.run()
        row = DiscoveredTarget.objects.for_tenant().get(instance="kube-state:8080")
        admin = User.objects.create_superuser("ksmadmin", "a@x.test", "pw-123456-xyz")
        c = Client()
        c.force_login(admin)
        html = c.get(reverse("telemetry:discovery_detail", args=[row.pk])).content.decode()
        self.assertIn("Kubernetes nodes", html)
        self.assertIn("4 ready", html)
        self.assertIn("110 running", html)
        self.assertIn("Cluster Details", html)
