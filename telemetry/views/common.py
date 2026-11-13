import csv
from urllib.parse import urlparse

from django.core.paginator import Paginator
from django.http import HttpResponse

from core import prometheus
from core.access import can


def per_page(request, default=20):
    try:
        return max(5, min(100, int(request.GET.get("per", default))))
    except (TypeError, ValueError):
        return default


def paginate(request, items, default=20):
    return Paginator(items, per_page(request, default)).get_page(request.GET.get("page"))


def query_without_page(request, *drop):
    q = request.GET.copy()
    for k in ("page",) + drop:
        q.pop(k, None)
    return q


def perms(request):
    u = request.user
    return {"can_operate": can(u, "operate"), "can_admin": can(u, "admin")}


def wants_refresh(request):
    """Does the request ask for fresh data (?refresh=1) and may it have it? Bypassing the caches sends the full set of
    Prometheus / Nagios queries straight to the servers, so only a user with the operate role can; a viewer's
    ?refresh=1 is served from the cache like any other request."""
    return request.GET.get("refresh") == "1" and can(request.user, "operate")


def prom_context():
    # through integration_config, like core.prometheus: a URL saved in Administration › Integrations wins over the
    # environment, so reading settings.PROMETHEUS_URL here showed the wrong (or no) host
    from administration import integration_config as ic
    url = ic.prometheus_url() or ""
    host = urlparse(url).netloc or url
    return {"prom_configured": prometheus.configured(), "prom_host": host}


def csv_response(filename, header, rows):
    resp = HttpResponse(content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    w = csv.writer(resp)
    w.writerow(header)
    for r in rows:
        w.writerow([_cell(v) for v in r])
    return resp


def _cell(v):
    s = "" if v is None else str(v)
    # neutralise spreadsheet formulas
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") and not _is_number(s) else s


def _is_number(s):
    try:
        float(s)
        return True
    except ValueError:
        return False


def is_pk(value):
    """A request parameter that can be a primary key: ASCII digits only (str.isdigit() accepts '²', which int()
    rejects) and within the integer range of the database."""
    s = str(value or "")
    return s.isascii() and s.isdigit() and len(s) <= 18


def matches(q, *fields):
    q = (q or "").strip().lower()
    return not q or any(q in str(f or "").lower() for f in fields)
