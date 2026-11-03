"""Regression tests: per-request event cap for Alertmanager notifications (F10), stale duplicate "firing" after a
resolve (F16) and concurrent first inserts of one fingerprint."""
import json
from unittest import mock

from django.db import IntegrityError
from django.test import TestCase, override_settings

from core.models import Event

from . import alerts as svc
from .models import Alert

TOKEN = "t" * 32
T0 = "2026-11-01T10:00:00Z"


def payload(alerts):
    return json.dumps({"version": "4", "receiver": "le", "status": "firing", "alerts": alerts})


@override_settings(ALERTMANAGER_WEBHOOK_TOKEN=TOKEN, INCIDENT_AUTO_CREATE=False, NOTIFY_WEBHOOK_URLS=[],
                   NOTIFY_EMAILS=[])
class AlertPipelineTests(TestCase):
    def post(self, alerts):
        r = self.client.post("/api/v1/alertmanager/", payload(alerts), content_type="application/json",
                             HTTP_AUTHORIZATION="Bearer " + TOKEN)
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def test_alert_storm_emits_at_most_the_cap_plus_one_summary(self):
        storm = [{"fingerprint": f"f{i}", "status": "firing",
                  "labels": {"alertname": f"A{i}", "severity": "critical" if i % 10 == 9 else "warning"}}
                 for i in range(300)]
        with mock.patch("automation.services.evaluate_event") as rules:
            stats = self.post(storm)
        self.assertEqual(Alert.objects.count(), 300)                     # every row is upserted
        self.assertEqual(stats["events"], svc.MAX_EVENTS_PER_INGEST + 1)
        self.assertEqual(stats["suppressed"], 300 - svc.MAX_EVENTS_PER_INGEST)
        self.assertEqual(rules.call_count, svc.MAX_EVENTS_PER_INGEST + 1)  # listeners ran 51 times, not 300
        per_alert = Event.objects.filter(source="telemetry").exclude(fingerprint=svc.OVERFLOW_FINGERPRINT)
        # the most severe changes get their own event first
        self.assertEqual(per_alert.filter(severity="critical").count(), 30)
        summary = Event.objects.get(fingerprint=svc.OVERFLOW_FINGERPRINT)
        self.assertEqual(summary.title, "250 more alerts changed state")
        self.assertEqual(summary.severity, "warning")
        self.assertEqual(summary.data["count"], 250)

    def test_small_notifications_are_unchanged(self):
        stats = self.post([{"fingerprint": "one", "labels": {"alertname": "X", "severity": "critical"}}])
        self.assertEqual((stats["events"], stats["suppressed"]), (1, 0))
        self.assertFalse(Event.objects.filter(fingerprint=svc.OVERFLOW_FINGERPRINT).exists())

    def test_late_duplicate_firing_after_resolve_is_not_a_refire(self):
        base = {"fingerprint": "abc", "labels": {"alertname": "X", "severity": "critical"}, "startsAt": T0}
        self.post([dict(base, status="firing")])
        self.post([dict(base, status="resolved", endsAt="2026-11-01T10:05:00Z")])
        n = Event.objects.filter(fingerprint="am:abc").count()
        # delayed copy of the ORIGINAL firing notification (same startsAt), e.g. from an Alertmanager HA peer
        stats = self.post([dict(base, status="firing")])
        a = Alert.objects.get(fingerprint="abc")
        self.assertEqual((a.status, a.fire_count, a.receive_count), ("resolved", 1, 3))
        self.assertEqual(stats["events"], 0)
        self.assertEqual(Event.objects.filter(fingerprint="am:abc").count(), n)
        # a genuine new firing starts after the resolve
        self.post([dict(base, status="firing", startsAt="2026-11-01T10:30:00Z")])
        a.refresh_from_db()
        self.assertEqual((a.status, a.fire_count), ("firing", 2))
        self.assertEqual(Event.objects.filter(fingerprint="am:abc").latest("pk").data["reason"], "refired")

    def test_concurrent_first_insert_becomes_an_update(self):
        # another request committed the row after our SELECT ... FOR UPDATE found nothing: our INSERT then hits the
        # unique fingerprint and the retry takes the update path
        Alert.objects.create(fingerprint="race", alertname="X", status="firing", severity="critical", receive_count=1)
        real_first, calls = svc.Alert.objects.none().__class__.first, []

        def first(qs):
            calls.append(1)
            return None if len(calls) == 1 else real_first(qs)

        raw = {"fingerprint": "race", "labels": {"alertname": "X", "severity": "critical", "site": "DR"}}
        with mock.patch.object(svc.Alert.objects.none().__class__, "first", first):
            stats = svc.ingest({"receiver": "le"}, [raw])
        a = Alert.objects.get(fingerprint="race")
        self.assertEqual((stats["created"], stats["updated"], stats["events"]), (0, 1, 0))
        self.assertEqual(a.receive_count, 2)

    def test_fingerprint_is_unique(self):
        Alert.objects.create(fingerprint="dup", alertname="X")
        with self.assertRaises(IntegrityError):
            Alert.objects.create(fingerprint="dup", alertname="Y")
