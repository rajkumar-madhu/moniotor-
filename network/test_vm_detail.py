"""Virtual machine detail Overview, guest metrics (never invented), and stub tabs."""
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from . import vm_metrics
from .tests import T, dev, users
from .views import TABS, VM_TABS


@override_settings(LINKEDEYE_TENANT=T)
class VmMetaTests(TestCase):
    def test_parse_vm_meta_from_tags(self):
        d = dev(
            "vm-meta",
            device_type="virtual_machine",
            mgmt_ip="192.0.2.40",
            firmware="Ubuntu 22.04",
            tags="cluster:Compute-01, host:esxi-01, datastore:DS01, vcpu:4, leftover",
        )
        meta, extra = vm_metrics.parse_vm_meta(d)
        self.assertEqual(meta["cluster"], "Compute-01")
        self.assertEqual(meta["host"], "esxi-01")
        self.assertEqual(meta["datastore"], "DS01")
        self.assertEqual(meta["vcpu"], "4")
        self.assertEqual(meta["os"], "Ubuntu 22.04")
        self.assertEqual(extra, ["leftover"])

    def test_guest_power_label(self):
        d = dev("vm-power", device_type="virtual_machine", mgmt_ip="192.0.2.41", status="up")
        self.assertEqual(vm_metrics.guest_power_label(d), "Running")
        d.status = "down"
        self.assertEqual(vm_metrics.guest_power_label(d), "Stopped")


@override_settings(LINKEDEYE_TENANT=T, PROMETHEUS_URL="")
class VmDetailViewTests(TestCase):
    def setUp(self):
        self.viewer = users()[0]
        self.vm = dev(
            "VM-APP-01",
            device_type="virtual_machine",
            vendor="VMware",
            model="Virtual Machine",
            firmware="Ubuntu 22.04 LTS",
            mgmt_ip="10.20.1.15",
            site="Mumbai (NSE)",
            role="App server",
            status="up",
            tags="cluster:Compute-01, host:esxi-host-01, datastore:DS01, vcpu:4, memory_gb:16",
        )
        self.sw = dev("sw-regression", mgmt_ip="192.0.2.50")
        self.c = Client()
        self.c.force_login(self.viewer)

    def test_vm_overview_without_metrics_never_invents_numbers(self):
        r = self.c.get(reverse("network:detail", args=[self.vm.pk]) + "?sample=1")   # the old demo switch is ignored
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "CPU usage")
        self.assertNotContains(r, "24.6")
        self.assertNotContains(r, "27 days")
        self.assertNotContains(r, "Sample data")
        self.assertNotContains(r, "vcsa.")
        self.assertContains(r, "Guest metrics not connected")
        self.assertContains(r, "LINKEDEYE_PROMETHEUS_URL")
        self.assertContains(r, "VM Information")
        self.assertContains(r, "VM Actions")
        self.assertContains(r, "Hypervisor control is read-only")
        self.assertContains(r, 'id="vm-info"')
        self.assertContains(r, "Open Console")
        self.assertContains(r, "Performance")
        self.assertContains(r, "Snapshots")
        self.assertContains(r, "Compute-01")          # inventory from key:value tags
        self.assertNotContains(r, 'id="front-panel"')

    def test_vm_uses_vm_tabs_not_network_tabs(self):
        r = self.c.get(reverse("network:detail", args=[self.vm.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, ">Network<")
        self.assertContains(r, ">Guest OS<")
        self.assertNotContains(r, ">Neighbours<")
        self.assertNotContains(r, ">Hardware<")

    def test_invalid_tab_falls_back_to_overview(self):
        r = self.c.get(reverse("network:detail", args=[self.vm.pk]) + "?tab=not-a-tab")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "CPU usage")

    def test_performance_tab_without_prometheus_is_not_connected(self):
        r = self.c.get(reverse("network:detail", args=[self.vm.pk]) + "?tab=performance&sample=1&perf_range=1h")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Guest performance")
        self.assertContains(r, "Not connected.")
        self.assertNotContains(r, 'id="nw-vm-perf-cpu"')

    def test_stub_tab_returns_empty_state(self):
        r = self.c.get(reverse("network:detail", args=[self.vm.pk]) + "?tab=snapshots")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Snapshots")
        self.assertContains(r, "Not connected")
        self.assertContains(r, "hypervisor connector")

    def test_topology_stub_links_when_available(self):
        r = self.c.get(reverse("network:detail", args=[self.vm.pk]) + "?tab=topology")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Topology")

    def test_non_vm_keeps_classic_overview(self):
        r = self.c.get(reverse("network:detail", args=[self.sw.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Device Status")
        self.assertContains(r, "Ports up / down")
        self.assertContains(r, ">Interfaces<")
        self.assertContains(r, ">Hardware<")
        self.assertNotContains(r, "CPU usage")
        self.assertNotContains(r, "VM Actions")

    def test_tab_constants(self):
        self.assertEqual(dict(TABS)["interfaces"], "Interfaces")
        self.assertEqual(dict(VM_TABS)["interfaces"], "Network")
        self.assertIn("performance", dict(VM_TABS))
        self.assertIn("configuration", dict(VM_TABS))
