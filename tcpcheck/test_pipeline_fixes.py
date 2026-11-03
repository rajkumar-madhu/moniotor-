"""Regression tests: stale copies must not duplicate a transition event (F11); prune keeps other tenants' history (F27)."""
from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone

from core.models import Event

from . import services
from .models import Status, TcpCheck, TcpResult
from .probe import ProbeResult


@override_settings(INCIDENT_AUTO_CREATE=False, NOTIFY_WEBHOOK_URLS=[], NOTIFY_EMAILS=[])
class TcpPipelineTests(TestCase):
    def make(self, **kw):
        return TcpCheck.objects.create(**{"name": "db", "host": "127.0.0.1", "port": 5432, "site": "All Sites", **kw})

    def test_transition_is_emitted_once_by_two_writers(self):
        c = self.make(last_status=Status.ONLINE)
        worker_copy, web_copy = TcpCheck.objects.get(pk=c.pk), TcpCheck.objects.get(pk=c.pk)
        down = ProbeResult(ok=False, error="Connection refused")
        services.save_result(web_copy, down, timezone.now())
        self.assertEqual(services.save_result(worker_copy, down, timezone.now()), Status.DOWN)
        evs = Event.objects.filter(fingerprint=f"tcp:{c.pk}")
        self.assertEqual([(e.data["previous"], e.data["status"]) for e in evs], [("online", "down")])
        self.assertEqual(TcpResult.objects.filter(tcp_check=c).count(), 2)

    def test_deleted_check_is_unknown_not_an_error(self):
        c = self.make()
        copy = TcpCheck.objects.get(pk=c.pk)
        c.delete()
        with self.assertLogs("linkedeye.tcpcheck", "WARNING"):
            self.assertIsNone(services.save_result(copy, ProbeResult(ok=False, error="x"), timezone.now()))
        self.assertFalse(TcpResult.objects.exists())

    @override_settings(LINKEDEYE_TENANT="default", PROBE_RETENTION_DAYS=30)
    def test_prune_only_touches_this_tenant(self):
        mine = self.make(tenant_key="default")
        other = self.make(tenant_key="other")
        old = timezone.now() - timedelta(days=40)
        for c in (mine, other):
            TcpResult.objects.create(tcp_check=c, checked_at=old, status=Status.ONLINE)
        services.prune()
        self.assertFalse(TcpResult.objects.filter(tcp_check=mine).exists())
        self.assertTrue(TcpResult.objects.filter(tcp_check=other).exists())
