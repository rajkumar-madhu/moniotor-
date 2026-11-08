from django import template

register = template.Library()


@register.filter
def dict_get(value, key):
    """value[key] for a dict in a template (empty string when missing)."""
    try:
        return value.get(key, "")
    except AttributeError:
        return ""


@register.filter
def speed(value):
    """Mbps as 100M / 10G / 400G ("–" when unknown)."""
    if value in (None, ""):
        return "–"
    v = float(value)
    if v >= 1000:
        g = v / 1000
        return f"{g:g}G"
    return f"{v:g}M"
