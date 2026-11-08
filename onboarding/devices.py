"""The network-device path of onboarding: SNMP v2c and v3.

* ``SnmpCredentialForm`` hands the community (v2c) or the v3 login to the vault. The secret is
  encrypted by ``core.secrets.store`` and never rendered, logged, exported or put in an error.
  A v3 login is one encrypted JSON blob (security level, protocols and both passwords); the
  non-secret parts (version, module, auth profile, security level) are also kept on the target so
  the page can describe them.
* ``verify_device(target)`` is the verify hook for a ``device`` target. It asks the **snmp_exporter**
  to walk the device with the chosen module (``GET <exporter>/snmp?target=..&module=..&auth=..``)
  through ``probes.checks``, so the SSRF policy applies to the exporter *and* to the device, and
  records sysName, sysDescr, sysLocation, sysObjectID, uptime and the interface count as facts.

How the credential is used: snmp_exporter takes the community / v3 login from its own ``snmp.yml``
(an *auth profile*), not from the request. LinkedEye therefore verifies against the profile named in
the SNMP settings and, for the Enable step, can describe the profile to add (``exporter_auth_snippet``),
with placeholders where a secret goes: a stored secret is never shown, not even to its owner.
"""
import json
import re
from urllib.parse import urlencode, urlsplit

from django import forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_POST

from core import secrets
from core.access import require
from core.audit import audit
from probes import checks

from . import probing, services
from .models import CredentialKind, TargetState, TargetType

SNMP_KINDS = (CredentialKind.SNMP_V2C, CredentialKind.SNMP_V3)
SNMP_DEFAULT_MODULE = "if_mib"
SNMP_DEFAULT_AUTH = {"v2c": "public_v2", "v3": "linkedeye_v3"}
SECURITY_LEVELS = [("authPriv", "authPriv - authenticate and encrypt"),
                   ("authNoPriv", "authNoPriv - authenticate only"),
                   ("noAuthNoPriv", "noAuthNoPriv - neither")]
AUTH_PROTOCOLS = [("SHA", "SHA"), ("SHA224", "SHA-224"), ("SHA256", "SHA-256"), ("SHA384", "SHA-384"),
                  ("SHA512", "SHA-512"), ("MD5", "MD5 (legacy)")]
PRIV_PROTOCOLS = [("AES", "AES-128"), ("AES192", "AES-192"), ("AES256", "AES-256"), ("DES", "DES (legacy)")]
DEFAULT_COMMUNITIES = {"public", "private", "community", "snmp", "cisco", "admin", "default"}

#: snmp_exporter module / auth names are config identifiers: keep them to a safe alphabet so nothing
#: but a name can reach the exporter's query string
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")
MAX_TEXT = 500


# ----------------------------------------------------------------------------- settings
def exporter_url():
    """The snmp_exporter base address: Administration settings first, then LINKEDEYE_SNMP_EXPORTER_URL."""
    import os

    from administration import integration_config as ic
    value = ic._pick("snmp_exporter", "url", "SNMP_EXPORTER_URL", "") or os.environ.get("LINKEDEYE_SNMP_EXPORTER_URL", "")
    return str(value or "").strip()


def clean_exporter_url(raw):
    """(url, error) for an operator-typed snmp_exporter address."""
    text = (raw or "").strip()
    if not text:
        return "", "Enter the address of the snmp_exporter, e.g. http://snmp-exporter:9116."
    if "://" not in text:
        text = "http://" + text
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        return "", "That is not a valid address."
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "", "Use an http:// or https:// address."
    if parts.username or parts.password:
        return "", "Do not put a login in the address."
    if parts.query or parts.fragment:
        return "", "Enter the address without a query string."
    if len(text) > 300:
        return "", "That address is too long."
    host = parts.hostname
    host = f"[{host}]" if ":" in host else host
    return f"{parts.scheme}://{host}{':' + str(port) if port else ''}{parts.path.rstrip('/')}", ""


def snmp_settings(target):
    """The non-secret SNMP settings of a target: version, module, auth profile (with defaults)."""
    saved = dict((target.facts or {}).get("snmp_settings") or {})
    kind = getattr(target.credential, "kind", "") if target.credential_id else ""
    version = saved.get("version") or ("v3" if kind == CredentialKind.SNMP_V3 else "v2c")
    module = saved.get("module") if _NAME_RE.match(str(saved.get("module") or "")) else SNMP_DEFAULT_MODULE
    auth = saved.get("auth_profile") if _NAME_RE.match(str(saved.get("auth_profile") or "")) \
        else SNMP_DEFAULT_AUTH.get(version, "public_v2")
    return {"version": version, "module": module, "auth_profile": auth,
            "security_level": saved.get("security_level", "")}


def device_targets(run):
    return list(run.targets.filter(target_type=TargetType.DEVICE).select_related("credential"))


# ----------------------------------------------------------------------------- the credential form
class SnmpCredentialForm(forms.Form):
    """Hand over an SNMP community (v2c) or a v3 login. Write-only like the rest of the vault."""
    name = forms.CharField(label="Label", max_length=120,
                           help_text="How this credential is listed. Never put the secret in the label.")
    version = forms.ChoiceField(label="SNMP version", choices=[("v2c", "v2c - community string"),
                                                              ("v3", "v3 - user based security")], initial="v2c")
    community = forms.CharField(
        label="Community string", required=False, strip=False,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}),
        help_text="Encrypted the moment you save. It is never shown again.")
    username = forms.CharField(label="Security name (user)", max_length=150, required=False)
    security_level = forms.ChoiceField(label="Security level", choices=SECURITY_LEVELS, required=False,
                                       initial="authPriv")
    auth_protocol = forms.ChoiceField(label="Authentication", choices=AUTH_PROTOCOLS, required=False, initial="SHA")
    auth_password = forms.CharField(
        label="Authentication password", required=False, strip=False,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}))
    priv_protocol = forms.ChoiceField(label="Privacy", choices=PRIV_PROTOCOLS, required=False, initial="AES")
    priv_password = forms.CharField(
        label="Privacy password", required=False, strip=False,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}))
    module = forms.CharField(label="snmp_exporter module", max_length=64, initial=SNMP_DEFAULT_MODULE,
                             help_text="The module in snmp.yml to walk, e.g. if_mib.")
    auth_profile = forms.CharField(label="snmp_exporter auth profile", max_length=64, required=False,
                                   help_text="The auth profile in snmp.yml that holds this login on the exporter. "
                                             "Empty: public_v2 for v2c, linkedeye_v3 for v3.")
    notes = forms.CharField(label="Notes", max_length=300, required=False)
    apply_to = forms.ChoiceField(label="Apply to", required=False, initial="devices", choices=[
        ("devices", "Every network device of this run"), ("none", "Do not attach it yet")])

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in ("community", "auth_password", "priv_password"):      # belt and braces: never an initial value
            self.fields[field].widget.attrs.pop("value", None)
            self.initial.pop(field, None)

    def clean_module(self):
        value = (self.cleaned_data.get("module") or "").strip()
        if not _NAME_RE.match(value):
            raise forms.ValidationError("A module name: letters, digits, dot, dash or underscore.")
        return value

    def clean_auth_profile(self):
        value = (self.cleaned_data.get("auth_profile") or "").strip()
        if value and not _NAME_RE.match(value):
            raise forms.ValidationError("A profile name: letters, digits, dot, dash or underscore.")
        return value

    def clean(self):
        data = super().clean()
        version = data.get("version")
        if not version:
            return data
        if version == "v2c":
            community = data.get("community") or ""
            if not community.strip():
                self.add_error("community", "Enter the community string. Nothing is stored unencrypted.")
            elif len(community) > 255 or any(ord(c) < 32 for c in community):
                self.add_error("community", "A community string is at most 255 printable characters.")
            else:
                data["secret_value"] = community
                data["username"] = ""
                data["warn_default"] = community.strip().lower() in DEFAULT_COMMUNITIES
            return data
        user = (data.get("username") or "").strip()
        level = data.get("security_level") or "authPriv"
        if not user:
            self.add_error("username", "SNMP v3 needs a security name.")
        auth_pw, priv_pw = data.get("auth_password") or "", data.get("priv_password") or ""
        if level in ("authPriv", "authNoPriv") and len(auth_pw) < 8:
            self.add_error("auth_password", "The authentication password is at least 8 characters (RFC 3414).")
        if level == "authPriv" and len(priv_pw) < 8:
            self.add_error("priv_password", "The privacy password is at least 8 characters (RFC 3414).")
        if not self.errors:
            blob = {"security_level": level}
            if level != "noAuthNoPriv":
                blob.update(auth_protocol=data.get("auth_protocol") or "SHA", auth_password=auth_pw)
            if level == "authPriv":
                blob.update(priv_protocol=data.get("priv_protocol") or "AES", priv_password=priv_pw)
            data["secret_value"] = json.dumps(blob, separators=(",", ":"))
            data["username"] = user
            data["warn_default"] = False
        return data

    def non_secret(self):
        """Settings safe to keep on the target and render."""
        data = self.cleaned_data
        version = data["version"]
        return {"version": version, "module": data["module"],
                "auth_profile": data.get("auth_profile") or SNMP_DEFAULT_AUTH[version],
                "security_level": (data.get("security_level") or "authPriv") if version == "v3" else ""}


# ----------------------------------------------------------------------------- exporter profile
def exporter_auth_snippet(version, username="", security_level="", auth_protocol="", priv_protocol="",
                          profile=None):
    """The ``auths:`` block to add to the exporter's snmp.yml, with placeholders where a secret goes.

    Built from the non-secret settings only. The stored secret is not read, so it cannot leak through
    this text."""
    name = profile or SNMP_DEFAULT_AUTH.get(version, "public_v2")
    if version == "v3":
        lines = [f"  {name}:", "    version: 3", f"    username: {json.dumps(username or '<security name>')}",
                 f"    security_level: {security_level or 'authPriv'}"]
        if (security_level or "authPriv") != "noAuthNoPriv":
            lines += ['    password: "<authentication password>"', f"    auth_protocol: {auth_protocol or 'SHA'}"]
        if (security_level or "authPriv") == "authPriv":
            lines += ['    priv_password: "<privacy password>"', f"    priv_protocol: {priv_protocol or 'AES'}"]
    else:
        lines = [f"  {name}:", '    community: "<community string>"', "    version: 2"]
    return "auths:\n" + "\n".join(lines) + "\n"


# ----------------------------------------------------------------------------- reading the exporter
_METRIC_RE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})?\s+([^\s]+)(?:\s+\d+)?\s*$')
_LABEL_RE = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*"((?:[^"\\]|\\.)*)"')
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _unescape(value):
    return value.replace("\\n", " ").replace('\\"', '"').replace("\\\\", "\\")


def _text(value, limit=MAX_TEXT):
    """Device-supplied text made safe to store: no control characters, bounded."""
    return _CONTROL_RE.sub(" ", str(value or "")).strip()[:limit]


def parse_snmp_metrics(body):
    """Facts from an snmp_exporter response (Prometheus text format).

    ``sysName{sysName="core-sw-01"} 1`` style info metrics carry their text in a label of the same
    name; ``sysUpTime`` is TimeTicks (1/100 s). Returns a dict, empty when the device answered nothing."""
    text = body.decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) else str(body or "")
    out, interfaces, series = {}, set(), 0
    for line in text.splitlines()[:200000]:
        if not line or line[0] == "#":
            continue
        m = _METRIC_RE.match(line)
        if not m:
            continue
        name, raw_labels, raw_value = m.groups()
        if name == "snmp_scrape_pdus_returned":
            try:
                out["pdus"] = int(float(raw_value))
            except ValueError:
                pass
            continue
        if name == "snmp_scrape_duration_seconds":
            try:
                out["scrape_seconds"] = round(float(raw_value), 3)
            except ValueError:
                pass
            continue
        labels = {k: _unescape(v) for k, v in _LABEL_RE.findall(raw_labels or "")}
        if name in ("sysName", "sysDescr", "sysLocation", "sysObjectID") and labels.get(name):
            out.setdefault(name, _text(labels[name]))
            series += 1
        elif name == "sysUpTime":
            try:
                out["uptime_seconds"] = int(float(raw_value) / 100)
            except ValueError:
                pass
            series += 1
        elif name in ("ifOperStatus", "ifDescr", "ifAdminStatus", "ifHCInOctets", "ifInOctets"):
            idx = labels.get("ifIndex") or labels.get("ifDescr")
            if idx:
                interfaces.add(idx)
            series += 1
        elif name == "ifNumber":
            try:
                out["if_number"] = int(float(raw_value))
            except ValueError:
                pass
            series += 1
        elif not name.startswith(("snmp_scrape", "up", "scrape_")):
            series += 1
    if interfaces:
        out["interfaces"] = len(interfaces)
    elif "if_number" in out:
        out["interfaces"] = out["if_number"]
    out.pop("if_number", None)
    out["series"] = series
    return out


# ----------------------------------------------------------------------------- the verify hook
def _step_log(target, status, detail, ms=None):
    run = getattr(target, "run", None)
    if run is not None:
        services.log_step(run, "verify", status, detail, target=target, duration_ms=ms)


def verify_device(target):
    """Verify a ``device`` target. Returns a dict in the ``steps.verify_target`` hook contract.

    Stages, each written to the run's step log with its outcome and duration:
      1. the device name resolves and is a permitted probe target (``probes.checks.resolve_allowed``);
      2. the snmp_exporter answers ``/snmp?target=..&module=..&auth=..`` under the same policy;
      3. what came back: sysName / sysDescr / sysLocation / sysObjectID / uptime / interface count.
    An expected failure is a result (``state: unreachable`` and an ``error``), never an exception. No secret
    is read, returned or logged; the response body is parsed, never echoed.
    """
    import time

    cfg = snmp_settings(target)
    host = (target.address or target.hostname or "").strip()
    facts = {"exporter": "snmp_exporter", "snmp_version": cfg["version"], "snmp_module": cfg["module"],
             "snmp_settings": cfg}
    if target.credential_id and target.credential.kind not in SNMP_KINDS:
        facts["credential_note"] = "The attached credential is not an SNMP one; the exporter's own auth profile is used."
    elif not target.credential_id:
        facts["credential_note"] = "No SNMP credential is attached; the exporter's auth profile is used as configured."

    # 1. the device itself must resolve and be a permitted probe target
    t0 = time.monotonic()
    try:
        facts["resolved_address"] = checks.resolve_allowed(host, 161)
        facts["dns"] = True
        _step_log(target, "ok", f"{target.hostname}: name resolves and is a permitted probe target",
                  round((time.monotonic() - t0) * 1000, 1))
    except checks.ProbeBlocked:
        facts["dns"] = False
        _step_log(target, "error", f"{target.hostname}: {probing.POLICY_HINT}", round((time.monotonic() - t0) * 1000, 1))
        return {**facts, "state": TargetState.UNREACHABLE, "error": probing.POLICY_HINT}
    except OSError:
        facts["dns"] = False
        msg = "DNS resolution failed: the device name does not resolve."
        _step_log(target, "error", f"{target.hostname}: {msg}", round((time.monotonic() - t0) * 1000, 1))
        return {**facts, "state": TargetState.UNREACHABLE, "error": msg}

    # 2. the exporter
    base = exporter_url()
    if not base:
        msg = ("The SNMP exporter address is not set. Set it on the Credentials step "
               "(or LINKEDEYE_SNMP_EXPORTER_URL) and verify again.")
        _step_log(target, "error", f"{target.hostname}: {msg}")
        return {**facts, "state": TargetState.FAILED, "error": msg}
    # the exporter is given the address that passed the policy check, so a DNS answer cannot change in between
    query = urlencode({"target": facts["resolved_address"], "module": cfg["module"], "auth": cfg["auth_profile"]})
    resp = probing.get(f"{base.rstrip('/')}/snmp?{query}", max_bytes=2 * 1024 * 1024)
    facts["duration_ms"] = resp.duration_ms
    facts["http_status"] = resp.status
    if not resp.ok:
        _step_log(target, "error", f"{target.hostname}: snmp_exporter: {resp.error}", resp.duration_ms)
        return {**facts, "state": TargetState.UNREACHABLE, "error": f"The SNMP exporter did not answer. {resp.error}"}
    if not resp.success:
        # the exporter's own error text can name internals; give the status and a fixed explanation
        hint = {400: "the module or auth profile is not defined in the exporter's snmp.yml",
                404: "that address is not an snmp_exporter (no /snmp endpoint)",
                401: "the exporter wants a login", 403: "the exporter refused the request"}.get(
                    resp.status, "the exporter failed to walk the device")
        msg = f"The SNMP exporter answered HTTP {resp.status}: {hint}."
        _step_log(target, "error", f"{target.hostname}: {msg}", resp.duration_ms)
        return {**facts, "state": TargetState.UNREACHABLE, "error": msg}

    # 3. what the device told us
    found = parse_snmp_metrics(resp.body)
    if not found.get("series") or not any(k in found for k in ("sysName", "sysDescr", "sysUpTime", "interfaces")):
        msg = ("The exporter answered but the device returned no data: check the community or v3 login in the "
               "exporter's auth profile, that SNMP is enabled on the device and that UDP 161 is reachable "
               "from the exporter.")
        _step_log(target, "error", f"{target.hostname}: no SNMP data returned", resp.duration_ms)
        return {**facts, "state": TargetState.UNREACHABLE, "error": msg}
    snmp = {k: found[k] for k in ("sysName", "sysDescr", "sysLocation", "sysObjectID", "uptime_seconds",
                                  "interfaces", "pdus", "scrape_seconds") if k in found}
    facts["snmp"] = snmp
    if found.get("sysName"):
        facts["hostname"] = found["sysName"]
    if found.get("sysDescr"):
        facts["os"] = found["sysDescr"][:200]
    _step_log(target, "ok", f"{target.hostname}: snmp_exporter walked {cfg['module']}: "
                            f"{found.get('sysName') or 'unnamed'}, {found.get('interfaces', 0)} interface(s), "
                            f"{found['series']} series", resp.duration_ms)
    return {**facts, "state": TargetState.EXPORTER_FOUND, "error": ""}


def enable_device(target, options=None):
    """The enable_collection hook for a ``device`` target: choose the snmp_exporter module (the operator's
    entry on the Enable step, else the one already saved, else ``if_mib``), check that the exporter walks the
    device with it, and hand back the ``auths:`` block the exporter needs. Nothing is installed on the device.
    The snippet holds placeholders where a secret goes; the stored login is never read here."""
    options = options or {}
    cfg = snmp_settings(target)
    asked = str(options.get("snmp_module") or "").strip()
    note = ""
    if asked:
        if _NAME_RE.match(asked):
            cfg["module"] = asked
        else:
            note = "The module name was ignored: use letters, digits, dot, dash and underscore only."
    target.facts = {**(target.facts or {}), "snmp_settings": cfg}      # verify_device reads the module from here
    result = verify_device(target)
    cred = target.credential if target.credential_id and target.credential.kind in SNMP_KINDS else None
    snippet = exporter_auth_snippet(
        cfg["version"], username=(cred.username if cred is not None and cfg["version"] == "v3" else ""),
        security_level=cfg.get("security_level", ""), profile=cfg["auth_profile"])
    result["snmp_snippet"] = snippet
    if result.get("state") == TargetState.EXPORTER_FOUND:
        result["note"] = (f"The snmp_exporter walked this device with module {cfg['module']} and auth profile "
                          f"{cfg['auth_profile']}. {note}").strip()
    else:
        result["note"] = (f"Module {cfg['module']} is saved for this device. The exporter's snmp.yml needs the auth "
                          f"profile {cfg['auth_profile']} (block below, fill in the secrets there). {note}").strip()
    return result


# ----------------------------------------------------------------------------- views
def _views():
    from . import views                      # lazy: views imports steps, and steps may import this module
    return views


def _guard(request, pk):
    views = _views()
    run = views._run(request, pk)
    views._need_admin(request, run.site)
    return run


def _back(run):
    return redirect("onboarding:step", pk=run.pk, name="credentials")


@require("view")
@require_POST
@sensitive_post_parameters()
@sensitive_variables()
def snmp_save(request, pk):
    """Store an SNMP credential in the vault and attach it to the run's network devices."""
    run = _guard(request, pk)
    form = SnmpCredentialForm(request.POST)
    if not form.is_valid():
        return _views()._render_step(request, run, "credentials", snmp_form=form)
    data = form.cleaned_data
    try:
        credential = secrets.store(
            CredentialKind.SNMP_V3 if data["version"] == "v3" else CredentialKind.SNMP_V2C,
            data.get("username") or "", data["secret_value"], run.site, request.user,
            name=data["name"], notes=data.get("notes") or "", request=request, client=run.client)
    except (secrets.VaultUnavailable, ValueError) as exc:
        messages.error(request, str(exc))                       # the reason, never the value
        services.log_step(run, "credentials", "error", "SNMP credential was NOT stored: the vault is unavailable")
        return _back(run)
    settings_ = form.non_secret()
    services.log_step(run, "credentials", "ok",
                      f"{credential.get_kind_display()} “{credential.name}” stored (encrypted)")
    attached = 0
    targets = device_targets(run)
    if (data.get("apply_to") or "devices") == "devices":
        attached = services.attach_credential(run, credential, targets, request=request) if targets else 0
    for target in targets:
        if (data.get("apply_to") or "devices") == "devices":
            services.set_target_state(target, target.state, error=target.last_error,
                                      facts={"snmp_settings": settings_})
    audit(request, "onboarding.snmp.credential", run, site=run.site, version=data["version"],
          credential=credential.pk, devices=attached)
    messages.success(request, f"“{credential.name}” stored in the vault and attached to {attached} device(s). "
                              "It is never shown again.")
    if data.get("warn_default"):
        messages.warning(request, "That community string is a well-known default. Change it on the device "
                                  "and rotate the stored value.")
    if not targets:
        messages.info(request, "This run has no network device yet: add targets of type “Network device”, "
                               "then attach the credential.")
    return _back(run)


@require("view")
@require_POST
def snmp_test(request, pk):
    """Verify the run's network devices through the snmp_exporter now (an explicit operator action)."""
    run = _guard(request, pk)
    views = _views()
    wanted = request.POST.get("target")
    targets = [t for t in device_targets(run) if not wanted or str(t.pk) == wanted]
    if not targets:
        messages.error(request, "There is no network device on this run to test.")
        return _back(run)
    services.enter_step(run, "credentials")
    ok = bad = 0
    for target in targets:
        result = verify_device(target)
        state, error, facts = services.hook_outcome(
            result, target.state, TargetState.UNREACHABLE,
            default_state=services.verify_default_state(services.scrub(result)))
        services.set_target_state(target, state, error=error, facts=facts)
        views._record(run, "verify", target, target.get_state_display(), error, facts.get("duration_ms"))
        if state in (TargetState.UNREACHABLE, TargetState.FAILED):
            bad += 1
        else:
            ok += 1
    audit(request, "onboarding.snmp.test", run, site=run.site, devices=len(targets), ok=ok)
    if bad:
        messages.warning(request, f"{ok} device(s) answered, {bad} did not. Details are on each row.")
    else:
        messages.success(request, f"{ok} device(s) answered through the SNMP exporter. sysName, sysDescr and "
                                  "sysLocation are recorded.")
    return _back(run)


@require("view")
@require_POST
def snmp_exporter_save(request, pk):
    """Set the snmp_exporter address. It is a tenant-wide setting, so it needs the admin role on every site."""
    run = _guard(request, pk)
    if not services.can_manage_discovery(request.user):
        raise PermissionDenied("The SNMP exporter address is shared by every site: it needs the admin role on every site.")
    url, error = clean_exporter_url(request.POST.get("exporter_url"))
    if error:
        messages.error(request, error)
        return _back(run)
    from administration import integration_config as ic
    ic.save("snmp_exporter", {"url": url}, request.user)
    audit(request, "onboarding.snmp.exporter", run, site=run.site)
    services.log_step(run, "credentials", "ok", "SNMP exporter address saved")
    messages.success(request, "SNMP exporter address saved.")
    return _back(run)

