"""Guest metrics for virtual_machine detail pages, read live from node_exporter series in Prometheus.

Nothing here invents numbers: without Prometheus, an instance label or node_exporter series the page shows
a "not connected" state that names what to set."""
from __future__ import annotations

from datetime import datetime, timezone as dt_tz

from django.utils import timezone

from core import prometheus

from .metrics import align_series, promql_string, regex_alternation

# Tags like cluster:Compute-01, host:esxi-01, datastore:DS01, vcpu:4, memory_gb:16, vcenter:vcsa.example.com
META_KEYS = ("cluster", "host", "datastore", "vcpu", "memory_gb", "tools", "created", "ha", "os", "vcenter")

VM_ACTIONS = [
    ("power-on", "Power on", "play"),
    ("power-off", "Power off", "x-circle"),
    ("restart", "Restart", "refresh"),
    ("suspend", "Suspend", "pause"),
    ("reset", "Reset", "zap"),
    ("migrate", "Migrate", "share"),
    ("console", "Open console", "terminal"),
    ("snapshot", "Take snapshot", "file"),
    ("clone", "Clone VM", "layers"),
    ("edit", "Edit settings", "settings"),
    ("move", "Move to host", "server"),
    ("logs", "Download logs", "download"),
]


def parse_vm_meta(device):
    """Pull key:value pairs from device.tags; leftover tags stay in `extra`."""
    meta = {k: "" for k in META_KEYS}
    extra = []
    for tag in device.tag_list:
        if ":" in tag:
            key, _, val = tag.partition(":")
            key = key.strip().lower()
            val = val.strip()
            if key in meta and val:
                meta[key] = val
                continue
        extra.append(tag)
    if not meta["os"] and device.firmware:
        meta["os"] = device.firmware
    return meta, extra


def guest_power_label(device):
    """Map reachability status to a VM-style power word."""
    return {"up": "Running", "down": "Stopped", "unknown": "Unknown"}.get(device.status, device.get_status_display())


def _instant(promql):
    rows = prometheus.query(promql)
    if not rows or rows[0]["value"] is None:
        return None
    return rows[0]["value"]


def _chart_from_series(series_defs, unit, seconds, step=None):
    """Build the standard line-chart dict from [(style, [(ts, val), ...]), ...] (series from separate range
    queries are aligned onto one step grid first, see metrics.align_series)."""
    aligned = align_series([vals for _style, vals in series_defs], step)
    series_defs = [(style, vals) for (style, _old), vals in zip(series_defs, aligned)]
    stamps = sorted({t for _style, vals in series_defs for t, _v in vals})
    if not stamps:
        return None
    fmt = "%H:%M" if seconds <= 86400 else "%d %b"
    labels = [timezone.localtime(datetime.fromtimestamp(t, tz=dt_tz.utc)).strftime(fmt) for t in stamps]
    tips = [timezone.localtime(datetime.fromtimestamp(t, tz=dt_tz.utc)).strftime("%d %b %H:%M") for t in stamps]
    out_series = []
    for style, vals in series_defs:
        lookup = dict(vals)
        out_series.append({
            **style,
            "values": [None if lookup.get(t) is None else round(lookup[t], 2) for t in stamps],
        })
    return {
        "labels": labels,
        "tipLabels": tips,
        "unit": unit,
        "xLabelEvery": max(1, len(stamps) // 6),
        "series": out_series,
    }


def _range_values(promql, seconds, step):
    rows = prometheus.query_range(promql, seconds=seconds, step=step)
    return rows[0]["values"] if rows else []


def batch_guest_instants(devices):
    """Batch instant guest metrics for a page of devices (one PromQL each for CPU/mem/disk/net).

    Maps device pk -> {"cpu", "memory", "disk", "network_mbps"} with None for missing series.
    """
    out = {d.pk: {"cpu": None, "memory": None, "disk": None, "network_mbps": None} for d in devices}
    if not devices or not prometheus.configured():
        return out
    inst_to_pks = {}
    instances = []
    for d in devices:
        if not d.prometheus_instance:
            continue
        inst = d.prometheus_instance.strip()
        if not inst:
            continue
        inst_to_pks.setdefault(inst, []).append(d.pk)
        if inst not in instances:
            instances.append(inst)
    if not instances:
        return out
    regex = regex_alternation(instances)

    def _by_instance(rows):
        m = {}
        for r in rows:
            inst = (r.get("metric") or {}).get("instance", "")
            v = r.get("value")
            if inst:
                m[inst] = v
        return m

    try:
        cpu_rows = prometheus.query(
            f'100 * (1 - avg by (instance) (rate(node_cpu_seconds_total{{mode="idle",instance=~"{regex}"}}[5m])))'
        )
        mem_rows = prometheus.query(
            f"100 * (1 - avg by (instance) (node_memory_MemAvailable_bytes{{instance=~\"{regex}\"}}"
            f" / node_memory_MemTotal_bytes{{instance=~\"{regex}\"}}))"
        )
        disk_rows = prometheus.query(
            f'100 * (1 - (avg by (instance) (node_filesystem_avail_bytes{{fstype!~"tmpfs|overlay|squashfs",'
            f'mountpoint="/",instance=~"{regex}"}})'
            f' / avg by (instance) (node_filesystem_size_bytes{{fstype!~"tmpfs|overlay|squashfs",'
            f'mountpoint="/",instance=~"{regex}"}})))'
        )
        net_rows = prometheus.query(
            f'sum by (instance) (rate(node_network_receive_bytes_total{{device!~"lo|veth.*|docker.*|br.*",'
            f'instance=~"{regex}"}}[5m])'
            f' + rate(node_network_transmit_bytes_total{{device!~"lo|veth.*|docker.*|br.*",'
            f'instance=~"{regex}"}}[5m])) * 8 / 1e6'
        )
    except prometheus.PrometheusError:
        return out

    cpu_m, mem_m, disk_m, net_m = (
        _by_instance(cpu_rows), _by_instance(mem_rows), _by_instance(disk_rows), _by_instance(net_rows)
    )
    for inst, pks in inst_to_pks.items():
        row = {
            "cpu": cpu_m.get(inst),
            "memory": mem_m.get(inst),
            "disk": disk_m.get(inst),
            "network_mbps": net_m.get(inst),
        }
        for pk in pks:
            out[pk] = row
    return out


def batch_guest_extras(devices):
    """Inbound and outbound throughput and temperature for a page of devices, three batched queries in all.

    Maps device pk -> {"rx_mbps", "tx_mbps", "temp"} with None for missing series (or when Prometheus is unreachable)."""
    out = {d.pk: {"rx_mbps": None, "tx_mbps": None, "temp": None} for d in devices}
    inst_to_pks = {}
    for d in devices:
        inst = (d.prometheus_instance or "").strip()
        if inst:
            inst_to_pks.setdefault(inst, []).append(d.pk)
    if not inst_to_pks or not prometheus.configured():
        return out
    regex = regex_alternation(list(inst_to_pks))
    nic = 'device!~"lo|veth.*|docker.*|br.*"'
    queries = {
        "rx_mbps": f'sum by (instance) (rate(node_network_receive_bytes_total{{{nic},instance=~"{regex}"}}[5m])) * 8 / 1e6',
        "tx_mbps": f'sum by (instance) (rate(node_network_transmit_bytes_total{{{nic},instance=~"{regex}"}}[5m])) * 8 / 1e6',
        "temp": (f'max by (instance) (node_hwmon_temp_celsius{{instance=~"{regex}"}})'
                 f' or max by (instance) (node_thermal_zone_temp{{instance=~"{regex}"}})'),
    }
    for key, promql in queries.items():
        try:
            rows = prometheus.query(promql)
        except prometheus.PrometheusError:
            continue
        for r in rows:
            inst, v = (r.get("metric") or {}).get("instance", ""), r.get("value")
            if v is None or v != v:
                continue
            for pk in inst_to_pks.get(inst, []):
                out[pk][key] = v
    return out


def _perf_step(seconds):
    return max(15, int(seconds / 120))


PERF_RANGE_SECONDS = {
    "15m": 900,
    "1h": 3600,
    "6h": 21600,
    "24h": 86400,
    "7d": 604800,
}
# node_exporter boot time -> seconds since boot (max over duplicate scrape jobs of one instance)
UPTIME_PROMQL = "max(node_time_seconds{{{sel}}} - node_boot_time_seconds{{{sel}}})"


def guest_uptimes(instances):
    """{instance label: seconds since boot} for the instances node_exporter reports.
    Raises prometheus.PrometheusError (the caller decides how to report it)."""
    if not instances:
        return {}
    sel = f'instance=~"{regex_alternation(instances)}"'
    rows = prometheus.query(f"max by (instance) (node_time_seconds{{{sel}}} - node_boot_time_seconds{{{sel}}})")
    out = {}
    for r in rows:
        inst = (r.get("metric") or {}).get("instance")
        if inst and r.get("value") is not None:
            out[inst] = max(0, int(r["value"]))
    return out


def fetch_guest_performance(device, seconds=86400):
    """Time-series charts for VM Performance tab (node_exporter + PromQL)."""
    if not prometheus.configured() or not device.prometheus_instance:
        return None
    sel = f'instance="{promql_string(device.prometheus_instance)}"'
    step = _perf_step(seconds)
    disk_re = 'device=~"^(sd|vd|nvme|xvd|hd).+"'
    try:
        cpu = _range_values(
            f'100 * (1 - avg(rate(node_cpu_seconds_total{{mode="idle",{sel}}}[5m])))', seconds, step
        )
        mem = _range_values(
            f"100 * (1 - node_memory_MemAvailable_bytes{{{sel}}} / node_memory_MemTotal_bytes{{{sel}}})", seconds, step
        )
        disk_used = _range_values(
            f'100 * (1 - node_filesystem_avail_bytes{{fstype!~"tmpfs|overlay|squashfs",mountpoint="/",{sel}}}'
            f' / node_filesystem_size_bytes{{fstype!~"tmpfs|overlay|squashfs",mountpoint="/",{sel}}})',
            seconds,
            step,
        )
        net = _range_values(
            f'sum(rate(node_network_receive_bytes_total{{device!~"lo|veth.*|docker.*|br.*",{sel}}}[5m])'
            f' + rate(node_network_transmit_bytes_total{{device!~"lo|veth.*|docker.*|br.*",{sel}}}[5m]))'
            f' * 8 / 1e6',
            seconds,
            step,
        )
        disk_io = _range_values(
            f"sum(rate(node_disk_read_bytes_total{{{disk_re},{sel}}}[5m])"
            f" + rate(node_disk_written_bytes_total{{{disk_re},{sel}}}[5m])) / 1e6",
            seconds,
            step,
        )
        load1 = _range_values(f"node_load1{{{sel}}}", seconds, step)
    except prometheus.PrometheusError:
        return None

    cpu_c = _chart_from_series(
        [({"name": "CPU", "color": "var(--chart-6)", "area": True, "areaOpacity": 0.12, "width": 1.4}, cpu)],
        "%",
        seconds,
    )
    mem_c = _chart_from_series(
        [({"name": "Memory", "color": "var(--brand)", "area": True, "areaOpacity": 0.12, "width": 1.4}, mem)],
        "%",
        seconds,
    )
    disk_c = _chart_from_series(
        [({"name": "Root disk used", "color": "var(--teal)", "area": True, "areaOpacity": 0.12, "width": 1.4}, disk_used)],
        "%",
        seconds,
    )
    net_c = _chart_from_series(
        [({"name": "Network", "color": "var(--accent-blue)", "area": True, "areaOpacity": 0.12, "width": 1.4}, net)],
        "Mb/s",
        seconds,
    )
    dio_c = _chart_from_series(
        [({"name": "Read + write", "color": "var(--chart-3)", "area": True, "areaOpacity": 0.12, "width": 1.4}, disk_io)],
        "MB/s",
        seconds,
    )
    load_c = _chart_from_series(
        [({"name": "Load (1m)", "color": "var(--chart-7)", "width": 1.4}, load1)],
        "load",
        seconds,
    )
    charts = [cpu_c, mem_c, disk_c, net_c, dio_c, load_c]
    if not any(charts):
        return None
    return {
        "seconds": seconds,
        "cpu": cpu_c,
        "memory": mem_c,
        "disk_used": disk_c,
        "network": net_c,
        "disk_io": dio_c,
        "load1": load_c,
    }


def fetch_guest_metrics(device):
    """Live node_exporter metrics for `device.prometheus_instance`, or None on failure/empty."""
    if not prometheus.configured() or not device.prometheus_instance:
        return None
    sel = f'instance="{promql_string(device.prometheus_instance)}"'
    try:
        cpu = _instant(
            f'100 * (1 - avg(rate(node_cpu_seconds_total{{mode="idle",{sel}}}[5m])))'
        )
        mem_avail = _instant(f"node_memory_MemAvailable_bytes{{{sel}}}")
        mem_total = _instant(f"node_memory_MemTotal_bytes{{{sel}}}")
        disk = _instant(
            f'100 * (1 - node_filesystem_avail_bytes{{fstype!~"tmpfs|overlay|squashfs",'
            f'mountpoint="/",{sel}}} / node_filesystem_size_bytes{{fstype!~"tmpfs|overlay|squashfs",'
            f'mountpoint="/",{sel}}})'
        )
        net = _instant(
            f'sum(rate(node_network_receive_bytes_total{{device!~"lo|veth.*|docker.*|br.*",{sel}}}[5m])'
            f' + rate(node_network_transmit_bytes_total{{device!~"lo|veth.*|docker.*|br.*",{sel}}}[5m]))'
            f' * 8 / 1e6'
        )
        uptime = _instant(UPTIME_PROMQL.format(sel=sel))
    except prometheus.PrometheusError:
        return None

    mem_pct = None
    if mem_avail is not None and mem_total and mem_total > 0:
        mem_pct = 100.0 * (1.0 - (mem_avail / mem_total))

    if all(v is None for v in (cpu, mem_pct, disk, net, uptime)):
        return None

    step_1h, step_24h = 60, 600
    try:
        cpu_1h = _range_values(
            f'100 * (1 - avg(rate(node_cpu_seconds_total{{mode="idle",{sel}}}[5m])))', 3600, step_1h)
        mem_1h = _range_values(
            f'100 * (1 - node_memory_MemAvailable_bytes{{{sel}}} / node_memory_MemTotal_bytes{{{sel}}})',
            3600, step_1h)
        disk_1h = _range_values(
            f'100 * (1 - node_filesystem_avail_bytes{{fstype!~"tmpfs|overlay|squashfs",mountpoint="/",{sel}}}'
            f' / node_filesystem_size_bytes{{fstype!~"tmpfs|overlay|squashfs",mountpoint="/",{sel}}})',
            3600, step_1h)
        cpu_24h = _range_values(
            f'100 * (1 - avg(rate(node_cpu_seconds_total{{mode="idle",{sel}}}[5m])))', 86400, step_24h)
        mem_24h = _range_values(
            f'100 * (1 - node_memory_MemAvailable_bytes{{{sel}}} / node_memory_MemTotal_bytes{{{sel}}})',
            86400, step_24h)
        net_24h = _range_values(
            f'sum(rate(node_network_receive_bytes_total{{device!~"lo|veth.*|docker.*|br.*",{sel}}}[5m])'
            f' + rate(node_network_transmit_bytes_total{{device!~"lo|veth.*|docker.*|br.*",{sel}}}[5m]))'
            f' * 8 / 1e6',
            86400, step_24h)
    except prometheus.PrometheusError:
        cpu_1h = mem_1h = disk_1h = cpu_24h = mem_24h = net_24h = []

    util_1h = _chart_from_series([
        ({"name": "CPU", "color": "var(--chart-6)", "width": 1.6}, cpu_1h),
        ({"name": "Memory", "color": "var(--brand)", "width": 1.6}, mem_1h),
        ({"name": "Disk", "color": "var(--teal)", "width": 1.6}, disk_1h),
    ], "%", 3600, step_1h)
    cpu_chart = _chart_from_series(
        [({"name": "CPU", "color": "var(--chart-6)", "area": True, "areaOpacity": 0.12, "width": 1.4}, cpu_24h)],
        "%", 86400)
    mem_chart = _chart_from_series(
        [({"name": "Memory", "color": "var(--brand)", "area": True, "areaOpacity": 0.12, "width": 1.4}, mem_24h)],
        "%", 86400)
    net_chart = _chart_from_series(
        [({"name": "Network", "color": "var(--teal)", "area": True, "areaOpacity": 0.12, "width": 1.4}, net_24h)],
        "Mb/s", 86400)

    return {
        "source": f"Prometheus · node_exporter instance {device.prometheus_instance}",
        "uptime": None if uptime is None else max(0, int(uptime)),
        "cpu": cpu,
        "memory": mem_pct,
        "disk": disk,
        "network_mbps": net,
        "cpu_delta": None,
        "memory_delta": None,
        "disk_delta": None,
        "network_delta": None,
        "ha": "",
        "util_1h": util_1h,
        "cpu_24h": cpu_chart,
        "memory_24h": mem_chart,
        "network_24h": net_chart,
    }


def vm_info_rows(device, meta):
    """Rows for the VM Information table: device fields plus key:value tags (see META_KEYS)."""
    created = meta.get("created") or ""
    if not created and device.created_at:
        created = timezone.localtime(device.created_at).strftime("%d %b %Y")
    mem = meta.get("memory_gb") or ""
    if mem and not mem.upper().endswith("GB"):
        mem = f"{mem} GB"
    return [
        ("Hostname", device.name),
        ("Management IP", device.mgmt_ip),
        ("Prometheus instance", device.prometheus_instance or "–"),
        ("Vendor / platform", device.vendor or "–"),
        ("Guest OS", meta.get("os") or device.firmware or device.model or "–"),
        ("Cluster", meta.get("cluster") or "–"),
        ("Host", meta.get("host") or "–"),
        ("Datastore", meta.get("datastore") or "–"),
        ("vCPU", meta.get("vcpu") or "–"),
        ("Memory", mem or "–"),
        ("VMware Tools", meta.get("tools") or "–"),
        ("Created", created or "–"),
        ("vCenter", meta.get("vcenter") or "–"),
        ("Environment", device.get_environment_display() or "–"),
        ("Role", device.role or "–"),
    ]


STUB_COPY = {
    "storage": ("Storage & Disks", "Datastore and virtual disk inventory is not collected by this local monitor yet."),
    "snapshots": ("Snapshots", "Snapshot list and create/delete actions require a hypervisor connector (read-only in release 1)."),
    "guest": ("Guest OS", "Guest identity beyond firmware/OS and tags is not collected yet."),
    "processes": ("Processes", "Guest process listing is not connected."),
    "services": ("Services", "Guest service status is not connected."),
    "logs": ("Logs", "Guest and hypervisor log streaming is not connected."),
    "topology": ("Topology", "Open CMDB topology for related configuration items when a link is available."),
}
