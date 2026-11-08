"""Review fixes: console SMTP settings reach scheduled reports, exports are never silently truncated, odd dates
do not crash the preview, and a schedule's site is validated server-side."""
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core import mail
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from administration import integration_config as ic
from core.models import Event, Membership, Site

from . import registry, scheduler
from .models import ReportRun, ScheduledReport

PW = "Corr3ct-Horse-Battery-77"


class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        p = mock.patch("core.notify._post", side_effect=AssertionError("no network in tests"))
        p.start()
        self.addCleanup(p.stop)
        self.alpha = Site.objects.create(name="Site Alpha", code="alpha")
        self.beta = Site.objects.create(name="Site Beta", code="beta")
        self.gadm = User.objects.create_user("gadm", "g@example.com", PW)
        Membership.objects.create(user=self.gadm, site=None, role="admin")
        self.op_a = User.objects.create_user("opa", "a@example.com", PW)
        Membership.objects.create(user=self.op_a, site=self.alpha, role="operator")
        self.only_b = User.objects.create_user("onlyb", "b@example.com", PW)
        Membership.objects.create(user=self.only_b, site=self.beta, role="viewer")

    def login(self, user):
        c = Client()
        c.force_login(user)
        return c


@override_settings(NOTIFY_WEBHOOK_URLS=[], NOTIFY_EMAILS=[], EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
                   DEFAULT_FROM_EMAIL="linkedeye@localhost", EMAIL_HOST="localhost", EMAIL_PORT=25)
class SchedulerSmtpTests(Base):
    def make(self):
        return ScheduledReport.objects.create(name="Weekly", report_key="incidents", recipients="x@example.com",
                                              owner=self.gadm)

    def test_console_smtp_and_from_are_used(self):
        ic.save("email", {"email_host": "smtp.corp.example", "email_port": 587, "email_host_user": "mailer",
                          "email_host_password": "pw", "email_use_tls": True, "default_from_email": "noc@corp.example"},
                self.gadm)
        seen = {}
        real_get = scheduler.get_connection

        def spy(**kw):
            seen.update(kw)
            return real_get(backend="django.core.mail.backends.locmem.EmailBackend")

        with mock.patch.object(scheduler, "get_connection", side_effect=spy):
            run = scheduler.deliver(self.make())
        self.assertNotEqual(run.status, "failed", run.error)
        self.assertEqual(seen["host"], "smtp.corp.example")
        self.assertEqual(seen["port"], 587)
        self.assertEqual(seen["username"], "mailer")
        self.assertEqual(seen["password"], "pw")
        self.assertTrue(seen["use_tls"])
        self.assertEqual(mail.outbox[0].from_email, "noc@corp.example")

    def test_environment_fallback_without_console_settings(self):
        with mock.patch.object(scheduler, "get_connection", side_effect=AssertionError("not for env settings")):
            run = scheduler.deliver(self.make())
        self.assertNotEqual(run.status, "failed", run.error)
        self.assertEqual(mail.outbox[0].from_email, "linkedeye@localhost")


@override_settings(NOTIFY_WEBHOOK_URLS=[], NOTIFY_EMAILS=[], EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class TruncationTests(Base):
    def setUp(self):
        super().setUp()
        for i in range(6):
            Event.objects.create(source="t", title=f"e{i}", severity="info", occurred_at=timezone.now())

    def test_download_refuses_instead_of_truncating(self):
        with mock.patch.object(registry, "MAX_ROWS", 3):
            r = self.login(self.gadm).get(reverse("reports:download", args=["events"]), follow=True)
        self.assertNotEqual(r.get("Content-Type", ""), "text/csv; charset=utf-8")
        self.assertContains(r, "more than 3 rows")
        run = ReportRun.objects.get(channel="download")
        self.assertEqual(run.status, "failed")

    def test_download_at_the_limit_is_complete(self):
        n = registry.generate(registry.REPORTS["events"], registry.Ctx(timezone.now() - timedelta(days=1), timezone.now(),
                                                                       self.gadm, ""))[0]
        with mock.patch.object(registry, "MAX_ROWS", len(n)):
            r = self.login(self.gadm).get(reverse("reports:download", args=["events"]))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.content.decode().strip().splitlines()) - 1, len(n))

    def test_scheduled_email_fails_loudly_instead_of_sending_a_partial_csv(self):
        s = ScheduledReport.objects.create(name="Ev", report_key="events", recipients="x@example.com", owner=self.gadm)
        with mock.patch.object(registry, "MAX_ROWS", 3):
            run = scheduler.deliver(s)
        self.assertEqual(run.status, "failed")
        self.assertIn("more than 3 rows", run.error)
        self.assertEqual(mail.outbox, [])
        self.assertTrue(Event.objects.filter(source="reports", severity="warning").exists())

    def test_preview_cap_is_unchanged(self):
        r = self.login(self.gadm).get(reverse("reports:preview", args=["events"]))
        self.assertEqual(r.status_code, 200)


class PeriodOverflowTests(Base):
    def test_far_future_dates_do_not_crash(self):
        c = self.login(self.gadm)
        q = "?from=9999-12-31&to=9999-12-31"
        self.assertEqual(c.get(reverse("reports:preview", args=["events"]) + q).status_code, 200)
        self.assertEqual(c.get(reverse("reports:download", args=["events"]) + q).status_code, 200)

    def test_far_past_dates_do_not_crash(self):
        c = self.login(self.gadm)
        q = "?from=0001-01-01&to=0001-01-02"
        self.assertEqual(c.get(reverse("reports:preview", args=["events"]) + q).status_code, 200)


@override_settings(NOTIFY_WEBHOOK_URLS=[], NOTIFY_EMAILS=[], EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class ScheduleSiteValidationTests(Base):
    def data(self, site, **extra):
        d = {"name": "URGENT: contact helpdesk", "report_key": "incidents", "window_days": 7, "site": site,
             "frequency": "daily", "send_hour": 8, "recipients": "x@example.com", "enabled": "on"}
        d.update(extra)
        return d

    def test_foreign_site_is_rejected_on_create(self):
        r = self.login(self.op_a).post(reverse("reports:schedule_new"), self.data("Site Beta"))
        self.assertEqual(r.status_code, 200)
        self.assertIn("site", r.context["form"].errors)
        self.assertFalse(ScheduledReport.objects.exists())

    def test_nonexistent_site_is_rejected(self):
        r = self.login(self.op_a).post(reverse("reports:schedule_new"), self.data("Nonexistent"))
        self.assertEqual(r.status_code, 200)
        self.assertFalse(ScheduledReport.objects.exists())

    def test_own_site_and_blank_are_accepted(self):
        c = self.login(self.op_a)
        self.assertEqual(c.post(reverse("reports:schedule_new"), self.data("Site Alpha")).status_code, 302)
        self.assertEqual(c.post(reverse("reports:schedule_new"), self.data("", name="all mine")).status_code, 302)
        self.assertEqual(sorted(ScheduledReport.objects.values_list("site", flat=True)), ["", "Site Alpha"])

    def test_foreign_site_is_rejected_on_edit(self):
        s = ScheduledReport.objects.create(name="Mine", report_key="incidents", recipients="x@example.com",
                                           owner=self.op_a, site="Site Alpha")
        r = self.login(self.op_a).post(reverse("reports:schedule_edit", args=[s.pk]), self.data("Site Beta"))
        self.assertEqual(r.status_code, 200)
        s.refresh_from_db()
        self.assertEqual(s.site, "Site Alpha")

    def test_view_only_site_is_not_operable(self):
        Membership.objects.create(user=self.op_a, site=self.beta, role="viewer")
        r = self.login(self.op_a).post(reverse("reports:schedule_new"), self.data("Site Beta"))
        self.assertEqual(r.status_code, 200)
        self.assertFalse(ScheduledReport.objects.exists())

    def test_tenant_admin_may_pick_any_real_site(self):
        r = self.login(self.gadm).post(reverse("reports:schedule_new"), self.data("Site Beta"))
        self.assertEqual(r.status_code, 302)

    def test_stored_foreign_site_cannot_inject_an_event_on_another_site(self):
        s = ScheduledReport.objects.create(name="URGENT: call evil", report_key="incidents",
                                           recipients="x@example.com", owner=self.op_a, site="Site Beta")
        with mock.patch.object(scheduler, "get_connection", side_effect=AssertionError):
            run = scheduler.deliver(s)
        self.assertEqual(run.status, "failed")
        self.assertIn("site", run.error.lower())
        self.assertEqual(mail.outbox, [])
        ev = Event.objects.get(source="reports")
        self.assertEqual(ev.site, "")
