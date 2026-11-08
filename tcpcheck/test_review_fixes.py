"""Review round: a down check that is deleted or disabled closes its auto-incident; site names stay scoped."""
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import Event, Membership, Site
from servicedesk.models import Ticket

from . import services
from .models import Status, TcpCheck
from .probe import ProbeResult

LOCAL = override_settings(PROBE_ALLOWED_PRIVATE_CIDRS=["127.0.0.0/8"], INCIDENT_AUTO_CREATE=True,
                          NOTIFY_WEBHOOK_URLS=[], NOTIFY_EMAILS=[], LINKEDEYE_TENANT="default")


def down_check(name="db"):
    c = TcpCheck.objects.create(name=name, host="127.0.0.1", port=9, site="All Sites", timeout_seconds=2,
                                degraded_ms=500)
    services.save_result(c, ProbeResult(ok=False, error="Connection refused"), timezone.now())
    return c


def incidents(c):
    return Ticket.objects.filter(fingerprint=f"tcp:{c.pk}", kind="incident")


@LOCAL
class DownCheckIncidentTests(TestCase):
    def setUp(self):
        self.client.force_login(User.objects.create_superuser("admin", "a@x", "x"))

    def test_delete_closes_the_open_incident(self):
        c = down_check()
        self.assertEqual(incidents(c).get().status, "new")
        r = self.client.post(reverse("tcpcheck:delete", args=[c.pk]))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(incidents(c).get().status, "mitigated")
        last = Event.objects.filter(fingerprint=f"tcp:{c.pk}").order_by("-occurred_at", "-id").first()
        self.assertEqual(last.severity, "ok")

    def test_disable_closes_the_open_incident(self):
        c = down_check("db2")
        r = self.client.post(reverse("tcpcheck:edit", args=[c.pk]), {
            "name": c.name, "host": c.host, "port": c.port, "interval_seconds": 60, "site": "All Sites",
            "timeout_seconds": 2, "degraded_ms": 500})        # `enabled` omitted: unchecked
        self.assertEqual(r.status_code, 302, getattr(r, "context", None) and r.context["form"].errors)
        c.refresh_from_db()
        self.assertFalse(c.enabled)
        self.assertEqual(incidents(c).get().status, "mitigated")

    def test_healthy_check_emits_nothing_on_delete(self):
        c = TcpCheck.objects.create(name="ok", host="127.0.0.1", port=1, site="All Sites", last_status=Status.ONLINE)
        self.client.post(reverse("tcpcheck:delete", args=[c.pk]))
        self.assertFalse(Event.objects.filter(fingerprint=f"tcp:{c.pk}").exists())

    def test_closing_twice_does_not_repeat_the_event(self):
        c = down_check()
        services.close_failure(c, "Deleted")
        self.assertIsNone(services.close_failure(c, "Deleted"))
        self.assertEqual(Event.objects.filter(fingerprint=f"tcp:{c.pk}", severity="ok").count(), 1)


@override_settings(LINKEDEYE_TENANT="default")
class SiteNameScopeTests(TestCase):
    def test_all_sites_lists_only_visible_sites(self):
        a = Site.objects.create(name="Site Alpha", code="A", tenant_key="default")
        Site.objects.create(name="Site Gamma Secret", code="G", tenant_key="default")
        u = User.objects.create_user("v", password="pw")
        Membership.objects.create(user=u, site=a, role="viewer")
        from . import uptime
        self.assertEqual(uptime.all_sites(u), ["Site Alpha"])
        c = self.client
        c.force_login(u)
        r = c.get(reverse("tcpcheck:index"))
        self.assertContains(r, "Site Alpha")
        self.assertNotContains(r, "Site Gamma Secret")
        r = c.get("/monitoring/uptime/")
        self.assertNotContains(r, "Site Gamma Secret")
