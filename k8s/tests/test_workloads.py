"""Workloads, services and volumes: parsing, import, idempotency, cross-link, pages, scoping, tenant isolation.

Every fixture below is make-believe ("Acme"/"Globex", example.com host names, RFC 5737 addresses, a digest
made of repeated characters). The real workbook is never copied here.
"""
import csv
import io
import tempfile
from pathlib import Path

from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import Membership, Site

from k8s import importer
from k8s.models import Health, ImportRun, KubeCluster, Namespace, Node
from k8s.models_workload import KubeService, PodStatus, Risk, Volume, Workload
from k8s.sheets.workloads import parse_image
from k8s.views_workload import workload_signals

from . import fixtures

T = "test-tenant"
OTHER = "other-tenant"
MUM = "Mumbai"
DR = "DR Site"
DIGEST = "sha256:" + "ab" * 32

APP_HEADER = ["Client", "Project", "Environment", "Namespace", "Application", "Component", "Workload Type",
              "Workload Name", "Desired Replicas", "Ready Replicas", "Pod", "Pod Status", "Pod IP", "Node",
              "Container", "Image", "Image Tag", "Port", "Storage Attached", "URL", "Health", "Owner", "Remarks"]
ND = "NOT DISCOVERED"
NA = "NOT APPLICABLE"
APPLICATIONS = [
    APP_HEADER,
    ["Acme", "acme-prod", "PROD", "acme-prod", "shop", ND, "Deployment", "shop-web", "2", "2", "shop-web-aaa",
     "Running", "10.244.0.5", "worker-1.example.com", "web", "registry.example.com/acme/web:1.4.2", "1.4.2",
     "8080,8443", "shop-data", "https://shop.example.com", "Healthy", "MANUAL INPUT REQUIRED", ""],
    ["Acme", "acme-prod", "PROD", "acme-prod", "shop", ND, "Deployment", "shop-web", "2", "2", "shop-web-aaa",
     "Running", "10.244.0.5", "worker-1.example.com", "sidecar", "registry.example.com/acme/proxy:latest", "latest",
     "9000", NA, "https://shop.example.com", "Healthy", "MANUAL INPUT REQUIRED", ""],
    ["Acme", "acme-prod", "PROD", "acme-prod", "shop", ND, "Deployment", "shop-web", "2", "1", "shop-web-bbb",
     "Running", "10.244.0.6", "worker-1.example.com", "web", "registry.example.com/acme/web:1.4.2", "1.4.2",
     "8080,8443", "shop-data", "https://shop.example.com", "Warning", "MANUAL INPUT REQUIRED", ""],
    ["Acme", "acme-uat", "UAT", "acme-uat", "shop", ND, "StatefulSet", "shop-db", "3", "1", "shop-db-0",
     "Pending", ND, "worker-2.example.com", "db", "postgres:16", "16", "5432", "shop-db-data, shop-db-logs", ND,
     "Warning", "MANUAL INPUT REQUIRED", ""],
    ["Globex", "globex-prod", ND, "globex-prod", "agent", ND, "DaemonSet", "node-agent", ND, "3", "node-agent-x",
     "Running", "203.0.113.9", "ghost-node.example.com", "agent", "example.com:5000/ops/agent@" + DIGEST,
     "ab" * 32, ND, NA, "https://a.example.com, https://b.example.com", "Healthy", "MANUAL INPUT REQUIRED", ""],
    ["Globex", "globex-prod", ND, "globex-prod", "batch", ND, "Deployment", "batch-runner", "0", "0", ND, "NO POD",
     ND, ND, "runner", "ops/runner", "latest", ND, NA, ND, "Healthy", "MANUAL INPUT REQUIRED", ""],
    ["UNMAPPED / REVIEW REQUIRED", "scratch", ND, "scratch-ns", "scratch", ND, "Deployment", "scratch-app", "1", "1",
     "scratch-app-1", "Running", ND, "worker-2.example.com", "app", "registry.example.com/misc/app:2.0", "2.0", ND,
     NA, ND, "Healthy", "MANUAL INPUT REQUIRED", ""],
]

SVC_HEADER = ["Client", "Environment", "Namespace", "Application", "Service", "Service Type", "Cluster IP",
              "External IP", "LoadBalancer IP", "NodePort", "Service Port", "Target Port", "Protocol", "Selector",
              "Status"]
SERVICES = [
    SVC_HEADER,
    ["Acme", "PROD", "acme-prod", "shop", "shop-web", "ClusterIP", "10.96.0.10", NA, NA, NA, "80", "8080", "TCP",
     "app=shop-web", "Active"],
    ["Acme", "PROD", "acme-prod", "shop", "shop-web", "ClusterIP", "10.96.0.10", NA, NA, NA, "443", "https", "TCP",
     "app=shop-web", "Active"],
    ["Acme", "PROD", "acme-prod", "shop", "shop-edge", "NodePort", "10.96.0.11", NA, NA, "30080", "80", "http", "TCP",
     "app=shop-edge; tier=edge; ", "Active"],
    ["Globex", ND, "globex-prod", "agent", "node-agent", "LoadBalancer", "None", NA, "198.51.100.7", NA, "9100",
     "9100", "UDP", "app=node-agent", "Active"],
    ["Globex", ND, "globex-prod", "agent", "external-db", "ExternalName", "None", "db.example.com", NA, NA, "5432",
     "5432", "TCP", NA, "Active"],
]

VOL_HEADER = ["Client", "Environment", "Namespace", "Application", "PVC", "PVC Status", "PV", "PV Status",
              "Capacity", "Requested", "Access Mode", "Storage Class", "Reclaim Policy", "Volume Mode",
              "Provisioner", "Workload", "Pod", "Mount Path", "Age", "Risk", "Remarks"]
VOLUMES = [
    VOL_HEADER,
    ["Acme", "PROD", "acme-prod", "shop", "shop-data", "Bound", "pv-shop-data", "Bound", "10Gi", "10Gi",
     "ReadWriteOnce", "fast", "Retain", ND, "example.com/provisioner", "Deployment/shop-web", "shop-web-aaa",
     "/data", "22d", "Medium", "Retain volume"],
    ["Acme", ND, "acme-uat", "shop", "shop-db-data", "Pending", ND, ND, "20Gi", "20Gi", "ReadWriteOnce", "fast",
     ND, "Filesystem", ND, "StatefulSet/shop-db", ND, ND, "3d", "High", "Retain volume"],
    ["Acme", ND, "acme-uat", "shop", "shop-db-logs", "Bound", "pv-shop-db-logs", "Bound", "100G", "100G",
     "ReadWriteOnce", "slow", "Retain", "Filesystem", ND, "StatefulSet/shop-db", "shop-db-0", "/logs", "3d",
     "Medium", "Retain volume"],
    ["UNMAPPED / REVIEW REQUIRED", ND, NA, ND, NA, NA, "pv-orphan", "Available", "30Gi", NA, "ReadOnlyMany", "slow",
     "Retain", "Filesystem", ND, NA, NA, ND, "35d", "Medium", "Unclaimed PV"],
]

WORKLOAD_SHEETS = {
    "04_Application_Inventory": APPLICATIONS,
    "05_Service_Inventory": SERVICES,
    "07_PV_PVC_Storage": VOLUMES,
}


def sheets(**override):
    return {**fixtures.SHEETS, **WORKLOAD_SHEETS, **override}


def seed(site=MUM, tenant=T, **override):
    directory = Path(tempfile.mkdtemp())
    fixtures.write_csv_dir(directory, sheets(**override))
    with override_settings(LINKEDEYE_TENANT=tenant):
        return importer.run_import(directory, site=site)


@override_settings(LINKEDEYE_TENANT=T)
class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.mum = Site.objects.create(tenant_key=T, name=MUM, code="mum")
        cls.dr = Site.objects.create(tenant_key=T, name=DR, code="dr")
        cls.viewer = User.objects.create_user("viewer", password="pw-viewer-12345")
        Membership.objects.create(user=cls.viewer, site=None, role="viewer")
        cls.dr_viewer = User.objects.create_user("dr-viewer", password="pw-drviewer-12345")
        Membership.objects.create(user=cls.dr_viewer, site=cls.dr, role="viewer")
        cls.mum_viewer = User.objects.create_user("mum-viewer", password="pw-mumviewer-12345")
        Membership.objects.create(user=cls.mum_viewer, site=cls.mum, role="viewer")

    def login(self, user):
        self.client.force_login(user)
        return user

    def rows(self, response):
        return list(csv.reader(io.StringIO(response.content.decode())))


# ----------------------------------------------------------------------------- parsing
class ImageParsingTests(TestCase):
    def test_registry_tag_and_digest(self):
        self.assertEqual(parse_image("registry.example.com/acme/web:1.4.2"),
                         ("registry.example.com", "1.4.2", "", False))
        self.assertEqual(parse_image("example.com:5000/ops/agent@" + DIGEST),
                         ("example.com:5000", "", DIGEST, False))
        self.assertEqual(parse_image("registry.example.com/x/y:v1@" + DIGEST),
                         ("registry.example.com", "v1", DIGEST, False))

    def test_docker_hub_images(self):
        self.assertEqual(parse_image("postgres:16"), ("docker.io", "16", "", False))
        self.assertEqual(parse_image("ops/runner:3"), ("docker.io", "3", "", False))
        self.assertEqual(parse_image("localhost/app:1")[0], "localhost")

    def test_latest_tag_is_flagged_explicitly_and_implicitly(self):
        self.assertTrue(parse_image("registry.example.com/a/b:latest")[3])
        self.assertTrue(parse_image("registry.example.com/a/b:LATEST")[3])
        self.assertTrue(parse_image("redis")[3])                       # no tag resolves to latest
        self.assertFalse(parse_image("redis@" + DIGEST)[3])            # pinned by digest
        self.assertFalse(parse_image("registry.example.com:5000/a/b:1.0")[3])

    def test_a_port_in_the_registry_is_not_a_tag(self):
        registry, tag, _digest, latest = parse_image("example.com:5000/app")
        self.assertEqual((registry, tag, latest), ("example.com:5000", "", True))

    def test_empty_and_placeholder_images(self):
        self.assertEqual(parse_image(""), ("", "", "", False))
        self.assertEqual(parse_image("NOT DISCOVERED"), ("", "", "", False))


# ----------------------------------------------------------------------------- import
class ImportTests(Base):
    def test_rows_land_with_the_right_counts(self):
        result = seed()
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.counts["04_Application_Inventory"]["rows"], 7)
        # "created" also counts the namespace row an unmapped namespace gets (scratch-ns): 7 workloads + 1
        self.assertEqual(result.counts["04_Application_Inventory"]["created"], 8)
        self.assertEqual(Workload.objects.count(), 7)
        self.assertEqual(KubeService.objects.count(), 5)
        self.assertEqual(Volume.objects.count(), 4)

    def test_one_row_per_container_and_pod_with_a_stable_key(self):
        seed()
        web = Workload.objects.filter(workload_name="shop-web")
        self.assertEqual(web.count(), 3)
        self.assertEqual(sorted(web.values_list("container", "pod_name")),
                         [("sidecar", "shop-web-aaa"), ("web", "shop-web-aaa"), ("web", "shop-web-bbb")])

    def test_values_are_parsed(self):
        seed()
        w = Workload.objects.get(workload_name="shop-web", container="web", pod_name="shop-web-aaa")
        self.assertEqual((w.desired_replicas, w.ready_replicas), (2, 2))
        self.assertEqual(w.workload_type, "Deployment")
        self.assertEqual(w.health, Health.OK)
        self.assertEqual(w.pod_status, PodStatus.RUNNING)
        self.assertEqual(w.pod_ip, "10.244.0.5")
        self.assertEqual(w.image_registry, "registry.example.com")
        self.assertEqual(w.image_tag, "1.4.2")
        self.assertFalse(w.image_latest)
        self.assertEqual([p["port"] for p in w.ports], [8080, 8443])
        self.assertEqual(w.storage_attached, ["shop-data"])
        self.assertEqual(w.url, "https://shop.example.com")
        self.assertEqual(w.client_label, "Acme")
        self.assertEqual(w.environment, "PROD")
        self.assertEqual(w.namespace.name, "acme-prod")
        self.assertEqual(w.replicas_label, "2/2")

    def test_short_replicas(self):
        seed()
        short = Workload.objects.get(workload_name="shop-db")
        self.assertTrue(short.is_short)
        self.assertEqual(short.health, Health.WARN)
        self.assertEqual(short.pod_status, PodStatus.PENDING)
        self.assertEqual(short.storage_attached, ["shop-db-data", "shop-db-logs"])
        self.assertFalse(Workload.objects.get(workload_name="shop-web", container="sidecar").is_short)

    def test_an_unrecorded_replica_count_is_null_not_zero(self):
        seed()
        agent = Workload.objects.get(workload_name="node-agent")
        self.assertIsNone(agent.desired_replicas)
        self.assertEqual(agent.ready_replicas, 3)
        self.assertFalse(agent.is_short)
        self.assertEqual(agent.replicas_label, "3/–")
        self.assertEqual(Workload.objects.get(workload_name="batch-runner").replicas_label, "0/0")

    def test_latest_flag_covers_explicit_implicit_and_digest_pinned(self):
        seed()
        latest = set(Workload.objects.filter(image_latest=True).values_list("workload_name", "container"))
        self.assertEqual(latest, {("shop-web", "sidecar"), ("batch-runner", "runner")})
        agent = Workload.objects.get(workload_name="node-agent")
        self.assertEqual((agent.image_registry, agent.image_tag, agent.image_digest),
                         ("example.com:5000", "", DIGEST))
        self.assertTrue(agent.is_pinned)
        self.assertEqual(Workload.objects.get(workload_name="batch-runner").image_registry, "docker.io")

    def test_several_urls_in_one_cell(self):
        seed()
        agent = Workload.objects.get(workload_name="node-agent")
        self.assertEqual(agent.urls, ["https://a.example.com", "https://b.example.com"])
        self.assertEqual(agent.url, "https://a.example.com")

    def test_no_pod_row(self):
        seed()
        row = Workload.objects.get(workload_name="batch-runner")
        self.assertEqual(row.pod_status, PodStatus.NO_POD)
        self.assertEqual(row.pod_name, "")
        self.assertIsNone(row.pod_ip)

    def test_placeholders_are_never_stored(self):
        seed()
        bad = ("NOT DISCOVERED", "NOT APPLICABLE", "MANUAL INPUT REQUIRED", "MULTIPLE")
        for model in (Workload, KubeService, Volume):
            for obj in model.objects.all():
                for field in model._meta.get_fields():
                    if not hasattr(field, "attname") or field.is_relation:
                        continue
                    value = getattr(obj, field.attname)
                    text = str(value) if value is not None else ""
                    for marker in bad:
                        self.assertNotIn(marker, text, f"{model.__name__}.{field.name}")

    def test_unmapped_client_is_no_client(self):
        seed()
        scratch = Workload.objects.get(workload_name="scratch-app")
        self.assertEqual(scratch.client_label, "")
        self.assertIsNone(scratch.client)
        self.assertEqual(Volume.objects.get(pv="pv-orphan").client_label, "")

    def test_services_per_port(self):
        seed()
        web = KubeService.objects.filter(name="shop-web").order_by("port")
        self.assertEqual([(s.port, s.target_port, s.protocol) for s in web],
                         [(80, "8080", "TCP"), (443, "https", "TCP")])
        self.assertEqual(web[0].ports_label, "80 → 8080/TCP")
        self.assertEqual(web[0].cluster_ip, "10.96.0.10")
        edge = KubeService.objects.get(name="shop-edge")
        self.assertEqual((edge.service_type, edge.node_port), ("NodePort", 30080))
        self.assertEqual(edge.selector, {"app": "shop-edge", "tier": "edge"})
        self.assertTrue(edge.is_exposed)
        lb = KubeService.objects.get(name="node-agent")
        self.assertEqual((lb.service_type, lb.loadbalancer_ip, lb.protocol), ("LoadBalancer", "198.51.100.7", "UDP"))
        self.assertTrue(lb.headless)
        self.assertIsNone(lb.cluster_ip)
        ext = KubeService.objects.get(name="external-db")
        self.assertEqual((ext.service_type, ext.external_ip, ext.selector), ("ExternalName", "db.example.com", {}))

    def test_volumes(self):
        seed()
        bound = Volume.objects.get(pvc="shop-data")
        self.assertEqual(bound.capacity_bytes, 10 * 1024 ** 3)
        self.assertEqual(bound.capacity_raw, "10Gi")
        self.assertEqual(bound.age_days, 22)
        self.assertEqual(bound.risk, Risk.MEDIUM)
        self.assertEqual(bound.namespace.name, "acme-prod")
        self.assertEqual(bound.mount_path, "/data")
        self.assertEqual(bound.workload_refs, ["Deployment/shop-web"])
        self.assertEqual(Volume.objects.get(pvc="shop-db-logs").capacity_bytes, 100 * 1000 ** 3)   # "100G" is decimal
        pending = Volume.objects.get(pvc="shop-db-data")
        self.assertTrue(pending.is_pending)
        self.assertEqual(pending.pv, "")
        self.assertEqual(pending.risk, Risk.HIGH)
        orphan = Volume.objects.get(pv="pv-orphan")
        self.assertTrue(orphan.is_unclaimed)
        self.assertIsNone(orphan.namespace)
        self.assertEqual(orphan.pvc, "")
        self.assertIsNone(orphan.requested_bytes)
        self.assertFalse(bound.is_unclaimed)

    def test_namespaces_the_mapping_sheet_missed_are_created_for_review(self):
        result = seed()
        ns = Namespace.objects.get(name="scratch-ns")
        self.assertEqual(ns.mapping_status, "review")
        self.assertTrue(any("scratch-ns" in w for w in result.warnings))

    def test_reimport_is_idempotent(self):
        seed()
        before = {m: m.objects.count() for m in (Workload, KubeService, Volume)}
        again = seed()
        self.assertEqual(again.total_created, 0)
        self.assertEqual(again.total_updated, 0)
        self.assertEqual(again.total_missing, 0)
        self.assertEqual({m: m.objects.count() for m in (Workload, KubeService, Volume)}, before)
        self.assertEqual(ImportRun.objects.count(), 2)

    def test_reimport_updates_in_place_and_marks_missing(self):
        seed()
        pk = Workload.objects.get(workload_name="shop-db").pk
        changed = [list(r) for r in APPLICATIONS]
        for row in changed:
            if row[7] == "shop-db":
                row[9] = "3"                                    # now fully ready
        changed = [r for r in changed if r[7] != "batch-runner"]  # and one workload is gone
        result = seed(**{"04_Application_Inventory": changed})
        counts = result.counts["04_Application_Inventory"]
        self.assertEqual((counts["created"], counts["updated"], counts["missing"]), (0, 1, 1))
        self.assertEqual(Workload.objects.get(pk=pk).ready_replicas, 3)
        self.assertFalse(Workload.objects.get(pk=pk).is_short)
        self.assertIsNotNone(Workload.objects.get(workload_name="batch-runner").missing_since)
        # and it comes back
        seed()
        self.assertIsNone(Workload.objects.get(workload_name="batch-runner").missing_since)

    def test_a_partial_import_does_not_claim_the_other_sheets_vanished(self):
        seed()
        result = seed(**{"05_Service_Inventory": SERVICES})
        self.assertEqual(result.total_missing, 0)
        only_nodes = {k: v for k, v in sheets().items() if k not in WORKLOAD_SHEETS}
        directory = Path(tempfile.mkdtemp())
        fixtures.write_csv_dir(directory, only_nodes)
        with override_settings(LINKEDEYE_TENANT=T):
            importer.run_import(directory, site=MUM)
        self.assertEqual(Workload.objects.filter(missing_since__isnull=False).count(), 0)

    def test_dry_run_writes_nothing(self):
        directory = Path(tempfile.mkdtemp())
        fixtures.write_csv_dir(directory, sheets())
        with override_settings(LINKEDEYE_TENANT=T):
            result = importer.run_import(directory, dry_run=True, site=MUM)
        self.assertEqual(result.counts["04_Application_Inventory"]["created"], 8)
        self.assertEqual(Workload.objects.count(), 0)
        self.assertEqual(Volume.objects.count(), 0)

    def test_rows_without_a_key_are_skipped_with_a_warning(self):
        bad = [APP_HEADER, ["Acme", "p", ND, "acme-prod", "a", ND, "Deployment", ND, "1", "1", ND, "Running", ND, ND,
                            "c", "x:1", "1", ND, NA, ND, "Healthy", ND, ""]]
        result = seed(**{"04_Application_Inventory": bad, "07_PV_PVC_Storage": [VOL_HEADER, ["Acme", ND, "acme-prod", "shop"] + [ND] * (len(VOL_HEADER) - 4)]})
        self.assertEqual(Workload.objects.count(), 0)
        self.assertEqual(result.counts["04_Application_Inventory"]["skipped"], 1)
        self.assertEqual(result.counts["07_PV_PVC_Storage"]["skipped"], 1)

    def test_oversized_numbers_are_dropped_not_stored(self):
        rows = [list(r) for r in APPLICATIONS[:2]]
        rows[1][8] = "99999999999999999999"
        seed(**{"04_Application_Inventory": rows})
        self.assertIsNone(Workload.objects.get().desired_replicas)

    def test_a_secret_value_has_nowhere_to_go(self):
        names = {f.name for m in (Workload, KubeService, Volume) for f in m._meta.get_fields()}
        for forbidden in ("secret", "password", "token", "credential", "env", "value"):
            self.assertFalse([n for n in names if forbidden in n.split("_")], forbidden)


class CrossLinkTests(Base):
    def test_workloads_link_to_the_node_they_ran_on(self):
        seed()
        w = Workload.objects.get(workload_name="shop-db")
        self.assertEqual(w.node, Node.objects.get(hostname="worker-2.example.com"))
        self.assertEqual(w.node_name, "worker-2.example.com")
        self.assertEqual(Node.objects.get(hostname="worker-1.example.com").workloads.count(), 3)

    def test_a_node_missing_from_the_inventory_stays_text_only(self):
        result = seed()
        agent = Workload.objects.get(workload_name="node-agent")
        self.assertIsNone(agent.node)
        self.assertEqual(agent.node_name, "ghost-node.example.com")
        self.assertTrue(any("not in the node inventory" in w for w in result.warnings))

    def test_without_the_node_sheet_nothing_is_linked_but_names_are_kept(self):
        seed(**{"03_Node_Inventory": [fixtures.NODE_INVENTORY[0]]})
        self.assertEqual(Workload.objects.filter(node__isnull=False).count(), 0)
        self.assertEqual(Workload.objects.filter(node_name="worker-1.example.com").count(), 3)

    def test_a_workload_with_no_node_has_none(self):
        seed()
        self.assertIsNone(Workload.objects.get(workload_name="batch-runner").node)
        self.assertEqual(Workload.objects.get(workload_name="batch-runner").node_name, "")

    def test_clients_link_when_a_matching_client_exists_and_not_otherwise(self):
        from core.models import Client
        seed()
        self.assertEqual(Workload.objects.filter(client__isnull=False).count(), 0)
        Client.objects.create(tenant_key=T, name="Acme", code="acme")
        Workload.objects.all().delete()
        seed()
        self.assertEqual(Workload.objects.filter(client__isnull=False).count(), 4)
        self.assertEqual(Workload.objects.filter(client_label="Globex", client__isnull=True).count(), 2)

    def test_the_nodes_page_client_filter_now_works(self):
        seed()
        self.login(self.viewer)
        response = self.client.get(reverse("k8s:nodes"), {"client": "Acme"})
        self.assertEqual(sorted(n.hostname for n in response.context["items"]),
                         ["worker-1.example.com", "worker-2.example.com"])
        self.assertTrue(response.context["client_filter_available"])


# ----------------------------------------------------------------------------- pages
class PageTests(Base):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        seed()

    def test_every_page_renders(self):
        self.login(self.viewer)
        for name in ("k8s:workloads", "k8s:services", "k8s:volumes", "k8s:overview"):
            with self.subTest(name):
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 200)
        detail = Workload.objects.get(workload_name="shop-web", container="web", pod_name="shop-web-aaa")
        self.assertEqual(self.client.get(detail.get_absolute_url()).status_code, 200)

    def test_tabs_are_on(self):
        self.login(self.viewer)
        html = self.client.get(reverse("k8s:workloads")).content.decode()
        for name in ("k8s:workloads", "k8s:services", "k8s:volumes"):
            self.assertIn(f'href="{reverse(name)}"', html)

    def test_workloads_totals_and_flags(self):
        self.login(self.viewer)
        response = self.client.get(reverse("k8s:workloads"))
        totals = response.context["totals"]
        self.assertEqual(totals["containers"], 7)
        self.assertEqual(totals["workloads"], 5)       # shop-web counts once for its three rows
        self.assertEqual(totals["latest"], 2)
        self.assertEqual(totals["short"], 2)            # shop-web (a pod with 1 of 2... ) and shop-db
        self.assertEqual(totals["pods"], 5)
        self.assertContains(response, "latest")
        self.assertContains(response, 'class="short"')

    def test_charts_have_data(self):
        self.login(self.viewer)
        response = self.client.get(reverse("k8s:workloads"))
        types = {s["label"]: s["value"] for s in response.context["type_donut"]["segments"]}
        self.assertEqual(types, {"Deployment": 3, "DaemonSet": 1, "StatefulSet": 1})
        pods = {s["label"]: s["value"] for s in response.context["pod_donut"]["segments"]}
        self.assertEqual(pods["Running"], 4)
        self.assertEqual(pods["Pending"], 1)
        self.assertEqual(pods["No pod"], 1)
        self.assertContains(response, 'data-chart="donut"')

    def test_workload_filters(self):
        self.login(self.viewer)
        url = reverse("k8s:workloads")

        def names(**params):
            response = self.client.get(url, params)
            return sorted({w.workload_name for w in response.context["items"]})

        self.assertEqual(names(client="Acme"), ["shop-db", "shop-web"])
        self.assertEqual(names(client="acme"), ["shop-db", "shop-web"])
        self.assertEqual(names(namespace="acme-uat"), ["shop-db"])
        self.assertEqual(names(type="DaemonSet"), ["node-agent"])
        self.assertEqual(names(health="warn"), ["shop-db", "shop-web"])
        self.assertEqual(names(registry="docker.io"), ["batch-runner", "shop-db"])
        self.assertEqual(names(pod="pending"), ["shop-db"])
        self.assertEqual(names(signal="latest"), ["batch-runner", "shop-web"])
        self.assertEqual(names(signal="short"), ["shop-db", "shop-web"])
        self.assertEqual(names(signal="no_pod"), ["batch-runner"])
        self.assertEqual(names(signal="unlinked"), ["node-agent"])
        self.assertEqual(names(q="sidecar"), ["shop-web"])
        self.assertEqual(names(q="203.0.113"), [])             # pod IP is not searched, hostnames/images are
        self.assertEqual(names(q="example.com:5000"), ["node-agent"])
        self.assertEqual(names(type="nonsense"), sorted(set(Workload.objects.values_list("workload_name", flat=True))))

    def test_node_filter_and_link(self):
        node = Node.objects.get(hostname="worker-1.example.com")
        self.login(self.viewer)
        response = self.client.get(reverse("k8s:workloads"), {"node": node.pk})
        self.assertEqual(response.context["total"], 3)
        self.assertEqual(response.context["node_filter"], node)
        self.assertContains(response, reverse("k8s:node_detail", args=[node.pk]))

    def test_node_detail_lists_what_runs_on_the_node(self):
        node = Node.objects.get(hostname="worker-1.example.com")
        self.login(self.viewer)
        response = self.client.get(reverse("k8s:node_detail", args=[node.pk]))
        summary = response.context["node_workloads"]
        self.assertEqual(summary["rows"], 3)
        self.assertTrue(summary["by_namespace"])
        self.assertContains(response, "Running here")
        self.assertContains(response, reverse("k8s:workloads") + "?node=%d" % node.pk)

    def test_node_detail_workload_panel_is_tenant_scoped(self):
        node = Node.objects.get(hostname="worker-1.example.com")
        self.login(self.viewer)
        with mock.patch("k8s.services.tenant", return_value="other-tenant"):
            from k8s.views import _node_workload_summary
            self.assertEqual(_node_workload_summary(node)["node_workloads"]["rows"], 0)

    def test_service_link_counts_on_the_workload_list(self):
        self.login(self.viewer)
        response = self.client.get(reverse("k8s:workloads"), {"q": "shop-web"})
        counts = {w.container: w.service_count for w in response.context["items"]}
        self.assertEqual(counts["web"], 2)      # shop-web, plus shop-edge of the same application
        self.assertEqual(Workload.objects.filter(workload_name="shop-web").count(), 3)

    def test_sort_and_pagination(self):
        self.login(self.viewer)
        response = self.client.get(reverse("k8s:workloads"), {"per": 5, "sort": "replicas"})
        self.assertEqual(len(response.context["items"]), 5)
        self.assertEqual(response.context["page"].paginator.num_pages, 2)
        self.assertEqual(self.client.get(reverse("k8s:workloads"), {"page": 99}).status_code, 200)

    def test_workload_detail(self):
        row = Workload.objects.get(workload_name="shop-web", container="web", pod_name="shop-web-aaa")
        self.login(self.viewer)
        response = self.client.get(row.get_absolute_url())
        self.assertEqual({c["name"] for c in response.context["containers"]}, {"web", "sidecar"})
        self.assertEqual(len(response.context["pods"]), 3)
        self.assertEqual([v.pvc for v in response.context["volumes"]], ["shop-data"])
        self.assertEqual({s.name for s in response.context["service_rows"]}, {"shop-web", "shop-edge"})
        self.assertTrue(response.context["any_latest"])
        self.assertContains(response, "registry.example.com/acme/proxy:latest")
        self.assertContains(response, "8080/")
        self.assertNotContains(response, "NOT DISCOVERED")
        db = Workload.objects.get(workload_name="shop-db")
        page = self.client.get(db.get_absolute_url())
        self.assertEqual(sorted(v.pvc for v in page.context["volumes"]), ["shop-db-data", "shop-db-logs"])
        self.assertEqual(page.context["missing_pvcs"], [])

    def test_workload_detail_404(self):
        self.login(self.viewer)
        self.assertEqual(self.client.get(reverse("k8s:workload_detail", args=[999999])).status_code, 404)

    def test_services_page(self):
        self.login(self.viewer)
        response = self.client.get(reverse("k8s:services"))
        totals = response.context["totals"]
        self.assertEqual((totals["ports"], totals["services"]), (5, 4))
        self.assertEqual((totals["node_port"], totals["load_balancer"], totals["external_name"]), (1, 1, 1))
        self.assertEqual(totals["headless"], 2)
        self.assertEqual(totals["no_selector"], 1)
        self.assertContains(response, "80 → 8080/TCP")
        self.assertContains(response, "30080")
        self.assertContains(response, "app=shop-edge")

    def test_service_filters(self):
        self.login(self.viewer)
        url = reverse("k8s:services")

        def names(**params):
            return sorted({s.name for s in self.client.get(url, params).context["items"]})

        self.assertEqual(names(type="NodePort"), ["shop-edge"])
        self.assertEqual(names(namespace="globex-prod"), ["external-db", "node-agent"])
        self.assertEqual(names(client="Acme"), ["shop-edge", "shop-web"])
        self.assertEqual(names(exposed="1"), ["external-db", "node-agent", "shop-edge"])
        self.assertEqual(names(headless="1"), ["external-db", "node-agent"])
        self.assertEqual(names(q="30080"), ["shop-edge"])
        self.assertEqual(names(q="9100"), ["node-agent"])
        self.assertEqual(names(q="edge"), ["shop-edge"])

    def test_volumes_page_and_kpis(self):
        self.login(self.viewer)
        response = self.client.get(reverse("k8s:volumes"))
        totals = response.context["totals"]
        self.assertEqual(totals["volumes"], 4)
        self.assertEqual(totals["capacity"], (10 + 20 + 30) * 1024 ** 3 + 100 * 1000 ** 3)
        self.assertEqual((totals["pending"], totals["unclaimed"], totals["high"]), (1, 1, 1))
        self.assertContains(response, 'class="review')                # the pending PVC is highlighted
        self.assertContains(response, 'class="warnrow')               # and the unclaimed PV
        self.assertContains(response, "unclaimed")
        self.assertContains(response, 'data-chart="bars"')

    def test_storage_by_client_chart(self):
        self.login(self.viewer)
        bars = self.client.get(reverse("k8s:volumes")).context["storage_bars"]
        by_label = dict(zip(bars["tipLabels"], bars["series"][0]["values"]))
        self.assertEqual(by_label["Acme"], round((10 + 20) * 1024 ** 3 / 1024 ** 3 + 100e9 / 1024 ** 3, 1))
        self.assertEqual(by_label["(no client)"], 30.0)

    def test_volume_filters(self):
        self.login(self.viewer)
        url = reverse("k8s:volumes")

        def pvcs(**params):
            return sorted((v.pvc or v.pv) for v in self.client.get(url, params).context["items"])

        self.assertEqual(pvcs(status="pending"), ["shop-db-data"])
        self.assertEqual(pvcs(status="unclaimed"), ["pv-orphan"])
        self.assertEqual(pvcs(status="bound"), ["shop-data", "shop-db-logs"])
        self.assertEqual(pvcs(**{"class": "slow"}), ["pv-orphan", "shop-db-logs"])
        self.assertEqual(pvcs(risk="high"), ["shop-db-data"])
        self.assertEqual(pvcs(client="Acme", namespace="acme-uat"), ["shop-db-data", "shop-db-logs"])
        self.assertEqual(pvcs(q="/logs"), ["shop-db-logs"])

    def test_the_overview_shows_the_stale_image_count(self):
        self.login(self.viewer)
        response = self.client.get(reverse("k8s:overview"))
        self.assertEqual(response.context["extra"]["stale_images"]["value"], 2)
        self.assertEqual(response.context["extra"]["workloads"]["value"], 5)
        self.assertEqual(response.context["extra"]["services"]["value"], 4)
        self.assertEqual(response.context["extra"]["volumes"]["value"], 4)
        self.assertContains(response, "Stale image")
        self.assertContains(response, reverse("k8s:workloads") + "?signal=latest")

    def test_the_overview_without_workloads_shows_a_dash_not_a_made_up_zero(self):
        Workload.objects.all().delete()
        self.login(self.viewer)
        response = self.client.get(reverse("k8s:overview"))
        self.assertNotIn("stale_images", response.context["extra"])
        self.assertContains(response, "Workloads not imported")


class CsvTests(Base):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        seed()

    def test_exports_are_attachments_and_follow_the_filters(self):
        self.login(self.viewer)
        response = self.client.get(reverse("k8s:workloads_export"), {"signal": "latest"})
        self.assertEqual(response["Content-Type"].split(";")[0], "text/csv")
        self.assertIn("kubernetes-workloads.csv", response["Content-Disposition"])
        rows = self.rows(response)
        self.assertEqual(len(rows), 1 + 2)
        header = rows[0]
        self.assertEqual({r[header.index("Workload")] for r in rows[1:]}, {"shop-web", "batch-runner"})
        self.assertEqual(self.rows(self.client.get(reverse("k8s:services_export")))[0][5], "Service")
        self.assertEqual(len(self.rows(self.client.get(reverse("k8s:volumes_export")))), 1 + 4)
        self.assertEqual(len(self.rows(self.client.get(reverse("k8s:services_export"), {"type": "NodePort"}))), 2)
        self.assertEqual(len(self.rows(self.client.get(reverse("k8s:volumes_export"), {"status": "pending"}))), 2)

    def test_exports_hold_no_placeholder_text(self):
        self.login(self.viewer)
        for name in ("k8s:workloads_export", "k8s:services_export", "k8s:volumes_export"):
            text = self.client.get(reverse(name)).content.decode()
            for marker in ("NOT DISCOVERED", "NOT APPLICABLE", "MANUAL INPUT REQUIRED"):
                self.assertNotIn(marker, text, name)

    def test_the_page_html_holds_no_placeholder_text(self):
        self.login(self.viewer)
        for name in ("k8s:workloads", "k8s:services", "k8s:volumes"):
            html = self.client.get(reverse(name)).content.decode()
            for marker in ("NOT DISCOVERED", "NOT APPLICABLE", "MANUAL INPUT REQUIRED"):
                self.assertNotIn(marker, html, name)

    def test_export_neutralises_spreadsheet_formulas(self):
        Workload.objects.filter(workload_name="shop-db").update(application="=HYPERLINK(\"x\")")
        self.login(self.viewer)
        text = self.client.get(reverse("k8s:workloads_export")).content.decode()
        self.assertNotIn(",=HYPERLINK", text)


# ----------------------------------------------------------------------------- scoping, tenants, access
class ScopingTests(Base):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        seed(site=MUM)

    PAGES = ["k8s:workloads", "k8s:services", "k8s:volumes"]
    EXPORTS = ["k8s:workloads_export", "k8s:services_export", "k8s:volumes_export"]

    def test_a_viewer_of_another_site_sees_nothing_anywhere(self):
        self.login(self.dr_viewer)
        for name in self.PAGES:
            with self.subTest(name):
                self.assertEqual(self.client.get(reverse(name)).context["total"], 0)
        for name in self.EXPORTS:
            with self.subTest(name):
                self.assertEqual(len(self.rows(self.client.get(reverse(name)))), 1)
        overview = self.client.get(reverse("k8s:overview"))
        self.assertNotIn("stale_images", overview.context["extra"])
        self.assertEqual(workload_signals(self.dr_viewer), {})

    def test_the_detail_page_of_another_site_is_a_404(self):
        row = Workload.objects.get(workload_name="shop-db")
        self.login(self.dr_viewer)
        self.assertEqual(self.client.get(row.get_absolute_url()).status_code, 404)

    def test_the_owning_site_sees_everything(self):
        self.login(self.mum_viewer)
        self.assertEqual(self.client.get(reverse("k8s:workloads")).context["total"], 7)
        self.assertEqual(self.client.get(reverse("k8s:services")).context["total"], 5)
        self.assertEqual(self.client.get(reverse("k8s:volumes")).context["total"], 4)

    def test_the_site_filter_ignores_a_site_the_user_cannot_see(self):
        self.login(self.dr_viewer)
        response = self.client.get(reverse("k8s:workloads"), {"site": MUM})
        self.assertEqual(response.context["site_filter"], "")
        self.assertEqual(response.context["total"], 0)

    def test_the_site_filter_narrows_for_a_user_who_sees_both(self):
        self.login(self.viewer)
        self.assertEqual(self.client.get(reverse("k8s:workloads"), {"site": MUM}).context["total"], 7)
        self.assertEqual(self.client.get(reverse("k8s:workloads"), {"site": DR}).context["total"], 0)
        self.assertEqual(self.client.get(reverse("k8s:volumes"), {"site": DR}).context["total"], 0)

    def test_global_rows_stay_visible_to_a_site_scoped_viewer(self):
        KubeCluster.objects.update(site="All Sites")
        self.login(self.dr_viewer)
        self.assertEqual(self.client.get(reverse("k8s:workloads")).context["total"], 7)

    def test_the_site_follows_the_cluster(self):
        w = Workload.objects.first()
        self.assertEqual(w.site, MUM)
        KubeCluster.objects.update(site=DR)
        w = Workload.objects.first()
        self.assertEqual(w.site, DR)

    def test_tenant_isolation(self):
        seed(site=MUM, tenant=OTHER)
        self.assertEqual(Workload.objects.count(), 14)
        self.login(self.viewer)
        self.assertEqual(self.client.get(reverse("k8s:workloads")).context["total"], 7)
        self.assertEqual(self.client.get(reverse("k8s:services")).context["total"], 5)
        self.assertEqual(self.client.get(reverse("k8s:volumes")).context["total"], 4)
        foreign = Workload.objects.filter(tenant_key=OTHER).first()
        self.assertEqual(self.client.get(foreign.get_absolute_url()).status_code, 404)
        # the dropdown options and the node link do not leak either
        response = self.client.get(reverse("k8s:workloads"))
        self.assertEqual(response.context["totals"]["containers"], 7)

    def test_importing_one_tenant_does_not_mark_the_other_tenants_rows_missing(self):
        seed(site=MUM, tenant=OTHER)
        directory = Path(tempfile.mkdtemp())
        fixtures.write_csv_dir(directory, sheets(**{"04_Application_Inventory": APPLICATIONS[:2]}))
        with override_settings(LINKEDEYE_TENANT=T):
            importer.run_import(directory, site=MUM)
        self.assertEqual(Workload.objects.filter(tenant_key=OTHER, missing_since__isnull=False).count(), 0)
        self.assertEqual(Workload.objects.filter(tenant_key=T, missing_since__isnull=False).count(), 6)


class AccessTests(Base):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        seed()

    def test_every_page_and_export_needs_a_sign_in(self):
        row = Workload.objects.first()
        urls = [reverse(n) for n in ("k8s:workloads", "k8s:services", "k8s:volumes", "k8s:workloads_export",
                                    "k8s:services_export", "k8s:volumes_export")] + [row.get_absolute_url()]
        for url in urls:
            with self.subTest(url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertIn("/accounts/login/", response["Location"])

    def test_the_pages_are_read_only(self):
        self.login(self.viewer)
        for name in ("k8s:workloads", "k8s:services", "k8s:volumes"):
            response = self.client.post(reverse(name), {"q": "x"})
            self.assertEqual(response.status_code, 200)       # a POST is just a GET here; nothing is written
        self.assertEqual(Workload.objects.count(), 7)
        self.assertEqual(Workload.objects.filter(workload_name="x").count(), 0)

    def test_a_viewer_may_read(self):
        self.login(self.viewer)
        self.assertEqual(self.client.get(reverse("k8s:workloads")).status_code, 200)

    def test_html_escapes_untrusted_values(self):
        Workload.objects.filter(workload_name="shop-db").update(application="<script>alert(1)</script>")
        Volume.objects.filter(pvc="shop-data").update(application="<img src=x onerror=alert(1)>")
        self.login(self.viewer)
        for name in ("k8s:workloads", "k8s:volumes"):
            html = self.client.get(reverse(name)).content.decode()
            self.assertNotIn("<script>alert(1)</script>", html)
            self.assertNotIn("<img src=x", html)
