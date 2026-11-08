"""Interface and uptime metrics from Prometheus (snmp_exporter IF-MIB / SNMPv2-MIB).

Standard snmp_exporter `if_mib` series are used:
  ifHCInOctets / ifHCOutOctets (falls back to the 32-bit ifInOctets / ifOutOctets),
  ifOperStatus, ifAdminStatus, ifHighSpeed (falls back to ifSpeed), ifType_info / ifType,
  ifInErrors / ifOutErrors, ifInDiscards / ifOutDiscards (per-second rates over 5 min),
  changes(ifOperStatus[24h]) as the flap count, sysUpTime (TimeTicks, hundredths of a second).
Device-level CPU, memory and temperature (the device card) are read from whichever of these the snmp_exporter module
exposes, in this order: CPU hrProcessorLoad, cpmCPUTotal5minRev (Cisco), jnxOperatingCPU (Juniper), fgSysCpuUsage
(Fortinet); memory hrStorageUsed/hrStorageSize for RAM, ciscoMemoryPoolUsed/Free, jnxOperatingBuffer, fgSysMemUsage;
temperature entPhySensorValue (celsius sensors), jnxOperatingTemp, ciscoEnvMonTemperatureStatusValue. A device whose
module has none of them simply has no figure: it is shown as "No data", never as 0.
Interfaces are identified by the `ifIndex` label; `ifName` / `ifDescr` / `ifAlias`
lookups give the names. Missing series are tolerated: whatever is present is stored.
Per-port facts for the interface page (network/portmetrics.py reads the live extras): ifMtu, ifPhysAddress (a label),
dot3StatsDuplexStatus (indexed by `dot3StatsIndex`, which equals ifIndex) and ifLastChange (TimeTicks, read against
sysUpTime) are stored on the Interface row. A fact never creates an interface, and a device that does not report one
keeps it empty ("not reported"), never a guess.
"""
import logging
import re
from datetime import datetime, timedelta, timezone as dt_tz

from django.utils import timezone

from core import prometheus

log = logging.getLogger("linkedeye.network")

IF_STATES = {1: "up", 2: "down", 3: "testing", 4: "unknown", 5: "dormant", 6: "notPresent", 7: "lowerLayerDown"}
IF_TYPES = {  # IANAifType numbers / names -> display names
    1: "Other", 6: "Ethernet", 24: "Loopback", 53: "Virtual", 71: "Wireless", 117: "Ethernet", 131: "Tunnel",
    135: "VLAN", 136: "VLAN", 161: "LAG", 23: "PPP", 32: "Frame Relay", 37: "ATM", 166: "MPLS", 209: "Bridge",
}
IF_TYPE_NAMES = {
    "ethernetCsmacd": "Ethernet", "gigabitEthernet": "Ethernet", "softwareLoopback": "Loopback", "propVirtual": "Virtual",
    "ieee80211": "Wireless", "tunnel": "Tunnel", "l2vlan": "VLAN", "l3ipvlan": "VLAN", "ieee8023adLag": "LAG",
    "other": "Other", "ppp": "PPP", "frameRelay": "Frame Relay", "atm": "ATM", "mpls": "MPLS", "bridge": "Bridge",
}
CHUNK = 40           # instances per batched query
MAX_INTERFACES = 1000
MAX_IF_INDEX = 2_147_483_647   # the largest value of the positive-integer column on PostgreSQL
# per-interface numbers that must never outlive the data they came from
STALE_RATES = {"in_mbps": None, "out_mbps": None, "utilization": None,
               "errors_per_s": None, "discards_per_s": None, "flaps_24h": None}
NOT_CONFIGURED = "No metrics source: set LINKEDEYE_PROMETHEUS_URL and the device Prometheus instance."
NO_INSTANCE = "No metrics source: set the device's Prometheus instance (snmp_exporter target label)."
# chart windows offered on the device detail page (?range=), shortest first
RANGES = {"1h": 3600, "6h": 6 * 3600, "24h": 86400, "7d": 7 * 86400}
RANGE_LABELS = {"1h": "Last hour", "6h": "Last 6 hours", "24h": "Last 24 hours", "7d": "Last 7 days"}
DEFAULT_RANGE = "24h"



def current_interfaces(device):
    """The device's interfaces as they should be shown now.

    Nothing is collected for a disabled device, so its last rates are not current traffic. Devices disabled
    before device_form started clearing them, or through the Django admin where `enabled` is editable, still
    carry them, so every reader blanks them (in memory only: the inventory and states stay)."""
    interfaces = list(device.interfaces.all())
    if not device.enabled:
        for i in interfaces:
            for field, value in STALE_RATES.items():
                setattr(i, field, value)
    return interfaces


def range_key(value):
    """A valid ?range= key (unknown values fall back to the default)."""
    return value if value in RANGES else DEFAULT_RANGE


def promql_string(value):
    """Escape a value for a double-quoted PromQL string literal."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def regex_alternation(values):
    """An RE2 regex (as a PromQL string body) that matches exactly one of `values`."""
    special = set(r"\.+*?()|[]{}^$")
    parts = []
    for v in values:
        parts.append("".join("\\" + c if c in special else c for c in v))
    return promql_string("|".join(parts))


def queries(selector):
    s = selector
    return {
        "in": f"(rate(ifHCInOctets{{{s}}}[5m]) or rate(ifInOctets{{{s}}}[5m])) * 8 / 1e6",
        "out": f"(rate(ifHCOutOctets{{{s}}}[5m]) or rate(ifOutOctets{{{s}}}[5m])) * 8 / 1e6",
        "oper": f"ifOperStatus{{{s}}}",
        "admin": f"ifAdminStatus{{{s}}}",
        "speed": f"(ifHighSpeed{{{s}}} * 1) or (ifSpeed{{{s}}} / 1e6)",
        "type": f"ifType_info{{{s}}} or ifType{{{s}}}",
        "errors": f'sum by (instance, ifIndex) (rate({{__name__=~"ifInErrors|ifOutErrors",{s}}}[5m]))',
        "discards": f'sum by (instance, ifIndex) (rate({{__name__=~"ifInDiscards|ifOutDiscards",{s}}}[5m]))',
        # max over the state-set series (one per state) so both exporter styles count each transition once
        "flaps": f"max by (instance, ifIndex) (changes(ifOperStatus{{{s}}}[24h]))",
        # inventory facts of the per-port page: one batched query each for the whole chunk, never one per port
        "mtu": f"ifMtu{{{s}}}",
        "mac": f"ifPhysAddress{{{s}}}",
        "duplex": f"dot3StatsDuplexStatus{{{s}}}",
        "lastchange": f"ifLastChange{{{s}}}",
        "uptime": f"sysUpTime{{{s}}} / 100",
        **device_queries(s),
    }


# series that are not listed here are not guessed at; add a vendor's by appending to the `or` chains
def device_queries(s):
    ram = r'hrStorageType=~".*(hrStorageRam|1\\.3\\.6\\.1\\.2\\.1\\.25\\.2\\.1\\.2)$"'
    return {
        "cpu": (f"avg by (instance) (hrProcessorLoad{{{s}}}) or avg by (instance) (cpmCPUTotal5minRev{{{s}}})"
                f" or max by (instance) (jnxOperatingCPU{{{s}}}) or max by (instance) (fgSysCpuUsage{{{s}}})"),
        "memory": (f"100 * sum by (instance) (hrStorageUsed{{{s},{ram}}}) / sum by (instance) (hrStorageSize{{{s},{ram}}})"
                   f" or 100 * sum by (instance) (ciscoMemoryPoolUsed{{{s}}})"
                   f" / (sum by (instance) (ciscoMemoryPoolUsed{{{s}}}) + sum by (instance) (ciscoMemoryPoolFree{{{s}}}))"
                   f" or max by (instance) (jnxOperatingBuffer{{{s}}}) or max by (instance) (fgSysMemUsage{{{s}}})"),
        "temp": (f'max by (instance) (entPhySensorValue{{{s},entPhySensorType=~"celsius|8"}})'
                 f" or max by (instance) (jnxOperatingTemp{{{s}}}) or max by (instance) (ciscoEnvMonTemperatureStatusValue{{{s}}})"),
    }


def _state(metric, value, label):
    # gauge with enum values (snmp_exporter default) or a state set ({ifOperStatus="up"} 1)
    if label in metric:
        return str(metric[label])[:20] if value == 1 else None
    if value is None:
        return None
    return IF_STATES.get(int(value), "unknown")


def _if_type(metric, value):
    raw = metric.get("ifType")
    if raw:
        name = raw.split("(")[0]
        return IF_TYPE_NAMES.get(name, name[:60])
    if value is None:
        return ""
    return IF_TYPES.get(int(value), f"type {int(value)}")


PORT_FACT_KEYS = ("mtu", "mac", "duplex", "lastchange")


def parse_mac(raw):
    """aa:bb:cc:dd:ee:ff from the forms snmp_exporter prints (0xAABBCCDDEEFF, aa:bb:.., aa-bb-.., aabb.ccdd.eeff).
    Anything that is not a non-zero 6-byte address is "" (not reported)."""
    text = str(raw or "").strip()
    if text[:2].lower() == "0x":
        text = text[2:]
    digits = re.sub(r"[:\-.\s]", "", text)
    if len(digits) != 12 or not re.fullmatch(r"[0-9A-Fa-f]{12}", digits) or set(digits) == {"0"}:
        return ""
    return ":".join(digits[i:i + 2] for i in range(0, 12, 2)).lower()


def parse_duplex(metric, value):
    """"full" / "half" / "" from dot3StatsDuplexStatus (enum 2 half, 3 full; or a state set {..="fullDuplex"} 1)."""
    if "dot3StatsDuplexStatus" in metric:
        label = str(metric["dot3StatsDuplexStatus"]).lower()
        if value != 1:
            return ""
        return "full" if "full" in label else "half" if "half" in label else ""
    if value is None:
        return ""
    return {2: "half", 3: "full"}.get(int(value), "")


def _mtu(value):
    return int(value) if isinstance(value, (int, float)) and value == value and 1 <= value <= 1_000_000 else None


def fetch(instances):
    """Query Prometheus for the given instance labels.
    Returns ({instance: {"interfaces": {ifIndex: {...}}, "uptime": seconds|None}}, [errors])."""
    data = {i: {"interfaces": {}, "uptime": None, "cpu": None, "memory": None, "temp": None} for i in instances}
    errors = []
    selector = f'instance=~"{regex_alternation(instances)}"'
    for key, promql in queries(selector).items():
        try:
            rows = prometheus.query(promql)
        except prometheus.PrometheusError as e:
            errors.append(f"{key}: {e}")
            continue
        for r in rows:
            m, v = r.get("metric", {}), r.get("value")
            inst = m.get("instance")
            if inst not in data:
                continue
            if key in ("uptime", "cpu", "memory", "temp"):
                data[inst][key] = v
                continue
            idx = m.get("ifIndex") or (m.get("dot3StatsIndex") if key == "duplex" else None) or ""
            # ASCII digits that fit the integer column (a label is device-reported text; int() refuses 4300+ digits)
            if not str(idx).isascii() or not str(idx).isdigit() or len(str(idx)) > 10 or int(idx) > MAX_IF_INDEX:
                continue
            ifs = data[inst]["interfaces"]
            if int(idx) not in ifs and (key in PORT_FACT_KEYS or len(ifs) >= MAX_INTERFACES):
                continue
            row = ifs.setdefault(int(idx), {"name": "", "alias": ""})
            row["name"] = row["name"] or m.get("ifName") or m.get("ifDescr") or ""
            row["alias"] = row["alias"] or m.get("ifAlias") or ""
            if key in ("oper", "admin"):
                label = "ifOperStatus" if key == "oper" else "ifAdminStatus"
                st = _state(m, v, label)
                if st:
                    row[key] = st
            elif key == "type":
                row["type"] = _if_type(m, v)
            elif key == "mtu":
                row["mtu"] = _mtu(v)
            elif key == "mac":
                row["mac"] = parse_mac(m.get("ifPhysAddress"))
            elif key == "duplex":
                duplex = parse_duplex(m, v)
                if duplex or "duplex" not in row:
                    row["duplex"] = duplex
            elif key == "lastchange":
                row["lastchange"] = v
            else:
                row[key] = v
    return data, errors


def _rate(v):
    return None if v is None else round(max(0.0, v), 3)


def _bounded(v, lo, hi):
    """A reading inside its physical range, else None (a bogus sensor value is no data, not a number to show)."""
    if v is None or isinstance(v, bool) or v != v or not lo <= v <= hi:
        return None
    return round(float(v), 1)


def failed_keys(errors):
    """The query keys fetch() could not read, taken back out of its "<key>: <message>" strings.

    Only real query keys count, so a generic note such as "no IF-MIB interface series" is ignored."""
    reported = {str(e).split(":", 1)[0] for e in (errors or ())}
    return reported & set(queries(""))


def _kept_state(obj, field, query_failed):
    """What to store for an admin/oper status the data does not carry.

    A query that failed says nothing about the interface, so the stored state is kept: overwriting it
    with "unknown" would also swallow the next up/down transition. A series that is simply absent
    (no error) still becomes "unknown"."""
    if query_failed and obj is not None:
        return getattr(obj, field) or "unknown"
    return "unknown"


def _port_facts(row, obj, failed, uptime, now):
    """mtu / mac / duplex / last_change_at for one interface row. A query that failed says nothing about the port, so
    the stored value is kept; a series that is simply absent clears it ("not reported")."""
    out = {}
    for field, key, empty in (("mtu", "mtu", None), ("mac", "mac", ""), ("duplex", "duplex", "")):
        out[field] = getattr(obj, field) if obj is not None and key in failed else row.get(key) or empty
    if obj is not None and ("lastchange" in failed or "uptime" in failed):
        out["last_change_at"] = obj.last_change_at
    else:
        ticks = row.get("lastchange")
        age = None if ticks is None or ticks < 0 or uptime is None else uptime - ticks / 100.0
        # an ifLastChange beyond sysUpTime (counter wrap, agent restart) says nothing: no value
        out["last_change_at"] = None if age is None or age < -5 else now - timedelta(seconds=max(0.0, age))
    return out


def apply(device, result, errors, now=None):
    """Store one device's metrics. Returns the list of (interface, old_oper, new_oper) transitions."""
    from .models import Interface
    now = now or timezone.now()
    interfaces = result.get("interfaces", {}) if result else {}
    uptime = result.get("uptime") if result else None
    cpu = _bounded(result.get("cpu") if result else None, 0, 100)
    memory = _bounded(result.get("memory") if result else None, 0, 100)
    temp = _bounded(result.get("temp") if result else None, -50, 250)
    transitions = []
    if not interfaces and uptime is None and cpu is None and memory is None and temp is None:
        # nothing for this instance: keep the inventory but stop showing stale traffic numbers
        Interface.objects.filter(device=device).update(**STALE_RATES)
        msg = f'No snmp_exporter series in Prometheus for instance "{device.prometheus_instance}"'
        if errors:
            msg += " (" + "; ".join(errors) + ")"
        device.metrics_error = msg[:300]
        device.uptime_seconds = device.cpu_pct = device.memory_pct = device.temperature_c = None
        device.save(update_fields=["metrics_error", "uptime_seconds", "cpu_pct", "memory_pct", "temperature_c"])
        return transitions

    existing = {i.if_index: i for i in Interface.objects.filter(device=device)}
    failed = failed_keys(errors)
    for idx, row in interfaces.items():
        speed = row.get("speed")
        speed = round(speed, 3) if speed else (0.0 if speed == 0 else None)
        i_in, i_out = _rate(row.get("in")), _rate(row.get("out"))
        util = None
        if speed and (i_in is not None or i_out is not None):
            util = round(min(100.0, 100.0 * max(i_in or 0, i_out or 0) / speed), 1)
        obj = existing.pop(idx, None)
        fields = {
            "name": (row.get("name") or f"if{idx}")[:150], "alias": (row.get("alias") or "")[:200],
            "if_type": row.get("type") or (obj.if_type if obj else ""),
            "admin_status": row.get("admin") or _kept_state(obj, "admin_status", "admin" in failed),
            "oper_status": row.get("oper") or _kept_state(obj, "oper_status", "oper" in failed),
            "speed_mbps": speed, "in_mbps": i_in, "out_mbps": i_out, "utilization": util,
            "errors_per_s": _rate(row.get("errors")), "discards_per_s": _rate(row.get("discards")),
            "flaps_24h": None if row.get("flaps") is None else max(0, int(row["flaps"])), "updated_at": now,
            **_port_facts(row, obj, failed, uptime, now),
        }
        if obj is None:
            Interface.objects.create(device=device, if_index=idx, **fields)
            continue
        old = obj.oper_status
        for k, val in fields.items():
            setattr(obj, k, val)
        obj.save()
        if old != obj.oper_status and obj.admin_status == "up" and {old, obj.oper_status} <= {"up", "down", "lowerLayerDown"}:
            transitions.append((obj, old, obj.oper_status))
    if interfaces and existing:  # interfaces that disappeared from the device
        Interface.objects.filter(pk__in=[i.pk for i in existing.values()]).delete()
    elif existing:  # only sysUpTime came back: keep the inventory but never show old rates as current
        Interface.objects.filter(pk__in=[i.pk for i in existing.values()]).update(**STALE_RATES)
        errors = list(errors) + ["no IF-MIB interface series"]
    device.uptime_seconds = int(uptime) if uptime is not None else None
    device.cpu_pct, device.memory_pct, device.temperature_c = cpu, memory, temp
    device.metrics_updated_at = now
    device.metrics_error = ("Partial data: " + "; ".join(errors))[:300] if errors else ""
    device.save(update_fields=["uptime_seconds", "cpu_pct", "memory_pct", "temperature_c", "metrics_updated_at", "metrics_error"])
    return transitions


def align_series(series, step):
    """Snap every series onto the first one's step grid.

    core.prometheus.query_range ends each query at its own time.time(), so the series of one chart come back with
    timestamps a few milliseconds apart. Merged as-is, every series has a gap at every other x position and
    charts.js draws isolated points instead of a line. Only timestamps within a few seconds of the grid move, so
    points that are genuinely elsewhere are never merged."""
    base = next((vals[0][0] for vals in series if vals), None)
    if base is None or not step:
        return series
    tol = min(step / 4, 15.0)

    def snap(t):
        g = base + round((t - base) / step) * step
        return g if abs(t - g) <= tol else t
    return [[(snap(t), v) for t, v in vals] for vals in series]


def _range_chart(device, seconds, if_index, unit, empty, build):
    """One line chart from `build(selector)` -> [(series style dict, promql)]. `if_index` narrows to one port.
    Returns (chart dict or None, message)."""
    if not prometheus.configured():
        return None, NOT_CONFIGURED
    if not device.prometheus_instance:
        return None, NO_INSTANCE
    sel = f'instance="{promql_string(device.prometheus_instance)}"'
    if if_index is not None:
        sel += f',ifIndex="{int(if_index)}"'
    step = max(60, int(seconds / 144))
    defs, series = build(sel), []
    try:
        for _style, q in defs:
            rows = prometheus.query_range(q, seconds=seconds, step=step)
            series.append(rows[0]["values"] if rows else [])
    except prometheus.PrometheusError as e:
        return None, f"Prometheus query failed: {e}"
    if not any(series):
        return None, empty
    series = align_series(series, step)
    stamps = sorted({t for vals in series for t, _v in vals})
    lookups = [dict(v) for v in series]
    fmt = "%H:%M" if seconds <= 86400 else "%d %b"
    labels = [timezone.localtime(datetime.fromtimestamp(t, tz=dt_tz.utc)).strftime(fmt) for t in stamps]
    tips = [timezone.localtime(datetime.fromtimestamp(t, tz=dt_tz.utc)).strftime("%d %b %H:%M") for t in stamps]

    def vals(lookup):
        return [None if lookup.get(t) is None else round(lookup[t], 2) for t in stamps]
    return {"labels": labels, "tipLabels": tips, "unit": unit, "xLabelEvery": max(1, len(stamps) // 6),
            "series": [dict(style, values=vals(lookup)) for (style, _q), lookup in zip(defs, lookups)]}, ""


def traffic_range(device, seconds=86400, if_index=None):
    """Inbound/outbound Mbps over time for the detail page chart: the whole device, or one port."""
    def build(sel):
        return [({"name": name, "color": color, "area": True, "areaOpacity": opacity, "width": 1.4, "endDot": True},
                 f"sum((rate(ifHC{metric}Octets{{{sel}}}[5m]) or rate(if{metric}Octets{{{sel}}}[5m]))) * 8 / 1e6")
                for name, metric, color, opacity in (("Inbound", "In", "var(--chart-6)", 0.12), ("Outbound", "Out", "var(--brand)", 0.13))]
    return _range_chart(device, seconds, if_index, "Mbps",
                        f'No interface counters in Prometheus for instance "{device.prometheus_instance}" in this period.', build)


def errors_range(device, seconds=86400, if_index=None):
    """Errors and discards per second (in + out) over time: the whole device, or one port."""
    def build(sel):
        return [({"name": name, "color": color, "width": 1.6},
                 f'sum(rate({{__name__=~"{counters}",{sel}}}[5m]))')
                for name, counters, color in (("Errors", "ifInErrors|ifOutErrors", "var(--chart-crit)"),
                                              ("Discards", "ifInDiscards|ifOutDiscards", "var(--chart-warn)"))]
    return _range_chart(device, seconds, if_index, "/s",
                        f'No error or discard counters in Prometheus for instance "{device.prometheus_instance}" in this period.',
                        build)


def port_bars(interfaces, limit=10):
    """Grouped bars of current inbound / outbound Mbps for the busiest ports (stored rates, no Prometheus call).
    None when no interface has a rate yet."""
    rated = [i for i in interfaces if i.in_mbps is not None or i.out_mbps is not None]
    if not rated:
        return None
    top = sorted(rated, key=lambda i: (-max(i.in_mbps or 0, i.out_mbps or 0), i.if_index))[:limit]
    return {"labels": [i.name for i in top],
            "tipLabels": [f"{i.name} · {i.alias}" if i.alias else i.name for i in top],
            "unit": "Mbps", "stacked": False,
            "series": [{"name": "Inbound", "color": "var(--chart-6)", "values": [i.in_mbps for i in top]},
                       {"name": "Outbound", "color": "var(--brand)", "values": [i.out_mbps for i in top]}]}
