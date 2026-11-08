"""Which tier (and, for servers, which group) a device belongs to on the architecture page.

Pure Python: no Django imports, so every rule is unit-testable. `infer()` is the one place that decides; its rules are
documented in docs/ARCHITECTURE_PAGE.md and summarised here. The order is fixed:

1. an operator override (a Placement row)                              -> source "override"
2. the device type: router -> wan, firewall -> firewall, wireless and
   unknown types -> other                                              -> source "type"
3. keywords in the tags, then the role, then the name (first source that has one wins; inside one source
   spine beats leaf beats core, so "border-leaf-01" is a leaf)        -> source "name"
4. LAST RESORT, the position in the link graph, only for a switch that no keyword placed:
   linked to a firewall or router -> core; linked to two or more switches and to no server and not mounted
   in a rack -> spine                                                  -> source "graph"
5. a plain switch with a rack item or at least one link -> leaf; a switch with neither -> other  -> source "default"

Servers (physical server, virtual machine, load balancer) form the server tier. Their group is an override, else a
keyword (storage, db, app, web: first one wins), else the name of the rack they are mounted in, else "Other servers".
Colours default per group like the reference drawing: web orange, application green, database blue, storage red,
everything else stone.
"""
import re
from dataclasses import dataclass, field

WAN, FIREWALL, CORE, SPINE, LEAF, SERVER, OTHER = "wan", "firewall", "core", "spine", "leaf", "server", "other"
TIERS = (WAN, FIREWALL, CORE, SPINE, LEAF, SERVER, OTHER)
TIER_LABELS = {WAN: "Internet / WAN", FIREWALL: "Firewalls", CORE: "Core / Border", SPINE: "Spine", LEAF: "Leaf",
               SERVER: "Server groups", OTHER: "Unclassified"}
COLOURS = ("orange", "green", "blue", "red", "purple", "teal", "stone")
SERVER_TYPES = ("physical_server", "virtual_machine", "load_balancer")
OTHER_SERVERS = "Other servers"

_B = r"(?<![a-z])(?:%s)(?![a-z])"
# precedence inside one text source: spine, then leaf, then core
SWITCH_KEYWORDS = (
    (SPINE, re.compile(_B % r"spines?")),
    (LEAF, re.compile(_B % r"leaf|leaves|tors?")),
    (CORE, re.compile(_B % r"core|border|edge|agg|aggregation")),
)
# precedence between groups: storage, database, application, web
GROUP_KEYWORDS = (
    ("storage", re.compile(_B % r"storage|san|nas")),
    ("db", re.compile(_B % r"dbs?|databases?|sql|mysql|mssql|postgres|postgresql|oracle|mongo|mongodb")),
    ("app", re.compile(_B % r"apps?|applications?")),
    ("web", re.compile(_B % r"web|webs|www")),
)
GROUPS = {"web": ("Web Servers", "orange"), "app": ("Application Servers", "green"),
          "db": ("Database Servers", "blue"), "storage": ("Storage (SAN)", "red")}


@dataclass(frozen=True)
class Facts:
    """What `infer` needs to know about one device (built from a network.Device and its rack item)."""
    id: int
    name: str
    device_type: str
    role: str = ""
    tags: tuple = ()
    rack: str = ""                # name of the rack the device is mounted in, "" when it is in none
    storage: bool = False         # the device looks like a storage array (network.art says so)
    tier_override: str = ""
    group_override: str = ""
    colour_override: str = ""

    @property
    def racked(self):
        return bool(self.rack)


@dataclass(frozen=True)
class Graph:
    """The device's neighbourhood in the link graph."""
    degree: int = 0
    types: dict = field(default_factory=dict)      # neighbour device_type -> number of links


@dataclass(frozen=True)
class Placed:
    tier: str
    group: str = ""            # display label, only for the server tier
    colour: str = ""           # one of COLOURS, only for the server tier
    source: str = "default"    # override | type | name | graph | default
    reason: str = ""
    group_reason: str = ""

    @property
    def inferred(self):
        return self.source != "override"

    @property
    def weak(self):
        """Placed by graph position or by the plain-switch default: the page marks these as "inferred"."""
        return self.source in ("graph", "default")

    @property
    def group_key(self):
        return group_key(self.group)


def group_key(label):
    return (label or "").strip().lower()


_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _tokens(text):
    return (text or "").lower()


def _variants(text):
    """The lower-cased text, then (only when it differs) the same text split at camelCase joins, so "SpineA", "DcLeaf"
    and "DBServer" match while "ToR" still matches as written."""
    plain = _tokens(text)
    split = _CAMEL.sub(" ", text or "").lower()
    return (plain, split) if split != plain else (plain,)


def _first_hit(rules, texts):
    """(key, source text label, matched word) of the first text source that has a keyword; texts are ordered."""
    for label, text in texts:
        variants = [t for t in _variants(text) if t]
        for key, pattern in rules:             # rule precedence first, so "Core-SpineA" is a spine, not a core
            for t in variants:
                m = pattern.search(t)
                if m:
                    return key, label, m.group(0)
    return None, "", ""


def _texts(facts):
    return (("tag", " ".join(facts.tags)), ("role", facts.role), ("name", facts.name))


def default_colour(label):
    """The colour a group label gets when nobody chose one: its keyword's colour, else stone."""
    key, _l, _w = _first_hit(GROUP_KEYWORDS, (("group", label),))
    return GROUPS[key][1] if key else "stone"


def _switch_tier(facts, graph):
    key, where, word = _first_hit(SWITCH_KEYWORDS, _texts(facts))
    if key:
        return key, "name", f"The {where} contains “{word}”."
    types = (graph.types if graph else {}) or {}
    degree = graph.degree if graph else 0
    if types.get("firewall") or types.get("router"):
        return CORE, "graph", "No tier keyword; it is linked to a firewall or router, which makes it a core/border switch."
    switches = types.get("switch", 0)
    servers = sum(types.get(t, 0) for t in SERVER_TYPES)
    if switches >= 2 and not servers and not facts.racked:
        return SPINE, "graph", (f"No tier keyword; it is linked to {switches} switches and to no server, "
                                "which makes it a spine switch.")
    if facts.racked or degree:
        why = "is mounted in a rack" if facts.racked else f"has {degree} link{'s' if degree != 1 else ''}"
        return LEAF, "default", f"No tier keyword and no distinctive links; a plain switch that {why} is treated as a leaf."
    return OTHER, "default", "A switch with no tier keyword, no rack item and no links cannot be placed."


def _group(facts, tier):
    """(label, colour, reason) for a server-tier device, ("", "", "") otherwise."""
    if tier != SERVER:
        return "", "", ""
    if facts.group_override.strip():
        label = " ".join(facts.group_override.split())
        why = "Group set by a placement override."
    else:
        key, where, word = _first_hit(GROUP_KEYWORDS, _texts(facts))
        if key is None and facts.storage:
            key, where, word = "storage", "model", "storage array"
        if key:
            label, why = GROUPS[key][0], f"The {where} contains “{word}”."
        elif facts.rack:
            label, why = facts.rack, f"No group keyword; grouped by its rack, {facts.rack}."
        else:
            label, why = OTHER_SERVERS, "No group keyword and no rack."
    colour = facts.colour_override if facts.colour_override in COLOURS else default_colour(label)
    return label, colour, why


def infer(facts, graph=None):
    """The Placed result for one device. `graph` (Graph) is only consulted for a switch no keyword placed."""
    tier, source, reason = None, "", ""
    if facts.tier_override in TIERS:
        tier, source, reason = facts.tier_override, "override", "Set by a placement override."
    elif facts.device_type == "router":
        tier, source, reason = WAN, "type", "Routers are placed in the Internet / WAN tier."
    elif facts.device_type == "firewall":
        tier, source, reason = FIREWALL, "type", "Firewalls are placed in the firewall tier."
    elif facts.device_type == "switch":
        tier, source, reason = _switch_tier(facts, graph)
    elif facts.device_type in SERVER_TYPES:
        tier, source, reason = SERVER, "type", "Servers, virtual machines and load balancers form the server tier."
    else:
        tier, source, reason = OTHER, "type", "This device type is not part of the data centre fabric."
    label, colour, why = _group(facts, tier)
    return Placed(tier=tier, group=label, colour=colour, source=source, reason=reason, group_reason=why)


def classify(facts_list, pairs):
    """{device id: Placed} for every device. `pairs` are (id, id) of linked devices; links to devices that are not in
    `facts_list` (hidden, filtered out) do not count."""
    by_id = {f.id: f for f in facts_list}
    types, degree = {}, {}
    for a, b in pairs:
        if a not in by_id or b not in by_id or a == b:
            continue
        for x, y in ((a, b), (b, a)):
            degree[x] = degree.get(x, 0) + 1
            t = types.setdefault(x, {})
            t[by_id[y].device_type] = t.get(by_id[y].device_type, 0) + 1
    out = {}
    for f in facts_list:
        out[f.id] = infer(f, Graph(degree=degree.get(f.id, 0), types=types.get(f.id, {})))
    # one colour per group: the first device (by name) that carries an explicit one decides, else the keyword default
    chosen = {}
    for f in sorted(facts_list, key=lambda f: (f.name.lower(), f.id)):
        p = out[f.id]
        if p.tier == SERVER and f.colour_override in COLOURS and p.group_key not in chosen:
            chosen[p.group_key] = f.colour_override
    for pk, p in list(out.items()):
        if p.tier == SERVER and p.group_key in chosen and p.colour != chosen[p.group_key]:
            out[pk] = Placed(p.tier, p.group, chosen[p.group_key], p.source, p.reason, p.group_reason)
    return out
