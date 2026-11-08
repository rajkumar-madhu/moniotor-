"""Map core.Client records to site names for filters and roll-ups in other modules."""
from django.conf import settings

from core.access import visible_sites


def tenant():
    return settings.LINKEDEYE_TENANT


def site_client_lookup():
    """{site name: (client_id, client name)} for sites linked to a client."""
    from core.models import Site

    return {s.name: (s.client_id, s.client.name)
            for s in Site.objects.filter(tenant_key=tenant(), client__isnull=False).select_related("client")}


MAX_PK_DIGITS = 18   # below 2**63: databases reject (SQLite: OverflowError) a larger integer in a WHERE clause


def safe_pk(value):
    """A positive integer primary key from user input, or None. Digits only, bounded, so it is always safe to hand
    to the ORM (a 20-digit ?client= used to raise OverflowError and turn into an HTTP 500)."""
    text = str(value if value is not None else "").strip()
    if not text or len(text) > MAX_PK_DIGITS or not text.isascii() or not text.isdigit():
        return None
    pk = int(text)
    return pk if pk > 0 else None


def client_visible(user, client_param):
    """True for the literal ``unassigned`` or the pk of a client with at least one site ``user`` may see."""
    from core.models import Site

    if str(client_param) == "unassigned":
        return True
    pk = safe_pk(client_param)
    if pk is None:
        return False
    qs = Site.objects.filter(tenant_key=tenant(), client_id=pk)
    allowed = visible_sites(user)
    if allowed is not None:
        qs = qs.filter(name__in=allowed)
    return qs.exists()


def sites_for_client(client_param):
    """Site names for a client filter value (int pk, or the literal ``unassigned``). None if invalid."""
    from core.models import Site

    if client_param in (None, ""):
        return None
    if str(client_param) == "unassigned":
        return list(Site.objects.filter(tenant_key=tenant(), client__isnull=True).values_list("name", flat=True))
    cid = safe_pk(client_param)
    if cid is None:
        return []
    return list(Site.objects.filter(tenant_key=tenant(), client_id=cid).values_list("name", flat=True))


def clients_for_user(user):
    """Clients that have at least one site visible to ``user``."""
    from core.models import Client, Site

    allowed = visible_sites(user)
    qs = Site.objects.filter(tenant_key=tenant(), client__isnull=False)
    if allowed is not None:
        qs = qs.filter(name__in=allowed)
    ids = qs.values_list("client_id", flat=True).distinct()
    return list(Client.objects.filter(pk__in=ids).order_by("name"))
