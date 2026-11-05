"""
Bootstrap a Docker (or any) Vault instance for LinkedEye.

Steps:
  1. Init Vault (1-of-1) if not initialized
  2. Unseal
  3. Enable KV v1 at mount ``secret/`` (device + app secrets)
  4. Create Django read-only AppRole
  5. Seed app secrets from current environment variables

Usage:
  docker compose -f docker-compose.vault.yml up -d
  set VAULT_HOST=127.0.0.1
  set VAULT_PORT=8200
  python manage.py vault_bootstrap
"""

import json
import os
from pathlib import Path

import requests
from django.core.management.base import BaseCommand

from lib.LinkedEyeVault.AppSecrets import (
    APP_SECRET_KEYS,
    clear_secrets_cache,
    secrets_mount_path,
    vault_url,
    write_vault_bundle,
)

KEYS_FILE = Path(__file__).resolve().parents[3] / "docker" / "vault-keys.json"
DJANGO_POLICY_NAME = "linkedeye_django_app"
DJANGO_ROLE_NAME = "linkedeye_django_role"


def _django_policy(mount_path: str) -> str:
    path = mount_path.strip("/")
    if int(os.getenv("VAULT_KV_VERSION", "1")) == 2:
        mount, _, subpath = path.partition("/")
        read_path = f"{mount}/data/{subpath}" if subpath else f"{mount}/data"
    else:
        read_path = path
    return f'''
path "auth/token/lookup-self" {{
  capabilities = ["read"]
}}
path "sys/health" {{
  capabilities = ["read"]
}}
path "{read_path}" {{
  capabilities = ["read"]
}}
'''.strip()


class Command(BaseCommand):
    help = "Initialize, unseal, and configure Vault for LinkedEye (Docker-friendly)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--skip-seed",
            action="store_true",
            help="Skip seeding app secrets from environment",
        )
        parser.add_argument(
            "--keys-file",
            default=str(KEYS_FILE),
            help="Where to store init keys (default: docker/vault-keys.json)",
        )

    def handle(self, *args, **options):
        keys_file = Path(options["keys_file"])
        base = vault_url()
        self.stdout.write(f"Vault URL: {base}")

        seal_status = self._get_seal_status(base)
        if seal_status is None:
            self.stderr.write(self.style.ERROR("Vault is not reachable. Start Docker first."))
            return

        keys = self._load_keys(keys_file)
        if not seal_status.get("initialized"):
            keys = self._init_vault(base, keys_file)
            if not keys:
                return
            seal_status = self._get_seal_status(base)
        elif not keys:
            # Dev mode or external init — use VAULT_TOKEN env
            env_token = os.getenv("VAULT_TOKEN", "")
            if not env_token:
                self.stderr.write(
                    self.style.ERROR("Vault is initialized but no keys file/token found. Set VAULT_TOKEN to continue.")
                )
                return
            keys = {"root_token": env_token}

        token = keys.get("root_token")
        if seal_status.get("sealed"):
            if not self._unseal(base, keys):
                return
            seal_status = self._get_seal_status(base)

        if seal_status.get("sealed"):
            self.stderr.write(self.style.ERROR("Vault is still sealed."))
            return

        self.stdout.write(self.style.SUCCESS("Vault is initialized and unsealed."))

        if not self._ensure_kv_engine(base, token):
            return

        role_id, secret_id = self._ensure_django_approle(base, token)
        if not role_id:
            return

        self._write_env_snippet(role_id, secret_id, token)

        if not options["skip_seed"]:
            bundle = {k: os.getenv(k, "") for k in APP_SECRET_KEYS if os.getenv(k, "")}
            if bundle:
                if write_vault_bundle(bundle, token=token):
                    clear_secrets_cache()
                    self.stdout.write(self.style.SUCCESS(f"Seeded {len(bundle)} app secret(s) into Vault."))
                else:
                    self.stderr.write(self.style.WARNING("Could not seed app secrets (check token/policy)."))
            else:
                self.stdout.write(self.style.WARNING(
                    "No env secrets to seed. Set MYSQL_DB_PASS, SECRET_KEY, etc. then re-run, "
                    "or: python manage.py vault_seed_app_secrets"
                ))

        self.stdout.write(self.style.SUCCESS("Vault bootstrap complete."))
        self.stdout.write("Enable in Django: VAULT_APP_SECRETS_ENABLED=true")

    def _get_seal_status(self, base):
        try:
            resp = requests.get(f"{base}/v1/sys/seal-status", timeout=5)
            if resp.status_code == 200:
                return resp.json()
        except requests.RequestException as exc:
            self.stderr.write(self.style.ERROR(f"Vault connection failed: {exc}"))
        return None

    def _load_keys(self, keys_file):
        if keys_file.exists() and keys_file.stat().st_size > 0:
            with open(keys_file, encoding="utf-8") as handle:
                return json.load(handle)
        return {}

    def _init_vault(self, base, keys_file):
        self.stdout.write("Initializing Vault (1 share / 1 threshold)...")
        resp = requests.put(
            f"{base}/v1/sys/init",
            json={"secret_shares": 1, "secret_threshold": 1},
            timeout=15,
        )
        if resp.status_code != 200:
            self.stderr.write(self.style.ERROR(f"Init failed: {resp.text}"))
            return None

        keys = resp.json()
        keys_file.parent.mkdir(parents=True, exist_ok=True)
        with open(keys_file, "w", encoding="utf-8") as handle:
            json.dump(keys, handle, indent=2)
        self.stdout.write(self.style.SUCCESS(f"Keys saved to {keys_file}"))
        self.stdout.write(self.style.WARNING("Do NOT commit vault-keys.json to git."))
        return keys

    def _unseal(self, base, keys):
        unseal_key = keys.get("keys_base64", [None])[-1] or keys.get("keys", [None])[-1]
        if not unseal_key:
            self.stderr.write(self.style.ERROR("No unseal key in keys file."))
            return False
        resp = requests.put(
            f"{base}/v1/sys/unseal",
            json={"key": unseal_key},
            timeout=10,
        )
        if resp.status_code != 200:
            self.stderr.write(self.style.ERROR(f"Unseal failed: {resp.text}"))
            return False
        self.stdout.write(self.style.SUCCESS("Vault unsealed."))
        return True

    def _ensure_kv_engine(self, base, token):
        headers = {"X-Vault-Token": token}
        mounts = requests.get(f"{base}/v1/sys/mounts", headers=headers, timeout=10)
        if mounts.status_code == 200 and "secret/" in mounts.json():
            self.stdout.write("KV engine already mounted at secret/")
            return True

        kv_version = os.getenv("VAULT_KV_VERSION", "2")
        options = {"version": str(kv_version)}
        resp = requests.post(
            f"{base}/v1/sys/mounts/secret",
            headers=headers,
            json={"type": "kv", "options": options},
            timeout=10,
        )
        if resp.status_code not in (200, 204):
            self.stderr.write(self.style.ERROR(f"Enable KV failed: {resp.text}"))
            return False
        self.stdout.write(self.style.SUCCESS(f"KV v{kv_version} enabled at secret/"))
        return True

    def _ensure_django_approle(self, base, token):
        headers = {"X-Vault-Token": token}
        mount = secrets_mount_path()
        policy = _django_policy(mount)

        requests.put(
            f"{base}/v1/sys/policies/acl/{DJANGO_POLICY_NAME}",
            headers=headers,
            json={"policy": policy},
            timeout=15,
        )
        resp = requests.post(
            f"{base}/v1/sys/auth/approle",
            headers=headers,
            json={"type": "approle"},
            timeout=10,
        )
        if resp.status_code not in (200, 204, 400):
            self.stderr.write(self.style.WARNING(f"AppRole enable: {resp.status_code} {resp.text}"))
        requests.post(
            f"{base}/v1/auth/approle/role/{DJANGO_ROLE_NAME}",
            headers=headers,
            json={
                "token_policies": [DJANGO_POLICY_NAME],
                "token_ttl": "1h",
                "token_max_ttl": "4h",
            },
            timeout=15,
        )

        role_resp = requests.get(
            f"{base}/v1/auth/approle/role/{DJANGO_ROLE_NAME}/role-id",
            headers=headers,
            timeout=10,
        )
        secret_resp = requests.post(
            f"{base}/v1/auth/approle/role/{DJANGO_ROLE_NAME}/secret-id",
            headers=headers,
            timeout=10,
        )
        if role_resp.status_code != 200 or secret_resp.status_code != 200:
            self.stderr.write(self.style.ERROR("Failed to create Django AppRole."))
            return None, None

        role_id = role_resp.json()["data"]["role_id"]
        secret_id = secret_resp.json()["data"]["secret_id"]
        self.stdout.write(self.style.SUCCESS("Django AppRole ready."))
        return role_id, secret_id

    def _write_env_snippet(self, role_id, secret_id, root_token):
        snippet = f"""
# Add to your shell before runserver / docker deploy:
$env:VAULT_HOST="127.0.0.1"
$env:VAULT_PORT="18200"
$env:VAULT_KV_VERSION="2"
$env:VAULT_APP_SECRETS_ENABLED="true"
# Use one auth method from your secure source (do not commit real values):
# $env:VAULT_TOKEN="<dev-bootstrap-token>"
$env:VAULT_APP_ROLE_ID="{role_id}"
$env:VAULT_APP_SECRET_ID="{secret_id}"
# Bootstrap token example (do not keep in files):
# $env:VAULT_TOKEN="{root_token}"
"""
        snippet_path = Path(__file__).resolve().parents[3] / "docker" / "vault-django.env.ps1"
        snippet_path.write_text(snippet.strip() + "\n", encoding="utf-8")
        self.stdout.write(snippet)
        self.stdout.write(self.style.SUCCESS(f"Env snippet saved: {snippet_path}"))
