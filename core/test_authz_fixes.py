"""core.access.can_on: global records need the role on every site."""
from django.contrib.auth.models import AnonymousUser, User
from django.test import TestCase

from core.access import can, can_on
from core.models import Membership, Site


class CanOnTests(TestCase):
    def setUp(self):
        self.mum = Site.objects.create(name="Mumbai", code="mum")
        Site.objects.create(name="DR", code="dr")

    def user(self, name, staff=False, **roles):
        u = User.objects.create_user(name, password="x", is_staff=staff)
        for site, role in roles.items():
            Membership.objects.create(user=u, site=None if site == "all" else Site.objects.get(name=site), role=role)
        return u

    def test_one_site_role_never_counts_for_global_records(self):
        u = self.user("mop", Mumbai="admin")
        self.assertTrue(can(u, "admin"))          # "anywhere" — the old, too weak check
        for site in (None, "", "All Sites", " All Sites "):
            self.assertFalse(can_on(u, "operate", site), repr(site))
        self.assertTrue(can_on(u, "admin", "Mumbai"))
        self.assertFalse(can_on(u, "view", "DR"))

    def test_tenant_wide_roles(self):
        g = self.user("g", all="operator", Mumbai="admin")
        self.assertTrue(can_on(g, "operate", None))
        self.assertFalse(can_on(g, "admin", None))
        self.assertTrue(can_on(g, "admin", "Mumbai"))
        self.assertTrue(can_on(User.objects.create_superuser("root", "r@x.com", "x"), "admin", None))
        # no memberships: the default role holds everywhere (staff = operator, others = viewer)
        self.assertTrue(can_on(self.user("staff", staff=True), "operate", ""))
        self.assertFalse(can_on(self.user("plain"), "operate", ""))
        self.assertTrue(can_on(self.user("plain2"), "view", None))
        self.assertFalse(can_on(AnonymousUser(), "view", None))
