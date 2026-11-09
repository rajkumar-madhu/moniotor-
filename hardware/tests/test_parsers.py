"""Vendor parsers against the fixtures: healthy, degraded, missing sections, hostile values."""
import copy
import json
from collections import Counter

from django.test import SimpleTestCase

from hardware import collector, parsers, redfish
from hardware.tests import fixtures
from hardware.tests.base import snapshot_of


def kinds(snap):
    return Counter(c.kind for c in snap.comps)


def one(snap, kind, name=None):
    return [c for c in snap.comps if c.kind == kind and (name is None or c.name == name)]


class DellHealthy(SimpleTestCase):
    def setUp(self):
        self.snap = snapshot_of(fixtures.dell_idrac9())

    def test_identity(self):
        s = self.snap
        self.assertEqual(s.vendor, "dell")
        self.assertEqual(s.power_state, "On")
        ov = s.overview
        self.assertEqual((ov["bmc_model"], ov["bmc_firmware"], ov["lifecycle"], ov["service_tag"]), ("iDRAC9", "6.10.30.00", "6.10.30.00", "7F3K9H2"))
        self.assertEqual((ov["model"], ov["license"]), ("PowerEdge R740", "Enterprise"))

    def test_counts(self):
        k = kinds(self.snap)
        self.assertEqual((k["temperature"], k["fan"], k["psu"], k["voltage"], k["cpu"], k["dimm"], k["drive"], k["nic"]), (4, 6, 2, 3, 2, 4, 2, 2))
        self.assertEqual(k["intrusion"], 1)

    def test_empty_dimm_slot_is_skipped(self):
        self.assertNotIn("DIMM A3", [c.name for c in one(self.snap, "dimm")])

    def test_thermal_values(self):
        inlet = one(self.snap, "temperature", "System Board Inlet Temp")[0]
        self.assertEqual((inlet.reading, inlet.unit, inlet.warn, inlet.crit, inlet.status), (24.0, "°C", 42.0, 47.0, "ok"))
        fan = one(self.snap, "fan")[0]
        self.assertEqual((fan.unit, fan.status), ("RPM", "ok"))
        self.assertEqual(fan.low_crit, 800.0)

    def test_psu(self):
        psu = one(self.snap, "psu", "PS1 Status")[0]
        self.assertEqual((psu.reading, psu.attrs["capacity_w"], psu.attrs["input_v"], psu.attrs["load_pct"]), (212.0, 750.0, 230.0, 28.3))

    def test_events_from_both_logs(self):
        ids = {e.entry_id for e in self.snap.events}
        self.assertEqual(ids, {"sel:1", "sel:2", "sel:3", "lclog:9"})
        sev = {e.entry_id: e.severity for e in self.snap.events}
        self.assertEqual((sev["sel:2"], sev["sel:3"]), ("warning", "info"))
        self.assertTrue(all(e.ts.tzinfo for e in self.snap.events))

    def test_drive_and_storage(self):
        d = one(self.snap, "drive")[0]
        self.assertEqual((d.attrs["media"], d.attrs["protocol"], d.attrs["capacity_gb"]), ("HDD", "SAS", 1200.2))
        self.assertEqual(one(self.snap, "storage")[0].attrs["firmware"], "25.5.9.0001")


class HpeHealthy(SimpleTestCase):
    def setUp(self):
        self.snap = snapshot_of(fixtures.hpe_ilo5())

    def test_identity(self):
        ov = self.snap.overview
        self.assertEqual(self.snap.vendor, "hpe")
        self.assertEqual((ov["bmc_model"], ov["bmc_firmware"], ov["license"], ov["serial"]), ("iLO 5", "iLO 5 v2.78", "iLO Advanced", "CZEXAMPLE01"))
        self.assertEqual(ov["service_tag"], "")

    def test_percent_fans_and_absent_sensor(self):
        fans = one(self.snap, "fan")
        self.assertEqual((len(fans), fans[0].unit), (4, "%"))
        self.assertEqual(len(one(self.snap, "temperature")), 3)            # "14-HD Max" is Absent

    def test_psu_names_come_from_the_bay_number(self):
        self.assertEqual([c.name for c in one(self.snap, "psu")], ["PSU 1", "PSU 2"])

    def test_critical_threshold_falls_back_to_fatal_and_noncritical_is_empty(self):
        t = one(self.snap, "temperature", "01-Inlet Ambient")[0]
        self.assertEqual((t.warn, t.crit), (None, 42.0))

    def test_battery_and_no_voltage_or_intrusion(self):
        k = kinds(self.snap)
        self.assertEqual((k["battery"], k["voltage"], k["intrusion"]), (1, 0, 0))
        self.assertEqual(self.snap.sections["intrusion"], "missing")

    def test_events(self):
        sev = {e.entry_id: e.severity for e in self.snap.events}
        self.assertEqual(sev, {"iml:7": "warning", "iml:6": "critical", "iel:21": "info"})


class Degraded(SimpleTestCase):
    def test_failed_psu_and_hot_cpu(self):
        d = fixtures.dell_idrac9()
        c = "/redfish/v1/Chassis/System.Embedded.1"
        d[c + "/Power"]["PowerSupplies"][1]["Status"] = {"Health": "Critical", "State": "Enabled"}
        d[c + "/Thermal"]["Temperatures"][1]["Status"] = {"Health": "Warning", "State": "Enabled"}
        d[c + "/Thermal"]["Temperatures"][1]["ReadingCelsius"] = 88
        snap = snapshot_of(d)
        self.assertEqual(one(snap, "psu", "PS2 Status")[0].status, "critical")
        self.assertEqual(one(snap, "temperature", "CPU1 Temp")[0].status, "warning")

    def test_status_without_health_is_unknown_never_ok(self):
        d = fixtures.dell_idrac9()
        for f in d["/redfish/v1/Chassis/System.Embedded.1/Thermal"]["Fans"]:
            f["Status"] = {}
        self.assertEqual({c.status for c in one(snapshot_of(d), "fan")}, {"unknown"})

    def test_threshold_derived_status_only_when_the_bmc_gives_no_health(self):
        d = fixtures.dell_idrac9()
        t = d["/redfish/v1/Chassis/System.Embedded.1/Thermal"]["Temperatures"][0]
        t["Status"] = {"State": "Enabled"}
        t["ReadingCelsius"] = 45
        self.assertEqual(one(snapshot_of(d), "temperature", "System Board Inlet Temp")[0].status, "warning")


class MissingSections(SimpleTestCase):
    def test_no_thermal_no_power_is_missing_not_ok(self):
        d = fixtures.dell_idrac9()
        c = "/redfish/v1/Chassis/System.Embedded.1"
        del d[c + "/Thermal"], d[c + "/Power"]
        snap = snapshot_of(d)
        self.assertEqual((snap.sections["thermal"], snap.sections["power"]), ("missing", "missing"))
        self.assertEqual(kinds(snap)["fan"] + kinds(snap)["temperature"] + kinds(snap)["psu"], 0)

    def test_no_chassis_links_falls_back_to_the_chassis_collection(self):
        d = fixtures.dell_idrac9()
        del d["/redfish/v1/Systems/System.Embedded.1"]["Links"]
        self.assertEqual(kinds(snapshot_of(d))["fan"], 6)

    def test_inventory_skipped_leaves_the_sections_skipped(self):
        snap = snapshot_of(fixtures.dell_idrac9(), inventory=False)
        self.assertEqual(snap.sections["storage"], "skipped")
        self.assertEqual(kinds(snap)["drive"] + kinds(snap)["dimm"], 0)

    def test_one_unreadable_section_does_not_hide_the_rest(self):
        client = fixtures.FixtureClient(fixtures.dell_idrac9(), fail={"/redfish/v1/Chassis/System.Embedded.1/Power": redfish.ProtocolError("x")})
        snap = parsers.parse(collector.collect(client))
        self.assertEqual(snap.sections["power"], "error")
        self.assertEqual(kinds(snap)["fan"], 6)

    def test_a_root_without_links_gives_missing_sections(self):
        client = fixtures.FixtureClient({"/redfish/v1": {"Name": "x"}})
        c = collector.collect(client)          # a root with no links: every section missing, but the root answered
        self.assertEqual(c["sections"]["system"], "missing")

    def test_login_failure_propagates(self):
        client = fixtures.FixtureClient(fixtures.dell_idrac9(), fail={"/redfish/v1": redfish.AuthFailed("no")})
        with self.assertRaises(redfish.AuthFailed):
            collector.collect(client)

    def test_unknown_vendor_is_generic(self):
        d = fixtures.dell_idrac9()
        d["/redfish/v1"] = {"Name": "Root", "Systems": {"@odata.id": "/redfish/v1/Systems"}}
        d["/redfish/v1/Systems/System.Embedded.1"]["Manufacturer"] = "Acme"
        d["/redfish/v1/Managers/iDRAC.Embedded.1"]["Manufacturer"] = "Acme"
        d["/redfish/v1/Managers/iDRAC.Embedded.1"]["Model"] = "X"
        self.assertEqual(snapshot_of(d).vendor, "generic")

    def test_vendor_override_changes_the_parser_but_never_crashes(self):
        snap = snapshot_of(fixtures.dell_idrac9(), vendor_hint="hpe")
        self.assertEqual(snap.vendor, "hpe")
        self.assertGreater(len(snap.comps), 10)


class Hostile(SimpleTestCase):
    def test_control_characters_bidi_and_length_are_stripped(self):
        d = fixtures.dell_idrac9()
        t = d["/redfish/v1/Chassis/System.Embedded.1/Thermal"]["Temperatures"][0]
        t["Name"] = "Inlet\x00\x1b[31m‮<script>alert(1)</script>" + "A" * 500
        snap = snapshot_of(d)
        name = [c.name for c in snap.comps if c.kind == "temperature"][0]
        self.assertLessEqual(len(name), 120)
        for ch in ("\x00", "\x1b", "‮"):
            self.assertNotIn(ch, name)

    def test_nonfinite_and_absurd_numbers_are_dropped(self):
        d = fixtures.dell_idrac9()
        temps = d["/redfish/v1/Chassis/System.Embedded.1/Thermal"]["Temperatures"]
        temps[0]["ReadingCelsius"] = float("nan")
        temps[1]["ReadingCelsius"] = 1e30
        temps[2]["ReadingCelsius"] = True
        temps[3]["ReadingCelsius"] = "12.5"
        text = json.dumps(d)                                   # NaN survives json.dumps/loads in Python
        snap = snapshot_of(json.loads(text))
        self.assertEqual([c.reading for c in one(snap, "temperature")], [None, None, None, 12.5])

    def test_wrong_types_everywhere_do_not_raise(self):
        d = fixtures.dell_idrac9()
        c = "/redfish/v1/Chassis/System.Embedded.1"
        d[c + "/Thermal"]["Temperatures"] = "nope"
        d[c + "/Thermal"]["Fans"] = [1, None, "x", {"Name": 5, "Reading": "lots", "Status": "bad"}]
        d[c + "/Power"]["PowerSupplies"] = {"a": 1}
        d[c + "/Power"]["Voltages"] = [[], {"ReadingVolts": {"x": 1}}]
        d["/redfish/v1/Systems/System.Embedded.1"]["Status"] = ["OK"]
        snap = snapshot_of(d)
        self.assertEqual(len(one(snap, "fan")), 1)
        self.assertEqual(one(snap, "fan")[0].status, "unknown")

    def test_too_many_members_are_capped(self):
        d = fixtures.dell_idrac9()
        d[("/redfish/v1/Chassis/System.Embedded.1") + "/Thermal"]["Fans"] = [{"Name": f"F{i}", "Reading": i, "ReadingUnits": "RPM"} for i in range(1000)]
        self.assertEqual(len(one(snapshot_of(d), "fan")), 128)

    def test_log_entries_without_a_time_or_message_are_dropped_and_future_ones_too(self):
        d = fixtures.dell_idrac9()
        ents = d["/redfish/v1/Managers/iDRAC.Embedded.1/LogServices/Sel/Entries"]["Members"]
        ents += [{"Id": "x1", "Message": "no time"}, {"Id": "x2", "Created": "2999-01-01T00:00:00Z", "Message": "future"},
                 {"Id": "x3", "Created": "garbage", "Message": "bad"}, {"Id": "x4", "Created": "2025-06-26T11:00:00Z"}]
        ids = {e.entry_id for e in snapshot_of(d).events}
        self.assertFalse(ids & {"sel:x1", "sel:x2", "sel:x3", "sel:x4"})

    def test_hostile_links_are_never_followed(self):
        d = fixtures.dell_idrac9()
        s = d["/redfish/v1/Systems/System.Embedded.1"]
        s["Processors"] = {"@odata.id": "http://evil.example/redfish/v1/x"}
        s["Memory"] = {"@odata.id": "/redfish/v1/../../etc/passwd"}
        s["Storage"] = {"@odata.id": "//evil.example/redfish/v1/Storage"}
        s["EthernetInterfaces"] = {"@odata.id": "/redfish/v1/Systems/%2e%2e/x"}
        client = fixtures.FixtureClient(d)
        collector.collect(client)
        self.assertFalse([p for p in client.log if "evil" in p or ".." in p or p.startswith("//")])


class Helpers(SimpleTestCase):
    def test_valid_path(self):
        for ok in ("/redfish/v1/", "/redfish/v1", "/redfish/v1/Systems/System.Embedded.1", "/redfish/v1/Systems/1/Entries?$top=50&$skip=10",
                   "/redfish/v1/Storage/Drives/Disk.Bay.0:Enclosure.Internal.0-1:RAID.Integrated.1-1"):
            self.assertTrue(redfish.valid_path(ok), ok)
        for bad in ("http://x/redfish/v1", "//x/redfish/v1", "/redfish/v2/", "/redfish/v1/../x", "/redfish/v1/./x", "/redfish/v1/a b",
                    "/redfish/v1/%2e%2e/x", "/etc/passwd", "", None, 5, "/redfish/v1/x\r\nHost: evil", "/redfish/v1/" + "a" * 500):
            self.assertFalse(redfish.valid_path(bad), repr(bad))

    def test_timestamps(self):
        self.assertIsNotNone(parsers.parse_ts("2025-06-26T11:42:17-05:00"))
        self.assertIsNotNone(parsers.parse_ts("2025-06-26T11:42:17+0530"))
        self.assertIsNone(parsers.parse_ts("1970-01-01T00:00:00Z"))
