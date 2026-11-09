"""Template tags for the example-data lifecycle (see core/examples.py).

{% example_banner %}   base.html: a slim notice for administrators while example data exists. It reads one integer from the
                        session (set at sign-in and by the Demo data card), so it adds no database query to a page view.
{% demo_data_card %}   Administration > System health: counts per area and the confirm-protected "Remove all example data".
"""
from django import template

from core import examples

register = template.Library()


@register.inclusion_tag("core/_example_banner.html", takes_context=True)
def example_banner(context):
    request = context.get("request")
    show = False
    if request is not None:
        user = getattr(request, "user", None)
        session = getattr(request, "session", None)
        if user is not None and user.is_authenticated and session is not None:
            if session.get(examples.SESSION_KEY) and not session.get(examples.DISMISS_KEY):
                from administration.services import is_global_admin
                show = is_global_admin(user)
    return {"show": show, "request": request}


@register.inclusion_tag("administration/_demo_card.html", takes_context=True)
def demo_data_card(context):
    request = context.get("request")
    report = examples.summary()
    n = examples.total(report)
    if request is not None and getattr(request, "session", None) is not None:
        examples.remember(request, n)
    return {"areas": [r for r in report if r["total"]], "total": n, "request": request, "csrf_token": context.get("csrf_token")}
