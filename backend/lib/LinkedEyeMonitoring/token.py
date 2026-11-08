"""
Server-to-server access tokens for Keycloak-protected monitoring services.

Grafana, Prometheus, Neo4j, and Elasticsearch (when their REST host is fronted
by a public oauth2-proxy ingress) each trust their own Keycloak client
(audience). Django authenticates to them machine-to-machine using the OAuth2
*client-credentials* grant — a separate client per service — and the caller
sends the resulting token upstream as ``Authorization: Bearer <token>``.

Requirements on the infrastructure side:
  * A confidential Keycloak client per service with "Service Accounts" enabled
    (KEYCLOAK_GRAFANA_CLIENT_ID/SECRET, KEYCLOAK_PROMETHEUS_CLIENT_ID/SECRET,
    KEYCLOAK_NEO4J_CLIENT_ID/SECRET, KEYCLOAK_ELASTIC_CLIENT_ID/SECRET).
  * Each service's oauth2-proxy must accept bearer tokens
    (``--skip-jwt-bearer-tokens=true`` + an ``--extra-jwt-issuers`` entry whose
    audience matches that client's token), otherwise it redirects to a login page.

Each service's token is cached in the shared Django cache (Redis) so every
Gunicorn worker reuses it and Keycloak is called at most once per token lifetime.
"""

import json
import logging

import requests
from requests.auth import HTTPBasicAuth
from django.conf import settings
from django.core.cache import cache

from lib.LinkedEyeVault.AppSecrets import get_app_secret

logger = logging.getLogger('linkedeye')

_CACHE_KEY_PREFIX = 'monitoring:kc_access_token:'
# Refresh this many seconds before the token actually expires, so an in-flight
# request never races a hard expiry.
_REFRESH_MARGIN_SECONDS = 30
# Floor for the cache TTL, guards against a broker returning a tiny expires_in.
_MIN_TTL_SECONDS = 30


def _service_config(service):
    """Return ``(token_url, client_id, client_secret)`` for a monitoring service."""
    token_url = getattr(settings, 'KEYCLOAK_MONITORING_TOKEN_URL', '') or ''
    if service == 'grafana':
        client_id = getattr(settings, 'KEYCLOAK_GRAFANA_CLIENT_ID', '') or ''
        client_secret = getattr(settings, 'KEYCLOAK_GRAFANA_CLIENT_SECRET', '') or ''
    elif service == 'neo4j':
        client_id = getattr(settings, 'KEYCLOAK_NEO4J_CLIENT_ID', '') or ''
        client_secret = getattr(settings, 'KEYCLOAK_NEO4J_CLIENT_SECRET', '') or ''
    elif service == 'elastic':
        client_id = getattr(settings, 'KEYCLOAK_ELASTIC_CLIENT_ID', '') or ''
        client_secret = getattr(settings, 'KEYCLOAK_ELASTIC_CLIENT_SECRET', '') or ''
    else:
        client_id = getattr(settings, 'KEYCLOAK_PROMETHEUS_CLIENT_ID', '') or ''
        client_secret = getattr(settings, 'KEYCLOAK_PROMETHEUS_CLIENT_SECRET', '') or ''
    return token_url, client_id, client_secret


def is_configured(service):
    """True when the client-credentials client for ``service`` is fully configured."""
    token_url, client_id, client_secret = _service_config(service)
    return bool(token_url and client_id and client_secret)


def _fetch_client_credentials_token(token_url, client_id, client_secret, cache_key, force_refresh=False, log_label=''):
    """Fetch and cache a Keycloak client-credentials access token."""
    if not (token_url and client_id and client_secret):
        return None

    if not force_refresh:
        try:
            cached = cache.get(cache_key)
        except Exception as e:
            logger.warning('monitoring token cache read failed for %s: %s', cache_key, e)
            cached = None
        if cached:
            return cached

    scope = getattr(settings, 'KEYCLOAK_MONITORING_SCOPE', '') or ''
    verify = verify_ssl()
    data = {
        'grant_type': 'client_credentials',
        'client_id': client_id,
        'client_secret': client_secret,
    }
    if scope:
        data['scope'] = scope

    try:
        resp = requests.post(token_url, data=data, timeout=10, verify=verify)
    except requests.exceptions.RequestException as e:
        logger.error('monitoring token fetch failed for %s (network): %s', log_label or cache_key, e)
        return None

    if not resp.ok:
        logger.error('monitoring token fetch failed for %s: HTTP %s %s',
                     log_label or cache_key, resp.status_code, resp.text[:300])
        return None

    try:
        payload = resp.json()
    except ValueError:
        logger.error('monitoring token response for %s was not JSON: %s', log_label or cache_key, resp.text[:300])
        return None

    token = payload.get('access_token')
    if not token:
        logger.error('monitoring token response for %s had no access_token', log_label or cache_key)
        return None

    try:
        expires_in = int(payload.get('expires_in', 300))
    except (TypeError, ValueError):
        expires_in = 300
    ttl = max(expires_in - _REFRESH_MARGIN_SECONDS, _MIN_TTL_SECONDS)
    try:
        cache.set(cache_key, token, ttl)
    except Exception as e:
        logger.warning('monitoring token cache write failed for %s: %s', cache_key, e)
    return token


def _normalize_entity_host(entity_host):
    host = (entity_host or '').strip().lower()
    if '://' in host:
        host = host.split('://', 1)[1]
    return host.split(':')[0].split('/')[0]


def normalize_host(entity_host):
    """Public alias of _normalize_entity_host for callers outside this module
    (e.g. analytics.views, which needs to strip a scheme/port/path off a
    per-site elastic_host value before handing it to the Elasticsearch client
    -- that client's ``host`` key expects a bare hostname, not a URL)."""
    return _normalize_entity_host(entity_host)


def _site_map(service):
    """host -> SITE_CODE for ``service``, from the KEYCLOAK_{SERVICE}_SITE_MAP
    setting (JSON object).

    Config-only, so onboarding another site is an env/Vault change, not a code
    deploy: add the host to this map and set
    KEYCLOAK_{SERVICE}_<CODE>_CLIENT_ID/CLIENT_SECRET/TOKEN_URL via get_app_secret.
    """
    setting_name = f'KEYCLOAK_{service.upper()}_SITE_MAP'
    raw = getattr(settings, setting_name, '') or ''
    if not raw:
        return {}
    try:
        site_map = json.loads(raw)
    except (TypeError, ValueError) as e:
        logger.error('%s is not valid JSON: %s', setting_name, e)
        return {}
    if not isinstance(site_map, dict):
        logger.error('%s must be a JSON object of {host: SITE_CODE}', setting_name)
        return {}
    return {str(k).strip().lower(): str(v).strip().upper() for k, v in site_map.items()}


def _site_host_client(service, entity_host):
    """Per-site Keycloak client for ``service``, for multi-site aggregators (e.g. Global LE)."""
    host = _normalize_entity_host(entity_host)
    if not host:
        return None

    code = _site_map(service).get(host)
    if not code:
        return None

    prefix = f'KEYCLOAK_{service.upper()}_{code}'
    client_id = get_app_secret(f'{prefix}_CLIENT_ID', default='') or ''
    client_secret = get_app_secret(f'{prefix}_CLIENT_SECRET', default='') or ''
    token_url = get_app_secret(f'{prefix}_TOKEN_URL', default='') or ''
    if client_id and client_secret and token_url:
        return token_url, client_id, client_secret
    logger.error('%s site %s (%s) is missing CLIENT_ID/CLIENT_SECRET/TOKEN_URL config',
                 service, code, host)
    return None


def get_site_token(service, entity_host='', force_refresh=False):
    """Return a bearer token for ``service`` at ``entity_host``, using per-site
    creds when configured, else falling back to the single shared client."""
    host_cfg = _site_host_client(service, entity_host)
    if host_cfg:
        token_url, client_id, client_secret = host_cfg
        norm_host = _normalize_entity_host(entity_host)
        cache_key = _CACHE_KEY_PREFIX + service + ':' + norm_host
        return _fetch_client_credentials_token(
            token_url, client_id, client_secret, cache_key,
            force_refresh=force_refresh, log_label=service + ':' + norm_host,
        )
    return get_monitoring_token(service, force_refresh=force_refresh)


def get_neo4j_token(entity_host='', force_refresh=False):
    """Return a bearer token for the Neo4j host, using per-site creds when configured."""
    return get_site_token('neo4j', entity_host, force_refresh=force_refresh)


def get_monitoring_token(service, force_refresh=False):
    """Return a valid Keycloak access token for ``service``, or ``None``.

    Returns ``None`` when the client is not configured or the fetch fails, so
    callers can fall back to legacy Basic Auth or surface a clear error rather
    than crash. ``force_refresh=True`` bypasses the cache (use after a 401).
    """
    if not is_configured(service):
        return None

    cache_key = _CACHE_KEY_PREFIX + service
    token_url, client_id, client_secret = _service_config(service)
    return _fetch_client_credentials_token(
        token_url, client_id, client_secret, cache_key,
        force_refresh=force_refresh, log_label=service,
    )


# ── Shared credential helpers used by the Grafana/Prometheus proxy views ──

def bearer_mode():
    return getattr(settings, 'MONITORING_AUTH_MODE', 'basic') == 'bearer'


def verify_ssl():
    """Value for requests' ``verify=``: True, a CA bundle path, or False (explicit opt-out)."""
    if not getattr(settings, 'MONITORING_VERIFY_SSL', True):
        return False
    return getattr(settings, 'MONITORING_CA_BUNDLE', '') or True


def _basic_auth(service):
    if service == 'grafana':
        user = getattr(settings, 'GRAFANA_USERNAME', 'grafana')
        pw = getattr(settings, 'GRAFANA_PASSWORD', '')
    else:
        user = getattr(settings, 'PROMETHEUS_USERNAME', 'prometheus')
        pw = getattr(settings, 'PROMETHEUS_PASSWORD', '')
    return HTTPBasicAuth(user, pw) if pw else None


def monitoring_credentials(service, force_refresh=False, entity_host=''):
    """Return ``(auth, headers)`` for a request to 'grafana' or 'prometheus'.

    Bearer header from that service's Keycloak client-credentials token when in
    bearer mode and a token is available -- using per-site creds when
    ``entity_host`` matches a KEYCLOAK_{SERVICE}_SITE_MAP entry (multi-site
    aggregators, e.g. Global LE), else the single shared client; otherwise
    legacy Basic Auth if a service password is set; otherwise ``(None, {})``
    (the caller should have checked :func:`service_configured` first).
    """
    if bearer_mode():
        token = get_site_token(service, entity_host, force_refresh=force_refresh)
        if token:
            return None, {'Authorization': 'Bearer ' + token}
    return _basic_auth(service), {}


def service_configured(service):
    """True when either bearer auth or legacy Basic Auth is available for ``service``."""
    if bearer_mode() and is_configured(service):
        return True
    if service == 'grafana':
        return bool(getattr(settings, 'GRAFANA_PASSWORD', ''))
    return bool(getattr(settings, 'PROMETHEUS_PASSWORD', ''))


# ── Elasticsearch credentials (separate from monitoring_credentials above) ──
#
# analytics/views.py builds its own elasticsearch-py client rather than calling
# requests directly, so it needs a plain (user, pass) tuple for http_auth, not
# a requests.auth.HTTPBasicAuth instance -- _basic_auth()/monitoring_credentials()
# are shaped for the Grafana/Prometheus proxy views' requests.get(auth=...) calls
# and also don't know ELASTIC_USER/ELASTIC_PASS (only grafana's/prometheus's own
# field names), so Elastic gets its own small helper instead of being forced
# through that path.

def elastic_credentials(entity_host='', force_refresh=False):
    """Return ``(basic_auth_tuple, headers)`` for an Elasticsearch client request.

    Bearer header from a per-site (KEYCLOAK_ELASTIC_SITE_MAP) or shared
    Keycloak client-credentials token when in bearer mode and available;
    otherwise legacy Basic Auth (ELASTIC_USER/ELASTIC_PASS) as a plain
    (user, pass) tuple -- the shape elasticsearch-py's ``http_auth`` param
    expects. ``(None, {})`` when neither is configured.
    """
    if bearer_mode():
        token = get_site_token('elastic', entity_host, force_refresh=force_refresh)
        if token:
            return None, {'Authorization': 'Bearer ' + token}
    user = getattr(settings, 'ELASTIC_USER', '') or ''
    pw = getattr(settings, 'ELASTIC_PASS', '') or ''
    return ((user, pw) if pw else None), {}


def elastic_configured():
    """True when either bearer auth or legacy Basic Auth is available for Elastic."""
    if bearer_mode() and is_configured('elastic'):
        return True
    return bool(getattr(settings, 'ELASTIC_PASS', ''))
