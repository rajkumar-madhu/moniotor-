"""Illustrative data for the heatmap page (shown with a "Sample data" label)."""
import random
from datetime import timedelta

from django.utils import timezone

HEALTH = ["ok", "warn", "crit"]


def _rng(name):
    return random.Random(name)

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
