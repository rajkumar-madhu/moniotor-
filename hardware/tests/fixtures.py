"""Recorded-style Redfish documents, written from the public Redfish schema and the shape of what Dell iDRAC9 and HPE iLO 5
return. They are NOT captures from a real machine (none was available): the field names and nesting follow the schema, the
values are invented (RFC 5737 addresses never appear; names and serials are synthetic).

``dell_idrac9()`` / ``hpe_ilo5()`` return {path: document}; ``FixtureClient`` serves such a dict through the same
``get(path)`` interface as the real client, so ``collector.collect`` runs unchanged against a fixture. Variants are made by
mutating the returned dict (see the tests): a 404 is a missing key, a hostile value is a replaced field."""
import copy
import json

from hardware import redfish


def ok(health="OK", state="Enabled", rollup=None):
    st = {"Health": health, "State": state}
    if rollup:
        st["HealthRollup"] = rollup
    return st


def coll(path, members, **extra):
    return {"@odata.id": path, "Members": [{"@odata.id": m} for m in members], "Members@odata.count": len(members), **extra}


class FixtureClient:
    """Serves a {path: doc} dict like RedfishClient does: GET only, 404 for anything that is not in the dict."""

    def __init__(self, docs, fail=None):
        self.docs, self.fail, self.requests, self.log = docs, fail or {}, 0, []

    def get(self, path):
        self.requests += 1
        self.log.append(path)
        if not redfish.valid_path(path):
            raise redfish.ProtocolError("invalid path")
        bare = path.split("?", 1)[0].rstrip("/") or "/"
        if bare in self.fail:
            raise self.fail[bare]
        for key in (path, bare, bare + "/"):
            if key in self.docs:
                return copy.deepcopy(self.docs[key])
        raise redfish.NotFound("Not found on this BMC.")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def close(self):
        pass


def dump(docs):
    return json.dumps(docs, indent=1, sort_keys=True)


# ============================================================================= Dell iDRAC9 (PowerEdge R740)
S = "/redfish/v1/Systems/System.Embedded.1"
C = "/redfish/v1/Chassis/System.Embedded.1"
M = "/redfish/v1/Managers/iDRAC.Embedded.1"


def _entries(path, items):
    return {"@odata.id": path, "Members": items, "Members@odata.count": len(items)}


def dell_idrac9():
    d = {}
    d["/redfish/v1"] = {
        "@odata.id": "/redfish/v1", "Name": "Root Service", "RedfishVersion": "1.11.0", "Vendor": "Dell",
        "Product": "Integrated Dell Remote Access Controller", "UUID": "00000000-0000-0000-0000-000000000001",
        "Systems": {"@odata.id": "/redfish/v1/Systems"}, "Chassis": {"@odata.id": "/redfish/v1/Chassis"},
        "Managers": {"@odata.id": "/redfish/v1/Managers"}, "Oem": {"Dell": {"@odata.type": "#DellServiceRoot.v1_0_0.ServiceRootSummary"}},
    }
    d["/redfish/v1/Systems"] = coll("/redfish/v1/Systems", [S])
    d[S] = {
        "@odata.id": S, "Id": "System.Embedded.1", "Name": "System", "Manufacturer": "Dell Inc.", "Model": "PowerEdge R740",
        "SKU": "7F3K9H2", "SerialNumber": "CNEXAMPLE0001", "BiosVersion": "2.14.2", "HostName": "demo-srv-01", "PowerState": "On",
        "Status": ok(rollup="OK"), "ProcessorSummary": {"Count": 2, "Model": "Intel(R) Xeon(R) Gold 6230", "Status": ok()},
        "MemorySummary": {"TotalSystemMemoryGiB": 256, "Status": ok(rollup="OK")},
        "Processors": {"@odata.id": S + "/Processors"}, "Memory": {"@odata.id": S + "/Memory"},
        "Storage": {"@odata.id": S + "/Storage"}, "EthernetInterfaces": {"@odata.id": S + "/EthernetInterfaces"},
        "LogServices": {"@odata.id": S + "/LogServices"},
        "Links": {"Chassis": [{"@odata.id": C}], "ManagedBy": [{"@odata.id": M}]},
        "Oem": {"Dell": {"DellSystem": {"LifecycleControllerVersion": "6.10.30.00", "ManagedSystemSize": "1 U"}}},
    }
    d["/redfish/v1/Chassis"] = coll("/redfish/v1/Chassis", [C])
    d[C] = {"@odata.id": C, "Id": "System.Embedded.1", "Name": "Computer System Chassis", "Status": ok(rollup="OK"),
            "Thermal": {"@odata.id": C + "/Thermal"}, "Power": {"@odata.id": C + "/Power"},
            "PhysicalSecurity": {"IntrusionSensor": "Normal", "IntrusionSensorNumber": 115}}
    temps = [("iDRAC.Embedded.1#SystemBoardInletTemp", "System Board Inlet Temp", 24, 42, 47),
             ("iDRAC.Embedded.1#CPU1Temp", "CPU1 Temp", 32, 85, 90), ("iDRAC.Embedded.1#CPU2Temp", "CPU2 Temp", 34, 85, 90),
             ("iDRAC.Embedded.1#SystemBoardExhaustTemp", "System Board Exhaust Temp", 28, 70, 75)]
    d[C + "/Thermal"] = {
        "@odata.id": C + "/Thermal", "Id": "Thermal", "Name": "Thermal",
        "Temperatures": [{"@odata.id": C + "/Sensors/Temperatures/" + k.split("#")[1], "MemberId": k, "Name": n, "ReadingCelsius": r,
                          "UpperThresholdNonCritical": w, "UpperThresholdCritical": c, "LowerThresholdNonCritical": 3,
                          "LowerThresholdCritical": -7, "PhysicalContext": "SystemBoard", "Status": ok()} for k, n, r, w, c in temps],
        "Fans": [{"MemberId": f"0x17||Fan.Embedded.{i}A", "FanName": f"System Board Fan{i}A", "Name": f"System Board Fan{i}A",
                  "Reading": 8000 + 100 * i, "ReadingUnits": "RPM", "LowerThresholdNonCritical": 1500, "LowerThresholdCritical": 800,
                  "PhysicalContext": "SystemBoard", "Status": ok()} for i in range(1, 7)],
    }
    d[C + "/Power"] = {
        "@odata.id": C + "/Power", "Id": "Power", "Name": "Power",
        "PowerControl": [{"MemberId": "PowerControl", "PowerConsumedWatts": 420, "PowerCapacityWatts": 1500}],
        "PowerSupplies": [
            {"MemberId": "PSU.Slot.1", "Name": "PS1 Status", "Model": "PWR SPLY,750W,RDNT,DELTA", "Manufacturer": "DELL",
             "SerialNumber": "CNEXAMPLEPS01", "PartNumber": "0PJMDNA00", "FirmwareVersion": "00.1B.53", "PowerCapacityWatts": 750,
             "LastPowerOutputWatts": 212, "LineInputVoltage": 230, "PowerSupplyType": "AC", "Status": ok()},
            {"MemberId": "PSU.Slot.2", "Name": "PS2 Status", "Model": "PWR SPLY,750W,RDNT,DELTA", "Manufacturer": "DELL",
             "SerialNumber": "CNEXAMPLEPS02", "PartNumber": "0PJMDNA00", "FirmwareVersion": "00.1B.53", "PowerCapacityWatts": 750,
             "LastPowerOutputWatts": 208, "LineInputVoltage": 229, "PowerSupplyType": "AC", "Status": ok()}],
        "Voltages": [
            {"MemberId": "iDRAC.Embedded.1#SystemBoard12VPG", "Name": "System Board 12V PG", "ReadingVolts": 12.1,
             "LowerThresholdNonCritical": 11.4, "UpperThresholdNonCritical": 12.6, "LowerThresholdCritical": 10.8, "UpperThresholdCritical": 13.2, "Status": ok()},
            {"MemberId": "iDRAC.Embedded.1#SystemBoard5VPG", "Name": "System Board 5V PG", "ReadingVolts": 5.0,
             "LowerThresholdNonCritical": 4.8, "UpperThresholdNonCritical": 5.2, "Status": ok()},
            {"MemberId": "iDRAC.Embedded.1#SystemBoard3.3VPG", "Name": "System Board 3.3V PG", "ReadingVolts": 3.3,
             "LowerThresholdNonCritical": 3.1, "UpperThresholdNonCritical": 3.5, "Status": ok()}],
    }
    d[M] = {"@odata.id": M, "Id": "iDRAC.Embedded.1", "Name": "Manager", "Model": "14G Monolithic", "ManagerType": "BMC",
            "FirmwareVersion": "6.10.30.00", "Manufacturer": "Dell Inc.", "LogServices": {"@odata.id": M + "/LogServices"},
            "Status": ok(rollup="OK")}
    d["/redfish/v1/Managers"] = coll("/redfish/v1/Managers", [M])
    d[M + "/LogServices"] = coll(M + "/LogServices", [M + "/LogServices/Sel", M + "/LogServices/Lclog"])
    d[M + "/LogServices/Sel"] = {"@odata.id": M + "/LogServices/Sel", "Id": "Sel", "Name": "System Event Log",
                                  "Entries": {"@odata.id": M + "/LogServices/Sel/Entries"}}
    d[M + "/LogServices/Lclog"] = {"@odata.id": M + "/LogServices/Lclog", "Id": "Lclog", "Name": "Lifecycle Controller Log",
                                    "Entries": {"@odata.id": M + "/LogServices/Lclog/Entries"}}
    d[M + "/LogServices/Sel/Entries"] = _entries(M + "/LogServices/Sel/Entries", [
        {"Id": "3", "Name": "Log Entry 3", "Created": "2025-06-26T11:42:17-05:00", "EntryType": "SEL", "Severity": "OK",
         "SensorType": "Power Supply", "Message": "The power supply redundancy is restored.", "MessageId": "PSU0001"},
        {"Id": "2", "Name": "Log Entry 2", "Created": "2025-06-26T10:58:01-05:00", "EntryType": "SEL", "Severity": "Warning",
         "SensorType": "Temperature", "Message": "The inlet temperature is above the upper warning threshold.", "MessageId": "TMP0120"},
        {"Id": "1", "Name": "Log Entry 1", "Created": "2025-06-26T08:44:30-05:00", "EntryType": "SEL", "Severity": "Warning",
         "SensorType": "Fan", "Message": "Fan 2 RPM is outside of range.", "MessageId": "FAN0001"}])
    d[M + "/LogServices/Lclog/Entries"] = _entries(M + "/LogServices/Lclog/Entries", [
        {"Id": "9", "Name": "Log Entry 9", "Created": "2025-06-26T07:15:09-05:00", "EntryType": "Oem", "Severity": "OK",
         "Message": "iDRAC firmware update completed successfully.", "MessageId": "SUP1906"}])
    d[S + "/LogServices"] = coll(S + "/LogServices", [])
    d[S + "/Processors"] = coll(S + "/Processors", [S + "/Processors/CPU.Socket.1", S + "/Processors/CPU.Socket.2"])
    for i in (1, 2):
        d[S + f"/Processors/CPU.Socket.{i}"] = {
            "@odata.id": S + f"/Processors/CPU.Socket.{i}", "Id": f"CPU.Socket.{i}", "Name": f"CPU {i}", "Socket": f"CPU.Socket.{i}",
            "ProcessorType": "CPU", "Model": "Intel(R) Xeon(R) Gold 6230 CPU @ 2.10GHz", "Manufacturer": "Intel", "TotalCores": 20,
            "TotalThreads": 40, "MaxSpeedMHz": 4000, "Status": ok()}
    slots = ["A1", "A2", "B1", "B2", "A3"]
    d[S + "/Memory"] = coll(S + "/Memory", [S + f"/Memory/DIMM.Socket.{s}" for s in slots])
    for s in slots:
        absent = s == "A3"
        d[S + f"/Memory/DIMM.Socket.{s}"] = {
            "@odata.id": S + f"/Memory/DIMM.Socket.{s}", "Id": f"DIMM.Socket.{s}", "Name": f"DIMM {s}", "DeviceLocator": f"DIMM {s}",
            "CapacityMiB": 0 if absent else 65536, "MemoryDeviceType": "DDR4", "OperatingSpeedMhz": 2933, "Manufacturer": "Hynix",
            "PartNumber": "HMA84GR7CJR4N-WM", "SerialNumber": f"EXDIMM{s}",
            "Status": {"Health": None, "State": "Absent"} if absent else ok()}
    d[S + "/Storage"] = coll(S + "/Storage", [S + "/Storage/RAID.Integrated.1-1"])
    d[S + "/Storage/RAID.Integrated.1-1"] = {
        "@odata.id": S + "/Storage/RAID.Integrated.1-1", "Id": "RAID.Integrated.1-1", "Name": "PERC H730P Mini",
        "Status": ok(rollup="OK"), "StorageControllers": [{"Name": "PERC H730P Mini", "Model": "PERC H730P Mini",
                                                            "FirmwareVersion": "25.5.9.0001", "Manufacturer": "DELL", "Status": ok()}],
        "Drives": [{"@odata.id": S + "/Storage/Drives/Disk.Bay.0:Enclosure.Internal.0-1:RAID.Integrated.1-1"},
                   {"@odata.id": S + "/Storage/Drives/Disk.Bay.1:Enclosure.Internal.0-1:RAID.Integrated.1-1"}]}
    for i in (0, 1):
        p = S + f"/Storage/Drives/Disk.Bay.{i}:Enclosure.Internal.0-1:RAID.Integrated.1-1"
        d[p] = {"@odata.id": p, "Id": f"Disk.Bay.{i}:Enclosure.Internal.0-1:RAID.Integrated.1-1", "Name": f"Physical Disk 0:1:{i}",
                "Model": "ST1200MM0099", "SerialNumber": f"EXDISK{i}", "CapacityBytes": 1200243695616, "MediaType": "HDD",
                "Protocol": "SAS", "Revision": "ST31", "PredictedMediaLifeLeftPercent": None, "Status": ok()}
    d[S + "/EthernetInterfaces"] = coll(S + "/EthernetInterfaces", [S + "/EthernetInterfaces/NIC.Integrated.1-1-1",
                                                                      S + "/EthernetInterfaces/NIC.Integrated.1-2-1"])
    for i in (1, 2):
        p = S + f"/EthernetInterfaces/NIC.Integrated.1-{i}-1"
        d[p] = {"@odata.id": p, "Id": f"NIC.Integrated.1-{i}-1", "Name": f"System Ethernet Interface NIC.Integrated.1-{i}-1",
                "MACAddress": f"00:11:22:33:44:5{i}", "SpeedMbps": 10000, "LinkStatus": "LinkUp", "Status": ok()}
    d["/redfish/v1/LicenseService/Licenses"] = coll("/redfish/v1/LicenseService/Licenses", ["/redfish/v1/LicenseService/Licenses/EX1"])
    d["/redfish/v1/LicenseService/Licenses/EX1"] = {"@odata.id": "/redfish/v1/LicenseService/Licenses/EX1", "Id": "EX1",
                                                     "Description": "iDRAC9 Enterprise License", "LicenseType": "Production"}
    return d


# ============================================================================= HPE iLO 5 (ProLiant DL380 Gen10)
def hpe_ilo5():
    d = {}
    d["/redfish/v1"] = {
        "@odata.id": "/redfish/v1/", "Name": "HPE RESTful API", "RedfishVersion": "1.6.0", "UUID": "00000000-0000-0000-0000-000000000002",
        "Systems": {"@odata.id": "/redfish/v1/Systems/"}, "Chassis": {"@odata.id": "/redfish/v1/Chassis/"},
        "Managers": {"@odata.id": "/redfish/v1/Managers/"},
        "Oem": {"Hpe": {"@odata.type": "#HpeiLOServiceExt.v2_3_0.HpeiLOServiceExt", "Manager": [
            {"DefaultLanguage": "en", "FQDN": "ilo-demo.example.com", "HostName": "ilo-demo", "Languages": [], "ManagerFirmwareVersion": "2.78",
             "ManagerType": "iLO 5"}]}}}
    d["/redfish/v1/Systems"] = coll("/redfish/v1/Systems/", ["/redfish/v1/Systems/1/"])
    s, c, m = "/redfish/v1/Systems/1", "/redfish/v1/Chassis/1", "/redfish/v1/Managers/1"
    d[s] = {"@odata.id": s + "/", "Id": "1", "Name": "Computer System", "Manufacturer": "HPE", "Model": "ProLiant DL380 Gen10",
            "SKU": "868703-B21", "SerialNumber": "CZEXAMPLE01", "BiosVersion": "U30 v2.72 (06/20/2022)", "HostName": "demo-hpe-01",
            "PowerState": "On", "Status": {"Health": "OK", "HealthRollup": "OK", "State": "Enabled"},
            "ProcessorSummary": {"Count": 2, "Model": "Intel(R) Xeon(R) Silver 4210", "Status": {"HealthRollup": "OK"}},
            "MemorySummary": {"TotalSystemMemoryGiB": 128, "Status": {"HealthRollup": "OK"}},
            "Processors": {"@odata.id": s + "/Processors/"}, "Memory": {"@odata.id": s + "/Memory/"},
            "Storage": {"@odata.id": s + "/Storage/"}, "EthernetInterfaces": {"@odata.id": s + "/EthernetInterfaces/"},
            "LogServices": {"@odata.id": s + "/LogServices/"},
            "Links": {"Chassis": [{"@odata.id": c + "/"}], "ManagedBy": [{"@odata.id": m + "/"}]}}
    d["/redfish/v1/Chassis"] = coll("/redfish/v1/Chassis/", [c + "/"])
    d[c] = {"@odata.id": c + "/", "Id": "1", "Name": "Computer System Chassis", "Status": {"Health": "OK", "State": "Enabled"},
            "Thermal": {"@odata.id": c + "/Thermal/"}, "Power": {"@odata.id": c + "/Power/"}}
    d[c + "/Thermal"] = {
        "@odata.id": c + "/Thermal/", "Id": "Thermal", "Name": "Thermal",
        "Temperatures": [
            {"MemberId": "0", "Name": "01-Inlet Ambient", "ReadingCelsius": 22, "UpperThresholdCritical": 42, "UpperThresholdFatal": 47,
             "PhysicalContext": "Intake", "Status": {"Health": "OK", "State": "Enabled"}},
            {"MemberId": "1", "Name": "02-CPU 1", "ReadingCelsius": 40, "UpperThresholdCritical": 70, "UpperThresholdFatal": 75,
             "PhysicalContext": "CPU", "Status": {"Health": "OK", "State": "Enabled"}},
            {"MemberId": "2", "Name": "03-CPU 2", "ReadingCelsius": 41, "UpperThresholdCritical": 70, "UpperThresholdFatal": 75,
             "PhysicalContext": "CPU", "Status": {"Health": "OK", "State": "Enabled"}},
            {"MemberId": "3", "Name": "14-HD Max", "ReadingCelsius": None, "Status": {"State": "Absent"}}],
        "Fans": [{"MemberId": str(i), "FanName": f"Fan {i + 1}", "CurrentReading": 28 + i, "Units": "Percent",
                  "Status": {"Health": "OK", "State": "Enabled"}} for i in range(4)]}
    d[c + "/Power"] = {
        "@odata.id": c + "/Power/", "Id": "Power", "Name": "PowerMetrics",
        "PowerControl": [{"MemberId": "0", "PowerConsumedWatts": 320, "PowerCapacityWatts": 1600}],
        "PowerSupplies": [
            {"MemberId": "0", "Name": "HpeServerPowerSupply", "Model": "865408-B21", "SerialNumber": "EXPSU01", "FirmwareVersion": "1.00",
             "LastPowerOutputWatts": 160, "PowerCapacityWatts": 800, "LineInputVoltage": 231, "PowerSupplyType": "AC",
             "Oem": {"Hpe": {"BayNumber": 1, "Hotplug": True}}, "Status": {"Health": "OK", "State": "Enabled"}},
            {"MemberId": "1", "Name": "HpeServerPowerSupply", "Model": "865408-B21", "SerialNumber": "EXPSU02", "FirmwareVersion": "1.00",
             "LastPowerOutputWatts": 158, "PowerCapacityWatts": 800, "LineInputVoltage": 230, "PowerSupplyType": "AC",
             "Oem": {"Hpe": {"BayNumber": 2, "Hotplug": True}}, "Status": {"Health": "OK", "State": "Enabled"}}],
        "Oem": {"Hpe": {"SmartStorageBattery": [{"Present": True, "Model": "875241-B21", "SerialNumber": "EXBAT01",
                                                  "FirmwareVersion": "0.70", "Status": {"Health": "OK", "State": "Enabled"}}]}}}
    d[m] = {"@odata.id": m + "/", "Id": "1", "Name": "Manager", "ManagerType": "BMC", "Model": "iLO 5", "FirmwareVersion": "iLO 5 v2.78",
            "LogServices": {"@odata.id": m + "/LogServices/"}, "Status": {"Health": "OK", "State": "Enabled"},
            "Oem": {"Hpe": {"License": {"LicenseKey": "XXXXX-XXXXX", "LicenseString": "iLO Advanced", "LicenseType": "Perpetual"}}}}
    d["/redfish/v1/Managers"] = coll("/redfish/v1/Managers/", [m + "/"])
    d[m + "/LogServices"] = coll(m + "/LogServices/", [m + "/LogServices/IEL/"])
    d[m + "/LogServices/IEL"] = {"@odata.id": m + "/LogServices/IEL/", "Id": "IEL", "Name": "Integrated Event Log",
                                  "Entries": {"@odata.id": m + "/LogServices/IEL/Entries/"}}
    d[m + "/LogServices/IEL/Entries"] = _entries(m + "/LogServices/IEL/Entries/", [
        {"Id": "21", "Created": "2025-06-26T09:00:00Z", "EntryType": "Oem", "Severity": "OK", "Message": "iLO network link up.",
         "Oem": {"Hpe": {"Class": 8, "Code": 1, "Category": "iLO"}}}])
    d[s + "/LogServices"] = coll(s + "/LogServices/", [s + "/LogServices/IML/"])
    d[s + "/LogServices/IML"] = {"@odata.id": s + "/LogServices/IML/", "Id": "IML", "Name": "Integrated Management Log",
                                  "Entries": {"@odata.id": s + "/LogServices/IML/Entries/"}}
    d[s + "/LogServices/IML/Entries"] = _entries(s + "/LogServices/IML/Entries/", [
        {"Id": "7", "Created": "2025-06-26T10:10:10Z", "EntryType": "Oem", "Severity": "Warning",
         "Message": "Fan Inserted: Fan 4.", "Oem": {"Hpe": {"Class": 5, "Code": 12, "Category": "Maintenance"}}},
        {"Id": "6", "Created": "2025-06-26T08:00:00Z", "EntryType": "Oem", "Severity": "Critical",
         "Message": "Uncorrectable Machine Check Exception (Board 0, Processor 1).",
         "Oem": {"Hpe": {"Class": 5, "Code": 3, "Category": "Maintenance"}}}])
    d[s + "/Processors"] = coll(s + "/Processors/", [s + "/Processors/1/", s + "/Processors/2/"])
    for i in (1, 2):
        d[s + f"/Processors/{i}"] = {"@odata.id": s + f"/Processors/{i}/", "Id": str(i), "Name": "Processors", "Socket": f"Proc {i}",
                                      "ProcessorType": "CPU", "Model": "Intel(R) Xeon(R) Silver 4210 CPU @ 2.20GHz",
                                      "Manufacturer": "Intel(R) Corporation", "TotalCores": 10, "TotalThreads": 20, "MaxSpeedMHz": 4000,
                                      "Status": {"Health": "OK", "State": "Enabled"}}
    d[s + "/Memory"] = coll(s + "/Memory/", [s + "/Memory/proc1dimm1/", s + "/Memory/proc1dimm2/"])
    for i in (1, 2):
        d[s + f"/Memory/proc1dimm{i}"] = {"@odata.id": s + f"/Memory/proc1dimm{i}/", "Id": f"proc1dimm{i}", "Name": f"proc1dimm{i}",
                                           "DeviceLocator": f"PROC 1 DIMM {i}", "CapacityMiB": 32768, "MemoryDeviceType": "DDR4",
                                           "OperatingSpeedMhz": 2400, "Manufacturer": "HPE", "PartNumber": "840758-091",
                                           "SerialNumber": f"EXHPDIMM{i}", "Status": {"Health": "OK", "State": "Enabled"}}
    d[s + "/Storage"] = coll(s + "/Storage/", [s + "/Storage/DE00A000/"])
    d[s + "/Storage/DE00A000"] = {"@odata.id": s + "/Storage/DE00A000/", "Id": "DE00A000", "Name": "HPE Smart Array P408i-a SR Gen10",
                                   "Status": {"Health": "OK", "State": "Enabled"},
                                   "StorageControllers": [{"Model": "HPE Smart Array P408i-a SR Gen10", "FirmwareVersion": "2.65",
                                                           "Manufacturer": "HPE", "Status": {"Health": "OK", "State": "Enabled"}}],
                                   "Drives": [{"@odata.id": s + "/Storage/DE00A000/Drives/0/"}]}
    d[s + "/Storage/DE00A000/Drives/0"] = {"@odata.id": s + "/Storage/DE00A000/Drives/0/", "Id": "0", "Name": "Drive 1:1:1",
                                            "Model": "MO000800JWTBR", "SerialNumber": "EXHPDRV0", "CapacityBytes": 800166076416,
                                            "MediaType": "SSD", "Protocol": "SAS", "Revision": "HPD4", "PredictedMediaLifeLeftPercent": 99,
                                            "Status": {"Health": "OK", "State": "Enabled"}}
    d[s + "/EthernetInterfaces"] = coll(s + "/EthernetInterfaces/", [s + "/EthernetInterfaces/1/"])
    d[s + "/EthernetInterfaces/1"] = {"@odata.id": s + "/EthernetInterfaces/1/", "Id": "1", "Name": "", "MACAddress": "AA:BB:CC:00:11:22",
                                       "SpeedMbps": 1000, "LinkStatus": "LinkUp", "Status": {"Health": "OK", "State": "Enabled"}}
    return d
