"""The ingest service: cells, history, streaks, idempotency, transitions and events, retention and stale detection."""
from datetime import timedelta

from django.test import override_settings
from django.utils import timezone

from apm import payloads, services
from apm.models import (AdapterCell, AdapterRun, CellEvent, ExecLog, ProcessState, RetryRequest, Source)
from core.models import Event

from .base import ApmCase, matrix, stamp, table


def events(**kw):
    return Event.objects.filter(source="apm", **kw)


class MatrixIngest(ApmCase):
    def test_happy_path_creates_cells_run_and_history(self):
        now = timezone.now()
        out = services.ingest_matrix(self.src_a, matrix(now, failing={("c001", "NSE")}), host="h", script="adapterStatus.py", duration_s=8.2, now=now)
        self.assertEqual((out["total"], out["success"], out["failure"], out["created"]), (4, 3, 1, 4))
        cells = {(c.client, c.segment): c for c in AdapterCell.objects.filter(source=self.src_a)}
        bad = cells[("c001", "NSE")]
        self.assertEqual((bad.status, bad.failure_streak, bad.failure_total, bad.last_error), (0, 1, 1, "boom"))
        self.assertIsNotNone(bad.last_failure_at)
        run = AdapterRun.objects.get(source=self.src_a)
        self.assertEqual((run.kind, run.total, run.success, run.failure, run.duration_s, run.host, run.script),
                         ("matrix", 4, 3, 1, 8.2, "h", "adapterStatus.py"))
        self.assertEqual(CellEvent.objects.filter(source=self.src_a).count(), 4)
        self.src_a.refresh_from_db()
        self.assertEqual(self.src_a.last_matrix_at, now)
        self.assertIsNone(self.src_a.silent_since)

    def test_every_row_is_tenant_stamped(self):
        services.ingest_matrix(self.src_a, matrix())
        services.ingest_table(self.src_a, table())
        services.ingest_logs(self.src_a, [{"message": "x"}])
        for model in (AdapterCell, AdapterRun, CellEvent, ProcessState, ExecLog):
            self.assertTrue(model.objects.exists())
            self.assertFalse(model.objects.exclude(tenant_key=services.tenant()).exists(), model)

    def test_replay_of_the_same_report_is_accepted_but_adds_nothing(self):
        now = timezone.now()
        payload = matrix(now)
        services.ingest_matrix(self.src_a, payload, now=now)
        self.src_a.refresh_from_db()
        first_at = self.src_a.last_matrix_at
        out = services.ingest_matrix(self.src_a, payload, now=now + timedelta(minutes=3))
        self.assertTrue(out["duplicate"])
        self.assertEqual(AdapterRun.objects.filter(source=self.src_a).count(), 1)
        self.assertEqual(CellEvent.objects.filter(source=self.src_a).count(), 4)
        self.src_a.refresh_from_db()
        self.assertEqual(self.src_a.last_matrix_at, first_at)     # a replay is not fresh data
        self.assertIsNotNone(self.src_a.last_seen)

    def test_same_executed_on_with_different_content_updates_without_a_new_run(self):
        now = timezone.now()
        p1 = matrix(now)
        p2 = matrix(now, failing={("c001", "NSE")})
        p2["executedOn"] = p1["executedOn"]
        services.ingest_matrix(self.src_a, p1, now=now)
        out = services.ingest_matrix(self.src_a, p2, now=now)
        self.assertEqual(out["status"], "update")
        self.assertEqual(AdapterRun.objects.filter(source=self.src_a).count(), 1)
        self.assertEqual(AdapterCell.objects.get(source=self.src_a, client="c001", segment="NSE").status, 0)
        self.assertEqual(AdapterCell.objects.get(source=self.src_a, client="c001", segment="NSE").failure_streak, 1)

    def test_older_report_is_history_only(self):
        now = timezone.now()
        services.ingest_matrix(self.src_a, matrix(now), now=now)
        out = services.ingest_matrix(self.src_a, matrix(now - timedelta(hours=1), failing={("c001", "NSE")}), now=now)
        self.assertEqual(out["status"], "older")
        self.assertEqual(AdapterCell.objects.get(source=self.src_a, client="c001", segment="NSE").status, 2)
        self.assertEqual(AdapterRun.objects.filter(source=self.src_a).count(), 2)

    def test_streaks_counts_and_last_times(self):
        t0 = timezone.now() - timedelta(minutes=30)
        services.ingest_matrix(self.src_a, matrix(t0), now=t0)
        for i in (1, 2, 3):
            t = t0 + timedelta(minutes=i * 5)
            services.ingest_matrix(self.src_a, matrix(t, failing={("c001", "NSE")}), now=t)
        c = AdapterCell.objects.get(source=self.src_a, client="c001", segment="NSE")
        self.assertEqual((c.failure_streak, c.failure_total), (3, 3))
        self.assertIsNotNone(c.last_success_at)           # last seen healthy at the first run
        self.assertLess(c.last_success_at, c.last_failure_at)
        t = t0 + timedelta(minutes=25)
        services.ingest_matrix(self.src_a, matrix(t), now=t)
        c.refresh_from_db()
        self.assertEqual((c.status, c.failure_streak, c.failure_total, c.last_error), (2, 0, 3, ""))

    def test_history_records_changes_only(self):
        t0 = timezone.now() - timedelta(minutes=20)
        services.ingest_matrix(self.src_a, matrix(t0), now=t0)
        for i in (1, 2, 3):
            t = t0 + timedelta(minutes=i * 5)
            services.ingest_matrix(self.src_a, matrix(t, failing={("c001", "NSE")}), now=t)
        rows = list(CellEvent.objects.filter(source=self.src_a, client="c001", segment="NSE").order_by("ts"))
        self.assertEqual([(r.old_status, r.new_status) for r in rows], [(None, 2), (2, 0)])

    def test_adapters_missing_from_a_report_are_removed_but_history_stays(self):
        services.ingest_matrix(self.src_a, matrix(clients=("c001", "c002")))
        later = timezone.now() + timedelta(minutes=5)
        out = services.ingest_matrix(self.src_a, matrix(later, clients=("c001",)), now=later)
        self.assertEqual(out["removed"], 2)
        self.assertFalse(AdapterCell.objects.filter(source=self.src_a, client="c002").exists())
        self.assertTrue(CellEvent.objects.filter(source=self.src_a, client="c002").exists())

    def test_an_empty_matrix_does_not_wipe_the_adapters(self):
        services.ingest_matrix(self.src_a, matrix())
        later = timezone.now() + timedelta(minutes=5)
        out = services.ingest_matrix(self.src_a, {"executedOn": stamp(later), "data": {}}, now=later)
        self.assertEqual(out["status"], "empty")
        self.assertEqual(AdapterCell.objects.filter(source=self.src_a).count(), 4)

    def test_a_refused_payload_changes_nothing(self):
        services.ingest_matrix(self.src_a, matrix())
        before = (AdapterCell.objects.count(), AdapterRun.objects.count(), CellEvent.objects.count())
        data = {f"c{i}": {"A": {"status": 0}} for i in range(payloads.MAX_BROKERS + 1)}
        with self.assertRaises(payloads.PayloadError):
            services.ingest_matrix(self.src_a, {"data": data})
        self.assertEqual(before, (AdapterCell.objects.count(), AdapterRun.objects.count(), CellEvent.objects.count()))

    def test_atomic_when_the_database_fails_midway(self):
        from unittest import mock
        before = (AdapterCell.objects.count(), AdapterRun.objects.count(), CellEvent.objects.count())
        with mock.patch("apm.services.CellEvent.objects.bulk_create", side_effect=RuntimeError("disk full")):
            with self.assertRaises(RuntimeError):
                services.ingest_matrix(self.src_a, matrix())
        self.assertEqual(before, (AdapterCell.objects.count(), AdapterRun.objects.count(), CellEvent.objects.count()))
        self.assertFalse(events().exists())

    def test_cells_of_two_sources_do_not_mix(self):
        services.ingest_matrix(self.src_a, matrix())
        services.ingest_matrix(self.src_b, matrix(failing={("c001", "NSE")}))
        self.assertEqual(AdapterCell.objects.get(source=self.src_a, client="c001", segment="NSE").status, 2)
        self.assertEqual(AdapterCell.objects.get(source=self.src_b, client="c001", segment="NSE").status, 0)

    def test_hostile_text_is_stored_verbatim_as_text(self):
        p = matrix(extra={"data": {"c001": {"NSE": {"status": 0, "ctcl_id": "<b>1</b>", "value": "=1+1", "error": "'; DROP TABLE apm_adaptercell;--"}}}})
        services.ingest_matrix(self.src_a, p)
        c = AdapterCell.objects.get(source=self.src_a)
        self.assertEqual((c.ctcl_id, c.value, c.last_error), ("<b>1</b>", "=1+1", "'; DROP TABLE apm_adaptercell;--"))


class Events(ApmCase):
    def test_down_and_recovered_events(self):
        t0 = timezone.now() - timedelta(minutes=10)
        services.ingest_matrix(self.src_a, matrix(t0), now=t0)
        self.assertFalse(events().exists())           # healthy on first sight: nothing to say
        t1 = t0 + timedelta(minutes=5)
        services.ingest_matrix(self.src_a, matrix(t1, failing={("c001", "NSE")}), now=t1)
        e = events().get()
        self.assertEqual((e.severity, e.site, e.kind), ("critical", "Mumbai", "state_change"))
        self.assertIn("c001 / NSE", e.title)
        fp = e.fingerprint
        self.assertTrue(fp.startswith(f"apm:cell:{self.src_a.pk}:"))
        t2 = t1 + timedelta(minutes=5)
        services.ingest_matrix(self.src_a, matrix(t2, failing={("c001", "NSE")}), now=t2)
        self.assertEqual(events().count(), 1)         # still failing: no second event
        t3 = t2 + timedelta(minutes=5)
        services.ingest_matrix(self.src_a, matrix(t3), now=t3)
        rec = events(severity="ok").get()
        self.assertEqual(rec.fingerprint, fp)

    def test_first_report_with_a_failure_raises_an_event(self):
        services.ingest_matrix(self.src_a, matrix(failing={("c002", "NFO")}))
        self.assertEqual(events(severity="critical").count(), 1)

    def test_warning_is_not_an_incident(self):
        services.ingest_matrix(self.src_a, matrix(warning={("c001", "NSE")}))
        self.assertFalse(events().exists())

    def test_emit_events_false_is_silent(self):
        services.ingest_matrix(self.src_a, matrix(failing={("c002", "NFO")}), emit_events=False)
        self.assertFalse(events().exists())

    def test_event_storm_is_capped(self):
        clients = tuple(f"c{i:03d}" for i in range(40))
        bad = {(c, "NSE") for c in clients}
        services.ingest_matrix(self.src_a, matrix(clients=clients, segments=("NSE",), failing=bad))
        self.assertEqual(events().count(), services.MAX_EVENTS_PER_INGEST + 1)
        self.assertEqual(events(severity="critical").count(), services.MAX_EVENTS_PER_INGEST)

    def test_a_failing_listener_never_loses_the_data(self):
        from django.dispatch import receiver

        from core.events import event_emitted

        @receiver(event_emitted, weak=False)
        def boom(sender, event, **kw):
            raise RuntimeError("listener bug")
        self.addCleanup(event_emitted.disconnect, boom)
        services.ingest_matrix(self.src_a, matrix(failing={("c001", "NSE")}))
        self.assertEqual(AdapterCell.objects.filter(source=self.src_a, status=0).count(), 1)

    def test_process_down_and_recovered(self):
        t0 = timezone.now() - timedelta(minutes=10)
        services.ingest_table(self.src_a, table(t0), now=t0)
        self.assertFalse(events().exists())
        t1 = t0 + timedelta(minutes=5)
        services.ingest_table(self.src_a, table(t1, down=("processStatus.py",)), now=t1)
        e = events(severity="critical").get()
        self.assertIn("processStatus.py", e.title)
        t2 = t1 + timedelta(minutes=5)
        services.ingest_table(self.src_a, table(t2), now=t2)
        self.assertEqual(events(severity="ok").get().fingerprint, e.fingerprint)

    def test_a_critical_event_can_open_an_incident_through_service_desk(self):
        from servicedesk.models import Ticket
        with override_settings(INCIDENT_AUTO_CREATE=True):
            services.ingest_matrix(self.src_a, matrix(failing={("c001", "NSE")}))
            self.assertEqual(Ticket.objects.filter(kind="incident", origin="apm").count(), 1)
            later = timezone.now() + timedelta(minutes=5)
            services.ingest_matrix(self.src_a, matrix(later), now=later)
            self.assertEqual(Ticket.objects.get(kind="incident", origin="apm").status, "mitigated")


class TableAndLogs(ApmCase):
    def test_table_upsert_and_removal_per_host(self):
        services.ingest_table(self.src_a, table(names=("a", "b"), host="h1"))
        services.ingest_table(self.src_a, table(names=("a",), host="h2"))
        later = timezone.now() + timedelta(minutes=5)
        services.ingest_table(self.src_a, table(later, names=("a",), host="h1"), now=later)
        names = sorted(ProcessState.objects.filter(source=self.src_a).values_list("host", "name"))
        self.assertEqual(names, [("h1", "a"), ("h2", "a")])      # b is gone from h1; h2 untouched

    def test_table_duplicate_replay(self):
        now = timezone.now()
        p = table(now)
        services.ingest_table(self.src_a, p, now=now)
        self.assertTrue(services.ingest_table(self.src_a, p, now=now)["duplicate"])
        self.assertEqual(AdapterRun.objects.filter(kind="table").count(), 1)

    def test_logs_are_deduplicated_across_retries(self):
        lines = [{"ts": "2026-08-27 15:02:01,741", "level": "INFO", "process": "p", "message": "one"},
                 {"ts": "2026-08-27 15:02:01,852", "level": "ERROR", "process": "p", "message": "two"}]
        a = services.ingest_logs(self.src_a, lines)
        b = services.ingest_logs(self.src_a, lines + [{"ts": "2026-08-27 15:02:02", "message": "three"}])
        self.assertEqual((a["accepted"], b["accepted"], b["duplicates"]), (2, 1, 2))
        self.assertEqual(ExecLog.objects.filter(source=self.src_a).count(), 3)

    def test_logs_of_two_sources_are_independent(self):
        line = [{"ts": "2026-08-27 15:02:01", "message": "same"}]
        services.ingest_logs(self.src_a, line)
        self.assertEqual(services.ingest_logs(self.src_b, line)["accepted"], 1)

    def test_log_cap_per_source(self):
        with override_settings(APM_CAP_LOGS=50):
            base = timezone.now() - timedelta(minutes=5)
            lines = [{"ts": (base + timedelta(seconds=i)).isoformat(), "message": f"m{i}"} for i in range(80)]
            services.ingest_logs(self.src_a, lines)
            self.assertEqual(ExecLog.objects.filter(source=self.src_a).count(), 50)
            self.assertTrue(ExecLog.objects.filter(source=self.src_a, message="m79").exists())
            self.assertFalse(ExecLog.objects.filter(source=self.src_a, message="m0").exists())


class Retention(ApmCase):
    def test_old_history_is_deleted_and_fresh_kept(self):
        old = timezone.now() - timedelta(days=40)
        services.ingest_matrix(self.src_a, matrix(old, failing={("c001", "NSE")}), now=old)
        fresh = timezone.now()
        services.ingest_matrix(self.src_a, matrix(fresh), now=fresh)
        services.ingest_logs(self.src_a, [{"ts": (timezone.now() - timedelta(days=9)).isoformat(), "message": "old"},
                                          {"ts": fresh.isoformat(), "message": "new"}])
        out = services.retention()
        self.assertEqual(AdapterRun.objects.filter(source=self.src_a).count(), 1)
        self.assertEqual(list(ExecLog.objects.values_list("message", flat=True)), ["new"])
        self.assertGreaterEqual(out["runs"], 1)
        self.assertTrue(AdapterCell.objects.filter(source=self.src_a).exists())      # current state is never aged out

    def test_run_cap_keeps_the_newest(self):
        with override_settings(APM_CAP_RUNS=3):
            base = timezone.now() - timedelta(hours=2)
            for i in range(6):
                t = base + timedelta(minutes=i * 5)
                services.ingest_matrix(self.src_a, matrix(t), now=t)
            services.retention()
            runs = AdapterRun.objects.filter(source=self.src_a)
            self.assertEqual(runs.count(), 3)
            oldest = min(r.executed_at for r in runs)
            self.assertLess(abs((oldest - (base + timedelta(minutes=15))).total_seconds()), 2)

    def test_retention_days_setting(self):
        t = timezone.now() - timedelta(days=3)
        services.ingest_matrix(self.src_a, matrix(t), now=t)
        with override_settings(APM_RETENTION_DAYS=2):
            services.retention()
        self.assertFalse(AdapterRun.objects.exists())

    def test_retention_only_touches_this_tenant(self):
        t = timezone.now() - timedelta(days=60)
        services.ingest_matrix(self.src_a, matrix(t), now=t)
        AdapterRun.objects.update(tenant_key="someone-else")
        services.retention()
        self.assertTrue(AdapterRun.objects.exists())


class StaleDetection(ApmCase):
    def test_a_silent_source_emits_once_and_recovery_emits_ok(self):
        long_ago = timezone.now() - timedelta(minutes=30)
        services.ingest_matrix(self.src_a, matrix(long_ago), now=long_ago)
        out = services.detect_stale()
        self.assertEqual(out["silent"], 1)
        e = events(severity="warning").get()
        self.assertIn("silent", e.title)
        self.assertEqual(services.detect_stale()["silent"], 0)          # only once
        self.assertEqual(events().count(), 1)
        services.ingest_matrix(self.src_a, matrix())
        rec = events(severity="ok").get()
        self.assertEqual(rec.fingerprint, e.fingerprint)
        self.src_a.refresh_from_db()
        self.assertIsNone(self.src_a.silent_since)

    def test_a_fresh_source_is_not_silent(self):
        services.ingest_matrix(self.src_a, matrix())
        self.assertEqual(services.detect_stale()["silent"], 0)

    def test_never_reported_or_disabled_sources_stay_quiet(self):
        self.assertEqual(services.detect_stale()["silent"], 0)
        long_ago = timezone.now() - timedelta(hours=3)
        services.ingest_matrix(self.src_b, matrix(long_ago), now=long_ago)
        Source.objects.filter(pk=self.src_b.pk).update(enabled=False)
        self.assertEqual(services.detect_stale()["silent"], 0)

    def test_threshold_follows_the_per_source_interval(self):
        t = timezone.now() - timedelta(minutes=30)
        services.ingest_matrix(self.src_a, matrix(t), now=t)
        Source.objects.filter(pk=self.src_a.pk).update(expected_interval_s=3600)
        self.assertEqual(services.detect_stale()["silent"], 0)
        self.assertEqual(services.stale_after(Source(expected_interval_s=300)), 330)
        self.assertEqual(services.stale_after(Source(expected_interval_s=3600)), 3630)

    def test_job_functions_never_raise(self):
        from apm import jobs
        self.assertIn("newly silent", jobs.stale())
        self.assertIn("runs", jobs.retention())
        self.assertIn("no sources", jobs.redis_poll())


class RetryLifecycle(ApmCase):
    def setUp(self):
        super().setUp()
        services.ingest_matrix(self.src_a, matrix(failing={("c001", "NSE")}))
        self.cell = AdapterCell.objects.get(source=self.src_a, client="c001", segment="NSE")

    def test_request_pick_done(self):
        req, created = services.request_retry(self.cell, self.mum_op)
        self.assertTrue(created)
        self.assertEqual((req.state, req.requested_by_name), ("pending", "mum-op"))
        same, created2 = services.request_retry(self.cell, self.mum_op)
        self.assertFalse(created2)
        self.assertEqual(same.pk, req.pk)                       # one open request per adapter
        picked = services.pick_commands(self.src_a)
        self.assertEqual([r.pk for r in picked], [req.pk])
        req.refresh_from_db()
        self.assertEqual(req.state, "picked")
        self.assertEqual(services.pick_commands(self.src_a), [])    # picked once
        self.assertTrue(services.complete_command(self.src_a, req.pk))
        self.assertFalse(services.complete_command(self.src_a, req.pk))

    def test_a_command_of_another_source_cannot_be_completed(self):
        req, _ = services.request_retry(self.cell, self.mum_op)
        self.assertFalse(services.complete_command(self.src_b, req.pk))
        self.assertEqual(services.pick_commands(self.src_b), [])

    def test_unpicked_requests_expire(self):
        req, _ = services.request_retry(self.cell, self.mum_op)
        RetryRequest.objects.filter(pk=req.pk).update(requested_at=timezone.now() - timedelta(hours=2))
        services.expire_retries()
        req.refresh_from_db()
        self.assertEqual(req.state, "expired")
        _, created = services.request_retry(self.cell, self.mum_op)
        self.assertTrue(created)

    def test_the_next_report_completes_a_picked_retry(self):
        req, _ = services.request_retry(self.cell, self.mum_op)
        services.pick_commands(self.src_a)
        later = timezone.now() + timedelta(minutes=2)
        services.ingest_matrix(self.src_a, matrix(later, failing={("c001", "NSE")}), now=later)
        req.refresh_from_db()
        self.assertEqual(req.state, "done")

    def test_completed_commands_field_marks_done_and_ignores_foreign_ids(self):
        mine, _ = services.request_retry(self.cell, self.mum_op)
        other_cell = AdapterCell.objects.create(tenant_key=services.tenant(), source=self.src_b, client="x", segment="NSE")
        theirs, _ = services.request_retry(other_cell, self.che_admin)
        later = timezone.now() + timedelta(minutes=2)
        services.ingest_matrix(self.src_a, matrix(later), now=later, completed=[mine.pk, theirs.pk])
        mine.refresh_from_db()
        theirs.refresh_from_db()
        self.assertEqual((mine.state, theirs.state), ("done", "pending"))
