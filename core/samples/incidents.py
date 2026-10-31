"""Illustrative data for the incidents page (shown with a "Sample data" label)."""
import random
from datetime import timedelta

from django.utils import timezone

HEALTH = ["ok", "warn", "crit"]


def _rng(name):
    return random.Random(name)

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
