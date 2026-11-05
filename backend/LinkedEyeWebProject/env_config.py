"""
Load LinkedEye configuration from environment variables and optional `.env` file.

Copy `.env.example` (or `config/local.env.example` for NodePort dev clusters) to
`.env` in the project root (directory containing `manage.py`).
"""

from __future__ import annotations

import os
from typing import Optional, Tuple


def load_env_file(base_dir: str, filename: str = ".env") -> None:
    """Populate os.environ from a dotenv-style file (does not override existing vars)."""
    path = os.path.join(base_dir, filename)
    if not os.path.isfile(path):
        return

    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            os.environ.setdefault(key, value)


def _truthy(value: Optional[str]) -> bool:
    return (value or "").lower() in ("1", "true", "yes", "on")


def _vault_enabled() -> bool:
    return _truthy(os.getenv("VAULT_APP_SECRETS_ENABLED", "false"))


def _vault_lookup(key: str) -> Optional[str]:
    # Vault connection variables must come from the process env itself.
    if key.startswith("VAULT_"):
        return None

    try:
        from lib.LinkedEyeVault.AppSecrets import _fetch_vault_bundle

        value = _fetch_vault_bundle().get(key)
        if value in (None, ""):
            return None
        return str(value)
    except Exception:
        # Keep env loader resilient; callers will fall back to os.environ/default.
        return None


def env(key: str, default: str = "") -> str:
    # Prefer direct process env first so runtime overrides still work.
    value = os.getenv(key)
    if value not in (None, ""):
        return value

    if _vault_enabled():
        vault_value = _vault_lookup(key)
        if vault_value not in (None, ""):
            return vault_value

    return default


def env_int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def service_url(scheme: str, host: str, port: str) -> str:
    return f"{scheme}://{host}:{port}"


def host_port_from_url(url: str, default_port: int = 9200) -> Tuple[str, int]:
    """Parse ``host:port`` or ``http://host:port`` into components."""
    raw = (url or "").strip()
    if "://" in raw:
        raw = raw.split("://", 1)[1]
    if "/" in raw:
        raw = raw.split("/", 1)[0]
    if ":" in raw:
        host, port_str = raw.rsplit(":", 1)
        return host, int(port_str)
    return raw, default_port
