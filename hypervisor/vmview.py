"""Context for the hypervisor tabs of a VM's device page, the live JSON they patch from, and the datastore page payload.

Everything here is read from the database (what the last sync stored). Nothing is invented: a value vCenter did not report is
``None`` and the template says "Not reported" (and why, where there is a reason: the REST API has no snapshots or events, the
optional pyvmomi provider is off or missing...).
"""
import hashlib

from django.urls import reverse
from django.utils import timezone

from . import providers, services
from .models import SNAPSHOT_REVIEW_DAYS, Datastore, VmControlRequest

TONE = {"on": "ok", "off": "crit", "suspended": "warn", "unknown": "mute"}
TOOLS = {"RUNNING": ("Running", "ok"), "NOT_RUNNING": ("Not running", "crit"), "EXECUTING_SCRIPTS": ("Running scripts", "ok")}
ACTIONS = [  # (code, label, icon, tone); console is a link, not a request
    ("power_on", "Power on", "play", "ok"), ("power_off", "Power off", "x-circle", "crit"), ("restart", "Restart", "refresh", "warn"),
    ("suspend", "Suspend", "pause", "ok"), ("reset", "Reset", "zap", "crit"), ("migrate", "Migrate", "share", ""),
    ("console", "Open console", "terminal", ""), ("snapshot", "Take snapshot", "file", ""), ("clone", "Clone VM", "layers", ""),
    ("edit_settings", "Edit settings", "settings", ""), ("move_to_host", "Move to host", "server", ""),
    ("download_logs", "Download logs", "download", ""),
]


def bytes_h(n):
    if n is None:
        return None
    n = float(n)
    for unit, div in (("TB", 1024 ** 4), ("GB", 1024 ** 3), ("MB", 1024 ** 2)):
        if n >= div:
            v = n / div
            return f"{v:.1f} {unit}" if v < 100 and unit != "MB" else f"{v:.0f} {unit}"
    return f"{n / 1024:.0f} KB"


def when(dt):
    return timezone.localtime(dt).strftime("%d %b %Y %H:%M") if dt else None


def short_when(dt):
    return timezone.localtime(dt).strftime("%d %b %H:%M") if dt else ""


def age(dt, now=None):
    if not dt:
        return "never"
    secs = int(((now or timezone.now()) - dt).total_seconds())
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60} min ago"
    if secs < 86400:
        return f"{secs // 3600} h ago"
    return f"{secs // 86400} d ago"


def tools_text(vm):
    if not vm.tools_run_state:
        return ("Powered off" if vm.power_state == "off" else "Not reported", "mute")
    label, tone = TOOLS.get(vm.tools_run_state, (vm.tools_run_state.replace("_", " ").capitalize(), "mute"))
    return (f"{label} ({vm.tools_version})" if vm.tools_version and tone == "ok" else label), tone


def provider_note(source):
    """Why snapshot / event data may be missing, in one sentence (or '' when the provider works)."""
    if not source.use_provider:
        return "Snapshots, creation date and event history are not in vCenter's stable REST API. Turn on “Read snapshots and events with pyvmomi” on the source to read them."
    if source.last_provider_error:
        return source.last_provider_error
    if not providers.pyvmomi_available() and not source.provider_ok_at:
        return providers.INSTALL_HINT
    return ""


def source_status(source, now=None):
    now = now or timezone.now()
    code, label = source.state
    stale = code in ("stale", "error", "auth")
    return {"code": code, "label": label, "stale": stale, "example": source.is_example,
            "synced_at": source.last_ok_at.timestamp() if source.last_ok_at else None,
            "synced_ago": age(source.last_ok_at, now), "error": source.last_error if code in ("error", "auth") else "",
            "interval": source.interval}


def datastore_block(vm, name=None):
    """A datastore of the VM (the first by default) as a dict for the card, or None."""
    name = name or (vm.datastore_names[0] if vm.datastore_names else "")
    if not name:
        return None
    ds = Datastore.objects.filter(source=vm.source, name=name).first()
    if ds is None:
        return {"name": name, "known": False}
    code, label = ds.status
    return {"known": True, "name": ds.name, "type": ds.ds_type or None, "capacity": bytes_h(ds.capacity_bytes),
            "used": bytes_h(ds.used_bytes), "free": bytes_h(ds.free_bytes), "used_pct": ds.used_pct, "free_pct": ds.free_pct,
            "provisioning": (None if ds.thin_provisioning is None else "Thin provisioning supported" if ds.thin_provisioning
                             else "Thin provisioning not supported"),
            "tone": code, "status": label, "pk": ds.pk}


def info_rows(vm, device):
    """[(label, value, kind)]: kind is text, mono, chip-<tone> or none (not reported)."""
    tools, tone = tools_text(vm)
    rows = [
        ("VM name", vm.name, "text"),
        ("IP address", ", ".join(vm.guest_ips[:4]) if vm.guest_ips else (device.mgmt_ip if device else None), "mono"),
        ("vCenter", vm.source.host, "mono"),
        ("Cluster", vm.cluster_name or None, "text"),
        ("Host", vm.host_name or None, "text"),
        ("Datastore", ", ".join(vm.datastore_names[:3]) if vm.datastore_names else None, "text"),
        ("vCPU", f"{vm.cpu_count} vCPU" if vm.cpu_count else None, "text"),
        ("Memory", f"{vm.memory_gb:g} GB" if vm.memory_mib else None, "text"),
        ("OS", vm.guest_full_name or vm.guest_id or None, "text"),
        ("Tools status", tools, f"chip-{tone}"),
        ("VM state", vm.power_label, f"chip-{TONE[vm.power_state]}"),
        ("Created on", when(vm.created_on), "text"),
        ("Description", vm.description or None, "text"),
    ]
    return [(label, value if value else None, kind if value else "none") for label, value, kind in rows]


def vm_context(user, device, tab, now=None):
    """The ``hv`` dict the device page uses, or None when no vCenter record is linked to this device (or it is out of scope)."""
    vm = services.vms_for(user).filter(device=device).first()
    if vm is None:
        return None
    now = now or timezone.now()
    src = vm.source
    snaps = list(vm.snapshots.all())
    hv = {
        "vm": vm, "source": src, "status": source_status(src, now), "tab": tab, "example": src.is_example,
        "rows": info_rows(vm, device), "datastore": datastore_block(vm),
        "snapshots": [{"name": s.name, "created": when(s.created_at), "created_short": short_when(s.created_at),
                       "size": bytes_h(s.size_bytes), "description": s.description, "age_days": s.age_days(now),
                       "review": s.needs_review(now), "current": s.is_current} for s in snaps],
        "snap_note": provider_note(src), "snap_known": vm.snapshots_synced_at is not None,
        "snap_review": sum(1 for s in snaps if s.needs_review(now)), "review_days": SNAPSHOT_REVIEW_DAYS,
        "events": [{"ts": short_when(e.ts), "severity": e.severity, "message": e.message, "origin": e.origin}
                   for e in vm.events.all()[:8 if tab == "overview" else 50]],
        "disks": [dict(d, size=bytes_h(d.get("capacity_bytes"))) for d in vm.disks or []],
        "nics": vm.nics or [], "guest_ips": vm.guest_ips or [],
        "tools": tools_text(vm), "power": (vm.power_label, TONE[vm.power_state]), "ha": vm.ha_status or None,
        "vcenter_url": src.vm_console_url(vm.moref),
        "can_request": services.can_on(user, "operate", device.site) and not src.is_example,
        "can_decide": services.can_on(user, "admin", services.request_site(vm)),
        "actions": ACTIONS, "action_labels": dict(VmControlRequest.ACTIONS), "needs_detail": services.NEEDS_DETAIL,
        "pending": list(vm.requests.filter(state="pending")[:5]),
        "recent_requests": list(vm.requests.exclude(state="pending")[:3]) if tab == "overview" else [],
        "live_url": reverse("hypervisor:vm_live", args=[device.pk]),
        "request_url": reverse("hypervisor:vm_request", args=[device.pk]),
        "requests_url": reverse("hypervisor:requests"),
        "synced_note": age(vm.last_detail_at, now) if vm.last_detail_at else "not yet",
        "config": {"url": reverse("hypervisor:vm_live", args=[device.pk]), "interval": 30, "kind": "vm"},
        "detail_known": vm.last_detail_at is not None,
        "disk_total": bytes_h(vm.disk_bytes),
        "datastores": [datastore_block(vm, n) for n in vm.datastore_names[:6]] if tab == "storage" else [],
        "template": {"storage": "hypervisor/_vm_storage.html", "snapshots": "hypervisor/_vm_snapshots.html",
                     "guest": "hypervisor/_vm_guest.html"}.get(tab),
    }
    return hv


def vm_meta_overlay(meta, hv_vm):
    """Fill the header's cluster / host / datastore / OS / HA from vCenter (tags stay the fallback)."""
    meta = dict(meta)
    for key, value in (("cluster", hv_vm.cluster_name), ("host", hv_vm.host_name),
                       ("datastore", ", ".join(hv_vm.datastore_names[:2])), ("os", hv_vm.guest_full_name),
                       ("vcpu", str(hv_vm.cpu_count or "")), ("vcenter", hv_vm.source.host), ("ha", hv_vm.ha_status),
                       ("tools", tools_text(hv_vm)[0] if hv_vm.tools_run_state else ""),
                       ("memory_gb", f"{hv_vm.memory_gb:g}" if hv_vm.memory_mib else "")):
        if value:
            meta[key] = value
    return meta


def live_payload(user, device, now=None):
    now = now or timezone.now()
    vm = services.vms_for(user).filter(device=device).select_related("source").first()
    if vm is None:
        return None
    ds = datastore_block(vm)
    tools, tone = tools_text(vm)
    snaps = list(vm.snapshots.values_list("pk", "created_at"))
    last_event = vm.events.values_list("pk", flat=True).first()
    rev = hashlib.sha1(f"{len(snaps)}:{max([s[0] for s in snaps], default=0)}:{last_event}:{vm.power_state}:{vm.host_name}".encode()).hexdigest()[:12]
    return {"ok": True, "now": now.timestamp(), "rev": rev, "status": source_status(vm.source, now),
            "vm": {"power": vm.power_label, "power_tone": TONE[vm.power_state], "tools": tools, "tools_tone": tone,
                   "host": vm.host_name or None, "cluster": vm.cluster_name or None,
                   "datastore": ", ".join(vm.datastore_names[:3]) or None,
                   "pending": vm.requests.filter(state="pending").count()},
            "datastore": ds if ds and ds.get("known") else None}


def datastore_rows(user):
    """[dict] for the datastore page and its live JSON."""
    rows = []
    for d in services.datastores_for(user).order_by("source__name", "name"):
        code, label = d.status
        rows.append({"pk": d.pk, "name": d.name, "source": d.source.name, "site": d.source.site, "type": d.ds_type or None,
                     "capacity": bytes_h(d.capacity_bytes), "used": bytes_h(d.used_bytes), "free": bytes_h(d.free_bytes),
                     "used_pct": d.used_pct, "free_pct": d.free_pct, "tone": code, "status": label,
                     "provisioning": None if d.thin_provisioning is None else ("Thin" if d.thin_provisioning else "Thick"),
                     "missing": bool(d.missing_since), "vms": None,
                     "example": d.source.is_example, "synced_at": d.seen_at.timestamp()})
    return rows
