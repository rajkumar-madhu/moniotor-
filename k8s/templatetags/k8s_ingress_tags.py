"""Template tag that lets the Kubernetes overview show the edge-exposure summary (ingresses, TLS coverage,
certificates about to expire) without the overview view knowing about them."""
from django import template

register = template.Library()


@register.inclusion_tag("k8s/ingress_overview.html", takes_context=True)
def ingress_overview(context):
    from k8s.views_ingress import edge_summary
    request = context.get("request")
    user = getattr(request, "user", None)
    if user is None:
        return {"edge": {"has_data": False}}
    return {"edge": edge_summary(user, site=context.get("site_filter") or "")}
