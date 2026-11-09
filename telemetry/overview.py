"""Fleet Overview data layer (Monitoring → Overview): the Netdata room "Overview" page for LinkedEye.

One long page of synced charts that each aggregate every selected node_exporter host, grouped in sections
(Compute, Memory, Storage, Network, System). Everything is derived from Prometheus through ``telemetry.promapi``
and from the host list ``telemetry.fleet.host_states`` already builds for the Fleet page; nothing is invented, and
a chart whose metric no node reports says so instead of drawing an empty frame.

Safety rules (each one has a test in tests_overview.py)

* PromQL is assembled from fixed text plus exactly three variable parts: the **instance matcher**, the rate
  window / step / offset (integers computed here) and nothing else. The matcher is built from instance ids that
  were validated against the user's site-scoped host list (``fleet.scope_hosts``), regex-escaped and quoted with
  ``fmt.promql_string``; a node id from the request that is not in that list is dropped, never queried.
* The time range comes from an allow-list (15m / 1h / 6h / 24h / 7d) or a bounded "last N minutes/hours/days"
  custom value; the step is computed server-side.
* Series per chart are bounded (``MAX_SERIES`` plus one "Other" series) and so is the number of nodes in a
  matcher (``MAX_MATCH`` / ``MAX_NODES``).
* **Call budget.** A cold view issues at most ``MAX_QUERIES`` Prometheus requests, whatever the number of
  hosts: one metric probe (cached 5 minutes) plus one range query per chart series group (dimension view, 22),
  or one per chart (by node / site / environment / client view, 15), plus one per chart when "compare to previous
  period" is on (15), so 38 at most. The host list itself comes from the Fleet page's cache. Results are cached for
  ``CACHE_SECONDS`` keyed by matcher + options, so a refresh storm costs one set of queries per 30 seconds.
* One failing query only blanks its own chart (the page says which); when every query fails the Prometheus
  error is shown as a banner.
"""
import hashlib
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone as dt_timezone

from django.core.cache import cache
from django.utils import timezone

from core import prometheus
from core.access import visible_sites

from . import fleet, palette as pal, promapi
from . import hosts as hosts_svc
from .fmt import finite, promql_string
from .hosts_full import DISK_FILTER, _gauge, fmt_value, legend_stats

log = logging.getLogger("linkedeye.telemetry")

CACHE_SECONDS = hosts_svc.CACHE_SECONDS      # chart payloads: same freshness as every other telemetry page
PROBE_SECONDS = 300                          # which metrics exist barely changes
MAX_QUERIES = 45                             # documented call budget for one cold view (see module docstring)
MAX_SERIES = 10                              # series drawn per chart in the per-node / grouped views, then "Other"
MAX_MATCH = 500                              # instances in one regex matcher (the URL stays well under 16 KB)
MAX_NODES = 200                              # nodes drawn individually (per-instance result sets are large)
POINTS, BAR_POINTS, NODE_POINTS = 120, 60, 60
EXPLORE_MAX = 1800                           # longer queries are not offered to the Metrics Explorer link
CSV_MAX_ROWS = 2000
GIB = 1024.0 ** 3

RANGES = {"15m": ("Last 15 minutes", 900), "1h": ("Last hour", 3600), "6h": ("Last 6 hours", 6 * 3600),
          "24h": ("Last 24 hours", 86400), "7d": ("Last 7 days", 7 * 86400)}
DEFAULT_RANGE = "15m"
CUSTOM_UNITS = {"m": ("minute", 60), "h": ("hour", 3600), "d": ("day", 86400)}
CUSTOM_MIN, CUSTOM_MAX = 300, 30 * 86400
LIVE_CHOICES = (15, 30, 60)

BY_CHOICES = [("", "Dimension"), ("node", "Node"), ("site", "Site"), ("env", "Environment"), ("client", "Client")]
BY_KEYS = tuple(k for k, _l in BY_CHOICES if k)
OTHER = "Other"
UNASSIGNED = fleet.UNKNOWN

PREVIOUS = "Previous period"
CPU_MODES = ["user", "system", "iowait", "irq", "softirq", "steal", "nice"]


# ----------------------------------------------------------------------------- request parameters
def _digits(value, limit=6):
    s = str(value or "")
    return int(s) if s.isascii() and s.isdigit() and len(s) <= limit else None


def parse_range(get):
    """``(key, label, seconds)`` for the request. ``range=custom&last=N&unit=m|h|d`` is bounded to 5 min..30 days;
    anything unknown falls back to the default 15 minutes."""
    key = (get.get("range") or "").strip()
    if key in RANGES:
        return key, RANGES[key][0], RANGES[key][1]
    if key == "custom":
        n, unit = _digits(get.get("last")), get.get("unit") if get.get("unit") in CUSTOM_UNITS else "h"
        if n:
            word, mult = CUSTOM_UNITS[unit]
            seconds = max(CUSTOM_MIN, min(CUSTOM_MAX, n * mult))
            if seconds == n * mult:
                return "custom", f"Last {n} {word}{'s' if n != 1 else ''}", seconds
            return "custom", "Last " + _human_seconds(seconds) + " (limited)", seconds
    return DEFAULT_RANGE, RANGES[DEFAULT_RANGE][0], RANGES[DEFAULT_RANGE][1]


def _human_seconds(s):
    if s % 86400 == 0:
        n = s // 86400
        return f"{n} day{'s' if n != 1 else ''}"
    if s % 3600 == 0:
        n = s // 3600
        return f"{n} hour{'s' if n != 1 else ''}"
    n = max(1, s // 60)
    return f"{n} minute{'s' if n != 1 else ''}"


class Params:
    """Everything the page state consists of. Built from GET only, so a URL is a shareable view."""

    def __init__(self, get):
        self.range_key, self.range_label, self.seconds = parse_range(get)
        self.last = get.get("last", "") if self.range_key == "custom" else ""
        unit = get.get("unit")
        self.unit = unit if self.range_key == "custom" and unit in CUSTOM_UNITS else ("h" if self.range_key == "custom" else "")
        by = (get.get("by") or "").strip().lower()
        self.by = by if by in BY_KEYS else ""
        self.compare = get.get("compare") == "1"
        live = _digits(get.get("live"), 3)
        self.live = live if live in LIVE_CHOICES else 0
        self.nodes = [n for n in dict.fromkeys(get.getlist("nodes") if hasattr(get, "getlist") else []) if n][:2000]
        self.site = (get.get("site") or "").strip()
        env = (get.get("env") or "").strip()
        self.env = env
        # the client NAME filter has its own parameter: ``client`` belongs to the sidebar's client picker (a client id,
        # kept in the session by core.client_scope), which the sidebar forms send along with every site change
        self.client = (get.get("client_name") or "").strip()

    @property
    def filtered(self):
        return bool(self.nodes or self.site or self.env or self.client)


# ----------------------------------------------------------------------------- the host selection
def _re2_escape(text):
    """Escape an instance id for an RE2 (Prometheus) regular expression: every ASCII character that is not a word
    character gets a backslash, which RE2 reads as that literal character."""
    return re.sub(r"[^A-Za-z0-9_\u0080-\U0010ffff]", lambda m: "\\" + m.group(0), text)


def matcher_for(instances, whole_fleet=False):
    """The PromQL label matcher for a validated list of instance ids.

    ``whole_fleet`` (an unrestricted user looking at every node) uses the node-job matcher the host pages already
    use, so a 3000 host fleet does not put 3000 names in the URL. A single host reuses ``hosts.host_selector``."""
    if whole_fleet:
        return hosts_svc.NODE_JOB
    names = sorted(dict.fromkeys(i for i in instances if i))
    if len(names) == 1:
        return hosts_svc.host_selector(names[0])
    return "instance=~" + promql_string("|".join(_re2_escape(n) for n in names))


def _group_of(host, by):
    if by == "site":
        return (host.get("site") or "").strip() or UNASSIGNED
    if by == "env":
        return host.get("environment_label") or UNASSIGNED
    if by == "client":
        return (host.get("client") or "").strip() or UNASSIGNED
    return host.get("name") or host["instance"]


class Selection:
    """The hosts a view covers and how they are matched in PromQL."""

    def __init__(self, visible, params, user):
        self.visible = [h for h in visible if h.get("instance")]
        with_metrics = [h for h in self.visible if h["state"] != fleet.NEVER_SEEN]
        rows = with_metrics
        if params.site:
            rows = [h for h in rows if (h.get("site") or "") == params.site]
        if params.env:
            rows = [h for h in rows if (h.get("environment") or "") == params.env]
        if params.client:
            rows = [h for h in rows if (h.get("client") or "") == params.client]
        valid = {h["instance"] for h in with_metrics}
        self.ignored = [n for n in params.nodes if n not in valid]
        if params.nodes:
            chosen = {n for n in params.nodes if n in valid}
            rows = [h for h in rows if h["instance"] in chosen]
        self.hosts = rows
        self.unrestricted = visible_sites(user) is None
        self.whole_fleet = bool(self.unrestricted and not params.filtered and rows and len(rows) == len(with_metrics))
        self.truncated = 0

    def matcher(self, by):
        """``(matcher, instances)`` for the chosen view. The per-node / grouped views always list instances (bounded
        by MAX_NODES, the rest is reported as truncated); the dimension view may use the node-job matcher."""
        insts = [h["instance"] for h in self.hosts]
        if by:
            self.truncated = max(0, len(insts) - MAX_NODES)
            insts = insts[:MAX_NODES]
            return matcher_for(insts), insts
        if self.whole_fleet:
            return matcher_for(insts, whole_fleet=True), insts
        self.truncated = max(0, len(insts) - MAX_MATCH)
        insts = insts[:MAX_MATCH]
        return matcher_for(insts), insts


# ----------------------------------------------------------------------------- chart specification
def _g(q, name=None, label=None, names=None):
    """One query_range: a fixed series name, or one series per value of `label` (named through `names`)."""
    return {"q": q, "name": name, "label": label, "names": names or {}}


def _c(key, section, title, unit, dims, metrics, node=None, total=None, kind="line", stacked=False, ymax=None,
       agg="sum", order=None, node_note="", hint="", area=False):
    return {"key": key, "section": section, "title": title, "unit": unit, "dims": dims, "metrics": tuple(metrics),
            "node": node, "total": total, "kind": kind, "stacked": stacked, "ymax": ymax, "agg": agg,
            "order": order or [], "node_note": node_note, "hint": hint, "area": area}


SECTIONS = [("compute", "Compute", "CPU, load, processes"), ("memory", "Memory", "RAM and swap"),
            ("storage", "Storage", "Disk space and I/O"), ("network", "Network", "Traffic, errors, TCP"),
            ("system", "System", "File descriptors, uptime, temperature")]


def chart_specs(sel, win, off=""):
    """The chart list. ``sel`` is the validated matcher, ``win`` a rate window ("300s"), ``off`` "" or
    " offset 900s" (used only to build the compare-to-previous-period query)."""
    FS, NET = hosts_svc.FS_FILTER, hosts_svc.NET_FILTER

    def m(name, extra=""):
        return f"{name}{{{sel}{',' + extra if extra else ''}}}{off}"

    def rate(name, extra=""):
        return f"rate({name}{{{sel}{',' + extra if extra else ''}}}[{win}]{off})"

    def mre(names, extra=""):          # selector over several metric names
        return f'{{__name__=~"{names}",{sel}{"," + extra if extra else ""}}}{off}'

    def rre(names, extra=""):
        return f'rate({{__name__=~"{names}",{sel}{"," + extra if extra else ""}}}[{win}]{off})'

    idle = f'node_cpu_seconds_total{{mode="idle",{sel}}}{off}'
    idle_rate = f'rate(node_cpu_seconds_total{{mode="idle",{sel}}}[{win}]{off})'
    fs_pct = f"100 * (1 - {m('node_filesystem_avail_bytes', FS)} / {m('node_filesystem_size_bytes', FS)})"
    mem_used = (f"{m('node_memory_MemTotal_bytes')} - {m('node_memory_MemFree_bytes')}"
                f" - {m('node_memory_Buffers_bytes')} - {m('node_memory_Cached_bytes')}")
    swap_used = f"{m('node_memory_SwapTotal_bytes')} - {m('node_memory_SwapFree_bytes')}"
    net_rw = (lambda a, b: f"{rate(a, NET)} + {rate(b, NET)}")
    return [
        _c("cpu", "compute", "CPU utilisation", "%",
           [_g(f'100 * sum by (mode) (rate(node_cpu_seconds_total{{mode=~"{"|".join(CPU_MODES)}",{sel}}}[{win}]{off}))'
               f" / scalar(count({idle}))", label="mode",
               names={"user": "User", "system": "System", "iowait": "I/O wait", "irq": "IRQ", "softirq": "Soft IRQ",
                      "steal": "Steal", "nice": "Nice"})],
           ["node_cpu_seconds_total"], kind="bars", stacked=True, ymax=100, agg="avg",
           order=["User", "System", "I/O wait", "IRQ", "Soft IRQ", "Steal", "Nice"],
           node=f"100 * (1 - avg by (instance) ({idle_rate}))", total=f"100 * (1 - avg({idle_rate}))",
           node_note="busy % per node"),
        _c("load", "compute", "Load average", "",
           [_g(f"avg by (__name__) ({mre('node_load1|node_load5|node_load15')})", label="__name__",
               names={"node_load1": "1 min", "node_load5": "5 min", "node_load15": "15 min"})],
           ["node_load1", "node_load5", "node_load15"], agg="avg", order=["1 min", "5 min", "15 min"],
           node=f"max by (instance) ({m('node_load1')})", total=f"avg({m('node_load1')})",
           node_note="1 min load per node"),
        _c("ctx", "compute", "Context switches and interrupts", "/s",
           [_g(f"sum({rate('node_context_switches_total')})", "Context switches"),
            _g(f"sum({rate('node_intr_total')})", "Interrupts")],
           ["node_context_switches_total", "node_intr_total"],
           node=f"sum by (instance) ({rate('node_context_switches_total')})",
           total=f"sum({rate('node_context_switches_total')})", node_note="context switches per node"),
        _c("procs", "compute", "Processes", "",
           [_g(f"sum by (__name__) ({mre('node_procs_running|node_procs_blocked')})", label="__name__",
               names={"node_procs_running": "Running", "node_procs_blocked": "Blocked"})],
           ["node_procs_running", "node_procs_blocked"], order=["Running", "Blocked"],
           node=f"sum by (instance) ({m('node_procs_running')})", total=f"sum({m('node_procs_running')})",
           node_note="running processes per node"),
        _c("mem", "memory", "Memory usage", "B",
           [_g(f"sum({mem_used})", "Used"),
            _g(f"sum by (__name__) ({mre('node_memory_(Cached|Buffers|MemFree)_bytes')})", label="__name__",
               names={"node_memory_Cached_bytes": "Cached", "node_memory_Buffers_bytes": "Buffers",
                      "node_memory_MemFree_bytes": "Free"})],
           ["node_memory_MemTotal_bytes"], kind="bars", stacked=True, order=["Used", "Cached", "Buffers", "Free"],
           node=f"sum by (instance) ({mem_used})", total=f"sum({mem_used})", node_note="used memory per node"),
        _c("swap", "memory", "Swap used", "B", [_g(f"sum({swap_used})", "Used")], ["node_memory_SwapTotal_bytes"],
           node=f"sum by (instance) ({swap_used})", total=f"sum({swap_used})", node_note="swap used per node", area=True),
        _c("disk_space", "storage", "Disk space used", "%",
           [_g(f"100 * (1 - sum({m('node_filesystem_avail_bytes', FS)}) / sum({m('node_filesystem_size_bytes', FS)}))",
               "Whole fleet"),
            _g(f"max({fs_pct})", "Fullest filesystem")],
           ["node_filesystem_size_bytes"], ymax=100, agg="max",
           node=f"max by (instance) ({fs_pct})", total=f"max({fs_pct})", node_note="fullest filesystem per node"),
        _c("disk_bw", "storage", "Disk throughput", "B/s",
           [_g(f"sum({rate('node_disk_read_bytes_total', DISK_FILTER)})", "Read"),
            _g(f"sum({rate('node_disk_written_bytes_total', DISK_FILTER)})", "Write")],
           ["node_disk_read_bytes_total", "node_disk_written_bytes_total"],
           node=f"sum by (instance) ({rate('node_disk_read_bytes_total', DISK_FILTER)}"
                f" + {rate('node_disk_written_bytes_total', DISK_FILTER)})",
           total=f"sum({rate('node_disk_read_bytes_total', DISK_FILTER)} + {rate('node_disk_written_bytes_total', DISK_FILTER)})",
           node_note="read + write per node"),
        _c("disk_iops", "storage", "Disk operations", "/s",
           [_g(f"sum({rate('node_disk_reads_completed_total', DISK_FILTER)})", "Reads"),
            _g(f"sum({rate('node_disk_writes_completed_total', DISK_FILTER)})", "Writes")],
           ["node_disk_reads_completed_total", "node_disk_writes_completed_total"],
           node=f"sum by (instance) ({rate('node_disk_reads_completed_total', DISK_FILTER)}"
                f" + {rate('node_disk_writes_completed_total', DISK_FILTER)})",
           total=f"sum({rate('node_disk_reads_completed_total', DISK_FILTER)} + {rate('node_disk_writes_completed_total', DISK_FILTER)})",
           node_note="reads + writes per node"),
        _c("net_bw", "network", "Network traffic", "B/s",
           [_g(f"sum({rate('node_network_receive_bytes_total', NET)})", "In"),
            _g(f"sum({rate('node_network_transmit_bytes_total', NET)})", "Out")],
           ["node_network_receive_bytes_total", "node_network_transmit_bytes_total"],
           node=f"sum by (instance) ({net_rw('node_network_receive_bytes_total', 'node_network_transmit_bytes_total')})",
           total=f"sum({net_rw('node_network_receive_bytes_total', 'node_network_transmit_bytes_total')})",
           node_note="in + out per node"),
        _c("net_pkts", "network", "Network packets", "/s",
           [_g(f"sum by (__name__) ({rre('node_network_(receive|transmit)_packets_total', NET)})", label="__name__",
               names={"node_network_receive_packets_total": "In", "node_network_transmit_packets_total": "Out"})],
           ["node_network_receive_packets_total", "node_network_transmit_packets_total"], order=["In", "Out"],
           node=f"sum by (instance) ({rre('node_network_(receive|transmit)_packets_total', NET)})",
           total=f"sum({rre('node_network_(receive|transmit)_packets_total', NET)})", node_note="in + out per node"),
        _c("net_errs", "network", "Network errors and drops", "/s",
           [_g(f"sum by (__name__) ({rre('node_network_(receive|transmit)_(errs|drop)_total', NET)})", label="__name__",
               names={"node_network_receive_errs_total": "Receive errors", "node_network_transmit_errs_total": "Transmit errors",
                      "node_network_receive_drop_total": "Receive drops", "node_network_transmit_drop_total": "Transmit drops"})],
           ["node_network_receive_errs_total", "node_network_transmit_errs_total", "node_network_receive_drop_total",
            "node_network_transmit_drop_total"],
           node=f"sum by (instance) ({rre('node_network_(receive|transmit)_(errs|drop)_total', NET)})",
           total=f"sum({rre('node_network_(receive|transmit)_(errs|drop)_total', NET)})", node_note="errors + drops per node"),
        _c("tcp", "network", "TCP connections", "",
           [_g(f"sum by (__name__) ({mre('node_netstat_Tcp_CurrEstab|node_sockstat_TCP_tw')})", label="__name__",
               names={"node_netstat_Tcp_CurrEstab": "Established", "node_sockstat_TCP_tw": "Time-wait"})],
           ["node_netstat_Tcp_CurrEstab", "node_sockstat_TCP_tw"], order=["Established", "Time-wait"],
           node=f"sum by (instance) ({m('node_netstat_Tcp_CurrEstab')})", total=f"sum({m('node_netstat_Tcp_CurrEstab')})",
           node_note="established per node"),
        _c("fds", "system", "File descriptors allocated", "",
           [_g(f"sum({m('node_filefd_allocated')})", "Allocated")], ["node_filefd_allocated"],
           node=f"sum by (instance) ({m('node_filefd_allocated')})", total=f"sum({m('node_filefd_allocated')})",
           node_note="allocated per node", area=True),
        _c("temp", "system", "Temperature", "°C",
           [_g(f"max({m('node_hwmon_temp_celsius')})", "Hottest sensor"), _g(f"avg({m('node_hwmon_temp_celsius')})", "Average")],
           ["node_hwmon_temp_celsius"], agg="max", node=f"max by (instance) ({m('node_hwmon_temp_celsius')})",
           total=f"max({m('node_hwmon_temp_celsius')})", node_note="hottest sensor per node",
           hint="needs the hwmon collector"),
    ]


ALL_METRICS = sorted({m for c in chart_specs("instance=\"x\"", "300s") for m in c["metrics"]})
PROBE_QUERY = 'count by (__name__) ({__name__=~"' + "|".join(ALL_METRICS) + '"})'


# ----------------------------------------------------------------------------- probe (which metrics exist)
def probe(use_cache=True):
    """``(names, error)``: the node_exporter metric names (from ALL_METRICS) that Prometheus has series for. One
    instant query, cached PROBE_SECONDS (30 s while empty, so a fresh exporter shows up quickly). Never raises."""
    key = "telemetry:overview:probe:v1"
    if use_cache:
        hit = cache.get(key)
        if hit is not None:
            return hit, ""
    try:
        rows = promapi.query(PROBE_QUERY)
    except prometheus.PrometheusError as e:
        return set(), str(e)
    names = {(r.get("metric") or {}).get("__name__") for r in rows if finite(r.get("value"))}
    names = {n for n in names if n in ALL_METRICS}
    cache.set(key, names, PROBE_SECONDS if names else CACHE_SECONDS)
    return names, ""


# ----------------------------------------------------------------------------- series maths
def _as_map(values):
    return {t: v for t, v in values or () if finite(v)}


def _combine(maps, agg):
    """Pointwise sum / mean / max over {ts: value} maps (a timestamp only some series have uses those)."""
    stamps = sorted({t for m in maps for t in m})
    out = []
    for t in stamps:
        vs = [m[t] for m in maps if t in m]
        out.append((t, sum(vs) if agg == "sum" else max(vs) if agg == "max" else sum(vs) / len(vs)))
    return out


def _mean(values):
    vs = [v for _t, v in values if finite(v)]
    return sum(vs) / len(vs) if vs else None


def _dim_series(spec, results):
    """Series of the dimension view: [(name, [(ts, v)])] in the chart's declared order."""
    out = []
    for gi, g in enumerate(spec["dims"]):
        for row in results.get(("dim", spec["key"], gi)) or []:
            vals = row.get("values") or []
            if g["name"]:
                name = g["name"]
            else:
                raw = (row.get("metric") or {}).get(g["label"] or "", "")
                name = g["names"].get(raw, raw) or "?"
            if g["name"] and any(n == name for n, _v in out):
                continue
            if legend_stats(vals) is not None:
                out.append((name, vals))
    if spec["order"]:
        rank = {n: i for i, n in enumerate(spec["order"])}
        out.sort(key=lambda s: rank.get(s[0], len(rank)))
    return out


def _unique_names(hosts):
    """{instance: display name}: the CMDB name, with the instance appended when two hosts share a name."""
    names = {}
    seen = {}
    for h in hosts:
        seen[h["name"]] = seen.get(h["name"], 0) + 1
    for h in hosts:
        names[h["instance"]] = h["name"] if seen[h["name"]] == 1 else f"{h['name']} ({h['instance']})"
    return names


def _node_series(spec, rows, by, hosts_by_instance, names):
    """Per-node (by == "node") or grouped (site / env / client) series from one `by (instance)` result, capped at
    MAX_SERIES plus an "Other" series. Returns ([(name, values)], total_series_before_the_cap)."""
    groups = {}
    for row in rows or []:
        inst = (row.get("metric") or {}).get("instance")
        vals = row.get("values") or []
        if not inst or legend_stats(vals) is None:
            continue
        h = hosts_by_instance.get(inst)
        key = names.get(inst, inst) if by == "node" else (_group_of(h, by) if h else UNASSIGNED)
        groups.setdefault(key, []).append(_as_map(vals))
    merged = {k: (sorted(maps[0].items()) if len(maps) == 1 else _combine(maps, spec["agg"]))
              for k, maps in groups.items()}
    ranked = sorted(merged.items(), key=lambda kv: (-(_mean(kv[1]) or 0), kv[0]))
    total = len(ranked)
    top = ranked[:MAX_SERIES]
    rest = ranked[MAX_SERIES:]
    if rest:
        other = _combine([_as_map(v) for _k, v in rest], spec["agg"])
        top.append((f"{OTHER} ({len(rest)})", other))
    return top, total


# ----------------------------------------------------------------------------- one chart payload
def _legend_row(name, color, stats, unit):
    return {"name": name, "color": color, **{k: fmt_value(stats[k], unit) for k in ("last", "mean", "min", "max")},
            "raw": stats}


def _chart_payload(spec, series, compare, seconds, by, error="", reason="", total=None):
    """The template / Chart.js payload for one chart from its [(name, [(ts, v)])] series."""
    colored = []
    for i, (name, vals) in enumerate(series):
        colored.append((name, pal.STONE if name.startswith(OTHER + " (") else pal.SERIES[i % len(pal.SERIES)], vals))
    has_main = bool(colored)
    if compare:
        colored.append((PREVIOUS, pal.STONE, compare))
    legend_src = colored
    axis_unit = spec["unit"]
    if axis_unit == "B":      # the axis says "4 GiB", not "4000M B"; the legend below keeps the exact human bytes
        colored = [(n, c, [(t, v / GIB if finite(v) else v) for t, v in vals]) for n, c, vals in colored]
        axis_unit = "GiB"
    # a stack only makes sense for quantities that add up: per-node CPU % or load must not be piled on top of each other
    stacked = spec["stacked"] and not compare and (not by or spec["agg"] == "sum")
    data = hosts_svc.align(colored, seconds)
    if compare:
        for s in data["series"]:
            if s["name"] == PREVIOUS:
                s["dash"] = "5 4"
                s["width"] = 1.5
    if axis_unit:
        data["unit"] = axis_unit
    if spec["ymax"] is not None:
        data["yMax"] = spec["ymax"]
    kind = "bars" if spec["kind"] == "bars" and stacked else "line"
    if kind == "bars":
        data["stacked"] = True
    if spec["area"]:
        for s in data["series"]:
            if s["name"] != PREVIOUS:
                s["area"] = True
    data["title"] = spec["title"]
    legend = []
    for name, color, vals in legend_src:
        st = legend_stats(vals)
        if st is not None:
            legend.append(_legend_row(name, color, st, spec["unit"]))
    return {"key": spec["key"], "dom_id": "ov-" + spec["key"], "title": spec["title"], "unit": spec["unit"],
            "kind": kind, "stacked": stacked, "has": has_main, "data": data, "legend": legend,
            "error": error, "reason": reason, "metrics": ", ".join(spec["metrics"]), "hint": spec["hint"],
            "note": spec["node_note"] if by else "", "total": total if total is not None else len(series),
            "truncated": bool(total and total > MAX_SERIES), "explore_q": ""}


def _empty_chart(spec, reason, error=""):
    return {"key": spec["key"], "dom_id": "ov-" + spec["key"], "title": spec["title"], "unit": spec["unit"],
            "kind": spec["kind"], "stacked": False, "has": False, "data": None, "legend": [], "error": error,
            "reason": reason, "metrics": ", ".join(spec["metrics"]), "hint": spec["hint"], "note": "", "total": 0,
            "truncated": False, "explore_q": ""}


# ----------------------------------------------------------------------------- running the queries
def _run(jobs, seconds):
    """Run {name: (kind, promql, step)} concurrently. Returns ({name: rows}, {name: error text}); a failing job
    only blanks its own chart. Raises the first PrometheusError when every job failed."""
    results, errors = {}, {}

    def one(job):
        kind, q, step = job
        return promapi.query(q) if kind == "instant" else promapi.query_range(q, seconds, step)
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(jobs)))) as pool:
        futures = {name: pool.submit(one, job) for name, job in jobs.items()}
        for name, fut in futures.items():
            try:
                results[name] = fut.result()
            except prometheus.PrometheusError as e:
                errors[name] = str(e)
    if jobs and len(errors) == len(jobs):
        raise prometheus.PrometheusError(next(iter(errors.values())))
    return results, errors


def build_charts(sel, seconds, by, compare, present, hosts, use_cache=True):
    """Every chart of the page for one matcher: ``{"sections", "query_count", "errors", "step"}``.

    ``present`` is the probe result (metric names that exist). ``hosts`` are the selected host dicts, used to name
    per-node series and to map a node to its site / environment / client."""
    hosts_by_instance = {h["instance"]: h for h in hosts}
    names = _unique_names(hosts)
    group_sig = ""
    if by:
        group_sig = hashlib.sha1("|".join(f"{i}={names.get(i) if by == 'node' else _group_of(h, by)}"
                                          for i, h in sorted(hosts_by_instance.items())).encode()).hexdigest()
    ckey = "telemetry:overview:v1:" + hashlib.sha1(
        f"{sel}|{seconds}|{by}|{int(compare)}|{group_sig}|{','.join(sorted(present))}".encode()).hexdigest()
    if use_cache:
        hit = cache.get(ckey)
        if hit is not None:
            return hit

    steps = {"line": hosts_svc.step_for(seconds, NODE_POINTS if by else POINTS),
             "bars": hosts_svc.step_for(seconds, BAR_POINTS)}
    base_step = steps["line"]
    specs = chart_specs(sel, f"{max(300, base_step)}s")
    cmp_specs = {}
    if compare:
        # the previous period: the same total, evaluated `seconds` earlier (the offset modifier moves the data,
        # not the timestamps, so it lines up with the current series on the same axis)
        cmp_specs = {c["key"]: c for c in chart_specs(sel, f"{max(300, base_step)}s", off=f" offset {seconds}s")}

    jobs, active = {}, []
    for spec in specs:
        if not any(mn in present for mn in spec["metrics"]):
            continue
        step = steps["bars"] if (spec["kind"] == "bars" and not by) else base_step
        spec_step = step
        per_node = bool(by and spec["node"])
        if per_node:
            jobs[("node", spec["key"])] = ("range", spec["node"], spec_step)
        else:
            for gi, g in enumerate(spec["dims"]):
                jobs[("dim", spec["key"], gi)] = ("range", g["q"], spec_step)
        cmp_q = cmp_specs.get(spec["key"], {}).get("total") if compare else None
        if cmp_q:
            jobs[("cmp", spec["key"])] = ("range", cmp_q, spec_step)
        active.append((spec, per_node))
    if len(jobs) > MAX_QUERIES - 1:    # one more for the probe: the budget is a hard bound, not a hope
        raise RuntimeError(f"overview call budget exceeded: {len(jobs)} queries")
    results, errors = _run(jobs, seconds) if jobs else ({}, {})

    sections = []
    by_key = {spec["key"]: (spec, per_node) for spec, per_node in active}
    for sid, title, eyebrow in SECTIONS:
        charts = []
        for spec in (c for c in specs if c["section"] == sid):
            if spec["key"] not in by_key:
                charts.append(_empty_chart(spec, "not_reported"))
                continue
            _s, per_node = by_key[spec["key"]]
            keys = [("node", spec["key"])] if per_node else [("dim", spec["key"], gi) for gi in range(len(spec["dims"]))]
            failed = [errors[k] for k in keys if k in errors]
            if failed and len(failed) == len(keys):
                charts.append(_empty_chart(spec, "error", failed[0]))
                continue
            if per_node:
                series, total = _node_series(spec, results.get(("node", spec["key"])), by, hosts_by_instance, names)
            else:
                series, total = _dim_series(spec, results), None
            cmp_rows = results.get(("cmp", spec["key"])) or []
            cmp_vals = cmp_rows[0].get("values") if cmp_rows and legend_stats(cmp_rows[0].get("values")) else None
            if not series:
                charts.append(_empty_chart(spec, "no_data"))
                continue
            chart = _chart_payload(spec, series, cmp_vals, seconds, by if per_node else "", total=total)
            q = spec["node"] if per_node else spec["dims"][0]["q"]
            chart["explore_q"] = q if len(q) <= EXPLORE_MAX else ""
            if by and not per_node:
                chart["note"] = "no per-node view for this chart"
            charts.append(chart)
        sections.append({"id": sid, "title": title, "eyebrow": eyebrow, "charts": charts,
                         "shown": sum(1 for c in charts if c["has"]),
                         "missing": [c for c in charts if c["reason"] == "not_reported"]})
    out = {"sections": sections, "query_count": len(jobs) + 1, "errors": errors,
           "step": base_step, "range_seconds": seconds}
    cache.set(ckey, out, CACHE_SECONDS)
    return out


# ----------------------------------------------------------------------------- the whole page
UPTIME_BUCKETS = [("under an hour", 0, 3600), ("1 to 24 hours", 3600, 86400), ("1 to 7 days", 86400, 7 * 86400),
                  ("7 to 30 days", 7 * 86400, 30 * 86400), ("30 to 90 days", 30 * 86400, 90 * 86400),
                  ("over 90 days", 90 * 86400, None)]


def uptime_distribution(hosts):
    """[{label, count, pct}] of how long the live hosts have been up, from the fleet cache (no query)."""
    ups = [h["uptime"] for h in hosts if h.get("state") == fleet.LIVE and finite(h.get("uptime"))]
    out = []
    for label, lo, hi in UPTIME_BUCKETS:
        n = sum(1 for u in ups if u >= lo and (hi is None or u < hi))
        out.append({"label": label, "count": n, "pct": round(100.0 * n / len(ups), 1) if ups else 0})
    return out, len(ups)


def _avg(rows, field):
    vals = [h[field] for h in rows if finite(h.get(field))]
    return (sum(vals) / len(vals)) if vals else None


def strip_data(hosts, selected):
    """NodesSummary strip + gauges, from the cached host list (no Prometheus call)."""
    live = [h for h in selected if h["state"] == fleet.LIVE]
    rx = sum(h["rx"] for h in live if finite(h.get("rx")))
    tx = sum(h["tx"] for h in live if finite(h.get("tx")))
    gauges = {"cpu": _gauge("cpu", "", _avg(live, "cpu"), [], "now", sub=f"{len(live)} live nodes"),
              "memory": _gauge("memory", "", _avg(live, "memory"), [], "now", sub="used"),
              "disk": _gauge("disk", "", _avg(live, "disk"), [], "now", sub="used on /")}
    return {"counts": fleet.state_counts(hosts), "selected": len(selected), "live": len(live),
            "gauges": gauges, "rx": rx, "tx": tx}


def hot_nodes(selected, limit=10):
    """Live nodes above a warning threshold (the thresholds of the Infrastructure page), worst first."""
    rows = []
    for h in selected:
        if h["state"] != fleet.LIVE:
            continue
        grades = {m: hosts_svc.grade(m, h.get(m)) for m in ("cpu", "memory", "disk")}
        rank = max((2 if g == "crit" else 1 if g == "warn" else 0) for g in grades.values())
        if rank:
            worst = max((h[m] for m in ("cpu", "memory", "disk") if finite(h.get(m)) and grades[m] != "ok"), default=0)
            rows.append({"host": h, "grades": grades, "rank": rank, "worst": worst})
    rows.sort(key=lambda r: (-r["rank"], -r["worst"], r["host"]["name"]))
    return rows[:limit]


def page_data(user, params, use_cache=True):
    """Everything the page and the live JSON need. Never raises a PrometheusError: ``prom_error`` says what failed."""
    configured = prometheus.configured()
    fl = fleet.host_states(use_cache=use_cache, with_prometheus=configured)
    hosts = fleet.scope_hosts(user, fl["hosts"])
    sel = Selection(hosts, params, user)
    out = {"configured": configured, "hosts": hosts, "selection": sel, "fleet_errors": dict(fl["errors"]),
           "charts": None, "prom_error": "", "present": set(), "matcher": "", "matched": 0}
    out["strip"] = strip_data(hosts, sel.hosts)
    out["uptime"], out["uptime_n"] = uptime_distribution(sel.hosts)
    if not configured:
        return out
    if fl["errors"].get("hosts"):
        out["prom_error"] = fl["errors"]["hosts"]
        return out
    if not sel.hosts:
        return out
    present, perr = probe(use_cache=use_cache)
    out["present"] = present
    if perr:
        out["prom_error"] = perr
        return out
    matcher, insts = sel.matcher(params.by)
    out["matcher"], out["matched"] = matcher, len(insts)
    try:
        out["charts"] = build_charts(matcher, params.seconds, params.by, params.compare, present, sel.hosts,
                                     use_cache=use_cache)
    except prometheus.PrometheusError as e:
        out["prom_error"] = str(e)
    return out


# ----------------------------------------------------------------------------- JSON for Live mode / CSV
def live_payload(page):
    charts, legends = {}, {}
    for sec in (page["charts"] or {}).get("sections", []):
        for c in sec["charts"]:
            if c["has"]:
                charts[c["dom_id"]] = c["data"]
                legends[c["dom_id"]] = [{k: r[k] for k in ("name", "last", "mean", "min", "max")} for r in c["legend"]]
    strip = page["strip"]
    for k, dom in (("cpu", "ov-g-cpu"), ("memory", "ov-g-memory"), ("disk", "ov-g-disk")):
        charts[dom] = strip["gauges"][k]
    return {"generated": timezone.now().isoformat(timespec="seconds"), "charts": charts, "legends": legends,
            "counts": strip["counts"], "selected": strip["selected"], "error": page["prom_error"]}


def csv_rows(chart):
    """``(header, rows)`` of one chart: a UTC timestamp column and one column per series, bounded to CSV_MAX_ROWS."""
    data = chart["data"] or {}
    unit = f" [{data['unit']}]" if data.get("unit") else ""
    header = ["time_utc"] + [s["name"] + unit for s in data.get("series", [])]
    rows = []
    for i, t in enumerate((data.get("ts") or [])[:CSV_MAX_ROWS]):
        stamp = datetime.fromtimestamp(t, tz=dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows.append([stamp] + ["" if s["values"][i] is None else s["values"][i] for s in data["series"]])
    return header, rows
