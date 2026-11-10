"""Verification fixes (BOD-1, BOD-2, BOD-3).

BOD-1: the re-check lock (and the re-check budget) must be shared by every web worker. The default cache is a per-process
LocMemCache, so "another gunicorn worker" is simulated here by clearing the cache: what is left is what a second process
would see.
BOD-2: a re-check that outlives its lock must not free a later re-check's lock.
BOD-3: the checklist page must tell the script which run it shows, or the first poll reports a "new" run on every load.
"""
import re
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone

from core.models import SchedulerLease

from . import live, services
from .test_live import HangingExecutor, ImmediateExecutor, LiveBase, api
from .tests import client_for


def other_worker():
    """What a second web worker sees: none of this process's cache entries."""
    cache.clear()


def expire_lock(mode):
    """The lock's time-to-live has passed (whatever holds it)."""
    cache.delete(live._lock_key(mode))
    SchedulerLease.objects.filter(name=live._lock_key(mode)).update(expires_at=timezone.now() - timedelta(seconds=1))


class SharedLockTests(LiveBase):
    """BOD-1"""

    def setUp(self):
        super().setUp()
        self.c = client_for(self.operator)

    def post(self, client=None):
        return (client or self.c).post(reverse("bodeod:api_recheck"), {"mode": self.mode.pk})

    def test_a_running_recheck_is_seen_by_every_worker(self):
        hang = HangingExecutor()
        with mock.patch.object(live, "_executor", hang), mock.patch.object(live, "RECHECK_WAIT", 0.05), \
                mock.patch("bodeod.services.refresh_references", return_value=services.RefreshOutcome(ran=3)):
            self.assertEqual(self.post().json()["state"], "running")
            other_worker()
            self.assertTrue(live.is_rechecking(self.mode))
            self.assertTrue(api(self.c).json()["modes"][0]["rechecking"])     # the poll does not say "finished" early
            again = self.post(client_for(self.admin))
            self.assertEqual((again.status_code, again.json()["state"]), (202, "busy"))
            self.assertEqual(len(hang.jobs), 1)                                # no second probe run
            fn, args, kwargs = hang.jobs[0]
            fn(*args, **kwargs)
        other_worker()
        self.assertFalse(live.is_rechecking(self.mode))

    def test_the_recheck_budget_is_shared_by_every_worker(self):
        with mock.patch.object(live, "_executor", ImmediateExecutor()), mock.patch.object(live, "RECHECK_LIMIT", 2), \
                mock.patch("bodeod.services.refresh_references", return_value=services.RefreshOutcome(ran=3)):
            self.assertEqual(self.post().status_code, 200)
            other_worker()
            self.assertEqual(self.post().status_code, 200)
            other_worker()
            r = self.post()
            self.assertEqual(r.status_code, 429)
            self.assertEqual(r["Retry-After"], "30")
            self.assertEqual(self.post(client_for(self.admin)).status_code, 200)   # another user has their own budget


class LockOwnerTests(LiveBase):
    """BOD-2"""

    def setUp(self):
        super().setUp()
        self.c = client_for(self.operator)

    def post(self):
        return self.c.post(reverse("bodeod:api_recheck"), {"mode": self.mode.pk})

    def test_a_job_that_outlived_its_lock_does_not_free_the_next_one(self):
        first, second = HangingExecutor(), HangingExecutor()
        refresh = mock.patch("bodeod.services.refresh_references", return_value=services.RefreshOutcome(ran=3))
        with mock.patch.object(live, "RECHECK_WAIT", 0.05), refresh:
            with mock.patch.object(live, "_executor", first):
                self.assertEqual(self.post().json()["state"], "running")             # job A
            expire_lock(self.mode)                                                  # A is slower than the lock's TTL
            self.assertFalse(live.is_rechecking(self.mode))
            with mock.patch.object(live, "_executor", second):
                self.assertEqual(self.post().json()["state"], "running")             # job B takes the mode
            fn, args, kwargs = first.jobs[0]
            fn(*args, **kwargs)                                                     # A finishes while B still runs
            self.assertTrue(live.is_rechecking(self.mode))
            with mock.patch.object(live, "_executor", HangingExecutor()) as third:
                again = self.post()
            self.assertEqual(again.json()["state"], "busy")
            self.assertEqual(third.jobs, [])
            fn, args, kwargs = second.jobs[0]
            fn(*args, **kwargs)                                                     # B finishes: the mode is free
        self.assertFalse(live.is_rechecking(self.mode))

    def test_the_lock_lifetime_starts_when_the_job_starts(self):
        hang = HangingExecutor()
        seen = {}

        def refresh(refs, resolver):
            seen["expires"] = SchedulerLease.objects.get(name=live._lock_key(self.mode)).expires_at
            return services.RefreshOutcome(ran=1)

        with mock.patch.object(live, "_executor", hang), mock.patch.object(live, "RECHECK_WAIT", 0.05):
            self.assertEqual(self.post().json()["state"], "running")
            # the job waited in the executor queue for most of the lock's lifetime
            SchedulerLease.objects.filter(name=live._lock_key(self.mode)).update(expires_at=timezone.now() + timedelta(seconds=2))
            fn, args, kwargs = hang.jobs[0]
            with mock.patch("bodeod.services.refresh_references", side_effect=refresh):
                fn(*args, **kwargs)
        self.assertGreater(seen["expires"], timezone.now() + timedelta(seconds=live.RECHECK_LOCK_TTL - 10))


class ChecklistRunIdentityTests(LiveBase):
    """BOD-3"""

    def test_checklist_page_names_the_latest_run(self):
        run = services.start_run(self.mode, refresh=False)
        html = client_for(self.viewer).get(reverse("bodeod:checklist")).content.decode()
        m = re.search(r'data-live-mode="BOD" data-run-id="([^"]*)" data-run-status="([^"]*)"', html)
        self.assertIsNotNone(m)
        payload = api(client_for(self.viewer), mode="BOD").json()["modes"][0]["run"]
        # what the page shows equals what the first poll reports, so the script raises no "run is now ..." notice
        self.assertEqual(m.groups(), (str(payload["id"]), payload["status"]))
        self.assertEqual(m.groups(), (str(run.pk), run.status))

    def test_checklist_page_without_a_run(self):
        html = client_for(self.viewer).get(reverse("bodeod:checklist")).content.decode()
        self.assertIn('data-live-mode="BOD" data-run-id="" data-run-status=""', html)
