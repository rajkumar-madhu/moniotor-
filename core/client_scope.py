"""Persisted client filter for multi-customer tenants (session + ?client=)."""
from core.access import scope_by_site, visible_sites
from core.client_sites import client_visible, clients_for_user, sites_for_client

SESSION_KEY = "le_client"


def sync_client_session(request):
    """Store ?client= in session; auto-pick when the user only has one client."""
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        request.le_client = ""
        return ""
    asked = "client" in request.GET
    if asked:
        val = request.GET.get("client", "").strip()
        if not val:
            request.session.pop(SESSION_KEY, None)
        elif client_visible(request.user, val):
            request.session[SESSION_KEY] = val
        # anything else (a stale bookmark, another user's id, a huge number) is ignored
    stored = request.session.get(SESSION_KEY, "")
    stale = bool(stored) and not client_visible(request.user, stored)
    if stale:
        request.session.pop(SESSION_KEY, None)
    if not asked and (stale or SESSION_KEY not in request.session):
        ones = clients_for_user(request.user)
        if len(ones) == 1:
            request.session[SESSION_KEY] = str(ones[0].pk)
    cid = request.session.get(SESSION_KEY, "")
    request.le_client = cid
    return cid


def active_client(request):
    """Active client filter id (str), or "" for all clients."""
    if hasattr(request, "le_client"):
        return request.le_client or ""
    return request.session.get(SESSION_KEY, "") if getattr(request, "session", None) else ""


def client_site_names(user, client_param):
    """Site names for ``client_param`` that ``user`` may see; empty set if none."""
    if not client_param:
        return None
    names = set(sites_for_client(client_param) or [])
    allowed = visible_sites(user)
    if allowed is not None:
        names &= allowed
    return names


def filter_qs_by_scope(qs, user, field="site", client_param="", include_global=("All Sites", "")):
    """User site access, optionally narrowed to one client's sites."""
    if client_param:
        names = client_site_names(user, client_param)
        if not names:
            return qs.none()
        return qs.filter(**{f"{field}__in": list(names)})
    return scope_by_site(qs, user, field=field, include_global=include_global)


def narrow_site_names(names, user, client_param):
    """Intersect dashboard/site-picker names with an active client filter."""
    if not client_param:
        return names
    allowed = client_site_names(user, client_param)
    if not allowed:
        return []
    return [n for n in names if n in allowed]
