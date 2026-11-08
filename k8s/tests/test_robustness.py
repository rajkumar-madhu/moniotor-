"""Robustness of the importer: quantities and ages, snapshot changes, damaged sheets, hostile cell values.

Everything here is synthetic ("Acme"/"Globex", example.com, RFC 5737). SQLite does not enforce CharField
lengths, so the length checks below compare every stored value with its field's ``max_length`` explicitly -
that is what PostgreSQL would refuse.
"""
import copy
import tempfile
from pathlib import Path

from django.apps import apps
from django.db import models as dj_models
from django.test import TestCase, override_settings

from core.models import Site

from k8s import importer, parsing
from k8s.models import ImportRun, ImportStatus, KubeCluster, Namespace, Node

from . import fixtures, test_ingress, test_ops, test_workloads

T = "test-tenant"
LONG = "é" * 3000        # non-ASCII on purpose: length is counted in characters, not bytes


def all_sheets():
    """Every sheet this module imports, as {name: rows} (header first)."""
    sheets = copy.deepcopy(fixtures.SHEETS)
    sheets.update(copy.deepcopy(test_workloads.WORKLOAD_SHEETS))
    sheets.update(copy.deepcopy(test_ingress.edge_sheets()))
    sheets.update(copy.deepcopy(test_ops.ops_sheets()))
    return sheets


def write(sheets):
    directory = Path(tempfile.mkdtemp())
    fixtures.write_csv_dir(directory, sheets={}, extra=sheets)
    return directory


class QuantityAndAgeTests(TestCase):
    def test_every_quantity_suffix(self):
        cases = {
            "1Ki": 1024, "1Mi": 1024 ** 2, "1Gi": 1024 ** 3, "1Ti": 1024 ** 4, "1Pi": 1024 ** 5,
            "1k": 1000, "1M": 1000 ** 2, "1G": 1000 ** 3, "1T": 1000 ** 4, "1P": 1000 ** 5,
            "512": 512, "0": 0, "1.5Gi": int(1.5 * 1024 ** 3), "2.5 Ti": int(2.5 * 1024 ** 4),
            "  10 Mi ": 10 * 1024 ** 2,
        }
        for text, expected in cases.items():
            self.assertEqual(parsing.parse_quantity(text), expected, text)
        self.assertEqual(parsing.parse_quantity(1024), 1024)        # a numeric cell from xlsx
        self.assertEqual(parsing.parse_quantity(2048.0), 2048)

    def test_quantities_that_would_overflow_or_make_no_sense_are_none(self):
        for bad in ("8Ei", "9" * 40 + "Ki", "-5Gi", "1e3", "NaN", "inf", "1Gib", "Gi", "1 2 3", "10Zi"):
            self.assertIsNone(parsing.parse_quantity(bad), bad)
        self.assertEqual(parsing.parse_quantity("1Ei"), 1024 ** 6)       # still fits a signed 64-bit column
        biggest = parsing.parse_quantity("8191Pi")
        self.assertTrue(biggest is None or biggest <= parsing.MAX_BIGINT)

    def test_every_age_unit(self):
        cases = {"134d": 134, "48h": 2, "23h": 0, "1440m": 1, "59m": 0, "90s": 0, "1w": 7, "1y": 365,
                 "3d4h": 3, "1d23h59m": 1, "12h30m": 0, "5": 5, "0d": 0}
        for text, expected in cases.items():
            self.assertEqual(parsing.parse_age(text), expected, text)
        for bad in ("soon", "", "-3d", "NOT DISCOVERED", None):
            self.assertIsNone(parsing.parse_age(bad), bad)

    def test_ages_and_counts_stay_inside_an_integer_column(self):
        self.assertIsNone(parsing.parse_age("9" * 15 + "y"))
        self.assertIsNone(parsing.parse_int("9" * 30))
        self.assertIsNone(parsing.parse_cpu("9" * 30))
        self.assertIsNone(parsing.parse_pods("9" * 30))
        self.assertIsNone(parsing.parse_int("1e999"))

    def test_clean_survives_awkward_cells(self):
        self.assertEqual(parsing.clean("a\x00b"), "ab")
        self.assertEqual(parsing.clean(float("inf")), "inf")
        self.assertEqual(parsing.clean(float("nan")), "nan")
        self.assertEqual(parsing.clean("  "), "")


@override_settings(LINKEDEYE_TENANT=T)
class SnapshotTests(TestCase):
    def setUp(self):
        Site.objects.create(tenant_key=T, name="Mumbai", code="mum")
        self.sheets = all_sheets()

    def run_import(self, sheets=None, **kwargs):
        return importer.run_import(write(sheets if sheets is not None else self.sheets), **kwargs)

    def state(self):
        """A comparable picture of every k8s table except the run log."""
        out = {}
        for model in apps.get_app_config("k8s").get_models():
            if model is ImportRun:
                continue
            out[model._meta.label] = sorted(
                (obj.pk, getattr(obj, "missing_since", None) is not None) for obj in model.objects.all())
        return out

    def test_a_second_identical_import_changes_nothing_on_any_sheet(self):
        first = self.run_import()
        self.assertTrue(first.ok, first.errors)
        before = self.state()
        second = self.run_import()
        self.assertTrue(second.ok, second.errors)
        for sheet, counts in second.counts.items():
            self.assertEqual((counts["created"], counts["updated"], counts["missing"]), (0, 0, 0), sheet)
        self.assertEqual(self.state(), before)

    def test_a_renamed_node_is_a_new_row_and_the_old_one_is_retired_not_deleted(self):
        self.run_import()
        count = Node.objects.count()
        renamed = copy.deepcopy(self.sheets)
        renamed["03_Node_Inventory"][2][1] = "worker-9.example.com"      # worker-1 -> worker-9
        result = self.run_import(renamed)
        self.assertTrue(result.ok, result.errors)
        counts = result.counts["03_Node_Inventory"]
        self.assertEqual((counts["created"], counts["missing"]), (1, 1))
        self.assertEqual(Node.objects.count(), count + 1)
        self.assertIsNotNone(Node.objects.get(hostname="worker-1.example.com").missing_since)
        self.assertIsNone(Node.objects.get(hostname="worker-9.example.com").missing_since)
        # the same snapshot again settles down
        again = self.run_import(renamed)
        self.assertEqual((again.total_created, again.total_updated, again.total_missing), (0, 0, 0))

    def test_removed_and_added_rows_on_every_sheet(self):
        self.run_import()
        shrunk = copy.deepcopy(self.sheets)
        removed = {}
        for name, rows in shrunk.items():
            if name in ("15_RAW_Discovery", "02_Client_Master", "13_Client_Summary", "14_Namespace_Mapping"):
                continue
            if len(rows) > 2:
                removed[name] = rows.pop()          # drop the last data row
        result = self.run_import(shrunk)
        self.assertTrue(result.ok, result.errors)
        for name in removed:
            self.assertGreaterEqual(result.counts[name]["missing"], 1, name)
        grown = copy.deepcopy(self.sheets)
        restored = self.run_import(grown)               # the rows come back
        self.assertTrue(restored.ok, restored.errors)
        for model in apps.get_app_config("k8s").get_models():
            if hasattr(model, "missing_since"):
                self.assertFalse(model.objects.filter(missing_since__isnull=False).exclude(
                    **({"status": "closed"} if hasattr(model, "incident") else {})).exists(), model.__name__)

    def test_a_failing_sheet_must_not_retire_that_sheets_existing_rows(self):
        """A sheet that blows up half way has not told us what is gone; its rows stay as they were."""
        self.run_import()
        nodes = Node.objects.count()
        original = importer._SHEETS["03_Node_Inventory"]

        def explode(ctx, sheet):
            ctx.track(Node, "03_Node_Inventory")
            raise RuntimeError("synthetic failure")

        importer.register_sheet("03_Node_Inventory", original[0], explode)
        self.addCleanup(lambda: importer.register_sheet("03_Node_Inventory", original[0], original[1]))
        result = self.run_import()
        self.assertFalse(result.ok)
        self.assertEqual(Node.objects.count(), nodes)
        self.assertEqual(Node.objects.filter(missing_since__isnull=False).count(), 0)

    def test_a_failing_sheet_does_not_leave_a_stale_cluster_cache_for_later_sheets(self):
        """The cluster row is created inside the failing sheet's transaction and rolled back with it."""
        original = importer._SHEETS["03_Node_Inventory"]

        def explode(ctx, sheet):
            ctx.cluster_for("kubernetes")
            raise RuntimeError("synthetic failure")

        importer.register_sheet("03_Node_Inventory", original[0], explode)
        self.addCleanup(lambda: importer.register_sheet("03_Node_Inventory", original[0], original[1]))
        result = self.run_import()
        self.assertFalse(result.ok)
        # the later sheets that need a cluster must still have written their rows against a real one
        self.assertEqual(KubeCluster.objects.count(), 1)
        self.assertEqual(Namespace.objects.filter(cluster__isnull=True).count(), 0)
        self.assertGreater(Namespace.objects.count(), 0)


@override_settings(LINKEDEYE_TENANT=T)
class DamagedSheetTests(TestCase):
    def setUp(self):
        self.sheets = all_sheets()

    def run_import(self, sheets):
        return importer.run_import(write(sheets))

    def test_a_sheet_missing_from_the_file_is_a_warning_and_leaves_its_records(self):
        self.run_import(self.sheets)
        nodes = Node.objects.count()
        partial = {k: v for k, v in self.sheets.items() if k != "03_Node_Inventory"}
        result = self.run_import(partial)
        self.assertTrue(result.ok, result.errors)
        self.assertIn("03_Node_Inventory", result.missing_sheets)
        self.assertEqual(Node.objects.count(), nodes)
        self.assertEqual(Node.objects.filter(missing_since__isnull=False).count(), 0)

    def test_dropping_any_one_column_never_raises(self):
        for name, rows in self.sheets.items():
            for col in range(len(rows[0])):
                damaged = copy.deepcopy(self.sheets)
                damaged[name] = [r[:col] + r[col + 1:] for r in rows]
                result = self.run_import(damaged)
                self.assertIn(result.status, (ImportStatus.OK, ImportStatus.FAILED), (name, col))
                for error in result.errors:
                    self.assertNotIn("Traceback", error)
                    # a column that is simply absent is reported as a skipped row or a warning, not a crash
                    self.assertNotRegex(error, r"(KeyError|AttributeError|TypeError|IndexError)", (name, col, error))

    def test_a_sheet_whose_rows_cannot_be_read_retires_nothing(self):
        """Headers renamed beyond recognition: every row is skipped, which must not look like 'all gone'."""
        self.run_import(self.sheets)
        models = [m for m in apps.get_app_config("k8s").get_models() if hasattr(m, "missing_since")]
        for name, rows in self.sheets.items():
            damaged = copy.deepcopy(self.sheets)
            damaged[name] = [[f"Unknown {i}" for i in range(len(rows[0]))]] + rows[1:]
            result = self.run_import(damaged)
            self.assertIn(result.status, (ImportStatus.OK, ImportStatus.FAILED), name)
            for model in models:
                self.assertEqual(model.objects.filter(missing_since__isnull=False).count(), 0,
                                 f"{name} -> {model.__name__}")
        from k8s.models_ops import InventoryIssue, IssueStatus
        self.assertFalse(InventoryIssue.objects.filter(status=IssueStatus.CLOSED).exists())

    def test_a_summary_only_group_is_retired_by_the_summary_sheet_not_by_the_client_master(self):
        self.run_import(self.sheets)
        from k8s.models import ClientProfile
        summary_only = ClientProfile.objects.get(summary_only=True)
        # the client master alone (no summary sheet in the source) leaves the roll-up group alone
        only_master = {"02_Client_Master": self.sheets["02_Client_Master"]}
        self.run_import(only_master)
        summary_only.refresh_from_db()
        self.assertIsNone(summary_only.missing_since)
        # the summary sheet no longer lists the group: now it goes
        shrunk = copy.deepcopy(self.sheets["13_Client_Summary"])
        shrunk = [r for r in shrunk if r[0] != summary_only.workbook_name]
        result = self.run_import({"13_Client_Summary": shrunk})
        summary_only.refresh_from_db()
        self.assertIsNotNone(summary_only.missing_since)
        self.assertEqual(result.counts["13_Client_Summary"]["missing"], 1)

    def test_a_row_of_blank_cells_is_skipped_everywhere(self):
        blanked = copy.deepcopy(self.sheets)
        for name, rows in blanked.items():
            rows.append([""] * len(rows[0]))
            rows.append([" "] * len(rows[0]))
        clean = self.run_import(self.sheets)
        self.assertTrue(clean.ok, clean.errors)
        models = [m for m in apps.get_app_config("k8s").get_models() if m is not ImportRun]
        baseline = {m._meta.label: m.objects.count() for m in models}
        for model in models:
            model.objects.all().delete()
        result = self.run_import(blanked)
        self.assertTrue(result.ok, result.errors)
        for model in models:
            self.assertEqual(model.objects.count(), baseline[model._meta.label], model._meta.label)

    def test_a_header_only_sheet_and_an_empty_sheet(self):
        for name in self.sheets:
            damaged = copy.deepcopy(self.sheets)
            damaged[name] = damaged[name][:1]
            result = self.run_import(damaged)
            self.assertIn(result.status, (ImportStatus.OK, ImportStatus.FAILED), name)
            self.assertFalse([e for e in result.errors if "Error" in e], (name, result.errors))

    def test_values_longer_than_the_field_are_truncated_not_raised(self):
        """One import where, for every data row of every sheet, each column in turn holds a huge value."""
        hostile = copy.deepcopy(self.sheets)
        for name, rows in hostile.items():
            head, body = rows[0], rows[1:]
            extra = []
            for row in body:
                for col in range(len(head)):
                    mutated = list(row)
                    mutated[col] = LONG
                    extra.append(mutated)
            hostile[name] = [head] + body + extra
        result = self.run_import(hostile)
        self.assertFalse([e for e in result.errors if "Error" in e and "Truncat" not in e],
                         result.errors[:5])
        self.assertTrue(result.ok, result.errors[:5])
        too_long = []
        for model in apps.get_app_config("k8s").get_models():
            for field in model._meta.get_fields():
                if not isinstance(field, dj_models.CharField) or not getattr(field, "max_length", None):
                    continue
                for value in model.objects.values_list(field.name, flat=True).iterator():
                    if value and len(value) > field.max_length:
                        too_long.append(f"{model.__name__}.{field.name}")
                        break
        self.assertEqual(too_long, [], "stored values longer than their column")

    def test_unicode_survives_a_round_trip(self):
        sheets = copy.deepcopy(self.sheets)
        sheets["14_Namespace_Mapping"][1][4] = "Évidence – 数据 – 🚀"
        self.run_import(sheets)
        self.assertEqual(Namespace.objects.get(name="acme-prod").evidence, "Évidence – 数据 – 🚀")

    def test_incompressible_full_width_keys_are_reported_or_stored_never_raised(self):
        """Index keys built from the widest columns of 4-byte characters: PostgreSQL may refuse a btree entry
        that large; the importer must then report it on the sheet, not crash the run."""
        import random
        rng = random.Random(7)

        def blob(n):
            return "".join(chr(rng.randrange(0x1F300, 0x1F5FF)) for _ in range(n))

        hostile = copy.deepcopy(self.sheets)
        head = hostile["06_Ingress_URLs"][0]
        row = list(hostile["06_Ingress_URLs"][1])
        for column, width in (("Ingress", 253), ("Hostname", 253), ("Path", 500), ("Backend Service", 253)):
            if column in head:
                row[head.index(column)] = blob(width)
        hostile["06_Ingress_URLs"].append(row)
        result = self.run_import(hostile)
        self.assertIn(result.status, (ImportStatus.OK, ImportStatus.FAILED))
        self.assertTrue(Node.objects.exists())          # the other sheets were not lost
