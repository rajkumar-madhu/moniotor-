"""Regression test for F27b: device-poll pruning only touches this instance's tenant."""
from datetime import timedelta
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from . import jobs
from .models import Device, DevicePoll


@override_settings(PROBE_RETENTION_DAYS=30)
class PruneTenantTests(TestCase):
    def test_prune_keeps_other_tenants_polls(self):
        mine = Device.objects.create(name="sw1", mgmt_ip="10.0.0.1")
        theirs = Device.objects.create(name="sw1", mgmt_ip="10.0.0.2", tenant_key="other-tenant")
        old = timezone.now() - timedelta(days=40)
        for d in (mine, theirs):
            DevicePoll.objects.create(device=d, checked_at=old, status="up")
            DevicePoll.objects.create(device=d, status="up")
        with mock.patch.dict(jobs._state, {"last_prune": None}):
            self.assertEqual(jobs._maybe_prune(), 1)
        self.assertEqual(DevicePoll.objects.filter(device=mine).count(), 1)
        self.assertEqual(DevicePoll.objects.filter(device=theirs).count(), 2)
