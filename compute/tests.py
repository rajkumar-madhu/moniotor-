"""Compute module tests: models/validation, forms, per-site access, every view, CSV, seed, metrics and the
kube-state-metrics parsing/job with core.prometheus mocked."""
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse

from core import jobs as core_jobs
from core.models import AuditEvent, Event, Membership, Site
from core.prometheus import PrometheusError
from network.models import Device

from . import jobs, k8s, metrics, services
from .forms import ClusterForm, MemberForm, PlacementForm
from .models import Cluster, ClusterMember, VMPlacement, format_selector, parse_selector

T = "default"
MUM = "Mumbai"
DR = "DR Site"
PROM = override_settings(PROMETHEUS_URL="http://prom.example:9090")


def dev(name, dtype="physical_server", site=MUM, **kw):
    kw.setdefault("tenant_key", T)
    kw.setdefault("mgmt_ip", "192.0.2.10")
    return Device.objects.create(name=name, device_type=dtype, site=site, **kw)


def row(value, **labels):
    return {"metric": labels, "value": value}


@override_settings(LINKEDEYE_TENANT=T)
class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.mum = Site.objects.create(tenant_key=T, name=MUM, code="mum")
        cls.dr = Site.objects.create(tenant_key=T, name=DR, code="dr")
        cls.viewer = User.objects.create_user("viewer", password="pw")
        Membership.objects.create(user=cls.viewer, site=None, role="viewer")
        cls.operator = User.objects.create_user("operator", password="pw")
        Membership.objects.create(user=cls.operator, site=None, role="operator")
        cls.admin = User.objects.create_user("admin", password="pw")
        Membership.objects.create(user=cls.admin, site=None, role="admin")
        cls.dr_op = User.objects.create_user("dr-op", password="pw")
        Membership.objects.create(user=cls.dr_op, site=cls.dr, role="operator")
        cls.dr_admin = User.objects.create_user("dr-admin", password="pw")
        Membership.objects.create(user=cls.dr_admin, site=cls.dr, role="admin")

        cls.esx1 = dev("esx-01", prometheus_instance="192.0.2.10:9100")
        cls.esx2 = dev("esx-02", mgmt_ip="192.0.2.11", prometheus_instance="192.0.2.11")
        cls.dr_host = dev("dr-host-01", site=DR, mgmt_ip="192.0.2.20")
        cls.vm1 = dev("web-01", "virtual_machine", mgmt_ip="192.0.2.101")
        cls.vm2 = dev("db-01", "virtual_machine", mgmt_ip="192.0.2.102")
        cls.dr_vm = dev("dr-web-01", "virtual_machine", site=DR, mgmt_ip="192.0.2.120")
        cls.other_tenant = Device.objects.create(tenant_key="other", name="foreign", device_type="physical_server",
                                                 mgmt_ip="192.0.2.200", site=MUM)
        cls.vsphere = Cluster.objects.create(tenant_key=T, name="vsphere-mum", kind="vmware", site=MUM,
                                             prometheus_selector='cluster="vsphere-mum"')
        cls.dr_cluster = Cluster.objects.create(tenant_key=T, name="pve-dr", kind="proxmox", site=DR)
        cls.k8s = Cluster.objects.create(tenant_key=T, name="k8s-mum", kind="kubernetes", site=MUM,
                                         prometheus_selector='cluster="k8s-mum"')
        cls.global_cluster = Cluster.objects.create(tenant_key=T, name="global-hv", kind="other", site="All Sites")
        Cluster.objects.create(tenant_key="other", name="foreign-cluster", kind="vmware", site=MUM)
        cls.m1 = ClusterMember.objects.create(tenant_key=T, cluster=cls.vsphere, device=cls.esx1, role="host",
                                              cpu_cores=32, memory_gb=256)
        cls.m2 = ClusterMember.objects.create(tenant_key=T, cluster=cls.vsphere, device=cls.esx2, role="host")
        cls.dr_m = ClusterMember.objects.create(tenant_key=T, cluster=cls.dr_cluster, device=cls.dr_host, role="host",
                                                cpu_cores=16, memory_gb=64)
        cls.p1 = VMPlacement.objects.create(tenant_key=T, vm=cls.vm1, host=cls.esx1, cluster=cls.vsphere, vcpus=8,
                                            memory_gb=32, disk_gb=100, power_state="running", os="Ubuntu")
        cls.p2 = VMPlacement.objects.create(tenant_key=T, vm=cls.vm2, host=cls.esx1, cluster=cls.vsphere, vcpus=16,
                                            memory_gb=64, power_state="stopped")
        cls.dr_p = VMPlacement.objects.create(tenant_key=T, vm=cls.dr_vm, host=cls.dr_host, cluster=cls.dr_cluster,
                                              vcpus=4, memory_gb=8, power_state="suspended")

    def setUp(self):
        cache.clear()

    def login(self, user):
        self.client.force_login(user)
        return self.client


# ---------------------------------------------------------------------------------------------- models
class SelectorTests(TestCase):
    def test_accepts_plain_pairs(self):
        self.assertEqual(parse_selector('cluster="prod-k8s", env="prod"'), [("cluster", "prod-k8s"), ("env", "prod")])
        self.assertEqual(parse_selector('{cluster="a"}'), [("cluster", "a")])
        self.assertEqual(parse_selector(""), [])
        self.assertEqual(format_selector([("a", "b"), ("c", "d e")]), 'a="b",c="d e"')

    def test_rejects_injection_and_other_matchers(self):
        bad = ['cluster="a"} or vector(1) {x="y"', 'cluster=~"a.*"', 'cluster!="a"', 'cluster="a\\"b"',
               '__name__="up"', 'cluster="a", cluster="b"', 'cluster="a",', 'cluster=a', 'cluster="a" env="b"',
               'x="a{b}"', 'x="line\nbreak"', "x=\"" + "a" * 121 + '"']
        for value in bad:
            with self.assertRaises(ValidationError, msg=value):
                parse_selector(value)
        with self.assertRaises(ValidationError):
            parse_selector(",".join(f'l{i}="v"' for i in range(7)))

    def test_model_validator_runs_on_full_clean(self):
        c = Cluster(name="x", kind="kubernetes", prometheus_selector='a=~"b"')
        with self.assertRaises(ValidationError):
            c.full_clean()


class ModelTests(Base):
    def test_properties(self):
        self.assertTrue(self.k8s.is_kubernetes)
        self.assertFalse(self.vsphere.is_kubernetes)
        self.assertEqual(self.k8s.selector_pairs, [("cluster", "k8s-mum")])
        c = Cluster(name="t", tags="Example, prod ,", prometheus_selector="broken")
        self.assertEqual(c.tag_list, ["Example", "prod"])
        self.assertTrue(c.is_example)
        self.assertEqual(c.selector_pairs, [])
        self.assertEqual(self.vsphere.get_absolute_url(), reverse("compute:cluster_detail", args=[self.vsphere.pk]))
        self.assertEqual(str(self.m1), "esx-01 in vsphere-mum")
        self.assertEqual(str(self.p1), "web-01 placement")

    def test_unique_constraints_include_tenant(self):
        Cluster.objects.create(tenant_key="other", name="vsphere-mum", kind="vmware")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Cluster.objects.create(tenant_key=T, name="vsphere-mum", kind="vmware")
        with self.assertRaises(IntegrityError), transaction.atomic():
            ClusterMember.objects.create(tenant_key=T, cluster=self.vsphere, device=self.esx1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            VMPlacement.objects.create(tenant_key=T, vm=self.vm1)
        VMPlacement.objects.create(tenant_key="other", vm=self.vm1)  # another tenant may hold the same vm key

    def test_devices_are_not_duplicated(self):
        self.assertEqual(Device.objects.filter(tenant_key=T, device_type="physical_server").count(), 3)
        self.assertEqual(services.servers_for(self.admin).count(), 3)
        self.assertEqual(services.vms_for(self.admin).count(), 3)


# ---------------------------------------------------------------------------------------------- services
class ServiceTests(Base):
    def test_scoping_by_site(self):
        self.assertEqual(set(services.clusters_for(self.dr_op).values_list("name", flat=True)), {"pve-dr", "global-hv"})
        self.assertEqual(list(services.members_for(self.dr_op).values_list("pk", flat=True)), [self.dr_m.pk])
        self.assertEqual(list(services.placements_for(self.dr_op).values_list("pk", flat=True)), [self.dr_p.pk])
        self.assertEqual(services.clusters_for(self.admin).count(), 4)  # never the other tenant's

    def test_can_manage(self):
        self.assertTrue(services.can_manage(self.operator, "operate", MUM))
        self.assertTrue(services.can_manage(self.dr_op, "operate", DR))
        self.assertFalse(services.can_manage(self.dr_op, "operate", MUM))
        self.assertFalse(services.can_manage(self.dr_admin, "admin", "All Sites"))  # global needs tenant-wide
        self.assertTrue(services.can_manage(self.admin, "admin", "All Sites"))
        self.assertFalse(services.can_manage(self.viewer, "operate", MUM))
        self.assertEqual(services.manageable_sites(self.dr_op), [DR])
        self.assertEqual(services.manageable_sites(self.operator)[0], "All Sites")

    def test_capacity_and_overcommit(self):
        cap = services.cluster_capacity(self.vsphere, self.admin)
        self.assertEqual(cap["vms"], 2)
        self.assertEqual(cap["running"], 1)
        self.assertEqual(cap["vcpus"], 8)  # stopped VM holds nothing
        self.assertEqual(cap["memory_gb"], Decimal(32))
        self.assertEqual(cap["cores"], 32)
        self.assertEqual(cap["cpu_known"], 1)
        self.assertAlmostEqual(cap["cpu_ratio"], 8 / 32)
        self.assertAlmostEqual(cap["mem_ratio"], 32 / 256)
        cap = services.cluster_capacity(self.vsphere, self.admin, host_metrics={self.esx2.pk: {"cores": 8, "mem_total_gb": 64.0}})
        self.assertEqual(cap["cores"], 40)
        self.assertEqual(cap["cpu_known"], 2)
        m = [x for x in cap["members"] if x.device_id == self.esx2.pk][0]
        self.assertEqual(m.capacity_source, "node_exporter")
        self.assertIsNone(services.ratio(1, 0))
        rows = services.cluster_rows(self.admin, [self.vsphere, self.k8s])
        self.assertEqual((rows[0].member_count, rows[0].vm_count, rows[0].running, rows[0].vcpus), (2, 2, 1, 8))
        self.assertEqual(rows[1].vm_count, 0)
        self.assertEqual(services.allocation_by_host(self.admin)[self.esx1.pk]["vms"], 2)

    def test_csv_safe(self):
        self.assertEqual(services.csv_safe("=SUM(A1)"), "'=SUM(A1)")
        self.assertEqual(services.csv_safe("-5"), "-5")
        self.assertEqual(services.csv_safe("+cmd"), "'+cmd")
        self.assertEqual(services.csv_safe(None), "")
        self.assertEqual(services.csv_safe("plain"), "plain")


# ---------------------------------------------------------------------------------------------- forms
class FormTests(Base):
    def test_cluster_form_cleans_selector_and_site(self):
        f = ClusterForm({"name": "New", "kind": "vmware", "site": MUM, "prometheus_selector": ' { cluster = "a" , env="b" } ',
                         "tags": "a, a, b"}, user=self.operator)
        self.assertTrue(f.is_valid(), f.errors)
        self.assertEqual(f.cleaned_data["prometheus_selector"], 'cluster="a",env="b"')
        self.assertEqual(f.cleaned_data["tags"], "a, b")
        f = ClusterForm({"name": "VSPHERE-MUM", "kind": "vmware", "site": MUM, "prometheus_selector": 'a=~"b"'}, user=self.operator)
        self.assertFalse(f.is_valid())
        self.assertIn("name", f.errors)
        self.assertIn("prometheus_selector", f.errors)
        f = ClusterForm({"name": "x", "kind": "vmware", "site": MUM}, user=self.dr_op)
        self.assertFalse(f.is_valid())
        self.assertIn("site", f.errors)

    def test_cluster_form_refuses_moves_that_strand_members(self):
        f = ClusterForm({"name": "vsphere-mum", "kind": "vmware", "site": DR}, instance=self.vsphere, user=self.admin)
        self.assertFalse(f.is_valid())
        self.assertIn("Members on other sites", str(f.errors["site"]))
        f = ClusterForm({"name": "vsphere-mum", "kind": "kubernetes", "site": MUM}, instance=self.vsphere, user=self.admin)
        self.assertFalse(f.is_valid())
        self.assertIn("kind", f.errors)

    def test_member_form_rules(self):
        f = MemberForm({"device": self.vm1.pk, "role": "host"}, cluster=self.vsphere, user=self.admin)
        self.assertFalse(f.is_valid())  # VM not offered for a hypervisor cluster
        f = MemberForm({"device": self.dr_host.pk, "role": "host"}, cluster=self.vsphere, user=self.admin)
        self.assertFalse(f.is_valid())  # other site
        f = MemberForm({"device": self.esx1.pk, "role": "host"}, cluster=self.vsphere, user=self.admin)
        self.assertFalse(f.is_valid())  # duplicate
        f = MemberForm({"device": self.esx1.pk, "role": "host"}, cluster=self.global_cluster, user=self.admin)
        self.assertFalse(f.is_valid())  # already a host of another hypervisor cluster
        f = MemberForm({"device": self.esx1.pk, "role": "worker"}, cluster=self.k8s, user=self.admin)
        self.assertTrue(f.is_valid(), f.errors)  # k8s may share the server
        f = MemberForm({"device": self.vm1.pk, "role": "worker"}, cluster=self.k8s, user=self.admin)
        self.assertTrue(f.is_valid(), f.errors)  # VMs may be k8s nodes

    def test_placement_form_rules(self):
        f = PlacementForm({"host": self.esx1.pk, "cluster": self.k8s.pk, "power_state": "running"}, vm=self.vm1, user=self.admin)
        self.assertFalse(f.is_valid())
        self.assertIn("cluster", f.errors)
        f = PlacementForm({"host": self.esx1.pk, "cluster": self.dr_cluster.pk, "power_state": "running"}, vm=self.vm1, user=self.admin)
        self.assertFalse(f.is_valid())  # DR cluster not offered for a Mumbai VM
        f = PlacementForm({"host": self.esx1.pk, "cluster": self.global_cluster.pk, "power_state": "running"}, vm=self.vm1, user=self.admin)
        self.assertFalse(f.is_valid())
        self.assertIn("is not a member", str(f.errors["host"]))
        f = PlacementForm({"host": self.esx1.pk, "power_state": "running", "vcpus": 64}, vm=self.vm1, user=self.admin)
        self.assertFalse(f.is_valid())
        self.assertIn("vcpus", f.errors)
        f = PlacementForm({"host": self.esx1.pk, "power_state": "running", "vcpus": 4, "memory_gb": "8"}, vm=self.vm1, user=self.admin)
        self.assertTrue(f.is_valid(), f.errors)
        self.assertEqual(f.cleaned_data["cluster"], self.vsphere)  # filled from the host's cluster


# ---------------------------------------------------------------------------------------------- views
class ViewTests(Base):
    PAGES = ["compute:overview", "compute:servers", "compute:vms", "compute:clusters", "compute:kubernetes"]

    def test_sidebar_names_resolve_and_render_for_viewer(self):
        c = self.login(self.viewer)
        for name in self.PAGES:
            r = c.get(reverse(name))
            self.assertEqual(r.status_code, 200, name)
            self.assertContains(r, 'class="pg-compute"')
        r = c.get(reverse("compute:cluster_detail", args=[self.vsphere.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "0.25 : 1")
        self.assertNotContains(r, 'action="' + reverse("compute:member_add", args=[self.vsphere.pk]))
        r = c.get(reverse("compute:cluster_detail", args=[self.k8s.pk]))
        self.assertContains(r, "Prometheus is not connected")

    def test_overview_without_prometheus_is_honest(self):
        r = self.login(self.admin).get(reverse("compute:overview"))
        self.assertContains(r, "Prometheus not connected")
        self.assertContains(r, "1 VMware vSphere")

    def test_overview_counts(self):
        r = self.login(self.admin).get(reverse("compute:overview"))
        self.assertEqual(r.context["srv"]["total"], 3)
        self.assertEqual(r.context["vms_total"], 3)
        self.assertEqual(r.context["alloc_vcpus"], 12)  # 8 running + 4 suspended
        self.assertEqual(r.context["kinds_text"], "1 VMware vSphere · 1 Proxmox VE · 1 Kubernetes · 1 Other")
        self.assertEqual(r.context["power_donut"]["segments"][0]["value"], 1)
        self.assertEqual(r.context["site_bars"]["tipLabels"], [MUM, DR])

    def test_servers_page_filters_and_links(self):
        c = self.login(self.admin)
        r = c.get(reverse("compute:servers"))
        self.assertEqual(r.context["page"].paginator.count, 3)
        self.assertContains(r, reverse("network:detail", args=[self.esx1.pk]))
        self.assertContains(r, "Prometheus is not connected")
        self.assertFalse(r.context["prom_on"])
        self.assertEqual(c.get(reverse("compute:servers"), {"guests": "with"}).context["page"].paginator.count, 2)
        self.assertEqual(c.get(reverse("compute:servers"), {"cluster": "none"}).context["page"].paginator.count, 0)
        self.assertEqual(c.get(reverse("compute:servers"), {"cluster": self.dr_cluster.pk}).context["page"].paginator.count, 1)
        self.assertEqual(c.get(reverse("compute:servers"), {"site": DR}).context["page"].paginator.count, 1)
        self.assertEqual(c.get(reverse("compute:servers"), {"metrics": "none"}).context["page"].paginator.count, 1)
        self.assertEqual(c.get(reverse("compute:servers"), {"q": "esx-0"}).context["page"].paginator.count, 2)
        self.assertEqual(c.get(reverse("compute:servers"), {"per": "x", "page": "9"}).status_code, 200)

    def test_vms_page_filters(self):
        c = self.login(self.admin)
        r = c.get(reverse("compute:vms"))
        self.assertEqual(r.context["page"].paginator.count, 3)
        self.assertContains(r, "esx-01")
        self.assertEqual(c.get(reverse("compute:vms"), {"power": "stopped"}).context["page"].paginator.count, 1)
        self.assertEqual(c.get(reverse("compute:vms"), {"host": self.esx1.pk}).context["page"].paginator.count, 2)
        self.assertEqual(c.get(reverse("compute:vms"), {"cluster": self.dr_cluster.pk}).context["page"].paginator.count, 1)
        self.assertEqual(c.get(reverse("compute:vms"), {"q": "ubuntu"}).context["page"].paginator.count, 1)
        VMPlacement.objects.filter(pk=self.p2.pk).delete()
        self.assertEqual(c.get(reverse("compute:vms"), {"power": "none"}).context["page"].paginator.count, 1)

    def test_clusters_page_filters(self):
        c = self.login(self.admin)
        r = c.get(reverse("compute:clusters"))
        self.assertEqual(r.context["page"].paginator.count, 4)
        self.assertEqual(c.get(reverse("compute:clusters"), {"kind": "kubernetes"}).context["page"].paginator.count, 1)
        self.assertEqual(c.get(reverse("compute:clusters"), {"q": "vsphere"}).context["page"].paginator.count, 1)
        self.assertEqual(c.get(reverse("compute:clusters"), {"site": DR}).context["page"].paginator.count, 1)

    def test_site_scoped_user_sees_only_their_site(self):
        c = self.login(self.dr_op)
        self.assertEqual(c.get(reverse("compute:servers")).context["page"].paginator.count, 1)
        self.assertEqual(c.get(reverse("compute:vms")).context["page"].paginator.count, 1)
        self.assertEqual(c.get(reverse("compute:clusters")).context["page"].paginator.count, 2)
        self.assertEqual(c.get(reverse("compute:cluster_detail", args=[self.vsphere.pk])).status_code, 404)
        self.assertEqual(c.get(reverse("compute:cluster_detail", args=[self.dr_cluster.pk])).status_code, 200)
        self.assertEqual(c.get(reverse("compute:placement_edit", args=[self.vm1.pk])).status_code, 404)
        self.assertEqual(c.get(reverse("compute:member_edit", args=[self.m1.pk])).status_code, 404)
        self.assertEqual(c.get(reverse("compute:cluster_edit", args=[self.global_cluster.pk])).status_code, 403)

    def test_other_tenant_and_unknown_are_404(self):
        c = self.login(self.admin)
        foreign = Cluster.objects.get(name="foreign-cluster")
        self.assertEqual(c.get(reverse("compute:cluster_detail", args=[foreign.pk])).status_code, 404)
        self.assertEqual(c.get(reverse("compute:cluster_detail", args=[99999])).status_code, 404)
        self.assertEqual(c.get(reverse("compute:placement_edit", args=[self.other_tenant.pk])).status_code, 404)
        self.assertEqual(c.get(reverse("compute:placement_edit", args=[self.esx1.pk])).status_code, 404)  # not a VM

    def test_viewer_cannot_mutate(self):
        c = self.login(self.viewer)
        self.assertEqual(c.get(reverse("compute:cluster_create")).status_code, 403)
        self.assertEqual(c.get(reverse("compute:cluster_edit", args=[self.vsphere.pk])).status_code, 403)
        self.assertEqual(c.get(reverse("compute:placement_edit", args=[self.vm1.pk])).status_code, 403)
        r = c.post(reverse("compute:cluster_delete", args=[self.vsphere.pk]))
        self.assertEqual(r.status_code, 302)
        self.assertTrue(Cluster.objects.filter(pk=self.vsphere.pk).exists())
        r = c.post(reverse("compute:cluster_create"), {"name": "x", "kind": "vmware", "site": MUM}, HTTP_X_CSRFTOKEN="x")
        self.assertEqual(r.status_code, 403)
        self.assertFalse(Cluster.objects.filter(name="x").exists())

    def test_cluster_crud_with_audit(self):
        c = self.login(self.operator)
        self.assertEqual(c.get(reverse("compute:cluster_create") + "?kind=kubernetes").status_code, 200)
        r = c.post(reverse("compute:cluster_create"), {"name": "hv-new", "kind": "hyperv", "site": MUM,
                                                       "prometheus_selector": 'cluster="hv"', "tags": "t"})
        obj = Cluster.objects.get(name="hv-new")
        self.assertRedirects(r, obj.get_absolute_url())
        self.assertEqual(obj.tenant_key, T)
        self.assertTrue(AuditEvent.objects.filter(action="create", object_id=str(obj.pk), object_type="compute.cluster").exists())
        r = c.post(reverse("compute:cluster_create"), {"name": "bad", "kind": "vmware", "site": MUM, "prometheus_selector": 'x=~"y"'})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(Cluster.objects.filter(name="bad").exists())
        # edit: a changed selector resets the cached k8s state
        Cluster.objects.filter(pk=self.k8s.pk).update(k8s_state="warning", k8s_summary={"x": 1}, k8s_error="e")
        r = c.post(reverse("compute:cluster_edit", args=[self.k8s.pk]), {"name": "k8s-mum", "kind": "kubernetes", "site": MUM,
                                                                           "prometheus_selector": 'cluster="new"'})
        self.assertEqual(r.status_code, 302)
        self.k8s.refresh_from_db()
        self.assertEqual((self.k8s.k8s_state, self.k8s.k8s_summary, self.k8s.k8s_error), ("unknown", {}, ""))
        ev = AuditEvent.objects.filter(action="update", object_id=str(self.k8s.pk)).latest("pk")
        self.assertIn("prometheus_selector", ev.details["changed"])
        # operator cannot delete; admin can
        r = c.post(reverse("compute:cluster_delete", args=[obj.pk]))
        self.assertEqual(r.status_code, 302)
        self.assertTrue(Cluster.objects.filter(pk=obj.pk).exists())
        r = self.login(self.admin).post(reverse("compute:cluster_delete", args=[self.vsphere.pk]))
        self.assertRedirects(r, reverse("compute:clusters"))
        self.assertFalse(Cluster.objects.filter(pk=self.vsphere.pk).exists())
        self.p1.refresh_from_db()
        self.assertIsNone(self.p1.cluster)  # placements keep their host, lose the cluster
        self.assertEqual(self.p1.host_id, self.esx1.pk)
        self.assertTrue(AuditEvent.objects.filter(action="delete", object_repr="vsphere-mum").exists())

    def test_foreign_site_users_cannot_touch_other_sites(self):
        c = self.login(self.dr_admin)
        r = c.post(reverse("compute:cluster_create"), {"name": "mum-new", "kind": "vmware", "site": MUM})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(Cluster.objects.filter(name="mum-new").exists())
        r = c.post(reverse("compute:cluster_create"), {"name": "dr-new", "kind": "vmware", "site": DR})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(c.post(reverse("compute:cluster_delete", args=[self.vsphere.pk])).status_code, 404)
        self.assertEqual(c.post(reverse("compute:cluster_delete", args=[self.global_cluster.pk])).status_code, 403)
        self.assertEqual(c.post(reverse("compute:member_delete", args=[self.m1.pk])).status_code, 404)
        self.assertEqual(c.post(reverse("compute:placement_delete", args=[self.p1.pk])).status_code, 404)
        self.assertEqual(c.post(reverse("compute:cluster_delete", args=[self.dr_cluster.pk])).status_code, 302)
        self.assertFalse(Cluster.objects.filter(pk=self.dr_cluster.pk).exists())

    def test_members(self):
        c = self.login(self.operator)
        self.assertEqual(c.get(reverse("compute:member_add", args=[self.k8s.pk])).status_code, 302)  # GET never mutates
        self.assertEqual(ClusterMember.objects.filter(cluster=self.k8s).count(), 0)
        r = c.post(reverse("compute:member_add", args=[self.k8s.pk]), {"device": self.esx1.pk, "role": "control-plane", "cpu_cores": 8})
        self.assertRedirects(r, self.k8s.get_absolute_url() + "#members")
        m = ClusterMember.objects.get(cluster=self.k8s, device=self.esx1)
        self.assertEqual(m.tenant_key, T)
        self.assertTrue(AuditEvent.objects.filter(action="create", object_id=str(m.pk)).exists())
        r = c.post(reverse("compute:member_add", args=[self.vsphere.pk]), {"device": self.vm1.pk, "role": "host"}, follow=True)
        self.assertContains(r, "Member not added")
        self.assertEqual(c.get(reverse("compute:member_edit", args=[m.pk])).status_code, 200)
        r = c.post(reverse("compute:member_edit", args=[m.pk]), {"device": self.esx1.pk, "role": "worker", "cpu_cores": 12})
        self.assertEqual(r.status_code, 302)
        m.refresh_from_db()
        self.assertEqual((m.role, m.cpu_cores), ("worker", 12))
        self.assertEqual(c.post(reverse("compute:member_delete", args=[m.pk])).status_code, 302)
        self.assertTrue(ClusterMember.objects.filter(pk=m.pk).exists())
        self.assertEqual(self.login(self.admin).post(reverse("compute:member_delete", args=[m.pk])).status_code, 302)
        self.assertFalse(ClusterMember.objects.filter(pk=m.pk).exists())

    def test_placements(self):
        c = self.login(self.operator)
        VMPlacement.objects.filter(pk=self.p2.pk).delete()
        self.assertEqual(c.get(reverse("compute:placement_edit", args=[self.vm2.pk])).status_code, 200)
        r = c.post(reverse("compute:placement_edit", args=[self.vm2.pk]), {"host": self.esx1.pk, "power_state": "running", "vcpus": 2,
                                                                            "memory_gb": "4", "disk_gb": "40", "os": "Debian",
                                                                            "next": "/compute/vms/?site=Mumbai"})
        self.assertRedirects(r, "/compute/vms/?site=Mumbai", fetch_redirect_response=False)
        p = VMPlacement.objects.get(vm=self.vm2)
        self.assertEqual((p.tenant_key, p.cluster_id), (T, self.vsphere.pk))
        self.assertTrue(AuditEvent.objects.filter(action="create", object_id=str(p.pk)).exists())
        r = c.post(reverse("compute:placement_edit", args=[self.vm2.pk]), {"host": self.esx1.pk, "power_state": "stopped", "vcpus": 2,
                                                                            "memory_gb": "4", "next": "http://evil.example/"})
        self.assertRedirects(r, reverse("compute:vms"))
        ev = Event.objects.get(source="compute", fingerprint=f"compute:vm:{self.vm2.pk}")
        self.assertEqual(ev.severity, "info")
        self.assertEqual(ev.site, MUM)
        r = c.post(reverse("compute:placement_edit", args=[self.vm2.pk]), {"host": self.esx1.pk, "power_state": "stopped", "vcpus": 999})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(c.post(reverse("compute:placement_delete", args=[p.pk])).status_code, 302)
        self.assertTrue(VMPlacement.objects.filter(pk=p.pk).exists())
        r = self.login(self.admin).post(reverse("compute:placement_delete", args=[p.pk]), {"next": reverse("compute:placement_edit", args=[self.vm2.pk])})
        self.assertRedirects(r, reverse("compute:vms"))
        self.assertFalse(VMPlacement.objects.filter(pk=p.pk).exists())
        self.assertTrue(Device.objects.filter(pk=self.vm2.pk).exists())

    def test_refresh_requires_prometheus(self):
        c = self.login(self.operator)
        r = c.post(reverse("compute:cluster_refresh", args=[self.k8s.pk]), follow=True)
        self.assertContains(r, "Prometheus is not connected")
        r = c.post(reverse("compute:cluster_refresh", args=[self.vsphere.pk]), follow=True)
        self.assertContains(r, "Only Kubernetes clusters")


# ---------------------------------------------------------------------------------------------- csv
class CsvTests(Base):
    def test_exports_are_scoped_and_formula_safe(self):
        dev("=HYPERLINK(\"x\")", "virtual_machine", mgmt_ip="192.0.2.150")
        c = self.login(self.dr_op)
        r = c.get(reverse("compute:servers_export"))
        self.assertEqual(r["Content-Type"], "text/csv; charset=utf-8")
        lines = r.content.decode().splitlines()
        self.assertEqual(lines[0].split(",")[:4], ["name", "mgmt_ip", "site", "status"])
        self.assertEqual(len(lines), 2)
        self.assertIn("dr-host-01", lines[1])
        r = self.login(self.admin).get(reverse("compute:vms_export"))
        body = r.content.decode()
        self.assertIn("'=HYPERLINK", body)
        self.assertIn("web-01,192.0.2.101,Mumbai,unknown,Running,esx-01,vsphere-mum,8,32,100,Ubuntu", body)
        r = self.client.get(reverse("compute:clusters_export"), {"kind": "vmware"})
        lines = r.content.decode().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[1].startswith('vsphere-mum,VMware vSphere,Mumbai,2,2,1,8,32,"cluster=""vsphere-mum"""'))


# ---------------------------------------------------------------------------------------------- seed
@override_settings(LINKEDEYE_TENANT=T)
class SeedTests(TestCase):
    def test_seed_is_idempotent_and_removable(self):
        out = StringIO()
        call_command("seed_compute", stdout=out)
        self.assertIn("9 servers, 11 VMs, 3 clusters, 8 members, 10 placements created", out.getvalue())
        servers = Device.objects.filter(tenant_key=T, device_type="physical_server").count()
        call_command("seed_compute", stdout=StringIO())
        self.assertEqual(Device.objects.filter(tenant_key=T, device_type="physical_server").count(), servers)
        self.assertEqual(Cluster.objects.filter(tenant_key=T).count(), 3)
        self.assertEqual(VMPlacement.objects.filter(tenant_key=T).count(), 10)
        self.assertTrue(all(c.is_example for c in Cluster.objects.all()))
        self.assertTrue(all(d.is_example for d in Device.objects.filter(tenant_key=T)))
        self.assertEqual(Cluster.objects.get(name="ex-k8s-mumbai").k8s_state, "unknown")  # nothing invented
        real = Device.objects.get(name="ex-esx-01")
        real.tags = "production"
        real.save()
        out = StringIO()
        call_command("seed_compute", stdout=out)
        self.assertIn("Skipped ex-esx-01", out.getvalue())
        call_command("seed_compute", "--remove", stdout=StringIO())
        self.assertEqual(Cluster.objects.count(), 0)
        self.assertEqual(Device.objects.filter(tenant_key=T).count(), 1)  # the "real" one survives


# ---------------------------------------------------------------------------------------------- metrics
class MetricsTests(Base):
    HOSTS = [{"instance": "192.0.2.10:9100", "address": "192.0.2.10", "up": 1, "cpu": 42.5, "memory": 91.0, "disk": 10.0,
              "cores": 16, "health": "warning"},
             {"instance": "192.0.2.11:9100", "address": "192.0.2.11", "up": 0, "cpu": None, "memory": None, "disk": None,
              "cores": None, "health": "down"}]

    def test_not_configured(self):
        self.assertEqual(metrics.server_metrics([self.esx1]), ({}, "", False))

    @PROM
    def test_matches_by_instance_and_address(self):
        with mock.patch("telemetry.hosts.fetch_hosts", return_value=self.HOSTS), \
                mock.patch("core.prometheus.query", return_value=[row(64 * 1024 ** 3, instance="192.0.2.10:9100")]):
            out, err, on = metrics.server_metrics([self.esx1, self.esx2, self.dr_host])
        self.assertTrue(on)
        self.assertEqual(err, "")
        self.assertEqual(set(out), {self.esx1.pk, self.esx2.pk})
        m = out[self.esx1.pk]
        self.assertEqual((m["cpu"], m["cores"], m["mem_total_gb"]), (42.5, 16, 64.0))
        self.assertEqual(m["url"], reverse("telemetry:host_detail", args=["192.0.2.10:9100"]))
        self.assertEqual(out[self.esx2.pk]["up"], 0)
        self.assertIsNone(out[self.esx2.pk]["cpu"])

    @PROM
    def test_errors_are_messages_not_crashes(self):
        with mock.patch("telemetry.hosts.fetch_hosts", side_effect=PrometheusError("Prometheus unreachable: boom")):
            out, err, on = metrics.server_metrics([self.esx1])
        self.assertEqual((out, on), ({}, True))
        self.assertIn("unreachable", err)
        with mock.patch("core.prometheus.query", return_value="garbage"):
            with self.assertRaises(PrometheusError):
                metrics.query("up")
        self.assertEqual(metrics.tone(95), "crit")
        self.assertEqual(metrics.tone(None), "mute")

    @PROM
    def test_servers_page_shows_live_values(self):
        with mock.patch("telemetry.hosts.fetch_hosts", return_value=self.HOSTS), mock.patch("core.prometheus.query", return_value=[]):
            r = self.login(self.admin).get(reverse("compute:servers"))
        self.assertEqual(r.context["matched"], 2)
        self.assertContains(r, "cm-meter")
        self.assertContains(r, "no instance")  # dr-host has no Prometheus instance
        self.assertContains(r, reverse("telemetry:host_detail", args=["192.0.2.10:9100"]))
        self.assertNotContains(r, "Prometheus is not connected")
        with mock.patch("telemetry.hosts.fetch_hosts", return_value=self.HOSTS), mock.patch("core.prometheus.query", return_value=[]):
            r = self.client.get(reverse("compute:servers_export"))
        self.assertIn("esx-01,192.0.2.10,Mumbai,unknown,,,vsphere-mum (Hypervisor host),2,8,32,192.0.2.10:9100,42.5,91.0,10.0", r.content.decode())


# ---------------------------------------------------------------------------------------------- kubernetes
def k8s_rows(promql):
    """Fake Prometheus for the selector cluster="k8s-mum"."""
    if 'cluster="k8s-mum"' not in promql:
        return []
    if promql.startswith("count(kube_node_status_condition"):
        return [row(6)]
    if promql.startswith("count(kube_pod_status_phase"):
        return [row(40)]
    if "sum by (status)" in promql:
        return [row(2, status="true"), row(1, status="false")]
    if "topk(50, kube_node_status_condition" in promql:
        return [row(1, node="worker-2", status="false")]
    if "sum by (phase)" in promql:
        return [row(30, phase="Running"), row(3, phase="Pending"), row(2, phase="Failed"), row(1, phase="Weird")]
    if promql.startswith("count(kube_namespace_created"):
        return [row(7)]
    if promql.startswith("count(kube_deployment_spec_replicas"):
        return [row(12)]
    if promql.startswith("topk(50, kube_deployment_status_replicas_unavailable"):
        return [row(2, namespace="shop", deployment="api"), row(1, namespace="shop", deployment="web")]
    if promql.startswith("count(kube_deployment_status_replicas_unavailable"):
        return [row(2)]
    return []


@PROM
class K8sTests(Base):
    def test_queries_are_built_from_validated_pairs_only(self):
        q = k8s.queries([("cluster", 'a"b'), ("env", "p\\q")])
        self.assertIn('cluster="a\\"b",env="p\\\\q"', q["pods"])
        self.assertEqual(k8s.queries([])["nodes"], 'sum by (status) (kube_node_status_condition{condition="Ready"} == 1)')
        self.assertTrue(q["not_ready"].startswith("topk(50, kube_node_status_condition{condition=\"Ready\",status!=\"true\",cluster="))

    def test_fetch_summary_parses_kube_state_metrics(self):
        with mock.patch("core.prometheus.query", side_effect=k8s_rows):
            s = k8s.fetch_summary([("cluster", "k8s-mum")])
        self.assertEqual(s["nodes"], {"ready": 2, "not_ready": 1, "total": 3})
        self.assertEqual(s["not_ready_nodes"], ["worker-2"])
        self.assertEqual(s["pods"], {"Running": 30, "Pending": 3, "Succeeded": 0, "Failed": 2, "Unknown": 1})
        self.assertEqual(s["pods_total"], 36)
        self.assertEqual((s["namespaces"], s["deployments"], s["unavailable_count"]), (7, 12, 2))
        self.assertEqual(s["unavailable"][0], {"namespace": "shop", "deployment": "api", "replicas": 2})
        self.assertEqual(k8s.state_of(s), "warning")
        self.assertEqual(k8s.state_of({"nodes": {"not_ready": 0}, "unavailable_count": 0}), "ok")
        self.assertEqual(k8s.state_of(None), "unknown")

    def test_missing_series_and_errors(self):
        with mock.patch("core.prometheus.query", return_value=[]):
            with self.assertRaises(k8s.MetricsMissing):
                k8s.fetch_summary([("cluster", "nope")])
            self.assertEqual(k8s.live_summary(self.k8s), (None, "missing"))
        with self.assertRaises(k8s.MetricsMissing):
            k8s.fetch_summary([])
        self.assertEqual(k8s.live_summary(self.global_cluster), (None, "no_selector"))
        cache.clear()
        with mock.patch("core.prometheus.query", side_effect=PrometheusError("Prometheus returned HTTP 502")):
            s, problem = k8s.live_summary(self.k8s)
        self.assertIsNone(s)
        self.assertIn("502", problem)

    def test_live_summary_is_cached(self):
        with mock.patch("core.prometheus.query", side_effect=k8s_rows) as q:
            s1, _p = k8s.live_summary(self.k8s)
            s2, _p = k8s.live_summary(self.k8s)
        self.assertEqual(s1["pods_total"], 36)
        self.assertEqual(s1, s2)
        self.assertEqual(s1["collected_at"], s2["collected_at"])  # second call served from the 30 s cache
        self.assertLess(q.call_count, 12)

    def test_job_registered_and_emits_on_state_change_only(self):
        self.assertEqual(core_jobs.JOBS["compute.k8s"]["interval"], 300)
        self.assertIs(core_jobs.JOBS["compute.k8s"]["func"], jobs.collect_k8s)
        with override_settings(PROMETHEUS_URL=""):
            self.assertEqual(jobs.collect_k8s(), "prometheus not configured")
        with mock.patch("core.prometheus.query", side_effect=k8s_rows):
            self.assertEqual(jobs.collect_k8s(), "1 kubernetes cluster(s) collected, 1 degraded, 0 failed")
            self.k8s.refresh_from_db()
            self.assertEqual(self.k8s.k8s_state, "warning")
            self.assertEqual(self.k8s.k8s_summary["nodes"]["not_ready"], 1)
            self.assertIsNotNone(self.k8s.k8s_checked_at)
            jobs.collect_k8s()  # still degraded: no second event
        evs = Event.objects.filter(source="compute", fingerprint=f"compute:k8s:{self.k8s.pk}")
        self.assertEqual(evs.count(), 1)
        ev = evs.get()
        self.assertEqual((ev.severity, ev.site), ("warning", MUM))
        self.assertIn("worker-2", ev.message)
        self.assertIn("shop/api", ev.message)
        healthy = lambda q: [] if ("status!=" in q or "unavailable" in q and "topk" in q) else (  # noqa: E731
            [row(3, status="true")] if "sum by (status)" in q else [row(0)] if "unavailable" in q else k8s_rows(q))
        with mock.patch("core.prometheus.query", side_effect=healthy):
            self.assertEqual(jobs.refresh_cluster(Cluster.objects.get(pk=self.k8s.pk)), "ok")
            jobs.refresh_cluster(Cluster.objects.get(pk=self.k8s.pk))
        self.assertEqual(evs.count(), 2)
        self.assertEqual(evs.latest("pk").severity, "ok")
        # a Prometheus failure keeps the last good state and records the error, without raising
        with mock.patch("core.prometheus.query", side_effect=PrometheusError("Prometheus unreachable")):
            self.assertEqual(jobs.refresh_cluster(Cluster.objects.get(pk=self.k8s.pk)), "ok")
        self.k8s.refresh_from_db()
        self.assertIn("unreachable", self.k8s.k8s_error)
        with mock.patch("core.prometheus.query", return_value=[]):
            jobs.refresh_cluster(self.k8s)
        self.k8s.refresh_from_db()
        self.assertEqual(self.k8s.k8s_error, "No kube-state-metrics series match the selector.")
        with mock.patch("compute.jobs.refresh_cluster", side_effect=RuntimeError("x")):
            self.assertEqual(jobs.collect_k8s(), "0 kubernetes cluster(s) collected, 0 degraded, 1 failed")

    def test_kubernetes_page_live_and_empty_states(self):
        c = self.login(self.operator)
        with mock.patch("core.prometheus.query", side_effect=k8s_rows):
            r = c.get(reverse("compute:kubernetes"))
        self.assertEqual(r.context["totals"]["live"], 1)
        self.assertEqual(r.context["totals"]["ready"], 2)
        self.assertContains(r, "worker-2")
        self.assertContains(r, "Degraded")
        self.assertContains(r, f'k8s-pods-{self.k8s.pk}')
        Cluster.objects.create(tenant_key=T, name="k8s-nosel", kind="kubernetes", site=MUM)
        cache.clear()
        with mock.patch("core.prometheus.query", return_value=[]):
            r = c.get(reverse("compute:kubernetes"))
        self.assertContains(r, "No kube-state-metrics series found")
        self.assertContains(r, "Deploy <b>kube-state-metrics</b>")
        self.assertContains(r, "No Prometheus selector")
        self.assertEqual(r.context["totals"]["live"], 0)
        with mock.patch("core.prometheus.query", side_effect=k8s_rows):
            r = c.get(reverse("compute:cluster_detail", args=[self.k8s.pk]))
        self.assertContains(r, "Collect now")
        with mock.patch("core.prometheus.query", side_effect=k8s_rows):
            r = c.post(reverse("compute:cluster_refresh", args=[self.k8s.pk]), follow=True)
        self.assertContains(r, "Degraded")
        self.assertTrue(AuditEvent.objects.filter(action="run", object_id=str(self.k8s.pk)).exists())
        self.k8s.refresh_from_db()
        self.assertEqual(self.k8s.k8s_state, "warning")

    def test_kubernetes_page_without_prometheus(self):
        with override_settings(PROMETHEUS_URL=""):
            r = self.login(self.viewer).get(reverse("compute:kubernetes"))
        self.assertContains(r, "Prometheus is not connected")
        self.assertContains(r, "kube-state-metrics")
        self.assertContains(r, "kube_deployment_status_replicas_unavailable")


# ---------------------------------------------------------------------------------------------- review fixes
@PROM
class PrometheusOutageTests(Base):
    """ipam-dcim-compute-3: with Prometheus down the pages and the job make one slow call, not one per cluster."""

    def setUp(self):
        super().setUp()
        for n in range(4):
            Cluster.objects.create(tenant_key=T, name=f"k8s-extra-{n}", kind="kubernetes", site=MUM,
                                   prometheus_selector=f'cluster="extra-{n}"')

    def test_kubernetes_page_asks_prometheus_once(self):
        c = self.login(self.operator)
        down = PrometheusError("Prometheus unreachable: timed out")
        with mock.patch("core.prometheus.query", side_effect=down) as q:
            r = c.get(reverse("compute:kubernetes"))
            self.assertEqual(r.status_code, 200)
            self.assertEqual(q.call_count, 1)                 # 5 clusters on the page, one failed call
            self.assertContains(r, "unreachable")
            c.get(reverse("compute:kubernetes"))
            self.assertEqual(q.call_count, 1)                 # a second view within the outage window: none
        cache.clear()
        with mock.patch("core.prometheus.query", side_effect=k8s_rows):
            r = c.get(reverse("compute:kubernetes"))
        self.assertEqual(r.context["totals"]["live"], 1)       # recovers as soon as the window is over

    def test_a_query_error_does_not_short_circuit(self):
        c = self.login(self.operator)
        with mock.patch("core.prometheus.query", side_effect=PrometheusError("parse error: bad expression")) as q:
            c.get(reverse("compute:kubernetes"))
        self.assertEqual(q.call_count, 5)

    def test_job_stops_after_the_first_outage(self):
        with mock.patch("core.prometheus.query", side_effect=PrometheusError("Prometheus unreachable: timed out")) as q:
            out = jobs.collect_k8s()
        self.assertEqual(q.call_count, 1)
        self.assertIn("stopped early (Prometheus unreachable)", out)
        self.assertTrue(Cluster.objects.exclude(k8s_error="").filter(kind="kubernetes").exists())

    def test_job_time_budget(self):
        with mock.patch("core.prometheus.query", side_effect=k8s_rows) as q, mock.patch.object(jobs, "MAX_SECONDS", -1):
            out = jobs.collect_k8s()
        self.assertIn("1 kubernetes cluster(s) collected", out)
        self.assertIn("time budget spent", out)

    def test_manual_collect_ignores_the_outage_window(self):
        cache.set(k8s.OUTAGE_KEY, "Prometheus unreachable: x", 60)
        c = self.login(self.operator)
        with mock.patch("core.prometheus.query", side_effect=k8s_rows):
            c.post(reverse("compute:cluster_refresh", args=[self.k8s.pk]), follow=True)
        self.k8s.refresh_from_db()
        self.assertEqual(self.k8s.k8s_state, "warning")
