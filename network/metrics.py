"""Interface and uptime metrics from Prometheus (snmp_exporter IF-MIB / SNMPv2-MIB).

Standard snmp_exporter `if_mib` series are used:
  ifHCInOctets / ifHCOutOctets (falls back to the 32-bit ifInOctets / ifOutOctets),
  ifOperStatus, ifAdminStatus, ifHighSpeed (falls back to ifSpeed), ifType_info / ifType,
  ifInErrors / ifOutErrors, ifInDiscards / ifOutDiscards (per-second rates over 5 min),
  changes(ifOperStatus[24h]) as the flap count, sysUpTime (TimeTicks, hundredths of a second).
Interfaces are identified by the `ifIndex` label; `ifName` / `ifDescr` / `ifAlias`
lookups give the names. Missing series are tolerated: whatever is present is stored.
"""
import logging
from datetime import datetime, timezone as dt_tz

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
# per-interface numbers that must never outlive the data they came from
STALE_RATES = {"in_mbps": None, "out_mbps": None, "utilization": None,
               "errors_per_s": None, "discards_per_s": None, "flaps_24h": None}
NOT_CONFIGURED = "No metrics source: set LINKEDEYE_PROMETHEUS_URL and the device Prometheus instance."
NO_INSTANCE = "No metrics source: set the device's Prometheus instance (snmp_exporter target label)."


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
        "uptime": f"sysUpTime{{{s}}} / 100",
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


def fetch(instances):
    """Query Prometheus for the given instance labels.
    Returns ({instance: {"interfaces": {ifIndex: {...}}, "uptime": seconds|None}}, [errors])."""
    data = {i: {"interfaces": {}, "uptime": None} for i in instances}
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
            if key == "uptime":
                data[inst]["uptime"] = v
                continue
            idx = m.get("ifIndex", "")
            if not str(idx).isdigit():
                continue
            ifs = data[inst]["interfaces"]
            if int(idx) not in ifs and len(ifs) >= MAX_INTERFACES:
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
            else:
                row[key] = v
    return data, errors


def _rate(v):
    return None if v is None else round(max(0.0, v), 3)


def apply(device, result, errors, now=None):
    """Store one device's metrics. Returns the list of (interface, old_oper, new_oper) transitions."""
    from .models import Interface
    now = now or timezone.now()
    interfaces = result.get("interfaces", {}) if result else {}
    uptime = result.get("uptime") if result else None
    transitions = []
    if not interfaces and uptime is None:
        # nothing for this instance: keep the inventory but stop showing stale traffic numbers
        Interface.objects.filter(device=device).update(**STALE_RATES)
        msg = f'No snmp_exporter series in Prometheus for instance "{device.prometheus_instance}"'
        if errors:
            msg += " (" + "; ".join(errors) + ")"
        device.metrics_error = msg[:300]
        device.uptime_seconds = None
        device.save(update_fields=["metrics_error", "uptime_seconds"])
        return transitions

    existing = {i.if_index: i for i in Interface.objects.filter(device=device)}
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
            "admin_status": row.get("admin") or "unknown", "oper_status": row.get("oper") or "unknown",
            "speed_mbps": speed, "in_mbps": i_in, "out_mbps": i_out, "utilization": util,
            "errors_per_s": _rate(row.get("errors")), "discards_per_s": _rate(row.get("discards")),
            "flaps_24h": None if row.get("flaps") is None else max(0, int(row["flaps"])), "updated_at": now,
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
    device.metrics_updated_at = now
    device.metrics_error = ("Partial data: " + "; ".join(errors))[:300] if errors else ""
    device.save(update_fields=["uptime_seconds", "metrics_updated_at", "metrics_error"])
    return transitions


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
        return [({"name": name, "color": color, "area": True, "areaOpacity": opacity, "width": 1.4},
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
