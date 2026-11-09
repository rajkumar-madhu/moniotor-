"""The operations home (the dashboard): "what needs me right now?" first.

``build(request)`` assembles everything the page shows from the shared services (core.overview, telemetry.fleet,
bodeod, servicedesk) as plain, JSON-serialisable dicts, so the same data renders the template and feeds the small
refresh endpoint (``core.views.dashboard_data``). Nothing here talks to Prometheus on a cold dashboard: the fleet
panel reads the cache the Fleet page fills and says "open Fleet" when it is empty. History for the sparklines is
computed on request, from the databases LinkedEye already keeps, and cached for SPARK_CACHE_SECONDS.

Every figure respects the user's sites (core.access) and the site and client pickers (core.overview.focus_sites).
"""
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from . import overview
from .access import can_on, tenant_wide
from .client_scope import active_client

RANGES = (("15m", "15 min", 900), ("1h", "1 h", 3600), ("6h", "6 h", 6 * 3600), ("24h", "24 h", 86400))
DEFAULT_RANGE = "1h"
SPARK_POINTS = 30
SPARK_CACHE_SECONDS = 30
PROBLEM_ROWS = 50
HEAT_SITE_ROWS = 12            # heat-map rows with data shown before the card scrolls
CRIT_STATUSES = {"down", "critical", "Expired", "Invalid"}
SOURCE_ICONS = {"URL": "link", "Domain": "globe", "Device": "network", "TCP": "plug", "Alert": "bell"}
FLEET_CACHE_KEYS = ("telemetry:hosts:v1", "telemetry:targets:v1", "telemetry:fleet:os:v1")


def range_key(raw):
    return raw if raw in {k for k, _l, _s in RANGES} else DEFAULT_RANGE


def range_seconds(key):
    return next(s for k, _l, s in RANGES if k == key)


def _ago(when):
    from .templatetags.le import ago
    return ago(when)


# ------------------------------------------------------------------ needs attention + banner
def severity_of(problem):
    return "crit" if problem["status"] in CRIT_STATUSES else "warn"


def problem_rows(user, problems, limit=PROBLEM_ROWS):
    """The Needs attention table: one dict per problem, ready to render (and to send as JSON)."""
    from django.urls import reverse
    rows = []
    for p in problems[:limit]:
        row = {"kind": p["kind"], "icon": SOURCE_ICONS.get(p["kind"], "alert"), "name": p["name"], "href": p["href"],
               "site": p["site"] or "", "status": p["status"], "tone": severity_of(p),
               "status_label": str(p["status"]).replace("_", " ").title(),
               "detail": (p["detail"] or "")[:90], "since": _ago(p["when"]), "ack_url": ""}
        if p.get("alert_pk") and not p.get("acknowledged") and can_on(user, "operate", p["site"]):
            row["ack_url"] = reverse("telemetry:alert_ack", args=[p["alert_pk"]])
        rows.append(row)
    return rows


def banner(problems, desk, scope, now):
    """One status line: operational / N need attention / critical."""
    crit = sum(1 for p in problems if severity_of(p) == "crit")
    n = len(problems)
    if crit:
        tone, title = "crit", f"Critical: {crit} down or critical"
        detail = f"{n} item{'s' if n != 1 else ''} need attention" + (f", {n - crit} degraded" if n > crit else "")
    elif n:
        tone, title = "warn", f"{n} item{'s' if n != 1 else ''} need{'' if n != 1 else 's'} attention"
        detail = "Nothing is down; the rest is degraded or expiring"
    else:
        tone, title, detail = "ok", "All systems operational", "Everything monitored is healthy"
    p1 = desk.get("p12")
    if p1:
        detail += f" · {p1} open incident{'s' if p1 != 1 else ''} at P1 or P2"
    return {"tone": tone, "title": title, "detail": detail, "scope": scope, "crit": crit, "warn": n - crit,
            "time": now.strftime("%a %-d %b, %H:%M"), "iso": now.isoformat()}


# ------------------------------------------------------------------ fleet panel (cache only)
def fleet_panel(user, focus=None):
    """Host states and the busiest hosts, from the caches the Fleet page fills. Never calls Prometheus: when the
    caches are empty it returns ``{"loaded": False}`` and the template points at the Fleet page. ``focus`` (the site
    names the site and client pickers selected, or None) narrows the hosts like every other figure on the page."""
    from . import prometheus
    from django.urls import reverse
    out = {"loaded": False, "configured": prometheus.configured(), "href": overview._url("telemetry:fleet"),
           "overview_href": overview._url("telemetry:overview")}
    if any(cache.get(k) is None for k in FLEET_CACHE_KEYS):
        return out
    try:
        from telemetry import fleet
        rows = fleet.scope_hosts(user, fleet.host_states(use_cache=True)["hosts"])
        if focus is not None:
            rows = [h for h in rows if (h.get("site") or "") in focus]
    except Exception:   # a module that is missing or half-migrated just leaves the panel empty
        return out
    counts = fleet.state_counts(rows)
    total = len(rows)
    segs = []
    for key in fleet.STATES:
        n = counts.get(key, 0)
        segs.append({"key": key, "label": fleet.STATE_LABEL[key], "count": n, "tone": fleet.STATE_TONE[key],
                     "pct": round(100.0 * n / total, 1) if total else 0,
                     "href": f"{out['href']}?state={key}" if out["href"] else ""})

    def top(field):
        have = [h for h in rows if isinstance(h.get(field), (int, float)) and h.get("state") == fleet.LIVE]
        have.sort(key=lambda h: h[field], reverse=True)
        return [{"name": h["name"], "value": round(h[field], 1), "href": h.get("url") or "",
                 "tone": "crit" if h[field] >= 90 else "warn" if h[field] >= 75 else "ok"} for h in have[:5]]

    out.update(loaded=True, total=total, live=counts.get(fleet.LIVE, 0), segments=segs,
               top_cpu=top("cpu"), top_memory=top("memory"))
    return out


# ------------------------------------------------------------------ KPI cards
def check_health(matrix):
    """(healthy, total) across URL, TCP and domain checks."""
    ok = total = 0
    for t in matrix["totals"]:
        if t["key"] in ("url", "tcp", "domain"):
            ok += t["ok"]
            total += t["total"]
    return ok, total


def bod_eod_state(md):
    """('2/3', 'BOD passed, EOD pending') for the BOD / EOD windows of today; None without the module."""
    if not md or not md["modes"]:
        return None
    modes = md["modes"]
    passed = sum(1 for m in modes if m["status"] == "pass")
    bad = [m for m in modes if m["status"] in ("fail", "missed")]
    warn = [m for m in modes if m["status"] == "warn"]
    words = {"pass": "passed", "warn": "warnings", "fail": "failed", "missed": "no run", "running": "running", "pending": "pending"}
    summary = ", ".join(f"{m['code']} {words.get(m['status'], m['status'])}" for m in modes[:3])
    tone = "crit" if bad else "warn" if warn else "ok" if passed == len(modes) else "info"
    return {"value": f"{passed}/{len(modes)}", "sub": summary, "tone": tone}


def kpi_cards(problems, desk, matrix, md, fleet):
    cards = []
    n = len(problems)
    crit = sum(1 for p in problems if severity_of(p) == "crit")
    cards.append({"key": "attention", "label": "Needs attention", "value": str(n), "icon": "alert",
                  "sub": (f"{crit} down or critical, {n - crit} degraded" if n else "Nothing down or degraded"),
                  "tone": "crit" if crit else "warn" if n else "ok", "href": "#attention", "spark": False})
    if "incidents" in desk:
        cards.append({"key": "incidents", "label": "Open incidents", "value": str(desk["incidents"]), "icon": "headset",
                      "sub": f"{desk['p12']} at P1 or P2", "tone": "crit" if desk["p12"] else "info" if desk["incidents"] else "ok",
                      "href": desk["incidents_href"], "spark": True})
    if "alerts" in desk:
        cards.append({"key": "alerts", "label": "Alerts firing", "value": str(desk["alerts"]), "icon": "bell",
                      "sub": f"{desk['alerts_crit']} critical", "tone": "crit" if desk["alerts_crit"] else "warn" if desk["alerts"] else "ok",
                      "href": desk["alerts_href"], "spark": True})
    if fleet.get("loaded"):
        down = next((s["count"] for s in fleet["segments"] if s["key"] == "down"), 0)
        stale = next((s["count"] for s in fleet["segments"] if s["key"] == "stale"), 0)
        cards.append({"key": "hosts", "label": "Hosts up", "value": f"{fleet['live']}/{fleet['total']}", "icon": "radar",
                      "sub": f"{down} down, {stale} stale" if (down or stale) else "All reporting",
                      "tone": "crit" if down else "warn" if stale else "ok", "href": fleet["href"], "spark": False})
    elif fleet.get("href"):
        cards.append({"key": "hosts", "label": "Hosts up", "value": "–", "icon": "radar", "sub": "Open Fleet to load",
                      "tone": "mute", "href": fleet["href"], "spark": False})
    ok, total = check_health(matrix)
    pct = round(100.0 * ok / total) if total else None
    cards.append({"key": "health", "label": "Check health", "value": f"{pct}%" if pct is not None else "–", "icon": "check-circle",
                  "sub": f"{ok} of {total} URL, TCP and domain checks healthy" if total else "No checks yet",
                  "tone": "mute" if pct is None else "ok" if pct == 100 else "warn" if pct >= 90 else "crit",
                  "href": overview._url("urlchecker:index") or "#", "spark": total > 0})
    bod = bod_eod_state(md)
    if "breached" in desk:
        sub = (bod["sub"] if bod else "SLA breaches on open tickets")
        cards.append({"key": "sla", "label": "SLA breaches", "value": str(desk["breached"]), "icon": "clock",
                      "sub": sub, "tone": "crit" if desk["breached"] else "ok", "href": desk["breached_href"], "spark": False})
    elif bod:
        cards.append({"key": "sla", "label": "BOD / EOD", "value": bod["value"], "icon": "calendar", "sub": bod["sub"],
                      "tone": bod["tone"], "href": overview._url("bodeod:status") or "#", "spark": False})
    return cards


# ------------------------------------------------------------------ heat-map, service health, start card
def heatmap_view(matrix):
    """Rows with data, and the names of the sites that report nothing (collapsed under a disclosure)."""
    with_data, empty = [], []
    for r in matrix["rows"]:
        if any(c["c"] for c in r["cells"]) or r["alerts"]["n"]:
            with_data.append(r)
        else:
            empty.append(r["site"])
    return {"rows": with_data, "empty": empty}


def get_started(user, matrix):
    """The "Get started" checklist for a new install: shown to tenant administrators while nothing is monitored yet."""
    if any(t["total"] for t in matrix["totals"]) or matrix["rows"] and any(
            any(c["c"] for c in r["cells"]) for r in matrix["rows"]):
        return None
    if not tenant_wide(user, "admin"):
        return None
    steps = []
    from django.urls import NoReverseMatch, reverse

    def url(name):
        try:
            return reverse(name)
        except NoReverseMatch:
            return ""
    has_sites = bool(matrix["rows"])
    steps.append({"label": "Add a site", "done": has_sites, "url": url("administration:site_create"), "cta": "Add a site",
                  "detail": "Sites group devices, checks and people."})
    steps.append({"label": "Onboard servers and devices", "done": False, "url": url("onboarding:index"), "cta": "Start onboarding",
                  "detail": "Guided: site, server, device and Prometheus."})
    try:
        from administration import services
        checklist = services.onboarding_checklist()
        for s in checklist["steps"]:
            if s["key"] in ("prometheus", "targets"):
                steps.append({"label": s["label"], "done": s["done"], "url": s["url"], "cta": s["cta"], "detail": s["detail"]})
    except Exception:
        pass
    steps.append({"label": "Import your inventory", "done": False, "url": url("cmdb:items_import"), "cta": "Import a CSV",
                  "detail": "Bring existing configuration items in from a spreadsheet."})
    return {"steps": [s for s in steps if s["url"]], "done": sum(1 for s in steps if s["done"]), "total": len(steps)}


def legacy_backend_configured():
    """True when the legacy backend summary URL and token are set. No network: the card then loads it on its own."""
    from . import backend_summary
    try:
        endpoint, token = backend_summary._backend_credentials()
        return bool(backend_summary._safe_endpoint(endpoint) and token)
    except Exception:
        return False


# ------------------------------------------------------------------ the page
def build(request):
    """Everything the dashboard shows, for ``request`` (a signed-in user). Also hands the sidebar its badge counts."""
    user = request.user
    names = overview.site_names(user)
    request.le_site_names = names   # reused by the context processor
    site = request.GET.get("site", "")
    client = active_client(request)
    focus = overview.focus_sites(user, site, client, names)
    rng = range_key(request.GET.get("range", ""))
    probs = overview.problems(user, sites=focus)
    matrix = overview.site_matrix(user, site, names=names, client_param=client)
    desk = overview.desk_summary(user, sites=focus)
    md = overview.market_day(user, sites=focus)
    fleet = fleet_panel(user, focus)
    now = md["now"] if md else timezone.localtime()
    scope = site   # empty: the template falls back to the client name or "All sites"
    if focus is None:   # unfiltered numbers are the ones the sidebar badges show: no second set of queries for them
        urls = {t["key"]: t for t in matrix["totals"]}
        down = sum(urls[k]["bad"] for k in ("url", "tcp") if k in urls)
        request.le_badges = {"incidents": desk.get("incidents", 0), "alerts": desk.get("alerts", 0), "checks": down}
    start = get_started(user, matrix)
    head = banner(probs, desk, scope, now)
    if start:   # nothing is monitored yet: say so instead of claiming everything is fine
        head.update(tone="info", title="Nothing is monitored yet", detail="Finish the steps below to start seeing data here")
    return {
        "range": rng, "ranges": [{"key": k, "label": label} for k, label, _s in RANGES],
        "banner": head,
        "kpis": kpi_cards(probs, desk, matrix, md, fleet),
        "problems": problem_rows(user, probs), "problem_total": len(probs),
        "events": [{"severity": e.severity, "source": e.source, "title": e.title[:70],
                    "when": timezone.localtime(e.occurred_at).strftime("%d %b %H:%M")}
                   for e in overview.recent_events(user, 10, sites=focus)],
        "fleet": fleet, "matrix": matrix, "heat": heatmap_view(matrix), "md": md,
        "start": start,
        "legacy_backend": legacy_backend_configured(),
        "heatmap_url": overview._url("cmdb:heatmap") or overview._url("heatmap"),
        "focus": focus,
    }


def refresh_payload(request):
    """The small JSON the page polls: same dicts as the page, plus sparkline history for ``range``."""
    d = build(request)
    rng = d["range"]
    user = request.user
    return {"range": rng, "updated": timezone.now().isoformat(), "banner": d["banner"], "kpis": d["kpis"],
            "problems": d["problems"], "problem_total": d["problem_total"], "events": d["events"],
            "fleet": d["fleet"], "spark": spark_data(user, rng, d["focus"])}


def spark_payload(request):
    """Just the sparkline history for ``range`` (``?only=spark``): the page calls this once after it renders, because
    it already holds every current figure. Same scoping as refresh_payload; a few cheap queries, cached."""
    user = request.user
    focus = overview.focus_sites(user, request.GET.get("site", ""), active_client(request))
    rng = range_key(request.GET.get("range", ""))
    return {"range": rng, "updated": timezone.now().isoformat(), "spark": spark_data(user, rng, focus)}


# ------------------------------------------------------------------ sparkline history
def _edges(seconds, now):
    step = seconds / SPARK_POINTS
    t0 = now - timedelta(seconds=seconds)
    return [t0 + timedelta(seconds=step * (i + 1)) for i in range(SPARK_POINTS)]


def _count_open(rows, edges):
    """[count of (start, end) intervals open at each edge]; end None = still open."""
    out = []
    for edge in edges:
        out.append(sum(1 for start, end in rows if start <= edge and (end is None or end > edge)))
    return out


def spark_data(user, rng, focus):
    """``{"incidents": [...], "alerts": [...], "health": [...]}``: SPARK_POINTS values over the range, cached for
    SPARK_CACHE_SECONDS per user, range and picker selection. A series without history is left out."""
    import hashlib
    raw = "{}\x1f{}\x1f{}\x1f{}".format(settings.LINKEDEYE_TENANT, user.pk, rng,
                                         "*" if focus is None else "\x1e".join(sorted(focus)))
    key = "le:dash-spark:" + hashlib.sha256(raw.encode()).hexdigest()   # site names carry spaces and more: no raw cache keys
    hit = cache.get(key)
    if hit is not None:
        return hit
    now = timezone.now()
    seconds = range_seconds(rng)
    edges = _edges(seconds, now)
    t0 = now - timedelta(seconds=seconds)
    out = {}
    from django.db.models import Count, Q
    Ticket, Alert = overview.model("servicedesk", "Ticket"), overview.model("telemetry", "Alert")
    if Ticket:
        def incidents():
            qs = overview._scoped(Ticket, user, sites=focus).filter(kind="incident", opened_at__lte=now).filter(
                Q(resolved_at__isnull=True, closed_at__isnull=True) | Q(resolved_at__gt=t0) | Q(closed_at__gt=t0)
                | Q(resolved_at__isnull=True, closed_at__isnull=False, closed_at__gt=t0))
            rows = []
            for o, r, c in qs.values_list("opened_at", "resolved_at", "closed_at")[:5000]:
                ends = [x for x in (r, c) if x]
                rows.append((o, min(ends) if ends else None))
            return _count_open(rows, edges)
        series = overview._safe(incidents, None)
        if series is not None:
            out["incidents"] = series
    if Alert:
        def alerts():
            qs = overview._scoped(Alert, user, sites=focus).filter(Q(status="firing") | Q(ends_at__gt=t0))
            rows = []
            for s, c, e, st in qs.values_list("starts_at", "created_at", "ends_at", "status")[:5000]:
                rows.append(((s or c), e if st != "firing" else None))
            return _count_open(rows, edges)
        series = overview._safe(alerts, None)
        if series is not None:
            out["alerts"] = series

    def health():
        ok = [0] * SPARK_POINTS
        tot = [0] * SPARK_POINTS
        step = timedelta(seconds=seconds / SPARK_POINTS)
        for app_label, name, rel in (("urlchecker", "UrlResult", "url_check"), ("tcpcheck", "TcpResult", "tcp_check")):
            m = overview.model(app_label, name)
            if not m:
                continue
            from .access import scope_by_site
            qs = m.objects.filter(checked_at__gt=t0, checked_at__lte=now,
                                  **{f"{rel}__tenant_key": settings.LINKEDEYE_TENANT, f"{rel}__enabled": True})
            qs = scope_by_site(qs, user, field=f"{rel}__site")
            if focus is not None:
                qs = qs.filter(**{f"{rel}__site__in": list(focus)})
            aggs = {}
            for i in range(SPARK_POINTS):
                lo, hi = edges[i] - step, edges[i]
                cond = Q(checked_at__gt=lo, checked_at__lte=hi)
                aggs[f"t{i}"] = Count("pk", filter=cond)
                aggs[f"o{i}"] = Count("pk", filter=cond & Q(status="online"))
            r = qs.aggregate(**aggs)
            for i in range(SPARK_POINTS):
                tot[i] += r[f"t{i}"]
                ok[i] += r[f"o{i}"]
        if not any(tot):
            return None
        return [round(100.0 * ok[i] / tot[i], 1) if tot[i] else None for i in range(SPARK_POINTS)]
    series = overview._safe(health, None)
    if series is not None:
        out["health"] = series
    cache.set(key, out, SPARK_CACHE_SECONDS)
    return out
