"""Parsers: clean bounded dicts from hostile or incomplete vSphere answers."""
from django.test import SimpleTestCase

from hypervisor import parsers


class CleanText(SimpleTestCase):
    def test_controls_and_formatting_stripped_and_capped(self):
        self.assertEqual(parsers.clean_text("vm\nname\x1b[31m‮evil", 50), "vm name[31mevil")
        self.assertEqual(len(parsers.clean_text("x" * 1000, 40)), 40)
        for bad in (None, True, {"a": 1}, ["x"], b"bytes"):
            self.assertEqual(parsers.clean_text(bad), "")
        self.assertEqual(parsers.clean_text(12), "12")

    def test_numbers(self):
        self.assertEqual(parsers.to_int(5), 5)
        for bad in (True, "5", None, float("nan"), float("inf"), -1, 2 ** 70):
            self.assertIsNone(parsers.to_int(bad), bad)

    def test_moref_never_a_path(self):
        self.assertEqual(parsers.moref("vm-12"), "vm-12")
        for bad in ("../../x", "vm 1", "vm-1/guest", "", None, "a" * 60, "vm-1\nX"):
            self.assertEqual(parsers.moref(bad), "")


class Records(SimpleTestCase):
    def test_vm_summary_missing_fields_are_none(self):
        vm = parsers.parse_vm_summary({"vm": "vm-1"})
        self.assertEqual(vm["name"], "vm-1")
        self.assertIsNone(vm["cpu_count"])
        self.assertEqual(vm["power_state"], "unknown")
        self.assertIsNone(parsers.parse_vm_summary({"name": "no id"}))
        self.assertIsNone(parsers.parse_vm_summary("junk"))

    def test_hostile_vm_detail(self):
        raw = {"name": "<script>alert(1)</script>\n", "power_state": "POWERED_ON", "identity": {"bios_uuid": "not a uuid!!", "instance_uuid": "ABCDEF12-0000"},
               "cpu": {"count": -4}, "memory": {"size_MiB": "lots"},
               "disks": {"1": {"label": "d\x00isk", "capacity": 10 ** 30, "backing": {"vmdk_file": "[DS 01] a/b.vmdk"}}, "2": "junk"},
               "nics": {"1": {"mac_address": "zz", "backing": {"network_name": "x" * 500}}}}
        d = parsers.parse_vm_detail(raw)
        self.assertEqual(d["name"], "<script>alert(1)</script>")      # stored as text, escaped on output
        self.assertEqual(d["bios_uuid"], "")
        self.assertEqual(d["instance_uuid"], "abcdef12-0000")
        self.assertIsNone(d["cpu_count"])
        self.assertIsNone(d["memory_mib"])
        self.assertEqual(d["disks"][0]["datastore"], "DS 01")
        self.assertIsNone(d["disks"][0]["capacity_bytes"])
        self.assertEqual(d["nics"][0]["mac"], "")
        self.assertEqual(len(d["nics"][0]["network"]), 120)

    def test_limits_on_disks_and_nics(self):
        raw = {"disks": {str(i): {"label": "d"} for i in range(500)}, "nics": {str(i): {} for i in range(500)}}
        d = parsers.parse_vm_detail(raw)
        self.assertEqual(len(d["disks"]), parsers.MAX_DISKS)
        self.assertEqual(len(d["nics"]), parsers.MAX_NICS)

    def test_guest_identity_message_forms_and_ip(self):
        i = parsers.parse_guest_identity({"full_name": {"default_message": "Ubuntu"}, "ip_address": "10.0.0.5", "host_name": "h\n1"})
        self.assertEqual((i["full_name"], i["ip"], i["host_name"]), ("Ubuntu", "10.0.0.5", "h 1"))
        self.assertEqual(parsers.parse_guest_identity({"ip_address": "999.1.1.1; DROP"})["ip"], "")

    def test_guest_interfaces_drop_link_local_and_garbage(self):
        out = parsers.parse_guest_interfaces([{"mac_address": "00:50:56:AA:BB:01", "ip": {"ip_addresses": [
            {"ip_address": "10.0.0.5"}, {"ip_address": "fe80::1"}, {"ip_address": "x"}, {"ip_address": "10.0.0.5"}]}}, "junk"])
        self.assertEqual(out[0], ("00:50:56:aa:bb:01", ["10.0.0.5"]))

    def test_datastore_free_never_above_capacity(self):
        d = parsers.parse_datastore({"datastore": "datastore-1", "name": "DS", "capacity": 100, "free_space": 500})
        self.assertEqual(d["free_bytes"], 100)
        d = parsers.parse_datastore({"datastore": "datastore-1", "capacity": "big"}, {"accessible": "yes"})
        self.assertIsNone(d["capacity_bytes"])
        self.assertIsNone(d["accessible"])

    def test_merge_guest_without_tools(self):
        detail = parsers.parse_vm_detail({"name": "x", "nics": {"1": {"mac_address": "00:50:56:aa:bb:01"}}})
        merged = parsers.merge_guest(detail, None, None, None)
        self.assertEqual(merged["guest_ips"], [])
        self.assertIsNone(merged["tools"])
