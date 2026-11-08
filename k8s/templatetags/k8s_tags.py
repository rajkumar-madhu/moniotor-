"""Template helpers for the Kubernetes inventory pages."""
from django import template

from k8s.parsing import fmt_bytes
from k8s.tabs import enabled_tabs

register = template.Library()


@register.filter(name="bytes")
def bytes_filter(value):
    """1073741824 -> '1 GiB'; nothing for an unknown value (never a made-up zero)."""
    return fmt_bytes(value) if value not in (None, "") else "–"


@register.filter
def cores(value):
    """Millicores -> cores: 16000 -> '16'."""
    if value in (None, ""):
        return "–"
    try:
        return f"{int(value) / 1000:g}"
    except (TypeError, ValueError):
        return "–"


@register.filter
def num(value):
    """A count with thousands separators, or a dash when it was never discovered."""
    if value in (None, ""):
        return "–"
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return value


@register.filter
def yesno_dash(value):
    """True/False/None -> Yes/No/– (an unknown state must not read as "No")."""
    if value is None:
        return "–"
    return "Yes" if value else "No"


@register.filter
def tone_for(value):
    """Chip tone for a confidence or mapping status."""
    return {"high": "ok", "medium": "warn", "low": "crit", "mapped": "ok", "review": "crit",
            "ok": "ok", "warn": "warn", "crit": "crit"}.get(str(value or ""), "mute")


@register.inclusion_tag("k8s/_tabs.html")
def k8s_tabs(active=""):
    """The section tab strip. Tabs whose page is not built yet are simply absent (see k8s/tabs.py)."""
    return {"tabs": enabled_tabs(), "active": active}
