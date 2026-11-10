"""Per-port data for the interface detail page (network:interface): graphs, optics, PoE, neighbours, VLAN, LAG, STP.

Everything here reads Prometheus (snmp_exporter series) for ONE port of ONE device and shapes it for the page. Rules:

* Every PromQL string is built from the device's validated instance label and the integer ifIndex of an Interface row
  (never from request text; the only request values that reach a query are the range key, which is looked up in an
  allow-list, and the integer ifIndex, which is matched against the stored interfaces first). Names that come from the
  device (port name, LLDP bridge-port numbers) are escaped with metrics.promql_string / metrics.regex_alternation or are
  digits only.
* Anything the device does not report is absent ("not reported" on the page), never 0 and never guessed.
* Everything that comes back from Prometheus is untrusted text: control characters are stripped and lengths capped
  here, and the templates / port.js render it with escaping / textContent.
* The number of Prometheus calls for one page view is bounded (MAX_PROM_CALLS) and answers are cached in Django's cache
  for a short time (EXTRAS_TTL / RANGE_TTL), so prev / next browsing, reloads and the live poll stay cheap.
"""
import ipaddress
import logging
import re
from datetime import datetime, timezone as dt_tz

from django.core.cache import cache
from django.utils import timezone

from core import prometheus

from . import metrics
from .models import Interface

log = logging.getLogger("linkedeye.network")

# Worst case for one cold page view (everything cached afterwards):
#   instant "extras": ip, lag parent, lag members, bridge port, pvid, stp, cisco access vlan, cisco trunk native vlan,
#                     lldp, cdp, optics sensors, optics thresholds, poe                                     = 13
#   range:            traffic, packets, errors, oper state (selected range) + optics when sensors exist     = 5
#   range, 24 h:      traffic, errors, oper state for the KPI sparklines (only when the range is not 24 h)  = 3
MAX_PROM_CALLS = 21
EXTRAS_TTL = 60          # seconds an answer to the instant queries is reused (any viewer of the same port)
EXTRAS_FAIL_TTL = 10     # a partly failed answer is kept this long so a sick Prometheus is not asked 13 times per reload
RANGE_TTL = 25           # seconds a range answer is reused; the live poll interval is 30 s
OPTICS_FLAG_TTL = 600    # the live poll only asks for the optical power chart when the page found sensors
SPARK_SECONDS = 86400
MAX_POINTS_CSV = 1000
TEXT_MAX = 200
UNREACHABLE = ("Prometheus unreachable", "Prometheus is not connected")

NOT_CONFIGURED = metrics.NOT_CONFIGURED
NO_INSTANCE = metrics.NO_INSTANCE

# control characters, line separators and the invisible bidi / zero-width marks that make text read differently from what it is
_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029\u200b\u200e\u200f\u202a-\u202e\u2066-\u2069\ufeff]")
_SCALE_NAMES = {"yocto": -8, "zepto": -7, "atto": -6, "femto": -5, "pico": -4, "nano": -3, "micro": -2, "milli": -1,
                "units": 0, "kilo": 1, "mega": 2, "giga": 3, "tera": 4, "exa": 5, "peta": 6, "zetta": 7, "yotta": 8}
SENSOR_TYPES = {"3": "voltsac", "4": "voltsdc", "5": "amperes", "8": "celsius", "14": "dbm"}
STP_STATES = {1: "disabled", 2: "blocking", 3: "listening", 4: "learning", 5: "forwarding", 6: "broken"}
POE_DETECTION = {1: "disabled", 2: "searching", 3: "delivering power", 4: "fault", 5: "test", 6: "other fault"}
POE_ADMIN = {1: "enabled", 2: "disabled"}
RELATIONS = {"lessthan": "<", "lessorequal": "<=", "greaterthan": ">", "greaterorequal": ">=", "equalto": "=",
             "notequalto": "≠"}
OPER_CLASS = {"up": "up", "down": "down", "lowerLayerDown": "down", "notPresent": "other", "testing": "other",
              "dormant": "other", "unknown": "other"}
STATE_LABELS = dict(Interface.STATES)
POE_POWER_METRICS = ("cpeExtPsePortPwrConsumption",)   # milliwatts; append another vendor's draw series here


def clean(value, limit=TEXT_MAX):
    """Untrusted device text: control characters out, length capped."""
    if value is None:
        return ""
    return _CTRL.sub(" ", str(value)).strip()[:limit]


def finite(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) != float("inf")


# ------------------------------------------------------------------ selectors
def instance_label(device):
    """The escaped Prometheus instance label of the device, or "" when it has none (or a malformed one)."""
    inst = (device.prometheus_instance or "").strip()
    if not inst or _CTRL.search(inst) or len(inst) > 255:
        return ""
    return metrics.promql_string(inst)


def selector(device, if_index):
    """`instance="…",ifIndex="N"`, or None when the device has no usable instance."""
    inst = instance_label(device)
    if not inst:
        return None
    return f'instance="{inst}",ifIndex="{int(if_index)}"'


def name_regex(name):
    """Anchored RE2 body matching the port name itself or the name followed by a space and more text (sensor names
    such as "Ethernet1/1 Transceiver Rx Power Sensor"). `Gi1/0/1` never matches `Gi1/0/10`."""
    safe = clean(name, 80)
    return metrics.regex_alternation([safe]) + "( .*)?" if safe else ""


def unavailable_reason(device):
    """Why no Prometheus query is made for this device, or "" when queries can run."""
    if not device.enabled:
        return "Monitoring is disabled for this device, so no checks or metrics are collected."
    if not prometheus.configured():
        return NOT_CONFIGURED
    if not instance_label(device):
        return NO_INSTANCE
    return ""


MAX_INSTANT_ROWS = 500
MAX_RANGE_SERIES = 60
MAX_RANGE_POINTS = 2000
SHAPE_ERRORS = (TypeError, ValueError, AttributeError, KeyError, IndexError, OverflowError)
BAD_ANSWER = "Prometheus returned something that is not a valid API response"


def _labels_of(raw):
    """A label set as {str: str} whatever came back (a missing or non-dict metric is an empty set)."""
    if not isinstance(raw, dict):
        return {}
    return {str(k): ("" if v is None else str(v)) for k, v in raw.items()}


def normalise_instant(rows):
    """Instant answer -> [{"metric": {str: str}, "value": finite float | None}], at most MAX_INSTANT_ROWS rows. Anything
    that is not a row of that shape is dropped, so the parsers below never meet None, lists or infinities."""
    out = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        v = r.get("value")
        out.append({"metric": _labels_of(r.get("metric")), "value": v if finite(v) else None})
        if len(out) >= MAX_INSTANT_ROWS:
            break
    return out


def normalise_range(rows):
    """Range answer -> [{"metric": {str: str}, "values": [(finite ts, finite float | None)]}] (at most MAX_RANGE_SERIES
    series of MAX_RANGE_POINTS points, the newest kept)."""
    out = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        raw = r.get("values")
        points = []
        for p in raw[-MAX_RANGE_POINTS:] if isinstance(raw, (list, tuple)) else []:
            try:
                t, v = p
            except (TypeError, ValueError):
                continue
            if finite(t):
                points.append((float(t), float(v) if finite(v) else None))
        out.append({"metric": _labels_of(r.get("metric")), "values": points})
        if len(out) >= MAX_RANGE_SERIES:
            break
    return out


class Fetcher:
    """The page's Prometheus access: counts calls, stops asking once Prometheus is unreachable, and hands back answers
    in one fixed shape (see normalise_instant / normalise_range) or None when the query failed."""

    def __init__(self):
        self.calls = 0
        self.errors = []
        self.dead = False

    def instant(self, promql):
        if self.dead:
            return None
        self.calls += 1
        try:
            return normalise_instant(prometheus.query(promql))
        except prometheus.PrometheusError as e:
            return self._fail(e)
        except SHAPE_ERRORS:
            return self._fail(BAD_ANSWER)

    def range(self, promql, seconds, step):
        if self.dead:
            return None
        self.calls += 1
        try:
            return normalise_range(prometheus.query_range(promql, seconds=seconds, step=step))
        except prometheus.PrometheusError as e:
            return self._fail(e)
        except SHAPE_ERRORS:
            return self._fail(BAD_ANSWER)

    def _fail(self, e):
        msg = clean(e, 160)
        if msg not in self.errors:
            self.errors.append(msg)
        if msg.startswith(UNREACHABLE):
            self.dead = True
        return None


# ------------------------------------------------------------------ range charts
def step_for(seconds):
    return max(60, int(seconds / 144))


def _lab(expr, tag):
    return f'label_replace({expr},"m","{tag}","","")'


def _rate(metric, sel):
    """Per-second rate of one port counter as ONE series. snmp_exporter's if_mib lookups put ifAlias / ifName / ifDescr
    on every sample, so a port renamed inside the window has an old and a new series for the same instance and ifIndex;
    `max by` joins them into one (max, not sum: during the overlap both are the same counter)."""
    return f"max by (instance, ifIndex) (rate({metric}{{{sel}}}[5m]))"


def _counter(hc, plain, sel):
    """rate of the 64-bit counter, else of the 32-bit one (`or` keeps the first when both exist)."""
    return f"{_rate(hc, sel)} or {_rate(plain, sel)}"


def traffic_query(sel):
    return " or ".join(_lab(_counter(f"ifHC{d}Octets", f"if{d}Octets", sel), tag) for d, tag in (("In", "in"), ("Out", "out")))


PACKET_TAGS = (("in_uni", "InUcastPkts"), ("out_uni", "OutUcastPkts"), ("in_multi", "InMulticastPkts"),
               ("out_multi", "OutMulticastPkts"), ("in_bcast", "InBroadcastPkts"), ("out_bcast", "OutBroadcastPkts"))
ERROR_TAGS = (("in_err", "ifInErrors"), ("out_err", "ifOutErrors"), ("in_disc", "ifInDiscards"), ("out_disc", "ifOutDiscards"))


def packets_query(sel):
    return " or ".join(_lab(_counter(f"ifHC{s}", f"if{s}", sel), tag) for tag, s in PACKET_TAGS)


def errors_query(sel):
    return " or ".join(_lab(_rate(metric, sel), tag) for tag, metric in ERROR_TAGS)


def by_tag(rows):
    """{m tag: [(ts, value)]} from a label_replace-tagged range result. Several series with one tag (the same port
    counter under old and new ifAlias / ifName / ifDescr labels) are merged point by point, keeping the larger value
    where they overlap, so a rename inside the window never drops part of the history."""
    merged = {}
    for r in rows or []:
        tag = (r.get("metric") or {}).get("m")
        if not tag:
            continue
        points = merged.setdefault(tag, {})
        for t, v in r.get("values", []):
            if v is not None and (points.get(t) is None or v > points[t]):
                points[t] = v
            else:
                points.setdefault(t, v)
    return {tag: sorted(points.items()) for tag, points in merged.items()}


def _range_rows(f, device, iface, kind, seconds, promql):
    """Cached raw answer of one range query ([] when the port has no series, None when the query failed)."""
    key = f"nwport:r:{device.pk}:{iface.if_index}:{kind}:{seconds}"
    hit = cache.get(key)
    if hit is not None:
        return hit
    rows = f.range(promql, seconds, step_for(seconds))
    if rows is None:
        return None
    cache.set(key, rows, RANGE_TTL)
    return rows


def _num(points, scale=1.0, floor=True):
    out = []
    for t, v in points:
        if not finite(v):
            out.append((t, None))
            continue
        v = v * scale
        out.append((t, max(0.0, v) if floor else v))
    return out


def _labels(stamps, seconds):
    fmt = "%H:%M" if seconds <= 86400 else "%d %b"
    local = [timezone.localtime(datetime.fromtimestamp(t, tz=dt_tz.utc)) for t in stamps]
    return [d.strftime(fmt) for d in local], [d.strftime("%d %b %H:%M") for d in local]


def assemble(defs, seconds, unit, **extra):
    """defs: [(series style, [(ts, value)])] -> a charts.js line chart (with `ts`, so hover and zoom sync), or None when
    no series has a point."""
    if not any(points for _style, points in defs):
        return None
    step = step_for(seconds)
    aligned = metrics.align_series([points for _style, points in defs], step)
    stamps = sorted({t for points in aligned for t, _v in points})
    lookups = [dict(points) for points in aligned]
    labels, tips = _labels(stamps, seconds)

    def vals(lookup):
        return [None if not finite(lookup.get(t)) else round(lookup[t], 3) for t in stamps]
    return {"labels": labels, "tipLabels": tips, "ts": [int(t) for t in stamps], "unit": unit,
            "xLabelEvery": max(1, len(stamps) // 6),
            "series": [dict(style, values=vals(lookup)) for (style, _p), lookup in zip(defs, lookups)], **extra}


def _combine(aligned_lists, fn):
    """Pointwise fn over lists of (ts, value) that share a grid; values missing from every list stay missing."""
    stamps = sorted({t for points in aligned_lists for t, _v in points})
    lookups = [dict(points) for points in aligned_lists]
    out = []
    for t in stamps:
        present = [lk[t] for lk in lookups if finite(lk.get(t))]
        out.append((t, fn(present) if present else None))
    return out


def _align(lists, seconds):
    return metrics.align_series(lists, step_for(seconds))


def traffic_charts(rows, seconds, speed_mbps):
    """(traffic chart, utilisation chart) in Mbps / % from the tagged octet-rate answer."""
    tagged = by_tag(rows)
    inbound, outbound = _num(tagged.get("in", []), 8 / 1e6), _num(tagged.get("out", []), 8 / 1e6)
    peak = max([v for _t, v in inbound + outbound if v is not None] or [0])
    div, unit = (1000.0, "Gbps") if peak >= 2000 else (1.0, "Mbps")   # a 100G port reads better in Gbps
    traffic = assemble([
        ({"name": "Inbound", "color": "var(--chart-6)", "area": True, "areaOpacity": 0.12, "width": 1.4, "endDot": True},
         [(t, None if v is None else v / div) for t, v in inbound]),
        ({"name": "Outbound", "color": "var(--brand)", "area": True, "areaOpacity": 0.13, "width": 1.4, "endDot": True},
         [(t, None if v is None else v / div) for t, v in outbound]),
    ], seconds, unit)
    util = None
    if traffic and speed_mbps and speed_mbps > 0:
        a_in, a_out = _align([inbound, outbound], seconds)
        points = _combine([a_in, a_out], lambda present: min(100.0, 100.0 * max(present) / speed_mbps))
        util = assemble([({"name": "Utilisation", "color": "var(--chart-4)", "area": True, "areaOpacity": 0.12, "width": 1.5,
                           "endDot": True}, points)], seconds, "%", yMin=0, yMax=100)
    return traffic, util


def _flat_zero(defs):
    """chart extras for an all-zero (or all-missing) chart: a fixed 0..1 axis, so the ticks do not all read "0 /s"."""
    nums = [v for _style, points in defs for _t, v in points if finite(v)]
    return {"yMin": 0, "yMax": 1} if not any(v > 1e-9 for v in nums) else {}


def packets_chart(rows, seconds):
    tagged = by_tag(rows)
    if not tagged:
        return None
    names = [t for t, _s in PACKET_TAGS]
    aligned = dict(zip(names, _align([_num(tagged.get(t, [])) for t in names], seconds)))
    both = lambda a, b: _combine([aligned[a], aligned[b]], sum)  # noqa: E731
    defs = [
        ({"name": "Unicast", "color": "var(--chart-6)", "width": 1.5}, both("in_uni", "out_uni")),
        ({"name": "Multicast", "color": "var(--chart-7)", "width": 1.5}, both("in_multi", "out_multi")),
        ({"name": "Broadcast", "color": "var(--chart-2)", "width": 1.5}, both("in_bcast", "out_bcast")),
    ]
    return assemble(defs, seconds, "pps", **_flat_zero(defs))


def errors_chart(rows, seconds):
    tagged = by_tag(rows)
    if not tagged:
        return None
    defs = [
        ({"name": "In errors", "color": "var(--chart-crit)", "width": 1.6}, _num(tagged.get("in_err", []))),
        ({"name": "Out errors", "color": "var(--chart-9)", "width": 1.6, "dash": "5 3"}, _num(tagged.get("out_err", []))),
        ({"name": "In discards", "color": "var(--chart-warn)", "width": 1.6}, _num(tagged.get("in_disc", []))),
        ({"name": "Out discards", "color": "var(--chart-2)", "width": 1.6, "dash": "5 3"}, _num(tagged.get("out_disc", []))),
    ]
    return assemble(defs, seconds, "/s", **_flat_zero(defs))


# ------------------------------------------------------------------ oper-state timeline
def oper_points(rows):
    """[(ts, state name)] from the ifOperStatus range answer: one enum series (value = code) or a state set (one 0/1
    series per state, the active one is 1)."""
    by_t = {}
    for r in rows or []:
        label = (r.get("metric") or {}).get("ifOperStatus")
        for t, v in r.get("values", []):
            if not finite(v):
                continue
            if label is not None:
                if v == 1:
                    by_t[t] = clean(label, 20)
            else:
                by_t[t] = metrics.IF_STATES.get(int(v), "unknown")
    return sorted(by_t.items())


def human_duration(seconds):
    seconds = int(max(0, seconds))
    if seconds < 90:
        return f"{seconds} s"
    if seconds < 5400:
        return f"{round(seconds / 60)} min"
    if seconds < 172800:
        h, m = divmod(round(seconds / 60), 60)
        return f"{h} h {m} min" if m else f"{h} h"
    return f"{round(seconds / 86400)} days"


def oper_timeline(points, seconds):
    """Status bands for the page: consecutive runs of one state, gaps as "no data". None without points."""
    if not points:
        return None
    step = step_for(seconds)
    end = points[-1][0] + step
    start = end - seconds
    runs = []                       # [state, t0, t1]
    for i, (t, state) in enumerate(points):
        nxt = points[i + 1][0] if i + 1 < len(points) else None
        t1 = t + step if nxt is None or nxt - t > 1.5 * step else nxt
        if runs and runs[-1][0] == state and abs(runs[-1][2] - t) < 1:
            runs[-1][2] = t1
        else:
            if runs and t - runs[-1][2] > 1:
                runs.append(["nodata", runs[-1][2], t])
            runs.append([state, t, t1])
    if runs and runs[0][1] - start > step:
        runs.insert(0, ["nodata", start, runs[0][1]])
    stamp = lambda t: timezone.localtime(datetime.fromtimestamp(t, tz=dt_tz.utc)).strftime("%d %b %H:%M")  # noqa: E731
    segments, totals = [], {"up": 0.0, "down": 0.0, "other": 0.0, "nodata": 0.0}
    for state, t0, t1 in runs:
        cls = "nodata" if state == "nodata" else OPER_CLASS.get(state, "other")
        totals[cls] += max(0.0, t1 - t0)
        shown = "No data" if state == "nodata" else STATE_LABELS.get(state, state)
        segments.append({"state": state, "cls": cls, "t0": int(t0), "t1": int(t1), "seconds": int(t1 - t0),
                         "label": f"{shown} · {stamp(t0)} – {stamp(t1)} · {human_duration(t1 - t0)}"})
    known = totals["up"] + totals["down"] + totals["other"]
    changes = sum(1 for a, b in zip(runs, runs[1:]) if "nodata" not in (a[0], b[0]) and a[0] != b[0])
    pct = {k: (round(100.0 * totals[k] / known, 1) if known else None) for k in ("up", "down", "other")}
    parts = [f"{label} {pct[k]:g}%" for k, label in (("up", "Up"), ("down", "Down"), ("other", "Other")) if pct[k]]
    summary = (" · ".join(parts) or "No state samples") + f" · {changes} change{'' if changes == 1 else 's'}"
    return {"segments": segments, "t0": int(start), "t1": int(end), "start_label": stamp(start), "end_label": stamp(end),
            "summary": summary, "changes": changes, "pct": pct}


def flap_buckets(points, seconds=SPARK_SECONDS, buckets=24):
    """State changes per time bucket across the window, for the flaps sparkline (None without samples)."""
    if len(points) < 2:
        return None
    end = points[-1][0]
    width = seconds / buckets
    out = [0] * buckets
    for (_t0, a), (t1, b) in zip(points, points[1:]):
        if a != b:
            out[min(buckets - 1, max(0, int((t1 - (end - seconds)) / width)))] += 1
    return out


# ------------------------------------------------------------------ chart legends and sparklines
def fmt_value(v, unit):
    if not finite(v):
        return "–"
    a = abs(v)
    if a >= 1e15:   # not a plausible reading; keep the legend cell short
        return f"{v:.2e}" + ("%" if unit == "%" else f" {unit}" if unit else "")
    s = (f"{v / 1e6:.1f}M" if a >= 1e6 else f"{v / 1e3:.0f}K" if a >= 1e4 else f"{v:,.0f}" if a >= 100
         else f"{v:.1f}" if a >= 10 else f"{v:.2f}")
    s = re.sub(r"\.0+(?=[A-Z]|$)", "", s)
    return s + ("%" if unit == "%" else f" {unit}" if unit else "")


def legend_rows(chart):
    """Last / mean / min / max per series, formatted, for the legend table under a chart."""
    rows = []
    for s in (chart or {}).get("series", []):
        nums = [v for v in s["values"] if v is not None]
        unit = chart.get("unit", "")
        rows.append({"name": s["name"], "color": s["color"],
                     "last": fmt_value(nums[-1], unit) if nums else "–",
                     "mean": fmt_value(sum(nums) / len(nums), unit) if nums else "–",
                     "min": fmt_value(min(nums), unit) if nums else "–",
                     "max": fmt_value(max(nums), unit) if nums else "–"})
    return rows


def series_values(chart, name):
    for s in (chart or {}).get("series", []):
        if s["name"] == name:
            return list(s["values"])
    return None


def sum_values(chart, names):
    """Pointwise sum of the named series (None where every one of them is missing)."""
    lists = [series_values(chart, n) for n in names]
    lists = [v for v in lists if v is not None]
    if not lists:
        return None
    return [(round(sum(p), 3) if (p := [v[i] for v in lists if v[i] is not None]) else None) for i in range(len(lists[0]))]


def spark(values, color):
    return {"values": values, "color": color} if values and any(v is not None for v in values) else None


def sparklines(day):
    """KPI sparklines from the 24 h charts: {kpi key: {values, color}} (only keys with data)."""
    out = {}
    traffic, errors = day.get("traffic"), day.get("errors")
    out["in"] = spark(series_values(traffic, "Inbound"), "var(--chart-6)")
    out["out"] = spark(series_values(traffic, "Outbound"), "var(--brand)")
    out["util"] = spark(series_values(day.get("util"), "Utilisation"), "var(--chart-4)")
    out["errors"] = spark(sum_values(errors, ["In errors", "Out errors"]), "var(--chart-crit)")
    out["discards"] = spark(sum_values(errors, ["In discards", "Out discards"]), "var(--chart-warn)")
    flaps = flap_buckets(day.get("oper_points") or [])
    out["flaps"] = spark([float(n) for n in flaps], "var(--chart-warn)") if flaps is not None else None
    return {k: v for k, v in out.items() if v}


# ------------------------------------------------------------------ the charts of one page view
CHART_EMPTY = {
    "traffic": "No interface counters in Prometheus for this port in this period.",
    "util": "Utilisation needs the port speed (ifHighSpeed) and traffic counters; this port reports neither.",
    "packets": "No packet counters for this port (ifHCInUcastPkts, ifHCInMulticastPkts and ifHCInBroadcastPkts come from the "
               "snmp_exporter if_mib module).",
    "errors": "No error or discard counters in Prometheus for this port in this period.",
    "oper": "No ifOperStatus samples for this port in this period.",
    "optics": "No optical power sensors reported for this port.",
}


def load_charts(f, device, iface, seconds, optics_names=None, kinds=("traffic", "packets", "errors", "oper")):
    """The graphs for one window: {"traffic", "util", "packets", "errors", "oper", "oper_points", "optics"} (each a
    chart / timeline dict or None) and {key: why it is missing}. `kinds` limits which range queries run (the 24 h
    sparkline set needs only traffic, errors and oper). Never raises; a failed query is a message."""
    reason = unavailable_reason(device)
    if reason:
        return {}, {k: reason for k in CHART_EMPTY}
    sel = selector(device, iface.if_index)
    rows = {}
    for kind, promql in (("traffic", traffic_query), ("packets", packets_query), ("errors", errors_query),
                         ("oper", lambda s: f"ifOperStatus{{{s}}}")):
        if kind in kinds:
            rows[kind] = _range_rows(f, device, iface, kind, seconds, promql(sel))
    charts = {"traffic": None, "util": None, "packets": None, "errors": None, "oper": None, "oper_points": [], "optics": None}
    if rows.get("traffic") is not None:
        charts["traffic"], charts["util"] = traffic_charts(rows["traffic"], seconds, iface.speed_mbps)
    if rows.get("packets") is not None:
        charts["packets"] = packets_chart(rows["packets"], seconds)
    if rows.get("errors") is not None:
        charts["errors"] = errors_chart(rows["errors"], seconds)
    if rows.get("oper") is not None:
        charts["oper_points"] = oper_points(rows["oper"])
        charts["oper"] = oper_timeline(charts["oper_points"], seconds)
    if optics_names:
        optics_rows = _range_rows(f, device, iface, "optics", seconds, optics_query(device, iface))
        rows["optics"] = optics_rows
        if optics_rows is not None:
            charts["optics"] = optics_chart(optics_rows, seconds, iface.name)
    why = {}
    for key in CHART_EMPTY:
        source = {"util": "traffic"}.get(key, key)
        if charts.get(key) is not None or source not in rows and key != "optics":
            continue
        if key == "optics" and not optics_names:
            why[key] = CHART_EMPTY["optics"]
        elif source in rows and rows[source] is None:
            why[key] = "Prometheus query failed: " + (f.errors[0] if f.errors else "no answer")
        else:
            why[key] = CHART_EMPTY[key]
    return charts, why


# ------------------------------------------------------------------ optics (entity sensors)
def sensor_scale(m):
    raw = m.get("entSensorScale", m.get("entPhySensorScale"))
    if raw is None:
        return 0
    text = str(raw).strip().lower()
    if text in _SCALE_NAMES:
        return _SCALE_NAMES[text] * 3
    if text.isdigit() and 1 <= int(text) <= 17:
        return (int(text) - 9) * 3
    return 0


def sensor_precision(m):
    raw = m.get("entSensorPrecision", m.get("entPhySensorPrecision"))
    try:
        p = int(str(raw).strip())
    except (TypeError, ValueError):
        return 0
    return p if -8 <= p <= 9 else 0


def sensor_kind(m):
    """("rx_power" | "tx_power" | "power" | "bias" | "temperature" | "voltage" | None, unit)."""
    kind = str(m.get("entSensorType") or m.get("entPhySensorType") or "").strip().lower()
    kind = SENSOR_TYPES.get(kind, kind)
    name = clean(m.get("entPhysicalName"), 120).lower()
    if kind == "dbm":
        if re.search(r"\b(rx|receive|received)\b", name):
            return "rx_power", "dBm"
        if re.search(r"\b(tx|transmit|transmitted)\b", name):
            return "tx_power", "dBm"
        return "power", "dBm"
    if kind == "celsius":
        return "temperature", "°C"
    if kind == "amperes":
        return "bias", "mA"
    if kind in ("voltsdc", "voltsac"):
        return "voltage", "V"
    return None, ""


SENSOR_BOUNDS = {"rx_power": (-60, 30), "tx_power": (-60, 30), "power": (-60, 30), "bias": (0, 2000),
                 "temperature": (-50, 250), "voltage": (0, 60)}


def sensor_value(m, raw, kind, scale=None, precision=None):
    """The sensor reading in its display unit (scale and precision applied, amperes shown as mA) or None when it is
    missing or outside the physical range for that kind of sensor. `scale` / `precision` default to the labels of `m`."""
    if not finite(raw) or kind not in SENSOR_BOUNDS:
        return None
    scale = sensor_scale(m) if scale is None else scale
    precision = sensor_precision(m) if precision is None else precision
    value = raw * (10 ** scale) / (10 ** precision)
    if kind == "bias":
        value *= 1000.0
    lo, hi = SENSOR_BOUNDS[kind]
    return round(value, 2) if lo <= value <= hi else None


def optics_query(device, iface):
    return (f'{{__name__=~"entSensorValue|entPhySensorValue",instance="{instance_label(device)}",'
            f'entPhysicalName=~"{name_regex(iface.name)}"}}')


def parse_sensors(rows, port_name):
    """[{name, kind, unit, value, index}] for the sensors of this port, one per sensor name (Cisco's entSensorValue
    wins over the standard entPhySensorValue), power first."""
    out, seen = [], set()
    for r in sorted(rows or [], key=lambda r: (r.get("metric", {}).get("__name__") != "entSensorValue")):
        m = r.get("metric", {})
        kind, unit = sensor_kind(m)
        full = clean(m.get("entPhysicalName"), 120)
        if kind is None or not full or full in seen:
            continue
        seen.add(full)
        prefix = clean(port_name, 80)
        label = full[len(prefix):].strip() if prefix and full.lower().startswith(prefix.lower()) else full
        out.append({"name": full, "label": label or full, "kind": kind, "unit": unit,
                    "value": sensor_value(m, r.get("value"), kind), "index": clean(m.get("entPhysicalIndex"), 12),
                    "scale": sensor_scale(m), "precision": sensor_precision(m)})
    order = {"rx_power": 0, "tx_power": 1, "power": 2, "bias": 3, "temperature": 4, "voltage": 5}
    return sorted(out, key=lambda s: (order[s["kind"]], s["name"]))[:16]


def parse_thresholds(rows, sensors):
    """{sensor name: ["< -14 dBm (major)", ...]} from entSensorThresholdValue, scaled like the sensor they belong to."""
    by_index = {s["index"]: s for s in sensors if s["index"]}
    by_name = {s["name"]: s for s in sensors}
    out = {}
    for r in rows or []:
        m = r.get("metric", {})
        s = by_index.get(clean(m.get("entPhysicalIndex"), 12)) or by_name.get(clean(m.get("entPhysicalName"), 120))
        raw = r.get("value")
        if not s or not finite(raw):
            continue
        has_scale = any(k in m for k in ("entSensorScale", "entPhySensorScale", "entSensorPrecision", "entPhySensorPrecision"))
        value = sensor_value(m, raw, s["kind"], None if has_scale else s["scale"], None if has_scale else s["precision"])
        symbol = RELATIONS.get(str(m.get("entSensorThresholdRelation", "")).lower())
        if value is None or symbol is None:
            continue
        severity = clean(m.get("entSensorThresholdSeverity"), 20)
        text = f"{symbol} {value:g} {s['unit']}" + (f" ({severity})" if severity else "")
        if len(out.setdefault(s["name"], [])) < 4:
            out[s["name"]].append(text)
    return out


def optics_chart(rows, seconds, port_name=""):
    """Optical power (dBm) over time: Tx and Rx lines, one per sensor (lane)."""
    defs, seen = [], set()
    prefix = clean(port_name, 80)
    for r in sorted(rows or [], key=lambda r: (r.get("metric", {}).get("__name__") != "entSensorValue")):
        m = r.get("metric", {})
        kind, _unit = sensor_kind(m)
        if kind not in ("rx_power", "tx_power", "power"):
            continue
        full = clean(m.get("entPhysicalName"), 120)
        if full in seen:
            continue
        seen.add(full)
        points = [(t, sensor_value(m, v, kind)) for t, v in r.get("values", [])]
        rest = full[len(prefix):].strip() if prefix and full.lower().startswith(prefix.lower()) else full
        lane = re.search(r"lane\s*\d+", rest, re.I)
        base = "Rx power" if kind == "rx_power" else "Tx power" if kind == "tx_power" else "Power"
        defs.append(({"name": base + (f" ({lane.group(0).lower()})" if lane else ""),
                      "color": "var(--chart-5)" if kind == "rx_power" else "var(--chart-3)", "width": 1.5}, points))
    defs = defs[:8]
    names = [d[0]["name"] for d in defs]
    for i, d in enumerate(defs):   # two sensors that would share a name stay distinguishable
        if names.count(d[0]["name"]) > 1:
            d[0]["name"] += f" #{i + 1}"
    if not defs:
        return None
    nums = [v for _s, pts in defs for _t, v in pts if v is not None]
    extra = {"yMin": float(int(min(nums)) - 2), "yMax": float(int(max(nums)) + 2)} if nums else {}
    return assemble(defs, seconds, "dBm", **extra)


# ------------------------------------------------------------------ instant queries ("extras")
def _first_digits(rows, label, limit=6):
    for r in rows or []:
        v = str((r.get("metric") or {}).get(label, ""))
        if v.isascii() and v.isdigit() and len(v) <= limit:
            return v
    return None


def _int_value(rows, lo, hi):
    for r in rows or []:
        v = r.get("value")
        if finite(v) and lo <= v <= hi:
            return int(v)
    return None


def _addr(raw):
    """A printable IP address from a dotted / colon address or a hex string (0xC0A80101), else ""."""
    text = clean(raw, 60)
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        pass
    digits = text[2:] if text[:2].lower() == "0x" else re.sub(r"[\s:]", "", text)
    if re.fullmatch(r"(?:[0-9A-Fa-f]{8}|[0-9A-Fa-f]{32})", digits or ""):
        try:
            return str(ipaddress.ip_address(bytes.fromhex(digits)))
        except ValueError:
            return ""
    return ""


def _ident(raw):
    """A chassis / port identifier: a MAC when it is one, else the text."""
    return metrics.parse_mac(raw) or clean(raw, 100)


def parse_lldp(rows):
    groups = {}
    for r in rows or []:
        m = r.get("metric", {})
        name = m.get("__name__", "")
        if not name.startswith("lldpRem"):
            continue
        g = groups.setdefault(clean(m.get("lldpRemIndex"), 12), {"proto": "LLDP"})
        field = {"lldpRemSysName": "system", "lldpRemPortId": "port", "lldpRemPortDesc": "port_desc", "lldpRemChassisId": "chassis",
                 "lldpRemSysDesc": "descr", "lldpRemManAddrIfSubtype": "ip"}.get(name)
        if field == "ip":
            g["ip"] = g.get("ip") or _addr(m.get("lldpRemManAddr"))
        elif field == "chassis" or field == "port":
            g[field] = _ident(m.get(name))
        elif field:
            g[field] = clean(m.get(name))
    return [g for g in groups.values() if any(g.get(k) for k in ("system", "port", "chassis", "ip"))][:4]


def parse_cdp(rows):
    groups = {}
    for r in rows or []:
        m = r.get("metric", {})
        name = m.get("__name__", "")
        if not name.startswith("cdpCache"):
            continue
        g = groups.setdefault(clean(m.get("cdpCacheDeviceIndex"), 12), {"proto": "CDP"})
        if name == "cdpCacheDeviceId":
            g["system"] = clean(m.get(name))
        elif name == "cdpCacheDevicePort":
            g["port"] = clean(m.get(name))
        elif name == "cdpCachePlatform":
            g["descr"] = clean(m.get(name))
        elif name == "cdpCacheAddress":
            g["ip"] = _addr(m.get(name))
    return [g for g in groups.values() if any(g.get(k) for k in ("system", "port", "ip"))][:4]


def parse_poe(rows):
    state = {}
    for r in rows or []:
        m, v = r.get("metric", {}), r.get("value")
        name = m.get("__name__", "")
        if name == "pethPsePortAdminEnable" and finite(v):
            state["admin"] = POE_ADMIN.get(int(v))
        elif name == "pethPsePortDetectionStatus" and finite(v):
            state["detection"] = POE_DETECTION.get(int(v))
        elif name == "pethPsePortPowerClassifications" and finite(v) and 1 <= v <= 5:
            state["class"] = f"class {int(v) - 1}"
        elif name in POE_POWER_METRICS and finite(v) and 0 <= v <= 400000:
            state["power_w"] = round(v / 1000.0, 2)
    return {k: v for k, v in state.items() if v is not None} or None


def parse_ips(rows):
    out = []
    for r in rows or []:
        try:
            ip = str(ipaddress.ip_address(clean((r.get("metric") or {}).get("ipAdEntAddr"), 45)))
        except ValueError:
            continue
        if ip not in out:
            out.append(ip)
    return sorted(out, key=lambda a: (ipaddress.ip_address(a).version, int(ipaddress.ip_address(a))))[:16]


def parse_lag_members(rows):
    out = []
    for r in rows or []:
        v = str((r.get("metric") or {}).get("dot3adAggPortIndex", ""))
        if v.isascii() and v.isdigit() and len(v) <= 9 and int(v) not in out:
            out.append(int(v))
    return sorted(out)[:64]


def port_extras(f, device, iface, use_cache=True):
    """The instant-query side of the page, one cached dict per port (EXTRAS_TTL):
    ips, lag {"parent", "members"}, vlan {"access", "native", "pvid"}, stp, neighbours, sensors, thresholds, poe, "errors".
    A query that fails leaves its part empty and is named in "errors"; nothing is invented."""
    reason = unavailable_reason(device)
    if reason:
        return {"reason": reason}
    key = f"nwport:x:{device.pk}:{iface.if_index}"
    hit = cache.get(key) if use_cache else None
    if hit is not None:
        return hit
    inst, n = instance_label(device), int(iface.if_index)
    sel = f'instance="{inst}"'
    port = f'{sel},ifIndex="{n}"'
    out = {"ips": [], "lag": {"parent": None, "members": []}, "vlan": {}, "stp": None, "neighbours": [], "sensors": [],
           "thresholds": {}, "poe": None, "errors": []}

    ips = f.instant(f"ipAdEntIfIndex{{{sel}}} == {n}")
    out["ips"] = parse_ips(ips)
    parent = f.instant(f'dot3adAggPortAttachedAggID{{{sel},dot3adAggPortIndex="{n}"}}')
    out["lag"]["parent"] = _int_value(parent, 1, 2 ** 31)
    if out["lag"]["parent"] == n:
        out["lag"]["parent"] = None
    out["lag"]["members"] = parse_lag_members(f.instant(f"dot3adAggPortAttachedAggID{{{sel}}} == {n}"))
    bridge = _first_digits(f.instant(f"dot1dBasePortIfIndex{{{sel}}} == {n}"), "dot1dBasePort")
    if bridge:
        out["vlan"]["pvid"] = _int_value(f.instant(f'dot1qPvid{{{sel},dot1dBasePort="{bridge}"}}'), 1, 4094)
        state = f.instant(f'dot1dStpPortState{{{sel},dot1dStpPort="{bridge}"}}')
        code = _int_value(state, 1, 6)
        label = next((clean(r["metric"].get("dot1dStpPortState"), 20) for r in state or []
                      if r.get("value") == 1 and r.get("metric", {}).get("dot1dStpPortState")), None)
        out["stp"] = STP_STATES.get(code) if code and not label else label
    out["vlan"]["access"] = _int_value(f.instant(f"vmVlan{{{port}}}"), 1, 4094)
    out["vlan"]["native"] = _int_value(f.instant(f'vlanTrunkPortNativeVlan{{{sel},vlanTrunkPortIfIndex="{n}"}}'), 1, 4094)
    out["vlan"] = {k: v for k, v in out["vlan"].items() if v}
    local = f'lldpRemLocalPortNum="{n}"'
    out["neighbours"] = parse_lldp(f.instant(
        f'{{__name__=~"lldpRemSysName|lldpRemPortId|lldpRemPortDesc|lldpRemChassisId|lldpRemSysDesc|lldpRemManAddrIfSubtype",{sel},{local}}}'))
    out["neighbours"] += parse_cdp(f.instant(
        f'{{__name__=~"cdpCacheDeviceId|cdpCacheDevicePort|cdpCachePlatform|cdpCacheAddress",{sel},cdpCacheIfIndex="{n}"}}'))
    regex = name_regex(iface.name)
    if regex:
        out["sensors"] = parse_sensors(f.instant(optics_query(device, iface)), iface.name)
        if out["sensors"]:
            out["thresholds"] = parse_thresholds(f.instant(
                f'entSensorThresholdValue{{{sel},entPhysicalName=~"{regex}"}}'), out["sensors"])
    out["poe"] = parse_poe(f.instant(
        '{__name__=~"' + "|".join(("pethPsePortAdminEnable", "pethPsePortDetectionStatus", "pethPsePortPowerClassifications")
                                 + POE_POWER_METRICS) + f'",{port}}}'))
    out["errors"] = list(f.errors)
    cache.set(key, out, EXTRAS_TTL if not f.errors else EXTRAS_FAIL_TTL)
    try:
        cache.set(f"nwport:o:{device.pk}:{iface.if_index}", [s["name"] for s in out["sensors"]], OPTICS_FLAG_TTL)
    except Exception:  # noqa: BLE001 - a cache that cannot store a list just means no live optics chart
        pass
    return out


def cached_optics(device, iface):
    """Sensor names the page found for this port (the live poll draws the optical chart only then)."""
    return cache.get(f"nwport:o:{device.pk}:{iface.if_index}") or []


def explorer_query(device, iface):
    """The PromQL behind the traffic graph, for "Open in Metrics explorer" (None without a usable instance)."""
    sel = selector(device, iface.if_index)
    if sel is None:
        return None
    return f'({_counter("ifHCInOctets", "ifInOctets", sel)}) * 8 / 1e6'
