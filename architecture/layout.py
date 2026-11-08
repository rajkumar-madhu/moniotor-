"""Server-side layout of the data centre architecture drawing. Pure Python: no Django imports.

Tiers are horizontal bands from top to bottom (Internet / WAN, firewalls, core / border, spine, leaf, server groups,
unclassified). Devices inside a tier are ordered by the barycentre of their neighbours (a few sweeps, deterministic
tie-break by name) so links cross as little as possible. Links between switches are Bezier paths from the bottom of the
upper node to the top of the lower one; each link takes its own attachment slot on both nodes, so the 4 x 4 full mesh
of the reference drawing stays readable. Servers live inside one rounded card per group; the links between a switch
and a group are drawn as parallel strokes (a "bundle") whose number equals the real link count.

Scaling: a tier with up to `collapse_over` devices is drawn with full nodes (photo, name, model) and shrinks the nodes,
then wraps into rows, as it gets crowded. A bigger tier is drawn as compact chips (one per device, nothing is dropped)
and a note says so; `expand` names the tiers or groups to draw at full size instead (the page's drill-down). When more
than `agg_limit` links join two tiers, those links are drawn only for the highlighted device and a translucent ribbon
with the count stands in for the rest.
"""
import math
from bisect import bisect_left
from dataclasses import dataclass, field, replace

TIER_ORDER = ("wan", "firewall", "core", "spine", "leaf", "server", "other")
TIER_LABELS = {"wan": "Internet / WAN", "firewall": "Firewalls", "core": "Core / Border Switches", "spine": "Spine Layer",
               "leaf": "Leaf Layer", "server": "Server groups", "other": "Unclassified"}

PAD_X, PAD_TOP, BAND_PAD = 24.0, 18.0, 14.0
HEADER, BAND_BOTTOM, GAP_Y, ROW_GAP = 34.0, 12.0, 66.0, 12.0
CLOUD_W, CLOUD_H = 220.0, 66.0
SIZES = {"full": (156.0, 98.0), "medium": (108.0, 76.0), "small": (80.0, 54.0), "chip": (68.0, 26.0)}
NODE_GAP = 14.0
MAX_CARDS_PER_ROW = 4
CARD_PAD, CARD_HEAD, TILE_GAP, FOOT_LINE = 12.0, 58.0, 8.0, 15.0
TILE_SIZES = {"full": (100.0, 78.0), "small": (80.0, 58.0), "chip": (72.0, 26.0)}
MAX_TILE_ROWS = 5
WIDE_GROUP = 30                  # an opened group with more servers than this takes a full-width row


@dataclass
class LNode:
    id: str
    tier: str
    name: str = ""
    group: str = ""            # group key, for the server tier
    order: int = 0


@dataclass
class LGroup:
    key: str
    label: str
    colour: str = "stone"
    footer: int = 0            # footer lines (racks) the page prints under the tiles


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
    mode: str
    chars: int
    group: str = ""
    photo_h: float = 0.0


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
class Card:
    key: str
    label: str
    colour: str
    x: float
    y: float
    w: float
    h: float
    members: list = field(default_factory=list)       # server ids, in drawing order, with their tile boxes
    tiles: list = field(default_factory=list)         # (id, x, y, w, h, mode)
    shown: int = 0
    hidden: int = 0
    mode: str = "full"
    cols: int = 1
    foot_y: float = 0.0
    chars: int = 12
    more: tuple = None                                 # (x, y, w, h) of the "+N more" tile


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


def _size_class(n, inner_w, collapse_over, expanded):
    if n > collapse_over and not expanded:
        return "chip"
    for mode in ("full", "medium"):
        if n * (SIZES[mode][0] + NODE_GAP) - NODE_GAP <= inner_w:
            return mode
    return "small"


def _bez(x1, y1, x2, y2):
    dy = (y2 - y1) * 0.5
    if abs(x1 - x2) < 0.05:
        return f"M{_r(x1)} {_r(y1)}L{_r(x2)} {_r(y2)}"
    return f"M{_r(x1)} {_r(y1)}C{_r(x1)} {_r(y1 + dy)} {_r(x2)} {_r(y2 - dy)} {_r(x2)} {_r(y2)}"


# ------------------------------------------------------------------ the layout
def layout(nodes, edges, groups=(), options=None):
    """Position everything. `nodes` are LNode, `edges` LEdge (between node ids), `groups` LGroup for the server tier."""
    opt = options or Options()
    W = opt.width
    inner_x0 = PAD_X + BAND_PAD
    inner_w = W - 2 * inner_x0
    # Canonical input: an unknown tier is "other", a repeated id keeps its first row, and the edges are taken in key
    # order, so the drawing never depends on the order the database handed the rows over in.
    seen = {}
    for n in nodes:
        seen.setdefault(n.id, n if n.tier in TIER_ORDER else replace(n, tier="other"))
    nodes = list(seen.values())
    edges = sorted(edges, key=lambda e: (str(e.key), str(e.a), str(e.b)))
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

    layers, layer_of = [], {}
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
        y += CLOUD_H + 26
    placed = {}
    cards = {}
    for t in present:
        if t == "server":
            y = _place_cards(res, opt, ordered["server"], group_members, gmeta, placed, cards, y, inner_x0, inner_w, n_linked) + GAP_Y
            continue
        ids = ordered[t]
        n = len(ids)
        expanded = t in opt.expand
        mode = _size_class(n, inner_w, opt.collapse_over, expanded)
        nw, nh = SIZES[mode]
        cap = max(1, int((inner_w + NODE_GAP) // (nw + NODE_GAP)))
        rows = math.ceil(n / cap)
        per_row = math.ceil(n / rows)
        band_h = HEADER + rows * nh + (rows - 1) * ROW_GAP + BAND_BOTTOM
        photo_h = {"full": 52.0, "medium": 40.0, "small": 26.0, "chip": 0.0}[mode]
        for i, nid in enumerate(ids):
            r, c = divmod(i, per_row)
            in_row = min(per_row, n - r * per_row)
            cx = inner_x0 + (c + 0.5) * inner_w / in_row
            placed[nid] = Placed(nid, t, _r(cx - nw / 2), _r(y + HEADER + r * (nh + ROW_GAP)), nw, nh, mode,
                                 _chars(nw, 11 if mode != "chip" else 10), photo_h=photo_h)
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
        for i in target:
            p = placed[i]
            res.paths.append(Path(f"cloud:{i}", "sym", _bez(cx, cloud_y + CLOUD_H, p.x + p.w / 2, p.y), "cloud", i,
                                  x1=cx, y1=cloud_y + CLOUD_H, x2=p.x + p.w / 2, y2=p.y))

    _route(res, opt, placed, cards, by_id, sw_edges, srv_edges)
    return res


def _place_cards(res, opt, keys, group_members, gmeta, placed, cards, y, inner_x0, inner_w, n_linked=0):
    """Draw the server groups as rounded cards, `MAX_CARDS_PER_ROW` per row. Returns the y below the last row."""
    # an opened group with many servers gets a row (and the full width) of its own, so it does not become a tall strip
    wide = [k for k in keys if (k in opt.expand or "server" in opt.expand) and len(group_members[k]) > WIDE_GROUP]
    if wide:
        n_linked = len([k for k in keys[:n_linked] if k not in wide])
        keys = [k for k in keys if k not in wide]
    ng = len(keys)
    # as many cards per row as fit at the smallest readable width (a narrow canvas gets fewer, wider cards)
    mpr = max(1, min(MAX_CARDS_PER_ROW, int(inner_w // (TILE_SIZES["full"][0] + 2 * CARD_PAD + 18))))
    if 0 < n_linked <= mpr < ng:
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
        for ci, key in enumerate(row_keys):
            meta = gmeta.get(key) or LGroup(key, key or "Servers")
            srv = group_members[key]
            exp = key in opt.expand or "server" in opt.expand
            cw = slot - 18.0 if key in wide else min(310.0, slot - 18.0)
            s = len(srv)
            tmode = "full" if s <= 4 else "small" if s <= 30 else "chip"
            tw, th = TILE_SIZES[tmode]
            cols = max(1, int((cw - 2 * CARD_PAD + TILE_GAP) // (tw + TILE_GAP)))
            rows_t = math.ceil(s / cols)
            if not exp and rows_t > MAX_TILE_ROWS:
                rows_t = MAX_TILE_ROWS
            cap = cols * rows_t
            shown = s if s <= cap else cap - 1
            hidden = s - shown
            n_tiles = shown + (1 if hidden else 0)
            rows_t = math.ceil(n_tiles / cols)
            foot = min(meta.footer, 4) * FOOT_LINE + (8 if meta.footer else 0)
            ch = CARD_HEAD + rows_t * th + max(0, rows_t - 1) * TILE_GAP + foot + CARD_PAD
            cx = inner_x0 + (ci + 0.5) * slot
            card = Card(key, meta.label, meta.colour, _r(cx - cw / 2), _r(yy), _r(cw), _r(ch), mode=tmode, cols=cols,
                        shown=shown, hidden=hidden, chars=_chars(tw, 10))
            used = min(cols, n_tiles)
            x0 = card.x + (cw - (used * tw + (used - 1) * TILE_GAP)) / 2
            for i, n in enumerate(srv[:shown]):
                r, c = divmod(i, cols)
                box = (n.id, _r(x0 + c * (tw + TILE_GAP)), _r(yy + CARD_HEAD + r * (th + TILE_GAP)), tw, th, tmode)
                card.tiles.append(box)
                placed[n.id] = Placed(n.id, "server", box[1], box[2], tw, th, "tile-" + tmode, card.chars, group=key,
                                      photo_h={"full": 42.0, "small": 28.0, "chip": 0.0}[tmode])
            if hidden:
                r, c = divmod(shown, cols)
                card.more = (_r(x0 + c * (tw + TILE_GAP)), _r(yy + CARD_HEAD + r * (th + TILE_GAP)), tw, th)
                for n in srv[shown:]:
                    placed[n.id] = Placed(n.id, "server", _r(card.x), _r(yy), tw, th, "hidden", card.chars, group=key)
                res.notes.append({"group": key, "text": f"{meta.label}: {hidden} of {s} servers are not drawn as tiles. "
                                                        "Open the group to draw them all; the list view has every device."})
            card.members = [n.id for n in srv]
            card.foot_y = _r(yy + ch - foot - CARD_PAD + 6)
            cards[key] = card
            row_cards.append(card)
        row_h = max(c.h for c in row_cards)
        for c in row_cards:
            c.h = row_h
        yy += row_h + (34.0 if ri < rows_n - 1 else 0.0)
    return yy


# ------------------------------------------------------------------ edge routing
def _slots(items, w, cap_step=9.0):
    """items: list of (token, other_x, count) -> {token: [offsets]} spread around the centre, ordered by other_x."""
    items = sorted(items, key=lambda t: (t[1], str(t[0])))
    total = sum(t[2] for t in items)
    if total <= 1:
        return {t[0]: [0.0] * t[2] for t in items}
    step = min(cap_step, 0.82 * w / (total - 1))
    out, i = {}, 0
    for token, _ox, count in items:
        out[token] = [(i + j - (total - 1) / 2) * step for j in range(count)]
        i += count
    return out


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
    offsets = {k: _slots(v, placed[k[0]].w) for k, v in side.items()}
    # card attachment points: ordered by the switch x
    card_in = {}
    for sw, card, shown, above in bundle_plan:
        card_in.setdefault(card.key, []).append((sw, placed[sw].x + placed[sw].w / 2))
    card_x = {}
    for gk, lst in card_in.items():
        lst.sort(key=lambda t: (t[1], t[0]))
        card = cards[gk]
        for j, (sw, _x) in enumerate(lst):
            card_x[(sw, gk)] = card.x + card.w * (j + 1) / (len(lst) + 1)

    rows = {}                                    # (tier, row y) -> sorted x of its nodes, for the same-tier links
    for pl in placed.values():
        if pl.tier != "server":
            rows.setdefault((pl.tier, pl.y), []).append(pl.x)
    for v in rows.values():
        v.sort()
    counts = {}
    for e, kind, a, b in plan:
        if kind == "vert":
            up, down = a, b
            x1 = up.x + up.w / 2 + offsets[(up.id, "bottom")][e.key][0]
            x2 = down.x + down.w / 2 + offsets[(down.id, "top")][e.key][0]
            y1, y2 = up.y + up.h, down.y
            tiers = (by_id[up.id].tier, by_id[down.id].tier)
            counts.setdefault(tiers, []).append(e.key)
            res.paths.append(Path(e.key, "link", _bez(x1, y1, x2, y2), e.a, e.b, x1=x1, y1=y1, x2=x2, y2=y2))
        else:
            left, right = (a, b) if a.x <= b.x else (b, a)
            lo = rows[(a.tier, a.y)]
            adjacent = bisect_left(lo, right.x) - bisect_left(lo, left.x) == 1
            if adjacent:
                x1, x2, ym = left.x + left.w, right.x, left.y + left.h / 2
                res.paths.append(Path(e.key, "link", f"M{_r(x1)} {_r(ym)}L{_r(x2)} {_r(ym)}", e.a, e.b, x1=x1, y1=ym, x2=x2, y2=ym))
            else:
                x1, x2, yt = left.x + left.w / 2, right.x + right.w / 2, left.y
                lift = 22 + 6 * min(4, len(lo) // 4)
                res.paths.append(Path(e.key, "arc", f"M{_r(x1)} {_r(yt)}C{_r(x1)} {_r(yt - lift)} {_r(x2)} {_r(yt - lift)} {_r(x2)} {_r(yt)}",
                                      e.a, e.b, x1=x1, y1=yt, x2=x2, y2=yt))
    for sw, card, shown, above in bundle_plan:
        pw = placed[sw]
        offs = offsets[(sw, "bottom" if above else "top")][("b", card.key)]
        n = len(shown)
        step = (offs[1] - offs[0]) if n > 1 else 0.0
        base = pw.x + pw.w / 2
        ex = card_x[(sw, card.key)]
        if n > 1:                      # the strokes fan in around their entry point, never into the neighbour's
            step = min(step, 0.8 * card.w / (len(card_in[card.key]) + 1) / (n - 1))
        for i, key in enumerate(shown):
            sx = base + offs[i]
            tx = ex + (i - (n - 1) / 2) * step
            if above:
                y1, y2 = pw.y + pw.h, card.y
            else:
                y1, y2 = card.y, pw.y
            res.paths.append(Path(key, "bundle", _bez(sx, y1, tx, y2), sw, card.key, group=card.key, x1=sx, y1=y1, x2=tx, y2=y2))
    extra = sum(max(0, len(v) - opt.max_bundle) for v in bundles.values())
    if extra:
        res.notes.append({"text": f"{extra} server link(s) beyond {opt.max_bundle} per bundle are counted in the totals but not drawn as strokes."})
    # aggregate ribbons
    for (ta, tb), keys in counts.items():
        if len(keys) <= opt.agg_limit:
            continue
        ka = set(keys)
        xs_a = [p for p in res.paths if p.key in ka]
        ax0, ax1 = min(p.x1 for p in xs_a), max(p.x1 for p in xs_a)
        bx0, bx1 = min(p.x2 for p in xs_a), max(p.x2 for p in xs_a)
        ay, by = min(p.y1 for p in xs_a), max(p.y2 for p in xs_a)
        for p in xs_a:
            p.lazy = True
        d = f"M{_r(ax0)} {_r(ay)}L{_r(ax1)} {_r(ay)}L{_r(bx1)} {_r(by)}L{_r(bx0)} {_r(by)}Z"
        res.aggregates.append(Aggregate(ta, tb, len(keys), d, _r((ax0 + ax1 + bx0 + bx1) / 4), _r((ay + by) / 2), sorted(keys)))
        res.notes.append({"text": f"{len(keys)} links join {TIER_LABELS[ta]} and {TIER_LABELS[tb]}: they are drawn for the "
                                  "device you hover or focus, and as one ribbon otherwise."})
