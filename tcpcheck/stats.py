"""Aggregations for the Telnet Monitoring page and check detail (computed from stored TcpResult rows)."""
from collections import defaultdict
from datetime import datetime, time, timedelta

from django.db.models import Avg, Count, Max, Min, Q
from django.db.models.functions import TruncDate
from django.utils import timezone

from urlchecker.stats import axis_scale

from .models import Status, TcpResult

# Theme palette (see core/static/linkedeye/app.css): ok green, crit red, warn amber, stone for "not checked".
COLORS = {"online": "#0f7a55", "down": "#d92d20", "degraded": "#e08a00", "unknown": "#a8a398"}


def _results(checks):
    return TcpResult.objects.filter(tcp_check__in=checks)


def status_counts(checks):
    counts = {s: 0 for s in Status.values}
    for c in checks:
        counts[c.last_status] = counts.get(c.last_status, 0) + 1
    return counts


def uptime_map(checks, hours):
    """{check_id: uptime %} over the window; Degraded counts as available."""
    since = timezone.now() - timedelta(hours=hours)
    rows = (_results(checks).filter(checked_at__gte=since).values("tcp_check_id")
            .annotate(total=Count("id"), up=Count("id", filter=~Q(status=Status.DOWN))))
    return {r["tcp_check_id"]: 100.0 * r["up"] / r["total"] for r in rows if r["total"]}


def overall_uptime(checks, hours):
    since = timezone.now() - timedelta(hours=hours)
    agg = _results(checks).filter(checked_at__gte=since).aggregate(
        total=Count("id"), up=Count("id", filter=~Q(status=Status.DOWN)))
    return 100.0 * agg["up"] / agg["total"] if agg["total"] else None


def avg_response(checks, hours=24, offset_hours=0):
    """Mean connect time over [now - offset - hours, now - offset)."""
    end = timezone.now() - timedelta(hours=offset_hours)
    qs = _results(checks).filter(checked_at__gte=end - timedelta(hours=hours), response_ms__isnull=False)
    if offset_hours:
        qs = qs.filter(checked_at__lt=end)
    return qs.aggregate(v=Avg("response_ms"))["v"]


def response_trend(checks, hours=24, buckets=48):
    """Average / minimum / maximum connect time per time bucket. Empty buckets are None (gaps, not zero).

    One aggregate query: avg/min/max per bucket [start + i*size, start + (i+1)*size) are computed by the database
    (as in urlchecker.stats.response_trend); the last bucket is open-ended, like before."""
    now = timezone.now()
    size = hours * 3600 / buckets
    start = now - timedelta(hours=hours)
    edges = [start + timedelta(seconds=size * i) for i in range(buckets)]
    aggs = {}
    for i in range(buckets):
        q = Q(checked_at__gte=edges[i]) if i == buckets - 1 else Q(checked_at__gte=edges[i], checked_at__lt=edges[i + 1])
        aggs.update({f"a{i}": Avg("response_ms", filter=q), f"n{i}": Min("response_ms", filter=q),
                     f"x{i}": Max("response_ms", filter=q)})
    agg = _results(checks).filter(checked_at__gte=start, response_ms__isnull=False).aggregate(**aggs)
    labels, tips, avg, mn, mx = [], [], [], [], []
    for i in range(buckets):
        t = timezone.localtime(edges[i])
        labels.append(t.strftime("%H:%M"))
        tips.append(t.strftime("%d %b %H:%M"))
        a = agg[f"a{i}"]
        avg.append(round(a, 1) if a is not None else None)
        mn.append(agg[f"n{i}"])
        mx.append(agg[f"x{i}"])
    data = {"labels": labels, "tipLabels": tips, "unit": "ms", "series": [
        {"name": "Average", "color": "#2b4cff", "values": avg, "width": 2},
        {"name": "Minimum", "color": COLORS["online"], "values": mn},
        {"name": "Maximum", "color": COLORS["down"], "values": mx, "area": True, "areaOpacity": 0.08},
    ]}
    data.update(axis_scale(max((v for v in mx if v is not None), default=0)))
    return data


def daily_history(checks, days=7):
    """Per day, how many checks were Online / Degraded / Down.
    A check counts as Down for a day if >5% of its results were down, Degraded if >5% were degraded.

    Counts per (check, local day) are aggregated by the database (TruncDate in the current time zone, as in
    urlchecker.stats.daily_history); only those few rows reach Python."""
    today = timezone.localdate()
    start = today - timedelta(days=days - 1)
    since = timezone.make_aware(datetime.combine(start, time.min))
    rows = (_results(checks).filter(checked_at__gte=since)
            .annotate(day=TruncDate("checked_at", tzinfo=timezone.get_current_timezone()))
            .values("tcp_check_id", "day").order_by()
            .annotate(total=Count("id"), dn=Count("id", filter=Q(status=Status.DOWN)),
                      dg=Count("id", filter=Q(status=Status.DEGRADED))))
    per = defaultdict(list)  # day -> [(total, down, degraded) per check]
    for r in rows:
        per[r["day"]].append((r["total"], r["dn"], r["dg"]))
    labels, online, degraded, down = [], [], [], []
    for i in range(days):
        d = start + timedelta(days=i)
        labels.append(d.strftime("%b %d"))
        o = g = x = 0
        for total, dn, dg in per.get(d, ()):
            if not total:
                continue
            if dn / total > 0.05:
                x += 1
            elif dg / total > 0.05:
                g += 1
            else:
                o += 1
        online.append(o or None)
        degraded.append(g or None)
        down.append(x or None)
    data = {"labels": labels, "xLabelEvery": 1, "series": [
        {"name": "Online", "color": COLORS["online"], "values": online},
        {"name": "Down", "color": COLORS["down"], "values": down},
        {"name": "Degraded", "color": COLORS["degraded"], "values": degraded},
    ]}
    peak = max((sum(s["values"][i] or 0 for s in data["series"]) for i in range(days)), default=0)
    data.update(axis_scale(peak))
    return data


def donut(counts, total, label="Total Checks"):
    return {"segments": [
        {"label": "Online", "value": counts.get("online", 0), "color": COLORS["online"]},
        {"label": "Down", "value": counts.get("down", 0), "color": COLORS["down"]},
        {"label": "Degraded", "value": counts.get("degraded", 0), "color": COLORS["degraded"]},
        {"label": "Not checked", "value": counts.get("unknown", 0), "color": COLORS["unknown"]}],
        "center": {"value": str(total), "label": label}}
