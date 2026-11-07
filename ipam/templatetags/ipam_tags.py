from django import template

from ipam import services

register = template.Library()


@register.filter
def count(value):
    """Address counts: thousands separators, huge IPv6 sizes as powers of two."""
    try:
        return services.fmt_count(int(value))
    except (TypeError, ValueError):
        return "–"


@register.filter
def util(value):
    """Utilisation %: '<0.01%' for tiny non-zero values (large IPv6 ranges), otherwise one decimal."""
    if value is None:
        return "–"
    v = float(value)
    if 0 < v < 0.01:
        return "<0.01%"
    if v < 10 and v != int(v):
        return f"{v:.2f}%"
    return f"{v:.1f}%"


@register.filter
def bar(value):
    """Width for a utilisation bar (0–100, at least 1 when non-zero so it stays visible)."""
    if not value:
        return 0
    v = float(value)
    return max(1, min(100, round(v)))


@register.filter
def tone(pct):
    return services.util_tone(pct)


@register.filter
def get(d, key):
    try:
        return d.get(key, 0)
    except AttributeError:
        return 0


STATUS_CHIP = {"active": "ok", "reserved": "purple", "dhcp": "teal", "deprecated": "warn"}


@register.filter
def stone(status):
    """Chip tone for an address status."""
    return STATUS_CHIP.get(status, "mute")
