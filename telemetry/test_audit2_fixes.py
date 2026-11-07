"""Audit round 2 regressions: tenant-scoped Alertmanager upserts, stale out-of-order resolves, and non-ASCII
digits in primary-key parameters."""
import json

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import Event

from .models import Alert, SavedQuery

TOKEN = "t" * 32
T0 = "2026-10-01T00:00:00Z"


def payload(alerts):
    return json.dumps({"version": "4", "receiver": "le", "status": "firing", "alerts": alerts})


@override_settings(ALERTMANAGER_WEBHOOK_TOKEN=TOKEN, INCIDENT_AUTO_CREATE=False, NOTIFY_WEBHOOK_URLS=[],
                   NOTIFY_EMAILS=[])
class TenantScopedUpsertTests(TestCase):
    def post(self, alerts):
        r = self.client.post("/api/v1/alertmanager/", payload(alerts), content_type="application/json",
                             HTTP_AUTHORIZATION="Bearer " + TOKEN)
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def test_same_fingerprint_in_two_tenants_gives_two_rows(self):
        base = {"fingerprint": "aaaa", "status": "firing", "startsAt": T0}
        with override_settings(LINKEDEYE_TENANT="A"):
            self.post([dict(base, labels={"alertname": "Watchdog", "severity": "critical"})])
        with override_settings(LINKEDEYE_TENANT="B"):
            stats = self.post([dict(base, labels={"alertname": "Watchdog", "severity": "critical", "secret": "B-only"})])
            self.assertEqual((stats["created"], stats["updated"], stats["events"]), (1, 0, 1))
            self.assertEqual(Alert.objects.for_tenant().count(), 1)
            b = Alert.objects.for_tenant().get()
        a = Alert.objects.get(tenant_key="A", fingerprint="aaaa")
        self.assertNotEqual(a.pk, b.pk)
        self.assertEqual(a.receive_count, 1)
        self.assertNotIn("secret", a.labels)                      # B's labels never land on A's row
        self.assertEqual(b.labels["secret"], "B-only")
        # B resolving its alert leaves A's alert firing
        with override_settings(LINKEDEYE_TENANT="B"):
            self.post([dict(base, status="resolved", endsAt="2026-10-01T01:00:00Z",
                            labels={"alertname": "Watchdog", "severity": "critical", "secret": "B-only"})])
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual((a.status, b.status), ("firing", "resolved"))

    def test_alert_detail_shows_only_this_tenants_events(self):
        a = Alert.objects.create(fingerprint="x", alertname="Watchdog", status="firing", severity="critical")
        Event.objects.create(tenant_key=a.tenant_key, source="telemetry", title="mine", severity="critical",
                             fingerprint="am:x")
        Event.objects.create(tenant_key="other-tenant", source="telemetry", title="theirs", severity="critical",
                             fingerprint="am:x")
        self.client.force_login(User.objects.create_user("v", password="x"))
        r = self.client.get(reverse("telemetry:alert_detail", args=[a.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertEqual([e.title for e in r.context["events"]], ["mine"])

    def test_seed_command_does_not_steal_another_tenants_rows(self):
        from django.core.management import call_command
        with override_settings(LINKEDEYE_TENANT="A"):
            call_command("seed_telemetry", verbosity=0)
        with override_settings(LINKEDEYE_TENANT="B"):
            call_command("seed_telemetry", verbosity=0)
            n_b = Alert.objects.for_tenant().count()
        self.assertGreater(n_b, 0)
        self.assertEqual(Alert.objects.filter(tenant_key="A").count(), n_b)


@override_settings(ALERTMANAGER_WEBHOOK_TOKEN=TOKEN, INCIDENT_AUTO_CREATE=False, NOTIFY_WEBHOOK_URLS=[],
                   NOTIFY_EMAILS=[])
class StaleNotificationTests(TestCase):
    def post(self, alerts):
        r = self.client.post("/api/v1/alertmanager/", payload(alerts), content_type="application/json",
                             HTTP_AUTHORIZATION="Bearer " + TOKEN)
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def test_late_resolve_of_the_previous_instance_does_not_resolve_a_newer_firing(self):
        base = {"fingerprint": "abc", "labels": {"alertname": "Watchdog", "severity": "critical"}}
        self.post([dict(base, status="firing", startsAt=T0)])
        self.post([dict(base, status="resolved", startsAt=T0, endsAt="2026-10-01T01:00:00Z")])
        self.post([dict(base, status="firing", startsAt="2026-10-01T02:00:00Z")])      # genuine re-fire
        n = Event.objects.filter(fingerprint="am:abc").count()
        # a delayed/retried "resolved" for the FIRST instance arrives after the re-fire
        stats = self.post([dict(base, status="resolved", startsAt=T0, endsAt="2026-10-01T01:00:00Z")])
        a = Alert.objects.get(fingerprint="abc")
        self.assertEqual(a.status, "firing")
        self.assertEqual(a.starts_at.isoformat(), "2026-10-01T02:00:00+00:00")
        self.assertEqual((stats["events"], a.receive_count, a.fire_count), (0, 4, 2))
        self.assertEqual(Event.objects.filter(fingerprint="am:abc").count(), n)
        # the resolve of the current instance still lands
        self.post([dict(base, status="resolved", startsAt="2026-10-01T02:00:00Z", endsAt="2026-10-01T03:00:00Z")])
        a.refresh_from_db()
        self.assertEqual(a.status, "resolved")

    def test_stale_firing_of_the_previous_instance_does_not_move_the_start_back(self):
        base = {"fingerprint": "abc", "labels": {"alertname": "Watchdog", "severity": "critical"}}
        self.post([dict(base, status="firing", startsAt=T0)])
        self.post([dict(base, status="resolved", startsAt=T0, endsAt="2026-10-01T01:00:00Z")])
        self.post([dict(base, status="firing", startsAt="2026-10-01T02:00:00Z")])
        stats = self.post([dict(base, status="firing", startsAt=T0)])     # repeat of the old instance
        a = Alert.objects.get(fingerprint="abc")
        self.assertEqual(stats["events"], 0)
        self.assertEqual((a.status, a.starts_at.isoformat(), a.receive_count), ("firing", "2026-10-01T02:00:00+00:00", 4))
        # ... so the old instance's resolve, arriving after it, is still recognised as stale
        self.post([dict(base, status="resolved", startsAt=T0, endsAt="2026-10-01T01:00:00Z")])
        a.refresh_from_db()
        self.assertEqual(a.status, "firing")

    def test_resolve_with_merged_earlier_starts_at_still_resolves(self):
        # an HA Prometheus pair: Alertmanager merged the instance to the earlier startsAt after our first notification
        base = {"fingerprint": "abc", "labels": {"alertname": "Watchdog", "severity": "critical"}}
        self.post([dict(base, status="firing", startsAt="2026-10-01T00:00:10Z")])
        self.post([dict(base, status="resolved", startsAt=T0, endsAt="2026-10-01T00:20:00Z")])
        a = Alert.objects.get(fingerprint="abc")
        self.assertEqual(a.status, "resolved")
        self.assertEqual(Event.objects.filter(fingerprint="am:abc").latest("pk").severity, "ok")


class UnicodeDigitTests(TestCase):
    def setUp(self):
        self.op = User.objects.create_user("op", password="x", is_staff=True)
        self.client.force_login(self.op)

    def test_explorer_saved_param_with_unicode_digit(self):
        r = self.client.get(reverse("telemetry:explorer") + "?saved=%C2%B2")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.context["loaded"])

    def test_explorer_saved_param_with_huge_number(self):
        r = self.client.get(reverse("telemetry:explorer") + "?saved=99999999999999999999999")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.context["loaded"])

    def test_explorer_saved_param_still_loads_a_real_query(self):
        q = SavedQuery.objects.create(name="Q", promql="up", owner=self.op)
        r = self.client.get(reverse("telemetry:explorer") + f"?saved={q.pk}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["loaded"].pk, q.pk)

    def test_bulk_acknowledge_with_unicode_digit_ids(self):
        a = Alert.objects.create(fingerprint="x", alertname="Watchdog", status="firing", severity="critical")
        r = self.client.post(reverse("telemetry:alert_ack_bulk"), {"ids": ["²", "99999999999999999999999", str(a.pk)]})
        self.assertEqual(r.status_code, 302)
        a.refresh_from_db()
        self.assertIsNotNone(a.acknowledged_at)
        r = self.client.post(reverse("telemetry:alert_ack_bulk"), {"ids": ["²"]})
        self.assertEqual(r.status_code, 302)
