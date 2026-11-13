"""Command Centre: one screen for the whole estate — assets, compute, Kubernetes, rack power / cooling / space,
incidents, alerts, hardware telemetry, threshold monitors and the event stream.

The layout follows the WeCrew InfraAsset Command Centre. Every figure is real data limited to the user's sites
(and to ``?site=`` when given); a block whose module or integration has no data says so instead of showing a number.
Other apps are imported lazily: a module that is missing, or whose tables are not migrated, drops out of the page.
"""
from datetime import timedelta

from django.conf import settings
from django.db import DatabaseError
from django.db.models import Avg, Count, Max, Q
from django.db.models.functions import TruncHour
from django.shortcuts import render
from django.utils import timezone

from . import overview
from .access import require

HISTORY_HOURS = 24
RACK_ROWS = 12
LIST_ROWS = 6
STREAM_ROWS = 10
GLOBAL_SITES = ("All Sites", "")


# ------------------------------------------------------------------ formatting / tones
def kw(value):
    """Power in kW like the DCIM pages: 2 decimals below 10 kW, 1 above; "–" when unknown."""
    if value is None:
        return "–"
    return f"{value:,.{2 if abs(value) < 10 else 1}f} kW"


def pct(value):
    return "–" if value is None else f"{value:.0f}%"


def temp(value):
    return "–" if value is None else f"{value:.1f} °C"


def load_tone(value):
    """Utilisation tone, same thresholds as DCIM: ok below 80 %, warn from 80 %, crit from 90 %."""
    if value is None:
        return "mute"
    return "crit" if value >= 90 else "warn" if value >= 80 else "ok"


def bar_width(value):
    return 0 if value is None else round(max(0.0, min(100.0, float(value))), 1)


def _narrow(qs, field, site):
    """`site` is "" (everything the user may see), one site name, or a frozenset of site names (an active client
    filter); shared "All Sites" records stay visible under a client filter, as in the dashboard's site matrix."""
    if not site:
        return qs
    if isinstance(site, frozenset):
        return qs.filter(**{f"{field}__in": [*site, *GLOBAL_SITES]})
    return qs.filter(**{field: site})


def _guard(fn, default):
    """Run one block; a module whose tables are missing or broken contributes `default` instead of a 500."""
    try:
        return fn()
    except (ImportError, LookupError, DatabaseError):
        return default


# ------------------------------------------------------------------ header
def health(user, site, names, client=""):
    """Estate health index: monitored checks healthy / total (URL, domain, device and TCP checks)."""
    totals = overview.site_matrix(user, site, names=names, client_param=client)["totals"]
    total = sum(t["total"] for t in totals)
    ok = sum(t["ok"] for t in totals)
    bad = sum(t["bad"] for t in totals)
    value = round(100.0 * ok / total) if total else None
    tone = "mute" if value is None else ("crit" if bad and value < 90 else "warn" if value < 98 else "ok")
    return {"value": value, "ok": ok, "total": total, "bad": bad, "tone": tone}


def alert_counts(user, site):
    Alert = overview.model("telemetry", "Alert")
    if not Alert:
        return None
    qs = _narrow(overview._scoped(Alert, user).filter(status="firing"), "site", site)
    return qs.aggregate(firing=Count("pk"), critical=Count("pk", filter=Q(severity="critical")))


def incident_counts(user, site):
    Ticket = overview.model("servicedesk", "Ticket")
    if not Ticket:
        return None
    qs = _narrow(overview._scoped(Ticket, user).filter(kind="incident").open(), "site", site)
    return {"open": qs.count(), "p12": qs.filter(priority__in=["P1", "P2"]).count()}


# ------------------------------------------------------------------ estate signals
def _tile(key, label, value, note, icon, tone="info", href="", missing=False):
    return {"key": key, "label": label, "value": value, "note": note, "icon": icon, "tone": tone, "href": href,
            "missing": missing}


def assets_tile(user, site):
    CI = overview.model("cmdb", "ConfigurationItem")
    if not CI:
        return None
    qs = _narrow(overview._scoped(CI, user), "site", site).exclude(status="retired")
    n = qs.count()
    active = qs.filter(status="active").count()
    return _tile("assets", "Assets", n, f"cmdb · {active} active" if n else "cmdb · none recorded", "box",
                 "info" if n else "mute", overview._url("cmdb:items"))


def servers_tile(user, site):
    from compute import services as compute
    qs = _narrow(compute.servers_for(user), "site", site)
    agg = qs.aggregate(n=Count("pk"), monitored=Count("pk", filter=Q(enabled=True)),
                       up=Count("pk", filter=Q(enabled=True, status="up")),
                       down=Count("pk", filter=Q(enabled=True, status="down")))
    n, monitored, up, down = agg["n"], agg["monitored"], agg["up"], agg["down"]
    if not n:
        note = "compute · none recorded"
    elif down:
        note = f"compute · {down} of {monitored} monitored down"
    elif monitored and up == monitored:
        note = f"compute · {monitored} monitored, all up"
    else:
        note = f"compute · {monitored} monitored"
    tone = "crit" if down else ("ok" if monitored and up == monitored else ("info" if n else "mute"))
    return _tile("servers", "Servers", n, note, "server", tone, overview._url("compute:servers"))


def k8s_tile(user, site):
    from k8s import services as k8s
    qs = _narrow(k8s.nodes_for(user).filter(missing_since=None), "cluster__site", site)
    n = qs.count()
    not_ready = qs.exclude(ready=True).count()
    note = (f"k8s · {not_ready} not ready" if not_ready else "k8s · all ready") if n else "k8s · no inventory"
    return _tile("k8s", "K8s nodes", n, note, "hexagon", "warn" if not_ready else ("ok" if n else "mute"),
                 overview._url("k8s:nodes"))


def gpu_tile():
    # Nothing in LinkedEye collects GPU telemetry yet (BMC inventory has no GPU component kind): never invent one.
    return _tile("gpus", "GPUs", "–", "Not connected · no DCGM source", "cpu", "mute", missing=True)


# ------------------------------------------------------------------ data centre (racks, power, cooling, space)
def data_centre(user, site):
    from dcim import services as dcim
    rooms = dcim.room_summaries(_narrow(dcim.rooms_for(user), "site", site))
    t = dcim.totals(rooms)
    room_by_id = {room.pk: room for room in rooms}
    racks = [r for room in rooms for r in room.rack_list if dcim.counts_toward_capacity(r)]
    for r in racks:
        r.room_obj = room_by_id.get(r.room_id)
        r.power_tone, r.cooling_tone, r.space_tone = (load_tone(r.power_pct), load_tone(r.cooling_pct),
                                                      load_tone(r.space_pct))
        r.power_w, r.cooling_w, r.space_w = bar_width(r.power_pct), bar_width(r.cooling_pct), bar_width(r.space_pct)
        r.load_label = kw(r.load_kw)
    top = sorted(racks, key=lambda r: (-max(r.worst_pct, r.cooling_pct or 0.0), r.name))[:RACK_ROWS]
    cooling_used = None
    if t["cooling_kw"]:
        cooling_used = 100.0 * (t["cooling_kw"] - t["headroom_kw"]) / t["cooling_kw"]
    chart_racks = [r for r in top if r.load_kw is not None or r.max_power_kw or r.max_cooling_kw]
    chart = {
        "labels": [r.name[:12] for r in chart_racks],
        "tipLabels": [f"{r.name} · {r.room_obj.name if r.room_obj else ''} · {r.site}" for r in chart_racks],
        "unit": "kW", "stacked": False,
        "series": [
            {"name": "IT load", "color": "var(--chart-3)", "values": [r.load_kw for r in chart_racks]},
            {"name": "Power budget", "color": "var(--chart-unknown)", "values": [r.max_power_kw for r in chart_racks]},
            {"name": "Cooling budget", "color": "var(--chart-5)", "values": [r.max_cooling_kw for r in chart_racks]},
        ]}
    summary = [
        {"label": "Power used", "value": pct(t["power_pct"]), "pct": t["power_pct"], "w": bar_width(t["power_pct"]),
         "tone": load_tone(t["power_pct"]),
         "note": (f"{kw(t['load_kw'])} of {kw(t['budget_kw'])} budget" if t["budget_kw"]
                  else "No rack power budgets set") + (" · partly estimated" if t["estimated"] else "")},
        {"label": "Cooling used", "value": pct(cooling_used), "pct": cooling_used, "w": bar_width(cooling_used),
         "tone": load_tone(cooling_used),
         "note": f"{kw(t['cooling_kw'])} active cooling" if t["cooling_kw"] else "No active cooling units"},
        {"label": "Rack space", "value": pct(t["u_pct"]), "pct": t["u_pct"], "w": bar_width(t["u_pct"]),
         "tone": load_tone(t["u_pct"]), "note": f"{t['u_used']} of {t['u_total']} U used" if t["u_total"]
         else "No racks in service"},
    ]
    return {"t": t, "racks": top, "rack_count": len(racks), "chart": chart, "has_chart": bool(chart_racks),
            "summary": summary, "rooms": rooms,
            "headroom": t["headroom_kw"], "headroom_label": kw(t["headroom_kw"]),
            "headroom_tone": "mute" if t["headroom_kw"] is None else ("crit" if t["headroom_kw"] < 0 else "ok"),
            "risk": len(t["hot"]), "href": overview._url("dcim:overview")}


def headroom_tile(dc):
    if dc is None:
        return None
    if dc["headroom"] is None:
        return _tile("headroom", "Headroom", "–", "dcim · no active cooling recorded", "sun", "mute", dc["href"])
    rooms_short = dc["t"]["negative_rooms"]
    note = f"dcim · {rooms_short} room{'s' if rooms_short != 1 else ''} short" if rooms_short else "dcim · cooling"
    # same rule as the DCIM overview: a room short of cooling is a warning even when the estate total is positive
    tone = ("crit" if dc["headroom"] < 0 else "warn") if rooms_short else dc["headroom_tone"]
    return _tile("headroom", "Headroom", dc["headroom_label"], note, "sun", tone, dc["href"])


def risk_tile(dc):
    if dc is None:
        return None
    n = dc["risk"]
    note = (f"racks · of {dc['rack_count']} at 80%+ power or space" if dc["rack_count"]
            else "racks · none recorded")
    return _tile("risk", "Capacity risk", n, note, "flame", "warn" if n else ("ok" if dc["rack_count"] else "mute"),
                 overview._url("dcim:racks"))


# ------------------------------------------------------------------ lists
def open_incidents(user, site):
    Ticket = overview.model("servicedesk", "Ticket")
    if not Ticket:
        return None
    qs = _narrow(overview._scoped(Ticket, user).filter(kind="incident").open(), "site", site)
    return list(qs.order_by("priority", "-opened_at")[:LIST_ROWS])


def firing_alerts(user, site):
    Alert = overview.model("telemetry", "Alert")
    if not Alert:
        return None
    qs = _narrow(overview._scoped(Alert, user).filter(status="firing"), "site", site)
    rows = list(qs.order_by("-starts_at")[:50])
    rank = {"critical": 0, "warning": 1}
    rows.sort(key=lambda a: rank.get(a.severity, 2))     # critical, then warning, then the rest; newest first in each
    return rows[:LIST_ROWS]


def event_stream(user, site):
    Event = overview.model("core", "Event")
    if not Event:
        return []
    return list(_narrow(overview._scoped(Event, user), "site", site).order_by("-occurred_at")[:STREAM_ROWS])


# ------------------------------------------------------------------ hardware telemetry
def telemetry(user, site, dc):
    """Latest hall readings: IT power (feeds), cooling (units), BMC temperatures and health, plus 24 h history."""
    out = {"tiles": [], "chart": None, "fresh": False}
    from dcim.models import is_fresh
    if dc is not None:
        t = dc["t"]
        feeds_fresh = any(is_fresh(f.last_read_at) for room in dc["rooms"] for r in room.rack_list
                          for f in r.feed_list if f.prometheus_query)
        out["fresh"] |= feeds_fresh
        src = "pdu · live" if feeds_fresh else ("pdu · manual / estimated" if t["load_kw"] is not None else "pdu · no feeds")
        out["tiles"].append({"label": "IT power", "value": kw(t["load_kw"]), "note": src,
                             "tone": load_tone(t["power_pct"]) if t["load_kw"] is not None else "mute"})
        units = [u for room in dc["rooms"] for u in room.unit_list if u.status == "active"]
        supply = [u.supply_c for u in units if u.supply_c is not None]
        units_fresh = any(is_fresh(u.last_read_at) for u in units if u.has_queries)
        out["fresh"] |= units_fresh
        note = (f"cdu · {'live' if units_fresh else 'manual'} · supply {temp(sum(supply) / len(supply))}" if supply
                else f"cdu · {len(units)} active unit{'s' if len(units) != 1 else ''}" if units else "cdu · no units")
        out["tiles"].append({"label": "Cooling", "value": kw(t["cooling_kw"]), "note": note,
                             "tone": "mute" if t["cooling_kw"] is None else dc["headroom_tone"]})
    hw = _guard(lambda: hardware(user, site), None)
    if hw:
        out["fresh"] |= hw["fresh"]
        out["tiles"].extend(hw["tiles"])
        out["chart"] = hw["chart"]
    return out


def hardware(user, site):
    from hardware import services as hws
    from hardware.models import HwReading
    endpoints = _narrow(hws.endpoints_for(user).filter(enabled=True), "device__site", site)
    n = endpoints.count()
    if not n:
        return {"tiles": [{"label": "BMC health", "value": "–", "note": "bmc · no Redfish endpoints", "tone": "mute"}],
                "chart": None, "fresh": False}
    healthy = endpoints.filter(health="healthy").count()
    critical = endpoints.filter(health="critical").count()
    # LIVE only for a real poll in the last 15 minutes; example endpoints carry synthetic data and are never polled
    last = endpoints.filter(is_example=False).aggregate(m=Max("last_ok_at"))["m"]
    fresh = bool(last and timezone.now() - last < timedelta(minutes=15))
    temps = _narrow(hws.components_for(user), "endpoint__device__site", site).filter(
        kind="temperature", reading__isnull=False, endpoint__enabled=True)
    agg = temps.aggregate(avg=Avg("reading"), mx=Max("reading"), n=Count("pk"))
    tiles = [
        {"label": "Hardware temp", "value": temp(agg["avg"]),
         "note": f"bmc · avg of {agg['n']} sensors · max {temp(agg['mx'])}" if agg["n"] else "bmc · no temperature sensors",
         "tone": "mute" if agg["avg"] is None else "ok"},
        {"label": "BMC health", "value": f"{healthy}/{n}", "note": f"redfish · {critical} critical" if critical
         else "redfish · healthy" if healthy == n else "redfish · warnings",
         "tone": "crit" if critical else ("ok" if healthy == n else "warn")},
    ]
    since = timezone.now() - timedelta(hours=HISTORY_HOURS)
    rows = list(HwReading.objects.filter(tenant_key=settings.LINKEDEYE_TENANT, component__in=temps.values("pk"),
                                         ts__gte=since)
                .annotate(h=TruncHour("ts")).values("h").annotate(avg=Avg("value"), mx=Max("value")).order_by("h"))
    chart = None
    if len(rows) >= 2:
        chart = {"labels": [timezone.localtime(r["h"]).strftime("%H:%M") for r in rows],
                 "ts": [int(r["h"].timestamp()) for r in rows], "unit": "°C",
                 "series": [{"name": "Average", "color": "var(--chart-2)", "values": [round(r["avg"], 1) for r in rows],
                             "area": True},
                            {"name": "Hottest sensor", "color": "var(--chart-crit)",
                             "values": [round(r["mx"], 1) for r in rows]}]}
    return {"tiles": tiles, "chart": chart, "fresh": fresh}


# ------------------------------------------------------------------ threshold monitors
def _monitor(name, detail, count, tone_if_any, href="", source=True):
    if not source:
        return {"name": name, "detail": detail, "value": "–", "tone": "mute", "href": href, "state": "No data"}
    tone = tone_if_any if count else "ok"
    return {"name": name, "detail": detail, "value": count, "tone": tone, "href": href,
            "state": "Breached" if count and tone_if_any == "crit" else "Warning" if count else "OK"}


def monitors(user, site, dc):
    out = []
    if dc is not None:
        racks = [r for room in dc["rooms"] for r in room.rack_list]
        with_budget = [r for r in racks if r.max_power_kw and r.power_pct is not None]
        out.append(_monitor("Rack power ≥ 90% of budget", "Load against each rack's power budget",
                            sum(1 for r in with_budget if r.power_pct >= 90), "crit", overview._url("dcim:power"),
                            source=bool(with_budget)))
        cooled = [room for room in dc["rooms"] if room.headroom_kw is not None]
        out.append(_monitor("Room cooling short", "IT load above active cooling capacity",
                            sum(1 for room in cooled if room.headroom_kw < 0), "crit", overview._url("dcim:cooling"),
                            source=bool(cooled)))

    def sensors():
        from hardware import services as hws
        comps = _narrow(hws.components_for(user), "endpoint__device__site", site).filter(endpoint__enabled=True)
        if not comps.exists():
            return _monitor("BMC sensors outside thresholds", "Temperature, fan, PSU and voltage limits", 0, "warn",
                            source=False)
        bad = comps.filter(status__in=["warning", "critical"])
        n_crit = bad.filter(status="critical").count()
        return _monitor("BMC sensors outside thresholds", "Temperature, fan, PSU and voltage limits", bad.count(),
                        "crit" if n_crit else "warn")
    m = _guard(sensors, None)
    if m:
        out.append(m)
    Device = overview.model("network", "Device")
    if Device:
        qs = _narrow(overview._scoped(Device, user).filter(enabled=True), "site", site)
        total = qs.count()
        out.append(_monitor("Devices down", "Reachability checks every 60 s", qs.filter(status="down").count(), "crit",
                            overview._url("network:all"), source=bool(total)))
    a = alert_counts(user, site)
    if a is not None:
        out.append(_monitor("Critical alerts firing", "From Alertmanager", a["critical"], "crit",
                            overview._url("telemetry:alertmanager")))
    Ticket = overview.model("servicedesk", "Ticket")
    if Ticket:
        qs = _narrow(overview._scoped(Ticket, user).open(), "site", site)
        out.append(_monitor("SLA breached", "Open tickets past response or resolution due",
                            qs.filter(Q(response_breached=True) | Q(resolution_breached=True)).count(), "warn",
                            overview._url("servicedesk:incidents")))
    return out


# ------------------------------------------------------------------ view
def context(user, site="", client=""):
    """Everything the Command Centre renders (also used by tests). `client` is the shell's client filter."""
    from .client_scope import client_site_names
    names = overview.site_names(user)
    allowed = client_site_names(user, client) if client else None
    if allowed is not None:
        names = [n for n in names if n in allowed]
    if site and site not in names:
        site = "__none__"     # a site outside the user's (or the client's) scope shows nothing, same as site_matrix
    picked = site
    site = site or (frozenset(allowed) if allowed is not None else "")
    dc = _guard(lambda: data_centre(user, site), None)
    tiles = [_guard(lambda: assets_tile(user, site), None), _guard(lambda: servers_tile(user, site), None),
             _guard(lambda: k8s_tile(user, site), None), gpu_tile(), headroom_tile(dc), risk_tile(dc)]
    return {
        "names": names,
        "health": _guard(lambda: health(user, picked, names, client), None),
        "alerts": _guard(lambda: alert_counts(user, site), None),
        "incident_counts": _guard(lambda: incident_counts(user, site), None),
        "tiles": [t for t in tiles if t],
        "dc": dc,
        "incidents": _guard(lambda: open_incidents(user, site), None),
        "firing": _guard(lambda: firing_alerts(user, site), None),
        "telemetry": _guard(lambda: telemetry(user, site, dc), {"tiles": [], "chart": None, "fresh": False}),
        "monitors": _guard(lambda: monitors(user, site, dc), []),
        "stream": _guard(lambda: event_stream(user, site), []),
        "now": timezone.localtime(),
    }


@require("view")
def command_centre(request):
    from .client_scope import active_client
    site = request.GET.get("site", "")
    ctx = context(request.user, site, active_client(request))
    request.le_site_names = ctx["names"]      # reused by the context processor (site picker)
    ctx["site"] = site
    ctx["urls"] = {k: overview._url(n) for k, n in {
        "incidents": "servicedesk:incidents", "alerts": "telemetry:alertmanager", "events": "observe:events",
        "dcim": "dcim:overview", "racks": "dcim:racks", "hardware": "compute:servers"}.items()}
    return render(request, "core/command.html", ctx)
