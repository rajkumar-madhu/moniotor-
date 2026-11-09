import csv
import io
import ipaddress
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from cmdb.models import ConfigurationItem
from core import jobs
from core.models import AuditEvent, Event, Membership, Site
from network.models import Device, DiscoveredHost, DiscoveryRun

from . import csvio, dns, services, sync
from .forms import DNSRecordForm, DNSZoneForm, IPAddressForm, SubnetForm, VLANForm
from .models import DNSRecord, DNSZone, IPAddress, Role, Subnet, SyncRun, VLAN, ip_key

T = "default"
MUM, DR = "Mumbai (NSE)", "DR Site"


def subnet(cidr, vrf="", site="All Sites", role="", **kw):
    return Subnet.objects.create(tenant_key=T, cidr=cidr, vrf=vrf, site=site, role=role, **kw)


def addr(address, vrf="", status="active", **kw):
    s = services.subnet_for(address, vrf, T)
    a = IPAddress(tenant_key=T, address=address, vrf=vrf, subnet=s, site=s.site if s else "All Sites", status=status, **kw)
    a.save()
    return a


def device(name, ip, site="", **kw):
    return Device.objects.create(tenant_key=T, name=name, mgmt_ip=ip, site=site, **kw)


class Users:
    def make_users(self):
        self.mum = Site.objects.create(tenant_key=T, name=MUM, code="mum")
        self.dr = Site.objects.create(tenant_key=T, name=DR, code="dr")
        self.viewer = User.objects.create_user("viewer", password="pw")
        Membership.objects.create(user=self.viewer, site=None, role="viewer")
        self.operator = User.objects.create_user("operator", password="pw")
        Membership.objects.create(user=self.operator, site=None, role="operator")
        self.admin = User.objects.create_user("admin", password="pw")
        Membership.objects.create(user=self.admin, site=None, role="admin")
        self.dr_op = User.objects.create_user("dr-op", password="pw")
        Membership.objects.create(user=self.dr_op, site=self.dr, role="operator")
        self.dr_admin = User.objects.create_user("dr-admin", password="pw")
        Membership.objects.create(user=self.dr_admin, site=self.dr, role="admin")


# ------------------------------------------------------------------ models & arithmetic
class ModelTests(TestCase):
    def test_subnet_normalised_and_range_keys(self):
        s = subnet("2001:DB8:0:0::/64")
        self.assertEqual(s.cidr, "2001:db8::/64")
        self.assertEqual(s.version, 6)
        self.assertEqual(s.prefixlen, 64)
        self.assertEqual(len(s.net_start), 32)
        self.assertEqual(s.size, 2 ** 64)
        self.assertEqual(s.usable, 2 ** 64 - 1)
        v4 = subnet("10.0.0.0/24")
        self.assertEqual((v4.size, v4.usable), (256, 254))
        self.assertEqual(subnet("10.0.1.0/31").usable, 2)
        self.assertEqual(subnet("10.0.1.4/32").usable, 1)
        self.assertLess(v4.net_start, v4.net_end)

    def test_huge_ipv6_never_enumerated(self):
        s = subnet("2001:db8::/32", role=Role.CONTAINER)
        self.assertEqual(s.size, 2 ** 96)
        self.assertEqual(services.fmt_count(s.size), "2^96")
        self.assertEqual(str(services.next_free(s)), "2001:db8::1")
        self.assertLess(services.utilisation(s, 5), 0.0001)
        amap, note = services.address_map(s)
        self.assertIsNone(amap)
        self.assertIn("IPv6", note)

    def test_unique_constraints_include_tenant(self):
        subnet("10.0.0.0/24")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Subnet.objects.create(tenant_key=T, cidr="10.0.0.0/24")
        Subnet.objects.create(tenant_key="other", cidr="10.0.0.0/24")      # another tenant may reuse it
        subnet("10.0.0.0/24", vrf="RED")                                   # and another VRF too
        VLAN.objects.create(tenant_key=T, site=MUM, vid=10, name="a")
        VLAN.objects.create(tenant_key=T, site=DR, vid=10, name="b")
        with self.assertRaises(IntegrityError), transaction.atomic():
            VLAN.objects.create(tenant_key=T, site=MUM, vid=10, name="c")
        a = addr("10.0.0.5")
        with self.assertRaises(IntegrityError), transaction.atomic():
            IPAddress(tenant_key=T, address="10.0.0.5", subnet=a.subnet).save()
        IPAddress(tenant_key="other", address="10.0.0.5").save()

    def test_tenant_default_is_callable(self):
        field = Subnet._meta.get_field("tenant_key")
        self.assertTrue(callable(field.default))
        self.assertEqual(field.default(), T)
        for model in (VLAN, IPAddress, DNSZone, DNSRecord, SyncRun):
            self.assertTrue(callable(model._meta.get_field("tenant_key").default), model)

    def test_address_normalised_and_site_from_subnet(self):
        s = subnet("2001:db8::/64", site=MUM)
        a = addr("2001:DB8:0::0010")
        self.assertEqual(a.address, "2001:db8::10")
        self.assertEqual(a.addr_key, ip_key(ipaddress.ip_address("2001:db8::10")))
        self.assertEqual(a.subnet, s)
        self.assertEqual(a.site, MUM)

    def test_record_value_hash(self):
        z = DNSZone.objects.create(tenant_key=T, name="example.test")
        r = DNSRecord.objects.create(tenant_key=T, zone=z, name="www", rtype="A", value="10.0.0.1")
        self.assertEqual(len(r.value_hash), 64)
        with self.assertRaises(IntegrityError), transaction.atomic():
            DNSRecord.objects.create(tenant_key=T, zone=z, name="www", rtype="A", value="10.0.0.1")


class PlacementTests(TestCase):
    def test_overlap_rules(self):
        subnet("10.0.0.0/24")
        err = services.placement_error(T, "", ipaddress.ip_network("10.0.0.0/24"), "")
        self.assertIn("already exists", err)
        err = services.placement_error(T, "", ipaddress.ip_network("10.0.0.128/25"), "")
        self.assertIn("overlaps 10.0.0.0/24", err)
        err = services.placement_error(T, "", ipaddress.ip_network("10.0.0.0/16"), "")
        self.assertIn("Container", err)
        self.assertIsNone(services.placement_error(T, "", ipaddress.ip_network("10.0.0.0/16"), Role.CONTAINER))
        self.assertIsNone(services.placement_error(T, "", ipaddress.ip_network("10.0.1.0/24"), ""))
        self.assertIsNone(services.placement_error(T, "BLUE", ipaddress.ip_network("10.0.0.0/24"), ""))
        self.assertIsNone(services.placement_error("other", "", ipaddress.ip_network("10.0.0.0/24"), ""))

    def test_nesting_in_container_and_v6(self):
        subnet("2001:db8::/48", role=Role.CONTAINER)
        self.assertIsNone(services.placement_error(T, "", ipaddress.ip_network("2001:db8:0:1::/64"), ""))
        subnet("2001:db8:0:1::/64")
        self.assertIn("overlaps", services.placement_error(T, "", ipaddress.ip_network("2001:db8:0:1::/80"), ""))

    def test_most_specific_subnet_and_reassign(self):
        sup = subnet("10.0.0.0/16", role=Role.CONTAINER, site=MUM)
        a = addr("10.0.1.20")
        self.assertEqual(a.subnet, sup)
        child = subnet("10.0.1.0/24", site=DR)
        changed = services.reassign(T, "", 4, child.net_start, child.net_end)
        self.assertEqual(changed, 1)
        a.refresh_from_db()
        self.assertEqual((a.subnet, a.site), (child, DR))
        start, end = child.net_start, child.net_end
        child.delete()
        services.reassign(T, "", 4, start, end)
        a.refresh_from_db()
        self.assertEqual((a.subnet, a.site), (sup, MUM))

    def test_next_free_skips_used_gateway_and_children(self):
        s = subnet("10.1.0.0/29", gateway="10.1.0.1")
        self.assertEqual(str(services.next_free(s)), "10.1.0.2")
        for n in range(2, 6):
            addr(f"10.1.0.{n}")
        self.assertEqual(str(services.next_free(s)), "10.1.0.6")
        addr("10.1.0.6")
        self.assertIsNone(services.next_free(s))      # .7 is the broadcast address
        c = subnet("10.2.0.0/16", role=Role.CONTAINER)
        subnet("10.2.0.0/24")
        self.assertEqual(str(services.next_free(c)), "10.2.1.0")
        p2p = subnet("10.3.0.0/31")
        self.assertEqual(str(services.next_free(p2p)), "10.3.0.0")

    def test_used_counts_and_utilisation(self):
        c = subnet("10.0.0.0/16", role=Role.CONTAINER)
        leaf = subnet("10.0.1.0/24")
        for n in range(1, 11):
            addr(f"10.0.1.{n}")
        addr("10.0.9.9")
        used = services.used_counts([c, leaf])
        self.assertEqual(used, {c.pk: 11, leaf.pk: 10})
        self.assertAlmostEqual(services.utilisation(leaf, 10), 100 * 10 / 254)
        self.assertEqual(services.ipv4_capacity(Subnet.objects.all()), (10, 254))

    def test_address_map_modes(self):
        s = subnet("10.4.0.0/24", gateway="10.4.0.1")
        a = addr("10.4.0.5", status="reserved")
        amap, _ = services.address_map(s, can_add=True)
        self.assertEqual(amap["mode"], "address")
        self.assertEqual(len(amap["cells"]), 256)
        kinds = {c["ip"]: c["kind"] for c in amap["cells"]}
        self.assertEqual(kinds["10.4.0.0"], "net")
        self.assertEqual(kinds["10.4.0.255"], "net")
        self.assertEqual(kinds["10.4.0.1"], "gateway")
        self.assertEqual(kinds["10.4.0.5"], "reserved")
        self.assertEqual(next(c for c in amap["cells"] if c["ip"] == "10.4.0.5")["pk"], a.pk)
        self.assertTrue(next(c for c in amap["cells"] if c["ip"] == "10.4.0.9")["add"])
        big = subnet("10.8.0.0/22")
        self.assertEqual(len(services.address_map(big)[0]["cells"]), 1024)
        blocks = subnet("10.16.0.0/20", role=Role.CONTAINER)
        addr("10.16.3.4")
        bmap, _ = services.address_map(blocks)
        self.assertEqual(bmap["mode"], "block")
        self.assertEqual(len(bmap["cells"]), 16)
        self.assertEqual(bmap["cells"][3]["used"], 1)
        huge = subnet("10.32.0.0/12", role=Role.CONTAINER)
        self.assertIsNone(services.address_map(huge)[0])


# ------------------------------------------------------------------ DNS helpers
class DNSTests(TestCase):
    def test_zone_names(self):
        self.assertEqual(dns.zone_name("Corp.Example.COM.", "forward"), "corp.example.com")
        self.assertEqual(dns.zone_name("10.1.2.0/24", "reverse"), "2.1.10.in-addr.arpa")
        self.assertEqual(dns.zone_name("2001:db8::/32", "reverse"), "8.b.d.0.1.0.0.2.ip6.arpa")
        for bad, kind in [("1.10.in-addr.arpa", "forward"), ("example.com", "reverse"), ("10.1.2.0/25", "reverse"),
                          ("bad_-.com", "forward"), ("-x.com", "forward"), ("300.10.in-addr.arpa", "reverse"), ("", "forward")]:
            with self.assertRaises(dns.DNSError, msg=bad):
                dns.zone_name(bad, kind)

    def test_record_values(self):
        self.assertEqual(dns.record_value("A", " 10.0.0.1 "), "10.0.0.1")
        self.assertEqual(dns.record_value("AAAA", "2001:DB8::1"), "2001:db8::1")
        self.assertEqual(dns.record_value("MX", "10 Mail.Example.com."), "10 mail.example.com")
        self.assertEqual(dns.record_value("SRV", "0 5 5060 sip.example.com"), "0 5 5060 sip.example.com")
        for rtype, bad in [("A", "2001:db8::1"), ("A", "10.0.0.256"), ("AAAA", "10.0.0.1"), ("CNAME", "10.0.0.1"),
                           ("MX", "mail.example.com"), ("MX", "70000 mail.example.com"), ("SRV", "1 2 3"),
                           ("TXT", "x" * 501), ("TXT", "a\nb"), ("PTR", "")]:
            with self.assertRaises(dns.DNSError, msg=(rtype, bad)):
                dns.record_value(rtype, bad)

    def test_ptr_helpers(self):
        self.assertEqual(str(dns.ptr_address("20", "2.1.10.in-addr.arpa")), "10.1.2.20")
        self.assertIsNone(dns.ptr_address("2", "1.10.in-addr.arpa"))
        self.assertEqual(dns.ptr_name(ipaddress.ip_address("10.1.2.20"), "2.1.10.in-addr.arpa"), "20")
        self.assertIsNone(dns.ptr_name(ipaddress.ip_address("10.9.2.20"), "2.1.10.in-addr.arpa"))
        v6zone = dns.zone_name("2001:db8::/32", "reverse")
        name = dns.ptr_name(ipaddress.ip_address("2001:db8::5"), v6zone)
        self.assertEqual(str(dns.ptr_address(name, v6zone)), "2001:db8::5")
        self.assertEqual(dns.record_name("WWW.example.com.", "example.com"), "www")
        self.assertEqual(dns.record_name("example.com.", "example.com"), "@")
        self.assertEqual(dns.record_name("*.dev", "example.com"), "*.dev")
        with self.assertRaises(dns.DNSError):
            dns.record_name("www.other.org.", "example.com")


# ------------------------------------------------------------------ forms
class FormTests(Users, TestCase):
    def setUp(self):
        self.make_users()

    def test_subnet_form_validation(self):
        subnet("10.0.0.0/24")
        cases = [
            ({"cidr": "10.0.0.5/24"}, "cidr", "Host bits are set; the network is 10.0.0.0/24"),
            ({"cidr": "10.0.0.0"}, "cidr", "prefix length"),
            ({"cidr": "nonsense/8"}, "cidr", "not a valid"),
            ({"cidr": "10.0.0.0/25"}, "cidr", "overlaps"),
            ({"cidr": "10.0.0.0/24"}, "cidr", "already exists"),
            ({"cidr": "10.0.0.0/7"}, "cidr", "/8 or smaller"),
            ({"cidr": "10.1.0.0/24", "gateway": "10.2.0.1"}, "gateway", "inside"),
            ({"cidr": "10.1.0.0/24", "gateway": "10.1.0.255"}, "gateway", "broadcast"),
            ({"cidr": "10.1.0.0/24", "gateway": "2001:db8::1"}, "gateway", "inside"),
            ({"cidr": "2001:db8::/64", "gateway": "2001:db8::"}, "gateway", "anycast"),
            ({"cidr": "10.1.0.0/24", "vrf": "bad vrf!"}, "vrf", "letters"),
        ]
        for data, field, text in cases:
            f = SubnetForm(dict({"site": "All Sites", "vrf": "", "gateway": ""}, **data), user=self.admin)
            self.assertFalse(f.is_valid(), data)
            self.assertIn(text, " ".join(f.errors[field]), data)
        f = SubnetForm({"cidr": "2001:db8:1::/64", "site": MUM, "vrf": "", "gateway": "2001:db8:1::1"}, user=self.admin)
        self.assertTrue(f.is_valid(), f.errors)
        s = f.save()
        self.assertEqual((s.gateway, s.site, s.version), ("2001:db8:1::1", MUM, 6))

    def test_subnet_form_site_permission_and_vlan_site(self):
        f = SubnetForm({"cidr": "10.5.0.0/24", "site": MUM}, user=self.dr_op)
        self.assertFalse(f.is_valid())
        self.assertIn("site", f.errors)
        f = SubnetForm({"cidr": "10.5.0.0/24", "site": "All Sites"}, user=self.dr_op)
        self.assertFalse(f.is_valid())            # global records need a tenant-wide role
        self.assertTrue(SubnetForm({"cidr": "10.5.0.0/24", "site": DR}, user=self.dr_op).is_valid())
        v = VLAN.objects.create(tenant_key=T, site=MUM, vid=5, name="m")
        f = SubnetForm({"cidr": "10.5.0.0/24", "site": DR, "vlan": v.pk}, user=self.admin)
        self.assertFalse(f.is_valid())
        self.assertIn("vlan", f.errors)

    def test_subnet_edit_guards_addresses(self):
        s = subnet("10.6.0.0/24")
        addr("10.6.0.200")
        f = SubnetForm({"cidr": "10.6.0.0/25", "site": "All Sites", "vrf": ""}, instance=s, user=self.admin)
        self.assertFalse(f.is_valid())
        self.assertIn("would fall outside", f.errors["cidr"][0])
        f = SubnetForm({"cidr": "10.6.0.0/24", "site": "All Sites", "vrf": "RED"}, instance=s, user=self.admin)
        self.assertFalse(f.is_valid())
        self.assertIn("vrf", f.errors)
        c = subnet("10.7.0.0/16", role=Role.CONTAINER)
        subnet("10.7.1.0/24")
        f = SubnetForm({"cidr": "10.7.0.0/16", "site": "All Sites", "role": "server"}, instance=c, user=self.admin)
        self.assertFalse(f.is_valid())             # cannot stop being a container while it holds subnets

    def test_address_form_validation(self):
        s = subnet("10.0.0.0/24")
        subnet("10.0.0.0/24", vrf="RED")
        addr("10.0.0.7")
        cases = [("10.0.0.0", "", "network address"), ("10.0.0.255", "", "broadcast"), ("10.9.0.1", "", "No subnet"),
                 ("10.0.0.7", "", "already recorded"), ("10.0.0.300", "", "not a valid"), ("10.0.0.1/24", "", "prefix"),
                 ("fe80::1%eth0", "", "Zone identifiers")]
        for value, vrf, text in cases:
            f = IPAddressForm({"address": value, "vrf": vrf, "status": "active"}, user=self.admin)
            self.assertFalse(f.is_valid(), value)
            self.assertIn(text, " ".join(f.errors["address"]), value)
        f = IPAddressForm({"address": "10.0.0.7", "vrf": "RED", "status": "active"}, user=self.admin)
        self.assertTrue(f.is_valid(), f.errors)    # unique per VRF only
        f = IPAddressForm({"address": "10.0.0.8", "vrf": "", "status": "bogus"}, user=self.admin)
        self.assertFalse(f.is_valid())
        f = IPAddressForm({"address": "10.0.0.8", "vrf": "", "status": "active", "hostname": "bad host"}, user=self.admin)
        self.assertFalse(f.is_valid())
        f = IPAddressForm({"address": "10.0.0.8", "vrf": "", "status": "dhcp", "hostname": "Host.Example.com."}, user=self.admin)
        self.assertTrue(f.is_valid(), f.errors)
        a = f.save()
        self.assertEqual((a.subnet, a.hostname), (s, "host.example.com"))

    def test_address_form_hides_foreign_subnets_and_devices(self):
        subnet("10.10.0.0/24", site=MUM)
        d = device("mum-sw", "10.10.0.2", site=MUM)
        f = IPAddressForm({"address": "10.10.0.9", "vrf": "", "status": "active"}, user=self.dr_op)
        self.assertFalse(f.is_valid())
        self.assertIn("No subnet", f.errors["address"][0])
        self.assertNotIn(d, f.fields["device"].queryset)

    def test_vlan_form(self):
        VLAN.objects.create(tenant_key=T, site=DR, vid=100, name="x")
        for vid in (0, 4095, -1):
            self.assertFalse(VLANForm({"vid": vid, "name": "n", "site": DR}, user=self.admin).is_valid(), vid)
        f = VLANForm({"vid": 100, "name": "n", "site": DR}, user=self.admin)
        self.assertFalse(f.is_valid())
        self.assertIn("already exists", f.errors["vid"][0])
        self.assertTrue(VLANForm({"vid": 100, "name": "n", "site": MUM}, user=self.admin).is_valid())
        self.assertFalse(VLANForm({"vid": 101, "name": "  ", "site": DR}, user=self.admin).is_valid())

    def test_zone_and_record_forms(self):
        f = DNSZoneForm({"name": "10.20.30.0/24", "kind": "reverse", "site": "All Sites", "default_ttl": 3600}, user=self.admin)
        self.assertTrue(f.is_valid(), f.errors)
        rz = f.save(commit=False)
        rz.name = f.cleaned_data["name"]
        rz.save()
        self.assertEqual(rz.name, "30.20.10.in-addr.arpa")
        self.assertFalse(DNSZoneForm({"name": "30.20.10.in-addr.arpa", "kind": "reverse", "site": "All Sites",
                                      "default_ttl": 3600}, user=self.admin).is_valid())
        self.assertFalse(DNSZoneForm({"name": "x.test", "kind": "forward", "site": "All Sites", "default_ttl": 0},
                                     user=self.admin).is_valid())
        fz = DNSZone.objects.create(tenant_key=T, name="corp.test")
        ok = DNSRecordForm({"name": "www", "rtype": "A", "value": "10.0.0.1"}, zone=fz)
        self.assertTrue(ok.is_valid(), ok.errors)
        bad = [({"name": "www", "rtype": "A", "value": "2001:db8::1"}, "value"),
               ({"name": "www", "rtype": "PTR", "value": "x.test"}, "rtype"),
               ({"name": "@", "rtype": "CNAME", "value": "x.test"}, "name"),
               ({"name": "bad name", "rtype": "A", "value": "10.0.0.1"}, "name"),
               ({"name": "www", "rtype": "A", "value": "10.0.0.1", "ttl": 0}, "ttl")]
        for data, field in bad:
            f = DNSRecordForm(data, zone=fz)
            self.assertFalse(f.is_valid(), data)
            self.assertIn(field, f.errors, data)
        DNSRecord.objects.create(tenant_key=T, zone=fz, name="www", rtype="A", value="10.0.0.1")
        self.assertIn("already exists", DNSRecordForm({"name": "www", "rtype": "A", "value": "10.0.0.1"}, zone=fz).errors["value"][0])
        self.assertIn("name", DNSRecordForm({"name": "www", "rtype": "CNAME", "value": "a.test"}, zone=fz).errors)
        ptr = DNSRecordForm({"name": "10.20.30.40", "rtype": "PTR", "value": "host.corp.test"}, zone=rz)
        self.assertTrue(ptr.is_valid(), ptr.errors)
        self.assertEqual(ptr.cleaned_data["name"], "40")
        outside = DNSRecordForm({"name": "10.99.30.40", "rtype": "PTR", "value": "host.corp.test"}, zone=rz)
        self.assertFalse(outside.is_valid())
        self.assertFalse(DNSRecordForm({"name": "40", "rtype": "A", "value": "10.0.0.1"}, zone=rz).is_valid())


# ------------------------------------------------------------------ sync & conflicts
class SyncTests(Users, TestCase):
    def setUp(self):
        self.make_users()
        self.mgmt = subnet("10.50.0.0/24", site=MUM)
        self.dr_net = subnet("10.60.0.0/24", site=DR)

    def test_sync_creates_updates_and_reports(self):
        seen = timezone.now() - timedelta(minutes=5)
        d1 = device("sw1", "10.50.0.2", site=MUM, last_seen=seen)
        device("sw2", "10.99.0.2", site=MUM)                       # outside every subnet
        device("sw3", "core.example.com", site=MUM)                # host name, not an IP
        existing = addr("10.50.0.3", hostname="")
        device("sw4", "10.50.0.3", site=MUM)
        run = DiscoveryRun.objects.create(tenant_key=T, cidr="10.60.0.0/24", site=DR, status="done")
        DiscoveredHost.objects.create(run=run, ip="10.60.0.9")
        DiscoveredHost.objects.create(run=run, ip="10.60.0.10", status="ignored")
        r = sync.sync(trigger="manual", actor_name="t")
        self.assertEqual(r.created, 2)
        self.assertEqual(r.updated, 1)
        a = IPAddress.objects.get(address="10.50.0.2")
        self.assertEqual((a.device, a.hostname, a.status, a.source, a.last_seen), (d1, "sw1", "active", "device", seen))
        self.assertEqual(IPAddress.objects.get(address="10.60.0.9").source, "discovery")
        self.assertFalse(IPAddress.objects.filter(address="10.60.0.10").exists())
        self.assertFalse(IPAddress.objects.filter(address="10.99.0.2").exists())
        existing.refresh_from_db()
        self.assertEqual((existing.device.name, existing.hostname), ("sw4", "sw4"))
        self.assertEqual(r.report["not_ip"], 1)
        self.assertEqual([o["address"] for o in r.report["outside"]], ["10.99.0.2"])
        again = sync.sync()
        self.assertEqual((again.created, again.updated), (0, 0))
        self.assertEqual(again.new_conflicts, 0)

    def test_sync_respects_operator_sites(self):
        device("mum", "10.50.0.2", site=MUM)
        device("dr", "10.60.0.2", site=DR)
        r = sync.sync(user=self.dr_op)
        self.assertEqual(r.sites, [DR])
        self.assertTrue(IPAddress.objects.filter(address="10.60.0.2").exists())
        self.assertFalse(IPAddress.objects.filter(address="10.50.0.2").exists())

    def test_duplicate_device_is_reported_not_overwritten(self):
        a = device("a", "10.50.0.5", site=MUM)
        device("b", "10.50.0.5", site=MUM)
        r = sync.sync()
        self.assertEqual(r.created, 1)
        self.assertEqual(IPAddress.objects.get(address="10.50.0.5").device, a)
        self.assertEqual(len(r.report["duplicate"]), 1)

    def test_conflicts(self):
        device("a", "10.50.0.5", site=MUM)
        device("b", "10.50.0.5", site=MUM)
        device("c", "172.16.0.1", site=DR)
        z = DNSZone.objects.create(tenant_key=T, name="corp.test")
        DNSRecord.objects.create(tenant_key=T, zone=z, name="ghost", rtype="A", value="10.50.0.77")
        addr("10.50.0.88")
        DNSRecord.objects.create(tenant_key=T, zone=z, name="real", rtype="A", value="10.50.0.88")
        orphan = IPAddress(tenant_key=T, address="192.0.2.1")
        orphan.save()
        kinds = sorted(c["kind"] for c in sync.conflicts())
        self.assertEqual(kinds, ["dns_free", "duplicate", "orphan", "outside"])
        dr_kinds = sorted(c["kind"] for c in sync.conflicts(user=self.dr_op))
        self.assertEqual(dr_kinds, ["dns_free", "orphan", "outside"])   # global zone + orphan, DR device only

    def test_job_emits_warning_for_new_conflicts_only(self):
        self.assertIn("ipam.sync", jobs.JOBS)
        self.assertEqual(jobs.JOBS["ipam.sync"]["interval"], 3600)
        device("x", "172.16.0.1", site=MUM)
        summary = sync.run_job()
        self.assertIn("1 conflicts (1 new)", summary)
        ev = Event.objects.get(source="ipam")
        self.assertEqual(ev.severity, "warning")
        sync.run_job()
        self.assertEqual(Event.objects.filter(source="ipam").count(), 1)
        device("y", "172.16.0.2", site=MUM)
        sync.run_job()
        self.assertEqual(Event.objects.filter(source="ipam").count(), 2)

    def test_job_never_raises(self):
        from unittest import mock
        with mock.patch("ipam.sync.sync", side_effect=RuntimeError("boom")), self.assertLogs("linkedeye.ipam", "ERROR"):
            self.assertEqual(sync.run_job(), "error")

    def test_record_links(self):
        a = addr("10.50.0.20")
        z = DNSZone.objects.create(tenant_key=T, name="corp.test")
        r = DNSRecord(tenant_key=T, zone=z, name="h", rtype="A", value="10.50.0.20")
        self.assertEqual(sync.link_record_ip(r), a)
        r2 = DNSRecord.objects.create(tenant_key=T, zone=z, name="later", rtype="A", value="10.50.0.21")
        b = addr("10.50.0.21")
        self.assertEqual(sync.relink_records(T, ["10.50.0.21"]), 1)
        r2.refresh_from_db()
        self.assertEqual(r2.ip, b)
        rz = DNSZone.objects.create(tenant_key=T, name="0.50.10.in-addr.arpa", kind="reverse")
        self.assertEqual(sync.link_record_ip(DNSRecord(tenant_key=T, zone=rz, name="20", rtype="PTR", value="h.corp.test")), a)


# ------------------------------------------------------------------ CSV
class CSVTests(Users, TestCase):
    def setUp(self):
        self.make_users()
        subnet("10.70.0.0/24", site=MUM)
        subnet("10.80.0.0/24", site=DR)
        subnet("2001:db8:70::/64", site=MUM)
        self.c = Client()

    def upload(self, text, **extra):
        return self.c.post(reverse("ipam:addresses_import"),
                           {"file": SimpleUploadedFile("a.csv", text.encode(), content_type="text/csv"), **extra})

    def test_csv_safe(self):
        self.assertEqual(csvio.csv_safe("=1+1"), "'=1+1")
        self.assertEqual(csvio.csv_safe("@SUM(A1)"), "'@SUM(A1)")
        self.assertEqual(csvio.csv_safe("-5"), "-5")
        self.assertEqual(csvio.csv_safe("+cmd|"), "'+cmd|")
        self.assertEqual(csvio.csv_safe(None), "")

    def test_export_is_formula_safe_and_scoped(self):
        addr("10.70.0.5", hostname="", description="=HYPERLINK(\"http://x\")")
        addr("10.80.0.5")
        self.c.force_login(self.dr_op)
        r = self.c.get(reverse("ipam:addresses_export"))
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertNotIn("10.70.0.5", body)
        self.assertIn("10.80.0.5", body)
        self.c.force_login(self.viewer)
        rows = list(csv.reader(io.StringIO(self.c.get(reverse("ipam:addresses_export")).content.decode())))
        self.assertEqual(rows[0][:3], ["address", "vrf", "status"])
        row = next(r for r in rows if r[0] == "10.70.0.5")
        self.assertTrue(row[4].startswith("'="))
        for name in ("ipam:subnets_export", "ipam:vlans_export", "ipam:zones_export", "ipam:conflicts_export"):
            self.assertEqual(self.c.get(reverse(name)).status_code, 200, name)

    def test_import_all_or_nothing(self):
        self.c.force_login(self.operator)
        r = self.upload("address,status,hostname\n10.70.0.10,active,a.example.com\n10.99.0.1,active,\n10.70.0.11,bogus,\n")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Nothing was imported")
        self.assertContains(r, "Line 3")
        self.assertContains(r, "Line 4")
        self.assertFalse(IPAddress.objects.exists())
        r = self.upload("Address,Status,Hostname,Description\n10.70.0.10,active,a.example.com,=evil()\n"
                        "2001:db8:70::10,reserved,,\n10.70.0.12,,,\n")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(IPAddress.objects.count(), 3)
        self.assertEqual(IPAddress.objects.get(address="10.70.0.12").status, "active")
        self.assertEqual(IPAddress.objects.get(address="2001:db8:70::10").source, "import")
        self.assertTrue(AuditEvent.objects.filter(action="import").exists())

    def test_import_duplicates_update_and_permissions(self):
        addr("10.70.0.10", hostname="old.example.com")
        self.c.force_login(self.operator)
        r = self.upload("address,hostname\n10.70.0.10,new.example.com\n")
        self.assertContains(r, "already exists")
        r = self.upload("address,hostname\n10.70.0.20,\n10.70.0.20,\n")
        self.assertContains(r, "more than once")
        r = self.upload("address,hostname\n10.70.0.10,new.example.com\n", update_existing="on")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(IPAddress.objects.get(address="10.70.0.10").hostname, "new.example.com")
        r = self.upload("address,bogus\n10.70.0.30,x\n")
        self.assertContains(r, "Unknown column")
        r = self.upload("hostname\nx\n")
        self.assertContains(r, "needs an “address” column")
        self.c.force_login(self.dr_op)
        r = self.upload("address\n10.70.0.40\n")
        self.assertContains(r, "No subnet")                     # MUM subnet is invisible to a DR operator
        r = self.upload("address\n10.80.0.40\n")
        self.assertEqual(r.status_code, 302)
        r = self.c.post(reverse("ipam:addresses_import"), {"file": SimpleUploadedFile("a.csv", b"\xff\xfe\x00", content_type="text/csv")})
        self.assertContains(r, "not UTF-8")

    def test_import_links_device_and_ci(self):
        d = device("sw", "10.70.0.2", site=MUM)
        ci = ConfigurationItem.objects.create(tenant_key=T, name="app01", site=MUM)
        self.c.force_login(self.operator)
        r = self.upload("address,device,ci\n10.70.0.2,SW,app01\n")
        self.assertEqual(r.status_code, 302)
        a = IPAddress.objects.get(address="10.70.0.2")
        self.assertEqual((a.device, a.ci), (d, ci))
        r = self.upload("address,device\n10.70.0.3,nope\n")
        self.assertContains(r, "No network device")

    def test_viewer_cannot_import(self):
        self.c.force_login(self.viewer)
        self.assertEqual(self.c.get(reverse("ipam:addresses_import")).status_code, 403)
        r = self.upload("address\n10.70.0.9\n")
        self.assertEqual(r.status_code, 302)
        self.assertFalse(IPAddress.objects.exists())


# ------------------------------------------------------------------ views
@override_settings(LINKEDEYE_TENANT=T)
class ViewTests(Users, TestCase):
    def setUp(self):
        self.make_users()
        self.vlan = VLAN.objects.create(tenant_key=T, site=MUM, vid=10, name="SRV")
        self.sup = subnet("10.90.0.0/16", role=Role.CONTAINER, site=MUM, description="campus")
        self.net = subnet("10.90.1.0/24", site=MUM, vlan=self.vlan, gateway="10.90.1.1")
        self.dr_net = subnet("10.91.0.0/24", site=DR)
        self.v6 = subnet("2001:db8:90::/64", site=MUM)
        self.a = addr("10.90.1.10", hostname="app.example.com")
        self.dr_a = addr("10.91.0.10")
        self.zone = DNSZone.objects.create(tenant_key=T, name="corp.test", site=MUM)
        self.rec = DNSRecord.objects.create(tenant_key=T, zone=self.zone, name="app", rtype="A", value="10.90.1.10", ip=self.a)
        self.dr_zone = DNSZone.objects.create(tenant_key=T, name="dr.test", site=DR)
        self.dr_rec = DNSRecord.objects.create(tenant_key=T, zone=self.dr_zone, name="x", rtype="A", value="10.91.0.99")
        self.run = sync.sync()
        self.c = Client()

    def pages(self):
        return [reverse("ipam:overview"), reverse("ipam:subnets"), reverse("ipam:addresses"), reverse("ipam:vlans"),
                reverse("ipam:dns"), reverse("ipam:conflicts"), reverse("ipam:subnet_detail", args=[self.net.pk]),
                reverse("ipam:subnet_detail", args=[self.sup.pk]), reverse("ipam:subnet_detail", args=[self.v6.pk]),
                reverse("ipam:address_detail", args=[self.a.pk]), reverse("ipam:zone_detail", args=[self.zone.pk]),
                reverse("ipam:sync_run", args=[self.run.pk]), reverse("ipam:zone_records_export", args=[self.zone.pk]),
                reverse("ipam:subnets_export"), reverse("ipam:addresses_export"), reverse("ipam:vlans_export"),
                reverse("ipam:zones_export"), reverse("ipam:conflicts_export")]

    def forms(self):
        return [reverse("ipam:subnet_create"), reverse("ipam:subnet_edit", args=[self.net.pk]), reverse("ipam:address_create"),
                reverse("ipam:address_edit", args=[self.a.pk]), reverse("ipam:vlan_create"),
                reverse("ipam:vlan_edit", args=[self.vlan.pk]), reverse("ipam:zone_create"),
                reverse("ipam:zone_edit", args=[self.zone.pk]), reverse("ipam:record_create", args=[self.zone.pk]),
                reverse("ipam:record_edit", args=[self.rec.pk]), reverse("ipam:addresses_import")]

    def test_nav_names_resolve(self):
        for name in ("ipam:overview", "ipam:subnets", "ipam:addresses", "ipam:vlans", "ipam:dns"):
            self.assertTrue(reverse(name).startswith("/ipam/"))

    def test_login_required(self):
        r = self.c.get(reverse("ipam:overview"))
        self.assertEqual(r.status_code, 302)

    def test_viewer_reads_everything_but_cannot_change(self):
        self.c.force_login(self.viewer)
        for url in self.pages():
            self.assertEqual(self.c.get(url).status_code, 200, url)
        for url in self.forms():
            self.assertEqual(self.c.get(url).status_code, 403, url)
        r = self.c.get(reverse("ipam:subnets"))
        self.assertContains(r, "10.90.1.0/24")
        self.assertNotContains(r, "Add subnet")
        r = self.c.post(reverse("ipam:subnet_delete", args=[self.net.pk]))
        self.assertEqual(r.status_code, 302)
        self.assertTrue(Subnet.objects.filter(pk=self.net.pk).exists())
        r = self.c.post(reverse("ipam:sync"))
        self.assertEqual(SyncRun.objects.count(), 1)

    def test_operator_forms_render_and_admin_actions_denied(self):
        self.c.force_login(self.operator)
        for url in self.forms():
            self.assertEqual(self.c.get(url).status_code, 200, url)
        r = self.c.get(reverse("ipam:subnets"))
        self.assertContains(r, "Add subnet")
        self.assertNotContains(r, "Delete subnet 10.90.1.0/24")
        for url in (reverse("ipam:subnet_delete", args=[self.net.pk]), reverse("ipam:address_delete", args=[self.a.pk]),
                    reverse("ipam:vlan_delete", args=[self.vlan.pk]), reverse("ipam:zone_delete", args=[self.zone.pk]),
                    reverse("ipam:record_delete", args=[self.rec.pk])):
            self.c.post(url)
        self.assertTrue(Subnet.objects.filter(pk=self.net.pk).exists())
        self.assertTrue(IPAddress.objects.filter(pk=self.a.pk).exists())
        self.assertTrue(VLAN.objects.filter(pk=self.vlan.pk).exists())
        self.assertTrue(DNSRecord.objects.filter(pk=self.rec.pk).exists())

    def test_404_for_unknown_and_foreign_objects(self):
        self.c.force_login(self.admin)
        for name in ("ipam:subnet_detail", "ipam:address_detail", "ipam:zone_detail", "ipam:sync_run", "ipam:subnet_edit",
                     "ipam:address_edit", "ipam:vlan_edit", "ipam:zone_edit", "ipam:record_edit", "ipam:record_create",
                     "ipam:zone_records_export"):
            self.assertEqual(self.c.get(reverse(name, args=[999999])).status_code, 404, name)
        self.c.force_login(self.dr_op)
        for url in (reverse("ipam:subnet_detail", args=[self.net.pk]), reverse("ipam:address_detail", args=[self.a.pk]),
                    reverse("ipam:zone_detail", args=[self.zone.pk]), reverse("ipam:subnet_edit", args=[self.net.pk]),
                    reverse("ipam:record_edit", args=[self.rec.pk]), reverse("ipam:vlan_edit", args=[self.vlan.pk])):
            self.assertEqual(self.c.get(url).status_code, 404, url)
        self.assertEqual(self.c.post(reverse("ipam:subnet_reserve", args=[self.net.pk])).status_code, 404)

    def test_foreign_site_user_sees_only_their_site(self):
        self.c.force_login(self.dr_op)
        r = self.c.get(reverse("ipam:subnets"))
        self.assertContains(r, "10.91.0.0/24")
        self.assertNotContains(r, "10.90.1.0/24")
        r = self.c.get(reverse("ipam:addresses"))
        self.assertContains(r, "10.91.0.10")
        self.assertNotContains(r, "10.90.1.10")
        r = self.c.get(reverse("ipam:dns"))
        self.assertContains(r, "dr.test")
        self.assertNotContains(r, "corp.test")
        r = self.c.get(reverse("ipam:vlans"))
        self.assertNotContains(r, "SRV")
        r = self.c.get(reverse("ipam:overview"))
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, "10.90.1.10")
        self.assertEqual(self.c.get(reverse("ipam:subnet_edit", args=[self.dr_net.pk])).status_code, 200)
        # cannot move a DR subnet to Mumbai
        r = self.c.post(reverse("ipam:subnet_edit", args=[self.dr_net.pk]), {"cidr": "10.91.0.0/24", "site": MUM, "vrf": ""})
        self.assertEqual(r.status_code, 200)
        self.dr_net.refresh_from_db()
        self.assertEqual(self.dr_net.site, DR)
        # but can edit it in place
        r = self.c.post(reverse("ipam:subnet_edit", args=[self.dr_net.pk]),
                        {"cidr": "10.91.0.0/24", "site": DR, "vrf": "", "description": "dr servers"})
        self.assertEqual(r.status_code, 302)
        # and cannot delete (operator)
        self.c.post(reverse("ipam:subnet_delete", args=[self.dr_net.pk]))
        self.assertTrue(Subnet.objects.filter(pk=self.dr_net.pk).exists())
        # a DR admin can delete DR objects only
        self.c.force_login(self.dr_admin)
        r = self.c.post(reverse("ipam:address_delete", args=[self.dr_a.pk]))
        self.assertEqual(r.status_code, 302)
        self.assertFalse(IPAddress.objects.filter(pk=self.dr_a.pk).exists())
        self.assertEqual(self.c.post(reverse("ipam:address_delete", args=[self.a.pk])).status_code, 404)

    def test_global_records_need_tenant_wide_role(self):
        g = subnet("10.95.0.0/24")                      # All Sites
        self.c.force_login(self.dr_op)
        self.assertEqual(self.c.get(reverse("ipam:subnet_detail", args=[g.pk])).status_code, 200)
        self.assertEqual(self.c.get(reverse("ipam:subnet_edit", args=[g.pk])).status_code, 403)
        r = self.c.post(reverse("ipam:subnet_reserve", args=[g.pk]))
        self.assertEqual(r.status_code, 302)
        self.assertFalse(IPAddress.objects.filter(subnet=g).exists())

    def test_get_never_mutates(self):
        self.c.force_login(self.admin)
        before = (Subnet.objects.count(), IPAddress.objects.count(), SyncRun.objects.count(), AuditEvent.objects.count())
        for url in [reverse("ipam:sync"), reverse("ipam:subnet_reserve", args=[self.net.pk]),
                    reverse("ipam:subnet_delete", args=[self.net.pk]), reverse("ipam:address_delete", args=[self.a.pk])]:
            self.assertEqual(self.c.get(url).status_code, 405, url)
        for url in self.pages():
            self.c.get(url)
        after = (Subnet.objects.count(), IPAddress.objects.count(), SyncRun.objects.count(), AuditEvent.objects.count())
        self.assertEqual(before, after)

    def test_csrf_enforced(self):
        c = Client(enforce_csrf_checks=True)
        c.force_login(self.admin)
        self.assertEqual(c.post(reverse("ipam:subnet_delete", args=[self.net.pk])).status_code, 403)
        self.assertEqual(c.post(reverse("ipam:sync")).status_code, 403)

    def test_subnet_crud_with_audit(self):
        self.c.force_login(self.admin)
        r = self.c.post(reverse("ipam:subnet_create"), {"cidr": "10.90.2.0/24", "site": MUM, "vrf": "", "role": "server",
                                                          "gateway": "10.90.2.1", "vlan": self.vlan.pk, "description": "new"})
        self.assertEqual(r.status_code, 302)
        s = Subnet.objects.get(cidr="10.90.2.0/24")
        self.assertTrue(AuditEvent.objects.filter(action="create", object_type="ipam.subnet", object_id=str(s.pk)).exists())
        r = self.c.post(reverse("ipam:subnet_create"), {"cidr": "10.90.2.128/25", "site": MUM, "vrf": ""})
        self.assertContains(r, "overlaps", status_code=200)
        # creating a child moves the container's addresses into it
        mid = addr("10.90.3.7")
        self.assertEqual(mid.subnet, self.sup)
        self.c.post(reverse("ipam:subnet_create"), {"cidr": "10.90.3.0/24", "site": DR, "vrf": ""})
        mid.refresh_from_db()
        self.assertEqual((mid.subnet.cidr, mid.site), ("10.90.3.0/24", DR))
        r = self.c.post(reverse("ipam:subnet_edit", args=[s.pk]), {"cidr": "10.90.2.0/24", "site": MUM, "vrf": "",
                                                                    "description": "renamed"})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(AuditEvent.objects.filter(action="update", object_id=str(s.pk)).exists())
        r = self.c.post(reverse("ipam:subnet_delete", args=[self.net.pk]))
        self.assertEqual(r.status_code, 302)
        self.a.refresh_from_db()
        self.assertEqual(self.a.subnet, self.sup)          # fell back to the container
        self.assertTrue(AuditEvent.objects.filter(action="delete", object_type="ipam.subnet").exists())

    def test_subnet_detail_map_and_next_free(self):
        self.c.force_login(self.operator)
        r = self.c.get(reverse("ipam:subnet_detail", args=[self.net.pk]))
        self.assertContains(r, "Address Map")
        self.assertContains(r, "10.90.1.2")             # next free (gateway .1 skipped)
        self.assertContains(r, reverse("ipam:address_detail", args=[self.a.pk]))
        r = self.c.get(reverse("ipam:subnet_detail", args=[self.sup.pk]))
        self.assertContains(r, "One cell per /24 block")
        self.assertContains(r, "10.90.1.0/24")           # child subnet listed
        r = self.c.get(reverse("ipam:subnet_detail", args=[self.v6.pk]))
        self.assertContains(r, "IPv6 subnets have no address map")
        r = self.c.post(reverse("ipam:subnet_reserve", args=[self.net.pk]), {"hostname": "x"})
        self.assertEqual(r.status_code, 302)
        res = IPAddress.objects.get(address="10.90.1.2")
        self.assertEqual(res.status, "reserved")
        self.assertTrue(AuditEvent.objects.filter(action="create", object_id=str(res.pk)).exists())

    def test_full_subnet_emits_warning(self):
        small = subnet("10.97.0.0/29", site=MUM)
        for n in range(1, 6):
            addr(f"10.97.0.{n}")
        self.c.force_login(self.operator)
        r = self.c.post(reverse("ipam:subnet_reserve", args=[small.pk]))
        self.assertEqual(r.status_code, 302)
        self.assertTrue(Event.objects.filter(fingerprint=f"ipam:subnet-full:{small.pk}", severity="warning").exists())
        r = self.c.post(reverse("ipam:subnet_reserve", args=[small.pk]))
        self.assertEqual(IPAddress.objects.filter(subnet=small).count(), 6)

    def test_address_crud(self):
        self.c.force_login(self.operator)
        d = device("app-dev", "10.90.1.50", site=MUM)
        ci = ConfigurationItem.objects.create(tenant_key=T, name="app-ci", site=MUM)
        r = self.c.post(reverse("ipam:address_create"), {"address": "10.90.1.50", "vrf": "", "status": "active",
                                                           "hostname": "app2.example.com", "device": d.pk, "ci": ci.pk})
        self.assertEqual(r.status_code, 302)
        a = IPAddress.objects.get(address="10.90.1.50")
        self.assertEqual((a.subnet, a.device, a.ci, a.site), (self.net, d, ci, MUM))
        r = self.c.get(reverse("ipam:addresses") + "?linked=device")
        self.assertContains(r, "app-dev")
        self.assertContains(r, "app-ci")
        r = self.c.post(reverse("ipam:address_edit", args=[a.pk]), {"address": "10.90.1.50", "vrf": "", "status": "deprecated"})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(Event.objects.filter(fingerprint=f"ipam:address:{a.pk}").exists())
        r = self.c.post(reverse("ipam:address_create"), {"address": "10.90.1.50", "vrf": "", "status": "active"})
        self.assertContains(r, "already recorded")
        self.c.force_login(self.admin)
        r = self.c.post(reverse("ipam:address_delete", args=[a.pk]))
        self.assertEqual(r.status_code, 302)
        self.assertFalse(IPAddress.objects.filter(pk=a.pk).exists())

    def test_address_filters(self):
        addr("10.90.1.11", status="reserved")
        self.c.force_login(self.viewer)
        base = reverse("ipam:addresses")
        r = self.c.get(base + "?status=reserved")
        self.assertContains(r, "10.90.1.11")
        self.assertNotContains(r, "10.90.1.10<")
        r = self.c.get(base + f"?subnet={self.net.pk}")
        self.assertNotContains(r, "10.91.0.10")
        r = self.c.get(base + "?q=app.example")
        self.assertContains(r, "10.90.1.10")
        r = self.c.get(base + f"?site={DR}")
        self.assertContains(r, "10.91.0.10")
        self.assertNotContains(r, "10.90.1.11")
        r = self.c.get(base + "?within=10.91.0.0/24")
        self.assertContains(r, "10.91.0.10")
        self.assertNotContains(r, "10.90.1.11")
        r = self.c.get(base + "?within=garbage&per=abc&page=99")
        self.assertEqual(r.status_code, 200)
        r = self.c.get(reverse("ipam:subnets") + "?q=10.90.1.77")
        self.assertContains(r, "10.90.1.0/24")
        self.assertNotContains(r, "10.91.0.0/24")
        r = self.c.get(reverse("ipam:subnets") + "?version=6")
        self.assertContains(r, "2001:db8:90::/64")
        self.assertNotContains(r, "10.90.1.0/24")
        r = self.c.get(reverse("ipam:subnets") + "?vrf=__global__&role=container")
        self.assertContains(r, "10.90.0.0/16")

    def test_vlan_crud_and_counts(self):
        self.c.force_login(self.operator)
        r = self.c.get(reverse("ipam:vlans"))
        self.assertContains(r, f'?vlan={self.vlan.pk}">1</a>')
        r = self.c.post(reverse("ipam:vlan_create"), {"vid": 20, "name": "USERS", "site": MUM})
        self.assertEqual(r.status_code, 302)
        r = self.c.post(reverse("ipam:vlan_create"), {"vid": 20, "name": "dup", "site": MUM})
        self.assertContains(r, "already exists")
        r = self.c.post(reverse("ipam:vlan_create"), {"vid": 5000, "name": "x", "site": MUM})
        self.assertEqual(r.status_code, 200)
        v = VLAN.objects.get(vid=20)
        r = self.c.post(reverse("ipam:vlan_edit", args=[v.pk]), {"vid": 21, "name": "USERS", "site": MUM})
        self.assertEqual(r.status_code, 302)
        self.c.force_login(self.admin)
        self.c.post(reverse("ipam:vlan_delete", args=[self.vlan.pk]))
        self.net.refresh_from_db()
        self.assertIsNone(self.net.vlan)

    def test_zone_and_record_crud(self):
        self.c.force_login(self.operator)
        r = self.c.post(reverse("ipam:zone_create"), {"name": "10.90.1.0/24", "kind": "reverse", "site": MUM, "default_ttl": 3600})
        self.assertEqual(r.status_code, 302)
        rz = DNSZone.objects.get(name="1.90.10.in-addr.arpa")
        r = self.c.post(reverse("ipam:record_create", args=[rz.pk]), {"name": "10.90.1.10", "rtype": "PTR", "value": "app.example.com"})
        self.assertEqual(r.status_code, 302)
        ptr = DNSRecord.objects.get(zone=rz)
        self.assertEqual((ptr.name, ptr.ip), ("10", self.a))
        r = self.c.post(reverse("ipam:record_create", args=[self.zone.pk]), {"name": "new", "rtype": "AAAA", "value": "2001:db8:90::5"})
        self.assertEqual(r.status_code, 302)
        r = self.c.post(reverse("ipam:record_create", args=[self.zone.pk]), {"name": "new2", "rtype": "A", "value": "not-ip"})
        self.assertContains(r, "IPv4 address")
        r = self.c.post(reverse("ipam:record_edit", args=[self.rec.pk]), {"name": "app", "rtype": "A", "value": "10.90.1.11", "ttl": 300})
        self.assertEqual(r.status_code, 302)
        self.rec.refresh_from_db()
        self.assertIsNone(self.rec.ip)
        r = self.c.get(reverse("ipam:zone_detail", args=[self.zone.pk]))
        self.assertContains(r, "free address")
        r = self.c.get(reverse("ipam:zone_records_export", args=[self.zone.pk]))
        self.assertIn("10.90.1.11", r.content.decode())
        self.c.force_login(self.admin)
        self.c.post(reverse("ipam:record_delete", args=[self.rec.pk]))
        self.assertFalse(DNSRecord.objects.filter(pk=self.rec.pk).exists())
        self.c.post(reverse("ipam:zone_delete", args=[self.zone.pk]))
        self.assertFalse(DNSZone.objects.filter(pk=self.zone.pk).exists())

    def test_sync_view_and_conflicts_page(self):
        device("dup1", "10.90.1.60", site=MUM)
        device("dup2", "10.90.1.60", site=MUM)
        device("outside", "192.0.2.50", site=MUM)
        self.c.force_login(self.operator)
        r = self.c.post(reverse("ipam:sync"))
        run = SyncRun.objects.order_by("-pk").first()
        self.assertRedirects(r, reverse("ipam:sync_run", args=[run.pk]))
        self.assertEqual(run.actor_name, "operator")
        self.assertTrue(AuditEvent.objects.filter(action="run", object_type="ipam.syncrun").exists())
        r = self.c.get(reverse("ipam:sync_run", args=[run.pk]))
        self.assertContains(r, "10.90.1.60")
        self.assertContains(r, "192.0.2.50")
        r = self.c.get(reverse("ipam:conflicts"))
        self.assertContains(r, "management address of 2 devices")
        self.assertContains(r, "outside every subnet")
        self.assertContains(r, "x.dr.test")
        r = self.c.get(reverse("ipam:conflicts") + "?kind=duplicate")
        self.assertNotContains(r, "x.dr.test")
        rows = list(csv.reader(io.StringIO(self.c.get(reverse("ipam:conflicts_export")).content.decode())))
        self.assertEqual(rows[0], ["kind", "address", "site", "problem", "detail"])
        self.assertGreaterEqual(len(rows), 4)
        self.c.force_login(self.dr_op)
        r = self.c.get(reverse("ipam:sync_run", args=[run.pk]))
        self.assertNotContains(r, "10.90.1.60")
        r = self.c.get(reverse("ipam:conflicts"))
        self.assertNotContains(r, "dup1")

    def test_empty_states(self):
        IPAddress.objects.all().delete()
        Subnet.objects.all().delete()
        DNSZone.objects.all().delete()
        VLAN.objects.all().delete()
        SyncRun.objects.all().delete()
        self.c.force_login(self.viewer)
        r = self.c.get(reverse("ipam:overview"))
        self.assertContains(r, "No subnets yet")
        self.assertNotContains(r, "seed_ipam")            # a live console never tells the visitor to load demo data
        for name in ("ipam:subnets", "ipam:addresses", "ipam:vlans", "ipam:dns", "ipam:conflicts"):
            self.assertEqual(self.c.get(reverse(name)).status_code, 200, name)


class SeedTests(TestCase):
    def test_seed_is_idempotent_and_marked(self):
        call_command("seed_ipam", stdout=io.StringIO())
        counts = (Subnet.objects.count(), IPAddress.objects.count(), DNSRecord.objects.count(), VLAN.objects.count())
        call_command("seed_ipam", stdout=io.StringIO())
        self.assertEqual(counts, (Subnet.objects.count(), IPAddress.objects.count(), DNSRecord.objects.count(), VLAN.objects.count()))
        self.assertTrue(all(s.is_example for s in Subnet.objects.all()))
        self.assertTrue(all(a.is_example for a in IPAddress.objects.all()))
        self.assertTrue(all(a.subnet_id for a in IPAddress.objects.all()))
        self.assertFalse(IPAddress.objects.exclude(last_seen=None).exists())     # nothing invented
        self.assertTrue(any(c["kind"] == "dns_free" for c in sync.conflicts()))
        call_command("seed_ipam", "--remove", stdout=io.StringIO())
        self.assertEqual(Subnet.objects.count() + IPAddress.objects.count() + DNSZone.objects.count(), 0)


# ------------------------------------------------------------------ review fixes
class ReviewFixTests(Users, TestCase):
    def setUp(self):
        self.make_users()
        self.c = Client()

    def test_container_detail_hides_foreign_site_children(self):
        """ipam-dcim-compute-1: a global container must not leak addresses / CIDRs of sites the user cannot see."""
        sup = subnet("10.0.0.0/16", role=Role.CONTAINER)                       # All Sites
        child = subnet("10.0.1.0/25", site=MUM)
        addr("10.0.1.5", hostname="secret-db.mum")
        mine = subnet("10.0.2.0/24", site=DR)
        addr("10.0.2.7", hostname="dr-host.dr")
        self.c.force_login(self.dr_op)
        self.assertEqual(self.c.get(reverse("ipam:subnet_detail", args=[child.pk])).status_code, 404)
        r = self.c.get(reverse("ipam:subnet_detail", args=[sup.pk]))
        self.assertEqual(r.status_code, 200)
        for leaked in ("10.0.1.5", "secret-db.mum", "10.0.1.0/25"):
            self.assertNotContains(r, leaked)
        self.assertContains(r, "10.0.2.7")
        self.assertContains(r, "10.0.2.0/24")
        self.assertEqual(r.context["used"], 1)                                  # only the visible address is counted
        self.assertEqual(r.context["status_counts"], {"active": 1})
        # the /16 container draws a /24 block map: hidden child cells must not carry the CIDR or hostname
        self.assertTrue(all("10.0.1.0/25" not in c["title"] and "secret" not in c["title"] for c in r.context["amap"]["cells"]))
        # a tenant-wide user still sees everything
        self.c.force_login(self.operator)
        r = self.c.get(reverse("ipam:subnet_detail", args=[sup.pk]))
        for seen in ("10.0.1.5", "secret-db.mum", "10.0.1.0/25", "10.0.2.7"):
            self.assertContains(r, seen)
        self.assertEqual(r.context["used"], 2)

    def test_container_address_map_cells_for_hidden_child(self):
        sup = subnet("10.0.0.0/22", role=Role.CONTAINER)
        subnet("10.0.1.0/24", site=MUM)
        amap0, _ = services.address_map(sup)
        self.assertIn("10.0.1.0/24", next(c for c in amap0["cells"] if c["ip"] == "10.0.1.5")["title"])
        addr("10.0.1.5", hostname="secret-db.mum")
        amap, _ = services.address_map(sup, user=self.dr_op)
        cell = next(c for c in amap["cells"] if c["ip"] == "10.0.1.5")
        self.assertEqual(cell["kind"], "child")
        self.assertIsNone(cell["pk"])
        self.assertIsNone(cell["child"])
        self.assertNotIn("secret", cell["title"])
        self.assertNotIn("10.0.1.0/24", cell["title"])
        self.assertIn("cannot see", cell["title"])

    def test_reserve_keeps_hostname(self):
        """ipam-dcim-compute-4: the hostname typed next to 'Reserve next free' is stored (validated)."""
        net = subnet("10.50.0.0/24", site=MUM)
        self.c.force_login(self.operator)
        r = self.c.post(reverse("ipam:subnet_reserve", args=[net.pk]), {"hostname": "App01.Corp.Example.com"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(IPAddress.objects.get(address="10.50.0.1").hostname, "app01.corp.example.com")
        # an invalid hostname reserves nothing and says why
        r = self.c.post(reverse("ipam:subnet_reserve", args=[net.pk]), {"hostname": "not a host!"}, follow=True)
        self.assertEqual(IPAddress.objects.filter(subnet=net).count(), 1)
        self.assertContains(r, "valid DNS name")
        # blank stays allowed
        self.c.post(reverse("ipam:subnet_reserve", args=[net.pk]), {"hostname": ""})
        self.assertEqual(IPAddress.objects.get(address="10.50.0.2").hostname, "")

    def test_import_oversized_field_is_an_error_not_a_500(self):
        """ipam-dcim-compute-5."""
        subnet("10.70.0.0/24", site=MUM)
        self.c.force_login(self.operator)
        text = "address,description\n10.70.0.5," + "x" * 200_000 + "\n"
        r = self.c.post(reverse("ipam:addresses_import"),
                        {"file": SimpleUploadedFile("a.csv", text.encode(), content_type="text/csv")})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(IPAddress.objects.exists())
        self.assertTrue(r.context["result"].errors)
        self.assertContains(r, "too long")

    def test_ipv6_zone_id_networks_are_rejected(self):
        """ipam-dcim-compute-6."""
        with self.assertRaises(ValueError):
            services.parse_network("fe80::%eth0/64")
        subnet("fe80::/64")
        for text in ("fe80::%a/64", "fe80::%b/64"):
            f = SubnetForm({"cidr": text, "vrf": "", "site": "All Sites"}, user=self.admin)
            self.assertFalse(f.is_valid(), text)
        # same range, same prefix, different spelling is a duplicate whatever the strings say
        net = ipaddress.ip_network("fe80::/64")
        self.assertIn("already exists", services.placement_error(T, "", net, ""))
        Subnet.objects.filter(cidr="fe80::/64").update(cidr="FE80::/64")        # stored spelling differs
        self.assertIn("already exists", services.placement_error(T, "", net, ""))

    def _race_once(self, module, name, net, at_after=True):
        """Wrap services.<name> so a competing row for the address in play is committed right after the first call."""
        from unittest import mock
        real = getattr(module, name)
        state = {"done": False}

        def wrapper(*a, **kw):
            out = real(*a, **kw)
            if not state["done"]:
                state["done"] = True
                target = out if name == "next_free" else self.race_address
                IPAddress(tenant_key=T, address=str(target), vrf=net.vrf, subnet=net, site=net.site).save()
            return out
        return mock.patch.object(module, name, wrapper)

    def test_address_save_race_is_a_form_error(self):
        """ipam-dcim-compute-8: a duplicate inserted between validation and save must not 500."""
        net = subnet("10.60.0.0/24", site=MUM)
        self.c.force_login(self.operator)
        self.race_address = "10.60.0.9"
        with self._race_once(services, "used_count", net):
            r = self.c.post(reverse("ipam:address_create"), {"address": "10.60.0.9", "vrf": "", "status": "active"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "already recorded")
        self.assertEqual(IPAddress.objects.filter(address="10.60.0.9").count(), 1)

    def test_reserve_race_retries_next_free(self):
        net = subnet("10.61.0.0/24", site=MUM)
        self.c.force_login(self.operator)
        with self._race_once(services, "next_free", net):
            r = self.c.post(reverse("ipam:subnet_reserve", args=[net.pk]))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(sorted(IPAddress.objects.filter(subnet=net).values_list("address", flat=True)), ["10.61.0.1", "10.61.0.2"])

    def test_vlan_zone_record_subnet_races_become_form_errors(self):
        from unittest import mock
        from . import views
        self.c.force_login(self.operator)
        zone = DNSZone.objects.create(tenant_key=T, name="r.test", site=MUM)
        posts = [(reverse("ipam:vlan_create"), {"vid": 77, "name": "A", "site": MUM}, VLAN),
                 (reverse("ipam:zone_create"), {"name": "z.test", "kind": "forward", "site": MUM, "default_ttl": 300}, DNSZone),
                 (reverse("ipam:record_create", args=[zone.pk]), {"name": "w", "rtype": "A", "value": "10.1.1.1"}, DNSRecord),
                 (reverse("ipam:subnet_create"), {"cidr": "10.99.0.0/24", "vrf": "", "site": MUM}, Subnet)]
        for url, data, model in posts:
            before = model.objects.count()
            with mock.patch.object(model, "save", side_effect=IntegrityError("UNIQUE constraint failed")):
                r = self.c.post(url, data)
            self.assertEqual(r.status_code, 200, url)
            self.assertContains(r, "saved by someone else")
            self.assertEqual(model.objects.count(), before)

    def test_import_race_reports_an_error(self):
        from unittest import mock
        subnet("10.62.0.0/24", site=MUM)
        self.c.force_login(self.operator)
        with mock.patch.object(IPAddress, "save", side_effect=IntegrityError("UNIQUE constraint failed")):
            r = self.c.post(reverse("ipam:addresses_import"), {"file": SimpleUploadedFile(
                "a.csv", b"address\n10.62.0.5\n", content_type="text/csv")})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "added by someone else")
        self.assertFalse(IPAddress.objects.exists())

    def _primary_ip_selects(self, ctx):
        return [q["sql"] for q in ctx.captured_queries if q["sql"].startswith('SELECT "ipam_ipaddress".')]

    def test_pages_do_not_load_every_address_of_the_tenant(self):
        """ipam-dcim-compute-10."""
        from django.test.utils import CaptureQueriesContext
        from django.db import connection
        sup = subnet("10.0.0.0/16", role=Role.CONTAINER, site=MUM)
        subnet("10.0.1.0/24", site=MUM)
        addr("10.0.1.5")
        zone = DNSZone.objects.create(tenant_key=T, name="perf.test", site=MUM)
        DNSRecord.objects.create(tenant_key=T, zone=zone, name="a", rtype="A", value="10.0.1.5")
        DNSRecord.objects.create(tenant_key=T, zone=zone, name="b", rtype="A", value="10.0.1.77")
        self.c.force_login(self.admin)
        with CaptureQueriesContext(connection) as ctx:
            r = self.c.get(reverse("ipam:zone_detail", args=[zone.pk]))
        self.assertEqual(r.status_code, 200)
        for sql in self._primary_ip_selects(ctx):
            self.assertIn(" IN (", sql)                       # only the page's own values are looked up
        free = {x.name: x.free_target for x in r.context["page"]}
        self.assertEqual(free, {"a": False, "b": True})
        with CaptureQueriesContext(connection) as ctx:
            found = sync.conflicts()
        self.assertEqual([c["address"] for c in found if c["kind"] == "dns_free"], ["10.0.1.77"])
        for sql in self._primary_ip_selects(ctx):
            self.assertIn("IS NULL", sql)                     # the orphan query; no tenant-wide address set

    def test_subnet_list_depth_needs_a_constant_number_of_queries(self):
        from django.test.utils import CaptureQueriesContext
        from django.db import connection
        self.c.force_login(self.admin)
        subnet("10.0.0.0/8", role=Role.CONTAINER)
        subnet("10.1.0.0/16", role=Role.CONTAINER)

        def count():
            with CaptureQueriesContext(connection) as ctx:
                r = self.c.get(reverse("ipam:subnets") + "?per=100")
            self.assertEqual(r.status_code, 200)
            return len(ctx), {s.cidr: s.depth for s in r.context["items"]}

        for n in range(5):
            subnet(f"10.1.{n}.0/24")
        # the sidebar's badge counts are cached per user for a few seconds: the first page view fills that cache
        # (four extra queries), so warm it first and compare only what the subnet list itself costs
        self.c.get(reverse("ipam:subnets"))
        few, depths = count()
        self.assertEqual((depths["10.0.0.0/8"], depths["10.1.0.0/16"], depths["10.1.3.0/24"]), (0, 1, 2))
        for n in range(5, 25):
            subnet(f"10.1.{n}.0/24")
        many, _ = count()
        self.assertEqual(few, many)

    def test_seed_remove_only_deletes_seed_rows(self):
        """ipam-dcim-compute-11."""
        call_command("seed_ipam", stdout=io.StringIO())
        real_net = subnet("10.123.0.0/24", description="Example Corp customer LAN")
        real_addr = addr("10.123.0.7", description="Example: lab box")
        real_vlan = VLAN.objects.create(tenant_key=T, site="All Sites", vid=999, name="REAL", description="Example Corp DMZ")
        call_command("seed_ipam", "--remove", stdout=io.StringIO())
        self.assertTrue(Subnet.objects.filter(pk=real_net.pk).exists())
        self.assertTrue(IPAddress.objects.filter(pk=real_addr.pk).exists())
        self.assertTrue(VLAN.objects.filter(pk=real_vlan.pk).exists())
        self.assertEqual(Subnet.objects.exclude(pk=real_net.pk).count(), 0)
        self.assertEqual(IPAddress.objects.exclude(pk=real_addr.pk).count(), 0)
        self.assertEqual(VLAN.objects.exclude(pk=real_vlan.pk).count(), 0)
