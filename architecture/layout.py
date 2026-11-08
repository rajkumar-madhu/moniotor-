"""Server-side layout of the data centre architecture drawing. Pure Python: no Django imports.

Tiers are horizontal bands from top to bottom (Internet / WAN, firewalls, core / border, spine, leaf, server groups,
unclassified). Devices inside a tier are ordered by the barycentre of their neighbours (a few sweeps, deterministic
tie-break by name) so links cross as little as possible.

A device is a photo-first node: a large face (170 x 62 at full size) with its label under it. Links between switches
are straight lines that leave the label's bottom edge of the upper node and enter the top edge of the lower one; each
link takes its own attachment slot on both nodes, spread evenly along the edge and ordered by the position of the other
end, so the 4 x 4 full mesh of the reference drawing reads as a clean lattice. Only links inside one tier that are not
neighbours are curved (an arc over the row).

Server groups are drawn as a rounded card holding a rack cabinet: the group's servers are 1U / 2U faces stacked in it
(capped, with a "+N more" strip) and the links from a switch are a bundle of parallel strokes whose number equals the real
link count, running straight down into the cabinet's top. When the number of leaves equals the number of groups on the
first row, leaf i sits directly above group i and its bundle is vertical.

Scaling: a tier with up to `collapse_over` devices is drawn with full nodes (face, name, model) and shrinks the nodes,
then wraps into rows, as it gets crowded. A bigger tier is drawn as compact chips (one per device, nothing is dropped)
and a note says so; `expand` names the tiers or groups to draw at full size instead (the page's drill-down). An opened
group with many servers is drawn as several cabinets side by side. When more than `agg_limit` links join two tiers,
those links are drawn only for the highlighted device and a translucent ribbon with the count stands in for the rest.
"""
import math
from bisect import bisect_left
from dataclasses import dataclass, field, replace

TIER_ORDER = ("wan", "firewall", "core", "spine", "leaf", "server", "other")
TIER_LABELS = {"wan": "Internet / WAN", "firewall": "Firewalls", "core": "Core / Border Switches", "spine": "Spine Layer",
               "leaf": "Leaf Layer", "server": "Server groups", "other": "Unclassified"}

PAD_X, PAD_TOP, BAND_PAD = 24.0, 16.0, 22.0
HEADER, BAND_BOTTOM, GAP_Y, GAP_SERVER, ROW_GAP = 36.0, 20.0, 96.0, 70.0, 24.0
WAN_HEADER, WAN_BOTTOM = 6.0, 2.0              # the WAN row has no visible band
CLOUD_W, CLOUD_H, CLOUD_GAP = 230.0, 76.0, 46.0
# size class -> (node width, node height with its label, face height)
SIZES = {"full": (208.0, 118.0, 76.0), "medium": (170.0, 102.0, 62.0), "small": (126.0, 76.0, 46.0), "chip": (68.0, 26.0, 26.0)}
WAN_SCALE = 0.84                                # routers are a little smaller than the switches
NODE_GAP = 14.0
MIN_WIDTH = 360.0                               # narrowest canvas the layout accepts (smaller values are raised to this)
LANE_STEP = 7.0                                 # distance between parallel links of one pair inside a tier
LINK_CAP_STEP, BUNDLE_CAP_STEP = 34.0, 10.0     # widest gap between two attachment points of a link / of a bundle stroke

# rack cabinets
MAX_CARDS_PER_ROW = 6
UNIT_K = 1.2                                    # the rack unit faces are drawn on a 120 x 11 sheet and scaled by this
CAB_W, UNIT_W, CAB_PAD_X = 178.0, 120.0 * UNIT_K, 9.0
SLOT_H = {1: 12.0 * UNIT_K, 2: 24.0 * UNIT_K}   # rack space of a 1U / 2U server
FACE_H = {1: 11.0 * UNIT_K, 2: 22.0 * UNIT_K}
CAB_TOP, CAB_BOTTOM, CAB_PAD_Y, CAB_GAP = 24.0, 14.0, 7.0, 16.0
MIN_U, CAP_U, CAP_U_EXP = 10, 14, 24            # body height floor, units shown closed, units per cabinet when opened
CARD_PAD, CARD_TOP, PILL_H, FOOT_LINE = 14.0, 16.0, 27.0, 15.0


@dataclass
class LNode:
    id: str
    tier: str
    name: str = ""
    group: str = ""            # group key, for the server tier
    order: int = 0
    unit: int = 1              # rack units a server takes in its cabinet drawing (1 or 2)


@dataclass
class LGroup:
    key: str
    label: str
    colour: str = "stone"
    footer: int = 0            # footer lines (racks) the page prints under the cabinet


@dataclass
class LEdge:
    key: str
    a: str
    b: str


@dataclass
class Options:
    width: float = 1200.0
    collapse_over: int = 24
    expand: frozenset = frozenset()
    agg_limit: int = 300
    bundle_agg: int = 60       # more server strokes than this: one ribbon per group, strokes on hover
    max_bundle: int = 16
    sweeps: int = 4


@dataclass
class Placed:
    id: str
    tier: str
    x: float
    y: float
    w: float
    h: float
    mode: str                  # full | medium | small | chip | face1 | face2 | hidden
    chars: int
    group: str = ""
    fh: float = 0.0            # height of the face (photo); the label sits under it


@dataclass
class Band:
    tier: str
    label: str
    x: float
    y: float
    w: float
    h: float
    count: int
    collapsed: bool = False
    expanded: bool = False


@dataclass
class Cab:
    x: float
    y: float
    w: float
    h: float
    body_y: float
    body_h: float


@dataclass
class Card:
    key: str
    label: str
    colour: str
    x: float
    y: float
    w: float
    h: float
    members: list = field(default_factory=list)       # server ids, in drawing order
    tiles: list = field(default_factory=list)         # (id, x, y, w, h, mode) one per drawn server face
    shown: int = 0
    hidden: int = 0
    mode: str = "rack"
    cabs: list = field(default_factory=list)          # Cab, one per drawn cabinet
    cab_top: float = 0.0                              # where the cable bundle enters
    pill_y: float = 0.0
    count_y: float = 0.0
    foot_y: float = 0.0
    chars: int = 12
    more: tuple = None                                 # (x, y, w, h) of the "+N more" strip


@dataclass
class Path:
    key: str
    kind: str                  # link | bundle | arc | sym
    d: str
    a: str = ""
    b: str = ""
    group: str = ""
    lazy: bool = False
    x1: float = 0.0
    y1: float = 0.0
    x2: float = 0.0
    y2: float = 0.0


@dataclass
class Aggregate:
    a_tier: str
    b_tier: str
    count: int
    d: str
    cx: float
    cy: float
    keys: list = field(default_factory=list)
    colour: str = ""            # set for a server-group ribbon


@dataclass
class Result:
    width: float
    height: float
    bands: list = field(default_factory=list)
    nodes: dict = field(default_factory=dict)
    cards: list = field(default_factory=list)
    paths: list = field(default_factory=list)
    aggregates: list = field(default_factory=list)
    cloud: tuple = None                                 # (x, y, w, h) or None
    notes: list = field(default_factory=list)           # {"text", "tier"|"group"}
    ignored: int = 0                                    # links between two servers: counted, not drawn


def _r(v):
    return round(v + 0.0, 1)


def _chars(width, size=11.0):
    return max(4, int((width - 10) / (size * 0.56)))


def _sort_key(n):
    return (n.order, n.name.lower(), n.id)


# ------------------------------------------------------------------ ordering
def _order_layers(layers, adjacency, sweeps):
    """layers: list of lists of keys (mutated in place). adjacency: key -> list of neighbour keys (any layer)."""
    where = {}

    def reindex():
        for li, layer in enumerate(layers):
            n = len(layer)
            for i, k in enumerate(layer):
                where[k] = (li, (i + 0.5) / n)

    reindex()
    for s in range(sweeps):
        order = range(1, len(layers)) if s % 2 == 0 else range(len(layers) - 2, -1, -1)
        for li in order:
            layer = layers[li]
            scored = []
            for i, k in enumerate(layer):
                vals = [where[o][1] for o in adjacency.get(k, ()) if o in where and
                        (where[o][0] < li if s % 2 == 0 else where[o][0] > li)]
                scored.append(((sum(vals) / len(vals)) if vals else where[k][1], i, k))
            scored.sort(key=lambda t: (t[0], t[1]))
            layers[li] = [t[2] for t in scored]
            n = len(layers[li])
            for i, k in enumerate(layers[li]):
                where[k] = (li, (i + 0.5) / n)


def _size_class(n, inner_w, collapse_over, expanded, scale=1.0):
    if n > collapse_over and not expanded:
        return "chip"
    for mode in ("full", "medium"):
        if n * (SIZES[mode][0] * scale + NODE_GAP) - NODE_GAP <= inner_w:
            return mode
    return "small"


def _line(x1, y1, x2, y2):
    return f"M{_r(x1)} {_r(y1)}L{_r(x2)} {_r(y2)}"


# ------------------------------------------------------------------ the layout
def layout(nodes, edges, groups=(), options=None):
    """Position everything. `nodes` are LNode, `edges` LEdge (between node ids), `groups` LGroup for the server tier."""
    opt = options or Options()
    W = opt.width
    if not isinstance(W, (int, float)) or not math.isfinite(W):
        W = 1200.0
    W = max(float(W), MIN_WIDTH)             # below this a rack card cannot hold its cabinet
    inner_x0 = PAD_X + BAND_PAD
    inner_w = W - 2 * inner_x0
    # Canonical input: an unknown tier is "other", a repeated id keeps its first row, and the edges are taken in key
    # order, so the drawing never depends on the order the database handed the rows over in.
    seen = {}
    for n in nodes:
        seen.setdefault(n.id, n if n.tier in TIER_ORDER else replace(n, tier="other"))
    nodes = list(seen.values())
    edges = sorted(edges, key=lambda e: (str(e.key), str(e.a), str(e.b)))
    edges = [e for i, e in enumerate(edges) if i == 0 or str(e.key) != str(edges[i - 1].key)]     # a link key is drawn once
    by_id = seen
    gmeta = {g.key: g for g in groups}
    res = Result(width=W, height=0.0)

    # ---- tiers present and their members
    members = {t: [] for t in TIER_ORDER}
    for n in sorted(nodes, key=_sort_key):
        members[n.tier if n.tier in members else "other"].append(n)
    group_members = {}
    for n in members["server"]:
        group_members.setdefault(n.group, []).append(n)
    gkeys = sorted(group_members, key=lambda k: (gmeta[k].label.lower() if k in gmeta else k, k))
    present = [t for t in TIER_ORDER if (members[t] if t != "server" else gkeys)]

    # ---- edges: drop unknowns, split off server-to-server links
    sw_edges, srv_edges = [], []
    for e in edges:
        a, b = by_id.get(e.a), by_id.get(e.b)
        if a is None or b is None or a.id == b.id:
            continue
        sa, sb = a.tier == "server", b.tier == "server"
        if sa and sb:
            res.ignored += 1
        elif sa or sb:
            srv_edges.append(e)
        else:
            sw_edges.append(e)

    # ---- ordering (the server layer holds group keys)
    def lkey(n):
        return ("g", n.group) if n.tier == "server" else ("n", n.id)

    layers = []
    for t in present:
        if t == "server":
            layers.append([("g", k) for k in gkeys])
        else:
            layers.append([("n", n.id) for n in members[t]])
    adj = {}
    for e in sw_edges + srv_edges:
        ka, kb = lkey(by_id[e.a]), lkey(by_id[e.b])
        if ka != kb:
            adj.setdefault(ka, []).append(kb)
            adj.setdefault(kb, []).append(ka)
    _order_layers(layers, adj, opt.sweeps)
    ordered = {}
    for t, layer in zip(present, layers):
        ordered[t] = [k[1] for k in layer]
    # groups nothing is cabled to go last, so a bundle never has to cross them
    linked_groups = [k for k in ordered.get("server", []) if ("g", k) in adj]
    if linked_groups:
        ordered["server"] = linked_groups + [k for k in ordered["server"] if ("g", k) not in adj]
    n_linked = len(linked_groups)

    # ---- geometry: bands, nodes and server cards, top to bottom
    y = PAD_TOP
    has_cloud = "wan" in present or "firewall" in present
    cloud_y = y
    if has_cloud:
        y += CLOUD_H + CLOUD_GAP
    placed = {}
    cards = {}
    for t in present:
        if t == "server":
            y = _place_cards(res, opt, ordered["server"], group_members, gmeta, placed, cards, y - GAP_Y + GAP_SERVER if res.bands else y,
                             inner_x0, inner_w, n_linked) + GAP_Y
            continue
        ids = ordered[t]
        n = len(ids)
        expanded = t in opt.expand
        scale = WAN_SCALE if t == "wan" else 1.0
        mode = _size_class(n, inner_w, opt.collapse_over, expanded, scale)
        k = scale if mode != "chip" else 1.0
        nw, nh, nfh = (_r(v * k) for v in SIZES[mode])
        cap = max(1, int((inner_w + NODE_GAP) // (nw + NODE_GAP)))
        rows = math.ceil(n / cap)
        per_row = math.ceil(n / rows)
        head, foot = (WAN_HEADER, WAN_BOTTOM) if t == "wan" else (HEADER, BAND_BOTTOM)
        band_h = head + rows * nh + (rows - 1) * ROW_GAP + foot
        for i, nid in enumerate(ids):
            r, c = divmod(i, per_row)
            in_row = min(per_row, n - r * per_row)
            cx = inner_x0 + (c + 0.5) * inner_w / in_row
            placed[nid] = Placed(nid, t, _r(cx - nw / 2), _r(y + head + r * (nh + ROW_GAP)), nw, nh, mode,
                                 _chars(nw, 11 if mode != "chip" else 10), fh=nfh)
        res.bands.append(Band(t, TIER_LABELS[t], PAD_X, y, W - 2 * PAD_X, band_h, n, collapsed=(mode == "chip"),
                              expanded=expanded and n > opt.collapse_over))
        if mode == "chip":
            res.notes.append({"tier": t, "text": f"{TIER_LABELS[t]}: {n} devices are shown as compact chips (more than "
                                                 f"{opt.collapse_over}). Open the tier to see them at full size."})
        y += band_h + GAP_Y
    res.height = _r(y - GAP_Y + 16) if present else 120.0
    res.nodes = placed
    res.cards = [cards[k] for k in ordered.get("server", [])]

    # ---- the cloud above the WAN (or firewall) tier
    if has_cloud:
        target = ordered["wan"] if "wan" in present else ordered["firewall"]
        xs = [placed[i].x + placed[i].w / 2 for i in target]
        cx = sum(xs) / len(xs)
        cx = min(max(cx, inner_x0 + CLOUD_W / 2), W - inner_x0 - CLOUD_W / 2)
        res.cloud = (_r(cx - CLOUD_W / 2), _r(cloud_y), CLOUD_W, CLOUD_H)
        k = len(target)
        step = min(64.0, 150.0 / (k - 1)) if k > 1 else 0.0
        for j, i in enumerate(target):
            p = placed[i]
            sx = cx + (j - (k - 1) / 2) * step
            res.paths.append(Path(f"cloud:{i}", "sym", _line(sx, cloud_y + CLOUD_H - 6, p.x + p.w / 2, p.y), "cloud", i,
                                  x1=sx, y1=cloud_y + CLOUD_H - 6, x2=p.x + p.w / 2, y2=p.y))

    _route(res, opt, placed, cards, by_id, sw_edges, srv_edges)
    return res


# ------------------------------------------------------------------ server groups: rack cabinets
def _units(n):
    return 2 if getattr(n, "unit", 1) == 2 else 1


def _plan_cabinets(srv, expanded):
    """Split a group's servers over cabinets. Returns (list of cabinets, each a list of nodes, hidden nodes)."""
    if expanded:
        cabs, cur, used = [], [], 0
        for n in srv:
            u = _units(n)
            if cur and used + u > CAP_U_EXP:
                cabs.append(cur)
                cur, used = [], 0
            cur.append(n)
            used += u
        if cur:
            cabs.append(cur)
        return cabs, []
    if sum(_units(n) for n in srv) <= CAP_U:
        return [list(srv)], []
    shown, used = [], 0
    for n in srv:
        u = _units(n)
        if used + u > CAP_U - 1:          # keep one unit for the "+N more" strip
            break
        shown.append(n)
        used += u
    return [shown], list(srv[len(shown):])


def _place_cards(res, opt, keys, group_members, gmeta, placed, cards, y, inner_x0, inner_w, n_linked=0):
    """Draw the server groups as rounded cards holding rack cabinets. Returns the y below the last row."""
    plans = {}
    for k in keys:
        exp = k in opt.expand or "server" in opt.expand
        plans[k] = _plan_cabinets(group_members[k], exp)
    # an opened group that needs several cabinets gets a row (and the full width) of its own
    wide = [k for k in keys if len(plans[k][0]) > 1]
    if wide:
        n_linked = len([k for k in keys[:n_linked] if k not in wide])
        keys = [k for k in keys if k not in wide]
    ng = len(keys)
    # as many cards per row as fit at the smallest readable width (a narrow canvas gets fewer, wider cards)
    mpr = max(1, min(MAX_CARDS_PER_ROW, int(inner_w // (CAB_W + 2 * CARD_PAD + 18))))
    if 0 < n_linked <= mpr and n_linked < ng:
        # the cabled groups share the first row (as in the reference drawing); the rest wrap below them
        rest = keys[n_linked:]
        rn = math.ceil(len(rest) / mpr)
        pr = math.ceil(len(rest) / rn)
        chunks = [keys[:n_linked]] + [rest[i * pr:(i + 1) * pr] for i in range(rn)]
    elif ng:
        rows_n = math.ceil(ng / mpr)
        per_row = math.ceil(ng / rows_n)
        chunks = [keys[i * per_row:(i + 1) * per_row] for i in range(rows_n)]
    else:
        chunks = []
    chunks = [c for c in chunks if c] + [[k] for k in wide]
    rows_n = len(chunks)
    yy = y
    for ri in range(rows_n):
        row_keys = chunks[ri]
        slot = inner_w / len(row_keys)
        row_cards = []
        # one cabinet height per row, so the four racks of the reference drawing stand level
        body_u = {}
        for key in row_keys:
            cabs, hidden = plans[key]
            body_u[key] = max(MIN_U, max(sum(_units(n) for n in c) for c in cabs) + (1 if hidden else 0))
        row_u = max(body_u.values())
        for ci, key in enumerate(row_keys):
            meta = gmeta.get(key) or LGroup(key, key or "Servers")
            srv = group_members[key]
            cabs_plan, hidden_nodes = plans[key]
            is_wide = key in wide
            cw = slot - 18.0 if is_wide else min(320.0, slot - 18.0)
            cx = inner_x0 + (ci + 0.5) * slot
            card_x = cx - cw / 2
            body_h = row_u * SLOT_H[1] + 2 * CAB_PAD_Y
            cab_h = CAB_TOP + body_h + CAB_BOTTOM
            per_cab_row = max(1, int((cw - 2 * CARD_PAD + CAB_GAP) // (CAB_W + CAB_GAP))) if is_wide else 1
            n_cab = len(cabs_plan)
            cab_rows = math.ceil(n_cab / per_cab_row)
            top = yy + CARD_TOP
            card = Card(key, meta.label, meta.colour, _r(card_x), _r(yy), _r(cw), 0.0, cab_top=_r(top), chars=_chars(UNIT_W, 10))
            tile_hidden = 0
            for ci2, members_ in enumerate(cabs_plan):
                r_, c_ = divmod(ci2, per_cab_row)
                in_row = min(per_cab_row, n_cab - r_ * per_cab_row)
                row_w = in_row * CAB_W + (in_row - 1) * CAB_GAP
                cabx = cx - row_w / 2 + c_ * (CAB_W + CAB_GAP)
                caby = top + r_ * (cab_h + CAB_GAP)
                cab = Cab(_r(cabx), _r(caby), CAB_W, _r(cab_h), _r(caby + CAB_TOP), _r(body_h))
                card.cabs.append(cab)
                off = cab.body_y + CAB_PAD_Y
                for n in members_:
                    u = _units(n)
                    fx, fy, fh = _r(cabx + CAB_PAD_X), _r(off), FACE_H[u]
                    card.tiles.append((n.id, fx, fy, UNIT_W, fh, f"face{u}"))
                    placed[n.id] = Placed(n.id, "server", fx, fy, UNIT_W, fh, f"face{u}", card.chars, group=key, fh=fh)
                    off += SLOT_H[u]
                if hidden_nodes and ci2 == n_cab - 1:
                    card.more = (_r(cabx + CAB_PAD_X), _r(off), UNIT_W, FACE_H[1])
            card.shown = len(card.tiles)
            card.hidden = len(hidden_nodes)
            if hidden_nodes:
                tile_hidden = len(hidden_nodes)
                for n in hidden_nodes:
                    placed[n.id] = Placed(n.id, "server", _r(card.x), _r(yy), UNIT_W, FACE_H[1], "hidden", card.chars, group=key)
                res.notes.append({"group": key, "text": f"{meta.label}: {tile_hidden} of {len(srv)} servers are not drawn in the rack. "
                                                        "Open the group to draw them all; the list view has every device."})
            card.members = [n.id for n in srv]
            cabs_bottom = top + cab_rows * cab_h + (cab_rows - 1) * CAB_GAP
            card.pill_y = _r(cabs_bottom + 14)
            card.count_y = _r(card.pill_y + PILL_H + 17)
            card.foot_y = _r(card.count_y + 8)
            foot = min(meta.footer, 4) * FOOT_LINE
            card.h = _r(card.foot_y - yy + foot + CARD_PAD)
            cards[key] = card
            row_cards.append(card)
        row_h = max(c.h for c in row_cards)
        for c in row_cards:
            c.h = row_h
        yy += row_h + (34.0 if ri < rows_n - 1 else 0.0)
    return yy


# ------------------------------------------------------------------ edge routing
def _slots(items, w, cap_step):
    """items: list of (token, other_x, count) -> ({token: [offsets]}, step). Spread evenly around the centre of an edge
    `w` wide, ordered by other_x (so lines to neighbours on the left leave on the left and never cross at the node)."""
    items = sorted(items, key=lambda t: (t[1], str(t[0])))
    total = sum(t[2] for t in items)
    if total <= 1:
        return {t[0]: [0.0] * t[2] for t in items}, 0.0
    step = min(cap_step, 0.84 * w / (total - 1))
    out, i = {}, 0
    for token, _ox, count in items:
        out[token] = [(i + j - (total - 1) / 2) * step for j in range(count)]
        i += count
    return out, step


def _route(res, opt, placed, cards, by_id, sw_edges, srv_edges):
    # switch <-> switch
    side = {}          # (node id, "top"|"bottom") -> [(token, other x, 1)]
    plan = []
    for e in sw_edges:
        pa, pb = placed[e.a], placed[e.b]
        if abs(pa.y - pb.y) < 1.0:
            plan.append((e, "same", pa, pb))
            continue
        up, down = (pa, pb) if pa.y < pb.y else (pb, pa)
        plan.append((e, "vert", up, down))
        side.setdefault((up.id, "bottom"), []).append((e.key, down.x + down.w / 2, 1))
        side.setdefault((down.id, "top"), []).append((e.key, up.x + up.w / 2, 1))
    # bundles: switch <-> server group
    bundles = {}
    for e in srv_edges:
        sw, sv = (e.a, e.b) if by_id[e.a].tier != "server" else (e.b, e.a)
        bundles.setdefault((sw, by_id[sv].group), []).append(e.key)
    bundle_plan = []
    for (sw, gkey), keys in bundles.items():
        card = cards.get(gkey)
        if card is None:
            continue
        keys = sorted(keys)
        shown = keys[:opt.max_bundle]
        n = len(shown)
        pw = placed[sw]
        above = pw.y + pw.h / 2 < card.y
        bundle_plan.append((sw, card, shown, above))
        side.setdefault((sw, "bottom" if above else "top"), []).append((("b", gkey), card.x + card.w / 2, n))
    offsets, steps = {}, {}
    for k, v in side.items():
        only_bundles = all(isinstance(t[0], tuple) for t in v)
        offsets[k], steps[k] = _slots(v, placed[k[0]].w, BUNDLE_CAP_STEP if only_bundles else LINK_CAP_STEP)
    # card attachment points: ordered by the switch x
    card_in = {}
    for sw, card, shown, above in bundle_plan:
        card_in.setdefault(card.key, []).append((sw, placed[sw].x + placed[sw].w / 2))
    card_x = {}
    for gk, lst in card_in.items():
        lst.sort(key=lambda t: (t[1], t[0]))
        card = cards[gk]
        span = (card.cabs[0].w if len(card.cabs) == 1 else card.w) if card.cabs else card.w
        for j, (sw, _x) in enumerate(lst):
            card_x[(sw, gk)] = card.x + card.w / 2 + ((j + 1) / (len(lst) + 1) - 0.5) * span

    rows = {}                                    # (tier, row y) -> sorted x of its nodes, for the same-tier links
    for pl in placed.values():
        if pl.tier != "server":
            rows.setdefault((pl.tier, pl.y), []).append(pl.x)
    for v in rows.values():
        v.sort()
    lanes = {}                                   # same-tier links between one pair: each takes its own lane
    for e, kind, a, b in plan:
        if kind == "same":
            lanes.setdefault(tuple(sorted((e.a, e.b))), []).append(e.key)
    counts = {}
    for e, kind, a, b in plan:
        if kind == "vert":
            up, down = a, b
            x1 = up.x + up.w / 2 + offsets[(up.id, "bottom")][e.key][0]
            x2 = down.x + down.w / 2 + offsets[(down.id, "top")][e.key][0]
            y1, y2 = up.y + up.h, down.y
            tiers = (by_id[up.id].tier, by_id[down.id].tier)
            counts.setdefault(tiers, []).append(e.key)
            res.paths.append(Path(e.key, "link", _line(x1, y1, x2, y2), e.a, e.b, x1=x1, y1=y1, x2=x2, y2=y2))
        else:
            left, right = (a, b) if a.x <= b.x else (b, a)
            lo = rows[(a.tier, a.y)]
            adjacent = bisect_left(lo, right.x) - bisect_left(lo, left.x) == 1
            mates = lanes[tuple(sorted((e.a, e.b)))]
            lane, n_lane = mates.index(e.key), len(mates)
            if adjacent:
                step = min(LANE_STEP, 0.8 * left.fh / (n_lane - 1)) if n_lane > 1 else 0.0
                x1, x2, ym = left.x + left.w, right.x, left.y + left.fh / 2 + (lane - (n_lane - 1) / 2) * step
                res.paths.append(Path(e.key, "link", _line(x1, ym, x2, ym), e.a, e.b, x1=x1, y1=ym, x2=x2, y2=ym))
            else:
                step = min(LANE_STEP, 0.6 * min(left.w, right.w) / max(1, n_lane - 1)) if n_lane > 1 else 0.0
                dx = (lane - (n_lane - 1) / 2) * step
                x1, x2, yt = left.x + left.w / 2 + dx, right.x + right.w / 2 + dx, left.y
                lift = min(40.0, 22 + 6 * min(4, len(lo) // 4) + lane * LANE_STEP)       # never above the top of the canvas
                res.paths.append(Path(e.key, "arc", f"M{_r(x1)} {_r(yt)}C{_r(x1)} {_r(yt - lift)} {_r(x2)} {_r(yt - lift)} {_r(x2)} {_r(yt)}",
                                      e.a, e.b, x1=x1, y1=yt, x2=x2, y2=yt))
    for sw, card, shown, above in bundle_plan:
        pw = placed[sw]
        k = (sw, "bottom" if above else "top")
        offs = offsets[k][("b", card.key)]
        n = len(shown)
        step = steps[k] if n > 1 else 0.0
        base = pw.x + pw.w / 2
        ex = card_x[(sw, card.key)]
        if n > 1:                      # the strokes fan in around their entry point, never into the neighbour's
            span = card.cabs[0].w - 20 if len(card.cabs) == 1 else card.w - 2 * CARD_PAD
            step = min(step, 0.9 * span / (len(card_in[card.key])) / (n - 1))
        for i, key in enumerate(shown):
            sx = base + offs[i]
            tx = ex + (i - (n - 1) / 2) * step
            if above:
                y1, y2 = pw.y + pw.h, card.cab_top + 3
            else:
                y1, y2 = card.y + card.h, pw.y
            res.paths.append(Path(key, "bundle", _line(sx, y1, tx, y2), sw, card.key, group=card.key, x1=sx, y1=y1, x2=tx, y2=y2))
    extra = sum(max(0, len(v) - opt.max_bundle) for v in bundles.values())
    if extra:
        res.notes.append({"text": f"{extra} server link(s) beyond {opt.max_bundle} per bundle are counted in the totals but not drawn as strokes."})
    # a crowded leaf row: the server strokes of each group become one ribbon, the strokes show on hover
    bundle_paths = {}
    for p in res.paths:
        if p.kind == "bundle":
            bundle_paths.setdefault(p.group, []).append(p)
    if sum(len(v) for v in bundle_paths.values()) > opt.bundle_agg:
        card_by_key = {c.key: c for c in res.cards}
        band_by_tier = {b.tier: b for b in res.bands}
        for gk, ps in sorted(bundle_paths.items()):
            if len(ps) < 12 or gk not in card_by_key:
                continue
            ax0, ax1 = min(p.x1 for p in ps), max(p.x1 for p in ps)
            bx0, bx1 = min(p.x2 for p in ps), max(p.x2 for p in ps)
            ay, by = max(p.y1 for p in ps), min(p.y2 for p in ps)
            sw_band = band_by_tier.get(by_id[ps[0].a].tier) if ps[0].a in by_id else None
            if sw_band is not None and sw_band.y + sw_band.h < by:
                ay = sw_band.y + sw_band.h
            if ay >= by:
                continue
            for p in ps:
                p.lazy = True
            d = f"M{_r(ax0)} {_r(ay)}L{_r(ax1)} {_r(ay)}L{_r(bx1)} {_r(by)}L{_r(bx0)} {_r(by)}Z"
            res.aggregates.append(Aggregate(by_id[ps[0].a].tier, "server", len(ps), d, _r((ax0 + ax1 + bx0 + bx1) / 4), _r((ay * 2 + by) / 3),
                                            sorted(p.key for p in ps), colour=card_by_key[gk].colour))
    # aggregate ribbons
    for (ta, tb), keys in counts.items():
        if len(keys) <= opt.agg_limit:
            continue
        ka = set(keys)
        xs_a = [p for p in res.paths if p.key in ka]
        ax0, ax1 = min(p.x1 for p in xs_a), max(p.x1 for p in xs_a)
        bx0, bx1 = min(p.x2 for p in xs_a), max(p.x2 for p in xs_a)
        ay, by = max(p.y1 for p in xs_a), min(p.y2 for p in xs_a)     # between the lowest start and the highest end
        if ay >= by:
            ay, by = min(p.y1 for p in xs_a), max(p.y2 for p in xs_a)
        bands = {b.tier: b for b in res.bands}
        if ta in bands and tb in bands and bands[ta].y + bands[ta].h < bands[tb].y:
            ay, by = bands[ta].y + bands[ta].h, bands[tb].y      # the ribbon fills the gap between the two bands only
        for p in xs_a:
            p.lazy = True
        d = f"M{_r(ax0)} {_r(ay)}L{_r(ax1)} {_r(ay)}L{_r(bx1)} {_r(by)}L{_r(bx0)} {_r(by)}Z"
        res.aggregates.append(Aggregate(ta, tb, len(keys), d, _r((ax0 + ax1 + bx0 + bx1) / 4), _r((ay + by) / 2), sorted(keys)))
        res.notes.append({"text": f"{len(keys)} links join {TIER_LABELS[ta]} and {TIER_LABELS[tb]}: they are drawn for the "
                                  "device you hover or focus, and as one ribbon otherwise."})
