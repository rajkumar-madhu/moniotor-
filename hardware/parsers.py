"""Turn what a BMC sent (Redfish JSON documents) into plain, bounded, safe values.

Everything here is a pure function of the documents: no network, no database. Rules:

* a BMC is untrusted: text is length-capped and stripped of control characters (``text``), numbers must be finite and
  sensible (``num``), unknown fields are ignored and nothing is ever used as a URL, host or path here;
* vendors differ (Dell iDRAC9, HPE iLO 5/6): one parser handles the standard Redfish schema (Thermal, Power, Systems,
  Chassis, Managers, Storage, Memory, Processors, LogServices) and reads the few well-known OEM spots for each vendor;
* a section the BMC did not report is ``missing``, never OK: its tile says "not reported" and the roll-up does not count it
  as healthy. A component whose Status is absent is ``unknown``, not ``ok``.
"""
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

OK, WARNING, CRITICAL, UNKNOWN = "ok", "warning", "critical", "unknown"
RANK = {UNKNOWN: 0, OK: 1, WARNING: 2, CRITICAL: 3}

_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f​-‏ -‮⁦-⁩﻿]")
MAX_COMPONENTS = 400


def text(value, limit=120):
    """A BMC string made safe to store and show: control and bidi characters removed, whitespace collapsed, capped."""
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return ""
        value = str(value)
    if not isinstance(value, str):
        return ""
    return " ".join(_CONTROL.sub(" ", value[: limit * 4]).split())[:limit]


def num(value, lo=-1e6, hi=1e9):
    """A finite float inside [lo, hi], else None. Booleans and out-of-range numbers are not numbers."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        if len(value) > 24:
            return None
        try:
            value = float(value)
        except ValueError:
            return None
    if not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)
    except OverflowError:                       # an integer with hundreds of digits
        return None
    if not math.isfinite(value) or value < lo or value > hi:
        return None
    return value


def get(doc, *path, default=None):
    """doc[a][b][c] without raising on a missing key or a non-dict in the way."""
    cur = doc
    for p in path:
        if isinstance(cur, dict):
            cur = cur.get(p)
        elif isinstance(cur, list) and isinstance(p, int) and -len(cur) <= p < len(cur):
            cur = cur[p]
        else:
            return default
        if cur is None:
            return default
    return cur


def as_list(value, limit=64):
    return [v for v in value[:limit] if isinstance(v, dict)] if isinstance(value, list) else []


_HEALTH = {"ok": OK, "warning": WARNING, "critical": CRITICAL}
_NOT_READING = {"disabled", "unavailableoffline", "standbyoffline", "deferring", "quiesced", "starting", "updating",
                "standbyspare", "inttest"}


def status_of(status_obj):
    """(status, health text, state text, absent) from a Redfish Status object. No Status or Health = unknown."""
    st = status_obj if isinstance(status_obj, dict) else {}
    health = text(st.get("HealthRollup") if st.get("HealthRollup") else st.get("Health"), 24)
    state = text(st.get("State"), 24)
    absent = state.lower() == "absent"
    status = _HEALTH.get(health.lower(), UNKNOWN)       # no Health is unknown, never OK
    return status, health, state, absent


def worst(statuses):
    """The most severe of the statuses (unknown ranks lowest, so it never hides a real warning)."""
    out = UNKNOWN
    for s in statuses:
        if RANK.get(s, 0) > RANK[out]:
            out = s
    return out


@dataclass
class Comp:
    kind: str
    key: str
    name: str
    reading: Optional[float] = None
    unit: str = ""
    warn: Optional[float] = None
    crit: Optional[float] = None
    low_warn: Optional[float] = None
    low_crit: Optional[float] = None
    status: str = UNKNOWN
    health: str = ""
    state: str = ""
    attrs: dict = field(default_factory=dict)
    position: int = 0


@dataclass
class Entry:
    entry_id: str
    ts: datetime
    severity: str
    sensor: str
    message: str
    log: str


@dataclass
class Snapshot:
    vendor: str
    overview: dict
    power_state: str
    comps: list
    events: list
    sections: dict           # section -> "ok" | "missing" | "error" | "skipped"
    bmc_status: str          # the BMC's own roll-up (System HealthRollup), or unknown


def _key(doc, fallback):
    member = text(doc.get("MemberId") or doc.get("Id"), 80)
    if member:
        return member
    oid = text(get(doc, "@odata.id"), 200)
    return oid.rsplit("/", 1)[-1] if oid else text(fallback, 80)


def _uniq(comps):
    """Give components of one kind distinct names ('PSU' twice becomes 'PSU 1' and 'PSU 2') and distinct keys."""
    seen, dup, keys = {}, set(), set()
    for c in comps:
        seen[c.name] = seen.get(c.name, 0) + 1
    for c in comps:
        if seen[c.name] > 1:
            dup.add(c.name)
    counter = {}
    for i, c in enumerate(comps):
        if c.name in dup:
            counter[c.name] = counter.get(c.name, 0) + 1
            c.name = f"{c.name} {counter[c.name]}"[:160]
        k = c.key
        n = 2
        while k in keys:
            k = f"{c.key}#{n}"
            n += 1
        c.key = k[:200]
        keys.add(c.key)
        c.position = i
    return comps


def _distinct(comps):
    """No two components of the whole poll share (kind, key): the database has a unique row per pair, and a BMC that lists the same
    id twice (in two sections, or by mistake) must not make the whole poll fail to be stored."""
    taken, out = set(), []
    for c in comps:
        k, n = c.key, 2
        while (c.kind, k) in taken:
            k = f"{c.key[:190]}#{n}"
            n += 1
        c.key = k
        taken.add((c.kind, k))
        out.append(c)
    return out


# ----------------------------------------------------------------------------- vendor
def detect_vendor(root, system=None, manager=None):
    """dell | hpe | generic, from the service root, the system and the manager."""
    blob = " ".join(text(x, 80).lower() for x in (
        get(root, "Vendor"), get(root, "Product"), get(system, "Manufacturer"), get(manager, "Manufacturer"),
        get(manager, "Model"), get(manager, "FirmwareVersion"))).strip()
    oem = get(root, "Oem")
    oem_keys = {str(k).lower() for k in oem} if isinstance(oem, dict) else set()
    if "dell" in oem_keys or "dell" in blob:
        return "dell"
    if oem_keys & {"hpe", "hp"} or "hpe" in blob or "hewlett" in blob or re.search(r"\bilo\b", blob):
        return "hpe"
    return "generic"


def bmc_model(vendor, manager):
    model = text(get(manager, "Model"), 60)
    if vendor == "dell":
        gen = re.match(r"^(\d{2})G", model)
        if gen:
            return {"12": "iDRAC7", "13": "iDRAC8", "14": "iDRAC9", "15": "iDRAC9", "16": "iDRAC9", "17": "iDRAC10"}.get(gen.group(1), "iDRAC")
        return "iDRAC"
    if vendor == "hpe":
        fw = text(get(manager, "FirmwareVersion"), 60)
        m = re.match(r"^(iLO\s?\d+)", fw, re.I) or re.match(r"^(iLO\s?\d+)", model, re.I)
        return m.group(1) if m else (model or "iLO")
    return model or text(get(manager, "Name"), 60) or "BMC"


# ----------------------------------------------------------------------------- sections
def parse_thermal(doc):
    out_t, out_f = [], []
    for i, t in enumerate(as_list(get(doc, "Temperatures"), 128)):
        status, health, state, absent = status_of(t.get("Status"))
        if absent:
            continue
        reading = num(t.get("ReadingCelsius"), -100, 1000)
        warn = num(t.get("UpperThresholdNonCritical"), -100, 1000)
        crit = num(t.get("UpperThresholdCritical"), -100, 1000)
        if crit is None:
            crit = num(t.get("UpperThresholdFatal"), -100, 1000)
        if status == UNKNOWN and not health and reading is not None and (warn is not None or crit is not None):
            status = CRITICAL if crit is not None and reading >= crit else WARNING if warn is not None and reading >= warn else OK
        if state.lower() in _NOT_READING and reading is None:
            status = UNKNOWN
        out_t.append(Comp("temperature", _key(t, f"t{i}"), text(t.get("Name"), 120) or f"Temperature {i + 1}", reading, "°C",
                          warn, crit, num(t.get("LowerThresholdNonCritical"), -100, 1000),
                          num(t.get("LowerThresholdCritical"), -100, 1000), status, health, state,
                          {"context": text(t.get("PhysicalContext"), 40)}))
    for i, f in enumerate(as_list(get(doc, "Fans"), 128)):
        status, health, state, absent = status_of(f.get("Status"))
        if absent:
            continue
        reading = num(f.get("Reading") if f.get("Reading") is not None else f.get("CurrentReading"), 0, 1e6)
        raw_unit = text(f.get("ReadingUnits") or f.get("Units"), 12).lower()
        unit = "%" if raw_unit in ("percent", "%") else "RPM" if raw_unit in ("rpm", "") else text(raw_unit, 12)
        out_f.append(Comp("fan", _key(f, f"f{i}"), text(f.get("Name") or f.get("FanName"), 120) or f"Fan {i + 1}", reading, unit,
                          None, None, num(f.get("LowerThresholdNonCritical"), 0, 1e6), num(f.get("LowerThresholdCritical"), 0, 1e6),
                          status, health, state, {"context": text(f.get("PhysicalContext"), 40)}))
    return _uniq(out_t), _uniq(out_f)


def parse_power(doc):
    psus, volts, batteries = [], [], []
    for i, p in enumerate(as_list(get(doc, "PowerSupplies"), 32)):
        status, health, state, absent = status_of(p.get("Status"))
        if absent:
            continue
        out_w = num(p.get("PowerOutputWatts") if p.get("PowerOutputWatts") is not None else p.get("LastPowerOutputWatts"), 0, 1e6)
        cap = num(p.get("PowerCapacityWatts"), 0, 1e6)
        bay = get(p, "Oem", "Hpe", "BayNumber")
        name = f"PSU {bay}" if isinstance(bay, int) and not isinstance(bay, bool) else text(p.get("Name"), 120) or f"PSU {i + 1}"
        psus.append(Comp("psu", _key(p, f"p{i}"), name, out_w, "W", None, None, None, None, status, health, state, {
            "model": text(p.get("Model"), 60), "manufacturer": text(p.get("Manufacturer"), 60),
            "serial": text(p.get("SerialNumber"), 60), "part": text(p.get("PartNumber"), 60),
            "firmware": text(p.get("FirmwareVersion"), 40), "capacity_w": cap,
            "input_v": num(p.get("LineInputVoltage"), 0, 1000), "input_w": num(p.get("PowerInputWatts"), 0, 1e6),
            "type": text(p.get("PowerSupplyType"), 20),
            "load_pct": round(100 * out_w / cap, 1) if out_w is not None and cap else None}))
    for i, v in enumerate(as_list(get(doc, "Voltages"), 128)):
        status, health, state, absent = status_of(v.get("Status"))
        if absent:
            continue
        reading = num(v.get("ReadingVolts"), -1000, 1000)
        up_w, up_c = num(v.get("UpperThresholdNonCritical"), -1000, 1000), num(v.get("UpperThresholdCritical"), -1000, 1000)
        lo_w, lo_c = num(v.get("LowerThresholdNonCritical"), -1000, 1000), num(v.get("LowerThresholdCritical"), -1000, 1000)
        if status == UNKNOWN and not health and reading is not None:
            hi, lo = up_c if up_c is not None else up_w, lo_c if lo_c is not None else lo_w
            if hi is not None or lo is not None:
                status = CRITICAL if (hi is not None and reading > hi) or (lo is not None and reading < lo) else OK
        volts.append(Comp("voltage", _key(v, f"v{i}"), text(v.get("Name"), 120) or f"Voltage {i + 1}", reading, "V",
                          up_w, up_c, lo_w, lo_c, status, health, state, {}))
    # HPE Smart Array / Smart Storage battery lives in the OEM block of the Power resource
    for i, b in enumerate(as_list(get(doc, "Oem", "Hpe", "SmartStorageBattery"), 8)):
        status, health, state, absent = status_of(b.get("Status"))
        if absent or b.get("Present") is False:
            continue
        batteries.append(Comp("battery", _key(b, f"b{i}"), text(b.get("Model") or b.get("ProductName"), 80) or f"Battery {i + 1}",
                              None, "", None, None, None, None, status, health, state,
                              {"serial": text(b.get("SerialNumber"), 60), "firmware": text(b.get("FirmwareVersion"), 40)}))
    return _uniq(psus), _uniq(volts), _uniq(batteries)


def parse_intrusion(chassis):
    sensor = text(get(chassis, "PhysicalSecurity", "IntrusionSensor"), 40)
    if not sensor:
        return None
    low = sensor.lower()
    status = OK if low == "normal" else WARNING if low in ("hardwareintrusion", "tamperingdetected") else UNKNOWN
    return Comp("intrusion", "intrusion", "Chassis intrusion", None, "", None, None, None, None, status, sensor, "", {})


def parse_processors(docs):
    out = []
    for i, p in enumerate(docs):
        status, health, state, absent = status_of(p.get("Status"))
        ptype = text(p.get("ProcessorType"), 20).lower()
        if absent or (ptype and ptype != "cpu"):
            continue
        out.append(Comp("cpu", _key(p, f"c{i}"), text(p.get("Socket") or p.get("Name") or p.get("Id"), 80) or f"CPU {i + 1}",
                        None, "", None, None, None, None, status, health, state, {
                            "model": text(p.get("Model"), 100), "manufacturer": text(p.get("Manufacturer"), 40),
                            "cores": num(p.get("TotalCores"), 0, 10000), "threads": num(p.get("TotalThreads"), 0, 20000),
                            "max_mhz": num(p.get("MaxSpeedMHz"), 0, 100000)}))
    return _uniq(out)


def parse_memory(docs):
    out = []
    for i, m in enumerate(docs):
        status, health, state, absent = status_of(m.get("Status"))
        mib = num(m.get("CapacityMiB"), 0, 1e9)
        if absent or (mib is not None and mib == 0):
            continue
        out.append(Comp("dimm", _key(m, f"d{i}"), text(m.get("DeviceLocator") or m.get("Name") or m.get("Id"), 80) or f"DIMM {i + 1}",
                        None, "", None, None, None, None, status, health, state, {
                            "capacity_gib": round(mib / 1024, 1) if mib else None, "type": text(m.get("MemoryDeviceType"), 20),
                            "speed_mhz": num(m.get("OperatingSpeedMhz"), 0, 100000), "manufacturer": text(m.get("Manufacturer"), 40),
                            "part": text(m.get("PartNumber"), 60), "serial": text(m.get("SerialNumber"), 40)}))
    return _uniq(out)


def parse_storage(controllers):
    """controllers: [{"ctrl": doc, "drives": [doc, ...]}] -> (storage controllers, drives, batteries)"""
    ctrls, drives, batteries = [], [], []
    for i, entry in enumerate(controllers):
        c = entry.get("ctrl") or {}
        status, health, state, absent = status_of(c.get("Status"))
        inner = get(c, "StorageControllers", 0, default={}) or {}
        if status == UNKNOWN and not health:
            status, health, state, _ = status_of(inner.get("Status"))
        if not absent:
            ctrls.append(Comp("storage", _key(c, f"s{i}"), text(c.get("Name") or inner.get("Name") or c.get("Id"), 100) or f"Controller {i + 1}",
                              None, "", None, None, None, None, status, health, state, {
                                  "model": text(inner.get("Model"), 80), "firmware": text(inner.get("FirmwareVersion"), 40),
                                  "manufacturer": text(inner.get("Manufacturer"), 40), "drives": len(entry.get("drives") or [])}))
        bat = get(inner, "Oem", "Dell", "DellControllerBattery") or get(c, "Oem", "Dell", "DellControllerBattery")
        if isinstance(bat, dict):
            bstatus, bhealth, bstate, _ = status_of({"Health": bat.get("PrimaryStatus") or bat.get("Health")})
            batteries.append(Comp("battery", f"ctrl-{i}", text(bat.get("Name"), 80) or "RAID controller battery", None, "", None,
                                  None, None, None, bstatus, bhealth, bstate, {}))
        for j, d in enumerate(entry.get("drives") or []):
            dstatus, dhealth, dstate, dabsent = status_of(d.get("Status"))
            if dabsent:
                continue
            cap = num(d.get("CapacityBytes"), 0, 1e18)
            drives.append(Comp("drive", _key(d, f"dr{i}-{j}"), text(d.get("Name") or d.get("Id"), 120) or f"Drive {j + 1}",
                               None, "", None, None, None, None, dstatus, dhealth, dstate, {
                                   "model": text(d.get("Model"), 80), "serial": text(d.get("SerialNumber"), 40),
                                   "capacity_gb": round(cap / 1e9, 1) if cap else None, "media": text(d.get("MediaType"), 12),
                                   "protocol": text(d.get("Protocol"), 20), "firmware": text(d.get("Revision"), 24),
                                   "life_left_pct": num(d.get("PredictedMediaLifeLeftPercent"), 0, 100),
                                   "failure_predicted": bool(d.get("FailurePredicted")) if isinstance(d.get("FailurePredicted"), bool) else None,
                                   "controller": text(c.get("Name") or c.get("Id"), 60)}))
    return _uniq(ctrls), _uniq(drives), _uniq(batteries)


def parse_nics(docs):
    out = []
    for i, n in enumerate(docs):
        status, health, state, absent = status_of(n.get("Status"))
        if absent:
            continue
        out.append(Comp("nic", _key(n, f"n{i}"), text(n.get("Name") or n.get("Id"), 80) or f"NIC {i + 1}", None, "", None, None,
                        None, None, status, health, state, {
                            "mac": text(n.get("MACAddress") or n.get("PermanentMACAddress"), 24),
                            "speed_mbps": num(n.get("SpeedMbps"), 0, 1e7), "link": text(n.get("LinkStatus"), 20)}))
    return _uniq(out)


# ----------------------------------------------------------------------------- logs
_SEVERITY = {"ok": "info", "informational": "info", "info": "info", "warning": "warning", "critical": "critical"}


def parse_ts(value, now=None):
    """An aware datetime from an ISO-8601 string, or None. Never in the future."""
    s = text(value, 40)
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    m = re.match(r"^(.*[+-]\d{2})(\d{2})$", s)              # +0530 -> +05:30
    if m and "T" in s:
        s = m.group(1) + ":" + m.group(2)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    if dt > now + timedelta(minutes=5) or dt.year < 2000:
        return None
    return dt


def parse_entries(log_id, docs, now=None):
    out, seen = [], set()
    for e in docs:
        eid = text(e.get("Id") or e.get("EntryCode") or e.get("RecordId"), 60)
        ts = parse_ts(e.get("Created") or e.get("EventTimestamp"), now)
        msg = text(e.get("Message") or e.get("MessageId"), 500)
        if not eid or ts is None or not msg:
            continue
        full = f"{log_id}:{eid}"[:120]
        if full in seen:
            continue
        seen.add(full)
        sev = _SEVERITY.get(text(e.get("Severity"), 20).lower(), "info")
        sensor = text(e.get("SensorType") or get(e, "Oem", "Hpe", "Category") or get(e, "Oem", "Dell", "Category"), 60)
        out.append(Entry(full, ts, sev, sensor, msg, text(log_id, 40)))
    return out


# ----------------------------------------------------------------------------- the whole thing
SECTION_TILES = ("thermal", "power", "system", "chassis", "manager", "storage", "memory", "processors", "logs")


def parse(c, now=None):
    """Collected documents (see hardware.collector) -> Snapshot."""
    vendor = c.get("vendor") or "generic"
    system, chassis, manager = c.get("system"), c.get("chassis"), c.get("manager")
    secs = dict(c.get("sections") or {})
    comps = []

    if c.get("thermal") is not None:
        temps, fans = parse_thermal(c["thermal"])
        comps += temps + fans
    if c.get("power") is not None:
        psus, volts, batts = parse_power(c["power"])
        comps += psus + volts + batts
    if chassis is not None:
        intr = parse_intrusion(chassis)
        if intr:
            comps.append(intr)
        else:
            secs.setdefault("intrusion", "missing")
    if system is not None:
        status, health, state, _ = status_of(system.get("Status"))
        comps.append(Comp("system", "system", "System", None, "", None, None, None, None, status, health, state, {}))
        msum = get(system, "MemorySummary")
        if isinstance(msum, dict):
            mstatus, mhealth, mstate, _ = status_of(msum.get("Status"))
            comps.append(Comp("memory", "summary", "System memory", None, "", None, None, None, None, mstatus, mhealth, mstate,
                              {"total_gib": num(msum.get("TotalSystemMemoryGiB"), 0, 1e7)}))
    if chassis is not None:
        status, health, state, _ = status_of(chassis.get("Status"))
        comps.append(Comp("system", "chassis", "Chassis", None, "", None, None, None, None, status, health, state, {}))
    if c.get("processors") is not None:
        comps += parse_processors(c["processors"])
    if c.get("memory") is not None:
        comps += parse_memory(c["memory"])
    if c.get("storage") is not None:
        ctrls, drives, batts = parse_storage(c["storage"])
        comps += ctrls + drives + batts
    if c.get("nics") is not None:
        comps += parse_nics(c["nics"])

    entries = []
    for log in c.get("logs") or []:
        entries += parse_entries(log.get("id") or "log", log.get("entries") or [], now)

    ov = overview(vendor, system, chassis, manager, c)
    return Snapshot(vendor, ov, text(get(system, "PowerState"), 20), _distinct(comps[:MAX_COMPONENTS]), entries, secs,
                    status_of(get(system, "Status"))[0] if system else UNKNOWN)


def overview(vendor, system, chassis, manager, c):
    """The identity facts shown on the overview card. Missing facts are empty strings (shown as 'not reported')."""
    dell_sys = get(system, "Oem", "Dell", "DellSystem", default={}) or {}
    lic = ""
    if vendor == "hpe":
        block = get(manager, "Oem", "Hpe", "License") or get(manager, "Oem", "Hp", "License")
        lic = text(get(block, "LicenseString") or get(block, "LicenseType"), 60)
    elif c.get("license"):
        desc = text(get(c["license"], "Description") or get(c["license"], "Name"), 80)
        edition = re.search(r"(enterprise|datacenter|express|basic)", desc, re.I)
        lic = edition.group(1).capitalize() if edition else (desc or text(get(c["license"], "LicenseType"), 60))
    return {
        "bmc_model": bmc_model(vendor, manager) if manager is not None else "",
        "bmc_firmware": text(get(manager, "FirmwareVersion"), 60),
        "lifecycle": text(dell_sys.get("LifecycleControllerVersion"), 40) if vendor == "dell" else "",
        "service_tag": text(get(system, "SKU"), 40) if vendor == "dell" else "",
        "serial": text(get(system, "SerialNumber") or get(chassis, "SerialNumber"), 60),
        "model": text(get(system, "Model") or get(chassis, "Model"), 80),
        "manufacturer": text(get(system, "Manufacturer"), 60),
        "bios": text(get(system, "BiosVersion"), 40),
        "hostname": text(get(system, "HostName"), 80),
        "license": lic,
        "cpu_count": num(get(system, "ProcessorSummary", "Count"), 0, 1000),
        "memory_gib": num(get(system, "MemorySummary", "TotalSystemMemoryGiB"), 0, 1e7),
    }
