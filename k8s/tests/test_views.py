"""Pages: rendering, filters, CSV, site scoping, tenant isolation, RBAC and the upload flow."""
import tempfile
from pathlib import Path

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from cmdb.models import ConfigurationItem
from core.models import AuditEvent, Client, Event, Membership, Site

from k8s import importer
from k8s.models import ClientProfile, ImportRun, ImportStatus, KubeCluster, Namespace, Node

from . import fixtures

T = "test-tenant"
OTHER = "other-tenant"
MUM = "Mumbai"
DR = "DR Site"

LIST_PAGES = ["k8s:overview", "k8s:clients", "k8s:namespaces", "k8s:nodes", "k8s:imports"]
EXPORTS = ["k8s:clients_export", "k8s:namespaces_export", "k8s:nodes_export"]


def seed(site="All Sites", tenant=T):
    """Import the synthetic sheets into `site`; returns the ImportResult."""
    directory = Path(tempfile.mkdtemp())
    fixtures.write_csv_dir(directory)
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
        cls.operator = User.objects.create_user("operator", password="pw-operator-12345")
        Membership.objects.create(user=cls.operator, site=None, role="operator")
        cls.admin = User.objects.create_user("admin", password="pw-admin-12345")
        Membership.objects.create(user=cls.admin, site=None, role="admin")
        cls.site_admin = User.objects.create_user("dr-admin", password="pw-dradmin-12345")
        Membership.objects.create(user=cls.site_admin, site=cls.dr, role="admin")
        cls.dr_viewer = User.objects.create_user("dr-viewer", password="pw-drviewer-12345")
        Membership.objects.create(user=cls.dr_viewer, site=cls.dr, role="viewer")

    def login(self, user):
        self.client.force_login(user)
        return user


class PageTests(Base):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        seed(site=MUM)

    def test_every_page_renders(self):
        self.login(self.viewer)
        for name in LIST_PAGES:
            with self.subTest(name):
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Kubernetes")

    def test_overview_shows_the_imported_numbers(self):
        self.login(self.viewer)
        response = self.client.get(reverse("k8s:overview"))
        self.assertEqual(response.context["nodes"]["total"], 3)
        self.assertEqual(response.context["namespaces"]["total"], 4)
        self.assertEqual(response.context["namespaces"]["review"], 1)
        self.assertEqual(response.context["profiles"]["total"], 3)
        self.assertEqual(response.context["cluster_count"], 1)
        self.assertTrue(response.context["imported"])
        self.assertContains(response, "Acme")

    def test_overview_has_no_data_state_without_an_import(self):
        Node.objects.all().delete()
        Namespace.objects.all().delete()
        ClientProfile.objects.all().delete()
        KubeCluster.objects.all().delete()
        ImportRun.objects.all().delete()
        self.login(self.viewer)
        response = self.client.get(reverse("k8s:overview"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No inventory has been imported yet")
        self.assertNotContains(response, "<div class=\"value\">0</div>")

    def test_overview_counts_unbuilt_sheets_from_the_last_import(self):
        self.login(self.viewer)
        run = ImportRun.objects.first()
        run.counts = {**run.counts, "12_Issues_Risks": {"rows": 42}}
        run.save(update_fields=["counts"])
        extra = self.client.get(reverse("k8s:overview")).context["extra"]
        self.assertEqual(extra["issues"], {"value": 42, "source": "last import", "sheet": "12_Issues_Risks"})
        self.assertIsNone(extra["tools"]["value"])
        self.assertIsNone(extra["tools"]["source"])

    def test_node_detail_shows_labels_taints_capacity_and_the_linked_ci(self):
        ci = ConfigurationItem.objects.create(tenant_key=T, name="cp-1.example.com", site=MUM)
        node = Node.objects.get(hostname="cp-1.example.com")
        node.ci = ci
        node.save(update_fields=["ci"])
        self.login(self.viewer)
        response = self.client.get(reverse("k8s:node_detail", args=[node.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "node-role.kubernetes.io/control-plane")
        self.assertContains(response, "NoSchedule")
        self.assertContains(response, "16314368Ki")
        self.assertContains(response, ci.name)
        self.assertContains(response, reverse("cmdb:item_detail", args=[ci.pk]))

    def test_overview_client_chart_only_when_clients_carry_numbers(self):
        self.login(self.viewer)
        response = self.client.get(reverse("k8s:overview"))
        bars = response.context["client_bars"]
        if bars:
            self.assertEqual({s["name"] for s in bars["series"]}, {"Applications", "Open issues"})
            self.assertContains(response, 'data-src="k8-client-bars"')
        else:
            self.assertNotContains(response, "k8-client-bars")

    def test_node_detail_404_for_an_unknown_node(self):
        self.login(self.viewer)
        self.assertEqual(self.client.get(reverse("k8s:node_detail", args=[9999])).status_code, 404)

    def test_node_filters(self):
        self.login(self.viewer)
        url = reverse("k8s:nodes")
        self.assertEqual(self.client.get(url, {"role": "control_plane"}).context["total"], 1)
        self.assertEqual(self.client.get(url, {"role": "worker"}).context["total"], 2)
        self.assertEqual(self.client.get(url, {"exposure": "public"}).context["total"], 1)
        self.assertEqual(self.client.get(url, {"state": "not_ready"}).context["total"], 1)
        self.assertEqual(self.client.get(url, {"state": "unlinked"}).context["total"], 3)
        self.assertEqual(self.client.get(url, {"q": "worker-1"}).context["total"], 1)
        self.assertEqual(self.client.get(url, {"q": "Ubuntu 22.04"}).context["total"], 1)
        self.assertEqual(self.client.get(url, {"role": "nonsense"}).context["total"], 3)
        cluster = KubeCluster.objects.get()
        self.assertEqual(self.client.get(url, {"cluster": cluster.pk}).context["total"], 3)
        self.assertEqual(self.client.get(url, {"cluster": "abc"}).context["total"], 3)
        # the client filter is offered now that workloads exist; with none imported it matches no node
        # (the filtered result with workloads is in test_workloads.py)
        self.assertTrue(self.client.get(url).context["client_filter_available"])
        self.assertEqual(self.client.get(url, {"client": "Acme"}).context["total"], 0)

    def test_namespace_filters_and_review_highlight(self):
        self.login(self.viewer)
        url = reverse("k8s:namespaces")
        self.assertEqual(self.client.get(url, {"status": "review"}).context["total"], 1)
        self.assertEqual(self.client.get(url, {"confidence": "high"}).context["total"], 2)
        self.assertEqual(self.client.get(url, {"client": "Acme"}).context["total"], 2)
        self.assertEqual(self.client.get(url, {"environment": "PROD"}).context["total"], 1)
        self.assertEqual(self.client.get(url, {"q": "sandbox"}).context["total"], 1)
        response = self.client.get(url)
        self.assertContains(response, "Review required")
        self.assertContains(response, 'class="review"')

    def test_client_filters_and_rollup(self):
        self.login(self.viewer)
        url = reverse("k8s:clients")
        self.assertEqual(self.client.get(url, {"health": "crit"}).context["total"], 1)
        self.assertEqual(self.client.get(url, {"linked": "no"}).context["total"], 3)
        self.assertEqual(self.client.get(url, {"q": "CL-001"}).context["total"], 1)
        rows = {r["profile"].workbook_name: r for r in self.client.get(url).context["rows"]}
        self.assertEqual(rows["Acme"]["namespaces"], 2)
        self.assertEqual(rows["Globex"]["namespaces"], 1)
        self.assertIn("GiB", rows["Acme"]["storage"])

    def test_import_detail_shows_the_discovery_trail(self):
        self.login(self.viewer)
        run = ImportRun.objects.first()
        response = self.client.get(reverse("k8s:import_detail", args=[run.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "kubectl get nodes -o wide")
        self.assertContains(response, "Secret values were not fetched")
        self.assertContains(response, "03_Node_Inventory")

    def test_pagination_and_per_page(self):
        self.login(self.viewer)
        response = self.client.get(reverse("k8s:nodes"), {"per": 5})
        self.assertEqual(response.context["page"].paginator.per_page, 5)
        self.assertEqual(len(response.context["items"]), 3)
        for value, expected in ((2, 5), (9999, 100), ("lots", 20), ("", 20)):
            with self.subTest(per=value):
                page = self.client.get(reverse("k8s:nodes"), {"per": value}).context["page"]
                self.assertEqual(page.paginator.per_page, expected)
        self.assertEqual(self.client.get(reverse("k8s:nodes"), {"page": 99}).context["page"].number, 1)


class CsvTests(Base):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        seed(site=MUM)

    def test_exports_are_csv_attachments_with_the_filtered_rows(self):
        self.login(self.viewer)
        for name in EXPORTS:
            with self.subTest(name):
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response["Content-Type"].startswith("text/csv"))
                self.assertIn("attachment; filename=", response["Content-Disposition"])
        body = self.client.get(reverse("k8s:nodes_export"), {"role": "worker"}).content.decode()
        self.assertIn("worker-1.example.com", body)
        self.assertNotIn("cp-1.example.com", body)
        self.assertIn("74098976Ki", body)

    def test_exports_hold_no_placeholder_text(self):
        self.login(self.viewer)
        for name in EXPORTS:
            body = self.client.get(reverse(name)).content.decode()
            for placeholder in ("NOT DISCOVERED", "NOT APPLICABLE", "MANUAL INPUT REQUIRED",
                                "MULTIPLE / SEE NODE INVENTORY"):
                self.assertNotIn(placeholder, body, f"{name} / {placeholder}")

    def test_export_is_site_scoped(self):
        self.login(self.dr_viewer)
        self.assertNotIn("worker-1.example.com",
                         self.client.get(reverse("k8s:nodes_export")).content.decode())


class ScopingTests(Base):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        seed(site=MUM)

    def test_a_site_scoped_viewer_sees_nothing_from_another_site(self):
        self.login(self.dr_viewer)
        self.assertEqual(self.client.get(reverse("k8s:nodes")).context["total"], 0)
        self.assertEqual(self.client.get(reverse("k8s:namespaces")).context["total"], 0)
        self.assertEqual(self.client.get(reverse("k8s:clients")).context["total"], 0)
        overview = self.client.get(reverse("k8s:overview"))
        self.assertEqual(overview.context["nodes"]["total"], 0)
        self.assertEqual(overview.context["cluster_count"], 0)

    def test_a_site_scoped_viewer_cannot_open_another_site_s_node(self):
        node = Node.objects.get(hostname="cp-1.example.com")
        self.login(self.dr_viewer)
        self.assertEqual(self.client.get(reverse("k8s:node_detail", args=[node.pk])).status_code, 404)

    def test_global_records_stay_visible_to_a_site_scoped_viewer(self):
        KubeCluster.objects.update(site="All Sites")
        Namespace.objects.update(site="All Sites")
        ClientProfile.objects.update(site="All Sites")
        self.login(self.dr_viewer)
        self.assertEqual(self.client.get(reverse("k8s:nodes")).context["total"], 3)

    def test_site_filter_ignores_a_site_the_user_cannot_see(self):
        self.login(self.dr_viewer)
        response = self.client.get(reverse("k8s:nodes"), {"site": MUM})
        self.assertEqual(response.context["site_filter"], "")
        self.assertEqual(response.context["total"], 0)

    def test_site_filter_narrows_for_a_user_who_can_see_both(self):
        self.login(self.viewer)
        self.assertEqual(self.client.get(reverse("k8s:nodes"), {"site": MUM}).context["total"], 3)
        self.assertEqual(self.client.get(reverse("k8s:nodes"), {"site": DR}).context["total"], 0)

    def test_tenant_isolation_on_the_pages(self):
        with override_settings(LINKEDEYE_TENANT=OTHER):
            seed(site=MUM, tenant=OTHER)
        self.login(self.viewer)
        self.assertEqual(self.client.get(reverse("k8s:nodes")).context["total"], 3)
        self.assertEqual(self.client.get(reverse("k8s:clients")).context["total"], 3)
        self.assertEqual(Node.objects.count(), 6)      # both tenants are in the table


class AccessTests(Base):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        seed(site=MUM)

    def test_pages_need_a_sign_in(self):
        for name in LIST_PAGES + EXPORTS:
            with self.subTest(name):
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 302)
                self.assertIn("/accounts/login/", response["Location"])

    def test_a_viewer_may_read_but_not_import(self):
        self.login(self.viewer)
        self.assertEqual(self.client.get(reverse("k8s:overview")).status_code, 200)
        self.assertEqual(self.client.get(reverse("k8s:import_inventory")).status_code, 403)
        self.assertFalse(self.client.get(reverse("k8s:overview")).context["can_import"])

    def test_an_operator_may_not_import(self):
        self.login(self.operator)
        self.assertEqual(self.client.get(reverse("k8s:import_inventory")).status_code, 403)
        self.assertFalse(self.client.get(reverse("k8s:overview")).context["can_import"])

    def test_an_admin_on_every_site_may_import(self):
        self.login(self.admin)
        response = self.client.get(reverse("k8s:import_inventory"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["can_import"])

    def test_a_single_site_admin_may_not_import_the_whole_estate(self):
        self.login(self.site_admin)
        self.assertEqual(self.client.get(reverse("k8s:import_inventory")).status_code, 403)

    def test_a_superuser_may_import(self):
        self.login(User.objects.create_superuser("root", "root@example.com", "pw-root-123456"))
        self.assertEqual(self.client.get(reverse("k8s:import_inventory")).status_code, 200)

    def test_linking_a_client_needs_admin_on_the_record_s_site(self):
        profile = ClientProfile.objects.get(workbook_name="Acme")
        client = Client.objects.create(tenant_key=T, name="Acme", code="acme")
        url = reverse("k8s:client_link", args=[profile.pk])

        self.login(self.viewer)
        self.client.post(url, {"client": str(client.pk)})
        profile.refresh_from_db()
        self.assertIsNone(profile.client_id)

        self.login(self.operator)
        self.client.post(url, {"client": str(client.pk)})
        profile.refresh_from_db()
        self.assertIsNone(profile.client_id)

        self.login(self.site_admin)            # admin, but on another site
        self.assertEqual(self.client.post(url, {"client": str(client.pk)}).status_code, 404)

        self.login(self.admin)
        response = self.client.post(url, {"client": str(client.pk)})
        self.assertEqual(response.status_code, 302)
        profile.refresh_from_db()
        self.assertEqual(profile.client_id, client.pk)
        self.assertTrue(AuditEvent.objects.filter(action="update",
                                                   object_type="k8s.clientprofile").exists())

    def test_unlinking_a_client(self):
        client = Client.objects.create(tenant_key=T, name="Acme", code="acme")
        profile = ClientProfile.objects.get(workbook_name="Acme")
        profile.client = client
        profile.save(update_fields=["client"])
        self.login(self.admin)
        self.client.post(reverse("k8s:client_link", args=[profile.pk]), {"client": ""})
        profile.refresh_from_db()
        self.assertIsNone(profile.client_id)

    def test_link_rejects_a_client_id_that_is_not_an_option(self):
        profile = ClientProfile.objects.get(workbook_name="Acme")
        self.login(self.admin)
        response = self.client.post(reverse("k8s:client_link", args=[profile.pk]), {"client": "9999"})
        self.assertEqual(response.status_code, 302)
        profile.refresh_from_db()
        self.assertIsNone(profile.client_id)

    def test_link_rejects_get(self):
        profile = ClientProfile.objects.get(workbook_name="Acme")
        self.login(self.admin)
        self.assertEqual(self.client.get(reverse("k8s:client_link", args=[profile.pk])).status_code, 405)


class UploadTests(Base):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))

    def workbook(self, name="inventory.xlsx"):
        path = fixtures.write_xlsx(self.dir / name)
        return open(path, "rb")

    def test_upload_previews_then_imports(self):
        self.login(self.admin)
        url = reverse("k8s:import_inventory")
        with self.workbook() as fh:
            preview = self.client.post(url, {"file": fh, "site": MUM})
        self.assertEqual(preview.status_code, 200)
        result = preview.context["result"]
        self.assertTrue(result.dry_run)
        self.assertEqual(result.counts["03_Node_Inventory"]["created"], 3)
        self.assertContains(preview, "Import 1")              # the confirm button
        self.assertEqual(Node.objects.count(), 0)             # nothing written yet
        self.assertEqual(ImportRun.objects.count(), 0)

        token = preview.context["pending"]["token"]
        applied = self.client.post(url, {"action": "confirm", "token": token})
        self.assertEqual(applied.status_code, 302)
        self.assertEqual(Node.objects.count(), 3)
        self.assertEqual(Namespace.objects.count(), 4)
        run = ImportRun.objects.get()
        self.assertEqual(run.status, ImportStatus.OK)
        self.assertEqual(run.source_name, "inventory.xlsx")
        self.assertEqual(run.uploaded_by, self.admin)
        self.assertEqual(run.site, MUM)
        self.assertEqual(KubeCluster.objects.get().site, MUM)
        self.assertEqual(applied["Location"], reverse("k8s:import_detail", args=[run.pk]))
        self.assertTrue(AuditEvent.objects.filter(action="import",
                                                   object_type="k8s.importrun").exists())
        self.assertTrue(Event.objects.filter(source="k8s").exists())

    def test_confirm_without_a_staged_file_is_refused(self):
        self.login(self.admin)
        response = self.client.post(reverse("k8s:import_inventory"),
                                    {"action": "confirm", "token": "0" * 32}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Node.objects.count(), 0)
        self.assertContains(response, "expired")

    def test_confirm_with_a_path_shaped_token_is_refused(self):
        self.login(self.admin)
        for token in ("../../etc/passwd", "not-hex-at-all", "", "A" * 32):
            with self.subTest(token):
                response = self.client.post(reverse("k8s:import_inventory"),
                                            {"action": "confirm", "token": token})
                self.assertEqual(response.status_code, 302)
                self.assertEqual(Node.objects.count(), 0)

    def test_a_viewer_cannot_upload(self):
        self.login(self.viewer)
        with self.workbook() as fh:
            response = self.client.post(reverse("k8s:import_inventory"), {"file": fh, "site": MUM})
        self.assertNotEqual(response.status_code, 200)
        self.assertEqual(Node.objects.count(), 0)
        self.assertEqual(ImportRun.objects.count(), 0)

    def test_an_operator_cannot_upload(self):
        self.login(self.operator)
        with self.workbook() as fh:
            response = self.client.post(reverse("k8s:import_inventory"), {"file": fh, "site": MUM})
        self.assertNotEqual(response.status_code, 200)
        self.assertEqual(Node.objects.count(), 0)
        self.assertEqual(ImportRun.objects.count(), 0)

    def test_a_single_site_admin_cannot_upload(self):
        self.login(self.site_admin)
        with self.workbook() as fh:
            response = self.client.post(reverse("k8s:import_inventory"), {"file": fh, "site": DR})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(ImportRun.objects.count(), 0)

    def test_upload_requires_csrf(self):
        enforcing = self.client_class(enforce_csrf_checks=True)
        enforcing.force_login(self.admin)
        with self.workbook() as fh:
            response = enforcing.post(reverse("k8s:import_inventory"), {"file": fh, "site": MUM})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(ImportRun.objects.count(), 0)

    def test_upload_rejects_a_wrong_file_type_and_an_empty_file(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        self.login(self.admin)
        url = reverse("k8s:import_inventory")
        response = self.client.post(url, {"file": SimpleUploadedFile("notes.pdf", b"%PDF-1.4"),
                                           "site": MUM})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors)
        response = self.client.post(url, {"file": SimpleUploadedFile("sheets.zip", b""), "site": MUM})
        self.assertTrue(response.context["form"].errors)
        self.assertEqual(ImportRun.objects.count(), 0)

    def test_upload_rejects_an_oversized_file(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from k8s.forms import MAX_UPLOAD_BYTES
        self.login(self.admin)
        big = SimpleUploadedFile("inventory.xlsx", b"x" * (MAX_UPLOAD_BYTES + 1))
        response = self.client.post(reverse("k8s:import_inventory"), {"file": big, "site": MUM})
        self.assertIn("larger than", str(response.context["form"].errors))
        self.assertEqual(ImportRun.objects.count(), 0)

    def test_a_malformed_upload_is_reported_not_raised(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        self.login(self.admin)
        response = self.client.post(reverse("k8s:import_inventory"),
                                    {"file": SimpleUploadedFile("inventory.xlsx", b"PK\x03\x04 not a workbook"),
                                     "site": MUM})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["result"].ok)
        self.assertTrue(response.context["result"].errors)
        self.assertFalse(response.context["pending"])
        self.assertEqual(Node.objects.count(), 0)

    def test_upload_can_mirror_into_compute(self):
        from compute.models import Cluster
        self.login(self.admin)
        url = reverse("k8s:import_inventory")
        with self.workbook() as fh:
            preview = self.client.post(url, {"file": fh, "site": MUM, "create_compute": "on"})
        self.assertEqual(Cluster.objects.count(), 0)
        self.client.post(url, {"action": "confirm", "token": preview.context["pending"]["token"]})
        self.assertEqual(Cluster.objects.filter(tenant_key=T).count(), 1)
