"""Administration services: global-admin checks, password generation, integration status and tests, system
health. Everything here is safe to call from a view: functions never raise for an unreachable integration and
never return secret values (tokens, API keys, passwords, webhook paths)."""
import io
import logging
import os
import platform
import secrets
import socket
import string
import sys
import time
import urllib.error
import urllib.request
from datetime import timedelta
from urllib.parse import urlsplit

import django
from django.apps import apps
from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.models import Count, Max, Q
from django.utils import timezone

from .models import Heartbeat, IntegrationTest, LoginThrottle

log = logging.getLogger("linkedeye.administration")

PROBE_STALE_SECONDS = 600          # probe results older than this are stale ("warn if > 10 min old") ...
PROBE_STALE_GRACE_SECONDS = 300    # ... unless a longer check interval is configured: then interval + this grace
HEARTBEAT_INTERVAL = 60            # administration.heartbeat job interval
HEARTBEAT_STALE_SECONDS = 3 * HEARTBEAT_INTERVAL
TEST_TIMEOUT = 5
ROLE_LABELS = dict([("admin", "Admin"), ("operator", "Operator"), ("viewer", "Viewer")])


def tenant():
    return settings.LINKEDEYE_TENANT


# ------------------------------------------------------------------ access
def is_global_admin(user):
    """Admin console access: superusers, or users holding the admin role on *All sites*.

    A site-scoped admin passes core.access.can(user, "admin") but must not manage users, sites or integrations of
    the whole tenant (that would let them grant themselves wider access)."""
    if not user or not user.is_authenticated or not user.is_active:
        return False
    if user.is_superuser:
        return True
    from core.models import Membership
    return Membership.objects.filter(user=user, site__isnull=True, role="admin").exists()


def effective_role(user):
    """(role, note) for display: what core.access grants this user anywhere."""
    if user.is_superuser:
        return "admin", "Superuser"
    ms = list(user.le_memberships.all())
    if not ms:
        return ("operator", "Staff default") if user.is_staff else ("viewer", "Default")
    best = max(ms, key=lambda m: {"viewer": 1, "operator": 2, "admin": 3}[m.role])
    return best.role, ""


def forget_access_cache(user):
    """core.access caches memberships on the user object; drop it after changing them."""
    if hasattr(user, "_le_memberships"):
        del user._le_memberships


# ------------------------------------------------------------------ passwords
PASSWORD_ALPHABET = string.ascii_letters + string.digits + "-_.!@#%+="


def generate_password(user=None, length=20):
    """A random password that satisfies AUTH_PASSWORD_VALIDATORS (retries until it does)."""
    for _ in range(50):
        pw = "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(length))
        if not (any(c.islower() for c in pw) and any(c.isupper() for c in pw) and any(c.isdigit() for c in pw)):
            continue
        try:
            validate_password(pw, user)
            return pw
        except ValidationError:
            continue
    raise RuntimeError("could not generate a password that passes the configured validators")


# ------------------------------------------------------------------ sign-in throttling
LOGIN_MAX_FAILURES_DEFAULT = 5          # per username
LOGIN_MAX_FAILURES_PER_IP_DEFAULT = 20  # per client address
LOGIN_WINDOW_MINUTES_DEFAULT = 15


def _int_setting(name, default):
    try:
        v = int(_setting(name, default))
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


def login_limits():
    """(max failures per username, max failures per client address, window) from LINKEDEYE_LOGIN_MAX_FAILURES,
    LINKEDEYE_LOGIN_MAX_FAILURES_PER_IP and LINKEDEYE_LOGIN_WINDOW_MINUTES (settings or environment). The window
    is both the counting window and the lock-out time."""
    return (_int_setting("LOGIN_MAX_FAILURES", LOGIN_MAX_FAILURES_DEFAULT),
            _int_setting("LOGIN_MAX_FAILURES_PER_IP", LOGIN_MAX_FAILURES_PER_IP_DEFAULT),
            timedelta(minutes=_int_setting("LOGIN_WINDOW_MINUTES", LOGIN_WINDOW_MINUTES_DEFAULT)))


def _throttle_keys(username, ip):
    keys = []
    name = str(username or "").strip().lower()[:191]
    if name:
        keys.append(("user", name))
    if ip:
        keys.append(("ip", str(ip)[:191]))
    return keys


def login_lock_remaining(username, ip, now=None):
    """(kind, timedelta) of the longest active lock on the username or client address, or None."""
    now = now or timezone.now()
    keys = _throttle_keys(username, ip)
    if not keys:
        return None
    q = None
    for kind, value in keys:
        q = Q(kind=kind, value=value) if q is None else q | Q(kind=kind, value=value)
    row = LoginThrottle.objects.filter(q, locked_until__gt=now).order_by("-locked_until").first()
    return (row.kind, row.locked_until - now) if row else None


def login_failed(username, ip, now=None):
    """Count one failed sign-in for the username and the client address. Returns the kinds that became locked by
    this failure (e.g. ["user"]). Never raises: a throttle error must not turn a wrong password into a 500."""
    now = now or timezone.now()
    max_user, max_ip, window = login_limits()
    locked = []
    try:
        for kind, value in _throttle_keys(username, ip):
            limit = max_user if kind == "user" else max_ip
            for _attempt in range(2):   # a concurrent first failure may win the insert; retry the update once
                try:
                    with transaction.atomic():
                        row = LoginThrottle.objects.select_for_update().filter(kind=kind, value=value).first()
                        if row is None:
                            row = LoginThrottle.objects.create(kind=kind, value=value, failures=0, window_started_at=now)
                        if row.window_started_at <= now - window:
                            row.failures, row.window_started_at = 0, now
                        row.failures += 1
                        row.updated_at = now
                        if row.failures >= limit and not (row.locked_until and row.locked_until > now):
                            row.locked_until = now + window
                            locked.append(kind)
                        row.save(update_fields=["failures", "window_started_at", "locked_until", "updated_at"])
                    break
                except IntegrityError:
                    continue
        # housekeeping: forget counters that are neither counting nor locking any more
        LoginThrottle.objects.filter(updated_at__lt=now - 2 * window).exclude(locked_until__gt=now).delete()
    except Exception:
        log.exception("could not record a failed sign-in")
    return locked


def login_succeeded(username):
    """A successful sign-in clears the username's counter (the client address keeps its count)."""
    try:
        for kind, value in _throttle_keys(username, None):
            LoginThrottle.objects.filter(kind=kind, value=value).delete()
    except Exception:
        log.exception("could not clear the sign-in throttle")


# ------------------------------------------------------------------ helpers
def safe_url(url):
    """scheme://host[:port]/path without credentials, query or fragment (for display)."""
    url = str(url or "").strip()
    if not url:
        return ""
    try:
        p = urlsplit(url)
        host = p.hostname or ""
        if p.port:
            host += f":{p.port}"
    except ValueError:
        return "(invalid URL)"
    if not p.scheme or not host:
        return "(invalid URL)"
    return f"{p.scheme}://{host}{p.path}"


def url_host(url):
    try:
        p = urlsplit(str(url or "").strip())
        return p.hostname or "(invalid URL)"
    except ValueError:
        return "(invalid URL)"


def _setting(name, default=""):
    """Setting value, falling back to the LINKEDEYE_<NAME> environment variable (as telemetry.nagios does)."""
    v = getattr(settings, name, None)
    if v in (None, ""):
        v = os.environ.get(f"LINKEDEYE_{name}", default)
    return v


def _reverse(name):
    from django.urls import NoReverseMatch, reverse
    try:
        return reverse(name)
    except NoReverseMatch:
        return ""


def _latest_event(**filters):
    from core.models import Event
    return Event.objects.filter(tenant_key=tenant(), **filters).order_by("-occurred_at").values_list("occurred_at", flat=True).first()


def _token_detail(token):
    if not token:
        return "Not set"
    if len(token) < 24:
        return f"Set ({len(token)} characters; use at least 24)"
    return "Set"


def last_tests():
    out = {}
    for t in IntegrationTest.objects.filter(tenant_key=tenant()).order_by("-tested_at")[:200]:
        out.setdefault(t.integration, t)
    return out


# ------------------------------------------------------------------ integrations
def integrations():
    """Status of every external integration. Never includes secret values."""
    from administration import integration_config as ic

    tests = last_tests()
    items = []

    prom = ic.prometheus_url()
    disc = discovery_status()
    auto = [label for flag, label in ((ic.prometheus_autocreate_ci(), "CMDB items"),
                                      (ic.prometheus_autocreate_devices(), "devices"),
                                      (ic.prometheus_autoregister_exporters(), "exporters")) if flag]
    items.append({
        "key": "prometheus", "name": "Prometheus", "icon": "activity", "configured": bool(prom),
        "purpose": "Metrics, targets, alert rules and target discovery for the Monitoring pages.",
        "env": ["LINKEDEYE_PROMETHEUS_URL", "LINKEDEYE_PROMETHEUS_TIMEOUT"],
        "details": [("Server", safe_url(prom) or "–"), ("Timeout", f"{ic.prometheus_timeout():g} s"),
                    ("Source", "Console" if ic.values_for_form("prometheus").get("url") else "Environment"),
                    ("Site label", ic.prometheus_site_label()),
                    ("Auto-create", ", ".join(auto) if auto else "Off"),
                    ("Discovered targets", disc["summary"]),
                    ("Last discovery", disc["when"])],
        "testable": True, "test_label": "Run 'up' query", "last_test": tests.get("prometheus"),
        "editable": True, "discover": bool(prom), "discovery": disc,
    })

    am = ic.alertmanager_webhook_token()
    am_url = _reverse("telemetry:alertmanager_webhook") or "/api/v1/alertmanager/"
    last_alert = _latest_event(source="telemetry", kind="alert")
    items.append({
        "key": "alertmanager", "name": "Alertmanager webhook", "icon": "bell", "configured": bool(am),
        "purpose": "Alertmanager posts firing/resolved alerts here with this bearer token.",
        "env": ["LINKEDEYE_ALERTMANAGER_WEBHOOK_TOKEN"],
        "details": [("Endpoint", f"POST {am_url}"), ("Bearer token", _token_detail(am)),
                    ("Last alert received", last_alert)],
        "testable": False, "last_test": None,
        "warning": bool(am) and len(am) < 24,
        "editable": True,
    })

    li = ic.log_ingest_token()
    li_url = _reverse("observe:log_ingest") or "/api/v1/logs/"
    last_log = _latest_event(source="observe", kind="log")
    items.append({
        "key": "logs", "name": "Log ingest", "icon": "logs", "configured": bool(li),
        "purpose": "Log shippers (Fluent Bit, Vector, Promtail…) post log lines with this bearer token.",
        "env": ["LINKEDEYE_LOG_INGEST_TOKEN"],
        "details": [("Endpoint", f"POST {li_url}"), ("Bearer token", _token_detail(li)),
                    ("Last error-level log event", last_log)],
        "testable": False, "last_test": None,
        "warning": bool(li) and len(li) < 24,
        "editable": True,
    })

    st2 = ic.stackstorm_url()
    items.append({
        "key": "stackstorm", "name": "StackStorm", "icon": "bot", "configured": bool(st2),
        "purpose": "Automation runbooks and event workflows trigger StackStorm webhooks.",
        "env": ["LINKEDEYE_STACKSTORM_URL", "LINKEDEYE_STACKSTORM_API_KEY"],
        "details": [("Webhook URL", safe_url(st2) or "–"),
                    ("API key", "Set" if ic.stackstorm_api_key() else "Not set"),
                    ("Source", "Console" if ic.has_override("stackstorm") else "Environment")],
        "testable": True, "test_label": "Ping", "last_test": tests.get("stackstorm"),
        "editable": True,
    })

    hooks = ic.notify_webhook_urls()
    sev = ", ".join(ic.notify_severities()) or "none"
    items.append({
        "key": "webhooks", "name": "Notification webhooks", "icon": "share", "configured": bool(hooks),
        "purpose": "JSON POST for every critical/warning event (Slack, Teams, Apprise, StackStorm…).",
        "env": ["LINKEDEYE_NOTIFY_WEBHOOK_URLS", "LINKEDEYE_NOTIFY_SEVERITIES"],
        "details": [("Endpoints", f"{len(hooks)} configured" if hooks else "None"),
                    ("Hosts", ", ".join(sorted({url_host(u) for u in hooks})) or "–"),
                    ("Severities", sev)],
        "testable": True, "test_key": "notify", "test_label": "Send test notification",
        "last_test": tests.get("notify"),
        "editable": True,
    })

    emails = ic.notify_emails()
    backend = getattr(settings, "EMAIL_BACKEND", "")
    items.append({
        "key": "email", "name": "E-mail notifications", "icon": "mail", "configured": bool(emails),
        "purpose": "E-mail for every critical/warning event.",
        "env": ["LINKEDEYE_NOTIFY_EMAILS", "LINKEDEYE_EMAIL_HOST", "LINKEDEYE_EMAIL_PORT", "LINKEDEYE_EMAIL_HOST_USER",
                "LINKEDEYE_EMAIL_HOST_PASSWORD", "LINKEDEYE_EMAIL_USE_TLS", "LINKEDEYE_DEFAULT_FROM_EMAIL"],
        "details": [("Recipients", f"{len(emails)} address(es)" if emails else "None"),
                    ("Backend", backend.rsplit(".", 2)[-2] if backend.count(".") >= 2 else backend or "–"),
                    ("SMTP server", f"{ic.email_host()}:{ic.email_port()}"),
                    ("TLS", "On" if ic.email_use_tls() else "Off"),
                    ("SMTP password", "Set" if ic.email_host_password() else "Not set"),
                    ("From", ic.default_from_email() or "–"),
                    ("Source", "Console" if ic.has_override("email") else "Environment")],
        "testable": True, "test_key": "notify", "test_label": "Send test notification",
        "last_test": tests.get("notify"),
        "editable": True,
    })

    agent_key = getattr(settings, "AGENT_API_KEY", "") or ""
    items.append({
        "key": "agent", "name": "Ops Agent", "icon": "bot", "configured": bool(agent_key.strip()),
        "purpose": "In-console assistant over live checks, alerts, tickets and runbooks (OpenAI-compatible).",
        "env": ["LINKEDEYE_AGENT_API_KEY", "LINKEDEYE_AGENT_BASE_URL", "LINKEDEYE_AGENT_MODEL",
                "LINKEDEYE_AGENT_EMBEDDING_MODEL", "LINKEDEYE_AGENT_TIMEOUT"],
        "details": [("API key", "Set" if agent_key.strip() else "Not set"),
                    ("Model", getattr(settings, "AGENT_MODEL", "") or "–"),
                    ("Endpoint", safe_url(getattr(settings, "AGENT_BASE_URL", "")) or "–")],
        "testable": True, "test_label": "Ping the model", "last_test": tests.get("agent"),
    })

    nag = ic.nagios_url()
    items.append({
        "key": "nagios", "name": "Nagios", "icon": "server", "configured": bool(nag),
        "purpose": "Host and service status for Monitoring › Nagios Status (statusjson.cgi).",
        "env": ["LINKEDEYE_NAGIOS_URL", "LINKEDEYE_NAGIOS_USER", "LINKEDEYE_NAGIOS_PASSWORD"],
        "details": [("Server", safe_url(nag) or "–"),
                    ("User", "Set" if ic.nagios_user() else "Not set"),
                    ("Password", "Set" if ic.nagios_password() else "Not set"),
                    ("Source", "Console" if ic.has_override("nagios") else "Environment")],
        "testable": False, "last_test": None,
        "editable": True,
    })
    channels = notify_channels()
    for it in items:
        it.setdefault("test_key", it["key"])
        it.setdefault("warning", False)
        it.setdefault("editable", False)
        it.setdefault("discover", False)
        it["test_enabled"] = it["testable"] and (channels > 0 if it["test_key"] == "notify" else it["configured"])
    return items


# ------------------------------------------------------------------ Prometheus discovery (telemetry.discovery)
def discovery_status():
    """What discovery has gathered so far: counts, the last run and its error. Cheap queries only."""
    out = {"available": False, "targets": 0, "hosts": 0, "unlinked": 0, "last_run": None, "error": "",
           "summary": "–", "when": "Never"}
    DT = _opt_model("telemetry", "DiscoveredTarget")
    if DT is None:
        return out
    try:
        present = DT.objects.for_tenant().present()
        out["targets"] = present.count()
        out["hosts"] = present.hosts().count()
        out["unlinked"] = present.unlinked_hosts().count()
        run = _opt_model("telemetry", "DiscoveryRun").objects.filter(tenant_key=tenant()).order_by("-started_at").first()
    except Exception:   # telemetry not migrated yet
        return out
    out["available"] = True
    out["last_run"] = run
    out["error"] = (run.error if run is not None and not run.ok else "") or ""
    out["summary"] = (f"{out['targets']} target(s), {out['hosts']} host(s), {out['unlinked']} not in the CMDB"
                      if out["targets"] else "None yet")
    if run is not None:
        out["when"] = run.started_at
    return out


def _opt_model(app_label, name):
    try:
        return apps.get_model(app_label, name)
    except LookupError:
        return None


def onboarding_checklist():
    """"Is Prometheus actually feeding the product?" – the steps an operator must finish after connecting it.

    Each step is {key, label, done, detail, url, cta}. Only cheap queries: counts and one row lookup each."""
    from administration import integration_config as ic

    prom = ic.prometheus_url()
    disc = discovery_status()
    integrations_url = _reverse("administration:integrations") or ""
    steps = [{
        "key": "prometheus", "label": "Prometheus connected", "done": bool(prom),
        "detail": safe_url(prom) if prom else "Set the server URL on the Prometheus card below.",
        "url": f"{integrations_url}#prometheus", "cta": "Connect Prometheus",
    }, {
        "key": "targets", "label": "Targets discovered", "done": bool(prom) and disc["targets"] > 0,
        "detail": (f"{disc['targets']} target(s) across {disc['hosts']} host(s)" if disc["targets"]
                   else ("Discovery has not found a target yet – run it now."
                         if prom else "Connect Prometheus first.")),
        "url": _reverse("telemetry:discovery") or f"{integrations_url}#prometheus", "cta": "Open Discovery",
    }]

    CI = _opt_model("cmdb", "ConfigurationItem")
    in_cmdb = total_hosts = 0
    if CI is not None and disc["available"]:
        try:
            total_hosts = disc["hosts"]
            in_cmdb = total_hosts - disc["unlinked"]
        except Exception:
            total_hosts = in_cmdb = 0
    steps.append({
        "key": "cmdb", "label": "Hosts in the CMDB", "done": bool(total_hosts) and disc["unlinked"] == 0,
        "detail": (f"{in_cmdb} of {total_hosts} discovered host(s)" if total_hosts
                   else "Nothing to add yet – discover targets first."),
        "url": _reverse("telemetry:discovery") or "", "cta": "Review hosts",
    })

    am = ic.alertmanager_webhook_token()
    steps.append({
        "key": "alertmanager", "label": "Alertmanager webhook configured", "done": bool(am),
        "detail": ("Bearer token set – point Alertmanager at "
                   f"{_reverse('telemetry:alertmanager_webhook') or '/api/v1/alertmanager/'}") if am
                  else "No bearer token: Alertmanager cannot post alerts here.",
        "url": f"{integrations_url}#alertmanager", "cta": "Set the token",
    })

    channels = notify_channels()
    steps.append({
        "key": "channels", "label": "Notification channel configured", "done": channels > 0,
        "detail": (f"{channels} channel(s): webhooks and/or e-mail" if channels
                   else "Nobody is notified when an alert fires."),
        "url": f"{integrations_url}#webhooks", "cta": "Add a channel",
    })

    Alert = _opt_model("telemetry", "Alert")
    first_alert = None
    if Alert is not None:
        try:
            first_alert = Alert.objects.filter(tenant_key=tenant()).order_by("created_at").values_list(
                "created_at", flat=True).first()
        except Exception:
            first_alert = None
    steps.append({
        "key": "alert", "label": "First alert received", "done": first_alert is not None,
        "detail": "Alertmanager has posted an alert." if first_alert is not None
                  else "No alert has arrived yet (nothing may be firing).",
        "url": _reverse("telemetry:alertmanager") or "", "cta": "Open Alert Manager", "when": first_alert,
    })
    done = sum(1 for s in steps if s["done"])
    return {"steps": steps, "done": done, "total": len(steps), "complete": done == len(steps),
            "pct": round(100.0 * done / len(steps))}


def integration_form_initial(slug):
    from administration import integration_config as ic

    data = ic.values_for_form(slug)
    if slug == "webhooks":
        urls = ic.notify_webhook_urls() if ic.has_override("webhooks") else []
        return {"urls": "\n".join(urls), "severities": ", ".join(ic.notify_severities())}
    if slug == "email":
        base = {
            "email_use_tls": ic.email_use_tls(),
            "default_from_email": ic.default_from_email(),
        }
        if ic.has_override("email"):
            return {
                **data,
                **base,
                "recipients": "\n".join(ic.notify_emails()),
                "email_host": ic.email_host(),
                "email_port": ic.email_port(),
                "email_host_user": ic.email_host_user(),
            }
        return {**data, **base, "email_port": ic.email_port()}
    if slug == "prometheus":
        data.setdefault("timeout", ic.prometheus_timeout())
        if ic.has_override("prometheus") and not data.get("url"):
            data["url"] = ic.prometheus_url() or ""
    if slug == "stackstorm" and ic.has_override("stackstorm") and not data.get("url"):
        data["url"] = ic.stackstorm_url() or ""
    if slug == "nagios":
        data.setdefault("timeout", ic.nagios_timeout())
        if ic.has_override("nagios"):
            data.setdefault("url", ic.nagios_url() or "")
            data.setdefault("user", ic.nagios_user())
    return data


def _record(request, integration, ok, message, started):
    user = getattr(request, "user", None)
    authed = user is not None and user.is_authenticated
    return IntegrationTest.objects.create(
        integration=integration, ok=ok, message=str(message)[:500],
        duration_ms=round((time.monotonic() - started) * 1000, 1),
        tested_by=user if authed else None, tested_by_name=user.get_username() if authed else "system",
    )


def test_prometheus():
    """Instant query `up`. Returns (ok, message)."""
    from core import prometheus
    if not prometheus.configured():
        return False, "Prometheus is not connected (set LINKEDEYE_PROMETHEUS_URL)."
    try:
        rows = prometheus.query("up")
    except prometheus.PrometheusError as e:
        return False, str(e)
    except Exception as e:  # never surface a traceback
        log.exception("prometheus test failed")
        return False, f"Unexpected error: {e.__class__.__name__}"
    up = sum(1 for r in rows if r.get("value") == 1)
    if not rows:
        return True, "Prometheus answered, but the 'up' query returned no targets."
    return True, f"Prometheus answered: {up} of {len(rows)} target(s) up."


def notify_channels():
    from administration import integration_config as ic

    return len(ic.notify_webhook_urls()) + (1 if ic.notify_emails() else 0)


def test_notify(actor="system"):
    """Send a synthetic warning event through core.notify.deliver. The event is never saved, so it does not show up
    in the event stream or open incidents. Returns (ok, message)."""
    from core.models import Event
    from core.notify import deliver
    channels = notify_channels()
    if not channels:
        return False, "No notification channel is configured (set LINKEDEYE_NOTIFY_WEBHOOK_URLS or LINKEDEYE_NOTIFY_EMAILS)."
    from administration import integration_config as ic

    if "warning" not in ic.notify_severities():
        return False, "Warning events are not delivered: add 'warning' to notification severities."
    ev = Event(tenant_key=tenant(), source="administration", kind="test", severity="warning",
               title="LinkedEye test notification",
               message=f"This is a test notification sent by {actor} from Administration › Integrations. No action is needed.",
               occurred_at=timezone.now(), object_type="administration.test", object_id="")
    try:
        sent = deliver(ev)
    except Exception as e:  # deliver() swallows channel errors itself; this is a last resort
        log.exception("test notification failed")
        return False, f"Unexpected error: {e.__class__.__name__}"
    if sent == channels:
        return True, f"Test notification delivered to {sent} of {channels} channel(s)."
    return False, f"Test notification delivered to {sent} of {channels} channel(s); see the server log for failures."


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Do not follow redirects: the API key header must not be forwarded to another host."""

    def redirect_request(self, *args, **kwargs):
        return None


def test_stackstorm():
    """GET the configured StackStorm URL (no redirects, short timeout). Returns (ok, message). Never raises."""
    try:  # the same URL builder the automation module uses for triggers
        from automation.services import stackstorm_endpoint
        url = stackstorm_endpoint()
    except ImportError:  # pragma: no cover - automation not installed
        url = (getattr(settings, "STACKSTORM_URL", "") or "").strip()
    if not url:
        return False, "StackStorm is not configured (set LINKEDEYE_STACKSTORM_URL)."
    p = urlsplit(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        return False, "LINKEDEYE_STACKSTORM_URL must be an http(s) URL."
    from administration import integration_config as ic

    headers = {"Accept": "application/json", "User-Agent": "LinkedEye-Admin/1.0"}
    if ic.stackstorm_api_key():
        headers["St2-Api-Key"] = ic.stackstorm_api_key()
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with opener.open(req, timeout=TEST_TIMEOUT) as r:  # noqa: S310 - operator-configured URL
            return True, f"StackStorm answered HTTP {r.status}."
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return False, f"StackStorm rejected the API key (HTTP {e.code}); check LINKEDEYE_STACKSTORM_API_KEY."
        if e.code in (404, 405):
            return True, f"StackStorm is reachable (HTTP {e.code} for a GET on a webhook URL is expected)."
        if 300 <= e.code < 400:
            return False, f"StackStorm redirected (HTTP {e.code}); set LINKEDEYE_STACKSTORM_URL to the final URL."
        return False, f"StackStorm returned HTTP {e.code}."
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as e:
        return False, f"StackStorm unreachable: {getattr(e, 'reason', e)}"
    except Exception as e:
        log.exception("stackstorm ping failed")
        return False, f"Unexpected error: {e.__class__.__name__}"


def test_agent():
    """Tiny chat completion against the configured model. Never raises; never returns the API key."""
    try:
        from agent.llm import ping
    except ImportError:  # pragma: no cover - agent not installed
        return False, "The Ops Agent module is not installed."
    return ping()


TESTS = {"prometheus": test_prometheus, "notify": None, "stackstorm": test_stackstorm, "agent": test_agent}


def run_test(request, key):
    """Run one integration test, store an IntegrationTest row and return it."""
    started = time.monotonic()
    if key == "notify":
        actor = request.user.get_username() if getattr(request, "user", None) and request.user.is_authenticated else "system"
        ok, msg = test_notify(actor)
    else:
        ok, msg = TESTS[key]()
    return _record(request, key, ok, msg, started)


# ------------------------------------------------------------------ system health
def _check(key, label, status, value, detail="", icon="info"):
    return {"key": key, "label": label, "status": status, "value": value, "detail": detail, "icon": icon}


def database_check():
    started = time.monotonic()
    vendor = connection.vendor
    name = settings.DATABASES["default"].get("NAME", "")
    if vendor == "sqlite":
        name = os.path.basename(str(name))
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
    except Exception as e:
        return _check("database", "Database", "crit", "Unreachable", f"{vendor}: {e.__class__.__name__}", "database")
    ms = (time.monotonic() - started) * 1000
    return _check("database", "Database", "ok", "Reachable", f"{vendor} · {name} · {ms:.0f} ms", "database")


def pending_migrations():
    """List of (app_label, migration_name) not yet applied, or raises."""
    from django.db.migrations.executor import MigrationExecutor
    executor = MigrationExecutor(connection)
    plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
    return [(m.app_label, m.name) for m, backwards in plan if not backwards]


def migrations_check():
    try:
        pending = pending_migrations()
    except Exception as e:
        return _check("migrations", "Migrations", "crit", "Unknown", f"Could not read migration state: {e.__class__.__name__}", "layers"), []
    if pending:
        apps_ = sorted({a for a, _ in pending})
        return _check("migrations", "Migrations", "warn", f"{len(pending)} pending",
                      "Run manage.py migrate · " + ", ".join(apps_), "layers"), pending
    return _check("migrations", "Migrations", "ok", "Up to date", "All migrations applied", "layers"), []


def _age(dt):
    return None if dt is None else max(0, int((timezone.now() - dt).total_seconds()))


def _fmt_age(seconds):
    if seconds is None:
        return "never"
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} d ago"


def stale_after(longest_interval):
    """Seconds after which the newest result counts as stale: a check only runs once per interval, so the
    threshold follows the longest enabled interval (plus a grace period) and is never below PROBE_STALE_SECONDS."""
    return max(PROBE_STALE_SECONDS, int(longest_interval or 0) + PROBE_STALE_GRACE_SECONDS)


def probe_freshness():
    """Latest UrlResult / DomainCheck times. Returns a list of dicts with status ok/warn/mute."""
    from domainmonitor.models import Domain, DomainCheck
    from urlchecker.models import UrlCheck, UrlResult
    t = tenant()
    rows = []
    urls = UrlCheck.objects.filter(tenant_key=t, enabled=True).aggregate(n=Count("id"), longest=Max("interval_seconds"))
    domains = (Domain.objects.filter(tenant_key=t, is_active=True, monitor_enabled=True)
               .aggregate(n=Count("id"), longest=Max("check_interval_seconds")))
    sources = [
        ("url", "URL checks", urls["n"], stale_after(urls["longest"]),
         UrlResult.objects.filter(url_check__tenant_key=t).order_by("-checked_at").values_list("checked_at", flat=True).first()),
        ("domain", "Domain checks", domains["n"], stale_after(domains["longest"]),
         DomainCheck.objects.filter(domain__tenant_key=t).order_by("-checked_at").values_list("checked_at", flat=True).first()),
    ]
    for key, label, enabled, threshold, latest in sources:
        age = _age(latest)
        if not enabled:
            status, note = "mute", "Nothing enabled to check"
        elif latest is None:
            status, note = "warn", "No result stored yet — is manage.py run_probes running?"
        elif age > threshold:
            status, note = "warn", f"Older than {threshold // 60} min — is manage.py run_probes running?"
        else:
            status, note = "ok", "Fresh"
        rows.append({"key": key, "label": label, "enabled": enabled, "latest": latest, "age": age,
                     "age_text": _fmt_age(age), "status": status, "note": note, "stale_after": threshold,
                     "stale_minutes": threshold // 60})
    return rows


def scheduler_check():
    hb = Heartbeat.objects.filter(key="scheduler").first()
    if hb is None:
        return _check("scheduler", "Scheduler", "warn", "No heartbeat",
                      "manage.py run_probes has not reported yet (job administration.heartbeat).", "clock"), None
    age = hb.age_seconds
    where = f"{hb.host or 'unknown host'}" + (f" · pid {hb.pid}" if hb.pid else "")
    if age > HEARTBEAT_STALE_SECONDS:
        return _check("scheduler", "Scheduler", "warn", f"Last seen {_fmt_age(age)}",
                      f"run_probes stopped reporting ({where}).", "clock"), hb
    return _check("scheduler", "Scheduler", "ok", "Running", f"Heartbeat {_fmt_age(age)} · {where}", "clock"), hb


def event_counts(hours=24):
    from core.models import Event
    since = timezone.now() - timedelta(hours=hours)
    qs = Event.objects.filter(tenant_key=tenant(), occurred_at__gte=since)
    by_sev = dict(qs.values_list("severity").annotate(n=Count("id")).order_by())
    sev = [{"key": k, "label": label, "count": by_sev.get(k, 0)} for k, label in Event.SEVERITY]
    sources = list(qs.values("source").annotate(n=Count("id")).order_by("-n", "source")[:8])
    return {"severity": sev, "total": sum(by_sev.values()), "sources": sources, "hours": hours}


def _fmt_interval(seconds):
    s = int(seconds)
    if s % 3600 == 0 and s >= 3600:
        return f"every {s // 3600} h"
    if s % 60 == 0 and s >= 60:
        return f"every {s // 60} min"
    return f"every {s} s"


def registered_jobs():
    from core.jobs import JOBS
    out = []
    for name, job in sorted(JOBS.items()):
        func = job.get("func")
        out.append({"name": name, "interval": job.get("interval"), "interval_text": _fmt_interval(job.get("interval") or 0),
                    "func": f"{getattr(func, '__module__', '')}.{getattr(func, '__qualname__', getattr(func, '__name__', ''))}".strip(".")})
    return out


def _db_version():
    try:
        if connection.vendor == "sqlite":
            import sqlite3
            return f"SQLite {sqlite3.sqlite_version}"
        with connection.cursor() as cur:
            if connection.vendor == "postgresql":
                cur.execute("SHOW server_version")
                return f"PostgreSQL {cur.fetchone()[0]}"
            if connection.vendor == "mysql":
                cur.execute("SELECT VERSION()")
                return f"MySQL {cur.fetchone()[0]}"
    except Exception:
        pass
    return connection.vendor


def versions(pending=()):
    """Platform versions plus every local app with its latest applied migration."""
    from django.db.migrations.recorder import MigrationRecorder
    platform_rows = [("LinkedEye", getattr(settings, "LINKEDEYE_VERSION", "–")), ("Django", django.get_version()),
                     ("Python", platform.python_version()), ("Database", _db_version()),
                     ("OS", f"{platform.system()} {platform.release()}"), ("Tenant", tenant())]
    try:
        applied = MigrationRecorder(connection).applied_migrations()
    except Exception:
        applied = {}
    latest = {}
    for app_label, name in applied:
        if app_label not in latest or name > latest[app_label]:
            latest[app_label] = name
    pend = {}
    for app_label, _ in pending:
        pend[app_label] = pend.get(app_label, 0) + 1
    base = str(settings.BASE_DIR)
    rows = []
    for cfg in apps.get_app_configs():
        if not str(cfg.path).startswith(base) or "site-packages" in str(cfg.path):
            continue
        mod = sys.modules.get(cfg.name)
        rows.append({"label": cfg.label, "name": cfg.verbose_name,
                     "version": getattr(mod, "__version__", "") or getattr(settings, "LINKEDEYE_VERSION", ""),
                     "migration": latest.get(cfg.label, ""), "pending": pend.get(cfg.label, 0)})
    return platform_rows, rows


def deployment_checks():
    rows = []
    rows.append(("DEBUG", "warn" if settings.DEBUG else "ok", "On — development only" if settings.DEBUG else "Off"))
    rows.append(("Secret key", "warn" if settings.SECRET_KEY == "dev-only-insecure-key" else "ok",
                 "Development key — set LINKEDEYE_SECRET_KEY" if settings.SECRET_KEY == "dev-only-insecure-key" else "Set"))
    rows.append(("Allowed hosts", "ok" if settings.ALLOWED_HOSTS else "warn", ", ".join(settings.ALLOWED_HOSTS) or "None — set LINKEDEYE_ALLOWED_HOSTS"))
    secure = getattr(settings, "SESSION_COOKIE_SECURE", False) and getattr(settings, "CSRF_COOKIE_SECURE", False)
    rows.append(("Secure cookies", "ok" if secure else "warn", "On" if secure else "Off (HTTPS cookies disabled)"))
    return rows


STATUS_RANK = {"ok": 0, "mute": 0, "warn": 1, "crit": 2}


def health():
    db = database_check()
    mig, pending = migrations_check()
    sched, hb = scheduler_check()
    probes = probe_freshness()
    probe_status = max((p["status"] for p in probes), key=lambda s: STATUS_RANK[s], default="mute")
    probe_card = _check("probes", "Probe results", probe_status,
                        "Fresh" if probe_status == "ok" else ("Stale" if probe_status == "warn" else "Idle"),
                        " · ".join(f"{p['label']}: {p['age_text']}" for p in probes), "radar")
    checks = [db, mig, sched, probe_card]
    overall = max((c["status"] for c in checks), key=lambda s: STATUS_RANK[s])
    platform_rows, app_rows = versions(pending)
    return {"checks": checks, "overall": overall, "pending": pending, "heartbeat": hb, "probes": probes,
            "events": event_counts(24), "jobs": registered_jobs(), "platform": platform_rows, "apps": app_rows,
            "deployment": deployment_checks(), "stale_minutes": max(p["stale_minutes"] for p in probes)}


def health_summary():
    """Cheap overall status for the index card (no migration graph walk when the database is down)."""
    db = database_check()
    if db["status"] == "crit":
        return "crit"
    sched, _ = scheduler_check()
    statuses = [db["status"], sched["status"]] + [p["status"] for p in probe_freshness()]
    try:
        if pending_migrations():
            statuses.append("warn")
    except Exception:
        statuses.append("crit")
    return max(statuses, key=lambda s: STATUS_RANK[s])


# ------------------------------------------------------------------ sites
def site_usage():
    """{site name: {"urls": n, "domains": n}} for the tenant."""
    from domainmonitor.models import Domain
    from urlchecker.models import UrlCheck
    t = tenant()
    usage = {}
    for name, n in UrlCheck.objects.filter(tenant_key=t).values_list("site").annotate(n=Count("id")).order_by():
        usage.setdefault(name, {"urls": 0, "domains": 0})["urls"] = n
    for name, n in Domain.objects.filter(tenant_key=t).values_list("site_key").annotate(n=Count("id")).order_by():
        usage.setdefault(name, {"urls": 0, "domains": 0})["domains"] = n
    return usage


# Every model that stores a site *name* (not a foreign key): (app label, model, field, label shown to admins).
# core.Membership points at core.Site by foreign key, so it follows a rename by itself and is not listed.
SITE_NAME_FIELDS = [
    ("urlchecker", "UrlCheck", "site", "URL checks"),
    ("domainmonitor", "Domain", "site_key", "Domains"),
    ("tcpcheck", "TcpCheck", "site", "TCP checks"),
    ("network", "Device", "site", "Network devices"),
    ("network", "DiscoveryRun", "site", "Discovery runs"),
    ("cmdb", "ConfigurationItem", "site", "Configuration items"),
    ("observe", "Exporter", "site", "Exporters"),
    ("observe", "LogEntry", "site", "Log entries"),
    ("telemetry", "Alert", "site", "Alerts"),
    ("telemetry", "DiscoveredTarget", "site", "Discovered Prometheus targets"),
    ("servicedesk", "Ticket", "site", "Tickets"),
    ("automation", "Runbook", "site", "Runbooks"),
    ("automation", "Rule", "site", "Workflow rules"),
    ("automation", "Execution", "site", "Automation executions"),
    ("bodeod", "Mode", "site", "BOD/EOD modes"),
    ("reports", "ScheduledReport", "site", "Scheduled reports"),
    ("reports", "ReportRun", "site", "Report runs"),
    ("core", "Event", "site", "Events"),
    ("agent", "Thread", "site", "Ops Agent threads"),
    ("agent", "KnowledgeChunk", "site", "Ops Agent knowledge"),
    ("ipam", "VLAN", "site", "VLANs"),
    ("ipam", "Subnet", "site", "Subnets"),
    ("ipam", "IPAddress", "site", "IP addresses"),
    ("ipam", "DNSZone", "site", "DNS zones"),
    ("dcim", "Room", "site", "Data centre rooms"),
    ("dcim", "Rack", "site", "Racks"),
    ("dcim", "CoolingUnit", "site", "Cooling units"),
    ("compute", "Cluster", "site", "Compute clusters"),
    ("onboarding", "OnboardingRun", "site", "Onboarding runs"),
    ("onboarding", "Credential", "site", "Stored credentials"),
    ("k8s", "KubeCluster", "site", "Kubernetes clusters"),
    ("k8s", "Namespace", "site", "Kubernetes namespaces"),
    ("k8s", "ClientProfile", "site", "Kubernetes client profiles"),
    ("k8s", "ImportRun", "site", "Kubernetes inventory imports"),
    ("k8s", "Ingress", "site", "Kubernetes ingress rules"),
    ("k8s", "Certificate", "site", "Kubernetes certificates"),
    ("k8s", "SecretRef", "site", "Kubernetes secret references"),
    ("k8s", "ToolInstance", "site", "Kubernetes monitoring and CI/CD tools"),
    ("k8s", "InventoryIssue", "site", "Kubernetes inventory issues"),
    # APM: a Source holds the site name. Its adapter cells, runs, history, processes, logs and retry requests carry no site
    # of their own (they belong to one source and are scoped through source__site), so a rename follows by itself.
    ("apm", "Source", "site", "APM sources"),
    # Hypervisor: a vCenter source holds the site name; its hosts, datastores, VM records, snapshots, events and control requests
    # belong to one source and are scoped through source__site, so a rename follows by itself.
    ("hypervisor", "VCenterSource", "site", "vCenter sources"),
]


def _site_name_querysets(name):
    """(label, queryset of this tenant's records whose site field equals `name`, field) for installed models."""
    t = tenant()
    for app_label, model_name, field, label in SITE_NAME_FIELDS:
        try:
            model = apps.get_model(app_label, model_name)
            model._meta.get_field(field)
        except LookupError:
            continue
        except Exception:  # FieldDoesNotExist: the module changed shape; skip rather than break the admin page
            continue
        qs = model._default_manager.filter(**{field: name})
        if any(f.name == "tenant_key" for f in model._meta.get_fields()):
            qs = qs.filter(tenant_key=t)
        yield label, qs, field


def _templates_for_site(name):
    """automation.Template rows whose default values name the site (stored inside a JSON field)."""
    try:
        Template = apps.get_model("automation", "Template")
    except LookupError:
        return []
    return [tpl for tpl in Template.objects.filter(tenant_key=tenant())
            if isinstance(tpl.fields, dict) and tpl.fields.get("site") == name]


def site_references(name):
    """[(label, count)] of records in every module that store the site name `name` (only non-zero counts)."""
    out = []
    for label, qs, _field in _site_name_querysets(name):
        n = qs.count()
        if n:
            out.append((label, n))
    n = len(_templates_for_site(name))
    if n:
        out.append(("Automation templates", n))
    return out


def rename_site_references(old, new):
    """Point every record that stores the site name `old` at `new`, in one transaction. Returns [(label, count)]
    of updated rows (non-zero only). Run it in the same transaction that renames the core.Site row."""
    out = []
    with transaction.atomic():
        for label, qs, field in _site_name_querysets(old):
            n = qs.update(**{field: new})
            if n:
                out.append((label, n))
        tpls = _templates_for_site(old)
        for tpl in tpls:
            tpl.fields = {**tpl.fields, "site": new}
            tpl.save(update_fields=["fields"])
        if tpls:
            out.append(("Automation templates", len(tpls)))
    return out


def unmapped_site_names():
    """Site names used by URL checks / domains that have no core.Site record yet."""
    from core.models import Site
    known = set(Site.objects.filter(tenant_key=tenant()).values_list("name", flat=True))
    return sorted(n for n in site_usage() if n and n != "All Sites" and n not in known)


def bootstrap_sites():
    """Run the core `bootstrap_sites` command. Returns (created names, command output)."""
    from django.core.management import call_command

    from core.models import Site
    before = set(Site.objects.filter(tenant_key=tenant()).values_list("name", flat=True))
    out = io.StringIO()
    call_command("bootstrap_sites", stdout=out)
    after = set(Site.objects.filter(tenant_key=tenant()).values_list("name", flat=True))
    return sorted(after - before), out.getvalue().strip()


def _client_qs():
    from core.models import Client
    return Client.objects.filter(tenant_key=tenant())


def bootstrap_clients():
    """Create client records for every distinct service-desk client name not yet registered."""
    from django.utils.text import slugify

    from core.models import Client
    from servicedesk.models import Ticket

    t = tenant()
    known = {n.lower() for n in _client_qs().values_list("name", flat=True)}
    names = sorted({n.strip() for n in Ticket.objects.filter(tenant_key=t).exclude(client="").values_list("client", flat=True)
                    if n and n.strip()})
    created = []
    for name in names:
        if name.lower() in known:
            continue
        base = slugify(name)[:50] or "client"
        code = base
        n = 2
        while _client_qs().filter(code=code).exists():
            suffix = f"-{n}"
            code = f"{base[:50 - len(suffix)]}{suffix}"
            n += 1
        Client.objects.create(tenant_key=t, name=name, code=code)
        known.add(name.lower())
        created.append(name)
    return created


def client_wise_rows(status=None):
    """Roll-up counts per client from live module data (sites, tickets, checks, devices, domains)."""
    from django.db.models import Q

    from core.models import Client, Site
    from domainmonitor.models import Domain
    from network.models import Device
    from servicedesk import workflow as wf
    from servicedesk.models import Ticket
    from urlchecker.models import UrlCheck

    t = tenant()
    clients = list(_client_qs().prefetch_related("sites").order_by("name"))
    if status in dict(Client.STATUSES):
        clients = [c for c in clients if c.status == status]

    site_by_client = {}
    for site in Site.objects.filter(tenant_key=t).select_related("client"):
        if site.client_id:
            site_by_client.setdefault(site.client_id, []).append(site.name)

    open_incidents = Ticket.objects.filter(tenant_key=t, kind="incident").exclude(status__in=wf.DONE)
    url_qs = UrlCheck.objects.filter(tenant_key=t, enabled=True)
    dev_qs = Device.objects.filter(tenant_key=t)
    dom_qs = Domain.objects.filter(tenant_key=t, is_active=True)

    rows = []
    for client in clients:
        sites = site_by_client.get(client.pk, [])
        site_q = Q(site__in=sites) if sites else Q(pk__in=[])
        inc = open_incidents.filter(Q(client=client.name) | site_q)
        rows.append({
            "client": client,
            "sites": len(sites),
            "open_incidents": inc.count(),
            "open_major": inc.filter(priority__in=("P1", "P2")).count(),
            "urls": url_qs.filter(site_q).count() if sites else 0,
            "urls_down": url_qs.filter(site_q, last_status__in=("down", "degraded")).count() if sites else 0,
            "devices": dev_qs.filter(site_q).count() if sites else 0,
            "devices_down": dev_qs.filter(site_q, status="down").count() if sites else 0,
            "devices_by_type": dict(dev_qs.filter(site_q).values_list("device_type").annotate(n=Count("id")).order_by())
                               if sites else {},
            "domains": dom_qs.filter(site_key__in=sites).count() if sites else 0,
            "domains_issue": dom_qs.filter(site_key__in=sites).filter(
                Q(status="down") | Q(ssl_status__in=("Expiring", "Expired", "Invalid")) | Q(dns_status="Failed")
            ).count() if sites else 0,
        })

    unassigned_sites = list(Site.objects.filter(tenant_key=t, client__isnull=True).values_list("name", flat=True))
    if unassigned_sites and status is None:
        site_q = Q(site__in=unassigned_sites)
        inc = open_incidents.filter(site_q)
        rows.append({
            "client": None,
            "sites": len(unassigned_sites),
            "open_incidents": inc.count(),
            "open_major": inc.filter(priority__in=("P1", "P2")).count(),
            "urls": url_qs.filter(site_q).count(),
            "urls_down": url_qs.filter(site_q, last_status__in=("down", "degraded")).count(),
            "devices": dev_qs.filter(site_q).count(),
            "devices_down": dev_qs.filter(site_q, status="down").count(),
            "devices_by_type": dict(dev_qs.filter(site_q).values_list("device_type").annotate(n=Count("id")).order_by()),
            "domains": dom_qs.filter(site_key__in=unassigned_sites).count(),
            "domains_issue": dom_qs.filter(site_key__in=unassigned_sites).filter(
                Q(status="down") | Q(ssl_status__in=("Expiring", "Expired", "Invalid")) | Q(dns_status="Failed")
            ).count(),
        })
    return rows
