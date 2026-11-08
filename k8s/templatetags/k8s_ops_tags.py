"""Template tags for the operations pages: the overview card (open issues, clients without monitoring, tool
coverage) and a dict lookup for the coverage matrix."""
from django import template

register = template.Library()


@register.filter
def get_item(mapping, key):
    try:
        return mapping.get(key)
    except AttributeError:
        return None


@register.inclusion_tag("k8s/monitoring_overview.html", takes_context=True)
def ops_overview(context):
    from k8s.views_ops import ops_summary
    request = context.get("request")
    user = getattr(request, "user", None)
    if user is None:
        return {"ops": {"has_data": False}}
    return {"ops": ops_summary(user, site=context.get("site_filter") or "")}
