"""Regression tests for the code-review findings in the k8s area (k8s-1 ... k8s-10, AZ-05).

Everything is synthetic ("Acme"/"Globex", example.com): the real workbook is never copied here.
"""
import io
import tempfile
import zipfile
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from core.models import Membership, Site

from k8s import importer, readers, services
from k8s.models import (ClientProfile, Health, ImportRun, ImportStatus, KubeCluster, Namespace, Node)
from k8s.models_ops import InventoryIssue
from k8s.models_workload import Risk, Volume, Workload

from . import fixtures, test_ops, test_workloads

T = "test-tenant"
MUM = "Mumbai"
DR = "DR Site"


def node_row(hostname, cluster="kubernetes"):
    row = list(fixtures.NODE_INVENTORY[1])
    row[0] = cluster
    row[1] = hostname
    return row


def node_sheet(*rows):
    return [fixtures.NODE_INVENTORY[0]] + list(rows)


def write(sheets, extra=None):
    directory = Path(tempfile.mkdtemp())
    fixtures.write_csv_dir(directory, sheets=sheets, extra=extra)
    return directory


# ----------------------------------------------------------------------------- k8s-1
@override_settings(LINKEDEYE_TENANT=T)
class TruncatedSheetTests(TestCase):
    def test_a_sheet_over_the_row_cap_is_flagged_truncated(self):
        rows = iter([["a"]] + [[str(i)] for i in range(10)])
        with mock.patch.object(readers, "MAX_ROWS_PER_SHEET", 4):
            sheet = readers._sheet_from_rows("x", rows)
        self.assertTrue(sheet.truncated)
        self.assertEqual(len(sheet.rows), 4)

    def test_a_sheet_exactly_at_the_cap_is_not_truncated(self):
        rows = iter([["a"]] + [[str(i)] for i in range(4)])
        with mock.patch.object(readers, "MAX_ROWS_PER_SHEET", 4):
            sheet = readers._sheet_from_rows("x", rows)
        self.assertFalse(sheet.truncated)
        self.assertEqual(len(sheet.rows), 4)

    def test_data_beyond_the_scan_cap_is_flagged_truncated(self):
        rows = iter([["a"]] + [[None]] * 2 + [["1"]] * 5)
        with mock.patch.object(readers, "MAX_SCANNED_ROWS", 3):
            sheet = readers._sheet_from_rows("x", rows)
        self.assertTrue(sheet.truncated)

    def test_a_truncated_sheet_retires_nothing_and_is_reported(self):
        names = [f"w{i}.example.com" for i in range(10)]
        directory = write({**fixtures.SHEETS, "03_Node_Inventory": node_sheet(*[node_row(n) for n in names])})
        self.assertTrue(importer.run_import(directory).ok)
        self.assertEqual(Node.objects.filter(missing_since__isnull=True).count(), 10)

        with mock.patch.object(readers, "MAX_ROWS_PER_SHEET", 4):
            result = importer.run_import(directory)
        self.assertEqual(result.status, ImportStatus.FAILED)
        self.assertTrue(any("03_Node_Inventory" in e and "100" not in e and "limit" in e.lower()
                            for e in result.errors), result.errors)
        self.assertEqual(Node.objects.filter(missing_since__isnull=False).count(), 0)
        self.assertEqual(KubeCluster.objects.filter(missing_since__isnull=False).count(), 0)


# ----------------------------------------------------------------------------- k8s-2
@override_settings(LINKEDEYE_TENANT=T)
class VanishedClusterTests(TestCase):
    def test_nodes_of_a_cluster_that_left_the_node_sheet_are_retired_with_it(self):
        both = node_sheet(node_row("a.example.com"), node_row("b.example.com"), node_row("c.example.com", "dr"))
        self.assertTrue(importer.run_import(write({**fixtures.SHEETS, "03_Node_Inventory": both})).ok)
        only_prod = node_sheet(node_row("a.example.com"), node_row("b.example.com"))
        result = importer.run_import(write({**fixtures.SHEETS, "03_Node_Inventory": only_prod}))
        self.assertTrue(result.ok, result.errors)
        self.assertIsNotNone(KubeCluster.objects.get(name="dr").missing_since)
        self.assertIsNotNone(Node.objects.get(hostname="c.example.com").missing_since)
        self.assertIsNone(Node.objects.get(hostname="a.example.com").missing_since)
        self.assertEqual(result.counts["03_Node_Inventory"]["missing"], 2)
        self.assertEqual(services.node_totals(Node.objects.filter(missing_since__isnull=True))["total"], 2)


# ----------------------------------------------------------------------------- k8s-3
@override_settings(LINKEDEYE_TENANT=T)
class PartialNamespaceImportTests(TestCase):
    def test_a_mapping_only_import_does_not_retire_namespaces_other_sheets_mention(self):
        apps_rows = [list(r) for r in test_workloads.APPLICATIONS]
        stub = list(apps_rows[1])
        stub[3] = "stubns"
        apps_rows.append(stub)
        full = {**fixtures.SHEETS, **test_workloads.WORKLOAD_SHEETS, "04_Application_Inventory": apps_rows}
        self.assertTrue(importer.run_import(write(full)).ok)
        self.assertTrue(Namespace.objects.filter(name="stubns").exists())

        only_mapping = write({"14_Namespace_Mapping": fixtures.NAMESPACE_MAPPING})
        result = importer.run_import(only_mapping)
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(Namespace.objects.filter(missing_since__isnull=False).count(), 0)
        self.assertTrue(any("namespaces" in w.lower() for w in result.warnings), result.warnings)
        self.assertEqual(Workload.objects.filter(missing_since__isnull=False).count(), 0)


# ----------------------------------------------------------------------------- k8s-4
@override_settings(LINKEDEYE_TENANT=T)
class ClientSummaryCaseTests(TestCase):
    def test_a_case_variant_in_the_summary_updates_the_master_profile(self):
        summary = [list(r) for r in fixtures.CLIENT_SUMMARY]
        summary[1][0] = "ACME"
        sheets = {**fixtures.SHEETS, "13_Client_Summary": summary}
        for _ in range(2):
            result = importer.run_import(write(sheets))
            self.assertTrue(result.ok, result.errors)
        names = sorted(ClientProfile.objects.values_list("workbook_name", flat=True))
        self.assertEqual(names, ["Acme", "Globex", "Others/Unmapped"])
        acme = ClientProfile.objects.get(workbook_name="Acme")
        self.assertFalse(acme.summary_only)
        self.assertEqual(acme.health, Health.WARN)
        self.assertEqual(acme.open_issues, 2)


# ----------------------------------------------------------------------------- k8s-5
@override_settings(LINKEDEYE_TENANT=T)
class ImportCostTests(TestCase):
    def test_a_reimport_costs_far_fewer_queries_than_rows(self):
        header = test_workloads.APPLICATIONS[0]
        rows = []
        for i in range(60):
            row = list(test_workloads.APPLICATIONS[1])
            row[14] = f"c{i}"        # Container
            rows.append(row)
        directory = write({**fixtures.SHEETS, "04_Application_Inventory": [header] + rows})
        self.assertTrue(importer.run_import(directory).ok)
        self.assertEqual(Workload.objects.count(), 60)
        with CaptureQueriesContext(connection) as ctx:
            result = importer.run_import(directory)
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.total_created, 0)
        self.assertEqual(result.total_updated, 0)
        self.assertLess(len(ctx.captured_queries), 60, "a re-import must not cost a query per row")
        self.assertEqual(Workload.objects.filter(last_seen=result.started_at).count(), 60)
        self.assertEqual(Workload.objects.exclude(import_run_id=result.run_id).count(), 0)

    def test_a_changed_row_is_still_updated_and_a_returning_row_is_revived(self):
        directory = write(fixtures.SHEETS)
        importer.run_import(directory)
        Node.objects.filter(hostname="worker-1.example.com").update(missing_since=timezone.now())
        rows = [list(r) for r in fixtures.NODE_INVENTORY]
        rows[2][7] = "Ubuntu 25.10"
        result = importer.run_import(write({**fixtures.SHEETS, "03_Node_Inventory": rows}))
        self.assertEqual(result.counts["03_Node_Inventory"]["updated"], 1)
        node = Node.objects.get(hostname="worker-1.example.com")
        self.assertEqual(node.os, "Ubuntu 25.10")
        self.assertIsNone(node.missing_since)

    def test_a_stale_running_import_is_marked_failed(self):
        old = ImportRun.objects.create(tenant_key=T, source_name="killed.xlsx", status=ImportStatus.RUNNING,
                                       started_at=timezone.now() - timezone.timedelta(hours=3))
        fresh = ImportRun.objects.create(tenant_key=T, source_name="going.xlsx", status=ImportStatus.RUNNING)
        self.assertEqual(services.fail_stale_runs(), 1)
        old.refresh_from_db()
        fresh.refresh_from_db()
        self.assertEqual(old.status, ImportStatus.FAILED)
        self.assertTrue(old.errors)
        self.assertIsNotNone(old.finished_at)
        self.assertEqual(fresh.status, ImportStatus.RUNNING)


# ----------------------------------------------------------------------------- k8s-6
class ReaderBudgetTests(TestCase):
    def _zip(self, members):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in members.items():
                archive.writestr(name, data)
        buffer.seek(0)
        return buffer

    def test_a_sheet_over_the_cell_budget_is_refused(self):
        text = "a,b,c,d\n" + "1,2,3,4\n" * 50
        with mock.patch.object(readers, "MAX_CELLS_PER_SHEET", 100):
            with self.assertRaises(readers.SourceError):
                readers.read_zip(self._zip({"x.csv": text}), name="s.zip")

    def test_the_whole_source_has_a_cell_budget(self):
        text = "a,b\n" + "1,2\n" * 40
        members = {f"s{i}.csv": text for i in range(5)}
        with mock.patch.object(readers, "MAX_CELLS_PER_SOURCE", 300):
            with self.assertRaises(readers.SourceError):
                readers.read_zip(self._zip(members), name="s.zip")

    def test_sheets_nobody_imports_are_not_parsed(self):
        archive = self._zip({"03_Node_Inventory.csv": "Hostname\nw1\n", "Random_Extra.csv": "a\n1\n"})
        source = readers.read_zip(archive, name="s.zip", only={"03_Node_Inventory"})
        self.assertEqual(list(source.sheets), ["03_Node_Inventory"])
        self.assertEqual(source.unread, ["Random_Extra"])

    def test_unread_sheets_are_reported_as_unknown_by_the_importer(self):
        directory = write(fixtures.SHEETS, extra={"Random_Extra": [["a"], ["1"]]})
        with override_settings(LINKEDEYE_TENANT=T):
            result = importer.run_import(directory)
        self.assertEqual(result.unknown_sheets, ["Random_Extra"])

    def test_a_full_sheet_dimension_xlsx_is_read_within_bounds(self):
        import openpyxl
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wide.xlsx"
            book = openpyxl.Workbook()
            sheet = book.active
            sheet.title = "03_Node_Inventory"
            sheet.append(["Hostname", "Role"])
            sheet.append(["w1.example.com", "Worker"])
            book.save(str(path))
            # rewrite the declared dimension to the whole sheet, as a hostile file would
            patched = Path(tmp) / "hostile.xlsx"
            with zipfile.ZipFile(path) as src, zipfile.ZipFile(patched, "w", zipfile.ZIP_DEFLATED) as dst:
                for info in src.infolist():
                    data = src.read(info.filename)
                    if info.filename.startswith("xl/worksheets/sheet"):
                        data = data.replace(b'<dimension ref="A1:B2" />', b'<dimension ref="A1:XFD1048576" />')
                        self.assertIn(b"XFD1048576", data)
                        data = data.replace(b"</sheetData>", b'<row r="1048576"><c r="A1048576" t="inlineStr">'
                                                              b"<is><t>far</t></is></c></row></sheetData>")
                        self.assertIn(b"far", data)
                    dst.writestr(info.filename, data)
            from openpyxl.worksheet._read_only import ReadOnlyWorksheet
            real = ReadOnlyWorksheet.iter_rows
            calls = []

            def spy(sheet_self, *args, **kwargs):
                calls.append(kwargs)
                return real(sheet_self, *args, **kwargs)

            with mock.patch.object(ReadOnlyWorksheet, "iter_rows", spy):
                source = readers.read_xlsx(patched)
            # padding every row to 16,384 columns (7+ s of CPU for this 5 KB file) is what the bound prevents
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0].get("max_col"), readers.MAX_COLUMNS)
            self.assertLessEqual(calls[0].get("max_row") or 10 ** 9, readers.MAX_SCANNED_ROWS + 1)
        self.assertEqual(len(source.sheets["03_Node_Inventory"].rows), 1)


# ----------------------------------------------------------------------------- k8s-7
@override_settings(LINKEDEYE_TENANT=T)
class ClosedIssueTests(TestCase):
    def test_a_closed_issue_is_not_rewritten_on_every_import(self):
        closed = list(test_ops.ISSUE_ROWS[0])
        closed[-1] = "Closed"
        directory = write({**fixtures.SHEETS}, extra=test_ops.ops_sheets(issues=[closed]))
        first = importer.run_import(directory)
        self.assertTrue(first.ok, first.errors)
        stored = InventoryIssue.objects.get(issue_id="ISS-0001").closed_at
        self.assertIsNotNone(stored)
        for _ in range(2):
            again = importer.run_import(directory)
            self.assertEqual(again.counts["12_Issues_Risks"]["updated"], 0)
            self.assertEqual(InventoryIssue.objects.get(issue_id="ISS-0001").closed_at, stored)

    def test_an_open_issue_that_closes_gets_a_closed_at(self):
        opened = list(test_ops.ISSUE_ROWS[0])
        importer.run_import(write({**fixtures.SHEETS}, extra=test_ops.ops_sheets(issues=[opened])))
        self.assertIsNone(InventoryIssue.objects.get(issue_id="ISS-0001").closed_at)
        closed = list(opened)
        closed[-1] = "Closed"
        result = importer.run_import(write({**fixtures.SHEETS}, extra=test_ops.ops_sheets(issues=[closed])))
        self.assertEqual(result.counts["12_Issues_Risks"]["updated"], 1)
        self.assertIsNotNone(InventoryIssue.objects.get(issue_id="ISS-0001").closed_at)


# ----------------------------------------------------------------------------- views
@override_settings(LINKEDEYE_TENANT=T)
class ViewBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.mum = Site.objects.create(tenant_key=T, name=MUM, code="mum")
        cls.dr = Site.objects.create(tenant_key=T, name=DR, code="dr")
        cls.admin = User.objects.create_user("admin", password="pw-admin-12345")
        Membership.objects.create(user=cls.admin, site=None, role="admin")
        cls.mum_viewer = User.objects.create_user("mum-viewer", password="pw-mumviewer-12345")
        Membership.objects.create(user=cls.mum_viewer, site=cls.mum, role="viewer")
        cls.dr_viewer = User.objects.create_user("dr-viewer", password="pw-drviewer-12345")
        Membership.objects.create(user=cls.dr_viewer, site=cls.dr, role="viewer")


# ----------------------------------------------------------------------------- k8s-8
class OverviewFallbackTests(ViewBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        with override_settings(LINKEDEYE_TENANT=T):
            directory = write({**fixtures.SHEETS}, extra=test_ops.ops_sheets())
            importer.run_import(directory, site=DR)

    def test_a_site_limited_viewer_does_not_see_the_estate_wide_row_counts(self):
        self.client.force_login(self.mum_viewer)
        extra = self.client.get(reverse("k8s:overview")).context["extra"]
        for key in ("issues", "tools"):
            self.assertNotEqual(extra[key].get("source"), "last import", key)
            self.assertFalse(extra[key].get("value"), key)

    def test_a_site_filter_with_no_records_shows_no_number(self):
        self.client.force_login(self.admin)
        extra = self.client.get(reverse("k8s:overview"), {"site": MUM}).context["extra"]
        self.assertFalse(extra["issues"].get("value"))
        self.assertNotEqual(extra["issues"].get("source"), "last import")

    def test_the_owner_site_still_sees_its_issues(self):
        self.client.force_login(self.dr_viewer)
        extra = self.client.get(reverse("k8s:overview")).context["extra"]
        self.assertEqual(extra["issues"]["value"], len(test_ops.ISSUE_ROWS))


# ----------------------------------------------------------------------------- k8s-9
class SingleCsvUploadTests(ViewBase):
    def test_a_single_csv_upload_imports_under_its_own_sheet_name(self):
        self.client.force_login(self.admin)
        buffer = io.StringIO()
        import csv
        csv.writer(buffer).writerows(fixtures.NODE_INVENTORY)
        upload = io.BytesIO(buffer.getvalue().encode())
        upload.name = "03_Node_Inventory.csv"
        preview = self.client.post(reverse("k8s:import_inventory"), {"file": upload, "site": "All Sites"})
        result = preview.context["result"]
        self.assertEqual(result.unknown_sheets, [])
        self.assertEqual(result.counts["03_Node_Inventory"]["created"], 3)
        token = preview.context["pending"]["token"]
        self.client.post(reverse("k8s:import_inventory"), {"action": "confirm", "token": token})
        self.assertEqual(Node.objects.count(), 3)

    def test_open_source_uses_the_given_name_for_a_csv_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "deadbeef.csv"
            path.write_text("Hostname\nw1\n")
            source = readers.open_source(path, name="03_Node_Inventory.csv")
        self.assertEqual(list(source.sheets), ["03_Node_Inventory"])


# ----------------------------------------------------------------------------- k8s-10
@override_settings(LINKEDEYE_TENANT=T)
class SortOrderTests(ViewBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        with override_settings(LINKEDEYE_TENANT=T):
            base = [list(r) for r in test_workloads.APPLICATIONS]
            header, template = base[0], base[1]
            rows = []
            for name, health in (("w-ok", "Healthy"), ("w-warn", "Warning"), ("w-crit", "Critical"),
                                 ("w-unk", "NOT DISCOVERED")):
                row = list(template)
                row[7] = name
                row[10] = name + "-pod"
                row[20] = health
                rows.append(row)
            vols = [list(r) for r in test_workloads.VOLUMES]
            vheader, vtemplate = vols[0], vols[1]
            vrows = []
            for name, risk in (("pvc-low", "Low"), ("pvc-med", "Medium"), ("pvc-high", "High"),
                               ("pvc-unk", "NOT DISCOVERED")):
                row = list(vtemplate)
                row[4] = name
                row[6] = "pv-" + name
                row[-2] = risk
                vrows.append(row)
            importer.run_import(write({**fixtures.SHEETS, "04_Application_Inventory": [header] + rows,
                                       "07_PV_PVC_Storage": [vheader] + vrows}))

    def test_workloads_sort_by_health_puts_the_worst_first(self):
        self.client.force_login(self.admin)
        page = self.client.get(reverse("k8s:workloads"), {"sort": "health"})
        order = [w.workload_name for w in page.context["items"]]
        self.assertEqual(order[0], "w-crit", order)
        self.assertEqual(order.index("w-warn"), 1, order)
        self.assertLess(order.index("w-warn"), order.index("w-unk"))
        self.assertLess(order.index("w-unk"), order.index("w-ok"))

    def test_volumes_sort_by_risk_puts_high_first(self):
        self.client.force_login(self.admin)
        page = self.client.get(reverse("k8s:volumes"), {"sort": "risk"})
        order = [v.pvc for v in page.context["items"]]
        self.assertEqual(order[0], "pvc-high", order)
        self.assertLess(order.index("pvc-med"), order.index("pvc-low"))

    def test_descending_numeric_sorts_put_empty_values_last(self):
        """SQLite hides the difference, so check the ordering expressions: PostgreSQL sorts NULLs first on DESC."""
        from k8s import views, views_workload

        def expressions(order):
            return [order] if not isinstance(order, (list, tuple)) else list(order)

        wanted = [(views.NODE_SORTS, ("age", "cpu", "memory")), (views_workload.VOLUME_SORTS, ("capacity", "age")),
                  (views_workload.SERVICE_SORTS, ("nodeport",))]
        for mapping, keys in wanted:
            for key in keys:
                first = expressions(mapping[key])[0]
                self.assertFalse(isinstance(first, str), f"{key} is a plain string, so NULLs sort first")
                self.assertTrue(first.descending and first.nulls_last, key)

    def test_the_sort_keys_render(self):
        self.client.force_login(self.admin)
        for name, keys in (("k8s:nodes", ("hostname", "role", "age", "cpu", "memory")),
                           ("k8s:workloads", ("name", "namespace", "health", "replicas", "image", "node")),
                           ("k8s:services", ("name", "namespace", "type", "nodeport")),
                           ("k8s:volumes", ("name", "namespace", "capacity", "risk", "age"))):
            for key in keys:
                with self.subTest(page=name, sort=key):
                    self.assertEqual(self.client.get(reverse(name), {"sort": key}).status_code, 200)


# ----------------------------------------------------------------------------- AZ-05
class ImportRunScopeTests(ViewBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.run_mum = ImportRun.objects.create(tenant_key=T, source_name="mum.xlsx", site=MUM,
                                               status=ImportStatus.OK)
        cls.run_dr = ImportRun.objects.create(tenant_key=T, source_name="dr.xlsx", site=DR,
                                              status=ImportStatus.OK)
        cls.run_all = ImportRun.objects.create(tenant_key=T, source_name="all.xlsx", site="All Sites",
                                               status=ImportStatus.OK)

    def test_a_site_viewer_sees_only_own_and_global_runs(self):
        self.client.force_login(self.mum_viewer)
        page = self.client.get(reverse("k8s:imports"))
        names = {r.source_name for r in page.context["items"]}
        self.assertEqual(names, {"mum.xlsx", "all.xlsx"})
        self.assertEqual(self.client.get(reverse("k8s:import_detail", args=[self.run_dr.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("k8s:import_detail", args=[self.run_mum.pk])).status_code, 200)

    def test_an_all_site_user_sees_every_run(self):
        self.client.force_login(self.admin)
        page = self.client.get(reverse("k8s:imports"))
        self.assertEqual(len(page.context["items"]), 3)

    def test_last_run_without_a_user_still_works(self):
        self.assertIsNotNone(services.last_run())
