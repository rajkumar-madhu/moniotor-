"""
Seed application secrets into HashiCorp Vault from environment variables.

Run once during initial deployment (or when rotating secrets):

    export VAULT_TOKEN=<bootstrap-root-or-admin-token>
    export SECRET_KEY=...
    export MYSQL_DB_PASS=...
    python manage.py vault_seed_app_secrets

With AppRole already configured, root token is not required if VAULT_APP_ROLE_ID
has write access (bootstrap role only).
"""

import os
from pathlib import Path

from django.core.management.base import BaseCommand

from lib.LinkedEyeVault.AppSecrets import APP_SECRET_KEYS, write_vault_bundle


class Command(BaseCommand):
    help = "Write app secrets (or all .env keys) into Vault KV bundle"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print secrets that would be written without calling Vault",
        )
        parser.add_argument(
            "--all-env",
            action="store_true",
            help="Seed all keys from .env (plus currently exported env values for same keys)",
        )

    def _load_env_keys(self):
        env_path = Path.cwd() / ".env"
        keys = []
        if not env_path.exists():
            return keys

        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            key = line.split("=", 1)[0].strip()
            if key:
                keys.append(key)
        return keys

    def handle(self, *args, **options):
        bundle = {}
        missing = []
        seed_keys = self._load_env_keys() if options["all_env"] else list(APP_SECRET_KEYS)
        seen = set()
        for key in seed_keys:
            if key in seen:
                continue
            seen.add(key)
            value = os.getenv(key, "")
            if value:
                bundle[key] = value
            else:
                missing.append(key)

        if not bundle:
            self.stderr.write(self.style.ERROR("No secrets found in environment to seed."))
            return

        self.stdout.write(f"Prepared {len(bundle)} key(s) for Vault.")
        if missing:
            self.stdout.write(self.style.WARNING(f"Skipped empty keys: {', '.join(missing)}"))

        if options["dry_run"]:
            for key in sorted(bundle):
                self.stdout.write(f"  {key}=***")
            return

        if write_vault_bundle(bundle):
            self.stdout.write(self.style.SUCCESS("Vault app secrets bundle updated successfully."))
        else:
            self.stderr.write(self.style.ERROR("Failed to write secrets to Vault."))
