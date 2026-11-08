"""Hardware pages: the endpoints admin, test connection / certificate pinning, the live JSON of the Hardware tab, the inventory
and event pages, the CSV report and the control-request workflow.

Scoping: every endpoint is looked up through ``services.scoped_endpoint`` (tenant + the device's site through
``core.access`` + the active client), so another site's server is a 404, never a 403. Changing anything needs the role on the
DEVICE's site (``can_on``), not just somewhere.

The control requests are RECORDS. Nothing in this module can reach a BMC with anything but the read-only Redfish client."""
import logging

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from core import secrets as vault
from core.access import can, can_on, require
from core.audit import audit

from . import forms, services, snapshot
from .models import BmcEndpoint, ControlRequest, HwComponent, HwEvent

log = logging.getLogger("linkedeye.hardware")


def _int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _per(request, default=20):
    n = _int(request.GET.get("per"), default)
    return max(5, min(100, n))


def _query(request, *drop):
    q = request.GET.copy()
    for k in ("page",) + drop:
        q.pop(k, None)
    return q


def _scoped_devices(request):
    from network.services import devices_for
    return devices_for(request.user, request)


def _endpoint_for_device(request, device_pk):
    """The endpoint of a physical server the user can see (404 otherwise: also for a server without an endpoint)."""
    endpoints = services.endpoints_for(request.user).filter(device__in=_scoped_devices(request).values("pk"), device_id=device_pk)
    return get_object_or_404(endpoints)


def _endpoint(request, pk):
    endpoints = services.endpoints_for(request.user).filter(device__in=_scoped_devices(request).values("pk"))
    return get_object_or_404(endpoints, pk=pk)


def _need(request, perm, endpoint):
    if not can_on(request.user, perm, endpoint.device.site):
        raise PermissionDenied(f"You need the {perm} role on “{endpoint.device.site or 'All Sites'}” for this.")


def _tab_url(endpoint):
    return reverse("network:detail", args=[endpoint.device_id]) + "?tab=hardware"


def _perms(user, endpoint):
    return {"operate": can_on(user, "operate", endpoint.device.site), "admin": can_on(user, "admin", endpoint.device.site)}


def _json(data, status=200):
    resp = JsonResponse(data, status=status)
    resp["Cache-Control"] = "no-store"
    return resp


# ----------------------------------------------------------------------------- the Hardware tab's data
@require("view")
@require_GET
def live(request, device_pk):
    ep = _endpoint_for_device(request, device_pk)
    return _json(snapshot.build(ep, request.GET.get("range"), _perms(request.user, ep)))


@require("operate")
@require_POST
def refresh(request, device_pk):
    ep = _endpoint_for_device(request, device_pk)
    _need(request, "operate", ep)
    ok, message = services.request_refresh(ep)
    if ok:
        audit(request, "hardware.refresh", ep, device=ep.device.name)
    return _json({"ok": ok, "message": message}, status=200 if ok else 429 if "Try again" in message else 409)


@require("admin")
@require_POST
def vendor(request, device_pk):
    """The Dell | HPE switch: override the detected vendor (admin), or '' for Auto."""
    ep = _endpoint_for_device(request, device_pk)
    _need(request, "admin", ep)
    choice = request.POST.get("vendor", "")
    if choice not in ("", "dell", "hpe", "generic"):
        return _json({"ok": False, "message": "Unknown vendor."}, status=400)
    old = ep.vendor_override
    ep.vendor_override = choice
    ep.inventory_at = None                                  # the other vendor's parser reads the inventory afresh
    ep.save(update_fields=["vendor_override", "inventory_at", "updated_at"])
    audit(request, "hardware.vendor", ep, device=ep.device.name, old=old or "auto", new=choice or "auto")
    if request.headers.get("Accept", "").startswith("application/json") or request.headers.get("X-Requested-With"):
        return _json({"ok": True})
    return redirect(_tab_url(ep))


@require("view")
@require_GET
def export(request, device_pk):
    ep = _endpoint_for_device(request, device_pk)
    resp = HttpResponse(content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="{slugify("hardware-" + ep.device.name) or "hardware"}.csv"'
    resp["Cache-Control"] = "no-store"
    snapshot.write_csv(resp, ep)
    audit(request, "hardware.export", ep, device=ep.device.name)
    return resp


# ----------------------------------------------------------------------------- inventory and events
INVENTORY_LAYOUT = [
    ("cpu", "Processors", ["Socket", "Model", "Cores / threads", "Max speed", "Health"],
     lambda c, a: [c.name, a.get("model"), _pair(a.get("cores"), a.get("threads")), _mhz(a.get("max_mhz"))]),
    ("dimm", "Memory modules", ["Slot", "Capacity", "Type", "Speed", "Part number", "Serial", "Health"],
     lambda c, a: [c.name, _gib(a.get("capacity_gib")), a.get("type"), _mhz(a.get("speed_mhz")), a.get("part"), a.get("serial")]),
    ("drive", "Drives", ["Drive", "Model", "Serial", "Capacity", "Media", "Protocol", "Firmware", "Life left", "Health"],
     lambda c, a: [c.name, a.get("model"), a.get("serial"), _gb(a.get("capacity_gb")), a.get("media"), a.get("protocol"),
                   a.get("firmware"), _pct(a.get("life_left_pct"))]),
    ("storage", "Storage controllers", ["Controller", "Model", "Firmware", "Drives", "Health"],
     lambda c, a: [c.name, a.get("model"), a.get("firmware"), a.get("drives")]),
    ("nic", "Network interfaces", ["Interface", "MAC address", "Speed", "Link", "Health"],
     lambda c, a: [c.name, a.get("mac"), (f"{int(a['speed_mbps'])} Mb/s" if a.get("speed_mbps") else None), a.get("link")]),
    ("psu", "Power supplies", ["PSU", "Model", "Serial", "Firmware", "Capacity", "Input", "Health"],
     lambda c, a: [c.name, a.get("model"), a.get("serial"), a.get("firmware"),
                   (f"{int(a['capacity_w'])} W" if a.get("capacity_w") else None),
                   (f"{int(a['input_v'])} V" if a.get("input_v") else None)]),
    ("battery", "Batteries", ["Battery", "Serial", "Firmware", "Health"], lambda c, a: [c.name, a.get("serial"), a.get("firmware")]),
]


def _pair(a, b):
    return f"{int(a)} / {int(b)}" if a and b else (str(int(a)) if a else None)


def _mhz(v):
    return f"{int(v)} MHz" if v else None


def _gib(v):
    return f"{v:g} GiB" if v else None


def _gb(v):
    return f"{v:g} GB" if v else None


def _pct(v):
    return f"{v:g} %" if v is not None else None


def inventory_sections(endpoint):
    by = {}
    for c in HwComponent.objects.filter(endpoint=endpoint).order_by("kind", "position", "name"):
        by.setdefault(c.kind, []).append(c)
    out = []
    for kind, title, cols, fn in INVENTORY_LAYOUT:
        rows = []
        for c in by.get(kind, []):
            cells = [("" if v is None else str(v)) for v in fn(c, c.attrs or {})]
            rows.append({"cells": cells, "status": c.status, "health": c.health or c.get_status_display()})
        out.append({"kind": kind, "title": title, "columns": cols, "rows": rows,
                    "reported": bool(rows)})
    return out


@require("view")
@require_GET
def inventory(request, device_pk):
    ep = _endpoint_for_device(request, device_pk)
    return render(request, "hardware/inventory.html", {
        "endpoint": ep, "device": ep.device, "sections": inventory_sections(ep), "tab_url": _tab_url(ep),
        "inventory_at": ep.inventory_at, "example": ep.is_example})


@require("view")
@require_GET
def events(request, device_pk):
    ep = _endpoint_for_device(request, device_pk)
    qs = HwEvent.objects.filter(endpoint=ep)
    sev = request.GET.get("severity", "")
    if sev in ("info", "warning", "critical"):
        qs = qs.filter(severity=sev)
    q = request.GET.get("q", "").strip()[:80]
    if q:
        qs = qs.filter(message__icontains=q) | qs.filter(sensor__icontains=q)
    page = Paginator(qs.order_by("-ts", "-id"), _per(request)).get_page(request.GET.get("page"))
    return render(request, "hardware/events.html", {
        "endpoint": ep, "device": ep.device, "page": page, "query": _query(request), "severity": sev, "q": q,
        "tab_url": _tab_url(ep), "pager_noun": "log entries"})


# ----------------------------------------------------------------------------- endpoints admin
@require("view")
@require_GET
def endpoints(request):
    qs = services.endpoints_for(request.user).filter(device__in=_scoped_devices(request).values("pk"))
    q = request.GET.get("q", "").strip()[:80]
    if q:
        qs = qs.filter(device__name__icontains=q) | qs.filter(host__icontains=q)
    health = request.GET.get("health", "")
    if health in ("healthy", "warning", "critical", "unknown"):
        qs = qs.filter(health=health)
    page = Paginator(qs.order_by("device__name"), _per(request)).get_page(request.GET.get("page"))
    now = timezone.now()
    rows = []
    for ep in page.object_list:
        skey, slabel, stone = snapshot.connection_state(ep, now)
        rows.append({"ep": ep, "state": slabel, "tone": stone, "can_manage": can_on(request.user, "admin", ep.device.site),
                     "health_tone": snapshot.TONE.get("ok" if ep.health == "healthy" else ep.health, "mute")})
    free = forms.manageable_devices(request.user) if can(request.user, "admin") else []
    return render(request, "hardware/endpoints.html", {
        "rows": rows, "page": page, "query": _query(request), "q": q, "health": health, "free_devices": free[:50],
        "can_admin": can(request.user, "admin"), "pager_noun": "BMC endpoints"})


def _save_credentials(request, form, ep):
    """Store / rotate the vault row. Raises VaultUnavailable / ValueError for the form."""
    state = services.store_login(request, ep, form.cleaned_data.get("username") or "", form.cleaned_data.get("password") or "")
    if state:
        ep.auth_failures, ep.consecutive_failures, ep.next_poll_at = 0, 0, timezone.now()
        ep.save(update_fields=["auth_failures", "consecutive_failures", "next_poll_at"])
    return state


@require("admin")
@require_http_methods(["GET", "POST"])
def endpoint_new(request):
    device = None
    pk = _int(request.GET.get("device"))
    if pk:
        device = next((d for d in forms.manageable_devices(request.user) if d.pk == pk), None)
    form = forms.EndpointForm(request.POST or None, user=request.user, device=device,
                              initial={"port": 443, "poll_interval_s": 60, "verify_mode": "pin", "enabled": True})
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                ep = form.save(commit=False)
                ep.tenant_key = services.tenant()
                ep.device = form.cleaned_data["device"]
                ep.created_by = request.user
                ep.save()
                state = _save_credentials(request, form, ep)
        except (vault.VaultUnavailable, ValueError) as exc:
            form.add_error("password", str(exc))
        else:
            audit(request, "hardware.endpoint.create", ep, device=ep.device.name, host=ep.host, verify=ep.verify_mode, credential=state)
            messages.success(request, "BMC endpoint saved. Run Test connection to check the login and confirm the certificate.")
            return redirect("hardware:endpoint_edit", pk=ep.pk)
    return render(request, "hardware/endpoint_form.html", {"form": form, "endpoint": None})


@require("admin")
@require_http_methods(["GET", "POST"])
def endpoint_edit(request, pk):
    ep = _endpoint(request, pk)
    _need(request, "admin", ep)
    where = (ep.host, ep.port)                              # the form edits the instance while it validates
    form = forms.EndpointForm(request.POST or None, instance=ep, user=request.user)
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                changed = [f for f in form.changed_data if f not in ("password", "confirm_unsafe")]
                ep = form.save()
                if (ep.host, ep.port) != where and ep.pinned_sha256:
                    ep.pinned_sha256, ep.pinned_at = "", None   # another address: a fingerprint pinned for the old one means nothing
                    ep.save(update_fields=["pinned_sha256", "pinned_at"])
                state = _save_credentials(request, form, ep)
        except (vault.VaultUnavailable, ValueError) as exc:
            form.add_error("password", str(exc))
        else:
            audit(request, "hardware.endpoint.update", ep, device=ep.device.name, changed=changed, credential=state or "unchanged")
            if state:
                audit(request, "hardware.endpoint.credential", ep, device=ep.device.name, change=state)
            messages.success(request, "BMC endpoint saved.")
            return redirect("hardware:endpoint_edit", pk=ep.pk)
    return render(request, "hardware/endpoint_form.html", _edit_ctx(form, ep))


def _edit_ctx(form, ep, test=None):
    return {"form": form, "endpoint": ep, "test": test, "device": ep.device if ep else None,
            "state": snapshot.connection_state(ep, timezone.now()) if ep else None,
            "fingerprint": ep.pinned_sha256 if ep else "", "now": timezone.now()}


@require("admin")
@require_POST
def endpoint_test(request, pk):
    ep = _endpoint(request, pk)
    _need(request, "admin", ep)
    result = services.test_connection(ep)
    audit(request, "hardware.endpoint.test", ep, device=ep.device.name, ok=result["ok"], login_attempted=result["login_attempted"],
          steps=[(s["label"], s["ok"]) for s in result["steps"]])
    if result["ok"] and (ep.auth_failures or ep.conn_state != BmcEndpoint.CONN_OK):
        ep.auth_failures, ep.consecutive_failures, ep.next_poll_at = 0, 0, timezone.now()
        ep.save(update_fields=["auth_failures", "consecutive_failures", "next_poll_at"])
    resp = render(request, "hardware/endpoint_form.html", _edit_ctx(forms.EndpointForm(instance=ep, user=request.user), ep, test=result))
    resp["Cache-Control"] = "no-store"
    return resp


@require("admin")
@require_POST
def endpoint_pin(request, pk):
    ep = _endpoint(request, pk)
    _need(request, "admin", ep)
    ok, message = services.pin_certificate(ep, request.POST.get("fingerprint", ""))
    audit(request, "hardware.endpoint.pin", ep, device=ep.device.name, ok=ok,
          fingerprint=ep.pinned_sha256 if ok else (request.POST.get("fingerprint", "")[:64]))
    (messages.success if ok else messages.error)(request, message + (" Run Test connection to check the login." if ok else ""))
    return redirect("hardware:endpoint_edit", pk=ep.pk)


@require("admin")
@require_POST
def endpoint_toggle(request, pk):
    ep = _endpoint(request, pk)
    _need(request, "admin", ep)
    ep.enabled = not ep.enabled
    ep.save(update_fields=["enabled", "updated_at"])
    audit(request, "hardware.endpoint.update", ep, device=ep.device.name, enabled=ep.enabled)
    messages.success(request, f"Polling of {ep.device.name} is now {'on' if ep.enabled else 'off'}.")
    return redirect("hardware:endpoints")


@require("admin")
@require_POST
def endpoint_delete(request, pk):
    from django.apps import apps
    ep = _endpoint(request, pk)
    _need(request, "admin", ep)
    name, cred = ep.device.name, ep.credential_id
    with transaction.atomic():
        if cred:
            apps.get_model("onboarding", "Credential").objects.filter(pk=cred, tenant_key=ep.tenant_key).delete()
        audit(request, "hardware.endpoint.delete", ep, device=name)
        ep.delete()
    messages.success(request, f"The BMC endpoint of {name} and its readings were deleted. The server device is unchanged.")
    return redirect("hardware:endpoints")


@require("admin")
@require_http_methods(["GET", "POST"])
def alerts(request, device_pk):
    ep = _endpoint_for_device(request, device_pk)
    _need(request, "admin", ep)
    form = forms.AlertsForm(request.POST or None, instance=ep)
    if request.method == "POST" and form.is_valid():
        changed = list(form.changed_data)
        form.save()
        audit(request, "hardware.alerts", ep, device=ep.device.name, changed=changed)
        messages.success(request, "Alert settings saved. They apply from the next poll.")
        return redirect(_tab_url(ep))
    return render(request, "hardware/alerts.html", {"form": form, "endpoint": ep, "device": ep.device, "tab_url": _tab_url(ep)})


# ----------------------------------------------------------------------------- control requests (records only)
@require("operate")
@require_POST
def request_new(request, device_pk):
    ep = _endpoint_for_device(request, device_pk)
    _need(request, "operate", ep)
    try:
        req = services.create_request(ep, request.user, request.POST.get("action", ""), request.POST.get("reason", ""))
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        audit(request, "hardware.request.create", req, device=ep.device.name, what=req.action, reason=req.reason)
        messages.success(request, f"Request recorded: {req.action_label} for {ep.device.name}. An administrator decides; "
                                  "LinkedEye never sends it to the BMC.")
    return redirect(_tab_url(ep))


@require("view")
@require_GET
def requests_list(request):
    qs = services.requests_for(request.user).filter(endpoint__device__in=_scoped_devices(request).values("pk"))
    services.expire_requests()
    state = request.GET.get("state", "pending")
    if state in ("pending", "approved", "rejected", "expired"):
        qs = qs.filter(state=state)
    else:
        state = "all"
    page = Paginator(qs.order_by("-requested_at", "-id"), _per(request)).get_page(request.GET.get("page"))
    rows = [{"r": r, "steps": services.MANUAL_STEPS.get(r.action, ""),
             "can_decide": r.state == ControlRequest.PENDING and can_on(request.user, "admin", r.endpoint.device.site)}
            for r in page.object_list]
    return render(request, "hardware/requests.html", {"rows": rows, "page": page, "query": _query(request, "state"), "state": state,
                                                      "pager_noun": "requests"})


@require("admin")
@require_POST
def request_decide(request, pk):
    qs = services.requests_for(request.user).filter(endpoint__device__in=_scoped_devices(request).values("pk"))
    req = get_object_or_404(qs, pk=pk)
    _need(request, "admin", req.endpoint)
    approve = request.POST.get("decision") == "approve"
    if request.POST.get("decision") not in ("approve", "reject"):
        messages.error(request, "Choose approve or reject.")
        return redirect("hardware:requests")
    try:
        done = services.decide_request(req, request.user, approve, request.POST.get("note", ""))
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        audit(request, "hardware.request.approve" if approve else "hardware.request.reject", done,
              device=req.endpoint.device.name, what=done.action, note=done.decision_note)
        if approve:
            messages.success(request, "Approval recorded. LinkedEye does not carry it out: " + services.MANUAL_STEPS.get(done.action, ""))
        else:
            messages.success(request, "Request rejected.")
    return redirect(request.POST.get("next") if (request.POST.get("next") or "").startswith("/hardware/") else "hardware:requests")
