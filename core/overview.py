"""Cross-module numbers for the dashboard, the alert badge and the site picker.

Every module is optional: its model is looked up lazily, and a module that is missing (or whose tables are not
migrated) simply contributes nothing. Rows are limited to the user's sites.
"""
from django.apps import apps
from django.conf import settings
from django.core.exceptions import FieldError
from django.db import DatabaseError
from django.urls import NoReverseMatch, reverse
from urllib.parse import urlencode

from .access import scope_by_site, visible_sites


def model(app_label, name):
    try:
        return apps.get_model(app_label, name)
    except LookupError:
        return None


def _safe(fn, default):
    try:
        return fn()
    except (DatabaseError, FieldError):
        return default


def _url(name, *args):
    try:
        return reverse(name, args=args)
    except NoReverseMatch:
        return ""


def _tenant(qs):
    return qs.filter(tenant_key=settings.LINKEDEYE_TENANT) if hasattr(qs.model, "tenant_key") else qs


def _scoped(m, user, field="site"):
    qs = _tenant(m.objects.all())
    return scope_by_site(qs, user, field=field) if user is not None else qs


# ------------------------------------------------------------------ problems ("needs attention")
def problems(user, limit=None):
    """Everything currently down or degraded, newest first. Each item: kind, name, status, detail, when, href."""
    out = []
    UrlCheck = model("urlchecker", "UrlCheck")
    if UrlCheck:
        for c in _safe(lambda: list(_scoped(UrlCheck, user).filter(enabled=True, last_status__in=["down", "degraded"])), []):
            out.append({"kind": "URL", "name": c.name, "status": c.last_status, "detail": c.last_error,
                        "when": c.last_checked_at, "href": _url("urlchecker:detail", c.pk), "site": c.site})
    Domain = model("domainmonitor", "Domain")
    if Domain:
        qs = _safe(lambda: list(_scoped(Domain, user, "site_key").filter(is_active=True).exclude(
            status="up", ssl_status__in=["Valid", "Unknown"])), [])
        for d in qs:
            if d.status != "down" and d.ssl_status not in ("Expiring", "Expired", "Invalid"):
                continue
            out.append({"kind": "Domain", "name": d.hostname, "status": d.status if d.status == "down" else d.ssl_status,
                        "detail": d.last_error or f"SSL {d.ssl_status}", "when": d.last_checked_at,
                        "href": f"{_url('domains:list')}?selected={d.pk}", "site": d.site_key})
    Device = model("network", "Device")
    if Device:
        for d in _safe(lambda: list(_scoped(Device, user).filter(enabled=True, status="down")), []):
            out.append({"kind": "Device", "name": d.name, "status": "down", "detail": d.last_error,
                        "when": d.last_checked_at, "href": d.get_absolute_url(), "site": d.site})
    TcpCheck = model("tcpcheck", "TcpCheck")
    if TcpCheck:
        for c in _safe(lambda: list(_scoped(TcpCheck, user).filter(enabled=True, last_status__in=["down", "degraded"])), []):
            out.append({"kind": "TCP", "name": f"{c.name} ({c.host}:{c.port})", "status": c.last_status,
                        "detail": c.last_error, "when": c.last_checked_at, "href": _url("tcpcheck:detail", c.pk),
                        "site": c.site})
    Alert = model("telemetry", "Alert")
    if Alert:
        for a in _safe(lambda: list(_scoped(Alert, user).filter(status="firing", severity="critical")
                                    .order_by("-starts_at")[:50]), []):
            out.append({"kind": "Alert", "name": a.alertname, "status": "critical", "detail": a.summary or a.instance,
                        "when": a.starts_at, "href": _url("telemetry:alert_detail", a.pk), "site": a.site})
    out.sort(key=lambda p: p["when"].timestamp() if p["when"] else 0, reverse=True)
    return out[:limit] if limit else out


def problem_count(user):
    """Same population as problems(), counted in the database (used on every page for the bell badge)."""
    from django.db.models import Q
    total = 0
    checks = [
        ("urlchecker", "UrlCheck", "site", Q(enabled=True, last_status__in=["down", "degraded"])),
        ("domainmonitor", "Domain", "site_key",
         Q(is_active=True) & (Q(status="down") | Q(ssl_status__in=["Expiring", "Expired", "Invalid"]))),
        ("network", "Device", "site", Q(enabled=True, status="down")),
        ("tcpcheck", "TcpCheck", "site", Q(enabled=True, last_status__in=["down", "degraded"])),
        ("telemetry", "Alert", "site", Q(status="firing", severity="critical")),
    ]
    for app_label, name, field, cond in checks:
        m = model(app_label, name)
        if m:
            total += _safe(lambda: _scoped(m, user, field).filter(cond).count(), 0)
    return total


# ------------------------------------------------------------------ KPIs
def kpis(user):
    """Dashboard tiles; a module that is not installed is left out."""
    tiles = []
    Ticket = model("servicedesk", "Ticket")
    if Ticket:
        def incidents():
            qs = _scoped(Ticket, user).filter(kind="incident").open()
            return qs.count(), qs.filter(priority__in=["P1", "P2"]).count()
        n = _safe(incidents, None)
        if n is not None:
            tiles.append({"label": "Incidents", "value": n[0], "sub": f"Open · {n[1]} P1/P2", "icon": "alert",
                          "tile": "s-crit" if n[1] else "s-info", "href": _url("servicedesk:incidents")})
    Alert = model("telemetry", "Alert")
    if Alert:
        n = _safe(lambda: _scoped(Alert, user).filter(status="firing").count(), None)
        if n is not None:
            tiles.append({"label": "Alerts", "value": n, "sub": "Firing in Alertmanager", "icon": "bell",
                          "tile": "s-warn" if n else "s-ok", "href": _url("telemetry:alertmanager")})

    def ratio(label, m, up_q, total_q, icon, href, sub):
        if not m:
            return
        r = _safe(lambda: (up_q().count(), total_q().count()), None)
        if r is not None:
            up, total = r
            tiles.append({"label": label, "value": f"{up}/{total}", "sub": sub, "icon": icon,
                          "tile": "s-ok" if up == total else "s-warn", "href": href})

    UrlCheck, Domain = model("urlchecker", "UrlCheck"), model("domainmonitor", "Domain")
    Device, TcpCheck = model("network", "Device"), model("tcpcheck", "TcpCheck")
    ratio("URLs online", UrlCheck, lambda: _scoped(UrlCheck, user).filter(enabled=True, last_status="online"),
          lambda: _scoped(UrlCheck, user).filter(enabled=True), "link", _url("urlchecker:index"), "Enabled checks")
    ratio("Domains up", Domain, lambda: _scoped(Domain, user, "site_key").filter(is_active=True, status="up"),
          lambda: _scoped(Domain, user, "site_key").filter(is_active=True), "globe", _url("domains:overview"),
          "Active domains")
    if Device:
        ratio("Devices up", Device, lambda: _scoped(Device, user).filter(enabled=True, status="up"),
              lambda: _scoped(Device, user).filter(enabled=True), "network", _url("network:all"), "Monitored devices")
    if TcpCheck:
        ratio("TCP online", TcpCheck, lambda: _scoped(TcpCheck, user).filter(enabled=True, last_status="online"),
              lambda: _scoped(TcpCheck, user).filter(enabled=True), "plug", _url("tcpcheck:index"), "Enabled checks")
    return tiles


def recent_events(user, limit=10):
    Event = model("core", "Event")
    return _safe(lambda: list(_scoped(Event, user).exclude(severity="info").order_by("-occurred_at")[:limit]), [])


# ------------------------------------------------------------------ site picker
def site_names(user=None):
    """Site records plus any site name used by monitored objects (minus 'All Sites'), limited to the user's sites."""
    names = set()
    Site = model("core", "Site")
    if Site:
        names |= set(_safe(lambda: list(_tenant(Site.objects.all()).values_list("name", flat=True)), []))
    for app_label, name, field in (("urlchecker", "UrlCheck", "site"), ("domainmonitor", "Domain", "site_key"),
                                   ("network", "Device", "site"), ("tcpcheck", "TcpCheck", "site")):
        m = model(app_label, name)
        if m:
            names |= set(_safe(lambda: list(_tenant(m.objects.all()).values_list(field, flat=True).distinct()), []))
    names -= {"", "All Sites", None}
    allowed = visible_sites(user) if user is not None else None
    if allowed is not None:
        names &= allowed
    return sorted(names)


# ------------------------------------------------------------------ operations overview (dashboard)
MATRIX_KINDS = [
    # (key, label, app, model, site field, enabled filter, ok statuses, bad statuses, url name)
    ("url", "URL checks", "urlchecker", "UrlCheck", "site", {"enabled": True}, "last_status",
     ("online",), ("down",), "urlchecker:index"),
    ("tcp", "TCP services", "tcpcheck", "TcpCheck", "site", {"enabled": True}, "last_status",
     ("online",), ("down",), "tcpcheck:index"),
    ("domain", "Domains", "domainmonitor", "Domain", "site_key", {"is_active": True}, "status",
     ("up",), ("down",), "domains:list"),
    ("device", "Devices", "network", "Device", "site", {"enabled": True}, "status",
     ("up",), ("down",), "network:all"),
]
SHARED = "All sites"


def site_matrix(user, selected_site="", names=None):
    """Per site, per check kind: total / healthy / down / other, plus firing alerts. Records with no site or
    'All Sites' are grouped under one shared row. Returns {"kinds": [...], "rows": [...], "totals": {...}}.
    `selected_site` narrows to one site; a site the user cannot see yields no rows (same as backend_summary).
    Pass `names` (the user's site_names) when the caller already has them, to avoid recomputing."""
    from django.db.models import Count, Max, Q
    names = site_names(user) if names is None else names
    kinds, cells, updates_by_site = [], {}, {}
    for key, label, app_label, name, field, enabled, status_field, ok, bad, url_name in MATRIX_KINDS:
        m = model(app_label, name)
        if not m:
            continue
        rows = _safe(lambda: list(_scoped(m, user, field).filter(**enabled).values(field).annotate(
            total=Count("pk"), ok=Count("pk", filter=Q(**{f"{status_field}__in": ok})),
            bad=Count("pk", filter=Q(**{f"{status_field}__in": bad})),
            last_update=Max("last_checked_at"))), None)
        if rows is None:
            continue
        kinds.append({"key": key, "label": label, "href": _url(url_name)})
        for r in rows:
            site = r[field] if r[field] not in ("", "All Sites", None) else SHARED
            site_cells = cells.setdefault(site, {})
            site_cells[key] = {"total": r["total"], "ok": r["ok"], "bad": r["bad"],
                               "other": r["total"] - r["ok"] - r["bad"]}
            if r["last_update"] and (site not in updates_by_site or r["last_update"] > updates_by_site[site]):
                updates_by_site[site] = r["last_update"]
    Alert = model("telemetry", "Alert")
    alerts = {}
    if Alert:
        for r in _safe(lambda: list(_scoped(Alert, user).filter(status="firing").values("site").annotate(
                n=Count("pk"), crit=Count("pk", filter=Q(severity="critical")))), []):
            site = r["site"] if r["site"] not in ("", "All Sites", None) else SHARED
            a = alerts.setdefault(site, {"n": 0, "crit": 0})
            a["n"] += r["n"]
            a["crit"] += r["crit"]
    if selected_site:
        sites = [selected_site] if selected_site in names else []
    else:
        sites = sorted(set(cells) | set(alerts) | set(names), key=lambda s: (s == SHARED, s.lower()))
    out, totals = [], {k["key"]: {"total": 0, "ok": 0, "bad": 0, "other": 0} for k in kinds}
    for site in sites:
        row = {"site": site, "cells": [], "alerts": alerts.get(site, {"n": 0, "crit": 0}), "bad": 0,
               "last_update": updates_by_site.get(site)}
        for k in kinds:
            c = cells.get(site, {}).get(k["key"])
            if c:
                for f in ("total", "ok", "bad", "other"):
                    totals[k["key"]][f] += c[f]
                row["bad"] += c["bad"]
                tone = "crit" if c["bad"] else ("warn" if c["other"] else "ok")
            else:
                tone = "none"
            q = "" if site == SHARED else "?" + urlencode({"site": site})
            row["cells"].append({"kind": k["key"], "c": c, "tone": tone, "href": (k["href"] + q) if k["href"] else ""})
        row["tone"] = "crit" if row["bad"] or row["alerts"]["crit"] else (
            "warn" if any(x["tone"] == "warn" for x in row["cells"]) or row["alerts"]["n"] else "ok")
        out.append(row)
    out.sort(key=lambda r: ({"crit": 0, "warn": 1, "ok": 2}[r["tone"]], r["site"] == SHARED, r["site"].lower()))
    summaries = []
    for kind in kinds:
        summary = dict(totals[kind["key"]], key=kind["key"], label=kind["label"], href=kind["href"])
        total = summary["total"]
        summary["ok_pct"] = round(summary["ok"] * 100 / total, 1) if total else 0
        summary["bad_end_pct"] = round((summary["ok"] + summary["bad"]) * 100 / total, 1) if total else 0
        summaries.append(summary)
    return {"kinds": kinds, "rows": out, "totals": summaries}


def site_map_points(user, rows, selected_site=""):
    """Return authorized sites with explicitly configured coordinates and current status. With `selected_site`
    (the same filter passed to site_matrix) only that site is plotted, so unfiltered sites never show as 'none'."""
    from .models import Site

    status_by_name = {row["site"]: row["tone"] for row in rows}
    qs = _scoped(Site, user, field="name")
    if selected_site:
        qs = qs.filter(name=selected_site)
    points = []
    for site in qs.filter(latitude__isnull=False, longitude__isnull=False).only(
            "name", "latitude", "longitude"):
        points.append({
            "name": site.name,
            "latLng": [float(site.latitude), float(site.longitude)],
            "tone": status_by_name.get(site.name, "none"),
        })
    return points


DAY_START_H, DAY_END_H = 6, 24   # the market-day strip spans 06:00–24:00 IST


def _strip_pct(t):
    """Position of a time-of-day on the strip, 0–100."""
    h = t.hour + t.minute / 60 + t.second / 3600
    return round(max(0.0, min(100.0, (h - DAY_START_H) * 100 / (DAY_END_H - DAY_START_H))), 2)


def market_day(user, now=None):
    """Today's trading day in IST: phase bands, BOD/EOD windows with their latest run, critical/warning events
    placed on a 06:00–24:00 strip, and a one-line state. None when the BOD/EOD module is not installed."""
    from datetime import datetime, time as dtime
    from django.utils import timezone
    try:
        from bodeod import services as bo
        from bodeod.models import Run
    except ImportError:
        return None
    n = bo.now_ist(now)
    today = n.date()
    trading, reason = _safe(lambda: bo.trading_day(today), (today.weekday() < 5, ""))
    phase = bo.market_phase(now)
    phases = [{"key": k, "label": label, "start": s, "end": e, "left": _strip_pct(s),
               "width": round(_strip_pct(e) - _strip_pct(s), 2), "current": phase["key"] == k}
              for k, label, s, e in bo.PHASES]
    modes = []
    for mode in _safe(lambda: list(scope_by_site(bo.modes_qs().filter(enabled=True), user)
                                   .order_by("window_start")), []):
        run = _safe(lambda: Run.objects.filter(mode=mode, trading_date=today).order_by("-started_at").first(), None)
        state = bo.window_state(mode, now, trading) if trading else "no_trading"
        status = run.status if run else ("missed" if state == "closed" else "pending")
        modes.append({"code": mode.code, "name": mode.name, "site": mode.site, "start": mode.window_start,
                      "end": mode.window_end, "left": _strip_pct(mode.window_start),
                      "width": max(1.5, round(_strip_pct(mode.window_end) - _strip_pct(mode.window_start), 2)),
                      "status": status, "state": state, "run": run,
                      "href": _url("bodeod:detail", run.pk) if run else _url("bodeod:status")})
    lanes = []  # greedy lanes so window labels never overlap (a label needs ~9% of the strip)
    for m in modes:
        span_end = m["left"] + max(m["width"], 9.0)
        for i, last in enumerate(lanes):
            if last <= m["left"]:
                m["lane"], lanes[i] = i, span_end
                break
        else:
            m["lane"] = len(lanes)
            lanes.append(span_end)
    Event = model("core", "Event")
    start = timezone.make_aware(datetime.combine(today, dtime(DAY_START_H)), bo.IST)
    marks = []
    if Event:
        evs = _safe(lambda: list(_scoped(Event, user).filter(occurred_at__gte=start, severity__in=["critical", "warning"])
                                 .order_by("occurred_at")[:400]), [])
        for e in evs:
            t = timezone.localtime(e.occurred_at, bo.IST).time()
            marks.append({"left": _strip_pct(t), "severity": e.severity, "title": e.title, "at": t})
    hours = [{"h": h, "left": _strip_pct(dtime(h % 24)) if h < 24 else 100.0} for h in range(DAY_START_H, DAY_END_H + 1, 2)]
    now_left = _strip_pct(n.time()) if n.hour >= DAY_START_H else None
    upcoming = next((p for p in bo.PHASES if n.time() < p[2]), None)
    if not trading:
        nxt = _safe(lambda: bo.next_trading_day(today), None)
        line = f"{reason}. Next trading day: {nxt:%A %-d %B}." if nxt else f"{reason}."
    elif phase["key"] != "closed":
        line = f"{phase['label']} until {phase['end']:%H:%M} IST."
    elif upcoming:
        line = f"Market closed. {upcoming[1]} opens at {upcoming[2]:%H:%M} IST."
    else:
        line = "Market closed for the day."
    return {"now": n, "trading": trading, "reason": reason, "phase": phase, "phases": phases, "modes": modes,
            "marks": marks, "crit": sum(1 for m in marks if m["severity"] == "critical"),
            "warn": sum(1 for m in marks if m["severity"] == "warning"), "hours": hours, "now_left": now_left,
            "line": line, "lanes": max(1, len(lanes))}


def desk_summary(user):
    """Open incidents (and P1/P2 among them) and firing alerts, for the overview header."""
    out = {}
    Ticket = model("servicedesk", "Ticket")
    if Ticket:
        r = _safe(lambda: (lambda qs: (qs.count(), qs.filter(priority__in=["P1", "P2"]).count()))(
            _scoped(Ticket, user).filter(kind="incident").open()), None)
        if r:
            out["incidents"], out["p12"] = r
            out["incidents_href"] = _url("servicedesk:incidents")
    Alert = model("telemetry", "Alert")
    if Alert:
        n = _safe(lambda: _scoped(Alert, user).filter(status="firing").count(), None)
        if n is not None:
            out["alerts"], out["alerts_href"] = n, _url("telemetry:alertmanager")
    return out
