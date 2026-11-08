"""Security and data-safety checks for the inventory import: hostile files, hostile cells, secret-looking text,
upload sniffing, tenant-scoped choices and the redaction of run details for site-limited users.

Everything here is synthetic (Acme / Globex, example.com, RFC 5737 addresses).
"""
import io
import tempfile
import zipfile
from pathlib import Path
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from core.models import Site
from k8s import importer, parsing, readers
from k8s.forms import UploadForm, site_choices
from k8s.models import ImportRun
from k8s.models_ingress import SecretRef

from . import fixtures
from .test_views import Base, T, seed

SECRET_SHEET = "08_Credentials_References"
SECRET_ROWS = [
    ["Client", "Environment", "Application", "Authentication", "Secret Name", "Secret Namespace", "Remarks"],
    ["Acme", "PROD", "web", "Kubernetes Secret reference", "web-credentials", "acme-prod",
     "Referenced by Deployment web; password=Hunter2-example"],
    ["Acme", "PROD", "web", "Kubernetes Secret reference", "password=Hunter2-example", "acme-prod", ""],
]


class ParsingHardeningTests(SimpleTestCase):
    def test_huge_or_non_finite_numbers_are_dropped_not_stored(self):
        for text in ("9" * 400, "99999999999Ei", "nan", "inf", "1e400", "-5"):
            with self.subTest(text=text[:12]):
                self.assertIsNone(parsing.parse_quantity(text))
                self.assertIsNone(parsing.parse_cpu(text))
                self.assertIsNone(parsing.parse_int(text, minimum=0))
        self.assertIsNone(parsing.parse_pods("99999999999999Ei"))
        self.assertIsNone(parsing.parse_age("99999999999999999999y"))

    def test_values_that_fit_still_parse(self):
        self.assertEqual(parsing.parse_quantity("10Gi"), 10 * 1024 ** 3)
        self.assertEqual(parsing.parse_cpu("500m"), 500)
        self.assertEqual(parsing.parse_cpu("16"), 16000)
        self.assertEqual(parsing.parse_pods("110"), 110)
        self.assertEqual(parsing.parse_age("134d"), 134)
        self.assertEqual(parsing.parse_int("42"), 42)

    def test_numbers_never_exceed_the_database_column_sizes(self):
        self.assertIsNone(parsing.parse_quantity("9000000Ei"))
        self.assertEqual(parsing.parse_quantity("7Ei"), 7 * 1024 ** 6)
        self.assertIsNone(parsing.parse_int("3000000000"))
        self.assertEqual(parsing.parse_int("2147483647"), 2147483647)

    def test_control_characters_are_removed(self):
        self.assertEqual(parsing.clean("ab\x00c\x07d"), "abcd")
        self.assertEqual(parsing.clean("line1\nline2\tx"), "line1\nline2\tx")

    def test_placeholders_still_normalise_to_empty(self):
        for text in ("NOT DISCOVERED", "NOT APPLICABLE", "MANUAL INPUT REQUIRED", "MULTIPLE / SEE NODE INVENTORY"):
            self.assertEqual(parsing.clean(text), "")

    def test_credential_looking_text_is_masked_but_names_are_kept(self):
        masked = {
            "password=Hunter2-example": "password=[redacted]",
            "Token: abcdef123456": "Token=[redacted]",
            "run --token abc123def456": "run token=[redacted]",
            "Authorization: Bearer abcdefghijklmnop1234": "Authorization: Bearer [redacted]",
        }
        for raw, expected in masked.items():
            with self.subTest(raw=raw):
                self.assertEqual(parsing.clean(raw), expected)
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhY21lIn0.c2lnbmF0dXJl"
        self.assertEqual(parsing.clean(jwt), "[redacted]")
        pem = "-----BEGIN PRIVATE KEY-----\nMIIexample\n-----END PRIVATE KEY-----"
        self.assertEqual(parsing.clean(pem), "[redacted]")
        for kept in ("web-credentials", "docker-registry-token-abcde", "tokenizer=1",
                     "Referenced by Deployment web; secret value not inspected", "TLS secret: web-tls",
                     "may require authorization: authorization failed: no basic auth credentials",
                     "basic authentication failed for the registry"):
            with self.subTest(kept=kept):
                self.assertEqual(parsing.clean(kept), kept)


class ReaderHardeningTests(SimpleTestCase):
    def _zip(self, members):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in members.items():
                archive.writestr(name, data)
        buffer.seek(0)
        return buffer

    def test_a_csv_zip_that_expands_too_far_is_refused(self):
        archive = self._zip({"02_Client_Master.csv": "Client Name\n" + "Acme\n" * 2000})
        with mock.patch.object(readers, "MAX_TOTAL_BYTES", 1000):
            with self.assertRaises(readers.SourceError):
                readers.read_zip(archive, name="sheets.zip")

    def test_a_zip_with_too_many_members_is_refused(self):
        archive = self._zip({f"s{i}.csv": "a\n1\n" for i in range(5)})
        with mock.patch.object(readers, "MAX_ZIP_MEMBERS", 3):
            with self.assertRaises(readers.SourceError):
                readers.read_zip(archive, name="sheets.zip")

    def test_a_workbook_that_expands_too_far_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = fixtures.write_xlsx(Path(tmp) / "inv.xlsx")
            with mock.patch.object(readers, "MAX_TOTAL_BYTES", 1000):
                with self.assertRaises(readers.SourceError):
                    readers.read_xlsx(path)

    def test_a_workbook_with_an_xml_entity_declaration_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = fixtures.write_xlsx(Path(tmp) / "inv.xlsx")
            hostile = Path(tmp) / "hostile.xlsx"
            with zipfile.ZipFile(path) as src, zipfile.ZipFile(hostile, "w", zipfile.ZIP_DEFLATED) as dst:
                for info in src.infolist():
                    data = src.read(info.filename)
                    if info.filename == "xl/workbook.xml":
                        data = (b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;">]>'
                                + data.split(b"?>", 1)[-1])
                    dst.writestr(info.filename, data)
            with self.assertRaises(readers.SourceError) as caught:
                readers.read_xlsx(hostile)
            self.assertIn("DOCTYPE", str(caught.exception))

    def test_a_file_that_is_not_a_zip_package_is_refused_as_a_workbook(self):
        with self.assertRaises(readers.SourceError):
            readers.read_xlsx(io.BytesIO(b"this is not a workbook"), name="inv.xlsx")

    def test_a_real_workbook_still_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = fixtures.write_xlsx(Path(tmp) / "inv.xlsx")
            source = readers.read_xlsx(path)
            self.assertIn("03_Node_Inventory", source.sheets)
            with open(path, "rb") as fh:
                again = readers.read_xlsx(fh, name="inv.xlsx")
            self.assertEqual(source.sha256, again.sha256)

    def test_blank_row_floods_are_bounded(self):
        rows = iter([["a", "b"]] + [[None, None]] * 50 + [["1", "2"]])
        with mock.patch.object(readers, "MAX_SCANNED_ROWS", 10):
            sheet = readers._sheet_from_rows("x", rows)
        self.assertEqual(sheet.rows, [])


@override_settings(LINKEDEYE_TENANT=T)
class UploadSniffingTests(TestCase):
    def _form(self, name, content):
        return UploadForm({"site": "All Sites"}, {"file": SimpleUploadedFile(name, content)})

    def test_an_xlsx_that_is_not_a_zip_is_refused(self):
        form = self._form("inventory.xlsx", b"plain text pretending to be a workbook")
        self.assertFalse(form.is_valid())
        self.assertIn("file", form.errors)

    def test_a_zip_that_is_not_a_zip_is_refused(self):
        self.assertFalse(self._form("sheets.zip", b"not a zip at all").is_valid())

    def test_a_binary_csv_is_refused(self):
        self.assertFalse(self._form("sheet.csv", b"\x00\x01\x02binary").is_valid())

    def test_a_genuine_workbook_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = fixtures.write_xlsx(Path(tmp) / "inv.xlsx")
            form = self._form("inventory.xlsx", path.read_bytes())
            self.assertTrue(form.is_valid(), form.errors)

    def test_site_choices_never_list_another_tenants_sites(self):
        Site.objects.create(tenant_key=T, name="Acme HQ", code="acme-hq")
        Site.objects.create(tenant_key="someone-else", name="Globex HQ", code="globex-hq")
        names = [name for name, _label in site_choices()]
        self.assertIn("Acme HQ", names)
        self.assertNotIn("Globex HQ", names)
        form = UploadForm({"site": "Globex HQ"}, {"file": SimpleUploadedFile("a.csv", b"a,b\n1,2\n")})
        self.assertFalse(form.is_valid())
        self.assertIn("site", form.errors)


@override_settings(LINKEDEYE_TENANT=T)
class SecretNamesOnlyTests(TestCase):
    def test_a_pasted_value_is_masked_and_a_non_name_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = fixtures.write_csv_dir(tmp, extra={SECRET_SHEET: SECRET_ROWS})
            result = importer.run_import(directory)
        self.assertEqual(result.counts[SECRET_SHEET]["created"], 1)
        self.assertEqual(result.counts[SECRET_SHEET]["skipped"], 1)
        ref = SecretRef.objects.get()
        self.assertEqual(ref.secret_name, "web-credentials")
        stored = " ".join(str(getattr(ref, f.name)) for f in SecretRef._meta.concrete_fields)
        self.assertNotIn("Hunter2", stored)
        self.assertIn("[redacted]", ref.remarks)

    def test_an_nul_byte_in_a_cell_does_not_reach_the_database(self):
        rows = [row[:] for row in SECRET_ROWS[:2]]
        rows[1][3] = "Kubernetes Secret reference"
        rows[1][4] = "web-cred\x00entials"
        rows[1][6] = "a\x00b"
        with tempfile.TemporaryDirectory() as tmp:
            directory = fixtures.write_csv_dir(tmp, extra={SECRET_SHEET: rows})
            importer.run_import(directory)
        ref = SecretRef.objects.get()
        self.assertEqual(ref.secret_name, "web-credentials")
        self.assertNotIn("\x00", ref.remarks)


class RunDetailRedactionTests(Base):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        seed(site="All Sites")
        cls.import_run = ImportRun.objects.filter(tenant_key=T).first()
        ImportRun.objects.filter(pk=cls.import_run.pk).update(
            warnings=["02_Client_Master: namespace “globex-prod” is not in the namespace mapping sheet"])

    def test_a_tenant_wide_viewer_sees_the_warnings_and_the_discovery_log(self):
        self.login(self.viewer)
        response = self.client.get(reverse("k8s:import_detail", args=[self.import_run.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "globex-prod")
        self.assertContains(response, "kubectl get nodes -o wide")
        self.assertFalse(response.context["restricted"])

    def test_a_site_limited_viewer_sees_neither(self):
        self.login(self.dr_viewer)
        response = self.client.get(reverse("k8s:import_detail", args=[self.import_run.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["restricted"])
        self.assertNotContains(response, "globex-prod")
        self.assertNotContains(response, "kubectl get nodes -o wide")
        self.assertNotContains(response, "admin@example-cluster")
