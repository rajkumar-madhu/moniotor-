"""Second audit round: the audit-trail report needs the admin role on every site (not admin on one site plus a
tenant-wide viewer membership), and the Analytics "previous window" is as long as the elapsed current window."""
from datetime import datetime, timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import AuditEvent, Membership, Site
from urlchecker.models import UrlCheck, UrlResult

from . import analytics as an
from . import registry


class AuditReportRoleTests(TestCase):
    """A viewer of every site who is admin on one site is not an administrator of every site."""

    def setUp(self):
        self.mum = Site.objects.create(name="Mumbai", code="mum")
        Site.objects.create(name="Delhi", code="del")
        self.mixed = User.objects.create_user("mix", "mix@example.com", "x")
        Membership.objects.create(user=self.mixed, site=None, role="viewer")
        Membership.objects.create(user=self.mixed, site=self.mum, role="admin")
        AuditEvent.objects.create(actor_name="root", action="update", object_type="auth.user", object_repr="SECRET-ROW")

    def test_mixed_membership_user_cannot_run_the_audit_report(self):
        self.assertFalse(registry.REPORTS["audit"].allowed(self.mixed))
        self.client.force_login(self.mixed)
        self.assertEqual(self.client.get(reverse("reports:download", args=["audit"])).status_code, 403)
        self.assertEqual(self.client.get(reverse("reports:preview", args=["audit"])).status_code, 403)
        self.assertNotContains(self.client.get(reverse("reports:index")), "Audit trail")
        # the same rule the administration console applies
        self.assertEqual(self.client.get(reverse("administration:audit")).status_code, 403)
        resp = self.client.post(reverse("reports:schedule_new"), {
            "name": "Audit", "report_key": "audit", "window_days": 1, "frequency": "daily", "send_hour": 8,
            "recipients": "x@example.com"})
        self.assertContains(resp, "Choose a report you are allowed to run")

    def test_site_scoped_reports_still_open_to_the_same_user(self):
        self.assertTrue(registry.REPORTS["incidents"].allowed(self.mixed))
        self.assertTrue(registry.REPORTS["events"].allowed(self.mixed))

    def test_tenant_wide_admin_and_superuser_still_allowed(self):
        gadm = User.objects.create_user("gadm", password="x")
        Membership.objects.create(user=gadm, site=None, role="admin")
        self.assertTrue(registry.REPORTS["audit"].allowed(gadm))
        self.assertTrue(registry.REPORTS["audit"].allowed(User.objects.create_superuser("root", password="x")))


class AnalyticsWindowTests(TestCase):
    def test_previous_window_is_as_long_as_the_elapsed_current_window(self):
        now = timezone.make_aware(datetime(2026, 3, 10, 1, 0))   # 01:00 local: today has barely started
        w = an.Window(7, now=now)
        self.assertEqual(w.start, timezone.make_aware(datetime(2026, 3, 4, 0, 0)))
        self.assertEqual(w.start - w.prev_start, w.now - w.start)
        self.assertEqual(w.start - w.prev_start, timedelta(days=6, hours=1))

    def test_availability_change_compares_equal_windows(self):
        now = timezone.make_aware(datetime(2026, 3, 10, 1, 0))
        w = an.Window(7, now=now)
        c = UrlCheck.objects.create(name="svc", url="https://svc.example/", site="All Sites")
        UrlResult.objects.create(url_check=c, checked_at=w.start - timedelta(hours=1), status="online", response_ms=10)
        UrlResult.objects.create(url_check=c, checked_at=w.start + timedelta(minutes=30), status="online", response_ms=10)
        # 6.5 days before the current window started: outside a 6-day-1-hour previous window
        UrlResult.objects.create(url_check=c, checked_at=w.start - timedelta(days=6, hours=12), status="down", response_ms=None)
        admin = User.objects.create_superuser("root", password="x")
        av = an.availability(admin, w)
        self.assertEqual(av["url"], 100.0)
        self.assertEqual(av["url_prev"], 100.0)
        self.assertEqual(av["url_change"], 0.0)
