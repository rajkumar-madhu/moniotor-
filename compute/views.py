import csv
from decimal import Decimal

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_http_methods, require_POST

from core import prometheus
from core.access import can, require, scope_by_site
from core.audit import audit
from core.events import emit

from . import jobs, k8s, metrics, services
from .forms import ClusterForm, MemberForm, PlacementForm
from .models import HYPERVISOR_KINDS, Cluster, ClusterKind, K8sState, PowerState, VMPlacement

STATUS_CHOICES = [("up", "Up"), ("down", "Down"), ("unknown", "Unknown")]
POWER_COLORS = {"running": "#0f7a55", "suspended": "#e08a00", "stopped": "#a8a398", "none": "#d6cfc1"}
STATUS_COLORS = {"up": "#0f7a55", "down": "#d92d20", "unknown": "#a8a398"}
PHASE_COLORS = {"Running": "#0f7a55", "Pending": "#e08a00", "Succeeded": "#2b4cff", "Failed": "#d92d20",
                "Unknown": "#a8a398"}
CLUSTER_FIELDS = ["name", "kind", "site", "prometheus_selector", "tags", "description"]
PLACEMENT_FIELDS = ["host_id", "cluster_id", "power_state", "vcpus", "memory_gb", "disk_gb", "os", "notes"]
MEMBER_FIELDS = ["role", "cpu_cores", "memory_gb"]
K8S_PER_PAGE = 10


# ----------------------------------------------------------------------------- helpers
def _per_page(request, default=20):
    try:
        return max(5, min(100, int(request.GET.get("per", default))))
    except (TypeError, ValueError):
        return default


def _query(request, *drop):
    q = request.GET.copy()
    for k in ("page",) + drop:
        q.pop(k, None)
    return q


def _next(request, fallback):
    nxt = request.POST.get("next") or request.GET.get("next") or ""
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return nxt
    return fallback


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _perms(request):
    u = request.user
    return {"can_operate": can(u, "operate"), "can_admin": can(u, "admin")}


def _need(request, perm, site):
    if not services.can_manage(request.user, perm, site):
        raise PermissionDenied(f"You need the {perm} permission on “{site or 'All Sites'}”.")


def _csv(filename, header, rows):
    resp = HttpResponse(content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    w = csv.writer(resp)
    w.writerow(header)
    for r in rows:
        w.writerow([services.csv_safe(v) for v in r])
    return resp


def _num(v):
    if v is None:
        return ""
    if isinstance(v, Decimal):
        return f"{v.normalize():f}"
    return v


def _snapshot(obj, fields):
    return {f: getattr(obj, f) for f in fields}


def _changes(before, obj, fields):
    return {f: [str(before[f]), str(getattr(obj, f))] for f in fields if before[f] != getattr(obj, f)}


def _sites_of(qs, field="site"):
    return sorted({s for s in qs.order_by().values_list(field, flat=True).distinct() if s}, key=str.lower)


def _recent_events(user, limit=8, **filters):
    from core.models import Event
    qs = scope_by_site(Event.objects.filter(tenant_key=services.tenant(), source="compute", **filters), user)
    return qs[:limit]


def _kinds_text(kinds):
    return " · ".join(f"{n} {ClusterKind(k).label}" for k, n in kinds.items() if n) or "None yet"


def _vm_pairs(user):
    """Placements of real VM devices only (a device whose type changed away from VM is ignored)."""
    return services.placements_for(user).filter(vm__device_type=services.VM)


# ----------------------------------------------------------------------------- overview
@require("view")
def overview(request):
    u = request.user
    servers = services.servers_for(u)
    srv = services.status_counts(servers)
    vms_total = services.vms_for(u).count()
    placements = _vm_pairs(u)
    power = services.power_counts(placements)
    placed = sum(power.values())
    no_data = max(0, vms_total - placed)
    clusters = services.clusters_for(u)
    kinds = services.kind_counts(clusters)
    guests = services.guest_counts(u)
    server_ids = set(servers.values_list("pk", flat=True))
    on_servers = {h: n for h, n in guests.items() if h in server_ids}
    hosts_without = len(server_ids - set(on_servers))
    density = (sum(on_servers.values()) / len(on_servers)) if on_servers else None
    alloc = placements.exclude(power_state=PowerState.STOPPED).aggregate(v=Sum("vcpus"), m=Sum("memory_gb"))
    k8s_degraded = clusters.filter(kind=ClusterKind.KUBERNETES, k8s_state=K8sState.WARNING).count()

    names = dict(servers.filter(pk__in=list(on_servers)).values_list("pk", "name"))
    top_hosts = sorted(on_servers.items(), key=lambda kv: (-kv[1], names.get(kv[0], "")))[:10]
    per_host = {"labels": [names.get(h, "?") for h, _n in top_hosts],
                "series": [{"name": "VMs", "color": "#2b4cff", "values": [n for _h, n in top_hosts]}],
                "tipLabels": [names.get(h, "?") for h, _n in top_hosts]}
    power_donut = {"segments": [{"label": PowerState(s).label, "value": power[s], "color": POWER_COLORS[s]}
                                for s in PowerState.values]
                   + [{"label": "No placement data", "value": no_data, "color": POWER_COLORS["none"]}],
                   "center": {"value": str(vms_total), "label": "VMs"}}
    status_donut = {"segments": [{"label": l, "value": srv[s], "color": STATUS_COLORS[s]} for s, l in STATUS_CHOICES],
                    "center": {"value": str(srv["total"]), "label": "Servers"}}
    by_site = list(servers.order_by().values("site").annotate(
        total=Count("id"), up=Count("id", filter=Q(status="up")), down=Count("id", filter=Q(status="down")))
        .order_by("-total")[:8])
    site_names = [r["site"] or "(No site)" for r in by_site]
    site_bars = {"labels": [n if len(n) <= 14 else n[:13] + "…" for n in site_names], "tipLabels": site_names,
                 "series": [{"name": "Up", "color": STATUS_COLORS["up"], "values": [r["up"] for r in by_site]},
                            {"name": "Down", "color": STATUS_COLORS["down"], "values": [r["down"] for r in by_site]},
                            {"name": "Unknown", "color": STATUS_COLORS["unknown"],
                             "values": [r["total"] - r["up"] - r["down"] for r in by_site]}]}
    top_clusters = services.cluster_rows(u, list(clusters.order_by("name")[:8]))
    return render(request, "compute/overview.html", {
        **_perms(request), "srv": srv, "vms_total": vms_total, "power": power, "placed": placed, "no_data": no_data,
        "clusters_total": sum(kinds.values()), "kinds_text": _kinds_text(kinds),
        "hosts_without": hosts_without, "hosts_with": len(on_servers), "density": density,
        "max_density": max(on_servers.values()) if on_servers else None,
        "alloc_vcpus": alloc["v"] or 0, "alloc_mem": alloc["m"] or 0, "k8s_degraded": k8s_degraded,
        "power_donut": power_donut, "status_donut": status_donut, "per_host": per_host, "site_bars": site_bars,
        "has_site_bars": bool(by_site), "top_clusters": top_clusters, "events": _recent_events(u),
        "prometheus_on": prometheus.configured(),
    })


# ----------------------------------------------------------------------------- servers
def _filter_servers(request, qs):
    g = request.GET
    q = g.get("q", "").strip()
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(mgmt_ip__icontains=q) | Q(vendor__icontains=q) | Q(model__icontains=q)
                       | Q(tags__icontains=q) | Q(prometheus_instance__icontains=q))
    if g.get("site"):
        qs = qs.filter(site=g["site"])
    if g.get("status") in dict(STATUS_CHOICES):
        qs = qs.filter(status=g["status"])
    cid = _int(g.get("cluster"))
    if cid:
        qs = qs.filter(pk__in=services.members_for(request.user).filter(cluster_id=cid).values("device_id"))
    if g.get("cluster") == "none":
        qs = qs.exclude(pk__in=services.members_for(request.user).values("device_id"))
    hosting = services.placements_for(request.user).exclude(host=None).values("host_id")
    if g.get("guests") == "with":
        qs = qs.filter(pk__in=hosting)
    elif g.get("guests") == "none":
        qs = qs.exclude(pk__in=hosting)
    if g.get("metrics") == "with":
        qs = qs.exclude(prometheus_instance="")
    elif g.get("metrics") == "none":
        qs = qs.filter(prometheus_instance="")
    return qs


def _decorate_servers(request, devices):
    ids = [d.pk for d in devices]
    by_dev = services.clusters_by_device(request.user, ids)
    alloc = services.allocation_by_host(request.user, ids)
    for d in devices:
        d.memberships = by_dev.get(d.pk, [])
        d.alloc = alloc.get(d.pk, {})
    return devices


@require("view")
def servers(request):
    u = request.user
    base = services.servers_for(u)
    counts = services.status_counts(base)
    qs = _filter_servers(request, base).order_by("name")
    page = Paginator(qs, _per_page(request)).get_page(request.GET.get("page"))
    rows = _decorate_servers(request, list(page))
    live, prom_error, prom_on = metrics.server_metrics(rows)
    for d in rows:
        d.m = live.get(d.pk)
    with_instance = base.exclude(prometheus_instance="").count()
    hosting = services.placements_for(u).exclude(host=None).values("host_id").distinct().count()
    return render(request, "compute/servers.html", {
        **_perms(request), "page": page, "rows": rows, "counts": counts, "with_instance": with_instance,
        "hosting": hosting, "prom_on": prom_on, "prom_error": prom_error, "matched": len(live),
        "sites": _sites_of(base), "statuses": STATUS_CHOICES,
        "clusters": services.clusters_for(u).order_by("name"), "query": _query(request),
    })


@require("view")
def servers_export(request):
    rows = _decorate_servers(request, list(_filter_servers(request, services.servers_for(request.user)).order_by("name")))
    live, _err, _on = metrics.server_metrics(rows)
    out = []
    for d in rows:
        m = live.get(d.pk) or {}
        out.append([d.name, d.mgmt_ip, d.site, d.status, d.vendor, d.model,
                    "; ".join(f"{x.cluster.name} ({x.get_role_display()})" for x in d.memberships),
                    d.alloc.get("vms", 0), d.alloc.get("vcpus", 0), _num(d.alloc.get("memory_gb")),
                    d.prometheus_instance, "" if m.get("cpu") is None else round(m["cpu"], 1),
                    "" if m.get("memory") is None else round(m["memory"], 1),
                    "" if m.get("disk") is None else round(m["disk"], 1)])
    return _csv("compute-servers.csv", ["name", "mgmt_ip", "site", "status", "vendor", "model", "clusters", "vms",
                                        "vcpus_allocated", "memory_gb_allocated", "prometheus_instance", "cpu_pct",
                                        "memory_pct", "disk_pct"], out)


# ----------------------------------------------------------------------------- VMs
def _filter_vms(request, qs):
    g = request.GET
    u = request.user
    q = g.get("q", "").strip()
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(mgmt_ip__icontains=q) | Q(tags__icontains=q)
                       | Q(pk__in=services.placements_for(u).filter(os__icontains=q).values("vm_id")))
    if g.get("site"):
        qs = qs.filter(site=g["site"])
    if g.get("status") in dict(STATUS_CHOICES):
        qs = qs.filter(status=g["status"])
    pl = services.placements_for(u)
    power = g.get("power")
    if power in PowerState.values:
        qs = qs.filter(pk__in=pl.filter(power_state=power).values("vm_id"))
    elif power == "none":
        qs = qs.exclude(pk__in=pl.values("vm_id"))
    host = _int(g.get("host"))
    if host:
        qs = qs.filter(pk__in=pl.filter(host_id=host).values("vm_id"))
    cid = _int(g.get("cluster"))
    if cid:
        qs = qs.filter(pk__in=pl.filter(cluster_id=cid).values("vm_id"))
    return qs


def _placements_map(user, vms):
    return {p.vm_id: p for p in services.placements_for(user).filter(vm_id__in=[v.pk for v in vms])
            .select_related("host", "cluster")}


@require("view")
def vms(request):
    u = request.user
    base = services.vms_for(u)
    total = base.count()
    pl = _vm_pairs(u)
    power = services.power_counts(pl)
    alloc = pl.exclude(power_state=PowerState.STOPPED).aggregate(v=Sum("vcpus"), m=Sum("memory_gb"), d=Sum("disk_gb"))
    qs = _filter_vms(request, base).order_by("name")
    page = Paginator(qs, _per_page(request)).get_page(request.GET.get("page"))
    rows = list(page)
    pmap = _placements_map(u, rows)
    for v in rows:
        v.placement = pmap.get(v.pk)
        v.can_manage = services.can_manage(u, "operate", v.site)
        v.can_delete = v.placement is not None and services.can_manage(u, "admin", v.site)
    return render(request, "compute/vms.html", {
        **_perms(request), "page": page, "rows": rows, "total": total, "power": power,
        "no_data": max(0, total - sum(power.values())), "alloc": alloc, "sites": _sites_of(base),
        "statuses": STATUS_CHOICES, "powers": PowerState.choices,
        "hosts": services.servers_for(u).filter(pk__in=pl.exclude(host=None).values("host_id")).order_by("name"),
        "clusters": services.clusters_for(u).filter(kind__in=HYPERVISOR_KINDS).order_by("name"),
        "query": _query(request),
    })


@require("view")
def vms_export(request):
    rows = list(_filter_vms(request, services.vms_for(request.user)).order_by("name"))
    pmap = _placements_map(request.user, rows)
    out = []
    for v in rows:
        p = pmap.get(v.pk)
        out.append([v.name, v.mgmt_ip, v.site, v.status,
                    p.get_power_state_display() if p else "", p.host.name if p and p.host else "",
                    p.cluster.name if p and p.cluster else "", _num(p.vcpus) if p else "",
                    _num(p.memory_gb) if p else "", _num(p.disk_gb) if p else "", p.os if p else "",
                    p.notes if p else ""])
    return _csv("compute-vms.csv", ["name", "mgmt_ip", "site", "status", "power_state", "host", "cluster", "vcpus",
                                    "memory_gb", "disk_gb", "os", "notes"], out)


@require("operate")
@require_http_methods(["GET", "POST"])
def placement_edit(request, vm_pk):
    u = request.user
    vm = get_object_or_404(services.vms_for(u), pk=vm_pk)
    _need(request, "operate", vm.site)
    placement = VMPlacement.objects.filter(tenant_key=services.tenant(), vm=vm).first()
    before = _snapshot(placement, PLACEMENT_FIELDS) if placement else None
    form = PlacementForm(request.POST or None, instance=placement, vm=vm, user=u)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        obj.vm, obj.tenant_key = vm, services.tenant()
        obj.save()
        if before is None:
            audit(request, "create", obj, vm=vm.name, host=obj.host.name if obj.host else "",
                  cluster=obj.cluster.name if obj.cluster else "", power_state=obj.power_state)
            messages.success(request, f"Saved placement data for “{vm.name}”.")
        else:
            changed = _changes(before, obj, PLACEMENT_FIELDS)
            audit(request, "update", obj, vm=vm.name, changed=changed)
            messages.success(request, f"Saved placement data for “{vm.name}”." if changed else f"No changes for “{vm.name}”.")
            if "power_state" in changed:
                emit(source="compute", title=f"VM {vm.name} is now {obj.get_power_state_display().lower()}",
                     severity="info", message=f"Power state changed from {before['power_state']} to {obj.power_state} "
                                              f"by {u.get_username()}.", site=vm.site, obj=obj,
                     fingerprint=f"compute:vm:{vm.pk}", power_state=obj.power_state)
        return redirect(_next(request, reverse("compute:vms")))
    return render(request, "compute/placement_form.html", {
        **_perms(request), "form": form, "vm": vm, "placement": placement,
        "can_delete": placement is not None and services.can_manage(u, "admin", vm.site),
        "next": _next(request, reverse("compute:vms")),
    }, status=400 if request.method == "POST" else 200)


@require("admin")
@require_POST
def placement_delete(request, pk):
    obj = get_object_or_404(services.placements_for(request.user).select_related("vm"), pk=pk)
    _need(request, "admin", obj.vm.site)
    name = obj.vm.name
    audit(request, "delete", obj, vm=name, host=obj.host.name if obj.host else "",
          cluster=obj.cluster.name if obj.cluster else "")
    obj.delete()
    messages.success(request, f"Removed the placement data of “{name}”.")
    nxt = _next(request, reverse("compute:vms"))
    if "/placement/" in nxt:
        nxt = reverse("compute:vms")
    return redirect(nxt)


# ----------------------------------------------------------------------------- clusters
def _filter_clusters(request, qs):
    g = request.GET
    q = g.get("q", "").strip()
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(description__icontains=q) | Q(tags__icontains=q)
                       | Q(prometheus_selector__icontains=q))
    if g.get("kind") in ClusterKind.values:
        qs = qs.filter(kind=g["kind"])
    if g.get("site"):
        qs = qs.filter(site=g["site"])
    return qs


@require("view")
def clusters(request):
    u = request.user
    base = services.clusters_for(u)
    kinds = services.kind_counts(base)
    qs = _filter_clusters(request, base).order_by("name")
    page = Paginator(qs, _per_page(request)).get_page(request.GET.get("page"))
    rows = services.cluster_rows(u, list(page))
    for c in rows:
        c.can_manage = services.can_manage(u, "operate", c.site)
        c.can_delete = services.can_manage(u, "admin", c.site)
    hosts = services.members_for(u).filter(cluster__kind__in=HYPERVISOR_KINDS).values("device_id").distinct().count()
    in_clusters = _vm_pairs(u).exclude(cluster=None).count()
    return render(request, "compute/clusters.html", {
        **_perms(request), "page": page, "rows": rows, "total": sum(kinds.values()),
        "kinds_text": _kinds_text(kinds), "kind_choices": ClusterKind.choices,
        "hypervisor_hosts": hosts, "vms_in_clusters": in_clusters,
        "k8s_degraded": base.filter(kind=ClusterKind.KUBERNETES, k8s_state=K8sState.WARNING).count(),
        "sites": _sites_of(base), "query": _query(request),
    })


@require("view")
def clusters_export(request):
    rows = services.cluster_rows(request.user, list(_filter_clusters(request, services.clusters_for(request.user)).order_by("name")))
    return _csv("compute-clusters.csv",
                ["name", "kind", "site", "members", "vms", "running", "vcpus_allocated", "memory_gb_allocated",
                 "prometheus_selector", "k8s_state", "tags", "description"],
                [[c.name, c.get_kind_display(), c.site, c.member_count, c.vm_count, c.running, c.vcpus, _num(c.memory_gb),
                  c.prometheus_selector, c.get_k8s_state_display() if c.is_kubernetes else "", c.tags, c.description]
                 for c in rows])


def _cluster(request, pk):
    return get_object_or_404(services.clusters_for(request.user), pk=pk)


def _save_cluster(request, form, existing):
    obj = form.save(commit=False)
    _need(request, "operate", obj.site)
    obj.tenant_key = services.tenant()
    if existing is not None and (existing.prometheus_selector != obj.prometheus_selector or existing.kind != obj.kind):
        obj.k8s_summary, obj.k8s_state, obj.k8s_error, obj.k8s_checked_at = {}, K8sState.UNKNOWN, "", None
    obj.save()
    return obj


@require("operate")
@require_http_methods(["GET", "POST"])
def cluster_create(request):
    initial = {}
    if request.GET.get("kind") in ClusterKind.values:
        initial["kind"] = request.GET["kind"]
    form = ClusterForm(request.POST or None, initial=initial, user=request.user)
    if request.method == "POST" and form.is_valid():
        obj = _save_cluster(request, form, None)
        audit(request, "create", obj, kind=obj.kind, site=obj.site, selector=obj.prometheus_selector)
        messages.success(request, f"Created the cluster “{obj.name}”. Add its hosts below.")
        return redirect(obj.get_absolute_url())
    return render(request, "compute/cluster_form.html", {**_perms(request), "form": form, "cluster": None},
                  status=400 if request.method == "POST" else 200)


@require("operate")
@require_http_methods(["GET", "POST"])
def cluster_edit(request, pk):
    cluster = _cluster(request, pk)
    _need(request, "operate", cluster.site)
    before = _snapshot(cluster, CLUSTER_FIELDS)
    form = ClusterForm(request.POST or None, instance=Cluster.objects.get(pk=cluster.pk), user=request.user)
    if request.method == "POST" and form.is_valid():
        obj = _save_cluster(request, form, cluster)
        changed = _changes(before, obj, CLUSTER_FIELDS)
        audit(request, "update", obj, changed=changed)
        messages.success(request, f"Saved “{obj.name}”." if changed else f"No changes to “{obj.name}”.")
        return redirect(obj.get_absolute_url())
    return render(request, "compute/cluster_form.html", {**_perms(request), "form": form, "cluster": cluster},
                  status=400 if request.method == "POST" else 200)


@require("admin")
@require_POST
def cluster_delete(request, pk):
    cluster = _cluster(request, pk)
    _need(request, "admin", cluster.site)
    name = cluster.name
    members = cluster.members.count()
    placements = cluster.placements.count()
    audit(request, "delete", cluster, name=name, kind=cluster.kind, site=cluster.site, members=members,
          placements_detached=placements)
    cluster.delete()
    messages.success(request, f"Deleted the cluster “{name}”" + (f"; {placements} VM placement(s) no longer name a cluster."
                                                                  if placements else "."))
    return redirect("compute:clusters")


@require("view")
def cluster_detail(request, pk):
    u = request.user
    cluster = _cluster(request, pk)
    members_qs = services.members_for(u).filter(cluster=cluster).select_related("device")
    devices = [m.device for m in members_qs]
    live, prom_error, prom_on = metrics.server_metrics(devices)
    cap = services.cluster_capacity(cluster, u, host_metrics=live)
    for m in cap["members"]:
        m.m = live.get(m.device_id)
    placements = (services.placements_for(u).filter(cluster=cluster).select_related("vm", "host")
                  .order_by("vm__name"))
    page = Paginator(placements, _per_page(request)).get_page(request.GET.get("page"))
    can_op = services.can_manage(u, "operate", cluster.site)
    member_form = MemberForm(cluster=cluster, user=u) if can_op else None
    summary = cluster.k8s_summary if cluster.is_kubernetes else None
    return render(request, "compute/cluster_detail.html", {
        **_perms(request), "cluster": cluster, "cap": cap, "page": page, "query": _query(request),
        "member_form": member_form, "can_manage": can_op,
        "can_delete": services.can_manage(u, "admin", cluster.site),
        "prom_on": prom_on, "prom_error": prom_error, "summary": summary,
        "pod_bars": _pod_bars(summary) if summary else None,
        "events": _recent_events(u, 8, fingerprint=f"compute:k8s:{cluster.pk}") if cluster.is_kubernetes else [],
    })


@require("operate")
@require_POST
def cluster_refresh(request, pk):
    cluster = _cluster(request, pk)
    _need(request, "operate", cluster.site)
    if not cluster.is_kubernetes or not cluster.prometheus_selector:
        messages.error(request, "Only Kubernetes clusters with a Prometheus selector can be collected.")
    elif not prometheus.configured():
        messages.error(request, "Prometheus is not connected (set LINKEDEYE_PROMETHEUS_URL).")
    else:
        state = jobs.refresh_cluster(cluster)
        cluster.refresh_from_db()
        audit(request, "run", cluster, state=state, error=cluster.k8s_error)
        if cluster.k8s_error:
            messages.error(request, f"{cluster.name}: {cluster.k8s_error}")
        else:
            messages.success(request, f"Collected {cluster.name}: {K8sState(state).label}.")
    return redirect(_next(request, cluster.get_absolute_url()))


# ----------------------------------------------------------------------------- members
@require("operate")
@require_http_methods(["GET", "POST"])
def member_add(request, pk):
    cluster = _cluster(request, pk)
    if request.method == "GET":
        return redirect(cluster.get_absolute_url() + "#members")
    _need(request, "operate", cluster.site)
    form = MemberForm(request.POST, cluster=cluster, user=request.user)
    if form.is_valid():
        obj = form.save(commit=False)
        obj.cluster, obj.tenant_key = cluster, services.tenant()
        obj.save()
        audit(request, "create", obj, cluster=cluster.name, device=obj.device.name, role=obj.role)
        messages.success(request, f"Added {obj.device.name} to “{cluster.name}”.")
        return redirect(cluster.get_absolute_url() + "#members")
    errs = "; ".join(f"{' '.join(v)}" for _k, v in form.errors.items())
    messages.error(request, f"Member not added – {errs}")
    return redirect(cluster.get_absolute_url() + "#members")


def _member(request, pk):
    return get_object_or_404(services.members_for(request.user).select_related("cluster", "device"), pk=pk)


@require("operate")
@require_http_methods(["GET", "POST"])
def member_edit(request, pk):
    member = _member(request, pk)
    _need(request, "operate", member.cluster.site)
    before = _snapshot(member, MEMBER_FIELDS)
    form = MemberForm(request.POST or None, instance=member, cluster=member.cluster, user=request.user)
    if request.method == "POST" and form.is_valid():
        obj = form.save()
        changed = _changes(before, obj, MEMBER_FIELDS)
        audit(request, "update", obj, cluster=obj.cluster.name, device=obj.device.name, changed=changed)
        messages.success(request, f"Saved {obj.device.name} in “{obj.cluster.name}”.")
        return redirect(obj.cluster.get_absolute_url() + "#members")
    return render(request, "compute/member_form.html", {**_perms(request), "form": form, "member": member},
                  status=400 if request.method == "POST" else 200)


@require("admin")
@require_POST
def member_delete(request, pk):
    member = _member(request, pk)
    _need(request, "admin", member.cluster.site)
    cluster = member.cluster
    name = member.device.name
    audit(request, "delete", member, cluster=cluster.name, device=name, role=member.role)
    member.delete()
    messages.success(request, f"Removed {name} from “{cluster.name}”.")
    return redirect(cluster.get_absolute_url() + "#members")


# ----------------------------------------------------------------------------- kubernetes
def _pod_bars(summary):
    pods = (summary or {}).get("pods") or {}
    return {"labels": list(k8s.PHASES), "stacked": False,
            "series": [{"name": "Pods", "color": "#0e8a7a", "values": [pods.get(p, 0) for p in k8s.PHASES]}]}


@require("view")
def kubernetes(request):
    u = request.user
    base = services.clusters_for(u).filter(kind=ClusterKind.KUBERNETES)
    total = base.count()
    page = Paginator(base.order_by("name"), _per_page(request, K8S_PER_PAGE)).get_page(request.GET.get("page"))
    prom_on = prometheus.configured()
    rows = list(page)
    totals = {"nodes": 0, "ready": 0, "not_ready": 0, "pods": 0, "running": 0, "pending": 0, "failed": 0,
              "namespaces": 0, "deployments": 0, "unavailable": 0, "live": 0}
    for c in rows:
        c.summary, c.problem = (None, "")
        if prom_on:
            c.summary, c.problem = k8s.live_summary(c)
        c.cached = c.k8s_summary if (c.summary is None and c.k8s_summary) else None
        c.pods_id = f"k8s-pods-{c.pk}"
        c.can_manage = services.can_manage(u, "operate", c.site)
        s = c.summary
        if s:
            totals["live"] += 1
            totals["nodes"] += s["nodes"]["total"]
            totals["ready"] += s["nodes"]["ready"]
            totals["not_ready"] += s["nodes"]["not_ready"]
            totals["pods"] += s["pods_total"]
            totals["running"] += s["pods"].get("Running", 0)
            totals["pending"] += s["pods"].get("Pending", 0)
            totals["failed"] += s["pods"].get("Failed", 0)
            totals["namespaces"] += s["namespaces"]
            totals["deployments"] += s["deployments"]
            totals["unavailable"] += s["unavailable_count"]
            c.pod_bars = _pod_bars(s)
            c.state = k8s.state_of(s)
    return render(request, "compute/kubernetes.html", {
        **_perms(request), "page": page, "rows": rows, "total": total, "prom_on": prom_on, "totals": totals,
        "with_selector": base.exclude(prometheus_selector="").count(), "query": _query(request),
        "required_metrics": k8s.REQUIRED_METRICS, "paged": page.paginator.num_pages > 1,
    })
