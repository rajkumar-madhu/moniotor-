import csv
import ipaddress
import os
import re
from collections import Counter
from pathlib import Path
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.db.models.functions import Trim
from django.http import Http404, HttpResponse, HttpResponseBase, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from core import prometheus
from core.access import can, can_on, require, visible_sites
from core.audit import audit
from core.client_scope import SESSION_KEY as CLIENT_SESSION_KEY
from core.client_scope import active_client

from . import discovery, images, jobs, legacy_panels, metrics, services, vm_metrics
from .forms import ApproveForm, DeviceForm, DiscoveryForm, FieldImageError
from .live import FRONT_PANEL_MAX, LIVE_INTERVAL, apply_guest_uptime, card_data, device_snapshot, panel_ports, port_data
from .models import TYPE_PAGES, Device, DeviceImage, DeviceStatus, DeviceType, DiscoveredHost, DiscoveryRun, Environment

TYPE_INTRO = {
    "switch": "Access, distribution and core switches",
    "router": "WAN, edge and core routers",
    "firewall": "Perimeter and internal firewalls",
    "load_balancer": "Application delivery controllers and load balancers",
    "wireless": "Wireless controllers and access points",
    "physical_server": "Bare-metal servers and appliances",
    "virtual_machine": "Virtual machines and their hypervisor guests",
}
TABS = [("overview", "Overview"), ("interfaces", "Interfaces"), ("traffic", "Traffic"),
        ("hardware", "Hardware"), ("neighbours", "Neighbours"), ("events", "Alerts"), ("configuration", "Configuration")]
# Prototype VM console tabs. `interfaces` / `events` reuse network templates under Network / Alerts labels.
VM_TABS = [
    ("overview", "Overview"), ("performance", "Performance"), ("storage", "Storage"),
    ("interfaces", "Network"), ("snapshots", "Snapshots"), ("guest", "Guest OS"),
    ("processes", "Processes"), ("services", "Services"), ("logs", "Logs"),
    ("events", "Alerts"), ("topology", "Topology"), ("configuration", "Configuration"),
]
CHECK_ALL_LIMIT = 100
PANEL_CHUNK = 12       # ports per column group (two rows of six), as on most fixed-port switches
IF_FILTERS = [("all", "All"), ("up", "Up"), ("busy", "Busy or errors"), ("down", "Down"), ("unused", "Unused or admin down")]


def _front_panel(ports):
    """Ports in if_index order, grouped into runs of one speed (48 × 25G, then 4 × 100G), each run split into
    PANEL_CHUNK-port groups. A port with no reported speed (common while the link is down) joins the current run.
    Entries are (position on the panel, interface)."""
    from .templatetags.network_tags import speed
    blocks = []
    for n, p in enumerate(ports, 1):
        s = p.speed_mbps or None
        if blocks and (s is None or blocks[-1]["speed"] in (None, s)):
            blocks[-1]["speed"] = blocks[-1]["speed"] or s
        else:
            blocks.append({"speed": s, "ports": []})
        blocks[-1]["ports"].append((n, p))
    for b in blocks:
        b["label"] = f'{len(b["ports"])} × {speed(b["speed"])}' if b["speed"] else f'{len(b["ports"])} ports'
        b["chunks"] = [b["ports"][i:i + PANEL_CHUNK] for i in range(0, len(b["ports"]), PANEL_CHUNK)]
    return blocks


def _current_interfaces(device):
    """See metrics.current_interfaces: a disabled device's last rates are not current traffic."""
    return metrics.current_interfaces(device)


def _iface_matches(i, state, q):
    if state == "up" and i.oper_status != "up":
        return False
    if state in ("busy", "down", "unused") and i.port_tone != state:
        return False
    return not q or q in f"{i.name} {i.alias}".lower()


# ------------------------------------------------------------------ helpers
def _int_param(value, max_len=9):
    """A non-negative integer from request data, or None. ASCII digits only and a bounded length: str.isdigit() accepts
    characters such as superscript two that int() then rejects, and int() refuses more than 4300 digits."""
    value = value or ""
    return int(value) if value.isascii() and value.isdecimal() and len(value) <= max_len else None


def _per_page(request, default=20):
    try:
        return max(5, min(100, int(request.GET.get("per", default))))
    except (TypeError, ValueError):
        return default


def _query(request, *drop):
    q = request.GET.copy()
    for k in ("page",) + drop:
        q.pop(k, None)
    return q


def _view_links(request):
    """Table / Cards layout links that keep the filters."""
    base = _query(request, "view")
    cards = base.copy()
    cards["view"] = "cards"
    return {"table": "?" + base.urlencode(), "cards": "?" + cards.urlencode()}


def _wants_json(request):
    return request.headers.get("Accept", "").startswith("application/json")


def _next(request, fallback):
    nxt = request.POST.get("next") or request.GET.get("next") or ""
    if _safe_url(request, nxt):
        return nxt
    return fallback


def _perms(request):
    u = request.user
    return {"can_operate": can(u, "operate"), "can_admin": can(u, "admin")}


def _safe_url(request, url):
    return bool(url) and url_has_allowed_host_and_scheme(url, allowed_hosts={request.get_host()}, require_https=request.is_secure())


def _deny(request, text="You do not have permission to do that.", fallback="network:all"):
    if _wants_json(request):
        return JsonResponse({"error": text}, status=403)
    messages.error(request, text)
    ref = request.META.get("HTTP_REFERER", "")
    return redirect(_next(request, ref if _safe_url(request, ref) else reverse(fallback)))


CSV_FORMULA_CHARS = ("=", "+", "-", "@", "\t", "\r")


def _cell(value):
    """CSV cell safe to open in a spreadsheet: text starting like a formula is prefixed with a quote."""
    if value is None:
        return ""
    if isinstance(value, str) and value.startswith(CSV_FORMULA_CHARS):
        return "'" + value
    return value


def _device(request, pk):
    """Return the device in scope, or a redirect that clears an over-narrow client filter.

    Deep links 404 when the header client picker hides a site (e.g. R15 Rack while
    FinSpot DR is selected). If the user can see the device with All Clients, clear
    the filter and retry instead of a bare 404.
    """
    qs = services.devices_for(request.user, request)
    device = qs.filter(pk=pk).first()
    if device:
        return device
    bare = Device.objects.filter(tenant_key=services.tenant(), pk=pk).first()
    if bare and active_client(request) and services.devices_for(request.user).filter(pk=pk).exists():
        request.session.pop(CLIENT_SESSION_KEY, None)
        request.session.modified = True
        site = bare.site or "—"
        messages.info(
            request,
            f"“{bare.name}” is at site {site}, outside the selected client. Switched to All Clients.",
        )
        params = request.GET.copy()
        # Keep client= so middleware does not auto-reselect a single client on the next request.
        params["client"] = ""
        return redirect(f"{request.path}?{params.urlencode()}")
    raise Http404("No Device matches the given query.")


def _as_device(device_or_response):
    """Unpack `_device` when callers must return redirects early."""
    if isinstance(device_or_response, HttpResponseBase):
        return None, device_or_response
    return device_or_response, None


def _filtered(request, qs, with_type=False):
    g = request.GET
    q = g.get("q", "").strip()
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(mgmt_ip__icontains=q) | Q(vendor__icontains=q) | Q(model__icontains=q)
                       | Q(serial__icontains=q) | Q(tags__icontains=q) | Q(role__icontains=q))
    for param, field in (("site", "site"), ("status", "status"), ("vendor", "vendor"), ("env", "environment")):
        v = g.get(param, "").strip()
        if v:
            qs = qs.filter(**{field: v})
    if with_type and g.get("type") in DeviceType.values:
        qs = qs.filter(device_type=g["type"])
    sort = g.get("sort", "")
    order = {"name": ["name"], "status": ["status", "name"], "site": ["site", "name"], "seen": ["-last_seen", "name"],
             "latency": ["last_latency_ms", "name"], "ip": ["mgmt_ip"]}.get(sort.lstrip("-"))
    if order:
        if sort.startswith("-"):
            order = [o[1:] if o.startswith("-") else "-" + o for o in order]
        qs = qs.order_by(*order)
    return qs


# ------------------------------------------------------------------ lists
@require("view")
def device_list(request, device_type=None):
    base = services.devices_for(request.user, request)
    if device_type:
        base = base.filter(device_type=device_type)
    counts = services.status_counts(base)
    qs = _filtered(request, base, with_type=device_type is None)
    page = Paginator(qs, _per_page(request)).get_page(request.GET.get("page"))
    ids = [d.pk for d in page]
    avail = services.availability(ids, 24)
    ifs = services.interface_counts(ids)
    guest_metrics = {}
    if prometheus.configured():
        guest_metrics = vm_metrics.batch_guest_instants(list(page))
    cards = card_data(list(page), guest=guest_metrics if prometheus.configured() else None)
    list_colspan = (11 if device_type is None else 10) + (4 if prometheus.configured() else 0)
    for d in page:
        d.avail24 = avail.get(d.pk)
        d.ifs = ifs.get(d.pk)
        d.guest_live = guest_metrics.get(d.pk) if prometheus.configured() else None
        d.card = cards[d.pk]
    total = counts["total"]
    pct = {s: (100.0 * counts[s] / total if total else 0) for s in DeviceStatus.values}
    if device_type:
        url_name, title, icon = TYPE_PAGES[device_type]
        intro = TYPE_INTRO[device_type]
    else:
        url_name, title, icon, intro = "network:all", "All Devices", "network", "Every monitored network device, server and VM"
    vendors = sorted({v for v in base.order_by().values_list("vendor", flat=True).distinct() if v}, key=str.lower)
    sites = sorted({s for s in base.order_by().values_list("site", flat=True).distinct() if s}, key=str.lower)
    groups = []
    key = "site" if device_type else "device_type"
    rows = base.order_by().values(key).annotate(
        total=Count("id"), up=Count("id", filter=Q(status=DeviceStatus.UP)), down=Count("id", filter=Q(status=DeviceStatus.DOWN)))
    if device_type is None:
        by_type = {r["device_type"]: r for r in rows}
        for value, _label in DeviceType.choices:
            r = by_type.get(value)
            if r:
                groups.append({"label": TYPE_PAGES[value][1], "url": reverse(TYPE_PAGES[value][0]), "total": r["total"],
                               "up": r["up"], "down": r["down"], "icon": TYPE_PAGES[value][2]})
    else:
        for r in sorted(rows, key=lambda r: (-r["down"], (r["site"] or "~").lower()))[:8]:
            groups.append({"label": r["site"] or "(No site)", "url": f"?{urlencode({'site': r['site']})}" if r["site"] else "?",
                           "total": r["total"], "up": r["up"], "down": r["down"], "icon": "globe"})
    export_q = _query(request)
    if device_type:
        export_q["type"] = device_type
    ctx = {
        "page": page, "counts": counts, "pct": pct, "device_type": device_type, "title": title, "icon": icon, "intro": intro,
        "list_url": reverse(url_name), "vendors": vendors, "sites": sites, "statuses": DeviceStatus.choices,
        "envs": Environment.choices, "types": DeviceType.choices, "groups": groups,
        "events": services.recent_events(request.user, 8, device_type),
        "donut": {"segments": [{"label": "Up", "value": counts["up"], "color": "var(--chart-ok)"},
                               {"label": "Down", "value": counts["down"], "color": "var(--chart-crit)"},
                               {"label": "Unknown", "value": counts["unknown"], "color": "var(--chart-unknown)"}],
                  "center": {"value": str(total), "label": "Devices"}},
        "query": _query(request), "export_query": export_q.urlencode(),
        "create_url": reverse("network:create") + (f"?type={device_type}" if device_type else ""),
        "prometheus_on": prometheus.configured(),
        "last_poll": base.exclude(last_checked_at=None).order_by("-last_checked_at").values_list("last_checked_at", flat=True).first(),
        "guest_metrics": guest_metrics,
        "list_colspan": list_colspan,
        "view_mode": "cards" if request.GET.get("view") == "cards" else "table",
        "view_links": _view_links(request),
        "cards_url": reverse("network:cards_live"), "live_interval": LIVE_INTERVAL,
        **_perms(request),
    }
    return render(request, "network/list.html", ctx)


def _type_view(device_type):
    def view(request):
        return device_list(request, device_type)
    view.__name__ = f"{device_type}_list"
    return view


switches = _type_view("switch")
routers = _type_view("router")
firewalls = _type_view("firewall")
load_balancers = _type_view("load_balancer")
wireless = _type_view("wireless")
physical_servers = _type_view("physical_server")
virtual_machines = _type_view("virtual_machine")


@require("view")
def export_csv(request):
    qs = _filtered(request, services.devices_for(request.user, request), with_type=True)
    resp = HttpResponse(content_type="text/csv; charset=utf-8")
    kind = request.GET.get("type") if request.GET.get("type") in DeviceType.values else "all"
    resp["Content-Disposition"] = f'attachment; filename="network-devices-{kind}.csv"'
    w = csv.writer(resp)
    w.writerow(["name", "type", "vendor", "model", "serial", "firmware", "mgmt_ip", "check_port", "site", "role", "environment",
                "status", "last_seen", "last_checked", "latency_ms", "last_error", "prometheus_instance", "uptime_seconds",
                "enabled", "tags", "contact"])
    for d in qs:
        w.writerow([_cell(v) for v in [d.name, d.device_type, d.vendor, d.model, d.serial, d.firmware, d.mgmt_ip, d.check_port, d.site, d.role,
                    d.environment, d.status, d.last_seen.isoformat() if d.last_seen else "",
                    d.last_checked_at.isoformat() if d.last_checked_at else "",
                    "" if d.last_latency_ms is None else d.last_latency_ms, d.last_error, d.prometheus_instance,
                    "" if d.uptime_seconds is None or not d.enabled else d.uptime_seconds,
                    "yes" if d.enabled else "no", d.tags, d.contact]])
    return resp


# ------------------------------------------------------------------ detail
def _metrics_msg(device):
    """Why the device has no (or stale) metrics, or "" when Prometheus data is flowing."""
    if not device.enabled:
        return "Monitoring is disabled for this device, so no checks or metrics are collected."
    if not prometheus.configured():
        return "No metrics source: set LINKEDEYE_PROMETHEUS_URL (or Administration → Integrations → Prometheus)."
    if not device.prometheus_instance:
        return "Set this device’s Prometheus instance to the scrape target’s instance label (e.g. IP:9100)."
    if device.metrics_error:
        return device.metrics_error
    if not device.metrics_updated_at:
        return "Waiting for the first metrics collection (every 60 s while run_probes is running)."
    return ""


def _range_links(request, current):
    """Segmented range control that keeps the other query parameters (tab, port)."""
    links = []
    for key in metrics.RANGES:
        q = _query(request, "range")
        q["range"] = key
        links.append({"key": key, "label": key, "title": metrics.RANGE_LABELS[key], "url": f"?{q.urlencode()}",
                      "on": key == current})
    return links


@require("view")
def detail(request, pk):
    device, early = _as_device(_device(request, pk))
    if early:
        return early
    is_vm = device.device_type == DeviceType.VIRTUAL_MACHINE
    tabs = VM_TABS if is_vm else TABS
    tab = request.GET.get("tab", "overview")
    if tab not in dict(tabs):
        tab = "overview"
    interfaces = _current_interfaces(device)
    if not device.enabled:
        device.uptime_seconds = None   # in memory only (detail never saves it): not current either
    summary = services.interface_summary(interfaces)
    ranked = sorted(interfaces, key=lambda i: (i.utilization is None, -(i.utilization or 0), i.if_index))
    top = ranked[:10] if summary["has_rates"] else interfaces[:10]
    ports = [] if is_vm else panel_ports(interfaces)
    tones = Counter(i.port_tone for i in interfaces)
    # ?port=<ifIndex> narrows the charts to one interface of this device
    port = None
    port_no = None if is_vm else _int_param(request.GET.get("port"))
    if port_no is not None:
        port = next((i for i in interfaces if i.if_index == port_no), None)
    range_key = metrics.range_key(request.GET.get("range"))
    traffic, traffic_msg, errors_chart, errors_msg = None, "", None, ""
    if not is_vm:   # the chart strip sits above the tabs, so every tab needs it
        seconds = metrics.RANGES[range_key]
        traffic, traffic_msg = metrics.traffic_range(device, seconds, port.if_index if port else None)
        errors_chart, errors_msg = metrics.errors_range(device, seconds, port.if_index if port else None)
    if_filter = request.GET.get("state") if request.GET.get("state") in dict(IF_FILTERS) else "all"
    if_q = request.GET.get("q", "").strip()[:100]
    if_counts = {"all": len(interfaces), "up": summary["if_up"], "busy": tones["busy"], "down": tones["down"],
                 "unused": tones["unused"]}
    can_op = services.can_manage(request.user, "operate", device.site)
    metrics_msg = _metrics_msg(device)
    latest = services.events_for(device, 1)
    live_q = {"range": range_key}
    if port:
        live_q["port"] = port.if_index
    ctx = {
        "device": device, "tab": tab, "tabs": tabs, "is_vm": is_vm,
        "interfaces": interfaces, "top_interfaces": top, **summary,
        "ports": ports, "panel_blocks": _front_panel(ports) if ports else [], "port_data": port_data(ports),
        "panel_truncated": summary["ports_total"] > FRONT_PANEL_MAX,
        "port": port, "port_pos": next((n for n, p in enumerate(ports) if port and p.pk == port.pk), None),
        "errors_chart": errors_chart, "errors_msg": errors_msg,
        "if_filters": [(key, label, if_counts[key]) for key, label in IF_FILTERS], "if_filter": if_filter, "if_q": if_q,
        "if_rows": [i for i in interfaces if _iface_matches(i, if_filter, if_q.lower())],
        "avail24": services.availability([device.pk], 24).get(device.pk),
        "avail7": services.availability([device.pk], 24 * 7).get(device.pk),
        "avg_latency": services.avg_latency(device), "spark": services.latency_spark(device),
        "avail_chart": services.availability_chart(device), "latency_chart": services.latency_chart(device),
        "traffic": traffic, "traffic_msg": traffic_msg, "traffic_range": range_key,
        "range_label": metrics.RANGE_LABELS[range_key], "range_links": _range_links(request, range_key),
        "port_bars": None if is_vm else metrics.port_bars(interfaces),
        "prometheus_on": prometheus.configured(), "metrics_msg": metrics_msg,
        "metrics_ok": bool(prometheus.configured() and device.prometheus_instance and device.metrics_updated_at
                           and not device.metrics_error),
        "events": services.events_for(device, 50 if tab == "events" else 6),
        "latest_event_id": latest[0].pk if latest else "",
        "polls": device.polls.all()[:50] if tab == "events" else [],
        "topology_url": services.topology_url(device), "discovery_url": services.discovery_url(device),
        "can_operate": can_op, "can_admin": services.can_manage(request.user, "admin", device.site),
        "discovered": device.discovered_as.select_related("run").first(),
        "legacy_onboard": legacy_panels.resolve_legacy_panel(device),
        "live_url": f"{reverse('network:live', args=[device.pk])}?{urlencode(live_q)}",
        "live_interval": LIVE_INTERVAL,
        "card": None,
        "vm_metrics": None, "vm_meta": {}, "vm_info_rows": [],
        "vm_actions": vm_metrics.VM_ACTIONS, "vm_power": "", "vm_stub": None,
    }
    guest = None
    if is_vm:
        guest = vm_metrics.fetch_guest_metrics(device)
        apply_guest_uptime(device, guest)
        perf_range_key = request.GET.get("perf_range", "24h")
        if perf_range_key not in vm_metrics.PERF_RANGE_SECONDS:
            perf_range_key = "24h"
        perf_seconds = vm_metrics.PERF_RANGE_SECONDS[perf_range_key]
        vm_perf = vm_metrics.fetch_guest_performance(device, seconds=perf_seconds) if tab == "performance" else None
        meta, _extra = vm_metrics.parse_vm_meta(device)
        if guest is not None:   # guest series are flowing: an SNMP note from the metrics job does not apply
            metrics_msg = ""
        elif prometheus.configured() and device.prometheus_instance:
            metrics_msg = f'No node_exporter series in Prometheus for instance "{device.prometheus_instance}".'
        ctx.update({
            "vm_metrics": guest,
            "metrics_msg": metrics_msg,
            "vm_meta": meta,
            "vm_info_rows": vm_metrics.vm_info_rows(device, meta),
            "vm_power": vm_metrics.guest_power_label(device),
            "vm_performance": vm_perf,
            "perf_range": perf_range_key,
            "perf_seconds": perf_seconds,
            "perf_range_choices": list(vm_metrics.PERF_RANGE_SECONDS.items()),
        })
        if tab in vm_metrics.STUB_COPY:
            title, blurb = vm_metrics.STUB_COPY[tab]
            ctx["vm_stub"] = {"title": title, "blurb": blurb}
    ctx["card"] = card_data([device], guest={device.pk: guest} if guest else None)[device.pk]
    if tab == "configuration":
        from core.models import AuditEvent
        ctx["audit_rows"] = AuditEvent.objects.filter(object_type="network.device", object_id=str(device.pk))[:25]
    return render(request, "network/detail.html", ctx)


@require("view")
@require_GET
def live(request, pk):
    """JSON snapshot polled by the detail page (network/live.js) for live KPIs, charts and port states."""
    device = services.devices_for(request.user, request).filter(pk=pk).first()
    if device is None:
        return JsonResponse({"error": "Device not found, or outside the selected client or your sites."}, status=404)
    data = device_snapshot(device, metrics.range_key(request.GET.get("range")), _int_param(request.GET.get("port")))
    resp = JsonResponse(data)
    resp["Cache-Control"] = "no-store"
    return resp


@require("view")
@require_GET
def cards_live(request):
    """JSON for the device cards on a list page (network/device-card.js): ?ids=1,2,3 (at most 60). Only devices the user
    may see are answered; the figures come from the same card_data() that rendered the cards."""
    # ASCII digits only and a sane length: "²" passes str.isdigit() but not int(), and a 20-digit id overflows the database
    ids = [int(i) for i in (p.strip() for p in request.GET.get("ids", "")[:2000].split(",")[:200])
           if i.isascii() and i.isdigit() and len(i) <= 12][:60]
    devices = list(services.devices_for(request.user, request).filter(pk__in=ids))
    cards = card_data(devices)
    resp = JsonResponse({"generated_at": timezone.now().isoformat(),
                         "cards": {str(pk): {"status": c["status"], "tone": c["tones"]["status"], "label": c["values"]["status"],
                                             "values": c["values"], "tones": c["tones"], "figures": c["figures"]}
                                   for pk, c in cards.items()}})
    resp["Cache-Control"] = "no-store"
    return resp


@require("view")
@require_GET
@xframe_options_sameorigin     # the Hardware tab frames this page; every other page keeps the global X-Frame-Options: DENY
def legacy_panel(request, pk):
    """Serve the matched iframeGraphs front-panel markup (SVG) for a device."""
    device, early = _as_device(_device(request, pk))
    if early:
        return early
    info = legacy_panels.resolve_legacy_panel(device)
    if not info or not info.get("iframe_j2"):
        return HttpResponse("No legacy panel matched for this vendor/model.", status=404)
    markup = legacy_panels.load_panel_markup(device, info["iframe_j2"])
    if not markup:
        return HttpResponse("Legacy panel file is missing on the server.", status=404)
    html = (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<style>body{margin:0;padding:12px;background:#12151a;color:#e8eaed;"
        "font:13px system-ui,sans-serif}.firerow,svg{max-width:100%;height:auto}"
        "legend{width:auto!important}</style></head><body>"
        f"{markup}</body></html>"
    )
    return HttpResponse(html)


# ------------------------------------------------------------------ mutations
@require("operate")
@require_http_methods(["GET", "POST"])
def device_form(request, pk=None):
    if pk:
        device, early = _as_device(_device(request, pk))
        if early:
            return early
    else:
        device = None
    if device and not services.can_manage(request.user, "operate", device.site):
        raise PermissionDenied
    initial = {}
    if not device:
        if request.GET.get("type") in DeviceType.values:
            initial["device_type"] = request.GET["type"]
        if request.GET.get("site"):
            initial["site"] = request.GET["site"]
    form = DeviceForm(request.POST or None, request.FILES or None, instance=device, initial=initial, user=request.user)
    if request.method == "POST" and form.is_valid():
        site = form.cleaned_data.get("site", "")
        photo = ("keep", None)
        if not services.can_manage(request.user, "operate", site):
            form.add_error("site", "You cannot manage devices on this site.")
        else:
            # the upload / download happens only here, after the site permission check, and is validated byte by byte
            try:
                photo = form.resolve_image(request.user)
            except FieldImageError as e:
                form.add_error(e.field, str(e))
        if not form.errors:
            changed = list(form.changed_data)
            old_image = device.image if device else None
            obj = form.save(commit=False)
            if photo[0] == "set":
                obj.image = photo[1]
                changed.append("image")
            elif photo[0] == "clear":
                obj.image = None
                changed.append("image")
            obj.tenant_key = settings.LINKEDEYE_TENANT
            reset = set()                    # poll-owned columns this edit deliberately resets
            if device and {"mgmt_ip", "check_port"} & set(changed):
                obj.consecutive_failures = 0
                reset.add("consecutive_failures")
            if device and "enabled" in changed and not obj.enabled:
                # a device that is no longer checked must not keep counting as up or down
                obj.status, obj.last_error, obj.status_changed_at = DeviceStatus.UNKNOWN, "Monitoring disabled", timezone.now()
                # ...nor keep showing the last collected rates as current traffic (the inventory is kept)
                obj.uptime_seconds, obj.metrics_updated_at = None, None
                reset |= {"status", "last_error", "status_changed_at", "uptime_seconds", "metrics_updated_at"}
                device.interfaces.all().update(**metrics.STALE_RATES)
            if device and "prometheus_instance" in changed:
                # interfaces and uptime belonged to the previous snmp_exporter target
                obj.uptime_seconds, obj.metrics_updated_at, obj.metrics_error = None, None, ""
                reset |= {"uptime_seconds", "metrics_updated_at", "metrics_error"}
                device.interfaces.all().delete()
            if device and "enabled" in changed and obj.enabled and obj.last_error == "Monitoring disabled":
                obj.last_error = ""
                reset.add("last_error")
            if device:
                # Only what this form owns (plus the state fields it deliberately resets above): the poll job may have
                # changed status, failures or last_alert_status while a photo was being fetched, and a whole-row save
                # would put the stale copy back and lose the recovery event.
                obj.save(update_fields=sorted(set(DeviceForm._meta.fields) | {"image", "tenant_key", "updated_at"} | reset))
            else:
                obj.save()
            if photo[0] != "keep" and old_image is not None and old_image.pk != obj.image_id:
                images.release(old_image)
            if device:
                audit(request, "update", obj, changed=changed)
                if obj.enabled and {"enabled", "mgmt_ip", "check_port"} & set(changed):
                    # re-enabled or pointed at a new target: check it now instead of waiting for the poll
                    result = jobs.check_devices([obj])[0]
                    obj.refresh_from_db()
                    messages.success(request, f"Saved “{obj.name}”. Check now – {_check_summary(obj, result)}")
                else:
                    messages.success(request, f"Saved “{obj.name}”.")
            else:
                audit(request, "create", obj, device_type=obj.device_type, mgmt_ip=obj.mgmt_ip, site=obj.site)
                if obj.enabled:
                    jobs.check_devices([obj])
                    obj.refresh_from_db()
                    messages.success(request, f"Added “{obj.name}”. First check: {obj.get_status_display()}"
                                              + (f" ({obj.last_error})" if obj.last_error else "") + ".")
                else:
                    messages.success(request, f"Added “{obj.name}” (monitoring disabled).")
            return redirect(obj.get_absolute_url())
    vendors = sorted({v for v in Device.objects.filter(tenant_key=settings.LINKEDEYE_TENANT).values_list("vendor", flat=True) if v}
                     | {"Cisco", "Juniper", "Arista", "Fortinet", "Palo Alto Networks", "F5", "Aruba", "Dell", "HPE", "VMware"})
    return render(request, "network/form.html", {"form": form, "device": device, "vendors": vendors,
                                                 "can_admin": can_on(request.user, "admin", None)})


@require("admin")
@require_POST
def device_delete(request, pk):
    device, early = _as_device(_device(request, pk))
    if early:
        return early
    if not services.can_manage(request.user, "admin", device.site):
        return _deny(request)
    name, back = device.name, device.type_list_url
    audit(request, "delete", device, name=name, mgmt_ip=device.mgmt_ip, device_type=device.device_type, site=device.site)
    image = DeviceImage.objects.filter(pk=device.image_id).defer("data").first() if device.image_id else None
    with transaction.atomic():
        device.delete()
        images.release(image)           # the photo goes with its last user, so it stops counting toward the library cap
    messages.success(request, f"Deleted “{name}” and its check history.")
    return redirect(back)


def _check_summary(device, result):
    if result.ok:
        return f"{device.name}: {device.get_status_display()} in {result.latency_ms:g} ms"
    if device.status == DeviceStatus.UP and not result.blocked:
        # one failed check does not mark an Up device down (see jobs.DOWN_AFTER_FAILURES)
        return (f"{device.name}: check failed – {result.error}. Still Up: marked down after "
                f"{jobs.DOWN_AFTER_FAILURES} failed checks in a row ({device.consecutive_failures} so far).")
    return f"{device.name}: {device.get_status_display()} – {result.error}"


@require("operate")
@require_POST
def check_now(request, pk):
    device, early = _as_device(_device(request, pk))
    if early:
        return early
    if not services.can_manage(request.user, "operate", device.site):
        return _deny(request)
    if not device.enabled:
        text = f"Monitoring is disabled for {device.name}; enable it before running a check."
        if _wants_json(request):
            return JsonResponse({"error": text, "status": device.status}, status=409)
        messages.warning(request, text)
        return redirect(_next(request, device.get_absolute_url()))
    result = jobs.check_devices([device])[0]
    device.refresh_from_db()
    audit(request, "run", device, check="reachability", status=device.status, ok=result.ok,
          latency_ms=result.latency_ms, error=result.error)
    summary = _check_summary(device, result)
    if _wants_json(request):
        return JsonResponse({"status": device.status, "summary": summary, "latency_ms": result.latency_ms, "error": result.error})
    (messages.success if result.ok else messages.warning)(request, summary)
    return redirect(_next(request, device.get_absolute_url()))


@require("operate")
@require_POST
def check_all(request):
    qs = services.devices_for(request.user, request).filter(enabled=True)
    t = request.POST.get("type") or request.GET.get("type")
    if t in DeviceType.values:
        qs = qs.filter(device_type=t)
    devices = [d for d in qs.order_by("name") if services.can_manage(request.user, "operate", d.site)]
    limited = len(devices) > CHECK_ALL_LIMIT
    devices = devices[:CHECK_ALL_LIMIT]
    results = jobs.check_devices(devices)
    up = sum(1 for r in results if r.ok)
    audit(request, "run", None, object_type="network.device", check="reachability", device_type=t or "all",
          checked=len(results), up=up)
    summary = f"Checked {len(results)} device(s): {up} reachable, {len(results) - up} not reachable."
    if limited:
        summary += f" Only the first {CHECK_ALL_LIMIT} were checked now; the rest are polled every minute."
    if _wants_json(request):
        return JsonResponse({"checked": len(results), "up": up, "summary": summary})
    messages.success(request, summary)
    return redirect(_next(request, reverse("network:all")))


# ------------------------------------------------------------------ discovery
@require("view")
@require_http_methods(["GET", "POST"])
def discovery_view(request):
    runs_qs = _runs_for(request.user)
    can_op = can(request.user, "operate")
    if request.method == "POST":
        if not can_op:
            return _deny(request, fallback="network:discovery")
        form = DiscoveryForm(request.POST, user=request.user)
        if form.is_valid():
            site = form.cleaned_data["site"]
            # a run without a site (or "All Sites") scans for every site: it needs operate on every site
            if not can_on(request.user, "operate", site or None):
                form.add_error("site", "You cannot manage devices on this site." if site else
                               "Choose one of your sites: a discovery for all sites needs the operator role on every site.")
            elif DiscoveryRun.objects.filter(tenant_key=settings.LINKEDEYE_TENANT, status__in=["queued", "running"]).count() >= 3:
                form.add_error(None, "Three discovery runs are already in progress. Wait for one to finish.")
            else:
                run = DiscoveryRun.objects.create(
                    tenant_key=settings.LINKEDEYE_TENANT, cidr=form.cleaned_data["cidr"], ports=form.cleaned_data["ports"],
                    site=site, requested_by=request.user, requested_by_name=request.user.get_username())
                audit(request, "discover", run, cidr=run.cidr, ports=run.ports, site=run.site)
                discovery.launch(run)
                messages.success(request, f"Discovery of {run.cidr} started.")
                return redirect(run.get_absolute_url())
    else:
        form = DiscoveryForm(initial={k: request.GET[k] for k in ("cidr", "ports", "site") if request.GET.get(k)}, user=request.user)
    page = Paginator(runs_qs, _per_page(request, 10)).get_page(request.GET.get("page"))
    return render(request, "network/discovery.html", {
        "form": form, "page": page, "query": _query(request), "can_operate": can_op,
        "allowed_cidrs": settings.PROBE_ALLOWED_PRIVATE_CIDRS, "active": runs_qs.filter(status__in=["queued", "running"]).exists(),
        "limits": {"prefix": discovery.MAX_V4_PREFIX, "ports": discovery.MAX_PORTS, "timeout": discovery.SCAN_TIMEOUT},
    })


def _runs_for(user):
    """Discovery runs the user may see: runs for their sites, runs without a site and runs they started."""
    qs = DiscoveryRun.objects.filter(tenant_key=settings.LINKEDEYE_TENANT)
    sites = visible_sites(user)
    if sites is None:
        return qs
    return qs.filter(Q(site__in=list(sites) + ["", "All Sites"]) | Q(requested_by=user))


def _run(request, pk):
    return get_object_or_404(_runs_for(request.user), pk=pk)


def _can_manage_run(user, run):
    """Cancel / ignore: the requester, or an operator for the run's site (tenant-wide for runs without a site)."""
    return (run.requested_by_id is not None and run.requested_by_id == user.pk) or services.can_manage(user, "operate", run.site)


def _ip_key(h):
    try:
        a = ipaddress.ip_address(h.ip)
        return (a.version, int(a))
    except ValueError:
        return (9, 0)


@require("view")
def discovery_run(request, pk):
    run = _run(request, pk)
    hosts = sorted(run.hosts.select_related("device"), key=_ip_key)
    show = request.GET.get("show", "")
    if show in ("new", "approved", "ignored", "existing"):
        hosts = [h for h in hosts if h.status == show]
    counts = {s: 0 for s, _ in DiscoveredHost.STATUS}
    for s in run.hosts.values_list("status", flat=True):
        counts[s] += 1
    # a scanned address can already be a device on a site this user may not see: the page then shows the
    # address and the "Already a device" badge, but not the device's name or a link to it
    linked = [h.device_id for h in hosts if h.device_id]
    visible = set(services.devices_for(request.user, request).filter(pk__in=linked).values_list("pk", flat=True)) if linked else set()
    for h in hosts:
        h.show_device = h.device_id in visible
    return render(request, "network/discovery_run.html", {
        "run": run, "hosts": hosts, "counts": counts, "show": show,
        "approve_form": ApproveForm(initial={"site": run.site, "device_type": "switch"}, user=request.user),
        "can_operate": can(request.user, "operate"), "can_manage_run": _can_manage_run(request.user, run),
    })


def _selected_hosts(request, run, statuses=("new",)):
    ids = [n for n in (_int_param(i, 12) for i in request.POST.getlist("host")) if n is not None]
    return list(run.hosts.filter(pk__in=ids, status__in=statuses))


def _unique_name(base):
    t = settings.LINKEDEYE_TENANT
    name, n = base[:150], 2
    while Device.objects.filter(tenant_key=t, name__iexact=name).exists():
        name = f"{base[:140]}-{n}"
        n += 1
    return name


NAME_ATTEMPTS = 3


def _create_device(base, **fields):
    """Create a device named after `base`, re-picking the name when another request claims it between
    the uniqueness check and the INSERT (tenant_key + name is a unique constraint). Each attempt is its
    own atomic block so a failed INSERT does not poison the surrounding transaction.
    Returns the device, or None when no free name could be claimed."""
    for _attempt in range(NAME_ATTEMPTS):
        try:
            with transaction.atomic():
                return Device.objects.create(name=_unique_name(base), **fields)
        except IntegrityError:
            continue
    return None


@require("operate")
@require_POST
def discovery_approve(request, pk):
    run = _run(request, pk)
    form = ApproveForm(request.POST, initial={"site": run.site}, user=request.user)
    back = run.get_absolute_url()
    if not form.is_valid():
        messages.error(request, "Choose a device type (and a valid site) to approve hosts.")
        return redirect(back)
    site, dtype, prefix = form.cleaned_data["site"], form.cleaned_data["device_type"], form.cleaned_data["name_prefix"].strip()
    if not services.can_manage(request.user, "operate", site):
        return _deny(request, "You cannot add devices on this site.", "network:discovery")
    hosts = _selected_hosts(request, run)
    if not hosts:
        messages.warning(request, "Select at least one new host to approve.")
        return redirect(back)
    now = timezone.now()
    who = request.user.get_username()
    created, existing, decided, unnamed = [], [], [], []
    for h in hosts:
        # Claim the host first (new -> approved in one statement): a double-click, or a second operator
        # approving the same run, must not create the device twice.
        if not run.hosts.filter(pk=h.pk, status="new").update(status="approved", decided_by_name=who, decided_at=now):
            decided.append(h.ip)
            continue
        # re-read after the claim: the address may have become a device since the scan, or just now
        dev = Device.objects.annotate(_ip=Trim("mgmt_ip")).filter(tenant_key=settings.LINKEDEYE_TENANT, _ip=h.ip).first()
        if dev is not None:
            # approved from another run (or added by hand) since this scan: link it, never duplicate it
            run.hosts.filter(pk=h.pk).update(status="existing", device=dev)
            existing.append(h.ip)
            continue
        port = 22 if 22 in h.open_ports else (h.open_ports[0] if h.open_ports else 22)
        dev = _create_device(
            f"{prefix}{h.ip}",
            tenant_key=settings.LINKEDEYE_TENANT, device_type=dtype, mgmt_ip=h.ip,
            check_port=port, site=site, environment="", tags="discovered",
            description=f"Discovered by run #{run.pk} ({run.cidr}) on {timezone.localtime(run.created_at):%Y-%m-%d}; "
                        f"open ports {h.ports_display}.")
        if dev is None:
            # every candidate name was taken while we were inserting: leave the host for another try
            run.hosts.filter(pk=h.pk).update(status="new", decided_by_name="", decided_at=None)
            unnamed.append(h.ip)
            continue
        run.hosts.filter(pk=h.pk).update(device=dev)
        audit(request, "create", dev, via="discovery", run=run.pk, mgmt_ip=h.ip, device_type=dtype, site=site)
        created.append(dev)
    audit(request, "approve", run, hosts=[d.mgmt_ip for d in created], already_devices=existing, device_type=dtype, site=site)
    text = f"Added {len(created)} device(s) as {dict(DeviceType.choices)[dtype]}."
    if created:
        text += " They are checked within a minute."
    if existing:
        text += f" Skipped {len(existing)} address(es) that are already devices: {', '.join(existing)}."
    if decided:
        text += f" Skipped {len(decided)} host(s) someone else had already decided: {', '.join(decided)}."
    if unnamed:
        text += f" Could not pick a free device name for {', '.join(unnamed)}; try again."
    (messages.success if created else messages.warning)(request, text)
    return redirect(back)


@require("operate")
@require_POST
def discovery_ignore(request, pk):
    run = _run(request, pk)
    if not _can_manage_run(request.user, run):
        return _deny(request, "You cannot manage discovery runs for this site.", "network:discovery")
    hosts = _selected_hosts(request, run)
    if not hosts:
        messages.warning(request, "Select at least one new host to ignore.")
        return redirect(run.get_absolute_url())
    now = timezone.now()
    run.hosts.filter(pk__in=[h.pk for h in hosts]).update(status="ignored", decided_by_name=request.user.get_username(), decided_at=now)
    audit(request, "ignore", run, hosts=[h.ip for h in hosts])
    messages.success(request, f"Ignored {len(hosts)} host(s).")
    return redirect(run.get_absolute_url())


@require("operate")
@require_POST
def discovery_cancel(request, pk):
    run = _run(request, pk)
    if not _can_manage_run(request.user, run):
        return _deny(request, "You cannot manage discovery runs for this site.", "network:discovery")
    if not run.active:
        messages.warning(request, "This run has already finished.")
        return redirect(run.get_absolute_url())
    DiscoveryRun.objects.filter(pk=run.pk, status__in=["queued", "running"]).update(status="cancelled", finished_at=timezone.now())
    audit(request, "cancel", run, cidr=run.cidr)
    messages.success(request, f"Cancelled the discovery of {run.cidr}.")
    return redirect(run.get_absolute_url())


@require("view")
def interfaces_csv(request, pk):
    device, early = _as_device(_device(request, pk))
    if early:
        return early
    resp = HttpResponse(content_type="text/csv; charset=utf-8")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", device.name).strip("-") or f"device-{device.pk}"
    resp["Content-Disposition"] = f'attachment; filename="{safe}-interfaces.csv"'
    w = csv.writer(resp)
    w.writerow(["if_index", "name", "alias", "type", "admin_status", "oper_status", "speed_mbps", "in_mbps", "out_mbps",
                "utilization_pct", "errors_per_s", "discards_per_s", "flaps_24h", "updated_at"])
    for i in _current_interfaces(device):
        w.writerow([_cell(v) for v in [i.if_index, i.name, i.alias, i.if_type, i.admin_status, i.oper_status, _blank(i.speed_mbps),
                    _blank(i.in_mbps), _blank(i.out_mbps), _blank(i.utilization), _blank(i.errors_per_s),
                    _blank(i.discards_per_s), _blank(i.flaps_24h), i.updated_at.isoformat()]])
    return resp


def _blank(v):
    return "" if v is None else v

