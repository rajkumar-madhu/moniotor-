"""Querysets, access helpers and roll-ups for the Kubernetes inventory pages.

Every queryset filters the tenant and is site-scoped for the requesting user. Child rows carry no site of
their own where the parent already names it (a node belongs to its cluster's site), and are scoped through
that parent, so a site can never drift out of sync with the record it describes.

Public helpers other modules (and the module's own later pages) may use:

    k8s.services.clusters_for(user) / nodes_for(user) / namespaces_for(user) / profiles_for(user)
    k8s.services.can_manage(user, perm, site)      permission on one record's site
    k8s.services.can_import(user)                  who may upload a workbook
    k8s.services.last_run(user)                    newest ImportRun, or None
    k8s.services.optional_counts(user)             KPI numbers for sheets whose pages are not built yet
"""
from django.apps import apps
from django.conf import settings
from django.db.models import Count, Q, Sum

from core.access import PERMS, RANK, _memberships, can, scope_by_site, visible_sites

from .models import (GLOBAL_SITE, ClientProfile, Health, ImportRun, ImportStatus, KubeCluster, MappingStatus,
                     Namespace, Node, is_global)


def tenant():
    return settings.LINKEDEYE_TENANT


# ----------------------------------------------------------------------------- access
def _tenant_wide(user, perm):
    if user.is_superuser:
        return True
    memberships = _memberships(user)
    if not memberships:  # users without memberships hold their default role everywhere
        return True
    return any(m.site is None and RANK[m.role] >= PERMS[perm] for m in memberships)


def can_manage(user, perm, site):
    """`perm` on a record's site. Global records ("All Sites" / blank) need the permission tenant-wide."""
    if not is_global(site):
        return can(user, perm, site_name=site)
    return can(user, perm) and _tenant_wide(user, perm)


def can_import(user):
    """Who may import a workbook: an admin on **every** site.

    One workbook describes the whole estate across sites, so a site-scoped admin must not be able to
    write rows for sites they cannot see.
    """
    return bool(user) and user.is_authenticated and can(user, "admin") and _tenant_wide(user, "admin")


def sees_all_sites(user):
    """True when `user` is not limited to particular sites (superuser, or a role held on every site)."""
    return visible_sites(user) is None


def visible_site_names(user):
    names = visible_sites(user)
    return None if names is None else set(names)


# ----------------------------------------------------------------------------- querysets
def clusters_for(user):
    return scope_by_site(KubeCluster.objects.filter(tenant_key=tenant()), user).select_related("compute_cluster")


def nodes_for(user):
    return scope_by_site(Node.objects.filter(tenant_key=tenant()), user, field="cluster__site").select_related(
        "cluster", "ci", "device", "member", "member__cluster")


def namespaces_for(user):
    return scope_by_site(Namespace.objects.filter(tenant_key=tenant()), user).select_related("cluster", "client")


def profiles_for(user):
    return scope_by_site(ClientProfile.objects.filter(tenant_key=tenant()), user).select_related("client")


def runs_for(user):
    """Import runs are tenant-wide (only a tenant-wide admin can create one), newest first."""
    return ImportRun.objects.filter(tenant_key=tenant()).select_related("uploaded_by")


def last_run(user=None):
    return runs_for(user).filter(status__in=[ImportStatus.OK, ImportStatus.FAILED]).first()


def site_names(user):
    """Site names that imported Kubernetes records actually use, for the page filters."""
    names = set(clusters_for(user).order_by().values_list("site", flat=True).distinct())
    names |= set(namespaces_for(user).order_by().values_list("site", flat=True).distinct())
    return sorted({n for n in names if n}, key=str.lower)


# ----------------------------------------------------------------------------- roll-ups
def node_totals(nodes):
    """Counts and capacity sums over a node queryset (one query)."""
    agg = nodes.aggregate(
        total=Count("pk"),
        control_plane=Count("pk", filter=Q(role="control_plane")),
        workers=Count("pk", filter=Q(role="worker")),
        ready_count=Count("pk", filter=Q(ready=True)),
        public=Count("pk", filter=Q(exposure="public")),
        not_ready=Count("pk", filter=Q(ready=False)),
        unschedulable=Count("pk", filter=Q(schedulable=False)),
        missing=Count("pk", filter=Q(missing_since__isnull=False)),
        cpu=Sum("cpu_capacity_millicores"),
        memory=Sum("memory_capacity_bytes"),
        pods=Sum("pod_capacity"),
        linked=Count("pk", filter=Q(ci__isnull=False)),
    )
    agg["cpu_cores"] = (agg["cpu"] or 0) / 1000.0
    return agg


def namespace_totals(namespaces):
    return namespaces.aggregate(
        total=Count("pk"),
        review=Count("pk", filter=Q(mapping_status=MappingStatus.REVIEW)),
        linked=Count("pk", filter=Q(client__isnull=False)),
        missing=Count("pk", filter=Q(missing_since__isnull=False)),
    )


def profile_totals(profiles):
    return profiles.aggregate(
        total=Count("pk"),
        linked=Count("pk", filter=Q(client__isnull=False)),
        red=Count("pk", filter=Q(status=Health.CRIT) | Q(health=Health.CRIT)),
        amber=Count("pk", filter=Q(status=Health.WARN) | Q(health=Health.WARN)),
        green=Count("pk", filter=Q(status=Health.OK, health__in=[Health.OK, Health.UNKNOWN])),
        issues=Sum("open_issues"),
        storage=Sum("total_storage_bytes"),
    )


# ---- KPI numbers for sheets whose own pages are not built yet -------------------------------------
# (metric key, workbook sheet the number comes from, candidate model that will own it)
EXTRA_METRICS = [
    ("workloads", "04_Application_Inventory", ("k8s", "Workload")),
    ("services", "05_Service_Inventory", ("k8s", "KubeService")),
    ("ingresses", "06_Ingress_URLs", ("k8s", "Ingress")),
    ("volumes", "07_PV_PVC_Storage", ("k8s", "Volume")),
    ("certificates", "09_Certificate_Inventory", ("k8s", "Certificate")),
    ("tools", "11_CICD_Platform_Tools", ("k8s", "PlatformTool")),
    ("issues", "12_Issues_Risks", ("k8s", "Issue")),
]


def _model_count(app_label, model_name, user):
    """Count a model that may not exist yet; None when it is absent, so the page can say so."""
    try:
        model = apps.get_model(app_label, model_name)
    except LookupError:
        return None
    field_names = {f.name for f in model._meta.get_fields()}
    qs = model._default_manager.all()
    if "tenant_key" in field_names:
        qs = qs.filter(tenant_key=tenant())
    if "site" in field_names:
        qs = scope_by_site(qs, user)
    elif "cluster" in field_names:
        qs = scope_by_site(qs, user, field="cluster__site")
    try:
        return qs.count()
    except Exception:  # pragma: no cover - a half-built model must never break the overview
        return None


def optional_counts(user, run=None):
    """{key: {"value": n|None, "source": "records"|"last import"|None, "sheet": name}}.

    A number is read from the module's own records when that page's model exists; otherwise it falls
    back to the row count the last import recorded for that sheet, labelled as such. When neither is
    available the value is None and the page shows a dash — it never invents a number.
    """
    run = run if run is not None else last_run(user)
    out = {}
    for key, sheet, (app_label, model_name) in EXTRA_METRICS:
        value = _model_count(app_label, model_name, user)
        if value is not None:
            out[key] = {"value": value, "source": "records", "sheet": sheet}
            continue
        rows = run.sheet_count(sheet) if run is not None else None
        out[key] = {"value": rows, "source": "last import" if rows is not None else None, "sheet": sheet}
    return out


def client_rollup(user):
    """One row per client profile with its namespace / node numbers, for the overview roll-up."""
    profiles = list(profiles_for(user).order_by("workbook_name"))
    namespaces = namespaces_for(user)
    by_profile_name = {}
    for row in namespaces.values("client_label").annotate(
            n=Count("pk"), review=Count("pk", filter=Q(mapping_status=MappingStatus.REVIEW))):
        by_profile_name[(row["client_label"] or "").strip().lower()] = row
    by_client_id = {}
    for row in namespaces.exclude(client__isnull=True).values("client_id").annotate(
            n=Count("pk"), review=Count("pk", filter=Q(mapping_status=MappingStatus.REVIEW))):
        by_client_id[row["client_id"]] = row
    out = []
    for profile in profiles:
        stats = by_client_id.get(profile.client_id) if profile.client_id else None
        if stats is None:
            stats = by_profile_name.get(profile.workbook_name.strip().lower())
        out.append({
            "profile": profile,
            "namespaces": (stats or {}).get("n") or 0,
            "review": (stats or {}).get("review") or 0,
        })
    return out


def unlinked_profiles(user):
    """Workbook clients with no LinkedEye client yet — the Clients page offers linking them."""
    return profiles_for(user).filter(client__isnull=True).order_by("workbook_name")


def linkable_clients(user):
    """core.Client rows an admin may link a profile to."""
    Client = apps.get_model("core", "Client")
    return Client.objects.filter(tenant_key=tenant()).order_by("name")


def default_site():
    return GLOBAL_SITE
