"""The per-port page of a network device: /network/devices/<pk>/interfaces/<ifIndex>/ (network:interface), its live JSON
(interface_live) and the CSV of the port's time series (interface_export).

Access is exactly the device detail's: the device must be in the user's sites (and the selected client), otherwise 404,
and the ifIndex must be one of the device's stored interfaces. A viewer can see everything here; nothing on these pages
changes data. Prometheus is asked through network.portmetrics only (see its header for the call budget).
"""
import csv
import re
from datetime import datetime, timezone as dt_tz
from urllib.parse import urlencode

from django.apps import apps
from django.core.cache import cache
from django.db.models import Q
from django.db.models.functions import Lower
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django.views.decorators.http import require_GET

from core.access import require, visible_sites
from core.templatetags.le import ago, short_dt

from . import metrics, portmetrics, services
from .live import LIVE_INTERVAL
from .models import DeviceType
from .templatetags.network_tags import mbps, per_s, speed
from .views import _as_device, _cell, _device, _int_param, _perms, _range_links

STALE_AFTER = 300          # seconds; stored port figures older than this are called stale on the page
MAX_IF_INDEX = 2 ** 31
LIVE_PER_MINUTE = 30       # live JSON requests per user and minute (a page polls twice a minute)
CSV_PER_MINUTE = 12
EVENT_LIMIT = 10
COMPARE_LIMIT = 5
FINGERPRINT = re.compile(r"network:(\d+):if(\d+)")

PANELS = (  # (key, title, hint, kinds)
    ("traffic", "Traffic in / out", "5 min rate", "line,area,bars"),
    ("util", "Utilisation", "busiest direction, % of port speed", "line,area"),
    ("packets", "Packets", "unicast / multicast / broadcast, in + out", "line,area"),
    ("errors", "Errors and discards", "per second, per direction", "line,bars"),
    ("optics", "Optical power", "Tx / Rx, dBm", "line"),
)


def port_url(device_pk, if_index, **query):
    url = reverse("network:interface", args=[device_pk, if_index])
    return f"{url}?{urlencode(query)}" if query else url


def _throttled(request, scope, limit, window=60):
    """True when this user has used up `limit` requests of this kind in the current minute (fixed window, in the cache;
    a cache that fails never blocks the page)."""
    key = f"nwport:rl:{scope}:{request.user.pk}"
    try:
        if cache.add(key, 1, window):
            return False
        return cache.incr(key) > limit
    except Exception:  # noqa: BLE001
        return False


def _lookup(request, pk, if_index, json_errors=False):
    """(device, interface, all interfaces, early response). 404 for an invisible device, a virtual machine or an
    ifIndex the device does not have."""
    if pk >= MAX_IF_INDEX:     # beyond any row id: Django 4.2 raises OverflowError on SQLite instead of matching nothing
        if json_errors:
            return None, None, None, JsonResponse({"error": "Device not found, or outside the selected client or your sites."}, status=404)
        raise Http404("No such device.")
    if json_errors:
        device = services.devices_for(request.user, request).filter(pk=pk).first()
        early = None if device else JsonResponse({"error": "Device not found, or outside the selected client or your sites."}, status=404)
    else:
        device, early = _as_device(_device(request, pk))
    if early or device is None:
        return None, None, None, early
    interfaces = metrics.current_interfaces(device)
    iface = next((i for i in interfaces if i.if_index == if_index), None) if if_index < MAX_IF_INDEX else None
    if device.device_type == DeviceType.VIRTUAL_MACHINE or iface is None:
        if json_errors:
            return None, None, None, JsonResponse({"error": "No such interface on this device."}, status=404)
        raise Http404("No such interface on this device.")
    return device, iface, interfaces, None


# ------------------------------------------------------------------ figures
def _peak(chart, names):
    values = portmetrics.sum_values(chart, names) if len(names) > 1 else portmetrics.series_values(chart, names[0])
    nums = [v for v in values or [] if v is not None]
    if not nums:
        return None
    return max(nums) * (1000.0 if (chart or {}).get("unit") == "Gbps" else 1.0)


def kpi_cards(device, iface, day, sparks):
    """The six KPI tiles as dicts (key, label, value, sub, tone, spark?), from the stored port row and the 24 h charts."""
    traffic, errors = day.get("traffic"), day.get("errors")
    peaks = {"in": _peak(traffic, ["Inbound"]), "out": _peak(traffic, ["Outbound"]),
             "errors": _peak(errors, ["In errors", "Out errors"]), "discards": _peak(errors, ["In discards", "Out discards"])}

    def sub_rate(key, fmt):
        return "no 24 h data" if peaks[key] is None else f"24 h peak {fmt(peaks[key])}"
    util = iface.utilization
    cards = [
        {"key": "in", "label": "In rate", "value": mbps(iface.in_mbps), "sub": sub_rate("in", mbps),
         "tone": "mute" if iface.in_mbps is None else ""},
        {"key": "out", "label": "Out rate", "value": mbps(iface.out_mbps), "sub": sub_rate("out", mbps),
         "tone": "mute" if iface.out_mbps is None else ""},
        {"key": "util", "label": "Utilisation", "value": "–" if util is None else f"{util:.0f}%",
         "sub": f"of {speed(iface.speed_mbps)}" if iface.speed_mbps else "port speed not reported", "tone": iface.util_tone, "gauge": True},
        {"key": "errors", "label": "Errors / s", "value": per_s(iface.errors_per_s), "sub": sub_rate("errors", per_s),
         "tone": "mute" if iface.errors_per_s is None else "warn" if iface.errors_per_s > 0 else ""},
        {"key": "discards", "label": "Discards / s", "value": per_s(iface.discards_per_s), "sub": sub_rate("discards", per_s),
         "tone": "mute" if iface.discards_per_s is None else "warn" if iface.discards_per_s > 0 else ""},
        {"key": "flaps", "label": "Flaps (24 h)", "value": "–" if iface.flaps_24h is None else str(iface.flaps_24h),
         "sub": "oper state changes" if iface.flaps_24h is not None else "not reported",
         "tone": "mute" if iface.flaps_24h is None else "warn" if iface.flaps_24h else ""},
    ]
    for c in cards:
        c["spark"] = c["key"] in sparks
    return cards


def gauge_data(iface):
    return {"value": iface.utilization, "max": 100, "unit": "%", "label": "Utilisation",
            "thresholds": [{"to": 60, "color": "var(--chart-ok)"}, {"to": 85, "color": "var(--chart-warn)"},
                           {"color": "var(--chart-crit)"}]}


def state_values(device, iface, now=None):
    """Header facts as display strings: the live JSON patches these into the header and the information card."""
    now = now or timezone.now()
    age = (now - iface.updated_at).total_seconds() if iface.updated_at else None
    changed = iface.last_change_at
    return {
        "admin": iface.get_admin_status_display(), "oper": iface.get_oper_status_display(),
        "speed": speed(iface.speed_mbps) if iface.speed_mbps else "not reported",
        "last_change": f"{short_dt(changed)} ({ago(changed)})" if changed else "not reported",
        "last_change_short": ago(changed) if changed else "not reported",
        "updated": ago(iface.updated_at) if iface.updated_at else "never",
        "stale": bool(device.enabled and (age is None or age > STALE_AFTER)),
    }


def info_rows(iface, interfaces, extras, device):
    """The Interface information card: [{"k", "v" (text or None = not reported), "mono", "items", "links"}]."""
    by_index = {i.if_index: i for i in interfaces}
    live = "reason" not in extras
    rows = []

    def add(k, v=None, mono=False, items=None, links=None, key=""):
        rows.append({"k": k, "v": v or None, "mono": mono, "items": items or [], "links": links or [], "key": key})

    changed = iface.last_change_at
    add("ifIndex", str(iface.if_index), mono=True)
    add("Name (ifName)", iface.name)
    add("Description (ifAlias)", iface.alias)
    add("Type (ifType)", iface.if_type)
    add("MTU (ifMtu)", f"{iface.mtu:,} bytes" if iface.mtu else None)
    add("MAC (ifPhysAddress)", iface.mac, mono=True)
    add("Speed", speed(iface.speed_mbps) if iface.speed_mbps else None, key="state_speed")
    add("Duplex", {"full": "Full duplex", "half": "Half duplex"}.get(iface.duplex))
    add("Admin status", iface.get_admin_status_display(), key="state_admin")
    add("Oper status", iface.get_oper_status_display(), key="state_oper")
    add("Last change", f"{short_dt(changed)} ({ago(changed)})" if changed else None, key="state_last_change")
    vlan = extras.get("vlan") or {} if live else {}
    role = (f"Trunk, native VLAN {vlan['native']}" if vlan.get("native") else
            f"Access, VLAN {vlan['access']}" if vlan.get("access") else
            f"PVID {vlan['pvid']}" if vlan.get("pvid") else None)
    add("Port role / VLAN", role)
    add("IP addresses", None, mono=True, items=(extras.get("ips") or [])[:8] if live else [])
    lag = extras.get("lag") or {} if live else {}
    links = []
    if lag.get("parent"):
        parent = by_index.get(lag["parent"])
        links.append({"text": f"Member of {parent.name if parent else 'aggregate ' + str(lag['parent'])}",
                      "href": port_url(device.pk, lag["parent"]) if parent else ""})
    for member in (lag.get("members") or [])[:8]:
        m = by_index.get(member)
        links.append({"text": f"Member port {m.name}" if m else f"Member port {member}",
                      "href": port_url(device.pk, member) if m else ""})
    add("LAG membership", None, links=links)
    add("Spanning tree state", extras.get("stp") if live else None)
    return rows


# ------------------------------------------------------------------ neighbours, links, events, comparison
def _matches(found, n):
    system = (n.get("system") or "").lower()
    for d in found:
        if system and d._lname in (system, system.split(".")[0]):
            return d
    for d in found:
        if n.get("ip") and d.mgmt_ip.strip() == n["ip"]:
            return d
    return None


def neighbour_cards(request, neighbours):
    names, ips = set(), set()
    for n in neighbours:
        system = (n.get("system") or "").lower()
        if system:
            names |= {system, system.split(".")[0]}
        if n.get("ip"):
            ips.add(n["ip"])
    found = []
    if names or ips:
        qs = services.devices_for(request.user, request).annotate(_lname=Lower("name"))
        found = list(qs.filter(Q(_lname__in=names) | Q(mgmt_ip__in=ips))[:20])
    cards = []
    for n in neighbours:
        d = _matches(found, n)
        cards.append(dict(n, device=d, device_url=d.get_absolute_url() if d else ""))
    return cards


def _unique_by_name(interfaces):
    """{lower-case port name: Interface} for names only one port of the device carries. Cables are documented by port
    name, so a name two ports share cannot be told apart and belongs to neither (never guessed)."""
    seen, dup = {}, set()
    for i in interfaces:
        key = i.name.lower()
        if key in seen:
            dup.add(key)
        seen[key] = i
    return {k: v for k, v in seen.items() if k not in dup}


def physical_links(request, device, iface, can_operate, interfaces=()):
    """Documented cables (architecture app, optional) that end on this port. The far end is named only when the user may
    see that device; a link to a device on a site the user cannot see is shown without its far end."""
    if not apps.is_installed("architecture"):
        return []
    if interfaces and iface.name.lower() not in _unique_by_name(interfaces):
        return []
    from architecture.models import Link
    rows = list(Link.objects.filter(tenant_key=services.tenant())
                .filter(Q(a_device=device, a_port__iexact=iface.name) | Q(b_device=device, b_port__iexact=iface.name))
                .select_related("a_device", "b_device")[:5])
    others = {(r.b_device if r.a_device_id == device.pk else r.a_device) for r in rows}
    visible = set(services.devices_for(request.user, request).filter(pk__in=[d.pk for d in others]).values_list("pk", flat=True)) if others else set()
    out = []
    for r in rows:
        mine_a = r.a_device_id == device.pk
        far, far_port = (r.b_device, r.b_port) if mine_a else (r.a_device, r.a_port)
        shown = far.pk in visible
        try:
            edit = reverse("architecture:link_edit", args=[r.pk]) if can_operate else ""
        except NoReverseMatch:
            edit = ""
        out.append({"device": far if shown else None, "device_url": far.get_absolute_url() if shown else "",
                    "port": far_port if shown else "", "medium": r.get_medium_display(), "kind": r.kind,
                    "speed": speed(r.speed_mbps) if r.speed_mbps else "", "confirmed": r.confirmed,
                    "origin": r.get_origin_display(), "note": r.note if shown else "", "edit_url": edit})
    return out


def device_links(request, device, interfaces):
    """Rows for the Neighbours tab: every documented cable of the device, with its port linked to the port page."""
    if not apps.is_installed("architecture"):
        return []
    from architecture.models import Link
    rows = list(Link.objects.filter(tenant_key=services.tenant()).filter(Q(a_device=device) | Q(b_device=device))
                .select_related("a_device", "b_device")[:200])
    others = {(r.b_device if r.a_device_id == device.pk else r.a_device) for r in rows}
    visible = set(services.devices_for(request.user, request).filter(pk__in=[d.pk for d in others]).values_list("pk", flat=True)) if others else set()
    by_name = _unique_by_name(interfaces)
    out = []
    for r in rows:
        mine_a = r.a_device_id == device.pk
        port, far, far_port = (r.a_port, r.b_device, r.b_port) if mine_a else (r.b_port, r.a_device, r.a_port)
        iface = by_name.get(port.lower()) if port else None
        shown = far.pk in visible
        out.append({"port": port, "port_url": port_url(device.pk, iface.if_index) if iface else "",
                    "device": far if shown else None, "device_url": far.get_absolute_url() if shown else "",
                    "far_port": far_port if shown else "", "medium": r.get_medium_display(), "kind": r.kind,
                    "speed": speed(r.speed_mbps) if r.speed_mbps else "", "confirmed": r.confirmed})
    return sorted(out, key=lambda x: (x["port"] == "", x["port"].lower()))


def port_events(device, iface):
    from core.models import Event
    return list(Event.objects.filter(tenant_key=device.tenant_key, object_type="network.device", object_id=str(device.pk),
                                     fingerprint=f"network:{device.pk}:if{iface.if_index}")[:EVENT_LIMIT])


def compare_rows(device, iface, interfaces):
    """The five busiest other ports of the device by utilisation (stored figures, no query)."""
    rated = [i for i in interfaces if i.pk != iface.pk and i.utilization is not None]
    top = sorted(rated, key=lambda i: (-i.utilization, i.if_index))[:COMPARE_LIMIT]
    return [{"name": i.name, "url": port_url(device.pk, i.if_index), "util": f"{i.utilization:.0f}%", "tone": i.util_tone,
             "width": f"{min(100, max(0, i.utilization)):.0f}", "in": mbps(i.in_mbps), "out": mbps(i.out_mbps),
             "alias": i.alias} for i in top]


def explorer_url(request, device, iface):
    q = portmetrics.explorer_query(device, iface)
    if q is None or not apps.is_installed("telemetry") or visible_sites(request.user) is not None:
        return ""
    try:
        return f"{reverse('telemetry:explorer')}?{urlencode({'expr': q, 'range': metrics.range_key(request.GET.get('range'))})}"
    except NoReverseMatch:
        return ""


# ------------------------------------------------------------------ the page
def _day_charts(f, device, iface, range_key, main, optics_names):
    """24 h charts for the sparklines: the selected range's own charts when it is 24 h, else three more range queries."""
    if range_key == "24h":
        return main
    day, _why = portmetrics.load_charts(f, device, iface, portmetrics.SPARK_SECONDS, kinds=("traffic", "errors", "oper"))
    return day


def panel_list(charts, why):
    panels = []
    for key, title, hint, kinds in PANELS:
        chart = charts.get(key)
        if key == "optics" and chart is None and not why.get("optics_found"):
            continue
        panels.append({"key": key, "id": f"pd-{key}", "title": title, "hint": hint, "kinds": kinds, "chart": chart,
                       "legend": portmetrics.legend_rows(chart), "why": why.get(key, "")})
    return panels


def snapshot(request, device, iface, interfaces, range_key, f, extras=None, optics_names=None):
    """Everything both the page and the live JSON need, built from the stored port row plus the range charts."""
    seconds = metrics.RANGES[range_key]
    charts, why = portmetrics.load_charts(f, device, iface, seconds, optics_names=optics_names)
    day = _day_charts(f, device, iface, range_key, charts, optics_names)
    sparks = portmetrics.sparklines(day) if day else {}
    return charts, why, sparks, kpi_cards(device, iface, day or {}, sparks)


@require("view")
@require_GET
def interface(request, pk, if_index):
    device, iface, interfaces, early = _lookup(request, pk, if_index)
    if early:
        return early
    goto = _int_param(request.GET.get("goto"))
    if goto is not None and any(i.if_index == goto for i in interfaces):
        return redirect(port_url(device.pk, goto, range=metrics.range_key(request.GET.get("range"))))
    range_key = metrics.range_key(request.GET.get("range"))
    f = portmetrics.Fetcher()
    extras = portmetrics.port_extras(f, device, iface)
    thresholds = extras.get("thresholds") or {}
    sensors = [dict(sn, thresholds=thresholds.get(sn["name"], [])) for sn in extras.get("sensors") or []]
    charts, why, sparks, kpis = snapshot(request, device, iface, interfaces, range_key, f, extras,
                                         optics_names=[s["name"] for s in sensors])
    why["optics_found"] = bool(sensors)
    pos = next(n for n, i in enumerate(interfaces) if i.pk == iface.pk)
    can_operate = services.can_manage(request.user, "operate", device.site)
    reason = portmetrics.unavailable_reason(device)
    state = state_values(device, iface)
    banners = []
    if reason:
        banners.append(("mute" if not device.enabled else "warn", reason))
    elif f.errors:
        banners.append(("warn", "Prometheus did not answer every query: " + "; ".join(f.errors[:2])))
    only_extras = [e for e in extras.get("errors") or [] if e not in f.errors]   # the same failure is said once
    if only_extras and not reason:
        banners.append(("warn", "Some live details could not be read: " + "; ".join(only_extras[:2])))
    if state["stale"]:
        banners.append(("warn", f"The stored figures for this port {'have never been collected' if iface.updated_at is None else 'were last updated ' + state['updated']}; interface metrics are collected every minute while run_probes is running."))
    neighbours = neighbour_cards(request, extras.get("neighbours") or []) if not reason else []
    ctx = {
        "device": device, "iface": iface, "pos": pos + 1, "total": len(interfaces),
        "prev_port": interfaces[pos - 1] if pos > 0 else None,
        "next_port": interfaces[pos + 1] if pos + 1 < len(interfaces) else None,
        "ports": interfaces,
        "range_key": range_key, "range_label": metrics.RANGE_LABELS[range_key],
        "range_links": _range_links(request, range_key),
        "state": state, "banners": banners, "kpis": kpis, "sparks": sparks, "gauge": gauge_data(iface),
        "panels": (panels := panel_list(charts, why)),
        # the state bands share the last row with the last graph when the graphs leave one half-row open
        "oper_half": len([p for p in panels if p["key"] != "traffic"]) % 2 == 1, "oper": charts.get("oper"), "oper_why": why.get("oper", ""),
        "info": info_rows(iface, interfaces, extras, device), "neighbours": neighbours,
        "links": physical_links(request, device, iface, can_operate, interfaces),
        "sensors": sensors, "poe": extras.get("poe"),
        "events": port_events(device, iface), "compare": compare_rows(device, iface, interfaces),
        "export_url": reverse("network:interface_export", args=[device.pk, iface.if_index]) + f"?{urlencode({'range': range_key})}",
        "explorer_url": explorer_url(request, device, iface),
        "live_url": reverse("network:interface_live", args=[device.pk, iface.if_index]) + f"?{urlencode({'range': range_key})}",
        "live_interval": LIVE_INTERVAL, "prom_calls": f.calls, "can_operate": can_operate,
        "interfaces_url": reverse("network:detail", args=[device.pk]) + "?tab=interfaces",
        **_perms(request),
    }
    return render(request, "network/port.html", ctx)


# ------------------------------------------------------------------ live JSON
def live_payload(request, device, iface, interfaces, range_key, f):
    optics = portmetrics.cached_optics(device, iface)
    charts, why, sparks, kpis = snapshot(request, device, iface, interfaces, range_key, f, optics_names=optics)
    state = state_values(device, iface)
    values, tones = {}, {}
    for c in kpis:
        values[f"kpi_{c['key']}"], values[f"kpi_{c['key']}_sub"], tones[f"kpi_{c['key']}"] = c["value"], c["sub"], c["tone"]
    values.update({f"state_{k}": v for k, v in state.items() if isinstance(v, str)})
    tones["oper"] = {"up": "ok", "down": "crit", "lowerLayerDown": "crit"}.get(iface.oper_status, "mute")
    tones["admin"] = "ok" if iface.admin_status == "up" else "mute"
    panels = {p["id"]: p for p in panel_list(charts, why) if p["chart"]}
    return {
        "generated_at": timezone.now().isoformat(), "stale": state["stale"], "oper_status": iface.oper_status,
        "values": values, "tones": tones,
        "charts": {pid: p["chart"] for pid, p in panels.items()},
        "legends": {pid: p["legend"] for pid, p in panels.items()},
        "sparks": {f"pd-spark-{k}": v for k, v in sparks.items()},
        "gauge": gauge_data(iface), "oper": charts.get("oper"),
        "notes": [m for m in (portmetrics.unavailable_reason(device),) if m] + f.errors[:2],
    }


@require("view")
@require_GET
def interface_live(request, pk, if_index):
    device, iface, interfaces, early = _lookup(request, pk, if_index, json_errors=True)
    if early:
        return early
    if _throttled(request, "live", LIVE_PER_MINUTE):
        resp = JsonResponse({"error": "Too many requests. The page refreshes itself every 30 seconds."}, status=429)
        resp["Retry-After"] = "30"
        return resp
    resp = JsonResponse(live_payload(request, device, iface, interfaces, metrics.range_key(request.GET.get("range")),
                                     portmetrics.Fetcher()))
    resp["Cache-Control"] = "no-store"
    return resp


# ------------------------------------------------------------------ CSV
CSV_COLUMNS = ["timestamp_utc", "device", "interface", "alias", "in_mbps", "out_mbps", "utilization_pct", "unicast_pps",
               "multicast_pps", "broadcast_pps", "in_errors_per_s", "out_errors_per_s", "in_discards_per_s",
               "out_discards_per_s", "oper_state"]
CSV_SERIES = (("traffic", "Inbound", "in_mbps"), ("traffic", "Outbound", "out_mbps"), ("util", "Utilisation", "utilization_pct"),
              ("packets", "Unicast", "unicast_pps"), ("packets", "Multicast", "multicast_pps"),
              ("packets", "Broadcast", "broadcast_pps"), ("errors", "In errors", "in_errors_per_s"),
              ("errors", "Out errors", "out_errors_per_s"), ("errors", "In discards", "in_discards_per_s"),
              ("errors", "Out discards", "out_discards_per_s"))


def csv_table(charts):
    """[{column: value}] keyed by timestamp from the page's charts (at most portviews.MAX_POINTS rows, oldest first)."""
    rows = {}
    for chart_key, series, column in CSV_SERIES:
        chart = charts.get(chart_key)
        if not chart:
            continue
        values = portmetrics.series_values(chart, series) or []
        if chart.get("unit") == "Gbps":   # the traffic graph switches to Gbps on fast ports; the CSV is always Mbps
            values = [None if v is None else round(v * 1000.0, 3) for v in values]
        for t, v in zip(chart["ts"], values):
            rows.setdefault(t, {})[column] = v
    states = {int(t): s for t, s in charts.get("oper_points") or []}
    for t in rows:
        if t in states:
            rows[t]["oper_state"] = states[t]
    return [dict(ts=t, **rows[t]) for t in sorted(rows)][-portmetrics.MAX_POINTS_CSV:]


@require("view")
@require_GET
def interface_csv(request, pk, if_index):
    device, iface, _interfaces, early = _lookup(request, pk, if_index)
    if early:
        return early
    if _throttled(request, "csv", CSV_PER_MINUTE):
        resp = HttpResponse("Too many exports. Try again in a minute.", status=429, content_type="text/plain; charset=utf-8")
        resp["Retry-After"] = "60"
        return resp
    range_key = metrics.range_key(request.GET.get("range"))
    f = portmetrics.Fetcher()
    charts, _why = portmetrics.load_charts(f, device, iface, metrics.RANGES[range_key])
    resp = HttpResponse(content_type="text/csv; charset=utf-8")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", f"{device.name}-{iface.name}")[:80].strip("-") or f"device-{device.pk}-if{iface.if_index}"
    resp["Content-Disposition"] = f'attachment; filename="{safe}-{range_key}.csv"'
    resp["Cache-Control"] = "no-store"
    w = csv.writer(resp)
    w.writerow(CSV_COLUMNS)
    for row in csv_table(charts):
        stamp = datetime.fromtimestamp(row["ts"], tz=dt_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        w.writerow([_cell(v) for v in [stamp, device.name, iface.name, iface.alias]
                    + ["" if row.get(c) is None else row[c] for c in CSV_COLUMNS[4:]]])
    return resp
