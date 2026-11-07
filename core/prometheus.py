"""Minimal Prometheus HTTP API client (instant/range queries, targets, alerts).

Only used when settings.PROMETHEUS_URL is set. Queries go server-side; the
browser never talks to Prometheus. Errors raise PrometheusError with a
user-safe message.
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings


class PrometheusError(Exception):
    pass


def configured():
    from administration import integration_config as ic

    return bool(ic.prometheus_url())


def _get(path, params=None):
    from administration import integration_config as ic

    if not configured():
        raise PrometheusError("Prometheus is not connected (set LINKEDEYE_PROMETHEUS_URL).")
    url = ic.prometheus_url().rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    try:
        with urllib.request.urlopen(url, timeout=ic.prometheus_timeout()) as r:  # noqa: S310 - operator-configured URL
            body = json.load(r)
    except urllib.error.HTTPError as e:
        try:
            body = json.load(e)
        except Exception:
            raise PrometheusError(f"Prometheus returned HTTP {e.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise PrometheusError(f"Prometheus unreachable: {getattr(e, 'reason', e)}") from None
    if body.get("status") != "success":
        raise PrometheusError(body.get("error") or "Prometheus query failed")
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
        out.append({"metric": r.get("metric", {}), "value": float(v) if v not in (None, "NaN") else None})
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
