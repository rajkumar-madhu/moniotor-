"""Give the example switch (`seed_network`) demo interfaces so the per-port page can be shown without a real exporter
(`manage.py seed_ports`).

The rows are DEMONSTRATION values, not measurements: they exist only on devices tagged "example" (a real device is never
touched), and the next real metrics collection for a device with a Prometheus instance replaces them. Idempotent;
`--remove` deletes them again. Pair it with a Prometheus (or a stub) that answers for instance 192.0.2.10 to see graphs."""
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from network.models import Device, Interface

DEVICE = "core-sw-01"
ALIASES = {1: "uplink to dist-a", 2: "uplink to dist-b", 5: "oms-db-01 (LACP)", 6: "oms-db-01 (LACP)", 9: "backup server",
           14: "hypervisor esx-host-01", 22: "printer vlan", 49: "to edge-rtr-01 (100G, QSFP28)", 50: "to FG-01 (100G, QSFP28)"}


def rows():
    out = []
    for n in range(1, 53):
        big = n > 48
        name = f"HundredGigE1/0/{n}" if big else f"TwentyFiveGigE1/0/{n}"
        admin = "down" if n in (30, 31, 44, 45, 46) else "up"
        oper = "down" if admin == "down" or n in (12, 27, 38) else "up"
        speed = 100000.0 if big else 25000.0
        busy = {1: 0.74, 2: 0.41, 5: 0.22, 6: 0.2, 9: 0.9, 14: 0.35, 49: 0.58, 50: 0.31}.get(n, 0.03 + (n % 7) * 0.02)
        in_mbps = round(speed * busy * 0.8, 2) if oper == "up" else None
        out_mbps = round(speed * busy * 0.55, 2) if oper == "up" else None
        util = round(100 * max(in_mbps, out_mbps) / speed, 1) if in_mbps is not None else None
        if n == 51:   # an aggregate (LACP) so the LAG membership card has something to show
            out.append(dict(if_index=n, name="Port-channel1", alias="oms-db-01 (LACP)", if_type="LAG", admin_status="up",
                            oper_status="up", speed_mbps=50000.0, in_mbps=410.0, out_mbps=300.0, utilization=0.8,
                            errors_per_s=0.0, discards_per_s=0.0, flaps_24h=0, mtu=9216, mac="00:1a:2b:3c:33:00", duplex="full"))
            continue
        out.append(dict(if_index=n, name=name, alias=ALIASES.get(n, ""), if_type="Ethernet", admin_status=admin, oper_status=oper,
                        speed_mbps=speed if oper == "up" else None, in_mbps=in_mbps, out_mbps=out_mbps, utilization=util,
                        errors_per_s=0.4 if n == 9 else 0.0 if oper == "up" else None,
                        discards_per_s=0.02 if n in (1, 9) else 0.0 if oper == "up" else None,
                        flaps_24h=3 if n in (12, 9) else 0, mtu=9216 if n in (1, 2, 5, 6, 49, 50) else 1500,
                        mac=f"00:1a:2b:3c:{n:02x}:01", duplex="full" if oper == "up" else ""))
    return out


class Command(BaseCommand):
    help = "Create (or remove with --remove) demo interfaces on the example switch core-sw-01."

    def add_arguments(self, parser):
        parser.add_argument("--remove", action="store_true", help="Delete the demo interfaces instead.")

    def handle(self, *args, remove=False, **opts):
        device = Device.objects.filter(tenant_key=settings.LINKEDEYE_TENANT, name__iexact=DEVICE).first()
        if device is None or not device.is_example:
            self.stdout.write(self.style.WARNING(f"No example device {DEVICE}: run manage.py seed_network first. Nothing changed."))
            return
        if remove:
            n, _ = Interface.objects.filter(device=device).delete()
            self.stdout.write(self.style.SUCCESS(f"Removed {n} demo interface(s) from {device.name}."))
            return
        now = timezone.now()
        for r in rows():
            changed = now - timedelta(hours=r["flaps_24h"] + 1) if r["flaps_24h"] else now - timedelta(days=9, hours=r["if_index"])
            Interface.objects.update_or_create(device=device, if_index=r["if_index"],
                                               defaults=dict(r, last_change_at=changed, updated_at=now))
        self.stdout.write(self.style.SUCCESS(f"{device.name}: 52 demo interfaces (demonstration values, not measurements)."))
