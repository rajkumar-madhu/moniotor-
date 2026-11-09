from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from administration.finspot_site import ensure_finspot_site as ensure


class Command(BaseCommand):
    help = "Create or update core.Site and Lesites profile for a FinSpot slug (e.g. fs-dx-le)."

    def add_arguments(self, parser):
        parser.add_argument("slug", help="Legacy Lesites sitename slug, e.g. fs-dx-le.")
        parser.add_argument(
            "--no-sync-devices",
            action="store_true",
            help="Do not rename legacy Device.site labels (e.g. FinSpot DX → FinSpot DX DR).",
        )

    def handle(self, *args, **opts):
        slug = (opts["slug"] or "").strip().lower()
        if not slug:
            raise CommandError("slug is required")
        tenant = settings.LINKEDEYE_TENANT
        site, created, renamed = ensure(
            tenant,
            slug,
            sync_device_sites=not opts["no_sync_devices"],
        )
        verb = "Created" if created else "Updated"
        self.stdout.write(
            self.style.SUCCESS(
                f"{verb} site {site.name!r} (code={site.code!r}); {renamed} device(s) aligned to site name."
            )
        )
