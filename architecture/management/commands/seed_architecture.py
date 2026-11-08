"""Create (or remove with --remove) the example spine-leaf fabric on documentation IP ranges. Idempotent."""
from django.core.management.base import BaseCommand, CommandError

from architecture import example, services
from core.models import Site


class Command(BaseCommand):
    help = ("Create the example data centre fabric (2 routers, 2 firewalls, 2 core, 4 spine, 4 leaf switches, 19 servers in "
            "4 groups, their links and racks), tagged 'example'. --remove deletes only what this command created.")

    def add_arguments(self, parser):
        parser.add_argument("--remove", action="store_true", help="Delete the example fabric again.")
        parser.add_argument("--site", default="", help="Site name for the example devices (default: the first site, else global).")

    def handle(self, *args, remove=False, site="", **opts):
        tenant = services.tenant()
        if remove:
            out = example.remove(tenant)
            self.stdout.write(self.style.SUCCESS(f"Removed {out['devices']} example device(s) and {out['racks']} example rack(s)."))
            return
        if not site:
            first = Site.objects.filter(tenant_key=tenant).order_by("name").values_list("name", flat=True).first()
            site = first or "All Sites"
        elif site != "All Sites" and not Site.objects.filter(tenant_key=tenant, name=site).exists():
            raise CommandError(f"There is no site named “{site}”.")
        n = example.load(site, tenant, log=lambda m: self.stdout.write(self.style.WARNING(m)))
        self.stdout.write(self.style.SUCCESS(
            f"Example fabric on “{site}”: {n['devices']} devices, {n['links']} links, {n['racks']} racks, "
            f"{n['items']} rack items created ({n['skipped']} skipped). Tagged 'example'."))
