"""Read side of APM: the dashboard snapshot (also served as /apm/live/), one adapter's detail, the evidence bundle,
CSV rows and the RCA draft. Every function takes a Source that the caller has ALREADY scoped to the user; nothing here
looks at another source. Query counts are constant: they do not grow with the number of adapters, processes or logs."""
import csv
import hashlib
import re
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from . import services
from .models import (FAILED, HEALTHY, STATUS_LABELS, UNKNOWN, WARNING, AdapterCell, AdapterRun, CellEvent, ExecLog,
                     ProcessState, RetryRequest)

STATE_KEY = {FAILED: "failure", WARNING: "warning", HEALTHY: "success", UNKNOWN: "nodata"}
STATE_LABEL = {"failure": "Failed", "warning": "Warning", "success": "Success", "nodata": "No data", "stale": "Stale"}
LOG_CHOICES = (25, 100, 500)
AVG_WINDOW = timedelta(minutes=5)


# ----------------------------------------------------------------------------- formatting
def fmt(dt):
    return timezone.localtime(dt).strftime("%d-%m-%Y %H:%M:%S") if dt else ""


def fmt_ms(dt):
    return timezone.localtime(dt).strftime("%d-%m-%Y %H:%M:%S.%f")[:-3] if dt else ""


def fmt_duration(seconds):
    """02m 18s style (hours when needed)."""
    if seconds is None:
        return ""
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m {s:02d}s" if h else f"{m:02d}m {s:02d}s"


def fmt_clock(seconds):
    if seconds is None:
        return ""
    s = int(round(seconds))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def pct(part, total):
    return round(100.0 * part / total, 2) if total else None


def ordered_segments(names):
    names = set(names)
    head = [s for s in services.CANON_SEGMENTS if s in names]
    return head + sorted(names - set(head), key=str.lower)


# ----------------------------------------------------------------------------- the dashboard snapshot
def _newest(*stamps):
    got = [t for t in stamps if t]
    return max(got) if got else None


def _latest(source, kind):
    return AdapterRun.objects.filter(source=source, kind=kind).order_by("-executed_at", "-id").first()


def revision(source, logs_n, now):
    """A short token that changes whenever the snapshot would: new data of any kind, a stale / banner flip, the log window or a
    rename. It is computed from the source row alone, so /apm/live/?rev=... can answer 'unchanged' without reading the data."""
    flags = (source.last_matrix_at is not None and services.is_stale(source, source.last_matrix_at, now),
             source.last_table_at is not None and services.is_stale(source, source.last_table_at, now),
             unchanged_banner(source, now))
    raw = "|".join(str(x) for x in (source.pk, source.name, source.site, source.enabled, source.expected_interval_s,
                                    source.last_matrix_at, source.last_table_at, source.last_log_at, logs_n, flags))
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def unchanged_banner(source, now):
    last = _newest(source.last_matrix_at, source.last_table_at)
    age = services.age_seconds(last, now)
    return bool(last) and age is not None and age > max(30, int(source.expected_interval_s or 300)) * 3


def build_snapshot(source, *, logs_n=100, now=None):
    now = now or timezone.now()
    logs_n = logs_n if logs_n in LOG_CHOICES else 100
    cells = list(AdapterCell.objects.filter(source=source))
    matrix_run = _latest(source, "matrix")
    table_run = _latest(source, "table")
    procs = list(ProcessState.objects.filter(source=source))
    logs = list(ExecLog.objects.filter(source=source).order_by("-ts", "-id")[:logs_n])
    logs.reverse()

    matrix_stale = bool(cells) and services.is_stale(source, source.last_matrix_at, now)
    table_stale = bool(procs) and services.is_stale(source, source.last_table_at, now)
    segments = ordered_segments(c.segment for c in cells)
    run_at = matrix_run.executed_at if matrix_run else None

    counts = {"success": 0, "failure": 0, "warning": 0, "nodata": 0, "stale": 0}
    by_client = {}
    for c in cells:
        key = "stale" if matrix_stale else STATE_KEY.get(c.status, "nodata")
        counts[key] += 1
        row = by_client.setdefault(c.client, {"client": c.client, "cells": {}, "ok": 0, "bad": 0, "warn": 0,
                                              "ls": None, "lf": None})
        row["cells"][c.segment] = [c.pk, c.ctcl_id, c.value, key, c.status]
        if c.status == FAILED:
            row["bad"] += 1
        elif c.status == WARNING:
            row["warn"] += 1
        elif c.status == HEALTHY:
            row["ok"] += 1
        last_ok = run_at if c.status == HEALTHY else c.last_success_at
        if last_ok and (row["ls"] is None or last_ok > row["ls"]):
            row["ls"] = last_ok
        if c.last_failure_at and (row["lf"] is None or c.last_failure_at > row["lf"]):
            row["lf"] = c.last_failure_at
    clients = []
    for name in sorted(by_client, key=str.lower):
        r = by_client[name]
        state = ("stale" if matrix_stale else "failure" if r["bad"] else "warning" if r["warn"] else
                 "success" if r["ok"] else "nodata")
        clients.append({"client": name, "cells": r["cells"], "state": state, "last_success": fmt(r["ls"]),
                        "last_failure": fmt(r["lf"])})

    total = len(cells)
    process_rows = []
    down_procs = 0
    for p in procs:
        key = "stale" if table_stale else STATE_KEY.get(p.status, "nodata")
        if key == "failure":
            down_procs += 1
        process_rows.append({
            "id": p.pk, "host": p.host, "name": p.name, "pid": p.pid, "state": key, "cpu": p.cpu_pct, "mem": p.mem_pct,
            "uptime": fmt_clock(p.uptime_s), "heartbeat": fmt(p.last_heartbeat), "last_execution": fmt(p.last_execution),
            "next_execution": fmt(p.next_execution), "response_ms": p.response_ms, "exit_code": p.exit_code,
            "remarks": p.remarks})
    process_rows.sort(key=lambda r: (r["host"].lower(), r["name"].lower()))

    cutoff = now - AVG_WINDOW
    resp = [p.response_ms for p in procs if p.response_ms is not None and p.updated_at >= cutoff]
    avg_ms = round(sum(resp) / len(resp), 1) if resp else None

    if not total and not procs:
        overall = "nodata"
    elif matrix_stale or (table_stale and not total):
        overall = "stale"
    elif counts["failure"] and not counts["success"] and not counts["warning"]:
        overall = "down"
    elif counts["failure"] or counts["warning"] or down_procs:
        overall = "degraded"
    else:
        overall = "healthy"

    interval = max(30, int(source.expected_interval_s or 300))
    last_data = _newest(source.last_matrix_at, source.last_table_at)
    age = services.age_seconds(last_data, now)
    stale = {"expected_interval_s": interval, "age_s": None if age is None else int(age),
             "matrix_stale": matrix_stale, "table_stale": table_stale, "banner_after_s": interval * 3,
             "banner": bool(last_data) and age is not None and age > interval * 3, "waiting": last_data is None,
             "last_data": fmt(last_data)}

    return {
        "rev": revision(source, logs_n, now), "now": now.isoformat(), "tz": timezone.get_current_timezone_name(),
        "source": {"id": source.pk, "name": source.name, "site": source.site, "enabled": source.enabled,
                   "redis_poll": source.redis_poll, "last_seen": fmt(source.last_seen),
                   "script": matrix_run.script if matrix_run else "", "host": matrix_run.host if matrix_run else ""},
        "kpis": {"overall": overall, "total": total, "exchanges": len(segments), "success": counts["success"],
                 "failure": counts["failure"], "warning": counts["warning"], "nodata": counts["nodata"],
                 "stale": counts["stale"], "success_pct": pct(counts["success"], total),
                 "failure_pct": pct(counts["failure"], total), "avg_response_ms": avg_ms,
                 "processes": len(procs), "processes_down": down_procs, "last_executed": fmt(run_at),
                 "duration": fmt_duration(matrix_run.duration_s) if matrix_run else "",
                 "duration_s": matrix_run.duration_s if matrix_run else None},
        "stale": stale, "segments": segments, "clients": clients, "processes": process_rows,
        "logs": [{"id": ln.pk, "ts": fmt_ms(ln.ts), "level": ln.level, "process": ln.process, "message": ln.message,
                  "client": ln.client, "segment": ln.segment} for ln in logs],
        "logs_n": logs_n,
    }


def unchanged_stub(source, snap_rev, now=None):
    """The small answer to /apm/live/?rev=...: only what depends on the clock."""
    now = now or timezone.now()
    last = _newest(source.last_matrix_at, source.last_table_at)
    age = services.age_seconds(last, now)
    interval = max(30, int(source.expected_interval_s or 300))
    return {"unchanged": True, "rev": snap_rev, "now": now.isoformat(),
            "stale": {"expected_interval_s": interval, "age_s": None if age is None else int(age),
                      "banner": bool(last) and age is not None and age > interval * 3, "waiting": last is None,
                      "last_data": fmt(last)}}


# ----------------------------------------------------------------------------- one adapter
def cell_logs(cell, limit=100):
    """Log lines about ONE adapter: lines tagged with its client and segment, lines naming its adapter id, and free-text
    lines that name the client but no OTHER exchange of that source (so c003 / NFO does not show c003 / NSE failures)."""
    q = Q(client=cell.client) & (Q(segment="") | Q(segment=cell.segment))
    if cell.ctcl_id and len(cell.ctcl_id) >= 4:
        q |= Q(message__icontains=cell.ctcl_id)
    if len(cell.client) >= 3:
        q |= Q(message__icontains=cell.client)
    rows = list(ExecLog.objects.filter(source=cell.source).filter(q).order_by("-ts", "-id")[:limit * 3])
    # bounded: the segment names come from the producer, and a regex of thousands of alternatives would be slow to run
    others = [s for s in AdapterCell.objects.filter(source=cell.source).order_by("segment").values_list("segment", flat=True)
              .distinct()[:60] if s and s != cell.segment]
    foreign = re.compile(r"\b(?:" + "|".join(re.escape(s) for s in others) + r")\b", re.I) if others else None
    own_id = cell.ctcl_id if cell.ctcl_id and len(cell.ctcl_id) >= 4 else None
    own = re.compile(r"\b" + re.escape(cell.segment) + r"\b", re.I)
    out = []
    for r in rows:
        if r.segment and r.segment != cell.segment and not (own_id and own_id in r.message):
            continue                                    # tagged to another exchange
        about_me = (r.segment == cell.segment and r.client == cell.client) or (own_id and own_id in r.message) or own.search(r.message)
        if foreign and not about_me and foreign.search(r.message):
            continue                                    # it names another exchange of this client
        out.append(r)
        if len(out) >= limit:
            break
    out.reverse()
    return out


def spark(cell, now, hours=24, buckets=48):
    """A 24 h success strip: one entry per bucket, 'ok' / 'warn' / 'crit' / 'none', built by replaying the status changes."""
    start = now - timedelta(hours=hours)
    step = timedelta(hours=hours) / buckets
    events = list(CellEvent.objects.filter(source=cell.source, client=cell.client, segment=cell.segment,
                                           ts__gte=start - timedelta(days=30)).order_by("ts", "id")[:500])
    cur, i, out = None, 0, []
    names = {HEALTHY: "ok", WARNING: "warn", FAILED: "crit"}
    for b in range(buckets):
        edge = start + step * (b + 1)
        while i < len(events) and events[i].ts <= edge:
            cur = events[i].new_status
            i += 1
        out.append(names.get(cur, "none"))
    return out


def retry_info(cell):
    r = RetryRequest.objects.filter(source=cell.source, client=cell.client, segment=cell.segment).order_by("-requested_at", "-id").first()
    if r is None:
        return None
    services.expire_retries(cell.source)
    r.refresh_from_db()
    return {"id": r.pk, "state": r.state, "label": r.get_state_display(), "requested_at": fmt(r.requested_at),
            "requested_by": r.requested_by_name, "picked_at": fmt(r.picked_at), "done_at": fmt(r.done_at)}


def cell_detail(cell, *, can_retry=False, can_rca=False, now=None, log_limit=100):
    """Everything the detail panel shows for one adapter, as plain JSON-able data."""
    now = now or timezone.now()
    src = cell.source
    run = _latest(src, "matrix")
    stale = services.is_stale(src, src.last_matrix_at, now)
    history = list(CellEvent.objects.filter(source=src, client=cell.client, segment=cell.segment).order_by("-ts", "-id")[:50])
    logs = cell_logs(cell, log_limit)
    snippet = logs[-8:]
    last_ok = run.executed_at if (cell.status == HEALTHY and run) else cell.last_success_at
    interval = max(30, int(src.expected_interval_s or 300))
    next_exec = run.executed_at + timedelta(seconds=interval) if run else None
    failing = cell.status == FAILED
    summary = ""
    if failing:
        summary = (f"{cell.client} / {cell.segment} (adapter {cell.ctcl_id or 'n/a'}) has failed "
                   f"{cell.failure_streak} time(s) in a row"
                   + (f" since {fmt(cell.last_failure_at)}" if cell.last_failure_at else "") + ".")
    data = {
        "id": cell.pk, "client": cell.client, "segment": cell.segment, "ctcl_id": cell.ctcl_id, "value": cell.value,
        "status": cell.status, "state": "stale" if stale else STATE_KEY.get(cell.status, "nodata"),
        "status_label": STATUS_LABELS.get(cell.status, "Unknown"), "stale": stale,
        "source": {"id": src.pk, "name": src.name, "site": src.site},
        "details": {"last_execution": fmt(run.executed_at) if run else "", "duration": fmt_clock(run.duration_s) if run and run.duration_s is not None else "",
                    "script": run.script if run else "", "host": run.host if run else "", "next_execution": fmt(next_exec)},
        "failure": {"active": failing, "summary": summary, "error": cell.last_error, "exit_code": cell.exit_code,
                    "failure_time": fmt(cell.last_failure_at), "count": cell.failure_total, "streak": cell.failure_streak,
                    "last_success": fmt(last_ok),
                    "snippet": [{"ts": fmt_ms(x.ts), "level": x.level, "message": x.message} for x in snippet]},
        "history": [{"ts": fmt(h.ts), "old": None if h.old_status is None else STATUS_LABELS.get(h.old_status, "Unknown"),
                     "new": STATUS_LABELS.get(h.new_status, "Unknown"), "new_key": STATE_KEY.get(h.new_status, "nodata"),
                     "error": h.error} for h in history],
        "spark": spark(cell, now),
        "logs": [{"ts": fmt_ms(x.ts), "level": x.level, "process": x.process, "message": x.message} for x in logs],
        "retry": retry_info(cell), "can_retry": bool(can_retry), "can_rca": bool(can_rca),
    }
    data["evidence"] = evidence_text(data, now)
    return data


def evidence_text(d, now=None):
    """A plain-text bundle with timestamps. Contains no tokens, passwords or Redis settings."""
    now = now or timezone.now()
    L = ["LinkedEye APM evidence", "=" * 24, f"Generated: {fmt(now)} ({timezone.get_current_timezone_name()})",
         f"Source: {d['source']['name']} (site: {d['source']['site']})", "",
         f"Adapter: {d['client']} / {d['segment']}", f"Adapter / CTCL id: {d['ctcl_id'] or '-'}", f"Value: {d['value'] or '-'}",
         f"Status: {d['status_label']}" + (" (data is stale)" if d["stale"] else ""),
         f"Last execution: {d['details']['last_execution'] or '-'}", f"Duration: {d['details']['duration'] or '-'}",
         f"Script: {d['details']['script'] or '-'}", f"Host: {d['details']['host'] or '-'}", "",
         "Failure", "-------", f"Summary: {d['failure']['summary'] or 'The adapter is not failing.'}",
         f"Error message: {d['failure']['error'] or '-'}",
         f"Exit code: {d['failure']['exit_code'] if d['failure']['exit_code'] is not None else '-'}",
         f"Failure time: {d['failure']['failure_time'] or '-'}", f"Last success: {d['failure']['last_success'] or '-'}",
         f"Failure count: {d['failure']['count']}", f"Consecutive failures: {d['failure']['streak']}", "", "History (newest first)",
         "---------------------"]
    L += [f"{h['ts']}  {h['old'] or 'new'} -> {h['new']}" + (f"  {h['error']}" if h["error"] else "") for h in d["history"][:20]] or ["(no changes recorded)"]
    L += ["", "Recent log lines", "----------------"]
    L += [f"{x['ts']}  {x['level']:<7} {x['process']}  {x['message']}" for x in d["logs"][-20:]] or ["(none mention this adapter)"]
    return "\n".join(L) + "\n"


def rca_draft(d):
    f = d["failure"]
    return "\n".join([
        f"Root cause analysis: APM adapter {d['client']} / {d['segment']} ({d['ctcl_id'] or 'no id'})", "",
        "What happened", f"- {f['summary'] or 'The adapter reported a failure.'}", f"- Error: {f['error'] or 'not provided'}",
        f"- Exit code: {f['exit_code'] if f['exit_code'] is not None else 'not provided'}",
        f"- First failure in this streak: see history; last failure at {f['failure_time'] or 'n/a'}; total failures {f['count']}",
        f"- Last success: {f['last_success'] or 'unknown'}", "",
        "Where", f"- Source: {d['source']['name']} (site {d['source']['site']})",
        f"- Script: {d['details']['script'] or 'n/a'} on {d['details']['host'] or 'n/a'}", "",
        "Investigation to do", "- [ ] Confirm the adapter / CTCL id and its configuration", "- [ ] Check the broker or exchange side for an outage",
        "- [ ] Review the log lines in the attached evidence", "- [ ] Retry once the cause is fixed and confirm the status recovers", "",
        "Evidence", d["evidence"]])


# ----------------------------------------------------------------------------- CSV
def csv_safe(value):
    """A spreadsheet-safe cell: text starting with = + - @ tab or CR (or space-led formulas) is not a formula."""
    if value is None:
        return ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    s = str(value)
    if s.lstrip()[:1] in ("=", "+", "-", "@") or s[:1] in ("\t", "\r"):
        return "'" + s
    return s


def filter_cells(qs, *, exchange="", client="", status="", q=""):
    if exchange:
        qs = qs.filter(segment=exchange)
    if client:
        qs = qs.filter(client=client)
    if status in ("success", "failure", "warning", "nodata"):
        qs = qs.filter(status={"failure": FAILED, "warning": WARNING, "success": HEALTHY, "nodata": UNKNOWN}[status])
    if q:
        qs = qs.filter(Q(client__icontains=q) | Q(segment__icontains=q) | Q(ctcl_id__icontains=q) | Q(value__icontains=q))
    return qs


def write_cells_csv(resp, cells, run_at=None, limit=20000):
    w = csv.writer(resp)
    w.writerow(["client", "exchange", "adapter_id", "value", "status", "last_success", "last_failure", "failure_streak",
                "failure_total", "last_error"])
    for c in cells[:limit]:
        last_ok = run_at if (c.status == HEALTHY and run_at) else c.last_success_at
        w.writerow([csv_safe(v) for v in (c.client, c.segment, c.ctcl_id, c.value, STATUS_LABELS.get(c.status, ""),
                                          fmt(last_ok), fmt(c.last_failure_at), c.failure_streak, c.failure_total, c.last_error)])
