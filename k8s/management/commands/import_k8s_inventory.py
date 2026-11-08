"""Import a Kubernetes client infrastructure workbook.

    manage.py import_k8s_inventory /path/to/inventory.xlsx
    manage.py import_k8s_inventory /path/to/sheets/ --dry-run
    manage.py import_k8s_inventory inventory.xlsx --site "Mumbai (NSE)" --create-compute

`path` is the .xlsx workbook, a .zip of its sheets as CSV, or a directory of those CSV files. The import
is idempotent: running it again updates the same rows, and rows the new snapshot no longer contains are
marked as missing rather than deleted. `--dry-run` reports what would change and writes nothing.
"""
from django.core.management.base import BaseCommand, CommandError

from k8s.importer import run_import
from k8s.models import GLOBAL_SITE, ImportStatus


class Command(BaseCommand):
    help = "Import a Kubernetes client infrastructure workbook (.xlsx, .zip of CSVs, or a CSV directory)."

    def add_arguments(self, parser):
        parser.add_argument("path", help="The .xlsx workbook, a .zip of CSV sheets, or a directory of them.")
        parser.add_argument("--dry-run", action="store_true",
                            help="Report what would change and write nothing.")
        parser.add_argument("--create-compute", action="store_true",
                            help="Also mirror the cluster into Compute → Clusters, adding a device for "
                                 "every node that is not in the device inventory yet.")
        parser.add_argument("--site", default=GLOBAL_SITE,
                            help=f'Site to file the imported records under (default "{GLOBAL_SITE}").')

    def handle(self, *args, **options):
        result = run_import(options["path"], user=None, dry_run=options["dry_run"],
                            site=options["site"] or GLOBAL_SITE,
                            create_compute=options["create_compute"])
        self.stdout.write(f"Source: {result.source_name}")
        if result.source_sha256:
            self.stdout.write(f"SHA-256: {result.source_sha256}")
        self.stdout.write(f"Site: {result.site}")
        if result.dry_run:
            self.stdout.write(self.style.WARNING("Dry run — nothing was written."))

        if result.counts:
            width = max(len(name) for name in result.counts)
            self.stdout.write("")
            self.stdout.write(f"{'sheet'.ljust(width)}   rows  created  updated  missing  skipped")
            for sheet, counts in result.rows:
                self.stdout.write(
                    f"{sheet.ljust(width)} {counts.get('rows', 0):6} {counts.get('created', 0):8} "
                    f"{counts.get('updated', 0):8} {counts.get('missing', 0):8} "
                    f"{counts.get('skipped', 0):8}")
            self.stdout.write(f"{'total'.ljust(width)} {result.total_rows:6} {result.total_created:8} "
                              f"{result.total_updated:8} {result.total_missing:8}")

        for sheet in result.unknown_sheets:
            self.stdout.write(self.style.WARNING(f"No importer for sheet {sheet} — it was skipped."))
        for sheet in result.ignored_sheets:
            self.stdout.write(f"Ignored sheet {sheet} (layout or per-client repeat).")
        for warning in result.warnings:
            self.stdout.write(self.style.WARNING(f"warning: {warning}"))
        for error in result.errors:
            self.stdout.write(self.style.ERROR(f"error: {error}"))

        if result.status == ImportStatus.FAILED:
            raise CommandError("The import did not finish cleanly; see the errors above.")
        self.stdout.write(self.style.SUCCESS(
            f"Imported {result.total_rows} row(s): {result.total_created} created, "
            f"{result.total_updated} updated, {result.total_missing} no longer present."
            if not result.dry_run else
            f"Would import {result.total_rows} row(s): {result.total_created} new, "
            f"{result.total_updated} changed, {result.total_missing} no longer present."))
