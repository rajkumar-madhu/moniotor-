"""Activation (CMDB item, device or compute server, exporter, service discovery), the liveness job
that completes a run, and the Done page with its links. Every Prometheus call is mocked; the base
class makes any real socket attempt fail the test."""
import logging
from html import escape
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from cmdb.models import ConfigurationItem
from compute.models import Cluster, ClusterKind, ClusterMember
from core import prometheus, secrets
from core.models import AuditEvent, Event
from network.models import Device, DeviceType
from observe.models import Exporter, LogEntry
from onboarding import activate, jobs, sd, services, steps
from onboarding.models import (CredentialKind, OnboardingRun, OnboardingTarget, RunStatus, StepLog, TargetState,
                               TargetType)
from telemetry.models import Alert

from .base import PASSWORD, SECRET, OnboardingTestCase, with_key


def step_url(run, name):
    return reverse("onboarding:step", args=[run.pk, name])


def up_rows(*instances, job="node", with_target=None):
    rows = []
    for i, inst in enumerate(instances):
        metric = {"instance": inst, "job": job}
        if with_target:
            metric["le_target"] = str(with_target[i])
        rows.append({"metric": metric, "value": 1.0})
    return rows


def prom(rows=None, error=None, configured=True):
    """Patch core.prometheus: every query returns `rows` (or raises `error`)."""
    stack = [mock.patch.object(prometheus, "configured", return_value=configured)]
    if error is not None:
        stack.append(mock.patch.object(prometheus, "query", side_effect=error))
    else:
        stack.append(mock.patch.object(prometheus, "query", return_value=rows or []))

    class Both:
        def __enter__(self_inner):
            for p in stack:
                p.start()

        def __exit__(self_inner, *exc):
            for p in reversed(stack):
                p.stop()
    return Both()


@with_key
class ActivateTests(OnboardingTestCase):
    def setUp(self):
        super().setUp()
        self.run_ = self.make_run(environment="prod")
        self.t = self.make_target(self.run_, "db-01", address="10.0.3.14", exporter_kind="node_exporter",
                                  facts={"os": "Debian 12", "kernel": "6.1.0", "cpus": 8, "memory_mb": 16000,
                                         "exporter": "node_exporter", "exporter_version": "1.8.1"})

    # ------------------------------------------------------------------ what a first activation creates
    def test_creates_the_cmdb_item_the_exporter_and_joins_service_discovery(self):
        out = steps.activate(self.t)
        self.t.refresh_from_db()
        ci = ConfigurationItem.objects.get()
        self.assertEqual((ci.name, ci.site, ci.prometheus_instance, ci.ip_address, ci.environment),
                         ("db-01", "Mumbai", "10.0.3.14:9100", "10.0.3.14", "prod"))
        self.assertEqual(ci.ci_type, "server")
        self.assertEqual((ci.attributes["os"], ci.attributes["cpus"], ci.attributes["onboarding_run"]),
                         ("Debian 12", 8, self.run_.pk))
        exporter = Exporter.objects.get()
        self.assertEqual((exporter.exporter_type, exporter.category, exporter.site, exporter.target),
                         ("Node Exporter", "Servers", "Mumbai", "db-01"))
        self.assertEqual(exporter.metrics_url, "http://10.0.3.14:9100/metrics")
        self.assertEqual(exporter.status, "not_configured")           # until the first scrape
        self.assertEqual((self.t.ci_id, self.t.exporter_id, self.t.device_id), (ci.pk, exporter.pk, None))
        self.assertFalse(Device.objects.exists())                      # a plain Linux server is not a device
        self.assertTrue(self.t.enabled)
        self.assertEqual(out["state"], TargetState.SCRAPING)
        self.assertEqual(out["created"], ["cmdb item", "exporter"])
        self.assertEqual((out["ci"], out["exporter"], out["device"]), (ci.pk, exporter.pk, None))
        self.assertTrue(out["service_discovery"])

    def test_the_target_is_served_by_service_discovery_after_activation(self):
        self.assertEqual(sd.targets_payload(), [])
        result = steps.activate(self.t)
        services.set_target_state(self.t, result["state"])
        payload = sd.targets_payload()
        self.assertEqual(payload[0]["targets"], ["10.0.3.14:9100"])
        self.assertEqual(payload[0]["labels"]["le_hostname"], "db-01")

    def test_writes_a_step_log_line_and_an_audit_row(self):
        steps.activate(self.t)
        line = StepLog.objects.get(run=self.run_, step="confirm", status="ok")
        self.assertIn("created cmdb item, exporter", line.detail)
        self.assertIn("joined service discovery", line.detail)
        self.assertEqual(line.target_id, self.t.pk)
        row = AuditEvent.objects.get(action="onboarding.activate.target")
        self.assertEqual(row.details["hostname"], "db-01")

    def test_the_hook_is_wired_and_no_longer_pending(self):
        self.assertIsInstance(steps.activate(self.t), dict)         # no StepPending any more

    # ------------------------------------------------------------------ idempotency
    def test_running_it_twice_creates_nothing_new(self):
        first = steps.activate(self.t)
        counts = (ConfigurationItem.objects.count(), Exporter.objects.count(), Device.objects.count())
        self.t.refresh_from_db()
        second = steps.activate(self.t)
        self.assertEqual(counts, (ConfigurationItem.objects.count(), Exporter.objects.count(), Device.objects.count()))
        self.assertEqual((first["ci"], first["exporter"]), (second["ci"], second["exporter"]))
        self.assertEqual(second["created"], [])
        self.assertEqual(second["linked"], ["cmdb item", "exporter"])

    def test_a_live_target_stays_live_when_activated_again(self):
        steps.activate(self.t)
        services.set_target_state(self.t, TargetState.LIVE)
        self.t.refresh_from_db()
        self.assertEqual(steps.activate(self.t)["state"], TargetState.LIVE)

    def test_a_second_run_for_the_same_host_links_to_the_same_records(self):
        steps.activate(self.t)
        run2 = self.make_run()
        t2 = self.make_target(run2, "db-01", address="10.0.3.14", exporter_kind="node_exporter")
        out = steps.activate(t2)
        self.assertEqual(out["created"], [])
        self.assertEqual((ConfigurationItem.objects.count(), Exporter.objects.count()), (1, 1))
        t2.refresh_from_db()
        self.t.refresh_from_db()
        self.assertEqual((t2.ci_id, t2.exporter_id), (self.t.ci_id, self.t.exporter_id))

    def test_the_wizard_activate_button_twice_does_not_duplicate(self):
        self.login(self.admin_a)
        for _ in range(2):
            self.c.post(step_url(self.run_, "confirm"), {"action": "activate"})
        self.assertEqual((ConfigurationItem.objects.count(), Exporter.objects.count()), (1, 1))
        self.t.refresh_from_db()
        self.assertEqual(self.t.state, TargetState.SCRAPING)
        self.assertEqual(self.t.facts["activation"]["linked"], ["cmdb item", "exporter"])
        self.assertTrue(AuditEvent.objects.filter(action="onboarding.activate").exists())

    # ------------------------------------------------------------------ linking to what is already known
    def test_links_to_an_existing_item_by_name_and_only_fills_blanks(self):
        old = ConfigurationItem.objects.create(name="DB-01", site="Mumbai", owner="dba-team", vendor="Dell",
                                               attributes={"rack": "R12", "os": "RHEL 9"})
        out = steps.activate(self.t)
        old.refresh_from_db()
        self.assertEqual(ConfigurationItem.objects.count(), 1)
        self.assertEqual(out["ci"], old.pk)
        self.assertEqual(out["created"], ["exporter"])
        self.assertEqual((old.prometheus_instance, old.ip_address, old.owner, old.vendor, old.name),
                         ("10.0.3.14:9100", "10.0.3.14", "dba-team", "Dell", "DB-01"))
        self.assertEqual((old.attributes["rack"], old.attributes["os"]), ("R12", "RHEL 9"))   # not overwritten
        self.assertEqual(old.attributes["kernel"], "6.1.0")                                    # filled in

    def test_links_by_prometheus_instance_even_when_the_name_differs(self):
        old = ConfigurationItem.objects.create(name="oms-primary", site="Mumbai", prometheus_instance="10.0.3.14:9100")
        self.assertEqual(steps.activate(self.t)["ci"], old.pk)
        self.assertEqual(ConfigurationItem.objects.count(), 1)

    def test_an_item_with_another_instance_is_kept_and_the_operator_is_told(self):
        old = ConfigurationItem.objects.create(name="db-01", site="Mumbai", prometheus_instance="db-01.corp:9100")
        out = steps.activate(self.t)
        old.refresh_from_db()
        self.assertEqual(old.prometheus_instance, "db-01.corp:9100")
        self.assertTrue(any("db-01.corp:9100" in w for w in out["warnings"]))
        self.assertTrue(StepLog.objects.filter(run=self.run_, status="warn", detail__contains="db-01.corp").exists())

    def test_an_item_on_another_site_is_never_stolen(self):
        other = ConfigurationItem.objects.create(name="db-01", site="Chennai", prometheus_instance="")
        out = steps.activate(self.t)
        other.refresh_from_db()
        self.assertEqual(other.prometheus_instance, "")
        self.assertNotEqual(out["ci"], other.pk)
        mine = ConfigurationItem.objects.get(pk=out["ci"])
        self.assertEqual((mine.name, mine.site), ("db-01 (Mumbai)", "Mumbai"))
        self.assertEqual(steps.activate(self.t)["ci"], mine.pk)         # and the re-run finds its own again

    def test_an_item_for_all_sites_may_be_linked(self):
        shared = ConfigurationItem.objects.create(name="db-01", site="All Sites")
        self.assertEqual(steps.activate(self.t)["ci"], shared.pk)

    def test_links_to_an_existing_exporter_by_url_and_keeps_its_settings(self):
        exp = Exporter.objects.create(name="Mumbai DB node", exporter_type="Node Exporter", category="Servers",
                                      metrics_url="http://10.0.3.14:9100/metrics", site="Mumbai", description="mine")
        out = steps.activate(self.t)
        exp.refresh_from_db()
        self.assertEqual((out["exporter"], Exporter.objects.count(), exp.description), (exp.pk, 1, "mine"))

    def test_an_exporter_without_a_url_gets_one(self):
        exp = Exporter.objects.create(name="node_exporter db-01", exporter_type="Node Exporter", site="Mumbai")
        steps.activate(self.t)
        exp.refresh_from_db()
        self.assertEqual(exp.metrics_url, "http://10.0.3.14:9100/metrics")
        self.assertEqual(exp.last_error, "Waiting for the first scrape")

    def test_records_of_another_tenant_are_not_matched(self):
        ConfigurationItem.objects.create(tenant_key="other-tenant", name="db-01", site="Mumbai")
        out = steps.activate(self.t)
        self.assertEqual(ConfigurationItem.objects.get(pk=out["ci"]).tenant_key, "default")

    # ------------------------------------------------------------------ devices and compute servers
    def test_an_snmp_target_gets_a_network_device_with_the_plain_host_as_instance(self):
        t = self.make_target(self.run_, "sw-core-1", address="10.0.9.1", target_type=TargetType.DEVICE, port=9116,
                             exporter_kind="snmp_exporter",
                             facts={"snmp": {"sysDescr": "Cisco IOS 15.2"}, "snmp_settings": {
                                 "version": "v2c", "module": "if_mib", "auth_profile": "public_v2"}})
        with mock.patch("onboarding.devices.exporter_url", return_value="http://snmp-exporter:9116"):
            out = steps.activate(t)
        device = Device.objects.get()
        self.assertEqual((device.name, device.device_type, device.mgmt_ip, device.prometheus_instance, device.site),
                         ("sw-core-1", DeviceType.SWITCH, "10.0.9.1", "10.0.9.1", "Mumbai"))
        self.assertEqual(device.firmware, "Cisco IOS 15.2")
        ci = ConfigurationItem.objects.get(name="sw-core-1")
        self.assertEqual((ci.ci_type, ci.prometheus_instance), ("network_device", "10.0.9.1"))
        exp = Exporter.objects.get(name="snmp_exporter sw-core-1")
        self.assertEqual((exp.exporter_type, exp.category), ("SNMP", "Network"))
        self.assertEqual(exp.metrics_url,
                         "http://snmp-exporter:9116/snmp?target=10.0.9.1&module=if_mib&auth=public_v2")
        t.refresh_from_db()
        self.assertEqual((t.device_id, out["created"]), (device.pk, ["cmdb item", "network device", "exporter"]))
        with mock.patch("onboarding.devices.exporter_url", return_value="http://snmp-exporter:9116"):
            again = steps.activate(t)
        self.assertEqual((Device.objects.count(), ConfigurationItem.objects.count(), Exporter.objects.count()),
                         (1, 1, 1))
        self.assertEqual(again["created"], [])

    def test_an_snmp_device_known_by_ip_is_linked_not_duplicated(self):
        known = Device.objects.create(name="core-sw", mgmt_ip="10.0.9.1", site="Mumbai")
        t = self.make_target(self.run_, "sw-core-1", address="10.0.9.1", target_type=TargetType.DEVICE, port=9116)
        out = steps.activate(t)
        known.refresh_from_db()
        self.assertEqual((out["device"], Device.objects.count(), known.prometheus_instance), (known.pk, 1, "10.0.9.1"))

    def test_an_snmp_exporter_that_is_not_set_leaves_the_exporter_row_unconfigured(self):
        t = self.make_target(self.run_, "sw-2", target_type=TargetType.DEVICE, port=9116)
        with mock.patch("onboarding.devices.exporter_url", return_value=""):
            steps.activate(t)
        exp = Exporter.objects.get(name="snmp_exporter sw-2")
        self.assertEqual((exp.metrics_url, exp.status), ("", "not_configured"))

    def test_a_kubernetes_node_becomes_a_compute_server_and_joins_its_cluster(self):
        cluster = Cluster.objects.create(name="prod-k8s", kind=ClusterKind.KUBERNETES, site="Mumbai")
        Cluster.objects.create(name="other", kind=ClusterKind.KUBERNETES, site="Chennai")
        t = self.make_target(self.run_, "k8s-w1", target_type=TargetType.K8S_NODE, port=8080,
                             exporter_kind="kube-state-metrics", facts={"cluster": "prod-k8s"})
        out = steps.activate(t)
        device = Device.objects.get()
        self.assertEqual((device.device_type, device.prometheus_instance, device.site),
                         (DeviceType.PHYSICAL_SERVER, "k8s-w1:8080", "Mumbai"))
        member = ClusterMember.objects.get()
        self.assertEqual((member.cluster_id, member.device_id, member.role), (cluster.pk, device.pk, "worker"))
        self.assertEqual(out["created"], ["cmdb item", "compute server", "exporter"])
        self.assertIn("cluster membership", out["linked"])
        steps.activate(t)
        self.assertEqual((Device.objects.count(), ClusterMember.objects.count()), (1, 1))
        self.assertEqual(Exporter.objects.get().category, "Kubernetes")

    def test_a_cluster_on_another_site_or_unknown_is_not_joined(self):
        Cluster.objects.create(name="chennai-k8s", kind=ClusterKind.KUBERNETES, site="Chennai")
        t = self.make_target(self.run_, "k8s-w2", target_type=TargetType.K8S_NODE, facts={"cluster": "chennai-k8s"})
        t2 = self.make_target(self.run_, "k8s-w3", target_type=TargetType.K8S_NODE, facts={"cluster": "nope"})
        steps.activate(t)
        steps.activate(t2)
        self.assertFalse(ClusterMember.objects.exists())
        self.assertEqual(Cluster.objects.count(), 1 + 0)         # a cluster is never created

    def test_a_hypervisor_host_is_a_compute_server_too(self):
        t = self.make_target(self.run_, "esx-01", facts={"hypervisor": True})
        steps.activate(t)
        self.assertEqual(Device.objects.get().device_type, DeviceType.PHYSICAL_SERVER)

    def test_a_url_target_is_a_service_item_with_a_blackbox_exporter(self):
        t = self.make_target(self.run_, "portal.example.com", target_type=TargetType.URL, port=443,
                             exporter_kind="blackbox_exporter")
        steps.activate(t)
        self.assertEqual(ConfigurationItem.objects.get(name="portal.example.com").ci_type, "service")
        self.assertEqual(Exporter.objects.get().exporter_type, "Blackbox (HTTP/TCP)")
        self.assertFalse(Device.objects.exists())

    # ------------------------------------------------------------------ states
    def test_an_unreachable_target_is_registered_with_a_warning(self):
        services.set_target_state(self.t, TargetState.UNREACHABLE, error="connection refused")
        out = steps.activate(self.t)
        self.assertEqual(out["state"], TargetState.SCRAPING)
        self.assertTrue(any("unreachable" in w for w in out["warnings"]))
        self.assertTrue(StepLog.objects.filter(run=self.run_, status="warn", detail__contains="unreachable").exists())

    def test_an_unverified_target_is_registered_with_a_note(self):
        out = steps.activate(self.t)
        self.assertTrue(any("not been verified" in w for w in out["warnings"]))

    def test_a_failure_halfway_leaves_nothing_behind(self):
        with mock.patch.object(activate, "link_exporter", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                steps.activate(self.t)
        self.assertEqual((ConfigurationItem.objects.count(), Exporter.objects.count()), (0, 0))
        self.t.refresh_from_db()
        self.assertIsNone(self.t.ci_id)

    def test_the_wizard_records_an_exception_by_type_only(self):
        self.login(self.admin_a)
        with mock.patch.object(activate, "link_exporter", side_effect=RuntimeError(SECRET)):
            self.c.post(step_url(self.run_, "confirm"), {"action": "activate"})
        details = " ".join(StepLog.objects.values_list("detail", flat=True))
        self.assertIn("RuntimeError", details)
        self.assertNotIn(SECRET, details)

    # ------------------------------------------------------------------ secrets
    def test_a_stored_credential_never_reaches_a_record_a_log_or_the_result(self):
        cred = secrets.store(CredentialKind.SSH_PASSWORD, "svc", SECRET, self.run_.site, self.admin_a, name="m-ssh")
        self.t.credential = cred
        self.t.save(update_fields=["credential"])

        class Capture(logging.Handler):
            lines = []

            def emit(self, record):
                self.lines.append(record.getMessage())
        cap = Capture()
        logging.getLogger().addHandler(cap)
        self.addCleanup(logging.getLogger().removeHandler, cap)
        out = steps.activate(self.t)
        blob = " ".join([
            str(out), " ".join(cap.lines),
            " ".join(StepLog.objects.values_list("detail", flat=True)),
            str(list(AuditEvent.objects.values_list("details", flat=True))),
            str(list(ConfigurationItem.objects.values_list("attributes", "description"))),
            str(list(Exporter.objects.values_list("description", "metrics_url", "last_error")))])
        self.assertNotIn(SECRET, blob)
        self.assertNotIn(cred.secret, blob)
        cred.refresh_from_db()
        self.assertIsNone(cred.last_used_at)            # activation never reads the vault

    # ------------------------------------------------------------------ access
    def test_only_an_admin_for_the_site_can_activate(self):
        for user in (self.viewer, self.operator, self.admin_b):
            self.login(user)
            resp = self.c.post(step_url(self.run_, "confirm"), {"action": "activate"})
            self.assertIn(resp.status_code, (403, 404), user.get_username())
        self.assertFalse(ConfigurationItem.objects.exists())

    def test_activation_needs_post_and_csrf(self):
        from django.test import Client
        self.login(self.admin_a)
        self.assertEqual(self.c.get(step_url(self.run_, "confirm")).status_code, 200)
        self.assertFalse(ConfigurationItem.objects.exists())            # a GET changes nothing
        strict = Client(enforce_csrf_checks=True)
        strict.login(username="admin-a", password=PASSWORD)
        self.assertEqual(strict.post(step_url(self.run_, "confirm"), {"action": "activate"}).status_code, 403)
        self.assertFalse(ConfigurationItem.objects.exists())


@with_key
class PollTests(OnboardingTestCase):
    """The background job: up == 1 with samples -> live, first_sample_at, run complete, one event."""

    def setUp(self):
        super().setUp()
        self.run_ = self.make_run(step="confirm", status=RunStatus.WAITING_FOR_DATA)
        self.a = self.make_target(self.run_, "db-01", state=TargetState.SCRAPING)
        self.b = self.make_target(self.run_, "web-01", state=TargetState.SCRAPING)

    def reload(self):
        for obj in (self.run_, self.a, self.b):
            obj.refresh_from_db()

    def test_both_live_completes_the_run_stamps_the_time_and_emits_one_event(self):
        with prom(up_rows("db-01:9100", "web-01:9100")):
            out = activate.poll()
        self.reload()
        self.assertEqual((self.a.state, self.b.state), (TargetState.LIVE, TargetState.LIVE))
        self.assertIsNotNone(self.a.first_sample_at)
        self.assertEqual(self.run_.status, RunStatus.COMPLETE)
        self.assertIsNotNone(self.run_.finished_at)
        self.assertEqual((out["live"], out["completed"], out["checked"]), (2, 1, 2))
        self.assertEqual(Event.objects.filter(source="onboarding", title__contains="complete").count(), 1)
        self.assertTrue(StepLog.objects.filter(run=self.run_, status="ok", detail__contains="first samples").exists())
        with prom(up_rows("db-01:9100", "web-01:9100")):         # a second pass finds nothing waiting
            again = activate.poll()
        self.assertEqual((again["checked"], again["live"], again["completed"]), (0, 0, 0))
        self.assertEqual(Event.objects.filter(source="onboarding", title__contains="complete").count(), 1)

    def test_one_live_target_does_not_complete_the_run(self):
        with prom(up_rows("db-01:9100")):
            out = activate.poll()
        self.reload()
        self.assertEqual((self.a.state, self.b.state), (TargetState.LIVE, TargetState.SCRAPING))
        self.assertEqual((self.run_.status, out["live"], out["completed"]), (RunStatus.WAITING_FOR_DATA, 1, 0))
        self.assertFalse(Event.objects.filter(source="onboarding", title__contains="complete").exists())
        with prom(up_rows("db-01:9100", "web-01:9100")):
            self.assertEqual(activate.poll()["completed"], 1)
        self.reload()
        self.assertEqual(self.run_.status, RunStatus.COMPLETE)

    def test_matches_the_le_target_label_the_service_discovery_endpoint_adds(self):
        with prom(up_rows("10.9.9.9:9100", "10.9.9.8:9100", with_target=[self.a.pk, self.b.pk])):
            self.assertEqual(activate.poll()["live"], 2)

    def test_up_without_samples_is_not_live_yet(self):
        def query(promql, at=None):
            return [] if "scrape_samples_scraped" in promql else up_rows("db-01:9100")
        with mock.patch.object(prometheus, "configured", return_value=True), \
                mock.patch.object(prometheus, "query", side_effect=query):
            out = activate.poll()
        self.reload()
        self.assertEqual((self.a.state, out["live"]), (TargetState.SCRAPING, 0))
        self.assertIsNone(self.a.first_sample_at)

    def test_samples_without_up_is_not_live_either(self):
        def query(promql, at=None):
            return up_rows("db-01:9100") if "scrape_samples_scraped" in promql else []
        with mock.patch.object(prometheus, "configured", return_value=True), \
                mock.patch.object(prometheus, "query", side_effect=query):
            self.assertEqual(activate.poll()["live"], 0)

    def test_a_down_target_keeps_waiting(self):
        rows = [{"metric": {"instance": "db-01:9100"}, "value": 0.0}]
        def query(promql, at=None):
            return rows if "samples" in promql else []          # `up == 1` filters the 0 out server side
        with mock.patch.object(prometheus, "configured", return_value=True), \
                mock.patch.object(prometheus, "query", side_effect=query):
            self.assertEqual(activate.poll()["live"], 0)

    def test_prometheus_unreachable_or_not_connected_changes_nothing_and_never_raises(self):
        with prom(error=prometheus.PrometheusError("down")):
            out = activate.poll()
        self.assertEqual((out["live"], out["note"]), (0, "Prometheus did not answer"))
        with prom(configured=False):
            self.assertEqual(activate.poll()["note"], "Prometheus is not connected")
        self.reload()
        self.assertEqual((self.a.state, self.b.state), (TargetState.SCRAPING, TargetState.SCRAPING))
        with prom(error=RuntimeError("bug")):
            self.assertEqual(activate.poll()["note"], "error")

    def test_failed_runs_and_held_back_targets_are_skipped(self):
        self.b.enabled = False
        self.b.save(update_fields=["enabled"])
        failed = self.make_run(status=RunStatus.FAILED)
        c = self.make_target(failed, "old-01", state=TargetState.SCRAPING)
        with prom(up_rows("db-01:9100", "web-01:9100", "old-01:9100")):
            out = activate.poll()
        self.reload()
        c.refresh_from_db()
        self.assertEqual((self.a.state, self.b.state, c.state),
                         (TargetState.LIVE, TargetState.SCRAPING, TargetState.SCRAPING))
        self.assertEqual(out["checked"], 1)

    def test_a_completed_run_still_promotes_a_late_target(self):
        services.complete_run(self.run_)
        with prom(up_rows("db-01:9100")):
            self.assertEqual(activate.poll()["live"], 1)

    def test_only_the_current_tenant_is_polled(self):
        other = OnboardingRun.objects.create(tenant_key="other-tenant", site="Mumbai", step="confirm")
        o = OnboardingTarget.objects.create(run=other, hostname="db-01", state=TargetState.SCRAPING)
        with prom(up_rows("db-01:9100")):
            activate.poll()
        o.refresh_from_db()
        self.assertEqual(o.state, TargetState.SCRAPING)

    def test_after_fifteen_minutes_it_says_once_what_to_check(self):
        old = (timezone.now() - timedelta(minutes=16)).isoformat()
        services.set_target_state(self.a, TargetState.SCRAPING, facts={"activation": {"activated_at": old}})
        for _ in range(2):
            with prom([]):
                activate.poll()
        self.a.refresh_from_db()
        self.assertIn("scrape_config", self.a.last_error)
        self.assertEqual(StepLog.objects.filter(run=self.run_, status="warn", detail__contains="scrape_config").count(), 1)
        self.b.refresh_from_db()
        self.assertEqual(self.b.last_error, "")                 # no activation time recorded: nothing to say

    def test_a_recent_activation_is_not_nagged_about(self):
        now = timezone.now().isoformat()
        services.set_target_state(self.a, TargetState.SCRAPING, facts={"activation": {"activated_at": now}})
        with prom([]):
            activate.poll()
        self.a.refresh_from_db()
        self.assertEqual(self.a.last_error, "")

    def test_the_job_is_registered_every_sixty_seconds_and_reports(self):
        from core.jobs import JOBS
        self.assertEqual(JOBS["onboarding.liveness"]["interval"], 60)
        self.assertIs(JOBS["onboarding.liveness"]["func"], jobs.liveness)
        with prom(up_rows("db-01:9100", "web-01:9100")):
            text = jobs.liveness()
        self.assertIn("2 newly live", text)
        self.assertIn("1 run(s) complete", text)
        self.make_target(self.run_, "late-01", state=TargetState.SCRAPING)
        with prom(configured=False):
            self.assertIn("Prometheus is not connected", jobs.liveness())

    def test_the_job_never_raises(self):
        with mock.patch.object(services, "refresh_liveness", side_effect=RuntimeError):
            with prom(up_rows("db-01:9100")):
                self.assertEqual(jobs.liveness(), "error")

    def test_end_to_end_activate_then_the_job_completes_the_run(self):
        run = self.make_run()
        t = self.make_target(run, "app-02", address="10.0.4.2", state=TargetState.REACHABLE)
        self.login(self.admin_a)
        self.c.post(step_url(run, "confirm"), {"action": "activate"})
        t.refresh_from_db()
        self.assertEqual(t.state, TargetState.SCRAPING)
        served = {e["labels"]["le_hostname"]: e["targets"] for e in sd.targets_payload()}
        self.assertEqual(served["app-02"], ["10.0.4.2:9100"])
        with prom(up_rows("10.0.4.2:9100")):
            activate.poll()
        t.refresh_from_db()
        run.refresh_from_db()
        self.assertEqual((t.state, run.status), (TargetState.LIVE, RunStatus.COMPLETE))
        self.assertIsNotNone(t.first_sample_at)
        self.assertIn("Onboarding complete", self.c.get(step_url(run, "done")).content.decode())


@with_key
class DonePageTests(OnboardingTestCase):
    """The Done page links to every place the host shows and says which already have data."""

    def setUp(self):
        super().setUp()
        self.run_ = self.make_run(step="done", status=RunStatus.COMPLETE, finished_at=timezone.now())
        self.t = self.make_target(self.run_, "db-01", address="10.0.3.14", exporter_kind="node_exporter",
                                  state=TargetState.LIVE, first_sample_at=timezone.now())
        steps.activate(self.t)
        self.t.refresh_from_db()
        self.root = get_user_model().objects.create_superuser("root", password=PASSWORD)

    def places(self, user, rows=None, **kw):
        """{key: item} from the coverage of the run for `user`, with Prometheus mocked."""
        with prom(rows if rows is not None else up_rows("10.0.3.14:9100", with_target=[self.t.pk]), **kw):
            blocks = activate.coverage(self.run_, user)
        return {i["key"]: i for i in blocks[0]["items"]}, blocks[0]

    def test_every_place_is_linked_for_a_node_target(self):
        items, block = self.places(self.root)
        self.assertEqual(list(items), ["fleet", "infrastructure", "metrics", "explorer", "logs", "cmdb", "alerts",
                                       "exporter"])
        self.assertEqual(items["metrics"]["href"], "/monitoring/infrastructure/10.0.3.14:9100/metrics/")
        self.assertIn("q=db-01", items["infrastructure"]["href"])
        self.assertIn("host=db-01", items["logs"]["href"])
        self.assertEqual(items["cmdb"]["href"], reverse("cmdb:item_detail", args=[self.t.ci_id]))
        self.assertIn("up%7Binstance", items["explorer"]["href"])
        self.assertTrue(block["prometheus"])

    def test_it_states_which_already_have_data(self):
        LogEntry.objects.create(source="syslog", message="boot", host="db-01", site="Mumbai")
        Alert.objects.create(fingerprint="f1", alertname="HighCPU", instance="10.0.3.14:9100", site="Mumbai",
                             status="firing")
        items, block = self.places(self.root)
        states = {k: v["state"] for k, v in items.items()}
        self.assertEqual(states, {"fleet": "yes", "infrastructure": "yes", "metrics": "yes", "explorer": "yes",
                                  "logs": "yes", "cmdb": "yes", "alerts": "yes", "exporter": "no"})
        self.assertEqual((block["with_data"], block["applicable"]), (7, 8))
        self.assertIn("1 firing", items["alerts"]["note"])
        self.assertIn("1 line", items["logs"]["note"])

    def test_places_without_data_say_so(self):
        items, block = self.places(self.root, rows=[])
        self.assertEqual((items["infrastructure"]["state"], items["metrics"]["state"], items["explorer"]["state"]),
                         ("no", "no", "no"))
        self.assertEqual((items["logs"]["state"], items["alerts"]["state"]), ("no", "no"))
        self.assertIn("ship logs", items["logs"]["note"])
        self.assertEqual(items["cmdb"]["state"], "yes")

    def test_when_prometheus_does_not_answer_the_metric_places_are_not_checked(self):
        items, block = self.places(self.root, rows=None, error=prometheus.PrometheusError("down"))
        self.assertEqual((items["infrastructure"]["state"], items["metrics"]["state"]), ("unknown", "unknown"))
        self.assertFalse(block["prometheus"])
        with prom(configured=False):
            self.assertEqual(activate.coverage(self.run_, self.root)[0]["items"][1]["state"], "unknown")

    def test_a_windows_target_has_no_node_view_and_says_why(self):
        t = self.make_target(self.run_, "win-01", target_type=TargetType.WINDOWS, port=9182,
                             exporter_kind="windows_exporter", state=TargetState.LIVE)
        steps.activate(t)
        with prom(up_rows("win-01:9182", job="windows")):
            blocks = activate.coverage(self.run_, self.root)
        items = {i["key"]: i for i in next(b for b in blocks if b["target"].pk == t.pk)["items"]}
        self.assertEqual(items["infrastructure"]["state"], "na")
        self.assertNotIn("metrics", items)
        self.assertEqual(items["explorer"]["state"], "yes")

    def test_a_device_links_its_device_page_as_the_full_metrics_page(self):
        t = self.make_target(self.run_, "sw-1", address="10.0.9.1", target_type=TargetType.DEVICE, port=9116,
                             exporter_kind="snmp_exporter", state=TargetState.LIVE)
        with mock.patch("onboarding.devices.exporter_url", return_value="http://snmp:9116"):
            steps.activate(t)
        with prom([]):
            blocks = activate.coverage(self.run_, self.root)
        items = {i["key"]: i for i in next(b for b in blocks if b["target"].pk == t.pk)["items"]}
        t.refresh_from_db()
        self.assertEqual(items["metrics"]["href"], reverse("network:detail", args=[t.device_id]))
        self.assertEqual(items["device"]["state"], "yes")
        self.assertEqual(items["metrics"]["state"], "no")           # no SNMP poll has landed yet
        Device.objects.filter(pk=t.device_id).update(metrics_updated_at=timezone.now())
        with prom([]):
            blocks = activate.coverage(self.run_, self.root)
        items = {i["key"]: i for i in next(b for b in blocks if b["target"].pk == t.pk)["items"]}
        self.assertEqual(items["metrics"]["state"], "yes")

    def test_a_site_admin_does_not_get_the_explorer_link(self):
        items, _ = self.places(self.admin_a)
        self.assertNotIn("explorer", items)

    def test_logs_and_alerts_of_other_sites_are_not_counted(self):
        LogEntry.objects.create(source="syslog", message="x", host="db-01", site="Chennai")
        Alert.objects.create(fingerprint="f2", alertname="X", instance="10.0.3.14:9100", site="Chennai")
        items, _ = self.places(self.admin_a)
        self.assertEqual((items["logs"]["state"], items["alerts"]["state"]), ("no", "no"))

    def test_every_link_on_the_page_resolves(self):
        LogEntry.objects.create(source="syslog", message="boot", host="db-01", site="Mumbai")
        self.login(self.root)
        with prom(up_rows("10.0.3.14:9100", with_target=[self.t.pk])):
            page = self.c.get(step_url(self.run_, "done"))
        self.assertEqual(page.status_code, 200)
        html = page.content.decode()
        self.assertIn("db-01", html)
        self.assertIn("Has data", html)
        with prom([]):
            html_none = self.c.get(step_url(self.run_, "done")).content.decode()
        self.assertIn("No data yet", html_none)
        with prom(configured=False):
            blocks = activate.coverage(self.run_, self.root)
        self.assertGreaterEqual(len(blocks[0]["items"]), 8)
        for item in blocks[0]["items"]:
            self.assertIn(escape(item["href"]), html)
            with prom(configured=False):
                resp = self.c.get(item["href"])
            self.assertEqual(resp.status_code, 200, f'{item["key"]} -> {item["href"]}')

    def test_every_link_resolves_for_a_device_and_a_compute_server_too(self):
        dev = self.make_target(self.run_, "sw-1", address="10.0.9.1", target_type=TargetType.DEVICE, port=9116,
                               exporter_kind="snmp_exporter", state=TargetState.LIVE)
        node = self.make_target(self.run_, "k8s-w1", target_type=TargetType.K8S_NODE, port=8080,
                                exporter_kind="kube-state-metrics", state=TargetState.LIVE)
        with mock.patch("onboarding.devices.exporter_url", return_value="http://snmp:9116"):
            steps.activate(dev)
        steps.activate(node)
        self.login(self.root)
        with prom(configured=False):
            blocks = activate.coverage(self.run_, self.root)
            self.assertEqual(len(blocks), 3)
            for block in blocks:
                for item in block["items"]:
                    self.assertEqual(self.c.get(item["href"]).status_code, 200, f'{block["target"]} {item["key"]}')
            self.assertEqual(self.c.get(step_url(self.run_, "done")).status_code, 200)

    def test_the_done_page_for_a_run_nobody_has_activated_still_renders(self):
        run = self.make_run()
        self.make_target(run, "fresh-01")
        self.login(self.admin_a)
        with prom(configured=False):
            resp = self.c.get(step_url(run, "done"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "fresh-01")
        self.assertContains(resp, "Fleet overview")

    def test_a_user_of_another_site_cannot_open_the_done_page(self):
        self.login(self.admin_b)
        self.assertEqual(self.c.get(step_url(self.run_, "done")).status_code, 404)

    def test_the_confirm_page_lists_what_activation_wired_up(self):
        self.login(self.admin_a)
        with prom(configured=False):
            html = self.c.get(step_url(self.run_, "confirm")).content.decode()
        self.assertIn("What activation wired up", html)
        self.assertIn(reverse("cmdb:item_detail", args=[self.t.ci_id]), html)
        self.assertIn(reverse("observe:exporter_detail", args=[self.t.exporter_id]), html)
        self.assertIn("served", html)

    def test_no_secret_on_the_done_or_confirm_page(self):
        cred = secrets.store(CredentialKind.SSH_PASSWORD, "svc", SECRET, "Mumbai", self.admin_a, name="m-ssh")
        self.t.credential = cred
        self.t.save(update_fields=["credential"])
        self.login(self.admin_a)
        with prom(configured=False):
            for name in ("confirm", "done"):
                body = self.c.get(step_url(self.run_, name)).content.decode()
                self.assertNotIn(SECRET, body)
                self.assertNotIn(cred.secret, body)
