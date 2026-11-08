"""Workloads, services and volumes pages.

/kubernetes/workloads/        every container of every pod, with replicas, image, node and the stale-image flag
/kubernetes/workloads/<pk>/   one workload: its containers, ports, image, volumes and services
/kubernetes/services/         service ports with the port -> target port mapping, NodePort and selector
/kubernetes/volumes/          PVC / PV pairs with capacity, status, risk; pending PVCs and unclaimed PVs stand out

Read-only: nothing here changes a record, so the only gate is the ``view`` permission. Every queryset is
tenant-filtered and site-scoped through the cluster the row belongs to, so a site-limited user sees only
their own sites' rows on every page, export and detail view.
"""
from collections import defaultdict

from django.db.models import CharField, Count, F, Q, Sum, Value
from django.db.models.functions import Cast, Concat
from django.shortcuts import get_object_or_404, render

from core.access import require, scope_by_site
from telemetry.views.common import csv_response

from . import services
from .models import Health
from .models_workload import KubeService, PodStatus, Risk, ServiceType, Volume, Workload, WorkloadType
from .parsing import fmt_bytes
from .views import _apply_site, _base, _site_filter, paginate

# colours shared with the rest of the console's charts
C_OK, C_WARN, C_CRIT, C_MUTE, C_BLUE, C_PURPLE, C_TEAL = ("#0f7a55", "#b7791f", "#d92d20", "#a8a398", "#2b4cff",
                                                           "#6d4fd6", "#0e7c86")
WORKLOAD_TYPE_COLORS = {"Deployment": C_BLUE, "DaemonSet": C_PURPLE, "StatefulSet": C_TEAL, "Other": C_MUTE}
POD_STATUS_COLORS = {"running": C_OK, "pending": C_WARN, "failed": C_CRIT, "succeeded": C_BLUE,
                     "no_pod": C_MUTE, "unknown": "#cfc9bb"}
EXPORT_LIMIT = 10000


# ----------------------------------------------------------------------------- querysets
def workloads_for(user):
    qs = Workload.objects.filter(tenant_key=services.tenant())
    return scope_by_site(qs, user, field="cluster__site").select_related("namespace", "cluster", "node", "client")


def services_for(user):
    qs = KubeService.objects.filter(tenant_key=services.tenant())
    return scope_by_site(qs, user, field="cluster__site").select_related("namespace", "cluster", "client")


def volumes_for(user):
    qs = Volume.objects.filter(tenant_key=services.tenant())
    return scope_by_site(qs, user, field="cluster__site").select_related("namespace", "cluster", "client")


def _workload_key():
    """One value per workload (namespace + name): a workload has a row per container and pod."""
    return Concat(Cast("namespace_id", CharField()), Value("/"), "workload_name", output_field=CharField())


def _short_q():
    return Q(desired_replicas__isnull=False, ready_replicas__isnull=False, ready_replicas__lt=F("desired_replicas"))


def _clean_page_params(request):
    return any(request.GET.get(k) for k in request.GET if k not in ("page", "per", "site", "sort"))


# ----------------------------------------------------------------------------- roll-ups
def workload_totals(qs):
    """Counts over a workload queryset. 'workloads' counts namespace+name pairs; 'containers' counts rows."""
    key = _workload_key()
    latest = Q(image_latest=True)
    return qs.order_by().aggregate(
        containers=Count("pk"),
        workloads=Count(key, distinct=True),
        pods=Count("pod_name", filter=~Q(pod_name=""), distinct=True),
        nodes=Count("node_id", distinct=True),
        latest=Count(key, filter=latest, distinct=True),
        short=Count(key, filter=_short_q(), distinct=True),
        unhealthy=Count(key, filter=Q(health__in=[Health.WARN, Health.CRIT]), distinct=True),
        pending=Count(key, filter=Q(pod_status=PodStatus.PENDING), distinct=True),
        failed=Count(key, filter=Q(pod_status=PodStatus.FAILED), distinct=True),
        no_pod=Count(key, filter=Q(pod_status=PodStatus.NO_POD), distinct=True),
    )


def workload_signals(user, site=""):
    """The workload / storage numbers the overview shows (and the Workloads KPIs reuse).

    Returned in the overview's ``extra`` shape so the card can read them like the other counts. A value of
    ``None`` means there is no data (nothing imported), which the card shows as a dash.
    """
    workloads = _apply_site(workloads_for(user), site, field="cluster__site")
    volumes = _apply_site(volumes_for(user), site, field="cluster__site")
    kube_services = _apply_site(services_for(user), site, field="cluster__site")
    out = {}
    if workloads.exists():
        totals = workload_totals(workloads)
        out["workloads"] = {"value": totals["workloads"], "source": "records", "sheet": "04_Application_Inventory"}
        out["stale_images"] = {"value": totals["latest"], "source": "records", "sheet": "04_Application_Inventory",
                               "short": totals["short"], "pending": totals["pending"]}
    if kube_services.exists():
        out["services"] = {"value": kube_services.order_by().values("namespace_id", "name").distinct().count(),
                           "source": "records", "sheet": "05_Service_Inventory"}
    if volumes.exists():
        agg = volumes.order_by().aggregate(n=Count("pk"), capacity=Sum("capacity_bytes"),
                                           pending=Count("pk", filter=Q(pvc_status__iexact="pending")))
        out["volumes"] = {"value": agg["n"], "source": "records", "sheet": "07_PV_PVC_Storage",
                          "capacity": agg["capacity"], "pending": agg["pending"]}
    return out


def _donut(counts, colors, labels, center_label):
    segments = [{"label": labels[k], "value": counts[k], "color": colors.get(k, C_MUTE)}
                for k in labels if counts.get(k)]
    total = sum(s["value"] for s in segments)
    return {"segments": segments, "center": {"value": str(total), "label": center_label}}


# ----------------------------------------------------------------------------- workloads
WORKLOAD_SORTS = {"name": ("workload_name", "namespace__name"), "namespace": ("namespace__name", "workload_name"),
                  "health": ("-health", "namespace__name", "workload_name"),
                  "replicas": ("ready_replicas", "namespace__name", "workload_name"),
                  "image": ("image_registry", "image", "workload_name"),
                  "node": ("node_name", "namespace__name", "workload_name")}
WORKLOAD_SIGNALS = [("latest", "Stale image (latest)"), ("short", "Replicas short"),
                    ("unhealthy", "Warning health"), ("no_pod", "No pod"), ("unlinked", "Node not linked")]


def _workloads_queryset(request, base=None):
    user = request.user
    qs = base if base is not None else _apply_site(workloads_for(user), _site_filter(request, user),
                                                   field="cluster__site")
    params = request.GET
    q = (params.get("q") or "").strip()
    if q:
        qs = qs.filter(Q(workload_name__icontains=q) | Q(application__icontains=q) | Q(container__icontains=q)
                       | Q(pod_name__icontains=q) | Q(image__icontains=q) | Q(namespace__name__icontains=q)
                       | Q(client_label__icontains=q) | Q(node_name__icontains=q))
    client = (params.get("client") or "").strip()
    if client:
        qs = qs.filter(client_label__iexact=client)
    namespace = (params.get("namespace") or "").strip()
    if namespace:
        qs = qs.filter(namespace__name=namespace)
    wtype = params.get("type") or ""
    if wtype in WorkloadType.values:
        qs = qs.filter(workload_type=wtype)
    health = params.get("health") or ""
    if health in Health.values:
        qs = qs.filter(health=health)
    registry = (params.get("registry") or "").strip()
    if registry:
        qs = qs.filter(image_registry__iexact=registry)
    pod = params.get("pod") or ""
    if pod in PodStatus.values:
        qs = qs.filter(pod_status=pod)
    node = params.get("node") or ""
    if node.isdigit():
        qs = qs.filter(node_id=int(node))
    signal = params.get("signal") or ""
    if signal == "latest":
        qs = qs.filter(image_latest=True)
    elif signal == "short":
        qs = qs.filter(_short_q())
    elif signal == "unhealthy":
        qs = qs.filter(health__in=[Health.WARN, Health.CRIT])
    elif signal == "no_pod":
        qs = qs.filter(pod_status=PodStatus.NO_POD)
    elif signal == "unlinked":
        qs = qs.filter(node__isnull=True).exclude(node_name="")
    order = WORKLOAD_SORTS.get(params.get("sort") or "", WORKLOAD_SORTS["namespace"])
    return qs.order_by(*order, "container", "pod_name", "pk")


def _service_matches(rows):
    """{workload row pk: (count, search text)} — services of the same namespace that carry the workload's
    name or its application name. Nothing is guessed from selectors: the sheets carry no pod labels."""
    ns_ids = {w.namespace_id for w in rows}
    if not ns_ids:
        return {}
    index = defaultdict(dict)          # namespace id -> {service name: application}
    for ns_id, name, app in KubeService.objects.filter(tenant_key=services.tenant(), namespace_id__in=ns_ids) \
            .order_by().values_list("namespace_id", "name", "application").distinct():
        index[ns_id][name] = app
    out = {}
    for w in rows:
        names = {w.workload_name} | ({w.application} if w.application else set())
        hits = [n for n, app in index.get(w.namespace_id, {}).items() if n in names or (app and app in names)]
        if hits:
            out[w.pk] = (len(hits), w.workload_name if any(n == w.workload_name or index[w.namespace_id][n]
                                                          == w.workload_name for n in hits) else w.application)
    return out


@require("view")
def workloads(request):
    user = request.user
    visible = _apply_site(workloads_for(user), _site_filter(request, user), field="cluster__site")
    qs = _workloads_queryset(request, visible)
    page = paginate(request, qs)
    rows = list(page.object_list)
    matches = _service_matches(rows)
    for row in rows:
        row.service_count, row.service_q = matches.get(row.pk, (0, ""))
    totals = workload_totals(qs)

    by_type = {r["workload_type"]: r["n"] for r in qs.order_by().values("workload_type")
               .annotate(n=Count(_workload_key(), distinct=True))}
    pods = defaultdict(int)
    for r in qs.order_by().exclude(pod_name="").values("pod_status").annotate(n=Count("pod_name", distinct=True)):
        pods[r["pod_status"]] += r["n"]
    for r in qs.order_by().filter(pod_name="").values("pod_status").annotate(n=Count("pk")):
        pods[r["pod_status"]] += r["n"]
    type_donut = _donut(by_type, WORKLOAD_TYPE_COLORS, {v: l for v, l in WorkloadType.choices}, "Workloads")
    pod_donut = _donut(pods, POD_STATUS_COLORS, {v: l for v, l in PodStatus.choices}, "Pods")
    pod_donut["center"]["value"] = str(sum(s["value"] for s in pod_donut["segments"]))

    def distinct(field):
        return sorted({v for v in visible.order_by().values_list(field, flat=True).distinct() if v},
                      key=str.lower)

    ctx = _base(request, "workloads",
                page=page, items=rows, total=qs.count(), totals=totals,
                type_donut=type_donut, pod_donut=pod_donut,
                types=WorkloadType.choices, healths=Health.choices, pod_statuses=PodStatus.choices,
                signals=WORKLOAD_SIGNALS,
                client_options=distinct("client_label"), namespace_options=distinct("namespace__name"),
                registry_options=distinct("image_registry"),
                node_filter=_node_for_filter(request, user),
                filtered=_clean_page_params(request))
    return render(request, "k8s/workloads.html", ctx)


def _node_for_filter(request, user):
    node = request.GET.get("node") or ""
    if not node.isdigit():
        return None
    return services.nodes_for(user).filter(pk=int(node)).first()


WORKLOAD_CSV = ["Cluster", "Client", "Environment", "Namespace", "Application", "Type", "Workload", "Container",
                "Pod", "Pod status", "Pod IP", "Desired replicas", "Ready replicas", "Health", "Node",
                "Image", "Registry", "Tag", "Digest", "Latest tag", "Ports", "URL", "Storage (PVCs)", "Site",
                "Last seen", "Missing since"]


def _opt(value):
    return "" if value is None else value


def _workload_row(w):
    return [w.cluster.name, w.client.name if w.client_id else w.client_label, w.environment, w.namespace.name,
            w.application, w.get_workload_type_display(), w.workload_name, w.container, w.pod_name,
            w.get_pod_status_display(), w.pod_ip or "", _opt(w.desired_replicas), _opt(w.ready_replicas),
            w.get_health_display(), w.node.hostname if w.node_id else w.node_name, w.image, w.image_registry,
            w.image_tag, w.image_digest, "yes" if w.image_latest else "no", ", ".join(w.ports_display),
            w.url, ", ".join(w.pvc_names), w.site, w.last_seen.isoformat() if w.last_seen else "",
            w.missing_since.isoformat() if w.missing_since else ""]


@require("view")
def workloads_export(request):
    qs = _workloads_queryset(request)
    return csv_response("kubernetes-workloads.csv", WORKLOAD_CSV, (_workload_row(w) for w in qs[:EXPORT_LIMIT]))


@require("view")
def workload_detail(request, pk):
    user = request.user
    row = get_object_or_404(workloads_for(user), pk=pk)
    siblings = list(workloads_for(user).filter(namespace_id=row.namespace_id, workload_name=row.workload_name)
                    .order_by("container", "pod_name", "pk")[:300])
    pvc_names = []
    for sibling in siblings:
        for name in sibling.pvc_names:
            if name not in pvc_names:
                pvc_names.append(name)
    ref = f"{row.workload_type}/{row.workload_name}"
    candidates = volumes_for(user).filter(Q(namespace_id=row.namespace_id),
                                          Q(pvc__in=pvc_names) | Q(workload_ref__icontains=row.workload_name))
    vols = [v for v in candidates.order_by("pvc", "pv")
            if v.pvc in pvc_names or ref in v.workload_refs or f"{row.get_workload_type_display()}/{row.workload_name}"
            in v.workload_refs]
    names = {row.workload_name}
    if row.application:
        names.add(row.application)
    svc_qs = services_for(user).filter(namespace_id=row.namespace_id).filter(Q(name__in=names) | Q(application__in=names))
    svcs = list(svc_qs.order_by("name", "port")[:200])

    containers = defaultdict(list)
    for s in siblings:
        containers[s.container or "(unnamed)"].append(s)
    container_rows = []
    for name, group in containers.items():
        first = group[0]
        ports = []
        for g in group:
            for label in g.ports_display:
                if label not in ports:
                    ports.append(label)
        container_rows.append({"name": name, "row": first, "ports": ports, "pods": [g for g in group if g.pod_name],
                               "pod_count": len([g for g in group if g.pod_name]), "images": {g.image for g in group if g.image}})
    pod_rows = [s for s in siblings if s.pod_name]
    ctx = _base(request, "workloads",
                w=row, siblings=siblings, containers=container_rows, pods=pod_rows, volumes=vols,
                missing_pvcs=[n for n in pvc_names if n not in {v.pvc for v in vols}],
                service_rows=svcs, any_latest=any(s.image_latest for s in siblings),
                total_desired=row.desired_replicas, total_ready=row.ready_replicas)
    return render(request, "k8s/workload_detail.html", ctx)


# ----------------------------------------------------------------------------- services
SERVICE_SORTS = {"name": ("name", "namespace__name", "port"), "namespace": ("namespace__name", "name", "port"),
                 "type": ("service_type", "namespace__name", "name", "port"),
                 "nodeport": ("-node_port", "namespace__name", "name")}


def _services_queryset(request, base=None):
    user = request.user
    qs = base if base is not None else _apply_site(services_for(user), _site_filter(request, user),
                                                   field="cluster__site")
    params = request.GET
    q = (params.get("q") or "").strip()
    if q:
        match = (Q(name__icontains=q) | Q(application__icontains=q) | Q(namespace__name__icontains=q)
                 | Q(client_label__icontains=q) | Q(target_port__icontains=q))
        if q.isdigit() and int(q) <= 65535:
            match |= Q(port=int(q)) | Q(node_port=int(q))
        qs = qs.filter(match)
    stype = params.get("type") or ""
    if stype in ServiceType.values:
        qs = qs.filter(service_type=stype)
    namespace = (params.get("namespace") or "").strip()
    if namespace:
        qs = qs.filter(namespace__name=namespace)
    client = (params.get("client") or "").strip()
    if client:
        qs = qs.filter(client_label__iexact=client)
    if params.get("exposed") == "1":
        qs = qs.exclude(service_type=ServiceType.CLUSTER_IP)
    if params.get("headless") == "1":
        qs = qs.filter(headless=True)
    order = SERVICE_SORTS.get(params.get("sort") or "", SERVICE_SORTS["namespace"])
    return qs.order_by(*order, "protocol", "pk")


def service_totals(qs):
    key = Concat(Cast("namespace_id", CharField()), Value("/"), "name", output_field=CharField())
    return qs.order_by().aggregate(
        ports=Count("pk"),
        services=Count(key, distinct=True),
        node_port=Count(key, filter=Q(service_type=ServiceType.NODE_PORT), distinct=True),
        load_balancer=Count(key, filter=Q(service_type=ServiceType.LOAD_BALANCER), distinct=True),
        external_name=Count(key, filter=Q(service_type=ServiceType.EXTERNAL_NAME), distinct=True),
        headless=Count(key, filter=Q(headless=True), distinct=True),
        no_selector=Count(key, filter=Q(selector={}), distinct=True),
    )


@require("view")
def services_list(request):
    user = request.user
    visible = _apply_site(services_for(user), _site_filter(request, user), field="cluster__site")
    qs = _services_queryset(request, visible)
    page = paginate(request, qs)

    def distinct(field):
        return sorted({v for v in visible.order_by().values_list(field, flat=True).distinct() if v},
                      key=str.lower)

    totals = service_totals(qs)
    types = {r["service_type"]: r["n"] for r in qs.order_by().values("service_type").annotate(n=Count("pk"))}
    ctx = _base(request, "services",
                page=page, items=page.object_list, total=qs.count(), totals=totals,
                types=ServiceType.choices,
                type_counts=[(label, types.get(value, 0)) for value, label in ServiceType.choices
                             if types.get(value)],
                client_options=distinct("client_label"), namespace_options=distinct("namespace__name"),
                filtered=_clean_page_params(request))
    return render(request, "k8s/services.html", ctx)


SERVICE_CSV = ["Cluster", "Client", "Environment", "Namespace", "Application", "Service", "Type", "Cluster IP",
               "Headless", "External IP", "LoadBalancer IP", "NodePort", "Port", "Target port", "Protocol",
               "Selector", "Status", "Site", "Last seen", "Missing since"]


def _service_row(s):
    return [s.cluster.name, s.client.name if s.client_id else s.client_label, s.environment, s.namespace.name,
            s.application, s.name, s.get_service_type_display(), s.cluster_ip or "",
            "yes" if s.headless else "no", s.external_ip, s.loadbalancer_ip, _opt(s.node_port), _opt(s.port),
            s.target_port, s.protocol, s.selector_text, s.status, s.site,
            s.last_seen.isoformat() if s.last_seen else "", s.missing_since.isoformat() if s.missing_since else ""]


@require("view")
def services_export(request):
    qs = _services_queryset(request)
    return csv_response("kubernetes-services.csv", SERVICE_CSV, (_service_row(s) for s in qs[:EXPORT_LIMIT]))


# ----------------------------------------------------------------------------- volumes
VOLUME_STATUSES = [("bound", "Bound"), ("pending", "Pending PVC"), ("lost", "Lost"),
                   ("unclaimed", "Unclaimed PV")]
VOLUME_SORTS = {"name": ("pvc", "pv"), "namespace": ("namespace__name", "pvc", "pv"),
                "capacity": ("-capacity_bytes", "namespace__name", "pvc"),
                "risk": ("-risk", "namespace__name", "pvc"), "age": ("-age_days", "namespace__name", "pvc")}


def _unclaimed_q():
    return Q(pv__gt="") & (Q(pvc="") | Q(pv_status__iexact="available") | Q(pv_status__iexact="released"))


def _volumes_queryset(request, base=None):
    user = request.user
    qs = base if base is not None else _apply_site(volumes_for(user), _site_filter(request, user),
                                                   field="cluster__site")
    params = request.GET
    q = (params.get("q") or "").strip()
    if q:
        qs = qs.filter(Q(pvc__icontains=q) | Q(pv__icontains=q) | Q(application__icontains=q)
                       | Q(namespace__name__icontains=q) | Q(client_label__icontains=q)
                       | Q(storage_class__icontains=q) | Q(workload_ref__icontains=q) | Q(pod__icontains=q)
                       | Q(mount_path__icontains=q))
    status = params.get("status") or ""
    if status == "bound":
        qs = qs.filter(pvc_status__iexact="bound")
    elif status == "pending":
        qs = qs.filter(pvc_status__iexact="pending")
    elif status == "lost":
        qs = qs.filter(Q(pvc_status__iexact="lost") | Q(pv_status__iexact="failed"))
    elif status == "unclaimed":
        qs = qs.filter(_unclaimed_q())
    storage_class = (params.get("class") or "").strip()
    if storage_class:
        qs = qs.filter(storage_class=storage_class)
    risk = params.get("risk") or ""
    if risk in Risk.values:
        qs = qs.filter(risk=risk)
    client = (params.get("client") or "").strip()
    if client:
        qs = qs.filter(client_label__iexact=client)
    namespace = (params.get("namespace") or "").strip()
    if namespace:
        qs = qs.filter(namespace__name=namespace)
    order = VOLUME_SORTS.get(params.get("sort") or "", VOLUME_SORTS["namespace"])
    return qs.order_by(*order, "pk")


def volume_totals(qs):
    return qs.order_by().aggregate(
        volumes=Count("pk"),
        capacity=Sum("capacity_bytes"),
        requested=Sum("requested_bytes"),
        pending=Count("pk", filter=Q(pvc_status__iexact="pending")),
        unclaimed=Count("pk", filter=_unclaimed_q()),
        high=Count("pk", filter=Q(risk=Risk.HIGH)),
        no_capacity=Count("pk", filter=Q(capacity_bytes__isnull=True)),
    )


GIB = 1024 ** 3


@require("view")
def volumes(request):
    user = request.user
    visible = _apply_site(volumes_for(user), _site_filter(request, user), field="cluster__site")
    qs = _volumes_queryset(request, visible)
    page = paginate(request, qs)
    totals = volume_totals(qs)

    by_client = list(qs.order_by().values("client_label").annotate(total=Sum("capacity_bytes"), n=Count("pk"))
                     .order_by("-total", "client_label")[:10])
    names = [r["client_label"] or "(no client)" for r in by_client]
    storage_bars = {"labels": [n if len(n) <= 14 else n[:13] + "…" for n in names], "tipLabels": names,
                    "unit": " GiB",
                    "series": [{"name": "Capacity", "color": C_BLUE,
                                "values": [round((r["total"] or 0) / GIB, 1) for r in by_client]}]}

    def distinct(field):
        return sorted({v for v in visible.order_by().values_list(field, flat=True).distinct() if v},
                      key=str.lower)

    ctx = _base(request, "volumes",
                page=page, items=page.object_list, total=qs.count(), totals=totals,
                capacity_label=fmt_bytes(totals["capacity"]) if totals["capacity"] else "",
                requested_label=fmt_bytes(totals["requested"]) if totals["requested"] else "",
                statuses=VOLUME_STATUSES, risks=Risk.choices, storage_bars=storage_bars,
                has_bars=any(r["total"] for r in by_client),
                class_options=distinct("storage_class"), client_options=distinct("client_label"),
                namespace_options=distinct("namespace__name"), filtered=_clean_page_params(request))
    return render(request, "k8s/volumes.html", ctx)


VOLUME_CSV = ["Cluster", "Client", "Environment", "Namespace", "Application", "PVC", "PVC status", "PV",
              "PV status", "Capacity", "Capacity (bytes)", "Requested", "Requested (bytes)", "Access mode",
              "Storage class", "Reclaim policy", "Volume mode", "Provisioner", "Workloads", "Pod", "Mount path",
              "Age (days)", "Risk", "Remarks", "Site", "Last seen", "Missing since"]


def _volume_row(v):
    return [v.cluster.name, v.client.name if v.client_id else v.client_label, v.environment,
            v.namespace.name if v.namespace_id else "", v.application, v.pvc, v.pvc_status, v.pv, v.pv_status,
            v.capacity_raw, _opt(v.capacity_bytes), v.requested_raw, _opt(v.requested_bytes), v.access_mode,
            v.storage_class, v.reclaim_policy, v.volume_mode, v.provisioner, v.workload_ref, v.pod,
            v.mount_path, _opt(v.age_days), v.get_risk_display(), v.remarks, v.site,
            v.last_seen.isoformat() if v.last_seen else "", v.missing_since.isoformat() if v.missing_since else ""]


@require("view")
def volumes_export(request):
    qs = _volumes_queryset(request)
    return csv_response("kubernetes-volumes.csv", VOLUME_CSV, (_volume_row(v) for v in qs[:EXPORT_LIMIT]))
