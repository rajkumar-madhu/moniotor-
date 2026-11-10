"""Minimal Prometheus HTTP API client (instant/range queries, targets, alerts, rules).

Only used when settings.PROMETHEUS_URL is set. Queries go server-side; the
browser never talks to Prometheus. Errors raise PrometheusError with a
user-safe message.
"""
import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings
from django.views.decorators.debug import sensitive_variables


class PrometheusError(Exception):
    pass


USER_AGENT = "LinkedEye-Prometheus/1.0"


def configured():
    from administration import integration_config as ic

    return bool(ic.prometheus_url())


_AUTH_TTL = 60
_auth_memo = {}      # (credential id, rotated_at) -> (expires monotonic, header value); in process memory only


@sensitive_variables()
def _auth_header():
    """The ``Authorization`` header for a Prometheus that needs a login, or None.

    The password lives in the credential vault (administration.integration_config only holds the id). It is
    decrypted at most once a minute per process, so the audit trail records the use without one entry per
    query, and it is attached as an *unredirected* header so a redirect can never forward it."""
    import base64
    import time

    from administration import integration_config as ic

    cid = ic.prometheus_credential_id()
    if not cid:
        return None
    from core import secrets
    from onboarding.models import Credential

    cred = Credential.objects.filter(pk=cid, tenant_key=settings.LINKEDEYE_TENANT, kind="basic_auth").first()
    if cred is None:
        raise PrometheusError("The stored Prometheus login was deleted: connect Prometheus again.")
    key = (cred.pk, cred.rotated_at.isoformat() if cred.rotated_at else "")
    hit = _auth_memo.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    try:
        with secrets.use(cred, reason="prometheus query") as password:
            token = base64.b64encode(f"{cred.username}:{password}".encode()).decode()
    except (secrets.VaultUnavailable, secrets.VaultError) as exc:
        raise PrometheusError(f"Prometheus needs a login but the credential vault cannot supply it. {exc}") from None
    value = f"Basic {token}"
    _auth_memo.clear()
    _auth_memo[key] = (time.monotonic() + _AUTH_TTL, value)
    return value


MAX_GET_QUERY = 4096                 # longer query strings go as a form POST (see _get)
POST_PATHS = ("/api/v1/query", "/api/v1/query_range")


def _get(path, params=None):
    """Call a read-only API endpoint and return its ``data``.

    Parameters go in the URL, except that a query/query_range whose encoded parameters exceed ``MAX_GET_QUERY``
    bytes is sent as a form-encoded POST (the Prometheus HTTP API accepts both). PromQL that lists many instances
    (the Fleet Overview's site-scoped matcher) runs to tens of KB, and a reverse proxy in front of Prometheus
    (nginx keeps the request line within 8 KB by default) would refuse it with HTTP 414 as a GET."""
    from administration import integration_config as ic

    if not configured():
        raise PrometheusError("Prometheus is not connected (set LINKEDEYE_PROMETHEUS_URL).")
    url = ic.prometheus_url().rstrip("/") + path
    data = None
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if params:
        encoded = urllib.parse.urlencode(params, doseq=True)
        if len(encoded) > MAX_GET_QUERY and path in POST_PATHS:
            data = encoded.encode("ascii")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            url += "?" + encoded
    request = urllib.request.Request(url, data=data, headers=headers)
    header = _auth_header()
    if header:
        request.add_unredirected_header("Authorization", header)
    try:
        with urllib.request.urlopen(request, timeout=ic.prometheus_timeout()) as r:  # noqa: S310 - operator-configured URL
            body = json.load(r)
    except urllib.error.HTTPError as e:
        try:
            body = json.load(e)
        except Exception:
            raise PrometheusError(f"Prometheus returned HTTP {e.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise PrometheusError(f"Prometheus unreachable: {getattr(e, 'reason', e)}") from None
    except (ValueError, http.client.HTTPException):
        # a login page or proxy error page that answers 200, or a connection cut mid-body
        raise PrometheusError("Prometheus returned something that is not a valid API response") from None
    if not isinstance(body, dict):
        raise PrometheusError("Prometheus returned something that is not a valid API response")
    if body.get("status") != "success":
        raise PrometheusError(body.get("error") or "Prometheus query failed")
    if "data" not in body:
        raise PrometheusError("Prometheus returned something that is not a valid API response")
    return body["data"]


def query(promql, at=None):
    """Instant query -> list of {"metric": {...}, "value": float}."""
    params = {"query": promql}
    if at:
        params["time"] = at
    data = _get("/api/v1/query", params)
    out = []
    for r in data.get("result", []):
        v = r.get("value", [None, None])[1]
        try:
            value = float(v) if v not in (None, "NaN") else None
        except (TypeError, ValueError):
            value = None
        out.append({"metric": r.get("metric", {}), "value": value})
    return out


def query_range(promql, seconds=3600, step=None):
    """Range query over the last `seconds` -> list of {"metric", "values": [(ts, float|None)]}.

    `end` is snapped down to a whole multiple of `step`. Prometheus evaluates a range query at exactly
    start + k*step, so without the snap two queries issued a few milliseconds apart (every chart whose series
    come from more than one query, run concurrently) come back on grids offset by that difference. Aligning
    them would then union both grids and leave every series full of gaps, which breaks the shared time axis
    (`ts`) that the charts use to sync their crosshair and zoom. It also makes the response cacheable per step.
    """
    step = max(1, int(step or max(15, int(seconds / 120))))
    end = (int(time.time()) // step) * step
    data = _get("/api/v1/query_range", {"query": promql, "start": end - seconds, "end": end, "step": step})
    out = []
    for r in data.get("result", []):
        out.append({"metric": r.get("metric", {}),
                    "values": [(float(t), (float(v) if v != "NaN" else None)) for t, v in r.get("values", [])]})
    return out


def targets():
    return _get("/api/v1/targets").get("activeTargets", [])


def alerts():
    return _get("/api/v1/alerts").get("alerts", [])


def rules():
    """Rule groups from /api/v1/rules (alerting and recording rules with their state)."""
    return _get("/api/v1/rules").get("groups", [])
