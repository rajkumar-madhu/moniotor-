"""Load a FinSpot-style client-wise tracker CSV into Client + Site records."""
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from administration.client_tracker_csv import apply_client_tracker_csv


class Command(BaseCommand):
    help = "Import LinkedEye client-wise tracker CSV (FinSpot DEV template). Passwords are not stored."

    def add_arguments(self, parser):
        parser.add_argument("path", type=str, help="Path to the .csv file")

    def handle(self, *args, **options):
        path = Path(options["path"]).expanduser()
        if not path.is_file():
            raise CommandError(f"File not found: {path}")
        raw = path.read_bytes()
        for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
            try:
                content = raw.decode(enc)
                break
            except UnicodeDecodeError:
                content = None
        if content is None:
            raise CommandError(f"Could not decode {path} as text.")
        result = apply_client_tracker_csv(content)
        if not result.get("ok"):
            raise CommandError("; ".join(result.get("errors", ["Import failed"])))
        self.stdout.write(self.style.SUCCESS(
            f"Client “{result['client']}” ({result['client_code']}) — "
            f"{'created' if result['client_created'] else 'updated'}; "
            f"{len(result['sites'])} site(s); {result['tools']} tool row(s) in notes (no secrets)."
        ))
        for s in result["sites"]:
            self.stdout.write(f"  • {s['name']} ({s['code']}) — {'created' if s['created'] else 'updated'}")
