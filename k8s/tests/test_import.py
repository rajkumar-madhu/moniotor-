"""Parsing, the readers and the import engine. Synthetic fixtures only (see fixtures.py)."""
import tempfile
import zipfile
from io import StringIO
from pathlib import Path

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from cmdb.models import ConfigurationItem
from compute.models import Cluster, ClusterKind, ClusterMember
from core.models import Client, Site
from network.models import Device

from k8s import importer, parsing, readers
from k8s.models import (ClientProfile, Confidence, Exposure, Health, ImportRun, ImportStatus, KubeCluster,
                        MappingStatus, Namespace, Node, NodeRole)

from . import fixtures

T = "test-tenant"


class ParsingTests(TestCase):
    def test_clean_normalises_placeholders(self):
        for placeholder in ("NOT DISCOVERED", "NOT APPLICABLE", "MANUAL INPUT REQUIRED",
                            "MULTIPLE / SEE NODE INVENTORY", "  not discovered  ", "N/A", "-", ""):
            self.assertEqual(parsing.clean(placeholder), "", placeholder)
        self.assertEqual(parsing.clean(None), "")
        self.assertEqual(parsing.clean("  spaced  "), "spaced")
        self.assertEqual(parsing.clean(13), "13")
        self.assertEqual(parsing.clean(13.0), "13")
        self.assertEqual(parsing.clean(0), "0")
        self.assertEqual(parsing.clean(True), "True")
        self.assertEqual(parsing.clean(" padded "), "padded")

    def test_unmapped_marker_is_not_a_placeholder(self):
        self.assertEqual(parsing.clean("UNMAPPED / REVIEW REQUIRED"), "UNMAPPED / REVIEW REQUIRED")
        self.assertTrue(parsing.is_unmapped("UNMAPPED / REVIEW REQUIRED"))
        self.assertFalse(parsing.is_unmapped("Acme"))

    def test_parse_quantity(self):
        self.assertEqual(parsing.parse_quantity("10Gi"), 10 * 1024 ** 3)
        self.assertEqual(parsing.parse_quantity("74098976Ki"), 74098976 * 1024)
        self.assertEqual(parsing.parse_quantity("2.70 Ti"), int(round(2.70 * 1024 ** 4)))
        self.assertEqual(parsing.parse_quantity("140.00 Gi"), int(round(140 * 1024 ** 3)))
        self.assertEqual(parsing.parse_quantity("0 Gi"), 0)
        self.assertEqual(parsing.parse_quantity("1k"), 1000)
        self.assertEqual(parsing.parse_quantity("5M"), 5_000_000)
        self.assertEqual(parsing.parse_quantity("1024"), 1024)
        for bad in ("NOT DISCOVERED", "", "lots", "10Zi", None):
            self.assertIsNone(parsing.parse_quantity(bad), bad)

    def test_parse_cpu(self):
        self.assertEqual(parsing.parse_cpu("16"), 16000)
        self.assertEqual(parsing.parse_cpu("8"), 8000)
        self.assertEqual(parsing.parse_cpu("500m"), 500)
        self.assertEqual(parsing.parse_cpu("1.5"), 1500)
        self.assertIsNone(parsing.parse_cpu("NOT DISCOVERED"))
        self.assertIsNone(parsing.parse_cpu("-2"))

    def test_parse_pods_handles_shortened_capacity(self):
        self.assertEqual(parsing.parse_pods("110"), 110)
        self.assertEqual(parsing.parse_pods("1k"), 1000)
        self.assertIsNone(parsing.parse_pods("NOT APPLICABLE"))

    def test_parse_age(self):
        self.assertEqual(parsing.parse_age("134d"), 134)
        self.assertEqual(parsing.parse_age("3d4h"), 3)
        self.assertEqual(parsing.parse_age("12h"), 0)
        self.assertEqual(parsing.parse_age("2y"), 730)
        self.assertEqual(parsing.parse_age("90m"), 0)
        self.assertEqual(parsing.parse_age("7"), 7)
        self.assertIsNone(parsing.parse_age("NOT DISCOVERED"))

    def test_parse_labels(self):
        parsed = parsing.parse_labels("a=1; b=2; node-role.kubernetes.io/control-plane=")
        self.assertEqual(parsed, {"a": "1", "b": "2", "node-role.kubernetes.io/control-plane": ""})
        self.assertEqual(parsing.parse_labels("NOT APPLICABLE"), {})
        self.assertEqual(parsing.parse_labels(""), {})

    def test_parse_taints(self):
        self.assertEqual(parsing.parse_taints("node-role.kubernetes.io/control-plane=:NoSchedule"),
                         [{"key": "node-role.kubernetes.io/control-plane", "value": "", "effect": "NoSchedule"}])
        self.assertEqual(parsing.parse_taints("a=b:NoExecute; c=d:NoSchedule"),
                         [{"key": "a", "value": "b", "effect": "NoExecute"},
                          {"key": "c", "value": "d", "effect": "NoSchedule"}])
        self.assertEqual(parsing.parse_taints("NOT APPLICABLE"), [])

    def test_parse_ports(self):
        self.assertEqual(parsing.parse_ports("80, 443/TCP, http"),
                         [{"port": 80, "name": "", "protocol": ""},
                          {"port": 443, "name": "", "protocol": "TCP"},
                          {"port": None, "name": "http", "protocol": ""}])
        self.assertEqual(parsing.parse_ports("NOT DISCOVERED"), [])
        self.assertEqual(parsing.parse_ports("99999"), [])

    def test_parse_list_bool_ip_url(self):
        self.assertEqual(parsing.parse_list("a, b, NOT DISCOVERED, a"), ["a", "b"])
        self.assertIs(parsing.parse_bool("True"), True)
        self.assertIs(parsing.parse_bool("Yes"), True)
        self.assertIs(parsing.parse_bool("No"), False)
        self.assertIsNone(parsing.parse_bool("NOT DISCOVERED"))
        self.assertEqual(parsing.parse_ip("203.0.113.10"), "203.0.113.10")
        self.assertIsNone(parsing.parse_ip("None"))
        self.assertIsNone(parsing.parse_ip("NOT DISCOVERED"))
        self.assertEqual(parsing.parse_url("https://app.example.com"), "https://app.example.com")
        self.assertEqual(parsing.parse_url("app.example.com"), "")
        self.assertEqual(parsing.parse_url("NOT DISCOVERED"), "")

    def test_fmt_bytes(self):
        self.assertEqual(parsing.fmt_bytes(1024 ** 3), "1 GiB")
        self.assertEqual(parsing.fmt_bytes(0), "0 B")
        self.assertEqual(parsing.fmt_bytes(None), "")


class ReaderTests(TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))

    def test_csv_directory(self):
        fixtures.write_csv_dir(self.dir)
        source = readers.open_source(self.dir)
        self.assertEqual(set(source.sheets), set(fixtures.SHEETS))
        self.assertEqual(len(source.get("03_Node_Inventory").rows), 3)
        self.assertEqual(source.get("14_Namespace_Mapping").rows[0]["Namespace"], "acme-prod")
        self.assertTrue(source.sha256)

    def test_csv_directory_hash_is_stable_and_content_sensitive(self):
        fixtures.write_csv_dir(self.dir)
        first = readers.open_source(self.dir).sha256
        self.assertEqual(readers.open_source(self.dir).sha256, first)
        fixtures.write_csv_dir(self.dir, sheets=fixtures.nodes_without("worker-2.example.com"))
        self.assertNotEqual(readers.open_source(self.dir).sha256, first)

    def test_ignores_dashboard_and_per_client_sheets(self):
        fixtures.write_csv_dir(self.dir, extra=fixtures.IGNORED_SHEETS)
        source = readers.open_source(self.dir)
        self.assertNotIn("01_Executive_Dashboard", source.sheets)
        self.assertNotIn("CL_Acme", source.sheets)
        self.assertEqual(sorted(source.ignored), ["01_Executive_Dashboard", "CL_Acme"])

    def test_xlsx_round_trip(self):
        path = fixtures.write_xlsx(self.dir / "inventory.xlsx")
        source = readers.open_source(path)
        self.assertEqual(set(source.sheets), set(fixtures.SHEETS))
        node = source.get("03_Node_Inventory").rows[0]
        self.assertEqual(node["Hostname"], "cp-1.example.com")
        self.assertEqual(node["Memory Capacity"], "32866108Ki")
        # the workbook's int cells arrive as plain text, not "8.0"
        self.assertEqual(source.get("15_RAW_Discovery").rows[0]["Return Code"], "0")

    def test_zip_of_csv_sheets(self):
        fixtures.write_csv_dir(self.dir)
        archive = self.dir.parent / "sheets.zip"
        self.addCleanup(lambda: archive.unlink(missing_ok=True))
        with zipfile.ZipFile(archive, "w") as zf:
            for path in self.dir.glob("*.csv"):
                zf.write(path, path.name)
        source = readers.open_source(archive)
        self.assertEqual(set(source.sheets), set(fixtures.SHEETS))

    def test_malformed_sources_raise_source_error(self):
        bad = self.dir / "broken.xlsx"
        bad.write_bytes(b"this is definitely not a workbook")
        with self.assertRaises(readers.SourceError):
            readers.open_source(bad)
        with self.assertRaises(readers.SourceError):
            readers.open_source(self.dir / "nope.xlsx")
        (self.dir / "notes.txt").write_text("hello")
        with self.assertRaises(readers.SourceError):
            readers.open_source(self.dir / "notes.txt")
        empty = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(empty, ignore_errors=True))
        with self.assertRaises(readers.SourceError):
            readers.open_source(empty)


@override_settings(LINKEDEYE_TENANT=T)
class ImportTests(TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))
        fixtures.write_csv_dir(self.dir)
        Site.objects.create(tenant_key=T, name="Mumbai", code="mum")

    # ---------------------------------------------------------------- happy path
    def test_import_creates_the_inventory(self):
        result = importer.run_import(self.dir)
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.counts["03_Node_Inventory"]["rows"], 3)
        self.assertEqual(result.counts["03_Node_Inventory"]["created"], 3)
        self.assertEqual(result.counts["02_Client_Master"]["created"], 2)
        self.assertEqual(result.counts["14_Namespace_Mapping"]["created"], 4)

        cluster = KubeCluster.objects.get(tenant_key=T)
        self.assertEqual(cluster.name, "kubernetes")
        self.assertEqual(cluster.context, "admin@example-cluster")
        self.assertEqual(cluster.kubernetes_version, "v1.30.1")
        self.assertIsNotNone(cluster.discovered_at)
        self.assertEqual(cluster.site, "All Sites")

        cp = Node.objects.get(hostname="cp-1.example.com")
        self.assertEqual(cp.role, NodeRole.CONTROL_PLANE)
        self.assertEqual(cp.exposure, Exposure.PRIVATE)
        self.assertEqual(cp.cpu_capacity_millicores, 8000)
        self.assertEqual(cp.memory_capacity_bytes, 32866108 * 1024)
        self.assertEqual(cp.memory_capacity_raw, "32866108Ki")
        self.assertEqual(cp.pod_capacity, 110)
        self.assertEqual(cp.age_days, 134)
        self.assertIs(cp.ready, True)
        self.assertEqual(cp.labels["tier"], "platform")
        self.assertEqual(cp.taints[0]["effect"], "NoSchedule")
        self.assertIsNone(cp.external_ip)

        worker = Node.objects.get(hostname="worker-1.example.com")
        self.assertEqual(worker.role, NodeRole.WORKER)
        self.assertEqual(worker.exposure, Exposure.PUBLIC)
        self.assertEqual(worker.external_ip, "203.0.113.10")
        self.assertEqual(worker.pod_capacity, 1000)       # "1k"
        self.assertEqual(worker.age_days, 3)              # "3d4h"
        self.assertEqual(worker.taints, [])               # "NOT APPLICABLE"

        big = Node.objects.get(hostname="worker-2.example.com")
        self.assertEqual(big.memory_capacity_bytes, int(round(2.70 * 1024 ** 4)))
        self.assertIs(big.ready, False)
        self.assertIs(big.schedulable, False)

    def test_placeholders_are_never_stored(self):
        importer.run_import(self.dir)
        globex = ClientProfile.objects.get(workbook_name="Globex")
        self.assertEqual(globex.primary_url, "")
        self.assertEqual(globex.monitoring_url, "")
        self.assertEqual(globex.cicd_url, "")
        self.assertEqual(globex.environments, [])
        self.assertEqual(globex.total_storage_bytes, 0)
        self.assertEqual(globex.status, Health.WARN)
        acme_ns = Namespace.objects.get(name="globex-prod")
        self.assertEqual(acme_ns.environment, "")
        for value in Node.objects.values_list("taints_raw", flat=True):
            self.assertNotIn("NOT APPLICABLE", value)
        for field in ("remarks", "status_raw"):
            self.assertNotIn("MANUAL INPUT REQUIRED", getattr(globex, field))

    def test_client_master_and_summary_merge(self):
        importer.run_import(self.dir)
        acme = ClientProfile.objects.get(workbook_name="Acme")
        self.assertEqual(acme.workbook_id, "CL-001")
        self.assertEqual(acme.project_names, ["acme-prod", "acme-uat"])
        self.assertEqual(acme.environments, ["PROD", "UAT"])
        self.assertEqual(acme.app_count, 12)
        self.assertEqual(acme.total_storage_bytes, int(round(140 * 1024 ** 3)))
        self.assertEqual(acme.status, Health.OK)
        # the roll-up sheet filled the summary columns of the same row
        self.assertEqual(acme.summary_namespaces, 2)
        self.assertEqual(acme.open_issues, 2)
        self.assertEqual(acme.health, Health.WARN)
        self.assertEqual(acme.monitoring_tools, ["Grafana", "Prometheus"])
        self.assertFalse(acme.summary_only)
        # a roll-up group the master sheet does not list becomes its own, flagged row
        other = ClientProfile.objects.get(workbook_name="Others/Unmapped")
        self.assertTrue(other.summary_only)
        self.assertEqual(other.health, Health.CRIT)
        self.assertIsNone(other.app_count)

    def test_namespace_mapping_keeps_confidence_and_review(self):
        importer.run_import(self.dir)
        self.assertEqual(Namespace.objects.count(), 4)
        mapped = Namespace.objects.get(name="acme-prod")
        self.assertEqual(mapped.confidence, Confidence.HIGH)
        self.assertEqual(mapped.mapping_status, MappingStatus.MAPPED)
        self.assertEqual(mapped.client_label, "Acme")
        self.assertEqual(mapped.environment, "PROD")
        medium = Namespace.objects.get(name="globex-prod")
        self.assertEqual(medium.confidence, Confidence.MEDIUM)
        review = Namespace.objects.get(name="sandbox")
        self.assertEqual(review.mapping_status, MappingStatus.REVIEW)
        self.assertEqual(review.confidence, Confidence.LOW)
        self.assertEqual(review.client_label, "")      # the unmapped marker is not a client name
        self.assertIsNone(review.client_id)
        self.assertTrue(review.needs_review)
        self.assertEqual(review.client_display, "")

    def test_raw_discovery_is_kept_as_run_metadata_not_rows(self):
        result = importer.run_import(self.dir)
        run = ImportRun.objects.get(pk=result.run_id)
        discovery = run.metadata["discovery"]
        self.assertEqual(discovery["resource_counts"], {"nodes": 3, "namespaces": 4})
        self.assertEqual(len(discovery["commands"]), 3)
        self.assertEqual(run.metadata["cluster_context"], "admin@example-cluster")
        self.assertIn("Secret values were not fetched", discovery["safety"][0]["note"])
        self.assertTrue(any("did not return 0" in w for w in run.warnings))

    def test_run_records_source_and_counts(self):
        result = importer.run_import(self.dir)
        run = ImportRun.objects.get(pk=result.run_id)
        self.assertEqual(run.status, ImportStatus.OK)
        self.assertEqual(run.source_name, self.dir.name)
        self.assertEqual(len(run.source_sha256), 64)
        self.assertIsNotNone(run.finished_at)
        self.assertEqual(run.total_rows, result.total_rows)
        self.assertEqual(Node.objects.exclude(import_run=run).count(), 0)

    # ---------------------------------------------------------------- idempotency & versioning
    def test_reimport_changes_nothing(self):
        importer.run_import(self.dir)
        before = {m.__name__: m.objects.count() for m in (KubeCluster, Node, Namespace, ClientProfile)}
        result = importer.run_import(self.dir)
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.total_created, 0)
        self.assertEqual(result.total_updated, 0)
        self.assertEqual(result.total_missing, 0)
        after = {m.__name__: m.objects.count() for m in (KubeCluster, Node, Namespace, ClientProfile)}
        self.assertEqual(before, after)
        self.assertEqual(ImportRun.objects.count(), 2)

    def test_missing_rows_are_marked_then_cleared(self):
        importer.run_import(self.dir)
        fixtures.write_csv_dir(self.dir, sheets=fixtures.nodes_without("worker-2.example.com"))
        result = importer.run_import(self.dir)
        self.assertEqual(result.counts["03_Node_Inventory"]["missing"], 1)
        gone = Node.objects.get(hostname="worker-2.example.com")
        self.assertIsNotNone(gone.missing_since)      # kept, not deleted
        self.assertTrue(gone.is_missing)
        self.assertEqual(Node.objects.count(), 3)

        fixtures.write_csv_dir(self.dir)              # the node comes back
        result = importer.run_import(self.dir)
        gone.refresh_from_db()
        self.assertIsNone(gone.missing_since)
        self.assertEqual(result.counts["03_Node_Inventory"]["missing"], 0)
        self.assertEqual(Node.objects.count(), 3)

    def test_changed_cell_updates_in_place(self):
        importer.run_import(self.dir)
        node = Node.objects.get(hostname="worker-1.example.com")
        rows = [list(r) for r in fixtures.NODE_INVENTORY]
        rows[2][7] = "Ubuntu 26.04 LTS"       # OS column
        fixtures.write_csv_dir(self.dir, sheets={**fixtures.SHEETS, "03_Node_Inventory": rows})
        result = importer.run_import(self.dir)
        self.assertEqual(result.counts["03_Node_Inventory"]["updated"], 1)
        self.assertEqual(result.counts["03_Node_Inventory"]["created"], 0)
        node.refresh_from_db()
        self.assertEqual(node.os, "Ubuntu 26.04 LTS")
        self.assertEqual(Node.objects.count(), 3)

    def test_partial_source_does_not_retire_other_sheets(self):
        importer.run_import(self.dir)
        only_nodes = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(only_nodes, ignore_errors=True))
        fixtures.write_csv_dir(only_nodes, sheets={"03_Node_Inventory": fixtures.NODE_INVENTORY})
        result = importer.run_import(only_nodes)
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(Namespace.objects.filter(missing_since__isnull=False).count(), 0)
        self.assertEqual(ClientProfile.objects.filter(missing_since__isnull=False).count(), 0)
        self.assertIn("14_Namespace_Mapping", " ".join(result.warnings))

    # ---------------------------------------------------------------- dry run
    def test_dry_run_writes_nothing(self):
        result = importer.run_import(self.dir, dry_run=True)
        self.assertTrue(result.ok, result.errors)
        self.assertTrue(result.dry_run)
        self.assertIsNone(result.run_id)
        self.assertEqual(result.counts["03_Node_Inventory"]["created"], 3)
        for model in (ImportRun, KubeCluster, Node, Namespace, ClientProfile):
            self.assertEqual(model.objects.count(), 0, model.__name__)

    def test_dry_run_after_a_real_import_reports_the_same_diff(self):
        importer.run_import(self.dir)
        fixtures.write_csv_dir(self.dir, sheets=fixtures.nodes_without("worker-2.example.com"))
        preview = importer.run_import(self.dir, dry_run=True)
        self.assertEqual(preview.counts["03_Node_Inventory"]["missing"], 1)
        self.assertIsNone(Node.objects.get(hostname="worker-2.example.com").missing_since)
        self.assertEqual(ImportRun.objects.count(), 1)
        applied = importer.run_import(self.dir)
        self.assertEqual(applied.counts["03_Node_Inventory"]["missing"], 1)

    # ---------------------------------------------------------------- resolution
    def test_client_resolution_by_name_and_code(self):
        acme = Client.objects.create(tenant_key=T, name="Acme", code="acme")
        globex = Client.objects.create(tenant_key=T, name="Globex Corporation", code="globex")
        importer.run_import(self.dir)
        self.assertEqual(ClientProfile.objects.get(workbook_name="Acme").client_id, acme.pk)
        self.assertEqual(ClientProfile.objects.get(workbook_name="Globex").client_id, globex.pk)
        self.assertEqual(Namespace.objects.get(name="acme-prod").client_id, acme.pk)
        self.assertIsNone(Namespace.objects.get(name="sandbox").client_id)

    def test_unknown_client_is_left_unlinked_and_never_invented(self):
        result = importer.run_import(self.dir)
        self.assertEqual(Client.objects.count(), 0)
        self.assertEqual(ClientProfile.objects.filter(client__isnull=True).count(), 3)
        self.assertTrue(any("no matching LinkedEye client" in w for w in result.warnings))

    def test_resolve_namespace_creates_a_stub_for_an_unlisted_namespace(self):
        importer.run_import(self.dir)
        run = ImportRun.objects.create(tenant_key=T, source_name="t")
        ctx = importer.ImportContext(run, None)
        found = ctx.resolve_namespace("acme-prod")
        self.assertEqual(found.mapping_status, MappingStatus.MAPPED)
        made = ctx.resolve_namespace("brand-new", client_label="Acme", environment="PROD", sheet="04_X")
        self.assertEqual(made.name, "brand-new")
        self.assertEqual(made.client_label, "Acme")
        self.assertEqual(made.confidence, Confidence.UNKNOWN)
        self.assertEqual(made.cluster.name, "kubernetes")
        self.assertIs(ctx.resolve_namespace("brand-new"), made)
        self.assertIsNone(ctx.resolve_namespace("NOT DISCOVERED"))

    def test_site_option_files_records_under_that_site(self):
        importer.run_import(self.dir, site="Mumbai")
        self.assertEqual(KubeCluster.objects.get().site, "Mumbai")
        self.assertEqual(set(Namespace.objects.values_list("site", flat=True)), {"Mumbai"})
        self.assertEqual(set(ClientProfile.objects.values_list("site", flat=True)), {"Mumbai"})
        self.assertEqual(Node.objects.first().site, "Mumbai")

    # ---------------------------------------------------------------- cross-links
    def test_nodes_link_to_an_existing_configuration_item(self):
        by_name = ConfigurationItem.objects.create(tenant_key=T, name="cp-1.example.com", site="Mumbai")
        by_instance = ConfigurationItem.objects.create(tenant_key=T, name="apps-worker",
                                                        prometheus_instance="worker-1.example.com:9100",
                                                        site="Mumbai")
        by_ip = ConfigurationItem.objects.create(tenant_key=T, name="spare", ip_address="10.0.0.13",
                                                  site="Mumbai")
        device = Device.objects.create(tenant_key=T, name="cp-1.example.com", device_type="physical_server",
                                       mgmt_ip="10.0.0.11", site="Mumbai")
        importer.run_import(self.dir)
        self.assertEqual(Node.objects.get(hostname="cp-1.example.com").ci_id, by_name.pk)
        self.assertEqual(Node.objects.get(hostname="worker-1.example.com").ci_id, by_instance.pk)
        self.assertEqual(Node.objects.get(hostname="worker-2.example.com").ci_id, by_ip.pk)
        self.assertEqual(Node.objects.get(hostname="cp-1.example.com").device_id, device.pk)

    def test_a_near_miss_is_left_unlinked(self):
        ConfigurationItem.objects.create(tenant_key=T, name="cp-1", site="Mumbai")
        ConfigurationItem.objects.create(tenant_key=T, name="other",
                                          prometheus_instance="worker-10.example.com:9100", site="Mumbai")
        importer.run_import(self.dir)
        self.assertEqual(Node.objects.filter(ci__isnull=False).count(), 0)

    def test_create_compute_is_opt_in_and_never_duplicates(self):
        importer.run_import(self.dir)
        self.assertEqual(Cluster.objects.count(), 0)
        self.assertEqual(ClusterMember.objects.count(), 0)

        importer.run_import(self.dir, create_compute=True)
        compute_cluster = Cluster.objects.get(tenant_key=T)
        self.assertEqual(compute_cluster.kind, ClusterKind.KUBERNETES)
        self.assertEqual(ClusterMember.objects.count(), 3)
        self.assertEqual(KubeCluster.objects.get().compute_cluster_id, compute_cluster.pk)
        cp = Node.objects.get(hostname="cp-1.example.com")
        self.assertEqual(cp.member.role, "control-plane")
        self.assertEqual(Node.objects.get(hostname="worker-1.example.com").member.role, "worker")
        self.assertEqual(Device.objects.filter(tenant_key=T).count(), 3)

        importer.run_import(self.dir, create_compute=True)
        self.assertEqual(Cluster.objects.count(), 1)
        self.assertEqual(ClusterMember.objects.count(), 3)
        self.assertEqual(Device.objects.filter(tenant_key=T).count(), 3)

    def test_create_compute_leaves_a_same_named_non_kubernetes_cluster_alone(self):
        Cluster.objects.create(tenant_key=T, name="kubernetes", kind=ClusterKind.VMWARE, site="All Sites")
        result = importer.run_import(self.dir, create_compute=True)
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(Cluster.objects.get().kind, ClusterKind.VMWARE)
        self.assertEqual(ClusterMember.objects.count(), 0)
        self.assertIsNone(KubeCluster.objects.get().compute_cluster_id)
        self.assertTrue(any("not a Kubernetes cluster" in w for w in result.warnings))

    # ---------------------------------------------------------------- failure handling
    def test_a_malformed_file_is_reported_not_raised(self):
        bad = self.dir.parent / "broken.xlsx"
        bad.write_bytes(b"not a workbook at all")
        self.addCleanup(lambda: bad.unlink(missing_ok=True))
        result = importer.run_import(bad)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, ImportStatus.FAILED)
        self.assertTrue(result.errors)
        self.assertEqual(Node.objects.count(), 0)
        run = ImportRun.objects.get()
        self.assertEqual(run.status, ImportStatus.FAILED)
        self.assertTrue(run.errors)

    def test_a_sheet_with_a_bad_header_is_reported_not_raised(self):
        rows = [["Nope", "Also nope"], ["x", "y"]]
        fixtures.write_csv_dir(self.dir, sheets={**fixtures.SHEETS, "03_Node_Inventory": rows})
        result = importer.run_import(self.dir)
        self.assertTrue(result.ok, result.errors)   # the row is skipped, not fatal
        self.assertEqual(Node.objects.count(), 0)
        self.assertEqual(result.counts["03_Node_Inventory"]["skipped"], 1)
        self.assertEqual(Namespace.objects.count(), 4)   # the other sheets still imported

    def test_one_failing_sheet_does_not_lose_the_others(self):
        def explode(ctx, sheet):
            raise RuntimeError("synthetic failure")

        original = importer._SHEETS["14_Namespace_Mapping"]
        importer.register_sheet("14_Namespace_Mapping", original[0], explode)
        self.addCleanup(lambda: importer.register_sheet("14_Namespace_Mapping", original[0], original[1]))
        result = importer.run_import(self.dir)
        self.assertFalse(result.ok)
        self.assertTrue(any("synthetic failure" in e for e in result.errors))
        self.assertEqual(Node.objects.count(), 3)        # other sheets committed
        self.assertEqual(Namespace.objects.count(), 0)   # the failing sheet rolled back

    def test_unknown_sheets_are_listed_not_imported(self):
        fixtures.write_csv_dir(self.dir, extra={"99_Future_Sheet": [["A"], ["1"]]})
        result = importer.run_import(self.dir)
        self.assertEqual(result.unknown_sheets, ["99_Future_Sheet"])
        self.assertNotIn("99_Future_Sheet", result.counts)

    # ---------------------------------------------------------------- registry
    def test_registry_is_ordered_and_replaces_rather_than_duplicates(self):
        names = importer.sheet_names()
        self.assertEqual(names[:2], ["15_RAW_Discovery", "03_Node_Inventory"])
        self.assertEqual(len(names), len(set(names)))
        before = len(names)
        original = importer._SHEETS["15_RAW_Discovery"]
        importer.register_sheet("15_RAW_Discovery", original[0], original[1])
        self.addCleanup(lambda: importer.register_sheet("15_RAW_Discovery", original[0], original[1]))
        self.assertEqual(len(importer.sheet_names()), before)
        with self.assertRaises(TypeError):
            importer.register_sheet("bogus", 1, "not callable")

    # ---------------------------------------------------------------- xlsx end to end
    def test_xlsx_import_end_to_end(self):
        path = fixtures.write_xlsx(self.dir / "inventory.xlsx")
        result = importer.run_import(path)
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(Node.objects.count(), 3)
        self.assertEqual(Namespace.objects.count(), 4)
        self.assertEqual(ClientProfile.objects.count(), 3)
        self.assertEqual(ImportRun.objects.get(pk=result.run_id).source_name, "inventory.xlsx")
        self.assertEqual(sorted(result.ignored_sheets), ["01_Executive_Dashboard", "CL_Acme"])
        self.assertEqual(Node.objects.get(hostname="worker-1.example.com").pod_capacity, 1000)
        # and it is still idempotent through the xlsx path
        again = importer.run_import(path)
        self.assertEqual(again.total_created, 0)
        self.assertEqual(again.total_updated, 0)


@override_settings(LINKEDEYE_TENANT=T)
class ManagementCommandTests(TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))
        fixtures.write_csv_dir(self.dir)

    def call(self, *args, **options):
        out = StringIO()
        call_command("import_k8s_inventory", *args, stdout=out, stderr=out, **options)
        return out.getvalue()

    def test_command_imports(self):
        output = self.call(str(self.dir))
        self.assertIn("03_Node_Inventory", output)
        self.assertIn("Imported", output)
        self.assertEqual(Node.objects.count(), 3)

    def test_command_dry_run_writes_nothing(self):
        output = self.call(str(self.dir), "--dry-run")
        self.assertIn("Dry run", output)
        self.assertEqual(Node.objects.count(), 0)
        self.assertEqual(ImportRun.objects.count(), 0)

    def test_command_site_and_create_compute(self):
        Site.objects.create(tenant_key=T, name="Mumbai", code="mum")
        self.call(str(self.dir), "--site", "Mumbai", "--create-compute")
        self.assertEqual(KubeCluster.objects.get().site, "Mumbai")
        self.assertEqual(Cluster.objects.get(tenant_key=T).site, "Mumbai")

    def test_command_fails_loudly_on_a_bad_file(self):
        bad = self.dir / "broken.xlsx"
        bad.write_bytes(b"nope")
        with self.assertRaises(CommandError):
            self.call(str(bad))
        self.assertEqual(ImportRun.objects.get().status, ImportStatus.FAILED)
