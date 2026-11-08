"""Discovery robustness: job-name kinds, flapping, stale links, NUL bytes, worker-thread connections, cache keys."""
from unittest import mock

from django.apps import apps
from django.core.cache import cache
from django.test import TestCase, override_settings

from core import prometheus

from . import discovery as svc
from .models import DiscoveredTarget, TargetKind
from .tests_discovery import PROM, NODES, PromMixin, fake_query, target


class JobNameKindTests(TestCase):
    """What the usual job names of a Prometheus configuration are taken for when no metric answers."""

    def test_common_job_names(self):
        cases = {
            "node-exporter": TargetKind.NODE, "nodes": TargetKind.NODE, "prometheus-node": TargetKind.NODE,
            "node_exporter": TargetKind.NODE, "kubernetes-nodes": TargetKind.NODE,
            "windows": TargetKind.WINDOWS, "windows_exporter": TargetKind.WINDOWS,
            "snmp-cisco": TargetKind.SNMP, "snmp": TargetKind.SNMP,
            "blackbox-http": TargetKind.BLACKBOX, "blackbox": TargetKind.BLACKBOX,
            "kube-state-metrics": TargetKind.KUBE_STATE, "kube_state_metrics": TargetKind.KUBE_STATE,
            "kubernetes-cadvisor": TargetKind.CADVISOR,
            "prometheus": TargetKind.OTHER, "alertmanager": TargetKind.OTHER, "grafana": TargetKind.OTHER,
            "": TargetKind.OTHER,
        }
        for job, expected in cases.items():
            self.assertEqual(svc.detect_kind(job), expected, job)

    def test_uppercase_job_names(self):
        self.assertEqual(svc.detect_kind("Node-Exporter"), TargetKind.NODE)
        self.assertEqual(svc.detect_kind("SNMP-Cisco"), TargetKind.SNMP)


@override_settings(PROMETHEUS_URL=PROM)
class FlappingTests(PromMixin, TestCase):
    def test_a_failed_identity_probe_does_not_change_a_known_kind(self):
        """The job is called "prod", so only the metrics say it is a Linux host. When the probes fail for one
        pass the host must stay a node instead of flipping to "other" and back."""
        self.targets_data = [target("db-01:9100", "prod")]
        svc.run()
        row = DiscoveredTarget.objects.for_tenant().get(instance="db-01:9100")
        self.assertEqual(row.kind, TargetKind.NODE)
        with mock.patch("telemetry.promapi.query", side_effect=prometheus.PrometheusError("down")):
            run = svc.run()
        self.assertTrue(run.ok)
        row.refresh_from_db()
        self.assertEqual(row.kind, TargetKind.NODE)
        self.assertEqual(row.details["hostname"], "db-01")     # the details were kept as well

    def test_a_new_target_with_failed_probes_falls_back_to_the_job_name(self):
        self.targets_data = [target("db-01:9100", "node-exporter")]
        with mock.patch("telemetry.promapi.query", side_effect=prometheus.PrometheusError("down")):
            svc.run()
        self.assertEqual(DiscoveredTarget.objects.for_tenant().get().kind, TargetKind.NODE)


@override_settings(PROMETHEUS_URL=PROM)
class StaleLinkTests(PromMixin, TestCase):
    def test_a_deleted_item_puts_the_target_back_to_new(self):
        CI = apps.get_model("cmdb", "ConfigurationItem")
        ci = CI.objects.create(name="db-01", site="Mumbai", prometheus_instance="db-01:9100")
        svc.run()
        row = DiscoveredTarget.objects.for_tenant().get(instance="db-01:9100")
        self.assertEqual(row.state, DiscoveredTarget.LINKED)
        ci.delete()
        svc.run()
        row.refresh_from_db()
        self.assertIsNone(row.ci_id)
        self.assertEqual(row.state, DiscoveredTarget.NEW)
        self.assertIn(row.pk, [t.pk for t in DiscoveredTarget.objects.for_tenant().unlinked_hosts()])

    def test_ignored_stays_ignored_when_the_item_is_deleted(self):
        CI = apps.get_model("cmdb", "ConfigurationItem")
        ci = CI.objects.create(name="db-01", site="Mumbai", prometheus_instance="db-01:9100")
        svc.run()
        DiscoveredTarget.objects.for_tenant().update(state=DiscoveredTarget.IGNORED)
        ci.delete()
        svc.run()
        self.assertEqual(DiscoveredTarget.objects.for_tenant().get(instance="db-01:9100").state,
                         DiscoveredTarget.IGNORED)


@override_settings(PROMETHEUS_URL=PROM)
class NulByteTests(PromMixin, TestCase):
    def test_nul_characters_never_reach_the_database(self):
        """PostgreSQL refuses \\u0000 in text and jsonb columns: a hostile label must not abort the pass."""
        t = target("db-01:9100", "node", site="Mum\x00bai", error="bad\x00 thing")
        t["labels"]["owner"] = "a\x00b"
        self.targets_data = [t]

        def query(q):
            rows = fake_query(q)
            for r in rows:
                if r["metric"].get("nodename"):
                    r["metric"]["nodename"] = "db\x00-01"
            return rows
        with mock.patch("telemetry.promapi.query", side_effect=query):
            run = svc.run()
        self.assertTrue(run.ok, run.error)
        row = DiscoveredTarget.objects.for_tenant().get()
        self.assertEqual(row.site, "Mumbai")
        self.assertEqual(row.last_error, "bad thing")
        self.assertEqual(row.labels["owner"], "ab")
        self.assertEqual(row.details["hostname"], "db-01")
        self.assertNotIn("\x00", repr(row.details) + repr(row.labels) + row.scrape_url + row.instance)


@override_settings(PROMETHEUS_URL=PROM)
class WorkerThreadTests(PromMixin, TestCase):
    def test_pool_threads_close_their_database_connection(self):
        """Queries run in a thread pool; a thread that read the integration settings from the database must
        not leave its connection open."""
        with mock.patch("telemetry.discovery.connections") as conns:
            svc.run()
        # 3 identity probes + the Linux and kube-state-metrics detail queries, one close per worker call
        self.assertEqual(conns.close_all.call_count,
                         len(svc.IDENTITY_QUERIES) + len(svc.NODE_QUERIES) + len(svc.KSM_QUERIES))

    def test_a_failing_query_still_closes_the_connection(self):
        with mock.patch("telemetry.discovery.connections") as conns, \
                mock.patch("telemetry.promapi.query", side_effect=prometheus.PrometheusError("x")):
            with self.assertRaises(prometheus.PrometheusError):
                svc._query("up")
        conns.close_all.assert_called_once()


@override_settings(PROMETHEUS_URL=PROM)
class RulesCacheKeyTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_the_rules_cache_is_kept_per_tenant(self):
        rules = [{"name": "g", "file": "f", "rules": []}]
        other = [{"name": "other", "file": "f", "rules": []}]
        with override_settings(LINKEDEYE_TENANT="tenant-a"), mock.patch("telemetry.promapi.rules", return_value=rules):
            self.assertEqual(svc.rule_groups()[0]["name"], "g")
        with override_settings(LINKEDEYE_TENANT="tenant-b"), mock.patch("telemetry.promapi.rules", return_value=other):
            self.assertEqual(svc.rule_groups()[0]["name"], "other")
        with override_settings(LINKEDEYE_TENANT="tenant-a"), mock.patch("telemetry.promapi.rules",
                                                                         side_effect=AssertionError("cached")):
            self.assertEqual(svc.rule_groups()[0]["name"], "g")


@override_settings(PROMETHEUS_URL=PROM)
class ConcurrentPassTests(PromMixin, TestCase):
    def test_two_passes_in_a_row_with_the_same_clock_leave_one_row_per_target(self):
        now = svc.timezone.now()
        svc.run(now=now)
        svc.run(now=now)
        self.assertEqual(DiscoveredTarget.objects.for_tenant().count(), 6)
        self.assertEqual(DiscoveredTarget.objects.for_tenant().filter(instance__in=NODES).count(), 3)


@override_settings(PROMETHEUS_URL=PROM)
class PartialFailureTests(PromMixin, TestCase):
    def test_a_failed_detail_query_keeps_the_details_an_earlier_pass_learned(self):
        svc.run()
        before = DiscoveredTarget.objects.for_tenant().get(instance="db-01:9100").details
        self.assertEqual(before["os"], "Debian GNU/Linux 12 (bookworm)")

        def query(q):
            if q == "node_os_info":
                raise prometheus.PrometheusError("detail boom")
            return fake_query(q)
        with mock.patch("telemetry.promapi.query", side_effect=query):
            svc.run()
        after = DiscoveredTarget.objects.for_tenant().get(instance="db-01:9100").details
        self.assertEqual(after["os"], before["os"])
        self.assertEqual(after["hostname"], "db-01")
