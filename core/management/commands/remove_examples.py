"""Remove every example record the `seed_*` commands created, across all apps.

    manage.py remove_examples              # dry run: lists what would be removed, changes nothing
    manage.py remove_examples --yes        # removes it (one transaction per app, audited)
    manage.py remove_examples --only cmdb,network --yes

Only records that still carry the seed marker go (see core/examples.py); real records, including ones that reuse an
example name, are never touched. Run it once before go-live; `manage.py go_live_check` warns while example data exists.
"""
from django.core.management.base import BaseCommand, CommandError

from core import examples
from core.audit import audit


class Command(BaseCommand):
    help = "Remove all example data created by the seed_* commands (dry run unless --yes)."

    def add_arguments(self, parser):
        parser.add_argument("--yes", action="store_true", help="Actually delete. Without it (or with --dry-run) only counts are printed.")
        parser.add_argument("--dry-run", action="store_true", help="Print what would be removed and change nothing (the default).")
        parser.add_argument("--only", default="", help="Comma-separated areas to limit to (default: all). See the list printed by a dry run.")

    def handle(self, *args, yes=False, dry_run=False, only="", **opts):
        known = examples.known_keys()
        wanted = [k.strip() for k in only.split(",") if k.strip()]
        bad = [k for k in wanted if k not in known]
        if bad:
            raise CommandError(f"Unknown area(s): {', '.join(bad)}. Choose from: {', '.join(known)}.")
        report = [r for r in examples.summary(only=wanted or None)]
        total = examples.total(report)
        if not total:
            self.stdout.write(self.style.SUCCESS("No example data found. Nothing to remove."))
            return
        self.stdout.write("Example data found:")
        for r in report:
            if r["total"]:
                detail = ", ".join(f"{noun}: {n}" for noun, n in r["rows"])
                self.stdout.write(f"  {r['key']:<15} {r['total']:>6}  {detail}")
        self.stdout.write(f"  {'total':<15} {total:>6}")
        if dry_run or not yes:
            self.stdout.write(self.style.WARNING("Dry run: nothing was changed. Run again with --yes to remove it."))
            return
        results = examples.remove(only=wanted or None)
        removed = sum(r["removed"] for r in results)
        failed = [r for r in results if r["error"]]
        for r in results:
            if r["before"]:
                state = f"FAILED ({r['error']})" if r["error"] else f"removed {r['removed']}"
                self.stdout.write(f"  {r['key']:<15} {state}")
        audit(None, "delete", object_type="core.exampledata", result="failed" if failed else "ok", via="manage.py remove_examples",
              removed=removed, areas={r["key"]: r["removed"] for r in results if r["before"]},
              failed=[r["key"] for r in failed])
        if failed:
            raise CommandError(f"Removed {removed} record(s); {len(failed)} area(s) failed and kept their rows: "
                               + ", ".join(r["key"] for r in failed))
        self.stdout.write(self.style.SUCCESS(f"Removed {removed} example record(s)."))
