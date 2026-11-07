"""Regression tests: open_incident serialised per fingerprint (F12) and transition() saving only what it changed, so a
concurrently flagged SLA breach is not announced twice (F19)."""
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone

from core.models import Event

from . import services, sla
from .models import IncidentKey, Ticket


@override_settings(NOTIFY_WEBHOOK_URLS=[], NOTIFY_EMAILS=[], INCIDENT_AUTO_CREATE=True)
class PipelineTests(TestCase):
    def setUp(self):
        p = mock.patch("core.notify._post", side_effect=AssertionError("network call in tests"))
        p.start()
        self.addCleanup(p.stop)
        self.root = User.objects.create_superuser("root", "r@x.com", "x")

    def test_incident_opened_by_a_concurrent_caller_is_reused(self):
        """Simulates the race: while this call waited for the fingerprint lock, another one opened the incident.
        The open-incident lookup must happen after the lock, so this call adds a note instead of a duplicate."""
        real_lock = services._lock_fingerprint

        def lock_after_other_caller(fp):
            other = services._create_ticket_rows(kind="incident", title="first", fingerprint=fp, priority="P2")
            real_lock(fp)
            return other

        with mock.patch.object(services, "_lock_fingerprint", side_effect=lock_after_other_caller):
            t = services.open_incident("second", fingerprint="url:7", priority="P2", source="urlchecker")
        self.assertEqual(Ticket.objects.filter(fingerprint="url:7").count(), 1)
        self.assertEqual(t.title, "first")
        self.assertTrue(t.timeline.filter(body__startswith="Duplicate alert received").exists())

    def test_one_lock_row_per_fingerprint(self):
        a = services.open_incident("down", fingerprint="tcp:1", priority="P2")
        b = services.open_incident("down again", fingerprint="tcp:1", priority="P1")
        self.assertEqual(a.pk, b.pk)
        self.assertEqual(IncidentKey.objects.filter(fingerprint="tcp:1").count(), 1)
        self.assertEqual(Ticket.objects.get(pk=a.pk).priority, "P1")
        # a resolved incident does not block a new one for the same fingerprint
        services.transition(Ticket.objects.get(pk=a.pk), "resolved", force=True)
        c = services.open_incident("down once more", fingerprint="tcp:1")
        self.assertNotEqual(c.pk, a.pk)
        self.assertEqual(IncidentKey.objects.filter(fingerprint="tcp:1").count(), 1)

    def test_events_are_emitted_after_the_locked_block(self):
        t = services.open_incident("down", fingerprint="dom:3", priority="P1")
        self.assertTrue(Event.objects.filter(kind="ticket_opened", fingerprint=f"servicedesk:{t.pk}").exists())

    def test_transition_does_not_clobber_a_concurrent_breach_flag(self):
        t = services.create_ticket(user=self.root, kind="incident", title="late", priority="P1", site="Mumbai")
        Ticket.objects.filter(pk=t.pk).update(opened_at=timezone.now() - timedelta(hours=1),
                                              response_due=timezone.now() - timedelta(minutes=30))
        stale = Ticket.objects.get(pk=t.pk)           # the view loads the ticket
        sla.flag_breaches()                             # the SLA job runs meanwhile
        services.transition(stale, "acknowledged", user=self.root)
        self.assertEqual(Event.objects.filter(kind="sla_breach", fingerprint__endswith=":response").count(), 1)
        self.assertEqual(t.timeline.filter(kind="sla").count(), 1)
        t.refresh_from_db()
        self.assertTrue(t.response_breached)
        self.assertEqual(t.status, "acknowledged")
        self.assertIsNotNone(t.acknowledged_at)

    def test_transition_still_saves_what_it_changes(self):
        t = services.create_ticket(user=self.root, kind="incident", title="x", priority="P3")
        services.transition(t, "resolved", user=self.root, note="fixed it", force=True)
        t.refresh_from_db()
        self.assertEqual((t.status, t.resolution), ("resolved", "fixed it"))
        self.assertIsNotNone(t.resolved_at)

    def test_priority_change_keeps_a_concurrent_breach_flag(self):
        t = services.create_ticket(user=self.root, kind="incident", title="late", priority="P2", site="Mumbai")
        Ticket.objects.filter(pk=t.pk).update(opened_at=timezone.now() - timedelta(hours=2))
        stale = Ticket.objects.get(pk=t.pk)
        sla.flag_breaches()
        services.set_priority(stale, "P1", user=self.root)
        self.assertEqual(Event.objects.filter(kind="sla_breach", fingerprint__endswith=":response").count(), 1)
        t.refresh_from_db()
        self.assertTrue(t.response_breached)

    def test_edit_view_does_not_clobber_a_concurrent_breach_flag(self):
        """The edit page saves only the fields its form changed, so a breach the SLA job flagged between the view
        loading the ticket and saving it survives (and is not announced a second time)."""
        from django.urls import reverse

        from . import views
        t = services.create_ticket(user=self.root, kind="incident", title="late", priority="P1", site="Mumbai",
                                   category="Network", description="d")
        Ticket.objects.filter(pk=t.pk).update(opened_at=timezone.now() - timedelta(hours=1),
                                              response_due=timezone.now() - timedelta(minutes=30))
        real = views.services.approval_snapshot

        def sla_job_runs_meanwhile(ticket):
            sla.flag_breaches()
            return real(ticket)

        self.client.force_login(self.root)
        with mock.patch.object(views.services, "approval_snapshot", side_effect=sla_job_runs_meanwhile):
            self.client.post(reverse("servicedesk:edit", args=[t.number]),
                             {"title": "late, edited", "priority": "P1", "category": "Network", "description": "d"})
        t.refresh_from_db()
        self.assertEqual(t.title, "late, edited")
        self.assertTrue(t.response_breached)
        sla.flag_breaches()
        self.assertEqual(Event.objects.filter(kind="sla_breach", fingerprint__endswith=":response").count(), 1)
