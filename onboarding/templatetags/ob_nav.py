"""Navigation helper: the Administration crumb is a link only for users who may open the console."""
from django import template

from administration import services as administration_services

register = template.Library()


@register.simple_tag(takes_context=True)
def console_access(context):
    """True when the user holds the admin role on every site (the administration console is 403 otherwise)."""
    request = context.get("request")
    return bool(request and administration_services.is_global_admin(request.user))


@register.simple_tag(takes_context=True)
def can_onboard_any(context):
    """True when the user administers at least one site, so the Add buttons on other pages can offer onboarding."""
    from onboarding import services
    request = context.get("request")
    if not request or not request.user.is_authenticated:
        return False
    try:
        return bool(services.onboardable_sites(request.user))
    except Exception:                         # noqa: BLE001 - a button must never break the page it sits on
        return False
