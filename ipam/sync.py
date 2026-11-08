"""Sync from devices and conflict detection.

sync(): network.Device management addresses and network.DiscoveredHost addresses that fall inside a known subnet
become IPAddress rows (status active). Nothing outside a subnet is created – those show up as conflicts.
conflicts(): duplicate management IPs across devices, device IPs outside every subnet, DNS A/AAAA records that point
at addresses not recorded in IPAM, and address rows without a subnet.
"""
import ipaddress
import logging

from django.apps import apps
from django.conf import settings
from django.db import transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone

from core.access import scope_by_site
from core.events import emit

from . import dns, services
from .models import GLOBAL_SITE, AddressStatus, DNSRecord, IPAddress, Source, Subnet, SyncRun, ip_key

log = logging.getLogger("linkedeye.ipam")

MAX_DEVICES = 20000
MAX_HOSTS = 20000
MAX_REPORT_ITEMS = 500


def _model(app_label, name):
    try:
        return apps.get_model(app_label, name)
    except LookupError:
        return None


def _ip(text):
    try:
        return ipaddress.ip_address((text or "").strip())
    except ValueError:
        return None


class _Finder:
    """Most specific subnet for an address, preferring the global table, from one in-memory list of subnets."""

    def __init__(self, tenant_key):
        self.subnets = sorted(Subnet.objects.filter(tenant_key=tenant_key),
                              key=lambda s: (s.vrf != "", -s.prefixlen))

    def find(self, ip):
        key = ip_key(ip)
        for s in self.subnets:
            if s.version == ip.version and s.net_start <= key <= s.net_end:
                return s
        return None


def _devices(tenant_key, user=None):
    Device = _model("network", "Device")
    if Device is None:
        return []
    qs = Device.objects.filter(tenant_key=tenant_key)
    if user is not None:
        qs = scope_by_site(qs, user)
    return list(qs.order_by("name")[:MAX_DEVICES])


def _discovered(tenant_key, user=None):
    Host = _model("network", "DiscoveredHost")
    if Host is None:
        return []
    qs = Host.objects.filter(run__tenant_key=tenant_key).exclude(status="ignored").select_related("device", "run")
    if user is not None:
        qs = scope_by_site(qs, user, field="run__site")
    latest = {}
    for h in qs.order_by("-found_at")[:MAX_HOSTS]:
        latest.setdefault(h.ip, h)
    return list(latest.values())


def sync(user=None, trigger="manual", actor_name="system", tenant_key=None):
    """Create / update IPAddress rows from devices and discovery results. `user` limits the run to the sites
    the user may operate on (None = the scheduled job, every site). Returns the SyncRun."""
    t = tenant_key or settings.LINKEDEYE_TENANT
    allowed = services.operate_sites(user) if user is not None else None
    finder = _Finder(t)
    report = {"created": [], "updated": [], "outside": [], "duplicate": [], "denied": [], "not_ip": 0}
    counts = {"created": 0, "updated": 0, "unchanged": 0, "skipped": 0}
    now = timezone.now()
    existing = {(a.vrf, a.address): a for a in IPAddress.objects.filter(tenant_key=t).select_related("device")}

    def note(kind, item):
        if len(report[kind]) < MAX_REPORT_ITEMS:
            report[kind].append(item)

    def permitted(site):
        return allowed is None or site in allowed

    def apply(ip, hostname, device, seen, source, origin, origin_site):
        subnet = finder.find(ip)
        if subnet is None:
            counts["skipped"] += 1
            note("outside", {"address": str(ip), "origin": origin, "site": origin_site or GLOBAL_SITE})
            return
        if not permitted(subnet.site):
            counts["skipped"] += 1
            note("denied", {"address": str(ip), "origin": origin, "site": subnet.site})
            return
        a = existing.get((subnet.vrf, str(ip)))
        if a is None:
            a = IPAddress(tenant_key=t, address=str(ip), vrf=subnet.vrf, subnet=subnet, site=subnet.site,
                          status=AddressStatus.ACTIVE, hostname=hostname[:253], device=device, source=source,
                          last_seen=seen, description="")
            a.save()
            existing[(subnet.vrf, str(ip))] = a
            counts["created"] += 1
            note("created", {"pk": a.pk, "address": a.address, "origin": origin, "site": a.site})
            return
        if device is not None and a.device_id and a.device_id != device.pk:
            counts["skipped"] += 1
            note("duplicate", {"pk": a.pk, "address": a.address, "origin": origin, "site": a.site,
                               "holder": a.device.name if a.device else ""})
            return
        changes = []
        if device is not None and not a.device_id:
            a.device = device
            changes.append("device")
        if hostname and not a.hostname:
            a.hostname = hostname[:253]
            changes.append("hostname")
        if seen and (a.last_seen is None or seen > a.last_seen):
            a.last_seen = seen
            changes.append("last_seen")
        if changes:
            a.save()
            counts["updated"] += 1
            if changes != ["last_seen"]:
                note("updated", {"pk": a.pk, "address": a.address, "origin": origin, "site": a.site, "fields": changes})
        else:
            counts["unchanged"] += 1

    with transaction.atomic():
        for d in _devices(t, user):
            ip = _ip(d.mgmt_ip)
            if ip is None:
                report["not_ip"] += 1
                continue
            apply(ip, d.name, d, d.last_seen, Source.DEVICE, f"device {d.name}", d.site)
        for h in _discovered(t, user):
            ip = _ip(h.ip)
            if ip is None:
                continue
            apply(ip, h.device.name if h.device_id else "", h.device if h.device_id else None, h.found_at,
                  Source.DISCOVERY, f"discovery {h.run.cidr}", h.run.site)
        found = conflicts(t, user)
        keys = sorted({c["key"] for c in found})
        scope = sorted(allowed) if allowed is not None else []
        # compare with the last run over the same sites, so a site-limited manual run does not skew the job's diff
        previous = next((r for r in SyncRun.objects.filter(tenant_key=t).order_by("-ran_at", "-id")[:50] if r.sites == scope), None)
        before = set(previous.conflict_keys) if previous else set()
        new = [k for k in keys if k not in before]
        run = SyncRun.objects.create(
            tenant_key=t, trigger=trigger, actor_name=actor_name, ran_at=now,
            sites=scope,
            created=counts["created"], updated=counts["updated"], unchanged=counts["unchanged"], skipped=counts["skipped"],
            conflicts=len(keys), new_conflicts=len(new), conflict_keys=keys[:5000], report=report)
    if run.new_conflicts:
        who = "The hourly IPAM sync" if trigger == "job" else f"An IPAM sync by {actor_name}"
        emit(source="ipam", title=f"IPAM: {run.new_conflicts} new address conflict(s)", severity="warning",
             message=f"{who} found {run.conflicts} conflict(s), {run.new_conflicts} new since the previous run.",
             obj=run, fingerprint="ipam:conflicts", conflicts=run.conflicts, new=run.new_conflicts)
    return run


def run_job():
    """Hourly ipam.sync job: sync every site and warn about conflicts that were not there on the previous run."""
    try:
        run = sync(user=None, trigger="job", actor_name="ipam.sync")
    except Exception:  # a job must never raise
        log.exception("ipam.sync failed")
        return "error"
    return f"{run.created} created, {run.updated} updated, {run.conflicts} conflicts ({run.new_conflicts} new)"


# ------------------------------------------------------------------ conflicts
def conflicts(tenant_key=None, user=None):
    """[{kind, key, address, title, detail, site, links:[(label, url)]}] – scoped to the user's sites when given."""
    t = tenant_key or settings.LINKEDEYE_TENANT
    out = []
    finder = _Finder(t)
    devices = _devices(t, user)

    by_ip = {}
    for d in devices:
        ip = _ip(d.mgmt_ip)
        if ip is not None:
            by_ip.setdefault(ip, []).append(d)
    for ip, group in sorted(by_ip.items(), key=lambda kv: (kv[0].version, int(kv[0]))):
        if len(group) > 1:
            out.append({"kind": "duplicate", "key": f"dup:{ip}", "address": str(ip),
                        "title": f"{ip} is the management address of {len(group)} devices",
                        "detail": ", ".join(d.name for d in group), "site": group[0].site,
                        "links": [(d.name, d.get_absolute_url()) for d in group]})
        if finder.find(ip) is None:
            for d in group:
                out.append({"kind": "outside", "key": f"out:{ip}:{d.pk}", "address": str(ip),
                            "title": f"{d.name} uses {ip}, which is outside every subnet",
                            "detail": "Add the subnet it belongs to (or fix the device address).", "site": d.site,
                            "links": [(d.name, d.get_absolute_url())]})

    records = DNSRecord.objects.filter(tenant_key=t, rtype__in=["A", "AAAA"]).select_related("zone")
    if user is not None:
        records = scope_by_site(records, user, field="zone__site")
    # an anti-join in the database: no tenant-wide set of every recorded address is built in memory
    unallocated = records.annotate(allocated=Exists(IPAddress.objects.filter(tenant_key=t, address=OuterRef("value")))
                                   ).filter(allocated=False)
    for r in unallocated.order_by("zone__name", "name")[:MAX_HOSTS]:
        inside = finder.find(ipaddress.ip_address(r.value))
        out.append({"kind": "dns_free", "key": f"dns:{r.pk}", "address": r.value,
                    "title": f"{r.fqdn} ({r.rtype}) points to {r.value}, which is not allocated",
                    "detail": (f"Free address in {inside.cidr}." if inside else "Outside every subnet."),
                    "site": r.zone.site, "links": [(r.zone.name, r.zone.get_absolute_url())]})

    orphans = IPAddress.objects.filter(tenant_key=t, subnet__isnull=True)
    if user is not None:
        orphans = scope_by_site(orphans, user)
    for a in orphans.order_by("vrf", "version", "addr_key")[:MAX_HOSTS]:
        out.append({"kind": "orphan", "key": f"orphan:{a.vrf}:{a.address}", "address": a.address,
                    "title": f"{a} has no subnet", "detail": "Its subnet was deleted; add a subnet that contains it.",
                    "site": a.site, "links": [(a.address, a.get_absolute_url())]})
    return out


KIND_LABELS = {"duplicate": "Duplicate device IP", "outside": "Device IP outside subnets",
               "dns_free": "DNS record → free address", "orphan": "Address without subnet"}


def link_record_ip(record):
    """The IPAddress an A/AAAA/PTR record refers to (global table preferred), or None."""
    if record.rtype in ("A", "AAAA"):
        target = record.value
    elif record.rtype == "PTR":
        ip = dns.ptr_address(record.name, record.zone.name)
        target = str(ip) if ip else None
    else:
        target = None
    if not target:
        return None
    rows = IPAddress.objects.filter(tenant_key=record.tenant_key, address=target)
    return rows.filter(vrf="").first() or rows.order_by("vrf").first()


def relink_records(tenant_key, addresses):
    """Refresh DNSRecord.ip for records that point at any of these addresses (after an address is added)."""
    addresses = {str(a) for a in addresses}
    if not addresses:
        return 0
    changed = 0
    for r in DNSRecord.objects.filter(tenant_key=tenant_key, rtype__in=["A", "AAAA"], value__in=addresses, ip__isnull=True):
        r.ip = link_record_ip(r)
        if r.ip_id:
            r.save(update_fields=["ip"])
            changed += 1
    return changed


__all__ = ["sync", "run_job", "conflicts", "KIND_LABELS", "link_record_ip", "relink_records", "GLOBAL_SITE"]
