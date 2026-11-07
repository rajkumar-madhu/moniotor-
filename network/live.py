"""Live snapshot of one device for the detail page (network:live, polled by network/live.js).

Values come from the database (polls and interfaces the worker stores every minute) or from Prometheus, and are
formatted with the same filters the templates use, so the page swaps text in without re-deriving anything. Chart
payloads are keyed by the json_script ids the templates render, so charts.js redraws them in place."""
from django.utils import timezone

from core import prometheus
from core.templatetags.le import ago, short_dt, tone

from . import metrics, services, vm_metrics
from .models import DeviceType
from .templatetags.network_tags import avail_tone, latency, mbps, per_s, percent

LIVE_INTERVAL = 30       # seconds between polls; the worker refreshes polls and interface rates every 60 s
FRONT_PANEL_MAX = 96     # ports drawn on the front panel; the Interfaces tab still lists every interface


def panel_ports(interfaces):
    """The physical ports drawn on the front panel, in if_index order."""
    return [i for i in interfaces if i.is_physical][:FRONT_PANEL_MAX]


def port_data(ports):
    """What the port drawer shows, pre-formatted like the tables (device-reported names are rendered as text)."""
    from .templatetags.network_tags import speed
    return [{"n": n, "ifIndex": p.if_index, "name": p.name, "alias": p.alias, "type": p.if_type or "–",
             "tone": p.port_tone, "toneLabel": p.port_tone_label,
             "admin": p.get_admin_status_display(), "oper": p.get_oper_status_display(), "speed": speed(p.speed_mbps),
             "in": mbps(p.in_mbps), "out": mbps(p.out_mbps),
             "util": "–" if p.utilization is None else f"{p.utilization:.0f}%",
             "errors": per_s(p.errors_per_s), "discards": per_s(p.discards_per_s),
             "flaps": "–" if p.flaps_24h is None else str(p.flaps_24h)}
            for n, p in enumerate(ports, 1)]


def _pct(v):
    return "–" if v is None else f"{v:.1f}%"


def _avail_tone(v):
    return avail_tone(v).removeprefix("t-")


def apply_guest_uptime(device, guest):
    """A VM scraped only by node_exporter gets its uptime from the guest metrics (in memory, for display)."""
    if guest and device.uptime_seconds is None and guest.get("uptime") is not None:
        device.uptime_seconds = guest["uptime"]


def device_snapshot(device, range_key=metrics.DEFAULT_RANGE, port_index=None):
    """JSON-ready dict: formatted `values`, `tones` (status tone per key), `charts` (json_script id -> chart or
    None), `ports` (front panel), `notes` (why a chart is missing) and the latest alert."""
    is_vm = device.device_type == DeviceType.VIRTUAL_MACHINE
    seconds = metrics.RANGES[metrics.range_key(range_key)]
    interfaces = list(device.interfaces.all())
    summary = services.interface_summary(interfaces)
    avail24 = services.availability([device.pk], 24).get(device.pk)
    avail7 = services.availability([device.pk], 24 * 7).get(device.pk)
    values = {
        "status": vm_metrics.guest_power_label(device) if is_vm else device.get_status_display(),
        "last_seen": ago(device.last_seen), "last_seen_at": short_dt(device.last_seen),
        "last_check": ago(device.last_checked_at), "last_check_at": short_dt(device.last_checked_at),
        "latency": latency(device.last_latency_ms), "avg_latency": latency(services.avg_latency(device)),
        "avail24": percent(avail24, 2), "avail7": percent(avail7, 2),
        "metrics_at": ago(device.metrics_updated_at) if device.metrics_updated_at else "",
        "metrics_at_dt": short_dt(device.metrics_updated_at) if device.metrics_updated_at else "",
    }
    tones = {"status": tone(device.status), "avail24": _avail_tone(avail24)}
    charts = {"nw-avail": services.availability_chart(device), "nw-latency": services.latency_chart(device),
              "nw-spark": services.latency_spark(device)}
    notes, ports = [], []

    if is_vm:
        guest = vm_metrics.fetch_guest_metrics(device)
        apply_guest_uptime(device, guest)
        g = guest or {}
        values.update({"cpu": _pct(g.get("cpu")), "memory": _pct(g.get("memory")), "disk": _pct(g.get("disk")),
                       "network": "–" if g.get("network_mbps") is None else f"{g['network_mbps']:.1f} Mb/s"})
        for key, field in (("cpu", "cpu"), ("memory", "memory"), ("disk", "disk"), ("network", "network_mbps")):
            tones[key] = "mute" if g.get(field) is None else ""
        charts.update({"nw-vm-util": g.get("util_1h"), "nw-vm-cpu": g.get("cpu_24h"),
                       "nw-vm-mem": g.get("memory_24h"), "nw-vm-net": g.get("network_24h")})
        if guest is None and prometheus.configured() and device.prometheus_instance:
            notes.append(f'No node_exporter series in Prometheus for instance "{device.prometheus_instance}".')
    else:
        port = None
        if port_index is not None:
            port = next((i for i in interfaces if i.if_index == port_index), None)
        idx = port.if_index if port else None
        traffic, traffic_msg = metrics.traffic_range(device, seconds, idx)
        errors, errors_msg = metrics.errors_range(device, seconds, idx)
        # chart legends follow the ?port= filter; the KPI strip always shows the whole device
        if port:
            leg = (port.in_mbps, port.out_mbps, port.errors_per_s, port.discards_per_s)
        else:
            leg = (summary["total_in"], summary["total_out"], summary["total_errors"], summary["total_discards"])
        values.update({
            "ports_up": str(summary["ports_up"]), "ports_down": str(summary["ports_down"]),
            "ports_unused": str(summary["ports_unused"]),
            "traffic_in": mbps(summary["total_in"]), "traffic_out": mbps(summary["total_out"]),
            "errors": per_s(summary["total_errors"]), "discards": per_s(summary["total_discards"]),
            "legend_in": mbps(leg[0]), "legend_out": mbps(leg[1]),
            "legend_errors": per_s(leg[2]), "legend_discards": per_s(leg[3]),
        })
        tones["errors"] = ("mute" if summary["total_errors"] is None else "warn" if summary["total_errors"] > 0 else "")
        tones["ports_down"] = "crit" if summary["ports_down"] else ""
        charts.update({"nw-traffic-strip": traffic, "nw-errors": errors, "nw-port-bars": metrics.port_bars(interfaces)})
        notes += [m for m in (traffic_msg, errors_msg) if m and m != metrics.NOT_CONFIGURED]
        ports = port_data(panel_ports(interfaces))

    values["uptime_short"] = device.uptime_short or "–"
    values["uptime_display"] = device.uptime_display or ""
    latest = services.events_for(device, 1)
    latest = latest[0] if latest else None
    return {
        "generated_at": timezone.now().isoformat(),
        "status": device.status,
        "values": values, "tones": tones, "charts": charts, "ports": ports, "notes": notes,
        "latest_event": {"id": latest.pk, "title": latest.title, "severity": latest.severity,
                         "ago": ago(latest.occurred_at)} if latest else None,
    }
