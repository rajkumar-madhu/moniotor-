"""Go-live readiness checks: is this running configuration fit to serve real users?

Used by `manage.py go_live_check` (deploy/live/bootstrap.sh runs it after the first start) and by the "Go-live
readiness" card on Administration > System health. Every check is read-only: it inspects settings, the database
and, for Prometheus, one tiny query. It never changes anything and never returns a secret value.

Hook for other apps
-------------------
Checks live in a registry. An app adds its own check without touching this file, either

* in a module called ``golive_checks.py`` inside the app (imported automatically the first time checks run), or
* by calling :func:`register` from anywhere that is imported at start-up (for example ``AppConfig.ready``):

    from administration.golive import register, ok, warn

    @register("example_data", "Example data")
    def example_data(ctx):
        n = count_example_rows()
        if n:
            return warn("example_data", "Example data", f"{n} example records", "manage.py remove_examples --yes")
        return ok("example_data", "Example data", "none")

A check takes one argument (:class:`Context`) and returns one :class:`Result` or a list of them. A check that raises
is reported as FAIL ("check crashed"), it never takes the report down.
"""
import ipaddress
import logging
import os
import re
import socket
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from urllib.parse import urlsplit

from django.conf import settings
from django.db import DatabaseError
from django.utils.module_loading import autodiscover_modules

log = logging.getLogger("linkedeye.golive")

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
RANK = {PASS: 0, WARN: 1, FAIL: 2}

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]", "web", "testserver"}
GENERATE_SECRET_KEY = "python3 -c 'import secrets; print(secrets.token_urlsafe(50))'"
GENERATE_FERNET = 'python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
WEAK_DB_PASSWORDS = {"", "password", "postgres", "linkedeye", "changeme", "secret", "admin", "root", "123456", "12345678", "mysql"}
HSTS_MIN = 86400            # one day: enough to call HSTS "on"; docs/GO_LIVE.md says to ramp up to a year


@dataclass
class Result:
    key: str
    label: str
    status: str
    detail: str
    fix: str = ""

    def as_dict(self):
        return asdict(self)


def ok(key, label, detail, fix=""):
    return Result(key, label, PASS, detail, fix)


def warn(key, label, detail, fix=""):
    return Result(key, label, WARN, detail, fix)


def fail(key, label, detail, fix=""):
    return Result(key, label, FAIL, detail, fix)


@dataclass
class Context:
    """What a check may look at besides Django settings. `env` is injectable for tests."""
    env: dict = field(default_factory=lambda: dict(os.environ))
    fast: bool = False          # the System health page: keep network waits short

    def get(self, name, default=""):
        return (self.env.get(name) or default).strip()


# ------------------------------------------------------------------ registry
_REGISTRY = {}       # key -> (label, function), in registration order
_discovered = False


def register(key, label=None):
    """Decorator: add `fn(ctx)` to the report under `key`. Registering a key again replaces the earlier check."""
    def deco(fn):
        _REGISTRY[key] = (label or key, fn)
        return fn
    return deco


def unregister(key):
    _REGISTRY.pop(key, None)


def _discover():
    global _discovered
    if not _discovered:
        _discovered = True
        try:
            autodiscover_modules("golive_checks")
        except Exception:       # a broken plug-in module must not hide the built-in checks
            log.exception("could not import a golive_checks module")


def check_keys():
    _discover()
    return list(_REGISTRY)


def run_checks(only=None, skip=(), ctx=None, fast=False):
    """Run every registered check; returns a list of Result, built-ins first, then plug-ins in registration order."""
    _discover()
    ctx = ctx or Context(fast=fast)
    only = set(only or ())
    skip = set(skip or ())
    results = []
    for key, (label, fn) in list(_REGISTRY.items()):
        if (only and key not in only) or key in skip:
            continue
        try:
            out = fn(ctx)
        except DatabaseError as exc:
            results.append(fail(key, label, f"needs the database, which answered with {exc.__class__.__name__} (migrations not applied?)",
                                "python manage.py migrate --noinput"))
            continue
        except Exception as exc:
            log.warning("go-live check %s crashed", key, exc_info=True)
            results.append(fail(key, label, f"check crashed: {exc.__class__.__name__}"))
            continue
        results.extend([out] if isinstance(out, Result) else list(out or []))
    return results


def overall(results):
    return max((r.status for r in results), key=lambda s: RANK[s], default=PASS)


def counts(results):
    return {s: sum(1 for r in results if r.status == s) for s in (PASS, WARN, FAIL)}


def report(only=None, skip=(), ctx=None, fast=False):
    """Everything a template needs: results, per-status counts, the overall verdict."""
    results = run_checks(only=only, skip=skip, ctx=ctx, fast=fast)
    return {"results": results, "counts": counts(results), "overall": overall(results)}


# ------------------------------------------------------------------ built-in checks
@register("debug", "DEBUG off")
def check_debug(ctx):
    if settings.DEBUG:
        return fail("debug", "DEBUG off", "LINKEDEYE_DEBUG is on: tracebacks are shown to visitors and cookies are not Secure",
                    "set LINKEDEYE_DEBUG=false")
    return ok("debug", "DEBUG off", "off")


@register("secret_key", "Secret key")
def check_secret_key(ctx):
    key = settings.SECRET_KEY or ""
    if len(key) < 50 or len(set(key)) < 20 or key.lower().startswith(("change-me", "dev-only", "<replace")):
        return fail("secret_key", "Secret key", "short, repetitive or a published placeholder: sessions and CSRF tokens can be forged",
                    f"set LINKEDEYE_SECRET_KEY to a random value:  {GENERATE_SECRET_KEY}")
    return ok("secret_key", "Secret key", f"{len(key)} characters")


@register("allowed_hosts", "Allowed hosts")
def check_allowed_hosts(ctx):
    hosts = list(settings.ALLOWED_HOSTS)
    domain = ctx.get("LINKEDEYE_DOMAIN").lower()
    if not hosts:
        return fail("allowed_hosts", "Allowed hosts", "empty: every request is answered 400", "set LINKEDEYE_ALLOWED_HOSTS to the name users type")
    if "*" in hosts:
        return fail("allowed_hosts", "Allowed hosts", "'*' accepts any Host header (cache poisoning, password-reset links)",
                    "list the real names in LINKEDEYE_ALLOWED_HOSTS")
    lowered = {h.lower().lstrip(".") for h in hosts}
    if domain and domain not in lowered and not any(h.startswith(".") and domain.endswith(h.lower()) for h in hosts):
        return fail("allowed_hosts", "Allowed hosts", f"LINKEDEYE_DOMAIN={domain} is not listed: users would get 400",
                    f"add {domain} to LINKEDEYE_ALLOWED_HOSTS")
    public = [h for h in hosts if h.lower() not in LOCAL_HOSTS]
    if domain or public:
        return ok("allowed_hosts", "Allowed hosts", ", ".join(hosts))
    return warn("allowed_hosts", "Allowed hosts", f"only local names ({', '.join(hosts)}): reachable from this machine only",
                "add the public name or IP to LINKEDEYE_ALLOWED_HOSTS")


@register("csrf_origins", "CSRF trusted origins")
def check_csrf(ctx):
    origins = list(settings.CSRF_TRUSTED_ORIGINS)
    domain = ctx.get("LINKEDEYE_DOMAIN").lower()
    bad = [o for o in origins if not re.match(r"^https?://[^/\s]+$", o)]
    if bad:
        return fail("csrf_origins", "CSRF trusted origins", f"not of the form https://host[:port]: {', '.join(bad)}",
                    "fix LINKEDEYE_CSRF_TRUSTED_ORIGINS")
    if not origins:
        if settings.DEBUG:
            return ok("csrf_origins", "CSRF trusted origins", "empty (DEBUG)")
        return warn("csrf_origins", "CSRF trusted origins",
                    "empty: sign-in works only if the proxy forwards Host and X-Forwarded-Proto unchanged (403 CSRF otherwise)",
                    "set LINKEDEYE_CSRF_TRUSTED_ORIGINS=https://your.host")
    if domain and not any(urlsplit(o).hostname == domain.split(":")[0] for o in origins):
        return warn("csrf_origins", "CSRF trusted origins", f"none of them names {domain}: form posts from the browser may be refused",
                    f"add the origin of {domain} (with its scheme)")
    return ok("csrf_origins", "CSRF trusted origins", ", ".join(origins))


@register("https_cookies", "HTTPS-only cookies")
def check_cookies(ctx):
    if settings.SESSION_COOKIE_SECURE and settings.CSRF_COOKIE_SECURE:
        return ok("https_cookies", "HTTPS-only cookies", "session and CSRF cookies are Secure")
    plain = ctx.get("LINKEDEYE_MODE") == "http"
    return warn("https_cookies", "HTTPS-only cookies",
                "sign-in cookies travel unencrypted" + (" (plain-HTTP mode: use a trusted network only)" if plain else ""),
                "serve the console over HTTPS and set LINKEDEYE_SECURE_COOKIES=true")


@register("hsts", "HSTS")
def check_hsts(ctx):
    seconds = getattr(settings, "SECURE_HSTS_SECONDS", 0) or 0
    if seconds >= HSTS_MIN:
        return ok("hsts", "HSTS", f"Strict-Transport-Security max-age={seconds}")
    if ctx.get("LINKEDEYE_MODE") == "http":
        return warn("hsts", "HSTS", "not applicable on plain HTTP", "move to HTTPS, then set LINKEDEYE_HSTS_SECONDS")
    return warn("hsts", "HSTS", "the console does not send Strict-Transport-Security (fine if your proxy does)",
                "set LINKEDEYE_HSTS_SECONDS=86400, then 31536000 once HTTPS is verified (or add the header at the proxy)")


@register("trusted_proxies", "Trusted proxies")
def check_proxies(ctx):
    entries = list(settings.TRUSTED_PROXIES)
    bad = []
    for item in entries:
        try:
            ipaddress.ip_network(item, strict=False)
        except ValueError:
            bad.append(item)
    if bad:
        return fail("trusted_proxies", "Trusted proxies", f"not an IP address or CIDR (ignored by the console): {', '.join(bad)}",
                    "fix LINKEDEYE_TRUSTED_PROXIES")
    # a network as large as "everyone" makes X-Forwarded-For a client-chosen value: the sign-in throttle and the audit log's
    # source address can be set by whoever sends the request
    wide = [item for item in entries
            if ipaddress.ip_network(item, strict=False).prefixlen < (8 if ipaddress.ip_network(item, strict=False).version == 4 else 7)]
    if wide:
        return fail("trusted_proxies", "Trusted proxies",
                    f"{', '.join(wide)} covers far more than a proxy: any client could forge its address in X-Forwarded-For "
                    "(audit log, sign-in throttle)",
                    "list only the proxy's address or network in LINKEDEYE_TRUSTED_PROXIES (and GUNICORN_FORWARDED_ALLOW_IPS)")
    forwarded = ctx.get("GUNICORN_FORWARDED_ALLOW_IPS")
    if not entries:
        if settings.DEBUG:
            return ok("trusted_proxies", "Trusted proxies", "empty (DEBUG, no proxy)")
        return warn("trusted_proxies", "Trusted proxies",
                    "empty: behind a proxy every client shares the proxy's address (audit IPs, one sign-in throttle for all)",
                    "set LINKEDEYE_TRUSTED_PROXIES and GUNICORN_FORWARDED_ALLOW_IPS to the proxy's address")
    if not forwarded:
        return warn("trusted_proxies", "Trusted proxies",
                    "GUNICORN_FORWARDED_ALLOW_IPS is not set: gunicorn ignores X-Forwarded-Proto, so HTTPS requests look like HTTP",
                    "set GUNICORN_FORWARDED_ALLOW_IPS to the same proxy address")
    return ok("trusted_proxies", "Trusted proxies", f"X-Forwarded-For trusted from {', '.join(entries)}")


@register("database", "Database")
def check_database(ctx):
    from django.db import connection
    vendor = connection.vendor
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
    except Exception as exc:
        return fail("database", "Database", f"{vendor}: unreachable ({exc.__class__.__name__})", "check LINKEDEYE_DB_* and that the database is up")
    if vendor == "sqlite":
        return warn("database", "Database", "SQLite: fine for a trial, not for several workers and a scheduler writing at once",
                    "use PostgreSQL (LINKEDEYE_DB_ENGINE=django.db.backends.postgresql)")
    from administration.services import _db_version
    password = connection.settings_dict.get("PASSWORD") or ""
    if password.lower() in WEAK_DB_PASSWORDS or len(password) < 12 or password.lower().startswith(("change-me", "<replace")):
        return warn("database", "Database", f"{_db_version()}, but the database password is empty, short or a well-known default",
                    "set a random LINKEDEYE_DB_PASSWORD (at least 16 characters) and change it in the database as well")
    return ok("database", "Database", _db_version())


@register("migrations", "Migrations")
def check_migrations(ctx):
    from administration.services import pending_migrations
    try:
        pending = pending_migrations()
    except Exception as exc:
        return fail("migrations", "Migrations", f"cannot read the migration state ({exc.__class__.__name__})", "python manage.py migrate")
    if pending:
        apps_ = sorted({a for a, _ in pending})
        return fail("migrations", "Migrations", f"{len(pending)} pending ({', '.join(apps_)})", "python manage.py migrate --noinput")
    return ok("migrations", "Migrations", "all applied")


@register("credential_key", "Credential key")
def check_credential_key(ctx):
    from core import secrets
    if not secrets.keys():
        return warn("credential_key", "Credential key", "not set: Onboarding cannot store SSH/SNMP/HTTP secrets or a Prometheus login",
                    f"set LINKEDEYE_CREDENTIAL_KEY:  {GENERATE_FERNET}")
    reason = secrets.unavailable_reason()
    if reason:
        return fail("credential_key", "Credential key", "set but not usable (not a valid Fernet key, or 'cryptography' missing)",
                    f"replace it:  {GENERATE_FERNET}")
    return ok("credential_key", "Credential key", f"{len(secrets.keys())} key(s) loaded")


def _otp_on():
    return bool(getattr(settings, "LINKEDEYE_LOGIN_OTP", False))


@register("smtp", "Sign-in code mail (SMTP)")
def check_smtp(ctx):
    from administration import integration_config as ic
    backend = getattr(settings, "EMAIL_BACKEND", "")
    host = (ic.email_host() or "").strip()
    local = host.lower() in ("", "localhost", "127.0.0.1", "::1")
    sender = (ic.default_from_email() or "").strip()
    needed = _otp_on()
    if ic.has_override("email") is False and any(x in backend for x in ("console", "locmem", "dummy", "filebased")):
        kind = backend.rsplit(".", 2)[-2]
        if needed:
            return fail("smtp", "Sign-in code mail (SMTP)", f"sign-in codes are on but the e-mail backend is '{kind}': nobody receives a code",
                        "set LINKEDEYE_EMAIL_HOST (and port, user, password) or LINKEDEYE_LOGIN_OTP=false")
        return warn("smtp", "Sign-in code mail (SMTP)", f"e-mail backend is '{kind}': no mail leaves the server", "set LINKEDEYE_EMAIL_HOST")
    if local:
        if needed:
            return fail("smtp", "Sign-in code mail (SMTP)", "sign-in codes are on but the SMTP host is empty/localhost: nobody can sign in",
                        "set LINKEDEYE_EMAIL_HOST, or LINKEDEYE_LOGIN_OTP=false for a trusted network")
        return warn("smtp", "Sign-in code mail (SMTP)", "SMTP host is empty/localhost: notifications by e-mail will not be delivered",
                    "set LINKEDEYE_EMAIL_HOST")
    if sender.lower().endswith(("@localhost", "@example.internal", "@example.com")):
        return warn("smtp", "Sign-in code mail (SMTP)", f"SMTP host {host}, but the sender is {sender}: most providers reject it",
                    "set LINKEDEYE_DEFAULT_FROM_EMAIL to an address your SMTP server may send as")
    return ok("smtp", "Sign-in code mail (SMTP)", f"{host}:{ic.email_port()}, from {sender}")


@register("otp_mode", "Sign-in mode")
def check_otp(ctx):
    from django.contrib.auth import get_user_model
    User = get_user_model()
    if not _otp_on():
        sev = warn if not settings.DEBUG else ok
        return sev("otp_mode", "Sign-in mode", "password only (no e-mail code)",
                   "set LINKEDEYE_LOGIN_OTP=true once SMTP works; password-only is acceptable on a trusted network only")
    nomail = User.objects.filter(is_active=True, is_superuser=True, email="").count()
    if nomail:
        return warn("otp_mode", "Sign-in mode", f"e-mail code after password; {nomail} active administrator(s) have no e-mail address and cannot sign in",
                    "set an e-mail address on those users (Administration > Users)")
    return ok("otp_mode", "Sign-in mode", "password, then a one-time code by e-mail")


@register("admin_user", "Administrator and sites")
def check_admin(ctx):
    from django.contrib.auth import get_user_model
    from core.models import Membership, Site
    User = get_user_model()
    admins = User.objects.filter(is_active=True, is_superuser=True).count() + \
        Membership.objects.filter(role="admin", user__is_active=True, user__is_superuser=False).values("user").distinct().count()
    if not admins:
        return fail("admin_user", "Administrator and sites", "no active administrator: nobody can manage the console",
                    "python manage.py createsuperuser")
    sites = Site.objects.filter(tenant_key=settings.LINKEDEYE_TENANT).count()
    if not sites:
        return warn("admin_user", "Administrator and sites", f"{admins} administrator(s), but no site is defined yet",
                    "Administration > Sites, or python manage.py bootstrap_sites")
    return ok("admin_user", "Administrator and sites", f"{admins} administrator(s), {sites} site(s)")


def _tcp_ok(url, timeout):
    parts = urlsplit(url)
    host = parts.hostname
    if not host:
        return False, "no host in the URL"
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, ""
    except OSError as exc:
        return False, str(exc.strerror or exc.__class__.__name__)


@register("prometheus", "Prometheus")
def check_prometheus(ctx):
    from administration import integration_config as ic
    from core import prometheus
    url = ic.prometheus_url()
    if not url:
        return warn("prometheus", "Prometheus", "not connected: metrics, heat map and host pages show 'not connected'",
                    "set LINKEDEYE_PROMETHEUS_URL (the bundled one is http://prometheus:9090) or Administration > Integrations")
    shown = f"{urlsplit(url).scheme}://{urlsplit(url).netloc.rsplit('@', 1)[-1]}"      # never echo credentials in the URL
    reachable, why = _tcp_ok(url, 1.5 if ctx.fast else 4)
    if not reachable:
        return warn("prometheus", "Prometheus", f"{shown} is not reachable from here ({why})", "check the URL and that Prometheus is running")
    try:
        prometheus.query("vector(1)")
    except prometheus.PrometheusError as exc:
        return warn("prometheus", "Prometheus", f"{shown} answered, but the query failed: {exc}", "check the credentials on Administration > Integrations")
    return ok("prometheus", "Prometheus", f"{shown} answers queries")


@register("worker", "Worker (run_probes)")
def check_worker(ctx):
    from administration.models import Heartbeat
    from administration.services import HEARTBEAT_STALE_SECONDS, _fmt_age
    beat = Heartbeat.objects.filter(key="scheduler").first()
    standby = Heartbeat.objects.filter(key__startswith="scheduler-standby:").order_by("-seen_at").first()
    if beat is None:
        extra = " (a standby scheduler is running, but none is active)" if standby else ""
        return warn("worker", "Worker (run_probes)", "no heartbeat yet: the scheduler has not reported" + extra,
                    "start the worker (docker compose up -d worker / python manage.py run_probes) and wait a minute")
    age = beat.age_seconds
    where = f"{beat.host or 'unknown host'}" + (f", pid {beat.pid}" if beat.pid else "")
    if age > HEARTBEAT_STALE_SECONDS:
        return fail("worker", "Worker (run_probes)", f"last heartbeat {_fmt_age(age)} ({where}): the scheduler stopped",
                    "check the worker's logs: docker compose logs worker")
    lease = (beat.data or {}).get("lease", "")
    lease_text = {"held": ", holds the scheduler lease", "disabled": ", lease disabled"}.get(lease, "")
    return ok("worker", "Worker (run_probes)", f"heartbeat {_fmt_age(age)} ({where}{lease_text})")


@register("static_files", "Static files")
def check_static(ctx):
    root = settings.STATIC_ROOT
    if settings.DEBUG:
        return ok("static_files", "Static files", "served by the development server (DEBUG)")
    if not root or not os.path.isdir(root):
        return fail("static_files", "Static files", f"{root} does not exist: pages would be unstyled", "python manage.py collectstatic --noinput (the image does this at build)")
    if not os.path.isfile(os.path.join(root, "linkedeye", "app.css")):
        return fail("static_files", "Static files", "linkedeye/app.css is missing from STATIC_ROOT", "python manage.py collectstatic --noinput")
    manifest = os.path.isfile(os.path.join(root, "staticfiles.json"))
    return ok("static_files", "Static files", "collected" + ("" if manifest else " (no hashed manifest)"))


@register("probe_allowlist", "Private probe ranges")
def check_probe_ranges(ctx):
    ranges = list(settings.PROBE_ALLOWED_PRIVATE_CIDRS)
    bad = []
    for item in ranges:
        try:
            ipaddress.ip_network(item, strict=False)
        except ValueError:
            bad.append(item)
    if bad:
        return fail("probe_allowlist", "Private probe ranges", f"not a CIDR (ignored): {', '.join(bad)}", "fix LINKEDEYE_PROBE_ALLOWED_PRIVATE_CIDRS")
    if not ranges:
        return warn("probe_allowlist", "Private probe ranges",
                    "empty: URL, TCP and domain checks refuse private and loopback addresses (public targets are fine)",
                    "set LINKEDEYE_PROBE_ALLOWED_PRIVATE_CIDRS to the ranges you monitor, e.g. 10.0.0.0/8")
    return ok("probe_allowlist", "Private probe ranges", ", ".join(ranges))


@register("alertmanager_token", "Alertmanager webhook")
def check_alertmanager(ctx):
    from administration import integration_config as ic
    if not ic.prometheus_url():
        return ok("alertmanager_token", "Alertmanager webhook", "not used (no Prometheus connected)")
    if not ic.alertmanager_webhook_token():
        return warn("alertmanager_token", "Alertmanager webhook", "no token: /api/v1/alertmanager/ answers 503 and Prometheus alerts never reach the console",
                    "set LINKEDEYE_ALERTMANAGER_WEBHOOK_TOKEN (same value in Alertmanager)")
    return ok("alertmanager_token", "Alertmanager webhook", "token set")


@register("django_deploy", "Django deployment checks")
def check_django_deploy(ctx):
    from django.core import checks
    covered = {"security.W004", "security.W005", "security.W008", "security.W021",   # HSTS (own check), SSL redirect (the proxy does it)
               "security.W012", "security.W016"}                                 # secure cookies (own check)
    found = [m for m in checks.run_checks(include_deployment_checks=True) if m.id not in covered and not m.is_silenced()]
    errors = [m for m in found if m.level >= checks.ERROR]
    warnings = [m for m in found if checks.WARNING <= m.level < checks.ERROR]
    if errors:
        return fail("django_deploy", "Django deployment checks", "; ".join(f"{m.id}" for m in errors), "python manage.py check --deploy")
    if warnings:
        return warn("django_deploy", "Django deployment checks", "; ".join(m.id for m in warnings), "python manage.py check --deploy")
    return ok("django_deploy", "Django deployment checks", "no findings")


# ------------------------------------------------------------------ the running site over HTTP (--url)
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def check_url(base, timeout=8, verify=True):
    """Probe a *running* console at `base` (for example https://linkedeye.example.com): /healthz, the sign-in page, a
    static file, TLS and HSTS. Operator-supplied URL; nothing is posted. Returns a list of Result."""
    import json
    import ssl
    base = base.rstrip("/")
    context = None
    if base.startswith("https://") and not verify:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=context))
    out = []

    def get(path):
        req = urllib.request.Request(base + path, headers={"User-Agent": "linkedeye-go-live-check"})
        try:
            with opener.open(req, timeout=timeout) as resp:
                return resp.status, resp.headers, resp.read(65536)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, b""
        except (urllib.error.URLError, OSError) as exc:
            return None, None, str(getattr(exc, "reason", exc)).encode()

    status, headers, body = get("/healthz")
    if status is None:
        reason = body.decode("utf-8", "replace")
        hint = " (certificate not trusted: for a self-signed certificate pass --insecure)" if "CERTIFICATE" in reason.upper() else ""
        return [fail("url_healthz", "GET /healthz", f"{base} is unreachable: {reason}{hint}", "check DNS, the firewall and that the proxy is up")]
    try:
        healthy = status == 200 and json.loads(body.decode()).get("status") == "ok"
    except ValueError:
        healthy = False
    if healthy:
        out.append(ok("url_healthz", "GET /healthz", "200, database reachable"))
    else:
        hint = " (400: the host name is not in LINKEDEYE_ALLOWED_HOSTS)" if status == 400 else ""
        out.append(fail("url_healthz", "GET /healthz", f"HTTP {status}{hint}", "docker compose logs web caddy"))
    status, headers, _ = get("/accounts/login/")
    out.append(ok("url_login", "Sign-in page", "200") if status == 200
               else fail("url_login", "Sign-in page", f"HTTP {status}", "docker compose logs web"))
    status, _, _ = get("/static/linkedeye/app.css")
    out.append(ok("url_static", "Static files", "200") if status == 200
               else fail("url_static", "Static files", f"HTTP {status}: pages would be unstyled", "the image must be built from this repository's Dockerfile"))
    if base.startswith("https://"):
        hsts = (headers or {}).get("Strict-Transport-Security", "") if headers else ""
        out.append(ok("url_hsts", "HSTS header", hsts) if hsts
                   else warn("url_hsts", "HSTS header", "not sent", "set LINKEDEYE_HSTS_SECONDS"))
    elif (urlsplit(base).hostname or "") not in ("localhost", "127.0.0.1", "::1"):
        out.append(warn("url_tls", "TLS", "plain HTTP: passwords and session cookies travel unencrypted", "use https:// on any untrusted network"))
    return out
