"""Fleet home (Monitoring → Fleet): the Netdata room "Home" / Overview page for LinkedEye.

All page state lives in GET parameters (range, group, colour, state, env, job, q, site), so a view is
shareable and nothing is held in the browser. Every number comes from telemetry.fleet, which never raises:
when Prometheus is missing or broken the page still renders, with a banner saying what is unavailable.
"""
from django.shortcuts import render

from core.access import require, visible_sites

from .. import fleet as svc
from .. import hosts as hosts_svc
from ..fmt import finite
from .common import csv_response, matches, paginate, perms, prom_context, query_without_page, wants_refresh

GRID_MAX = 2000          # hard cap on status-grid cells, so a runaway fleet cannot blow up the page
LEADERBOARD = 10
CSV_HEADER = ["instance", "name", "state", "job", "cmdb_ci", "site", "environment", "client", "os",
              "cpu_pct", "memory_pct", "root_disk_pct", "load1", "cores", "uptime_s",
              "alerts_firing", "alerts_critical", "alerts_warning", "last_scrape", "scrape_interval"]


def _num(v, digits=2):
    return "" if not finite(v) else round(v, digits)


def _csv_row(h):
    return [h["instance"], h["name"], h["state"], h["job"], h["ci_name"], h["site"], h["environment_label"],
            h["client"], h["os"], _num(h.get("cpu")), _num(h.get("memory")), _num(h.get("disk")),
            _num(h.get("load1")), _num(h.get("cores"), 0), _num(h.get("uptime"), 0),
            h.get("alerts_firing", 0), h.get("alerts_critical", 0), h.get("alerts_warning", 0),
            h["last_scrape"].isoformat() if h.get("last_scrape") else "", h.get("interval") or ""]


def _grid(rows, group, colour):
    """[(group label, [cell])] for the node status grid, capped at GRID_MAX cells."""
    out, used = [], 0
    for label, hosts in svc.grouped(rows, group):
        cells = []
        for h in hosts:
            if used >= GRID_MAX:
                break
            used += 1
            hue, text = svc.colour_of(h, colour)
            title = f"{h['name']} · {h['state_label']}"
            if colour != "state":
                title += f" · {dict(svc.COLOURS)[colour]} {text}"
            cells.append({"url": h["url"], "title": title, "colour": hue, "state": h["state"]})
        if cells:
            out.append({"label": label, "count": len(hosts), "cells": cells})
        if used >= GRID_MAX:
            break
    return out, used


@require("view")
def fleet(request):
    ctx = {**prom_context(), **perms(request)}
    refresh = wants_refresh(request)
    use_cache = not refresh

    data = svc.host_states(use_cache=use_cache, with_prometheus=ctx["prom_configured"])
    hosts = svc.scope_hosts(request.user, data["hosts"])
    site = request.GET.get("site", "")
    if site:
        hosts = [h for h in hosts if (h.get("site") or "") == site]

    range_key, range_label, _seconds, start = svc.alert_window(request.GET.get("range", "24h"))
    window_alerts = list(svc.alerts_in_window(request.user, start, site=site))
    firing = list(svc.firing_alerts(request.user, site=site))
    svc.attach_alert_counts(hosts, svc.firing_by_instance(firing))

    targets, targets_error = ([], "")
    if ctx["prom_configured"]:
        targets, targets_error = svc.all_targets(use_cache=use_cache)
        targets = svc.scope_targets(request.user, targets)
    # series_count is the heaviest query: it always uses its 300 s cache, whatever ?refresh says
    series, series_error = svc.series_count() if ctx["prom_configured"] else (None, "")

    firing_total = len(firing)
    on_hosts = sum(h.get("alerts_firing", 0) for h in hosts)
    split = svc.severity_split(firing)

    group = svc.group_key(request.GET.get("group", ""))
    colour = svc.colour_key(request.GET.get("colour", "state"))

    # filters: the segment links and the toolbar narrow the grid and the table; the bars and counter cards
    # always describe the whole (site-scoped) fleet, so the next segment is always one click away.
    state, env, job = request.GET.get("state", ""), request.GET.get("env", ""), request.GET.get("job", "")
    q = request.GET.get("q", "")
    if state not in svc.STATES:
        state = ""
    rows = [h for h in hosts
            if (not state or h["state"] == state)
            and (not env or (h.get("environment") or "") == env)
            and (not job or h["job"] == job)
            and matches(q, h["instance"], h["ci_name"], h["job"], h["site"], h["os"], h["client"])]

    if request.GET.get("format") == "csv":
        return csv_response("fleet-hosts.csv", CSV_HEADER, [_csv_row(h) for h in rows])

    grid, grid_shown = _grid(rows, group, colour)
    page = paginate(request, rows)
    counts = svc.state_counts(hosts)
    errors = dict(data["errors"])
    if targets_error:
        errors["targets"] = targets_error
    if series_error:
        errors["series"] = series_error

    ctx.update({
        "hosts_total": len(hosts), "rows_total": len(rows), "page": page,
        "counters": svc.counters(hosts, targets, series, firing_total),
        "state_bar": svc.state_bar(hosts), "env_bar": svc.environment_bar(hosts),
        "alert_bar": svc.alert_bar(split), "alert_split": split, "alerts_on_hosts": on_hosts,
        "states": [{"key": s, "label": svc.STATE_LABEL[s], "tone": svc.STATE_TONE[s],
                    "help": svc.STATE_HELP[s], "count": counts[s]} for s in svc.STATES],
        "groups": svc.GROUPS, "colours": svc.COLOURS, "group": group, "colour": colour,
        "grid": grid, "grid_shown": grid_shown, "grid_capped": grid_shown < len(rows),
        "legend": svc.colour_legend(colour),
        "range_key": range_key, "range_label": range_label,
        "ranges": [(k, v[0]) for k, v in svc.RANGES.items()],
        "alert_leaders": svc.host_alert_leaders(window_alerts, hosts, start, limit=LEADERBOARD),
        "top_alerts": svc.top_alerts(window_alerts, start, limit=LEADERBOARD),
        "window_total": len(window_alerts),
        "sites": svc.site_rollup(hosts, window_alerts),
        "jobs": sorted({h["job"] for h in hosts if h["job"]}),
        "environments": [(k, v) for k, v in svc.ENV_LABEL.items()],
        "errors": errors, "error_text": " ".join(sorted(set(errors.values()))),
        "filtered": bool(state or env or job or q),
        "filter_state": state, "filter_env": env,
        "query": query_without_page(request, "refresh", "format"),
        "q_range": query_without_page(request, "refresh", "format", "range"),
        "q_filter": query_without_page(request, "refresh", "format", "state", "env", "job", "q"),
        "q_grid": query_without_page(request, "refresh", "format", "group", "colour"),
        "q_table": query_without_page(request, "refresh", "format", "state", "env", "job", "q"),
        "scoped": visible_sites(request.user) is not None,
        "thresholds": hosts_svc.thresholds(), "cache_seconds": svc.CACHE_SECONDS,
    })
    return render(request, "telemetry/fleet.html", ctx)
