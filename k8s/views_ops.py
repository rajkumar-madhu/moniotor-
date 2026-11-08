"""Operations pages: the tools running in the estate, who has monitoring, and the issue register.

/kubernetes/tools/        monitoring, CI/CD and platform tools grouped by tool, with per-client counts; CSV
/kubernetes/monitoring/   what monitoring each client has and, pointedly, which clients have none; CSV
/kubernetes/issues/       the issue register: filters, severity KPIs, a chart by category, bulk "open incident"; CSV

Every queryset is tenant- and site-scoped. The two mutations — creating / linking exporters from the tool list
and opening ServiceDesk incidents for High issues — are POST + CSRF + a role check on each record's own site +
audit, and both are opt-in. Tool pages show Secret *names* only; a credential value is never read or stored.
"""
from collections import OrderedDict, defaultdict
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.db.models import Case, Count, IntegerField, Q, Value, When
from django.shortcuts import redirect, render
from django.urls import path, reverse
from django.views.decorators.http import require_POST

from core.access import require, scope_by_site
from core.audit import audit
from telemetry.views.common import csv_response

from . import services
from . import views as base
from .parsing import is_unmapped
from .models_ops import (CAPABILITIES, CATEGORY_ICONS, SEVERITY_ORDER, InventoryIssue, IssueStatus, Severity,
                         ToolCategory, ToolInstance, capability_of)
from .sheets.ops import link_exporters, link_incidents

CSV_LIMIT = 10000
BULK_LIMIT = 500
C_CRIT, C_WARN, C_BLUE, C_OK, C_MUTE = "#d92d20", "#b7791f", "#2b4cff", "#0f7a55", "#a8a398"
HEALTHY_WORDS = ("healthy", "running", "ok", "ready")


def _tenant():
    return settings.LINKEDEYE_TENANT


def _dt(value):
    return value.isoformat() if value else ""


def tools_for(user):
    qs = scope_by_site(ToolInstance.objects.filter(tenant_key=_tenant()), user)
    return qs.select_related("namespace", "namespace__client", "exporter")


def issues_for(user):
    qs = scope_by_site(InventoryIssue.objects.filter(tenant_key=_tenant()), user)
    return qs.select_related("namespace", "namespace__client", "incident", "cluster")


def _site_param(user, params):
    """The ?site= filter, ignored when it names a site the user cannot see."""
    site = (params.get("site") or "").strip()
    if not site:
        return ""
    names = services.visible_site_names(user)
    return site if names is None or site in names else ""


def _distinct(qs, field):
    return sorted({v for v in qs.order_by().values_list(field, flat=True).distinct() if v}, key=str.lower)


def _unhealthy_q():
    """Tools the discovery did not call healthy, or that are scaled to zero."""
    not_healthy = ~Q(status="")
    for word in HEALTHY_WORDS:
        not_healthy &= ~Q(status__iexact=word)
    return Q(replica_count=0) | not_healthy


# ============================================================================ tools
TOOL_FILTERS = ("q", "category", "client", "tool", "namespace", "environment", "state", "site")


def filter_tools(user, params):
    """The tool instances a set of query parameters selects (a GET query string or the link form's POST)."""
    qs = tools_for(user)
    site = _site_param(user, params)
    if site:
        qs = qs.filter(site=site)
    category = params.get("category") or ""
    if category in ToolCategory.values:
        qs = qs.filter(category=category)
    q = (params.get("q") or "").strip()
    if q:
        qs = qs.filter(Q(tool__icontains=q) | Q(service__icontains=q) | Q(hostname__icontains=q)
                       | Q(url__icontains=q) | Q(namespace__name__icontains=q) | Q(storage__icontains=q)
                       | Q(credential_reference__icontains=q) | Q(client_label__icontains=q))
    client = (params.get("client") or "").strip()
    if client:
        qs = qs.filter(client_label__iexact=client)
    tool = (params.get("tool") or "").strip()
    if tool:
        qs = qs.filter(tool=tool)
    namespace = (params.get("namespace") or "").strip()
    if namespace:
        qs = qs.filter(namespace__name=namespace)
    environment = (params.get("environment") or "").strip()
    if environment:
        qs = qs.filter(environment__iexact=environment)
    state = params.get("state") or ""
    if state == "attention":
        qs = qs.filter(_unhealthy_q())
    elif state == "no_url":
        qs = qs.filter(url="")
    elif state == "no_exporter":
        qs = qs.filter(category=ToolCategory.MONITORING, exporter__isnull=True).exclude(url="")
    elif state == "gone":
        qs = qs.filter(missing_since__isnull=False)
    return qs


def tool_totals(qs):
    agg = qs.aggregate(instances=Count("pk"), with_url=Count("pk", filter=~Q(url="")),
                       attention=Count("pk", filter=_unhealthy_q()),
                       gone=Count("pk", filter=Q(missing_since__isnull=False)),
                       exporters=Count("pk", filter=Q(exporter__isnull=False)))
    agg["tools"] = qs.order_by().values("tool").distinct().count()
    agg["clients"] = qs.order_by().exclude(client_label="").values("client_label").distinct().count()
    return agg


def tool_groups(qs):
    """One dict per tool: instance count, clients with their counts, distinct URLs, unhealthy count."""
    rows = list(qs.order_by().values("category", "tool").annotate(
        n=Count("pk"), namespaces=Count("namespace", distinct=True), attention=Count("pk", filter=_unhealthy_q()),
        gone=Count("pk", filter=Q(missing_since__isnull=False)))
        .order_by("category", "-n", "tool"))
    per_client = defaultdict(list)
    for r in qs.order_by().values("category", "tool", "client_label").annotate(n=Count("pk")).order_by(
            "-n", "client_label"):
        per_client[(r["category"], r["tool"])].append((r["client_label"] or "", r["n"]))
    urls = defaultdict(list)
    for r in qs.order_by().exclude(url="").values("category", "tool", "url").distinct().order_by("url")[:2000]:
        bucket = urls[(r["category"], r["tool"])]
        if len(bucket) < 3:
            bucket.append(r["url"])
    labels = dict(ToolCategory.choices)
    out = []
    for r in rows:
        key = (r["category"], r["tool"])
        clients = per_client.get(key, [])
        out.append({**r, "category_label": labels.get(r["category"], r["category"]),
                    "clients": [(c, n) for c, n in clients if c], "unmapped": sum(n for c, n in clients if not c),
                    "client_count": len([1 for c, _n in clients if c]), "urls": urls.get(key, []),
                    "capability": capability_of(r["tool"])})
    return out


@require("view")
def tools(request):
    user = request.user
    qs = filter_tools(user, request.GET)
    everything = tools_for(user)
    page = base.paginate(request, qs.order_by("category", "tool", "client_label", "namespace__name", "service"))
    category = request.GET.get("category") or ""
    category_counts = dict(everything.order_by().values_list("category").annotate(n=Count("pk")))
    ctx = base._base(
        request, "tools",
        page=page, items=page.object_list, total=qs.count(), totals=tool_totals(qs), groups=tool_groups(qs),
        category=category if category in ToolCategory.values else "",
        category_tabs=[("", "All", sum(category_counts.values()), "grid")] + [
            (c.value, c.label, category_counts.get(c.value, 0), CATEGORY_ICONS[c]) for c in ToolCategory],
        tools_list=_distinct(everything, "tool"), clients=_distinct(everything, "client_label"),
        namespaces=_distinct(everything, "namespace__name"), environments=_distinct(everything, "environment"),
        can_link=base.can(user, "operate"), cat_query=base._query(request, "category").urlencode(),
        link_candidates=qs.filter(category=ToolCategory.MONITORING, exporter__isnull=True).exclude(url="").count(),
        filtered=any(request.GET.get(k) for k in TOOL_FILTERS if k not in ("site", "category")),
        filter_items=[(k, request.GET[k]) for k in TOOL_FILTERS if request.GET.get(k)])
    return render(request, "k8s/tools.html", ctx)


TOOL_CSV = ["Category", "Tool", "Client", "Environment", "Namespace", "Service", "Host", "URL", "Ports", "Storage",
            "Credential reference (Secret name)", "Status", "Replicas", "Authentication", "Owner", "Exporter", "Site",
            "Last seen", "Missing since"]


@require("view")
def tools_export(request):
    qs = filter_tools(request.user, request.GET).order_by("category", "tool", "client_label", "namespace__name",
                                                           "service")
    return csv_response("kubernetes-tools.csv", TOOL_CSV, (
        [t.get_category_display(), t.tool, t.client_display, t.environment, t.namespace.name, t.service,
         t.hostname, t.url, t.port_text, t.storage, t.credential_reference, t.status,
         "" if t.replica_count is None else t.replica_count, t.authentication, t.owner,
         t.exporter.name if t.exporter_id else "", t.site, _dt(t.last_seen), _dt(t.missing_since)]
        for t in qs[:CSV_LIMIT]))


def _plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


@require_POST
@require("operate")
def tools_link_exporters(request):
    """Create or link an exporter for each monitoring tool in view that has a URL. Opt-in; created disabled."""
    user = request.user
    qs = filter_tools(user, request.POST).filter(category=ToolCategory.MONITORING).select_related("namespace")
    result = link_exporters(_tenant(), qs[:BULK_LIMIT * 4],
                            allowed=lambda site: services.can_manage(user, "operate", site))
    audit(request, "link", None, object_type="k8s.tool", **result)
    text = (f"{_plural(result['created'], 'exporter')} created (scraping off), "
            f"{_plural(result['linked'], 'tool')} linked to an exporter.")
    if result["forbidden"]:
        messages.warning(request, f"{text} {_plural(result['forbidden'], 'tool')} on sites where you lack the "
                                  f"operator role {'was' if result['forbidden'] == 1 else 'were'} left alone.")
    else:
        messages.success(request, text)
    kept = urlencode({k: request.POST[k] for k in TOOL_FILTERS if request.POST.get(k)})
    return redirect(reverse("k8s:tools") + (f"?{kept}" if kept else ""))


# ============================================================================ monitoring coverage
def _key(name):
    return (name or "").strip().lower()


def monitoring_coverage(user, site=""):
    """Which clients have which monitoring capability — and which have none.

    The client list is every workbook client (client profiles) plus every client a namespace is mapped to; a
    client that only appears on a tool is added too. A client has monitoring when at least one monitoring
    tool runs in a namespace mapped to it.
    """
    profiles = services.profiles_for(user).filter(summary_only=False)
    namespaces = services.namespaces_for(user)
    tools = tools_for(user)
    issues = issues_for(user).filter(status=IssueStatus.OPEN)
    if site:
        profiles, namespaces = profiles.filter(site=site), namespaces.filter(site=site)
        tools, issues = tools.filter(site=site), issues.filter(site=site)

    names, display = OrderedDict(), {}

    def add(name):
        k = _key(name)
        if is_unmapped(name):          # "no client could be determined" is not a client
            return ""
        if k and k not in names:
            names[k] = name.strip()
        return k

    for p in profiles.order_by("workbook_name"):
        add(p.workbook_name)
    profiles = profiles.exclude(workbook_name__in=[p.workbook_name for p in profiles if is_unmapped(p.workbook_name)])
    ns_count = defaultdict(int)
    for r in namespaces.exclude(client_label="").values("client_label").annotate(n=Count("pk")):
        ns_count[add(r["client_label"])] += r["n"]
    profile_info = {_key(p.workbook_name): p for p in profiles}

    caps = defaultdict(lambda: defaultdict(int))        # client -> capability -> instances
    monitoring = defaultdict(int)
    attention = defaultdict(int)
    cicd = defaultdict(int)
    unmapped_tools = 0
    for r in tools.order_by().values("category", "tool", "client_label", "namespace__client_label").annotate(
            n=Count("pk"), bad=Count("pk", filter=_unhealthy_q())):
        label = r["client_label"] or r["namespace__client_label"]
        if not _key(label):
            if r["category"] == ToolCategory.MONITORING:
                unmapped_tools += r["n"]
            continue
        k = add(label)
        if r["category"] == ToolCategory.MONITORING:
            monitoring[k] += r["n"]
            attention[k] += r["bad"]
            caps[k][capability_of(r["tool"]) or "other"] += r["n"]
        else:
            cicd[k] += r["n"]
    high = defaultdict(int)
    for r in issues.filter(severity=Severity.HIGH).exclude(client_label="").values("client_label").annotate(
            n=Count("pk")):
        high[_key(r["client_label"])] += r["n"]

    rows = []
    for k, name in names.items():
        profile = profile_info.get(k)
        rows.append({
            "name": name, "profile": profile, "namespaces": ns_count.get(k, 0), "monitoring": monitoring.get(k, 0),
            "attention": attention.get(k, 0), "cicd": cicd.get(k, 0), "high": high.get(k, 0),
            "caps": {c[0]: caps[k].get(c[0], 0) for c in CAPABILITIES}, "other": caps[k].get("other", 0),
            "none": monitoring.get(k, 0) == 0,
            "apps": profile.app_count if profile else None,
            "urls": (profile.ingress_count if profile and profile.ingress_count is not None else None),
        })
    rows.sort(key=lambda r: (not r["none"], -r["high"], r["name"].lower()))
    total = len(rows)
    covered = [r for r in rows if not r["none"]]
    coverage = []
    for key, label, hint in CAPABILITIES:
        have = sum(1 for r in covered if r["caps"][key])
        coverage.append({"key": key, "label": label, "hint": hint, "clients": have,
                         "pct": round(100.0 * have / total) if total else None})
    return {
        "rows": rows, "none": [r for r in rows if r["none"]], "total": total, "covered": len(covered),
        "pct": round(100.0 * len(covered) / total) if total else None, "coverage": coverage,
        "instances": sum(monitoring.values()), "attention": sum(attention.values()),
        "unmapped_tools": unmapped_tools,
        "has_data": bool(total or monitoring or cicd),
        "tools_imported": bool(monitoring or cicd or unmapped_tools),
    }


@require("view")
def monitoring(request):
    user = request.user
    site = _site_param(user, request.GET)
    cover = monitoring_coverage(user, site)
    donut = {"segments": [s for s in (
        {"label": "With monitoring", "value": cover["covered"], "color": C_OK},
        {"label": "No monitoring", "value": len(cover["none"]), "color": C_CRIT}) if s["value"]],
        "center": {"value": str(cover["total"]), "label": "Clients"}}
    bars = {"labels": [c["label"] for c in cover["coverage"]], "tipLabels": [f'{c["label"]} ({c["hint"]})'
                                                                             for c in cover["coverage"]],
            "unit": "", "yMin": 0, "yMax": max(cover["total"], 1), "yTicks": min(max(cover["total"], 1), 5),
            "series": [{"name": "Clients covered", "color": C_BLUE, "values": [c["clients"] for c in cover["coverage"]]}]}
    ctx = base._base(request, "monitoring", cover=cover, donut=donut, bars=bars, capabilities=CAPABILITIES,
                     site_filter=site, filtered=bool(site))
    return render(request, "k8s/monitoring.html", ctx)


MONITORING_CSV = ["Client", "Has monitoring", "Monitoring tools", "Needing attention", "Namespaces",
                  "CI/CD and platform tools", "Open High issues"] + [c[1] for c in CAPABILITIES]


@require("view")
def monitoring_export(request):
    cover = monitoring_coverage(request.user, _site_param(request.user, request.GET))
    return csv_response("kubernetes-monitoring-coverage.csv", MONITORING_CSV, (
        [r["name"], "no" if r["none"] else "yes", r["monitoring"], r["attention"], r["namespaces"], r["cicd"],
         r["high"]] + [r["caps"][c[0]] for c in CAPABILITIES] for r in cover["rows"]))


# ============================================================================ issues
ISSUE_FILTERS = ("q", "severity", "category", "client", "namespace", "status", "incident", "site")
SEVERITY_RANK = Case(*[When(severity=s.value, then=Value(i)) for i, s in enumerate(SEVERITY_ORDER)],
                     default=Value(9), output_field=IntegerField())


def filter_issues(user, params, severity=True):
    """The issues a set of query parameters selects. `status` defaults to open; ``status=all`` shows both."""
    qs = issues_for(user)
    site = _site_param(user, params)
    if site:
        qs = qs.filter(site=site)
    status = params.get("status") or "open"
    if status in IssueStatus.values:
        qs = qs.filter(status=status)
    if severity:
        sev = params.get("severity") or ""
        if sev in Severity.values:
            qs = qs.filter(severity=sev)
    q = (params.get("q") or "").strip()
    if q:
        qs = qs.filter(Q(issue_id__icontains=q) | Q(resource__icontains=q) | Q(issue__icontains=q)
                       | Q(evidence__icontains=q) | Q(namespace__name__icontains=q) | Q(client_label__icontains=q))
    category = (params.get("category") or "").strip()
    if category:
        qs = qs.filter(category=category)
    client = (params.get("client") or "").strip()
    if client:
        qs = qs.filter(client_label__iexact=client)
    namespace = (params.get("namespace") or "").strip()
    if namespace:
        qs = qs.filter(namespace__name=namespace)
    incident = params.get("incident") or ""
    if incident == "with":
        qs = qs.filter(incident__isnull=False)
    elif incident == "without":
        qs = qs.filter(incident__isnull=True)
    return qs


def _issue_order(qs):
    return qs.annotate(sev_rank=SEVERITY_RANK).order_by("sev_rank", "issue_id")


def severity_counts(qs):
    counts = dict(qs.order_by().values_list("severity").annotate(n=Count("pk")))
    return [{"value": s.value, "label": s.label, "count": counts.get(s.value, 0)} for s in SEVERITY_ORDER]


def issue_chart(qs):
    """Stacked bars: issues per category, split by severity."""
    cells = defaultdict(lambda: defaultdict(int))
    for r in qs.order_by().values("category", "severity").annotate(n=Count("pk")):
        cells[r["category"] or "Other"][r["severity"]] += r["n"]
    cats = sorted(cells, key=lambda c: (-sum(cells[c].values()), c))[:12]
    colors = {Severity.HIGH: C_CRIT, Severity.MEDIUM: C_WARN, Severity.LOW: C_BLUE}
    return {"labels": [c if len(c) <= 12 else c[:11] + "…" for c in cats], "tipLabels": cats, "stacked": True,
            "series": [{"name": s.label, "color": colors[s], "values": [cells[c].get(s.value, 0) for c in cats]}
                       for s in SEVERITY_ORDER], "yTicks": 4}


@require("view")
def issues(request):
    user = request.user
    base_qs = filter_issues(user, request.GET, severity=False)          # the severity KPIs count across severities
    qs = filter_issues(user, request.GET)
    everything = issues_for(user)
    page = base.paginate(request, _issue_order(qs))
    sev_counts = severity_counts(base_qs)
    status = request.GET.get("status") or "open"
    ctx = base._base(
        request, "issues",
        page=page, items=page.object_list, total=qs.count(), sev_counts=sev_counts,
        sev_total=sum(s["count"] for s in sev_counts), chart=issue_chart(qs),
        with_incident=qs.filter(incident__isnull=False).count(),
        open_high=qs.filter(status=IssueStatus.OPEN, severity=Severity.HIGH, incident__isnull=True).count(),
        status=status if status in IssueStatus.values else "all",
        severities=Severity.choices, categories=_distinct(everything, "category"),
        clients=_distinct(everything, "client_label"), namespaces=_distinct(everything, "namespace__name"),
        can_incident=base.can(user, "admin"),
        filtered=any(request.GET.get(k) for k in ISSUE_FILTERS if k not in ("site", "status")),
        filter_items=[(k, request.GET[k]) for k in ISSUE_FILTERS if request.GET.get(k)])
    return render(request, "k8s/issues.html", ctx)


ISSUE_CSV = ["Issue ID", "Severity", "Status", "Category", "Client", "Environment", "Namespace", "Resource", "Issue",
             "Impact", "Evidence", "Recommended action", "Owner", "Incident", "Site", "First seen", "Last seen",
             "Closed at", "Missing since"]


@require("view")
def issues_export(request):
    qs = _issue_order(filter_issues(request.user, request.GET))
    return csv_response("kubernetes-issues.csv", ISSUE_CSV, (
        [i.issue_id, i.get_severity_display(), i.get_status_display(), i.category, i.client_display, i.environment,
         i.namespace.name if i.namespace_id else "", i.resource, i.issue, i.impact, i.evidence,
         i.recommended_action, i.owner, i.incident.number if i.incident_id else "", i.site, _dt(i.first_seen),
         _dt(i.last_seen), _dt(i.closed_at), _dt(i.missing_since)] for i in qs[:CSV_LIMIT]))


@require_POST
@require("admin")
def issues_open_incidents(request):
    """Open (or update) a ServiceDesk incident for the selected open High issues.

    Needs the admin role on each issue's site; only High issues qualify, others are counted as skipped. An issue
    that already has an open incident updates it (fingerprint ``k8sinv:<id>``) instead of opening a second.
    """
    user = request.user
    back = reverse("k8s:issues")
    if request.POST.get("scope") == "filtered":
        qs = filter_issues(user, request.POST).filter(severity=Severity.HIGH, status=IssueStatus.OPEN)
    else:
        ids = [int(i) for i in request.POST.getlist("ids") if i.isdigit()][:BULK_LIMIT]
        if not ids:
            messages.error(request, "Select at least one issue.")
            return redirect(back)
        qs = issues_for(user).filter(pk__in=ids)
    result = link_incidents(_tenant(), qs.order_by("issue_id")[:BULK_LIMIT],
                            allowed=lambda site: services.can_manage(user, "admin", site))
    audit(request, "open_incident", None, object_type="k8s.issue", **result)
    parts = []
    if result["opened"]:
        parts.append(f"{_plural(result['opened'], 'incident')} opened")
    if result["updated"]:
        parts.append(f"{_plural(result['updated'], 'existing incident')} updated")
    text = ("; ".join(parts) or "No incident was opened") + "."
    if result["skipped"]:
        text += f" {_plural(result['skipped'], 'issue')} skipped (only open High issues qualify)."
    if result["forbidden"]:
        messages.warning(request, f"{text} {_plural(result['forbidden'], 'issue')} on sites where you are not an "
                                  f"admin {'was' if result['forbidden'] == 1 else 'were'} left alone.")
    elif result["opened"] or result["updated"]:
        messages.success(request, text)
    else:
        messages.info(request, text)
    kept = urlencode({k: request.POST[k] for k in ISSUE_FILTERS if request.POST.get(k)})
    return redirect(back + (f"?{kept}" if kept else ""))


# ============================================================================ overview contribution
def ops_summary(user, site=""):
    """Numbers for the overview: open issues by severity, clients without monitoring, tool coverage."""
    issues_qs = issues_for(user).filter(status=IssueStatus.OPEN)
    if site:
        issues_qs = issues_qs.filter(site=site)
    sev = severity_counts(issues_qs)
    cover = monitoring_coverage(user, site)
    tools_qs = tools_for(user)
    if site:
        tools_qs = tools_qs.filter(site=site)
    totals = tool_totals(tools_qs)
    return {
        "issues": {"open": sum(s["count"] for s in sev), "by_severity": sev},
        "cover": cover, "tools": totals,
        "has_data": bool(totals["instances"] or sum(s["count"] for s in sev) or issues_for(user).exists()),
    }


def ops_signals(user, site=""):
    """The overview's ``extra`` entries for the issue and tool tiles, from this module's own records."""
    out = {}
    issues_qs = issues_for(user)
    if site:
        issues_qs = issues_qs.filter(site=site)
    if issues_qs.exists():
        out["issues"] = {"value": issues_qs.filter(status=IssueStatus.OPEN).count(), "source": "records",
                         "sheet": "12_Issues_Risks"}
    tools_qs = tools_for(user)
    if site:
        tools_qs = tools_qs.filter(site=site)
    if tools_qs.exists():
        out["tools"] = {"value": tools_qs.count(), "source": "records", "sheet": "10_Monitoring"}
    return out


# ============================================================================ urls
urlpatterns = [
    path("kubernetes/tools/", tools, name="tools"),
    path("kubernetes/tools/export.csv", tools_export, name="tools_export"),
    path("kubernetes/tools/exporters/", tools_link_exporters, name="tools_link_exporters"),
    path("kubernetes/monitoring/", monitoring, name="monitoring"),
    path("kubernetes/monitoring/export.csv", monitoring_export, name="monitoring_export"),
    path("kubernetes/issues/", issues, name="issues"),
    path("kubernetes/issues/export.csv", issues_export, name="issues_export"),
    path("kubernetes/issues/incidents/", issues_open_incidents, name="issues_open_incidents"),
]
