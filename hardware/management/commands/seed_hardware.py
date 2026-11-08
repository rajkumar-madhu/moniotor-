"""Create (or refresh) a clearly-marked EXAMPLE physical server with a SYNTHETIC BMC state so the Hardware tab can be demoed.

Idempotent: running it again rebuilds only the example server's state. ``--remove`` deletes the example server again.

Everything is invented: the server name, the sensor values (a smooth day with a little noise), the log entries. The endpoint
is marked ``is_example``: it is never polled (no host is contacted, ever), never raises events, and carries no stored login. The
address is from the RFC 5737 documentation range. Never run automatically."""
import math
import random
from datetime import datetime, timedelta, timezone as tz

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from hardware import collector, parsers, services
from hardware.models import BmcEndpoint, HwComponent, HwEvent, HwReading
from hardware.tests import fixtures
from network.models import Device

NAME = "example-srv-01"
NOTE = "Example record created by manage.py seed_hardware: synthetic BMC data, no live polling."
STEP_RECENT_MIN, STEP_OLD_MIN = 1, 5      # one reading a minute for the last 6 hours, one per 5 minutes before that


def synthetic(seed, base, wobble, ts):
    rnd = random.Random(f"{seed}:{int(ts)}")
    day = math.sin(ts / 86400 * 2 * math.pi)
    return round(base + wobble * day + rnd.uniform(-wobble / 6, wobble / 6), 1)


class Command(BaseCommand):
    help = "Create (or remove with --remove) one example physical server with synthetic iDRAC data."

    def add_arguments(self, parser):
        parser.add_argument("--remove", action="store_true", help="Delete the example server instead.")
        parser.add_argument("--site", default="", help="Site name for the example server (default: All Sites).")
        parser.add_argument("--vendor", choices=["dell", "hpe"], default="dell")

    def handle(self, *args, remove=False, site="", vendor="dell", **opts):
        tenant = settings.LINKEDEYE_TENANT
        existing = Device.objects.filter(tenant_key=tenant, name__iexact=NAME).first()
        if existing is not None and not existing.is_example:
            self.stdout.write(self.style.WARNING(f"Skipped: a real device called {NAME} exists."))
            return
        if remove:
            n = 0
            if existing is not None:
                existing.delete()
                n = 1
            self.stdout.write(self.style.SUCCESS(f"Removed {n} example server."))
            return
        hpe = vendor == "hpe"
        fields = dict(device_type="physical_server", vendor="HPE" if hpe else "Dell", model="ProLiant DL380 Gen10" if hpe else "PowerEdge R740",
                      serial="EXAMPLE-0001", firmware="BMC 6.10.30.00" if not hpe else "iLO 5 v2.78", mgmt_ip="192.0.2.21", check_port=443,
                      site=site, role="Example server", environment="prod", tags="example, hardware", description=NOTE)
        with transaction.atomic():
            if existing is None:
                device = Device.objects.create(tenant_key=tenant, name=NAME, **fields)
            else:
                device = existing
                for k, v in fields.items():
                    setattr(device, k, v)
                device.save()
            ep, _ = BmcEndpoint.objects.get_or_create(device=device, defaults=dict(tenant_key=tenant, host="192.0.2.22"))
            BmcEndpoint.objects.filter(pk=ep.pk).update(is_example=True, host="192.0.2.22", port=443, enabled=True,
                                                         vendor_override="", credential_id=None, alert_state="")
            ep.refresh_from_db()
            docs = fixtures.hpe_ilo5() if hpe else fixtures.dell_idrac9()
            snap = parsers.parse(collector.collect(fixtures.FixtureClient(docs), inventory=True))
            now = timezone.now()
            for i, e in enumerate(snap.events):                 # the fixtures' dates are fixed: make the log recent
                e.ts = now - timedelta(minutes=17 + 53 * i)
            ep.inventory_at = None
            HwEvent.objects.filter(endpoint=ep).delete()           # a re-run (maybe with the other vendor) starts clean
            HwComponent.objects.filter(endpoint=ep).delete()
            services.apply_snapshot(ep, snap, now, inventory=True)
            HwComponent.objects.filter(endpoint=ep).update(updated_at=now)
            self._history(ep, now)
        self.stdout.write(self.style.SUCCESS(
            f"Example server {NAME} ready ({'HPE iLO' if hpe else 'Dell iDRAC'}, synthetic data, never polled). "
            f"Open it under Network Devices, Physical Servers, Hardware tab."))

    def _history(self, ep, now):
        """24 hours of synthetic readings (one per 5 minutes) so the trend charts have something to draw."""
        HwReading.objects.filter(component__endpoint=ep).delete()
        rows = []
        end = int(now.timestamp()) // 60 * 60
        stamps = [t for t in range(end - 24 * 3600, end + 1, 60) if t > end - 6 * 3600 or (t // 60) % STEP_OLD_MIN == 0]
        for c in HwComponent.objects.filter(endpoint=ep, kind__in=HwComponent.SERIES).exclude(reading=None):
            wobble = {"temperature": 2.5, "fan": 2 if c.unit == "%" else 350, "psu": 12, "voltage": 0.03}[c.kind]
            base = c.reading
            for ts in stamps:
                rows.append(HwReading(tenant_key=ep.tenant_key, component=c, ts=datetime.fromtimestamp(ts, tz=tz.utc),
                                      value=max(0.0, synthetic(c.pk, base, wobble, ts))))
        HwReading.objects.bulk_create(rows, batch_size=1000)
