"""The Prometheus path of onboarding: connect an existing Prometheus and watch the product fill up.

The operator gives a URL and, optionally, an HTTP basic login. LinkedEye then, through
``probes.checks`` only (so the SSRF policy applies):

* asks ``/api/v1/status/buildinfo`` (which Prometheus, which version);
* asks ``/api/v1/targets`` (how many targets, how many healthy, how many distinct hosts, which jobs) and
  reports how much of that the product already knows (CMDB items, devices, discovered targets);
* only on the explicit *Verify and connect* button, saves the URL through
  ``administration.integration_config`` (the same store as Administration -> Integrations). The login's
  password goes to the credential vault; the settings keep only the id of the vault row, bound to the URL
  it was saved for. ``core.prometheus`` reads it from the vault when it queries.

Connecting replaces the Prometheus every site reads, so it needs the admin role on every site, and an
already-connected different server is only replaced when the operator ticks the box.
"""
import base64
import time
from collections import Counter
from urllib.parse import urlsplit

from django import forms
from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect
from django.utils import timezone
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_POST

from core import secrets
from core.access import require, scope_by_site
from core.audit import audit

from . import probing, services
from .models import Credential, CredentialKind, TargetState, TargetType

MAX_URL = 300
MAX_TARGETS_BYTES = 8 * 1024 * 1024
TOP_JOBS = 10


# ----------------------------------------------------------------------------- the address
def normalize_url(raw):
    """(url, error): an http(s) address without a login, query or fragment and without a trailing slash.
    A bare host is taken as https, like the Integrations page."""
    text = (raw or "").strip()
    if not text:
        return "", "Enter the address of your Prometheus, e.g. https://prometheus.example.internal:9090."
    if len(text) > MAX_URL:
        return "", "That address is too long."
    if "://" not in text:
        text = "https://" + text
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        return "", "That is not a valid address."
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "", "Use an http:// or https:// address."
    if parts.username or parts.password:
        return "", "Do not put the login in the address: use the user name and password fields."
    if parts.query or parts.fragment:
        return "", "Enter the address without a query string."
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    return f"{parts.scheme}://{host}{':' + str(port) if port else ''}{parts.path.rstrip('/')}", ""


def redact(url):
    """scheme://host[:port]/path for display: no login, query or fragment."""
    try:
        p = urlsplit(str(url or ""))
        host = p.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        if p.port:
            host += f":{p.port}"
        return f"{p.scheme}://{host}{p.path}" if p.scheme and host else ""
    except ValueError:
        return ""


def _host_of(instance):
    value = str(instance or "").strip()
    if value.startswith("["):
        return value[1:].split("]", 1)[0]
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


def basic_header(username, password):
    """The Authorization header value for a login. Never logged or returned to a page."""
    return "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()


# ----------------------------------------------------------------------------- the probe
def _json(resp):
    import json
    try:
        return json.loads(resp.body.decode("utf-8", "replace"))
    except ValueError:
        return None


def _http_problem(resp, authenticated):
    """Operator wording for a failed Prometheus request. Fixed text; never the response body."""
    if not resp.ok:
        return resp.error
    if resp.status in (401, 403):
        return ("Prometheus refused the login (HTTP %d). " % resp.status +
                ("Check the user name and password." if authenticated else
                 "It needs a login: enter the HTTP basic user name and password."))
    if resp.status == 404:
        return "HTTP 404: that address does not look like a Prometheus API."
    return f"Prometheus answered HTTP {resp.status}."


def fetch_overview(url, username="", password=""):
    """Probe the server and describe what it already sees. Returns a dict; never raises and never
    contains the password.

    ``ok`` means the product can use it: the target list answered (buildinfo is reported, and a 404 there is
    tolerated for old servers). Per-stage detail is in ``stages``."""
    started = time.monotonic()
    headers = {"Accept": "application/json"}
    if username or password:
        headers["Authorization"] = basic_header(username, password)
    authenticated = bool(username or password)
    base = url.rstrip("/")
    out = {"url": redact(url), "ok": False, "authenticated": authenticated, "stages": [], "errors": [],
           "warnings": [], "version": "", "revision": "", "go_version": "", "targets": 0, "up": 0, "down": 0,
           "unknown": 0, "hosts": 0, "jobs": [], "instances": [], "checked_at": timezone.now().isoformat()}

    def stage(name, ok, detail, ms):
        out["stages"].append({"name": name, "ok": ok, "detail": detail, "ms": ms})

    # 1. buildinfo
    resp = probing.get(base + "/api/v1/status/buildinfo", headers=headers, max_bytes=64 * 1024)
    build_ok = False
    if resp.success:
        body = _json(resp) or {}
        data = body.get("data") if isinstance(body, dict) else None
        if isinstance(body, dict) and body.get("status") == "success" and isinstance(data, dict):
            build_ok = True
            out["version"] = str(data.get("version") or "")[:40]
            out["revision"] = str(data.get("revision") or "")[:12]
            out["go_version"] = str(data.get("goVersion") or "")[:20]
            stage("buildinfo", True, f"Prometheus {out['version'] or '(version not reported)'}", resp.duration_ms)
        else:
            stage("buildinfo", False, "The answer is not a Prometheus build info document.", resp.duration_ms)
            out["errors"].append("/api/v1/status/buildinfo did not return a Prometheus document.")
    else:
        problem = _http_problem(resp, authenticated)
        stage("buildinfo", False, problem, resp.duration_ms)
        if resp.ok and resp.status == 404:
            out["warnings"].append("This server has no build info endpoint (an old Prometheus): the version is unknown.")
        else:
            out["errors"].append(problem)
    if out["errors"]:                     # unreachable or refused: do not hammer the second endpoint
        out["duration_ms"] = round((time.monotonic() - started) * 1000, 1)
        return out

    # 2. targets
    resp = probing.get(base + "/api/v1/targets?state=active", headers=headers, max_bytes=MAX_TARGETS_BYTES)
    if not resp.success:
        problem = _http_problem(resp, authenticated)
        stage("targets", False, problem, resp.duration_ms)
        out["errors"].append(problem)
    elif resp.truncated:
        msg = "The target list is larger than 8 MB; connect a Prometheus with fewer active targets or a filter."
        stage("targets", False, msg, resp.duration_ms)
        out["errors"].append(msg)
    else:
        body = _json(resp)
        active = ((body or {}).get("data") or {}).get("activeTargets") if isinstance(body, dict) else None
        if not isinstance(active, list):
            msg = "/api/v1/targets did not return a target list."
            stage("targets", False, msg, resp.duration_ms)
            out["errors"].append(msg)
        else:
            jobs, hosts, instances = Counter(), set(), []
            for t in active:
                if not isinstance(t, dict):
                    continue
                labels = t.get("labels") if isinstance(t.get("labels"), dict) else {}
                health = t.get("health")
                out["up" if health == "up" else "down" if health == "down" else "unknown"] += 1
                instance = str(labels.get("instance") or "")
                if instance:
                    hosts.add(_host_of(instance))
                    instances.append(instance[:255])
                jobs[str(labels.get("job") or "(no job)")[:100]] += 1
            out["targets"] = len(active)
            out["hosts"] = len(hosts)
            out["jobs"] = jobs.most_common(TOP_JOBS)
            out["instances"] = sorted(set(instances))[:5000]
            stage("targets", True, f"{out['targets']} target(s) on {out['hosts']} host(s)", resp.duration_ms)
    out["ok"] = not out["errors"]
    if out["ok"] and not build_ok and not out["warnings"]:
        out["warnings"].append("The build info could not be read: the version is unknown.")
    out["duration_ms"] = round((time.monotonic() - started) * 1000, 1)
    return out


def known_in_product(overview, user=None):
    """How much of what Prometheus sees LinkedEye already holds: CMDB items and devices whose
    ``prometheus_instance`` matches, and the targets telemetry discovery has gathered. Site-scoped to `user`."""
    from cmdb.models import ConfigurationItem
    from network.models import Device
    from telemetry.models import DiscoveredTarget

    instances = list(overview.get("instances") or [])
    known = {"ci": 0, "devices": 0, "discovered": DiscoveredTarget.objects.for_tenant().present().count()}
    tenant = settings.LINKEDEYE_TENANT
    for i in range(0, len(instances), 500):
        chunk = instances[i:i + 500]
        ci = ConfigurationItem.objects.filter(tenant_key=tenant, prometheus_instance__in=chunk)
        dev = Device.objects.filter(tenant_key=tenant, prometheus_instance__in=chunk)
        if user is not None:
            ci, dev = scope_by_site(ci, user), scope_by_site(dev, user)
        known["ci"] += ci.count()
        known["devices"] += dev.count()
    return known


def summary_facts(overview, known=None, discovery=None):
    """What is kept on the target and shown on the page: counts and names, never a login."""
    facts = {k: overview.get(k) for k in ("url", "version", "revision", "go_version", "targets", "up", "down",
                                          "unknown", "hosts", "authenticated", "checked_at", "warnings")}
    facts["jobs"] = [[j, n] for j, n in overview.get("jobs", [])]
    if known is not None:
        facts["known"] = known
    if discovery is not None:
        facts["discovery"] = discovery
    return facts


# ----------------------------------------------------------------------------- the form
class PrometheusForm(forms.Form):
    url = forms.CharField(label="Prometheus URL", max_length=MAX_URL,
                          widget=forms.TextInput(attrs={"placeholder": "https://prometheus.example.internal:9090",
                                                        "autocomplete": "off", "inputmode": "url"}),
                          help_text="The address LinkedEye queries. Private networks must be allowed in "
                                    "LINKEDEYE_PROBE_ALLOWED_PRIVATE_CIDRS.")
    username = forms.CharField(label="HTTP basic user", max_length=150, required=False,
                               widget=forms.TextInput(attrs={"autocomplete": "off"}),
                               help_text="Only when Prometheus sits behind basic auth.")
    password = forms.CharField(label="HTTP basic password", required=False, strip=False,
                               widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}),
                               help_text="Encrypted in the vault on save. Never shown again.")
    replace = forms.BooleanField(label="Replace the Prometheus that is connected now", required=False)
    autocreate = forms.BooleanField(
        label="Create CMDB items, devices and exporters for what it already sees", required=False,
        help_text="Turns on the auto-create switches of discovery (Integrations -> Prometheus).")
    run_discovery = forms.BooleanField(label="Run discovery now", required=False,
                                       help_text="Gather every target, host fact and rule straight away.")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial.pop("password", None)
        self.fields["password"].widget.attrs.pop("value", None)

    def clean_url(self):
        url, error = normalize_url(self.cleaned_data.get("url"))
        if error:
            raise forms.ValidationError(error)
        return url

    def clean(self):
        data = super().clean()
        user, pw = (data.get("username") or "").strip(), data.get("password") or ""
        if pw and not user:
            self.add_error("username", "A password needs the user name that goes with it.")
        if user and not pw:
            self.add_error("password", "Enter the password for that user (it is never shown again).")
        if ":" in user:
            self.add_error("username", "A basic-auth user name cannot contain a colon.")
        data["username"] = user
        return data


# ----------------------------------------------------------------------------- connecting
def current_connection():
    """What the product reads from today: the redacted URL and whether a vault login is bound to it."""
    from administration import integration_config as ic
    url = ic.prometheus_url()
    return {"configured": bool(url), "url": redact(url), "raw_url": url,
            "has_login": bool(ic.prometheus_credential_id())}


def _target_for(run, url, request):
    parts = urlsplit(url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    row = {"hostname": (parts.hostname or "").lower(), "target_type": TargetType.PROMETHEUS, "port": port}
    created, updated, _skipped = services.add_targets(run, [row], request=request)
    return run.targets.filter(hostname=row["hostname"]).first()


def _store_login(run, user, request, url, username, password):
    """Vault row for the login, rotated in place when this Prometheus already has one."""
    from administration import integration_config as ic
    existing_id = ic.prometheus_credential_id()
    existing = Credential.objects.filter(pk=existing_id, tenant_key=settings.LINKEDEYE_TENANT,
                                         kind=CredentialKind.BASIC_AUTH).first() if existing_id else None
    # site "" = tenant-wide: this login serves every site's queries, so it must not be selectable, attachable,
    # rotatable or deletable from one site's credentials step (the SD token is kept the same way)
    return secrets.store(CredentialKind.BASIC_AUTH, username, password, "", user,
                         name=f"Prometheus {redact(url)}"[:120], notes="Login for the connected Prometheus",
                         request=request, credential=existing, client=run.client)


def connect(request, run, form, overview):
    """Save a verified Prometheus. Returns (ok, message). Nothing is written unless `overview["ok"]`.

    Order matters: the vault first (fail closed - without the key nothing is saved), then the settings, then
    the run's target. Raises PermissionDenied when the user lacks the tenant-wide admin role."""
    from administration import integration_config as ic

    if not services.can_manage_discovery(request.user):
        raise PermissionDenied("Prometheus is shared by every site: connecting it needs the admin role on every site.")
    data = form.cleaned_data
    url, username, password = data["url"], data["username"], data.get("password") or ""
    now = current_connection()
    if now["configured"] and normalize_url(now["raw_url"])[0] != url and not data.get("replace"):
        return False, (f"Another Prometheus ({now['url']}) is connected. Tick “Replace the Prometheus that is "
                       "connected now” to switch every site to this one.")
    credential = None
    if username:
        try:
            credential = _store_login(run, request.user, request, url, username, password)
        except (secrets.VaultUnavailable, ValueError) as exc:
            services.log_step(run, "targets", "error", "Prometheus was NOT connected: the vault is unavailable")
            return False, str(exc)                 # the reason, never the value
    else:
        old = ic.prometheus_credential_id()
        if old:                                    # connecting without a login: drop the one bound to the URL
            Credential.objects.filter(pk=old, tenant_key=settings.LINKEDEYE_TENANT).delete()
    ic.save("prometheus", {"url": url, "credential_id": str(credential.pk) if credential else "",
                           "credential_url": url if credential else ""}, request.user)
    if data.get("autocreate"):
        ic.save("prometheus", {"autocreate_ci": True, "autocreate_devices": True,
                               "autoregister_exporters": True}, request.user)
    target = _target_for(run, url, request)
    discovery = None
    if data.get("run_discovery"):
        discovery = _run_discovery(request)
    known = known_in_product(overview, request.user)
    facts = summary_facts(overview, known, discovery)
    if target is not None:
        if credential is not None:
            services.attach_credential(run, credential, [target], request=request)
        services.set_target_state(target, TargetState.EXPORTER_FOUND, error="", facts={
            "exporter": "prometheus", "exporter_version": overview.get("version", ""), "prometheus": facts})
    detail = (f"Prometheus {overview.get('version') or ''} connected at {overview['url']}: "
              f"{overview['targets']} target(s), {overview['hosts']} host(s), {overview['up']} up")
    services.log_step(run, "targets", "ok", detail.strip(), target=target, duration_ms=overview.get("duration_ms"))
    audit(request, "onboarding.prometheus.connect", run, site=run.site, url=overview["url"],
          authenticated=bool(credential), targets=overview["targets"], hosts=overview["hosts"],
          autocreate=bool(data.get("autocreate")), discovery=bool(discovery))
    return True, detail


def _run_discovery(request):
    """One discovery pass (the operator ticked it). The result is counts only."""
    try:
        from telemetry import discovery
        row = discovery.run(trigger="manual")
        audit(request, "discover", object_type="telemetry.discoveredtarget", result="ok" if row.ok else "failed",
              targets=row.targets, new=row.new, created=row.created, error=row.error)
        if not row.ok:
            return {"ok": False, "error": (row.error or "discovery failed")[:200]}
        return {"ok": True, "targets": row.targets, "new": row.new, "linked": row.linked, "created": row.created,
                "summary": row.summary}
    except Exception as exc:                       # noqa: BLE001 - discovery must never break the step
        return {"ok": False, "error": f"discovery failed ({type(exc).__name__})"}


# ----------------------------------------------------------------------------- view
@require("view")
@require_POST
@sensitive_post_parameters()
@sensitive_variables()
def prometheus_connect(request, pk):
    """Test a Prometheus (no writes) or verify-and-connect it. POST + CSRF, admin for the run's site."""
    from . import views
    run = views._run(request, pk)
    views._need_admin(request, run.site)
    form = PrometheusForm(request.POST)
    if not form.is_valid():
        return views._render_step(request, run, "targets", prom_form=form)
    action = request.POST.get("action") or "test"
    data = form.cleaned_data
    overview = fetch_overview(data["url"], data["username"], data.get("password") or "")
    overview["known"] = known_in_product(overview, request.user) if overview["ok"] else None
    audit(request, "onboarding.prometheus.test", run, site=run.site, url=overview["url"], ok=overview["ok"],
          authenticated=overview["authenticated"], mode=action)
    if overview["ok"] and action == "connect":
        try:
            ok, message = connect(request, run, form, overview)
        except PermissionDenied as exc:
            ok, message = False, str(exc)
        if ok:
            services.enter_step(run, "targets")
            messages.success(request, message + ". What it already sees is listed below.")
            return redirect("onboarding:step", pk=run.pk, name="targets")
        messages.error(request, message)
    elif not overview["ok"]:
        services.log_step(run, "targets", "error", f"Prometheus check failed at {overview['url']}")
        messages.error(request, "That Prometheus did not pass the check. Nothing was saved.")
    else:
        messages.success(request, "Prometheus answered. Nothing was saved: press “Verify and connect” to use it.")
    return views._render_step(request, run, "targets", prom_form=form, prom_result=overview)


# ----------------------------------------------------------------------------- the verify hook
def verify_prometheus(target):
    """Verify hook for a ``prometheus`` target (``steps.verify_target`` contract).

    Probes the connected Prometheus when the target is that server, otherwise ``scheme://host:port`` of the
    target. A stored basic-auth login is read from the vault (audited) inside a ``with`` block and only
    ever goes into the request header."""
    from administration import integration_config as ic
    connected = ic.prometheus_url()
    parts = urlsplit(connected) if connected else None
    if parts is not None and (parts.hostname or "").lower() == target.hostname.lower():
        url = connected
    else:
        url = f"{'https' if target.port == 443 else 'http'}://{target.address or target.hostname}:{target.port}"
    cred = target.credential if target.credential_id and target.credential.kind == CredentialKind.BASIC_AUTH else None
    if cred is not None:
        # the connected Prometheus' own login goes only to that server; any other login only from its own site
        if cred.tenant_key != target.run.tenant_key:
            cred = None
        elif cred.pk == ic.prometheus_credential_id():
            cred = cred if url == connected else None
        elif cred.site != target.run.site:
            cred = None
    try:
        if cred is not None:
            with secrets.use(cred, reason="verify prometheus") as password:
                overview = fetch_overview(url, cred.username, password)
        else:
            overview = fetch_overview(url)
    except (secrets.VaultUnavailable, secrets.VaultError) as exc:
        return {"state": TargetState.FAILED, "error": str(exc)}
    facts = {"exporter": "prometheus", "exporter_version": overview.get("version", ""),
             "prometheus": summary_facts(overview), "duration_ms": overview.get("duration_ms")}
    if not overview["ok"]:
        return {**facts, "state": TargetState.UNREACHABLE, "error": (overview["errors"] or ["Prometheus did not answer."])[0]}
    return {**facts, "state": TargetState.EXPORTER_FOUND, "error": ""}
