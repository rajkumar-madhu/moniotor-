from django import template

register = template.Library()


@register.filter
def kw(value, digits=None):
    """Power in kW: 2 decimals below 10 kW, 1 decimal above; "–" when unknown."""
    if value is None or value == "":
        return "–"
    v = float(value)
    d = digits if digits is not None else (2 if abs(v) < 10 else 1)
    return f"{v:,.{int(d)}f} kW"


@register.filter
def num(value, digits=1):
    if value is None or value == "":
        return "–"
    return f"{float(value):,.{int(digits)}f}"


@register.filter
def temp(value):
    if value is None or value == "":
        return "–"
    return f"{float(value):.1f} °C"


@register.filter
def pctw(value):
    """Bar width 0..100 for a percentage (None -> 0)."""
    if value is None:
        return 0
    return round(max(0.0, min(100.0, float(value))), 1)


@register.filter
def pct0(value):
    if value is None:
        return "–"
    return f"{float(value):.0f}%"


@register.filter
def load_tone(value):
    """Tone for a utilisation percentage: ok below 80 %, warn from 80 %, crit from 90 %."""
    if value is None:
        return "mute"
    v = float(value)
    return "crit" if v >= 90 else "warn" if v >= 80 else "ok"


@register.filter
def headroom_tone(value):
    if value is None:
        return "mute"
    return "crit" if float(value) < 0 else "ok"
