"""Code / data / authz review: matching over time (unlink, rebuilt VM, VM moved between vCenters, two sources), query counts that do
not grow with the amount of data, and the sync not undoing what an operator decided."""
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from hypervisor import services
from hypervisor.models import Datastore, HvHost, VCenterSource, VmControlRequest, VmInfo
from hypervisor.tests.base import HvCase
from hypervisor.tests.fake_vcenter import FakeVCenter, default_inventory


class MatchingOverTime(HvCase):
    def setUp(self):
        super().setUp()
        self.fake = FakeVCenter().start()
        self.addCleanup(self.fake.stop)
        self.src = self.make_source(self.fake)

    def sync(self, src=None, **kw):
        return self.run_sync(src or self.src, **kw)

    def test_an_operator_unlink_is_not_undone_by_the_next_sync(self):
        dev = self.make_vm_device("app-01", ip="10.20.1.15")
        self.sync()
        vm = VmInfo.objects.get(moref="vm-101")
        self.assertEqual(vm.device_id, dev.pk)
        services.unlink_vm(vm, None)
        self.sync()
        self.sync()
        vm.refresh_from_db()
        self.assertIsNone(vm.device_id)
        self.assertNotEqual(vm.match_state, "matched")

    def test_a_rebuilt_vm_with_the_same_name_takes_over_the_device_of_the_one_that_is_gone(self):
        dev = self.make_vm_device("app-01", ip="10.20.1.15")
        self.sync()
        old = VmInfo.objects.get(moref="vm-101")
        self.assertEqual(old.device_id, dev.pk)
        inv = self.fake.inv["vms"]
        new = dict(inv.pop("vm-101"), bios="4237a1b2-0000-4000-8000-000000000999")
        inv["vm-901"] = new
        self.sync()
        old.refresh_from_db()
        fresh = VmInfo.objects.get(moref="vm-901")
        self.assertIsNotNone(old.missing_since)
        self.assertEqual((fresh.match_state, fresh.device_id), ("matched", dev.pk))
        self.assertIsNone(old.device_id)                      # the record of the VM that no longer exists let go of the device
        from core.models import AuditEvent
        self.assertTrue(AuditEvent.objects.filter(action="hypervisor.match.auto", object_id=str(fresh.pk)).exists())

    def test_a_vm_that_is_still_listed_never_loses_its_device_to_a_look_alike(self):
        dev = self.make_vm_device("app-01", ip="10.20.1.15")
        self.sync()
        inv = self.fake.inv["vms"]
        inv["vm-902"] = dict(inv["vm-101"], bios="4237a1b2-0000-4000-8000-000000000902")      # second VM, same name and address
        self.sync()
        self.assertEqual(VmInfo.objects.get(moref="vm-101").device_id, dev.pk)
        twin = VmInfo.objects.get(moref="vm-902")
        self.assertEqual((twin.match_state, twin.device_id), ("ambiguous", None))

    def test_a_vm_moved_to_another_vcenter_follows_to_its_new_source(self):
        dev = self.make_vm_device("app-01", ip="10.20.1.15")
        self.sync()
        other_fake = FakeVCenter(default_inventory()).start()
        self.addCleanup(other_fake.stop)
        other = self.make_source(other_fake, name="vc-two")
        moved = other_fake.inv["vms"]
        for ref in list(moved):
            if ref != "vm-101":
                moved.pop(ref)
        self.fake.inv["vms"].pop("vm-101")
        self.sync()
        self.sync(other)
        self.assertEqual(VmInfo.objects.get(source=other, moref="vm-101").device_id, dev.pk)
        self.assertIsNone(VmInfo.objects.get(source=self.src, moref="vm-101").device_id)

    def test_two_sources_listing_the_same_vm_give_the_device_to_the_first_and_flag_the_second(self):
        dev = self.make_vm_device("app-01", ip="10.20.1.15")
        other_fake = FakeVCenter(default_inventory()).start()
        self.addCleanup(other_fake.stop)
        other = self.make_source(other_fake, name="vc-two")
        self.sync()
        self.sync(other)
        first = VmInfo.objects.get(source=self.src, moref="vm-101")
        second = VmInfo.objects.get(source=other, moref="vm-101")
        self.assertEqual(first.device_id, dev.pk)
        self.assertEqual((second.match_state, second.device_id), ("ambiguous", None))

    def test_a_renamed_vm_keeps_its_device(self):
        dev = self.make_vm_device("app-01", ip="10.20.1.15")
        self.sync()
        self.fake.inv["vms"]["vm-101"]["name"] = "totally-new-name"
        self.fake.inv["vms"]["vm-101"]["hostname"] = "other"
        self.fake.inv["vms"]["vm-101"]["ips"] = ["10.55.5.5"]
        self.sync()
        vm = VmInfo.objects.get(moref="vm-101")
        self.assertEqual((vm.device_id, vm.name), (dev.pk, "totally-new-name"))


class OperatorEditsDuringASync(HvCase):
    """The sync reads vCenter first and stores afterwards. A link an operator makes in between must survive."""

    def test_a_link_made_while_the_sync_is_storing_is_not_undone(self):
        from unittest import mock

        from hypervisor import sync
        fake = FakeVCenter().start()
        self.addCleanup(fake.stop)
        src = self.make_source(fake)
        self.run_sync(src)                                   # the VMs exist, unmatched
        mine = self.make_vm_device("my-choice", ip="10.77.7.7")
        target = VmInfo.objects.get(moref="vm-102")
        real = sync._observed_events
        done = []

        def operator_acts_midway(vm, before, is_new, now):
            if not done:                                     # the first VM of the pass was just stored; vm-102 is still to come
                done.append(1)
                services.link_device(VmInfo.objects.get(pk=target.pk), mine, self.root, None)
            return real(vm, before, is_new, now)

        with mock.patch.object(sync, "_observed_events", operator_acts_midway):
            self.run_sync(src)
        target.refresh_from_db()
        self.assertEqual((target.device_id, target.match_state, target.matched_by), (mine.pk, "matched", "root"))
        self.run_sync(src)
        target.refresh_from_db()
        self.assertEqual(target.device_id, mine.pk)


class QueryCounts(HvCase):
    """The number of queries a page runs must not depend on how many rows it shows."""

    def setUp(self):
        super().setUp()
        self.src = VCenterSource.objects.create(tenant_key=services.tenant(), name="vc-q", site="Mumbai", host="127.0.0.1")

    def grow(self, n, start=0):
        from network.models import Device
        for i in range(start, start + n):
            dev = Device.objects.create(tenant_key=services.tenant(), name=f"q-vm-{i}", device_type="virtual_machine", mgmt_ip=f"10.9.{i // 200}.{i % 200 + 1}", site="Mumbai")
            vm = VmInfo.objects.create(tenant_key=services.tenant(), source=self.src, moref=f"vm-q{i}", name=f"q-vm-{i}", device=dev,
                                       match_state="matched", power_state="on", host_moref="h1", host_name="h1")
            VmInfo.objects.create(tenant_key=services.tenant(), source=self.src, moref=f"vm-u{i}", name=f"u-vm-{i}", match_state="unmatched")
            Datastore.objects.create(tenant_key=services.tenant(), source=self.src, moref=f"ds-{i}", name=f"ds-{i}", capacity_bytes=100, free_bytes=50)
            HvHost.objects.create(tenant_key=services.tenant(), source=self.src, moref=f"host-{i}", name=f"esx-{i}")
            VmControlRequest.objects.create(tenant_key=services.tenant(), vm=vm, vm_name=vm.name, action="power_off",
                                            requested_by=self.mum_op, requested_by_name="mum-op")

    def count(self, client, url):
        client.get(url)                                    # warm session / content-type caches
        with CaptureQueriesContext(connection) as ctx:
            resp = client.get(url)
        self.assertEqual(resp.status_code, 200, url)
        return len(ctx.captured_queries)

    def test_pages_run_the_same_queries_for_few_and_many_rows(self):
        c = self.login(self.mum_admin)
        self.grow(6)
        first = {}
        first_dev = VmInfo.objects.filter(device__isnull=False).first().device_id
        urls = {
            "unmatched": reverse("hypervisor:unmatched") + "?per=100",
            "requests": reverse("hypervisor:requests") + "?per=100",
            "datastores": reverse("hypervisor:datastores"),
            "datastores_live": reverse("hypervisor:datastores_live"),
            "ds_csv": reverse("hypervisor:datastores_export"),
            "req_csv": reverse("hypervisor:requests_export"),
            "sources": reverse("hypervisor:sources"),
            "vm_live": reverse("hypervisor:vm_live", args=[first_dev]),
        }
        for name, url in urls.items():
            first[name] = self.count(c, url)
        self.grow(40, start=100)
        for name, url in urls.items():
            self.assertEqual(self.count(c, url), first[name], f"{name}: the number of queries grows with the data")
