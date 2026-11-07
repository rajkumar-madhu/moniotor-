"""
Drop legacy Redmine / ticket tables and columns from MySQL.

Usage:
    python manage.py drop_redmine_schema
    python manage.py drop_redmine_schema --dry-run
"""

from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    help = "Remove Redmine-era MySQL schema (lesite.redmine_url, ticket_* tables)"

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Print planned DDL only; do not execute',
        )

    def _column_exists(self, cursor, table, column):
        cursor.execute(
            """
            SELECT COUNT(*) FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s
            """,
            [table, column],
        )
        return cursor.fetchone()[0] > 0

    def _table_exists(self, cursor, table):
        cursor.execute(
            """
            SELECT COUNT(*) FROM information_schema.TABLES
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
            """,
            [table],
        )
        return cursor.fetchone()[0] > 0

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        statements = []

        with connection.cursor() as cursor:
            if self._column_exists(cursor, 'lesite', 'redmine_url'):
                statements.append('ALTER TABLE lesite DROP COLUMN redmine_url')
            for table in ('ticket_overview', 'ticket_siteview'):
                if self._table_exists(cursor, table):
                    statements.append(f'DROP TABLE {table}')

        if not statements:
            self.stdout.write(self.style.SUCCESS('No Redmine schema objects found.'))
            return

        for stmt in statements:
            if dry_run:
                self.stdout.write(stmt)
            else:
                self.stdout.write(f'Executing: {stmt}')
                with connection.cursor() as cursor:
                    cursor.execute(stmt)

        if not dry_run:
            self.stdout.write(self.style.SUCCESS('Redmine schema cleanup complete.'))
