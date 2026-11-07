"""
Mirror LinkedEye app-onboarded users into Keycloak (Keycloak = SSO IdP; MySQL =
the fallback login store).

The admin onboards a user in the portal (useronboard) — that creates the Django
``auth_user`` row with a password. This module pushes the *same* user (same
password, mapped realm role, site/subsite attributes) into Keycloak via the
Keycloak Admin REST API, so:

  * normal login goes through Keycloak (SSO), and
  * if Keycloak is down, the user logs in against MySQL with the SAME password.

Design points:
  * Best-effort and non-fatal: every public function returns ``(ok, detail)`` and
    never raises into the caller. MySQL stays the source of truth; a Keycloak
    outage must not break app onboarding.
  * Auth to the Admin API is a dedicated confidential service-account client
    (KEYCLOAK_ADMIN_CLIENT_ID/SECRET) with realm-management manage-users. The
    admin token is cached in the shared Django cache (Redis).
  * Inert unless configured (KEYCLOAK_ADMIN_SYNC_ENABLED and the client secret).
"""

import json
import logging

import requests
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger('linkedeye')

_TOKEN_CACHE_KEY = 'keycloak:admin_token'
_REFRESH_MARGIN = 30
_MIN_TTL = 30
_TIMEOUT = 10


# ── configuration ──────────────────────────────────────────────────────────

def _server():
    return (getattr(settings, 'KEYCLOAK_SERVER_URL', '') or '').rstrip('/')


def _realm():
    return getattr(settings, 'KEYCLOAK_REALM', '') or ''


def _client_id():
    return getattr(settings, 'KEYCLOAK_ADMIN_CLIENT_ID', '') or ''


def _client_secret():
    return getattr(settings, 'KEYCLOAK_ADMIN_CLIENT_SECRET', '') or ''


def _verify():
    return getattr(settings, 'KEYCLOAK_VERIFY_SSL', True)


def is_configured():
    return bool(_server() and _realm() and _client_id() and _client_secret())


def sync_enabled():
    """True when admin-side user mirroring should run."""
    return bool(getattr(settings, 'KEYCLOAK_ADMIN_SYNC_ENABLED', False)) and is_configured()


# ── admin token ────────────────────────────────────────────────────────────

def _get_admin_token(force_refresh=False):
    if not is_configured():
        return None
    if not force_refresh:
        try:
            cached = cache.get(_TOKEN_CACHE_KEY)
        except Exception:
            cached = None
        if cached:
            return cached

    url = f'{_server()}/realms/{_realm()}/protocol/openid-connect/token'
    data = {
        'grant_type': 'client_credentials',
        'client_id': _client_id(),
        'client_secret': _client_secret(),
    }
    try:
        resp = requests.post(url, data=data, timeout=_TIMEOUT, verify=_verify())
    except requests.exceptions.RequestException as e:
        logger.error('keycloak admin token fetch failed (network): %s', e)
        return None
    if not resp.ok:
        logger.error('keycloak admin token fetch failed: HTTP %s %s', resp.status_code, resp.text[:300])
        return None
    try:
        payload = resp.json()
    except ValueError:
        return None
    token = payload.get('access_token')
    if not token:
        return None
    try:
        ttl = max(int(payload.get('expires_in', 300)) - _REFRESH_MARGIN, _MIN_TTL)
        cache.set(_TOKEN_CACHE_KEY, token, ttl)
    except Exception:
        pass
    return token


def _headers(token):
    return {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}


def _admin_url(path):
    return f'{_server()}/admin/realms/{_realm()}{path}'


def _request(method, path, token, **kwargs):
    """One Admin API call with a single 401 refresh-and-retry."""
    kwargs.setdefault('timeout', _TIMEOUT)
    kwargs.setdefault('verify', _verify())
    resp = requests.request(method, _admin_url(path), headers=_headers(token), **kwargs)
    if resp.status_code == 401:
        token = _get_admin_token(force_refresh=True)
        if token:
            resp = requests.request(method, _admin_url(path), headers=_headers(token), **kwargs)
    return resp


# ── helpers ────────────────────────────────────────────────────────────────

def _find_user_id(token, email):
    resp = _request('GET', f'/users?email={requests.utils.quote(email)}&exact=true', token)
    if resp.ok:
        for u in resp.json():
            if (u.get('email') or '').lower() == email.lower() or (u.get('username') or '').lower() == email.lower():
                return u.get('id')
    return None


def _build_attributes(site_ids, subsites):
    attrs = {}
    if site_ids:
        attrs['linkedeye_site_ids'] = [str(s) for s in site_ids]
    if subsites:
        attrs['linkedeye_subsites'] = [json.dumps(subsites)]
    return attrs


def _assign_only_realm_role(token, user_id, role_name):
    """Make ``role_name`` the user's single LinkedEye realm role."""
    if not role_name:
        return
    role_resp = _request('GET', f'/roles/{requests.utils.quote(role_name)}', token)
    if not role_resp.ok:
        logger.warning('keycloak realm role %r not found (create it in the realm)', role_name)
        return
    role = role_resp.json()
    role_rep = [{'id': role['id'], 'name': role['name']}]

    # Remove any known LinkedEye roles the user shouldn't have, then add the target.
    current = _request('GET', f'/users/{user_id}/role-mappings/realm', token)
    if current.ok:
        known = set(getattr(settings, 'KEYCLOAK_KNOWN_ROLES', []) or [])
        to_remove = [
            {'id': r['id'], 'name': r['name']}
            for r in current.json()
            if r.get('name') in known and r.get('name') != role_name
        ]
        if to_remove:
            _request('DELETE', f'/users/{user_id}/role-mappings/realm', token, data=json.dumps(to_remove))
    _request('POST', f'/users/{user_id}/role-mappings/realm', token, data=json.dumps(role_rep))


# ── public sync operations (best-effort, never raise) ───────────────────────

def sync_create_user(email, first_name, last_name, password, role, site_ids=None, subsites=None):
    """Create (or update-in-place) the user in Keycloak with the same password."""
    if not sync_enabled():
        return False, 'sync disabled'
    try:
        token = _get_admin_token()
        if not token:
            return False, 'no admin token'

        existing_id = _find_user_id(token, email)
        body = {
            'username': email,
            'email': email,
            'firstName': first_name or '',
            'lastName': last_name or '',
            'enabled': True,
            'emailVerified': True,
            'attributes': _build_attributes(site_ids, subsites),
        }
        if existing_id:
            user_id = existing_id
            _request('PUT', f'/users/{user_id}', token, data=json.dumps(body))
        else:
            body['credentials'] = [{'type': 'password', 'value': password, 'temporary': False}]
            resp = _request('POST', '/users', token, data=json.dumps(body))
            if resp.status_code not in (201, 204):
                return False, f'create failed HTTP {resp.status_code}: {resp.text[:200]}'
            user_id = _find_user_id(token, email)
            if not user_id:
                return False, 'created but could not resolve user id'

        # Set/reset the password to match MySQL (also covers the update-in-place path).
        if password:
            _request('PUT', f'/users/{user_id}/reset-password', token,
                     data=json.dumps({'type': 'password', 'value': password, 'temporary': False}))
        _assign_only_realm_role(token, user_id, role)
        return True, 'ok'
    except Exception as e:
        logger.error('keycloak sync_create_user failed for %s: %s', email, e)
        return False, str(e)


def sync_update_user(email, first_name, role, site_ids=None, subsites=None):
    """Update name / role / site attributes for an existing Keycloak user."""
    if not sync_enabled():
        return False, 'sync disabled'
    try:
        token = _get_admin_token()
        if not token:
            return False, 'no admin token'
        user_id = _find_user_id(token, email)
        if not user_id:
            # Not present yet — nothing to update (creation happens on register).
            return False, 'user not found in keycloak'
        body = {
            'firstName': first_name or '',
            'attributes': _build_attributes(site_ids, subsites),
        }
        _request('PUT', f'/users/{user_id}', token, data=json.dumps(body))
        _assign_only_realm_role(token, user_id, role)
        return True, 'ok'
    except Exception as e:
        logger.error('keycloak sync_update_user failed for %s: %s', email, e)
        return False, str(e)


def sync_delete_user(email):
    """Delete (or disable, per KEYCLOAK_DELETE_MODE) the user in Keycloak."""
    if not sync_enabled():
        return False, 'sync disabled'
    try:
        token = _get_admin_token()
        if not token:
            return False, 'no admin token'
        user_id = _find_user_id(token, email)
        if not user_id:
            return True, 'already absent'
        mode = getattr(settings, 'KEYCLOAK_DELETE_MODE', 'delete')
        if mode == 'disable':
            _request('PUT', f'/users/{user_id}', token, data=json.dumps({'enabled': False}))
        else:
            _request('DELETE', f'/users/{user_id}', token)
        return True, 'ok'
    except Exception as e:
        logger.error('keycloak sync_delete_user failed for %s: %s', email, e)
        return False, str(e)


def sync_set_enabled(email, enabled):
    """Enable/disable the user in Keycloak to mirror is_active."""
    if not sync_enabled():
        return False, 'sync disabled'
    try:
        token = _get_admin_token()
        if not token:
            return False, 'no admin token'
        user_id = _find_user_id(token, email)
        if not user_id:
            return False, 'user not found in keycloak'
        _request('PUT', f'/users/{user_id}', token, data=json.dumps({'enabled': bool(enabled)}))
        return True, 'ok'
    except Exception as e:
        logger.error('keycloak sync_set_enabled failed for %s: %s', email, e)
        return False, str(e)


def keycloak_reachable():
    """Lightweight health check for the login page fallback. Cached briefly."""
    server, realm = _server(), _realm()
    if not (server and realm):
        return False
    ck = 'keycloak:reachable'
    try:
        cached = cache.get(ck)
        if cached is not None:
            return cached
    except Exception:
        pass
    ok = False
    try:
        resp = requests.get(
            f'{server}/realms/{realm}/.well-known/openid-configuration',
            timeout=2, verify=_verify(),
        )
        ok = resp.ok
    except requests.exceptions.RequestException:
        ok = False
    try:
        cache.set(ck, ok, 15)  # short TTL so recovery is picked up quickly
    except Exception:
        pass
    return ok
