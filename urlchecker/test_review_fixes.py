"""URL check create, edit, delete and run leave an audit trail."""
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from core.models import AuditEvent, Membership, Site
from probes import checks

from .models import UrlCheck

FORM = {"url": "https://c.example.com/", "check_type": "web", "method": "GET", "expected_status": 200,
        "interval_seconds": 300, "degraded_ms": 1000, "timeout_seconds": 10, "enabled": "on", "follow_redirects": "on", "site": "SiteA"}


class UrlCheckAuditTests(TestCase):
    def setUp(self):
        self.site = Site.objects.create(name="SiteA", code="site-a")
        self.admin = User.objects.create_user("adm", password="x" * 14)
        Membership.objects.create(user=self.admin, site=None, role="admin")
        self.client.force_login(self.admin)
        p = mock.patch.object(checks, "http_check",
                              return_value=checks.HttpResult(ok=True, status=200, response_ms=10.0, ip="93.184.216.34", error=""))
        p.start()
        self.addCleanup(p.stop)

    def events(self, action):
        return AuditEvent.objects.filter(action=action, object_type="urlchecker.urlcheck")

    def test_create_is_audited(self):
        r = self.client.post(reverse("urlchecker:index"), {**FORM, "name": "portal"})
        self.assertEqual(r.status_code, 302)
        ev = self.events("create").get()
        self.assertEqual(ev.object_repr, "portal")
        self.assertEqual(ev.details["site"], "SiteA")
        self.assertEqual(ev.actor, self.admin)

    def test_edit_is_audited_with_changed_fields(self):
        c = UrlCheck.objects.create(name="portal", url="https://c.example.com/", site="SiteA")
        self.client.post(reverse("urlchecker:edit", args=[c.pk]), {**FORM, "name": "portal2", "interval_seconds": 600})
        ev = self.events("update").get()
        self.assertEqual(set(ev.details["changed"]), {"name", "interval_seconds"})

    def test_invalid_post_is_not_audited(self):
        self.client.post(reverse("urlchecker:index"), {**FORM, "name": "", "url": "not a url"})
        self.assertFalse(AuditEvent.objects.filter(object_type="urlchecker.urlcheck").exists())

    def test_delete_is_audited(self):
        c = UrlCheck.objects.create(name="portal", url="https://c.example.com/", site="SiteA")
        self.client.post(reverse("urlchecker:delete", args=[c.pk]))
        ev = self.events("delete").get()
        self.assertEqual(ev.object_repr, "portal")
        self.assertEqual(ev.details["site"], "SiteA")
        self.assertFalse(UrlCheck.objects.filter(pk=c.pk).exists())

    def test_run_one_and_run_all_are_audited(self):
        c = UrlCheck.objects.create(name="portal", url="https://c.example.com/", site="SiteA")
        self.client.post(reverse("urlchecker:run", args=[c.pk]))
        ev = self.events("run").get()
        self.assertEqual(ev.object_repr, "portal")
        AuditEvent.objects.all().delete()
        self.client.post(reverse("urlchecker:run_all"))
        ev = AuditEvent.objects.get(action="run", object_type="urlchecker.urlcheck")
        self.assertEqual(ev.details["checked"], 1)
