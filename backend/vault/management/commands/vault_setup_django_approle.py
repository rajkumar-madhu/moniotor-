"""
Create a read-only AppRole for the Django application.

Run once after Vault init (requires root/admin token via VAULT_TOKEN or keys file):

    python manage.py vault_setup_django_approle

Outputs role_id and secret_id to configure in Kubernetes secrets:
    VAULT_APP_ROLE_ID
    VAULT_APP_SECRET_ID
"""

import json

import requests
from django.core.management.base import BaseCommand

from lib.LinkedEyeVault.AppSecrets import secrets_mount_path, vault_url
from lib.LinkedEyeVault.Vault import Vault


DJANGO_POLICY_NAME = "linkedeye_django_app"
DJANGO_ROLE_NAME = "linkedeye_django_role"


def _django_policy(mount_path: str) -> str:
  path = mount_path.strip("/")
  return f'''
path "auth/token/lookup-self" {{
  capabilities = ["read"]
}}
path "sys/health" {{
  capabilities = ["read"]
}}
path "{path}" {{
  capabilities = ["read"]
}}
'''.strip()


class Command(BaseCommand):
    help = "Create Vault AppRole with read-only access to Django app secrets"

    def handle(self, *args, **options):
        vault = Vault()
        token = vault.GetRootToken()
        if not token:
            self.stderr.write(self.style.ERROR("No Vault token available."))
            return

        mount = secrets_mount_path()
        headers = {"X-Vault-Token": token}
        base = vault_url()

        policy = _django_policy(mount)
        resp = requests.put(
            f"{base}/v1/sys/policies/acl/{DJANGO_POLICY_NAME}",
            headers=headers,
            json={"policy": policy},
            timeout=15,
        )
        if resp.status_code not in (200, 204):
            self.stderr.write(self.style.ERROR(f"Policy create failed: {resp.text}"))
            return

        resp = requests.post(
            f"{base}/v1/sys/auth/approle",
            headers=headers,
            json={"type": "approle"},
            timeout=10,
        )

        resp = requests.post(
            f"{base}/v1/auth/approle/role/{DJANGO_ROLE_NAME}",
            headers=headers,
            json={"token_policies": [DJANGO_POLICY_NAME], "token_ttl": "1h", "token_max_ttl": "4h"},
            timeout=15,
        )
        if resp.status_code not in (200, 204):
            self.stderr.write(self.style.ERROR(f"AppRole create failed: {resp.text}"))
            return

        role_resp = requests.get(
            f"{base}/v1/auth/approle/role/{DJANGO_ROLE_NAME}/role-id",
            headers=headers,
            timeout=15,
        )
        secret_resp = requests.post(
            f"{base}/v1/auth/approle/role/{DJANGO_ROLE_NAME}/secret-id",
            headers=headers,
            timeout=15,
        )

        if role_resp.status_code != 200 or secret_resp.status_code != 200:
            self.stderr.write(self.style.ERROR("Failed to fetch AppRole credentials."))
            return

        role_id = role_resp.json()["data"]["role_id"]
        secret_id = secret_resp.json()["data"]["secret_id"]

        self.stdout.write(self.style.SUCCESS("Django AppRole configured."))
        self.stdout.write(f"VAULT_APP_ROLE_ID={role_id}")
        self.stdout.write(f"VAULT_APP_SECRET_ID={secret_id}")
        self.stdout.write(json.dumps({"role_id": role_id, "secret_id": secret_id}, indent=2))
