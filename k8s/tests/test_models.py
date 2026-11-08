"""Models, tenancy, the site registry and the tab strip."""
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from administration.services import SITE_NAME_FIELDS
from core.models import Client

from k8s import tabs
from k8s.models import (ClientProfile, Health, ImportRun, ImportStatus, KubeCluster, MappingStatus, Namespace,
                        Node, NodeRole, is_global)

T = "test-tenant"
OTHER = "other-tenant"


def run(tenant=T, **kw):
    kw.setdefault("source_name", "inventory.xlsx")
    return ImportRun.objects.create(tenant_key=tenant, **kw)


def cluster(tenant=T, name="kubernetes", **kw):
    return KubeCluster.objects.create(tenant_key=tenant, name=name, **kw)


def node(cl, hostname="node-1.example.com", tenant=T, **kw):
    return Node.objects.create(tenant_key=tenant, cluster=cl, hostname=hostname, **kw)


@override_settings(LINKEDEYE_TENANT=T)
class ModelTests(TestCase):
    def test_tenant_key_defaults_from_settings(self):
        self.assertEqual(KubeCluster.objects.create(name="k").tenant_key, T)
        self.assertEqual(ImportRun.objects.create(source_name="x").tenant_key, T)

    def test_global_site_helper(self):
        self.assertTrue(is_global(""))
        self.assertTrue(is_global("All Sites"))
        self.assertTrue(is_global(None))
        self.assertFalse(is_global("Mumbai"))

    def test_cluster_name_is_unique_per_tenant_only(self):
        cluster()
        with transaction.atomic(), self.assertRaises(IntegrityError):
            cluster()
        self.assertTrue(cluster(tenant=OTHER).pk)      # the same name in another tenant is fine

    def test_node_hostname_is_unique_per_cluster_and_tenant(self):
        one, two = cluster(name="a"), cluster(name="b")
        node(one)
        with transaction.atomic(), self.assertRaises(IntegrityError):
            node(one)
        self.assertTrue(node(two).pk)
        self.assertTrue(node(cluster(tenant=OTHER, name="a"), tenant=OTHER).pk)

    def test_namespace_is_unique_per_cluster_and_tenant(self):
        one = cluster()
        Namespace.objects.create(tenant_key=T, cluster=one, name="ns")
        with transaction.atomic(), self.assertRaises(IntegrityError):
            Namespace.objects.create(tenant_key=T, cluster=one, name="ns")

    def test_client_profile_is_unique_per_tenant(self):
        ClientProfile.objects.create(tenant_key=T, workbook_name="Acme")
        with transaction.atomic(), self.assertRaises(IntegrityError):
            ClientProfile.objects.create(tenant_key=T, workbook_name="Acme")
        self.assertTrue(ClientProfile.objects.create(tenant_key=OTHER, workbook_name="Acme").pk)

    def test_node_takes_its_site_from_its_cluster(self):
        n = node(cluster(site="Mumbai"))
        self.assertEqual(n.site, "Mumbai")
        self.assertEqual(n.site_label, "Mumbai")
        n.cluster.site = "DR Site"
        n.cluster.save(update_fields=["site"])
        self.assertEqual(Node.objects.get(pk=n.pk).site, "DR Site")

    def test_node_capacity_properties(self):
        n = node(cluster(), cpu_capacity_millicores=16000, cpu_allocatable_millicores=15500,
                 labels={"b": "2", "a": "1"}, role=NodeRole.CONTROL_PLANE)
        self.assertEqual(n.cpu_cores, 16.0)
        self.assertEqual(n.cpu_allocatable_cores, 15.5)
        self.assertTrue(n.is_control_plane)
        self.assertEqual(n.label_pairs, [("a", "1"), ("b", "2")])
        empty = node(cluster(name="z"), hostname="n2.example.com")
        self.assertIsNone(empty.cpu_cores)
        self.assertIsNone(empty.cpu_allocatable_cores)

    def test_memory_bytes_hold_a_very_large_estate(self):
        """Capacity is a BigInteger, so a petabyte-scale sum cannot overflow on PostgreSQL."""
        n = node(cluster(), memory_capacity_bytes=9 * 1024 ** 5, pod_capacity=1000)
        n.refresh_from_db()
        self.assertEqual(n.memory_capacity_bytes, 9 * 1024 ** 5)
        profile = ClientProfile.objects.create(tenant_key=T, workbook_name="Big",
                                                total_storage_bytes=5 * 1024 ** 5)
        profile.refresh_from_db()
        self.assertEqual(profile.total_storage_bytes, 5 * 1024 ** 5)

    def test_missing_flag(self):
        n = node(cluster())
        self.assertFalse(n.is_missing)
        n.missing_since = timezone.now()
        self.assertTrue(n.is_missing)

    def test_namespace_client_display_prefers_the_linked_client(self):
        client = Client.objects.create(tenant_key=T, name="Acme Ltd", code="acme")
        cl = cluster()
        linked = Namespace.objects.create(tenant_key=T, cluster=cl, name="a", client=client,
                                          client_label="Acme")
        self.assertEqual(linked.client_display, "Acme Ltd")
        raw = Namespace.objects.create(tenant_key=T, cluster=cl, name="b", client_label="Globex")
        self.assertEqual(raw.client_display, "Globex")
        none = Namespace.objects.create(tenant_key=T, cluster=cl, name="c",
                                        mapping_status=MappingStatus.REVIEW)
        self.assertEqual(none.client_display, "")
        self.assertTrue(none.needs_review)

    def test_profile_tones_and_display_name(self):
        client = Client.objects.create(tenant_key=T, name="Acme Ltd", code="acme")
        profile = ClientProfile.objects.create(tenant_key=T, workbook_name="Acme", client=client,
                                                status=Health.OK, health=Health.CRIT)
        self.assertEqual(profile.display_name, "Acme Ltd")
        self.assertEqual(profile.status_tone, "ok")
        self.assertEqual(profile.health_tone, "crit")
        self.assertEqual(profile.worst_tone, "crit")
        plain = ClientProfile.objects.create(tenant_key=T, workbook_name="Globex")
        self.assertEqual(plain.display_name, "Globex")
        self.assertEqual(plain.worst_tone, "mute")
        self.assertEqual(plain.links, [("Application", ""), ("Monitoring", ""), ("CI/CD", "")])

    def test_import_run_helpers(self):
        started = timezone.now()
        r = run(started_at=started, finished_at=started + timezone.timedelta(milliseconds=250),
                status=ImportStatus.OK,
                counts={"03_Node_Inventory": {"rows": 3, "created": 3}, "x": "not a dict"})
        self.assertEqual(r.total_rows, 3)
        self.assertAlmostEqual(r.duration_ms, 250, places=3)
        self.assertEqual(r.sheet_count("03_Node_Inventory"), 3)
        self.assertEqual(r.sheet_count("03_Node_Inventory", "created"), 3)
        self.assertIsNone(r.sheet_count("nope"))
        self.assertEqual([name for name, _ in r.sheet_rows], ["03_Node_Inventory"])
        self.assertIsNone(run().duration_ms)

    def test_import_run_keeps_no_field_that_could_hold_a_secret_value(self):
        """The workbook carries Secret names only; nothing in this module may hold a value."""
        forbidden = {"secret", "secret_value", "password", "token", "credential", "api_key", "value"}
        for model in (ImportRun, KubeCluster, Node, Namespace, ClientProfile):
            names = {f.name for f in model._meta.get_fields()}
            self.assertEqual(names & forbidden, set(), model.__name__)


@override_settings(LINKEDEYE_TENANT=T)
class TenantIsolationTests(TestCase):
    def test_querysets_never_cross_tenants(self):
        mine = cluster()
        theirs = cluster(tenant=OTHER, name="theirs")
        node(mine)
        node(theirs, tenant=OTHER)
        Namespace.objects.create(tenant_key=T, cluster=mine, name="ns")
        Namespace.objects.create(tenant_key=OTHER, cluster=theirs, name="ns")
        ClientProfile.objects.create(tenant_key=T, workbook_name="Acme")
        ClientProfile.objects.create(tenant_key=OTHER, workbook_name="Acme")
        for model in (KubeCluster, Node, Namespace, ClientProfile):
            self.assertEqual(model.objects.filter(tenant_key=T).count(), 1, model.__name__)
            self.assertEqual(model.objects.filter(tenant_key=OTHER).count(), 1, model.__name__)


class SiteRegistryTests(TestCase):
    def test_every_model_with_a_site_field_is_registered_for_site_renames(self):
        registered = {(app, model) for app, model, _field, _label in SITE_NAME_FIELDS}
        for model in (KubeCluster, Namespace, ClientProfile, ImportRun):
            names = {f.name for f in model._meta.get_fields()}
            self.assertIn("site", names, model.__name__)
            self.assertIn(("k8s", model.__name__), registered, model.__name__)

    def test_models_without_a_site_field_are_not_registered(self):
        registered = {(app, model) for app, model, _field, _label in SITE_NAME_FIELDS}
        self.assertNotIn("site", {f.name for f in Node._meta.get_fields()})
        self.assertNotIn(("k8s", "Node"), registered)

    def test_site_rename_follows_the_imported_records(self):
        from administration.services import rename_site_references
        with override_settings(LINKEDEYE_TENANT=T):
            cluster(site="Mumbai")
            Namespace.objects.create(tenant_key=T, cluster=KubeCluster.objects.get(), name="ns",
                                     site="Mumbai")
            ClientProfile.objects.create(tenant_key=T, workbook_name="Acme", site="Mumbai")
            rename_site_references("Mumbai", "Mumbai (NSE)")
            self.assertEqual(KubeCluster.objects.get().site, "Mumbai (NSE)")
            self.assertEqual(Namespace.objects.get().site, "Mumbai (NSE)")
            self.assertEqual(ClientProfile.objects.get().site, "Mumbai (NSE)")


class TabTests(TestCase):
    def test_only_built_pages_are_in_the_strip(self):
        keys = [t["key"] for t in tabs.enabled_tabs()]
        self.assertEqual(keys, ["overview", "clients", "namespaces", "nodes", "imports"])
        self.assertTrue(tabs.is_enabled("nodes"))

    def test_pending_tabs_cover_the_remaining_sheets(self):
        pending = {t["key"] for t in tabs.pending_tabs()}
        self.assertEqual(pending, {"workloads", "services", "ingresses", "volumes", "certificates", "tools",
                                   "issues"})
        for tab in tabs.pending_tabs():
            self.assertFalse(tabs.is_enabled(tab["key"]))

    def test_every_enabled_tab_resolves(self):
        from django.urls import reverse
        for tab in tabs.enabled_tabs():
            self.assertTrue(reverse(tab["url"]).startswith("/kubernetes/"), tab["url"])

    def test_tab_keys_and_urls_are_unique(self):
        keys = [t["key"] for t in tabs.TABS]
        urls = [t["url"] for t in tabs.TABS]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(len(urls), len(set(urls)))
