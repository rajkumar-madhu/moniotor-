"""Machine-to-machine API: ``POST /api/apm/ingest/``, ``GET /api/apm/commands/``, ``POST /api/apm/commands/<id>/done/``.

Authentication is a per-source bearer token and NOTHING else: a browser session (cookie) never authenticates these
endpoints, which is why they may be CSRF-exempt. The ``/api/apm/`` prefix is listed in ``core.middleware.PUBLIC_PREFIXES``
so the login redirect does not intercept the producers; the views themselves refuse every request without a valid token.

Every authentication failure (no header, malformed token, unknown prefix, wrong secret, disabled source) returns the same
401 body after the same work, and every response carries a request id.
"""
import logging
import uuid

from django.core.cache import cache
from django.db import DatabaseError
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.debug import sensitive_variables

from core.audit import _ip

from . import payloads, services
from .models import Source

log = logging.getLogger("linkedeye.apm.api")

AUTH_FAIL_LIMIT = 30           # failed token checks per source address per minute, then 429
WARNINGS_RETURNED = 50


def _json(request_id, payload=None, status=200, **headers):
    body = {"request_id": request_id}
    body.update(payload or {})
    resp = JsonResponse(body, status=status)
    resp["X-Request-Id"] = request_id
    resp["Cache-Control"] = "no-store"
    for k, v in headers.items():
        resp[k.replace("_", "-")] = v
    return resp


def _counter(key, window):
    """Increment a fixed-window counter and return its value (a broken cache never blocks producers)."""
    try:
        if cache.add(key, 1, window):
            return 1
        try:
            return cache.incr(key)
        except ValueError:
            cache.set(key, 1, window)
            return 1
    except Exception:                               # noqa: BLE001
        log.warning("apm rate limiting unavailable", exc_info=True)
        return 0


def _unauthorized(rid):
    return _json(rid, {"error": "unauthorized", "message": "Invalid or missing bearer token."}, 401,
                 WWW_Authenticate='Bearer realm="linkedeye-apm"')


@sensitive_variables()
def _authenticate(request, rid):
    """(source, None) or (None, response).

    The token is checked first and a valid one is never turned away because of earlier failures from the same address
    (otherwise anyone sharing that address, or a spoofed forwarded address, could lock a producer out). Failed checks are
    counted per source address and answered with 429 once there are too many."""
    src = services.authenticate(request.headers.get("Authorization", ""))
    if src is None:
        ip = _ip(request) or "unknown"
        if _counter(f"apm:authfail:{ip}", 60) > AUTH_FAIL_LIMIT:
            return None, _json(rid, {"error": "rate_limited", "message": "Too many failed attempts."}, 429, Retry_After="60")
        return None, _unauthorized(rid)
    if _counter(f"apm:rate:{src.pk}", services.RATE_WINDOW) > services.RATE_LIMIT:
        return None, _json(rid, {"error": "rate_limited", "message": f"At most {services.RATE_LIMIT} requests per minute per source."},
                           429, Retry_After=str(services.RATE_WINDOW))
    return src, None


def _method(request, rid, allowed):
    if request.method in allowed:
        return None
    return _json(rid, {"error": "method_not_allowed", "message": f"Use {', '.join(allowed)}."}, 405, Allow=", ".join(allowed))


@csrf_exempt
def ingest(request):
    rid = uuid.uuid4().hex[:16]
    refused = _method(request, rid, ("POST",))
    if refused:
        return refused
    try:
        src, resp = _authenticate(request, rid)
        if resp is not None:
            return resp
        try:
            length = int(request.META.get("CONTENT_LENGTH") or 0)
        except ValueError:
            length = 0
        if length > payloads.MAX_BODY:
            return _json(rid, {"error": "too_large", "message": f"The body is limited to {payloads.MAX_BODY // 1024} KB."}, 413)
        try:
            raw = request.body
        except Exception:                           # noqa: BLE001 - RequestDataTooBig, broken stream
            return _json(rid, {"error": "too_large", "message": "The body could not be read or is too large."}, 413)
        if len(raw) > payloads.MAX_BODY:
            return _json(rid, {"error": "too_large", "message": f"The body is limited to {payloads.MAX_BODY // 1024} KB."}, 413)
        try:
            req = payloads.parse_request(raw)
            if req.logs:
                payloads.parse_logs(req.logs)       # refuse the whole request before anything is applied
            return _apply(src, req, rid, _ip(request))
        except payloads.PayloadError as exc:
            return _json(rid, {"error": exc.code, "message": str(exc)}, exc.http)
    except DatabaseError:
        log.exception("apm ingest database error (request %s)", rid)
        return _json(rid, {"error": "unavailable", "message": "Temporarily unavailable; nothing was applied. Retry."}, 503,
                     Retry_After="30")
    except Exception as exc:                        # noqa: BLE001 - a hostile body must never become a 500 page
        log.error("apm ingest failed (request %s): %s", rid, type(exc).__name__)
        return _json(rid, {"error": "unavailable", "message": "The request could not be processed."}, 503)


def _apply(src, req, rid, ip):
    now = timezone.now()
    out = {"ok": True, "type": req.kind}
    if req.kind == "matrix":
        res = services.ingest_matrix(src, req.payload, host=req.host, script=req.script, duration_s=req.duration_s, now=now,
                                     ip=ip, completed=req.completed)
    elif req.kind == "table":
        res = services.ingest_table(src, req.payload, host=req.host, script=req.script, duration_s=req.duration_s, now=now,
                                    ip=ip, completed=req.completed)
    else:
        res = services.ingest_logs(src, req.logs, now=now, ip=ip)
    warnings = list(res.get("warnings", []))
    total = res.get("warnings_total", len(warnings))
    for key in ("status", "duplicate", "run_id", "total", "success", "failure", "created", "updated", "removed", "accepted",
                "duplicates"):
        if key in res:
            out[key] = res[key]
    if req.kind != "logs" and req.logs:
        lres = services.ingest_logs(src, req.logs, now=now, ip=ip)
        out["logs_accepted"], out["logs_duplicates"] = lres["accepted"], lres["duplicates"]
        warnings += lres["warnings"]
        total += lres["warnings_total"]
    out["warnings"] = warnings[:WARNINGS_RETURNED]
    out["warnings_total"] = total
    from .models import RetryRequest
    out["pending_commands"] = RetryRequest.objects.filter(source=src, state=RetryRequest.PENDING).count()
    return _json(rid, out, 200)


@csrf_exempt
def commands(request):
    rid = uuid.uuid4().hex[:16]
    refused = _method(request, rid, ("GET",))
    if refused:
        return refused
    try:
        src, resp = _authenticate(request, rid)
        if resp is not None:
            return resp
        now = timezone.now()
        Source.objects.filter(pk=src.pk).update(last_seen=now, last_ip=_ip(request))
        rows = services.pick_commands(src, now)
        return _json(rid, {"commands": [
            {"id": r.pk, "action": "retry", "client": r.client, "segment": r.segment, "note": r.note,
             "requested_at": r.requested_at.isoformat()} for r in rows]})
    except Exception as exc:                        # noqa: BLE001
        log.error("apm commands failed (request %s): %s", rid, type(exc).__name__)
        return _json(rid, {"error": "unavailable", "message": "The request could not be processed."}, 503)


@csrf_exempt
def command_done(request, pk):
    rid = uuid.uuid4().hex[:16]
    refused = _method(request, rid, ("POST",))
    if refused:
        return refused
    try:
        src, resp = _authenticate(request, rid)
        if resp is not None:
            return resp
        if not services.complete_command(src, pk):
            return _json(rid, {"error": "not_found", "message": "No open command with that id for this source."}, 404)
        return _json(rid, {"ok": True, "id": pk})
    except Exception as exc:                        # noqa: BLE001
        log.error("apm command done failed (request %s): %s", rid, type(exc).__name__)
        return _json(rid, {"error": "unavailable", "message": "The request could not be processed."}, 503)
