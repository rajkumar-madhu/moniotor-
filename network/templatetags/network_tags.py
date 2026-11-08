"""Formatting filters for the network pages."""
import re

from django import template

register = template.Library()

# First utilization % in title/message (e.g. 82%, 71.5 percent).
_PCT_RE = re.compile(r"\b(\d{1,2}(?:\.\d+)?|100(?:\.0+)?)\s*(?:%|(?:percent)\b)", re.I)
# Standalone high util phrases
_UTIL_WORD = re.compile(r"\butili[sz]ation\b|\busage\b|\bload\b|\bcpu\b|\bmemory\b|\bdisk\b", re.I)


@register.filter
def mbps(value):
    """Traffic rate in Mbps with a sensible precision ('–' when unknown)."""
    if value is None or value == "":
        return "–"
    v = float(value)
    if v >= 1000:
        return f"{v / 1000:,.2f} Gbps"
    if v >= 100:
        return f"{v:,.0f} Mbps"
    if v >= 1:
        return f"{v:,.1f} Mbps"
    if v > 0:
        return f"{v * 1000:,.0f} kbps"
    return "0 Mbps"


@register.filter
def mbps_num(value):
    """Rate as a bare number of Mbps (for columns headed "(Mbps)")."""
    if value is None or value == "":
        return "–"
    v = float(value)
    if v == 0:
        return "0"
    if v < 0.01:
        return "<0.01"
    if v < 10:
        return f"{v:.2f}".rstrip("0").rstrip(".")
    if v < 100:
        return f"{v:.1f}".rstrip("0").rstrip(".")
    return f"{v:,.0f}"


@register.filter
def speed(value):
    """Interface speed (Mbps) as 100M / 1G / 10G."""
    if value is None or value == "":
        return "–"
    v = float(value)
    if v >= 1000:
        g = v / 1000
        return f"{g:g}G" if g == int(g) else f"{g:.1f}G"
    return f"{v:g}M"


@register.filter
def per_s(value):
    """Errors / discards per second ('–' when not collected)."""
    if value is None or value == "":
        return "–"
    v = float(value)
    if v == 0:
        return "0/s"
    if v < 0.01:
        return "<0.01/s"
    return f"{v:.2f}/s".replace(".00/s", "/s") if v < 10 else f"{v:,.0f}/s"


@register.filter
def latency(value):
    if value is None or value == "":
        return "–"
    v = float(value)
    return f"{v:,.0f} ms" if v >= 10 else f"{v:.1f} ms"


@register.filter
def percent(value, digits=1):
    if value is None or value == "":
        return "–"
    return f"{float(value):.{int(digits)}f}%"


@register.filter
def avail_tone(value):
    if value is None:
        return "t-mute"
    return "t-ok" if value >= 99.5 else "t-warn" if value >= 95 else "t-crit"


@register.filter
def if_state(value):
    return {"up": "up", "down": "down", "lowerLayerDown": "down", "notPresent": "unknown", "dormant": "paused",
            "testing": "warning"}.get(value or "", "unknown")


@register.filter
def event_util_row_class(event):
    """Row class for events that carry a utilization % (Prometheus/other alerts).

    ≥80% → red, 70–79% → warning, &lt;70% → green.
    Uses ``event.data`` keys (utilization, util, usage, cpu, memory, mem, disk) or
    the first ``NN%`` in title/message when it looks utilization-related.
    """
    pct = _util_percent_from_event(event)
    if pct is None:
        return ""
    if pct >= 80:
        return "nw-ev-hot"
    if pct >= 70:
        return "nw-ev-warm"
    return "nw-ev-cool"


def _util_percent_from_event(event):
    data = getattr(event, "data", None) or {}
    if isinstance(data, dict):
        for key in ("utilization", "util", "usage", "cpu", "memory", "mem", "disk"):
            raw = data.get(key)
            if raw is None:
                continue
            try:
                v = float(raw)
            except (TypeError, ValueError):
                continue
            if 0 <= v <= 1:
                v *= 100
            if 0 <= v <= 100:
                return v
    title = (getattr(event, "title", None) or "") + " "
    message = getattr(event, "message", None) or ""
    text = title + message
    m = _PCT_RE.search(text)
    if not m:
        return None
    try:
        v = float(m.group(1))
    except ValueError:
        return None
    window = text[max(0, m.start() - 48) : m.end() + 48]
    if _UTIL_WORD.search(window) or _UTIL_WORD.search(text):
        return v
    return None


@register.simple_tag
def sort_link(query, field, current, label):
    """Column header link that toggles ascending/descending sort, keeping the other filters."""
    from django.utils.html import format_html
    q = query.copy()
    desc = current == field
    q["sort"] = f"-{field}" if desc else field
    arrow = " ↑" if current == field else " ↓" if current == f"-{field}" else ""
    return format_html('<a class="sort" href="?{}">{}{}</a>', q.urlencode(), label, arrow)


@register.simple_tag
def device_art(device):
    """The fallback illustration for a device without a photo (see network.art)."""
    from ..art import art_kind, illustration
    return illustration(art_kind(device))


@register.filter
def art_label(device):
    from ..art import LABELS, art_kind
    return LABELS[art_kind(device)]


@register.simple_tag
def photo_link(device, variant):
    """Where the photo links to: the full-size stored photo (or the vendor page of a legacy link) on the detail header,
    the device page everywhere else. Empty when there is nothing to open."""
    if variant == "hero":
        if device.image_id:
            return device.image.url
        return device.product_page_url if device.product_image_url else ""
    return device.get_absolute_url()


@register.filter
def to_json(value):
    """JSON text for a data-* attribute (the template engine escapes it for the attribute context)."""
    import json
    return json.dumps(value, separators=(",", ":"))
