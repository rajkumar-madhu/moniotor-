"""Effective integration settings: database overrides fall back to Django settings / environment."""
from django.conf import settings
from django.core.cache import cache

_CACHE_KEY = "administration:integration_settings"
_SECRET_FIELDS = {
    "alertmanager": {"token"},
    "logs": {"token"},
    "stackstorm": {"api_key"},
    "nagios": {"password"},
    "email": {"email_host_password"},
    "backend_summary": {"token"},
}


def invalidate_cache():
    cache.delete(_CACHE_KEY)


def _all():
    cached = cache.get(_CACHE_KEY)
    if cached is not None:
        return cached
    from administration.models import IntegrationSetting

    rows = IntegrationSetting.objects.filter(tenant_key=settings.LINKEDEYE_TENANT)
    out = {row.slug: dict(row.values or {}) for row in rows}
    cache.set(_CACHE_KEY, out, 60)
    return out


def _slug(slug):
    return _all().get(slug) or {}


def _pick(slug, field, env_attr, default=None, cast=str):
    raw = _slug(slug).get(field)
    if raw not in (None, ""):
        try:
            return cast(raw) if cast is not str else str(raw).strip()
        except (TypeError, ValueError):
            pass
    if env_attr:
        val = getattr(settings, env_attr, default)
        if val not in (None, ""):
            return val
    return default


def _lines(slug, field, env_attr):
    raw = _slug(slug).get(field)
    if raw not in (None, ""):
        if isinstance(raw, list):
            return [str(x).strip() for x in raw if str(x).strip()]
        return [ln.strip() for ln in str(raw).splitlines() if ln.strip()]
    return list(getattr(settings, env_attr, []) or [])


def has_override(slug):
    return bool(_slug(slug))


def values_for_form(slug):
    """Non-secret values for edit forms; secrets are never returned."""
    data = dict(_slug(slug))
    for field in _SECRET_FIELDS.get(slug, ()):
        data.pop(field, None)
    return data


def save(slug, cleaned, user):
    from administration.models import IntegrationSetting

    row, _ = IntegrationSetting.objects.get_or_create(
        tenant_key=settings.LINKEDEYE_TENANT, slug=slug, defaults={"values": {}},
    )
    merged = dict(row.values or {})
    secrets = _SECRET_FIELDS.get(slug, set())
    for key, val in cleaned.items():
        if key in secrets and val in (None, ""):
            continue
        if val in (None, ""):
            merged.pop(key, None)
        else:
            merged[key] = val
    if not merged:
        row.delete()
    else:
        row.values = merged
        row.updated_by = user if user and user.is_authenticated else None
        row.save(update_fields=["values", "updated_by", "updated_at"])
    invalidate_cache()


# ---- runtime getters (used by probes, telemetry, notify, …) ----
def prometheus_url():
    return _pick("prometheus", "url", "PROMETHEUS_URL", "") or ""


def prometheus_timeout():
    return float(_pick("prometheus", "timeout", "PROMETHEUS_TIMEOUT", settings.PROMETHEUS_TIMEOUT, cast=float))


def prometheus_credential_id():
    """Vault row (onboarding.Credential, kind basic_auth) holding the login for the Prometheus URL, or None.

    The password itself is never kept here: only the id of the encrypted vault row. The id counts only while
    the URL is still the one it was saved for, so changing the URL to another server on the Integrations page
    can never send the old login to it."""
    slug = _slug("prometheus")
    cid = str(slug.get("credential_id") or "").strip()
    if not cid.isdigit() or (slug.get("credential_url") or "") != (prometheus_url() or ""):
        return None
    return int(cid)


def _flag(slug, field, default=False):
    v = _slug(slug).get(field)
    if v in (None, ""):
        return default
    return str(v).lower() in ("1", "true", "yes", "on")


# ---- Prometheus discovery (telemetry.discovery). Console-only: no environment fallback. ----
def prometheus_site_label():
    """Target label discovery reads the site from. Default "site"."""
    return (_slug("prometheus").get("site_label") or "").strip() or "site"


def prometheus_autocreate_ci():
    """Create a CMDB configuration item for every newly discovered node / Windows host."""
    return _flag("prometheus", "autocreate_ci")


def prometheus_autocreate_devices():
    """Create a network device for every newly discovered SNMP target."""
    return _flag("prometheus", "autocreate_devices")


def prometheus_autoregister_exporters():
    """Register every newly discovered scrape target in the Exporters inventory."""
    return _flag("prometheus", "autoregister_exporters")


def alertmanager_webhook_token():
    return _pick("alertmanager", "token", "ALERTMANAGER_WEBHOOK_TOKEN", "") or ""


def log_ingest_token():
    return _pick("logs", "token", "LOG_INGEST_TOKEN", "") or ""


def stackstorm_url():
    return _pick("stackstorm", "url", "STACKSTORM_URL", "") or ""


def stackstorm_api_key():
    return _pick("stackstorm", "api_key", "STACKSTORM_API_KEY", "") or ""


def notify_webhook_urls():
    return _lines("webhooks", "urls", "NOTIFY_WEBHOOK_URLS")


def notify_severities():
    raw = _slug("webhooks").get("severities")
    if raw not in (None, ""):
        if isinstance(raw, list):
            return [str(x).strip() for x in raw if str(x).strip()]
        return [p.strip() for p in str(raw).replace(",", " ").split() if p.strip()]
    return list(settings.NOTIFY_SEVERITIES or [])


def notify_emails():
    return _lines("email", "recipients", "NOTIFY_EMAILS")


def email_host():
    return _pick("email", "email_host", "EMAIL_HOST", settings.EMAIL_HOST) or ""


def email_port():
    return int(_pick("email", "email_port", "EMAIL_PORT", settings.EMAIL_PORT, cast=int))


def email_host_user():
    return _pick("email", "email_host_user", "EMAIL_HOST_USER", "") or ""


def email_host_password():
    return _pick("email", "email_host_password", "EMAIL_HOST_PASSWORD", "") or ""


def email_use_tls():
    v = _slug("email").get("email_use_tls")
    if v is not None and v != "":
        return str(v).lower() in ("1", "true", "yes", "on")
    return bool(getattr(settings, "EMAIL_USE_TLS", False))


def default_from_email():
    return _pick("email", "default_from_email", "DEFAULT_FROM_EMAIL", settings.DEFAULT_FROM_EMAIL) or ""


def nagios_url():
    return _pick("nagios", "url", "NAGIOS_URL", "") or ""


def nagios_user():
    return _pick("nagios", "user", "NAGIOS_USER", "") or ""


def nagios_password():
    return _pick("nagios", "password", "NAGIOS_PASSWORD", "") or ""


def nagios_timeout():
    return float(_pick("nagios", "timeout", "NAGIOS_TIMEOUT", settings.NAGIOS_TIMEOUT, cast=float))


def backend_summary_url():
    return _pick("backend_summary", "url", "LINKEDEYE_BACKEND_SUMMARY_URL", "") or ""


def backend_summary_token():
    return _pick("backend_summary", "token", "LINKEDEYE_BACKEND_SUMMARY_TOKEN", "") or ""
