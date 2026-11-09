import secrets
import time

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.safestring import mark_safe
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_http_methods, require_POST

from core.access import can, require
from core.audit import audit

from . import csvio, example, faces, layout, lldp, services
from . import tiers as tr
from .forms import ImportForm, LinkForm, PlacementForm, duplicate_of
from .models import Link, Medium, Origin, Placement

IMPORT_TTL = 15 * 60
DENSE_PATHS = 120                 # more strokes than this are drawn thinner and fainter
MAX_BULK = 500


# ------------------------------------------------------------------ helpers
def _per_page(request, default=20):
    try:
        return max(5, min(100, int(request.GET.get("per", default))))
    except (TypeError, ValueError):
        return default


def _id(value):
    """A database id from query or form text, or None. ASCII digits only ("²".isdigit() is True but int() refuses it) and
    short enough to fit a 64-bit column, so nothing a user types can raise or overflow."""
    value = (value or "").strip()
    return int(value) if value.isascii() and value.isdigit() and len(value) <= 18 else None


def _safe_url(request, url):
    return bool(url) and url_has_allowed_host_and_scheme(url, allowed_hosts={request.get_host()}, require_https=request.is_secure())


def _next(request, fallback):
    nxt = request.POST.get("next") or request.GET.get("next") or ""
    return nxt if _safe_url(request, nxt) else fallback


def _query(request, drop=(), **set_):
    q = request.GET.copy()
    for k in tuple(drop) + ("page",):
        q.pop(k, None)
    for k, v in set_.items():
        if v in (None, ""):
            q.pop(k, None)
        else:
            q[k] = v
    return q


def _filters(request):
    g = request.GET
    # at most 40 opened tiers / groups: every link on the page repeats this list in its query string
    expand = frozenset([x.strip()[:80] for x in g.get("expand", "")[:4000].split(",") if x.strip()][:40])
    return {
        "site": services.clean_site(request.user, g.get("site")),
        "env": g.get("env") if g.get("env") in ("prod", "uat", "dr", "dev") else "",
        "view": g.get("view") if g.get("view") in ("diagram", "list") else "diagram",
        "lc": g.get("lc") if g.get("lc") in ("fabric", "health", "util") else "fabric",
        "live": g.get("live") != "0",
        "present": g.get("present") == "1",
        "expand": expand,
    }


def _visible_link(request, pk):
    """A link both of whose devices the user may see: anything else is a 404 (no hint that it exists)."""
    return get_object_or_404(services.links_queryset(request.user, request).select_related("a_device", "b_device"), pk=pk)


def _require_link(request, link):
    if not services.can_manage_link(request.user, link):
        raise PermissionDenied


def _perms(request):
    return {"can_edit": can(request.user, "operate"), "can_admin": can(request.user, "admin")}


def _ctx(request, **extra):
    return {**_perms(request), **extra}


TIER_TONE = {"wan": "info", "firewall": "crit", "core": "purple", "spine": "info", "leaf": "ok", "server": "warn", "other": "mute"}


# ------------------------------------------------------------------ the page
def _path_class(p, links, group_colour):
    l = links.get(p.key)
    if l is None:
        return f"lk lk-{p.kind}"
    cls = f"lk lk-{p.kind} st-{l['state']} ut-{l['tone']}"
    if not l["confirmed"]:
        cls += " unconf"
    if p.lazy:
        cls += " lazy"
    if p.kind == "bundle":
        cls += f" gc-{group_colour.get(p.group, 'stone')}"
    return cls


def _group_card(card, fb, nodes):
    members = [fb.by_id[int(i)] for i in card.members]
    states = [services.dev_state(d) for d in members]
    racks = fb.racks.get(card.key, [])
    lines = list(racks[:4]) if len(racks) <= 4 else list(racks[:3]) + [{"name": f"+{len(racks) - 3} more racks", "text": "", "url": ""}]
    lines = [dict(r, y=round(card.foot_y + i * 15 + 11, 1)) for i, r in enumerate(lines)]
    more = None
    if card.more:
        x, y, w, h = card.more
        more = {"x": x, "y": y, "w": w, "h": h, "cx": round(x + w / 2, 1), "cy": round(y + h / 2 + 3.4, 1)}
    tiles = [nodes[t[0]] for t in card.tiles]
    cabs = []
    for cab in card.cabs:
        inside = [t for t in card.tiles if cab.x <= t[1] < cab.x + cab.w and cab.body_y <= t[2] < cab.body_y + cab.body_h]
        tx = round(cab.x + cab.w - 9.5, 1)
        trunk = ""
        mids = [round(t[2] + t[4] / 2, 1) for t in inside]
        if card.more and cab.x <= card.more[0] < cab.x + cab.w and cab.body_y <= card.more[1] < cab.body_y + cab.body_h:
            mids.append(round(card.more[1] + card.more[3] / 2, 1))
        if mids:
            trunk = f"M{tx} {round(cab.body_y + 2, 1)}V{max(mids)}" + "".join(f"M{round(cab.x + layout.CAB_PAD_X + layout.UNIT_W, 1)} {m}H{tx}" for m in mids)
        sh = layout.SLOT_H[1]
        end = cab.body_y + layout.CAB_PAD_Y
        for t in inside:
            end = max(end, t[2] + (layout.SLOT_H[2] if t[5] == "face2" else sh))
        if card.more and cab.x <= card.more[0] < cab.x + cab.w and cab.body_y <= card.more[1] < cab.body_y + cab.body_h:
            end = max(end, card.more[1] + sh)
        room = cab.body_y + cab.body_h - layout.CAB_PAD_Y
        blanks = "".join(f"M{round(cab.x + layout.CAB_PAD_X, 1)} {round(end + i * sh + 1, 1)}h{layout.UNIT_W}v{round(sh - 2, 1)}h-{layout.UNIT_W}z"
                         for i in range(int((room - end + 0.5) // sh)))
        units = int((cab.body_h - 2 * layout.CAB_PAD_Y) // sh)
        ticks = "".join(f"M{round(cab.x + 3.5, 1)} {round(cab.body_y + layout.CAB_PAD_Y + (i + .5) * sh, 1)}h2.5M{round(cab.x + cab.w - 6, 1)} {round(cab.body_y + layout.CAB_PAD_Y + (i + .5) * sh, 1)}h2.5"
                        for i in range(units))
        cabs.append({"x": cab.x, "y": cab.y, "w": cab.w, "h": cab.h, "body_y": cab.body_y, "body_h": cab.body_h,
                     "blanks": blanks, "in_x": round(cab.x + 4.5, 1), "in_w": round(cab.w - 9, 1), "trunk": trunk, "ticks": ticks,
                     "cap_h": layout.CAB_TOP, "foot_y": round(cab.y + cab.h - 3, 1), "foot2_x": round(cab.x + cab.w - 24, 1),
                     "gland_x": round(cab.x + cab.w / 2 - 32, 1), "cx": round(cab.x + cab.w / 2, 1)})
    pill_w = round(min(card.w - 24, services._cells_len(card.label) * 7.6 + 30), 1)
    return {
        "key": card.key, "label": card.label, "colour": card.colour, "x": card.x, "y": card.y, "w": card.w, "h": card.h,
        "cx": round(card.x + card.w / 2, 1), "count": len(members), "up": states.count("up"), "down": states.count("down"),
        "unknown": states.count("unknown") + states.count("disabled"),
        "tiles": tiles, "hidden": card.hidden, "more": more, "foot_y": card.foot_y, "cabs": cabs,
        "racks": lines, "pill_y": card.pill_y, "text_y": round(card.pill_y + 18.6, 1), "count_y": card.count_y,
        "label_fit": services._clip(card.label, int((card.w - 24 - 30) / 7.6)), "pill_w": pill_w,
        "pill_x": round(card.x + card.w / 2 - pill_w / 2, 1),
        "count_text": f"{len(members)} server{'s' if len(members) != 1 else ''} · {states.count('up')} up · {states.count('down')} down",
    }


@require("view")
def overview(request):
    f = _filters(request)
    fb = services.build(request.user, request, f["site"], f["env"])
    base = {**_ctx(request), "f": f, "sites": services.site_choices(request.user, request), "envs": [("prod", "Production"), ("uat", "UAT"), ("dr", "DR"), ("dev", "Development")],
            "views": services.VIEWS, "link_colours": services.LINK_COLOURS, "prom": fb.prom, "kpis": fb.kpis,
            "q_view": {k: _query(request, ["view"], view=k if k != "diagram" else "").urlencode() for k, _l in services.VIEWS},
            "q_lc": {k: _query(request, ["lc"], lc=k if k != "fabric" else "").urlencode() for k, _l in services.LINK_COLOURS},
            "q_live": _query(request, ["live"], live="0" if f["live"] else "").urlencode(),
            "example_site": services.example_site(request.user) if can(request.user, "operate") else None,
            # the "Load example fabric" button is a demo aid: off on a live console unless LINKEDEYE_SHOW_SAMPLE_PAGES=true
            "show_example_loader": getattr(settings, "SHOW_SAMPLE_PAGES", False)}
    if not fb.devices:
        return render(request, "architecture/overview.html", {**base, "empty": True, "hidden_count": len(fb.hidden)})
    chips = services.tier_chips(fb)
    base.update(empty=False, chips=chips, hidden=fb.hidden, no_links=not fb.links)
    if f["view"] == "list":
        return render(request, "architecture/overview.html", {**base, "tables": _tables(request, fb)})
    res = services.diagram(fb, f["expand"])
    fset = faces.FaceSet(services.face_detail(res))
    nodes = {str(d.pk): services.node_view(d, fb, res.nodes[str(d.pk)], fset) for d in fb.devices if str(d.pk) in res.nodes}
    links = {l["key"]: l for l in fb.links}
    group_colour = {c.key: c.colour for c in res.cards}
    loose = [n for i, n in nodes.items() if res.nodes[i].tier != "server" and res.nodes[i].mode != "hidden"]
    paths = [{"key": p.key, "d": p.d, "kind": p.kind, "lazy": p.lazy, "cls": _path_class(p, links, group_colour),
              "hit": p.kind != "sym" and not p.lazy, "lw": (round(1.6 + 2.6 * (links[p.key]["util"] or 0) / 100, 2)
                                                              if p.key in links and links[p.key]["util"] is not None else "")}
             for p in res.paths]
    card_views = [_group_card(c, fb, nodes) for c in res.cards]
    unclassified = [{"d": d, "edit": reverse("architecture:placement_edit", args=[d.pk]) if services.can_manage_device(request.user, d) else "",
                     "state": services.dev_state(d)} for d in fb.devices if fb.placed[d.pk].tier == tr.OTHER]
    base.update({
        "res": res, "bands": [{"b": b, "tone": TIER_TONE[b.tier], "tag": f"{b.label} · {b.count}", "tag_w": round(len(b.label) * 7.6 + 56, 1), "expand_q": _query(request, ["expand"], expand=",".join(sorted(f["expand"] | {b.tier}))).urlencode()}
                              for b in res.bands],
        "nodes": loose, "cards": card_views, "paths": paths, "aggs": res.aggregates, "face_defs": mark_safe(fset.defs()), "dense": len(res.paths) > DENSE_PATHS,
        "notes": res.notes, "cloud": res.cloud, "data": services.page_data(fb), "ignored": res.ignored,
        "expand_group_q": {c.key: _query(request, ["expand"], expand=",".join(sorted(f["expand"] | {c.key}))).urlencode() for c in res.cards},
        "collapse_q": _query(request, ["expand"]).urlencode(), "unclassified": unclassified[:40], "unclassified_total": len(unclassified), "unclassified_more": max(0, len(unclassified) - 40),
        "q_present": _query(request, ["present"], present="" if f["present"] else "1").urlencode(),
        "site_label": f["site"] or "All sites", "generated": fb.now,
        "conf": {"liveUrl": reverse("architecture:live"), "query": _query(request, ["view", "lc", "live", "expand", "present"]).urlencode(),
                 "live": f["live"], "interval": 30},
    })
    return render(request, "architecture/overview.html", base)


def _tables(request, fb):
    degree = {}
    for l in fb.links:
        degree[l["a"]] = degree.get(l["a"], 0) + 1
        degree[l["b"]] = degree.get(l["b"], 0) + 1
    editable = {}
    out = []
    for t in tr.TIERS:
        rows = []
        for d in sorted(fb.devices, key=lambda d: ((fb.placed[d.pk].group or "").lower(), d.name.lower())):
            p = fb.placed[d.pk]
            if p.tier != t:
                continue
            if d.site not in editable:
                editable[d.site] = services.can_manage_device(request.user, d)
            rows.append({"d": d, "p": p, "state": services.dev_state(d), "state_label": services.STATE_LABELS[services.dev_state(d)],
                         "links": degree.get(d.pk, 0), "can_edit": editable[d.site], "tip": services.device_tip(d, fb.now),
                         "rack": fb.rack_of.get(d.pk, "")})
        if rows:
            out.append({"tier": t, "label": tr.TIER_LABELS[t], "tone": TIER_TONE[t], "rows": rows})
    return out


@require("view")
def live(request):
    f = _filters(request)
    fb = services.build(request.user, request, f["site"], f["env"], with_racks=False)
    resp = JsonResponse(services.live_payload(fb))
    resp["Cache-Control"] = "no-store"
    return resp


# ------------------------------------------------------------------ links list
@require("view")
def links(request):
    g = request.GET
    qs = services.links_queryset(request.user, request, services.clean_site(request.user, g.get("site"))).select_related("a_device", "b_device")
    q = g.get("q", "").strip()[:100]
    if q:
        qs = qs.filter(Q(a_device__name__icontains=q) | Q(b_device__name__icontains=q) | Q(a_port__icontains=q)
                       | Q(b_port__icontains=q) | Q(note__icontains=q) | Q(kind__icontains=q))
    if g.get("medium") in Medium.values:
        qs = qs.filter(medium=g["medium"])
    if g.get("origin") in Origin.values:
        qs = qs.filter(origin=g["origin"])
    if g.get("unconfirmed") == "1":
        qs = qs.filter(confirmed=False)
    qs = qs.order_by("a_device__name", "a_port", "b_device__name", "b_port", "pk")
    page = Paginator(qs, _per_page(request)).get_page(g.get("page"))
    rows = list(page)
    from django.utils import timezone
    dev = {}
    for l in rows:
        dev[l.a_device_id], dev[l.b_device_id] = l.a_device, l.b_device
    ifaces = services.fetch_interfaces({l.a_device_id for l in rows if l.a_port} | {l.b_device_id for l in rows if l.b_port})
    now = timezone.now()
    ok_site = {}

    def manageable(device):
        if device.site not in ok_site:
            ok_site[device.site] = services.can_manage_device(request.user, device)
        return ok_site[device.site]

    for l in rows:
        l.can_edit = manageable(l.a_device) and manageable(l.b_device)
        l.v = services.link_view(l, dev, ifaces, now, editable=l.can_edit)
    return render(request, "architecture/links.html", _ctx(
        request, page=page, rows=rows, q=q, query=_query(request), mediums=Medium.choices, origins=Origin.choices,
        f_medium=g.get("medium", ""), f_origin=g.get("origin", ""), f_unconf=g.get("unconfirmed") == "1",
        f_site=services.clean_site(request.user, g.get("site")), sites=services.site_choices(request.user, request),
        total=page.paginator.count, prom=services.prometheus_state(), pager_noun="links"))


@require("view")
def links_export(request):
    qs = services.links_queryset(request.user, request, services.clean_site(request.user, request.GET.get("site"))) \
        .select_related("a_device", "b_device").order_by("a_device__name", "a_port", "b_device__name", "b_port")
    return csvio.export_response(qs)


# ------------------------------------------------------------------ link forms
@require("operate")
@require_http_methods(["GET", "POST"])
def link_form(request, pk=None):
    link = _visible_link(request, pk) if pk else None
    if link is not None:
        _require_link(request, link)
    initial = {}
    if link is None:
        for key, field in (("a", "a_device"), ("b", "b_device")):
            if _id(request.GET.get(key)) is not None:
                initial[field] = _id(request.GET[key])
    before = None if link is None else {"a_port": link.a_port, "b_port": link.b_port, "medium": link.medium, "speed": link.speed_mbps}
    form = LinkForm(request.POST or None, instance=link, initial=initial, user=request.user, request=request)
    if request.method == "POST" and form.is_valid():
        changed = list(form.changed_data)
        try:
            with transaction.atomic():
                obj = form.save()
        except IntegrityError:
            form.add_error(None, "These two ports are already linked.")
        else:
            if link is None:
                audit(request, "create", obj, a=obj.a_device.name, a_port=obj.a_port, b=obj.b_device.name, b_port=obj.b_port,
                      medium=obj.medium, speed_mbps=obj.speed_mbps)
                messages.success(request, f"Added the link {obj}.")
            else:
                audit(request, "update", obj, changed=changed, previous=before)
                messages.success(request, f"Saved the link {obj}.")
            return redirect(_next(request, reverse("architecture:links")))
    return render(request, "architecture/link_form.html", _ctx(
        request, form=form, link=link, title="Edit link" if link else "Add link", back=_next(request, reverse("architecture:links"))))


@require("operate")
@require_POST
def link_delete(request, pk):
    link = _visible_link(request, pk)
    _require_link(request, link)
    label, a, b = str(link), link.a_device.name, link.b_device.name
    audit(request, "delete", link, a=a, a_port=link.a_port, b=b, b_port=link.b_port, medium=link.medium)
    link.delete()
    messages.success(request, f"Deleted the link {label}.")
    return redirect(_next(request, reverse("architecture:links")))


@require("operate")
@require_POST
def links_bulk(request):
    action = request.POST.get("action")
    ids = [i for i in (_id(x) for x in request.POST.getlist("ids")[:MAX_BULK]) if i is not None]
    if action not in ("delete", "confirm") or not ids:
        messages.error(request, "Choose at least one link and an action.")
        return redirect(_next(request, reverse("architecture:links")))
    qs = services.links_queryset(request.user, request).filter(pk__in=ids).select_related("a_device", "b_device")
    done = skipped = 0
    for link in qs:
        if not services.can_manage_link(request.user, link):
            skipped += 1
            continue
        if action == "delete":
            audit(request, "delete", link, a=link.a_device.name, a_port=link.a_port, b=link.b_device.name, b_port=link.b_port, bulk=True)
            link.delete()
        else:
            if link.confirmed:
                continue
            link.confirmed = True
            link.save(update_fields=["confirmed", "updated_at"])
            audit(request, "update", link, changed=["confirmed"], bulk=True)
        done += 1
    verb = "Deleted" if action == "delete" else "Confirmed"
    messages.success(request, f"{verb} {done} link{'s' if done != 1 else ''}." + (f" {skipped} could not be changed by you." if skipped else ""))
    return redirect(_next(request, reverse("architecture:links")))


# ------------------------------------------------------------------ CSV import (preview, then commit)
@require("operate")
@require_http_methods(["GET", "POST"])
def links_import(request):
    form = ImportForm(request.POST or None, request.FILES or None)
    plan = preview = None
    token = ""
    if request.method == "POST":
        if request.POST.get("commit"):
            stash = request.session.get("arch_import") or {}
            fresh = stash and time.time() - stash.get("at", 0) < IMPORT_TTL
            if not fresh or not secrets.compare_digest(str(stash.get("token", "")), request.POST.get("token", "")):
                messages.error(request, "The preview expired. Upload the file again.")
                return redirect(reverse("architecture:links_import"))
            plan = csvio.validate(stash["text"], request.user, request, stash["update"])
            request.session.pop("arch_import", None)
            if not plan.ok:
                preview = plan
                messages.error(request, "The data changed since the preview and no longer validates. Nothing was imported.")
            else:
                created, updated = csvio.commit(plan)
                audit(request, "import", None, object_type="architecture.link", created=len(created), updated=len(updated),
                      links=[l.pk for l in created + updated][:200])
                messages.success(request, f"Imported {len(created)} new link{'s' if len(created) != 1 else ''} and updated {len(updated)}.")
                return redirect(reverse("architecture:links"))
        elif form.is_valid():
            upload = form.cleaned_data["file"]
            if upload is None:
                form.add_error("file", "Choose a CSV file.")
            elif upload.size > csvio.MAX_BYTES:
                form.add_error("file", f"The file is larger than {csvio.MAX_BYTES // 1024 // 1024} MB.")
            else:
                text, problem = csvio.decode(upload.read())
                if text is None:
                    form.add_error("file", problem)
                else:
                    update = form.cleaned_data["update_existing"]
                    preview = csvio.validate(text, request.user, request, update)
                    if preview.ok:
                        token = secrets.token_urlsafe(24)
                        request.session["arch_import"] = {"token": token, "text": text, "update": update, "at": time.time()}
    return render(request, "architecture/import.html", _ctx(request, form=form, preview=preview, token=token,
                                                            columns=csvio.COLUMNS, mediums=Medium.values))


# ------------------------------------------------------------------ LLDP suggestions
@require("operate")
@require_http_methods(["GET", "POST"])
def suggest(request):
    result = None
    if request.method == "POST":
        picked = set(request.POST.getlist("pick")[:MAX_BULK])
        if not picked:
            messages.error(request, "Tick at least one suggestion to accept.")
            return redirect(reverse("architecture:suggest") + "?find=1")
        result = lldp.find(services.device_queryset(request.user, request))
        if result.error:
            messages.error(request, result.error)
            return redirect(reverse("architecture:suggest"))
        made = refused = 0
        by_id = {d.pk: d for d in services.device_queryset(request.user, request)}
        for s in result.suggestions:
            if s.token not in picked:
                continue
            a, b = by_id.get(s.a_id), by_id.get(s.b_id)
            if a is None or b is None or not (services.can_manage_device(request.user, a) and services.can_manage_device(request.user, b)):
                refused += 1
                continue
            if duplicate_of(a.pk, s.a_port, b.pk, s.b_port, services.tenant()) is not None:
                continue
            try:
                with transaction.atomic():
                    link = Link(tenant_key=services.tenant(), a_device=a, b_device=b, a_port=s.a_port, b_port=s.b_port,
                                origin=Origin.LLDP, confirmed=False, medium=Medium.FIBRE)
                    link.save()
            except IntegrityError:
                continue
            audit(request, "create", link, via="lldp", a=a.name, a_port=link.a_port, b=b.name, b_port=link.b_port)
            made += 1
        messages.success(request, f"Added {made} link{'s' if made != 1 else ''} found by LLDP. They are drawn dashed until you confirm them."
                         + (f" {refused} could not be added by you." if refused else ""))
        return redirect(reverse("architecture:links") + "?unconfirmed=1")
    if request.GET.get("find"):
        result = lldp.find(services.device_queryset(request.user, request))
    return render(request, "architecture/suggest.html", _ctx(request, result=result, prom=services.prometheus_state(),
                                                             searched=bool(request.GET.get("find"))))


# ------------------------------------------------------------------ placement overrides
def _device(request, pk):
    device = get_object_or_404(services.device_queryset(request.user, request), pk=pk)
    if not services.can_manage_device(request.user, device):
        raise PermissionDenied
    return device


@require("operate")
@require_http_methods(["GET", "POST"])
def placement_edit(request, pk):
    device = _device(request, pk)
    placement = Placement.objects.filter(tenant_key=services.tenant(), device=device).first()
    form = PlacementForm(request.POST or None, instance=placement or Placement(tenant_key=services.tenant(), device=device))
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        if obj.is_empty:
            if placement is not None:
                placement.delete()
                audit(request, "delete", None, object_type="architecture.placement", object_id=str(device.pk), device=device.name)
            messages.success(request, f"{device.name} is placed automatically again.")
        else:
            obj.tenant_key, obj.device = services.tenant(), device
            obj.save()
            audit(request, "update" if placement else "create", obj, device=device.name, tier=obj.tier, group=obj.group,
                  colour=obj.colour, hidden=obj.hidden)
            messages.success(request, f"Saved the placement of {device.name}.")
        return redirect(_next(request, reverse("architecture:overview")))
    return render(request, "architecture/placement_form.html", _ctx(
        request, form=form, device=device, placement=placement, explain=services.explain(device, request.user),
        back=_next(request, reverse("architecture:overview"))))


@require("operate")
@require_POST
def placement_reset(request, pk):
    device = _device(request, pk)
    n, _ = Placement.objects.filter(tenant_key=services.tenant(), device=device).delete()
    if n:
        audit(request, "delete", None, object_type="architecture.placement", object_id=str(device.pk), device=device.name)
    messages.success(request, f"{device.name} is placed automatically again.")
    return redirect(_next(request, reverse("architecture:overview")))


# ------------------------------------------------------------------ example fabric
@require("operate")
@require_POST
def example_load(request):
    if not getattr(settings, "SHOW_SAMPLE_PAGES", False):   # a live console never invents devices (the button is hidden too)
        raise Http404("The example fabric loader is switched off (LINKEDEYE_SHOW_SAMPLE_PAGES).")
    site = services.example_site(request.user)
    if site is None:
        messages.error(request, "You do not have permission to add devices on any site.")
        return redirect(reverse("architecture:overview"))
    n = example.load(site, services.tenant(), user=request.user)
    audit(request, "create", None, object_type="architecture.example", site=site, **n)
    messages.success(request, f"Loaded the example fabric on “{site}”: {n['devices']} devices, {n['links']} links, "
                              f"{n['racks']} racks. Everything is tagged “example”; remove it with manage.py seed_architecture --remove.")
    return redirect(reverse("architecture:overview"))
