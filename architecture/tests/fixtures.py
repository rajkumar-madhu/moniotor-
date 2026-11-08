from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase

from architecture.models import Link
from core.models import Membership, Site
from network.models import Device


def dev(name, device_type="switch", site="Mumbai", **kw):
    kw.setdefault("mgmt_ip", "192.0.2.%d" % (abs(hash(name)) % 250 + 1))
    return Device.objects.create(name=name, device_type=device_type, site=site, **kw)


def link(a, b, a_port="", b_port="", **kw):
    a_id, ap, b_id, bp = Link.order_ends(a.pk, a_port, b.pk, b_port)
    return Link.objects.create(a_device_id=a_id, a_port=ap, b_device_id=b_id, b_port=bp, **kw)


def make_fabric(spines, leaves, servers_per_leaf=2, site="Mumbai"):
    """A synthetic spine-leaf fabric built with bulk inserts (so big ones stay quick). Returns (spines, leaves, servers)."""
    sp = Device.objects.bulk_create([Device(name=f"spine-{i:02d}", device_type="switch", site=site, mgmt_ip="192.0.2.1") for i in range(spines)])
    lf = Device.objects.bulk_create([Device(name=f"leaf-{i:02d}", device_type="switch", site=site, mgmt_ip="192.0.2.2") for i in range(leaves)])
    sv = Device.objects.bulk_create([Device(name=f"{('web', 'app', 'db')[i % 3]}-{i:03d}", device_type="physical_server", site=site, mgmt_ip="192.0.2.3")
                                     for i in range(leaves * servers_per_leaf)])
    rows = []
    for s in sp:
        for n, l in enumerate(lf):
            a, ap, b, bp = Link.order_ends(s.pk, f"Eth1/{n + 1}", l.pk, f"Eth1/{49 + s.pk % 8}")
            rows.append(Link(a_device_id=a, a_port=ap, b_device_id=b, b_port=bp, medium="fibre", speed_mbps=100000))
    for i, srv in enumerate(sv):
        l = lf[i // servers_per_leaf]
        a, ap, b, bp = Link.order_ends(l.pk, f"Eth1/{i % servers_per_leaf + 1}", srv.pk, "eth0")
        rows.append(Link(a_device_id=a, a_port=ap, b_device_id=b, b_port=bp, medium="dac", speed_mbps=25000))
    Link.objects.bulk_create(rows)
    return sp, lf, sv


class Base(TestCase):
    """Two sites; users: admin, global viewer, global operator, Mumbai-only operator, Mumbai-only viewer, London-only operator."""

    @classmethod
    def setUpTestData(cls):
        cls.mum = Site.objects.create(name="Mumbai", code="mum")
        cls.lon = Site.objects.create(name="London", code="lon")
        cls.admin = User.objects.create_superuser("root", "r@example.com", "x")
        cls.viewer = User.objects.create_user("viewer", password="x")
        cls.operator = User.objects.create_user("op", password="x", is_staff=True)
        cls.mum_op = User.objects.create_user("mumop", password="x")
        Membership.objects.create(user=cls.mum_op, site=cls.mum, role="operator")
        cls.mum_viewer = User.objects.create_user("mumview", password="x")
        Membership.objects.create(user=cls.mum_viewer, site=cls.mum, role="viewer")
        cls.lon_op = User.objects.create_user("lonop", password="x")
        Membership.objects.create(user=cls.lon_op, site=cls.lon, role="operator")
        cls.spine = dev("dc1-spine-01", site="Mumbai")
        cls.leaf = dev("dc1-leaf-01", site="Mumbai")
        cls.leaf2 = dev("dc1-leaf-02", site="Mumbai")
        cls.lon_sw = dev("lon-core-01", site="London")
        cls.srv = dev("web-01", "physical_server", site="Mumbai")

    def setUp(self):
        cache.clear()

    def login(self, user):
        self.client.force_login(user)
