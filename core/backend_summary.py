"""Read-only bridge to the legacy Neo4j dashboard summary API."""
import hashlib
import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from .overview import site_names

MODULES = (("hardware", "Hardware"), ("software", "Software"), ("application", "Applications"))
STATUSES = ("CRITICAL", "WARNING", "OK", "UNKNOWN")
CACHE_SECONDS = 45
TIMEOUT_SECONDS = 4
TOTAL_BUDGET_SECONDS = 9   # stop issuing batches before the browser's 12 s fetch abort (backend-health.js)
MAX_RESPONSE_BYTES = 256 * 1024
MAX_SITES_PER_REQUEST = 50
MAX_QUERY_CHARS = 1800   # keep each upstream URL well under common 2–8 KB request-line limits (no 414s)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _empty(configured=False):
    return {"configured": configured, "available": False, "partial": False, "modules": [], "site_count": 0}


def _safe_endpoint(value):
    """Only use an administrator-configured HTTPS endpoint (or localhost in DEBUG)."""
    try:
        parts = urlsplit(value)
        if parts.scheme == "https" and parts.hostname and not parts.username and not parts.password:
            return parts
        if (settings.DEBUG and parts.scheme == "http" and parts.hostname in {"localhost", "127.0.0.1"}
                and not parts.username and not parts.password):
            return parts
    except (TypeError, ValueError):
        pass
    return None


def _count(raw, status):
    if not isinstance(raw, dict):
        return 0
    value = raw.get(status, raw.get(status.lower(), 0))
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, number)


def _modules(data):
    if not isinstance(data, dict):
        return []
    out = []
    for key, label in MODULES:
        raw = data.get(key)
        if not isinstance(raw, dict):
            continue
        counts = {state.lower(): _count(raw, state) for state in STATUSES}
        total = sum(counts.values())
        out.append({
            "key": key,
            "label": label,
            "counts": counts,
            "total": total,
            "ok_pct": round(counts["ok"] * 100 / total, 1) if total else 0,
            "critical_end_pct": round(counts["critical"] * 100 / total, 1) if total else 0,
            "warning_end_pct": round((counts["critical"] + counts["warning"]) * 100 / total, 1) if total else 0,
            "ok_end_pct": round((counts["critical"] + counts["warning"] + counts["ok"]) * 100 / total, 1) if total else 0,
        })
    return out


def _chunks(site_list, base_query=""):
    """Split sites into batches bounded by count and by encoded query length, so long or many site names
    never produce an oversized URL. A single name too long to fit on its own is dropped (it cannot be sent)."""
    chunk, size = [], len(base_query)
    for name in site_list:
        part = len(urlencode([("site", name)])) + 1
        if len(base_query) + part > MAX_QUERY_CHARS:
            continue
        if chunk and (len(chunk) >= MAX_SITES_PER_REQUEST or size + part > MAX_QUERY_CHARS):
            yield chunk
            chunk, size = [], len(base_query)
        chunk.append(name)
        size += part
    if chunk:
        yield chunk


def _cache_key(site_list):
    """Fixed-length, memcached-safe key (no spaces/control characters, never over 250 chars)."""
    material = "\n".join([str(settings.LINKEDEYE_TENANT), *sorted(site_list)])
    return "backend-summary:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def _backend_credentials():
    from administration import integration_config as ic

    return ic.backend_summary_url(), ic.backend_summary_token()


def _fetch(site_list):
    endpoint, token = _backend_credentials()
    parts = _safe_endpoint(endpoint)
    if not parts or not token or not site_list:
        return None

    totals = {key: {state.lower(): 0 for state in STATUSES} for key, _ in MODULES}
    available_modules = set()
    successful_sites = 0
    partial = False
    successes = 0
    base = parse_qsl(parts.query, keep_blank_values=True)
    sent = 0
    deadline = time.monotonic() + TOTAL_BUDGET_SECONDS
    for chunk in _chunks(site_list, urlencode(base)):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break   # remaining sites are reported as partial via `sent`
        sent += len(chunk)
        query = base + [("site", name) for name in chunk]
        url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))
        request = Request(url, headers={"Accept": "application/json", "Authorization": f"Bearer {token}"})
        try:
            with build_opener(_NoRedirect).open(request, timeout=min(TIMEOUT_SECONDS, remaining)) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                partial = True
                continue
            payload = json.loads(body.decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, UnicodeError):
            partial = True
            continue
        if not isinstance(payload, dict) or payload.get("status") != 200:
            partial = True
            continue
        modules = _modules(payload.get("data"))
        if not modules:
            partial = True
            continue
        for module in modules:
            available_modules.add(module["key"])
            for state, count in module["counts"].items():
                totals[module["key"]][state] += count
        try:
            successful_sites += max(0, int(payload.get("site_count", 0) or 0))
        except (TypeError, ValueError, OverflowError):
            partial = True
        partial = partial or bool(payload.get("partial"))
        successes += 1
    if not successes:
        return None
    partial = partial or sent < len(site_list)
    modules = _modules({key: totals[key] for key, _label in MODULES if key in available_modules})
    return {
        "configured": True,
        "available": True,
        "partial": partial,
        "modules": modules,
        "site_count": successful_sites,
        "fetched_at": timezone.now().isoformat(),
    }


def summary(user, selected_site="", client_param=""):
    """Return tenant/site-scoped backend counts; no site outside the user's visible sites is sent upstream."""
    from .client_scope import narrow_site_names

    endpoint, token = _backend_credentials()
    configured = bool(_safe_endpoint(endpoint) and token)
    if not configured:
        return _empty()

    visible = narrow_site_names(site_names(user), user, client_param)
    if selected_site:
        if selected_site not in visible:
            return _empty(configured=True)
        requested = [selected_site]
    else:
        requested = visible
    if not requested:
        return _empty(configured=True)

    cache_key = _cache_key(requested)
    result = cache.get(cache_key)
    if result is None:
        result = _fetch(requested) or _empty(configured=True)
        cache.set(cache_key, result, CACHE_SECONDS)
    return result
