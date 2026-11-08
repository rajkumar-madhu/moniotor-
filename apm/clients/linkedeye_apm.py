"""LinkedEye APM reference client for the cron toolkit (adapterStatus.py, processStatus.py).

Standard library only (urllib). Copy this single file next to your scripts. It never raises into the cron job: every
function returns a dict, ``{"ok": True, ...}`` on success or ``{"ok": False, "error": "..."}`` on failure, and logs
a one-line warning. TLS certificate verification is always on (set LINKEDEYE_APM_CA_BUNDLE to your CA file for a private
CA); plain http is refused unless LINKEDEYE_APM_ALLOW_HTTP=1 (testing only).

Configuration (environment, so no secret lives in a script):

    LINKEDEYE_APM_URL    https://linkedeye.example.com
    LINKEDEYE_APM_TOKEN  the ingest token shown once when the source was created
    LINKEDEYE_APM_TIMEOUT  seconds per request, default 10
    LINKEDEYE_APM_RETRIES  attempts per call, default 3 (exponential backoff, honours Retry-After)

Typical use, after the existing Redis write (``data`` is the dict the script already builds):

    import linkedeye_apm
    linkedeye_apm.push_matrix(data, script="adapterStatus.py", duration_s=elapsed)
    for cmd in linkedeye_apm.poll_commands().get("commands", []):   # "Retry Now" requests from the dashboard
        ...re-probe cmd["client"], cmd["segment"] ...
        linkedeye_apm.command_done(cmd["id"])
"""
import json
import logging
import math
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request

__all__ = ["push_matrix", "push_table", "push_logs", "poll_commands", "command_done"]
__version__ = "1.0"

log = logging.getLogger("linkedeye_apm")
MAX_BODY = 2 * 1024 * 1024
USER_AGENT = "linkedeye-apm-client/" + __version__


def _env(name, default=""):
    return (os.environ.get(name) or default).strip()


def _config(url=None, token=None):
    base = (url or _env("LINKEDEYE_APM_URL")).rstrip("/")
    token = token or _env("LINKEDEYE_APM_TOKEN")
    if not base or not token:
        return None, None, "LINKEDEYE_APM_URL and LINKEDEYE_APM_TOKEN must be set"
    if not base.lower().startswith("https://") and _env("LINKEDEYE_APM_ALLOW_HTTP") != "1":
        return None, None, "the URL must be https:// (set LINKEDEYE_APM_ALLOW_HTTP=1 for local testing only)"
    return base, token, ""


def _context():
    ca = _env("LINKEDEYE_APM_CA_BUNDLE")
    return ssl.create_default_context(cafile=ca or None)        # verification stays ON


def _clean(obj):
    """JSON-safe copy: NaN / infinity become null, unknown types become text."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if obj is None or isinstance(obj, (str, int, bool)):
        return obj
    return str(obj)


def _call(method, path, body=None, url=None, token=None):
    base, token, problem = _config(url, token)
    if problem:
        log.warning("linkedeye_apm: %s", problem)
        return {"ok": False, "error": problem}
    data = None
    if body is not None:
        try:
            data = json.dumps(_clean(body), allow_nan=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            return {"ok": False, "error": f"payload could not be encoded: {exc}"}
        if len(data) > MAX_BODY:
            return {"ok": False, "error": "payload is larger than the 2 MB limit; send fewer log lines"}
    try:
        attempts = max(1, min(6, int(_env("LINKEDEYE_APM_RETRIES", "3"))))
        timeout = max(1.0, float(_env("LINKEDEYE_APM_TIMEOUT", "10")))
    except ValueError:
        attempts, timeout = 3, 10.0
    ctx = _context()
    last = "no attempt was made"
    for attempt in range(attempts):
        req = urllib.request.Request(base + path, data=data, method=method, headers={
            "Content-Type": "application/json", "Accept": "application/json", "User-Agent": USER_AGENT})
        # urllib copies ordinary headers to a redirect target, even on another host; an "unredirected" header is not copied,
        # so a redirect can never carry the token anywhere else
        req.add_unredirected_header("Authorization", "Bearer " + token)
        delay = 2 ** attempt
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                payload = json.loads(resp.read().decode("utf-8") or "{}")
                payload.setdefault("ok", True)
                return payload
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("message", "")
            except Exception:                               # noqa: BLE001
                pass
            last = f"HTTP {exc.code} {detail}".strip()
            if exc.code in (400, 401, 403, 404, 405, 413, 422):
                break                                       # retrying cannot help
            try:
                delay = max(delay, min(60, int(exc.headers.get("Retry-After", "0"))))
            except (TypeError, ValueError):
                pass
        except (urllib.error.URLError, socket.timeout, ssl.SSLError, ConnectionError, OSError, ValueError) as exc:
            reason = getattr(exc, "reason", exc)
            last = f"{type(exc).__name__}: {reason}"
        if attempt < attempts - 1:
            time.sleep(delay)
    log.warning("linkedeye_apm: %s %s failed: %s", method, path, last)
    return {"ok": False, "error": last}


def _body(kind, data, host, script, duration_s, logs):
    body = {"type": kind, "host": host or socket.gethostname(), "script": script or os.path.basename(sys.argv[0] or "")}
    if isinstance(data, (bytes, bytearray)):
        data = data.decode("utf-8", "replace")
    body["payload_str" if isinstance(data, str) else "payload"] = data      # str(dict) from the Redis write works as is
    if duration_s is not None:
        body["duration_s"] = round(float(duration_s), 3)
    if logs:
        body["logs"] = logs
    return body


def push_matrix(data, host=None, script=None, duration_s=None, logs=None, completed_commands=None, url=None, token=None):
    """Send the adapter matrix: the dict adapterStatus.py builds (or its str()). Never raises."""
    try:
        body = _body("matrix", data, host, script, duration_s, logs)
        if completed_commands:
            body["completed_commands"] = list(completed_commands)
        return _call("POST", "/api/apm/ingest/", body, url, token)
    except Exception as exc:                                # noqa: BLE001 - never break the cron job
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def push_table(data, host=None, script=None, duration_s=None, logs=None, url=None, token=None):
    """Send the process table: the dict processStatus.py builds (or its str()). Never raises."""
    try:
        return _call("POST", "/api/apm/ingest/", _body("table", data, host, script, duration_s, logs), url, token)
    except Exception as exc:                                # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def push_logs(lines, host=None, script=None, url=None, token=None):
    """Send execution log lines: [{"ts": "2026-08-27 15:02:01,741", "level": "INFO", "process": "adapterStatus.py",
    "message": "..."}]. Optional "client" and "segment" tie a line to one adapter. Never raises."""
    try:
        body = {"type": "logs", "host": host or socket.gethostname(), "script": script or os.path.basename(sys.argv[0] or ""),
                "logs": list(lines)[:1000]}
        return _call("POST", "/api/apm/ingest/", body, url, token)
    except Exception as exc:                                # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def poll_commands(url=None, token=None):
    """Pending 'Retry Now' requests for this source: {"ok": True, "commands": [{"id", "client", "segment", ...}]}."""
    try:
        res = _call("GET", "/api/apm/commands/", None, url, token)
        res.setdefault("commands", [])
        return res
    except Exception as exc:                                # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "commands": []}


def command_done(command_id, url=None, token=None):
    """Report a command finished (the next push of the matrix also completes picked-up retries)."""
    try:
        return _call("POST", f"/api/apm/commands/{int(command_id)}/done/", {}, url, token)
    except Exception as exc:                                # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


if __name__ == "__main__":                                  # python linkedeye_apm.py  ->  list pending commands
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(poll_commands(), indent=2))
