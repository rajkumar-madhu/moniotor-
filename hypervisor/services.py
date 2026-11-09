"""Hypervisor services: scoping, the credential vault, connection test, matching actions and the control-request workflow.

Scoping (decided deliberately, tested): the source holds the site NAME and is in ``SITE_NAME_FIELDS``; everything else belongs
to one source and is scoped through ``source__site``. A VM that is matched to a device must also be visible through the device's
site (``vms_for``), so a user who may see the vCenter but not the VM's site never reads it, and the reverse. A request is visible
to whoever can see its VM.
"""
import logging
from datetime import timedelta

from django.apps import apps
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from core import secrets as vault
from core.access import can_on, is_global_site, scope_by_site, visible_sites
from core.audit import audit

from . import parsers, vsphere
from .models import Datastore, HvHost, VCenterSource, VmControlRequest, VmInfo

log = logging.getLogger("linkedeye.hypervisor")

REQUEST_TTL_DAYS = 7
SYNC_NOW_MIN_GAP = 30
TEST_MIN_GAP = 10
TEST_GAP_AFTER_REJECT = 60       # after the login was rejected (vCenter locks an account after a few quick failures)
#: rejected logins in a row after which syncing stops, so a wrong password never locks the vCenter account
AUTH_PAUSE_AFTER = 3


def tenant():
    return settings.LINKEDEYE_TENANT


def cfg(name, default):
    return getattr(settings, f"HYPERVISOR_{name}", default)


# ----------------------------------------------------------------------------- scoping
def sources_for(user):
    return scope_by_site(VCenterSource.objects.filter(tenant_key=tenant()), user, field="site")


def _sites_q(user, field):
    sites = visible_sites(user)
    if sites is None:
        return None
    return Q(**{f"{field}__in": list(sites) + ["All Sites", ""]})


def vms_for(user):
    """VM records the user may read: the source's site AND (when matched) the device's site."""
    qs = scope_by_site(VmInfo.objects.filter(tenant_key=tenant()).select_related("source", "device"), user, field="source__site")
    q = _sites_q(user, "device__site")
    return qs if q is None else qs.filter(Q(device__isnull=True) | q)


def datastores_for(user):
    return scope_by_site(Datastore.objects.filter(tenant_key=tenant()).select_related("source"), user, field="source__site")


def hosts_for(user):
    return scope_by_site(HvHost.objects.filter(tenant_key=tenant()).select_related("source", "device"), user, field="source__site")


def requests_for(user):
    visible = vms_for(user).values("pk")
    return VmControlRequest.objects.filter(tenant_key=tenant(), vm__in=visible).select_related("vm", "vm__source", "vm__device")


def request_site(req_or_vm):
    vm = getattr(req_or_vm, "vm", req_or_vm)
    return vm.device.site if vm.device_id and vm.device is not None else vm.source.site


# ----------------------------------------------------------------------------- the vault
def credential_row(source):
    if not source.credential_id:
        return None
    Credential = apps.get_model("onboarding", "Credential")
    return Credential.objects.filter(pk=source.credential_id, tenant_key=source.tenant_key).first()


def username_of(source):
    row = credential_row(source)
    return row.username if row is not None else ""


@sensitive_variables()
def store_login(request, source, username, password):
    """Create or rotate the vault row (basic auth). With ``password`` empty only the user name changes. Fails closed."""
    row = credential_row(source)
    if password:
        row = vault.store("basic_auth", username, password, source.site, getattr(request, "user", None),
                          name=f"vCenter login: {source.name}"[:120],
                          notes="Used only by the hypervisor integration (read-only account).", request=request, credential=row)
    elif row is not None and username is not None and username != row.username:
        row.username = username
        row.save(update_fields=["username"])
        audit(request, "hypervisor.source.username", source, site=source.site)
    if row is not None and source.credential_id != row.pk:
        source.credential_id = row.pk
        source.save(update_fields=["credential_id"])
    if row is not None and row.site != source.site:
        type(row).objects.filter(pk=row.pk).update(site=source.site)
    return row


@sensitive_variables()
def read_login(source, request=None, reason="vCenter sync"):
    """(username, password) from the vault. Raises vsphere.VSphereError (kind "auth") with a readable, secret-free message."""
    row = credential_row(source)
    if row is None:
        raise vsphere.VSphereError("No login is stored for this source. Edit the source and enter the read-only account.", "auth")
    try:
        if request is not None:
            with vault.use(row, request, reason=reason) as pw:
                return row.username, pw
        return row.username, vault.decrypt(row.secret)
    except (vault.VaultUnavailable, vault.VaultError) as exc:
        raise vsphere.VSphereError(str(exc), "auth") from None


def delete_login(source):
    row = credential_row(source)
    if row is not None:
        row.delete()
    source.credential_id = None


# ----------------------------------------------------------------------------- connection test
def auth_paused(source):
    """True while the account was rejected several times in a row: syncing (and testing) wait for a new password."""
    return (source.error_kind == "auth" and source.consecutive_failures >= AUTH_PAUSE_AFTER
            and (source.last_error or "").startswith(vsphere.REJECTED_PREFIX))


def _test_wait(source):
    """Seconds to wait before the next connection test of this source (0 = go). The cache entry is the atomic gate inside one
    process; the audit trail covers the other worker processes."""
    from django.core.cache import cache

    from core.models import AuditEvent
    gap = max(TEST_MIN_GAP, TEST_GAP_AFTER_REJECT) if source.error_kind == "auth" and source.consecutive_failures and TEST_MIN_GAP else TEST_MIN_GAP
    if not cache.add(f"hypervisor:test:{source.pk}", 1, gap):
        return gap
    last = (AuditEvent.objects.filter(action="hypervisor.source.test", object_type=source._meta.label_lower, object_id=str(source.pk),
                                      details__login_attempted=True)
            .order_by("-occurred_at").values_list("occurred_at", flat=True).first())
    if last is not None:
        since = (timezone.now() - last).total_seconds()
        if since < gap:
            return max(1, int(gap - since) + 1)
    return 0


@sensitive_variables()
def test_connection(source, request=None):
    """Reach the vCenter, show the certificate it presents and (unless a pin is still to be confirmed) log in read-only.

    -> {"ok", "message", "fingerprint", "needs_pin", "version", "counts"}. In ``pin`` mode with nothing pinned yet NO login is
    attempted: the fingerprint is recorded as pending and the operator confirms it first (trust on first use, as for SSH)."""
    out = {"ok": False, "message": "", "fingerprint": "", "needs_pin": False, "version": "", "counts": {}, "login_attempted": False}
    try:
        fp = vsphere.fetch_fingerprint(source.host, source.port)
    except vsphere.VSphereError as exc:
        out["message"] = str(exc)
        return out
    out["fingerprint"] = vsphere.format_fingerprint(fp)
    if fp != source.pending_sha256 and fp != source.pinned_sha256:
        source.pending_sha256, source.pending_seen_at = fp, timezone.now()
        source.save(update_fields=["pending_sha256", "pending_seen_at"])
    if source.tls_mode == "pin" and not source.pinned_sha256:
        out["needs_pin"] = True
        out["message"] = ("The server presented the certificate below. Compare it with the one you expect and confirm it to pin it. "
                          "No login was attempted.")
        return out
    # only a test that would send the login is rate limited (looking at the certificate sends nothing secret)
    wait = _test_wait(source)
    if wait:
        out["message"] = (f"A login test just ran for this source. Wait {wait} seconds before the next one"
                          + (", so a wrong password cannot lock the account." if source.error_kind == "auth" and source.consecutive_failures else "."))
        return out
    out["login_attempted"] = True                    # recorded in the audit trail: it is what the rate limit counts
    try:
        user, pw = read_login(source, request, reason="connection test")
        with vsphere.VSphereClient(source.host, source.port, user, pw, tls_mode=source.tls_mode,
                                   pinned_sha256=source.pinned_sha256, deadline=20) as client:
            version = client.get("/api/appliance/system/version", optional=True)
            out["version"] = parsers.clean_text((version or {}).get("version") if isinstance(version, dict) else "", 40)
            out["counts"] = {"vms": len(client.get_list("/api/vcenter/vm")), "hosts": len(client.get_list("/api/vcenter/host")),
                             "datastores": len(client.get_list("/api/vcenter/datastore"))}
        out["ok"] = True
        if source.consecutive_failures or source.error_kind:
            source.consecutive_failures, source.last_error, source.error_kind = 0, "", ""
            source.save(update_fields=["consecutive_failures", "last_error", "error_kind"])
        out["message"] = (f"Connected{' to vCenter ' + out['version'] if out['version'] else ''}: "
                          f"{out['counts']['vms']} VMs, {out['counts']['hosts']} hosts, {out['counts']['datastores']} datastores.")
    except vsphere.VSphereError as exc:
        out["message"] = str(exc)
        if exc.kind == "auth" and exc.status in (401, 403):
            # a rejected login counts like one in the sync, so repeated tests cannot lock the account either
            source.consecutive_failures += 1
            source.last_error, source.error_kind = parsers.clean_text(str(exc), 300), "auth"
            source.save(update_fields=["consecutive_failures", "last_error", "error_kind"])
        if exc.kind == "tls" and source.tls_mode == "verify":
            out["message"] += " Use “Pin the certificate fingerprint” to trust this certificate explicitly."
    except Exception as exc:  # noqa: BLE001
        log.warning("hypervisor connection test failed: %s", type(exc).__name__)
        out["message"] = f"Unexpected error ({type(exc).__name__})."
    return out


def confirm_pin(source, request):
    """The operator confirms the fingerprint the last test showed: it becomes the pinned one."""
    if not source.pending_sha256:
        return False
    old = source.pinned_sha256
    source.pinned_sha256, source.pending_sha256, source.pending_seen_at = source.pending_sha256, "", None
    source.consecutive_failures, source.last_error, source.error_kind = 0, "", ""
    source.save(update_fields=["pinned_sha256", "pending_sha256", "pending_seen_at", "consecutive_failures", "last_error", "error_kind"])
    audit(request, "hypervisor.source.pin", source, fingerprint=vsphere.format_fingerprint(source.pinned_sha256),
          replaced=bool(old), changed=bool(old and old != source.pinned_sha256))
    return True


def request_sync(source, request):
    """Queue an immediate sync (the job picks it up within a minute). Rate limited. -> (ok, message)."""
    now = timezone.now()
    last = source.sync_requested_at
    if last and (now - last).total_seconds() < SYNC_NOW_MIN_GAP:
        return False, f"A sync was requested {int((now - last).total_seconds())} s ago. Wait {SYNC_NOW_MIN_GAP} seconds between requests."
    if not source.enabled:
        return False, "The source is disabled."
    source.sync_requested_at = now
    source.save(update_fields=["sync_requested_at"])
    audit(request, "hypervisor.source.sync_now", source)
    return True, "Sync queued: the worker starts it within a minute."


# ----------------------------------------------------------------------------- matching actions
def link_device(vm, device, user, request, how="linked by"):
    """Link a VM record to a device (operator action, audited). Never replaces a link silently: the caller checks first."""
    vm.device = device
    vm.match_state = VmInfo.MATCHED
    vm.match_reason = f"{how} {user.get_username()}"[:300]
    vm.match_candidates = []
    vm.matched_by = user.get_username()[:150]
    vm.matched_at = timezone.now()
    vm.auto_match_off = False
    vm.save(update_fields=["device", "match_state", "match_reason", "match_candidates", "matched_by", "matched_at", "auto_match_off"])
    audit(request, "hypervisor.match.link", vm, device=device.name, device_id=device.pk, source=vm.source.name)


class DeviceCreateError(Exception):
    pass


def create_device_for(vm, user, request):
    """One-click: create the virtual_machine device for an unmatched VM and link it. Raises DeviceCreateError."""
    from django.core.exceptions import ValidationError

    from network.models import Device, DeviceType
    if vm.device_id:
        raise DeviceCreateError("This VM is already linked to a device.")
    name = vm.name[:150]
    if Device.objects.filter(tenant_key=vm.tenant_key, name=name).exists():
        raise DeviceCreateError(f"A device named “{name}” already exists. Link this VM to it instead of creating a second one.")
    host = (vm.guest_ips or [""])[0] or vm.guest_hostname or ""
    site = "" if is_global_site(vm.source.site) else vm.source.site
    if not can_on(user, "operate", site):
        raise DeviceCreateError("You need the operator role on that site to create the device.")
    dev = Device(tenant_key=vm.tenant_key, name=name, device_type=DeviceType.VIRTUAL_MACHINE, vendor="VMware",
                 model="Virtual Machine", firmware=(vm.guest_full_name or "")[:150], mgmt_ip=host or name, site=site,
                 check_port=3389 if vm.guest_family == "WINDOWS" else 22, tags="vcenter",
                 description=f"Created from vCenter source “{vm.source.name}”.")
    try:
        dev.full_clean(exclude=["image"])
    except ValidationError as exc:
        raise DeviceCreateError("The device could not be created: " + "; ".join(sum(exc.message_dict.values(), []))[:200]) from None
    with transaction.atomic():
        dev.save()
        audit(request, "hypervisor.match.create_device", dev, vm=vm.name, source=vm.source.name, site=site)
        link_device(vm, dev, user, request, how="device created by")
    return dev


def ignore_vm(vm, request, ignore=True):
    vm.match_state = VmInfo.IGNORED if ignore else VmInfo.UNMATCHED
    vm.match_reason = "ignored by an operator" if ignore else "no longer ignored; matched again on the next sync"
    if not ignore:
        vm.auto_match_off = False
    vm.save(update_fields=["match_state", "match_reason", "auto_match_off"])
    audit(request, "hypervisor.match.ignore" if ignore else "hypervisor.match.unignore", vm, source=vm.source.name)


def unlink_vm(vm, request):
    dev = vm.device
    vm.device = None
    vm.match_state, vm.matched_by = VmInfo.UNMATCHED, ""
    vm.match_reason = "unlinked by an operator: not matched automatically again; link it or create a device"
    vm.match_candidates = []
    vm.auto_match_off = True              # otherwise the next sync would find the same device again and undo the decision
    vm.save(update_fields=["device", "match_state", "match_reason", "matched_by", "match_candidates", "auto_match_off"])
    audit(request, "hypervisor.match.unlink", vm, device=dev.name if dev else "", source=vm.source.name)


# ----------------------------------------------------------------------------- control requests (RECORDS ONLY)
MANUAL_STEPS = {
    "power_on": "In the vSphere Client, right-click “{vm}” and choose Power > Power On (or run Start-VM in PowerCLI).",
    "power_off": "In the vSphere Client, right-click “{vm}” and choose Power > Shut Down Guest OS (or Power Off if the guest does not respond).",
    "restart": "In the vSphere Client, right-click “{vm}” and choose Power > Restart Guest OS.",
    "suspend": "In the vSphere Client, right-click “{vm}” and choose Power > Suspend.",
    "reset": "In the vSphere Client, right-click “{vm}” and choose Power > Reset. This is a hard reset: the guest is not shut down first.",
    "migrate": "In the vSphere Client, right-click “{vm}” and choose Migrate. Pick the target host or datastore stated in the request.",
    "snapshot": "In the vSphere Client, right-click “{vm}” and choose Snapshots > Take Snapshot, using the name stated in the request.",
    "clone": "In the vSphere Client, right-click “{vm}” and choose Clone > Clone to Virtual Machine.",
    "edit_settings": "In the vSphere Client, right-click “{vm}” and choose Edit Settings. Apply the change described in the request.",
    "move_to_host": "In the vSphere Client, right-click “{vm}” and choose Migrate > Change compute resource only, then pick the host named in the request.",
    "download_logs": "In the vSphere Client, export the VM's logs (Monitor > Tasks and Events, or the vmware.log in the VM's datastore folder).",
}
NEEDS_DETAIL = {"migrate": "Target host or datastore", "move_to_host": "Target host", "snapshot": "Snapshot name",
                "edit_settings": "What to change", "clone": "Name for the clone"}
ACTION_ORDER = [a for a, _ in VmControlRequest.ACTIONS]


def create_request(user, vm, action, reason="", detail="", request=None):
    """Record a request. -> (VmControlRequest, created). An identical open request is returned instead of a duplicate."""
    if action not in MANUAL_STEPS:
        raise ValueError("Unknown action.")
    existing = VmControlRequest.objects.filter(vm=vm, action=action, state=VmControlRequest.PENDING).first()
    if existing is not None:
        return existing, False
    req = VmControlRequest.objects.create(
        tenant_key=vm.tenant_key, vm=vm, vm_name=vm.name[:255], action=action, reason=parsers.clean_text(reason, 300),
        detail=parsers.clean_text(detail, 200), requested_by=user, requested_by_name=user.get_username()[:150])
    audit(request, "hypervisor.request.create", req, vm=vm.name, requested=action, source=vm.source.name)
    return req, True


def decide(req, user, approve, note="", request=None):
    """Approve or reject a pending request. Approving RECORDS the decision and stores the manual step; nothing is sent."""
    now = timezone.now()
    if req.state != VmControlRequest.PENDING:
        return False
    if req.requested_at < now - timedelta(days=cfg("REQUEST_TTL_DAYS", REQUEST_TTL_DAYS)):
        VmControlRequest.objects.filter(pk=req.pk, state=VmControlRequest.PENDING).update(state=VmControlRequest.EXPIRED)
        return False                                    # too old to approve, whether or not the retention job has run yet
    note = parsers.clean_text(note, 300)
    manual = MANUAL_STEPS[req.action].format(vm=req.vm_name) if approve else ""
    # one atomic compare-and-set: two administrators (or a replayed POST) can never both decide the same request
    won = VmControlRequest.objects.filter(pk=req.pk, state=VmControlRequest.PENDING).update(
        state=VmControlRequest.APPROVED if approve else VmControlRequest.REJECTED, decided_by=user,
        decided_by_name=user.get_username()[:150], decided_at=now, decision_note=note, manual_step=manual)
    if not won:
        return False
    req.refresh_from_db()
    audit(request, "hypervisor.request.approve" if approve else "hypervisor.request.reject", req, vm=req.vm_name,
          requested=req.action, note=req.decision_note)
    return True


def expire_requests(now=None):
    now = now or timezone.now()
    return VmControlRequest.objects.filter(
        tenant_key=tenant(), state=VmControlRequest.PENDING,
        requested_at__lt=now - timedelta(days=cfg("REQUEST_TTL_DAYS", REQUEST_TTL_DAYS))).update(state=VmControlRequest.EXPIRED)
