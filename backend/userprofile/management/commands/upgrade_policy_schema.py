"""
Upgrade the legacy ``policy`` table to the production escalation schema.

Usage:
    python manage.py upgrade_policy_schema
    python manage.py upgrade_policy_schema --dry-run
"""

from django.core.management.base import BaseCommand
from django.db import connection

from userprofile.policy_schema import upgrade_policy_schema


class Command(BaseCommand):
    help = "Upgrade policy table to device-wise escalation schema (idempotent)"

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Print planned DDL only; do not execute',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        steps = upgrade_policy_schema(connection, dry_run=dry_run)
        if not steps:
            self.stdout.write(self.style.SUCCESS('No policy schema changes needed.'))
            return

        for action, detail in steps:
            if dry_run:
                if isinstance(detail, list):
                    for line in detail:
                        self.stdout.write(f'[dry-run] {action}: {line}')
                else:
                    self.stdout.write(f'[dry-run] {action}: {detail}')
            else:
                self.stdout.write(f'{action}: {detail}')

        if not dry_run:
            self.stdout.write(self.style.SUCCESS('Policy schema upgrade complete.'))
