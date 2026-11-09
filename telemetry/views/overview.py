"""Fleet Overview (Monitoring → Overview): the Netdata room "Overview" page. See telemetry/overview.py.

All state is in GET parameters (range, last/unit, by, nodes, site, env, client_name, compare, live), so a URL is a share
link. Site scoping happens once, in telemetry.fleet.scope_hosts, before anything reaches a PromQL string.
"""
from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.urls import reverse

from core.access import require, visible_sites

from .. import fleet as fleet_svc
from .. import overview as ov
from ..models import severity_tone
from .common import _cell, csv_response, perms, prom_context, query_without_page, wants_refresh

NODE_LIST_MAX = 500        # checkboxes in the node picker; narrower filters (site / environment) get the rest
DRAWER_ALERTS = 8
SEV_RANK = {"crit": 0, "warn": 1}


def _alerts_for(user, params, selected_instances, known_instances):
    """Firing alerts for the drawer: site-scoped like the Fleet page, narrowed to the selected nodes when the view
    is filtered (an alert with no instance label always stays)."""
    rows = list(fleet_svc.firing_alerts(user, site=params.site)[:200])
    if params.filtered:
        rows = [a for a in rows if not a.instance or a.instance in selected_instances]
    else:
        rows = [a for a in rows if not a.instance or a.instance in known_instances]
    split = fleet_svc.severity_split(rows)
    rows.sort(key=lambda a: (SEV_RANK.get(severity_tone(a.severity), 2), -(a.last_received_at.timestamp())))
    return rows[:DRAWER_ALERTS], split


@require("view")
def overview(request):
    params = ov.Params(request.GET)
    page = ov.page_data(request.user, params, use_cache=not wants_refresh(request))

    if request.GET.get("format") == "csv":
        key = request.GET.get("chart", "")
        for sec in (page["charts"] or {}).get("sections", []):
            for c in sec["charts"]:
                if c["key"] == key and c["has"]:
                    header, rows = ov.csv_rows(c)
                    return csv_response(f"fleet-overview-{key}.csv", [_cell(h) for h in header], rows)
        raise Http404("No such chart")

    sel = page["selection"]
    hosts = page["hosts"]
    with_metrics = [h for h in hosts if h["state"] != fleet_svc.NEVER_SEEN]
    selected_instances = {h["instance"] for h in sel.hosts}
    alerts, alert_split = _alerts_for(request.user, params, selected_instances, {h["instance"] for h in hosts})

    ctx = {**prom_context(), **perms(request)}
    charts = page["charts"]
    sections = charts["sections"] if charts else []
    nodes_all = sorted(with_metrics, key=lambda h: (h["name"].lower(), h["instance"]))
    ctx.update({
        "params": params, "page": page, "sel": sel, "strip": page["strip"], "sections": sections,
        "charts_ready": bool(charts), "shown_total": sum(s["shown"] for s in sections),
        "missing_total": sum(len(s["missing"]) for s in sections),
        "uptime": page["uptime"], "uptime_n": page["uptime_n"],
        "prom_error": page["prom_error"], "fleet_errors": page["fleet_errors"],
        "states": [{"key": s, "label": fleet_svc.STATE_LABEL[s], "tone": fleet_svc.STATE_TONE[s],
                    "count": page["strip"]["counts"][s]} for s in fleet_svc.STATES],
        "ranges": [(k, v[0]) for k, v in ov.RANGES.items()], "range_key": params.range_key,
        "range_label": params.range_label, "custom_units": [(k, v[0] + "s") for k, v in ov.CUSTOM_UNITS.items()],
        "by_choices": ov.BY_CHOICES, "live_choices": ov.LIVE_CHOICES,
        "env_choices": list(fleet_svc.ENV_LABEL.items()),
        "site_choices": sorted({h["site"] for h in with_metrics if h.get("site")}),
        "client_choices": sorted({h["client"] for h in with_metrics if h.get("client")}),
        "node_options": nodes_all[:NODE_LIST_MAX], "nodes_capped": len(nodes_all) > NODE_LIST_MAX,
        "nodes_total": len(nodes_all), "chosen": set(params.nodes),
        "alerts": alerts, "alert_split": alert_split, "hot": ov.hot_nodes(sel.hosts),
        "drawer_nodes": sel.hosts[:100],
        "q_range": query_without_page(request, "refresh", "format", "chart", "range", "last", "unit"),
        "q_base": query_without_page(request, "refresh", "format", "chart"),
        "q_live": query_without_page(request, "refresh", "format", "chart", "live"),
        "json_url": reverse("telemetry:overview_json"),
        "live_interval": params.live or 30, "scoped": visible_sites(request.user) is not None,
        "call_budget": ov.MAX_QUERIES, "cache_seconds": ov.CACHE_SECONDS, "max_series": ov.MAX_SERIES,
        "by_label": dict(ov.BY_CHOICES).get(params.by, "Dimension"),
        "state_bar": fleet_svc.state_bar(hosts), "alerts_total": alert_split["total"],
        "explore_range": params.range_key if params.range_key in ov.hosts_svc.RANGES else "1h",
    })
    return render(request, "telemetry/overview.html", ctx)


@require("view")
def overview_json(request):
    """Live-mode poll: the chart payloads of the page for the same parameters (same scoping, caches and call budget),
    plus the node counts. Errors are {"error": text} so the page can show them without losing what it has."""
    params = ov.Params(request.GET)
    page = ov.page_data(request.user, params, use_cache=not wants_refresh(request))
    return JsonResponse(ov.live_payload(page), headers={"Cache-Control": "no-store"})
