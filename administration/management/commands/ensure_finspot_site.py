from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from administration.finspot_naming import FINSPOT_SITE_LABELS
from administration.finspot_site import ensure_finspot_site as ensure
from core.models import Membership


class Command(BaseCommand):
    help = (
        "Create or update FinSpot Client + Site + Lesites profile "
        "(e.g. fs-le-dev-finspot). Use --all for every known FinSpot slug."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "slug",
            nargs="?",
            default="",
            help="Legacy Lesites sitename slug, e.g. fs-le-dev-finspot or fs-dx-le.",
        )
        parser.add_argument(
            "--all",
            action="store_true",
            help=f"Ensure every known FinSpot slug: {', '.join(sorted(FINSPOT_SITE_LABELS))}.",
        )
        parser.add_argument(
            "--no-sync-devices",
            action="store_true",
            help="Do not rename legacy Device.site labels (e.g. FinSpot DX → FinSpot DX DR).",
        )
        parser.add_argument(
            "--grant-user",
            default="",
            help="Username to grant Membership (admin) on each ensured site.",
        )
        parser.add_argument(
            "--role",
            default="admin",
            choices=["admin", "operator", "viewer"],
            help="Role for --grant-user (default admin).",
        )

    def handle(self, *args, **opts):
        if opts["all"]:
            slugs = sorted(FINSPOT_SITE_LABELS)
        else:
            slug = (opts["slug"] or "").strip().lower()
            if not slug:
                raise CommandError("Pass a slug (e.g. fs-le-dev-finspot) or --all.")
            slugs = [slug]

        tenant = settings.LINKEDEYE_TENANT
        grant_user = None
        username = (opts["grant_user"] or "").strip()
        if username:
            User = get_user_model()
            try:
                grant_user = User.objects.get(username=username)
            except User.DoesNotExist as exc:
                raise CommandError(f"User {username!r} not found.") from exc

        for slug in slugs:
            site, created, renamed = ensure(
                tenant,
                slug,
                sync_device_sites=not opts["no_sync_devices"],
            )
            verb = "Created" if created else "Updated"
            self.stdout.write(
                self.style.SUCCESS(
                    f"{verb} site {site.name!r} (code={site.code!r}, client=finspot); "
                    f"{renamed} device(s) aligned."
                )
            )
            if grant_user is not None:
                mem, made = Membership.objects.update_or_create(
                    user=grant_user,
                    site=site,
                    defaults={"role": opts["role"]},
                )
                self.stdout.write(
                    f"  Membership {'created' if made else 'updated'}: "
                    f"{grant_user.username} → {site.code} ({mem.role})"
                )
