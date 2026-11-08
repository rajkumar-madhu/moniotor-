"""Template helpers for the Confirm and Done pages: what activation wired up and where each host shows.

Kept apart from ``views.py`` so the step views stay as they are; both tags only read, are scoped to the
requesting user, and never touch a secret.
"""
from django import template

from onboarding import activate

register = template.Library()


@register.simple_tag(takes_context=True)
def done_blocks(context):
    """One block per target of the run: a link to every place it shows and whether that place has data."""
    request = context.get("request")
    run = context.get("run")
    if run is None or request is None:
        return []
    return activate.coverage(run, request.user)


@register.simple_tag(takes_context=True)
def activation_rows(context):
    run = context.get("run")
    return activate.activation_rows(run) if run is not None else []
