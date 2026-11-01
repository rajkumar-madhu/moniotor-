#!/usr/bin/env python3
"""Health check for the LinkedEye container image (Docker HEALTHCHECK, compose healthcheck, Kubernetes exec probe).

    python deploy/docker/healthcheck.py           web process: GET /healthz on the local gunicorn must answer 200
    python deploy/docker/healthcheck.py probes    scheduler (manage.py run_probes): its heartbeat must be fresh

Exit status 0 = healthy, 1 = unhealthy; one line of explanation goes to stdout (Docker shows it in
`docker inspect`).

web
    Connects to 127.0.0.1 on the port from GUNICORN_BIND (default 8000). Django rejects unknown Host headers,
    so the request carries the first usable entry of LINKEDEYE_ALLOWED_HOSTS ("localhost" when unset).
    /healthz is public and checks the database connection.

probes
    The administration app's `administration.heartbeat` job (run by run_probes every 60 s) stores
    Heartbeat(key="scheduler") with this container's hostname. Healthy when that row was written by this
    host less than HEALTHCHECK_MAX_HEARTBEAT_AGE seconds ago (default 300). This catches a scheduler loop that
    hangs as well as one that died. If the administration app is not installed, the check falls back to
    "the database is reachable".
"""
import http.client
import os
import socket
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]  # /app


def _host_header():
    for entry in os.environ.get("LINKEDEYE_ALLOWED_HOSTS", "").split(","):
        entry = entry.strip().lstrip(".")
        if entry and entry != "*" and "*" not in entry:
            return entry
    return "localhost"


def _port():
    bind = os.environ.get("GUNICORN_BIND", "").strip() or "0.0.0.0:8000"
    try:
        return int(bind.split(",")[0].rsplit(":", 1)[1])
    except (IndexError, ValueError):
        return 8000


def check_web(timeout=4.0):
    conn = http.client.HTTPConnection("127.0.0.1", _port(), timeout=timeout)
    try:
        conn.request("GET", "/healthz", headers={"Host": _host_header(), "User-Agent": "linkedeye-healthcheck"})
        resp = conn.getresponse()
        body = resp.read(512).decode("utf-8", "replace")
    except OSError as exc:
        return False, f"web: /healthz unreachable on port {_port()}: {exc}"
    finally:
        conn.close()
    if resp.status != 200:
        hint = " (Host header rejected: check LINKEDEYE_ALLOWED_HOSTS)" if resp.status == 400 else ""
        return False, f"web: /healthz returned HTTP {resp.status}{hint}"
    return True, f"web: ok {body.strip()[:80]}"


def check_probes(max_age=None):
    if max_age is None:
        try:
            max_age = int(os.environ.get("HEALTHCHECK_MAX_HEARTBEAT_AGE", "300"))
        except ValueError:
            max_age = 300
    sys.path.insert(0, str(BASE_DIR))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "linkedeye.settings")
    try:
        import django
        django.setup()
        from django.apps import apps
        from django.db import connection
        from django.utils import timezone

        connection.ensure_connection()
        try:
            Heartbeat = apps.get_model("administration", "Heartbeat")
        except LookupError:
            return True, "probes: database reachable (administration app absent, no heartbeat to check)"
        beat = Heartbeat.objects.filter(key="scheduler").values("seen_at", "host").first()
    except Exception as exc:  # any setup/database error means unhealthy, never a traceback
        return False, f"probes: {type(exc).__name__}: {str(exc)[:200]}"
    if not beat:
        return False, "probes: no scheduler heartbeat recorded yet"
    age = (timezone.now() - beat["seen_at"]).total_seconds()
    me = socket.gethostname()[:255]
    if beat["host"] and beat["host"] != me:
        return False, (f"probes: last heartbeat came from {beat['host']!r}, not this container ({me!r}), "
                       f"{age:.0f}s ago; is a second scheduler running?")
    if age > max_age:
        return False, f"probes: last heartbeat {age:.0f}s ago (limit {max_age}s); run_probes is stalled"
    return True, f"probes: heartbeat {age:.0f}s ago"


def main(argv):
    mode = argv[1] if len(argv) > 1 else "web"
    if mode not in ("web", "probes"):
        print(f"usage: {argv[0]} [web|probes]")
        return 2
    ok, message = check_web() if mode == "web" else check_probes()
    print(message)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
