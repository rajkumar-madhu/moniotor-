"""Regression tests for F13 (sign-in throttling) and F25 (site rename cascades to every module)."""
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import AuditEvent, Event, Membership, Site

from . import services
from .models import LoginThrottle

PW = "Correct-Horse-Battery-9"
LOCKED = "Too many failed sign-ins. Try again in 15 minutes."


class LoginThrottleTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user("alice", password=PW)

    def login(self, username, password, url="/accounts/login/", **extra):
        return self.client.post(url, {"username": username, "password": password}, **extra)

    def test_username_locked_after_five_failures(self):
        for _ in range(5):
            r = self.login("alice", "wrong")
            self.assertEqual(r.status_code, 200)
            self.assertNotContains(r, "Too many failed sign-ins")
        r = self.login("alice", PW)   # even the right password is refused while locked
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, LOCKED)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertTrue(AuditEvent.objects.filter(action="login_locked", details__username="alice").exists())
        self.assertTrue(AuditEvent.objects.filter(action="login_throttled", result="denied").exists())
        # case variants share the counter
        self.assertContains(self.login("ALICE", PW), LOCKED)

    def test_lock_expires_and_success_clears_the_username_counter(self):
        for _ in range(4):
            self.login("alice", "wrong")
        r = self.login("alice", PW)
        self.assertEqual(r.status_code, 302)
        self.assertFalse(LoginThrottle.objects.filter(kind="user", value="alice").exists())
        self.client.logout()
        for _ in range(4):
            self.login("alice", "wrong")
        self.assertEqual(self.login("alice", PW).status_code, 302)
        self.client.logout()
        for _ in range(5):
            self.login("alice", "wrong")
        LoginThrottle.objects.filter(kind="user").update(locked_until=timezone.now() - timedelta(seconds=1),
                                                         window_started_at=timezone.now() - timedelta(hours=1))
        self.assertEqual(self.login("alice", PW).status_code, 302)

    def test_client_address_locked_after_twenty_failures(self):
        for i in range(20):
            self.login(f"nobody{i}", "wrong")
        r = self.login("alice", PW)
        self.assertContains(r, LOCKED)

    @override_settings(TRUSTED_PROXIES=["10.0.0.0/8"], LOGIN_MAX_FAILURES_PER_IP=3, LOGIN_WINDOW_MINUTES=5)
    def test_client_address_honours_trusted_proxies_and_settings(self):
        for i in range(3):
            self.login(f"x{i}", "wrong", REMOTE_ADDR="10.0.0.1", HTTP_X_FORWARDED_FOR="203.0.113.5")
        r = self.login("alice", PW, REMOTE_ADDR="10.0.0.1", HTTP_X_FORWARDED_FOR="203.0.113.5")
        self.assertContains(r, "Try again in 5 minutes.")
        r = self.login("alice", PW, REMOTE_ADDR="10.0.0.1", HTTP_X_FORWARDED_FOR="203.0.113.6")
        self.assertEqual(r.status_code, 302)

    def test_admin_login_is_throttled_too(self):
        # core.middleware.LoginRequiredMiddleware sends anonymous /admin/login/ requests to /accounts/login/, but the
        # admin site's own form is throttled as well in case that ever changes.
        from django.contrib import admin
        from django.test import RequestFactory

        from .forms import ThrottledAdminAuthenticationForm
        self.assertIs(admin.site.login_form, ThrottledAdminAuthenticationForm)
        User.objects.create_superuser("root", password=PW)
        rf = RequestFactory()

        def attempt(password):
            form = ThrottledAdminAuthenticationForm(rf.post("/admin/login/"), data={"username": "root", "password": password})
            return form.is_valid(), form

        for _ in range(5):
            self.assertFalse(attempt("wrong")[0])
        ok, form = attempt(PW)
        self.assertFalse(ok)
        self.assertIn(LOCKED, form.non_field_errors())

    def test_counter_is_stored_in_the_database(self):
        self.login("alice", "wrong")
        self.assertEqual(LoginThrottle.objects.get(kind="user", value="alice").failures, 1)
        self.assertEqual(LoginThrottle.objects.get(kind="ip", value="127.0.0.1").failures, 1)


class SiteRenameCascadeTests(TestCase):
    def setUp(self):
        self.site = Site.objects.create(name="DR Site", code="dr")
        self.admin = User.objects.create_user("boss", password=PW)
        Membership.objects.create(user=self.admin, site=None, role="admin")
        self.member = User.objects.create_user("m", password=PW)
        self.membership = Membership.objects.create(user=self.member, site=self.site, role="operator")
        self.client.force_login(self.admin)

    def make_records(self, site, tenant="default"):
        from automation.models import Execution, Rule, Runbook, Template
        from cmdb.models import ConfigurationItem
        from domainmonitor.models import Domain
        from network.models import Device
        from observe.models import Exporter, LogEntry
        from tcpcheck.models import TcpCheck
        from telemetry.models import Alert
        from urlchecker.models import UrlCheck
        t = dict(tenant_key=tenant)
        UrlCheck.objects.create(name="u", url="https://u.example/", site=site, **t)
        Domain.objects.create(hostname="d.example", site_key=site, **t)
        TcpCheck.objects.create(name="t", host="t.example", port=22, site=site, **t)
        Device.objects.create(name="sw", mgmt_ip="10.0.0.1", site=site, **t)
        ConfigurationItem.objects.create(name="ci", site=site, **t)
        Exporter.objects.create(name="ex", exporter_type="node", site=site, **t)
        LogEntry.objects.create(source="s", level="INFO", message="m", site=site, ts=timezone.now(), **t)
        Alert.objects.create(fingerprint=f"fp-{tenant}", alertname="A", site=site, **t)
        Runbook.objects.create(title="rb", steps="1. x", site=site, **t)
        Rule.objects.create(name="r", site=site, **t)
        Execution.objects.create(kind="rule", trigger="open_incident", site=site, **t)
        Template.objects.create(kind="url_check", name="tpl", fields={"site": site, "name": "x"}, **t)
        Event.objects.create(source="test", title="e", site=site, **t)

    def post(self, **extra):
        data = {"name": "DR Chennai", "code": "dr", "region": "", "timezone": "Asia/Kolkata", "criticality": "low", **extra}
        return self.client.post(reverse("administration:site_edit", args=[self.site.pk]), data, follow=True)

    def test_rename_updates_every_module_after_confirmation(self):
        self.make_records("DR Site")
        self.make_records("DR Site", tenant="other-tenant")
        r = self.post()
        self.assertContains(r, "Rename and update 13 records")
        refs = dict(r.context["rename_preview"]["refs"])
        self.assertEqual(refs["Log entries"], 1)
        self.assertEqual(refs["Automation templates"], 1)
        self.site.refresh_from_db()
        self.assertEqual(self.site.name, "DR Site")   # nothing changed before the confirmation

        r = self.post(confirm_rename="1")
        self.site.refresh_from_db()
        self.assertEqual(self.site.name, "DR Chennai")
        self.assertContains(r, "Renamed “DR Site” to “DR Chennai”")
        for label, qs, _f in services._site_name_querysets("DR Site"):
            self.assertEqual(qs.count(), 0, label)
        self.assertEqual(sum(qs.count() for _l, qs, _f in services._site_name_querysets("DR Chennai")), 12)
        from automation.models import Template
        self.assertEqual(Template.objects.get(tenant_key="default").fields["site"], "DR Chennai")
        self.assertEqual(Template.objects.get(tenant_key="other-tenant").fields["site"], "DR Site")
        from urlchecker.models import UrlCheck
        self.assertEqual(UrlCheck.objects.get(tenant_key="other-tenant").site, "DR Site")
        self.membership.refresh_from_db()
        self.assertEqual(self.membership.site_id, self.site.pk)
        a = AuditEvent.objects.get(action="update", object_type="core.site")
        self.assertEqual(a.details["renamed_records"]["URL checks"], 1)

    def test_rename_without_references_needs_no_confirmation(self):
        self.post()
        self.site.refresh_from_db()
        self.assertEqual(self.site.name, "DR Chennai")

    def test_model_list_covers_every_site_name_field(self):
        from django.apps import apps
        listed = {(a, m) for a, m, _f, _l in services.SITE_NAME_FIELDS}
        for model in apps.get_models():
            for f in model._meta.concrete_fields:
                if f.name in ("site", "site_key") and f.get_internal_type() == "CharField":
                    self.assertIn((model._meta.app_label, model.__name__), listed, model._meta.label)
