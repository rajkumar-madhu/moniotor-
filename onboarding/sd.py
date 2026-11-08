"""Prometheus service discovery for onboarded targets.

`GET /api/sd/targets.json` returns the http_sd format

    [{"targets": ["db-01:9100"],
      "labels": {"job": "node", "site": "Mumbai", "client": "NSE", "env": "prod",
                 "le_target": "12", "le_hostname": "db-01"}}, ...]

for every enabled onboarded target of the tenant, authenticated with a bearer token that lives in
the credential vault. The token is rotatable and every fetch is recorded (when, from where, how
many targets, and the outcome).

Security notes:

* the token is stored encrypted like any other credential and is **never read back for display**.
  It is shown exactly once, when it is generated or rotated, so the operator can paste the
  scrape_config into Prometheus. Afterwards the page shows a placeholder and a Rotate button.
* a bad, missing or foreign-tenant token gets a 401 with a fixed body: no detail, no timing
  shortcut (every candidate is compared with `hmac.compare_digest`), and never a 500.
* fetches are rate-limited per source address, so a misconfigured Prometheus cannot hammer the
  console.
"""
import hmac
import logging

from django.conf import settings
from django.core.cache import cache
from django.urls import reverse

from core import secrets
from core.audit import audit

from .models import JOB_LABELS, Credential, CredentialKind, SDFetch
from .services import enabled_sd_targets, tenant

log = logging.getLogger("linkedeye.onboarding.sd")

#: the vault row that holds the service-discovery bearer token (one per tenant)
TOKEN_NAME = "Prometheus service discovery"
JOB_NAME = "linkedeye-sd"
RATE_LIMIT = 60          # fetches ...
RATE_WINDOW = 60         # ... per this many seconds, per source address
REFRESH_INTERVAL = "60s"


# ----------------------------------------------------------------------------- the token
def token_row(tenant_key=None):
    """The tenant's service-discovery credential, or None."""
    return (Credential.objects
            .filter(tenant_key=tenant_key or tenant(), kind=CredentialKind.BEARER, name=TOKEN_NAME)
            .order_by("pk").first())


def configured():
    return token_row() is not None


def issue_token(user, request=None):
    """Create or rotate the token. Returns (credential, plaintext) - the only time the plaintext
    exists; it is never read back afterwards."""
    plaintext = secrets.generate_token()
    row = token_row()
    row = secrets.store(CredentialKind.BEARER, "prometheus", plaintext, "", user, name=TOKEN_NAME,
                        notes="Bearer token for GET /api/sd/targets.json", request=request, credential=row)
    audit(request, "onboarding.sd.token_rotate" if row.rotated_at else "onboarding.sd.token_issue", row)
    return row, plaintext


def authenticate(request):
    """The credential the request authenticated as, or None. Constant-time, tenant-scoped."""
    auth = request.headers.get("Authorization", "")
    scheme, _, given = auth.partition(" ")
    given = given.strip()
    if scheme.lower() != "bearer" or not given:
        return None
    row = token_row()
    if row is None:
        return None
    try:
        expected = secrets.decrypt(row.secret)
    except (secrets.VaultUnavailable, secrets.VaultError):
        raise
    if hmac.compare_digest(given.encode(), expected.encode()):
        return row
    return None


# ----------------------------------------------------------------------------- the payload
def labels_for(target):
    run = target.run
    return {
        "job": JOB_LABELS.get(target.target_type, "node"),
        "site": run.site or "",
        "client": (run.client.name if run.client_id else ""),
        "env": run.environment or "",
        "le_target": str(target.pk),
        "le_hostname": target.hostname,
    }


def targets_payload(tenant_key=None):
    """The http_sd document: one entry per enabled, scrape-ready target."""
    out = []
    for target in enabled_sd_targets(tenant_key):
        instance = target.instance
        if not instance:
            continue
        out.append({"targets": [instance], "labels": labels_for(target)})
    return out


# ----------------------------------------------------------------------------- bookkeeping
def record_fetch(request, count, result="ok"):
    from core.audit import _ip
    return SDFetch.objects.create(
        tenant_key=tenant(), source_ip=_ip(request), target_count=count, result=result,
        user_agent=(request.headers.get("User-Agent", "") or "")[:200],
    )


def recent_fetches(limit=10):
    return list(SDFetch.objects.filter(tenant_key=tenant())[:limit])


def rate_limited(request):
    """True when this source address has already had RATE_LIMIT fetches in the window."""
    from core.audit import _ip
    key = f"onboarding:sd:rate:{_ip(request) or 'unknown'}"
    try:
        added = cache.add(key, 1, RATE_WINDOW)
        if added:
            return False
        try:
            count = cache.incr(key)
        except ValueError:          # the entry expired between add() and incr()
            cache.set(key, 1, RATE_WINDOW)
            return False
        return count > RATE_LIMIT
    except Exception:               # a broken cache backend must never block discovery
        log.warning("service-discovery rate limiting unavailable", exc_info=True)
        return False


# ----------------------------------------------------------------------------- the paste block
def sd_url(request=None):
    path = reverse("onboarding:sd_targets")
    if request is not None:
        return request.build_absolute_uri(path)
    base = (getattr(settings, "LINKEDEYE_PORTAL_URL", "") or "").rstrip("/")
    if base:
        # LINKEDEYE_PORTAL_URL may point at the login page; keep only scheme://host
        parts = base.split("/")
        if len(parts) >= 3:
            base = "/".join(parts[:3])
    return f"{base}{path}" if base else path


def scrape_config(request=None, token=None):
    """The exact scrape_config block for the operator to paste once into prometheus.yml.

    `token` is passed only right after the token was generated or rotated; otherwise the block
    carries a placeholder, because a stored secret is never rendered.
    """
    creds = token or "<paste the token shown when you generated it>"
    return (
        "scrape_configs:\n"
        f"  - job_name: {JOB_NAME}\n"
        "    http_sd_configs:\n"
        f"      - url: {sd_url(request)}\n"
        f"        refresh_interval: {REFRESH_INTERVAL}\n"
        "        authorization:\n"
        "          type: Bearer\n"
        f"          credentials: {creds}\n"
    )
