"""Ingresses, certificates and Secret-name references: parsing, idempotent import, opt-in cross-links, the
three pages, CSV, site scoping, RBAC and tenant isolation.

Every fixture is synthetic: the clients are "Acme" and "Globex", addresses come from RFC 5737 (203.0.113.x)
and 10.0.0.0/8, host names are under example.com / example.co.uk. No row of the real workbook is used.
"""
import tempfile
from datetime import timedelta
from pathlib import Path

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import AuditEvent, Membership, Site
from domainmonitor.models import Domain
from urlchecker.models import UrlCheck

from k8s import importer
from k8s.models import Namespace
from k8s.models_ingress import (CertStatus, Certificate, Ingress, SecretRef, status_filter)
from k8s.sheets.ingress import (crosslink_options, link_ingresses, looks_like_name, parse_timestamp,
                                registrable_domain, split_remarks)
from k8s.views_ingress import certificates_for, ingresses_for, secrets_for

from . import fixtures

T = "test-tenant"
OTHER = "other-tenant"
MUM = "Mumbai"
DR = "DR Site"

INGRESS_HEAD = ["Client", "Environment", "Namespace", "Application", "Ingress", "Ingress Class", "Hostname", "URL",
                "Ingress IP", "IP Type", "Path", "Backend Service", "Backend Port", "TLS", "TLS Secret",
                "Certificate", "Status"]
INGRESS_ROWS = [
    ["Acme", "PROD", "acme-prod", "web", "web-ing", "nginx", "app.example.com", "https://app.example.com",
     "203.0.113.10", "Public", "/", "web", "80", "Yes", "app-tls", "app-cert", "Active"],
    ["Acme", "PROD", "acme-prod", "web", "web-ing", "nginx", "app.example.com", "https://app.example.com",
     "203.0.113.10", "Public", "/api", "api", "8080", "Yes", "app-tls", "app-cert", "Active"],
    ["Acme", "PROD", "acme-prod", "docs", "docs-ing", "nginx", "docs.example.com", "https://docs.example.com",
     "10.0.0.5", "Private", "/", "docs", "80", "Yes", "wild-tls", "NOT DISCOVERED", "Active"],
    ["Acme", "UAT", "acme-uat", "legacy", "legacy-ing", "nginx", "legacy.example.com", "http://legacy.example.com",
     "10.0.0.6", "Private", "/", "legacy", "http", "No", "NOT DISCOVERED", "NOT DISCOVERED", "Active"],
    ["Globex", "NOT DISCOVERED", "globex-prod", "shop", "shop-ing", "nginx-globex", "shop.example.co.uk",
     "https://shop.example.co.uk", "NOT DISCOVERED", "NOT DISCOVERED", "/", "shop", "3000", "Yes",
     "NOT DISCOVERED", "NOT DISCOVERED", "Missing Address"],
]

CERT_HEAD = ["Client", "Namespace", "Domain", "Certificate", "TLS Secret", "Issuer", "Ready", "Valid From",
             "Expiry", "Days Remaining", "Risk", "Remarks"]


def stamp(delta_days, now=None):
    return ((now or timezone.now()) + timedelta(days=delta_days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def cert_rows(now=None):
    return [CERT_HEAD,
            ["Acme", "acme-prod", "app.example.com", "app-cert", "app-tls", "letsencrypt-test", "True",
             stamp(-60, now), stamp(10, now), "999", "Healthy", "Up to date"],
            ["Acme", "acme-prod", "old.example.com", "old-cert", "old-tls", "letsencrypt-test", "True",
             stamp(-120, now), stamp(-2, now), "999", "Healthy", "NOT DISCOVERED"],
            ["Globex", "globex-prod", "shop.example.co.uk", "shop-cert", "shop-tls", "internal-ca", "False",
             stamp(-10, now), stamp(200, now), "999", "Healthy", "NOT DISCOVERED"]]


SECRET_HEAD = ["Client", "Environment", "Application", "URL", "Username", "Authentication", "Keycloak Realm", "LDAP",
               "Secret Name", "Secret Namespace", "Vault Path", "Password Location", "Credential Owner",
               "Last Verified", "Remarks"]
SECRET_ROWS = [SECRET_HEAD,
               ["Acme", "PROD", "web", "NOT DISCOVERED", "svc-sentinel-user", "Kubernetes Secret reference",
                "NOT DISCOVERED", "NOT DISCOVERED", "web-db-credentials", "acme-prod", "NOT DISCOVERED",
                "Stored in Kubernetes Secret", "MANUAL INPUT REQUIRED", "NOT DISCOVERED",
                "Referenced by Deployment web; secret volume"],
               ["Acme", "PROD", "web", "NOT DISCOVERED", "NOT DISCOVERED", "Kubernetes Secret reference",
                "NOT DISCOVERED", "NOT DISCOVERED", "app-tls", "acme-prod", "NOT DISCOVERED",
                "Stored in Kubernetes Secret", "MANUAL INPUT REQUIRED", "NOT DISCOVERED",
                "Referenced by Deployment web"],
               ["Acme", "PROD", "docs", "NOT DISCOVERED", "NOT DISCOVERED", "Kubernetes Secret reference",
                "NOT DISCOVERED", "NOT DISCOVERED", "web-db-credentials", "acme-prod", "NOT DISCOVERED",
                "Stored in Kubernetes Secret", "MANUAL INPUT REQUIRED", "NOT DISCOVERED",
                "Referenced by StatefulSet docs"],
               ["Globex", "NOT DISCOVERED", "shop", "NOT DISCOVERED", "NOT DISCOVERED",
                "Kubernetes Secret reference", "NOT DISCOVERED", "NOT DISCOVERED", "shop-api-key", "globex-prod",
                "NOT DISCOVERED", "Stored in Kubernetes Secret", "MANUAL INPUT REQUIRED", "NOT DISCOVERED",
                "Referenced by Deployment shop"]]


def edge_sheets(now=None, certs=True, secrets=True, ingresses=True):
    out = {}
    if ingresses:
        out["06_Ingress_URLs"] = [INGRESS_HEAD] + INGRESS_ROWS
    if certs:
        out["09_Certificate_Inventory"] = cert_rows(now)
    if secrets:
        out["08_Credentials_References"] = SECRET_ROWS
    return out


def write_source(**kwargs):
    directory = Path(tempfile.mkdtemp())
    fixtures.write_csv_dir(directory, extra=edge_sheets(**kwargs))
    return directory


def edge_only(sheets):
    directory = Path(tempfile.mkdtemp())
    fixtures.write_csv_dir(directory, sheets={}, extra=sheets)
    return directory


def seed(site="All Sites", tenant=T, **kwargs):
    directory = write_source(**kwargs)
    with override_settings(LINKEDEYE_TENANT=tenant):
        return importer.run_import(directory, site=site)


# ============================================================================ pure functions
class HelperTests(TestCase):
    def test_registrable_domain(self):
        self.assertEqual(registrable_domain("api.team.example.com"), "example.com")
        self.assertEqual(registrable_domain("example.com"), "example.com")
        self.assertEqual(registrable_domain("shop.example.co.uk"), "example.co.uk")
        self.assertEqual(registrable_domain("a.b.example.co.in"), "example.co.in")
        self.assertEqual(registrable_domain("*.example.com"), "example.com")
        self.assertEqual(registrable_domain("EXAMPLE.com."), "example.com")
        for bad in ("", "localhost", "203.0.113.10", "*", "a..b", "::1"):
            with self.subTest(bad):
                self.assertEqual(registrable_domain(bad), "")

    def test_parse_timestamp(self):
        self.assertEqual(parse_timestamp("2026-01-02T03:04:05Z").isoformat(), "2026-01-02T03:04:05+00:00")
        self.assertEqual(parse_timestamp("2026-01-02 03:04:05").utcoffset(), timedelta(0))
        self.assertEqual(parse_timestamp("02-01-2026").day, 2)
        for empty in ("", "NOT DISCOVERED", "soon", None):
            with self.subTest(empty):
                self.assertIsNone(parse_timestamp(empty))

    def test_split_remarks(self):
        self.assertEqual(split_remarks("Referenced by Deployment web; secret volume"),
                         ("Deployment web", "secret volume"))
        self.assertEqual(split_remarks("Referenced by Job nightly"), ("Job nightly", ""))
        self.assertEqual(split_remarks("something else"), ("", "something else"))
        self.assertEqual(split_remarks("NOT DISCOVERED"), ("", ""))

    def test_secret_name_shape(self):
        self.assertTrue(looks_like_name("web-db-credentials"))
        self.assertTrue(looks_like_name("sh.registry.v1"))
        for bad in ("", "has space", "a=b==", "x" * 254, "-leading", "tab\tname"):
            with self.subTest(bad):
                self.assertFalse(looks_like_name(bad))


# ============================================================================ models
@override_settings(LINKEDEYE_TENANT=T)
class ModelTests(TestCase):
    def test_secretref_has_no_field_that_could_hold_a_value(self):
        names = {f.name for f in SecretRef._meta.get_fields()}
        forbidden = ("value", "password", "passwd", "token", "data", "secret_data", "base64", "key_data", "content",
                     "username", "private")
        for name in names:
            for word in forbidden:
                self.assertNotIn(word, name, f"SecretRef.{name} looks like it could hold a secret value")
        self.assertIn("never holds a secret value", SecretRef.__doc__)
        # the TLS secret columns elsewhere are NAMES, and say so
        self.assertIn("never read", Ingress._meta.get_field("tls_secret").help_text)
        self.assertIn("never read", Certificate._meta.get_field("tls_secret").help_text)

    def test_days_remaining_is_computed_not_stored(self):
        self.assertNotIn("days_remaining", {f.name for f in Certificate._meta.get_fields()})
        now = timezone.now()
        cert = Certificate(expiry=now + timedelta(days=10, hours=3))
        self.assertEqual(cert.days_remaining(now), 10)
        self.assertEqual(cert.days_remaining(now + timedelta(days=4)), 6)       # same row, later reading
        self.assertEqual(Certificate(expiry=now - timedelta(hours=1)).days_remaining(now), 0)
        self.assertEqual(Certificate(expiry=now - timedelta(days=2, hours=1)).days_remaining(now), -2)
        self.assertIsNone(Certificate().days_remaining(now))

    def test_status_bands_and_tones(self):
        now = timezone.now()
        cases = [(-1, CertStatus.EXPIRED, "crit"), (3, CertStatus.CRITICAL, "crit"),
                 (7.5, CertStatus.EXPIRING, "warn"), (29, CertStatus.EXPIRING, "warn"),
                 (30.5, CertStatus.SOON, "info"), (89, CertStatus.SOON, "info"), (91, CertStatus.OK, "ok")]
        for days, status, tone in cases:
            with self.subTest(days=days):
                cert = Certificate(expiry=now + timedelta(days=days), ready=True)
                self.assertEqual(cert.status(now), status)
                self.assertEqual(cert.tone(now), tone)
        self.assertEqual(Certificate().status(now), CertStatus.UNKNOWN)
        self.assertEqual(Certificate(expiry=now + timedelta(days=200), ready=False).tone(now), "crit")

    def test_status_filter_agrees_with_the_property(self):
        ns = Namespace.objects.create(tenant_key=T, cluster=_cluster(), name="n1")
        now = timezone.now()
        for i, days in enumerate((-3, 0.5, 6.9, 7.1, 29.9, 30.1, 89.9, 90.1, 400)):
            Certificate.objects.create(tenant_key=T, namespace=ns, name=f"c{i}", expiry=now + timedelta(days=days))
        Certificate.objects.create(tenant_key=T, namespace=ns, name="no-expiry")
        for status in CertStatus.values:
            with self.subTest(status):
                by_query = set(Certificate.objects.filter(status_filter(status, now)).values_list("name", flat=True))
                by_property = {c.name for c in Certificate.objects.all() if c.status(now) == status}
                self.assertEqual(by_query, by_property)

    def test_lifetime_left(self):
        now = timezone.now()
        cert = Certificate(valid_from=now - timedelta(days=30), expiry=now + timedelta(days=30))
        self.assertEqual(cert.lifetime_left_pct(now), 50)
        self.assertEqual(Certificate(valid_from=now, expiry=now - timedelta(days=1)).lifetime_left_pct(now), None)
        self.assertEqual(Certificate(valid_from=now - timedelta(days=9), expiry=now - timedelta(days=1))
                         .lifetime_left_pct(now), 0)

    def test_check_url_never_invents_a_host(self):
        self.assertEqual(Ingress(url="https://a.example.com").check_url, "https://a.example.com")
        self.assertEqual(Ingress(hostname="a.example.com", tls=True).check_url, "https://a.example.com")
        self.assertEqual(Ingress(hostname="a.example.com", tls=False).check_url, "http://a.example.com")
        self.assertEqual(Ingress(hostname="", tls=True).check_url, "")
        self.assertEqual(Ingress(hostname="*.example.com", tls=True).check_url, "")


def _cluster():
    from k8s.models import KubeCluster
    cluster, _ = KubeCluster.objects.get_or_create(tenant_key=T, name="kubernetes")
    return cluster


# ============================================================================ import
@override_settings(LINKEDEYE_TENANT=T)
class ImportTests(TestCase):
    def test_import_creates_rows_and_normalises_placeholders(self):
        result = seed()
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(Ingress.objects.count(), 5)
        self.assertEqual(Certificate.objects.count(), 3)
        self.assertEqual(SecretRef.objects.count(), 4)
        self.assertEqual(result.counts["06_Ingress_URLs"]["created"], 5)
        self.assertEqual(result.counts["09_Certificate_Inventory"]["rows"], 3)
        shop = Ingress.objects.get(hostname="shop.example.co.uk")
        self.assertEqual(shop.tls_secret, "")
        self.assertEqual(shop.certificate_name, "")
        self.assertEqual(shop.ingress_ip, None)
        self.assertEqual(shop.ip_type, "unknown")
        self.assertEqual(shop.environment, "")
        self.assertTrue(shop.missing_address)
        self.assertEqual(shop.namespace.client_label, "Globex")
        for model in (Ingress, Certificate, SecretRef):
            for row in model.objects.all():
                for field in model._meta.get_fields():
                    value = getattr(row, field.name, None) if field.concrete else None
                    if isinstance(value, str):
                        self.assertNotIn("NOT DISCOVERED", value)
                        self.assertNotIn("MANUAL INPUT", value)

    def test_hostname_with_several_paths_is_several_rows_on_one_host(self):
        seed()
        rows = Ingress.objects.filter(hostname="app.example.com").order_by("path")
        self.assertEqual([r.path for r in rows], ["/", "/api"])
        self.assertEqual([r.backend for r in rows], ["web:80", "api:8080"])
        self.assertTrue(all(r.tls and r.ip_type == "public" and str(r.ingress_ip) == "203.0.113.10" for r in rows))
        legacy = Ingress.objects.get(hostname="legacy.example.com")
        self.assertFalse(legacy.tls)
        self.assertEqual(legacy.backend_port, "http")          # a named port stays a name

    def test_certificates_parse_dates_and_ignore_the_stale_days_column(self):
        now = timezone.now()
        seed()
        cert = Certificate.objects.get(name="app-cert")
        self.assertTrue(cert.ready)
        self.assertEqual(cert.days_remaining(now), 9)       # 10 days from seed time, floored a moment later
        self.assertEqual(cert.risk, "Healthy")
        self.assertEqual(cert.remarks, "Up to date")
        self.assertEqual(Certificate.objects.get(name="old-cert").remarks, "")
        self.assertIs(Certificate.objects.get(name="shop-cert").ready, False)

    def test_ingress_links_to_its_certificate(self):
        seed()
        app = Ingress.objects.filter(hostname="app.example.com")
        self.assertEqual({i.certificate.name for i in app}, {"app-cert"})
        self.assertIsNone(Ingress.objects.get(hostname="docs.example.com").certificate)
        # matched by TLS secret when the ingress names no certificate
        Ingress.objects.filter(hostname="docs.example.com").update(tls_secret="old-tls")
        from k8s.sheets.ingress import relink_certificates
        self.assertEqual(relink_certificates(T), 1)
        self.assertEqual(Ingress.objects.get(hostname="docs.example.com").certificate.name, "old-cert")

    def test_secret_refs_hold_names_only(self):
        result = seed()
        self.assertTrue(result.ok, result.errors)
        refs = SecretRef.objects.order_by("secret_namespace", "application", "secret_name")
        web = refs.get(application="web", secret_name="web-db-credentials")
        self.assertEqual((web.secret_namespace, web.referenced_by, web.remarks),
                         ("acme-prod", "Deployment web", "secret volume"))
        self.assertEqual(web.namespace.name, "acme-prod")
        # the same secret referenced by two applications is two references
        self.assertEqual(SecretRef.objects.filter(secret_name="web-db-credentials").count(), 2)
        # the user name column of the sheet was never read
        blob = " ".join(str(v) for r in SecretRef.objects.values() for v in r.values())
        self.assertNotIn("svc-sentinel-user", blob)
        self.assertNotIn("Stored in Kubernetes Secret", blob)

    def test_a_value_column_is_reported_and_never_stored(self):
        head = SECRET_HEAD + ["Secret Value", "password"]
        rows = [head] + [r + ["hunter2-sentinel", "pw-sentinel"] for r in SECRET_ROWS[1:]]
        result = importer.run_import(edge_only({"08_Credentials_References": rows}))
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(SecretRef.objects.count(), 4)
        warnings = " ".join(result.warnings)
        self.assertIn("Secret Value", warnings)
        self.assertIn("password", warnings)
        blob = " ".join(str(v) for r in SecretRef.objects.values() for v in r.values())
        self.assertNotIn("hunter2-sentinel", blob)
        self.assertNotIn("pw-sentinel", blob)
        run_blob = str(importer.ImportRun.objects.get(pk=result.run_id).__dict__)
        self.assertNotIn("hunter2-sentinel", run_blob)

    def test_a_secret_name_that_is_not_a_name_is_rejected(self):
        rows = SECRET_ROWS + [["Acme", "PROD", "web", "", "", "", "", "", "c2VjcmV0LXZhbHVl==", "acme-prod", "", "",
                               "", "", ""],
                              ["Acme", "PROD", "web", "", "", "", "", "", "has a space", "acme-prod", "", "", "", "",
                               ""]]
        result = importer.run_import(edge_only({"08_Credentials_References": rows}))
        self.assertEqual(SecretRef.objects.count(), 4)
        self.assertFalse(SecretRef.objects.filter(secret_name__contains="==").exists())
        self.assertIn("did not hold a Kubernetes object name", " ".join(result.warnings))
        self.assertEqual(result.counts["08_Credentials_References"]["skipped"], 2)

    def test_reimport_changes_nothing(self):
        now = timezone.now()
        directory = write_source(now=now)
        first = importer.run_import(directory)
        again = importer.run_import(directory)
        self.assertEqual(again.total_created, 0)
        self.assertEqual(again.total_updated, 0)
        self.assertEqual(again.total_missing, 0)
        self.assertEqual((Ingress.objects.count(), Certificate.objects.count(), SecretRef.objects.count()), (5, 3, 4))
        self.assertEqual(first.counts["06_Ingress_URLs"]["created"], 5)
        self.assertEqual(Ingress.objects.filter(certificate__isnull=False).count(), 2)

    def test_changed_cell_updates_in_place_and_a_removed_row_is_marked_missing(self):
        now = timezone.now()
        importer.run_import(write_source(now=now))
        rows = [INGRESS_HEAD] + [list(r) for r in INGRESS_ROWS if r[6] != "legacy.example.com"]
        rows[1][8] = "203.0.113.99"                         # the first rule's address changed
        result = importer.run_import(edge_only({"06_Ingress_URLs": rows}))
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.counts["06_Ingress_URLs"]["updated"], 1)
        self.assertEqual(result.counts["06_Ingress_URLs"]["missing"], 1)
        self.assertEqual(Ingress.objects.count(), 5)         # nothing deleted
        self.assertIsNotNone(Ingress.objects.get(hostname="legacy.example.com").missing_since)
        self.assertEqual(Ingress.objects.filter(missing_since__isnull=True).count(), 4)
        # it comes back
        importer.run_import(write_source(now=now))
        self.assertIsNone(Ingress.objects.get(hostname="legacy.example.com").missing_since)

    def test_a_partial_import_does_not_retire_the_other_sheets(self):
        importer.run_import(write_source())
        importer.run_import(edge_only({"06_Ingress_URLs": [INGRESS_HEAD] + INGRESS_ROWS}))
        self.assertEqual(Certificate.objects.filter(missing_since__isnull=False).count(), 0)
        self.assertEqual(SecretRef.objects.filter(missing_since__isnull=False).count(), 0)

    def test_dry_run_writes_nothing(self):
        result = importer.run_import(write_source(), dry_run=True)
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.counts["06_Ingress_URLs"]["created"], 5)
        self.assertEqual((Ingress.objects.count(), Certificate.objects.count(), SecretRef.objects.count()), (0, 0, 0))

    def test_rows_without_the_essentials_are_skipped_with_a_warning(self):
        rows = [INGRESS_HEAD,
                ["Acme", "PROD", "", "web", "web-ing", "nginx", "a.example.com", "", "", "", "/", "", "", "Yes", "", "", ""],
                ["Acme", "PROD", "acme-prod", "web", "", "nginx", "", "", "", "", "/", "", "", "Yes", "", "", ""]]
        result = importer.run_import(edge_only({"06_Ingress_URLs": rows}))
        self.assertEqual(Ingress.objects.count(), 0)
        self.assertEqual(result.counts["06_Ingress_URLs"]["skipped"], 2)
        bad = [CERT_HEAD, ["Acme", "acme-prod", "x.example.com", "x", "x-tls", "i", "True", "2026-01-01",
                           "garbage", "1", "", ""]]
        result = importer.run_import(edge_only({"09_Certificate_Inventory": bad}))
        self.assertIsNone(Certificate.objects.get(name="x").expiry)
        self.assertIn("expiry date could not be read", " ".join(result.warnings))

    def test_a_tls_secret_implies_tls_when_the_column_is_empty(self):
        row = list(INGRESS_ROWS[0])
        row[13] = ""
        importer.run_import(edge_only({"06_Ingress_URLs": [INGRESS_HEAD, row]}))
        self.assertTrue(Ingress.objects.get().tls)

    def test_site_follows_the_import_site(self):
        seed(site=MUM)
        self.assertEqual(set(Ingress.objects.values_list("site", flat=True)), {MUM})
        self.assertEqual(set(Certificate.objects.values_list("site", flat=True)), {MUM})
        self.assertEqual(set(SecretRef.objects.values_list("site", flat=True)), {MUM})

    def test_registered_with_the_site_rename_list(self):
        from administration.services import SITE_NAME_FIELDS
        for model in ("Ingress", "Certificate", "SecretRef"):
            self.assertIn(("k8s", model), {(a, m) for a, m, _f, _l in SITE_NAME_FIELDS})


# ============================================================================ cross-links
@override_settings(LINKEDEYE_TENANT=T)
class CrossLinkTests(TestCase):
    def run_it(self, urlchecks=False, domains=False, directory=None, site=MUM):
        directory = directory or write_source()
        with crosslink_options(urlchecks=urlchecks, domains=domains):
            return importer.run_import(directory, site=site)

    def test_off_by_default_creates_no_check_and_no_domain(self):
        self.run_it()
        self.assertEqual(UrlCheck.objects.count(), 0)
        self.assertEqual(Domain.objects.count(), 0)
        self.assertFalse(Ingress.objects.filter(url_check__isnull=False).exists())

    def test_urlchecks_are_created_disabled_on_the_same_site_for_https_only(self):
        result = self.run_it(urlchecks=True)
        self.assertTrue(result.ok, result.errors)
        checks = {c.url: c for c in UrlCheck.objects.all()}
        self.assertEqual(set(checks), {"https://app.example.com", "https://docs.example.com",
                                       "https://shop.example.co.uk"})          # http:// legacy is not probed
        for check in checks.values():
            self.assertFalse(check.enabled)
            self.assertEqual(check.site, MUM)
            self.assertEqual(check.tenant_key, T)
        # two paths on one host share one check
        app = Ingress.objects.filter(hostname="app.example.com")
        self.assertEqual({i.url_check_id for i in app}, {checks["https://app.example.com"].pk})
        self.assertIsNone(Ingress.objects.get(hostname="legacy.example.com").url_check)
        self.assertEqual(result.counts["06_Ingress_URLs"]["urlchecks_created"], 3)
        self.assertEqual(Domain.objects.count(), 0)

    def test_an_existing_check_is_linked_not_duplicated(self):
        existing = UrlCheck.objects.create(tenant_key=T, name="Our app", url="https://app.example.com/", site=MUM,
                                           enabled=True)
        self.run_it(urlchecks=True)
        self.assertEqual(UrlCheck.objects.filter(url__startswith="https://app.example.com").count(), 1)
        self.assertEqual({i.url_check_id for i in Ingress.objects.filter(hostname="app.example.com")}, {existing.pk})
        existing.refresh_from_db()
        self.assertTrue(existing.enabled)                   # an operator's check is never changed
        self.assertEqual(existing.name, "Our app")

    def test_name_collision_gets_a_suffix(self):
        UrlCheck.objects.create(tenant_key=T, name="app.example.com", url="https://other.example.com", site=MUM)
        self.run_it(urlchecks=True)
        names = set(UrlCheck.objects.values_list("name", flat=True))
        self.assertIn("app.example.com", names)
        self.assertIn("app.example.com (kubernetes)", names)

    def test_domains_link_to_an_existing_host_or_registrable_domain_else_are_created_unmonitored(self):
        exact = Domain.objects.create(tenant_key=T, site_key=MUM, hostname="docs.example.com")
        apex = Domain.objects.create(tenant_key=T, site_key=MUM, hostname="example.com")
        result = self.run_it(domains=True)
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(Ingress.objects.get(hostname="docs.example.com").domain_id, exact.pk)
        self.assertEqual({i.domain_id for i in Ingress.objects.filter(hostname="app.example.com")}, {apex.pk})
        created = Domain.objects.get(hostname="example.co.uk")
        self.assertEqual(Ingress.objects.get(hostname="shop.example.co.uk").domain_id, created.pk)
        self.assertFalse(created.monitor_enabled)
        self.assertEqual(created.site_key, MUM)
        self.assertEqual(Domain.objects.count(), 3)
        self.assertEqual(result.counts["06_Ingress_URLs"]["domains_created"], 1)
        self.assertEqual(UrlCheck.objects.count(), 0)

    def test_crosslinking_twice_is_idempotent(self):
        directory = write_source()
        self.run_it(urlchecks=True, domains=True, directory=directory)
        before = (UrlCheck.objects.count(), Domain.objects.count())
        again = self.run_it(urlchecks=True, domains=True, directory=directory)
        self.assertEqual((UrlCheck.objects.count(), Domain.objects.count()), before)
        self.assertEqual(again.total_created, 0)
        self.assertEqual(again.total_updated, 0)
        self.assertEqual(again.counts["06_Ingress_URLs"].get("urlchecks_created", 0), 0)

    def test_dry_run_with_links_writes_nothing(self):
        with crosslink_options(urlchecks=True, domains=True):
            result = importer.run_import(write_source(), dry_run=True)
        self.assertTrue(result.ok, result.errors)
        self.assertEqual((UrlCheck.objects.count(), Domain.objects.count(), Ingress.objects.count()), (0, 0, 0))

    def test_links_never_cross_tenants(self):
        other = UrlCheck.objects.create(tenant_key=OTHER, name="theirs", url="https://app.example.com", site=MUM)
        Domain.objects.create(tenant_key=OTHER, site_key=MUM, hostname="example.com")
        self.run_it(urlchecks=True, domains=True)
        mine = Ingress.objects.get(hostname="app.example.com", path="/")
        self.assertNotEqual(mine.url_check_id, other.pk)
        self.assertEqual(mine.url_check.tenant_key, T)
        self.assertEqual(mine.domain.tenant_key, T)
        self.assertEqual(UrlCheck.objects.filter(tenant_key=OTHER).count(), 1)

    def test_a_changed_url_drops_the_stale_link(self):
        directory = write_source()
        self.run_it(urlchecks=True, directory=directory)
        rows = [INGRESS_HEAD] + [list(r) for r in INGRESS_ROWS]
        rows[3][7] = "https://docs-new.example.com"            # docs rule now has another URL
        importer.run_import(edge_only({"06_Ingress_URLs": rows}))
        self.assertIsNone(Ingress.objects.get(hostname="docs.example.com").url_check)

    def test_link_ingresses_respects_the_permission_callback(self):
        importer.run_import(write_source(), site=MUM)
        result = link_ingresses(T, Ingress.objects.all(), urlchecks=True, allowed=lambda site: False)
        self.assertEqual(result["forbidden"], 5)
        self.assertEqual(UrlCheck.objects.count(), 0)


# ============================================================================ pages
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
        cls.dr_operator = User.objects.create_user("dr-operator", password="pw-droperator-12345")
        Membership.objects.create(user=cls.dr_operator, site=cls.dr, role="operator")
        cls.dr_viewer = User.objects.create_user("dr-viewer", password="pw-drviewer-12345")
        Membership.objects.create(user=cls.dr_viewer, site=cls.dr, role="viewer")

    def login(self, user):
        self.client.force_login(user)
        return user


PAGES = ["k8s:ingresses", "k8s:certificates", "k8s:secrets"]
EXPORTS = ["k8s:ingresses_export", "k8s:certificates_export", "k8s:secrets_export"]


class PageTests(Base):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        seed(site=MUM)

    def test_every_page_renders_for_a_viewer(self):
        self.login(self.viewer)
        for name in PAGES:
            with self.subTest(name):
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Kubernetes")

    def test_tabs_link_the_three_pages(self):
        self.login(self.viewer)
        response = self.client.get(reverse("k8s:overview"))
        for name in PAGES:
            self.assertContains(response, f'href="{reverse(name)}"')

    def test_pages_require_login(self):
        for name in PAGES + EXPORTS:
            with self.subTest(name):
                response = self.client.get(reverse(name))
                self.assertIn(response.status_code, (302, 401, 403))

    def test_ingress_page_groups_paths_under_one_host(self):
        self.login(self.viewer)
        response = self.client.get(reverse("k8s:ingresses"))
        groups = {g["hostname"]: g for g in response.context["groups"]}
        self.assertEqual(set(groups), {"app.example.com", "docs.example.com", "legacy.example.com",
                                       "shop.example.co.uk"})
        self.assertEqual([r.path for r in groups["app.example.com"]["rows"]], ["/", "/api"])
        self.assertEqual(groups["app.example.com"]["certificate"].name, "app-cert")
        self.assertEqual(groups["app.example.com"]["client"], "Acme")
        self.assertEqual(response.context["page"].paginator.count, 4)       # hosts, not rules
        self.assertEqual(response.context["totals"]["rules"], 5)
        self.assertEqual(response.context["totals"]["hosts"], 4)
        self.assertEqual(response.context["totals"]["tls_pct"], 80)
        self.assertContains(response, "app.example.com")
        self.assertContains(response, "/api")
        self.assertContains(response, "no certificate record")
        self.assertContains(response, "Missing address")

    def test_ingress_filters(self):
        self.login(self.viewer)
        url = reverse("k8s:ingresses")

        def total(**params):
            return self.client.get(url, params).context["totals"]["rules"]
        self.assertEqual(total(client="Acme"), 4)
        self.assertEqual(total(client="globex"), 1)
        self.assertEqual(total(**{"class": "nginx"}), 4)
        self.assertEqual(total(ip_type="public"), 2)
        self.assertEqual(total(ip_type="private"), 2)
        self.assertEqual(total(ip_type="nonsense"), 5)
        self.assertEqual(total(tls="yes"), 4)
        self.assertEqual(total(tls="no"), 1)
        self.assertEqual(total(environment="uat"), 1)
        self.assertEqual(total(q="/api"), 1)
        self.assertEqual(total(q="docs"), 1)
        self.assertEqual(total(state="missing_address"), 1)
        self.assertEqual(total(state="no_certificate"), 2)       # docs (wild-tls) and shop (no secret, but tls)
        self.assertEqual(total(state="no_check"), 5)
        self.assertEqual(total(namespace="acme-prod"), 3)
        self.assertContains(self.client.get(url, {"client": "nobody"}), "No ingress rules match these filters.")

    def test_ingress_pagination_counts_hosts(self):
        self.login(self.viewer)
        response = self.client.get(reverse("k8s:ingresses"), {"per": 5})
        self.assertEqual(response.context["page"].paginator.per_page, 5)
        self.assertEqual(len(response.context["groups"]), 4)

    def test_certificates_page_is_sorted_by_days_left_with_tones(self):
        self.login(self.viewer)
        response = self.client.get(reverse("k8s:certificates"))
        rows = response.context["rows"]
        self.assertEqual([r["cert"].name for r in rows], ["old-cert", "app-cert", "shop-cert"])
        self.assertEqual([r["status"] for r in rows], ["expired", "expiring", "ok"])
        self.assertEqual([r["tone"] for r in rows], ["crit", "warn", "crit"])      # shop-cert is not ready
        self.assertLess(rows[0]["days"], 0)
        self.assertEqual(rows[1]["days"], 9)
        totals = response.context["totals"]
        self.assertEqual((totals["total"], totals["expired"], totals["under30"], totals["not_ready"]), (3, 1, 2, 1))
        self.assertEqual(totals["next"].name, "app-cert")
        self.assertContains(response, "expired 2 d ago")
        self.assertContains(response, "9 days")
        self.assertContains(response, "Under 30 days")
        self.assertContains(response, "Not ready")

    def test_countdown_follows_the_clock_not_the_import(self):
        self.login(self.viewer)
        Certificate.objects.filter(name="app-cert").update(expiry=timezone.now() + timedelta(days=3, hours=1))
        rows = self.client.get(reverse("k8s:certificates")).context["rows"]
        by_name = {r["cert"].name: r for r in rows}
        self.assertEqual(by_name["app-cert"]["days"], 3)
        self.assertEqual(by_name["app-cert"]["status"], "critical")

    def test_certificate_filters(self):
        self.login(self.viewer)
        url = reverse("k8s:certificates")

        def names(**params):
            return [r["cert"].name for r in self.client.get(url, params).context["rows"]]
        self.assertEqual(names(status="expired"), ["old-cert"])
        self.assertEqual(names(status="expiring"), ["app-cert"])
        self.assertEqual(names(status="under30"), ["old-cert", "app-cert"])
        self.assertEqual(names(status="ok"), ["shop-cert"])
        self.assertEqual(names(status="unknown"), [])
        self.assertEqual(names(ready="no"), ["shop-cert"])
        self.assertEqual(names(issuer="internal-ca"), ["shop-cert"])
        self.assertEqual(names(client="acme"), ["old-cert", "app-cert"])
        self.assertEqual(names(q="co.uk"), ["shop-cert"])
        self.assertEqual(names(sort="-expiry"), ["shop-cert", "app-cert", "old-cert"])
        self.assertEqual(len(names(status="bogus")), 3)

    def test_certificate_page_counts_the_ingresses_that_use_it(self):
        self.login(self.viewer)
        by_name = {r["cert"].name: r["cert"].ingress_count for r in
                   self.client.get(reverse("k8s:certificates")).context["rows"]}
        self.assertEqual(by_name, {"old-cert": 0, "app-cert": 2, "shop-cert": 0})

    def test_secrets_page_states_that_values_are_never_stored(self):
        self.login(self.viewer)
        response = self.client.get(reverse("k8s:secrets"))
        self.assertContains(response, "secret values are never stored")
        self.assertEqual(response.context["totals"]["references"], 4)
        self.assertEqual(response.context["totals"]["secrets"], 3)
        self.assertEqual(response.context["totals"]["namespaces"], 2)
        self.assertContains(response, "web-db-credentials")
        self.assertContains(response, "Deployment web")
        self.assertNotContains(response, "svc-sentinel-user")
        by_ns = {row["namespace__name"]: row for row in response.context["by_namespace"]}
        self.assertEqual(by_ns["acme-prod"]["refs"], 3)
        self.assertEqual(by_ns["acme-prod"]["names"], 2)

    def test_secret_filters(self):
        self.login(self.viewer)
        url = reverse("k8s:secrets")

        def total(**params):
            return self.client.get(url, params).context["total"]
        self.assertEqual(total(namespace="acme-prod"), 3)
        self.assertEqual(total(client="Globex"), 1)
        self.assertEqual(total(application="web"), 2)
        self.assertEqual(total(q="shop-api"), 1)
        self.assertEqual(total(q="StatefulSet"), 1)
        self.assertEqual(total(environment="prod"), 3)

    def test_csv_exports_are_attachments_with_the_filtered_rows(self):
        self.login(self.viewer)
        for name in EXPORTS:
            with self.subTest(name):
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response["Content-Type"].startswith("text/csv"))
                self.assertIn("attachment; filename=", response["Content-Disposition"])
        body = self.client.get(reverse("k8s:ingresses_export"), {"class": "nginx-globex"}).content.decode()
        self.assertIn("shop.example.co.uk", body)
        self.assertNotIn("app.example.com", body)
        body = self.client.get(reverse("k8s:certificates_export"), {"status": "expired"}).content.decode()
        self.assertIn("old-cert", body)
        self.assertNotIn("app-cert", body)
        self.assertIn("Days remaining", body)
        body = self.client.get(reverse("k8s:secrets_export"), {"namespace": "globex-prod"}).content.decode()
        self.assertIn("shop-api-key", body)
        self.assertNotIn("web-db-credentials", body)
        self.assertNotIn("svc-sentinel-user", body)

    def test_overview_shows_the_edge_summary(self):
        self.login(self.viewer)
        response = self.client.get(reverse("k8s:overview"))
        self.assertContains(response, "Edge exposure")
        self.assertContains(response, "TLS coverage")
        self.assertContains(response, "80%")
        self.assertContains(response, "Certificates under 30 days")
        edge = response.context["extra"]["ingresses"]
        self.assertEqual(edge["value"], 5)
        self.assertEqual(response.context["extra"]["certificates"]["value"], 3)

    def test_overview_without_edge_data_shows_no_edge_panel(self):
        Ingress.objects.all().delete()
        Certificate.objects.all().delete()
        self.login(self.viewer)
        self.assertNotContains(self.client.get(reverse("k8s:overview")), "Edge exposure")

    def test_template_never_renders_a_placeholder(self):
        self.login(self.viewer)
        for name in PAGES:
            body = self.client.get(reverse(name)).content.decode()
            for word in ("NOT DISCOVERED", "MANUAL INPUT REQUIRED", "NOT APPLICABLE"):
                self.assertNotIn(word, body)


class ScopingTests(Base):
    """Site scoping and tenant isolation on every page, export and queryset."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        seed(site=MUM)
        # a second site's estate: one more of each
        ns = Namespace.objects.create(tenant_key=T, cluster=_cluster(), name="dr-ns", site=DR, client_label="Acme")
        Ingress.objects.create(tenant_key=T, namespace=ns, site=DR, name="dr-ing", hostname="dr.example.com",
                               tls=True, ip_type="private")
        Certificate.objects.create(tenant_key=T, namespace=ns, site=DR, name="dr-cert", domain="dr.example.com",
                                   expiry=timezone.now() + timedelta(days=5))
        SecretRef.objects.create(tenant_key=T, namespace=ns, site=DR, application="dr-app",
                                 secret_name="dr-secret", secret_namespace="dr-ns")
        # another tenant's rows must never show up
        other_ns = Namespace.objects.create(tenant_key=OTHER, cluster=_cluster_for(OTHER), name="theirs",
                                            site="All Sites")
        Ingress.objects.create(tenant_key=OTHER, namespace=other_ns, name="x", hostname="leak.example.net")
        Certificate.objects.create(tenant_key=OTHER, namespace=other_ns, name="leak-cert",
                                   expiry=timezone.now() + timedelta(days=1))
        SecretRef.objects.create(tenant_key=OTHER, namespace=other_ns, secret_name="leak-secret",
                                 secret_namespace="theirs")

    def test_a_site_user_sees_only_their_site(self):
        self.login(self.dr_viewer)
        body = self.client.get(reverse("k8s:ingresses")).content.decode()
        self.assertIn("dr.example.com", body)
        self.assertNotIn("app.example.com", body)
        self.assertEqual({r["cert"].name for r in self.client.get(reverse("k8s:certificates")).context["rows"]},
                         {"dr-cert"})
        self.assertEqual(self.client.get(reverse("k8s:secrets")).context["total"], 1)
        for name in EXPORTS:
            csv_body = self.client.get(reverse(name)).content.decode()
            self.assertNotIn("example.com\r\napp", csv_body)
            self.assertNotIn("web-db-credentials", csv_body)
            self.assertNotIn("app-cert", csv_body)

    def test_a_global_user_sees_every_site_of_their_tenant_only(self):
        self.login(self.viewer)
        body = self.client.get(reverse("k8s:ingresses")).content.decode()
        self.assertIn("dr.example.com", body)
        self.assertIn("app.example.com", body)
        self.assertNotIn("leak.example.net", body)
        self.assertNotIn("leak-cert", self.client.get(reverse("k8s:certificates")).content.decode())
        self.assertNotIn("leak-secret", self.client.get(reverse("k8s:secrets")).content.decode())
        for name in EXPORTS:
            self.assertNotIn("leak", self.client.get(reverse(name)).content.decode())

    def test_the_site_filter_ignores_a_site_the_user_cannot_see(self):
        self.login(self.dr_viewer)
        response = self.client.get(reverse("k8s:ingresses"), {"site": MUM})
        self.assertEqual(response.context["site_filter"], "")
        self.assertEqual(response.context["totals"]["rules"], 1)
        self.login(self.viewer)
        self.assertEqual(self.client.get(reverse("k8s:ingresses"), {"site": MUM}).context["totals"]["rules"], 5)
        self.assertEqual(self.client.get(reverse("k8s:ingresses"), {"site": DR}).context["totals"]["rules"], 1)

    def test_querysets_are_scoped_by_tenant_and_site(self):
        for fn in (ingresses_for, certificates_for, secrets_for):
            with self.subTest(fn.__name__):
                self.assertTrue(all(r.tenant_key == T for r in fn(self.viewer)))
                self.assertTrue(all(r.site == DR for r in fn(self.dr_viewer)))
                self.assertEqual(len(fn(self.dr_viewer)), 1)

    def test_overview_summary_counts_only_visible_rows(self):
        self.login(self.dr_viewer)
        response = self.client.get(reverse("k8s:overview"))
        self.assertEqual(response.context["extra"]["ingresses"]["value"], 1)
        self.assertEqual(response.context["extra"]["certificates"]["value"], 1)
        self.assertNotContains(response, "app.example.com")

    def test_other_tenant_users_see_nothing_of_ours(self):
        with override_settings(LINKEDEYE_TENANT=OTHER):
            user = User.objects.create_user("outsider", password="pw-outsider-12345")
            Membership.objects.create(user=user, site=None, role="admin")
            self.client.force_login(user)
            body = self.client.get(reverse("k8s:ingresses")).content.decode()
            self.assertIn("leak.example.net", body)
            self.assertNotIn("app.example.com", body)
            self.assertNotIn("app-cert", self.client.get(reverse("k8s:certificates")).content.decode())
            self.assertNotIn("web-db-credentials", self.client.get(reverse("k8s:secrets")).content.decode())


def _cluster_for(tenant):
    from k8s.models import KubeCluster
    cluster, _ = KubeCluster.objects.get_or_create(tenant_key=tenant, name="kubernetes")
    return cluster


class LinkActionTests(Base):
    """POST /kubernetes/ingresses/link/: opt-in, per-site operator rights, audited."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        seed(site=MUM)
        ns = Namespace.objects.create(tenant_key=T, cluster=_cluster(), name="dr-ns", site=DR, client_label="Acme")
        Ingress.objects.create(tenant_key=T, namespace=ns, site=DR, name="dr-ing", hostname="dr.example.com",
                               url="https://dr.example.com", tls=True)

    url = property(lambda self: reverse("k8s:ingresses_link"))

    def test_get_is_not_allowed_and_a_viewer_is_refused(self):
        self.login(self.operator)
        self.assertEqual(self.client.get(self.url).status_code, 405)
        self.login(self.viewer)
        self.client.post(self.url, {"urlchecks": "1"})
        self.assertEqual(UrlCheck.objects.count(), 0)
        self.assertEqual(AuditEvent.objects.filter(action="link").count(), 0)

    def test_nothing_chosen_does_nothing(self):
        self.login(self.operator)
        response = self.client.post(self.url, {})
        self.assertEqual(response.status_code, 302)
        self.assertEqual((UrlCheck.objects.count(), Domain.objects.count()), (0, 0))

    def test_an_operator_creates_disabled_checks_and_unmonitored_domains(self):
        self.login(self.operator)
        response = self.client.post(self.url, {"urlchecks": "1", "domains": "1", "client": "Acme"}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(UrlCheck.objects.filter(enabled=True).count(), 0)
        self.assertEqual(set(UrlCheck.objects.values_list("url", flat=True)),
                         {"https://app.example.com", "https://docs.example.com", "https://dr.example.com"})
        self.assertFalse(Domain.objects.filter(monitor_enabled=True).exists())
        self.assertTrue(Ingress.objects.filter(hostname="dr.example.com", url_check__isnull=False).exists())
        self.assertFalse(Ingress.objects.filter(hostname="shop.example.co.uk", url_check__isnull=False).exists())
        event = AuditEvent.objects.get(action="link")
        self.assertEqual(event.object_type, "k8s.ingress")
        self.assertEqual(event.details["urlchecks_created"], 3)
        self.assertContains(response, "3 URL checks created (disabled)")

    def test_a_second_click_creates_no_duplicates(self):
        self.login(self.operator)
        self.client.post(self.url, {"urlchecks": "1", "domains": "1"})
        counts = (UrlCheck.objects.count(), Domain.objects.count())
        self.client.post(self.url, {"urlchecks": "1", "domains": "1"})
        self.assertEqual((UrlCheck.objects.count(), Domain.objects.count()), counts)

    def test_a_site_operator_cannot_touch_another_site_or_all_sites(self):
        self.login(self.dr_operator)
        response = self.client.post(self.url, {"urlchecks": "1", "domains": "1"}, follow=True)
        self.assertEqual(set(UrlCheck.objects.values_list("url", flat=True)), {"https://dr.example.com"})
        self.assertEqual(UrlCheck.objects.get().site, DR)
        self.assertFalse(Ingress.objects.filter(hostname="app.example.com", url_check__isnull=False).exists())
        self.assertEqual(set(Domain.objects.values_list("hostname", flat=True)), {"example.com"})
        self.assertEqual(Domain.objects.get().site_key, DR)
        self.assertEqual(response.status_code, 200)

    def test_the_form_only_shows_for_operators(self):
        self.login(self.viewer)
        self.assertNotContains(self.client.get(reverse("k8s:ingresses")), "Create / link")
        self.login(self.operator)
        self.assertContains(self.client.get(reverse("k8s:ingresses")), "Create / link")

    def test_csrf_is_enforced(self):
        from django.test import Client
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.operator)
        self.assertEqual(strict.post(self.url, {"urlchecks": "1"}).status_code, 403)
        self.assertEqual(UrlCheck.objects.count(), 0)
