"""Import Lesites / FinSpot Devices sheet into network.Device (`manage.py import_lesites_devices_xlsx`).

Reads only the Devices worksheet (no credential sheets). Idempotent: matches by device name,
then management IP. Example seed devices are skipped unless --force."""
import zipfile
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core.models import Site
from network.lesites_import import import_devices, parse_devices_workbook


class Command(BaseCommand):
    help = "Import Lesites FinSpot Devices sheet from an .xlsx file into network inventory."

    def add_arguments(self, parser):
        parser.add_argument("path", type=str, help="Path to the .xlsx file on disk (Devices sheet only is read).")
        parser.add_argument("--sheet", default="Devices", help="Worksheet name (default: Devices).")
        parser.add_argument("--site", default="", help='Site name on Device records (e.g. "DR Site").')
        parser.add_argument("--site-code", default="", help="Resolve site from core.Site.code (overrides --site when found).")
        parser.add_argument("--environment", default="dr", help="Device environment key (default: dr).")
        parser.add_argument("--lesites-tag", default="fs-dr-le", help="Extra tag suffix after imported,lesites (default: fs-dr-le).")
        parser.add_argument("--dry-run", action="store_true", help="Parse and report without writing the database.")
        parser.add_argument(
            "--no-update",
            action="store_true",
            help="Create missing devices only; do not update existing records.",
        )
        parser.add_argument("--force", action="store_true", help="Allow updating devices tagged example.")

    def handle(self, *args, **opts):
        path = Path(opts["path"]).expanduser()
        if not path.is_file():
            raise CommandError(f"File not found: {path}")

        tenant = settings.LINKEDEYE_TENANT
        site_name = self._resolve_site(tenant, opts["site"], opts["site_code"])

        try:
            rows = parse_devices_workbook(str(path), sheet_name=opts["sheet"])
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(f"Parsed {len(rows)} row(s) from sheet {opts['sheet']!r}; site={site_name!r}.")

        result = import_devices(
            rows,
            tenant,
            site=site_name,
            environment=opts["environment"],
            lesites_tag=opts["lesites_tag"],
            dry_run=opts["dry_run"],
            update_existing=not opts["no_update"],
            force=opts["force"],
        )

        for name, dtype, ip, action in result.actions:
            self.stdout.write(f"  {action:16} {name} ({dtype}) {ip}")

        for w in result.warnings:
            self.stdout.write(self.style.WARNING(w))

        mode = "Dry-run" if opts["dry_run"] else "Import"
        self.stdout.write(
            self.style.SUCCESS(
                f"{mode}: {result.created} created, {result.updated} updated, {result.skipped} skipped."
            )
        )

    def _resolve_site(self, tenant_key: str, site: str, site_code: str) -> str:
        if site_code:
            found = Site.objects.filter(tenant_key=tenant_key, code__iexact=site_code).first()
            if found:
                return found.name
            raise CommandError(f"No Site with code={site_code!r} for tenant {tenant_key!r}.")
        if site:
            return site
        return "DR Site"
