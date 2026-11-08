"""Matching a vCenter VM onto a device: matched / ambiguous / unmatched, never silent."""
from django.test import SimpleTestCase

from hypervisor import matching
from hypervisor.matching import AMBIGUOUS, MATCHED, UNMATCHED, DeviceIndex, match_host, match_vm, resolve_claims
from hypervisor.models import VmInfo
from hypervisor.tests.base import HvCase


def idx(*devs):
    return DeviceIndex([dict({"serial": "", "tags": []}, **d) for d in devs])


class PureMatching(SimpleTestCase):
    def test_name_match_is_case_insensitive(self):
        i = idx({"id": 1, "name": "App-01", "mgmt_ip": "10.0.0.1"})
        st, dev, reason, cands = match_vm({"name": "app-01"}, i)
        self.assertEqual((st, dev, cands), (MATCHED, 1, [1]))
        self.assertIn("name", reason)

    def test_ip_match_when_names_differ(self):
        i = idx({"id": 1, "name": "legacy-name", "mgmt_ip": "10.0.0.7"})
        self.assertEqual(match_vm({"name": "x", "guest_ips": ["10.0.0.7"]}, i)[:2], (MATCHED, 1))

    def test_guest_host_name_short_form(self):
        i = idx({"id": 1, "name": "db-01", "mgmt_ip": "10.0.0.2"})
        self.assertEqual(match_vm({"name": "vmware-db", "guest_hostname": "DB-01.corp.example"}, i)[:2], (MATCHED, 1))

    def test_unmatched(self):
        st, dev, reason, cands = match_vm({"name": "nobody", "guest_ips": ["10.9.9.9"]}, idx({"id": 1, "name": "a", "mgmt_ip": "10.0.0.1"}))
        self.assertEqual((st, dev, cands), (UNMATCHED, None, []))
        self.assertTrue(reason)

    def test_name_and_ip_pointing_at_different_devices_is_ambiguous(self):
        i = idx({"id": 1, "name": "app-01", "mgmt_ip": "10.0.0.1"}, {"id": 2, "name": "other", "mgmt_ip": "10.0.0.50"})
        st, dev, reason, cands = match_vm({"name": "app-01", "guest_ips": ["10.0.0.50"]}, i)
        self.assertEqual((st, dev, cands), (AMBIGUOUS, None, [1, 2]))

    def test_two_devices_with_the_same_address_is_ambiguous(self):
        i = idx({"id": 1, "name": "a", "mgmt_ip": "10.0.0.1"}, {"id": 2, "name": "b", "mgmt_ip": "10.0.0.1"})
        self.assertEqual(match_vm({"name": "x", "guest_ips": ["10.0.0.1"]}, i)[0], AMBIGUOUS)

    def test_uuid_wins_over_a_conflicting_name(self):
        i = idx({"id": 1, "name": "renamed", "mgmt_ip": "10.0.0.1", "serial": "4237A1B2-0000"}, {"id": 2, "name": "app-01", "mgmt_ip": "10.0.0.2"})
        st, dev, reason, _c = match_vm({"name": "app-01", "bios_uuid": "4237a1b2-0000"}, i)
        self.assertEqual((st, dev), (MATCHED, 1))
        self.assertTrue(reason.startswith("UUID"))

    def test_uuid_tag(self):
        i = idx({"id": 3, "name": "x", "mgmt_ip": "10.0.0.1", "tags": ["uuid:ABC-123-DEF-456"]})
        self.assertEqual(match_vm({"name": "y", "instance_uuid": "abc-123-def-456"}, i)[:2], (MATCHED, 3))

    def test_uuid_on_two_devices_is_ambiguous(self):
        i = idx({"id": 1, "name": "a", "mgmt_ip": "1.1.1.1", "serial": "U-1"}, {"id": 2, "name": "b", "mgmt_ip": "1.1.1.2", "serial": "U-1"})
        self.assertEqual(match_vm({"name": "z", "bios_uuid": "u-1"}, i)[0], AMBIGUOUS)

    def test_empty_values_never_match_empty_fields(self):
        i = idx({"id": 1, "name": "a", "mgmt_ip": "10.0.0.1"})
        self.assertEqual(match_vm({"name": "", "guest_hostname": "", "guest_ips": [""], "bios_uuid": ""}, i)[0], UNMATCHED)

    def test_two_vms_for_one_device_are_both_withdrawn(self):
        i = idx({"id": 1, "name": "app-01", "mgmt_ip": "10.0.0.1"})
        raw = {k: match_vm({"name": "app-01"}, i) for k in ("a", "b")}
        out = resolve_claims(raw)
        self.assertEqual({k: v[0] for k, v in out.items()}, {"a": AMBIGUOUS, "b": AMBIGUOUS})
        self.assertTrue(all(v[1] is None for v in out.values()))

    def test_result_does_not_depend_on_order(self):
        i = idx({"id": 1, "name": "app-01", "mgmt_ip": "10.0.0.1"})
        a = resolve_claims({k: match_vm({"name": "app-01"}, i) for k in ("a", "b")})
        b = resolve_claims({k: match_vm({"name": "app-01"}, i) for k in ("b", "a")})
        self.assertEqual(a, b)

    def test_uuid_keeps_the_device_against_a_name_rival(self):
        i = idx({"id": 1, "name": "app-01", "mgmt_ip": "10.0.0.1", "serial": "U-9"})
        out = resolve_claims({"strong": match_vm({"name": "zzz", "bios_uuid": "u-9"}, i), "weak": match_vm({"name": "app-01"}, i)})
        self.assertEqual(out["strong"][0], MATCHED)
        self.assertEqual(out["weak"][0], AMBIGUOUS)

    def test_host_matching(self):
        i = idx({"id": 5, "name": "esxi-host-01", "mgmt_ip": "10.20.1.10"})
        self.assertEqual(match_host({"name": "esxi-host-01.corp.example"}, i)[:2], (MATCHED, 5))
        self.assertEqual(match_host({"name": "10.20.1.10"}, i)[:2], (MATCHED, 5))
        self.assertEqual(match_host({"name": "esxi-host-77"}, i)[0], UNMATCHED)


class Matching(HvCase):
    def sync(self, fake, src, **kw):
        return self.run_sync(src, **kw)

    def test_sync_links_by_name_and_by_ip_and_leaves_the_rest(self):
        from hypervisor.tests.fake_vcenter import FakeVCenter
        with FakeVCenter() as fake:
            src = self.make_source(fake)
            by_name = self.make_vm_device("app-01", ip="10.20.1.15")
            by_ip = self.make_vm_device("renamed-db", ip="10.20.1.16")
            self.run_sync(src)
        vms = {v.moref: v for v in VmInfo.objects.all()}
        self.assertEqual((vms["vm-101"].match_state, vms["vm-101"].device_id), ("matched", by_name.pk))
        self.assertEqual((vms["vm-102"].match_state, vms["vm-102"].device_id), ("matched", by_ip.pk))
        for ref in ("vm-103", "vm-104", "vm-105"):
            self.assertEqual((vms[ref].match_state, vms[ref].device_id), ("unmatched", None))
        from core.models import AuditEvent
        self.assertEqual(AuditEvent.objects.filter(action="hypervisor.match.auto").count(), 2)

    def test_two_devices_fit_one_vm_it_is_ambiguous_and_nothing_is_linked(self):
        from hypervisor.tests.fake_vcenter import FakeVCenter
        with FakeVCenter() as fake:
            src = self.make_source(fake)
            self.make_vm_device("app-01", ip="10.20.1.15")
            self.make_vm_device("dup-app", ip="10.20.1.15")
            self.run_sync(src)
        vm = VmInfo.objects.get(moref="vm-101")
        self.assertEqual((vm.match_state, vm.device_id), ("ambiguous", None))
        self.assertEqual(len(vm.match_candidates), 2)

    def test_a_manual_link_is_never_replaced_by_the_sync(self):
        from hypervisor.tests.fake_vcenter import FakeVCenter
        with FakeVCenter() as fake:
            src = self.make_source(fake)
            self.make_vm_device("app-01", ip="10.20.1.15")
            mine = self.make_vm_device("my-choice", ip="10.77.7.7")
            self.run_sync(src)
            vm = VmInfo.objects.get(moref="vm-101")
            from hypervisor import services
            services.unlink_vm(vm, None)
            services.link_device(vm, mine, self.root, None)
            self.run_sync(src)
            self.run_sync(src)
        vm.refresh_from_db()
        self.assertEqual(vm.device_id, mine.pk)
        self.assertEqual(vm.matched_by, "root")

    def test_a_device_already_used_by_another_vm_is_not_taken(self):
        from hypervisor.tests.fake_vcenter import FakeVCenter, default_inventory
        inv = default_inventory()
        inv["vms"]["vm-106"] = dict(inv["vms"]["vm-101"], bios="4237a1b2-0000-4000-8000-000000000106")   # same name and IP, second VM
        with FakeVCenter(inv) as fake:
            src = self.make_source(fake)
            self.make_vm_device("app-01", ip="10.20.1.15")
            self.run_sync(src)
        states = {v.moref: v.match_state for v in VmInfo.objects.filter(moref__in=["vm-101", "vm-106"])}
        self.assertEqual(states, {"vm-101": "ambiguous", "vm-106": "ambiguous"})

    def test_deleted_device_returns_the_vm_to_unmatched(self):
        from hypervisor.tests.fake_vcenter import FakeVCenter
        with FakeVCenter() as fake:
            src = self.make_source(fake)
            dev = self.make_vm_device("app-01", ip="10.20.1.15")
            self.run_sync(src)
            dev.delete()
            self.run_sync(src)
        vm = VmInfo.objects.get(moref="vm-101")
        self.assertEqual((vm.match_state, vm.device_id), ("unmatched", None))

    def test_ignored_vm_stays_ignored(self):
        from hypervisor import services
        from hypervisor.tests.fake_vcenter import FakeVCenter
        with FakeVCenter() as fake:
            src = self.make_source(fake)
            self.run_sync(src)
            vm = VmInfo.objects.get(moref="vm-104")
            services.ignore_vm(vm, None)
            self.make_vm_device("orphan-01", ip="10.99.9.9")
            self.run_sync(src)
        vm.refresh_from_db()
        self.assertEqual((vm.match_state, vm.device_id), ("ignored", None))

    def test_hosts_match_physical_servers(self):
        from hypervisor.models import HvHost
        from hypervisor.tests.fake_vcenter import FakeVCenter
        from network.models import Device
        with FakeVCenter() as fake:
            src = self.make_source(fake)
            Device.objects.create(tenant_key="default", name="esxi-host-01", device_type="physical_server", mgmt_ip="10.20.1.10", site="Mumbai")
            self.run_sync(src)
        h = HvHost.objects.get(moref="host-11")
        self.assertEqual(h.match_state, "matched")
        self.assertEqual(HvHost.objects.get(moref="host-12").match_state, "unmatched")
