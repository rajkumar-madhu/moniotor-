import csv

from django.apps import apps
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_http_methods, require_POST

from core import prometheus
from core.access import can, scope_by_site
from core.audit import audit

from . import services
from .forms import CoolingUnitForm, PowerFeedForm, RackForm, RackItemForm, RoomForm
from .models import (Face, CoolingKind, CoolingStatus, CoolingUnit, ItemKind, PowerFeed, Rack, RackStatus,
                     Room)

COLORS = {"ok": "#0f7a55", "warn": "#e08a00", "crit": "#d92d20", "free": "#d6cfc1", "primary": "#c23f14",
          "purple": "#7a4fd6", "teal": "#0e8a7a", "stone": "#a8a398"}
UNRACKED_TYPES = ("switch", "router", "firewall", "load_balancer", "physical_server")


# ------------------------------------------------------------------ helpers
def _per_page(request, default=10):
    try:
        return max(5, min(100, int(request.GET.get("per", default))))
    except (TypeError, ValueError):
        return default


def _query(request, *drop):
    q = request.GET.copy()
    for k in ("page",) + drop:
        q.pop(k, None)
    return q


def _next(request, fallback):
    nxt = request.POST.get("next") or request.GET.get("next") or ""
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return nxt
    return fallback


def _int(value, default=None):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return n if -2**62 < n < 2**62 else default   # a larger id would overflow the database driver (HTTP 500)


def csv_safe(value):
    """Spreadsheet-safe CSV cell: text starting with = + - @ (or a control character) is not a formula."""
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return value
    s = str(value)
    if s and s[0] in "=+-@\t\r":
        return "'" + s
    return s


def _csv_response(filename, header, rows):
    resp = HttpResponse(content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    w = csv.writer(resp)
    w.writerow(header)
    for r in rows:
        w.writerow([csv_safe(v) for v in r])
    return resp


def _r(v, digits=3):
    return "" if v is None else round(v, digits)


def _perms(request):
    u = request.user
    return {"can_operate": can(u, "operate"), "can_admin": can(u, "admin")}


def _need(request, perm, site):
    if not services.can_manage(request.user, perm, site):
        raise PermissionDenied(f"You cannot change records for “{site}”.")


def _site_filter(request):
    site = (request.GET.get("site") or "").strip()
    return site if site in services.visible_site_names(request.user) or site == services.GLOBAL_SITE else ""


def _prom():
    return {"prom_configured": prometheus.configured()}


def _base(request, section, **extra):
    return {**_perms(request), **_prom(), "section": section, "sites": services.visible_site_names(request.user),
            "site": _site_filter(request), **extra}


def _reevaluate(*rooms):
    """Re-check power/cooling alert state for the rooms touched by a change (emits on state change only)."""
    ids = {getattr(r, "pk", r) for r in rooms if r is not None}
    if ids:
        services.evaluate_alerts(Room.objects.filter(pk__in=ids))


def _changes(before, obj, fields):
    return {f: [str(before[f]), str(getattr(obj, f))] for f in fields if before[f] != getattr(obj, f)}


def _form_page(request, form, *, title, subtitle, icon, back, crumbs, section, obj=None, delete_url=None,
               delete_text="", status=200, extra=None):
    nxt = _next(request, "")
    ctx = {**_base(request, section), "form": form, "title": title,
           "subtitle": subtitle, "icon": icon, "back": nxt or back, "next_url": nxt, "crumbs": crumbs, "obj": obj,
           "delete_url": delete_url, "delete_text": delete_text, **(extra or {})}
    return render(request, "dcim/form.html", ctx, status=status)


def _summaries(request, site=""):
    rooms = services.rooms_for(request.user)
    if site:
        rooms = rooms.filter(site=site)
    return services.room_summaries(rooms)


# ------------------------------------------------------------------ overview
@require_http_methods(["GET"])
def overview(request):
    if not can(request.user, "view"):
        raise PermissionDenied
    ctx = _base(request, "overview")
    rooms = _summaries(request, ctx["site"])
    t = services.totals(rooms)
    racks = [r for room in rooms for r in room.rack_list if services.counts_toward_capacity(r)]
    room_by_id = {room.pk: room for room in rooms}
    for r in racks:
        r.room_obj = room_by_id.get(r.room_id)
    top = sorted(racks, key=lambda r: (-r.worst_pct, r.name))[:10]
    space_donut = {"segments": [
        {"label": "Used U", "value": t["u_used"], "color": COLORS["primary"]},
        {"label": "Free U", "value": max(0, t["u_total"] - t["u_used"]), "color": COLORS["free"]}],
        "center": {"value": f"{t['u_pct']:.0f}%" if t["u_pct"] is not None else "–", "label": "U used"}}
    chart_rooms = [room for room in rooms if room.load_kw is not None or room.cooling_capacity_kw is not None
                   or room.power_budget_kw is not None][:12]
    power_chart = {"labels": [room.name[:14] for room in chart_rooms],
                   "tipLabels": [f"{room.name} · {room.site}" for room in chart_rooms],
                   "unit": "kW", "stacked": False,
                   "series": [
                       {"name": "IT load", "color": COLORS["primary"], "values": [room.load_kw for room in chart_rooms]},
                       {"name": "Power budget", "color": COLORS["stone"],
                        "values": [room.power_budget_kw for room in chart_rooms]},
                       {"name": "Active cooling", "color": COLORS["teal"],
                        "values": [room.cooling_capacity_kw for room in chart_rooms]}]}
    Event = apps.get_model("core", "Event")
    events = scope_by_site(Event.objects.filter(tenant_key=services.tenant(), source="dcim"), request.user)
    if ctx["site"]:
        events = events.filter(site=ctx["site"])
    unracked, unracked_count = _unracked(request, ctx["site"])
    return render(request, "dcim/overview.html", {
        **ctx, "rooms": rooms, "t": t, "top": top, "space_donut": space_donut, "power_chart": power_chart,
        "has_power_chart": bool(chart_rooms), "events": events.order_by("-occurred_at")[:6],
        "unracked": unracked, "unracked_count": unracked_count,
        "room_rows": sorted(rooms, key=lambda r: (r.headroom_kw is None, r.headroom_kw or 0))[:8],
    })


def _unracked(request, site):
    """Physical network devices visible to the user that are not mounted in any rack yet."""
    try:
        Device = apps.get_model("network", "Device")
    except LookupError:
        return [], 0
    qs = scope_by_site(Device.objects.filter(tenant_key=services.tenant(), device_type__in=UNRACKED_TYPES),
                       request.user).filter(rack_items__isnull=True)
    if site:
        qs = qs.filter(site=site)
    return list(qs.order_by("name")[:6]), qs.count()


# ------------------------------------------------------------------ rooms & racks
def _filtered_rooms(request, site):
    g = request.GET
    rooms = services.rooms_for(request.user)
    if site:
        rooms = rooms.filter(site=site)
    room_id = _int(g.get("room"))
    if room_id:
        rooms = rooms.filter(pk=room_id)
    q = (g.get("q") or "").strip()
    if q:
        rooms = rooms.filter(Q(name__icontains=q) | Q(floor__icontains=q) | Q(racks__name__icontains=q)).distinct()
    return rooms, q


@require_http_methods(["GET"])
def racks(request):
    if not can(request.user, "view"):
        raise PermissionDenied
    ctx = _base(request, "racks")
    rooms_qs, q = _filtered_rooms(request, ctx["site"])
    status = request.GET.get("status") if request.GET.get("status") in RackStatus.values else ""
    page = Paginator(rooms_qs.order_by("site", "name"), _per_page(request)).get_page(request.GET.get("page"))
    rooms = services.room_summaries(list(page.object_list))
    u = request.user
    for room in rooms:
        shown = room.rack_list
        if q and q.lower() not in room.name.lower() and q.lower() not in room.floor.lower():
            shown = [r for r in shown if q.lower() in r.name.lower()]
        if status:
            shown = [r for r in shown if r.status == status]
        room.shown = sorted(shown, key=lambda r: r.name.lower())
        room.can_manage = services.can_manage(u, "operate", room.site)
        room.can_delete = services.can_manage(u, "admin", room.site)
    all_rooms = services.rooms_for(u)
    if ctx["site"]:
        all_rooms = all_rooms.filter(site=ctx["site"])
    return render(request, "dcim/racks.html", {
        **ctx, "page": page, "rooms": rooms, "query": _query(request), "statuses": RackStatus.choices,
        "status": status, "q": q, "room_filter": _int(request.GET.get("room")),
        "room_count": all_rooms.count(),
        "rack_count": services.racks_for(u).filter(room__in=all_rooms).count(),
        "can_add_room": bool(services.manageable_sites(u)) and ctx["can_operate"],
    })


def _rack(request, pk):
    return get_object_or_404(services.racks_for(request.user), pk=pk)


def elevation(rack, items, face):
    """Slots for one face, top (highest U) first: items as blocks spanning their units, free units one by one."""
    shown = sorted((it for it in items if it.face in (face, Face.BOTH)), key=lambda it: -it.u_position)
    by_top = {}
    for it in shown:
        by_top[min(it.u_top, rack.u_height)] = it
    slots, u = [], rack.u_height
    while u >= 1:
        it = by_top.get(u)
        if it is not None and it.u_position <= u:
            span = u - max(1, it.u_position) + 1
            slots.append({"item": it, "top": u, "bottom": max(1, it.u_position), "span": span,
                          "row": rack.u_height - u + 1})
            u -= span
        else:
            slots.append({"item": None, "top": u, "bottom": u, "span": 1, "row": rack.u_height - u + 1})
            u -= 1
    return slots


@require_http_methods(["GET"])
def rack_detail(request, pk):
    if not can(request.user, "view"):
        raise PermissionDenied
    rack = _rack(request, pk)
    items = list(rack.items.select_related("device", "ci").order_by("-u_position"))
    feeds = list(rack.feeds.order_by("name"))
    services.summarize_rack(rack, items, feeds)
    room = services.summarize_room(rack.room, services.rack_summaries(rack.room.racks.all()),
                                   list(rack.room.cooling_units.all()))
    can_manage = services.can_manage(request.user, "operate", rack.site)
    Event = apps.get_model("core", "Event")
    events = Event.objects.filter(tenant_key=services.tenant(), source="dcim",
                                  fingerprint=f"dcim:rack:{rack.pk}:power").order_by("-occurred_at")[:5]
    return render(request, "dcim/rack_detail.html", {
        **_base(request, "racks"), "rack": rack, "room": room, "items": items, "feeds": feeds,
        "faces": [("Front", Face.FRONT.value, elevation(rack, items, Face.FRONT)),
                  ("Rear", Face.REAR.value, elevation(rack, items, Face.REAR))],
        "unums": list(range(rack.u_height, 0, -1)),
        "can_manage": can_manage, "can_delete": services.can_manage(request.user, "admin", rack.site),
        "events": events, "kinds": ItemKind.choices,
    })


# ------------------------------------------------------------------ room CRUD
ROOM_FIELDS = ["site", "name", "floor", "description"]


@require_http_methods(["GET", "POST"])
def room_create(request):
    if not can(request.user, "operate"):
        raise PermissionDenied
    sites = services.manageable_sites(request.user)
    if not sites:
        raise PermissionDenied
    initial = {"site": _site_filter(request) if _site_filter(request) in sites else sites[0]}
    form = RoomForm(request.POST or None, user=request.user, initial=initial)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        _need(request, "operate", obj.site)
        obj.tenant_key = services.tenant()
        obj.save()
        audit(request, "create", obj, site=obj.site, floor=obj.floor)
        messages.success(request, f"Added room “{obj.name}”.")
        return redirect(obj.get_absolute_url())
    return _form_page(request, form, title="Add room", subtitle="A data hall, server room or cage that holds racks.",
                      icon="grid", back=reverse("dcim:racks"), crumbs=[("Rooms & Racks", reverse("dcim:racks"))],
                      status=400 if request.method == "POST" else 200, section="racks")


@require_http_methods(["GET", "POST"])
def room_edit(request, pk):
    if not can(request.user, "operate"):
        raise PermissionDenied
    room = get_object_or_404(services.rooms_for(request.user), pk=pk)
    _need(request, "operate", room.site)
    before = {f: getattr(room, f) for f in ROOM_FIELDS}
    form = RoomForm(request.POST or None, instance=room, user=request.user)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        _need(request, "operate", obj.site)          # moving: the target site too
        with transaction.atomic():
            obj.save()
            if before["site"] != obj.site:
                services.sync_room_site(obj)
        audit(request, "update", obj, changed=_changes(before, obj, ROOM_FIELDS))
        messages.success(request, f"Saved room “{obj.name}”.")
        return redirect(obj.get_absolute_url())
    return _form_page(request, form, title=f"Edit room “{room.name}”", subtitle=f"{room.site}", icon="grid",
                      back=room.get_absolute_url(), crumbs=[("Rooms & Racks", reverse("dcim:racks"))], obj=room,
                      delete_url=reverse("dcim:room_delete", args=[room.pk])
                      if services.can_manage(request.user, "admin", room.site) else None,
                      delete_text=f"Delete the room “{room.name}” and its cooling units?",
                      status=400 if request.method == "POST" else 200, section="racks")


@require_POST
def room_delete(request, pk):
    if not can(request.user, "admin"):
        raise PermissionDenied
    room = get_object_or_404(services.rooms_for(request.user), pk=pk)
    _need(request, "admin", room.site)
    n = room.racks.count()
    if n:
        messages.error(request, f"“{room.name}” still holds {n} rack(s). Move or delete them first.")
        return redirect(room.get_absolute_url())
    units = room.cooling_units.count()
    audit(request, "delete", room, name=room.name, site=room.site, cooling_units=units)
    name = room.name
    room.delete()
    messages.success(request, f"Deleted room “{name}”" + (f" and {units} cooling unit(s)." if units else "."))
    return redirect("dcim:racks")


# ------------------------------------------------------------------ rack CRUD
RACK_FIELDS = ["room", "name", "u_height", "status", "max_power_kw", "max_cooling_kw", "notes"]


@require_http_methods(["GET", "POST"])
def rack_create(request):
    if not can(request.user, "operate"):
        raise PermissionDenied
    initial = {}
    room_id = _int(request.GET.get("room"))
    if room_id:
        initial["room"] = room_id
    form = RackForm(request.POST or None, user=request.user, initial=initial)
    if not form.fields["room"].queryset.exists():
        messages.info(request, "Add a room first: racks belong to a room.")
        return redirect("dcim:room_create") if services.manageable_sites(request.user) else redirect("dcim:racks")
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        _need(request, "operate", obj.room.site)
        obj.tenant_key = services.tenant()
        obj.save()
        audit(request, "create", obj, room=obj.room.name, site=obj.site, u_height=obj.u_height,
              max_power_kw=obj.max_power_kw)
        messages.success(request, f"Added rack “{obj.name}”.")
        return redirect(obj.get_absolute_url())
    return _form_page(request, form, title="Add rack", subtitle="Racks hold items; power feeds attach to racks.",
                      icon="server", back=reverse("dcim:racks"), crumbs=[("Rooms & Racks", reverse("dcim:racks"))],
                      status=400 if request.method == "POST" else 200, section="racks")


@require_http_methods(["GET", "POST"])
def rack_edit(request, pk):
    if not can(request.user, "operate"):
        raise PermissionDenied
    rack = _rack(request, pk)
    _need(request, "operate", rack.site)
    before = {f: getattr(rack, f) for f in RACK_FIELDS}
    old_room = rack.room
    form = RackForm(request.POST or None, instance=rack, user=request.user)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        _need(request, "operate", obj.room.site)     # moving: the target site too
        obj.save()
        audit(request, "update", obj, changed=_changes(before, obj, RACK_FIELDS))
        _reevaluate(old_room, obj.room)
        messages.success(request, f"Saved rack “{obj.name}”.")
        return redirect(obj.get_absolute_url())
    return _form_page(request, form, title=f"Edit rack “{rack.name}”", subtitle=f"{rack.room.name} · {rack.site}",
                      icon="server", back=rack.get_absolute_url(),
                      crumbs=[("Rooms & Racks", reverse("dcim:racks")), (rack.name, rack.get_absolute_url())], obj=rack,
                      delete_url=reverse("dcim:rack_delete", args=[rack.pk])
                      if services.can_manage(request.user, "admin", rack.site) else None,
                      delete_text=f"Delete rack “{rack.name}” with its items and power feeds?",
                      status=400 if request.method == "POST" else 200, section="racks")


@require_POST
def rack_delete(request, pk):
    if not can(request.user, "admin"):
        raise PermissionDenied
    rack = _rack(request, pk)
    _need(request, "admin", rack.site)
    room = rack.room
    audit(request, "delete", rack, name=rack.name, room=room.name, site=rack.site, items=rack.items.count(),
          feeds=rack.feeds.count())
    name = rack.name
    rack.delete()
    _reevaluate(room)
    messages.success(request, f"Deleted rack “{name}” with its items and feeds.")
    return redirect(room.get_absolute_url())


# ------------------------------------------------------------------ rack items
ITEM_FIELDS = ["name", "kind", "u_position", "u_height", "face", "device", "ci", "power_draw_w", "notes"]


def _item_page(request, form, rack, item=None, status=200):
    return _form_page(
        request, form, title=f"Edit “{item.name}”" if item else f"Add item to {rack.name}",
        subtitle=f"{rack.room.name} · {rack.site} · {rack.u_height}U, U1 at the bottom", icon="server",
        back=rack.get_absolute_url(),
        crumbs=[("Rooms & Racks", reverse("dcim:racks")), (rack.name, rack.get_absolute_url())], obj=item,
        delete_url=reverse("dcim:item_delete", args=[item.pk])
        if item and services.can_manage(request.user, "admin", rack.site) else None,
        delete_text=f"Remove “{item.name}” from {rack.name}?" if item else "", status=status,
        section="racks", extra={"rack": rack})


@require_http_methods(["GET", "POST"])
def item_create(request, pk):
    if not can(request.user, "operate"):
        raise PermissionDenied
    rack = _rack(request, pk)
    _need(request, "operate", rack.site)
    if request.method == "POST":
        with transaction.atomic():
            rack = Rack.objects.select_for_update().select_related("room").get(pk=rack.pk)  # serialise placements
            form = RackItemForm(request.POST, rack=rack, user=request.user)
            if form.is_valid():
                obj = form.save(commit=False)
                obj.tenant_key = services.tenant()
                obj.rack = rack
                obj.save()
                audit(request, "create", obj, rack=rack.name, site=rack.site, u=obj.u_label, face=obj.face,
                      kind=obj.kind, device=str(obj.device or ""), ci=str(obj.ci or ""))
            else:
                obj = None
        if obj is not None:
            _reevaluate(rack.room)
            messages.success(request, f"Added “{obj.name}” at {obj.u_label} ({obj.get_face_display().lower()}).")
            return redirect(rack.get_absolute_url())
        return _item_page(request, form, rack, status=400)
    initial = {}
    u = _int(request.GET.get("u"))
    if u and 1 <= u <= rack.u_height:
        initial["u_position"] = u
    if request.GET.get("face") in Face.values:
        initial["face"] = request.GET["face"]
    return _item_page(request, RackItemForm(rack=rack, user=request.user, initial=initial), rack)


def _item(request, pk):
    return get_object_or_404(services.items_for(request.user).select_related("rack", "rack__room"), pk=pk)


@require_http_methods(["GET", "POST"])
def item_edit(request, pk):
    if not can(request.user, "operate"):
        raise PermissionDenied
    item = _item(request, pk)
    rack = item.rack
    _need(request, "operate", rack.site)
    if request.method == "POST":
        before = {f: getattr(item, f) for f in ITEM_FIELDS}
        with transaction.atomic():
            rack = Rack.objects.select_for_update().select_related("room").get(pk=rack.pk)
            form = RackItemForm(request.POST, instance=item, rack=rack, user=request.user)
            obj = form.save() if form.is_valid() else None
            if obj is not None:
                audit(request, "update", obj, rack=rack.name, changed=_changes(before, obj, ITEM_FIELDS))
        if obj is not None:
            _reevaluate(rack.room)
            messages.success(request, f"Saved “{obj.name}” ({obj.u_label}).")
            return redirect(rack.get_absolute_url())
        return _item_page(request, form, rack, item, status=400)
    return _item_page(request, RackItemForm(instance=item, rack=rack, user=request.user), rack, item)


@require_POST
def item_delete(request, pk):
    if not can(request.user, "admin"):
        raise PermissionDenied
    item = _item(request, pk)
    rack = item.rack
    _need(request, "admin", rack.site)
    audit(request, "delete", item, name=item.name, rack=rack.name, site=rack.site, u=item.u_label, face=item.face)
    name = item.name
    item.delete()
    _reevaluate(rack.room)
    messages.success(request, f"Removed “{name}” from {rack.name}.")
    return redirect(rack.get_absolute_url())


# ------------------------------------------------------------------ power
@require_http_methods(["GET"])
def power(request):
    if not can(request.user, "view"):
        raise PermissionDenied
    ctx = _base(request, "power")
    rooms = _summaries(request, ctx["site"])
    feeds = [f for room in rooms for r in room.rack_list for f in r.feed_list]
    q = (request.GET.get("q") or "").strip().lower()
    u = request.user
    sites = {}
    for room in rooms:
        rows = []
        for r in sorted(room.rack_list, key=lambda x: x.name.lower()):
            if q and q not in r.name.lower() and q not in room.name.lower() and not any(q in f.name.lower() for f in r.feed_list):
                continue
            r.can_manage = services.can_manage(u, "operate", r.site)
            rows.append(r)
        room.power_rows = rows
        if rows or not q:
            sites.setdefault(room.site, []).append(room)
    site_groups = []
    for name in sorted(sites, key=lambda s: (s != services.GLOBAL_SITE, s.lower())):
        rs = sites[name]
        loads = [r.load_kw for r in rs if r.load_kw is not None]
        caps = [r.feed_capacity_kw for r in rs if r.feed_capacity_kw]
        budgets = [r.power_budget_kw for r in rs if r.power_budget_kw]
        site_groups.append({"name": name, "rooms": rs, "load_kw": sum(loads) if loads else None,
                            "feed_capacity_kw": sum(caps) if caps else None,
                            "budget_kw": sum(budgets) if budgets else None})
    measured = [f for f in feeds if f.load_kw is not None]
    cap = sum(f.capacity_kw for f in feeds)
    load = sum(f.load_kw for f in measured) if measured else None
    with_query = [f for f in feeds if f.prometheus_query]
    errors = [f for f in with_query if f.last_error]
    last_read = max((f.last_read_at for f in with_query if f.last_read_at), default=None)
    hot_feeds = [f for f in measured if f.load_pct is not None and f.load_pct >= services.HOT_PCT]
    return render(request, "dcim/power.html", {
        **ctx, "site_groups": site_groups, "feeds": feeds, "feed_count": len(feeds), "measured_count": len(measured),
        "capacity_kw": cap if feeds else None, "load_kw": load,
        "headroom_kw": (cap - load) if feeds and load is not None else None,
        "load_pct": 100.0 * load / cap if load is not None and cap else None,
        "query_count": len(with_query), "live_count": sum(1 for f in feeds if f.source == "live"),
        "errors": errors, "last_read": last_read, "hot_feeds": hot_feeds, "q": request.GET.get("q", ""),
        "rack_count": sum(len(room.rack_list) for room in rooms),
        "estimated_racks": [r for room in rooms for r in room.rack_list if r.load_source == "estimated"],
        "can_add_feed": ctx["can_operate"] and any(services.can_manage(u, "operate", room.site) for room in rooms
                                                   if room.rack_list),
    })


FEED_FIELDS = ["rack", "name", "kind", "capacity_kw", "measured_kw", "prometheus_query", "notes"]


def _feed_page(request, form, feed=None, status=200):
    return _form_page(
        request, form, title=f"Edit feed “{feed.name}”" if feed else "Add power feed",
        subtitle="Capacity and load in kW. A Prometheus query (read every 5 minutes) takes precedence over the manual value.",
        icon="zap", back=reverse("dcim:power"), crumbs=[("Power", reverse("dcim:power"))], obj=feed,
        delete_url=reverse("dcim:feed_delete", args=[feed.pk])
        if feed and services.can_manage(request.user, "admin", feed.rack.site) else None,
        delete_text=f"Delete the feed “{feed.name}”?" if feed else "", status=status,
        section="power", extra={"show_prom_note": True})


def _after_feed_save(request, feed, query_changed):
    msg = f"Saved feed “{feed.name}”."
    if feed.prometheus_query and query_changed:
        if prometheus.configured():
            ok = services.read_feed(feed)
            msg += f" Live reading: {feed.last_value:.3f} kW." if ok else f" Live reading failed: {feed.last_error}"
        else:
            msg += " Prometheus is not connected (set LINKEDEYE_PROMETHEUS_URL), so the query is not read yet."
            # the value of the old query must not keep showing as live
            PowerFeed.objects.filter(pk=feed.pk).update(last_value=None, last_error="", last_read_at=None)
    elif not feed.prometheus_query and feed.last_value is not None:
        PowerFeed.objects.filter(pk=feed.pk).update(last_value=None, last_error="", last_read_at=None)
    _reevaluate(feed.rack.room)
    messages.success(request, msg)


@require_http_methods(["GET", "POST"])
def feed_create(request):
    if not can(request.user, "operate"):
        raise PermissionDenied
    initial = {}
    rack_id = _int(request.GET.get("rack"))
    if rack_id:
        initial["rack"] = rack_id
    form = PowerFeedForm(request.POST or None, user=request.user, initial=initial)
    if not form.fields["rack"].queryset.exists():
        messages.info(request, "Add a rack first: power feeds belong to a rack.")
        return redirect("dcim:racks")
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        _need(request, "operate", obj.rack.site)
        obj.tenant_key = services.tenant()
        obj.save()
        audit(request, "create", obj, rack=obj.rack.name, site=obj.rack.site, capacity_kw=obj.capacity_kw,
              measured_kw=obj.measured_kw, prometheus_query=obj.prometheus_query)
        _after_feed_save(request, obj, True)
        return redirect(_next(request, reverse("dcim:power")))
    return _feed_page(request, form, status=400 if request.method == "POST" else 200)


def _feed(request, pk):
    return get_object_or_404(services.feeds_for(request.user).select_related("rack", "rack__room"), pk=pk)


@require_http_methods(["GET", "POST"])
def feed_edit(request, pk):
    if not can(request.user, "operate"):
        raise PermissionDenied
    feed = _feed(request, pk)
    _need(request, "operate", feed.rack.site)
    before = {f: getattr(feed, f) for f in FEED_FIELDS}
    old_room = feed.rack.room
    form = PowerFeedForm(request.POST or None, instance=feed, user=request.user)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        _need(request, "operate", obj.rack.site)
        obj.save()
        changed = _changes(before, obj, FEED_FIELDS)
        audit(request, "update", obj, changed=changed)
        _after_feed_save(request, obj, "prometheus_query" in changed)
        _reevaluate(old_room)
        return redirect(_next(request, reverse("dcim:power")))
    return _feed_page(request, form, feed, status=400 if request.method == "POST" else 200)


@require_POST
def feed_delete(request, pk):
    if not can(request.user, "admin"):
        raise PermissionDenied
    feed = _feed(request, pk)
    _need(request, "admin", feed.rack.site)
    room = feed.rack.room
    audit(request, "delete", feed, name=feed.name, rack=feed.rack.name, site=feed.rack.site)
    name = feed.name
    feed.delete()
    _reevaluate(room)
    messages.success(request, f"Deleted feed “{name}”.")
    return redirect(_next(request, reverse("dcim:power")))


@require_POST
def readings_run(request):
    """Read Prometheus now for the feeds and cooling units the user may operate."""
    if not can(request.user, "operate"):
        raise PermissionDenied
    fallback = reverse("dcim:power")
    if not prometheus.configured():
        messages.warning(request, "Prometheus is not connected (set LINKEDEYE_PROMETHEUS_URL); nothing was read.")
        return redirect(_next(request, fallback))
    u = request.user
    sites = [s for s in set(services.feeds_for(u).values_list("rack__site", flat=True))
             | set(services.cooling_for(u).values_list("site", flat=True)) if services.can_manage(u, "operate", s)]
    feeds = services.feeds_for(u).filter(rack__site__in=sites)
    units = services.cooling_for(u).filter(site__in=sites)
    ok, failed = services.read_all(feeds, units)
    events = services.evaluate_alerts(services.rooms_for(u).filter(site__in=sites))
    audit(request, "run", None, object_type="dcim.readings", ok=ok, failed=failed, events=events)
    if ok + failed == 0:
        messages.info(request, "No power feeds or cooling units with a Prometheus query to read.")
    else:
        (messages.success if not failed else messages.warning)(
            request, f"Read {ok + failed} value set(s): {ok} ok, {failed} failed.")
    return redirect(_next(request, fallback))


# ------------------------------------------------------------------ cooling
@require_http_methods(["GET"])
def cooling(request):
    if not can(request.user, "view"):
        raise PermissionDenied
    ctx = _base(request, "cooling")
    rooms = _summaries(request, ctx["site"])
    u = request.user
    for room in rooms:
        room.can_manage = services.can_manage(u, "operate", room.site)
        room.unit_list = sorted(room.unit_list, key=lambda x: x.name.lower())
    units = [x for room in rooms for x in room.unit_list]
    active_cap = [room.cooling_capacity_kw for room in rooms if room.cooling_capacity_kw is not None]
    deltas = [x.delta_t for x in units if x.delta_t is not None and x.status == CoolingStatus.ACTIVE]
    supplies = [x.supply_c for x in units if x.supply_c is not None and x.status == CoolingStatus.ACTIVE]
    with_query = [x for x in units if x.has_queries]
    rows = sorted(rooms, key=lambda r: (r.headroom_kw is None, r.headroom_kw if r.headroom_kw is not None else 0,
                                        r.name.lower()))
    return render(request, "dcim/cooling.html", {
        **ctx, "rooms": rows, "units": units, "unit_count": len(units),
        "active_count": sum(1 for x in units if x.status == CoolingStatus.ACTIVE),
        "capacity_kw": sum(active_cap) if active_cap else None,
        "load_kw": sum(r.load_kw for r in rooms if r.load_kw is not None) if any(
            r.load_kw is not None for r in rooms) else None,
        "headroom_kw": sum(r.headroom_kw for r in rooms if r.headroom_kw is not None) if active_cap else None,
        "negative": [r for r in rooms if r.headroom_kw is not None and r.headroom_kw < 0],
        "uncooled": [r for r in rooms if r.cooling_capacity_kw is None and r.load_kw],
        "avg_delta": sum(deltas) / len(deltas) if deltas else None,
        "avg_supply": sum(supplies) / len(supplies) if supplies else None,
        "query_count": len(with_query), "errors": [x for x in with_query if x.last_error],
        "can_add_unit": ctx["can_operate"] and any(r.can_manage for r in rooms),
        "kinds": CoolingKind.choices,
    })


UNIT_FIELDS = ["room", "name", "kind", "status", "capacity_kw", "supply_temp_c", "return_temp_c", "supply_query",
               "return_query", "notes"]


def _unit_page(request, form, unit=None, status=200):
    return _form_page(
        request, form, title=f"Edit “{unit.name}”" if unit else "Add cooling unit",
        subtitle="Active units count towards the room's cooling capacity; temperatures are manual or read from Prometheus.",
        icon="sun", back=reverse("dcim:cooling"), crumbs=[("Cooling", reverse("dcim:cooling"))], obj=unit,
        delete_url=reverse("dcim:cooling_delete", args=[unit.pk])
        if unit and services.can_manage(request.user, "admin", unit.site) else None,
        delete_text=f"Delete the cooling unit “{unit.name}”?" if unit else "", status=status,
        section="cooling", extra={"show_prom_note": True})


def _after_unit_save(request, unit, query_changed):
    msg = f"Saved cooling unit “{unit.name}”."
    if unit.has_queries and query_changed:
        if prometheus.configured():
            ok = services.read_unit(unit)
            msg += " Live temperatures read." if ok else f" Live reading failed: {unit.last_error}"
        else:
            msg += " Prometheus is not connected (set LINKEDEYE_PROMETHEUS_URL), so the queries are not read yet."
            CoolingUnit.objects.filter(pk=unit.pk).update(last_supply_c=None, last_return_c=None, last_error="",
                                                          last_read_at=None)
    _reevaluate(unit.room)
    messages.success(request, msg)


@require_http_methods(["GET", "POST"])
def cooling_create(request):
    if not can(request.user, "operate"):
        raise PermissionDenied
    initial = {}
    room_id = _int(request.GET.get("room"))
    if room_id:
        initial["room"] = room_id
    form = CoolingUnitForm(request.POST or None, user=request.user, initial=initial)
    if not form.fields["room"].queryset.exists():
        messages.info(request, "Add a room first: cooling units belong to a room.")
        return redirect("dcim:room_create") if services.manageable_sites(request.user) else redirect("dcim:cooling")
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        _need(request, "operate", obj.room.site)
        obj.tenant_key = services.tenant()
        obj.save()
        audit(request, "create", obj, room=obj.room.name, site=obj.site, capacity_kw=obj.capacity_kw, status=obj.status)
        _after_unit_save(request, obj, True)
        return redirect(_next(request, reverse("dcim:cooling")))
    return _unit_page(request, form, status=400 if request.method == "POST" else 200)


def _unit(request, pk):
    return get_object_or_404(services.cooling_for(request.user), pk=pk)


@require_http_methods(["GET", "POST"])
def cooling_edit(request, pk):
    if not can(request.user, "operate"):
        raise PermissionDenied
    unit = _unit(request, pk)
    _need(request, "operate", unit.site)
    before = {f: getattr(unit, f) for f in UNIT_FIELDS}
    old_room = unit.room
    form = CoolingUnitForm(request.POST or None, instance=unit, user=request.user)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        _need(request, "operate", obj.room.site)
        obj.save()
        changed = _changes(before, obj, UNIT_FIELDS)
        audit(request, "update", obj, changed=changed)
        if not obj.has_queries and (obj.last_supply_c is not None or obj.last_return_c is not None):
            CoolingUnit.objects.filter(pk=obj.pk).update(last_supply_c=None, last_return_c=None, last_error="")
        _after_unit_save(request, obj, "supply_query" in changed or "return_query" in changed)
        _reevaluate(old_room)
        return redirect(_next(request, reverse("dcim:cooling")))
    return _unit_page(request, form, unit, status=400 if request.method == "POST" else 200)


@require_POST
def cooling_delete(request, pk):
    if not can(request.user, "admin"):
        raise PermissionDenied
    unit = _unit(request, pk)
    _need(request, "admin", unit.site)
    room = unit.room
    audit(request, "delete", unit, name=unit.name, room=room.name, site=unit.site, capacity_kw=unit.capacity_kw)
    name = unit.name
    unit.delete()
    _reevaluate(room)
    messages.success(request, f"Deleted cooling unit “{name}”.")
    return redirect(_next(request, reverse("dcim:cooling")))


# ------------------------------------------------------------------ CSV exports
@require_http_methods(["GET"])
def racks_export(request):
    if not can(request.user, "view"):
        raise PermissionDenied
    site = _site_filter(request)
    rooms_qs, q = _filtered_rooms(request, site)
    rooms = services.room_summaries(rooms_qs.order_by("site", "name"))
    status = request.GET.get("status") if request.GET.get("status") in RackStatus.values else ""
    rows = []
    for room in rooms:
        for r in sorted(room.rack_list, key=lambda x: x.name.lower()):
            if status and r.status != status:
                continue
            if q and q.lower() not in room.name.lower() and q.lower() not in room.floor.lower() \
                    and q.lower() not in r.name.lower():
                continue
            rows.append([r.site, room.name, room.floor, r.name, r.status, r.u_height, r.u_used, _r(r.space_pct, 1),
                         len(r.item_list), r.max_power_kw, _r(r.load_kw), r.load_source, _r(r.power_pct, 1),
                         _r(r.feed_capacity_kw), r.max_cooling_kw, r.notes])
    return _csv_response("dcim-racks.csv", ["site", "room", "floor", "rack", "status", "u_height", "u_used", "space_pct",
                                            "items", "power_budget_kw", "load_kw", "load_source", "power_pct",
                                            "feed_capacity_kw", "cooling_budget_kw", "notes"], rows)


@require_http_methods(["GET"])
def items_export(request):
    if not can(request.user, "view"):
        raise PermissionDenied
    qs = services.items_for(request.user).select_related("rack", "rack__room", "device", "ci")
    site = _site_filter(request)
    if site:
        qs = qs.filter(rack__site=site)
    rack_id = _int(request.GET.get("rack"))
    if rack_id:
        qs = qs.filter(rack_id=rack_id)
    rows = ([it.rack.site, it.rack.room.name, it.rack.name, it.name, it.kind, it.face, it.u_position, it.u_top,
             it.u_height, it.device.name if it.device else "", it.ci.name if it.ci else "", it.power_draw_w, it.notes]
            for it in qs.order_by("rack__site", "rack__room__name", "rack__name", "-u_position"))
    return _csv_response("dcim-rack-items.csv", ["site", "room", "rack", "item", "kind", "face", "u_bottom", "u_top",
                                                 "u_height", "device", "ci", "power_draw_w", "notes"], rows)


@require_http_methods(["GET"])
def power_export(request):
    if not can(request.user, "view"):
        raise PermissionDenied
    qs = services.feeds_for(request.user).select_related("rack", "rack__room")
    site = _site_filter(request)
    if site:
        qs = qs.filter(rack__site=site)
    rows = ([f.rack.site, f.rack.room.name, f.rack.name, f.name, f.kind, f.capacity_kw, f.measured_kw,
             _r(f.live_kw), _r(f.load_kw), f.source, _r(f.headroom_kw), _r(f.load_pct, 1), f.prometheus_query,
             f.last_read_at.isoformat() if f.last_read_at else "", f.last_error]
            for f in qs.order_by("rack__site", "rack__room__name", "rack__name", "name"))
    return _csv_response("dcim-power-feeds.csv", ["site", "room", "rack", "feed", "kind", "capacity_kw", "manual_kw",
                                                  "live_kw", "load_kw", "source", "headroom_kw", "load_pct",
                                                  "prometheus_query", "last_read_at", "last_error"], rows)

