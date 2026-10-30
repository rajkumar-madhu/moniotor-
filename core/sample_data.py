"""Illustrative data for pages whose real sources (Prometheus/Nagios, exporters,
Redmine, SNMP) are not connected yet. Every page built from this module shows a
"Sample data" label. Values are deterministic so screenshots are reproducible."""
import random
from datetime import timedelta

from django.utils import timezone

HEALTH = ["ok", "warn", "crit"]


def _rng(name):
    return random.Random(name)


# ------------------------------------------------------------------ heatmap
HEAT_COLUMNS = ["Availability", "Response", "Errors", "Latency", "CPU", "Memory", "Disk", "Network"]
HEAT_GROUPS = {
    "Trading": ["OMS", "Trade Adapter", "Risk Engine", "Market Data", "Order Gateway"],
    "Infrastructure": ["Kubernetes", "Node Exporter", "SNMP Devices", "Blackbox Probes", "Load Balancers"],
    "Applications": ["LinkedEye", "ITSM", "Keycloak", "Jenkins", "Harbor", "SonarQube"],
    "Databases": ["PostgreSQL", "Redis", "RabbitMQ", "MongoDB"],
    "External Services": ["Exchange APIs", "Broker API (MCX)"],
}
DOMAINS = [
    ("Trading", "Trading and order management systems", 8, "critical", 99.21, 2.4, 620),
    ("Infrastructure", "Core infrastructure and platform", 6, "warning", 99.88, 0.6, 180),
    ("Applications", "Business and internal applications", 7, "healthy", 99.94, 0.3, 120),
    ("Databases", "Database and cache services", 4, "healthy", 99.96, 0.2, 85),
    ("External Services", "Third party and exchange services", 3, "warning", 99.73, 1.1, 340),
    ("Security", "Identity and security services", 3, "healthy", 99.99, 0.1, 45),
]


def heatmap():
    r = _rng("heatmap")
    rows = []
    for group, services in HEAT_GROUPS.items():
        svc_rows = []
        for s in services:
            cells = [r.choices(HEALTH, weights=[86, 9, 5])[0] for _ in HEAT_COLUMNS]
            svc_rows.append({"name": s, "cells": cells})
        worst = [max((row["cells"][i] for row in svc_rows), key=HEALTH.index) for i in range(len(HEAT_COLUMNS))]
        rows.append({"group": group, "cells": worst, "services": svc_rows})
    return {"columns": HEAT_COLUMNS, "rows": rows, "domains": DOMAINS,
            "top_services": [("OMS", "critical", 99.02, 3.1, 820), ("Trade Adapter", "warning", 99.61, 1.2, 420),
                             ("Risk Engine", "healthy", 99.98, 0.1, 110), ("Market Data", "healthy", 99.95, 0.2, 95),
                             ("Order Gateway", "warning", 99.71, 1.0, 310)],
            "incidents": [("INC000123", "P1", "OMS processing delay", "87m"), ("INC000122", "P2", "Adapter connection errors", "2h"),
                          ("INC000121", "P2", "High order rejection rate", "3h"), ("INC000118", "P3", "Market data feed latency", "5h"),
                          ("INC000117", "P3", "Risk engine timeout", "8h")],
            "chain": ["Exchange", "Market Data", "OMS", "Risk Engine", "Database"]}


# ------------------------------------------------------------------ exporters / logs / events
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


# ------------------------------------------------------------------ incidents
INCIDENTS = [
    ("INC-1287", "VPN connection issue", "High", "Open", "WeCrew", "Network", "Arul", "2h"),
    ("INC-1286", "Server CPU high", "Critical", "In Progress", "Finspot", "Server", "Priya", "1h"),
    ("INC-1285", "Email not working", "Medium", "Resolved", "Mirae", "Email", "Karthik", "—"),
    ("INC-1284", "Website down", "High", "Open", "Client A", "Application", "Unassigned", "3h"),
    ("INC-1283", "Switch port flap", "Low", "Closed", "Client B", "Network", "Ravi", "—"),
    ("INC-1282", "Database slow", "High", "In Progress", "WeCrew", "Database", "Divya", "45m"),
    ("INC-1281", "Domain not reachable", "Critical", "Open", "Client C", "DNS", "Unassigned", "4h"),
    ("INC-1280", "Printer not working", "Low", "Resolved", "Client D", "Hardware", "Mani", "—"),
    ("INC-1279", "New user access", "Medium", "Open", "WeCrew", "Access", "Unassigned", "1h"),
]


def incidents():
    r = _rng("incidents")
    days = [(timezone.localdate() - timedelta(days=14 - i)) for i in range(15)]
    created = [r.randint(8, 22) + i for i in range(15)]
    resolved = [max(4, c - r.randint(0, 8)) for c in created]
    created[-1] += 10
    return {
        "rows": [dict(zip(["id", "title", "priority", "status", "client", "category", "assignee", "sla"], x)) for x in INCIDENTS],
        "trend": {"labels": [d.strftime("%b %d") for d in days], "unit": "", "series": [
            {"name": "Created", "color": "#1565f0", "values": created, "area": True, "width": 2},
            {"name": "Resolved", "color": "#16a34a", "values": resolved, "area": True, "width": 2}]},
        "by_status": {"segments": [{"label": "Open", "value": 48, "color": "#ef4444"}, {"label": "In Progress", "value": 24, "color": "#f59e0b"},
                                   {"label": "On Hold", "value": 12, "color": "#4f46e5"}, {"label": "Resolved", "value": 76, "color": "#16a34a"},
                                   {"label": "Closed", "value": 36, "color": "#94a3b8"}], "center": {"value": "124", "label": "Total"}},
        "sla": {"segments": [{"label": "Met", "value": 108, "color": "#16a34a"}, {"label": "Breached", "value": 12, "color": "#ef4444"},
                             {"label": "At Risk", "value": 4, "color": "#f59e0b"}], "center": {"value": "87%", "label": "SLA Met"}},
        "categories": [("Network", 32), ("Server", 28), ("Application", 20), ("Access Management", 16), ("Email", 12), ("Database", 8), ("Hardware", 8)],
        "major": [("INC-1286", "Server CPU high", "2 hours ago · Finspot", "Open"), ("INC-1281", "Domain not reachable", "12 hours ago · Client C", "Open"),
                  ("INC-1265", "Database outage", "1 day ago · WeCrew", "In Progress")],
    }


# ------------------------------------------------------------------ firewall device
def firewall():
    r = _rng("fg01")
    hours = [f"{h:02d}:00" for h in range(0, 24)]
    wave = lambda base, amp: [round(base + amp * r.uniform(-1, 1)) for _ in hours]  # noqa: E731
    ifaces = [("port1 (WAN1)", "Up", "WAN", 680, 520, 68), ("port2 (WAN2)", "Up", "WAN", 450, 320, 45), ("port3 (LAN)", "Up", "LAN", 220, 180, 32),
              ("port4 (DMZ)", "Up", "LAN", 120, 95, 18), ("port5 (Internal)", "Up", "LAN", 80, 60, 12), ("port6 (Guest)", "Up", "LAN", 40, 28, 6),
              ("port7 (Management)", "Up", "MGMT", 12, 8, 2), ("port8 (HA)", "Up", "HA", 5, 4, 1), ("port9 (Unused)", "Down", "-", 0, 0, 0),
              ("port10 (Unused)", "Down", "-", 0, 0, 0)]
    now = timezone.localtime()
    sec = [("Critical", "Intrusion", "203.0.113.45", "10.10.20.15", "ET MALWARE C2 Communication Blocked"),
           ("Warning", "Web Filter", "192.168.1.25", "45.33.100.11", "Malicious URL Blocked (phishing)"),
           ("Warning", "IPS", "198.51.100.32", "10.10.10.8", "SQL Injection Attempt Blocked"),
           ("Info", "VPN", "-", "-", "User logged in (SSL VPN)"),
           ("Warning", "AntiVirus", "203.0.113.88", "10.10.5.20", "Malware Download Blocked"),
           ("Info", "System", "-", "-", "Configuration change by admin")]
    return {
        "info": [("Hostname", "FG-01"), ("IP Address", "10.10.1.254"), ("Management IP", "10.10.1.254"), ("Model", "FortiGate 100F"),
                 ("Serial Number", "FG100FTK21012345"), ("Firmware Version", "v7.4.3 (build 2654)"), ("HA Status", "Standalone"),
                 ("Site / Location", "Mumbai (NSE) - DC-Rack-01"), ("Role", "Perimeter Firewall"), ("Contact", "network@wecrew.in"),
                 ("Description", "Main firewall for production network")],
        "ifaces": ifaces,
        "traffic": {"labels": hours, "unit": "Mb/s", "series": [{"name": "Inbound", "color": "#1565f0", "values": wave(620, 120)},
                                                                {"name": "Outbound", "color": "#f97316", "values": wave(470, 90)}]},
        "sessions": {"labels": hours, "unit": "", "series": [{"name": "Active Sessions", "color": "#16a34a", "area": True, "values": wave(125000, 18000)}]},
        "security": [((now - timedelta(minutes=37 * i + 5)).strftime("%Y-%m-%d %H:%M"),) + s for i, s in enumerate(sec)],
        "apps": [("YouTube", 320, 24, "#ef4444"), ("HTTPS", 280, 21, "#1565f0"), ("Netflix", 180, 13, "#9333ea"), ("Microsoft", 120, 9, "#f97316"),
                 ("Google", 100, 7, "#16a34a"), ("Facebook", 80, 6, "#2563eb"), ("Teams", 60, 4, "#7c3aed"), ("Others", 210, 15, "#94a3b8")],
        "spark_cpu": {"values": wave(18, 6), "color": "#16a34a"},
        "spark_mem": {"values": wave(62, 5), "color": "#1565f0"},
        "spark_disk": {"values": wave(46, 4), "color": "#f97316"},
    }
