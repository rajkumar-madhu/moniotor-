"""Querysets, access helpers and capacity maths for the compute pages.

Every queryset filters the tenant and is site-scoped for the requesting user. Servers and VMs are
network.Device rows (looked up lazily so the app still imports when network is absent in a test settings).
"""
from decimal import Decimal

from django.apps import apps
from django.conf import settings
from django.db.models import Count, Q, Sum

from core.access import PERMS, RANK, _memberships, can, scope_by_site, visible_sites

from .models import GLOBAL_SITES, Cluster, ClusterKind, ClusterMember, PowerState, VMPlacement, is_global

SERVER = "physical_server"
VM = "virtual_machine"


def tenant():
    return settings.LINKEDEYE_TENANT


def Device():  # noqa: N802 - model accessor
    return apps.get_model("network", "Device")


# ----------------------------------------------------------------------------- access
def _tenant_wide(user, perm):
    if user.is_superuser:
        return True
    ms = _memberships(user)
    if not ms:  # users without memberships hold their default role everywhere
        return True
    return any(m.site is None and RANK[m.role] >= PERMS[perm] for m in ms)


def can_manage(user, perm, site):
    """`perm` on a record's site. Global records ("All Sites" / blank) need the permission tenant-wide."""
    if not is_global(site):
        return can(user, perm, site_name=site)
    return can(user, perm) and _tenant_wide(user, perm)


# ----------------------------------------------------------------------------- querysets
def devices_for(user):
    return scope_by_site(Device().objects.filter(tenant_key=tenant()), user)


def servers_for(user):
    return devices_for(user).filter(device_type=SERVER)


def vms_for(user):
    return devices_for(user).filter(device_type=VM)


def clusters_for(user):
    return scope_by_site(Cluster.objects.filter(tenant_key=tenant()), user)


def members_for(user):
    qs = ClusterMember.objects.filter(tenant_key=tenant(), cluster__tenant_key=tenant(), device__tenant_key=tenant())
    qs = scope_by_site(qs, user, field="cluster__site")
    return scope_by_site(qs, user, field="device__site")


def placements_for(user):
    qs = VMPlacement.objects.filter(tenant_key=tenant(), vm__tenant_key=tenant())
    return scope_by_site(qs, user, field="vm__site")


def known_sites(user=None):
    """Site names that records may use: core Sites of the tenant plus sites already used by devices/clusters,
    limited to what the user can see."""
    names = set()
    try:
        Site = apps.get_model("core", "Site")
        names |= set(Site.objects.filter(tenant_key=tenant()).values_list("name", flat=True))
    except LookupError:  # pragma: no cover - core is always installed
        pass
    names |= set(Device().objects.filter(tenant_key=tenant()).exclude(site__in=GLOBAL_SITES)
                 .values_list("site", flat=True).distinct())
    names |= set(Cluster.objects.filter(tenant_key=tenant()).exclude(site__in=GLOBAL_SITES)
                 .values_list("site", flat=True).distinct())
    if user is not None:
        vis = visible_sites(user)
        if vis is not None:
            names &= set(vis)
    return sorted(n for n in names if n)


def manageable_sites(user, perm="operate"):
    out = ["All Sites"] if can_manage(user, perm, "All Sites") else []
    return out + [s for s in known_sites(user) if can_manage(user, perm, s)]


# ----------------------------------------------------------------------------- aggregates
def status_counts(qs):
    counts = {"up": 0, "down": 0, "unknown": 0}
    for r in qs.order_by().values("status").annotate(n=Count("id")):
        counts[r["status"]] = counts.get(r["status"], 0) + r["n"]
    counts["total"] = sum(counts.values())
    return counts


def power_counts(placements):
    counts = {s: 0 for s in PowerState.values}
    for r in placements.order_by().values("power_state").annotate(n=Count("id")):
        counts[r["power_state"]] = r["n"]
    return counts


def guest_counts(user, host_ids=None):
    """{host device pk: number of VMs placed on it} (only VMs the user can see)."""
    qs = placements_for(user).exclude(host=None)
    if host_ids is not None:
        qs = qs.filter(host_id__in=list(host_ids))
    return {r["host_id"]: r["n"] for r in qs.order_by().values("host_id").annotate(n=Count("id"))}


def allocation_by_host(user, host_ids=None):
    """{host pk: {"vms", "running", "vcpus", "memory_gb"}} – vCPU / RAM sums count running and suspended guests
    (a stopped VM holds no host resources)."""
    qs = placements_for(user).exclude(host=None)
    if host_ids is not None:
        qs = qs.filter(host_id__in=list(host_ids))
    out = {}
    rows = qs.order_by().values("host_id").annotate(
        vms=Count("id"), running=Count("id", filter=Q(power_state=PowerState.RUNNING)),
        vcpus=Sum("vcpus", filter=~Q(power_state=PowerState.STOPPED)),
        memory_gb=Sum("memory_gb", filter=~Q(power_state=PowerState.STOPPED)))
    for r in rows:
        out[r["host_id"]] = {"vms": r["vms"], "running": r["running"], "vcpus": r["vcpus"] or 0,
                             "memory_gb": r["memory_gb"] or Decimal(0)}
    return out


def clusters_by_device(user, device_ids):
    """{device pk: [ClusterMember…]} for the given devices."""
    out = {}
    for m in members_for(user).filter(device_id__in=list(device_ids)).select_related("cluster"):
        out.setdefault(m.device_id, []).append(m)
    return out


def ratio(allocated, capacity):
    if allocated is None or not capacity:
        return None
    return float(allocated) / float(capacity)


def cluster_capacity(cluster, user, host_metrics=None):
    """Capacity summary of one cluster.

    Host totals come from ClusterMember.cpu_cores / memory_gb when entered, otherwise from node_exporter
    (host_metrics: {device pk: {"cores", "mem_total_gb"}}). Overcommit = allocated / capacity, computed only over
    hosts whose capacity is known; `known_hosts` says how many that is."""
    host_metrics = host_metrics or {}
    members = list(members_for(user).filter(cluster=cluster).select_related("device"))
    placements = placements_for(user).filter(cluster=cluster)
    active = placements.exclude(power_state=PowerState.STOPPED)
    alloc = active.aggregate(v=Sum("vcpus"), m=Sum("memory_gb"), d=Sum("disk_gb"))
    by_host = allocation_by_host(user, [m.device_id for m in members])
    cores = mem = 0
    cpu_known = mem_known = 0
    alloc_v_known = 0
    alloc_m_known = Decimal(0)
    for m in members:
        hm = host_metrics.get(m.device_id) or {}
        c = m.cpu_cores or hm.get("cores")
        g = float(m.memory_gb) if m.memory_gb is not None else hm.get("mem_total_gb")
        m.capacity_cores, m.capacity_mem = c, g
        m.capacity_source = "entered" if (m.cpu_cores or m.memory_gb is not None) else ("node_exporter" if (c or g) else "")
        h = by_host.get(m.device_id, {})
        m.alloc = h
        if c:
            cores += c
            cpu_known += 1
            alloc_v_known += h.get("vcpus", 0)
        if g:
            mem += g
            mem_known += 1
            alloc_m_known += h.get("memory_gb", Decimal(0))
    hosts = [m for m in members if m.role in ("host", "worker", "control-plane", "other", "storage")]
    return {
        "members": members, "hosts": len(hosts),
        "vms": placements.count(), "running": placements.filter(power_state=PowerState.RUNNING).count(),
        "vcpus": alloc["v"] or 0, "memory_gb": alloc["m"] or Decimal(0), "disk_gb": alloc["d"] or Decimal(0),
        "cores": cores or None, "mem_total_gb": mem or None, "cpu_known": cpu_known, "mem_known": mem_known,
        "cpu_ratio": ratio(alloc_v_known, cores) if cpu_known else None,
        "mem_ratio": ratio(alloc_m_known, mem) if mem_known else None,
    }


def cluster_rows(user, clusters):
    """Annotate clusters with member / VM / allocation counts in a fixed number of queries."""
    ids = [c.pk for c in clusters]
    mem = {r["cluster_id"]: r["n"] for r in members_for(user).filter(cluster_id__in=ids).order_by()
           .values("cluster_id").annotate(n=Count("id"))}
    pl = {r["cluster_id"]: r for r in placements_for(user).filter(cluster_id__in=ids).order_by().values("cluster_id")
          .annotate(n=Count("id"), running=Count("id", filter=Q(power_state=PowerState.RUNNING)),
                    vcpus=Sum("vcpus", filter=~Q(power_state=PowerState.STOPPED)),
                    memory_gb=Sum("memory_gb", filter=~Q(power_state=PowerState.STOPPED)))}
    for c in clusters:
        p = pl.get(c.pk, {})
        c.member_count = mem.get(c.pk, 0)
        c.vm_count = p.get("n", 0)
        c.running = p.get("running", 0)
        c.vcpus = p.get("vcpus") or 0
        c.memory_gb = p.get("memory_gb") or Decimal(0)
    return clusters


def kind_counts(clusters_qs):
    counts = {k: 0 for k in ClusterKind.values}
    for r in clusters_qs.order_by().values("kind").annotate(n=Count("id")):
        counts[r["kind"]] = r["n"]
    return counts


def csv_safe(value):
    """Neutralise spreadsheet formulas (CSV injection): prefix risky leading characters with a quote."""
    if value is None:
        return ""
    s = str(value)
    if s and s[0] in "=+-@\t\r" and not _is_number(s):
        return "'" + s
    return s


def _is_number(s):
    try:
        float(s)
        return True
    except ValueError:
        return False
