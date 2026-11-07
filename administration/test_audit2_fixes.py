"""Second audit round: probe freshness follows the configured check intervals, activate/deactivate applies the
requested state (idempotent), and the users / audit-log filters never raise on odd input."""
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import AuditEvent, Event, Membership, Site
from domainmonitor.models import Domain, DomainCheck
from urlchecker.models import UrlCheck, UrlResult

from . import jobs, services

PW = "Corr3ct-Horse-Battery-77"


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class Base(TestCase):
    def setUp(self):
        self.mum = Site.objects.create(name="Mumbai (NSE)", code="mumbai-nse")
        self.admin = User.objects.create_user("boss", "boss@example.com", PW)
        Membership.objects.create(user=self.admin, site=None, role="admin")
        self.viewer = User.objects.create_user("view", "view@example.com", PW)
        self.client.force_login(self.admin)


@override_settings(INCIDENT_AUTO_CREATE=False, NOTIFY_WEBHOOK_URLS=[], NOTIFY_EMAILS=[])
class ProbeFreshnessIntervalTests(TestCase):
    def rows(self):
        return {r["key"]: r for r in services.probe_freshness()}

    def test_url_results_within_a_15_minute_interval_are_fresh(self):
        c = UrlCheck.objects.create(name="svc", url="https://svc.example", interval_seconds=900)
        UrlResult.objects.create(url_check=c, checked_at=timezone.now() - timedelta(minutes=11), status="online")
        self.assertEqual(self.rows()["url"]["status"], "ok")
        self.assertIsNone(jobs.heartbeat())          # the watchdog does not warn either
        self.assertFalse(Event.objects.filter(fingerprint=jobs.FINGERPRINT).exists())
        # but a result older than the interval plus the grace period is stale
        UrlResult.objects.filter(url_check=c).update(checked_at=timezone.now() - timedelta(minutes=25))
        self.assertEqual(self.rows()["url"]["status"], "warn")
        self.assertEqual(jobs.heartbeat(), "probe results stale")

    def test_default_interval_keeps_the_ten_minute_threshold(self):
        c = UrlCheck.objects.create(name="svc", url="https://svc.example")   # 5-minute interval
        UrlResult.objects.create(url_check=c, checked_at=timezone.now() - timedelta(minutes=11), status="online")
        self.assertEqual(self.rows()["url"]["status"], "warn")

    def test_disabled_checks_do_not_raise_the_threshold(self):
        c = UrlCheck.objects.create(name="svc", url="https://svc.example")
        UrlCheck.objects.create(name="slow", url="https://slow.example", interval_seconds=900, enabled=False)
        UrlResult.objects.create(url_check=c, checked_at=timezone.now() - timedelta(minutes=11), status="online")
        self.assertEqual(self.rows()["url"]["status"], "warn")

    def test_domain_results_within_an_hourly_interval_are_fresh(self):
        d = Domain.objects.create(tenant_key="default", site_key="Mumbai", hostname="shop.example", check_interval_seconds=3600)
        DomainCheck.objects.create(domain=d, checked_at=timezone.now() - timedelta(minutes=30), up=True,
                                   dns_status="Resolved", ssl_status="Valid")
        row = self.rows()["domain"]
        self.assertEqual(row["status"], "ok")
        self.assertGreaterEqual(row["stale_after"], 3600)
        DomainCheck.objects.filter(domain=d).update(checked_at=timezone.now() - timedelta(minutes=70))
        self.assertEqual(self.rows()["domain"]["status"], "warn")

    def test_health_page_shows_the_threshold(self):
        admin = User.objects.create_superuser("root", password=PW)
        UrlCheck.objects.create(name="svc", url="https://svc.example", interval_seconds=900)
        self.client.force_login(admin)
        r = self.client.get(reverse("administration:health"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "20 min")


class ToggleActiveTests(Base):
    def test_deactivate_twice_leaves_the_user_deactivated(self):
        url = reverse("administration:user_toggle_active", args=[self.viewer.pk])
        self.assertEqual(self.client.post(url, {"active": "0"}).status_code, 302)
        self.assertEqual(self.client.post(url, {"active": "0"}).status_code, 302)
        self.viewer.refresh_from_db()
        self.assertFalse(self.viewer.is_active)
        self.assertEqual(list(AuditEvent.objects.order_by("pk").values_list("action", flat=True)), ["deactivate"])
        self.client.post(url, {"active": "1", "next": "list"})
        self.viewer.refresh_from_db()
        self.assertTrue(self.viewer.is_active)
        self.assertEqual(list(AuditEvent.objects.order_by("pk").values_list("action", flat=True)), ["deactivate", "activate"])

    def test_missing_or_invalid_state_changes_nothing(self):
        url = reverse("administration:user_toggle_active", args=[self.viewer.pk])
        for data in ({}, {"active": "maybe"}):
            r = self.client.post(url, data, follow=True)
            self.assertEqual(r.status_code, 200)
            self.assertTrue(User.objects.get(pk=self.viewer.pk).is_active)
        self.assertFalse(AuditEvent.objects.exists())

    def test_forms_send_the_intended_state(self):
        self.viewer.is_active = False
        self.viewer.save(update_fields=["is_active"])
        r = self.client.get(reverse("administration:user_edit", args=[self.viewer.pk]))
        self.assertContains(r, 'name="active" value="1"')
        User.objects.create_user("other", "other@example.com", PW)   # active: its row offers "deactivate"
        r = self.client.get(reverse("administration:users"))
        self.assertContains(r, 'name="active" value="1"')   # the deactivated viewer's row offers "reactivate"
        self.assertContains(r, 'name="active" value="0"')


class FilterRobustnessTests(Base):
    def test_audit_log_dates_at_the_extremes_are_rejected_not_500(self):
        AuditEvent.objects.create(actor_name="root", action="update", object_type="auth.user")
        for params in ("to=9999-12-31", "from=0001-01-01", "from=0001-01-01&to=9999-12-31"):
            r = self.client.get(reverse("administration:audit") + "?" + params)
            self.assertEqual(r.status_code, 200, params)
            self.assertTrue(r.context["date_error"], params)
            # the out-of-range date is ignored, so every row stays listed (each export above adds an audit row)
            self.assertEqual(r.context["page"].paginator.count, AuditEvent.objects.count())
            r = self.client.get(reverse("administration:audit_export") + "?" + params)
            self.assertEqual(r.status_code, 200, params)
        r = self.client.get(reverse("administration:audit") + "?from=2020-01-01&to=2030-12-31")
        self.assertFalse(r.context["date_error"])

    def test_users_site_filter_with_non_ascii_digits_is_empty_not_500(self):
        for site in ("²", "①", "1e3", "-1", "+1"):
            r = self.client.get(reverse("administration:users"), {"site": site})
            self.assertEqual(r.status_code, 200, site)
            self.assertEqual(r.context["page"].paginator.count, 0, site)
            self.assertEqual(self.client.get(reverse("administration:users_export"), {"site": site}).status_code, 200, site)
        Membership.objects.create(user=self.viewer, site=self.mum, role="viewer")
        r = self.client.get(reverse("administration:users"), {"site": str(self.mum.pk)})
        self.assertEqual([u.pk for u in r.context["page"]], [self.viewer.pk])
