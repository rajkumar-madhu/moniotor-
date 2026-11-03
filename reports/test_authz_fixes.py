"""F02: only the owner or an administrator of every site may manage (edit, send, delete) a schedule."""
from unittest import mock

from django.contrib.auth.models import User
from django.core import mail
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import Membership, Site

from .models import ReportRun, ScheduledReport


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend", NOTIFY_WEBHOOK_URLS=[],
                   NOTIFY_EMAILS=[])
class ScheduleOwnershipTests(TestCase):
    def setUp(self):
        p = mock.patch("core.notify._post", side_effect=AssertionError("no network in tests"))
        p.start()
        self.addCleanup(p.stop)
        mum = Site.objects.create(name="Mumbai", code="mum")
        Site.objects.create(name="DR", code="dr")
        self.madm = User.objects.create_user("madm", password="x")
        Membership.objects.create(user=self.madm, site=mum, role="admin")
        self.gadm = User.objects.create_user("gadm", "g@x.com", "x")
        Membership.objects.create(user=self.gadm, site=None, role="admin")
        self.schedule = ScheduledReport.objects.create(name="Weekly", report_key="incidents", window_days=7,
                                                       frequency="weekly", recipients="g@x.com", owner=self.gadm)
        now = timezone.now()
        self.run = ReportRun.objects.create(report_key="incidents", channel="email", schedule=self.schedule,
                                            requested_by=self.gadm, period_start=now, period_end=now)

    def c(self, user):
        c = Client()
        c.force_login(user)
        return c

    def edit_data(self, recipients):
        return {"name": "Weekly", "report_key": "incidents", "window_days": 7, "site": "", "frequency": "weekly",
                "send_hour": 7, "recipients": recipients, "enabled": "on"}

    def test_single_site_admin_cannot_manage_someone_elses_schedule(self):
        c = self.c(self.madm)
        pk = self.schedule.pk
        self.assertEqual(c.get(reverse("reports:schedule_edit", args=[pk])).status_code, 403)
        self.assertEqual(c.post(reverse("reports:schedule_edit", args=[pk]), self.edit_data("attacker@evil.test")).status_code, 403)
        self.assertEqual(c.post(reverse("reports:schedule_send", args=[pk])).status_code, 403)
        self.assertEqual(c.post(reverse("reports:schedule_delete", args=[pk])).status_code, 403)
        self.schedule.refresh_from_db()
        self.assertEqual(self.schedule.recipients, "g@x.com")
        self.assertEqual(mail.outbox, [])

    def test_index_lists_only_own_schedules_for_single_site_admin(self):
        r = self.c(self.madm).get(reverse("reports:index"))
        self.assertEqual(r.context["schedules"], [])
        self.assertEqual(r.context["runs"], [])
        r = self.c(self.gadm).get(reverse("reports:index"))
        self.assertEqual([s.pk for s in r.context["schedules"]], [self.schedule.pk])
        self.assertTrue(r.context["schedules"][0].manageable)

    def test_tenant_wide_admin_and_owner_can_manage(self):
        own = ScheduledReport.objects.create(name="Mine", report_key="incidents", recipients="m@x.com", owner=self.madm)
        r = self.c(self.madm).post(reverse("reports:schedule_edit", args=[own.pk]), self.edit_data("m2@x.com"))
        self.assertEqual(r.status_code, 302)
        r = self.c(self.gadm).post(reverse("reports:schedule_edit", args=[self.schedule.pk]), self.edit_data("g2@x.com"))
        self.assertEqual(r.status_code, 302)
        self.schedule.refresh_from_db()
        self.assertEqual(self.schedule.recipients, "g2@x.com")
