"""Hypervisor pages: the sources admin, the datastore page, the unmatched-VMs page, the control-request list and the two
endpoints behind a VM's device page (request, live JSON).

Every queryset goes through ``services.*_for(user)`` (tenant + site scoping), so another site's source, VM or request is a 404, not
a 403. Every mutation is a POST with CSRF, a permission check on the RECORD's site (``can_on``) and an audit entry. Nothing here
ever sends anything to vCenter except the read-only connection test and sync.
"""
import csv
import logging

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from core import secrets as vault
from core.access import can, can_on, require
from core.audit import audit

from . import providers, services, vmview
from .forms import RotateForm, SourceForm
from .models import VmControlRequest, VmInfo

log = logging.getLogger("linkedeye.hypervisor")


def _csv_safe(value):
    s = "" if value is None else str(value)
    return "'" + s if s and s[0] in "=+-@\t\r" else s


def _csv(filename, header, rows):
    resp = HttpResponse(content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    w = csv.writer(resp)
    w.writerow(header)
    for r in rows:
        w.writerow([_csv_safe(v) for v in r])
    return resp


def _pk(value):
    """A primary key from a request parameter, or None. ASCII digits only (str.isdigit() is also true for "²", which int()
    refuses) and at most 18 of them: a longer string is past CPython's int() digit limit or outside a 64-bit column."""
    return int(value) if isinstance(value, str) and 0 < len(value) <= 18 and value.isascii() and value.isdigit() else None


def _per_page(request, default=20):
    try:
        return max(5, min(100, int(request.GET.get("per", default))))
    except (TypeError, ValueError):
        return default


def _need_admin(request, source):
    if not can_on(request.user, "admin", source.site):
        raise PermissionDenied(f"You need the admin role on “{source.site}” to manage this source.")


def _next(request, default):
    """The ``next`` the form carried, only when it is a path on this site."""
    nxt = request.POST.get("next", "")
    if nxt.startswith("/") and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
        return nxt
    return reverse(default)


def _no_store(resp):
    resp["Cache-Control"] = "no-store"
    return resp


# ----------------------------------------------------------------------------- sources admin
def _source_or_404(request, pk):
    return get_object_or_404(services.sources_for(request.user), pk=pk)


@require("admin")
@require_GET
def sources(request):
    rows = []
    for s in services.sources_for(request.user).order_by("name"):
        code, label = s.state
        rows.append({"s": s, "code": code, "label": label, "can_manage": can_on(request.user, "admin", s.site)})
    return render(request, "hypervisor/sources.html", {"rows": rows, "provider_ok": providers.pyvmomi_available(),
                                                       "vault_ok": vault.available()})


def _reset_trust(source):
    source.pinned_sha256, source.pending_sha256, source.pending_seen_at = "", "", None
    source.consecutive_failures, source.last_error, source.error_kind, source.alert_state = 0, "", "", ""


@require("admin")
@require_http_methods(["GET", "POST"])
def source_new(request):
    form = SourceForm(request.POST or None, user=request.user,
                      initial={"site": "All Sites", "port": 443, "sync_interval_s": 300, "enabled": True})
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                src = form.save(commit=False)
                src.tenant_key = services.tenant()
                src.created_by = request.user
                src.save()
                services.store_login(request, src, form.cleaned_data["username"], form.cleaned_data["password"])
        except (vault.VaultUnavailable, ValueError) as exc:
            form.add_error("password", str(exc))
        else:
            audit(request, "hypervisor.source.create", src, site=src.site, host=src.host, tls_mode=src.tls_mode)
            messages.success(request, f"Source “{src.name}” created. Test the connection to check the account and the certificate.")
            return redirect("hypervisor:source_edit", pk=src.pk)
    return render(request, "hypervisor/source_form.html", {"form": form, "source": None, "provider_ok": providers.pyvmomi_available()})


@require("admin")
@require_http_methods(["GET", "POST"])
def source_edit(request, pk):
    src = _source_or_404(request, pk)
    _need_admin(request, src)
    form = SourceForm(request.POST or None, instance=src, user=request.user)
    login_dropped = False
    if request.method == "POST" and form.is_valid():
        changed = [f for f in form.changed_data if f != "password"]
        try:
            with transaction.atomic():
                src = form.save()
                if "username" in changed or "site" in changed:
                    services.store_login(request, src, form.cleaned_data["username"], "")
                if "username" in changed and services.auth_paused(src):
                    src.consecutive_failures, src.last_error, src.error_kind = 0, "", ""      # a different account: try again
                    src.save(update_fields=["consecutive_failures", "last_error", "error_kind"])
                if {"host", "port"} & set(changed):
                    _reset_trust(src)         # a different server: the old pin and the old state do not apply
                    src.save()
                    if src.credential_id:
                        # the stored login must never follow the address to another server: whoever edits the address would
                        # otherwise be able to send the (write-only) password anywhere. It has to be entered again.
                        services.delete_login(src)
                        src.save(update_fields=["credential_id"])
                        login_dropped = True
        except (vault.VaultUnavailable, ValueError) as exc:
            form.add_error(None, str(exc))
        else:
            audit(request, "hypervisor.source.update", src, changed=changed, login_dropped=login_dropped)
            messages.success(request, f"Source “{src.name}” saved." + (" The pinned certificate was cleared because the address changed." if {"host", "port"} & set(changed) else ""))
            if login_dropped:
                messages.warning(request, "The stored login was removed because the address changed: enter the account and password "
                                          "again under “Rotate password” before the next sync.")
            return redirect("hypervisor:source_edit", pk=src.pk)
    stats = src.vms.aggregate(total=Count("id"), matched=Count("id", filter=Q(match_state="matched")),
                              ambiguous=Count("id", filter=Q(match_state="ambiguous")),
                              unmatched=Count("id", filter=Q(match_state="unmatched")))
    return render(request, "hypervisor/source_form.html", {
        "form": form, "source": src, "state": src.state, "stats": stats, "provider_ok": providers.pyvmomi_available(),
        "rotate": RotateForm(initial={"username": services.username_of(src)}), "username": services.username_of(src),
        "fingerprint": vmview_fp(src.pinned_sha256), "pending_fp": vmview_fp(src.pending_sha256),
        "provider_note": vmview.provider_note(src) if src.use_provider else ""})


def vmview_fp(value):
    from .vsphere import format_fingerprint
    return format_fingerprint(value)


@require("admin")
@require_POST
def source_rotate(request, pk):
    src = _source_or_404(request, pk)
    _need_admin(request, src)
    form = RotateForm(request.POST)
    if not form.is_valid():
        messages.error(request, " ".join(sum((list(v) for v in form.errors.values()), [])) or "Enter the account and the new password.")
        return redirect("hypervisor:source_edit", pk=src.pk)
    try:
        services.store_login(request, src, form.cleaned_data["username"], form.cleaned_data["password"])
    except (vault.VaultUnavailable, ValueError) as exc:
        messages.error(request, str(exc))
        return redirect("hypervisor:source_edit", pk=src.pk)
    src.consecutive_failures, src.last_error, src.error_kind = 0, "", ""
    src.save(update_fields=["consecutive_failures", "last_error", "error_kind"])
    audit(request, "hypervisor.source.rotate_password", src)
    messages.success(request, "The login was replaced. The next sync uses it.")
    return redirect("hypervisor:source_edit", pk=src.pk)


@require("admin")
@require_POST
def source_toggle(request, pk):
    src = _source_or_404(request, pk)
    _need_admin(request, src)
    src.enabled = not src.enabled
    src.save(update_fields=["enabled", "updated_at"])
    audit(request, "hypervisor.source.update", src, enabled=src.enabled)
    messages.success(request, f"“{src.name}” is now {'enabled' if src.enabled else 'disabled: it is not synced'}.")
    return redirect("hypervisor:sources")


@require("admin")
@require_POST
def source_delete(request, pk):
    src = _source_or_404(request, pk)
    _need_admin(request, src)
    name = src.name
    with transaction.atomic():
        services.delete_login(src)
        audit(request, "hypervisor.source.delete", src, site=src.site)
        src.delete()
    messages.success(request, f"Source “{name}” and everything read from it were deleted. Devices and compute records stay.")
    return redirect("hypervisor:sources")


@require("admin")
@require_POST
def source_test(request, pk):
    src = _source_or_404(request, pk)
    _need_admin(request, src)
    result = services.test_connection(src, request)
    audit(request, "hypervisor.source.test", src, result="ok" if result["ok"] or result["needs_pin"] else "failed",
          outcome="ok" if result["ok"] else "needs_pin" if result["needs_pin"] else "failed",
          message=result["message"][:200], fingerprint=result["fingerprint"], login_attempted=result["login_attempted"])
    level = messages.success if result["ok"] else messages.warning if result["needs_pin"] else messages.error
    level(request, "Connection test: " + result["message"] + (f" Certificate SHA-256: {result['fingerprint']}" if result["fingerprint"] else ""))
    return redirect("hypervisor:source_edit", pk=src.pk)


@require("admin")
@require_POST
def source_pin(request, pk):
    src = _source_or_404(request, pk)
    _need_admin(request, src)
    shown = vmview_fp(src.pending_sha256)
    if request.POST.get("fingerprint", "").strip().upper() != shown or not shown:
        messages.error(request, "The fingerprint does not match the one shown. Test the connection again and confirm what is displayed.")
    elif services.confirm_pin(src, request):
        messages.success(request, "Certificate pinned. LinkedEye now refuses any other certificate from this vCenter.")
    return redirect("hypervisor:source_edit", pk=src.pk)


@require("admin")
@require_POST
def source_sync(request, pk):
    src = _source_or_404(request, pk)
    _need_admin(request, src)
    ok, message = services.request_sync(src, request)
    (messages.success if ok else messages.warning)(request, message)
    return redirect("hypervisor:source_edit", pk=src.pk)


# ----------------------------------------------------------------------------- datastores
@require("view")
@require_GET
def datastores(request):
    rows = vmview.datastore_rows(request.user)
    # one query for the VM count of every host (the VMs this user may see), not one per host
    per_host = {(r["source_id"], r["host_moref"]): r["n"] for r in
                services.vms_for(request.user).filter(missing_since__isnull=True).exclude(host_moref="")
                .order_by().values("source_id", "host_moref").annotate(n=Count("id"))}
    host_list = list(services.hosts_for(request.user).order_by("source__name", "name"))
    # a global source's host can be linked to a physical server of any site: name it only when this user may see it
    from network.services import devices_for
    linked = {h.device_id for h in host_list if h.device_id}
    seen = set(devices_for(request.user, request).filter(pk__in=linked).values_list("pk", flat=True)) if linked else set()
    hosts = [{"h": h, "vms": per_host.get((h.source_id, h.moref), 0), "device": h.device if h.device_id in seen else None}
             for h in host_list]
    sources_n = services.sources_for(request.user).count()
    return render(request, "hypervisor/datastores.html", {
        "rows": rows, "hosts": hosts, "sources_n": sources_n, "can_admin": can(request.user, "admin"),
        "example": any(r["example"] for r in rows),
        "live_cfg": {"url": reverse("hypervisor:datastores_live"), "interval": 30, "kind": "datastores"},
        "crit": sum(1 for r in rows if r["tone"] == "crit"), "warn": sum(1 for r in rows if r["tone"] == "warn")})


@require("view")
@require_GET
def datastores_live(request):
    rows = vmview.datastore_rows(request.user)
    srcs = [{"name": s.name, "state": s.state[0], "label": s.state[1], "synced_at": s.last_ok_at.timestamp() if s.last_ok_at else None,
             "error": s.last_error if s.state[0] in ("error", "auth") else "", "interval": s.interval}
            for s in services.sources_for(request.user)]
    return _no_store(JsonResponse({"ok": True, "now": timezone.now().timestamp(), "rows": rows, "sources": srcs}))


@require("view")
@require_GET
def datastores_export(request):
    return _csv("datastores.csv", ["name", "vcenter", "type", "capacity_bytes", "free_bytes", "used_pct", "status"],
                [(d.name, d.source.name, d.ds_type, d.capacity_bytes, d.free_bytes, d.used_pct, d.status[1])
                 for d in services.datastores_for(request.user).order_by("source__name", "name")])


# ----------------------------------------------------------------------------- unmatched VMs
STATE_FILTERS = [("attention", "Needs a decision"), ("unmatched", "Unmatched"), ("ambiguous", "Ambiguous"),
                 ("ignored", "Ignored"), ("matched", "Matched")]


@require("view")
@require_GET
def unmatched(request):
    base = services.vms_for(request.user)
    counts = {k: base.filter(match_state=k).count() for k in ("unmatched", "ambiguous", "ignored", "matched")}
    state = request.GET.get("state", "attention")
    if state not in dict(STATE_FILTERS):
        state = "attention"
    qs = base.filter(match_state__in=["unmatched", "ambiguous"] if state == "attention" else [state])
    q = request.GET.get("q", "").strip()[:100]
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(guest_hostname__icontains=q) | Q(bios_uuid__icontains=q))
    src = request.GET.get("source", "")
    if _pk(src) is not None:
        qs = qs.filter(source_id=_pk(src))
    page = Paginator(qs.order_by("match_state", "name"), _per_page(request)).get_page(request.GET.get("page"))
    from network.services import devices_for
    cand_ids = {c for vm in page for c in (vm.match_candidates or []) if isinstance(c, int)}
    # a global source matches against devices of every site: show only the candidates this user may see
    cands = {d.pk: d for d in devices_for(request.user, request).filter(pk__in=cand_ids)} if cand_ids else {}
    q_dict = request.GET.copy()
    q_dict.pop("page", None)
    for vm in page:
        vm.cand_devices = [cands[c] for c in (vm.match_candidates or []) if c in cands]
        vm.can_resolve = can_on(request.user, "operate", vm.source.site) and not vm.source.is_example
    return render(request, "hypervisor/unmatched.html", {
        "page": page, "query": q_dict, "state": state, "filters": [(k, label, counts.get(k, 0) if k != "attention" else counts["unmatched"] + counts["ambiguous"]) for k, label in STATE_FILTERS],
        "q": q, "counts": counts, "sources": services.sources_for(request.user), "source_id": src, "pager_noun": "VMs"})


def _vm_or_404(request, pk):
    return get_object_or_404(services.vms_for(request.user), pk=pk)


def _need_resolve(request, vm):
    if not can_on(request.user, "operate", vm.source.site):
        raise PermissionDenied("You need the operator role on this vCenter's site.")
    if vm.source.is_example:
        raise PermissionDenied("This is example data.")


@require("operate")
@require_POST
def match_create(request, pk):
    vm = _vm_or_404(request, pk)
    _need_resolve(request, vm)
    try:
        dev = services.create_device_for(vm, request.user, request)
    except services.DeviceCreateError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Device “{dev.name}” created and linked to the vCenter VM.")
    return redirect(_next(request, "hypervisor:unmatched"))


@require("operate")
@require_POST
def match_link(request, pk):
    from network.services import devices_for
    vm = _vm_or_404(request, pk)
    _need_resolve(request, vm)
    dev_id = request.POST.get("device", "")
    dev = (devices_for(request.user, request).filter(pk=_pk(dev_id), device_type="virtual_machine").first()
           if _pk(dev_id) is not None else None)
    if dev is None:
        messages.error(request, "Choose one of the virtual-machine devices you can see.")
    elif not can_on(request.user, "operate", dev.site):
        messages.error(request, "You need the operator role on that device's site.")
    elif VmInfo.objects.filter(device=dev).exclude(pk=vm.pk).exists():
        messages.error(request, f"“{dev.name}” is already linked to another vCenter VM. Unlink that one first.")
    elif vm.device_id:
        messages.error(request, "This VM is already linked. Unlink it first.")
    else:
        services.link_device(vm, dev, request.user, request)
        messages.success(request, f"“{vm.name}” is now linked to the device “{dev.name}”.")
    return redirect(_next(request, "hypervisor:unmatched"))


def _ignore(request, pk, flag):
    vm = _vm_or_404(request, pk)
    _need_resolve(request, vm)
    services.ignore_vm(vm, request, flag)
    messages.success(request, f"“{vm.name}” {'is ignored' if flag else 'will be matched again on the next sync'}.")
    return redirect(_next(request, "hypervisor:unmatched"))


@require("operate")
@require_POST
def match_ignore(request, pk):
    return _ignore(request, pk, True)


@require("operate")
@require_POST
def match_unignore(request, pk):
    return _ignore(request, pk, False)


@require("operate")
@require_POST
def match_unlink(request, pk):
    vm = _vm_or_404(request, pk)
    _need_resolve(request, vm)
    if vm.device_id and not can_on(request.user, "operate", vm.device.site):
        raise PermissionDenied("You need the operator role on the device's site.")
    services.unlink_vm(vm, request)
    messages.success(request, f"“{vm.name}” was unlinked from its device.")
    return redirect(_next(request, "hypervisor:unmatched"))


# ----------------------------------------------------------------------------- control requests
@require("view")
@require_GET
def requests_list(request):
    base = services.requests_for(request.user)
    state = request.GET.get("state", "pending")
    if state not in ("pending", "approved", "rejected", "expired", "all"):
        state = "pending"
    qs = base if state == "all" else base.filter(state=state)
    page = Paginator(qs, _per_page(request)).get_page(request.GET.get("page"))
    for r in page:
        r.can_decide = r.state == "pending" and can_on(request.user, "admin", services.request_site(r))
    q_dict = request.GET.copy()
    q_dict.pop("page", None)
    counts = {k: base.filter(state=k).count() for k in ("pending", "approved", "rejected", "expired")}
    return render(request, "hypervisor/requests.html", {
        "page": page, "query": q_dict, "state": state, "counts": counts, "pager_noun": "requests",
        "states": [(k, label, counts.get(k)) for k, label in
                   (("pending", "Pending"), ("approved", "Approved"), ("rejected", "Rejected"), ("expired", "Expired"), ("all", "All"))]})


@require("view")
@require_GET
def requests_export(request):
    return _csv("vm-control-requests.csv", ["requested_at", "vm", "action", "detail", "reason", "requested_by", "state", "decided_by", "decided_at", "note"],
                [(r.requested_at.isoformat(), r.vm_name, r.action_label, r.detail, r.reason, r.requested_by_name, r.state,
                  r.decided_by_name, r.decided_at.isoformat() if r.decided_at else "", r.decision_note)
                 for r in services.requests_for(request.user)[:5000]])


@require("view")
@require_POST
def request_decide(request, pk):
    req = get_object_or_404(services.requests_for(request.user), pk=pk)
    if not can_on(request.user, "admin", services.request_site(req)):
        raise PermissionDenied("You need the admin role on this VM's site to approve or reject requests.")
    approve = request.POST.get("decision") == "approve"
    if request.POST.get("decision") not in ("approve", "reject"):
        messages.error(request, "Choose approve or reject.")
    elif services.decide(req, request.user, approve, request.POST.get("note", ""), request):
        messages.success(request, ("Approved and recorded. LinkedEye sent nothing to vCenter: the operator carries it out by hand. " +
                                   req.manual_step) if approve else "Rejected.")
    else:
        messages.warning(request, "This request was already decided.")
    return redirect(_next(request, "hypervisor:requests"))


def _device_vm_or_404(request, device_pk):
    from network.services import devices_for
    device = get_object_or_404(devices_for(request.user, request), pk=device_pk)
    vm = get_object_or_404(services.vms_for(request.user), device=device)
    return device, vm


@require("operate")
@require_POST
def vm_request(request, device_pk):
    device, vm = _device_vm_or_404(request, device_pk)
    back = f"{reverse('network:detail', args=[device.pk])}?tab=overview"
    if not can_on(request.user, "operate", device.site):
        raise PermissionDenied("You need the operator role on this VM's site to request an action.")
    if vm.source.is_example:
        messages.warning(request, "This is example data: requests are not recorded for it.")
        return redirect(back)
    action = request.POST.get("action", "")
    if action not in services.MANUAL_STEPS:
        messages.error(request, "Choose an action.")
        return redirect(back)
    detail = request.POST.get("detail", "").strip()
    if action in services.NEEDS_DETAIL and not detail:
        messages.error(request, f"{services.NEEDS_DETAIL[action]} is required for this request.")
        return redirect(back)
    req, created = services.create_request(request.user, vm, action, request.POST.get("reason", ""), detail, request)
    if created:
        messages.success(request, f"Request recorded: {req.action_label} for {vm.name}. An administrator approves or rejects it in LinkedEye; "
                                  "nothing was sent to vCenter.")
    else:
        messages.info(request, f"There is already a pending “{req.action_label}” request for this VM.")
    return redirect(back)


@require("view")
@require_GET
def vm_live(request, device_pk):
    from network.services import devices_for
    device = get_object_or_404(devices_for(request.user, request), pk=device_pk)
    data = vmview.live_payload(request.user, device)
    if data is None:
        return _no_store(JsonResponse({"ok": False, "error": "No vCenter record is linked to this device."}, status=404))
    return _no_store(JsonResponse(data))
