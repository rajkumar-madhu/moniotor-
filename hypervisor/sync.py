"""The sync: read one vCenter (``hypervisor.vsphere``), then apply what it said in ONE transaction.

* the network phase and the database phase are separate: nothing is written until vCenter has answered, so a failure cannot leave
  half an inventory behind;
* events (``core.events``) are emitted only after the transaction committed, once per TRANSITION (a state kept in the record
  decides), so a VM that stays off, a datastore that stays at 92 % or a tools service that stays down creates no storm;
* VMs are matched to devices by ``hypervisor.matching``; a match is explicit, audited and never replaces a link an operator made;
* ``compute.VMPlacement`` is filled from the sync, but a field is only overwritten while it still holds what the sync wrote
  last time (or is empty): a value an operator typed by hand wins;
* per-VM details (guest, tools, disks, NICs) are fetched round-robin, most overdue first, within a request / time budget, so a
  big estate is covered over several passes and one slow vCenter cannot stall the worker.
"""
import logging
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.apps import apps
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from core.access import is_global_site
from core.audit import audit
from core.events import emit

from . import matching, parsers, providers, services, vsphere
from .models import (DS_CRIT_PCT, DS_WARN_PCT, Datastore, HvHost, VCenterSource, VmEvent, VmInfo, VmSnapshot)

log = logging.getLogger("linkedeye.hypervisor")

TOOLS_GRACE_S = 300
FAIL_ALERT_AFTER = 3
CLAIM_STALE = timedelta(minutes=10)
MAX_EVENTS_PER_VM = 500
PROVIDER_LOOKBACK = timedelta(days=14)
POWER_TO_PLACEMENT = {"on": "running", "off": "stopped", "suspended": "suspended"}
UNEXPECTED_WINDOW = timedelta(hours=24)


def due(source, now):
    if not source.enabled or source.is_example:
        return False
    if services.auth_paused(source):                        # rejected logins in a row: wait for a new password (also for Sync now)
        return False
    if source.sync_requested_at and (source.last_sync_at is None or source.sync_requested_at > source.last_sync_at):
        return True
    if source.last_sync_at is None:
        return True
    wait = source.interval
    if source.consecutive_failures:                         # back off while it keeps failing, never beyond an hour
        wait = min(wait * min(2 ** source.consecutive_failures, 8), 3600)
    return now >= source.last_sync_at + timedelta(seconds=wait)


def sync_due(now=None, budget_s=45):
    """Sync every due source of this tenant, sequentially; stop starting new ones once ``budget_s`` is used."""
    import time
    now = now or timezone.now()
    started = time.monotonic()
    done = []
    for src in VCenterSource.objects.filter(tenant_key=services.tenant(), enabled=True).order_by("last_sync_at", "id"):
        if time.monotonic() - started > budget_s:
            break
        if due(src, timezone.now()):
            done.append(f"{src.name}: {sync_source(src)}")
    return "; ".join(done) or "nothing due"


# ----------------------------------------------------------------------------- one source
def sync_source(source, now=None, client_factory=None, provider_factory=None):
    """Run one sync of ``source``. Never raises. Returns a short summary. ``client_factory(source, user, password)`` and
    ``provider_factory(source, reader)`` exist so tests can substitute the transport."""
    now = now or timezone.now()
    claimed = VCenterSource.objects.filter(pk=source.pk).filter(
        Q(sync_started_at__isnull=True) | Q(sync_started_at__lt=now - CLAIM_STALE)).update(sync_started_at=now)
    if not claimed:
        return "busy"
    try:
        source.refresh_from_db()
        t0 = timezone.now()
        try:
            inv, prov, provider_error = _fetch(source, client_factory, provider_factory, now)
        except vsphere.VSphereError as exc:
            _failure(source, str(exc), exc.kind, now)
            return f"failed: {exc.kind}"
        except Exception as exc:  # noqa: BLE001
            log.warning("hypervisor sync of %s failed: %s", source.pk, type(exc).__name__, exc_info=True)
            _failure(source, f"Unexpected error ({type(exc).__name__}).", "other", now)
            return "failed: unexpected"
        try:
            with transaction.atomic():
                pending, stats = apply_inventory(source, inv, prov, provider_error, now)
        except Exception as exc:  # noqa: BLE001
            log.warning("hypervisor sync of %s could not be stored: %s", source.pk, type(exc).__name__, exc_info=True)
            _failure(source, f"The result could not be stored ({type(exc).__name__}).", "other", now)
            return "failed: store"
        ms = int((timezone.now() - t0).total_seconds() * 1000)
        pending += _source_ok(source, inv, stats, ms, now)
        _emit_all(pending)
        return f"{stats['vms']} VMs, {stats['hosts']} hosts, {stats['datastores']} datastores" + (" (partial)" if inv["partial"] else "")
    finally:
        VCenterSource.objects.filter(pk=source.pk).update(sync_started_at=None)


def _fetch(source, client_factory, provider_factory, now):
    user, password = services.read_login(source)
    known = {v["moref"]: v for v in source.vms.values("moref", "last_detail_at", "power_state", "snapshots_synced_at")}

    def picker(summaries):
        def rank(s):
            k = known.get(s["moref"])
            if k is None:
                return (0, 0)
            if k["power_state"] != s["power_state"]:
                return (1, 0)
            return (2, (k["last_detail_at"] or now - timedelta(days=3650)).timestamp())
        return [s["moref"] for s in sorted(summaries, key=rank)]

    factory = client_factory or (lambda src, u, p: vsphere.VSphereClient(
        src.host, src.port, u, p, tls_mode=src.tls_mode, pinned_sha256=src.pinned_sha256))
    with factory(source, user, password) as client:
        inv = vsphere.collect_inventory(client, picker, max_detail=services.cfg("MAX_DETAIL", vsphere.MAX_DETAIL))
    prov, provider_error = None, ""
    if source.use_provider:
        order = sorted(inv["vms"], key=lambda s: (known.get(s["moref"], {}).get("snapshots_synced_at") or now - timedelta(days=3650)))
        morefs = [s["moref"] for s in order]
        try:
            make = provider_factory or providers.get_provider
            provider = make(source, lambda: (user, password))
            if provider is not None:
                try:
                    prov = provider.fetch(morefs, now - PROVIDER_LOOKBACK)
                finally:
                    provider.close()
        except providers.ProviderError as exc:
            provider_error = parsers.clean_text(str(exc), 300)
        except Exception as exc:  # noqa: BLE001
            log.warning("hypervisor provider failed: %s", type(exc).__name__)
            provider_error = f"The pyvmomi provider failed ({type(exc).__name__})."
    return inv, prov, provider_error


# ----------------------------------------------------------------------------- status of the source
def _site_of(source, device=None):
    site = device.site if device is not None else source.site
    return "" if is_global_site(site) else site


def _failure(source, message, kind, now):
    pending = []
    source.refresh_from_db()
    source.consecutive_failures += 1
    source.last_error, source.error_kind, source.last_sync_at = parsers.clean_text(message, 300), kind[:12], now
    fields = ["consecutive_failures", "last_error", "error_kind", "last_sync_at"]
    if source.consecutive_failures >= FAIL_ALERT_AFTER and source.alert_state != "down":
        source.alert_state = "down"
        fields.append("alert_state")
        pending.append(dict(source="hypervisor", title=f"vCenter {source.name} is unreachable", severity="warning",
                            message=f"{source.consecutive_failures} syncs in a row failed: {source.last_error}",
                            site=_site_of(source), obj=source, fingerprint=f"hypervisor:source:{source.pk}", kind="state_change",
                            error_kind=kind))
    source.save(update_fields=fields)
    _emit_all(pending)


def _source_ok(source, inv, stats, ms, now):
    pending = []
    source.refresh_from_db()
    source.last_sync_at, source.last_ok_at = now, now
    source.consecutive_failures, source.last_error, source.error_kind = 0, "", ""
    source.last_duration_ms, source.api_version = ms, inv.get("api", "")
    source.vm_count, source.host_count = stats["vms"], stats["hosts"]
    source.datastore_count, source.cluster_count = stats["datastores"], stats["clusters"]
    if source.alert_state == "down":
        source.alert_state = ""
        pending.append(dict(source="hypervisor", title=f"vCenter {source.name} is reachable again", severity="ok",
                            message="The sync works again.", site=_site_of(source), obj=source,
                            fingerprint=f"hypervisor:source:{source.pk}", kind="state_change"))
    source.save()
    return pending


def _emit_all(pending):
    for kw in pending:
        try:
            emit(**kw)
        except Exception:  # noqa: BLE001 - an event must never break a sync
            log.warning("hypervisor could not emit an event", exc_info=True)


# ----------------------------------------------------------------------------- applying an inventory
def _device_index(source, device_type):
    """The devices a source's VMs / hosts may be matched to: those of the source's own site (or of every site). A vCenter that
    belongs to one site must not be able to link, and so write placement data onto, a device of another site just by naming a
    VM after it."""
    Device = apps.get_model("network", "Device")
    qs = Device.objects.filter(tenant_key=source.tenant_key, device_type=device_type)
    if not is_global_site(source.site):
        qs = qs.filter(Q(site=source.site) | Q(site="") | Q(site="All Sites") | Q(site__isnull=True))
    rows = qs.values("id", "name", "mgmt_ip", "serial", "tags")
    return matching.DeviceIndex({**r, "tags": [t.strip() for t in (r["tags"] or "").split(",") if t.strip()]} for r in rows)


def apply_inventory(source, inv, prov, provider_error, now):
    """Write one inventory. -> (events to emit, stats). Called inside a transaction."""
    pending = []
    key = source.tenant_key
    clusters = {c["moref"]: c for c in inv["clusters"]}
    datastores_by_ref = {d["moref"]: d for d in inv["datastores"]}
    hosts_by_ref = {h["moref"]: h for h in inv["hosts"]}

    host_devices = _apply_hosts(source, inv["hosts"], now)
    pending += _apply_datastores(source, inv["datastores"], now)
    cluster_cache = {}

    existing = {v.moref: v for v in source.vms.select_related("device")}
    for s in inv["vms"]:
        vm = existing.get(s["moref"])
        is_new = vm is None
        if is_new:
            vm = VmInfo(tenant_key=key, source=source, moref=s["moref"])
        before = {"power": vm.power_state, "host": vm.host_name, "tools": vm.tools_run_state}
        vm._before_power = None if is_new else vm.power_state       # transient: decides "powered off just now"
        vm.name = s["name"]
        vm.power_state = s["power_state"]
        if s.get("cpu_count"):
            vm.cpu_count = s["cpu_count"]
        if s.get("memory_mib"):
            vm.memory_mib = s["memory_mib"]
        host = hosts_by_ref.get(s.get("host_moref", ""))
        if host:
            vm.host_moref, vm.host_name = host["moref"], host["name"]
        cl = clusters.get(s.get("cluster_moref", "")) or ({"name": host.get("cluster_name", ""), "ha_enabled": None} if host else None)
        if cl:
            vm.cluster_name = (cl.get("name") or "")[:120]
            ha = cl.get("ha_enabled")
            vm.ha_status = "" if ha is None else ("protected" if ha else "not protected")
        names = [datastores_by_ref[r]["name"] for r in s.get("datastore_morefs", []) if r in datastores_by_ref]
        d = inv["details"].get(s["moref"])
        if d:
            _apply_detail(vm, d, now)
            for disk in d["disks"]:
                if disk["datastore"] and disk["datastore"] not in names:
                    names.append(disk["datastore"])
        if names:
            vm.datastore_names = names[:12]
        if vm.power_state != "on":
            vm.tools_run_state, vm.tools_bad_since = "", None
        vm.seen_at, vm.missing_since, vm.last_synced_at = now, None, now
        if is_new:
            vm.save()
        else:
            vm.save(update_fields=_sync_fields())      # never the link: an operator may have changed it since this pass began
        existing[s["moref"]] = vm
        _observed_events(vm, before, is_new, now)
        # a VM with no detail yet has no guest identity to match on; it is matched on the next pass that fetches it

    # VMs that vanished
    listed = {s["moref"] for s in inv["vms"]}
    if len(listed) < vsphere.MAX_VMS:
        source.vms.filter(missing_since__isnull=True).exclude(moref__in=listed).update(missing_since=now)

    _apply_matching(source, existing, host_devices, now)
    _apply_provider(source, existing, prov, provider_error, now)
    snaps = {}
    for snap in VmSnapshot.objects.filter(vm__source=source):
        snaps.setdefault(snap.vm_id, []).append(snap)
    for vm in existing.values():
        if vm.moref in listed:
            pending += _transitions(source, vm, snaps.get(vm.pk, []), now)
            if vm.is_matched:
                _update_placement(source, vm, host_devices, cluster_cache)
    return pending, {"vms": len(listed), "hosts": len(inv["hosts"]), "datastores": len(inv["datastores"]),
                     "clusters": len(inv["clusters"])}


#: what an operator decides on a VM record (the link and how it was reached): the sync saves everything else
OPERATOR_FIELDS = frozenset({"device", "match_state", "match_reason", "match_candidates", "matched_by", "matched_at", "auto_match_off"})


def _sync_fields():
    return [f.name for f in VmInfo._meta.concrete_fields if not f.primary_key and f.name not in OPERATOR_FIELDS]


def _apply_detail(vm, d, now):
    if d["name"]:
        vm.name = d["name"]
    vm.bios_uuid, vm.instance_uuid = d["bios_uuid"], d["instance_uuid"]
    for f in ("cpu_count", "cores_per_socket", "memory_mib"):
        if d.get(f):
            setattr(vm, f, d[f])
    if d["guest_id"]:
        vm.guest_id = d["guest_id"]
    vm.disks, vm.nics = d["disks"], d["nics"]
    vm.guest_ips = d["guest_ips"]
    if d["guest_full_name"]:
        vm.guest_full_name = d["guest_full_name"]
    vm.guest_family, vm.guest_hostname = d["guest_family"] or vm.guest_family, d["guest_hostname"] or vm.guest_hostname
    tools = d.get("tools")
    if tools:
        vm.tools_run_state, vm.tools_version_status, vm.tools_version = tools["run_state"], tools["version_status"], tools["version"]
    vm.last_detail_at = now


def _observed_events(vm, before, is_new, now):
    """Events the sync can observe itself (vCenter's REST API has no event history)."""
    if is_new:
        return
    stamp = int(now.timestamp())
    if before["power"] != vm.power_state and "unknown" not in (before["power"], vm.power_state):
        _add_event(vm, f"obs:power:{stamp}", now, "info", "power", f"VM power state changed to {vm.power_label.lower()}")
    if before["host"] and vm.host_name and before["host"] != vm.host_name:
        _add_event(vm, f"obs:host:{stamp}", now, "info", "host", f"VM moved from {before['host']} to {vm.host_name}")
    if before["tools"] and vm.tools_run_state and before["tools"] != vm.tools_run_state:
        sev = "warning" if vm.tools_run_state == "NOT_RUNNING" else "info"
        _add_event(vm, f"obs:tools:{stamp}", now, sev, "tools", f"VMware Tools: {vm.tools_run_state.replace('_', ' ').lower()}")


def _add_event(vm, key, ts, severity, kind, message, origin="sync"):
    VmEvent.objects.get_or_create(vm=vm, key=key[:80], defaults={"tenant_key": vm.tenant_key, "ts": ts, "severity": severity,
                                                                 "kind": kind[:80], "message": parsers.clean_text(message, 400),
                                                                 "origin": origin})


# ----------------------------------------------------------------------------- hosts, datastores
def _apply_hosts(source, hosts, now):
    existing = {h.moref: h for h in source.hosts.all()}
    index = _device_index(source, "physical_server")
    rows = {}
    for h in hosts:
        row = existing.get(h["moref"]) or HvHost(tenant_key=source.tenant_key, source=source, moref=h["moref"])
        row.name, row.connection_state, row.power_state = h["name"], h["connection_state"], h["power_state"]
        row.cluster_name, row.cluster_moref = h.get("cluster_name", ""), h.get("cluster_moref", "")
        row.seen_at, row.missing_since = now, None
        rows[h["moref"]] = row
    results = matching.resolve_claims({r: matching.match_host({"name": rows[r].name}, index) for r in rows})
    out = {}
    for ref, row in rows.items():
        state, dev, reason, _c = results[ref]
        row.match_state, row.match_reason, row.device_id = state, reason[:200], dev
        row.save()
        if dev:
            out[ref] = dev
    source.hosts.filter(missing_since__isnull=True).exclude(moref__in=list(rows)).update(missing_since=now)
    return out


def _ds_level(pct, previous):
    if pct is None:
        return previous
    crit_hold = previous >= DS_CRIT_PCT and pct >= DS_CRIT_PCT - 2
    warn_hold = previous >= DS_WARN_PCT and pct >= DS_WARN_PCT - 2
    if pct >= DS_CRIT_PCT or crit_hold:
        return DS_CRIT_PCT
    if pct >= DS_WARN_PCT or warn_hold:
        return DS_WARN_PCT
    return 0


def _apply_datastores(source, datastores, now):
    pending = []
    existing = {d.moref: d for d in source.datastores.all()}
    for d in datastores:
        row = existing.get(d["moref"]) or Datastore(tenant_key=source.tenant_key, source=source, moref=d["moref"])
        row.name, row.ds_type = d["name"], d["ds_type"]
        row.capacity_bytes, row.free_bytes = d["capacity_bytes"], d["free_bytes"]
        if d["accessible"] is not None:
            row.accessible = d["accessible"]
        if d["thin_provisioning"] is not None:
            row.thin_provisioning = d["thin_provisioning"]
        row.seen_at, row.missing_since = now, None
        previous = row.alert_level
        row.alert_level = _ds_level(row.used_pct, previous)
        row.save()
        if row.alert_level != previous:
            pct = row.used_pct
            worse = row.alert_level > previous
            if row.alert_level == 0:
                sev, title = "ok", f"Datastore {row.name} is below {DS_WARN_PCT}% used again"
            elif row.alert_level >= DS_CRIT_PCT:
                sev, title = "critical", f"Datastore {row.name} is {pct}% full"
            else:
                sev, title = ("warning", f"Datastore {row.name} is {pct}% full") if worse else (
                    "warning", f"Datastore {row.name} dropped below {DS_CRIT_PCT}% but is still {pct}% full")
            pending.append(dict(source="hypervisor", title=title, severity=sev,
                                message=f"{row.name} on {source.name}: {pct}% used.", site=_site_of(source), obj=row,
                                fingerprint=f"hypervisor:datastore:{row.pk}", kind="state_change", used_pct=pct))
    source.datastores.filter(missing_since__isnull=True).exclude(moref__in=[d["moref"] for d in datastores]).update(missing_since=now)
    return pending


# ----------------------------------------------------------------------------- matching
def _apply_matching(source, vms, host_devices, now):
    """Match the VMs that have no link yet. Matched, linked and ignored VMs are left alone (a link is never replaced)."""
    Device = apps.get_model("network", "Device")
    to_match = {}
    for vm in vms.values():
        if vm.match_state == VmInfo.MATCHED and vm.device_id is None:      # the device was deleted
            vm.match_state, vm.match_reason = VmInfo.UNMATCHED, "the linked device was deleted"
        if vm.device_id is None and vm.match_state != VmInfo.IGNORED and not vm.auto_match_off and vm.last_detail_at is not None:
            to_match[vm.moref] = vm
    if to_match:
        # an operator may have linked, ignored or unlinked a VM while the inventory was being read: what is stored now decides
        # (the rows are locked until this transaction ends on databases that support it)
        fresh = {r["pk"]: r for r in VmInfo.objects.select_for_update().filter(source=source)
                 .values("pk", "device_id", "match_state", "auto_match_off")}
        for ref, vm in list(to_match.items()):
            now_row = fresh.get(vm.pk)
            if now_row is None or now_row["device_id"] is not None or now_row["auto_match_off"] \
                    or now_row["match_state"] == VmInfo.IGNORED:
                del to_match[ref]
    if not to_match:
        return
    index = _device_index(source, "virtual_machine")
    linked = {d: (m, gone) for d, m, gone in
              VmInfo.objects.filter(tenant_key=source.tenant_key, device__isnull=False).values_list("device_id", "pk", "missing_since")}
    raw = {ref: matching.match_vm({"name": vm.name, "bios_uuid": vm.bios_uuid, "instance_uuid": vm.instance_uuid,
                                   "guest_hostname": vm.guest_hostname, "guest_ips": vm.guest_ips}, index)
           for ref, vm in to_match.items()}
    results = matching.resolve_claims(raw)
    for ref, vm in to_match.items():
        state, dev, reason, cands = results[ref]
        if state == matching.MATCHED and dev in linked:
            holder, holder_gone = linked[dev]
            if holder_gone is not None:
                # the VM that owned this device is no longer listed by its vCenter (rebuilt under the same name, or moved to
                # another vCenter): the device follows the VM that is there now. Audited; the old record lets go of it.
                VmInfo.objects.filter(pk=holder).update(
                    device=None, match_state=VmInfo.UNMATCHED, matched_by="", auto_match_off=False,
                    match_reason=f"vCenter no longer lists this VM; its device was matched to “{vm.name}” on {source.name}"[:300])
                for other in vms.values():
                    if other.pk == holder:
                        other.device_id = None
                audit(None, "hypervisor.match.release", vm, device_id=dev, released_vm_id=holder, source=source.name)
            else:
                state, dev, reason, cands = matching.AMBIGUOUS, None, "the device is already linked to another vCenter VM", [dev]
        vm.match_state, vm.match_reason, vm.match_candidates = state, reason[:300], cands
        if state == matching.MATCHED:
            vm.device_id = dev
            vm.matched_by, vm.matched_at = "system", now
            vm.device = Device.objects.get(pk=dev)
        vm.save(update_fields=["match_state", "match_reason", "match_candidates", "device", "matched_by", "matched_at"])
        if state == matching.MATCHED:
            audit(None, "hypervisor.match.auto", vm, device=vm.device.name, device_id=dev, reason=reason, source=source.name)


# ----------------------------------------------------------------------------- provider data
def _apply_provider(source, vms, prov, provider_error, now):
    source.last_provider_error = provider_error[:300]
    if prov is None:
        if provider_error:
            source.save(update_fields=["last_provider_error"])
        return
    source.provider_ok_at = now
    source.save(update_fields=["last_provider_error", "provider_ok_at"])
    for ref, snaps in prov.get("snapshots", {}).items():
        vm = vms.get(ref)
        if vm is None:
            continue
        clean = [s for s in (providers.clean_snapshot(x) for x in snaps) if s]
        keep = {s["snap_id"] for s in clean}
        VmSnapshot.objects.filter(vm=vm).exclude(snap_id__in=keep).delete()
        for s in clean:
            VmSnapshot.objects.update_or_create(vm=vm, snap_id=s["snap_id"], defaults={
                "tenant_key": vm.tenant_key, "name": s["name"], "description": s["description"], "created_at": s["created_at"],
                "size_bytes": s["size_bytes"], "is_current": s["is_current"]})
        vm.snapshots_synced_at = now
        vm.save(update_fields=["snapshots_synced_at"])
    for ref, events in prov.get("events", {}).items():
        vm = vms.get(ref)
        if vm is None:
            continue
        for e in (x for x in (providers.clean_event(y) for y in events) if x):
            _add_event(vm, f"vc:{e['key']}", e["ts"], e["severity"], e["kind"], e["message"], origin="provider")
        vm.events_synced_at = now
        vm.save(update_fields=["events_synced_at"])
    for ref, extra in prov.get("extras", {}).items():
        vm = vms.get(ref)
        if vm is None:
            continue
        created = extra.get("created_on")
        if created is not None and hasattr(created, "tzinfo"):
            vm.created_on = created if created.tzinfo else timezone.make_aware(created, timezone.utc)
        vm.description = parsers.clean_text(extra.get("description"), 500)
        vm.save(update_fields=["created_on", "description"])


# ----------------------------------------------------------------------------- transitions (events once per transition)
def _vm_target(vm):
    return vm.device if vm.device_id else vm


def _transitions(source, vm, snapshots, now):
    pending = []
    state = dict(vm.alert_state or {})
    changed = False
    site = _site_of(source, vm.device if vm.device_id else None)
    target = _vm_target(vm)

    def push(sev, title, msg, fp):
        pending.append(dict(source="hypervisor", title=title[:200], severity=sev, message=msg, site=site, obj=target,
                            fingerprint=fp, kind="state_change", vm=vm.name, moref=vm.moref))

    # powered off (an approved power-off request in the last day makes it expected: recorded as info, not a warning)
    if vm.power_state == "off" and state.get("power") != "off" and getattr(vm, "_before_power", None) == "on":
        expected = vm.requests.filter(action__in=("power_off", "restart", "suspend"), state="approved",
                                      decided_at__gte=now - UNEXPECTED_WINDOW).exists()
        push("info" if expected else "warning", f"VM {vm.name} powered off" + (" (approved request)" if expected else " unexpectedly"),
             f"{vm.name} on {source.name} is powered off." + (" An approved request exists." if expected else ""),
             f"hypervisor:vm:{vm.pk}:power")
        state["power"] = "off"
        changed = True
    elif vm.power_state == "on" and state.get("power") == "off":
        push("ok", f"VM {vm.name} is powered on again", f"{vm.name} on {source.name} is running.", f"hypervisor:vm:{vm.pk}:power")
        state.pop("power", None)
        changed = True

    # VMware Tools not running while the VM is on (after a grace period, so a booting guest does not alert)
    if vm.power_state == "on" and vm.tools_run_state == "NOT_RUNNING":
        if vm.tools_bad_since is None:
            vm.tools_bad_since = now
            changed = True
        if (now - vm.tools_bad_since).total_seconds() >= TOOLS_GRACE_S and state.get("tools") != "bad":
            push("warning", f"VMware Tools not running on {vm.name}", f"{vm.name} on {source.name} is powered on but VMware Tools is "
                 "not running.", f"hypervisor:vm:{vm.pk}:tools")
            state["tools"] = "bad"
            changed = True
    elif vm.tools_run_state == "RUNNING" or vm.power_state != "on":
        if vm.tools_bad_since is not None:
            vm.tools_bad_since = None
            changed = True
        if state.get("tools") == "bad" and vm.power_state == "on":
            push("ok", f"VMware Tools running again on {vm.name}", f"{vm.name} on {source.name}.", f"hypervisor:vm:{vm.pk}:tools")
            state.pop("tools", None)
            changed = True

    # snapshots older than the review threshold
    old = [s for s in snapshots if s.needs_review(now)]
    if old and state.get("snapshots") != "old":
        oldest = max(s.age_days(now) for s in old)
        push("warning", f"{len(old)} snapshot{'s' if len(old) != 1 else ''} of {vm.name} older than 7 days",
             f"The oldest is {oldest} days old. Snapshots slow a VM down and grow the datastore.", f"hypervisor:vm:{vm.pk}:snapshots")
        state["snapshots"] = "old"
        changed = True
    elif not old and state.get("snapshots") == "old":
        push("ok", f"No old snapshots left on {vm.name}", "", f"hypervisor:vm:{vm.pk}:snapshots")
        state.pop("snapshots", None)
        changed = True
    if changed:
        vm.alert_state = state
        vm.save(update_fields=["alert_state", "tools_bad_since"])
    return pending


# ----------------------------------------------------------------------------- compute.VMPlacement
def _q(value, places):
    return str(Decimal(value).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def _cluster(source, name, cache):
    if not name:
        return None
    if name in cache:
        return cache[name]
    Cluster = apps.get_model("compute", "Cluster")
    obj = Cluster.objects.filter(tenant_key=source.tenant_key, name=name).first()
    if obj is None:
        obj = Cluster.objects.create(tenant_key=source.tenant_key, name=name, kind="vmware", tags="vcenter",
                                     site="All Sites" if is_global_site(source.site) else source.site,
                                     description=f"Synced from vCenter “{source.name}”.")
    elif obj.kind != "vmware":
        obj = None                                  # a cluster of another kind with this name is not ours to link
    cache[name] = obj
    return obj


def _update_placement(source, vm, host_devices, cluster_cache):
    """Fill compute.VMPlacement from the sync. A field is overwritten only while it still holds what the sync wrote before."""
    if not apps.is_installed("compute"):
        return
    VMPlacement = apps.get_model("compute", "VMPlacement")
    cluster = _cluster(source, vm.cluster_name, cluster_cache)
    desired = {"host_id": host_devices.get(vm.host_moref), "cluster_id": cluster.pk if cluster else None,
               "power_state": POWER_TO_PLACEMENT.get(vm.power_state), "os": (vm.guest_full_name or "")[:120] or None,
               "vcpus": vm.cpu_count or None,
               "memory_gb": _q(vm.memory_mib / 1024, 2) if vm.memory_mib else None,
               "disk_gb": _q(vm.disk_bytes / 1024 ** 3, 1) if vm.disk_bytes else None}
    desired = {k: v for k, v in desired.items() if v not in (None, "")}
    if not desired:
        return
    placement = VMPlacement.objects.filter(tenant_key=vm.tenant_key, vm_id=vm.device_id).first()
    last = dict(vm.placement_synced or {})
    if placement is None:
        VMPlacement.objects.create(tenant_key=vm.tenant_key, vm_id=vm.device_id, **desired)
        vm.placement_synced = dict(desired)
        vm.save(update_fields=["placement_synced"])
        return
    overwrite = bool(services.cfg("OVERWRITE_PLACEMENT", False))      # the flag: also replace what an operator typed
    updates, synced = [], dict(last)
    for field, value in desired.items():
        cur = getattr(placement, field)
        cur_norm = "" if cur in (None, "") else (_q(cur, 2 if field == "memory_gb" else 1) if field in ("memory_gb", "disk_gb") else str(cur))
        val_norm = str(value)
        if overwrite or cur_norm in ("", val_norm) or (field in last and cur_norm == str(last[field])):
            if cur_norm != val_norm:
                setattr(placement, field, value)
                updates.append(field)
            synced[field] = value
    if updates:
        placement.save(update_fields=updates + ["updated_at"])
    if synced != last:
        vm.placement_synced = synced
        vm.save(update_fields=["placement_synced"])


# ----------------------------------------------------------------------------- retention
def retention(now=None):
    """Age out events, long-missing records and old requests. -> counts."""
    from .models import VmControlRequest
    now = now or timezone.now()
    t = services.tenant()
    out = {"events": 0, "missing": 0, "requests": 0, "expired": 0}
    days = services.cfg("EVENT_RETENTION_DAYS", 30)
    out["events"] += VmEvent.objects.filter(tenant_key=t, ts__lt=now - timedelta(days=days)).delete()[0]
    for vm_id in VmEvent.objects.filter(tenant_key=t).values_list("vm_id", flat=True).distinct():
        qs = VmEvent.objects.filter(vm_id=vm_id)
        if qs.count() > MAX_EVENTS_PER_VM:
            cut = list(qs.order_by("-ts", "-id").values_list("pk", flat=True)[MAX_EVENTS_PER_VM:])
            out["events"] += VmEvent.objects.filter(pk__in=cut).delete()[0]
    gone = now - timedelta(days=services.cfg("MISSING_DAYS", 30))
    for model in (VmInfo, HvHost, Datastore):
        out["missing"] += model.objects.filter(tenant_key=t, missing_since__lt=gone).delete()[0]
    out["expired"] = services.expire_requests(now)
    out["requests"] += VmControlRequest.objects.filter(
        tenant_key=t, state__in=["approved", "rejected", "expired"],
        requested_at__lt=now - timedelta(days=services.cfg("REQUEST_RETENTION_DAYS", 180))).delete()[0]
    return out
