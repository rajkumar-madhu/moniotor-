"""
One-time backfill: populate order_latency_daily_summary and
order_latency_minute_summary from order_latency rows that already exist in
MySQL. The regular latency.py ETL only writes summaries for days it still
processes a CSV for; this covers everything already sitting in the table so
the APM page and Grafana have history immediately instead of only from the
next ETL run onward.

Usage:
  python manage.py backfill_latency_summary                # every day present
  python manage.py backfill_latency_summary --date 2026-07-15   # one day
"""

from django.core.management.base import BaseCommand
from django.db import connection

import pandas as pd

from bodeodstatus.latency import compute_and_store_summaries


class Command(BaseCommand):
    help = "Backfill order_latency_daily_summary / order_latency_minute_summary from existing order_latency rows."

    def add_arguments(self, parser):
        parser.add_argument('--date', help='Single file_date to backfill (YYYY-MM-DD).', default=None)

    def handle(self, *args, **options):
        single_date = options.get('date')

        with connection.cursor() as cur:
            if single_date:
                cur.execute("SELECT DISTINCT file_date FROM order_latency WHERE file_date = %s", [single_date])
            else:
                cur.execute("SELECT DISTINCT file_date FROM order_latency ORDER BY file_date")
            dates = [row[0] for row in cur.fetchall()]

        if not dates:
            self.stdout.write(self.style.WARNING("No file_date values found in order_latency."))
            return

        self.stdout.write(f"Backfilling {len(dates)} day(s): {dates[0]} .. {dates[-1]}")

        for i, file_date_obj in enumerate(dates, start=1):
            # Force DOUBLE (not Decimal) so MySQLdb returns Python floats --
            # a DataFrame built from Decimal objects stays object-dtype (each
            # cell a boxed ~100-byte Decimal) instead of a packed float64
            # array. On a 4.4M-row day that difference was enough to OOM a
            # 3Gi container; plain floats keep the same day well under 1Gi.
            # MySQL 5.7 has no CAST(... AS DOUBLE) (added in 8.0.17), so force
            # floating-point via MySQL's numeric-type-combination rules: a
            # column multiplied by a float literal (1e0) returns DOUBLE.
            with connection.cursor() as cur:
                cur.execute(
                    "SELECT exch_seg, oms_latency * 1e0, "
                    "oms_exch_confirmation * 1e0, oms_update_time_conv "
                    "FROM order_latency WHERE file_date = %s",
                    [file_date_obj]
                )
                rows = cur.fetchall()

            if not rows:
                self.stdout.write(f"[{i}/{len(dates)}] {file_date_obj}: no rows, skipping")
                continue

            df = pd.DataFrame(
                rows, columns=['exch_seg', 'oms_latency', 'oms_exch_confirmation', 'oms_update_time_conv']
            )
            row_count = len(df)
            compute_and_store_summaries(df, file_date_obj)
            del df, rows
            self.stdout.write(self.style.SUCCESS(f"[{i}/{len(dates)}] {file_date_obj}: {row_count} rows summarized"))

        self.stdout.write(self.style.SUCCESS("Backfill complete."))
