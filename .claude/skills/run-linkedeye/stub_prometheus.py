"""Stub Prometheus HTTP API for a local LinkedEye lab run (agent tooling, never deployed).

Serves /api/v1/query and /api/v1/query_range the way snmp_exporter (instance lab-sw-01:161, 24 ports) and
node_exporter (instance lab-vm-01:9100) data would look, so the real network.metrics job, the detail page charts and
network:live all run end to end. Values drift with time so live polling has something to show.

Like real Prometheus, every range answer is computed from its own request's start/end, so two queries issued a few
ms apart return timestamps a few ms apart (that is what network.metrics.align_series exists for).

Usage: python stub_prometheus.py [port]      (default 9099, binds 127.0.0.1)
"""
import json
import math
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

SW, VM = "lab-sw-01:161", "lab-vm-01:9100"
PORTS = range(1, 25)
DOWN, ADMIN_DOWN = {7, 14}, {20}          # oper down while admin up; admin down ("unused")
BOOT = time.time() - (41 * 86400 + 6 * 3600)


def port_in(i, t):
    if i in DOWN | ADMIN_DOWN:
        return 0.0
    return round((40 + 15 * i) * (1 + 0.35 * math.sin(t / 45 + i)), 3)


def port_out(i, t):
    if i in DOWN | ADMIN_DOWN:
        return 0.0
    return round((25 + 9 * i) * (1 + 0.35 * math.cos(t / 50 + i)), 3)


def port_err(i, t):
    return 0.3 + 0.2 * math.sin(t / 30) if i == 5 else 0.0     # port 5 shows as "busy or errors"


def guest(q, t):
    """node_exporter series for the VM (already in the unit the PromQL expression produces)."""
    if "node_cpu_seconds_total" in q:
        return 22 + 12 * math.sin(t / 40)
    if "MemAvailable" in q and "MemTotal" in q:
        return 61 + 4 * math.sin(t / 70)                # percent (range query form)
    if "MemAvailable" in q:
        return 16e9 * (1 - (61 + 4 * math.sin(t / 70)) / 100)
    if "MemTotal" in q:
        return 16e9
    if "node_filesystem" in q:
        return 47.5
    if "node_network" in q:
        return 14 + 6 * math.sin(t / 35)
    if "node_disk" in q:
        return 3 + 2 * math.sin(t / 25)
    if "node_load1" in q:
        return 1.2 + 0.5 * math.sin(t / 60)
    if "node_boot_time_seconds" in q:
        return t - BOOT
    return None


def network_value(q, t):
    m = re.search(r'ifIndex="(\d+)"', q)        # ?port= narrows the detail charts to one interface
    ports = [int(m.group(1))] if m else list(PORTS)
    if "ifInErrors" in q:
        return sum(port_err(i, t) for i in ports)
    if "ifInDiscards" in q:
        return 0.0
    if "ifHCInOctets" in q:
        return sum(port_in(i, t) for i in ports)
    if "ifHCOutOctets" in q:
        return sum(port_out(i, t) for i in ports)
    return None


def instant(q, t):
    """Instant vectors as [(labels, value)]. Order of the checks matters: see network.metrics.queries()."""
    if "node_" in q:
        v = guest(q, t)
        return [] if v is None else [({"instance": VM, "job": "node"}, v)]
    if "lab-sw-01" not in q:
        return []
    base = {"instance": SW, "job": "snmp"}
    if "sysUpTime" in q:
        return [(base, t - BOOT)]
    rows = []
    for i in PORTS:
        lab = dict(base, ifIndex=str(i), ifName=f"Gi1/0/{i}", ifAlias="Uplink to core" if i == 24 else "")
        if "changes(ifOperStatus" in q:
            v = 3 if i == 5 else 0
        elif "ifInErrors" in q:
            v = port_err(i, t)
        elif "ifInDiscards" in q:
            v = 0.0
        elif "ifHCInOctets" in q:
            v = port_in(i, t)
        elif "ifHCOutOctets" in q:
            v = port_out(i, t)
        elif "ifOperStatus" in q:
            v = 2 if i in DOWN | ADMIN_DOWN else 1
        elif "ifAdminStatus" in q:
            v = 2 if i in ADMIN_DOWN else 1
        elif "ifHighSpeed" in q:
            v = 1000
        elif "ifType" in q:
            lab["ifType"] = "ethernetCsmacd"
            v = 1
        else:
            return []
        rows.append((lab, v))
    return rows


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_json(self, body):
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        # LinkedEye sends a query longer than 4 KB as a form-encoded POST (real Prometheus accepts both)
        n = int(self.headers.get("Content-Length") or 0)
        self.do_GET(parse_qs(self.rfile.read(n).decode("utf-8", "replace")))

    def do_GET(self, form=None):
        u = urlparse(self.path)
        p = {k: v[0] for k, v in {**parse_qs(u.query), **(form or {})}.items()}
        q, now = p.get("query", ""), time.time()
        if u.path == "/api/v1/query":
            res = [{"metric": m, "value": [now, str(v)]} for m, v in instant(q, now)]
            return self.send_json({"status": "success", "data": {"resultType": "vector", "result": res}})
        if u.path == "/api/v1/query_range":
            start, end, step = float(p["start"]), float(p["end"]), float(p["step"])
            vals, t = [], start
            while t <= end:
                v = guest(q, t) if "node_" in q else network_value(q, t)
                if v is not None:
                    vals.append([t, str(round(v, 4))])
                t += step
            res = [{"metric": {}, "values": vals}] if vals else []
            return self.send_json({"status": "success", "data": {"resultType": "matrix", "result": res}})
        # /api/v1/targets, /api/v1/alerts and anything else: empty but well-formed
        self.send_json({"status": "success", "data": {"activeTargets": [], "alerts": []}})


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9099
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
