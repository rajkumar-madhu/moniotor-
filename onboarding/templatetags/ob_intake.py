"""Template helpers for the scope / targets / credentials step pages.

The step views are rendered by ``onboarding.views`` and know nothing about the SNMP, Prometheus or
single-host panels, so those panels fetch their own (non-secret) state here. Nothing in this module
reads a secret: forms are unbound and write-only, and the SNMP exporter address and the Prometheus
URL are shown redacted.
"""
from django import template

from onboarding import devices, intake, prometheus_step, services
from onboarding.models import TargetType

register = template.Library()


@register.simple_tag
def new_single_form():
    return intake.SingleTargetForm(initial={"target_type": TargetType.LINUX})


@register.simple_tag
def new_snmp_form():
    return devices.SnmpCredentialForm(initial={"name": "SNMP", "module": devices.SNMP_DEFAULT_MODULE})


@register.simple_tag
def new_prom_form():
    return prometheus_step.PrometheusForm()


@register.simple_tag(takes_context=True)
def prom_state(context):
    """The Prometheus connection, whether this user may change it, and the last result for this run."""
    request = context.get("request")
    user = getattr(request, "user", None)
    run = context.get("run")
    current = prometheus_step.current_connection()
    current.pop("raw_url", None)
    result = context.get("prom_result")
    saved = None
    if run is not None:
        target = next((t for t in run.targets.all() if t.target_type == TargetType.PROMETHEUS), None)
        saved = ((target.facts or {}).get("prometheus") if target is not None else None) or None
    return {"current": current, "result": result, "saved": saved,
            "can_save": bool(user and services.can_manage_discovery(user))}


@register.simple_tag(takes_context=True)
def snmp_state(context):
    request = context.get("request")
    user = getattr(request, "user", None)
    run = context.get("run")
    targets = devices.device_targets(run) if run is not None else []
    rows = []
    for t in targets:
        cfg = devices.snmp_settings(t)
        rows.append({"target": t, "settings": cfg, "snmp": (t.facts or {}).get("snmp") or {}})
    return {"exporter": devices.exporter_url(), "can_set_exporter": bool(user and services.can_manage_discovery(user)),
            "devices": rows, "kinds": devices.SNMP_KINDS,
            "snippet_v2c": devices.exporter_auth_snippet("v2c"),
            "snippet_v3": devices.exporter_auth_snippet("v3", security_level="authPriv")}


@register.filter
def dict_get(value, key):
    try:
        return value.get(key)
    except AttributeError:
        return None


@register.simple_tag(takes_context=True)
def new_site_url(context):
    """Where to create a site, for a user who may (admin on every site); else an empty string."""
    from django.urls import NoReverseMatch, reverse
    user = getattr(context.get("request"), "user", None)
    if not user or not services.can_manage_discovery(user):
        return ""
    try:
        return reverse("administration:site_create")
    except NoReverseMatch:
        return ""


@register.simple_tag
def intake_limits():
    return {"kb": services.MAX_PASTE_BYTES // 1024, "targets": services.MAX_TARGETS_PER_RUN}
