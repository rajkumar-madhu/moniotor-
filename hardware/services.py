"""Hardware services: scoping, the vault login, polling (fetch without the database, then apply in one transaction), health
roll-up and the events on transitions, retention, test connection and certificate pinning, control requests.

Nothing in here writes to a BMC. The client has no write method; ``ControlRequest`` approvals only record a decision."""
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from core import secrets as vault
from core.access import scope_by_site

from . import collector, parsers, redfish
from .models import (CRITICAL, OK, UNKNOWN, WARNING, BmcEndpoint, ControlRequest, HwComponent, HwEvent, HwReading)

log = logging.getLogger("linkedeye.hardware")

# ----------------------------------------------------------------------------- settings (overridable in settings.py)
READING_RETENTION_DAYS = getattr(settings, "HARDWARE_READING_RETENTION_DAYS", 7)
FULL_RES_HOURS = 24                    # readings older than this are thinned to one per THIN_MINUTES
THIN_MINUTES = 10
EVENT_RETENTION_DAYS = getattr(settings, "HARDWARE_EVENT_RETENTION_DAYS", 90)
POLL_WORKERS = getattr(settings, "HARDWARE_POLL_WORKERS", 4)
MAX_PER_RUN = 16
MIN_INTERVAL = 30
FAIL_ALERT_AFTER = 3                   # consecutive failures before "unreachable" is announced
AUTH_ALERT_AFTER = 2
AUTH_PAUSE_AFTER = 3                   # failed logins in a row before polling stops (a BMC may lock the account)
BACKOFF_MAX = 900
REFRESH_GAP = 15                       # seconds between two "Refresh" presses
TEST_GAP = 10                          # seconds between two login tests of one endpoint
TEST_GAP_AFTER_REJECT = 60             # ... after the BMC rejected the login (BMCs lock an account after a few quick failures)

MANUAL_STEPS = {
    ControlRequest.POWER_ON: "Power the server on from the BMC web console (Power menu), or with the BMC command line "
                             "(racadm serveraction powerup, or iLO: power on in the Power Management page).",
    ControlRequest.GRACEFUL: "Ask the operating system to shut down first, then use the BMC Power menu's graceful shutdown "
                             "(racadm serveraction graceshutdown), after checking that nothing critical is still running.",
    ControlRequest.FORCE_OFF: "Only if a graceful shutdown did not work: use the BMC Power menu's force power off "
                              "(racadm serveraction powerdown). Unsaved data is lost.",
    ControlRequest.FIRMWARE: "Schedule the update in the BMC's firmware page or with your vendor's update tool, in an agreed "
                             "maintenance window, and read the release notes first.",
}


#: the columns a poll writes. A poll reads the BMC for seconds with the endpoint loaded beforehand, so it saves only these: a full
#: save would put back whatever an administrator changed meanwhile (enabled, pin, vendor override, alert limits, the login).
POLL_STATE_FIELDS = ["vendor", "health", "health_changed_at", "conn_state", "last_polled_at", "last_ok_at", "last_error",
                     "consecutive_failures", "auth_failures", "next_poll_at", "overview", "power_state", "sections",
                     "inventory_at", "alert_state"]


def tenant():
    return settings.LINKEDEYE_TENANT


# ----------------------------------------------------------------------------- scoping
def endpoints_for(user):
    """Endpoints the user may see: the tenant's, on the device's site (a device on 'All Sites' is visible to everybody)."""
    qs = BmcEndpoint.objects.filter(tenant_key=tenant()).select_related("device")
    return scope_by_site(qs, user, field="device__site")


def components_for(user):
    return scope_by_site(HwComponent.objects.filter(tenant_key=tenant()), user, field="endpoint__device__site")


def requests_for(user):
    return scope_by_site(ControlRequest.objects.filter(tenant_key=tenant()).select_related("endpoint__device"), user,
                         field="endpoint__device__site")


# ----------------------------------------------------------------------------- the vault login
def credential_row(endpoint):
    from django.apps import apps
    if not endpoint.credential_id:
        return None
    return apps.get_model("onboarding", "Credential").objects.filter(pk=endpoint.credential_id, tenant_key=endpoint.tenant_key).first()


@sensitive_variables()
def login_of(endpoint):
    """(username, password) from the credential vault. Raises ValueError with a fixed message (no secret in it).
    Read through ``vault.decrypt`` (not ``vault.use``): the poll job would otherwise write an audit row every minute."""
    row = credential_row(endpoint)
    if row is None:
        raise ValueError("No login is stored for this BMC. Edit the endpoint and enter the read-only user name and password.")
    try:
        return row.username, vault.decrypt(row.secret)
    except (vault.VaultUnavailable, vault.VaultError):
        raise ValueError("The credential vault cannot decrypt the stored login (check LINKEDEYE_CREDENTIAL_KEY).") from None


@sensitive_variables("password")
def store_login(request, endpoint, username, password):
    """Create or rotate the vault row. `password` empty keeps the stored one (a new user name is applied to it)."""
    from django.apps import apps
    Credential = apps.get_model("onboarding", "Credential")
    row = credential_row(endpoint)
    site = endpoint.device.site or ""
    name = f"BMC login: {endpoint.device.name}"[:120]
    if password:
        row = vault.store("basic_auth", username, password, site, getattr(request, "user", None), name=name,
                          notes="Read-only Redfish account of a server BMC. Used only to read sensors; never to change anything.",
                          request=request, credential=row)
        if endpoint.credential_id != row.pk:
            endpoint.credential_id = row.pk
            endpoint.save(update_fields=["credential_id"])
        return "stored"
    if row is not None and username and row.username != username:
        row.username = username
        row.save(update_fields=["username"])
        return "user"
    if row is not None and row.site != site:
        Credential.objects.filter(pk=row.pk).update(site=site)
    return ""


def sync_credential_site(endpoint):
    """Keep the vault row's site equal to the device's (a device can move to another site)."""
    row = credential_row(endpoint)
    site = endpoint.device.site or ""
    if row is not None and row.site != site:
        type(row).objects.filter(pk=row.pk).update(site=site)


# ----------------------------------------------------------------------------- polling
def new_client(endpoint, username, password, **kw):
    return redfish.make_client(endpoint.host, endpoint.port, username, password, endpoint.verify_mode,
                               endpoint.pinned_sha256, insecure_http=endpoint.insecure_http, **kw)


def wants_inventory(endpoint, now):
    return endpoint.inventory_at is None or (now - endpoint.inventory_at).total_seconds() > collector.INVENTORY_EVERY


def fetch(client, vendor_hint="", inventory=True):
    """Network and parsing only (no database): safe to run in a worker thread. Returns a Snapshot or raises RedfishError."""
    try:
        with client:
            return parsers.parse(collector.collect(client, vendor_hint, inventory=inventory))
    except redfish.RedfishError:
        raise
    except Exception:                                               # noqa: BLE001 - a parser bug must not stop the job
        log.exception("hardware: unexpected error while reading a BMC")
        raise redfish.ProtocolError("The BMC's answer could not be understood.") from None


def _next_interval(endpoint, now):
    return now + timedelta(seconds=max(MIN_INTERVAL, endpoint.poll_interval_s or 60))


def apply_overrides(endpoint, comps):
    """Per-endpoint warning / critical overrides ("Configure alerts"): they can only make a status MORE severe."""
    for c in comps:
        if c.kind == "temperature":
            if endpoint.temp_warn_c is not None:
                c.warn = endpoint.temp_warn_c
            if endpoint.temp_crit_c is not None:
                c.crit = endpoint.temp_crit_c
            if c.reading is not None:
                if endpoint.temp_crit_c is not None and c.reading >= endpoint.temp_crit_c:
                    c.status = parsers.worst([c.status, CRITICAL])
                elif endpoint.temp_warn_c is not None and c.reading >= endpoint.temp_warn_c:
                    c.status = parsers.worst([c.status, WARNING])
        elif c.kind == "fan" and c.unit == "RPM":
            if endpoint.fan_warn_rpm is not None:
                c.low_warn = endpoint.fan_warn_rpm
            if endpoint.fan_crit_rpm is not None:
                c.low_crit = endpoint.fan_crit_rpm
            if c.reading is not None:
                if endpoint.fan_crit_rpm is not None and c.reading <= endpoint.fan_crit_rpm:
                    c.status = parsers.worst([c.status, CRITICAL])
                elif endpoint.fan_warn_rpm is not None and c.reading <= endpoint.fan_warn_rpm:
                    c.status = parsers.worst([c.status, WARNING])


def rollup(statuses, bmc_status=UNKNOWN):
    """healthy | warning | critical | unknown. Unknown parts never make it 'healthy' and never hide a real problem."""
    worst = parsers.worst(list(statuses) + [bmc_status])
    if worst == CRITICAL:
        return CRITICAL
    if worst == WARNING:
        return WARNING
    return "healthy" if worst == OK else UNKNOWN


#: component kinds and the sections whose success proves that an unseen component is really gone
GONE_WHEN = {"temperature": ("thermal",), "fan": ("thermal",), "psu": ("power",), "voltage": ("power",),
             "intrusion": ("chassis",), "cpu": ("processors",), "dimm": ("memory",), "drive": ("storage",),
             "storage": ("storage",), "nic": ("nics",)}


def apply_snapshot(endpoint, snap, now=None, inventory=False):
    """Store one successful poll. Returns {"transition": new alert state or "", "components": n, "events": n}.
    The event of a transition is emitted AFTER the transaction committed: the listeners (incidents, notifications, workflows) may
    talk to the network, and must not run while this poll still holds its database locks."""
    with transaction.atomic():
        out, announce = _store_snapshot(endpoint, snap, now or timezone.now(), inventory)
    if announce:
        _emit(endpoint, *announce)
    return out


def _store_snapshot(endpoint, snap, now, inventory):
    comps = snap.comps
    apply_overrides(endpoint, comps)
    seen = set()
    existing = {(c.kind, c.key): c for c in HwComponent.objects.filter(endpoint=endpoint)}
    creates, updates, readings = [], [], []
    for c in comps:
        k = (c.kind, c.key)
        seen.add(k)
        row = existing.get(k)
        fields = dict(name=c.name, reading=c.reading, unit=c.unit, warn_at=c.warn, crit_at=c.crit, low_warn=c.low_warn,
                      low_crit=c.low_crit, status=c.status, health=c.health, state=c.state, attrs=c.attrs,
                      position=c.position, updated_at=now)
        if row is None:
            row = HwComponent(tenant_key=endpoint.tenant_key, endpoint=endpoint, kind=c.kind, key=c.key, **fields)
            creates.append(row)
        else:
            for f, v in fields.items():
                setattr(row, f, v)
            updates.append(row)
        if c.kind in HwComponent.SERIES and c.reading is not None:
            readings.append((row, c.reading))
    HwComponent.objects.bulk_create(creates)
    if updates:
        HwComponent.objects.bulk_update(updates, ["name", "reading", "unit", "warn_at", "crit_at", "low_warn", "low_crit",
                                                  "status", "health", "state", "attrs", "position", "updated_at"])
    # a part that is no longer reported by a section that was read successfully is gone (a removed fan, a pulled drive)
    gone = [pk.pk for (kind, key), pk in existing.items()
            if (kind, key) not in seen and kind in GONE_WHEN and all(snap.sections.get(s) == "ok" for s in GONE_WHEN[kind])]
    if inventory and snap.sections.get("power") == "ok" and snap.sections.get("storage") in ("ok", "missing"):
        gone += [row.pk for (kind, key), row in existing.items() if kind == "battery" and (kind, key) not in seen]
    if gone:
        HwComponent.objects.filter(pk__in=gone).delete()
    HwReading.objects.bulk_create([HwReading(tenant_key=endpoint.tenant_key, component=r, ts=now, value=v) for r, v in readings])

    new_events = 0
    if snap.events:
        before = HwEvent.objects.filter(endpoint=endpoint).count()
        HwEvent.objects.bulk_create(
            [HwEvent(tenant_key=endpoint.tenant_key, endpoint=endpoint, entry_id=e.entry_id, ts=e.ts, severity=e.severity,
                     sensor=e.sensor, message=e.message, log=e.log) for e in snap.events], ignore_conflicts=True, batch_size=100)
        new_events = HwEvent.objects.filter(endpoint=endpoint).count() - before

    health = rollup([c.status for c in comps if c.kind not in ("nic",)], snap.bmc_status)
    endpoint.vendor = snap.vendor or endpoint.vendor
    if health != endpoint.health:
        endpoint.health_changed_at = now
    old_alert = endpoint.alert_state
    endpoint.health = health
    endpoint.conn_state = BmcEndpoint.CONN_OK
    endpoint.last_polled_at = endpoint.last_ok_at = now
    endpoint.last_error = ""
    endpoint.consecutive_failures = endpoint.auth_failures = 0
    endpoint.next_poll_at = _next_interval(endpoint, now)
    ov = dict(endpoint.overview or {})
    ov.update({k: v for k, v in snap.overview.items() if v not in ("", None)})
    endpoint.overview = ov
    endpoint.power_state = snap.power_state or endpoint.power_state
    sections = dict(endpoint.sections or {})
    sections.update({k: v for k, v in snap.sections.items() if v != "skipped"})
    endpoint.sections = sections
    if inventory:
        endpoint.inventory_at = now
    new_alert = {"healthy": "ok", WARNING: WARNING, CRITICAL: CRITICAL}.get(health, "")
    transition = ""
    if new_alert:
        if new_alert != old_alert and not (new_alert == "ok" and old_alert == ""):
            transition = new_alert                           # the very first healthy poll is remembered, not announced
        endpoint.alert_state = new_alert
    endpoint.save(update_fields=POLL_STATE_FIELDS)
    return ({"transition": transition, "components": len(comps), "events": new_events, "health": health},
            (transition, comps) if transition else None)


def apply_failure(endpoint, exc, now=None):
    """Record a failed poll: state, back-off, and the unreachable / auth-failed transition (after a few in a row)."""
    now = now or timezone.now()
    state = BmcEndpoint.CONN_AUTH if exc.state == "auth_failed" else BmcEndpoint.CONN_UNREACHABLE
    endpoint.conn_state = state
    endpoint.last_polled_at = now
    endpoint.last_error = parsers.text(redfish.scrub(str(exc)), 300)
    endpoint.consecutive_failures += 1
    if state == BmcEndpoint.CONN_AUTH:
        if isinstance(exc, redfish.AuthFailed):             # a missing stored login is not a rejected login: it must not pause polling
            endpoint.auth_failures += 1
        delay = 900
    else:
        delay = min(max(MIN_INTERVAL, endpoint.poll_interval_s or 60) * (2 ** min(endpoint.consecutive_failures - 1, 5)), BACKOFF_MAX)
    endpoint.next_poll_at = now + timedelta(seconds=delay)
    new_alert, transition = "", ""
    if state == BmcEndpoint.CONN_AUTH and (endpoint.auth_failures >= AUTH_ALERT_AFTER or not isinstance(exc, redfish.AuthFailed)):
        new_alert = "auth_failed"
    elif state == BmcEndpoint.CONN_UNREACHABLE and endpoint.consecutive_failures >= FAIL_ALERT_AFTER:
        new_alert = "unreachable"
    if new_alert and new_alert != endpoint.alert_state:
        transition = new_alert
        endpoint.alert_state = new_alert
    endpoint.save(update_fields=POLL_STATE_FIELDS)
    if transition:
        _emit(endpoint, transition, [])
    return {"transition": transition, "state": state}


def _emit(endpoint, state, comps):
    """One core event per transition (the fingerprint is per endpoint, so a recovery closes the incident that a critical
    event opened). Example endpoints and endpoints with alerts switched off emit nothing."""
    if endpoint.is_example or not endpoint.alerts_enabled:
        return
    from core.events import emit
    dev = endpoint.device
    bad = [c for c in comps if c.status in (WARNING, CRITICAL) and c.kind != "system"]
    bad.sort(key=lambda c: -parsers.RANK[c.status])
    detail = "; ".join(f"{c.name} is {c.status}" for c in bad[:5]) + (f" (+{len(bad) - 5} more)" if len(bad) > 5 else "")
    label = f"{dev.name} {endpoint.bmc_name}"
    if state == "ok":
        title, sev, msg = f"{label}: hardware health is back to normal", "ok", "All reported hardware components are healthy."
    elif state == CRITICAL:
        title, sev, msg = f"{label}: hardware health is critical", "critical", detail or "The BMC reports a critical condition."
    elif state == WARNING:
        title, sev, msg = f"{label}: hardware health warning", "warning", detail or "The BMC reports a warning."
    elif state == "unreachable":
        title, sev, msg = f"{label} is not reachable", "warning", endpoint.last_error or "The BMC does not answer."
    else:
        title, sev, msg = f"{label} rejected the login", "warning", ("The read-only account was refused. Polling stops after "
                                                                      "repeated failures so the account is not locked.")
    emit(source="hardware", title=title, severity=sev, message=msg[:1000], site=dev.site, obj=dev,
         fingerprint=f"hardware:{endpoint.pk}", ci=dev.name, host=endpoint.host, vendor=endpoint.effective_vendor,
         status=state, endpoint=endpoint.pk)


def poll_endpoint(endpoint, now=None, force_inventory=False):
    """Poll one endpoint now, in this thread (tests, the Refresh action when run inline, the job's single-endpoint path)."""
    now = now or timezone.now()
    try:
        user, pw = login_of(endpoint)
    except ValueError as exc:
        return apply_failure(endpoint, redfish.LoginUnavailable(str(exc)), now)
    inv = force_inventory or wants_inventory(endpoint, now)
    try:
        snap = fetch(new_client(endpoint, user, pw), endpoint.vendor_override or "", inv)
    except redfish.RedfishError as exc:
        return apply_failure(endpoint, _scrubbed(exc, user, pw), now)
    return apply_snapshot(endpoint, snap, now, inventory=inv)


def _scrubbed(exc, user, pw):
    return type(exc)(redfish.scrub(str(exc), user, pw)) if not isinstance(exc, redfish.CertificateError) else exc


def due_endpoints(now=None, limit=MAX_PER_RUN):
    now = now or timezone.now()
    qs = (BmcEndpoint.objects.filter(tenant_key=tenant(), enabled=True, is_example=False, credential_id__isnull=False,
                                     auth_failures__lt=AUTH_PAUSE_AFTER)
          .filter(Q(next_poll_at__isnull=True) | Q(next_poll_at__lte=now))
          .exclude(host="").select_related("device").order_by("next_poll_at", "pk"))
    return list(qs[:limit])


def claim(endpoint, now):
    """Take the poll for this endpoint (compare-and-set on next_poll_at), so two workers never poll the same BMC."""
    lease = now + timedelta(seconds=60)
    return BmcEndpoint.objects.filter(pk=endpoint.pk, next_poll_at=endpoint.next_poll_at).update(next_poll_at=lease) == 1


def poll_due(now=None, workers=None):
    """The job body: network reads run in bounded worker threads (no database access there), results are applied here,
    one transaction each. Returns a summary string."""
    now = now or timezone.now()
    workers = workers or POLL_WORKERS
    todo = []
    for ep in due_endpoints(now):
        if not claim(ep, now):
            continue
        ep.refresh_from_db()
        try:
            user, pw = login_of(ep)
        except ValueError as exc:
            apply_failure(ep, redfish.LoginUnavailable(str(exc)), now)
            continue
        if ep.verify_mode == BmcEndpoint.VERIFY_PIN and not ep.pinned_sha256:
            apply_failure(ep, redfish.CertificateError("No certificate is pinned yet. Run Test connection and confirm the fingerprint."), now)
            continue
        todo.append((ep, user, pw, wants_inventory(ep, now)))
    if not todo:
        return "no endpoint due"
    results = []

    def work(item):
        ep, user, pw, inv = item
        try:
            return item, fetch(new_client(ep, user, pw), ep.vendor_override or "", inv), None
        except redfish.RedfishError as exc:
            return item, None, _scrubbed(exc, user, pw)

    if workers <= 1 or len(todo) == 1:
        results = [work(i) for i in todo]
    else:
        with ThreadPoolExecutor(max_workers=min(workers, len(todo))) as pool:
            results = list(pool.map(work, todo))
    ok = bad = 0
    for (ep, _u, _p, inv), snap, exc in results:
        try:
            if exc is None:
                apply_snapshot(ep, snap, timezone.now(), inventory=inv)
                ok += 1
            else:
                apply_failure(ep, exc, timezone.now())
                bad += 1
        except Exception:                                           # noqa: BLE001 - one endpoint never stops the others
            log.exception("hardware: could not store the poll of endpoint %s", ep.pk)
            bad += 1
            try:                                                    # say so on the endpoint (and back off), not only in the log
                ep.refresh_from_db()
                apply_failure(ep, redfish.ProtocolError("The BMC's answer could not be stored (a value it reported is out of "
                                                        "range). See the server log."), timezone.now())
            except Exception:                                       # noqa: BLE001
                log.exception("hardware: could not record the failed store of endpoint %s", ep.pk)
    return f"{ok} polled, {bad} failed"


def request_refresh(endpoint, now=None):
    """Queue an immediate poll (the job takes it within one scheduler pass). Returns (ok, message). Rate limited."""
    now = now or timezone.now()
    if endpoint.is_example:
        return False, "This is example data: there is no live BMC to poll."
    if not endpoint.enabled:
        return False, "This endpoint is disabled."
    if endpoint.auth_paused:
        return False, "Polling is paused after repeated login failures. Update the password on the endpoint to resume."
    if not endpoint.configured:
        return False, "The endpoint is not configured yet (stored login and certificate)."
    if endpoint.refresh_requested_at and (now - endpoint.refresh_requested_at).total_seconds() < REFRESH_GAP:
        wait = int(REFRESH_GAP - (now - endpoint.refresh_requested_at).total_seconds()) + 1
        return False, f"A refresh was just requested. Try again in {wait} s."
    # compare-and-set, so two presses at the same moment still queue one poll
    won = (BmcEndpoint.objects.filter(pk=endpoint.pk).filter(Q(refresh_requested_at__isnull=True)
                                                              | Q(refresh_requested_at__lte=now - timedelta(seconds=REFRESH_GAP)))
           .update(refresh_requested_at=now, next_poll_at=now))
    if not won:
        return False, f"A refresh was just requested. Try again in {REFRESH_GAP} s."
    endpoint.refresh_requested_at, endpoint.next_poll_at = now, now
    return True, "Refresh queued: the next scheduler pass reads the BMC."


# ----------------------------------------------------------------------------- test connection and pinning
def _test_wait(endpoint):
    """Seconds to wait before the next test of this endpoint (0 = go). The cache entry is the atomic gate inside one process,
    the audit trail covers the other worker processes."""
    from django.core.cache import cache

    from core.models import AuditEvent
    gap = max(TEST_GAP, TEST_GAP_AFTER_REJECT) if endpoint.auth_failures and TEST_GAP else TEST_GAP
    if not cache.add(f"hardware:test:{endpoint.pk}", 1, gap):
        return gap
    last = (AuditEvent.objects.filter(action="hardware.endpoint.test", object_type=endpoint._meta.label_lower, object_id=str(endpoint.pk),
                                      details__login_attempted=True)
            .order_by("-occurred_at").values_list("occurred_at", flat=True).first())
    if last is not None:
        since = (timezone.now() - last).total_seconds()
        if since < gap:
            return max(1, int(gap - since) + 1)
    return 0


def test_connection(endpoint):
    """Check reachability, certificate, login and vendor WITHOUT sending the login to an unverified server.
    Returns a dict for the page: ok, steps [(label, ok, detail)], fingerprint, trusted, needs_pin, vendor, ..."""
    out = {"ok": False, "steps": [], "fingerprint": "", "trusted": False, "needs_pin": False, "vendor": "", "overview": {},
           "mismatch": False, "login_attempted": False}

    def step(label, ok, detail=""):
        out["steps"].append({"label": label, "ok": ok, "detail": parsers.text(detail, 300)})
        return ok

    try:
        if not endpoint.insecure_http:
            cert = redfish.inspect_certificate(endpoint.host, endpoint.port)
            out.update(fingerprint=cert["fingerprint"], trusted=cert["trusted"])
            step("Reachable", True, f"TLS {cert['tls']}" if cert["tls"] else "")
            step("Certificate", True, "Signed by a CA this server trusts." if cert["trusted"] else "Not signed by a trusted CA (normal for a BMC with its own certificate).")
        else:
            redfish.resolve(endpoint.host, endpoint.port)
            step("Reachable", True, "Plain HTTP: the login would cross the network unencrypted.")
    except redfish.RedfishError as exc:
        step("Reachable", False, str(exc))
        return out
    if not endpoint.insecure_http:
        if endpoint.verify_mode == BmcEndpoint.VERIFY_PIN:
            if not endpoint.pinned_sha256:
                out["needs_pin"] = True
                step("Certificate pinning", False, "Nothing is pinned yet. Check the fingerprint below against the BMC and confirm it. "
                                                   "No login was sent.")
                return out
            if endpoint.pinned_sha256 != out["fingerprint"]:
                out["mismatch"] = True
                step("Certificate pinning", False, "The certificate does not match the pinned fingerprint. No login was sent.")
                return out
            step("Certificate pinning", True, "Matches the pinned fingerprint.")
        elif endpoint.verify_mode == BmcEndpoint.VERIFY_VERIFY and not out["trusted"]:
            step("Certificate verification", False, "The certificate is not trusted by this server's CA bundle. Pin it instead, "
                                                    "or install the issuing CA. No login was sent.")
            return out
    # only a test that would send the login is rate limited (looking at the certificate sends nothing secret)
    wait = _test_wait(endpoint)
    if wait:
        step("Rate limit", False, f"A login test just ran for this endpoint. Wait {wait} seconds before the next one"
                                  + (", so a wrong password cannot lock the BMC account." if endpoint.auth_failures else "."))
        return out
    try:
        user, pw = login_of(endpoint)
    except ValueError as exc:
        step("Login", False, str(exc))
        return out
    out["login_attempted"] = True                    # recorded in the audit trail: it is what the rate limit counts
    try:
        snap = fetch(new_client(endpoint, user, pw, deadline_s=20), endpoint.vendor_override or "", False)
    except redfish.AuthFailed as exc:
        step("Login", False, str(_scrubbed(exc, user, pw)))
        # a rejected login counts like one in the poll, so repeated tests cannot lock the BMC account either
        endpoint.auth_failures += 1
        endpoint.conn_state, endpoint.last_polled_at = BmcEndpoint.CONN_AUTH, timezone.now()
        endpoint.last_error = parsers.text(redfish.scrub(str(exc), user, pw), 300)
        endpoint.save(update_fields=["auth_failures", "conn_state", "last_polled_at", "last_error"])
        return out
    except redfish.RedfishError as exc:
        step("Redfish", False, str(_scrubbed(exc, user, pw)))
        return out
    step("Login", True, "The read-only account is accepted.")
    out["vendor"] = snap.vendor
    out["overview"] = {k: v for k, v in snap.overview.items() if v}
    n_ok = sum(1 for v in snap.sections.values() if v == "ok")
    step("Redfish data", n_ok > 0, f"{n_ok} sections read; {len(snap.comps)} sensors and parts understood.")
    out["ok"] = n_ok > 0
    return out


FP_RE = re.compile(r"^[0-9a-f]{64}$")


def pin_certificate(endpoint, fingerprint):
    """Pin `fingerprint` (what the operator saw) but only if the BMC STILL presents it. Returns (ok, message)."""
    fingerprint = (fingerprint or "").strip().lower().replace(":", "")
    if not FP_RE.fullmatch(fingerprint):
        return False, "That is not a SHA-256 fingerprint."
    try:
        cert = redfish.inspect_certificate(endpoint.host, endpoint.port)
    except redfish.RedfishError as exc:
        return False, str(exc)
    if cert["fingerprint"] != fingerprint:
        return False, "The BMC now presents a different certificate than the one you confirmed. Run Test connection again."
    now = timezone.now()
    endpoint.pinned_sha256, endpoint.pinned_at = fingerprint, now
    endpoint.verify_mode = BmcEndpoint.VERIFY_PIN
    endpoint.consecutive_failures, endpoint.next_poll_at = 0, now
    endpoint.save(update_fields=["pinned_sha256", "pinned_at", "verify_mode", "consecutive_failures", "next_poll_at"])
    return True, "Certificate pinned."


# ----------------------------------------------------------------------------- retention
def retention(now=None):
    """Delete old events and readings, thin readings older than 24 h to one per 10 minutes, expire old requests."""
    now = now or timezone.now()
    out = {}
    out["events"] = HwEvent.objects.filter(ts__lt=now - timedelta(days=EVENT_RETENTION_DAYS)).delete()[0]
    out["readings"] = HwReading.objects.filter(ts__lt=now - timedelta(days=READING_RETENTION_DAYS)).delete()[0]
    out["thinned"] = thin_readings(now)
    out["requests_expired"] = expire_requests(now)
    return out


def thin_readings(now):
    """Keep the first reading of every THIN_MINUTES window (per component) among rows older than FULL_RES_HOURS."""
    cutoff = now - timedelta(hours=FULL_RES_HOURS)
    floor = now - timedelta(days=READING_RETENTION_DAYS)
    drop, last_comp, last_bucket = [], None, None
    rows = (HwReading.objects.filter(ts__lt=cutoff, ts__gte=floor).order_by("component_id", "ts").values_list("pk", "component_id", "ts"))
    width = THIN_MINUTES * 60
    for pk, comp, ts in rows.iterator(chunk_size=5000):
        bucket = int(ts.timestamp()) // width
        if comp == last_comp and bucket == last_bucket:
            drop.append(pk)
        else:
            last_comp, last_bucket = comp, bucket
    n = 0
    for i in range(0, len(drop), 500):
        n += HwReading.objects.filter(pk__in=drop[i:i + 500]).delete()[0]
    return n


# ----------------------------------------------------------------------------- control requests (RECORDS ONLY)
def create_request(endpoint, user, action, reason, now=None):
    """A pending request. Raises ValueError with a message for the form."""
    now = now or timezone.now()
    reason = " ".join((reason or "").split())[:300]
    if action not in ControlRequest.ACTION_LABELS:
        raise ValueError("Choose one of the listed actions.")
    if len(reason) < 5:
        raise ValueError("Say why (at least a few words): the reason is part of the audit trail.")
    dup = ControlRequest.objects.filter(endpoint=endpoint, action=action, state=ControlRequest.PENDING, expires_at__gt=now)
    if dup.exists():
        raise ValueError("The same request is already waiting for a decision.")
    return ControlRequest.objects.create(
        tenant_key=endpoint.tenant_key, endpoint=endpoint, action=action, reason=reason, requested_by=user,
        requested_by_name=user.get_username(), requested_at=now,
        expires_at=now + timedelta(days=ControlRequest.LIFETIME_DAYS))


def decide_request(req, user, approve, note="", now=None):
    """Approve or reject a pending request. Approval is a RECORD: nothing is sent to the BMC. Raises ValueError."""
    now = now or timezone.now()
    problem = ""
    with transaction.atomic():
        locked = ControlRequest.objects.select_for_update().get(pk=req.pk)
        if locked.state == ControlRequest.PENDING and locked.expires_at <= now:
            locked.state = ControlRequest.EXPIRED
            locked.save(update_fields=["state"])
        if locked.state != ControlRequest.PENDING:
            problem = f"This request is already {locked.get_state_display().lower()}."
        else:
            locked.state = ControlRequest.APPROVED if approve else ControlRequest.REJECTED
            locked.decided_by, locked.decided_by_name, locked.decided_at = user, user.get_username(), now
            locked.decision_note = " ".join((note or "").split())[:300]
            locked.save()
    if problem:
        raise ValueError(problem)
    return locked


def expire_requests(now=None):
    now = now or timezone.now()
    return ControlRequest.objects.filter(state=ControlRequest.PENDING, expires_at__lte=now).update(state=ControlRequest.EXPIRED)
