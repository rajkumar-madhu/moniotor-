from django.apps import apps
from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.urls import reverse

from core import prometheus
from core.access import require, scope_by_site, visible_sites

from .. import discovery as disc, fleet, hosts as svc, hosts_full as full, palette as pal
from ..fmt import finite, human_duration, human_rate
from ..models import Alert
from .common import csv_response, matches, paginate, perms, prom_context, query_without_page

HEALTH_CHOICES = [("healthy", "Healthy"), ("warning", "Warning"), ("critical", "Critical"), ("down", "Down"),
                  ("unknown", "No data")]


def _site_ok(user, site, selected=""):
    if selected and site != selected:
        return False
    sites = visible_sites(user)
    return sites is None or site in ("", "All Sites") or site in sites


def visible_rows(user, rows, key="instance"):
    """Rows (hosts, scrape targets) the user may see: the site of the CMDB item whose prometheus_instance is the
    row's instance must be one of the user's sites (rows without a CMDB item, or on "All Sites", stay visible)."""
    if visible_sites(user) is None:
        return list(rows)
    rows = list(rows)
    cis = svc.cmdb_map([r[key] for r in rows])
    return [r for r in rows if _site_ok(user, (getattr(cis.get(r[key]), "site", "") or ""))]


def _attach_cmdb(hosts):
    cis = svc.cmdb_map([h["instance"] for h in hosts])
    for h in hosts:
        ci = cis.get(h["instance"])
        h["ci"] = ci
        h["ci_name"] = ci.name if ci is not None else ""
        h["ci_url"] = svc.ci_url(ci)
        h["site"] = (getattr(ci, "site", "") or "") if ci is not None else ""
    return hosts


@require("view")
def infrastructure(request):
    ctx = {**prom_context(), **perms(request), "error": "", "spark_error": ""}
    refresh = request.GET.get("refresh") == "1"
    hosts, sparks = [], {}
    if ctx["prom_configured"]:
        try:
            hosts = [dict(h) for h in svc.fetch_hosts(use_cache=not refresh)]
        except prometheus.PrometheusError as e:
            ctx["error"] = str(e)
        if hosts:
            try:
                sparks = svc.cpu_sparklines(use_cache=not refresh)
            except prometheus.PrometheusError as e:
                ctx["spark_error"] = str(e)
    # live / down / stale / never_seen comes from the same rule the Fleet page uses, so the two agree
    tmap, tmap_error = fleet.node_target_map(use_cache=not refresh) if ctx["prom_configured"] else ({}, "")
    fleet.annotate_states(hosts, tmap, discovered=fleet.discovered_hosts() if ctx["prom_configured"] else ())
    ctx["state_error"] = tmap_error
    _attach_cmdb(hosts)
    site = request.GET.get("site", "")
    hosts = [h for h in hosts if _site_ok(request.user, h["site"], site)]
    summary = svc.summarize(hosts)
    jobs = sorted({h["job"] for h in hosts if h["job"]})

    q, health, job = request.GET.get("q", ""), request.GET.get("health", ""), request.GET.get("job", "")
    rows = [h for h in hosts if matches(q, h["instance"], h["ci_name"], h["job"])
            and (not health or h["health"] == health) and (not job or h["job"] == job)]

    if request.GET.get("format") == "csv":
        return csv_response("infrastructure-hosts.csv",
                            ["instance", "job", "cmdb_ci", "site", "state", "health", "up", "cpu_pct", "memory_pct",
                             "root_disk_pct", "load1", "cores", "net_rx_Bps", "net_tx_Bps", "uptime_s"],
                            [[h["instance"], h["job"], h["ci_name"], h["site"], h["state"], h["health"], h["up"],
                              _r(h.get("cpu")), _r(h.get("memory")), _r(h.get("disk")), _r(h.get("load1")),
                              _r(h.get("cores"), 0), _r(h.get("rx"), 0), _r(h.get("tx"), 0), _r(h.get("uptime"), 0)]
                             for h in rows])

    page = paginate(request, rows)
    for i, h in enumerate(page):
        vals = sparks.get(h["instance"]) or []
        h["spark"] = {"values": vals, "color": pal.CRIT if h["health"] in ("critical", "down") else
                      pal.WARN if h["health"] == "warning" else pal.BLUE} if len(vals) >= 2 else None
        h["spark_id"] = f"spark-{i}"
        h["rx_text"], h["tx_text"], h["uptime_text"] = human_rate(h.get("rx")), human_rate(h.get("tx")), human_duration(h.get("uptime"))
    c = summary["counts"]
    ctx.update({
        "summary": summary, "page": page, "jobs": jobs, "health_choices": HEALTH_CHOICES,
        "query": query_without_page(request, "refresh"), "filtered": bool(q or health or job),
        "donut": {"segments": [
            {"label": "Healthy", "value": c["healthy"], "color": pal.OK},
            {"label": "Warning", "value": c["warning"], "color": pal.WARN},
            {"label": "Critical", "value": c["critical"], "color": pal.CRIT},
            {"label": "Down", "value": c["down"], "color": pal.DOWN},
            {"label": "No data", "value": c["unknown"], "color": pal.STONE_LIGHT}],
            "center": {"value": str(summary["total"]), "label": "Hosts"}},
        "top_cpu": svc.top(hosts, "cpu"), "top_memory": svc.top(hosts, "memory"), "top_disk": svc.top(hosts, "disk"),
        "thresholds": svc.thresholds(),
        # discovered hosts that are not in the CMDB yet (the Discovery page is where they are reviewed)
        "undiscovered": disc.unlinked_host_count(request.user),
    })
    return render(request, "telemetry/infrastructure.html", ctx)


def _r(v, digits=2):
    return "" if not finite(v) else round(v, digits)


@require("view")
def host_detail(request, instance):
    ctx = {**prom_context(), **perms(request), "instance": instance, "error": "", "host": None}
    range_key = request.GET.get("range", "1h")
    if range_key not in svc.RANGES:
        range_key = "1h"
    ci = svc.cmdb_map([instance]).get(instance)
    site = (getattr(ci, "site", "") or "") if ci is not None else ""
    if not _site_ok(request.user, site):
        raise Http404("No such host")
    status = 200
    if ctx["prom_configured"]:
        try:
            ctx["host"] = svc.fetch_host(instance, range_key, use_cache=request.GET.get("refresh") != "1")
            if ctx["host"] is None:
                status = 404
        except prometheus.PrometheusError as e:
            ctx["error"] = str(e)
    # alerts carry their own site (label or CMDB): a host without a CI is global, its alerts may not be
    alerts = (scope_by_site(Alert.objects.for_tenant(), request.user, field="site")
              .filter(instance=instance).order_by("status", "-last_received_at")[:10])
    ctx.update({"ci": ci, "ci_url": svc.ci_url(ci), "site": site, "range_key": range_key,
                "ranges": [(k, v[0]) for k, v in svc.RANGES.items()], "alerts": alerts,
                "explore_q": svc.range_queries(instance)["cpu"]})
    return render(request, "telemetry/host_detail.html", ctx, status=status)


def _load_host_metrics(request, instance):
    """The one access + fetch path shared by host_metrics (HTML) and host_metrics_json (Live poll).

    Raises Http404 when the host's CMDB site is outside the user's sites (exactly like host_detail). Returns
    {range_key, ci, site, host, full, error, prom_configured}: host is None when Prometheus has no such instance,
    error is the Prometheus failure text, and both fetches honour the cache unless ?refresh=1."""
    range_key = request.GET.get("range", "1h")
    if range_key not in svc.RANGES:
        range_key = "1h"
    ci = svc.cmdb_map([instance]).get(instance)
    site = (getattr(ci, "site", "") or "") if ci is not None else ""
    if not _site_ok(request.user, site):
        raise Http404("No such host")
    d = {"range_key": range_key, "ci": ci, "site": site, "host": None, "full": None, "error": "",
         "prom_configured": prometheus.configured()}
    if d["prom_configured"]:
        use_cache = request.GET.get("refresh") != "1"
        try:
            d["host"] = svc.fetch_host(instance, range_key, use_cache=use_cache)
            if d["host"] is not None:
                d["full"] = full.fetch_host_full(d["host"], range_key, use_cache=use_cache)
        except prometheus.PrometheusError as e:
            d["error"] = str(e)
    return d


@require("view")
def host_metrics(request, instance):
    """Full node_exporter view of one host (Netdata-style): same scoping and error handling as host_detail."""
    d = _load_host_metrics(request, instance)
    ctx = {**prom_context(), **perms(request), "instance": instance, "error": d["error"], "host": d["host"],
           "full": d["full"]}
    status = 404 if (ctx["prom_configured"] and not d["error"] and d["host"] is None) else 200
    ci, range_key = d["ci"], d["range_key"]
    ctx.update({"ci": ci, "ci_url": svc.ci_url(ci), "site": d["site"], "range_key": range_key,
                "ranges": [(k, v[0]) for k, v in svc.RANGES.items()],
                "compute_installed": apps.is_installed("compute"), "cache_seconds": svc.CACHE_SECONDS,
                "vms": full.placed_vms(instance, request.user) if ctx["host"] else None,
                "live_url": reverse("telemetry:host_metrics_json", args=[instance]) + "?range=" + range_key,
                "toc": [("summary", "Summary"), ("compute", "Compute"), ("memory", "Memory"), ("storage", "Storage"),
                        ("network", "Network"), ("system", "System info"), ("workloads", "Containers & VMs")]})
    return render(request, "telemetry/host_metrics.html", ctx, status=status)


@require("view")
def host_metrics_json(request, instance):
    """Live-mode poll for host_metrics: the same loader (site scoping, 404, cache, ?refresh=1, ?range=) as the
    page, returned as JSON. Errors are {"error": text} with 404 (no such host) or 503 (Prometheus)."""
    def fail(text, status):
        return JsonResponse({"error": text}, status=status, headers={"Cache-Control": "no-store"})
    try:
        d = _load_host_metrics(request, instance)
    except Http404:
        return fail("No such host", 404)
    if not d["prom_configured"]:
        return fail("Prometheus is not configured", 503)
    if d["error"]:
        return fail(d["error"], 503)
    if d["host"] is None:
        return fail("No such host", 404)
    return JsonResponse(full.live_payload(d["host"], d["full"]), headers={"Cache-Control": "no-store"})
