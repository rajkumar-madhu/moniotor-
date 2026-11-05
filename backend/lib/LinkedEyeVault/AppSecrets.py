"""
Production-grade application secrets resolver for LinkedEye.

Loads infrastructure credentials from HashiCorp Vault (KV v1 bundle) with
environment-variable fallback for local development.

Enable Vault for app secrets:
    VAULT_APP_SECRETS_ENABLED=true
    VAULT_APP_ROLE_ID=...
    VAULT_APP_SECRET_ID=...

Secrets are stored as a single KV v1 document at:
    VAULT_APP_SECRETS_PATH=secret/linkedeye/app   (default)

Seed once from env:
    python manage.py vault_seed_app_secrets
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger(__name__)

_CACHE: Dict[str, Dict[str, Any]] = {}
_TOKEN: Optional[str] = None
_TOKEN_EXPIRY: float = 0


def _truthy(value: Optional[str]) -> bool:
    return (value or "").lower() in ("1", "true", "yes", "on")


def vault_app_secrets_enabled() -> bool:
    return _truthy(os.getenv("VAULT_APP_SECRETS_ENABLED", "false"))


def _cache_ttl() -> int:
    return int(os.getenv("VAULT_SECRETS_CACHE_TTL", "300"))


def vault_url() -> str:
    scheme = os.getenv("VAULT_SCHEME", "http")
    host = os.getenv("VAULT_HOST", "vault.fs-linkedeye")
    port = os.getenv("VAULT_PORT", "8200")
    return f"{scheme}://{host}:{port}"


def secrets_mount_path() -> str:
    return os.getenv("VAULT_APP_SECRETS_PATH", "secret/linkedeye/app").strip("/")


def clear_secrets_cache() -> None:
    global _TOKEN, _TOKEN_EXPIRY
    _CACHE.clear()
    _TOKEN = None
    _TOKEN_EXPIRY = 0


def _get_vault_token() -> Optional[str]:
    global _TOKEN, _TOKEN_EXPIRY

    if _TOKEN and time.time() < _TOKEN_EXPIRY - 60:
        return _TOKEN

    static_token = os.getenv("VAULT_TOKEN")
    if static_token:
        _TOKEN = static_token
        _TOKEN_EXPIRY = time.time() + 3600
        return _TOKEN

    role_id = os.getenv("VAULT_APP_ROLE_ID")
    secret_id = os.getenv("VAULT_APP_SECRET_ID")
    if role_id and secret_id:
        response = requests.post(
            f"{vault_url()}/v1/auth/approle/login",
            json={"role_id": role_id, "secret_id": secret_id},
            timeout=10,
        )
        if response.status_code != 200:
            logger.error("Vault AppRole login failed: %s", response.text)
            return None
        auth = response.json().get("auth", {})
        _TOKEN = auth.get("client_token")
        _TOKEN_EXPIRY = time.time() + auth.get("lease_duration", 3600)
        return _TOKEN

    keyfile = os.getenv("VAULT_KEYS_FILE", "/vault/keys.json")
    try:
        if os.path.exists(keyfile) and os.path.getsize(keyfile) > 0:
            with open(keyfile, encoding="utf-8") as handle:
                keys = json.load(handle)
            _TOKEN = keys.get("root_token")
            _TOKEN_EXPIRY = time.time() + 3600
            return _TOKEN
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read Vault keys file %s: %s", keyfile, exc)

    return None


def _kv_version() -> int:
    return int(os.getenv("VAULT_KV_VERSION", "1"))


def _vault_api_paths(logical_path: str) -> tuple[str, str]:
    """Return (read_url_suffix, write_body) paths for KV v1 or v2."""
    logical_path = logical_path.strip("/")
    if _kv_version() == 2:
        mount, _, subpath = logical_path.partition("/")
        api_path = f"{mount}/data/{subpath}" if subpath else f"{mount}/data"
        return api_path, api_path
    return logical_path, logical_path


def _parse_bundle_payload(response_json: dict) -> Dict[str, str]:
    data = response_json.get("data", {})
    if _kv_version() == 2:
        inner = data.get("data", {})
        return inner if isinstance(inner, dict) else {}
    return data if isinstance(data, dict) else {}


def _fetch_vault_bundle() -> Dict[str, str]:
    cache_key = "__bundle__"
    cached = _CACHE.get(cache_key)
    if cached and time.time() < cached["expires"]:
        return cached["data"]

    token = _get_vault_token()
    if not token:
        return {}

    read_path, _ = _vault_api_paths(secrets_mount_path())
    response = requests.get(
        f"{vault_url()}/v1/{read_path}",
        headers={"X-Vault-Token": token},
        timeout=10,
    )
    if response.status_code != 200:
        logger.warning("Vault read %s failed (%s)", read_path, response.status_code)
        return {}

    data = _parse_bundle_payload(response.json())
    _CACHE[cache_key] = {"data": data, "expires": time.time() + _cache_ttl()}
    return data


def write_vault_bundle(secrets: Dict[str, str], token: Optional[str] = None) -> bool:
    """Write the full app-secrets bundle (bootstrap / seed command)."""
    auth_token = token or _get_vault_token()
    if not auth_token:
        logger.error("Cannot write Vault bundle: no token available")
        return False

    _, write_path = _vault_api_paths(secrets_mount_path())
    body = {"data": secrets} if _kv_version() == 2 else secrets
    response = requests.post(
        f"{vault_url()}/v1/{write_path}",
        headers={"X-Vault-Token": auth_token},
        json=body,
        timeout=15,
    )
    if response.status_code not in (200, 204):
        logger.error("Vault write %s failed (%s): %s", write_path, response.status_code, response.text)
        return False

    clear_secrets_cache()
    return True


def get_app_secret(
    name: str,
    env_var: Optional[str] = None,
    default: Optional[str] = None,
    required: bool = False,
) -> Optional[str]:
    """
    Resolve a secret value.

    Order when VAULT_APP_SECRETS_ENABLED=true:
      1. Vault bundle key
      2. Environment variable
      3. default

    When disabled:
      1. Environment variable
      2. default
    """
    env_name = env_var or name
    env_value = os.getenv(env_name)

    if vault_app_secrets_enabled():
        vault_value = _fetch_vault_bundle().get(name)
        if vault_value not in (None, ""):
            value = vault_value
        else:
            value = env_value if env_value not in (None, "") else default
    else:
        value = env_value if env_value not in (None, "") else default

    if required and not value:
        raise RuntimeError(f"Required secret '{name}' is not configured")

    return value


# Canonical keys stored in Vault bundle `secret/linkedeye/app`
APP_SECRET_KEYS = (
    "SECRET_KEY",
    "MYSQL_DB_PASS",
    "MYSQL_ROOT_PASSWORD",
    "POSTGRES_PASS",
    "NEO4J_PASS",
    "REDIS_PASSWORD",
    "REDIS_USER",
    "ELASTIC_PASS",
    "LINKEDEYE_EMAIL_APPKEY",
    "GOOGLE_SECRET",
    "TOTP_MASTER_KEY",
    "ADMIN_DEFAULT_PASSWORD",
    "AZURE_CLIENT_SECRET",
    "KEYCLOAK_CLIENT_SECRET",
    "KEYCLOAK_ADMIN_CLIENT_SECRET",
    "KEYCLOAK_GRAFANA_CLIENT_SECRET",
    "KEYCLOAK_PROMETHEUS_CLIENT_SECRET",
    "KEYCLOAK_NEO4J_CLIENT_SECRET",
    "KEYCLOAK_ELASTIC_CLIENT_SECRET",
    "SMTP_PASSWORD",
    "PROMETHEUS_PASSWORD",
    "GRAFANA_PASSWORD",
    "SUPERSET_PASSWORD",
    "LE_SERVICE_TOKEN",
)
