"""Kubernetes inventory pages.

/kubernetes/              overview: KPIs and the per-client health roll-up
/kubernetes/clients/      one row per workbook client, with its counts and links
/kubernetes/namespaces/   the namespace → client mapping, with confidence and review flags
/kubernetes/nodes/        every node column, filters, and a detail page per node
/kubernetes/import/       upload a workbook: dry-run diff first, then confirm (admins on all sites)
/kubernetes/imports/      import history

Every queryset is site-scoped through k8s.services; every mutation is POST + CSRF + permission check +
audit. Pages show what was imported and nothing else — a placeholder cell in the workbook means "no
value" and is shown as a dash, never as data.
"""
import logging
import os
import secrets
import tempfile
import time
from pathlib import Path

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.access import can, require
from core.audit import audit
from core.events import emit
from telemetry.views.common import csv_response

from . import services
from .forms import ACCEPTED_SUFFIXES, MAX_UPLOAD_BYTES, ProfileLinkForm, UploadForm, site_choices
from .importer import ImportStatus, run_import
from .models import Confidence, Exposure, Health, MappingStatus, NodeRole
from .parsing import fmt_bytes
from .tabs import enabled_tabs, pending_tabs

log = logging.getLogger("linkedeye.k8s")

STAGING_DIR = Path(tempfile.gettempdir()) / "linkedeye-k8s-imports"
STAGING_MAX_AGE = 30 * 60      # a staged upload waiting for its confirm button
SESSION_KEY = "k8s_pending_import"


# ----------------------------------------------------------------------------- helpers
def _per_page(request, default=20):
    try:
        return max(5, min(100, int(request.GET.get("per", default))))
    except (TypeError, ValueError):
        return default


def paginate(request, items, default=20):
    return Paginator(items, _per_page(request, default)).get_page(request.GET.get("page"))


def _query(request, *drop):
    q = request.GET.copy()
    for key in ("page",) + drop:
        q.pop(key, None)
    return q


def _perms(request):
    user = request.user
    return {"can_operate": can(user, "operate"), "can_admin": can(user, "admin"),
            "can_import": services.can_import(user)}


def _site_filter(request, user):
    """The ?site= filter, ignored when it names a site the user cannot see."""
    site = (request.GET.get("site") or "").strip()
    if not site:
        return ""
    names = services.visible_site_names(user)
    return site if names is None or site in names else ""


def _base(request, tab, **extra):
    user = request.user
    run = services.last_run(user)
    return {
        **_perms(request),
        "tab": tab,
        "tabs": enabled_tabs(),
        "pending": pending_tabs(),
        "sites": services.site_names(user),
        "site_filter": _site_filter(request, user),
        "last_run": run,
        "query": _query(request),
        **extra,
    }


def _apply_site(qs, site, field="site"):
    return qs.filter(**{field: site}) if site else qs


def _need_import(request):
    if not services.can_import(request.user):
        raise PermissionDenied("Importing an inventory workbook needs the admin role on every site.")


def _workload_model():
    """The workload model, once the application-inventory sheet has a page — else None.

    The node sheet carries no client column: the workload sheet is what ties a client to a node, so the
    nodes page offers a client filter only once those records exist.
    """
    from django.apps import apps
    try:
        Workload = apps.get_model("k8s", "Workload")
    except LookupError:
        return None
    names = {f.name for f in Workload._meta.get_fields()}
    if "node" not in names or not ({"client_label", "namespace"} & names):
        return None
    return Workload


def _client_node_hostnames(user, client_label):
    """Host names the workload sheet attributes to a client, or None when that sheet has no model yet."""
    Workload = _workload_model()
    if Workload is None or not client_label:
        return None
    names = {f.name for f in Workload._meta.get_fields()}
    qs = Workload._default_manager.filter(tenant_key=services.tenant())
    qs = (qs.filter(client_label__iexact=client_label) if "client_label" in names
          else qs.filter(namespace__client_label__iexact=client_label))
    try:
        return {h for h in qs.values_list("node", flat=True) if h}
    except Exception:  # pragma: no cover - a half-built model must not break the page
        return None


# ----------------------------------------------------------------------------- overview
@require("view")
def overview(request):
    user = request.user
    site = _site_filter(request, user)
    clusters = _apply_site(services.clusters_for(user), site)
    nodes = _apply_site(services.nodes_for(user), site, field="cluster__site")
    namespaces = _apply_site(services.namespaces_for(user), site)
    profiles = _apply_site(services.profiles_for(user), site)
    run = services.last_run(user)

    node_stats = services.node_totals(nodes)
    ns_stats = services.namespace_totals(namespaces)
    profile_stats = services.profile_totals(profiles)
    extra = services.optional_counts(user, run)
    rollup = services.client_rollup(user)
    if site:
        rollup = [r for r in rollup if r["profile"].site == site]

    cluster_rows = []
    for cluster in clusters.order_by("name"):
        own = nodes.filter(cluster=cluster)
        cluster_rows.append({"cluster": cluster, "nodes": own.count(),
                              "namespaces": namespaces.filter(cluster=cluster).count(),
                              "totals": services.node_totals(own)})

    ctx = _base(request, "overview",
                clusters=cluster_rows,
                cluster_count=len(cluster_rows),
                nodes=node_stats,
                namespaces=ns_stats,
                profiles=profile_stats,
                extra=extra,
                storage_label=fmt_bytes(profile_stats["storage"]) if profile_stats["storage"] else "",
                memory_label=fmt_bytes(node_stats["memory"]) if node_stats["memory"] else "",
                rollup=rollup,
                unlinked=services.unlinked_profiles(user).count(),
                imported=bool(run))
    return render(request, "k8s/overview.html", ctx)


# ----------------------------------------------------------------------------- nodes
NODE_SORTS = {"hostname": "hostname", "role": "role", "age": "-age_days", "cpu": "-cpu_capacity_millicores",
              "memory": "-memory_capacity_bytes"}


def _nodes_queryset(request):
    user = request.user
    site = _site_filter(request, user)
    qs = _apply_site(services.nodes_for(user), site, field="cluster__site")
    params = request.GET
    q = (params.get("q") or "").strip()
    if q:
        qs = qs.filter(Q(hostname__icontains=q) | Q(os__icontains=q) | Q(kernel__icontains=q)
                       | Q(container_runtime__icontains=q) | Q(kubernetes_version__icontains=q))
    role = params.get("role") or ""
    if role in NodeRole.values:
        qs = qs.filter(role=role)
    exposure = params.get("exposure") or ""
    if exposure in Exposure.values:
        qs = qs.filter(exposure=exposure)
    cluster = params.get("cluster") or ""
    if cluster.isdigit():
        qs = qs.filter(cluster_id=int(cluster))
    state = params.get("state") or ""
    if state == "not_ready":
        qs = qs.filter(ready=False)
    elif state == "unschedulable":
        qs = qs.filter(schedulable=False)
    elif state == "missing":
        qs = qs.filter(missing_since__isnull=False)
    elif state == "linked":
        qs = qs.filter(ci__isnull=False)
    elif state == "unlinked":
        qs = qs.filter(ci__isnull=True)
    client = (params.get("client") or "").strip()
    hostnames = _client_node_hostnames(user, client) if client else None
    if client and hostnames is not None:
        qs = qs.filter(hostname__in=hostnames)
    sort = NODE_SORTS.get(params.get("sort") or "", None)
    return qs.order_by(sort) if sort else qs


@require("view")
def nodes(request):
    user = request.user
    qs = _nodes_queryset(request)
    page = paginate(request, qs)
    ctx = _base(request, "nodes",
                page=page,
                items=page.object_list,
                total=qs.count(),
                totals=services.node_totals(qs),
                roles=NodeRole.choices,
                exposures=Exposure.choices,
                clusters=services.clusters_for(user).order_by("name"),
                client_filter_available=_workload_model() is not None,
                filtered=any(request.GET.get(k) for k in ("q", "role", "exposure", "cluster", "state")))
    return render(request, "k8s/nodes.html", ctx)


NODE_CSV = ["Cluster", "Hostname", "Role", "Internal IP", "External IP", "Exposure", "Kubernetes version",
            "OS", "Kernel", "Container runtime", "Architecture", "CPU capacity", "CPU allocatable",
            "Memory capacity", "Memory allocatable", "Memory capacity (bytes)", "Pod capacity", "Ready",
            "Schedulable", "Age (days)", "Labels", "Taints", "Configuration item", "Device", "Site",
            "Last seen", "Missing since"]


def _node_row(node):
    return [node.cluster.name, node.hostname, node.get_role_display(), node.internal_ip or "",
            node.external_ip or "", node.get_exposure_display(), node.kubernetes_version, node.os,
            node.kernel, node.container_runtime, node.architecture, node.cpu_capacity_raw,
            node.cpu_allocatable_raw, node.memory_capacity_raw, node.memory_allocatable_raw,
            node.memory_capacity_bytes if node.memory_capacity_bytes is not None else "",
            node.pod_capacity if node.pod_capacity is not None else "",
            "" if node.ready is None else ("yes" if node.ready else "no"),
            "" if node.schedulable is None else ("yes" if node.schedulable else "no"),
            node.age_days if node.age_days is not None else "",
            "; ".join(f"{k}={v}" for k, v in node.label_pairs),
            "; ".join(f"{t.get('key', '')}={t.get('value', '')}:{t.get('effect', '')}" for t in node.taints),
            node.ci.name if node.ci_id else "", node.device.name if node.device_id else "", node.site,
            node.last_seen.isoformat() if node.last_seen else "",
            node.missing_since.isoformat() if node.missing_since else ""]


@require("view")
def nodes_export(request):
    qs = _nodes_queryset(request)
    return csv_response("kubernetes-nodes.csv", NODE_CSV, (_node_row(n) for n in qs[:10000]))


@require("view")
def node_detail(request, pk):
    node = get_object_or_404(services.nodes_for(request.user), pk=pk)
    ctx = _base(request, "nodes",
                node=node,
                capacity=[
                    ("CPU", node.cpu_capacity_raw, node.cpu_allocatable_raw,
                     f"{node.cpu_cores:g} cores" if node.cpu_cores is not None else ""),
                    ("Memory", node.memory_capacity_raw, node.memory_allocatable_raw,
                     fmt_bytes(node.memory_capacity_bytes)),
                    ("Pods", node.pod_capacity_raw, "", str(node.pod_capacity or "")),
                ],
                namespaces=services.namespaces_for(request.user).filter(cluster=node.cluster).count())
    return render(request, "k8s/node_detail.html", ctx)


# ----------------------------------------------------------------------------- namespaces
def _namespaces_queryset(request):
    user = request.user
    qs = _apply_site(services.namespaces_for(user), _site_filter(request, user))
    params = request.GET
    q = (params.get("q") or "").strip()
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(client_label__icontains=q) | Q(evidence__icontains=q))
    status = params.get("status") or ""
    if status in MappingStatus.values:
        qs = qs.filter(mapping_status=status)
    confidence = params.get("confidence") or ""
    if confidence in Confidence.values:
        qs = qs.filter(confidence=confidence)
    client = (params.get("client") or "").strip()
    if client:
        qs = qs.filter(client_label__iexact=client)
    environment = (params.get("environment") or "").strip()
    if environment:
        qs = qs.filter(environment__iexact=environment)
    cluster = params.get("cluster") or ""
    if cluster.isdigit():
        qs = qs.filter(cluster_id=int(cluster))
    return qs.order_by("name")


@require("view")
def namespaces(request):
    user = request.user
    qs = _namespaces_queryset(request)
    page = paginate(request, qs)
    all_namespaces = services.namespaces_for(user)
    ctx = _base(request, "namespaces",
                page=page,
                items=page.object_list,
                total=qs.count(),
                totals=services.namespace_totals(qs),
                statuses=MappingStatus.choices,
                confidences=Confidence.choices,
                clusters=services.clusters_for(user).order_by("name"),
                clients=sorted({c for c in all_namespaces.order_by().values_list("client_label", flat=True)
                                if c}, key=str.lower),
                environments=sorted({e for e in all_namespaces.order_by()
                                     .values_list("environment", flat=True) if e}, key=str.lower),
                filtered=any(request.GET.get(k) for k in
                             ("q", "status", "confidence", "client", "environment", "cluster")))
    return render(request, "k8s/namespaces.html", ctx)


NAMESPACE_CSV = ["Cluster", "Namespace", "Client (workbook)", "LinkedEye client", "Environment",
                 "Confidence", "Mapping status", "Evidence", "Site", "Last seen", "Missing since"]


def _namespace_row(ns):
    return [ns.cluster.name, ns.name, ns.client_label, ns.client.name if ns.client_id else "",
            ns.environment, ns.get_confidence_display(), ns.get_mapping_status_display(), ns.evidence,
            ns.site, ns.last_seen.isoformat() if ns.last_seen else "",
            ns.missing_since.isoformat() if ns.missing_since else ""]


@require("view")
def namespaces_export(request):
    qs = _namespaces_queryset(request)
    return csv_response("kubernetes-namespaces.csv", NAMESPACE_CSV, (_namespace_row(n) for n in qs[:10000]))


# ----------------------------------------------------------------------------- clients
def _clients_queryset(request):
    user = request.user
    qs = _apply_site(services.profiles_for(user), _site_filter(request, user))
    params = request.GET
    q = (params.get("q") or "").strip()
    if q:
        qs = qs.filter(Q(workbook_name__icontains=q) | Q(workbook_id__icontains=q)
                       | Q(primary_namespace__icontains=q))
    health = params.get("health") or ""
    if health in Health.values:
        qs = qs.filter(Q(status=health) | Q(health=health))
    linked = params.get("linked") or ""
    if linked == "yes":
        qs = qs.filter(client__isnull=False)
    elif linked == "no":
        qs = qs.filter(client__isnull=True)
    return qs.order_by("workbook_name")


@require("view")
def clients(request):
    user = request.user
    qs = _clients_queryset(request)
    page = paginate(request, qs)
    rollup = {r["profile"].pk: r for r in services.client_rollup(user)}
    rows = []
    for profile in page.object_list:
        stats = rollup.get(profile.pk, {})
        rows.append({"profile": profile, "namespaces": stats.get("namespaces") or 0,
                      "review": stats.get("review") or 0,
                      "storage": fmt_bytes(profile.total_storage_bytes),
                      "summary_storage": fmt_bytes(profile.summary_storage_bytes)})
    ctx = _base(request, "clients",
                page=page,
                rows=rows,
                total=qs.count(),
                totals=services.profile_totals(qs),
                healths=Health.choices,
                link_form=ProfileLinkForm(clients=services.linkable_clients(user)),
                linkable=services.linkable_clients(user),
                filtered=any(request.GET.get(k) for k in ("q", "health", "linked")))
    return render(request, "k8s/clients.html", ctx)


CLIENT_CSV = ["Workbook ID", "Workbook client", "LinkedEye client", "Primary namespace", "Projects",
              "Environments", "Applications", "Services", "Ingresses", "PVs", "PVCs", "Storage",
              "Storage (bytes)", "Namespaces (roll-up)", "Applications (roll-up)", "URLs (roll-up)",
              "Open issues", "Status", "Overall health", "Monitoring tools", "CI/CD tools",
              "Application URL", "Monitoring URL", "CI/CD URL", "Site", "Remarks"]


def _client_row(profile):
    return [profile.workbook_id, profile.workbook_name, profile.client.name if profile.client_id else "",
            profile.primary_namespace, ", ".join(profile.project_names or []),
            ", ".join(profile.environments or []),
            profile.app_count if profile.app_count is not None else "",
            profile.service_count if profile.service_count is not None else "",
            profile.ingress_count if profile.ingress_count is not None else "",
            profile.pv_count if profile.pv_count is not None else "",
            profile.pvc_count if profile.pvc_count is not None else "",
            profile.total_storage_raw,
            profile.total_storage_bytes if profile.total_storage_bytes is not None else "",
            profile.summary_namespaces if profile.summary_namespaces is not None else "",
            profile.summary_applications if profile.summary_applications is not None else "",
            profile.summary_urls if profile.summary_urls is not None else "",
            profile.open_issues if profile.open_issues is not None else "",
            profile.get_status_display(), profile.get_health_display(),
            ", ".join(profile.monitoring_tools or []), ", ".join(profile.cicd_tools or []),
            profile.primary_url, profile.monitoring_url, profile.cicd_url, profile.site, profile.remarks]


@require("view")
def clients_export(request):
    qs = _clients_queryset(request)
    return csv_response("kubernetes-clients.csv", CLIENT_CSV, (_client_row(p) for p in qs[:10000]))


@require_POST
@require("admin")
def client_link(request, pk):
    """Link (or unlink) a workbook client to a LinkedEye client."""
    profile = get_object_or_404(services.profiles_for(request.user), pk=pk)
    if not services.can_manage(request.user, "admin", profile.site):
        raise PermissionDenied(f"You cannot change records for “{profile.site_label}”.")
    form = ProfileLinkForm(request.POST, clients=services.linkable_clients(request.user))
    if not form.is_valid():
        messages.error(request, "That client could not be linked.")
        return redirect(reverse("k8s:clients"))
    choice = form.cleaned_data["client"]
    before = profile.client.name if profile.client_id else ""
    profile.client = services.linkable_clients(request.user).filter(pk=int(choice)).first() if choice else None
    profile.save(update_fields=["client", "updated_at"])
    after = profile.client.name if profile.client_id else ""
    audit(request, "update", profile, field="client", before=before, after=after)
    messages.success(request, f"“{profile.workbook_name}” is now linked to {after}." if after
                     else f"“{profile.workbook_name}” is no longer linked to a client.")
    return redirect(reverse("k8s:clients"))


# ----------------------------------------------------------------------------- imports
def _staging_dir():
    STAGING_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    return STAGING_DIR


def _sweep_staging():
    """Drop staged uploads nobody confirmed."""
    try:
        now = time.time()
        for path in _staging_dir().iterdir():
            if path.is_file() and now - path.stat().st_mtime > STAGING_MAX_AGE:
                path.unlink(missing_ok=True)
    except OSError:  # pragma: no cover - a full or read-only temp dir must not break the page
        log.warning("k8s import: could not sweep the staging directory")


def _stage(uploaded):
    """Save the upload so the confirm button imports the very bytes that were previewed.

    The file keeps its (already validated) extension, because that is how the reader knows whether it is
    reading a workbook or a zip of CSV sheets.
    """
    token = secrets.token_hex(16)
    suffix = Path(uploaded.name or "").suffix.lower()
    path = _staging_dir() / f"{token}{suffix}"
    with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as fh:
        for chunk in uploaded.chunks():
            fh.write(chunk)
    return token, suffix, path


def _staged_path(token, suffix):
    """The staged file for `token`, or None.

    Token and suffix together make the file name, so both are checked against a fixed shape rather than
    trusted: plain hex, and one of the extensions the upload form accepts.
    """
    if not token or len(token) != 32 or any(c not in "0123456789abcdef" for c in token):
        return None
    if suffix not in ACCEPTED_SUFFIXES:
        return None
    path = _staging_dir() / f"{token}{suffix}"
    return path if path.is_file() else None


def _drop(token, suffix):
    path = _staged_path(token, suffix)
    if path is not None:
        path.unlink(missing_ok=True)


@require("admin")
def import_inventory(request):
    """Upload a workbook: the first POST previews the diff, the second one applies it."""
    _need_import(request)
    _sweep_staging()
    pending = request.session.get(SESSION_KEY) or {}
    form = UploadForm(initial={"site": pending.get("site") or services.default_site()})
    result = None

    if request.method == "POST" and request.POST.get("action") == "confirm":
        token = request.POST.get("token") or ""
        suffix = pending.get("suffix") or ""
        path = _staged_path(token, suffix)
        if path is None or token != pending.get("token"):
            messages.error(request, "That upload has expired. Please choose the file again.")
            request.session.pop(SESSION_KEY, None)
            return redirect(reverse("k8s:import_inventory"))
        try:
            result = run_import(path, user=request.user, dry_run=False, site=pending.get("site"),
                                create_compute=bool(pending.get("create_compute")),
                                source_name=pending.get("name"))
        finally:
            _drop(token, suffix)
            request.session.pop(SESSION_KEY, None)
        audit(request, "import", None, object_type="k8s.importrun", source=result.source_name,
              status=result.status, created=result.total_created, updated=result.total_updated,
              missing=result.total_missing, rows=result.total_rows)
        if result.ok:
            emit("k8s", f"Kubernetes inventory imported from {result.source_name}", severity="info",
                 kind="job", site=pending.get("site") or "",
                 message=f"{result.total_rows} row(s): {result.total_created} created, "
                         f"{result.total_updated} updated, {result.total_missing} no longer present.",
                 object_type="k8s.importrun", object_id=result.run_id or "")
            messages.success(request, f"Imported {result.total_rows} row(s): {result.total_created} new, "
                                       f"{result.total_updated} updated, {result.total_missing} no longer "
                                       f"in the snapshot.")
            return redirect(reverse("k8s:import_detail", args=[result.run_id]) if result.run_id
                            else reverse("k8s:overview"))
        messages.error(request, "The import did not finish cleanly — see the problems below.")

    elif request.method == "POST":
        form = UploadForm(request.POST, request.FILES)
        if form.is_valid():
            uploaded = form.cleaned_data["file"]
            token, suffix, path = _stage(uploaded)
            result = run_import(path, user=request.user, dry_run=True, site=form.cleaned_data["site"],
                                create_compute=form.cleaned_data["create_compute"],
                                source_name=uploaded.name)
            if not result.ok:
                _drop(token, suffix)
            request.session[SESSION_KEY] = {"token": token, "suffix": suffix, "name": uploaded.name,
                                             "site": form.cleaned_data["site"],
                                             "create_compute": form.cleaned_data["create_compute"],
                                             "staged_at": timezone.now().isoformat()}
            pending = request.session[SESSION_KEY]
            audit(request, "preview", None, object_type="k8s.importrun", source=uploaded.name,
                  status=result.status, rows=result.total_rows)

    ctx = _base(request, "imports",
                form=form,
                result=result,
                pending=pending if result is not None and result.dry_run and result.ok else {},
                max_mb=MAX_UPLOAD_BYTES // (1024 * 1024),
                sites=[name for name, _label in site_choices()],
                runs=services.runs_for(request.user)[:5])
    return render(request, "k8s/import.html", ctx)


@require("view")
def imports(request):
    qs = services.runs_for(request.user)
    page = paginate(request, qs)
    ctx = _base(request, "imports", page=page, items=page.object_list, total=qs.count(),
                statuses=ImportStatus.choices)
    return render(request, "k8s/imports.html", ctx)


@require("view")
def import_detail(request, pk):
    run = get_object_or_404(services.runs_for(request.user), pk=pk)
    discovery = (run.metadata or {}).get("discovery") or {}
    counts = discovery.get("resource_counts") or {}
    ctx = _base(request, "imports",
                run=run,
                sheets=run.sheet_rows,
                commands=discovery.get("commands") or [],
                resource_counts=sorted(counts.items()) if isinstance(counts, dict) else [],
                discovery_meta=sorted((discovery.get("metadata") or {}).items()),
                safety=discovery.get("safety") or [])
    return render(request, "k8s/import_detail.html", ctx)
