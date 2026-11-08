"""Importers for the operations sheets: monitoring, CI/CD and platform tools, and the issue register.

===========================  =====  ====================================================================
Sheet                        Order  Goes to
===========================  =====  ====================================================================
``10_Monitoring``               90  ``ToolInstance`` (category monitoring) (+ optional Exporter link)
``11_CICD_Platform_Tools``     100  ``ToolInstance`` (category cicd / platform)
``12_Issues_Risks``            110  ``InventoryIssue`` (+ optional ServiceDesk incident for High issues)
===========================  =====  ====================================================================

Data rules
----------
* The workbook describes a real customer estate; nothing from it belongs in this repository.
* Tools: the ``Credential Reference`` column is a Kubernetes Secret **name** and is stored as such; the
  ``Username`` column is never read. A column whose header suggests a secret value is reported and skipped.
* Placeholder cells ("NOT DISCOVERED", "NOT APPLICABLE", "MANUAL INPUT REQUIRED" …) become empty strings
  (``parsing.clean``). An issue whose namespace is "not applicable" is a cluster-level issue (no namespace).
* A re-import updates rows in place. Tools and issues the new snapshot no longer contains are marked
  missing (``missing_since``); an issue that disappeared is also closed, and re-opens if it comes back.

Cross-links (opt-in, both OFF by default)
-----------------------------------------
* ``exporters`` – a monitoring tool with a URL creates or links an ``observe.Exporter`` on the same site.
  A new exporter is created **disabled**, so nothing is scraped until an operator sets a metrics URL and
  enables it. An existing exporter for the same URL is linked, never duplicated.
* ``incidents`` – an open **High** issue opens a ServiceDesk incident through ``servicedesk.services.
  open_incident`` with the fingerprint ``k8sinv:<issue id>``; a re-import (or a second click) updates that
  incident instead of opening another. The incident is stored on the issue.

Switch them on for one import with::

    with ops_options(exporters=True, incidents=True):
        run_import(path)

or later from the Tools and Issues pages. A dry run never calls either: it only counts what would happen.
"""
import re
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import urlsplit

from django.db import transaction
from django.utils import timezone

from ..importer import register_sheet
from ..models import is_global
from ..models_ops import (KNOWN_TOOLS, TOOL_ALIASES, InventoryIssue, IssueStatus, Severity, ToolCategory,
                          ToolInstance)
from ..parsing import clean, is_unmapped, parse_int, parse_ports, parse_url

MONITORING_SHEET = "10_Monitoring"
TOOLS_SHEET = "11_CICD_Platform_Tools"
ISSUES_SHEET = "12_Issues_Risks"


def _get(row, *names):
    for name in names:
        value = row.get(name)
        if value:
            return value
    return ""


# ----------------------------------------------------------------------------- options
_OPTIONS = ContextVar("k8s_ops_crosslink", default=None)


@contextmanager
def ops_options(exporters=False, incidents=False):
    """Switch the cross-links on for the imports run inside the ``with`` block (thread/context local)."""
    token = _OPTIONS.set({"exporters": bool(exporters), "incidents": bool(incidents)})
    try:
        yield
    finally:
        _OPTIONS.reset(token)


_ATTRS = {"exporters": "link_exporters", "incidents": "open_incidents"}


def _option(ctx, name):
    """True when the import was asked to cross-link: an attribute on the context wins, then the block."""
    explicit = getattr(ctx, _ATTRS[name], None)
    if explicit is not None:
        return bool(explicit)
    return bool((_OPTIONS.get() or {}).get(name))


# ----------------------------------------------------------------------------- helpers
def normalise_tool(raw, default_category=ToolCategory.CICD):
    """'Idrac' -> ('iDRAC exporter', 'monitoring'); an unknown name keeps its spelling (first letter upper)
    and the category of the sheet it came from."""
    text = clean(raw)
    if not text:
        return "", default_category
    key = re.sub(r"[^a-z0-9]", "", text.lower())
    name = TOOL_ALIASES.get(key)
    if name is None:
        name = text[:1].upper() + text[1:]
        return name[:80], default_category
    return name, KNOWN_TOOLS.get(name, (default_category, ""))[0]


# Headers that would carry a secret VALUE if a workbook ever had them: never read, reported if present.
_VALUE_HEADER = re.compile(
    r"^(value|data|token|password|passwd|pwd|api[ _-]?key|private[ _-]?key|secret|secret[ _-]?(value|data|text)|"
    r"credential[ _-]?value|base64|plain[ _-]?text|decoded)$", re.IGNORECASE)
_NAME_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,252}$")


def _warn_value_columns(ctx, sheet_name, sheet):
    for column in sheet.columns:
        if _VALUE_HEADER.match(clean(column) or ""):
            ctx.warn(sheet_name, f"column “{column}” looks like it could hold a secret value — it was ignored")


def _host_of(url):
    try:
        return (urlsplit(url).hostname or "")[:253]
    except ValueError:
        return ""


def _mark_missing(ctx, sheet_name, qs, seen_pks, **extra):
    """Stamp ``missing_since`` on the rows of `qs` that this run did not see (the sheet speaks for all of them)."""
    if not seen_pks and (ctx.counts.get(sheet_name) or {}).get("rows"):
        # rows were present but none could be read (a key column renamed or removed): a damaged sheet is not
        # an empty register, so nothing is retired
        ctx.warn(sheet_name, "no row could be read, so existing records were left untouched")
        return 0
    qs = qs.filter(missing_since__isnull=True)
    if seen_pks:
        qs = qs.exclude(pk__in=seen_pks)
    gone = qs.update(missing_since=timezone.now(), **extra)
    if gone:
        ctx.count(sheet_name, "missing", gone)
    return gone


# ----------------------------------------------------------------------------- 10 / 11 tools
def _import_tools(ctx, sheet, sheet_name, tool_columns, default_category, categories):
    cluster = ctx.cluster_for()
    _warn_value_columns(ctx, sheet_name, sheet)
    seen = set()
    unknown = set()
    for row in sheet.rows:
        raw = clean(_get(row, *tool_columns))
        namespace_name = clean(_get(row, "Namespace"))
        if not raw or not namespace_name:
            ctx.warn(sheet_name, "a row has no tool or no namespace and was skipped")
            ctx.count(sheet_name, "skipped")
            continue
        tool, category = normalise_tool(raw, default_category)
        if tool not in KNOWN_TOOLS:
            unknown.add(tool)
        client = clean(_get(row, "Client"))
        namespace = ctx.resolve_namespace(namespace_name, cluster=cluster, client_label=client,
                                          environment=clean(_get(row, "Environment")), sheet=sheet_name)
        url = parse_url(_get(row, "URL"))
        reference = clean(_get(row, "Credential Reference"))
        if reference and not _NAME_OK.match(reference):
            ctx.warn(sheet_name, "a credential reference is not a Kubernetes object name and was left empty")
            reference = ""
        values = {
            "site": namespace.site,
            "tool_raw": raw[:80],
            "client_label": "" if is_unmapped(client) else client[:150],
            "environment": clean(_get(row, "Environment"))[:40],
            "hostname": (clean(_get(row, "Hostname")) or _host_of(url)).lower()[:253],
            "url": url,
            "ports": parse_ports(_get(row, "Port")),
            "storage": clean(_get(row, "Storage"))[:253],
            "authentication": clean(_get(row, "Authentication"))[:120],
            "credential_reference": reference[:253],
            "status": clean(_get(row, "Status"))[:40],
            "replica_count": parse_int(_get(row, "Replica Count"), minimum=0, maximum=100000),
            "owner": clean(_get(row, "Owner"))[:150],
            "notes": clean(_get(row, "Notes"))[:300],
        }
        keys = {"namespace": namespace, "category": category, "tool": tool,
                "service": clean(_get(row, "Service"))[:253]}
        instance, _created = ctx.upsert(ToolInstance, keys, values, sheet=sheet_name)
        seen.add(instance.pk)
    if unknown:
        ctx.warn(sheet_name, f"{len(unknown)} tool name(s) are not in the known list and were kept as written")

    _mark_missing(ctx, sheet_name, ToolInstance.objects.filter(
        tenant_key=ctx.tenant, namespace__cluster=cluster, category__in=categories), seen)

    if seen and sheet_name == MONITORING_SHEET:
        tools = ToolInstance.objects.filter(tenant_key=ctx.tenant, pk__in=seen).select_related("namespace")
        wanted = _option(ctx, "exporters")
        ctx.note("ops_crosslink_exporters", {"requested": wanted, "dry_run": ctx.dry_run})
        if wanted:
            result = link_exporters(ctx.tenant, tools)
            for key, amount in result.items():
                if amount and key != "forbidden":
                    ctx.count(sheet_name, f"exporters_{key}", amount)
            ctx.note("ops_crosslink_exporters", {"requested": True, "dry_run": ctx.dry_run, **result})


def import_monitoring(ctx, sheet):
    _import_tools(ctx, sheet, MONITORING_SHEET, ("Monitoring Component", "Tool", "Component"),
                  ToolCategory.MONITORING, [ToolCategory.MONITORING])


def import_cicd(ctx, sheet):
    _import_tools(ctx, sheet, TOOLS_SHEET, ("Tool", "Platform Tool"), ToolCategory.CICD,
                  [ToolCategory.CICD, ToolCategory.PLATFORM])


register_sheet(MONITORING_SHEET, 90, import_monitoring)
register_sheet(TOOLS_SHEET, 100, import_cicd)


# ----------------------------------------------------------------------------- exporter cross-link
def _unique_name(base, taken):
    base = base[:110]
    if base not in taken:
        return base
    for n in range(2, 1000):
        suffix = f" (kubernetes {n})" if n > 2 else " (kubernetes)"
        candidate = base[:120 - len(suffix)] + suffix
        if candidate not in taken:
            return candidate
    return base[:100] + f" ({len(taken)})"


def link_exporters(tenant, tools, allowed=None):
    """Create or link an ``observe.Exporter`` for each distinct (tool, URL) among the monitoring `tools`.

    * an existing exporter whose metrics URL is the tool URL (or ``<url>/metrics``), or whose target is that
      URL, is linked — never duplicated;
    * otherwise a new exporter is created on the tool's site, **disabled** and with no metrics URL, so
      nothing is scraped until an operator reviews it;
    * ``allowed`` – optional ``f(site) -> bool``; tools on a site it rejects are left alone and counted as
      ``forbidden``.

    Idempotent; returns ``{"created", "linked", "forbidden"}``.
    """
    from django.db.models import Q
    from observe.models import Category, Exporter, ExporterStatus

    out = {"created": 0, "linked": 0, "forbidden": 0}
    candidates = []
    for tool in tools:
        if tool.category != ToolCategory.MONITORING or not tool.url:
            continue
        if allowed is not None and not allowed(tool.site):
            out["forbidden"] += 1
            continue
        candidates.append(tool)
    if not candidates:
        return out

    urls = {t.url.rstrip("/") for t in candidates}
    forms = set()
    for u in urls:
        forms.update({u, u + "/", u + "/metrics"})
    existing = {}
    for exp in Exporter.objects.filter(tenant_key=tenant).filter(
            Q(metrics_url__in=forms) | Q(target__in=urls | {u + "/" for u in urls})).order_by("pk"):
        for u in (exp.metrics_url, exp.target):
            u = (u or "").rstrip("/")
            if u.endswith("/metrics"):
                existing.setdefault(u[:-len("/metrics")], exp)
            if u:
                existing.setdefault(u, exp)
    taken = set(Exporter.objects.filter(tenant_key=tenant).values_list("name", flat=True))

    changed = []
    by_key = {}
    for tool in sorted(candidates, key=lambda t: (t.tool, t.url, t.pk)):
        key = (tool.tool, tool.url.rstrip("/"))
        exporter = by_key.get(key) or existing.get(tool.url.rstrip("/"))
        if exporter is None:
            host = tool.hostname or _host_of(tool.url) or tool.service
            site = "" if is_global(tool.site) else tool.site
            exporter = Exporter.objects.create(
                tenant_key=tenant, name=_unique_name(f"{tool.tool} ({host})", taken), exporter_type=tool.tool[:60],
                category=Category.KUBERNETES, target=tool.url.rstrip("/")[:255], metrics_url="", site=site,
                enabled=False, status=ExporterStatus.INACTIVE,
                description="Created from the Kubernetes inventory. Scraping is off: set the metrics URL and "
                            "enable it to start collecting.")
            taken.add(exporter.name)
            out["created"] += 1
        by_key[key] = exporter
        if tool.exporter_id != exporter.pk:
            tool.exporter = exporter
            changed.append(tool)
            out["linked"] += 1
    if changed:
        ToolInstance.objects.bulk_update(changed, ["exporter"])
    return out


# ----------------------------------------------------------------------------- 12_Issues_Risks
SEVERITY_WORDS = {"high": Severity.HIGH, "critical": Severity.HIGH, "medium": Severity.MEDIUM,
                  "moderate": Severity.MEDIUM, "low": Severity.LOW, "info": Severity.LOW}
CLOSED_WORDS = {"closed", "resolved", "done", "fixed", "cleared"}


def import_issues(ctx, sheet):
    cluster = ctx.cluster_for()
    seen = set()
    bad_severity = 0
    for row in sheet.rows:
        issue_id = clean(_get(row, "Issue ID", "ID"))
        if not issue_id:
            ctx.warn(ISSUES_SHEET, "a row has no issue id and was skipped")
            ctx.count(ISSUES_SHEET, "skipped")
            continue
        client = clean(_get(row, "Client"))
        namespace_name = clean(_get(row, "Namespace"))
        namespace = (ctx.resolve_namespace(namespace_name, cluster=cluster, client_label=client,
                                           environment=clean(_get(row, "Environment")), sheet=ISSUES_SHEET)
                     if namespace_name else None)
        severity = SEVERITY_WORDS.get(clean(_get(row, "Severity")).lower())
        if severity is None:
            bad_severity += 1
            severity = Severity.MEDIUM
        closed = clean(_get(row, "Status")).lower() in CLOSED_WORDS
        values = {
            "cluster": cluster,
            "namespace": namespace,
            "site": namespace.site if namespace is not None else cluster.site,
            "client_label": "" if is_unmapped(client) else client[:150],
            "environment": clean(_get(row, "Environment"))[:40],
            "severity": severity,
            "category": clean(_get(row, "Category"))[:60],
            "resource": clean(_get(row, "Resource"))[:300],
            "issue": clean(_get(row, "Issue"))[:200],
            "impact": clean(_get(row, "Impact"))[:300],
            "evidence": clean(_get(row, "Evidence"))[:500],
            "recommended_action": clean(_get(row, "Recommended Action"))[:500],
            "owner": clean(_get(row, "Owner"))[:150],
            "status": IssueStatus.CLOSED if closed else IssueStatus.OPEN,
            "closed_at": ctx.stamp if closed else None,
        }
        issue, _created = ctx.upsert(InventoryIssue, {"issue_id": issue_id[:40]}, values, sheet=ISSUES_SHEET)
        seen.add(issue.pk)
    if bad_severity:
        ctx.warn(ISSUES_SHEET, f"{bad_severity} issue(s) had no recognisable severity and were filed as Medium")

    # The sheet is the full register for this cluster: an issue it no longer lists has cleared.
    _mark_missing(ctx, ISSUES_SHEET, InventoryIssue.objects.filter(tenant_key=ctx.tenant, cluster=cluster), seen)
    InventoryIssue.objects.filter(tenant_key=ctx.tenant, cluster=cluster, missing_since__isnull=False,
                                  status=IssueStatus.OPEN).update(status=IssueStatus.CLOSED, closed_at=timezone.now())

    wanted = _option(ctx, "incidents")
    eligible = InventoryIssue.objects.filter(tenant_key=ctx.tenant, pk__in=seen, status=IssueStatus.OPEN,
                                             severity=Severity.HIGH).select_related("namespace")
    if wanted and seen:
        if ctx.dry_run:
            # a dry run must not send notifications: count what would happen and stop
            ctx.count(ISSUES_SHEET, "incidents_would_open", eligible.filter(incident__isnull=True).count())
            ctx.note("ops_crosslink_incidents", {"requested": True, "dry_run": True})
        else:
            result = link_incidents(ctx.tenant, eligible)
            for key, amount in result.items():
                if amount and key != "forbidden":
                    ctx.count(ISSUES_SHEET, f"incidents_{key}", amount)
            ctx.note("ops_crosslink_incidents", {"requested": True, "dry_run": False, **result})
    else:
        ctx.note("ops_crosslink_incidents", {"requested": bool(wanted), "dry_run": ctx.dry_run})


register_sheet(ISSUES_SHEET, 110, import_issues)


# ----------------------------------------------------------------------------- incident cross-link
INCIDENT_SOURCE = "Kubernetes inventory"


def incident_text(issue):
    lines = [f"Client: {issue.client_display or 'not determined'}",
             f"Namespace: {issue.namespace.name}" if issue.namespace_id else "Namespace: cluster level",
             f"Resource: {issue.resource or '–'}", f"Issue: {issue.issue or '–'}"]
    for label, value in (("Evidence", issue.evidence), ("Impact", issue.impact),
                         ("Recommended action", issue.recommended_action)):
        if value:
            lines.append(f"{label}: {value}")
    lines.append(f"Workbook issue id: {issue.issue_id}")
    return "\n".join(lines)


def link_incidents(tenant, issues, allowed=None):
    """Open (or update) a ServiceDesk incident for each open High issue in `issues`.

    The incident carries the fingerprint ``k8sinv:<issue id>``, so an issue that already has an open incident
    updates that incident instead of opening a second one. The incident is stored on the issue.
    ``allowed`` is an optional ``f(site) -> bool``; issues on a site it rejects are counted as ``forbidden``.

    Returns ``{"opened", "updated", "skipped", "forbidden"}`` – ``skipped`` are issues that are not open or
    not High.
    """
    from servicedesk.models import Ticket
    from servicedesk.services import open_incident

    out = {"opened": 0, "updated": 0, "skipped": 0, "forbidden": 0}
    for issue in issues:
        if issue.status != IssueStatus.OPEN or issue.severity != Severity.HIGH:
            out["skipped"] += 1
            continue
        if allowed is not None and not allowed(issue.site):
            out["forbidden"] += 1
            continue
        already = Ticket.objects.for_tenant().open().filter(kind="incident", fingerprint=issue.fingerprint).exists()
        title = f"Kubernetes: {issue.issue or 'issue'} — {issue.resource or issue.issue_id}"
        site = "" if is_global(issue.site) else issue.site
        ticket = open_incident(title[:200], description=incident_text(issue), priority="P2", site=site,
                               source=INCIDENT_SOURCE, fingerprint=issue.fingerprint,
                               client=issue.client_display[:100])
        if issue.incident_id != ticket.pk:
            issue.incident = ticket
            with transaction.atomic():
                issue.save(update_fields=["incident", "updated_at"])
        out["updated" if already else "opened"] += 1
    return out
