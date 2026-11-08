"""manage.py discover_targets – run one Prometheus discovery pass now and print what it found.

The same pass the `telemetry.discovery` background job runs every 5 minutes (see telemetry/discovery.py):
reads /api/v1/targets, asks Prometheus a fixed set of bulk queries about every instance, upserts
telemetry.DiscoveredTarget rows, links them to CMDB items / devices / exporters and (when those settings are
on in Administration › Integrations) creates the missing records. Safe to re-run: it never duplicates.

    manage.py discover_targets            # run the pass and print the summary
    manage.py discover_targets --now      # the same (explicit; kept for operators who expect the flag)
    manage.py discover_targets --list     # also list every target the pass knows about
"""
from django.core.management.base import BaseCommand

from telemetry import discovery
from telemetry.models import DiscoveredTarget


class Command(BaseCommand):
    help = "Run one Prometheus discovery pass (targets, host details, CMDB/device/exporter links)."

    def add_arguments(self, parser):
        parser.add_argument("--now", action="store_true", help="Run the pass immediately (the default).")
        parser.add_argument("--list", action="store_true", help="Print every known target after the pass.")

    def handle(self, *args, **options):
        run = discovery.run(trigger="command")
        if not run.ok:
            self.stderr.write(self.style.ERROR(f"Discovery failed: {run.error or 'unknown error'}"))
        else:
            self.stdout.write(self.style.SUCCESS(run.summary))
            self.stdout.write(f"{run.queries} Prometheus quer{'y' if run.queries == 1 else 'ies'} "
                              f"in {run.duration_ms:.0f} ms")
        if options["list"]:
            rows = DiscoveredTarget.objects.for_tenant().order_by("kind", "instance")
            for r in rows:
                state = "missing" if r.missing_since else r.state
                link = getattr(r.linked_to, "name", "") or "–"
                self.stdout.write(f"  {r.instance:<40} {r.kind:<12} {r.health:<8} {state:<8} "
                                  f"{r.site:<22} {link}")
            if not rows:
                self.stdout.write("  (no targets)")
        return None
