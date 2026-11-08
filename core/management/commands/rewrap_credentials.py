"""Re-encrypt every stored credential with the primary LINKEDEYE_CREDENTIAL_KEY (key rotation)."""
from django.core.management.base import BaseCommand, CommandError

from core import secrets


class Command(BaseCommand):
    help = ("After prepending a new key to LINKEDEYE_CREDENTIAL_KEY, re-encrypt all stored credentials with it so the "
            "old key can be dropped. Rows no listed key can read are reported and left unchanged.")

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Count what would change; write nothing.")

    def handle(self, *args, **opts):
        try:
            result = secrets.rewrap(dry_run=opts["dry_run"])
        except secrets.VaultUnavailable as e:
            raise CommandError(str(e)) from None
        verb = "would re-encrypt" if opts["dry_run"] else "re-encrypted"
        self.stdout.write(f"{verb} {result['rewrapped']} credential(s); {result['current']} already on the primary key.")
        if result["failed"]:
            ids = ", ".join(str(i) for i in result["failed"])
            self.stdout.write(self.style.ERROR(
                f"{len(result['failed'])} credential(s) cannot be read with any listed key (ids: {ids}). "
                "Keep the old key until they are re-entered in Onboarding."))
            raise CommandError("some credentials could not be re-encrypted")
