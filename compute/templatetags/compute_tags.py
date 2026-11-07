from decimal import Decimal

from django import template
from django.utils.html import format_html

from core.templatetags.le import badge

from ..metrics import finite, tone

register = template.Library()

POWER = {"running": ("ok", "Running"), "stopped": ("paused", "Stopped"), "suspended": ("warning", "Suspended")}


@register.simple_tag
def power_badge(state):
    """Badge for a VM power state; "No data" when the VM has no placement record."""
    if not state:
        return badge("unknown", "No data")
    key, label = POWER.get(state, ("unknown", str(state).title()))
    return badge(key, label)


@register.filter
def gb(value):
    """12.50 -> "12.5 GB"; None -> "–"."""
    if value is None or value == "":
        return "–"
    v = float(value)
    if v >= 1024:
        return f"{v / 1024:.1f} TB".replace(".0 TB", " TB")
    return f"{v:.1f} GB".replace(".0 GB", " GB")


@register.filter
def num(value):
    if value is None or value == "":
        return "–"
    if isinstance(value, Decimal):
        value = float(value)
    if isinstance(value, float):
        return f"{int(value):,}" if value == int(value) else f"{value:,.2f}".rstrip("0").rstrip(".")
    return f"{value:,}"


@register.filter
def ratio(value):
    """Overcommit ratio 1.75 -> "1.75 : 1"."""
    if value is None:
        return "–"
    return f"{value:.2f} : 1"


@register.filter
def ratio_tone(value):
    if value is None:
        return "t-mute"
    return "t-crit" if value > 4 else "t-warn" if value > 1.5 else "t-ok"


@register.simple_tag
def meter(value, warn=80, crit=90):
    """Small usage bar + percentage for CPU / memory / disk."""
    if not finite(value):
        return format_html('<span class="t-mute">–</span>')
    v = max(0.0, min(100.0, float(value)))
    t = tone(v, warn, crit)
    return format_html('<span class="cm-meter" title="{}%"><i class="fill-{}" style="width:{}%"></i></span>'
                       '<span class="cm-pct t-{}">{}%</span>', f"{v:.1f}", t, f"{v:.0f}", t, f"{v:.0f}")


@register.filter
def selector_text(cluster):
    return cluster.prometheus_selector or ""
