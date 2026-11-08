"""Regression tests for the second BOD/EOD audit round.

Covered: a sign-off given while the checks are re-running survives, paused domains are not validated as OK,
finalize() re-reads the results (and locks the run) before writing a status, the holiday CSV parser survives a
broken CSV, references are scoped to the mode's site, run.refreshed tells the truth, and re-validating a run no
longer rewrites finished_at.
"""
import threading
from datetime import date, time, timedelta
from unittest import mock, skipUnless

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from core.models import Event, Membership, Site
from domainmonitor.models import Domain
from tcpcheck.models import TcpCheck
from urlchecker.models import UrlCheck

from . import services
from .forms import ChecklistItemForm
from .models import ChecklistItem, Holiday, Mode, Run, RunItem


def make_mode(code="BOD", site="All Sites", **kw):
    defaults = {"name": "Beginning of Day", "window_start": time(7, 0), "window_end": time(9, 0),
                "refresh_checks": False, "site": site}
    defaults.update(kw)
    return Mode.objects.create(code=code, **defaults)


def url_check(name, status="online", **kw):
    return UrlCheck.objects.create(name=name, url="https://example.com/", last_status=status,
                                   last_checked_at=timezone.now(), last_response_ms=100, **kw)


# ------------------------------------------------------------------ 1. sign-off vs. re-validation
class SignOffDuringRevalidationTests(TestCase):
    """evaluate() reads the rows before the (slow) re-run of the referenced checks; a sign-off that lands in
    between must not be written back as 'pending'."""

    def setUp(self):
        self.mode = make_mode()
        self.op = User.objects.create_user("op")
        Membership.objects.create(user=self.op, site=None, role="operator")
        url_check("Portal")
        ChecklistItem.objects.create(mode=self.mode, name="Portal", kind="url_check", ref_name="Portal", order=10)
        ChecklistItem.objects.create(mode=self.mode, name="Feed", kind="manual", order=20)

    def test_signoff_during_the_rerun_is_kept(self):
        run = services.start_run(self.mode, user=self.op, refresh=False)
        self.assertEqual(run.status, "warn")

        def sign_off_midway(items, resolver):      # the other request, while the checks are re-running
            services.sign_off(run.results.get(name="Feed"), self.op, "feed verified")
            return services.RefreshOutcome(ran=len(items))

        with mock.patch.object(services, "refresh_references", side_effect=sign_off_midway):
            services.revalidate(run, refresh=True)

        feed = run.results.get(name="Feed")
        self.assertEqual(feed.status, "signed_off")
        self.assertEqual(feed.note, "feed verified")
        self.assertEqual(feed.signed_off_by_id, self.op.pk)
        self.assertEqual(feed.detail, "Signed off")      # the re-validation must not overwrite the row at all
        run.refresh_from_db()
        self.assertEqual(run.status, "pass")

    def test_carry_forward_still_signs_off_a_pending_item(self):
        first = services.start_run(self.mode, user=self.op, refresh=False)
        services.sign_off(first.results.get(name="Feed"), self.op, "feed verified")
        second = services.start_run(self.mode, user=self.op, refresh=False)
        feed = second.results.get(name="Feed")
        self.assertEqual(feed.status, "signed_off")
        self.assertEqual(feed.note, "feed verified")
        self.assertIn(f"run #{first.pk}", feed.detail)


# ------------------------------------------------------------------ 2. paused domains
class PausedDomainTests(TestCase):
    def setUp(self):
        self.mode = make_mode()
        self.domain = Domain.objects.create(tenant_key=services.tenant(), site_key="All Sites",
                                            hostname="nse.example.com", status="up", dns_status="Resolved",
                                            ssl_status="Valid", last_checked_at=timezone.now() - timedelta(days=60))
        ChecklistItem.objects.create(mode=self.mode, name="Exchange site", kind="domain", ref_name="nse.example.com")

    def test_paused_domain_is_unknown_not_ok(self):
        self.assertEqual(services.status_of("domain", self.domain)[0], "ok")
        self.domain.monitor_enabled = False
        self.domain.save(update_fields=["monitor_enabled"])
        status, detail = services.status_of("domain", self.domain)
        self.assertEqual(status, "unknown")
        self.assertIn("paused", detail)

    def test_run_does_not_pass_on_a_paused_domain(self):
        Domain.objects.filter(pk=self.domain.pk).update(monitor_enabled=False)
        run = services.start_run(self.mode, refresh=False)
        self.assertEqual(run.results.get().status, "unknown")
        self.assertEqual(run.status, "warn")


# ------------------------------------------------------------------ 3. run finalization
class FinalizeTests(TestCase):
    def setUp(self):
        self.mode = make_mode()
        self.op = User.objects.create_user("op2")
        Membership.objects.create(user=self.op, site=None, role="operator")
        ChecklistItem.objects.create(mode=self.mode, name="a", kind="manual", order=10)
        ChecklistItem.objects.create(mode=self.mode, name="b", kind="manual", order=20)

    def test_finalize_reads_the_results_again_instead_of_a_stale_copy(self):
        run = services.start_run(self.mode, user=self.op, refresh=False)
        self.assertEqual(run.status, "warn")
        stale = Run.objects.prefetch_related("results").get(pk=run.pk)
        list(stale.results.all())                      # the caller's copy: both items still pending
        now = timezone.now()
        RunItem.objects.filter(run=run).update(status="signed_off", signed_off_by=self.op,
                                              signed_off_by_name="op2", signed_off_at=now, note="done")
        services.finalize(stale, previous="warn")
        self.assertEqual(Run.objects.get(pk=run.pk).status, "pass")

    def test_signoff_emits_the_state_change_once(self):
        run = services.start_run(self.mode, user=self.op, refresh=False)
        before = Event.objects.filter(source="bodeod").count()
        services.sign_off(run.results.get(name="a"), self.op, "a ok")
        self.assertEqual(Event.objects.filter(source="bodeod").count(), before)      # still 'warn': no new event
        services.sign_off(run.results.get(name="b"), self.op, "b ok")
        events = Event.objects.filter(source="bodeod").order_by("-pk")
        self.assertEqual(events.count(), before + 1)
        self.assertEqual(events[0].data["status"], "pass")
        self.assertEqual(events[0].data["previous"], "warn")

    def test_second_signoff_of_the_same_item_is_refused(self):
        run = services.start_run(self.mode, user=self.op, refresh=False)
        a = run.results.get(name="a")
        stale = RunItem.objects.get(pk=a.pk)           # another request's copy, read while "a" was still pending
        services.sign_off(a, self.op, "checked")
        with self.assertRaises(services.RunError):     # the check must be made against the database, not the copy
            services.sign_off(stale, self.op, "checked twice")
        self.assertEqual(RunItem.objects.get(pk=a.pk).note, "checked")
        with self.assertRaises(services.RunError):
            services.sign_off(RunItem.objects.get(pk=a.pk), self.op, "checked thrice")


class ConcurrentSignOffTests(TransactionTestCase):
    """Two operators signing off different items of the same run at the same moment (two web replicas) must not
    leave the run 'warn' with every item signed off. Needs real row locks (PostgreSQL/MySQL)."""

    reset_sequences = True

    def setUp(self):
        self.mode = make_mode()
        self.op = User.objects.create_user("op3")
        Membership.objects.create(user=self.op, site=None, role="operator")
        ChecklistItem.objects.create(mode=self.mode, name="a", kind="manual", order=10)
        ChecklistItem.objects.create(mode=self.mode, name="b", kind="manual", order=20)

    @skipUnless(connection.features.has_select_for_update, "the database has no row locks")
    def test_two_simultaneous_signoffs_agree_with_the_items(self):
        run = services.start_run(self.mode, user=self.op, refresh=False)
        a, b = run.results.get(name="a"), run.results.get(name="b")
        read_done = threading.Event()
        real_compute = services.compute_status
        slow_for = threading.local()

        def compute(results):
            out = real_compute(results)
            if getattr(slow_for, "slow", False):
                slow_for.slow = False
                read_done.set()
                threading.Event().wait(1.0)   # hold the run between reading and writing
            return out

        def first():
            try:
                slow_for.slow = True
                services.sign_off(b, self.op, "b checked")
            finally:
                read_done.set()
                connection.close()

        def second():
            try:
                read_done.wait(5)
                services.sign_off(a, self.op, "a checked")
            finally:
                connection.close()

        with mock.patch.object(services, "compute_status", side_effect=compute):
            t1, t2 = threading.Thread(target=first), threading.Thread(target=second)
            t1.start()
            t2.start()
            t1.join(30)
            t2.join(30)

        self.assertEqual(RunItem.objects.filter(run=run, status="signed_off").count(), 2)
        self.assertEqual(Run.objects.get(pk=run.pk).status, "pass")


# ------------------------------------------------------------------ 4. holiday CSV
class HolidayCsvTests(TestCase):
    def test_oversized_field_is_an_error_not_a_crash(self):
        data = ('date,description\n2026-01-01,"' + "x" * 140_000).encode()
        self.assertLess(len(data), services.MAX_CSV_BYTES)
        out = services.parse_holidays_csv(data)
        self.assertFalse(out.rows)
        self.assertTrue(out.errors)
        self.assertIn("not valid CSV", out.errors[-1])

    def test_broken_quoting_keeps_the_rows_parsed_so_far(self):
        data = ('date,description\n2026-01-26,Republic Day\n2026-03-03,"' + "y" * 140_000).encode()
        out = services.parse_holidays_csv(data)
        self.assertEqual(out.rows, [("NSE", date(2026, 1, 26), "Republic Day")])
        self.assertIn("not valid CSV", out.errors[-1])

    def test_nul_bytes_are_stripped(self):
        out = services.parse_holidays_csv(b"date,description\n2026-01-26,Republic\x00 Day\n")
        self.assertEqual(out.rows, [("NSE", date(2026, 1, 26), "Republic Day")])
        services.import_holidays(out.rows)
        self.assertEqual(Holiday.objects.get().description, "Republic Day")

    def test_broken_header_is_an_error(self):
        out = services.parse_holidays_csv(('"' + "z" * 140_000).encode())
        self.assertFalse(out.rows)
        self.assertIn("not valid CSV", out.errors[0])

    def test_upload_view_does_not_return_500(self):
        boss = User.objects.create_user("boss", password="pw")
        Membership.objects.create(user=boss, site=None, role="admin")
        c = Client()
        c.force_login(boss)
        f = SimpleUploadedFile("h.csv", ('date,description\n2026-01-01,"' + "x" * 140_000).encode(), "text/csv")
        r = c.post(reverse("bodeod:holiday_upload"), {"file": f, "default_exchange": "NSE"})
        self.assertEqual(r.status_code, 302)
        self.assertFalse(Holiday.objects.exists())


# ------------------------------------------------------------------ 5. reference scoping
class RefScopingTests(TestCase):
    def setUp(self):
        self.a = Site.objects.create(name="SiteA", code="A")
        self.b = Site.objects.create(name="SiteB", code="B")
        self.mode = make_mode(site="SiteA")
        self.theirs = TcpCheck.objects.create(name="Chennai gateway", host="10.9.9.9", port=22, site="SiteB",
                                              last_status="down", last_checked_at=timezone.now(),
                                              last_error="connection refused to 10.9.9.9:22")
        self.mine = TcpCheck.objects.create(name="Mumbai gateway", host="10.1.1.1", port=22, site="SiteA",
                                            last_status="online", last_checked_at=timezone.now())
        self.admin = User.objects.create_user("admA", password="pw")
        Membership.objects.create(user=self.admin, site=self.a, role="admin")

    def client_for(self, user):
        c = Client()
        c.force_login(user)
        return c

    def test_resolver_hides_another_sites_check(self):
        item = ChecklistItem.objects.create(mode=self.mode, name="Gateway", kind="tcp_check",
                                            ref_name="Chennai gateway")
        status, detail = services.live_status(item)
        self.assertEqual(status, "unknown")
        self.assertIn("not found", detail)
        self.assertNotIn("10.9.9.9", detail)
        item.ref_name = "Mumbai gateway"
        self.assertEqual(services.live_status(item)[0], "ok")

    def test_run_of_a_site_mode_does_not_read_another_sites_check(self):
        ChecklistItem.objects.create(mode=self.mode, name="Gateway", kind="tcp_check", ref_name="Chennai gateway")
        run = services.start_run(self.mode, refresh=False)
        result = run.results.get()
        self.assertEqual(result.status, "unknown")
        self.assertNotIn("refused", result.detail)

    def test_global_mode_may_reference_any_site(self):
        glob = make_mode(code="EOD", site="All Sites", name="End of Day", window_start=time(15, 30),
                         window_end=time(16, 0))
        item = ChecklistItem.objects.create(mode=glob, name="Gateway", kind="tcp_check", ref_name="Chennai gateway")
        self.assertEqual(services.live_status(item)[0], "critical")

    def test_suggestions_only_list_visible_sites(self):
        r = self.client_for(self.admin).get(reverse("bodeod:checklist") + "?new=1")
        self.assertContains(r, "Mumbai gateway")
        self.assertNotContains(r, "Chennai gateway")

    def test_form_refuses_a_reference_from_another_site(self):
        form = ChecklistItemForm(data={"mode": self.mode.pk, "order": 10, "name": "Gateway", "kind": "tcp_check",
                                       "ref_name": "Chennai gateway", "critical": "on", "enabled": "on"})
        self.assertFalse(form.is_valid())
        self.assertIn("ref_name", form.errors)
        ok = ChecklistItemForm(data={"mode": self.mode.pk, "order": 10, "name": "Gateway", "kind": "tcp_check",
                                    "ref_name": "Mumbai gateway", "critical": "on", "enabled": "on"})
        self.assertTrue(ok.is_valid(), ok.errors)

    def test_form_still_accepts_a_reference_that_does_not_exist_yet(self):
        form = ChecklistItemForm(data={"mode": self.mode.pk, "order": 10, "name": "Future", "kind": "tcp_check",
                                      "ref_name": "Not created yet", "critical": "on", "enabled": "on"})
        self.assertTrue(form.is_valid(), form.errors)


# ------------------------------------------------------------------ 6. "checks re-run" flag
class RefreshFlagTests(TestCase):
    def setUp(self):
        self.mode = make_mode(refresh_checks=True)
        url_check("Portal")
        ChecklistItem.objects.create(mode=self.mode, name="Portal", kind="url_check", ref_name="Portal")

    def test_failed_rerun_is_not_reported_as_refreshed(self):
        with mock.patch("probes.runner.run_url_checks", side_effect=RuntimeError("probe pool down")):
            run = services.start_run(self.mode, refresh=True)
        self.assertFalse(run.refreshed)
        self.assertIn("re-run failed", run.results.get().detail)
        self.assertFalse(Run.objects.get(pk=run.pk).refreshed)

    def test_successful_rerun_is_reported(self):
        with mock.patch("probes.runner.run_url_checks") as runner:
            run = services.start_run(self.mode, refresh=True)
        runner.assert_called_once()
        self.assertTrue(Run.objects.get(pk=run.pk).refreshed)
        self.assertNotIn("re-run failed", run.results.get().detail)

    def test_partly_failed_rerun_is_not_reported_as_refreshed(self):
        """One kind re-ran, another could not: the run must not claim that its checks were re-run."""
        TcpCheck.objects.create(name="Gateway", host="10.1.1.1", port=22, site="All Sites", last_status="online",
                                last_checked_at=timezone.now())
        ChecklistItem.objects.create(mode=self.mode, name="Gateway", kind="tcp_check", ref_name="Gateway", order=20)
        with mock.patch("probes.runner.run_url_checks"), \
                mock.patch("tcpcheck.services.run_checks", side_effect=RuntimeError("tcp pool down")):
            run = services.start_run(self.mode, refresh=True)
        self.assertFalse(run.refreshed)
        self.assertFalse(Run.objects.get(pk=run.pk).refreshed)
        self.assertIn("re-run failed", run.results.get(name="Gateway").detail)
        self.assertNotIn("re-run failed", run.results.get(name="Portal").detail)

    def test_nothing_to_rerun_is_not_reported_as_refreshed(self):
        UrlCheck.objects.update(enabled=False)
        run = services.start_run(self.mode, refresh=True)
        self.assertFalse(run.refreshed)


# ------------------------------------------------------------------ 7. duration after a re-validation
class RevalidateDurationTests(TestCase):
    def setUp(self):
        self.mode = make_mode()
        url_check("Portal")
        ChecklistItem.objects.create(mode=self.mode, name="Portal", kind="url_check", ref_name="Portal")

    def test_revalidation_does_not_inflate_the_duration(self):
        run = services.start_run(self.mode, refresh=False)
        started = timezone.now() - timedelta(minutes=90)
        Run.objects.filter(pk=run.pk).update(started_at=started, finished_at=started + timedelta(seconds=4))
        run.refresh_from_db()
        services.revalidate(run, refresh=False)
        run.refresh_from_db()
        self.assertEqual(run.finished_at, started + timedelta(seconds=4))
        self.assertAlmostEqual(run.duration_seconds, 4.0, places=3)
        self.assertIsNotNone(run.revalidated_at)
