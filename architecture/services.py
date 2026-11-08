"""Data side of the architecture page: scoping, tier classification, link state and the page and live payloads.

One query per model builds the whole page (devices with their photo row, placements, rack items, links, interfaces,
racks' items and feeds); nothing here runs a query per device or per link.
"""
import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache

from django.conf import settings
from django.core.cache import cache
from django.db.models import Q
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from core import prometheus
from core.access import can, scope_by_site, visible_sites

from . import layout as lay
from . import tiers as tr
from .models import Link, Origin, Placement
from .ports import port_key

GLOBAL_SITES = ("", "All Sites")
DOWN_OPER = ("down", "lowerLayerDown", "notPresent")
PROM_TTL = 20            # seconds the Prometheus health probe is remembered
STALE = 15 * 60          # stored interface and CPU figures older than this are "no data"
STATES = ("up", "down", "unknown")
LINK_COLOURS = (("fabric", "Fabric"), ("health", "Health"), ("util", "Utilisation"))
VIEWS = (("diagram", "Diagram"), ("list", "List view"))


def tenant():
    return settings.LINKEDEYE_TENANT


# ------------------------------------------------------------------ scoping
def device_queryset(user, request=None, site="", env=""):
    """The devices this user may see on the page, optionally narrowed to one site (plus global devices) and one
    environment. Same scoping as every network page (site roles and the client picker)."""
    from network.services import devices_for
    qs = devices_for(user, request)
    if site:
        qs = qs.filter(Q(site=site) | Q(site__in=GLOBAL_SITES))
    if env:
        qs = qs.filter(environment=env)
    return qs


def links_queryset(user, request=None, site="", env=""):
    """Links with BOTH ends among the visible devices: a link to a device the user cannot see is not theirs to see."""
    devs = device_queryset(user, request, site, env).order_by().values("pk")
    return Link.objects.filter(tenant_key=tenant(), a_device__in=devs, b_device__in=devs)


def can_manage_device(user, device):
    from network.services import can_manage
    return can_manage(user, "operate", device.site)


def can_manage_link(user, link):
    return can_manage_device(user, link.a_device) and can_manage_device(user, link.b_device)


def manageable_devices(user, request=None):
    """Visible devices the user may link or place (operate on the device's site; global devices need a tenant-wide role)."""
    from network.services import can_manage
    qs = device_queryset(user, request)
    if can_manage(user, "operate", ""):
        return qs
    sites = [s for s in (visible_sites(user) or ()) if can(user, "operate", site_name=s)]
    return qs.filter(site__in=sites)


def clean_site(user, value):
    value = (value or "").strip()
    if not value:
        return ""
    allowed = visible_sites(user)
    return value if allowed is None or value in allowed or value == "All Sites" else ""


def site_choices(user, request=None):
    qs = device_queryset(user, request)
    return sorted({s for s in qs.order_by().values_list("site", flat=True).distinct() if s and s != "All Sites"}, key=str.lower)


def example_site(user):
    """The site the example fabric goes to: the first site the user may operate on, or None when there is none."""
    from core.models import Site
    from network.services import can_manage
    names = list(Site.objects.filter(tenant_key=tenant()).order_by("name").values_list("name", flat=True))
    if can_manage(user, "operate", ""):
        return names[0] if names else "All Sites"
    allowed = visible_sites(user)
    for s in names:
        if (allowed is None or s in allowed) and can_manage(user, "operate", s):
            return s
    return None


# ------------------------------------------------------------------ Prometheus health (cached, never raises)
def prometheus_state(force=False):
    """{"state": "ok" | "off" | "down", "message": text}. One tiny query, remembered for PROM_TTL seconds."""
    if not prometheus.configured():
        return {"state": "off", "message": "Prometheus is not connected (set LINKEDEYE_PROMETHEUS_URL). Link states come "
                                            "from device reachability only; port state and utilisation are not available."}
    hit = None if force else cache.get("arch:prom")
    if hit:
        return hit
    try:
        prometheus.query("vector(1)")
        out = {"state": "ok", "message": ""}
    except prometheus.PrometheusError as exc:
        out = {"state": "down", "message": f"Prometheus is not answering ({str(exc)[:160]}). Link states come from device "
                                            "reachability only; port state and utilisation may be out of date."}
    except Exception:  # never let a monitoring probe break the page
        out = {"state": "down", "message": "Prometheus could not be queried. Link states come from device reachability only."}
    cache.set("arch:prom", out, PROM_TTL)
    return out


# ------------------------------------------------------------------ device state and tooltips
def dev_state(device):
    """up / down / unknown / disabled (monitoring turned off)."""
    return "disabled" if not device.enabled else device.status


STATE_LABELS = {"up": "Up", "down": "Down", "unknown": "Unknown", "disabled": "Monitoring disabled"}


@lru_cache(maxsize=16)
def _art_inner(kind):
    from django.utils.safestring import mark_safe
    from network.art import illustration
    # constant markup drawn by network.art (never user data), so it is safe to mark as such
    return mark_safe(re.sub(r"^<svg[^>]*>|</svg>$", "", str(illustration(kind))))


def art_inner(device):
    from network.art import art_kind
    return _art_inner(art_kind(device))


def _fresh(ts, now):
    return bool(ts and (now - ts).total_seconds() <= STALE)


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) != float("inf") else None


_BIDI = dict.fromkeys([*range(0x202A, 0x202F), *range(0x2066, 0x206A)])      # embeddings, overrides, isolates
_ZERO = ("\u200b", "\u200c", "\u200d", "\u200e", "\u200f", "\u061c", "\ufe0f")   # joiners and direction marks: no width


def _cells(ch):
    """How many narrow characters wide `ch` is drawn: CJK and emoji take two, combining marks none."""
    if ch in _ZERO or unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def _plain(text):
    """One line of text for the drawing: control characters and bidi overrides (which could reorder the words around
    them) removed, white space collapsed."""
    text = "".join(c for c in str(text or "").translate(_BIDI) if unicodedata.category(c) != "Cc" or c in " \t\n")
    return " ".join(text.split())


def _cells_len(text):
    return sum(_cells(c) for c in _plain(text))


def _clip(text, n):
    """At most `n` narrow characters (a wide one counts two), ending in an ellipsis when something was cut."""
    text = _plain(text)
    if sum(_cells(c) for c in text) <= n:
        return text
    out, used = [], 0
    for c in text:
        w = _cells(c)
        if used + w > n - 1:
            break
        out.append(c)
        used += w
    return "".join(out).rstrip("".join(_ZERO) + " ") + "…"


def _pct(v):
    return None if v is None else (f"{v:.0f}%" if v >= 10 else f"{v:.1f}%")


def device_tip(device, now):
    """What the hover tooltip shows (plain strings; the page writes them with textContent)."""
    from core.templatetags.le import ago
    fresh = device.enabled and _fresh(device.metrics_updated_at, now)
    return {
        "type": device.get_device_type_display(), "ip": device.mgmt_ip, "site": device.site or "All Sites",
        "env": device.get_environment_display() if device.environment else "",
        "uptime": device.uptime_short if fresh and device.uptime_seconds is not None and device.status != "down" else "",
        "cpu": _pct(_num(device.cpu_pct)) if fresh else None, "mem": _pct(_num(device.memory_pct)) if fresh else None,
        "seen": ago(device.last_seen) if device.last_seen else "never",
    }


# ------------------------------------------------------------------ links: interface join and state
def fetch_interfaces(device_ids):
    """{device id: {port key: Interface}} for the devices, in one query (names and aliases both index the port)."""
    from network.models import Interface
    out = {}
    if not device_ids:
        return out
    rows = (Interface.objects.filter(device_id__in=list(device_ids))
            .only("device_id", "name", "alias", "oper_status", "admin_status", "speed_mbps", "utilization", "in_mbps",
                  "out_mbps", "updated_at"))
    for i in rows:
        idx = out.setdefault(i.device_id, {})
        idx.setdefault(port_key(i.name), i)
        if i.alias:
            idx.setdefault(port_key(i.alias), i)
    return out


def _end(device, port, ifaces, now):
    """What is known about one end of a link: the interface row when the port matches one (None otherwise)."""
    row = ifaces.get(device.pk, {}).get(port_key(port)) if port else None
    if row is None or not device.enabled:
        return {"oper": None, "util": None, "speed": None, "in": None, "out": None}
    fresh = _fresh(row.updated_at, now)
    return {"oper": row.oper_status if fresh else None,
            "util": _num(row.utilization) if fresh else None,
            "speed": _num(row.speed_mbps),
            "in": _num(row.in_mbps) if fresh else None, "out": _num(row.out_mbps) if fresh else None}


def util_tone(util):
    return "none" if util is None else "crit" if util >= 85 else "warn" if util >= 60 else "ok"


def link_view(link, dev_by_id, ifaces, now, editable=False):
    """One link as plain data: state, utilisation and both ends' interface facts."""
    a, b = dev_by_id[link.a_device_id], dev_by_id[link.b_device_id]
    ea, eb = _end(a, link.a_port, ifaces, now), _end(b, link.b_port, ifaces, now)
    sa, sb = dev_state(a), dev_state(b)
    if "down" in (sa, sb) or ea["oper"] in DOWN_OPER or eb["oper"] in DOWN_OPER:
        state = "down"
    elif sa == "up" and sb == "up":
        state = "up"
    elif ea["oper"] == "up" and eb["oper"] == "up":
        state = "up"
    else:
        state = "unknown"
    utils = [u for u in (ea["util"], eb["util"]) if u is not None]
    util = max(utils) if utils else None
    speeds = [s for s in (ea["speed"], eb["speed"]) if s]
    speed = link.speed_mbps or (min(speeds) if speeds else None)
    return {
        "key": str(link.pk), "a": a.pk, "b": b.pk, "a_name": a.name, "b_name": b.name,
        "a_port": link.a_port, "b_port": link.b_port, "medium": link.get_medium_display(), "medium_key": link.medium,
        "speed": speed, "speed_set": bool(link.speed_mbps), "kind": link.kind, "origin": link.get_origin_display(),
        "origin_key": link.origin, "confirmed": link.confirmed, "note": link.note, "state": state,
        "util": None if util is None else round(util, 1), "tone": util_tone(util),
        "oper_a": ea["oper"], "oper_b": eb["oper"],
        "rate_a": [ea["in"], ea["out"]], "rate_b": [eb["in"], eb["out"]],
        "updated": link.updated_at.isoformat(), "edit": reverse("architecture:link_edit", args=[link.pk]) if editable else "",
    }


# ------------------------------------------------------------------ the fabric
@dataclass
class Fabric:
    devices: list = field(default_factory=list)          # visible, not hidden
    hidden: list = field(default_factory=list)           # devices left out by a placement
    by_id: dict = field(default_factory=dict)
    placed: dict = field(default_factory=dict)           # device id -> tiers.Placed
    placements: dict = field(default_factory=dict)       # device id -> Placement
    links: list = field(default_factory=list)            # link dicts (both ends drawn)
    racks: dict = field(default_factory=dict)            # group key -> [rack dicts]
    rack_of: dict = field(default_factory=dict)          # device id -> rack name
    prom: dict = field(default_factory=dict)
    kpis: dict = field(default_factory=dict)
    now: object = None


def _facts(devices, placements, rack_of):
    from network.art import art_kind
    out = []
    for d in devices:
        p = placements.get(d.pk)
        out.append(tr.Facts(
            id=d.pk, name=d.name, device_type=d.device_type, role=d.role, tags=tuple(d.tag_list),
            rack=rack_of.get(d.pk, ""), storage=art_kind(d) == "storage",
            tier_override=p.tier if p else "", group_override=p.group if p else "", colour_override=p.colour if p else ""))
    return out


def _racks(user, devices):
    """({device id: rack name}, {rack id: summarised rack}) from dcim, empty when dcim is absent or has no data."""
    try:
        from django.apps import apps
        RackItem = apps.get_model("dcim", "RackItem")
        PowerFeed = apps.get_model("dcim", "PowerFeed")
        from dcim import services as dcim
    except (LookupError, ImportError):
        return {}, {}
    ids = [d.pk for d in devices]
    if not ids:
        return {}, {}
    items = list(scope_by_site(RackItem.objects.filter(tenant_key=tenant(), device_id__in=ids), user, field="rack__site")
                 .select_related("rack").order_by("rack_id", "-u_position"))
    rack_of, racks = {}, {}
    for it in items:
        rack_of.setdefault(it.device_id, it.rack.name)
        racks[it.rack_id] = it.rack
    if not racks:
        return {}, {}
    all_items = {}
    for it in RackItem.objects.filter(rack_id__in=list(racks)).order_by():
        all_items.setdefault(it.rack_id, []).append(it)
    feeds = {}
    for f in PowerFeed.objects.filter(rack_id__in=list(racks)).order_by():
        feeds.setdefault(f.rack_id, []).append(f)
    summary = {rid: dcim.summarize_rack(rack, all_items.get(rid, []), feeds.get(rid, [])) for rid, rack in racks.items()}
    return rack_of, summary


def rack_line(rack):
    parts = [f"U {rack.u_used}/{rack.u_height}"]
    if rack.load_kw is not None:
        parts.append(f"{rack.load_kw:.1f} kW" + (" est." if rack.load_source == "estimated" else ""))
    try:
        url = reverse("dcim:rack_detail", args=[rack.pk])
    except NoReverseMatch:
        url = ""
    return {"name": _clip(rack.name, 18), "full": rack.name, "text": " · ".join(parts), "url": url}


def build(user, request=None, site="", env="", now=None, with_racks=True):
    """Load and classify everything the page and the live endpoint need."""
    now = now or timezone.now()
    fb = Fabric(now=now, prom=prometheus_state())
    devs_qs = device_queryset(user, request, site, env)
    all_devices = list(devs_qs)
    ids = [d.pk for d in all_devices]
    fb.placements = {p.device_id: p for p in Placement.objects.filter(tenant_key=tenant(), device__in=devs_qs.order_by().values("pk"))}
    for d in all_devices:
        (fb.hidden if getattr(fb.placements.get(d.pk), "hidden", False) else fb.devices).append(d)
    fb.by_id = {d.pk: d for d in fb.devices}
    rack_of, rack_sum = _racks(user, fb.devices) if with_racks else ({}, {})
    fb.rack_of = rack_of
    raw_links = list(links_queryset(user, request, site, env).select_related("a_device", "b_device"))
    drawn = [l for l in raw_links if l.a_device_id in fb.by_id and l.b_device_id in fb.by_id]
    fb.placed = tr.classify(_facts(fb.devices, fb.placements, rack_of), [(l.a_device_id, l.b_device_id) for l in drawn])
    ports_needed = {l.a_device_id for l in drawn if l.a_port} | {l.b_device_id for l in drawn if l.b_port}
    ifaces = fetch_interfaces(ports_needed)
    ok_by_site = {}
    site_ok = {}
    for d in fb.devices:
        if d.site not in ok_by_site:
            ok_by_site[d.site] = can_manage_device(user, d)
        site_ok[d.pk] = ok_by_site[d.site]
    fb.links = [link_view(l, fb.by_id, ifaces, now, editable=site_ok[l.a_device_id] and site_ok[l.b_device_id]) for l in drawn]
    # racks per server group
    if rack_sum:
        member_racks = {}
        try:
            from django.apps import apps
            RackItem = apps.get_model("dcim", "RackItem")
            for rid, did in RackItem.objects.filter(tenant_key=tenant(), device_id__in=[d.pk for d in fb.devices],
                                                    rack_id__in=list(rack_sum)).order_by().values_list("rack_id", "device_id"):
                member_racks.setdefault(did, set()).add(rid)
        except LookupError:
            pass
        per_group = {}
        for did, rids in member_racks.items():
            p = fb.placed.get(did)
            if p and p.tier == tr.SERVER:
                per_group.setdefault(p.group_key, set()).update(rids)
        fb.racks = {gk: [rack_line(rack_sum[r]) for r in sorted(rids, key=lambda r: rack_sum[r].name.lower())]
                    for gk, rids in per_group.items()}
    fb.kpis = kpis(fb)
    return fb


def kpis(fb):
    by_tier = {t: 0 for t in tr.TIERS}
    for d in fb.devices:
        by_tier[fb.placed[d.pk].tier] += 1
    counts = {s: 0 for s in STATES}
    for l in fb.links:
        counts[l["state"]] += 1
    down = sum(1 for d in fb.devices if dev_state(d) == "down")
    over = oversubscription(fb)
    return {"devices": len(fb.devices), "tiers": by_tier, "links": len(fb.links), "links_up": counts["up"],
            "links_down": counts["down"], "links_unknown": counts["unknown"], "devices_down": down,
            "unclassified": by_tier[tr.OTHER], "hidden": len(fb.hidden), "over": over}


def oversubscription(fb):
    """Leaf downlink Mbps over uplink Mbps, from links whose speed is known. None without both kinds of link."""
    down, up, known, total = {}, {}, 0, 0
    for l in fb.links:
        ta, tb = fb.placed[l["a"]], fb.placed[l["b"]]
        pairs = ((l["a"], ta, tb), (l["b"], tb, ta))
        for dev, mine, other in pairs:
            if mine.tier != tr.LEAF:
                continue
            total += 1
            if not l["speed"]:
                continue
            if other.tier == tr.SERVER:
                down[dev] = down.get(dev, 0) + l["speed"]
                known += 1
            elif other.tier in (tr.SPINE, tr.CORE, tr.FIREWALL, tr.WAN):
                up[dev] = up.get(dev, 0) + l["speed"]
                known += 1
    both = [d for d in down if up.get(d)]
    if not both:
        return None
    ratio = sum(down[d] for d in both) / sum(up[d] for d in both)
    worst = max(both, key=lambda d: down[d] / up[d])
    return {"ratio": round(ratio, 2), "worst": fb.by_id[worst].name, "worst_ratio": round(down[worst] / up[worst], 2),
            "leaves": len(both), "known": known, "total": total}


# ------------------------------------------------------------------ the drawing
def _group_label_colour(fb):
    out = {}
    for d in sorted(fb.devices, key=lambda d: (d.name.lower(), d.pk)):
        p = fb.placed[d.pk]
        if p.tier == tr.SERVER and p.group_key not in out:
            out[p.group_key] = (p.group, p.colour)
    return out


def diagram(fb, expand=frozenset(), width=1200.0):
    """Run the layout and return (layout result, nodes by id with their drawing data)."""
    nodes, groups = [], []
    for d in fb.devices:
        p = fb.placed[d.pk]
        pl = fb.placements.get(d.pk)
        nodes.append(lay.LNode(str(d.pk), p.tier, d.name, group=p.group_key if p.tier == tr.SERVER else "",
                               order=pl.sort_order if pl else 0))
    for gk, (label, colour) in _group_label_colour(fb).items():
        racks = fb.racks.get(gk, [])
        groups.append(lay.LGroup(gk, label, colour, footer=0 if not racks else (len(racks) if len(racks) <= 4 else 4)))
    edges = [lay.LEdge(l["key"], str(l["a"]), str(l["b"])) for l in fb.links]
    res = lay.layout(nodes, edges, groups, lay.Options(width=width, expand=frozenset(expand)))
    return res


FONT = {"full": 12, "medium": 11, "small": 10, "tile-full": 11, "tile-small": 10}


def _tail(text, n):
    """Chips are tiny: keep the end of a numbered name ("…leaf-23"), which tells devices apart, not the shared prefix."""
    text = _plain(text)
    if sum(_cells(c) for c in text) <= n:
        return text
    out, used = [], 0
    for c in reversed(text):
        w = _cells(c)
        if used + w > n - 1:
            break
        out.append(c)
        used += w
    kept = "".join(reversed(out))
    while kept and (_cells(kept[0]) == 0 and kept[0] != " "):      # do not start on a dangling combining mark
        kept = kept[1:]
    return "…" + kept.lstrip()


def node_view(d, fb, pos):
    """Everything the template needs to draw one device, geometry included (numbers only, no markup)."""
    p = fb.placed[d.pk]
    img = d.image if d.image_id else None
    state = dev_state(d)
    model = d.model if d.vendor and d.model.lower().startswith(d.vendor.lower()) else " ".join(x for x in (d.vendor, d.model) if x)
    model = model or d.get_device_type_display()
    mode = pos.mode
    chip = mode in ("chip", "tile-chip")
    fs = FONT.get(mode, 10)
    name_y = 5 + pos.photo_h + fs + 4
    chars = int((pos.w - 26) / 5.6) if chip else pos.chars
    return {
        "id": d.pk, "name": d.name, "label": _tail(d.name, chars) if chip else _clip(d.name, chars),
        "model": _clip(model, pos.chars + 2), "x": pos.x, "y": pos.y, "w": pos.w, "h": pos.h, "mode": mode, "chip": chip,
        "pw": round(pos.w - 10, 1), "ph": pos.photo_h, "cx": round(pos.w / 2, 1), "fs": fs, "name_y": round(name_y, 1),
        "model_y": round(name_y + 12, 1), "show_model": mode in ("full", "medium", "tile-full"),
        "dot_x": 11 if chip else round(pos.w - 10, 1), "dot_y": round(pos.h / 2, 1) if chip else 10,
        "name_x": 21 if chip else round(pos.w / 2, 1),
        "state": state, "state_label": STATE_LABELS[state], "href": d.get_absolute_url(), "type": d.get_device_type_display(),
        "photo": {"url": img.url, "w": img.width, "h": img.height} if img else None,
        "art": None if img else art_inner(d), "weak": p.weak, "reason": p.reason, "tier": p.tier,
    }


def tier_chips(fb):
    # the count is devices, so the server tier reads "Servers 42", not "Server groups 42"
    return [{"tier": t, "label": "Servers" if t == tr.SERVER else tr.TIER_LABELS[t], "count": fb.kpis["tiers"][t]}
            for t in tr.TIERS if fb.kpis["tiers"][t]]


def page_data(fb):
    """The JSON the page script reads: tooltips for devices, details for links, adjacency for highlighting."""
    now = fb.now
    devices = {}
    for d in fb.devices:
        p = fb.placed[d.pk]
        tip = device_tip(d, now)
        tip.update({"name": d.name, "state": dev_state(d), "state_label": STATE_LABELS[dev_state(d)],
                    "href": d.get_absolute_url(), "tier": tr.TIER_LABELS[p.tier], "group": p.group,
                    "why": p.reason, "weak": p.weak})
        devices[str(d.pk)] = tip
    adj = {}
    for l in fb.links:
        adj.setdefault(str(l["a"]), []).append(l["key"])
        adj.setdefault(str(l["b"]), []).append(l["key"])
    links = {l["key"]: l for l in fb.links}
    groups = {}
    for d in fb.devices:
        p = fb.placed[d.pk]
        if p.tier == tr.SERVER:
            groups.setdefault(p.group_key, []).append(str(d.pk))
    return {"devices": devices, "links": links, "adj": adj, "groups": groups}


def live_payload(fb):
    """The JSON the live endpoint returns: states only, keyed by device id and link key."""
    k = fb.kpis
    return {
        "generated_at": fb.now.isoformat(),
        "prometheus": fb.prom,
        "devices": {str(d.pk): {"status": d.status, "state": dev_state(d), "state_label": STATE_LABELS[dev_state(d)]}
                    for d in fb.devices},
        "links": {l["key"]: {"state": l["state"], "util": l["util"], "tone": l["tone"], "oper_a": l["oper_a"],
                             "oper_b": l["oper_b"], "rate_a": l["rate_a"], "rate_b": l["rate_b"]} for l in fb.links},
        "summary": {"devices_down": k["devices_down"], "links_up": k["links_up"], "links_down": k["links_down"],
                    "links_unknown": k["links_unknown"], "links": k["links"], "devices": k["devices"],
                    "over": k["over"]},
    }


def explain(device, user):
    """What automatic placement would do for one device (its override is ignored), for the placement form."""
    from network.art import art_kind
    rack = ""
    try:
        from django.apps import apps
        item = (scope_by_site(apps.get_model("dcim", "RackItem").objects.filter(tenant_key=tenant(), device=device), user,
                              field="rack__site").select_related("rack").first())
        rack = item.rack.name if item else ""
    except LookupError:
        pass
    types, degree = {}, 0
    for l in links_queryset(user).filter(Q(a_device=device) | Q(b_device=device)).select_related("a_device", "b_device"):
        other = l.b_device if l.a_device_id == device.pk else l.a_device
        types[other.device_type] = types.get(other.device_type, 0) + 1
        degree += 1
    facts = tr.Facts(id=device.pk, name=device.name, device_type=device.device_type, role=device.role,
                     tags=tuple(device.tag_list), rack=rack, storage=art_kind(device) == "storage")
    return tr.infer(facts, tr.Graph(degree=degree, types=types))
