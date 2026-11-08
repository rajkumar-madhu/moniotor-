"""Fallback illustrations for devices that have no stored photo.

Original, deliberately generic line drawings in the product palette (they use the theme's CSS variables, so they
follow light and dark mode). They are drawn here, not copied from anything, and the card marks them as an
"Illustration" so nobody mistakes one for a photo of the actual hardware."""
import re

from django.utils.safestring import mark_safe

# word fragments in role / model / name / tags that say what a "physical server" or generic device really is
_UPS = re.compile(r"\bups\b|uninterruptible|\bapc\b|\bsmart-?ups\b|\bpdu\b", re.I)
_STORAGE = re.compile(r"storage|\bnas\b|\bsan\b|disk array|\bfiler\b|netapp|\bpure\b|synology|qnap|powervault|nimble|3par|unity xt", re.I)

KINDS = ["router", "switch", "firewall", "access_point", "server", "storage", "ups", "generic"]
LABELS = {"router": "Router", "switch": "Switch", "firewall": "Firewall", "access_point": "Access point",
          "server": "Server", "storage": "Storage", "ups": "UPS", "generic": "Device"}
BY_TYPE = {"switch": "switch", "router": "router", "firewall": "firewall", "wireless": "access_point",
           "physical_server": "server", "virtual_machine": "server", "load_balancer": "generic"}


def art_kind(device):
    """Which illustration fits: a UPS or storage array is recognised by its role / model / name / tags, otherwise the
    device type decides."""
    text = " ".join([getattr(device, "role", "") or "", getattr(device, "model", "") or "", getattr(device, "name", "") or "",
                     getattr(device, "tags", "") or ""])
    if _UPS.search(text):
        return "ups"
    if _STORAGE.search(text):
        return "storage"
    return BY_TYPE.get(getattr(device, "device_type", ""), "generic")


def _ports(x, y, cols, rows, w=7, h=6, gap=3):
    return "".join(f'<rect class="a-dim" x="{x + c * (w + gap)}" y="{y + r * (h + gap)}" width="{w}" height="{h}" rx="1"/>'
                   for r in range(rows) for c in range(cols))


def _bays(x, y, cols, rows, w, h, gap):
    return "".join(f'<rect class="a-dim" x="{x + c * (w + gap)}" y="{y + r * (h + gap)}" width="{w}" height="{h}" rx="1.5"/>'
                   f'<rect class="a-led{" a-on" if (r + c) % 3 == 0 else ""}" x="{x + c * (w + gap) + 2}" y="{y + r * (h + gap) + h - 3}" width="3" height="1.6" rx=".8"/>'
                   for r in range(rows) for c in range(cols))


_BRICKS = "".join(
    f'<rect class="a-dim" x="{x}" y="{y}" width="{w}" height="9" rx="1"/>'
    for y, row in ((28, [(26, 32), (60, 32), (94, 40)]), (39, [(26, 15), (43, 32), (77, 32), (111, 23)]),
                   (50, [(26, 32), (60, 32), (94, 40)]), (61, [(26, 15), (43, 32), (77, 32), (111, 23)]))
    for x, w in row)

_ART = {
    "switch": f'<rect class="a-body" x="10" y="36" width="140" height="30" rx="4"/>{_ports(20, 42, 12, 2, w=7, h=6, gap=3)}'
              '<rect class="a-led a-on" x="132" y="43" width="4" height="4" rx="2"/><rect class="a-led" x="138" y="43" width="4" height="4" rx="2"/>'
              '<rect class="a-dim" x="132" y="53" width="10" height="5" rx="1"/>'
              '<path class="a-line" d="M16 66v6M144 66v6"/>',
    "router": '<rect class="a-body" x="14" y="48" width="132" height="26" rx="4"/>'
              '<rect class="a-dim" x="24" y="56" width="12" height="9" rx="1"/><rect class="a-dim" x="40" y="56" width="12" height="9" rx="1"/>'
              '<rect class="a-dim" x="56" y="56" width="12" height="9" rx="1"/><rect class="a-dim" x="72" y="56" width="12" height="9" rx="1"/>'
              '<rect class="a-led a-on" x="124" y="58" width="5" height="5" rx="2.5"/><rect class="a-led" x="133" y="58" width="5" height="5" rx="2.5"/>'
              '<circle class="a-ring" cx="80" cy="26" r="13"/>'
              '<path class="a-line" d="M80 16v20M70 26h20M80 16l-3 3M80 16l3 3M80 36l-3-3M80 36l3-3M70 26l3-3M70 26l3 3M90 26l-3-3M90 26l-3 3"/>'
              '<path class="a-line" d="M80 39v9"/>',
    "firewall": '<rect class="a-body" x="18" y="22" width="124" height="56" rx="4"/>' + _BRICKS +
                '<rect class="a-led a-on" x="128" y="68" width="5" height="5" rx="2.5"/>',
    "access_point": '<ellipse class="a-body" cx="80" cy="70" rx="42" ry="10"/><path class="a-body" d="M46 70a34 24 0 0 1 68 0z"/>'
                    '<rect class="a-led a-on" x="76" y="62" width="8" height="3" rx="1.5"/>'
                    '<path class="a-line" d="M62 38a26 26 0 0 1 36 0M54 30a38 38 0 0 1 52 0M70 46a14 14 0 0 1 20 0"/>',
    "server": '<rect class="a-body" x="12" y="30" width="136" height="40" rx="4"/>' + _bays(22, 37, 6, 1, 14, 12, 4) +
              '<circle class="a-ring" cx="132" cy="43" r="5"/><path class="a-line" d="M132 40v3"/>'
              '<rect class="a-dim" x="22" y="54" width="70" height="4" rx="1"/><rect class="a-led a-on" x="124" y="56" width="4" height="4" rx="2"/><rect class="a-led" x="131" y="56" width="4" height="4" rx="2"/>'
              '<path class="a-line" d="M20 70v5M140 70v5"/>',
    "storage": '<rect class="a-body" x="18" y="14" width="124" height="72" rx="4"/>' + _bays(26, 22, 7, 4, 13, 9, 3) +
               '<rect class="a-dim" x="26" y="70" width="60" height="5" rx="1"/><rect class="a-led a-on" x="126" y="72" width="4" height="4" rx="2"/>',
    "ups": '<rect class="a-body" x="40" y="10" width="80" height="80" rx="5"/>'
           '<rect class="a-dim" x="50" y="18" width="60" height="16" rx="2"/>'
           '<rect class="a-led a-on" x="54" y="22" width="9" height="8" rx="1"/><rect class="a-led a-on" x="66" y="22" width="9" height="8" rx="1"/>'
           '<rect class="a-led a-on" x="78" y="22" width="9" height="8" rx="1"/><rect class="a-led" x="90" y="22" width="9" height="8" rx="1"/>'
           '<path class="a-bolt" d="M84 44l-10 15h8l-3 14 11-17h-8z"/>'
           '<path class="a-line" d="M52 80h56"/>',
    "generic": '<rect class="a-body" x="20" y="28" width="120" height="44" rx="6"/>'
               '<circle class="a-ring" cx="48" cy="50" r="10"/><circle class="a-led a-on" cx="48" cy="50" r="3.5"/>'
               '<rect class="a-dim" x="70" y="40" width="56" height="6" rx="2"/><rect class="a-dim" x="70" y="54" width="38" height="6" rx="2"/>',
}


def illustration(kind):
    """The inline <svg> for a kind (safe markup: only the constant strings above, never user data)."""
    body = _ART.get(kind, _ART["generic"])
    return mark_safe(f'<svg class="dc-art" viewBox="0 0 160 100" role="img" aria-label="{LABELS.get(kind, "Device")} illustration" '
                     f'preserveAspectRatio="xMidYMid meet" focusable="false">{body}</svg>')
