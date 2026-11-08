"""Create (or refresh) a clearly-marked example APM source with SYNTHETIC data so the dashboard can be demoed without
the customer's hosts. Idempotent: running it again wipes and rebuilds only the example source. ``--remove`` deletes it.

Everything here is invented: client codes c001..c012, adapter ids, host names. It never runs automatically and never emits
events (no alerts, incidents or notifications are created by the demo data)."""
import random
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apm import services, snapshot
from apm.models import EXAMPLE_MARK, AdapterCell, AdapterRun, CellEvent, ExecLog, ProcessState, RetryRequest, Source

NAME = "Example APM (synthetic)"
SEGMENTS = ["NSE", "NFO", "CDS", "BSE", "BFO", "SLBM", "MSE", "NIPO", "BSTAR", "NCOM"]
CLIENTS = [f"c{n:03d}" for n in range(1, 13)]
HOST = "demo-host-01"
# which segments each synthetic client trades on (the rest are "-": not configured)
LAYOUT = {
    "c001": ["NSE", "NFO", "CDS", "BSE"], "c002": ["NSE", "NFO", "BSE", "BFO", "SLBM"], "c003": ["NSE", "NFO", "NCOM"],
    "c004": ["NSE", "NFO", "CDS", "BSE", "BFO"], "c005": ["NSE", "NFO", "BSE", "BFO", "MSE"], "c006": ["NSE", "NFO", "CDS"],
    "c007": ["NSE", "CDS", "BSE", "NIPO"], "c008": ["NSE", "NFO", "BSE", "BSTAR"], "c009": ["CDS", "BSE"],
    "c010": ["NSE", "NFO", "BSE", "BFO", "NCOM"], "c011": ["NSE", "NFO", "MSE", "SLBM"], "c012": ["NSE", "NFO", "CDS", "BSE", "BFO", "NIPO"],
}
ERRORS = {
    ("c003", "NSE"): "ConnectionError: adapter session rejected by the exchange gateway (code 5)",
    ("c003", "NFO"): "TimeoutError: no heartbeat from the adapter for 120 s",
    ("c007", "CDS"): "AuthError: CTCL login failed (invalid session token)",
    ("c010", "BSE"): "ConnectionError: adapter session rejected by the exchange gateway (code 5)",
}
# failures present at each run (oldest first): the last entry is the current state
STEPS = [
    (timedelta(minutes=90), {}, {}),
    (timedelta(minutes=60), {("c003", "NSE")}, {}),
    (timedelta(minutes=30), {("c003", "NSE"), ("c003", "NFO"), ("c007", "CDS")}, {}),
    (timedelta(minutes=0), {("c003", "NSE"), ("c003", "NFO"), ("c007", "CDS"), ("c010", "BSE")}, {("c011", "MSE")}),
]
PROCESSES = [("adapterStatus.py", 34121, 2.31, 0.8, 152), ("processStatus.py", 34122, 1.45, 0.6, 118),
             ("connectionDetails.py", 34123, 1.12, 0.5, 141), ("alertPublisher.py", 34124, 0.85, 0.4, 96)]


def adapter_id(client, segment):
    rnd = random.Random(f"{client}/{segment}")
    return str(rnd.randint(10000, 49999))


def matrix_payload(at, failing, warning):
    data, ok, bad = {}, 0, 0
    for c in CLIENTS:
        row = {}
        for seg in SEGMENTS:
            if seg not in LAYOUT[c]:
                row[seg] = "-"
                continue
            key = (c, seg)
            if key in failing:
                row[seg] = {"isSuccess": False, "ctcl_id": adapter_id(c, seg), "value": adapter_id(c, seg), "status": 0,
                            "error": ERRORS.get(key, "Adapter reported a failure"), "exit_code": 1}
                bad += 1
            elif key in warning:
                row[seg] = {"isSuccess": True, "ctcl_id": adapter_id(c, seg), "value": adapter_id(c, seg), "status": 1,
                            "error": "Latency above threshold (2.4 s)"}
            else:
                row[seg] = {"isSuccess": True, "ctcl_id": adapter_id(c, seg), "value": adapter_id(c, seg), "status": 2}
                ok += 1
        data[c] = row
    total = sum(len(v) for v in LAYOUT.values())
    stamp = timezone.localtime(at).strftime("%d-%m-%Y %H:%M:%S")
    return {"overallStatus": not failing, "status": 0 if failing else 2,
            "executedOn": f"{stamp} [ Total: {total} ; Success: {ok} ; Failure: {bad} ]", "type": "matrix", "data": data}


def table_payload(at, down=()):
    rows = []
    stamp = timezone.localtime(at).strftime("%Y-%m-%d %H:%M:%S")
    for i, (name, pid, cpu, mem, resp) in enumerate(PROCESSES):
        up = name not in down
        rows.append({"pid": pid if up else "NA", "name": name, "cpu_percent": cpu if up else "NA", "memory_percent": mem if up else "NA",
                     "status": 2 if up else 0, "Status_": "running" if up else "not running", "host": HOST,
                     "uptime_s": 8353 + i * 60 if up else None,
                     "last_heartbeat": (at - timedelta(seconds=40 + 5 * i)).isoformat(),
                     "last_execution": (at - timedelta(seconds=45)).isoformat(), "next_execution": (at + timedelta(minutes=5)).isoformat(),
                     "response_ms": resp, "exit_code": 0, "remarks": "OK" if up else "process not found"})
    return {"overallStatus": not down, "status": 0 if down else 2,
            "executedOn": f"{stamp} [ Total: {len(rows)} ; Success: {len(rows) - len(down)} ; Failure: {len(down)} ]",
            "type": "table", "data": rows}


def log_lines(now):
    """100 invented log lines over the last hour, with the failures' errors in them."""
    rnd = random.Random(7)
    lines = []
    for i in range(100):
        ts = now - timedelta(seconds=(100 - i) * 34 + rnd.randint(0, 20))
        kind = i % 10
        if kind == 0:
            lines.append({"ts": ts, "level": "INFO", "process": "adapterStatus.py", "message": f"Execution started for {sum(len(v) for v in LAYOUT.values())} adapters across 10 exchanges"})
        elif kind == 1:
            lines.append({"ts": ts, "level": "INFO", "process": "adapterStatus.py", "message": "Fetching adapter data from Redis key: adapterStatusMatrix"})
        elif kind in (2, 3) and i > 55:
            (c, seg), err = list(ERRORS.items())[(i // 10) % len(ERRORS)]
            lines.append({"ts": ts, "level": "ERROR", "process": "adapterStatus.py", "message": f"{c} {seg} {adapter_id(c, seg)}: {err}", "client": c, "segment": seg})
        elif kind == 4:
            lines.append({"ts": ts, "level": "WARNING", "process": "connectionDetails.py", "message": "Session pool at 91% of its limit"})
        elif kind == 5:
            lines.append({"ts": ts, "level": "INFO", "process": "processStatus.py", "message": "Collected status of 4 processes"})
        elif kind == 6:
            lines.append({"ts": ts, "level": "DEBUG", "process": "alertPublisher.py", "message": "Queue depth 0"})
        elif kind == 7:
            lines.append({"ts": ts, "level": "INFO", "process": "adapterStatus.py", "message": "Execution completed: Total=%d" % sum(len(v) for v in LAYOUT.values())})
        else:
            lines.append({"ts": ts, "level": "INFO", "process": "alertPublisher.py", "message": "Alert published to the demo channel"})
    return lines


class Command(BaseCommand):
    help = "Create or refresh the synthetic example APM source (idempotent). --remove deletes it."

    def add_arguments(self, parser):
        parser.add_argument("--remove", action="store_true", help="delete the example source and all of its data")
        parser.add_argument("--rotate-token", action="store_true", help="issue a new ingest token and print it once")

    def handle(self, *args, **opts):
        tenant = services.tenant()
        existing = Source.objects.filter(tenant_key=tenant, name=NAME).first()
        if opts["remove"]:
            if existing is None:
                self.stdout.write("No example APM source to remove.")
                return
            if not existing.is_example:
                self.stderr.write("A source with that name exists but is not marked as an example; not removing it.")
                return
            existing.delete()
            self.stdout.write("Removed the example APM source and its data.")
            return
        if existing is not None and not existing.is_example:
            self.stderr.write("A source named like the example exists and is not marked as an example; leaving it alone.")
            return
        now = timezone.now()
        token = None
        with transaction.atomic():
            src = existing
            if src is None:
                src = Source(tenant_key=tenant, name=NAME, site="All Sites", enabled=True, expected_interval_s=3600,
                             description=f"{EXAMPLE_MARK} Synthetic adapters, processes and logs for demos. Re-run seed_apm to refresh.")
                token = services.issue_token(src)
                src.save()
            elif opts["rotate_token"] or not src.has_token:
                token = services.issue_token(src)
                src.save()
            Source.objects.filter(pk=src.pk).update(enabled=True, expected_interval_s=3600)
            # rebuild from scratch so a re-run never duplicates anything
            for model in (AdapterCell, AdapterRun, CellEvent, ProcessState, ExecLog, RetryRequest):
                model.objects.filter(source=src).delete()
            Source.objects.filter(pk=src.pk).update(silent_since=None, last_matrix_at=None, last_table_at=None, last_data_at=None)
            src.refresh_from_db()
            for age, failing, warning in STEPS:
                at = now - age
                services.ingest_matrix(src, matrix_payload(at, failing, warning), host=HOST, script="adapterStatus.py",
                                       duration_s=138.0 if age == timedelta(0) else 8.2, now=at, emit_events=False)
            services.ingest_table(src, table_payload(now), host=HOST, script="processStatus.py", duration_s=2.1, now=now,
                                  emit_events=False)
            services.ingest_logs(src, [{**ln, "ts": ln["ts"].isoformat()} for ln in log_lines(now)], now=now)
            # the bookkeeping the last ingest wrote is "now": keep it
        cells = AdapterCell.objects.filter(source=src).count()
        failed = AdapterCell.objects.filter(source=src, status=0).count()
        self.stdout.write(f"Example APM source ready: {cells} adapters ({failed} failing), {len(PROCESSES)} processes, "
                          f"{ExecLog.objects.filter(source=src).count()} log lines.")
        if token:
            self.stdout.write(f"Ingest token (shown once): {token}")
        else:
            self.stdout.write("The existing ingest token was kept (use --rotate-token to issue a new one).")
        self.stdout.write(f"Open /apm/?source={src.pk}")
