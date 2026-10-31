"""Illustrative data for the firewall page (shown with a "Sample data" label)."""
import random
from datetime import timedelta

from django.utils import timezone

HEALTH = ["ok", "warn", "crit"]


def _rng(name):
    return random.Random(name)

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
