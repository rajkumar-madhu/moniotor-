"""F09: a CI's linked URL checks and domains on sites the user cannot see stay hidden (detail page, CSV export),
and importing the user's own export keeps those hidden links."""
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse

from core.models import Membership, Site
from domainmonitor.models import Domain
from urlchecker.models import UrlCheck

from .models import ConfigurationItem


class LinkVisibilityTests(TestCase):
    def setUp(self):
        self.mum = Site.objects.create(name="Mumbai", code="mum")
        Site.objects.create(name="DR", code="dr")
        self.secret = UrlCheck.objects.create(name="dr-secret-api", url="https://10.9.9.9/internal", site="DR",
                                              last_error="401 from internal-auth.dr.local")
        self.mine = UrlCheck.objects.create(name="mum-portal", url="https://portal.example.com/", site="Mumbai")
        self.secret_dom = Domain.objects.create(tenant_key="default", hostname="dr-secret.example.com", site_key="DR")
        self.ci = ConfigurationItem.objects.create(name="shared-lb", site="All Sites")
        self.ci.url_checks.add(self.secret, self.mine)
        self.ci.domains.add(self.secret_dom)

    def login(self, name, role):
        u = User.objects.create_user(name, password="x")
        Membership.objects.create(user=u, site=self.mum, role=role)
        c = Client()
        c.force_login(u)
        return c

    def test_detail_hides_links_of_other_sites(self):
        r = self.login("mview", "viewer").get(reverse("cmdb:item_detail", args=[self.ci.pk]))
        self.assertEqual(r.status_code, 200)
        for leak in ("dr-secret-api", "10.9.9.9", "internal-auth.dr.local", "dr-secret.example.com"):
            self.assertNotContains(r, leak)
        self.assertContains(r, "mum-portal")

    def test_export_lists_only_visible_links_and_reimport_keeps_hidden_ones(self):
        ci2 = ConfigurationItem.objects.create(name="mum-box", site="Mumbai")
        ci2.url_checks.add(self.secret, self.mine)
        c = self.login("mop", "operator")
        exp = c.get(reverse("cmdb:items_export") + "?site=Mumbai").content   # the items this operator may change
        self.assertNotIn(b"dr-secret", exp)
        self.assertIn(b"mum-portal", exp)
        r = c.post(reverse("cmdb:items_import"), {"file": SimpleUploadedFile("x.csv", exp), "update_existing": "on"})
        self.assertEqual(r.status_code, 302, r.content.decode()[:2000])   # imported (errors re-render the form)
        self.assertEqual(set(ci2.url_checks.all()), {self.secret, self.mine})

    def test_tenant_wide_user_exports_every_link(self):
        u = User.objects.create_user("all", password="x")   # no memberships: every site
        c = Client()
        c.force_login(u)
        self.assertIn(b"dr-secret-api", c.get(reverse("cmdb:items_export")).content)


class LinkHealthVisibilityTests(TestCase):
    """A shared CI's health colour comes only from the links the viewer can see."""

    def setUp(self):
        self.mum = Site.objects.create(name="Mumbai", code="mum")
        Site.objects.create(name="DR", code="dr")
        self.down = UrlCheck.objects.create(name="dr-api", url="https://10.9.9.9/", site="DR", last_status="down")
        self.up = UrlCheck.objects.create(name="mum-api", url="https://mum.example.com/", site="Mumbai",
                                          last_status="online")
        self.ci = ConfigurationItem.objects.create(name="shared-lb", site="All Sites", ci_type="service")
        self.ci.url_checks.add(self.down, self.up)
        self.viewer = User.objects.create_user("mview", password="x")
        Membership.objects.create(user=self.viewer, site=self.mum, role="viewer")

    def test_link_health_uses_visible_links_only(self):
        from . import services
        self.assertEqual(services.link_health([self.ci])[self.ci.pk], "critical")             # no user: everything
        self.assertEqual(services.link_health([self.ci], self.viewer)[self.ci.pk], "healthy")
        root = User.objects.create_superuser("root", "r@x.com", "x")
        self.assertEqual(services.link_health([self.ci], root)[self.ci.pk], "critical")

    def test_pages_use_the_viewers_links(self):
        c = Client()
        c.force_login(self.viewer)
        r = c.get(reverse("cmdb:items"))
        self.assertEqual({x.pk: x.health for x in r.context["page"]}[self.ci.pk], "healthy")
        r = c.get(reverse("cmdb:item_detail", args=[self.ci.pk]))
        self.assertEqual(r.status_code, 200)

    def test_heatmap_ignores_hidden_links(self):
        from . import heatmap
        d = heatmap.build(self.viewer)
        rows = [r for dom in d["domains"] for r in dom["service_rows"] if r["ci"].pk == self.ci.pk]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["checks"], 1)
        self.assertNotEqual(rows[0]["health"], "critical")
        rows = [r for dom in heatmap.build(None)["domains"] for r in dom["service_rows"] if r["ci"].pk == self.ci.pk]
        self.assertEqual(rows[0]["checks"], 2)
