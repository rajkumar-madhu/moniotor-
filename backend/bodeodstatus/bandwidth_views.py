import requests
import json
import traceback
from datetime import datetime, timedelta
from django.db import connection, transaction
from django.http import HttpResponse, JsonResponse
from django.contrib.auth.decorators import login_required
from django.views.decorators.csrf import csrf_exempt

from .models import (
    ExchangeBandwidthModel,
    DeviceBandwidthModel,
    AwsDirectBandwidthModel, VpnBandwidthModel, WanBandwidthModel,
    InterfaceStatusModel, BandwidthDailySummaryModel
)

# ─── Data Retrieval Client (Logic from LAMA) ──────────────────────────

class PrometheusClient:
    def __init__(self, prometheus_url):
        self.base_url = prometheus_url.rstrip('/')
        self.query_url = f"{self.base_url}/api/v1/query"
        self.query_range_url = f"{self.base_url}/api/v1/query_range"

    def _get(self, url, params, timeout):
        # Authenticate to a Keycloak-protected Prometheus (oauth2-proxy). Bearer
        # token in bearer mode, legacy Basic Auth otherwise; retry once with a
        # fresh token if the proxy rejects a stale one.
        from lib.LinkedEyeMonitoring.token import monitoring_credentials, verify_ssl, bearer_mode
        auth, headers = monitoring_credentials('prometheus')
        resp = requests.get(url, params=params, auth=auth, headers=headers,
                            timeout=timeout, verify=verify_ssl())
        if bearer_mode() and resp.status_code in (401, 403, 302):
            auth, headers = monitoring_credentials('prometheus', force_refresh=True)
            resp = requests.get(url, params=params, auth=auth, headers=headers,
                                timeout=timeout, verify=verify_ssl())
        return resp

    def query(self, promql):
        try:
            response = self._get(self.query_url, {'query': promql}, timeout=30)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            return {'status': 'error', 'error': str(e)}

    def query_range(self, promql, start, end, step='5m'):
        try:
            response = self._get(
                self.query_range_url,
                {
                    'query': promql,
                    'start': start.isoformat() + 'Z',
                    'end': end.isoformat() + 'Z',
                    'step': step
                },
                timeout=60
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            return {'status': 'error', 'error': str(e)}

# ─── Helpers ─────────────────────────────────────────────────────────

def get_start_time_from_duration(duration_str):
    """
    Returns a datetime object representing the start time based on duration string
    e.g., '1h', '6h', '24h', '7d', '30d'
    """
    now = datetime.utcnow()
    try:
        if duration_str.endswith('h'):
            hours = int(duration_str[:-1])
            return now - timedelta(hours=hours)
        elif duration_str.endswith('d'):
            days = int(duration_str[:-1])
            return now - timedelta(days=days)
        elif duration_str.endswith('m'):
            minutes = int(duration_str[:-1])
            return now - timedelta(minutes=minutes)
    except:
        pass
    return now - timedelta(hours=24) # Default to 24h

# ─── API Endpoints ───────────────────────────────────────────────────

@login_required
def get_bandwidth_summary(request):
    """
    Get aggregated current bandwidth for all device categories for a specific site
    """
    site_name = request.GET.get('site')
    if not site_name:
        return JsonResponse({'status': 400, 'error': 'Site name is required'})

    try:
        duration = request.GET.get('duration', '24h')
        start_time = get_start_time_from_duration(duration)
        
        summary = {
            'exchange': {'rx': 0.0, 'tx': 0.0},
            'aws': {'rx': 0.0, 'tx': 0.0},
            'fortigate': {'rx': 0.0, 'tx': 0.0},
            'vpn': {'rx': 0.0, 'tx': 0.0},
        }

        with connection.cursor() as cursor:
            # Exchange
            cursor.execute("SELECT AVG(rx_bytes_per_sec), AVG(tx_bytes_per_sec) FROM lama_exchange_bandwidth WHERE site = %s AND timestamp >= %s", [site_name, start_time])
            row = cursor.fetchone()
            if row:
                summary['exchange']['rx'] = round((row[0] or 0) * 8 / 1e6, 2)
                summary['exchange']['tx'] = round((row[1] or 0) * 8 / 1e6, 2)

            # AWS Direct Connect
            cursor.execute("SELECT AVG(rx_bytes_per_sec), AVG(tx_bytes_per_sec) FROM lama_aws_direct_bandwidth WHERE site = %s AND timestamp >= %s", [site_name, start_time])
            row = cursor.fetchone()
            if row:
                summary['aws']['rx'] = round((row[0] or 0) * 8 / 1e6, 2)
                summary['aws']['tx'] = round((row[1] or 0) * 8 / 1e6, 2)

            # Firewall (Combined Device Table)
            cursor.execute("""
                SELECT AVG(rx_bytes_per_sec), AVG(tx_bytes_per_sec) 
                FROM lama_device_bandwidth 
                WHERE site = %s AND timestamp >= %s 
                AND model IN ('fortigate', 'cisco', 'huawei')
            """, [site_name, start_time])
            row = cursor.fetchone()
            if row:
                summary['fortigate']['rx'] = round((row[0] or 0) * 8 / 1e6, 2)
                summary['fortigate']['tx'] = round((row[1] or 0) * 8 / 1e6, 2)

            # VPN
            cursor.execute("SELECT AVG(rx_bytes_per_sec), AVG(tx_bytes_per_sec) FROM lama_vpn_bandwidth WHERE site = %s AND timestamp >= %s", [site_name, start_time])
            row = cursor.fetchone()
            if row:
                summary['vpn']['rx'] = round((row[0] or 0) * 8 / 1e6, 2)
                summary['vpn']['tx'] = round((row[1] or 0) * 8 / 1e6, 2)

        return JsonResponse({'status': 200, 'summary': summary, 'duration': duration, 'site': site_name})
    except Exception as e:
        return JsonResponse({'status': 500, 'error': str(e)})

@login_required
def get_interface_status(request):
    """
    Get latest status of all monitored interfaces for a specific site
    """
    site_name = request.GET.get('site')
    if not site_name:
        return JsonResponse({'status': 400, 'error': 'Site name is required'})

    try:
        latest = InterfaceStatusModel.objects.filter(site=site_name).order_by('-snapshot_time').first()
        if not latest:
            return JsonResponse({'status': 200, 'data': []})

        qs = InterfaceStatusModel.objects.filter(
            site=site_name,
            snapshot_time=latest.snapshot_time
        ).values(
            'id', 'site', 'source_type', 'interface', 'instance',
            'status', 'speed_mbps', 'rx_bytes_total', 'tx_bytes_total', 'snapshot_time'
        )

        data = []
        for item in qs:
            # Serialize datetime
            if item.get('snapshot_time'):
                item['snapshot_time'] = item['snapshot_time'].isoformat()
            # Add UI fields not stored in the model (defaults)
            item['priority'] = 'P1'
            item['uptime']   = '100%'
            data.append(item)

        return JsonResponse({'status': 200, 'data': data, 'snapshot_time': latest.snapshot_time.isoformat()})
    except Exception as e:
        return JsonResponse({'status': 500, 'error': str(e)})

@login_required
def get_bandwidth_trends(request):
    """
    Get traffic trends for a specific site and category
    """
    site_name = request.GET.get('site')
    category = request.GET.get('category', 'exchange')
    duration = request.GET.get('duration', '24h')
    start_time = get_start_time_from_duration(duration)
    
    if not site_name:
        return JsonResponse({'status': 400, 'error': 'Site name is required'})

    table_map = {
        'exchange': 'lama_exchange_bandwidth',
        'cisco': 'lama_device_bandwidth',
        'fortigate': 'lama_device_bandwidth',
        'huawei': 'lama_device_bandwidth',
        'aws': 'lama_aws_direct_bandwidth',
        'vpn': 'lama_vpn_bandwidth'
    }
    
    table = table_map.get(category, 'lama_exchange_bandwidth')
    model_filter = ""
    if table == 'lama_device_bandwidth':
        model_filter = f"AND model = '{category}'"
    
    try:
        with connection.cursor() as cursor:
            # Grouping interval depends on duration
            interval = '%%H:%%i' # Default (per minute)
            if 'd' in duration:
                interval = '%%m-%%d %%H:00' # Per hour for daily charts

            query = f"""
                SELECT DATE_FORMAT(timestamp, '{interval}') as bucket,
                       AVG(rx_bytes_per_sec) * 8 / 1000000 as rx_mbps,
                       AVG(tx_bytes_per_sec) * 8 / 1000000 as tx_mbps
                FROM {table}
                WHERE site = %s AND timestamp >= %s {model_filter}
                GROUP BY bucket
                ORDER BY bucket ASC
            """
            cursor.execute(query, [site_name, start_time])
            rows = cursor.fetchall()
            
            data = []
            for r in rows:
                data.append({
                    'time': r[0],
                    'rx': round(r[1] or 0, 3),
                    'tx': round(r[2] or 0, 3)
                })
                
        return JsonResponse({'status': 200, 'category': category, 'duration': duration, 'data': data})
    except Exception as e:
        return JsonResponse({'status': 500, 'error': str(e)})

from lesites.models import SiteModel

# ─── Data Sync Engine ───────────────────────────────────────────────

@csrf_exempt
def sync_prometheus_bandwidth(request):
    """
    Manual trigger to pull data from Prometheus and save to local LinkedEye DB.
    Usually called by a background celery task or cron job.
    """
    site_name = request.GET.get('site')
    if not site_name:
        return JsonResponse({'status': 400, 'error': 'Site name is required'})

    try:
        site_obj = SiteModel.objects.filter(sitename=site_name).first()
        if not site_obj or not site_obj.prometheus_url:
            return JsonResponse({'status': 404, 'error': f'Prometheus URL not found for site {site_name}'})
            
        PROMETHEUS_URL = site_obj.prometheus_url
        prom = PrometheusClient(PROMETHEUS_URL)
        end_time = datetime.utcnow()
        start_time = end_time - timedelta(minutes=15) # Last 15 min
        
        results = {
            'exchange': 'rate(node_network_receive_bytes_total{device=~"BSE.*|NSE.*|LAN_Bond"}[5m])',
            'cisco': 'rate(ifHCInOctets{product_model="cisco"}[5m])',
            'fortigate': 'rate(ifHCInOctets{product_model="fortigate"}[5m])',
            'huawei': 'rate(ifHCInOctets{product_model="huawei"}[5m])',
        }
        
        synced_count = 0
        
        for category, query in results.items():
            rx_data = prom.query_range(query, start_time, end_time, '1m')
            
            if rx_data.get('status') == 'success':
                with transaction.atomic():
                    for res in rx_data.get('data', {}).get('result', []):
                        metric = res.get('metric', {})
                        iface = metric.get('device', metric.get('ifName', 'unknown'))
                        inst = metric.get('instance', 'unknown')
                        
                        for ts, val in res.get('values', []):
                            dt = datetime.utcfromtimestamp(ts)
                            
                            if category == 'exchange':
                                ExchangeBandwidthModel.objects.update_or_create(
                                    site=site_name, timestamp=dt, interface=iface, instance=inst,
                                    defaults={'rx_bytes_per_sec': float(val), 'tx_bytes_per_sec': 0.0}
                                )
                            else:
                                # All other models go to DeviceBandwidthModel
                                DeviceBandwidthModel.objects.update_or_create(
                                    site=site_name, timestamp=dt, interface=iface, instance=inst, model=category,
                                    defaults={'rx_bytes_per_sec': float(val), 'tx_bytes_per_sec': 0.0}
                                )
                            synced_count += 1

        return JsonResponse({'status': 200, 'msg': 'Sync completed', 'records_synced': synced_count, 'site': site_name})
    except Exception as e:
        return JsonResponse({'status': 500, 'error': str(e), 'trace': traceback.format_exc()})
