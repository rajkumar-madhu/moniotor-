"""F05: the domains JSON API keeps to this deployment's tenant and the caller's sites; "Check all now" is capped."""
import json
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import Membership, Site

from . import views
from .models import Domain

API = "/configuration/domains/api/"


class DomainApiAuthzTests(TestCase):
    def setUp(self):
        self.a = Site.objects.create(name="SiteA", code="A")
        self.b = Site.objects.create(name="SiteB", code="B")
        self.mine = Domain.objects.create(tenant_key="default", hostname="a.example.com", site_key="SiteA")
        self.theirs = Domain.objects.create(tenant_key="default", hostname="b.example.com", site_key="SiteB")
        self.shared = Domain.objects.create(tenant_key="default", hostname="all.example.com", site_key="All Sites")
        self.foreign = Domain.objects.create(tenant_key="other", hostname="x.example.com", site_key="SiteA")

    def staff(self, name, **roles):
        u = User.objects.create_user(name, password="pw", is_staff=True)
        for site, role in roles.items():
            Membership.objects.create(user=u, site=getattr(self, site), role=role)
        self.client.force_login(u)
        return u

    def post(self, body):
        return self.client.post(API, json.dumps(body), content_type="application/json")

    def patch(self, pk, body, tenant="default"):
        return self.client.patch(f"{API}{pk}/?tenant={tenant}", json.dumps(body), content_type="application/json")

    def test_staff_viewer_cannot_write(self):
        self.staff("sv", a="viewer")
        self.assertEqual(self.post({"tenant": "default", "site": "SiteA", "hostname": "new.example.com"}).status_code, 403)
        self.assertEqual(self.patch(self.mine.pk, {"active": False}).status_code, 403)
        self.assertEqual(self.client.delete(f"{API}{self.mine.pk}/?tenant=default").status_code, 403)
        self.assertFalse(Domain.objects.filter(hostname="new.example.com").exists())
        self.mine.refresh_from_db()
        self.assertTrue(self.mine.is_active)

    def test_other_tenant_is_refused(self):
        self.staff("op", a="operator")
        r = self.post({"tenant": "othertenant", "site": "SiteA", "hostname": "x2.example.com"})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(Domain.objects.filter(hostname="x2.example.com").exists())
        self.assertEqual(self.client.get(f"{API}?tenant=other").status_code, 400)
        self.assertEqual(self.patch(self.foreign.pk, {"description": "x"}, tenant="other").status_code, 400)
        self.assertEqual(self.patch(self.foreign.pk, {"description": "x"}).status_code, 404)

    def test_site_operator_writes_only_own_site(self):
        self.staff("op", a="operator", b="viewer")
        with mock.patch("probes.runner.run_domain_checks"):
            self.assertEqual(self.post({"tenant": "default", "site": "SiteB", "hostname": "evil.example.com"}).status_code, 403)
            self.assertEqual(self.post({"tenant": "default", "site": "All Sites", "hostname": "g.example.com"}).status_code, 403)
            self.assertEqual(self.post({"tenant": "default", "site": "SiteA", "hostname": "ok.example.com"}).status_code, 201)
        self.assertEqual(self.patch(self.theirs.pk, {"description": "x"}).status_code, 403)
        self.assertEqual(self.patch(self.shared.pk, {"description": "x"}).status_code, 403)
        # moving needs operate on the new site too
        self.assertEqual(self.patch(self.mine.pk, {"site": "SiteB"}).status_code, 403)
        self.mine.refresh_from_db()
        self.assertEqual(self.mine.site_key, "SiteA")
        self.assertEqual(self.patch(self.mine.pk, {"description": "ok"}).json(), {"status": "updated"})

    def test_get_is_scoped_to_visible_sites(self):
        self.staff("op", a="operator")
        rows = self.client.get(f"{API}?tenant=default").json()["results"]
        self.assertEqual({x["hostname"] for x in rows}, {"a.example.com", "all.example.com"})
        self.assertEqual(self.patch(self.theirs.pk, {"description": "x"}).status_code, 404)


class RunAllCapTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("op", password="pw", is_staff=True)
        self.client.force_login(self.user)
        now = timezone.now()
        Domain.objects.bulk_create([
            Domain(tenant_key="default", hostname=f"d{i:03}.example.com", site_key="All Sites",
                   last_checked_at=now - timedelta(minutes=i)) for i in range(views.RUN_ALL_LIMIT + 10)])
        Domain.objects.create(tenant_key="default", hostname="never.example.com", site_key="All Sites")

    @mock.patch("probes.runner.run_domain_checks", return_value=[])
    def test_run_all_checks_at_most_the_limit_oldest_first(self, run):
        r = self.client.post(reverse("domains:run_all"), HTTP_X_CSRFTOKEN="x", HTTP_ACCEPT="application/json")
        body = r.json()
        self.assertEqual(body["checked"], views.RUN_ALL_LIMIT)
        self.assertEqual(body["deferred"], 11)
        checked = [d.hostname for d in run.call_args[0][0]]
        self.assertEqual(len(checked), views.RUN_ALL_LIMIT)
        self.assertEqual(checked[0], "never.example.com")         # never checked first
        self.assertEqual(checked[1], f"d{views.RUN_ALL_LIMIT + 9:03}.example.com")   # then the oldest
        self.assertNotIn("d000.example.com", checked)              # the most recently checked waits
        msgs = [str(m) for m in r.wsgi_request._messages]
        self.assertTrue(any("11 more were not checked now" in m for m in msgs), msgs)


class ConsoleChangeVsRunTests(TestCase):
    """Changing an "All Sites" domain needs operate on every site; running a check on it does not."""

    def setUp(self):
        a = Site.objects.create(name="SiteA", code="A")
        self.user = User.objects.create_user("opA", password="pw")
        Membership.objects.create(user=self.user, site=a, role="operator")
        self.shared = Domain.objects.create(tenant_key="default", hostname="all.example.com", site_key="All Sites")
        self.mine = Domain.objects.create(tenant_key="default", hostname="a.example.com", site_key="SiteA")
        self.client.force_login(self.user)

    def form(self, **kw):
        data = {"hostname": "new.example.com", "domain_type": "API", "site_key": "All Sites",
                "check_interval_seconds": 300, "monitor_enabled": "on"}
        data.update(kw)
        return data

    @mock.patch("probes.runner.run_domain_checks", return_value=[])
    def test_one_site_operator_cannot_change_global_domain(self, run):
        self.client.post(reverse("domains:toggle", args=[self.shared.pk]))
        self.shared.refresh_from_db()
        self.assertTrue(self.shared.is_active)
        self.client.post(reverse("domains:list"), self.form(pk=self.shared.pk, hostname="all.example.com",
                                                             monitor_enabled=""))
        self.shared.refresh_from_db()
        self.assertTrue(self.shared.monitor_enabled)
        r = self.client.post(reverse("domains:list"), self.form())
        self.assertFalse(Domain.objects.filter(hostname="new.example.com").exists())
        self.assertContains(r, "need operator rights on every site")
        self.client.post(reverse("domains:list"), self.form(pk=self.mine.pk, hostname="a.example.com"))
        self.mine.refresh_from_db()
        self.assertEqual(self.mine.site_key, "SiteA")      # cannot move its own domain to All Sites either
        page = self.client.get(reverse("domains:list") + f"?selected={self.shared.pk}")
        self.assertNotContains(page, f"?edit={self.shared.pk}&")
        self.assertContains(page, reverse("domains:action", args=[self.shared.pk, "uptime"]))   # running stays allowed

    @mock.patch("probes.runner.run_domain_checks", return_value=[])
    def test_one_site_operator_can_still_run_checks_on_global_domain(self, run):
        r = self.client.post(reverse("domains:action", args=[self.shared.pk, "uptime"]), HTTP_X_CSRFTOKEN="x")
        self.assertEqual(r.status_code, 200)
        run.assert_called_once()

    @mock.patch("probes.runner.run_domain_checks", return_value=[])
    def test_tenant_wide_operator_can_change_global_domain(self, run):
        g = User.objects.create_user("gop", password="pw")
        Membership.objects.create(user=g, site=None, role="operator")
        self.client.force_login(g)
        self.client.post(reverse("domains:toggle", args=[self.shared.pk]))
        self.shared.refresh_from_db()
        self.assertFalse(self.shared.is_active)
        self.client.post(reverse("domains:list"), self.form())
        self.assertTrue(Domain.objects.filter(hostname="new.example.com", site_key="All Sites").exists())
