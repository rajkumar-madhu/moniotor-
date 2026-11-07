"""
Management command to sync Prometheus alerts into IncidentModel (MySQL) per site.

Usage:
    python manage.py sync_prometheus_incidents           # Sync all sites once
    python manage.py sync_prometheus_incidents --loop 60 # Run every 60 seconds
    python manage.py sync_prometheus_incidents --site fs-le-isv  # One site only
"""

import time
import requests
from datetime import datetime
from django.core.management.base import BaseCommand
from lesites.models import SiteModel
from incidents.models import IncidentModel


class Command(BaseCommand):
    help = 'Sync Prometheus alerts to IncidentModel for each enabled site'

    def add_arguments(self, parser):
        parser.add_argument('--loop', type=int, default=0, help='Run every N seconds (0 = run once)')
        parser.add_argument('--site', type=str, default='', help='Sync only this site name')

    def handle(self, *args, **options):
        loop_interval = options['loop']
        site_filter = options['site']

        if loop_interval > 0:
            self.stdout.write(f'Running Prometheus sync every {loop_interval}s...')
            while True:
                self._sync_all(site_filter)
                time.sleep(loop_interval)
        else:
            self._sync_all(site_filter)

    def _sync_all(self, site_filter=''):
        if site_filter:
            sites = SiteModel.objects.filter(sitename=site_filter, is_enable=True)
        else:
            sites = SiteModel.objects.filter(is_enable=True)

        for site in sites:
            if not site.prometheus_url:
                continue
            try:
                self._sync_site(site)
            except Exception as e:
                self.stderr.write(f'[{site.sitename}] Error: {e}')

    def _sync_site(self, site):
        prom_url = site.prometheus_url.rstrip('/')
        alerts = self._fetch_prometheus_alerts(prom_url)
        if alerts is None:
            self.stderr.write(f'[{site.sitename}] Failed to fetch alerts from {prom_url}')
            return

        synced = 0
        resolved = 0
        firing_fingerprints = set()

        for alert in alerts:
            state = alert.get('state', '')
            labels = alert.get('labels', {})
            annotations = alert.get('annotations', {})

            alertname = labels.get('alertname', 'Unknown')
            severity = labels.get('severity', 'warning')
            instance = labels.get('instance', '')
            job = labels.get('job', '')
            fingerprint = alert.get('fingerprint', '')

            if not fingerprint:
                fingerprint = f"{alertname}_{instance}_{job}"

            firing_fingerprints.add(fingerprint)

            priority = self._severity_to_priority(severity)
            title = annotations.get('summary', annotations.get('description', f'{alertname} on {instance}'))[:200]
            description = self._build_description(alert)

            # Check if incident already exists for this fingerprint + site
            existing = IncidentModel.objects.filter(
                site_name=site.sitename,
                short_description__startswith=f'[{fingerprint}]'
            ).first()

            if not existing:
                # Also check by number pattern
                existing = IncidentModel.objects.filter(
                    site_name=site.sitename,
                    description__contains=fingerprint
                ).first()

            if existing:
                if state == 'firing' and existing.state in ('RESOLVED', 'CLOSED'):
                    existing.state = 'NEW'
                    existing.sla_breached = False
                    existing.resolved_at = None
                    existing.save()
                    synced += 1
                elif state != 'firing' and existing.state not in ('RESOLVED', 'CLOSED'):
                    existing.state = 'RESOLVED'
                    existing.resolved_at = datetime.now()
                    existing.save()
                    resolved += 1
            else:
                if state == 'firing':
                    # Generate next incident number for this site
                    last = IncidentModel.objects.filter(site_name=site.sitename).order_by('-id').first()
                    if last:
                        try:
                            last_num = int(last.number.replace('INC', ''))
                            new_number = f"INC{last_num + 1:07d}"
                        except:
                            new_number = f"INC{IncidentModel.objects.count() + 1:07d}"
                    else:
                        new_number = "INC0000001"

                    active_at = alert.get('activeAt', '')
                    hostname = labels.get('nodename', labels.get('hostname', ''))

                    IncidentModel.objects.create(
                        number=new_number,
                        short_description=title,
                        description=f"[{fingerprint}]\n{description}",
                        priority=priority,
                        state='NEW',
                        assigned_to_first_name=hostname if hostname else None,
                        assigned_to_last_name='',
                        site_name=site.sitename,
                        sla_breached=False,
                    )
                    synced += 1

        # Resolve incidents whose alerts are no longer firing
        stale = IncidentModel.objects.filter(
            site_name=site.sitename,
            state__in=['NEW', 'IN_PROGRESS', 'ON_HOLD', 'ESCALATED'],
        ).exclude(description='')

        for inc in stale:
            # Check if this incident's fingerprint is still firing
            fp_match = False
            if inc.description:
                for fp in firing_fingerprints:
                    if fp in inc.description:
                        fp_match = True
                        break
            if inc.description and inc.description.startswith('[') and not fp_match:
                # This is a Prometheus-created incident whose alert is gone
                inc.state = 'RESOLVED'
                inc.resolved_at = datetime.now()
                inc.save()
                resolved += 1

        self.stdout.write(f'[{site.sitename}] Synced {synced} new/updated, {resolved} resolved')

    def _fetch_prometheus_alerts(self, prom_url):
        # Authenticate to a Keycloak-protected Prometheus (oauth2-proxy). Bearer
        # token in bearer mode, legacy Basic Auth otherwise; retry once with a
        # fresh token if the proxy rejects a stale one.
        from lib.LinkedEyeMonitoring.token import monitoring_credentials, verify_ssl, bearer_mode
        url = f'{prom_url}/api/v1/alerts'
        try:
            auth, headers = monitoring_credentials('prometheus')
            resp = requests.get(url, auth=auth, headers=headers, timeout=10, verify=verify_ssl())
            if bearer_mode() and resp.status_code in (401, 403, 302):
                auth, headers = monitoring_credentials('prometheus', force_refresh=True)
                resp = requests.get(url, auth=auth, headers=headers, timeout=10, verify=verify_ssl())
            if resp.status_code == 200:
                data = resp.json()
                if data.get('status') == 'success':
                    return data.get('data', {}).get('alerts', [])
            return None
        except Exception as e:
            print(f'Prometheus fetch error: {e}')
            return None

    def _severity_to_priority(self, severity):
        mapping = {
            'critical': 'P1',
            'error': 'P1',
            'high': 'P2',
            'warning': 'P2',
            'medium': 'P3',
            'info': 'P4',
            'low': 'P4',
            'none': 'P4',
        }
        return mapping.get(severity.lower(), 'P3')

    def _build_description(self, alert):
        labels = alert.get('labels', {})
        annotations = alert.get('annotations', {})

        lines = []
        lines.append(f"Auto-created from alert: {labels.get('alertname', 'Unknown')}")
        lines.append('')

        if annotations.get('summary'):
            lines.append(f"Summary: {annotations['summary']}")
        if annotations.get('description'):
            lines.append(f"Description: {annotations['description']}")

        lines.append('')
        lines.append(f"Severity: {labels.get('severity', 'unknown')}")
        lines.append(f"Instance: {labels.get('instance', 'N/A')}")
        lines.append(f"Job: {labels.get('job', 'N/A')}")

        if labels.get('nodename'):
            lines.append(f"Hostname: {labels['nodename']}")

        return '\n'.join(lines)
