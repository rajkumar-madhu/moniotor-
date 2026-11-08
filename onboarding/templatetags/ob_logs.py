"""Template helpers for the Logs step: the live arrival status of each target and the ingest facts."""
from django import template

from onboarding import logs

register = template.Library()


@register.simple_tag
def log_statuses(run, targets):
    """{target pk: status} read from the observe log store now (a handful of queries for the whole page)."""
    try:
        return logs.target_statuses(run, list(targets))
    except Exception:                       # noqa: BLE001 - the page must render even if the store is unavailable
        return {}


@register.simple_tag
def log_ingest():
    """What the step page needs to know about the ingest endpoint. Never carries the token."""
    return {"enabled": logs.ingest_enabled(), "url": logs.ingest_url(), "rotate_url": logs.rotate_url()}


@register.filter
def get_item(mapping, key):
    try:
        return mapping.get(key) or {}
    except AttributeError:
        return {}
