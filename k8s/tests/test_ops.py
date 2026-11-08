"""Tools, monitoring coverage and the issue register: parsing, idempotent import, opt-in cross-links, the three
pages, CSV, the two actions, site scoping, RBAC and tenant isolation.

Every fixture is synthetic: the clients are "Acme" and "Globex", host names are under example.com. No row of the
real workbook is used.
"""
import csv
import io
import tempfile
from pathlib import Path

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from administration.services import SITE_NAME_FIELDS
from core.models import AuditEvent, Membership, Site
from observe.models import Exporter
from servicedesk.models import Ticket

from k8s import importer
from k8s.models import ClientProfile, ImportRun, KubeCluster, Namespace
from k8s.models_ops import InventoryIssue, IssueStatus, Severity, ToolCategory, ToolInstance, capability_of
from k8s.sheets.ops import link_exporters, link_incidents, normalise_tool, ops_options
from k8s.views_ops import filter_issues, filter_tools, monitoring_coverage

from . import fixtures

T = "test-tenant"
OTHER = "other-tenant"
MUM = "Mumbai"
DR = "DR Site"

MON_HEAD = ["Client", "Environment", "Monitoring Component", "Namespace", "Service", "URL", "Port", "Storage",
            "Status", "Replica Count", "Notes"]
MON_ROWS = [
    ["Acme", "PROD", "Prometheus", "acme-prod", "prom-svc", "https://prom.example.com", "9090", "prom-data",
     "Healthy", "1", "Scope not safely inferable"],
    ["Acme", "PROD", "Grafana", "acme-prod", "grafana", "https://grafana.example.com", "3000", "NOT DISCOVERED",
     "Healthy", "1", "NOT DISCOVERED"],
    ["Acme", "PROD", "Grafana", "acme-prod", "grafana-proxy", "https://grafana.example.com", "4180",
     "NOT DISCOVERED", "Healthy", "1", "NOT DISCOVERED"],
    ["Acme", "PROD", "Idrac", "acme-prod", "metrics-agent-svc", "NOT DISCOVERED", "9348", "NOT DISCOVERED", "Warning",
     "0", "NOT DISCOVERED"],
    ["Acme", "UAT", "Alertmanager", "acme-uat", "alertmanager", "https://alerts.example.com", "9093",
     "NOT DISCOVERED", "Healthy", "1", "NOT DISCOVERED"],
    ["Acme", "UAT", "Node-Exporter", "acme-uat", "node-exp", "NOT DISCOVERED", "9100, 9101", "NOT DISCOVERED",
     "Healthy", "1", "NOT DISCOVERED"],
    ["UNMAPPED / REVIEW REQUIRED", "NOT DISCOVERED", "Loki", "sandbox", "loki", "NOT DISCOVERED", "3100",
     "NOT DISCOVERED", "Healthy", "1", "NOT DISCOVERED"],
]

TOOL_HEAD = ["Client", "Environment", "Tool", "Namespace", "Service", "Hostname", "URL", "Port", "Storage",
             "Authentication", "Username", "Credential Reference", "Status", "Owner"]
TOOL_ROWS = [
    ["Acme", "PROD", "Argocd", "acme-prod", "cd-web", "argocd.example.com", "https://argocd.example.com",
     "80,443", "NOT DISCOVERED", "NOT DISCOVERED", "svc-argo-user", "argocd-secret", "Healthy",
     "MANUAL INPUT REQUIRED"],
    ["Globex", "NOT DISCOVERED", "Vault", "globex-prod", "vault", "NOT DISCOVERED", "NOT DISCOVERED", "8200",
     "secrets-store-data", "NOT DISCOVERED", "NOT DISCOVERED", "MANUAL INPUT REQUIRED", "Healthy", "MANUAL INPUT REQUIRED"],
    ["Globex", "NOT DISCOVERED", "Ldap", "globex-prod", "ldap", "NOT DISCOVERED", "NOT DISCOVERED", "389",
     "NOT DISCOVERED", "NOT DISCOVERED", "NOT DISCOVERED", "not a name!", "Warning", "MANUAL INPUT REQUIRED"],
]

ISSUE_HEAD = ["Issue ID", "Client", "Environment", "Namespace", "Severity", "Category", "Resource", "Issue", "Impact",
              "Evidence", "Recommended Action", "Owner", "Target Date", "Status"]
ISSUE_ROWS = [
    ["ISS-0001", "Acme", "PROD", "acme-prod", "High", "Pod", "web-1", "Pod Pending", "Operational risk", "Pending",
     "Inspect events", "MANUAL INPUT REQUIRED", "MANUAL INPUT REQUIRED", "Open"],
    ["ISS-0002", "Acme", "PROD", "acme-prod", "High", "Deployment", "web", "Unavailable replicas",
     "Operational risk", "Desired=2; Ready=0", "Inspect rollout", "MANUAL INPUT REQUIRED",
     "MANUAL INPUT REQUIRED", "Open"],
    ["ISS-0003", "Globex", "NOT DISCOVERED", "globex-prod", "Medium", "Job", "nightly-1", "Job failures",
     "Operational risk", "Failed=1", "Inspect job logs", "MANUAL INPUT REQUIRED", "MANUAL INPUT REQUIRED", "Open"],
    ["ISS-0004", "UNMAPPED / REVIEW REQUIRED", "NOT DISCOVERED", "NOT APPLICABLE", "Medium", "Storage", "pv-0",
     "PV Released", "Operational risk", "Phase=Released", "Reclaim or delete", "MANUAL INPUT REQUIRED",
     "MANUAL INPUT REQUIRED", "Open"],
    ["ISS-0005", "Globex", "NOT DISCOVERED", "globex-prod", "Low", "Container", "shop/shop", "High restart count",
     "NOT DISCOVERED", "Restarts=9", "Review limits", "MANUAL INPUT REQUIRED", "MANUAL INPUT REQUIRED", "Open"],
    ["ISS-0006", "Globex", "NOT DISCOVERED", "globex-prod", "Sev?", "Pod", "shop-2", "Pod Failed",
     "Operational risk", "Failed", "Inspect", "MANUAL INPUT REQUIRED", "MANUAL INPUT REQUIRED", "Open"],
]


def ops_sheets(mon=None, tools=None, issues=None):
    return {"10_Monitoring": [MON_HEAD] + (MON_ROWS if mon is None else mon),
            "11_CICD_Platform_Tools": [TOOL_HEAD] + (TOOL_ROWS if tools is None else tools),
            "12_Issues_Risks": [ISSUE_HEAD] + (ISSUE_ROWS if issues is None else issues)}


def source(**kwargs):
    directory = Path(tempfile.mkdtemp())
    fixtures.write_csv_dir(directory, extra=ops_sheets(**kwargs))
    return directory


def seed(site="All Sites", tenant=T, flags=None, **kwargs):
    directory = source(**kwargs)
    with override_settings(LINKEDEYE_TENANT=tenant):
        if flags:
            with ops_options(**flags):
                return importer.run_import(directory, site=site)
        return importer.run_import(directory, site=site)


def count(result, sheet, key):
    return result.counts.get(sheet, {}).get(key, 0)


# ============================================================================ pure functions
class HelperTests(TestCase):
    def test_tool_names_are_normalised(self):
        cases = {"Idrac": ("iDRAC exporter", "monitoring"), "Ilo": ("iLO exporter", "monitoring"),
                 "Snmp": ("SNMP exporter", "monitoring"), "Node-Exporter": ("Node exporter", "monitoring"),
                 "Blackbox": ("Blackbox exporter", "monitoring"), "Argocd": ("Argo CD", "cicd"),
                 "Sonarqube": ("SonarQube", "cicd"), "Harbor": ("Harbor", "cicd"), "Ldap": ("LDAP", "platform"),
                 "Vault": ("Vault", "platform"), "Minio": ("MinIO", "platform"), "pgadmin": ("pgAdmin", "platform"),
                 "PROMETHEUS": ("Prometheus", "monitoring")}
        for raw, expected in cases.items():
            with self.subTest(raw):
                self.assertEqual(normalise_tool(raw), expected)

    def test_unknown_tool_keeps_its_name_and_the_sheet_category(self):
        self.assertEqual(normalise_tool("widget", "monitoring"), ("Widget", "monitoring"))
        self.assertEqual(normalise_tool("Widget"), ("Widget", "cicd"))
        self.assertEqual(normalise_tool("NOT DISCOVERED"), ("", "cicd"))

    def test_capabilities(self):
        self.assertEqual(capability_of("Prometheus"), "metrics")
        self.assertEqual(capability_of("Kibana"), "logs")
        self.assertEqual(capability_of("iLO exporter"), "hardware")
        self.assertEqual(capability_of("Argo CD"), "")


# ============================================================================ models
@override_settings(LINKEDEYE_TENANT=T)
class ModelTests(TestCase):
    def test_no_field_can_hold_a_secret_value(self):
        forbidden = ("password", "passwd", "token", "secret_data", "base64", "private", "username", "user_name",
                     "api_key", "apikey")
        for model in (ToolInstance, InventoryIssue):
            for field in model._meta.get_fields():
                for word in forbidden:
                    self.assertNotIn(word, field.name, f"{model.__name__}.{field.name} looks like a secret value")
        self.assertIn("never read", ToolInstance._meta.get_field("credential_reference").help_text)

    def test_sites_are_registered_for_rename(self):
        registered = {(a, m, f) for a, m, f, _label in SITE_NAME_FIELDS}
        self.assertIn(("k8s", "ToolInstance", "site"), registered)
        self.assertIn(("k8s", "InventoryIssue", "site"), registered)

    def test_status_tone(self):
        ok = ToolInstance(status="Healthy", replica_count=1)
        self.assertEqual((ok.is_unhealthy, ok.status_tone), (False, "ok"))
        self.assertEqual(ToolInstance(status="Warning", replica_count=1).status_tone, "warn")
        self.assertEqual(ToolInstance(status="Healthy", replica_count=0).status_tone, "crit")
        self.assertEqual(ToolInstance(status="").status_tone, "mute")

    def test_port_text(self):
        tool = ToolInstance(ports=[{"port": 80, "name": "", "protocol": ""}, {"port": None, "name": "http"}])
        self.assertEqual(tool.port_text, "80, http")


# ============================================================================ import
@override_settings(LINKEDEYE_TENANT=T)
class ImportTests(TestCase):
    def test_import_counts_and_normalisation(self):
        result = seed(site=MUM)
        self.assertEqual(result.status, "ok", result.errors)
        self.assertEqual(count(result, "10_Monitoring", "rows"), len(MON_ROWS))
        self.assertEqual(count(result, "11_CICD_Platform_Tools", "rows"), len(TOOL_ROWS))
        self.assertEqual(count(result, "12_Issues_Risks", "rows"), len(ISSUE_ROWS))
        self.assertEqual(ToolInstance.objects.filter(category="monitoring").count(), len(MON_ROWS))
        self.assertEqual(ToolInstance.objects.filter(category="cicd").count(), 1)
        self.assertEqual(ToolInstance.objects.filter(category="platform").count(), 2)
        self.assertEqual(InventoryIssue.objects.count(), len(ISSUE_ROWS))

        idrac = ToolInstance.objects.get(tool="iDRAC exporter")
        self.assertEqual(idrac.tool_raw, "Idrac")
        self.assertEqual(idrac.url, "")                     # "NOT DISCOVERED" is no value
        self.assertEqual(idrac.storage, "")
        self.assertEqual(idrac.environment, "PROD")
        self.assertEqual(idrac.replica_count, 0)
        self.assertEqual(idrac.status, "Warning")
        self.assertEqual(idrac.site, MUM)
        self.assertEqual(idrac.ports, [{"port": 9348, "name": "", "protocol": ""}])
        self.assertEqual(idrac.notes, "")
        argo = ToolInstance.objects.get(tool="Argo CD")
        self.assertEqual(argo.credential_reference, "argocd-secret")
        self.assertEqual(argo.hostname, "argocd.example.com")
        self.assertEqual(argo.owner, "")
        self.assertEqual(argo.authentication, "")
        self.assertEqual([p["port"] for p in argo.ports], [80, 443])
        self.assertEqual(ToolInstance.objects.get(tool="Node exporter").port_text, "9100, 9101")
        # a hostname is derived from the URL when the sheet has none
        self.assertEqual(ToolInstance.objects.get(tool="Prometheus").hostname, "prom.example.com")
        for tool in ToolInstance.objects.all():                 # no placeholder is ever stored
            for value in (tool.url, tool.storage, tool.owner, tool.authentication, tool.credential_reference,
                          tool.hostname, tool.notes, tool.environment):
                self.assertNotIn("NOT DISCOVERED", value)
                self.assertNotIn("MANUAL INPUT", value)

    def test_the_username_column_is_never_stored(self):
        seed()
        blob = " ".join(str(v) for t in ToolInstance.objects.values() for v in t.values())
        self.assertNotIn("svc-argo-user", blob)

    def test_a_credential_reference_that_is_not_a_name_is_dropped_with_a_warning(self):
        result = seed()
        self.assertEqual(ToolInstance.objects.get(tool="LDAP").credential_reference, "")
        self.assertTrue(any("credential reference" in w for w in result.warnings))

    def test_issue_fields(self):
        seed(site=MUM)
        issue = InventoryIssue.objects.get(issue_id="ISS-0001")
        self.assertEqual((issue.severity, issue.status, issue.category), ("high", "open", "Pod"))
        self.assertEqual(issue.namespace.name, "acme-prod")
        self.assertEqual(issue.client_label, "Acme")
        self.assertEqual(issue.owner, "")
        self.assertEqual(issue.site, MUM)
        self.assertIsNotNone(issue.first_seen)
        self.assertIsNone(issue.incident)
        cluster_level = InventoryIssue.objects.get(issue_id="ISS-0004")
        self.assertIsNone(cluster_level.namespace)            # "NOT APPLICABLE" means no namespace
        self.assertEqual(cluster_level.client_label, "")      # an unmapped marker is no client
        self.assertEqual(cluster_level.client_display, "")
        self.assertEqual(cluster_level.site, KubeCluster.objects.get().site)

    def test_unknown_severity_is_filed_as_medium_with_a_warning(self):
        result = seed()
        self.assertEqual(InventoryIssue.objects.get(issue_id="ISS-0006").severity, "medium")
        self.assertTrue(any("severity" in w for w in result.warnings))

    def test_reimport_is_idempotent(self):
        seed()
        pks = set(ToolInstance.objects.values_list("pk", flat=True)) | set(
            InventoryIssue.objects.values_list("pk", flat=True))
        again = seed()
        for sheet in ("10_Monitoring", "11_CICD_Platform_Tools", "12_Issues_Risks"):
            self.assertEqual(count(again, sheet, "created"), 0, sheet)
            self.assertEqual(count(again, sheet, "updated"), 0, sheet)
            self.assertEqual(count(again, sheet, "missing"), 0, sheet)
        self.assertEqual(ToolInstance.objects.count(), len(MON_ROWS) + len(TOOL_ROWS))
        self.assertEqual(InventoryIssue.objects.count(), len(ISSUE_ROWS))
        self.assertEqual(pks, set(ToolInstance.objects.values_list("pk", flat=True)) | set(
            InventoryIssue.objects.values_list("pk", flat=True)))

    def test_a_changed_cell_updates_in_place(self):
        seed()
        rows = [list(r) for r in MON_ROWS]
        rows[0][8] = "Warning"
        again = seed(mon=rows)
        self.assertEqual(count(again, "10_Monitoring", "updated"), 1)
        self.assertEqual(count(again, "10_Monitoring", "created"), 0)
        self.assertEqual(ToolInstance.objects.get(tool="Prometheus").status, "Warning")

    def test_removed_rows_are_marked_missing_and_come_back(self):
        seed()
        again = seed(mon=MON_ROWS[1:], issues=ISSUE_ROWS[1:])
        self.assertEqual(count(again, "10_Monitoring", "missing"), 1)
        self.assertEqual(count(again, "12_Issues_Risks", "missing"), 1)
        self.assertIsNotNone(ToolInstance.objects.get(tool="Prometheus").missing_since)
        gone = InventoryIssue.objects.get(issue_id="ISS-0001")
        self.assertIsNotNone(gone.missing_since)
        self.assertEqual(gone.status, IssueStatus.CLOSED)      # cleared
        self.assertIsNotNone(gone.closed_at)
        back = seed()
        self.assertIsNone(ToolInstance.objects.get(tool="Prometheus").missing_since)
        reopened = InventoryIssue.objects.get(issue_id="ISS-0001")
        self.assertIsNone(reopened.missing_since)
        self.assertEqual(reopened.status, IssueStatus.OPEN)
        self.assertIsNone(reopened.closed_at)
        self.assertEqual(ToolInstance.objects.count(), len(MON_ROWS) + len(TOOL_ROWS))

    def test_a_partial_import_does_not_touch_other_sheets(self):
        seed()
        directory = Path(tempfile.mkdtemp())
        fixtures.write_csv_dir(directory, sheets={}, extra={"10_Monitoring": [MON_HEAD] + MON_ROWS[:2]})
        with override_settings(LINKEDEYE_TENANT=T):
            importer.run_import(directory)
        self.assertEqual(ToolInstance.objects.filter(category__in=["cicd", "platform"],
                                                     missing_since__isnull=True).count(), len(TOOL_ROWS))
        self.assertEqual(InventoryIssue.objects.filter(missing_since__isnull=True).count(), len(ISSUE_ROWS))
        self.assertEqual(ToolInstance.objects.filter(category="monitoring", missing_since__isnull=False).count(),
                         len(MON_ROWS) - 2)

    def test_dry_run_writes_nothing(self):
        directory = source()
        with override_settings(LINKEDEYE_TENANT=T):
            result = importer.run_import(directory, dry_run=True)
        self.assertEqual(count(result, "12_Issues_Risks", "created"), len(ISSUE_ROWS))
        self.assertEqual(ToolInstance.objects.count(), 0)
        self.assertEqual(InventoryIssue.objects.count(), 0)
        self.assertEqual(ImportRun.objects.count(), 0)

    def test_rows_without_a_tool_namespace_or_id_are_skipped(self):
        bad_mon = [["Acme", "PROD", "NOT DISCOVERED", "acme-prod", "x", "", "", "", "", "", ""],
                   ["Acme", "PROD", "Grafana", "NOT DISCOVERED", "x", "", "", "", "", "", ""]]
        bad_issue = [["", "Acme", "", "acme-prod", "High", "Pod", "r", "i", "", "", "", "", "", "Open"]]
        result = seed(mon=bad_mon, tools=[], issues=bad_issue)
        self.assertEqual(count(result, "10_Monitoring", "skipped"), 2)
        self.assertEqual(count(result, "12_Issues_Risks", "skipped"), 1)
        self.assertEqual(ToolInstance.objects.count(), 0)

    def test_a_closed_status_is_kept(self):
        rows = [list(ISSUE_ROWS[0])]
        rows[0][-1] = "Closed"
        seed(issues=rows)
        issue = InventoryIssue.objects.get()
        self.assertEqual(issue.status, IssueStatus.CLOSED)
        self.assertIsNotNone(issue.closed_at)

    def test_xlsx_round_trip(self):
        import openpyxl
        book = openpyxl.Workbook()
        book.remove(book.active)
        for name, rows in {**fixtures.SHEETS, **ops_sheets()}.items():
            sheet = book.create_sheet(title=name)
            for row in rows:
                sheet.append(row)
        path = Path(tempfile.mkdtemp()) / "inv.xlsx"
        book.save(str(path))
        with override_settings(LINKEDEYE_TENANT=T):
            result = importer.run_import(path)
        self.assertEqual(result.status, "ok", result.errors)
        self.assertEqual(ToolInstance.objects.count(), len(MON_ROWS) + len(TOOL_ROWS))
        self.assertEqual(InventoryIssue.objects.count(), len(ISSUE_ROWS))


# ============================================================================ cross-links
@override_settings(LINKEDEYE_TENANT=T)
class CrossLinkTests(TestCase):
    def test_off_by_default(self):
        seed(site=MUM)
        self.assertEqual(Exporter.objects.count(), 0)
        self.assertEqual(Ticket.objects.count(), 0)
        self.assertFalse(ToolInstance.objects.filter(exporter__isnull=False).exists())
        self.assertFalse(InventoryIssue.objects.filter(incident__isnull=False).exists())

    def test_exporters_are_created_disabled_on_the_tools_site(self):
        result = seed(site=MUM, flags={"exporters": True})
        self.assertEqual(count(result, "10_Monitoring", "exporters_created"), 3)    # prom, grafana (once), alerts
        self.assertEqual(Exporter.objects.count(), 3)
        for exporter in Exporter.objects.all():
            self.assertFalse(exporter.enabled)
            self.assertEqual(exporter.metrics_url, "")           # nothing to scrape yet
            self.assertEqual(exporter.site, MUM)
            self.assertEqual(exporter.category, "Kubernetes")
            self.assertEqual(exporter.tenant_key, T)
            self.assertEqual(exporter.status, "inactive")
        grafana = ToolInstance.objects.filter(tool="Grafana")
        self.assertEqual({t.exporter_id for t in grafana}.__len__(), 1)          # same URL → one exporter
        self.assertFalse(ToolInstance.objects.filter(tool="iDRAC exporter", exporter__isnull=False).exists())
        self.assertFalse(ToolInstance.objects.filter(category="cicd", exporter__isnull=False).exists())

    def test_exporter_cross_link_is_idempotent(self):
        seed(flags={"exporters": True})
        again = seed(flags={"exporters": True})
        self.assertEqual(Exporter.objects.count(), 3)
        self.assertEqual(count(again, "10_Monitoring", "exporters_created"), 0)

    def test_an_existing_exporter_is_linked_not_duplicated(self):
        existing = Exporter.objects.create(tenant_key=T, name="Our Prometheus", exporter_type="Prometheus",
                                           metrics_url="https://prom.example.com/metrics", site=MUM)
        seed(site=MUM, flags={"exporters": True})
        self.assertEqual(ToolInstance.objects.get(tool="Prometheus").exporter_id, existing.pk)
        self.assertEqual(Exporter.objects.count(), 3)            # the existing one + grafana + alertmanager
        existing.refresh_from_db()
        self.assertEqual(existing.metrics_url, "https://prom.example.com/metrics")

    def test_exporter_names_do_not_collide(self):
        Exporter.objects.create(tenant_key=T, name="Prometheus (prom.example.com)", exporter_type="x")
        seed(flags={"exporters": True})
        names = list(Exporter.objects.values_list("name", flat=True))
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(Exporter.objects.count(), 4)

    def test_global_site_exporter_has_a_blank_site(self):
        seed(flags={"exporters": True})
        self.assertEqual(set(Exporter.objects.values_list("site", flat=True)), {""})

    def test_incidents_open_for_high_issues_only(self):
        result = seed(site=MUM, flags={"incidents": True})
        self.assertEqual(count(result, "12_Issues_Risks", "incidents_opened"), 2)
        self.assertEqual(Ticket.objects.filter(kind="incident").count(), 2)
        issue = InventoryIssue.objects.get(issue_id="ISS-0001")
        self.assertIsNotNone(issue.incident)
        self.assertEqual(issue.incident.fingerprint, "k8sinv:ISS-0001")
        self.assertEqual(issue.incident.priority, "P2")
        self.assertEqual(issue.incident.site, MUM)
        self.assertEqual(issue.incident.client, "Acme")
        self.assertIn("web-1", issue.incident.title)
        self.assertIn("Inspect events", issue.incident.description)
        self.assertFalse(InventoryIssue.objects.filter(severity__in=["medium", "low"],
                                                       incident__isnull=False).exists())

    def test_reimport_updates_the_incident_instead_of_duplicating(self):
        seed(flags={"incidents": True})
        first = InventoryIssue.objects.get(issue_id="ISS-0001").incident_id
        again = seed(flags={"incidents": True})
        self.assertEqual(Ticket.objects.filter(kind="incident").count(), 2)
        self.assertEqual(InventoryIssue.objects.get(issue_id="ISS-0001").incident_id, first)
        self.assertEqual(count(again, "12_Issues_Risks", "incidents_updated"), 2)
        self.assertEqual(count(again, "12_Issues_Risks", "incidents_opened"), 0)

    def test_a_dry_run_never_opens_an_incident_or_exporter(self):
        directory = source()
        with override_settings(LINKEDEYE_TENANT=T), ops_options(exporters=True, incidents=True):
            result = importer.run_import(directory, dry_run=True)
        self.assertEqual(count(result, "12_Issues_Risks", "incidents_would_open"), 2)
        self.assertEqual(Ticket.objects.count(), 0)
        self.assertEqual(Exporter.objects.count(), 0)

    def test_a_closed_or_medium_issue_is_skipped(self):
        seed()
        issues = list(InventoryIssue.objects.filter(issue_id__in=["ISS-0003", "ISS-0001"]))
        InventoryIssue.objects.filter(issue_id="ISS-0001").update(status="closed")
        issues = list(InventoryIssue.objects.filter(issue_id__in=["ISS-0003", "ISS-0001"]))
        result = link_incidents(T, issues)
        self.assertEqual(result, {"opened": 0, "updated": 0, "skipped": 2, "forbidden": 0})
        self.assertEqual(Ticket.objects.count(), 0)

    def test_link_exporters_helper_respects_allowed(self):
        seed(site=MUM)
        tools = ToolInstance.objects.filter(category="monitoring").select_related("namespace")
        result = link_exporters(T, tools, allowed=lambda site: False)
        self.assertEqual(result["created"] + result["linked"], 0)
        self.assertEqual(Exporter.objects.count(), 0)
        self.assertEqual(result["forbidden"], 4)          # the four monitoring tools that have a URL


# ============================================================================ pages
class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        with override_settings(LINKEDEYE_TENANT=T):
            cls.mum = Site.objects.create(tenant_key=T, name=MUM, code="mum")
            cls.dr = Site.objects.create(tenant_key=T, name=DR, code="dr")
            cls.viewer = User.objects.create_user("viewer", password="pw-viewer-12345")
            Membership.objects.create(user=cls.viewer, site=None, role="viewer")
            cls.operator = User.objects.create_user("operator", password="pw-operator-12345")
            Membership.objects.create(user=cls.operator, site=None, role="operator")
            cls.admin = User.objects.create_user("admin", password="pw-admin-12345")
            Membership.objects.create(user=cls.admin, site=None, role="admin")
            cls.dr_viewer = User.objects.create_user("dr-viewer", password="pw-drviewer-12345")
            Membership.objects.create(user=cls.dr_viewer, site=cls.dr, role="viewer")
            cls.dr_operator = User.objects.create_user("dr-operator", password="pw-droperator-12345")
            Membership.objects.create(user=cls.dr_operator, site=cls.dr, role="operator")
            cls.dr_admin = User.objects.create_user("dr-admin", password="pw-dradmin-12345")
            Membership.objects.create(user=cls.dr_admin, site=cls.dr, role="admin")
            seed(site=MUM)
            cls.add_dr_rows()

    @classmethod
    def add_dr_rows(cls):
        """A second cluster on the DR site, so site scoping has something to hide."""
        cluster = KubeCluster.objects.create(tenant_key=T, name="dr-cluster", site=DR)
        ns = Namespace.objects.create(tenant_key=T, cluster=cluster, name="dr-prod", site=DR, client_label="Initech")
        cls.dr_tool = ToolInstance.objects.create(
            tenant_key=T, namespace=ns, site=DR, category="monitoring", tool="Prometheus", service="dr-prom",
            client_label="Initech", url="https://dr-prom.example.com", status="Healthy", replica_count=1)
        cls.dr_issue = InventoryIssue.objects.create(
            tenant_key=T, cluster=cluster, namespace=ns, site=DR, issue_id="ISS-9001", client_label="Initech",
            severity="high", category="Pod", resource="dr-pod", issue="Pod Pending", status="open")

    def login(self, user):
        self.client.force_login(user)
        return user

    def setUp(self):
        self.settings_override = override_settings(LINKEDEYE_TENANT=T)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)


PAGES = ["k8s:tools", "k8s:monitoring", "k8s:issues"]
EXPORTS = ["k8s:tools_export", "k8s:monitoring_export", "k8s:issues_export"]


class PageTests(Base):
    def test_every_page_renders_and_has_its_tab(self):
        self.login(self.viewer)
        for name in PAGES:
            with self.subTest(name):
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'href="%s"' % reverse(name))
                self.assertContains(response, "Kubernetes")

    def test_pages_require_login(self):
        for name in PAGES + EXPORTS:
            with self.subTest(name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 302)

    def test_no_placeholder_leaks_into_a_page(self):
        self.login(self.admin)
        for name in PAGES:
            response = self.client.get(reverse(name), {"status": "all", "per": "100"})
            for text in ("NOT DISCOVERED", "NOT APPLICABLE", "MANUAL INPUT REQUIRED"):
                self.assertNotContains(response, text, msg_prefix=name)

    def test_tools_page_groups_by_tool_with_client_counts(self):
        self.login(self.admin)
        response = self.client.get(reverse("k8s:tools"))
        groups = {(g["category"], g["tool"]): g for g in response.context["groups"]}
        grafana = groups[("monitoring", "Grafana")]
        self.assertEqual(grafana["n"], 2)
        self.assertEqual(grafana["clients"], [("Acme", 2)])
        self.assertEqual(grafana["urls"], ["https://grafana.example.com"])
        self.assertEqual(groups[("monitoring", "Prometheus")]["client_count"], 2)    # Acme + Initech
        self.assertEqual(groups[("monitoring", "Loki")]["unmapped"], 1)
        self.assertEqual(groups[("platform", "Vault")]["clients"], [("Globex", 1)])
        self.assertEqual(response.context["totals"]["instances"], len(MON_ROWS) + len(TOOL_ROWS) + 1)
        self.assertContains(response, 'href="https://argocd.example.com"')
        self.assertContains(response, "argocd-secret")

    def test_tools_filters(self):
        self.login(self.admin)
        url = reverse("k8s:tools")
        total = len(MON_ROWS) + len(TOOL_ROWS) + 1
        self.assertEqual(self.client.get(url).context["total"], total)
        self.assertEqual(self.client.get(url, {"category": "cicd"}).context["total"], 1)
        self.assertEqual(self.client.get(url, {"category": "platform"}).context["total"], 2)
        self.assertEqual(self.client.get(url, {"category": "bogus"}).context["total"], total)
        self.assertEqual(self.client.get(url, {"tool": "Grafana"}).context["total"], 2)
        self.assertEqual(self.client.get(url, {"client": "acme"}).context["total"], 7)
        self.assertEqual(self.client.get(url, {"namespace": "globex-prod"}).context["total"], 2)
        self.assertEqual(self.client.get(url, {"environment": "uat"}).context["total"], 2)
        self.assertEqual(self.client.get(url, {"q": "argocd"}).context["total"], 1)
        self.assertEqual(self.client.get(url, {"state": "attention"}).context["total"], 2)       # iDRAC (0 replicas), LDAP (warning)
        self.assertEqual(self.client.get(url, {"state": "no_url"}).context["total"], total - 6)
        self.assertEqual(self.client.get(url, {"state": "no_exporter"}).context["total"], 4 + 1)
        self.assertEqual(self.client.get(url, {"site": DR}).context["total"], 1)

    def test_monitoring_page_lists_clients_without_monitoring(self):
        self.login(self.admin)
        # Globex has CI/CD and platform tools but no monitoring tool; Initech (DR) has Prometheus
        profile_names = {r["name"]: r for r in self.client.get(reverse("k8s:monitoring")).context["cover"]["rows"]}
        self.assertTrue(profile_names["Globex"]["none"])
        self.assertFalse(profile_names["Acme"]["none"])
        self.assertFalse(profile_names["Initech"]["none"])
        response = self.client.get(reverse("k8s:monitoring"))
        self.assertContains(response, "Clients with no monitoring")
        self.assertContains(response, "Globex")
        cover = response.context["cover"]
        self.assertEqual([r["name"] for r in cover["none"]], ["Globex"])
        self.assertEqual(cover["covered"], 2)
        self.assertEqual(cover["total"], 3)
        self.assertEqual(cover["pct"], 67)
        acme = profile_names["Acme"]
        self.assertEqual(acme["caps"]["metrics"], 1)
        self.assertEqual(acme["caps"]["dashboards"], 2)
        self.assertEqual(acme["caps"]["alerting"], 1)
        self.assertEqual(acme["caps"]["hardware"], 1)
        self.assertEqual(acme["caps"]["host"], 1)
        self.assertEqual(acme["caps"]["logs"], 0)
        self.assertEqual(acme["monitoring"], 6)
        self.assertEqual(acme["attention"], 1)
        self.assertEqual(acme["high"], 2)
        self.assertEqual(cover["unmapped_tools"], 1)
        coverage = {c["key"]: c for c in cover["coverage"]}
        self.assertEqual(coverage["metrics"]["clients"], 2)
        self.assertEqual(coverage["logs"]["clients"], 0)

    def test_an_unmapped_marker_is_not_a_client(self):
        ClientProfile.objects.create(tenant_key=T, workbook_name="UNMAPPED / REVIEW REQUIRED", site=MUM)
        self.login(self.admin)
        cover = self.client.get(reverse("k8s:monitoring")).context["cover"]
        self.assertEqual(sorted(r["name"] for r in cover["rows"]), ["Acme", "Globex", "Initech"])

    def test_monitoring_gap_is_not_claimed_when_the_sheet_was_not_imported(self):
        ToolInstance.objects.all().delete()
        self.login(self.admin)
        response = self.client.get(reverse("k8s:monitoring"))
        self.assertContains(response, "monitoring sheet has not been imported")
        self.assertNotContains(response, "Clients with no monitoring")

    def test_monitoring_page_with_nothing_imported_is_an_empty_state(self):
        ToolInstance.objects.all().delete()
        InventoryIssue.objects.all().delete()
        Namespace.objects.all().delete()
        KubeCluster.objects.all().delete()
        ClientProfile.objects.all().delete()
        self.login(self.admin)
        response = self.client.get(reverse("k8s:monitoring"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No clients or monitoring tools have been imported yet")

    def test_issues_page_defaults_to_open_and_counts_by_severity(self):
        self.login(self.admin)
        response = self.client.get(reverse("k8s:issues"))
        self.assertEqual(response.context["total"], len(ISSUE_ROWS) + 1)
        counts = {s["value"]: s["count"] for s in response.context["sev_counts"]}
        self.assertEqual(counts, {"high": 3, "medium": 3, "low": 1})
        self.assertEqual(response.context["chart"]["series"][0]["name"], "High")
        self.assertContains(response, 'data-src="ops-issue-cats"')
        self.assertContains(response, "ISS-0001")

    def test_issue_filters(self):
        self.login(self.admin)
        url = reverse("k8s:issues")
        total = len(ISSUE_ROWS) + 1
        self.assertEqual(self.client.get(url, {"severity": "high"}).context["total"], 3)
        self.assertEqual(self.client.get(url, {"severity": "bogus"}).context["total"], total)
        self.assertEqual(self.client.get(url, {"category": "Pod"}).context["total"], 3)
        self.assertEqual(self.client.get(url, {"client": "globex"}).context["total"], 3)
        self.assertEqual(self.client.get(url, {"namespace": "acme-prod"}).context["total"], 2)
        self.assertEqual(self.client.get(url, {"q": "ISS-0003"}).context["total"], 1)
        self.assertEqual(self.client.get(url, {"q": "Unavailable"}).context["total"], 1)
        self.assertEqual(self.client.get(url, {"incident": "with"}).context["total"], 0)
        self.assertEqual(self.client.get(url, {"incident": "without"}).context["total"], total)
        InventoryIssue.objects.filter(issue_id="ISS-0003").update(status="closed")
        self.assertEqual(self.client.get(url).context["total"], total - 1)
        self.assertEqual(self.client.get(url, {"status": "closed"}).context["total"], 1)
        self.assertEqual(self.client.get(url, {"status": "all"}).context["total"], total)
        self.assertEqual(self.client.get(url, {"status": "bogus"}).context["total"], total)

    def test_severity_kpis_ignore_the_severity_filter_but_not_the_others(self):
        self.login(self.admin)
        response = self.client.get(reverse("k8s:issues"), {"severity": "high", "client": "Acme"})
        counts = {s["value"]: s["count"] for s in response.context["sev_counts"]}
        self.assertEqual(counts, {"high": 2, "medium": 0, "low": 0})
        self.assertEqual(response.context["total"], 2)

    def test_pagination(self):
        self.login(self.admin)
        response = self.client.get(reverse("k8s:issues"), {"per": "5"})
        self.assertEqual(len(response.context["items"]), 5)
        self.assertEqual(response.context["page"].paginator.num_pages, 2)
        self.assertEqual(self.client.get(reverse("k8s:issues"), {"per": "5", "page": "2"}).status_code, 200)
        self.assertEqual(self.client.get(reverse("k8s:tools"), {"per": "5", "page": "999"}).status_code, 200)

    def test_issues_are_ordered_high_first(self):
        self.login(self.admin)
        severities = [i.severity for i in self.client.get(reverse("k8s:issues"), {"per": "100"}).context["items"]]
        self.assertEqual(severities, sorted(severities, key=["high", "medium", "low"].index))

    def test_overview_contribution(self):
        self.login(self.admin)
        response = self.client.get(reverse("k8s:overview"))
        self.assertEqual(response.status_code, 200)
        extra = response.context["extra"]
        self.assertEqual(extra["issues"], {"value": len(ISSUE_ROWS) + 1, "source": "records",
                                           "sheet": "12_Issues_Risks"})
        self.assertEqual(extra["tools"]["source"], "records")
        self.assertContains(response, "Clients without monitoring")
        self.assertContains(response, "Monitoring coverage")
        self.assertContains(response, reverse("k8s:monitoring"))

    def test_overview_without_ops_data_has_no_ops_card(self):
        ToolInstance.objects.all().delete()
        InventoryIssue.objects.all().delete()
        self.login(self.admin)
        response = self.client.get(reverse("k8s:overview"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Clients without monitoring")

    def test_tabs_are_enabled(self):
        from k8s.tabs import is_enabled
        for key in ("tools", "issues", "monitoring"):
            self.assertTrue(is_enabled(key), key)


class CsvTests(Base):
    def rows(self, name, **params):
        self.login(self.admin)
        response = self.client.get(reverse(name), params)
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response["Content-Type"])
        return list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))

    def test_tools_csv(self):
        rows = self.rows("k8s:tools_export")
        self.assertEqual(rows[0][:3], ["Category", "Tool", "Client"])
        self.assertEqual(len(rows) - 1, len(MON_ROWS) + len(TOOL_ROWS) + 1)
        self.assertEqual(len(self.rows("k8s:tools_export", category="cicd")) - 1, 1)
        header = rows[0]
        argo = [r for r in rows[1:] if r[1] == "Argo CD"][0]
        self.assertEqual(argo[header.index("Credential reference (Secret name)")], "argocd-secret")
        self.assertNotIn("svc-argo-user", " ".join(" ".join(r) for r in rows))

    def test_issues_csv_follows_the_filters(self):
        rows = self.rows("k8s:issues_export", severity="high")
        self.assertEqual(len(rows) - 1, 3)
        self.assertEqual({r[1] for r in rows[1:]}, {"High"})
        self.assertEqual(rows[0][0], "Issue ID")

    def test_monitoring_csv(self):
        rows = self.rows("k8s:monitoring_export")
        self.assertEqual(rows[0][:2], ["Client", "Has monitoring"])
        by_client = {r[0]: r for r in rows[1:]}
        self.assertEqual(by_client["Globex"][1], "no")
        self.assertEqual(by_client["Acme"][1], "yes")

    def test_csv_cells_that_start_with_a_formula_character_are_neutralised(self):
        ToolInstance.objects.filter(tool="Grafana", service="grafana").update(service="=1+1")
        rows = self.rows("k8s:tools_export", tool="Grafana")
        services = sorted(r[5] for r in rows[1:])
        self.assertEqual(services, ["'=1+1", "grafana-proxy"])


class ScopeTests(Base):
    def test_a_site_user_sees_only_their_sites_rows(self):
        self.login(self.dr_viewer)
        tools = self.client.get(reverse("k8s:tools"))
        self.assertEqual(tools.context["total"], 1)
        self.assertContains(tools, "dr-prom")
        self.assertNotContains(tools, "argocd")
        self.assertNotContains(tools, "grafana.example.com")
        issues = self.client.get(reverse("k8s:issues"))
        self.assertEqual(issues.context["total"], 1)
        self.assertContains(issues, "ISS-9001")
        self.assertNotContains(issues, "ISS-0001")
        cover = self.client.get(reverse("k8s:monitoring")).context["cover"]
        self.assertEqual([r["name"] for r in cover["rows"]], ["Initech"])
        self.assertEqual(cover["none"], [])

    def test_global_rows_are_visible_to_everyone(self):
        ToolInstance.objects.filter(pk=self.dr_tool.pk).update(site="All Sites")
        self.login(self.dr_viewer)
        self.assertEqual(self.client.get(reverse("k8s:tools")).context["total"], 1)

    def test_a_site_filter_for_a_site_you_cannot_see_is_ignored(self):
        self.login(self.dr_viewer)
        response = self.client.get(reverse("k8s:tools"), {"site": MUM})
        self.assertEqual(response.context["total"], 1)
        self.assertContains(response, "dr-prom")
        self.assertEqual(self.client.get(reverse("k8s:issues"), {"site": MUM}).context["total"], 1)

    def test_exports_are_scoped_too(self):
        self.login(self.dr_viewer)
        for name in ("k8s:tools_export", "k8s:issues_export", "k8s:monitoring_export"):
            body = self.client.get(reverse(name)).content.decode()
            self.assertNotIn("Acme", body, name)
            self.assertNotIn("argocd", body, name)

    def test_overview_numbers_are_scoped(self):
        self.login(self.dr_viewer)
        extra = self.client.get(reverse("k8s:overview")).context["extra"]
        self.assertEqual(extra["issues"]["value"], 1)
        self.assertEqual(extra["tools"]["value"], 1)

    def test_tenant_isolation(self):
        seed(tenant=OTHER)
        other_user = User.objects.create_user("other-admin", password="pw-other-12345")
        with override_settings(LINKEDEYE_TENANT=OTHER):
            Membership.objects.create(user=other_user, site=None, role="admin")
        self.login(self.admin)
        self.assertEqual(self.client.get(reverse("k8s:issues")).context["total"], len(ISSUE_ROWS) + 1)
        self.assertEqual(self.client.get(reverse("k8s:tools")).context["total"],
                         len(MON_ROWS) + len(TOOL_ROWS) + 1)
        self.assertEqual(ToolInstance.objects.filter(tenant_key=OTHER).count(), len(MON_ROWS) + len(TOOL_ROWS))
        # the same issue id may exist in another tenant
        self.assertEqual(InventoryIssue.objects.filter(issue_id="ISS-0001").count(), 2)
        qs = filter_issues(self.admin, {"status": "all"})
        self.assertTrue(all(i.tenant_key == T for i in qs))
        self.assertTrue(all(t.tenant_key == T for t in filter_tools(self.admin, {})))
        self.assertEqual(len(monitoring_coverage(self.admin)["rows"]), 3)

    def test_a_posted_id_from_another_tenant_is_ignored(self):
        seed(tenant=OTHER)
        foreign = InventoryIssue.objects.get(tenant_key=OTHER, issue_id="ISS-0001")
        self.login(self.admin)
        self.client.post(reverse("k8s:issues_open_incidents"), {"ids": [foreign.pk]})
        self.assertEqual(Ticket.objects.count(), 0)
        foreign.refresh_from_db()
        self.assertIsNone(foreign.incident)


class ActionTests(Base):
    def test_open_incident_for_selected_high_issues(self):
        self.login(self.admin)
        ids = list(InventoryIssue.objects.filter(issue_id__in=["ISS-0001", "ISS-0002"]).values_list("pk", flat=True))
        response = self.client.post(reverse("k8s:issues_open_incidents"), {"ids": ids}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Ticket.objects.filter(kind="incident").count(), 2)
        issue = InventoryIssue.objects.get(issue_id="ISS-0001")
        self.assertEqual(issue.incident.fingerprint, "k8sinv:ISS-0001")
        self.assertContains(response, "2 incidents opened")
        self.assertTrue(AuditEvent.objects.filter(action="open_incident").exists())
        # the page now shows the incident number and the link
        self.assertContains(self.client.get(reverse("k8s:issues")), issue.incident.number)

    def test_a_second_click_updates_instead_of_duplicating(self):
        self.login(self.admin)
        ids = list(InventoryIssue.objects.filter(issue_id="ISS-0001").values_list("pk", flat=True))
        self.client.post(reverse("k8s:issues_open_incidents"), {"ids": ids})
        response = self.client.post(reverse("k8s:issues_open_incidents"), {"ids": ids}, follow=True)
        self.assertEqual(Ticket.objects.filter(kind="incident").count(), 1)
        self.assertContains(response, "1 existing incident updated")

    def test_medium_issues_are_skipped(self):
        self.login(self.admin)
        pk = InventoryIssue.objects.get(issue_id="ISS-0003").pk
        response = self.client.post(reverse("k8s:issues_open_incidents"), {"ids": [pk]}, follow=True)
        self.assertEqual(Ticket.objects.count(), 0)
        self.assertContains(response, "skipped")

    def test_scope_filtered_opens_every_open_high_issue_in_view(self):
        self.login(self.admin)
        self.client.post(reverse("k8s:issues_open_incidents"), {"scope": "filtered", "client": "Acme"})
        self.assertEqual(Ticket.objects.filter(kind="incident").count(), 2)
        self.client.post(reverse("k8s:issues_open_incidents"), {"scope": "filtered"})
        self.assertEqual(Ticket.objects.filter(kind="incident").count(), 3)         # + the DR issue

    def test_nothing_selected_is_an_error(self):
        self.login(self.admin)
        response = self.client.post(reverse("k8s:issues_open_incidents"), {}, follow=True)
        self.assertContains(response, "Select at least one issue")

    def test_only_admins_may_open_incidents(self):
        pk = InventoryIssue.objects.get(issue_id="ISS-0001").pk
        for user in (self.viewer, self.operator):
            with self.subTest(user.username):
                self.login(user)
                self.client.post(reverse("k8s:issues_open_incidents"), {"ids": [pk]})
                self.assertEqual(Ticket.objects.count(), 0)
        self.login(self.viewer)
        page = self.client.get(reverse("k8s:issues"))
        self.assertNotContains(page, "Open incident for selected")
        self.assertNotContains(page, 'name="ids"')

    def test_a_site_admin_only_opens_incidents_on_their_own_site(self):
        self.login(self.dr_admin)
        pks = [InventoryIssue.objects.get(issue_id="ISS-0001").pk, self.dr_issue.pk]
        response = self.client.post(reverse("k8s:issues_open_incidents"), {"ids": pks})
        self.assertEqual(Ticket.objects.count(), 1)
        self.assertEqual(Ticket.objects.get().site, DR)
        self.dr_issue.refresh_from_db()
        self.assertIsNotNone(self.dr_issue.incident)
        # ISS-0001 sits on a site the user cannot even see, so it is not selectable at all
        self.assertIsNone(InventoryIssue.objects.get(issue_id="ISS-0001").incident)
        self.assertEqual(response.status_code, 302)

    def test_a_site_viewer_cannot_open_incidents(self):
        self.login(self.dr_viewer)
        self.client.post(reverse("k8s:issues_open_incidents"), {"ids": [self.dr_issue.pk]})
        self.assertEqual(Ticket.objects.count(), 0)

    def test_actions_are_post_only(self):
        self.login(self.admin)
        self.assertEqual(self.client.get(reverse("k8s:issues_open_incidents")).status_code, 405)
        self.assertEqual(self.client.get(reverse("k8s:tools_link_exporters")).status_code, 405)

    def test_link_exporters_action(self):
        self.login(self.operator)
        response = self.client.post(reverse("k8s:tools_link_exporters"), {"client": "Acme"}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Exporter.objects.count(), 3)
        self.assertFalse(Exporter.objects.filter(enabled=True).exists())
        self.assertContains(response, "3 exporters created")
        self.assertTrue(AuditEvent.objects.filter(action="link").exists())
        again = self.client.post(reverse("k8s:tools_link_exporters"), {"client": "Acme"}, follow=True)
        self.assertEqual(Exporter.objects.count(), 3)
        self.assertContains(again, "0 exporters created")

    def test_viewers_cannot_link_exporters(self):
        for user in (self.viewer, self.dr_viewer):
            with self.subTest(user.username):
                self.login(user)
                self.client.post(reverse("k8s:tools_link_exporters"), {})
                self.assertEqual(Exporter.objects.count(), 0)
        self.login(self.viewer)
        self.assertNotContains(self.client.get(reverse("k8s:tools")), "Create / link exporters")

    def test_a_site_operator_links_only_their_sites_tools(self):
        self.login(self.dr_operator)
        self.client.post(reverse("k8s:tools_link_exporters"), {})
        self.assertEqual(Exporter.objects.count(), 1)
        self.assertEqual(Exporter.objects.get().site, DR)
        self.assertFalse(ToolInstance.objects.filter(site=MUM, exporter__isnull=False).exists())

    def test_global_site_tools_need_a_tenant_wide_role(self):
        ToolInstance.objects.filter(pk=self.dr_tool.pk).update(site="All Sites")
        self.login(self.dr_operator)
        self.client.post(reverse("k8s:tools_link_exporters"), {})
        self.assertEqual(Exporter.objects.count(), 0)
        self.login(self.operator)
        self.client.post(reverse("k8s:tools_link_exporters"), {"site": ""})
        self.assertGreater(Exporter.objects.count(), 0)

    def test_a_user_who_sees_a_site_but_is_not_admin_there_is_left_out(self):
        mixed = User.objects.create_user("mixed", password="pw-mixed-12345")
        Membership.objects.create(user=mixed, site=self.mum, role="viewer")
        Membership.objects.create(user=mixed, site=self.dr, role="admin")
        self.login(mixed)
        pks = [InventoryIssue.objects.get(issue_id="ISS-0001").pk, self.dr_issue.pk]
        response = self.client.post(reverse("k8s:issues_open_incidents"), {"ids": pks}, follow=True)
        self.assertEqual(Ticket.objects.count(), 1)
        self.assertEqual(Ticket.objects.get().site, DR)
        self.assertContains(response, "left alone")
        self.assertIsNone(InventoryIssue.objects.get(issue_id="ISS-0001").incident)

    def test_a_user_who_sees_a_site_but_cannot_operate_there_links_nothing_for_it(self):
        mixed = User.objects.create_user("mixed2", password="pw-mixed-12345")
        Membership.objects.create(user=mixed, site=self.mum, role="viewer")
        Membership.objects.create(user=mixed, site=self.dr, role="operator")
        self.login(mixed)
        response = self.client.post(reverse("k8s:tools_link_exporters"), {}, follow=True)
        self.assertEqual(Exporter.objects.count(), 1)
        self.assertEqual(Exporter.objects.get().site, DR)
        self.assertContains(response, "left alone")
        self.assertFalse(ToolInstance.objects.filter(site=MUM, exporter__isnull=False).exists())

    def test_a_global_site_issue_needs_a_tenant_wide_admin(self):
        InventoryIssue.objects.filter(pk=self.dr_issue.pk).update(site="All Sites")
        self.login(self.dr_admin)
        self.client.post(reverse("k8s:issues_open_incidents"), {"ids": [self.dr_issue.pk]})
        self.assertEqual(Ticket.objects.count(), 0)
        self.login(self.admin)
        self.client.post(reverse("k8s:issues_open_incidents"), {"ids": [self.dr_issue.pk]})
        self.assertEqual(Ticket.objects.count(), 1)
        self.assertEqual(Ticket.objects.get().site, "")
