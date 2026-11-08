"""Read-only tools the ops agent may call. Every lookup is site-scoped to the asking user."""
import logging

from django.db.models import Q

from core.access import scope_by_site
from core.overview import _safe, _url, model as overview_model, problems as overview_problems

log = logging.getLogger("linkedeye.agent")

TOOL_LIMIT = 12

_TOOLS = []


def _tool(name, description, properties=None, required=None):
    def deco(fn):
        fn.le_name = name
        fn.le_description = description
        fn.le_schema = {
            "type": "object",
            "properties": properties or {},
            "required": required or [],
            "additionalProperties": False,
        }
        _TOOLS.append(fn)
        return fn
    return deco


def openai_tools():
    return [{"type": "function", "function": {
        "name": fn.le_name, "description": fn.le_description, "parameters": fn.le_schema,
    }} for fn in _TOOLS]


def run_tool(name, args, ctx):
    fn = next((f for f in _TOOLS if f.le_name == name), None)
    if fn is None:
        return {"error": f"unknown tool {name}"}
    if not isinstance(args, dict):
        args = {}
    allowed = set(fn.le_schema.get("properties") or {})
    clean = {k: str(v)[:200] if v is not None else "" for k, v in args.items() if k in allowed}
    try:
        return fn(ctx, **clean)
    except Exception:
        log.exception("agent tool %s failed", name)
        return {"error": f"{name} failed"}


def _site_qs(qs, ctx, field="site"):
    qs = scope_by_site(qs, ctx.user, field=field)
    if ctx.site:
        qs = qs.filter(**{field: ctx.site})
    return qs


def _tenant(qs, ctx):
    if hasattr(qs.model, "tenant_key"):
        return qs.filter(tenant_key=ctx.tenant)
    return qs


@_tool("list_problems", "List checks, devices and alerts that are currently down, degraded or firing.")
def list_problems(ctx, **_kwargs):
    items = overview_problems(ctx.user, limit=40)
    if ctx.site:
        items = [p for p in items if p.get("site") in (ctx.site, "", "All Sites")]
    return {"items": [_problem_row(p) for p in items[:TOOL_LIMIT]], "count": len(items)}


@_tool("site_summary", "KPI snapshot for the operator's sites: incidents, alerts, URL/device/TCP health.")
def site_summary(ctx, **_kwargs):
    from core import overview
    tiles = overview.kpis(ctx.user)
    count = overview.problem_count(ctx.user)
    return {
        "site": ctx.site or "All sites the operator can see",
        "needs_attention": count,
        "tiles": [{"label": t["label"], "value": t["value"], "sub": t.get("sub", "")} for t in tiles],
    }


@_tool("search_events", "Search the unified event stream (probe state changes, alerts, jobs).",
       properties={"query": {"type": "string", "description": "Words to match in title or message."}})
def search_events(ctx, query=""):
    Event = overview_model("core", "Event")
    if not Event:
        return {"items": []}
    qs = _site_qs(_tenant(Event.objects.all(), ctx), ctx).exclude(severity="info")
    q = (query or "").strip()
    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(message__icontains=q) | Q(source__icontains=q))
    rows = _safe(lambda: list(qs.order_by("-occurred_at")[:TOOL_LIMIT]), [])
    return {"items": [{
        "when": r.occurred_at.isoformat() if r.occurred_at else "",
        "severity": r.severity, "source": r.source, "title": r.title,
        "message": (r.message or "")[:240], "site": r.site,
    } for r in rows]}


@_tool("search_inventory", "Search URL checks, domains, network devices, TCP checks and CMDB items.",
       properties={"query": {"type": "string", "description": "Name, host, IP or tag."}})
def search_inventory(ctx, query=""):
    q = (query or "").strip()
    if not q:
        return {"items": [], "hint": "Provide a name, host or IP."}
    items = []

    def add(app, name, field, cond, row):
        m = overview_model(app, name)
        if not m:
            return
        qs = _site_qs(_tenant(m.objects.filter(cond), ctx), ctx, field=field)
        for obj in _safe(lambda: list(qs[:8]), []):
            items.append(row(obj))

    add("urlchecker", "UrlCheck", "site",
        Q(name__icontains=q) | Q(url__icontains=q) | Q(tags__icontains=q),
        lambda c: {"kind": "URL", "name": c.name, "status": c.last_status, "detail": c.url,
                   "site": c.site, "href": _url("urlchecker:detail", c.pk)})
    add("domainmonitor", "Domain", "site_key",
        Q(hostname__icontains=q) | Q(description__icontains=q),
        lambda d: {"kind": "Domain", "name": d.hostname, "status": d.status,
                   "detail": f"SSL {d.ssl_status}", "site": d.site_key,
                   "href": f"{_url('domains:list')}?selected={d.pk}"})
    add("network", "Device", "site",
        Q(name__icontains=q) | Q(mgmt_ip__icontains=q) | Q(serial__icontains=q) | Q(tags__icontains=q),
        lambda d: {"kind": "Device", "name": d.name, "status": d.status,
                   "detail": f"{d.get_device_type_display()} {d.mgmt_ip}", "site": d.site,
                   "href": d.get_absolute_url()})
    add("tcpcheck", "TcpCheck", "site",
        Q(name__icontains=q) | Q(host__icontains=q),
        lambda c: {"kind": "TCP", "name": c.name, "status": c.last_status,
                   "detail": f"{c.host}:{c.port}", "site": c.site, "href": _url("tcpcheck:detail", c.pk)})
    add("cmdb", "ConfigurationItem", "site",
        Q(name__icontains=q) | Q(ip_address__icontains=q) | Q(serial__icontains=q) | Q(asset_tag__icontains=q),
        lambda c: {"kind": "CI", "name": c.name, "status": getattr(c, "status", ""),
                   "detail": c.get_ci_type_display(), "site": c.site, "href": c.get_absolute_url()})
    return {"items": items[:TOOL_LIMIT]}


@_tool("search_tickets", "Search service-desk incidents, problems, changes and requests.",
       properties={"query": {"type": "string", "description": "Ticket number, title or CI name."}})
def search_tickets(ctx, query=""):
    Ticket = overview_model("servicedesk", "Ticket")
    if not Ticket:
        return {"items": []}
    qs = _site_qs(_tenant(Ticket.objects.all(), ctx), ctx)
    q = (query or "").strip()
    if q:
        qs = qs.filter(Q(number__iexact=q) | Q(title__icontains=q) | Q(ci_name__icontains=q) | Q(description__icontains=q))
    else:
        qs = qs.open() if hasattr(qs, "open") else qs.exclude(status__in=["resolved", "closed", "cancelled"])
    rows = _safe(lambda: list(qs.order_by("-opened_at")[:TOOL_LIMIT]), [])
    return {"items": [{
        "number": r.number, "title": r.title, "kind": r.kind, "status": r.status,
        "priority": r.priority, "site": r.site, "ci": r.ci_name,
        "href": r.get_absolute_url(),
    } for r in rows]}


@_tool("search_runbooks", "Find automation runbooks by title, service, CI or symptoms.",
       properties={"query": {"type": "string", "description": "Service, symptom or runbook title."}})
def search_runbooks(ctx, query=""):
    Runbook = overview_model("automation", "Runbook")
    if not Runbook:
        return {"items": []}
    qs = _site_qs(_tenant(Runbook.objects.all(), ctx), ctx)
    q = (query or "").strip()
    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(service__icontains=q) | Q(ci_name__icontains=q)
                       | Q(symptoms__icontains=q) | Q(tags__icontains=q) | Q(steps__icontains=q))
    rows = _safe(lambda: list(qs[:TOOL_LIMIT]), [])
    return {"items": [{
        "title": r.title, "service": r.service, "ci": r.ci_name, "site": r.site,
        "symptoms": (r.symptoms or "")[:240], "href": r.get_absolute_url(),
    } for r in rows]}


@_tool("search_logs", "Search ingested application/host log lines.",
       properties={"query": {"type": "string", "description": "Words in the log message, host or source."}})
def search_logs(ctx, query=""):
    LogEntry = overview_model("observe", "LogEntry")
    if not LogEntry:
        return {"items": []}
    qs = _site_qs(_tenant(LogEntry.objects.all(), ctx), ctx)
    q = (query or "").strip()
    if q:
        qs = qs.filter(Q(message__icontains=q) | Q(host__icontains=q) | Q(source__icontains=q))
    rows = _safe(lambda: list(qs.order_by("-ts")[:TOOL_LIMIT]), [])
    return {"items": [{
        "when": r.ts.isoformat() if getattr(r, "ts", None) else "",
        "level": r.level, "source": r.source, "host": r.host, "site": r.site,
        "message": (r.message or "")[:240],
    } for r in rows]}


@_tool("retrieve_knowledge", "Retrieve indexed monitoring snippets (events, problems, tickets, runbooks) for a question.",
       properties={"query": {"type": "string", "description": "The operator's question."}})
def retrieve_knowledge(ctx, query=""):
    from .knowledge import retrieve
    hits = retrieve(ctx.user, ctx.site, query or "", limit=8)
    return {"items": hits}


def _problem_row(p):
    return {
        "kind": p.get("kind"), "name": p.get("name"), "status": p.get("status"),
        "detail": (p.get("detail") or "")[:240], "site": p.get("site"), "href": p.get("href") or "",
        "when": p["when"].isoformat() if p.get("when") else "",
    }


# import-time registration of tools
_ = (list_problems, site_summary, search_events, search_inventory, search_tickets,
     search_runbooks, search_logs, retrieve_knowledge)
