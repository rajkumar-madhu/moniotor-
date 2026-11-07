"""
MessageQueue Dashboard - Django Views
======================================
Architecture: Isolated per-site database deployment.
Each site runs its own Django instance and queries its own local default DB.
Data is naturally isolated as each server has access only to its site's data.
Matches the "On-board Device" (allonboard) dashboard pattern.
"""

import os
import traceback
import json
from datetime import datetime, date, timedelta

from django.db import connections, connection, ProgrammingError, OperationalError
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth.decorators import login_required
from django.forms.models import model_to_dict
from django.utils import timezone

from .models import (
    OrderLatencyModel, QueueLine1Model, QueueLine2Model,
    OrderLatencyDailySummaryModel, OrderLatencyMinuteSummaryModel,
)

# ─── Data Helpers ──────────────────────────────────────────

def fmt_date(val):
    if isinstance(val, (date, datetime)): return val.strftime('%Y-%m-%d')
    return str(val) if val else None


def latest_available_date():
    """Newest file_date across the latency/queue rollups, or None.

    Reads order_latency_daily_summary (a few hundred rows, file_date is the
    leftmost column of its unique index) instead of the raw order_latency
    table (8M+ rows, no index on file_date -- that ORDER BY used to be a full
    table scan on every APM page load).

    Do not seed a default from a single table: queue_line1/queue_line2 are
    empty whenever the QueSize CSV feed has not been ingested, and seeding
    from an empty table yields no date, which made the stats/data endpoints
    return an empty payload on page load and blanked every tile -- even
    though order_latency held millions of rows for the same day.
    """
    newest = None
    try:
        newest = (
            OrderLatencyDailySummaryModel.objects
            .order_by('-file_date')
            .values_list('file_date', flat=True)
            .first()
        )
    except (ProgrammingError, OperationalError):
        pass
    for model in (QueueLine2Model, QueueLine1Model):
        try:
            row = model.objects.exclude(file_date__isnull=True).order_by('-file_date').first()
        except (ProgrammingError, OperationalError):
            continue
        if row and row.file_date and (newest is None or row.file_date > newest):
            newest = row.file_date
    return fmt_date(newest) if newest else None


def make_ts(date_str, time_str):
    """Build a naive IST datetime bound for querying the latency/queue tables.

    order_latency.oms_update_time_conv and queue_line*.time hold naive IST
    wall-clock (latency.py strips tzinfo after converting to Asia/Kolkata).
    An aware datetime must not be used here: with USE_TZ=True the ORM converts
    it to UTC before querying, so 09:15+05:30 is sent as 03:45 and matches no
    rows, while raw cursor queries pass the wall-clock through and do match.
    That split is what silently zeroed every percentile and segment panel.
    """
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M'):
        try:
            return datetime.strptime(f"{date_str} {time_str}", fmt)
        except ValueError:
            continue
    return None


# ─── Views ──────────────────────────────────────────────────

@csrf_exempt
def messagequeue_sites(request):
    """Note: site isolation is handled at the network/deployment level."""
    return HttpResponse(json.dumps({'status': 200, 'note': 'Local analytics DB access only'}), content_type="application/json")


@login_required
def messagequeue_dashboard(request):
    return redirect('/bod-eodstatus/le-adp-status?tab=messagequeue')


# ─── API Endpoints (Local Site Only) ─────────────────────────

def messagequeue_dates(request):
    try:
        found_dates = set()
        db_error = None

        # order_latency has no index on file_date, so DISTINCT/ORDER BY against
        # it directly is a full 8M+ row scan; order_latency_daily_summary has
        # one 'ALL' row per day already keyed on file_date, so use that instead.
        for model in [QueueLine1Model, QueueLine2Model]:
            try:
                d_list = model.objects.values_list('file_date', flat=True).order_by('-file_date').distinct()[:100]
                for d in d_list:
                    if d: found_dates.add(fmt_date(d))
            except (ProgrammingError, OperationalError) as e:
                db_error = str(e)
            except Exception:
                pass

        try:
            d_list = (
                OrderLatencyDailySummaryModel.objects
                .filter(exch_seg='ALL')
                .values_list('file_date', flat=True)
                .order_by('-file_date')[:100]
            )
            for d in d_list:
                if d: found_dates.add(fmt_date(d))
        except (ProgrammingError, OperationalError) as e:
            db_error = str(e)
        except Exception:
            pass

        if not found_dates and db_error:
            msg = f"Database or Table missing: {db_error}. Please check with administrator."
            return HttpResponse(json.dumps({'status': 503, 'error': msg}), content_type="application/json")

        dates = sorted(list(found_dates), reverse=True)[:60]
        return HttpResponse(json.dumps({'status': 200, 'dates': dates, 'count': len(dates)}), content_type="application/json")
    except Exception as e:
        return HttpResponse(json.dumps({'status': 500, 'error': str(e)}), content_type="application/json")


def messagequeue_data(request):
    target_date = request.GET.get('file_date', '').strip()
    time_start, time_end = request.GET.get('time_start', '09:15').strip(), request.GET.get('time_end', '15:35').strip()
    requested_segs = [s.strip() for s in request.GET.get('segment', '').split(',') if s.strip()]

    try:
        if not target_date:
            target_date = latest_available_date()

        if not target_date:
            return HttpResponse(json.dumps({'status': 200, 'data': []}), content_type="application/json")

        ts_start, ts_end = make_ts(target_date, f"{time_start}:00"), make_ts(target_date, f"{time_end}:59")
        if not ts_start or not ts_end:
            return HttpResponse(json.dumps({'status': 200, 'data': []}), content_type="application/json")
        
        seg_map = {}
        total_pts = 0

        # We downsample to minute buckets to handle high-density data (100k+ pts)
        # GROUP BY segment + Minute bucket. We take the MAX(queue_size) for peaks.
        ts_start_str, ts_end_str = f"{target_date} {time_start}:00", f"{target_date} {time_end}:59"
        seg_map = {}
        total_pts = 0

        with connection.cursor() as cur:
            for table, label in [('queue_line2', 'Line-2'), ('queue_line1', 'Line-1')]:
                # DATE_FORMAT is the most reliable way to bucket timestamps in MySQL
                query = (
                    f"SELECT segment, DATE_FORMAT(time, '%%Y-%%m-%%d %%H:%%i') as bucket, MAX(queue_size), AVG(queue_size) "
                    f"FROM {table} WHERE time BETWEEN %s AND %s "
                    f"GROUP BY segment, bucket ORDER BY bucket ASC"
                )
                cur.execute(query, [ts_start_str, ts_end_str])
                for r in cur.fetchall():
                    seg, bucket, peak_q, avg_q = r
                    
                    # Extract the HH:MM part from the bucket string
                    b_time = bucket.split(' ')[-1] if ' ' in bucket else bucket
                    
                    # Optional filter: if requested_segs is set, only include segments that match a prefix
                    if requested_segs:
                        matched = False
                        for rs in requested_segs:
                            if seg.upper().startswith(rs.upper()):
                                matched = True; break
                        if not matched: continue

                    seg_map.setdefault(seg, {'segment': seg, 'line': label, 'points': []})['points'].append({
                        'time': b_time,
                        # None (no non-null queue_size in this bucket) must stay None, not become a
                        # fabricated 0 -- a bucket with no data is not the same as a bucket that peaked at 0.
                        'queue_size': int(peak_q) if peak_q is not None else None,
                        'avg_queue': round(float(avg_q), 2) if avg_q is not None else None,
                    })
                    total_pts += 1

        return HttpResponse(json.dumps({'status': 200, 'file_date': target_date, 'data': list(seg_map.values()), 'total_points': total_pts}), content_type="application/json")
    except (ProgrammingError, OperationalError) as e:
        msg = f"Database or Table missing: {str(e)}. Please check with administrator."
        return HttpResponse(json.dumps({'status': 503, 'error': msg}), content_type="application/json")
    except Exception as e:
        return HttpResponse(json.dumps({'status': 500, 'error': str(e)}), content_type="application/json")


def messagequeue_stats(request):
    target_date = request.GET.get('file_date', '').strip()
    time_start, time_end = request.GET.get('time_start', '09:15').strip(), request.GET.get('time_end', '15:35').strip()

    try:
        if not target_date:
            target_date = latest_available_date()

        if not target_date:
            return HttpResponse(json.dumps({'status': 200, 'queue_stats': {}}), content_type="application/json")

        t_s_str, t_e_str = f"{target_date} {time_start}:00", f"{target_date} {time_end}:59"
        target_date_obj = datetime.strptime(target_date, '%Y-%m-%d').date()
        queue_stats = {}
        total_data_points = 0

        # ── Queue aggregates per segment ───────────────────────────────────
        # queue_line1/2 stay small (per-day CSV ingest), so a live GROUP BY
        # here is cheap -- unlike order_latency, they never needed a rollup.
        with connection.cursor() as cur:
            for table, label in [('queue_line2', 'Line-2'), ('queue_line1', 'Line-1')]:
                cur.execute(
                    f"SELECT segment, COUNT(*) as pts, MAX(queue_size) as peak, AVG(queue_size) as avg_q "
                    f"FROM {table} WHERE time BETWEEN %s AND %s GROUP BY segment",
                    [t_s_str, t_e_str]
                )
                for row in cur.fetchall():
                    seg, pts, peak, avg_q = row
                    queue_stats[seg] = {
                        'peak_queue': int(peak or 0),
                        'avg_queue': round(float(avg_q or 0), 2),
                        'total_points': int(pts),
                        'line': label,
                    }
                    total_data_points += int(pts)

        # ── Overall avg queue across all segments ──────────────────────────
        all_avgs = [v['avg_queue'] for v in queue_stats.values() if v['avg_queue'] > 0]
        avg_all = round(sum(all_avgs) / len(all_avgs), 2) if all_avgs else 0

        # ── Latency: read from the rollups the ETL writes, never scan
        # order_latency live. Percentiles (p50/p95/p99) are day-level, from
        # order_latency_daily_summary. avg/max/order_count for the requested
        # time_start/time_end window are derived from order_latency_minute_
        # summary (a few hundred rows/day) via a count-weighted average --
        # cheap regardless of how wide the window is or how big order_latency
        # itself grows.
        daily_rows = list(
            OrderLatencyDailySummaryModel.objects.filter(file_date=target_date_obj).values()
        )
        daily_by_seg = {r['exch_seg']: r for r in daily_rows}
        overall_daily = daily_by_seg.get('ALL')

        minute_rows = list(
            OrderLatencyMinuteSummaryModel.objects
            .filter(file_date=target_date_obj, minute_bucket__gte=time_start, minute_bucket__lte=time_end)
            .values()
        )

        def _weighted_avg(rows, val_key):
            pairs = [(r['order_count'], r[val_key]) for r in rows if r[val_key] is not None]
            total_n = sum(n for n, _ in pairs)
            return round(sum(n * v for n, v in pairs) / total_n, 2) if total_n else 0.0

        def _max_of(rows, val_key):
            vals = [r[val_key] for r in rows if r[val_key] is not None]
            return round(max(vals), 2) if vals else 0.0

        latency_stats = {}
        total_orders_window = sum(r['order_count'] for r in minute_rows)
        if total_orders_window:
            latency_stats = {
                'total_orders': total_orders_window,
                'avg_oms_latency': _weighted_avg(minute_rows, 'avg_oms_latency'),
                'max_oms_latency': _max_of(minute_rows, 'max_oms_latency'),
                'avg_exch_latency': _weighted_avg(minute_rows, 'avg_exch_confirmation'),
                'max_exch_latency': _max_of(minute_rows, 'max_exch_confirmation'),
            }

        # ── Per-segment latency percentiles (P50 / P95 / P99) ────────────
        seg_latency = {}
        latency_by_segment = []      # for Latency tab table
        latency_percentiles_by_segment = {}
        latency_percentiles_exch_by_segment = {}

        for seg, row in daily_by_seg.items():
            if seg == 'ALL' or not row['order_count']:
                continue

            seg_entry = {
                'orders': row['order_count'],
                'p50': row['p50_oms_latency'],
                'avg': row['avg_oms_latency'],
                'p95': row['p95_oms_latency'],
                'p99': row['p99_oms_latency'],
                'max': row['max_oms_latency'],
            }
            seg_latency[seg] = seg_entry

            latency_by_segment.append({
                'segment': seg,
                'orders': row['order_count'],
                'avg_oms': seg_entry['avg'],
                'max_oms': seg_entry['max'],
            })
            latency_percentiles_by_segment[seg] = {
                'p50_oms': seg_entry['p50'],
                'p95_oms': seg_entry['p95'],
                'p99_oms': seg_entry['p99'],
            }
            latency_percentiles_exch_by_segment[seg] = {'p50_exch': row['p50_exch_confirmation']}

        # Overall P50/P95/P99 -- the true day-level median from the 'ALL' row,
        # not a mean of per-segment medians (which would weight a 500-order
        # segment the same as a 2M-order one).
        latency_percentiles = {
            'p50_oms': overall_daily['p50_oms_latency'] if overall_daily else None,
            'p95_oms': overall_daily['p95_oms_latency'] if overall_daily else None,
            'p99_oms': overall_daily['p99_oms_latency'] if overall_daily else None,
        }
        latency_percentiles_exch = {
            'p50_exch': overall_daily['p50_exch_confirmation'] if overall_daily else None,
        }
        overall_p50 = latency_percentiles['p50_oms']

        return HttpResponse(json.dumps({
            'status': 200,
            'file_date': target_date,
            'queue_stats': queue_stats,
            'latency_stats': latency_stats,
            'seg_latency': seg_latency,
            'total_data_points': total_data_points,
            'avg_all': avg_all,
            'overall_p50': overall_p50,
            # Latency tab fields
            'latency_percentiles': latency_percentiles,
            'latency_percentiles_exch': latency_percentiles_exch,
            'latency_by_segment': latency_by_segment,
            'latency_percentiles_by_segment': latency_percentiles_by_segment,
            'latency_percentiles_exch_by_segment': latency_percentiles_exch_by_segment,
        }), content_type="application/json")
    except Exception as e:
        return HttpResponse(json.dumps({'status': 500, 'error': str(e), 'trace': traceback.format_exc()}), content_type="application/json")


def messagequeue_latency_data(request):
    """
    Per-minute aggregated OMS + Exchange latency.
    Returns p50_oms, avg_oms, max_oms, p50_exch, avg_exch, max_exch, order_count per minute bucket.
    Used by both LatencyPage (Latency tab charts) and MQPage (latency overlay chart).
    """
    target_date = request.GET.get('file_date', '').strip()
    time_start, time_end = request.GET.get('time_start', '09:15').strip(), request.GET.get('time_end', '15:35').strip()

    try:
        if not target_date:
            target_date = latest_available_date()

        if not target_date:
            return HttpResponse(json.dumps({'status': 200, 'data': [], 'total_orders': 0}), content_type="application/json")

        target_date_obj = datetime.strptime(target_date, '%Y-%m-%d').date()

        # Per-minute rollup, written by the ETL -- not a live GROUP BY over
        # order_latency. p50 per minute was never available here even before
        # (MySQL 5.7 has no percentile function); real P50/P95/P99 come from
        # messagequeue_stats and feed the stat tiles.
        rows = list(
            OrderLatencyMinuteSummaryModel.objects
            .filter(file_date=target_date_obj, minute_bucket__gte=time_start, minute_bucket__lte=time_end)
            .order_by('minute_bucket')
            .values()
        )

        data = [{
            'time':        r['minute_bucket'],
            'order_count': r['order_count'],
            'avg_oms':     r['avg_oms_latency'] or 0,
            'max_oms':     r['max_oms_latency'] or 0,
            'avg_exch':    r['avg_exch_confirmation'] or 0,
            'max_exch':    r['max_exch_confirmation'] or 0,
        } for r in rows]

        total_orders = sum(d['order_count'] for d in data)

        overall = (
            OrderLatencyDailySummaryModel.objects
            .filter(file_date=target_date_obj, exch_seg='ALL')
            .values('histogram_labels', 'histogram_pct')
            .first()
        )
        hist_labels = overall['histogram_labels'] if overall else []
        hist_pct = overall['histogram_pct'] if overall else []

        return HttpResponse(json.dumps({
            'status': 200,
            'data': data,
            'total_orders': total_orders,
            'histogram': hist_pct,
            'histogram_labels': hist_labels,
        }), content_type="application/json")
    except Exception as e:
        return HttpResponse(json.dumps({'status': 500, 'error': str(e), 'trace': traceback.format_exc()}), content_type="application/json")


def messagequeue_csv_files(request):
    """List ORDERLATENCY_*.csv / QueSize_*.csv files latency.py's ETL reads from."""
    csv_dir = os.environ.get('MQ_CSV_DIR', '/data/noren_core/COZY_LOG_FILES/nfs_ps')
    try:
        if not os.path.isdir(csv_dir):
            return HttpResponse(json.dumps({'status': 200, 'files': [], 'count': 0}), content_type="application/json")
        files = []
        for fname in os.listdir(csv_dir):
            if not fname.endswith('.csv'):
                continue
            fpath = os.path.join(csv_dir, fname)
            try:
                files.append({'name': fname, 'size': os.path.getsize(fpath)})
            except OSError:
                continue
        files.sort(key=lambda f: f['name'], reverse=True)
        return HttpResponse(json.dumps({'status': 200, 'files': files, 'count': len(files)}), content_type="application/json")
    except Exception as e:
        return HttpResponse(json.dumps({'status': 500, 'error': str(e)}), content_type="application/json")


# ─── Raw Data APIs (Local Site Only) ─────────────────────────

@csrf_exempt
def get_order_latency(request):
    try:
        data = list(OrderLatencyModel.objects.order_by('-id')[:100].values())
        return HttpResponse(json.dumps({'status': 200, 'data': data}, default=str), content_type="application/json")
    except Exception as e:
        return HttpResponse(json.dumps({'status': 400, 'msg': str(e)}), content_type="application/json")

@csrf_exempt
def get_queue_line1(request):
    try:
        data = list(QueueLine1Model.objects.order_by('-id')[:100].values())
        return HttpResponse(json.dumps({'status': 200, 'data': data}, default=str), content_type="application/json")
    except Exception as e:
        return HttpResponse(json.dumps({'status': 400, 'msg': str(e)}), content_type="application/json")

@csrf_exempt
def get_queue_line2(request):
    try:
        data = list(QueueLine2Model.objects.order_by('-id')[:100].values())
        return HttpResponse(json.dumps({'status': 200, 'data': data}, default=str), content_type="application/json")
    except Exception as e:
        return HttpResponse(json.dumps({'status': 400, 'msg': str(e)}), content_type="application/json")
