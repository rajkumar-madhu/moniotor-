"""Create clearly-marked example IPAM data (description "Example – …"). Idempotent; never runs automatically.

Uses documentation and private ranges only (10.80.0.0/16, 192.168.180.0/22, 192.0.2.0/24, 198.51.100.0/24,
2001:db8:80::/48) and example.* names.
No last-seen times are invented: example addresses show "never seen" until a real sync reports them.
"""
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from ipam import services
from ipam.models import DNSRecord, DNSZone, IPAddress, Role, Source, Subnet, VLAN
from ipam.sync import link_record_ip

GLOBAL = "All Sites"
VLANS = [(10, "EX-SERVERS", "Example – server VLAN"), (20, "EX-USERS", "Example – user access VLAN"),
         (30, "EX-MGMT", "Example – out-of-band management"), (40, "EX-VOICE", "Example – IP phones")]
SUBNETS = [
    # cidr, vrf, role, vlan vid, gateway, description
    ("10.80.0.0/16", "", Role.CONTAINER, None, None, "Example – campus supernet"),
    ("10.80.1.0/24", "", Role.SERVER, 10, "10.80.1.1", "Example – application servers"),
    ("10.80.2.0/24", "", Role.USER, 20, "10.80.2.1", "Example – office clients (DHCP)"),
    ("10.80.3.0/27", "", Role.MANAGEMENT, 30, "10.80.3.1", "Example – switch and firewall management"),
    ("10.80.4.0/22", "", Role.VOICE, 40, "10.80.4.1", "Example – voice"),
    ("10.80.8.0/30", "", Role.TRANSIT, None, None, "Example – WAN link to DR"),
    ("192.168.180.0/22", "EX-CUST-A", Role.CONTAINER, None, None, "Example – customer A VRF range"),
    ("192.168.180.0/24", "EX-CUST-A", Role.SERVER, None, "192.168.180.1", "Example – customer A servers"),
    # RFC 5737 documentation ranges used by seed_network's example devices, so "Sync from devices" has something to
    # place; 203.0.113.0/24 is deliberately left out and shows up as "outside every subnet" under Conflicts.
    ("192.0.2.0/24", "", Role.MANAGEMENT, 30, None, "Example – management network of the example devices"),
    ("198.51.100.0/24", "", Role.TRANSIT, None, None, "Example – WAN / edge addresses of the example devices"),
    ("2001:db8:80::/48", "", Role.CONTAINER, None, None, "Example – IPv6 campus allocation"),
    ("2001:db8:80:1::/64", "", Role.SERVER, 10, "2001:db8:80:1::1", "Example – IPv6 application servers"),
]
ADDRESSES = [
    # address, vrf, status, hostname
    ("10.80.1.10", "", "active", "app01.example.internal"), ("10.80.1.11", "", "active", "app02.example.internal"),
    ("10.80.1.12", "", "active", "db01.example.internal"), ("10.80.1.13", "", "reserved", ""),
    ("10.80.1.20", "", "active", "web01.example.internal"), ("10.80.1.21", "", "deprecated", "web-old.example.internal"),
    ("10.80.3.2", "", "active", "ex-core-sw01.example.internal"), ("10.80.3.3", "", "active", "ex-fw01.example.internal"),
    ("10.80.3.4", "", "reserved", ""), ("10.80.8.1", "", "active", "ex-wan-a.example.internal"),
    ("10.80.8.2", "", "active", "ex-wan-b.example.internal"),
    ("192.168.180.10", "EX-CUST-A", "active", "cust-a-app.example.internal"),
    ("2001:db8:80:1::10", "", "active", "app01.example.internal"),
] + [(f"10.80.2.{n}", "", "dhcp", "") for n in range(100, 140)] + [(f"10.80.3.{n}", "", "active", f"ex-sw{n:02d}.example.internal") for n in range(10, 28)]
ZONES = [("example.internal", "forward", "Example – internal forward zone"),
         ("1.80.10.in-addr.arpa", "reverse", "Example – reverse zone for 10.80.1.0/24")]
RECORDS = [
    ("example.internal", "app01", "A", "10.80.1.10"), ("example.internal", "app02", "A", "10.80.1.11"),
    ("example.internal", "db01", "A", "10.80.1.12"), ("example.internal", "web01", "A", "10.80.1.20"),
    ("example.internal", "app01", "AAAA", "2001:db8:80:1::10"), ("example.internal", "www", "CNAME", "web01.example.internal"),
    ("example.internal", "@", "MX", "10 mail.example.internal"), ("example.internal", "@", "TXT", "v=spf1 -all"),
    ("example.internal", "_ldap._tcp", "SRV", "0 100 389 dc01.example.internal"),
    ("example.internal", "legacy", "A", "10.80.1.99"),   # deliberately unallocated: shows up under Conflicts
    ("1.80.10.in-addr.arpa", "10", "PTR", "app01.example.internal"),
    ("1.80.10.in-addr.arpa", "11", "PTR", "app02.example.internal"),
]


class Command(BaseCommand):
    help = "Create example IPAM VLANs, subnets, addresses and DNS zones (description 'Example – …'; idempotent)."

    def add_arguments(self, parser):
        parser.add_argument("--remove", action="store_true", help="Delete the example IPAM records again.")

    def handle(self, *args, remove=False, **opts):
        t = settings.LINKEDEYE_TENANT
        if remove:
            with transaction.atomic():
                n = DNSZone.objects.filter(tenant_key=t, name__in=[z[0] for z in ZONES], description__startswith="Example").delete()[0]
                n += IPAddress.objects.filter(tenant_key=t, description__startswith="Example").delete()[0]
                n += Subnet.objects.filter(tenant_key=t, description__startswith="Example").delete()[0]
                n += VLAN.objects.filter(tenant_key=t, description__startswith="Example").delete()[0]
            self.stdout.write(self.style.SUCCESS(f"Removed example IPAM data ({n} row(s))."))
            return
        made = {"vlans": 0, "subnets": 0, "addresses": 0, "zones": 0, "records": 0}
        with transaction.atomic():
            vlans = {}
            for vid, name, desc in VLANS:
                v, c = VLAN.objects.get_or_create(tenant_key=t, site=GLOBAL, vid=vid, defaults={"name": name, "description": desc})
                vlans[vid] = v
                made["vlans"] += c
            for cidr, vrf, role, vid, gw, desc in SUBNETS:
                s, c = Subnet.objects.get_or_create(tenant_key=t, vrf=vrf, cidr=cidr, defaults={
                    "site": GLOBAL, "role": role, "vlan": vlans.get(vid), "gateway": gw, "description": desc})
                made["subnets"] += c
            for addr, vrf, status, host in ADDRESSES:
                if IPAddress.objects.filter(tenant_key=t, vrf=vrf, address=addr).exists():
                    continue
                subnet = services.subnet_for(addr, vrf, t)
                if subnet is None:
                    continue
                IPAddress(tenant_key=t, address=addr, vrf=vrf, subnet=subnet, site=subnet.site, status=status,
                          hostname=host, source=Source.MANUAL, description="Example – seed_ipam").save()
                made["addresses"] += 1
            zones = {}
            for name, kind, desc in ZONES:
                z, c = DNSZone.objects.get_or_create(tenant_key=t, name=name, defaults={"kind": kind, "site": GLOBAL, "description": desc})
                zones[name] = z
                made["zones"] += c
            for zname, name, rtype, value in RECORDS:
                zone = zones[zname]
                if DNSRecord.objects.filter(zone=zone, name=name, rtype=rtype, value=value).exists():
                    continue
                r = DNSRecord(tenant_key=t, zone=zone, name=name, rtype=rtype, value=value, description="Example – seed_ipam")
                r.ip = link_record_ip(r)
                r.save()
                made["records"] += 1
        self.stdout.write(self.style.SUCCESS("Example IPAM data: " + ", ".join(f"{v} {k}" for k, v in made.items()) + " created."))
