"""'All Sites' URL checks: a one-site operator may RUN them but not CHANGE or DELETE them (same split as Domains)."""
from unittest import mock

from django.test import TestCase
from django.urls import reverse

from core.models import Site
from probes import checks
from urlchecker.models import UrlCheck
from urlchecker.test_audit_fixes import FORM, ok_result, user_with


class GlobalCheckChangeVsRunTests(TestCase):
    def setUp(self):
        self.site_a = Site.objects.create(name="SiteA", code="site-a")
        Site.objects.create(name="SiteB", code="site-b")
        self.glob = UrlCheck.objects.create(name="exchange-gw", url="https://g.example.com/", site="All Sites")
        patcher = mock.patch.object(checks, "http_check", return_value=ok_result())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_one_site_operator_can_run_but_not_change_a_global_check(self):
        self.client.force_login(user_with("opa", "operator", self.site_a))
        r = self.client.post(reverse("urlchecker:run", args=[self.glob.pk]), HTTP_X_CSRFTOKEN="t")
        self.assertEqual(r.status_code, 200)
        r = self.client.post(reverse("urlchecker:edit", args=[self.glob.pk]),
                             dict(FORM, name="exchange-gw", site="All Sites", url="https://evil.example.com/"))
        self.glob.refresh_from_db()
        self.assertEqual(self.glob.url, "https://g.example.com/")
        page = self.client.get(reverse("urlchecker:detail", args=[self.glob.pk])).content.decode()
        self.assertIn(reverse("urlchecker:run", args=[self.glob.pk]), page)
        self.assertNotIn(reverse("urlchecker:edit", args=[self.glob.pk]), page)

    def test_one_site_operator_cannot_create_an_all_sites_check(self):
        self.client.force_login(user_with("opa", "operator", self.site_a))
        self.client.post(reverse("urlchecker:index"), dict(FORM, name="new-global", site="All Sites"))
        self.assertFalse(UrlCheck.objects.filter(name="new-global").exists())
        self.client.post(reverse("urlchecker:index"), dict(FORM, name="new-a", site="SiteA"))
        self.assertTrue(UrlCheck.objects.filter(name="new-a").exists())

    def test_one_site_admin_cannot_delete_a_global_check(self):
        self.client.force_login(user_with("ada", "admin", self.site_a))
        self.client.post(reverse("urlchecker:delete", args=[self.glob.pk]))
        self.assertTrue(UrlCheck.objects.filter(pk=self.glob.pk).exists())

    def test_tenant_wide_operator_can_change_a_global_check(self):
        self.client.force_login(user_with("opall", "operator", None))
        self.client.post(reverse("urlchecker:edit", args=[self.glob.pk]),
                         dict(FORM, name="exchange-gw", site="All Sites", url="https://g2.example.com/"))
        self.glob.refresh_from_db()
        self.assertEqual(self.glob.url, "https://g2.example.com/")
