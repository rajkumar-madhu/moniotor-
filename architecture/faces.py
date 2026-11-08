"""Generated front-panel faces for the architecture drawing: pure Python, no Django, no images, no dependencies.

`FaceSet` collects the distinct faces a drawing needs and renders each one once as an SVG `<symbol>`; every device then
draws itself with a `<use>`, so 48 ports are not 48 paths per device and a 60-device fabric stays small. The same
vendor, model and size always give the same symbol with the same id, byte for byte.

What is drawn (original artwork, no logos, no trademarks, no raster):
  switch        dark chassis, rack ears, vendor wordmark and model as plain text, an RJ45 / SFP / QSFP port grid guessed
                from the model string (9336C = 36 QSFP, 93180YC = 48 SFP + 6 QSFP, 9500-48Y4C, 6300M 48, default 24),
                uplink cages on the right and a strip of LEDs
  firewall      chassis with two port blocks around a red plate carrying a brick wall
  router        blue chassis with a plate of converging arrows between two port blocks
  server        bezel with drive bays and LEDs; storage array: many drive bays; load balancer, access point, generic
  rack units    1U and 2U bezels for the cabinets (`unit=1|2`), a storage unit and a virtual-machine unit
`detail="simple"` leaves out the text and the port detail (used when a page draws more than a few dozen faces).

Everything that comes from the database (vendor, model) is cleaned of control characters, clipped and XML-escaped.
Colours are not set here: every shape carries a class (`fc-body`, `fc-port`, ...) that the page stylesheet colours with
its tokens, in light and dark."""
import hashlib
import math
import re
from html import escape

FACE_W, FACE_H = 170.0, 62.0
UNIT_W = 120.0
UNIT_H = {1: 11.0, 2: 22.0}
KINDS = ("switch", "firewall", "router", "server", "storage", "load_balancer", "access_point", "generic")
DETAILS = ("full", "simple")


# ------------------------------------------------------------------ helpers
def _n(v):
    s = f"{v:.1f}"
    return s[:-2] if s.endswith(".0") else s


def _txt(value, limit):
    """One printable line of at most `limit` characters (an ellipsis marks a cut)."""
    s = "".join(c for c in str(value or "") if c.isprintable() and c not in "‪‫‬‭‮⁦⁧⁨⁩")
    s = " ".join(s.split())
    return s if len(s) <= limit else s[:max(1, limit - 1)].rstrip() + "…"


def _rects(rects):
    return "".join(f"M{_n(x)} {_n(y)}h{_n(w)}v{_n(h)}h-{_n(w)}z" for x, y, w, h in rects)


def _p(cls, d):
    return f'<path class="{cls}" d="{d}"/>' if d else ""


def _r(cls, x, y, w, h, rx=0.0):
    return f'<rect class="{cls}" x="{_n(x)}" y="{_n(y)}" width="{_n(w)}" height="{_n(h)}"' + (f' rx="{_n(rx)}"' if rx else "") + "/>"


def _c(cls, x, y, r):
    return f'<circle class="{cls}" cx="{_n(x)}" cy="{_n(y)}" r="{_n(r)}"/>'


def _t(cls, x, y, size, text, anchor=""):
    if not text:
        return ""
    return (f'<text class="{cls}" x="{_n(x)}" y="{_n(y)}" font-size="{_n(size)}"' + (f' text-anchor="{anchor}"' if anchor else "")
            + f">{escape(text, quote=False)}</text>")


def _short(model, limit):
    """The tail of a model string that fits `limit` characters ("Nexus 93180YC-FX" -> "93180YC-FX"): the family name
    in front is what the vendor mark and the page label already say."""
    words = _txt(model, 60).split(" ")
    out = ""
    for w in reversed(words):
        cand = (w + " " + out).strip()
        if len(cand) > limit:
            break
        out = cand
    return out or _txt(model, limit)


def _grid(x, y, w, h, n, rows, group=6, gap=2.0, row_gap=3.0):
    """`n` ports in `rows` rows inside the box, in blocks of `group` columns (the way a real front panel is laid out):
    port 1 on the top row, port 2 under it. Returns [(x, y, w, h)]."""
    if n <= 0:
        return []
    rows = max(1, min(rows, n))
    cols = math.ceil(n / rows)
    gaps = (cols - 1) // group
    pitch = (w - gaps * gap) / cols
    pw = max(1.0, pitch - 0.8)
    rh = (h - (rows - 1) * row_gap) / rows
    out = []
    for c in range(cols):
        cx = x + c * pitch + (c // group) * gap
        for r in range(rows):
            if c * rows + r < n:
                out.append((cx, y + r * (rh + row_gap), pw, rh))
    return out


def _sockets(rects):
    """The dark opening inside each port."""
    return [(x + 0.5, y + h * 0.3, max(0.4, w - 1.0), h * 0.62) for x, y, w, h in rects]


def _leds(x, y, n, step=5.5):
    return "".join(_c("fc-led" if i % 3 != 2 else "fc-led2", x + i * step, y, 1.3) for i in range(n))


# ------------------------------------------------------------------ port counts from the model string
_KNOWN = (
    (re.compile(r"93180YC|93180LC"), (0, 48, 6)),
    (re.compile(r"93240YC"), (0, 48, 12)),
    (re.compile(r"9364C"), (0, 0, 64)),
    (re.compile(r"9336C"), (0, 0, 36)),
    (re.compile(r"9332"), (0, 0, 32)),
    (re.compile(r"9316"), (0, 0, 16)),
)


def guess_ports(model):
    """(RJ45 ports, SFP ports, QSFP ports) of a switch, guessed from its model string. Default 24 RJ45 + 4 SFP."""
    m = str(model or "").upper()
    for rx, ports in _KNOWN:
        if rx.search(m):
            return ports
    split = re.search(r"(?<!\d)(\d{2})[YX](\d)[CQ](?![A-Z0-9])", m)          # 48Y4C: 48 x 25G + 4 x 100G
    if split:
        return (0, int(split.group(1)), int(split.group(2)))
    count = re.search(r"(?<!\d)(8|12|16|24|28|32|48|52|54|64)(?!\d)", m)
    n = int(count.group(1)) if count else 24
    if n >= 52:
        return (n - 4, 4, 0)
    if n >= 24:
        return (n, 4, 0)
    return (n, 2 if n >= 12 else 0, 0)


# ------------------------------------------------------------------ the faces (viewBox 0 0 170 62)
def _chassis(extra=""):
    return (_r("fc-body", 0.5, 0.5, 169, 61, 5) + _r("fc-sheen", 1.5, 1.5, 167, 7, 3.5) + _r("fc-ear", 0.5, 0.5, 6, 61, 3)
            + _r("fc-ear", 163.5, 0.5, 6, 61, 3) + _c("fc-screw", 3.5, 9, 1.1) + _c("fc-screw", 3.5, 53, 1.1)
            + _c("fc-screw", 166.5, 9, 1.1) + _c("fc-screw", 166.5, 53, 1.1) + extra)


def _switch(vendor, model, detail):
    rj, sfp, qsfp = guess_ports(model)
    if detail == "simple":
        return (_chassis() + _r("fc-panel", 20, 15, 130, 32, 2) + _r("fc-port", 24, 20, 122, 9, 1)
                + _r("fc-port", 24, 33, 122, 9, 1) + _c("fc-led", 156, 20, 1.5) + _c("fc-led2", 156, 27, 1.5))
    main_n, main_kind = (rj, "rj") if rj else (sfp, "sfp") if sfp else (qsfp, "qsfp")
    side = [(k, n) for k, n in (("sfp", sfp), ("qsfp", qsfp)) if n and k != main_kind]
    main_x = 50.0
    main_w = (70.0 if main_n > 24 else 84.0) if side else 106.0
    rows = 1 if main_n < 8 else 2
    rh = 12.0
    box_h = rows * rh + (rows - 1) * 3.0
    y0 = 31 - box_h / 2 - 1
    parts = [_r("fc-panel", main_x - 3.5, y0 - 4, main_w + 7, box_h + 8, 2.5)]
    cells = _grid(main_x, y0, main_w, box_h, main_n, rows)
    if main_kind == "rj":
        parts += [_p("fc-port", _rects(cells)), _p("fc-port-in", _rects(_sockets(cells)))]
    else:
        parts += [_p("fc-cage", _rects(cells)), _p("fc-cage-in", _rects(_sockets(cells)))]
    if side:
        sx = main_x + main_w + 7.5
        sw = 158.0 - sx
        parts.append(_r("fc-panel", sx - 3, y0 - 4, sw + 6, box_h + 8, 2.5))
        count = sum(n for _k, n in side)
        scells = _grid(sx, y0, sw, box_h, count, 2 if count > 1 else 1, group=99, gap=0, row_gap=3.0)
        parts += [_p("fc-cage", _rects(scells)), _p("fc-cage-in", _rects(_sockets(scells)))]
    parts.append(_leds(main_x, 56.0, 6))
    parts.append(_t("fc-mark", 12, 24, 8.4, _txt(vendor, 10).upper()))
    parts.append(_t("fc-model", 12, 34, 5.6, _short(model, 11)))
    parts.append(_r("fc-port", 12, 43, 7, 5, 1) + _r("fc-port", 22, 43, 7, 5, 1) + _c("fc-led", 35, 45.5, 1.3))
    return _chassis() + "".join(parts)


def _firewall(vendor, model, detail):
    left = _grid(12, 16, 50, 24, 12, 2, group=6, gap=0, row_gap=3.0)
    right = _grid(108, 16, 50, 24, 12, 2, group=6, gap=0, row_gap=3.0)
    brick = ("M75 20h20M75 27h20M75 34h20M75 41h20"                          # a brick wall: courses and staggered joints
             "M83 20v7M91 20v7M79 27v7M87 27v7M83 34v7M91 34v7M79 41v7M87 41v7")
    plate = (_r("fc-plate-fw", 69, 11, 32, 40, 4) + f'<path class="fc-brick" d="{brick}"/>')
    if detail == "simple":
        return _chassis() + plate + _r("fc-port", 12, 18, 50, 20, 1) + _r("fc-port", 108, 18, 50, 20, 1)
    return (_chassis() + _r("fc-panel", 9, 12, 56, 32, 2.5) + _r("fc-panel", 105, 12, 56, 32, 2.5)
            + _p("fc-port", _rects(left)) + _p("fc-port-in", _rects(_sockets(left)))
            + _p("fc-port", _rects(right)) + _p("fc-port-in", _rects(_sockets(right))) + plate
            + _t("fc-mark", 12, 56, 6.4, _txt(vendor, 12).upper()) + _t("fc-model", 158, 56, 5.6, _short(model, 18), "end")
            + _leds(110, 49, 4))


def _router(vendor, model, detail):
    ports_l = _grid(12, 24, 44, 14, 4, 1, group=4, gap=0)
    ports_r = _grid(114, 24, 44, 14, 4, 1, group=4, gap=0)
    arrows = ("M85 14v26M72 27h26M85 14l-3.2 4M85 14l3.2 4M85 40l-3.2-4M85 40l3.2-4M72 27l4-3.2M72 27l4 3.2M98 27l-4-3.2M98 27l-4 3.2")
    plate = _c("fc-plate-rt", 85, 27, 18) + _c("fc-ring", 85, 27, 18) + f'<path class="fc-arrow" d="{arrows}"/>'
    body = (_r("fc-body fc-body-r", 0.5, 0.5, 169, 61, 5) + _r("fc-sheen", 1.5, 1.5, 167, 7, 3.5) + _r("fc-ear", 0.5, 0.5, 6, 61, 3)
            + _r("fc-ear", 163.5, 0.5, 6, 61, 3) + _c("fc-screw", 3.5, 9, 1.1) + _c("fc-screw", 3.5, 53, 1.1)
            + _c("fc-screw", 166.5, 9, 1.1) + _c("fc-screw", 166.5, 53, 1.1))
    if detail == "simple":
        return body + plate
    return (body + _r("fc-panel", 9, 20, 50, 22, 2.5) + _r("fc-panel", 111, 20, 50, 22, 2.5)
            + _p("fc-port", _rects(ports_l)) + _p("fc-port-in", _rects(_sockets(ports_l)))
            + _p("fc-port", _rects(ports_r)) + _p("fc-port-in", _rects(_sockets(ports_r))) + plate
            + _t("fc-mark", 12, 56, 6.4, _txt(vendor, 12).upper()) + _t("fc-model", 158, 56, 5.6, _short(model, 18), "end")
            + _leds(112, 50, 4))


def _bays(x, y, w, h, cols, rows, gap=2.0):
    cw = (w - (cols - 1) * gap) / cols
    ch = (h - (rows - 1) * gap) / rows
    return [(x + c * (cw + gap), y + r * (ch + gap), cw, ch) for r in range(rows) for c in range(cols)]


def _server(vendor, model, detail):
    bays = _bays(14, 14, 100, 34, 6, 2)
    if detail == "simple":
        return _chassis() + _p("fc-bay", _rects(bays)) + _c("fc-led", 150, 18, 1.5)
    return (_chassis() + _p("fc-bay", _rects(bays)) + _r("fc-lcd", 122, 14, 22, 8, 1.5)
            + _c("fc-button", 152, 42, 4.2) + _c("fc-led", 150, 18, 1.5) + _c("fc-led2", 156, 18, 1.5) + _leds(14, 54, 3)
            + _t("fc-mark", 158, 56, 6.4, _txt(vendor, 12).upper(), "end") + _t("fc-model", 122, 36, 5.6, _short(model, 9)))


def _storage(vendor, model, detail):
    bays = _bays(12, 13, 120, 36, 12, 2, 1.6)
    if detail == "simple":
        return _chassis() + _p("fc-bay", _rects(bays)) + _c("fc-led", 150, 18, 1.5)
    return (_chassis() + _p("fc-bay", _rects(bays)) + _p("fc-bay-led", _rects([(x + 1, y + h - 3, w - 2, 1.4) for x, y, w, h in bays]))
            + _r("fc-lcd", 139, 14, 22, 8, 1.5) + _c("fc-button", 150, 40, 4.2) + _c("fc-led", 143, 29, 1.4) + _c("fc-led2", 149, 29, 1.4)
            + _t("fc-mark", 12, 57, 6.0, _txt(vendor, 12).upper()) + _t("fc-model", 132, 57, 5.4, _short(model, 18), "end"))


def _load_balancer(vendor, model, detail):
    ports = _grid(12, 22, 48, 18, 8, 2, group=4, gap=2.0)
    plate = _r("fc-plate-lb", 76, 11, 40, 40, 4) + f'<path class="fc-arrow" d="M84 31h12M96 31l8-9M96 31l8 9M104 22l-3.6 .6M104 22l-.6 3.6M104 40l-3.6-.6M104 40l-.6-3.6"/>'
    if detail == "simple":
        return _chassis() + plate + _r("fc-port", 12, 20, 52, 22, 1)
    return (_chassis() + _r("fc-panel", 9, 18, 56, 26, 2.5) + _p("fc-port", _rects(ports)) + _p("fc-port-in", _rects(_sockets(ports))) + plate
            + _leds(126, 20, 4) + _p("fc-bay", _rects(_bays(126, 30, 32, 14, 4, 1))) + _t("fc-mark", 12, 56, 6.4, _txt(vendor, 12).upper())
            + _t("fc-model", 158, 56, 5.6, _short(model, 18), "end"))


def _access_point(vendor, model, detail):
    arcs = "M60 27a34 34 0 0 1 50 0M68 33a22 22 0 0 1 34 0M76 39a10 10 0 0 1 18 0"
    puck = ('<ellipse class="fc-ap" cx="85" cy="46" rx="38" ry="11"/>'
            + '<ellipse class="fc-ap-top" cx="85" cy="42" rx="38" ry="9"/>' + _c("fc-led", 85, 47, 1.8))
    return f'<path class="fc-wave" d="{arcs}"/>' + puck + ("" if detail == "simple" else _t("fc-model-dark", 85, 60, 5.6, _txt(model, 20), "middle"))


def _generic(vendor, model, detail):
    vents = "".join(f"M{_n(46 + i * 6)} 16v30" for i in range(14))
    if detail == "simple":
        return _chassis() + _p("fc-vent", vents) + _c("fc-led", 150, 20, 1.5)
    return (_chassis() + _p("fc-vent", vents) + _c("fc-button", 150, 40, 4.2) + _c("fc-led", 150, 20, 1.5) + _c("fc-led2", 156, 20, 1.5)
            + _t("fc-mark", 12, 24, 8.4, _txt(vendor, 10).upper()) + _t("fc-model", 12, 34, 5.6, _short(model, 11)) + _leds(12, 50, 3))


_DRAW = {"switch": _switch, "firewall": _firewall, "router": _router, "server": _server, "storage": _storage,
         "load_balancer": _load_balancer, "access_point": _access_point, "generic": _generic}


# ------------------------------------------------------------------ rack units (viewBox 0 0 120 11 / 120 22)
def _unit(kind, u, detail):
    h = UNIT_H[u]
    cls = "fc-u fc-virt" if kind == "vm" else "fc-u"
    body = _r(cls, 0.5, 0.5, UNIT_W - 1, h - 1, 1.6)
    if detail == "simple":
        return body + _p("fc-bay", _rects(_bays(6, 2.4, 52, h - 4.8, 6, 1 if u == 1 else 2, 1.6))) + _c("fc-led", 108, h / 2, 1.2)
    if kind == "storage":
        bays = _bays(6, 3, 90, h - 6, 12, 1, 1.4)
        return (body + _p("fc-bay", _rects(bays)) + _p("fc-bay-led", _rects([(x + 1, y + hh - 3, w - 2, 1.2) for x, y, w, hh in bays]))
                + _r("fc-lcd", 100, h / 2 - 3, 11, 5, 1) + _c("fc-led", 114, h / 2 - 2, 1.1) + _c("fc-led2", 114, h / 2 + 2, 1.1))
    if kind == "vm":
        return body + _p("fc-bay", _rects(_bays(6, 2.6, 40, 5.8, 5, 1, 1.4))) + _t("fc-model-dark", 62, 7.6, 5.2, "VM") + _c("fc-led", 108, 5.5, 1.1)
    if u == 1:
        return (body + _p("fc-bay", _rects(_bays(6, 2.6, 56, 5.8, 6, 1, 1.5))) + _r("fc-lcd", 70, 3.6, 14, 3.8, 1)
                + _c("fc-led", 96, 4, 1.0) + _c("fc-led2", 96, 7.2, 1.0) + _c("fc-button", 108, 5.5, 2.0))
    return (body + _p("fc-bay", _rects(_bays(6, 3.2, 62, 15.6, 8, 2, 1.6))) + _r("fc-lcd", 78, 4.4, 18, 5, 1)
            + _c("fc-led", 102, 5, 1.2) + _c("fc-led2", 108, 5, 1.2) + _c("fc-button", 110, 14, 2.6))


# ------------------------------------------------------------------ the set of faces of one drawing
class FaceSet:
    """The distinct faces one page uses. `use()` returns the id of the symbol to reference; `defs()` renders them all."""

    def __init__(self, detail="full"):
        self.detail = detail if detail in DETAILS else "full"
        self._syms = {}

    def use(self, kind, vendor="", model="", unit=0):
        """The symbol id for a device face (`unit` 0) or a rack unit (`unit` 1 or 2, kind server / storage / vm)."""
        vendor, model = _txt(vendor, 24), _txt(model, 40)
        if unit:
            kind = kind if kind in ("storage", "vm") else "server"
            key = ("u", kind, unit, self.detail)
        else:
            kind = kind if kind in KINDS else "generic"
            ports = guess_ports(model) if kind == "switch" and self.detail == "full" else ()
            key = ("f", kind, vendor if self.detail == "full" else "", model if self.detail == "full" else "", ports, self.detail)
        digest = hashlib.sha1(repr(key).encode("utf-8")).hexdigest()[:8]
        sid = f"fc-{'u' + str(unit) if unit else 'f'}-{kind}-{digest}"
        if sid not in self._syms:
            if unit:
                self._syms[sid] = (f'<symbol id="{sid}" viewBox="0 0 {_n(UNIT_W)} {_n(UNIT_H[unit])}">'
                                   f"{_unit(kind, unit, self.detail)}</symbol>")
            else:
                self._syms[sid] = (f'<symbol id="{sid}" viewBox="0 0 {_n(FACE_W)} {_n(FACE_H)}">'
                                   f"{_DRAW[kind](vendor, model, self.detail)}</symbol>")
        return sid

    def __len__(self):
        return len(self._syms)

    def defs(self):
        """`<defs>` markup holding every symbol, sorted by id so the output is the same on every run."""
        return "<defs>" + "".join(self._syms[k] for k in sorted(self._syms)) + "</defs>" if self._syms else ""


def face_kind(device_type, art_kind=""):
    """Which face a network.Device draws: its art kind (storage, access point ...) wins, then its device type."""
    if art_kind in ("storage",):
        return "storage"
    if art_kind == "access_point":
        return "access_point"
    return {"switch": "switch", "router": "router", "firewall": "firewall", "load_balancer": "load_balancer",
            "wireless": "access_point", "physical_server": "server", "virtual_machine": "server"}.get(device_type, "generic")


_TWO_U = re.compile(r"\b2U\b|\bR7\d{2,3}\b|DL38\d|C240|SR650|SR850|2RU", re.I)


def rack_units(kind, model=""):
    """How many rack units a server takes in its cabinet drawing: a storage array is 2U; otherwise the model decides
    (1U / 2U in the name, PowerEdge R6xx = 1U and R7xx = 2U, ProLiant DL360 / DL380 ...); 1U when it cannot tell."""
    if kind == "storage":
        return 2
    m = str(model or "")
    if _TWO_U.search(m):
        return 2
    return 1
