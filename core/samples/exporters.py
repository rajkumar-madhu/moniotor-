"""Illustrative data for the exporters page (shown with a "Sample data" label)."""
import random
from datetime import timedelta

from django.utils import timezone

HEALTH = ["ok", "warn", "crit"]


def _rng(name):
    return random.Random(name)

EXPORTERS = [
    ("snmp-switch-01", "SNMP", "Network", "10.10.1.10", 12450), ("fortinet-fw-01", "HTTP API", "Firewalls", "192.168.1.1", 8230),
    ("node-exporter-01", "Node Exporter", "Servers", "srv-app-01", 45621), ("windows-exporter", "Windows", "Servers", "win-srv-01", 23112),
    ("vmware-exporter", "vSphere", "Virtualization", "vcenter01", 15890), ("mysql-exporter", "MySQL", "Databases", "db-prod-01", 34556),
    ("blackbox-exporter", "HTTP/TCP", "Applications", "https://wecrew.in", 22345), ("kube-state-metrics", "Kubernetes", "Kubernetes", "k8s-cluster-01", 28991),
    ("netflow-exporter", "NetFlow", "Network", "192.168.10.1", 11234), ("syslog-exporter", "Syslog", "Security", "syslog-server", 67890),
    ("postgres-exporter", "PostgreSQL", "Databases", "pg-ledger-01", 9120), ("redis-exporter", "Redis", "Databases", "redis-bod-01", 4410),
    ("ipmi-exporter", "IPMI", "Servers", "idrac-mum-07", 1180), ("aruba-exporter", "SNMP", "Wireless", "aruba-ctrl-01", 6650),
]
CATEGORIES = ["Network", "Firewalls", "Servers", "Virtualization", "Storage", "Databases", "Applications", "Kubernetes", "Wireless", "Security", "Custom"]
LOG_LINES = [
    ("snmp-switch-01", "INFO", "Interface Gi1/0/1 status up"), ("fortinet-fw-01", "WARN", "High session count: 9800"),
    ("node-exporter-01", "INFO", "CPU usage: 45%"), ("mysql-exporter", "ERROR", "Slow query detected (2.3s)"),
    ("blackbox-exporter", "INFO", "HTTPS check success (200)"), ("vmware-exporter", "INFO", "Datastore usage: 78%"),
    ("syslog-exporter", "WARN", "Failed login attempt from 10.1.1.5"), ("windows-exporter", "INFO", "Memory usage: 62%"),
    ("kube-state-metrics", "INFO", "Pod restart detected: api-7d9c8"), ("netflow-exporter", "INFO", "High traffic from 192.168.20.10"),
]
EVENTS = [
    ("Critical", "High CPU on srv-app-01"), ("Warning", "Firewall session threshold exceeded"), ("Info", "SSL certificate expiring (7 days)"),
    ("Warning", "VM datastore usage high"), ("Info", "Pod restart detected: api-7d9c8"), ("Critical", "Switch port down: Gi1/0/24"),
    ("Warning", "Failed login attempt detected"), ("Info", "HTTPS check failed: customer.in"),
]


def exporters():
    r = _rng("exporters")
    now = timezone.localtime()
    rows = []
    for i, (name, typ, cat, target, logs) in enumerate(EXPORTERS):
        status = "error" if i == 12 else "inactive" if i == 13 else "active"
        rows.append({"name": name, "type": typ, "category": cat, "target": target, "status": status,
                     "last_seen": f"{r.randint(1, 3)} min ago" if status == "active" else "2 h ago", "logs": logs})
    logs = [{"time": (now - timedelta(seconds=2 * i + r.randint(0, 1))).strftime("%H:%M:%S"), "exporter": e, "level": lvl, "msg": m}
            for i, (e, lvl, m) in enumerate(LOG_LINES)]
    hours = [f"{h:02d}:00" for h in range(24)]
    info = [r.randint(28, 52) * 1000 for _ in hours]
    trend = {"labels": hours, "unit": "", "series": [
        {"name": "Info", "color": "#1565f0", "values": info},
        {"name": "Warn", "color": "#f59e0b", "values": [r.randint(4, 12) * 1000 for _ in hours]},
        {"name": "Error", "color": "#dc2626", "values": [r.randint(1, 6) * 1000 for _ in hours]}]}
    counts = {c: sum(1 for x in rows if x["category"] == c) for c in CATEGORIES}
    top = sorted(rows, key=lambda x: -x["logs"])[:8]
    events = [{"time": (now - timedelta(minutes=2 * i + 1)).strftime("%H:%M"), "sev": s, "msg": m} for i, (s, m) in enumerate(EVENTS)]
    health = {"active": sum(x["status"] == "active" for x in rows), "inactive": sum(x["status"] == "inactive" for x in rows),
              "error": sum(x["status"] == "error" for x in rows)}
    return {"rows": rows, "logs": logs, "trend": trend, "counts": counts, "top": top, "top_max": top[0]["logs"],
            "events": events, "health": health, "total": len(rows),
            "donut": {"segments": [{"label": "Active", "value": health["active"], "color": "#16a34a"},
                                   {"label": "Inactive", "value": health["inactive"], "color": "#94a3b8"},
                                   {"label": "Error", "value": health["error"], "color": "#ef4444"}],
                      "center": {"value": str(len(rows)), "label": "Exporters"}}}
