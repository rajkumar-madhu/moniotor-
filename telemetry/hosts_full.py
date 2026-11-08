"""Full host metrics page (modelled on Netdata's node view) from node_exporter via Prometheus.

Builds on telemetry.hosts: the summary strip comes from fetch_host(); everything else is one query_range per
chart series group plus a handful of instant queries, all run through hosts._run_parallel and cached for
hosts.CACHE_SECONDS. Every PromQL string is built from a validated instance matcher (hosts.host_selector) and
the fixed filters below – never from free text. A chart whose metric is absent reports has=False so the page
can say "no <metric> for this host" instead of drawing an empty chart; nothing is ever invented.
"""
import hashlib
import logging

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from core.access import scope_by_site

from . import palette as pal, promapi
from .fmt import finite, from_epoch, human_bytes, human_duration, human_number, human_rate
from .hosts import (CACHE_SECONDS, FS_FILTER, HEALTH_LABEL, HEALTH_TONE, NET_FILTER, RANGES, _run_parallel, align,
                    grade, host_selector, step_for, thresholds)

log = logging.getLogger("linkedeye.telemetry")

DISK_FILTER = 'device!~"loop[0-9]+|ram[0-9]+|sr[0-9]+|fd[0-9]+"'
MAX_SERIES_PER_CHART = 12   # per-device charts on big hosts: keep the busiest, say how many were left out
STACKED_POINTS = 96         # stacked charts render as bars; fewer points keep the bars readable at 400 px
RATE = "[5m]"
GAUGE_DOM = {"cpu": "hm-g-cpu", "memory": "hm-g-memory", "disk": "hm-g-disk"}
SPARK_DOM = {"load": "hm-s-load", "net": "hm-s-net", "diskio": "hm-s-diskio"}


# ----------------------------------------------------------------------------- chart specification
def _g(q, name=None, label=None, template="{}", names=None):
    """One query_range: either a fixed series name or one series per value of `label` (named by template)."""
    return {"q": q, "name": name, "label": label, "template": template, "names": names or {}}


def _c(key, title, unit, groups, metrics, kind="line", stacked=False, ymax=None, hint="", area=False):
    return {"key": key, "title": title, "unit": unit, "groups": groups, "metrics": metrics, "kind": kind,
            "stacked": stacked, "ymax": ymax, "hint": hint, "area": area}


def sections_spec(instance):
    """Ordered sections and charts for one host. Queries embed only the validated instance matcher."""
    sel = host_selector(instance)
    cpu = f"node_cpu_seconds_total{{{sel}}}"
    cores = f'count(node_cpu_seconds_total{{mode="idle",{sel}}})'

    def m(name, extra=""):
        return f"{name}{{{sel}{',' + extra if extra else ''}}}"

    def rate(name, extra=""):
        return f"rate({m(name, extra)}{RATE})"

    def per_dev(name, extra, label, template, scale=""):
        return _g(f"{rate(name, extra)}{scale}", label=label, template=template)

    psi = [_g(f"100 * {rate('node_pressure_cpu_waiting_seconds_total')}", "CPU some"),
           _g(f"100 * {rate('node_pressure_memory_waiting_seconds_total')}", "Memory some"),
           _g(f"100 * {rate('node_pressure_memory_stalled_seconds_total')}", "Memory full"),
           _g(f"100 * {rate('node_pressure_io_waiting_seconds_total')}", "I/O some"),
           _g(f"100 * {rate('node_pressure_io_stalled_seconds_total')}", "I/O full")]
    modes = ["user", "system", "iowait", "irq", "softirq", "steal", "nice"]
    compute = [
        _c("cpu_modes", "CPU utilisation by mode", "%",
           [_g(f'100 * sum by (mode) (rate(node_cpu_seconds_total{{mode=~"{"|".join(modes)}",{sel}}}{RATE})) / scalar({cores})',
               label="mode")],
           "node_cpu_seconds_total", kind="bars", stacked=True, ymax=100),
        _c("load", "Load average", "",
           [_g(m("node_load1"), "1 min"), _g(m("node_load5"), "5 min"), _g(m("node_load15"), "15 min"),
            _g(cores, "CPU cores")],
           "node_load1 / node_load5 / node_load15"),
        _c("ctx", "Context switches and interrupts", "/s",
           [_g(rate("node_context_switches_total"), "Context switches"), _g(rate("node_intr_total"), "Interrupts")],
           "node_context_switches_total / node_intr_total"),
        _c("procs", "Processes", "",
           [_g(m("node_procs_running"), "Running"), _g(m("node_procs_blocked"), "Blocked")],
           "node_procs_running / node_procs_blocked"),
        _c("forks", "Process forks", "/s", [_g(rate("node_forks_total"), "Forks")], "node_forks_total", area=True),
        _c("psi", "Pressure stall (PSI)", "%", psi, "node_pressure_* metrics",
           hint="needs a kernel with CONFIG_PSI and node_exporter 1.0 or newer"),
        _c("timex", "Clock offset", "s", [_g(m("node_timex_offset_seconds"), "Offset")], "node_timex_offset_seconds"),
        _c("entropy", "Available entropy", "bits", [_g(m("node_entropy_available_bits"), "Entropy")],
           "node_entropy_available_bits"),
    ]
    mem = [
        _c("mem_usage", "Memory usage", "B",
           [_g(f"{m('node_memory_MemTotal_bytes')} - {m('node_memory_MemFree_bytes')} - {m('node_memory_Buffers_bytes')}"
               f" - {m('node_memory_Cached_bytes')}", "Used"),
            _g(m("node_memory_Cached_bytes"), "Cached"), _g(m("node_memory_Buffers_bytes"), "Buffers"),
            _g(m("node_memory_MemFree_bytes"), "Free")],
           "node_memory_MemTotal_bytes / MemFree / Buffers / Cached", kind="bars", stacked=True),
        _c("mem_avail", "Available memory", "B", [_g(m("node_memory_MemAvailable_bytes"), "Available")],
           "node_memory_MemAvailable_bytes", area=True),
        _c("committed", "Committed memory", "B",
           [_g(m("node_memory_Committed_AS_bytes"), "Committed_AS"), _g(m("node_memory_CommitLimit_bytes"), "CommitLimit")],
           "node_memory_Committed_AS_bytes / CommitLimit"),
        _c("swap", "Swap", "B",
           [_g(f"{m('node_memory_SwapTotal_bytes')} - {m('node_memory_SwapFree_bytes')}", "Used"),
            _g(m("node_memory_SwapTotal_bytes"), "Total")],
           "node_memory_SwapTotal_bytes / SwapFree"),
        _c("faults", "Page faults", "/s",
           [_g(rate("node_vmstat_pgfault"), "Minor + major"), _g(rate("node_vmstat_pgmajfault"), "Major")],
           "node_vmstat_pgfault / pgmajfault"),
        _c("paging", "Paging in / out", "B/s",
           [_g(f"1024 * {rate('node_vmstat_pgpgin')}", "In"), _g(f"1024 * {rate('node_vmstat_pgpgout')}", "Out")],
           "node_vmstat_pgpgin / pgpgout"),
        _c("slab", "Kernel slab", "B",
           [_g(m("node_memory_Slab_bytes"), "Slab"), _g(m("node_memory_SReclaimable_bytes"), "Reclaimable"),
            _g(m("node_memory_SUnreclaim_bytes"), "Unreclaimable")],
           "node_memory_Slab_bytes / SReclaimable / SUnreclaim"),
        _c("hugepages", "Huge pages", "pages",
           [_g(m("node_memory_HugePages_Total"), "Total"), _g(m("node_memory_HugePages_Free"), "Free"),
            _g(m("node_memory_HugePages_Rsvd"), "Reserved")],
           "node_memory_HugePages_* metrics", hint="the kernel reports them only when huge pages are configured"),
        _c("hwcorrupt", "Hardware-corrupted memory", "B",
           [_g(m("node_memory_HardwareCorrupted_bytes"), "Corrupted")], "node_memory_HardwareCorrupted_bytes"),
    ]
    disk = [
        _c("disk_bw", "Disk throughput", "B/s",
           [per_dev("node_disk_read_bytes_total", DISK_FILTER, "device", "{} read"),
            per_dev("node_disk_written_bytes_total", DISK_FILTER, "device", "{} write")],
           "node_disk_read_bytes_total / node_disk_written_bytes_total"),
        _c("disk_iops", "Disk operations", "/s",
           [per_dev("node_disk_reads_completed_total", DISK_FILTER, "device", "{} reads"),
            per_dev("node_disk_writes_completed_total", DISK_FILTER, "device", "{} writes")],
           "node_disk_reads_completed_total / node_disk_writes_completed_total"),
        _c("disk_lat", "Disk latency", "ms",
           [_g(f"1000 * {rate('node_disk_read_time_seconds_total', DISK_FILTER)} / {rate('node_disk_reads_completed_total', DISK_FILTER)}",
               label="device", template="{} read"),
            _g(f"1000 * {rate('node_disk_write_time_seconds_total', DISK_FILTER)} / {rate('node_disk_writes_completed_total', DISK_FILTER)}",
               label="device", template="{} write")],
           "node_disk_read_time_seconds_total / node_disk_write_time_seconds_total"),
        _c("disk_util", "Disk utilisation", "%",
           [_g(f"100 * {rate('node_disk_io_time_seconds_total', DISK_FILTER)}", label="device")],
           "node_disk_io_time_seconds_total", ymax=100),
        _c("disk_queue", "Disk queue (weighted I/O time)", "",
           [_g(rate("node_disk_io_time_weighted_seconds_total", DISK_FILTER), label="device")],
           "node_disk_io_time_weighted_seconds_total"),
    ]
    net = [
        _c("net_bw", "Network traffic", "B/s",
           [per_dev("node_network_receive_bytes_total", NET_FILTER, "device", "{} in"),
            per_dev("node_network_transmit_bytes_total", NET_FILTER, "device", "{} out")],
           "node_network_receive_bytes_total / node_network_transmit_bytes_total"),
        _c("net_pkts", "Network packets", "/s",
           [per_dev("node_network_receive_packets_total", NET_FILTER, "device", "{} in"),
            per_dev("node_network_transmit_packets_total", NET_FILTER, "device", "{} out")],
           "node_network_receive_packets_total / node_network_transmit_packets_total"),
        _c("net_errs", "Network errors", "/s",
           [per_dev("node_network_receive_errs_total", NET_FILTER, "device", "{} in"),
            per_dev("node_network_transmit_errs_total", NET_FILTER, "device", "{} out")],
           "node_network_receive_errs_total / node_network_transmit_errs_total"),
        _c("net_drops", "Network drops", "/s",
           [per_dev("node_network_receive_drop_total", NET_FILTER, "device", "{} in"),
            per_dev("node_network_transmit_drop_total", NET_FILTER, "device", "{} out")],
           "node_network_receive_drop_total / node_network_transmit_drop_total"),
        _c("tcp_estab", "TCP connections", "",
           [_g(m("node_netstat_Tcp_CurrEstab"), "Established")], "node_netstat_Tcp_CurrEstab", area=True),
        _c("tcp_opens", "TCP opens", "/s",
           [_g(rate("node_netstat_Tcp_ActiveOpens"), "Active"), _g(rate("node_netstat_Tcp_PassiveOpens"), "Passive")],
           "node_netstat_Tcp_ActiveOpens / PassiveOpens"),
        _c("tcp_retrans", "TCP retransmits and UDP errors", "/s",
           [_g(rate("node_netstat_Tcp_RetransSegs"), "TCP retransmitted segments"),
            _g(rate("node_netstat_Udp_InErrors"), "UDP receive errors")],
           "node_netstat_Tcp_RetransSegs / node_netstat_Udp_InErrors"),
        _c("sockets", "Sockets", "",
           [_g(m("node_sockstat_TCP_inuse"), "TCP in use"), _g(m("node_sockstat_TCP_tw"), "TCP time-wait"),
            _g(m("node_sockstat_UDP_inuse"), "UDP in use"), _g(m("node_sockstat_sockets_used"), "All used")],
           "node_sockstat_* metrics"),
        _c("conntrack", "Connection tracking", "",
           [_g(m("node_nf_conntrack_entries"), "Entries"), _g(m("node_nf_conntrack_entries_limit"), "Limit")],
           "node_nf_conntrack_* metrics", hint="exported only when the nf_conntrack kernel module is loaded"),
    ]
    return [
        {"id": "compute", "title": "Compute", "eyebrow": "CPU, load, processes", "charts": compute},
        {"id": "memory", "title": "Memory", "eyebrow": "RAM, swap, paging", "charts": mem},
        {"id": "storage", "title": "Storage", "eyebrow": "Disks and filesystems", "charts": disk},
        {"id": "network", "title": "Network", "eyebrow": "Interfaces, TCP/UDP", "charts": net},
    ]


def gauge_queries(instance):
    """Range queries the summary gauges need beyond what hosts.fetch_host already fetched (CPU %, memory %)."""
    sel = host_selector(instance)
    return {
        "disk_root": (f'100 * max by (instance) (1 - (node_filesystem_avail_bytes{{mountpoint="/",{FS_FILTER},{sel}}}'
                      f' / node_filesystem_size_bytes{{mountpoint="/",{FS_FILTER},{sel}}}))'),
    }


def instant_queries(instance):
    sel = host_selector(instance)
    return {
        "cores": f'100 * (1 - rate(node_cpu_seconds_total{{mode="idle",{sel}}}{RATE}))',
        "dmi": f"node_dmi_info{{{sel}}}",
        "tz": f"node_time_zone_offset_seconds{{{sel}}}",
        "boot": f"node_boot_time_seconds{{{sel}}}",
        "build": f"node_exporter_build_info{{{sel}}}",
        "fs_files": f"node_filesystem_files{{{FS_FILTER},{sel}}}",
        "fs_files_free": f"node_filesystem_files_free{{{FS_FILTER},{sel}}}",
        "fs_err": f"node_filesystem_device_error{{{FS_FILTER},{sel}}}",
        "net_up": f"node_network_up{{{NET_FILTER},{sel}}}",
        "net_carrier": f"node_network_carrier{{{NET_FILTER},{sel}}}",
        "net_speed": f"node_network_speed_bytes{{{NET_FILTER},{sel}}}",
        "net_mtu": f"node_network_mtu_bytes{{{NET_FILTER},{sel}}}",
        "net_info": f"node_network_info{{{NET_FILTER},{sel}}}",
        "cadv_cpu": f'100 * sum by (name, image) (rate(container_cpu_usage_seconds_total{{name!="",{sel}}}{RATE}))',
        "cadv_mem": f'sum by (name, image) (container_memory_working_set_bytes{{name!="",{sel}}})',
    }


# ----------------------------------------------------------------------------- stats / formatting
def legend_stats(values):
    """Last / mean / min / max of the finite values in a query_range series ([(ts, v)] or [v]). None if empty."""
    vs = [v[1] if isinstance(v, (tuple, list)) else v for v in values or ()]
    vs = [float(v) for v in vs if finite(v)]
    if not vs:
        return None
    return {"last": vs[-1], "mean": sum(vs) / len(vs), "min": min(vs), "max": max(vs)}


def fmt_value(v, unit):
    if not finite(v):
        return "–"
    if unit == "%":
        return f"{v:.1f}%"
    if unit == "B":
        return human_bytes(v)
    if unit == "B/s":
        return human_rate(v)
    if unit == "ms":
        return f"{v:.2f} ms" if abs(v) < 100 else f"{human_number(v)} ms"
    if unit == "s":
        return f"{v * 1000:.3g} ms" if abs(v) < 1 else f"{human_number(v)} s"
    n = human_number(v)
    return f"{n}{unit}" if unit == "/s" else f"{n} {unit}" if unit else n


def _series_name(group, metric):
    if group.get("name"):
        return group["name"]
    raw = (metric or {}).get(group.get("label") or "", "")
    return group.get("template", "{}").format(group.get("names", {}).get(raw, raw) or "?")


def build_chart(spec, results, seconds):
    """Chart.js data + legend rows for one chart spec from its groups' query_range rows."""
    series = []
    for gi, group in enumerate(spec["groups"]):
        for row in results.get((spec["key"], gi)) or []:
            vals = row.get("values") or []
            st = legend_stats(vals)
            if st is None:
                continue
            series.append({"name": _series_name(group, row.get("metric")), "vals": vals, "stats": st, "q": group["q"]})
    total = len(series)
    if total > MAX_SERIES_PER_CHART:
        series = sorted(series, key=lambda s: -s["stats"]["max"])[:MAX_SERIES_PER_CHART]
    for i, s in enumerate(series):
        s["color"] = pal.SERIES[i % len(pal.SERIES)]
    data = align([(s["name"], s["color"], s["vals"]) for s in series], seconds)
    if spec["unit"]:
        data["unit"] = spec["unit"]
    if spec["ymax"] is not None:
        data["yMax"] = spec["ymax"]
    if spec["kind"] == "bars":
        data["stacked"] = bool(spec["stacked"])
    if spec["area"]:
        for s in data["series"]:
            s["area"] = True
    legend = [{"name": s["name"], "color": s["color"],
               **{k: fmt_value(s["stats"][k], spec["unit"]) for k in ("last", "mean", "min", "max")},
               "raw": s["stats"]} for s in series]
    return {"key": spec["key"], "dom_id": "hm-" + spec["key"], "title": spec["title"], "unit": spec["unit"],
            "kind": spec["kind"], "metrics": spec["metrics"], "hint": spec["hint"], "has": bool(series),
            "data": data, "legend": legend, "total": total, "truncated": total > len(series),
            "explore_q": spec["groups"][0]["q"],
            "groups": [{"q": g["q"], "name": g["name"] or g["template"].format(f"per {g['label']}")} for g in spec["groups"]]}


# ----------------------------------------------------------------------------- instant-result tables
def _first_metric(rows):
    return (rows[0].get("metric") or {}) if rows else {}


def _first_value(rows):
    for r in rows or ():
        if finite(r.get("value")):
            return float(r["value"])
    return None


def _keyed(rows, *labels):
    out = {}
    for r in rows or ():
        mt = r.get("metric") or {}
        out[tuple(mt.get(k, "") for k in labels)] = r
    return out


def core_table(rows):
    cores = []
    for r in rows or ():
        v = r.get("value")
        if finite(v):
            cpu = (r.get("metric") or {}).get("cpu", "")
            busy = max(0.0, min(100.0, float(v)))
            cores.append({"cpu": cpu, "busy": busy, "grade": grade("cpu", busy), "text": f"{busy:.1f}%"})
    return sorted(cores, key=lambda c: (-c["busy"], c["cpu"]))


# ----------------------------------------------------------------------------- summary strip (gauges, sparks, KPIs)
def _edges(values):
    """(first, last) finite values of an aligned series, or (None, None)."""
    vs = [v for v in values or () if finite(v)]
    return (vs[0], vs[-1]) if vs else (None, None)


def _delta(values, range_key, unit="%"):
    """Change over the range as {value, text}: '+1.2 pts in 1h' / 'no change in 1h'; None without two points."""
    vs = [v for v in values or () if finite(v)]
    if len(vs) < 2:
        return None
    d = vs[-1] - vs[0]
    if abs(d) < 0.05:
        return {"value": 0.0, "text": f"no change in {range_key}"}
    word = "pts" if unit == "%" else unit
    return {"value": round(d, 2), "text": f"{d:+.1f} {word} in {range_key}"}


def _gauge(metric, label, value, values, range_key, sub=""):
    warn, crit = thresholds().get(metric, (70.0, 90.0))
    return {"value": round(value, 1) if finite(value) else None, "max": 100, "unit": "%", "label": label,
            "text": fmt_value(value, "%"), "tone": grade(metric, value), "sub": sub,
            "delta": _delta(values, range_key),
            "thresholds": [{"to": warn, "color": "var(--chart-ok)"}, {"to": crit, "color": "var(--chart-warn)"},
                           {"to": 100, "color": "var(--chart-crit)"}]}


def _spark(values, color):
    vs = [round(float(v), 3) for v in values or () if finite(v)]
    return {"values": vs, "color": color} if len(vs) >= 2 else None


def _series_values(chart, name=None):
    """Values of one series of an aligned chart (first series when `name` is None); [] when absent."""
    for s in (chart or {}).get("series") or []:
        if name is None or s.get("name") == name:
            return s.get("values") or []
    return []


def _sum_series(chart):
    """Per-point sum of every series of an aligned chart (None where no series has a value)."""
    series = [s.get("values") or [] for s in (chart or {}).get("series") or []]
    if not series:
        return []
    out = []
    for i in range(max(len(v) for v in series)):
        vs = [v[i] for v in series if i < len(v) and finite(v[i])]
        out.append(sum(vs) if vs else None)
    return out


def summary_strip(host, by_key, disk_root_values, range_key):
    """Gauge / sparkline payloads and the KPI texts of the summary strip, from data already fetched.

    The page renders these server-side and the Live poll (host_metrics_json) sends the same structure, so the
    strip never shows a value the server would not have rendered. Everything here is text or numbers."""
    charts = host.get("charts") or {}
    cores = host.get("cores")
    gauges = {
        "cpu": _gauge("cpu", "CPU", host.get("cpu"), _series_values(charts.get("cpu")), range_key,
                      sub=f"{cores:.0f} cores" if cores else "cores unknown"),
        "memory": _gauge("memory", "Used RAM", host.get("memory"), _series_values(charts.get("memory")), range_key,
                         sub=f"of {human_bytes(host.get('mem_total'))}"),
        "disk": _gauge("disk", "Root disk", host.get("disk"), disk_root_values, range_key, sub="used on /"),
    }
    disk_bw = (by_key.get("disk_bw") or {}).get("data")
    sparks = {
        "load": _spark(_series_values(charts.get("load"), "1m"), pal.CORAL),
        "net": _spark(_series_values(charts.get("net"), "Receive"), pal.TEAL),
        "diskio": _spark(_sum_series(disk_bw), pal.BLUE),
    }

    def last_sum(chart_key, suffix):
        vals = [row["raw"]["last"] for row in (by_key.get(chart_key) or {}).get("legend", []) if row["name"].endswith(suffix)]
        return sum(vals) if vals else None
    disk_read, disk_write = last_sum("disk_bw", " read"), last_sum("disk_bw", " write")
    kpis = {
        "cpu": gauges["cpu"]["text"], "cpu_sub": gauges["cpu"]["sub"],
        "memory": gauges["memory"]["text"], "memory_sub": gauges["memory"]["sub"],
        "disk": gauges["disk"]["text"], "disk_sub": gauges["disk"]["sub"],
        "load1": human_number(host.get("load1")),
        "load_sub": f"{human_number(host.get('load5'))} · {human_number(host.get('load15'))} (5m · 15m)",
        "net": human_rate(host.get("rx")), "net_sub": f"in · out {human_rate(host.get('tx'))}",
        "diskio": human_rate(disk_read), "diskio_sub": f"read · write {human_rate(disk_write)}",
        "uptime": human_duration(host.get("uptime")), "uptime_sub": "scrape down" if host.get("up") == 0 else "scrape up",
    }
    tones = {"cpu": gauges["cpu"]["tone"], "memory": gauges["memory"]["tone"], "disk": gauges["disk"]["tone"],
             "load1": grade("load", host.get("load_per_core")), "uptime_sub": "crit" if host.get("up") == 0 else "ok"}
    return {"gauges": gauges, "sparks": sparks, "kpis": kpis, "tones": tones,
            "disk_read": disk_read, "disk_write": disk_write}


def live_payload(host, full):
    """JSON for the Live poll: every chart payload keyed by its DOM id, plus the summary strip and per-core table."""
    charts = {c["dom_id"]: c["data"] for s in full["sections"] for c in s["charts"] if c["has"]}
    legends = {c["dom_id"]: [{k: r[k] for k in ("name", "last", "mean", "min", "max")} for r in c["legend"]]
               for s in full["sections"] for c in s["charts"] if c["has"]}
    summary = full["summary"]
    for k, dom in GAUGE_DOM.items():
        charts[dom] = summary["gauges"][k]
    for k, dom in SPARK_DOM.items():
        if summary["sparks"].get(k):
            charts[dom] = summary["sparks"][k]
    health = host.get("health", "unknown")
    return {"generated": timezone.now().isoformat(timespec="seconds"), "instance": full["instance"],
            "range": full["range"], "health": {"key": health, "label": HEALTH_LABEL.get(health, "Unknown"),
                                               "tone": HEALTH_TONE.get(health, "mute")},
            "summary": summary["kpis"], "tones": summary["tones"], "charts": charts, "legends": legends,
            "cores": full["cores"]}


def mount_table(filesystems, inst):
    files = _keyed(inst.get("fs_files"), "mountpoint", "device")
    free = _keyed(inst.get("fs_files_free"), "mountpoint", "device")
    err = _keyed(inst.get("fs_err"), "mountpoint", "device")
    out = []
    for f in filesystems or ():
        k = (f["mountpoint"], f["device"])
        total, nfree = (files.get(k) or {}).get("value"), (free.get(k) or {}).get("value")
        inode_pct = (100.0 * (1 - nfree / total)) if finite(total) and total > 0 and finite(nfree) else None
        e = (err.get(k) or {}).get("value")
        out.append({**f, "inode_pct": inode_pct, "inodes": total,
                    "error": (e is not None and finite(e) and e > 0), "error_known": finite(e)})
    return out


def iface_table(inst):
    up, carrier = _keyed(inst.get("net_up"), "device"), _keyed(inst.get("net_carrier"), "device")
    speed, mtu, info = _keyed(inst.get("net_speed"), "device"), _keyed(inst.get("net_mtu"), "device"), _keyed(inst.get("net_info"), "device")
    devs = sorted(set(up) | set(carrier) | set(speed) | set(mtu) | set(info))
    out = []
    for (d,) in devs:
        k = (d,)
        sp = (speed.get(k) or {}).get("value")
        meta = (info.get(k) or {}).get("metric") or {}
        upv, cv = (up.get(k) or {}).get("value"), (carrier.get(k) or {}).get("value")
        out.append({"device": d, "up": None if not finite(upv) else upv > 0, "carrier": None if not finite(cv) else cv > 0,
                    "speed": sp if finite(sp) and sp > 0 else None, "mtu": (mtu.get(k) or {}).get("value"),
                    "operstate": meta.get("operstate", ""), "address": meta.get("address", ""), "duplex": meta.get("duplex", "")})
    return out


def container_table(inst):
    """cAdvisor containers scraped under this instance label; None when the metric is absent."""
    cpu_rows = inst.get("cadv_cpu") or []
    mem_rows = inst.get("cadv_mem") or []
    if not cpu_rows and not mem_rows:
        return None
    mem = _keyed(mem_rows, "name", "image")
    out = []
    seen = set()
    for r in cpu_rows:
        mt = r.get("metric") or {}
        k = (mt.get("name", ""), mt.get("image", ""))
        seen.add(k)
        out.append({"name": k[0], "image": k[1], "cpu": r.get("value"), "memory": (mem.get(k) or {}).get("value")})
    for k, r in mem.items():
        if k not in seen:
            out.append({"name": k[0], "image": k[1], "cpu": None, "memory": r.get("value")})
    return sorted(out, key=lambda c: -(c["cpu"] if finite(c["cpu"]) else -1))[:200]


def _utc_offset(seconds):
    if not finite(seconds):
        return ""
    sign = "-" if seconds < 0 else "+"
    h, m = divmod(int(abs(seconds)) // 60, 60)
    return f"UTC{sign}{h:02d}:{m:02d}"


def system_info(host, inst):
    dmi, build = _first_metric(inst.get("dmi")), _first_metric(inst.get("build"))
    tz_rows = inst.get("tz") or []
    tz_off = _first_value(tz_rows)
    return {
        "nodename": host.get("nodename", ""), "os": host.get("os", ""), "kernel": host.get("kernel", ""),
        "arch": host.get("arch", ""), "cores": host.get("cores"), "mem_total": host.get("mem_total"),
        "vendor": dmi.get("system_vendor", ""), "product": " ".join(x for x in (dmi.get("product_name", ""), dmi.get("product_version", "")) if x),
        "bios": " ".join(x for x in (dmi.get("bios_vendor", ""), dmi.get("bios_version", "")) if x),
        "timezone": _first_metric(tz_rows).get("time_zone", ""), "tz_offset": tz_off, "tz_text": _utc_offset(tz_off),
        "boot_time": from_epoch(_first_value(inst.get("boot"))),
        "exporter_version": build.get("version", ""), "go_version": build.get("goversion", ""),
    }


# ----------------------------------------------------------------------------- fetch
def fetch_host_full(host, range_key="1h", use_cache=True):
    """Every section of the full metrics page for a host dict from hosts.fetch_host(). Raises PrometheusError."""
    instance = host["instance"]
    if range_key not in RANGES:
        range_key = "1h"
    # v2: this payload gained "summary" and the per-core rows gained grade/text. live_payload() reads full["summary"]
    # directly, so a v1 entry left in a shared cache (Redis) by the previous release would 500 the Live poll.
    key = "telemetry:hostfull:v2:" + hashlib.sha1(f"{instance}|{range_key}".encode()).hexdigest()
    if use_cache:
        hit = cache.get(key)
        if hit is not None:
            return hit
    seconds = RANGES[range_key][1]
    step, bar_step = step_for(seconds), step_for(seconds, STACKED_POINTS)
    specs = sections_spec(instance)
    jobs = {}
    for sec in specs:
        for c in sec["charts"]:
            for gi, g in enumerate(c["groups"]):
                jobs[(c["key"], gi)] = ("range", g["q"], bar_step if c["kind"] == "bars" else step)
    for name, q in gauge_queries(instance).items():
        jobs[("gauge", name)] = ("range", q, step)
    for name, q in instant_queries(instance).items():
        jobs[name] = ("instant", q, None)

    def run(job):
        kind, q, st = job
        return promapi.query(q) if kind == "instant" else promapi.query_range(q, seconds, st)
    res = _run_parallel(jobs, run)
    inst = {k: v for k, v in res.items() if isinstance(k, str)}
    sections = []
    for sec in specs:
        charts = [build_chart(c, res, seconds) for c in sec["charts"]]
        sections.append({**sec, "charts": charts, "has_any": any(c["has"] for c in charts),
                         "missing": [c for c in charts if not c["has"]]})
    by_key = {c["key"]: c for s in sections for c in s["charts"]}
    disk_rows = res.get(("gauge", "disk_root")) or []
    disk_root = [v for _t, v in (disk_rows[0].get("values") or [])] if disk_rows else []
    summary = summary_strip(host, by_key, disk_root, range_key)
    out = {
        "instance": instance, "range": range_key, "seconds": seconds, "step": step, "bar_step": bar_step,
        "sections": sections, "summary": summary,
        "disk_read": summary["disk_read"], "disk_write": summary["disk_write"],
        "cores": core_table(inst.get("cores")),
        "mounts": mount_table(host.get("filesystems"), inst),
        "ifaces": iface_table(inst),
        "containers": container_table(inst),
        "system": system_info(host, inst),
        "query_count": len(jobs),
    }
    cache.set(key, out, CACHE_SECONDS)
    return out


# ----------------------------------------------------------------------------- compute module join (optional)
def placed_vms(instance, user):
    """VMs the compute module places on the device whose prometheus_instance is this host, site-scoped for
    `user`. None when the compute module is absent (or its tables are not migrated), [] when nothing is placed."""
    from django.apps import apps
    if not apps.is_installed("compute"):
        return None
    try:
        VMP = apps.get_model("compute", "VMPlacement")
    except LookupError:
        return None
    try:
        qs = (VMP.objects.filter(tenant_key=settings.LINKEDEYE_TENANT, host__prometheus_instance=instance)
              .select_related("vm", "host", "cluster").order_by("vm__name"))
        qs = scope_by_site(qs, user, field="vm__site")
        out = []
        for p in qs[:200]:
            try:
                url = p.vm.get_absolute_url()
            except Exception:
                url = ""
            out.append({"name": p.vm.name, "url": url, "site": p.vm.site, "status": p.vm.status,
                        "power": p.get_power_state_display(), "power_state": p.power_state,
                        "vcpus": p.vcpus, "memory_gb": p.memory_gb, "disk_gb": p.disk_gb, "os": p.os,
                        "cluster": p.cluster.name if p.cluster is not None else "",
                        "cluster_url": p.cluster.get_absolute_url() if p.cluster is not None else ""})
        return out
    except Exception as e:  # compute tables missing (not migrated) must not break the page
        log.warning("compute placement lookup failed: %s", e)
        return None
