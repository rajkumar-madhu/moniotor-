"""The JSON behind the Hardware tab (rendered and patched by hardware.js) and the CSV report.

Everything the BMC sent is already length-capped and stripped by ``hardware.parsers`` before it was stored; the page puts it
in with ``textContent`` only. Nothing here reads the network."""
import csv
from datetime import datetime, timedelta, timezone as dt_tz

from django.urls import reverse
from django.utils import timezone

from .models import BmcEndpoint, ControlRequest, HwComponent, HwEvent, HwReading

RANGES = {"1h": 3600, "6h": 6 * 3600, "24h": 24 * 3600}
RANGE_LABELS = {"1h": "Last 1 hour", "6h": "Last 6 hours", "24h": "Last 24 hours"}
TONE = {"ok": "ok", "warning": "warn", "critical": "crit", "unknown": "mute"}
SEV_TONE = {"info": "info", "warning": "warn", "critical": "crit"}
CHART_COLORS = [f"var(--chart-{n})" for n in (4, 3, 5, 6, 7, 8, 2, 10, 1, 9)]
NOT_REPORTED = "not reported"
READING_ROW_CAP = 60000


def range_key(value):
    return value if value in RANGES else "1h"


def _fmt(v, digits=1):
    if v is None:
        return None
    return int(v) if float(v).is_integer() else round(float(v), digits)


def _local(ts, fmt="%Y-%m-%d %H:%M:%S"):
    return timezone.localtime(ts).strftime(fmt) if ts else ""


def connection_state(endpoint, now):
    """(key, label, tone) of how the endpoint stands: example / disabled / not_configured / connected / stale / unreachable ..."""
    if endpoint.is_example:
        return "example", "Example data", "info"
    if not endpoint.enabled:
        return "disabled", "Disabled", "mute"
    if not endpoint.configured:
        return "not_configured", "Not configured", "mute"
    if endpoint.conn_state == BmcEndpoint.CONN_AUTH:
        return "auth_failed", "Auth failed", "crit"
    if endpoint.conn_state == BmcEndpoint.CONN_UNREACHABLE:
        return "unreachable", "Unreachable", "crit"
    if endpoint.conn_state == BmcEndpoint.CONN_NEVER or not endpoint.last_ok_at:
        return "waiting", "Waiting for the first poll", "mute"
    if (now - endpoint.last_ok_at).total_seconds() > max(180, 3 * endpoint.poll_interval_s):
        return "stale", "Stale", "warn"
    return "connected", "Connected", "ok"


def _status_word(kind, tone):
    return {"ok": "Normal" if kind in ("temperature", "voltage", "intrusion") else "OK", "warn": "Warning",
            "crit": "Critical", "mute": "Unknown"}[tone]


def _tile(key, label, comps, count=False, total=None):
    """One tile of the health card. No component means NOT REPORTED: never 'OK'."""
    if not comps:
        return {"key": key, "label": label, "value": "Not reported", "tone": "mute", "reported": False}
    statuses = [c.status for c in comps]
    tone = TONE[_worst(statuses)]
    n_ok = sum(1 for s in statuses if s == "ok")
    if count:
        m = max(len(comps), int(total or 0))
        return {"key": key, "label": label, "value": f"{n_ok} / {m} OK", "tone": tone, "reported": True}
    return {"key": key, "label": label, "value": _status_word(key, tone), "tone": tone, "reported": True}


def _worst(statuses):
    from . import parsers
    return parsers.worst(statuses)


def _chart(comps, readings, seconds, now, unit, interval=60):
    """Line-chart data for charts.js, bucketed to ~120 points. Gaps stay None (never drawn as zero)."""
    if not comps:
        return None
    step = max(60, seconds // 120, int(interval or 60))        # never finer than the poll interval (that would draw gaps)
    end = int(now.timestamp()) // step * step
    stamps = list(range(end - seconds, end + 1, step))
    pos = {t: i for i, t in enumerate(stamps)}
    acc = {c.pk: [[0.0, 0] for _ in stamps] for c in comps}
    seen = False
    for comp_id, ts, value in readings:
        slot = pos.get(int(ts.timestamp()) // step * step)
        if slot is not None and comp_id in acc:
            acc[comp_id][slot][0] += value
            acc[comp_id][slot][1] += 1
            seen = True
    if not seen:
        return None
    fmt = "%H:%M"
    labels = [timezone.localtime(datetime.fromtimestamp(t, tz=dt_tz.utc)).strftime(fmt) for t in stamps]
    tips = [timezone.localtime(datetime.fromtimestamp(t, tz=dt_tz.utc)).strftime("%d %b %H:%M") for t in stamps]
    series = []
    for i, c in enumerate(comps):
        vals = [None if n == 0 else round(total / n, 2) for total, n in acc[c.pk]]
        if all(v is None for v in vals):
            continue
        series.append({"name": c.name, "color": CHART_COLORS[i % len(CHART_COLORS)], "values": vals, "width": 1.6})
    if not series:
        return None
    return {"labels": labels, "tipLabels": tips, "ts": stamps, "unit": unit, "xLabelEvery": max(1, len(stamps) // 6), "series": series}


def charts(endpoint, comps, key, now):
    seconds = RANGES[range_key(key)]
    temps = [c for c in comps if c.kind == "temperature"]
    fans = [c for c in comps if c.kind == "fan" and c.unit == "RPM"] or [c for c in comps if c.kind == "fan"]
    ids = [c.pk for c in temps + fans]
    # the cap keeps the NEWEST points (a 24 h chart of many sensors read every 30 s is over it): newest first, then any order will do
    rows = list(HwReading.objects.filter(component_id__in=ids, ts__gte=now - timedelta(seconds=seconds))
                .order_by("-ts").values_list("component_id", "ts", "value")[:READING_ROW_CAP]) if ids else []
    iv = endpoint.poll_interval_s
    return {"temp": _chart(temps, rows, seconds, now, "°C", iv),
            "fan": _chart(fans, rows, seconds, now, fans[0].unit if fans else "RPM", iv)}


def _overview_rows(endpoint, now):
    ov = endpoint.overview or {}
    v = endpoint.effective_vendor
    bmc = endpoint.bmc_name

    def row(label, value, mono=False):
        return {"label": label, "value": value or "", "mono": mono, "reported": bool(value)}

    rows = [row(f"{bmc} IP" if bmc != "BMC" else "BMC address", endpoint.host, True), row("Firmware", ov.get("bmc_firmware"), True)]
    if v == "dell":
        rows += [row("Service tag", ov.get("service_tag"), True), row("Model", ov.get("model")),
                 row("Lifecycle controller", ov.get("lifecycle"), True), row(f"{bmc} license", ov.get("license"))]
    elif v == "hpe":
        rows += [row("Serial number", ov.get("serial"), True), row("Model", ov.get("model")),
                 row("System ROM", ov.get("bios"), True), row(f"{bmc} license", ov.get("license"))]
    else:
        rows += [row("Serial number", ov.get("serial"), True), row("Model", ov.get("model")), row("BIOS", ov.get("bios"), True),
                 row("License", ov.get("license"))]
    rows.append({"label": "Last updated", "value": _local(endpoint.last_ok_at), "mono": False, "reported": bool(endpoint.last_ok_at)})
    return rows


def _comp_row(c):
    a = c.attrs or {}
    return {"id": c.pk, "name": c.name, "reading": _fmt(c.reading), "unit": c.unit, "warn": _fmt(c.warn_at), "crit": _fmt(c.crit_at),
            "low": _fmt(c.low_crit if c.low_crit is not None else c.low_warn), "low_warn": _fmt(c.low_warn),
            "status": c.status, "tone": TONE[c.status], "model": a.get("model") or "", "input_v": _fmt(a.get("input_v")),
            "output_w": _fmt(c.reading) if c.kind == "psu" else None, "capacity_w": _fmt(a.get("capacity_w")),
            "load_pct": a.get("load_pct")}


def request_row(r, now=None):
    return {"id": r.pk, "action": r.action, "label": r.action_label, "state": r.state, "state_label": r.get_state_display(),
            "reason": r.reason, "by": r.requested_by_name, "at": _local(r.requested_at, "%Y-%m-%d %H:%M"),
            "decided_by": r.decided_by_name, "decided_at": _local(r.decided_at, "%Y-%m-%d %H:%M"), "note": r.decision_note}


def build(endpoint, key="1h", perms=None, now=None):
    """The whole tab as one JSON-able dict."""
    now = now or timezone.now()
    key = range_key(key)
    perms = perms or {}
    comps = list(HwComponent.objects.filter(endpoint=endpoint).order_by("kind", "position", "name"))
    by = {}
    for c in comps:
        by.setdefault(c.kind, []).append(c)
    skey, slabel, stone = connection_state(endpoint, now)
    age = int((now - endpoint.last_ok_at).total_seconds()) if endpoint.last_ok_at else None
    health = endpoint.health if skey in ("connected", "stale", "unreachable", "auth_failed") else "unknown"
    if skey in ("example",):
        health = endpoint.health
    n_bad = sum(1 for c in comps if c.status in ("warning", "critical") and c.kind not in ("system",))
    cpu_total = (endpoint.overview or {}).get("cpu_count")
    tiles = [
        _tile("psu", "Power supply", by.get("psu", []), count=True),
        _tile("fan", "Fans", by.get("fan", []), count=True),
        _tile("temperature", "Temperature", by.get("temperature", [])),
        _tile("storage", "Storage", by.get("storage", []) + by.get("drive", [])),
        _tile("memory", "Memory", by.get("memory", []) + by.get("dimm", [])),
        _tile("voltage", "Voltage", by.get("voltage", [])),
        _tile("intrusion", "Intrusion", by.get("intrusion", [])),
        _tile("cpu", "CPUs", by.get("cpu", []), count=True, total=cpu_total),
        _tile("battery", "Batteries", by.get("battery", [])),
    ]
    if health == "healthy":
        sub = "All reported components are normal"
    elif health in ("warning", "critical"):
        sub = f"{n_bad} component{'s' if n_bad != 1 else ''} need{'s' if n_bad == 1 else ''} attention"
    else:
        sub = "No hardware data reported yet" if not comps else "Health could not be determined"
    events = [{"id": e.pk, "ts": _local(e.ts, "%Y-%m-%d %H:%M"), "ts_short": _local(e.ts, "%d %b %H:%M"), "severity": e.severity, "tone": SEV_TONE.get(e.severity, "info"),
               "sensor": e.sensor, "message": e.message, "log": e.log}
              for e in HwEvent.objects.filter(endpoint=endpoint).order_by("-ts", "-id")[:6]]
    dev = endpoint.device
    reqs = [request_row(r) for r in ControlRequest.objects.filter(endpoint=endpoint).order_by("-requested_at", "-id")[:5]]
    pending = ControlRequest.objects.filter(endpoint=endpoint, state=ControlRequest.PENDING).count()
    return {
        "generated_at": now.isoformat(), "range": key, "range_label": RANGE_LABELS[key],
        "endpoint": {
            "id": endpoint.pk, "device": dev.pk, "state": skey, "state_label": slabel, "tone": stone,
            "vendor": endpoint.effective_vendor, "vendor_label": endpoint.vendor_label, "bmc": endpoint.bmc_name,
            "detected": endpoint.vendor or "", "override": endpoint.vendor_override or "",
            "host": endpoint.host, "console_url": endpoint.base_url + "/" if endpoint.host else "",
            "last_ok_ts": endpoint.last_ok_at.timestamp() if endpoint.last_ok_at else None, "age_s": age,
            "interval_s": endpoint.poll_interval_s, "error": endpoint.last_error if skey in ("unreachable", "auth_failed") else "",
            "example": endpoint.is_example, "verify_mode": endpoint.verify_mode, "insecure_http": endpoint.insecure_http,
            "auth_paused": endpoint.auth_paused, "power_state": endpoint.power_state, "configured": endpoint.configured,
            "enabled": endpoint.enabled, "refresh_pending": bool(endpoint.refresh_requested_at and (
                not endpoint.last_polled_at or endpoint.refresh_requested_at > endpoint.last_polled_at)),
        },
        "overview": _overview_rows(endpoint, now),
        "health": {"rollup": health, "label": {"healthy": "Healthy", "warning": "Warning", "critical": "Critical"}.get(health, "Unknown"),
                   "sub": sub, "tiles": tiles},
        "temps": [_comp_row(c) for c in by.get("temperature", [])],
        "fans": [_comp_row(c) for c in by.get("fan", [])],
        "psus": [_comp_row(c) for c in by.get("psu", [])],
        "volts": [_comp_row(c) for c in by.get("voltage", [])],
        "events": events, "requests": reqs, "pending_requests": pending,
        "charts": charts(endpoint, comps, key, now),
        "perms": {"operate": bool(perms.get("operate")), "admin": bool(perms.get("admin"))},
    }


# ----------------------------------------------------------------------------- CSV report
def csv_safe(value):
    """A spreadsheet-safe cell: text that starts like a formula gets a leading quote."""
    s = "" if value is None else str(value)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def write_csv(out, endpoint):
    """Current sensors and the latest BMC log entries as one CSV (a `section` column tells them apart)."""
    w = csv.writer(out)
    w.writerow(["section", "name", "reading", "unit", "warn_at", "critical_at", "status", "time", "message"])
    for c in HwComponent.objects.filter(endpoint=endpoint).order_by("kind", "position", "name")[:2000]:
        w.writerow([csv_safe(c.kind), csv_safe(c.name), c.reading if c.reading is not None else "", csv_safe(c.unit),
                    c.warn_at if c.warn_at is not None else "", c.crit_at if c.crit_at is not None else "", c.status,
                    _local(c.updated_at), ""])
    for e in HwEvent.objects.filter(endpoint=endpoint).order_by("-ts", "-id")[:5000]:
        w.writerow(["event", csv_safe(e.sensor), "", "", "", "", e.severity, _local(e.ts), csv_safe(e.message)])
