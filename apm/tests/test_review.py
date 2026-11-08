"""Review additions: every status transition of one adapter (streak, last success / failure, history, events), flapping,
adapters and processes that stop being reported while failing, and ingest cost that does not grow per row."""
from datetime import timedelta

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apm import services
from apm.models import AdapterCell, CellEvent, ProcessState, RetryRequest
from core.models import Event

from .base import ApmCase, matrix, table


def events(**kw):
    return Event.objects.filter(source="apm", **kw)


def with_status(at, status, failing_others=()):
    """c001/NSE carries ``status`` (0-3); the rest of the matrix is healthy."""
    p = matrix(at, failing=set(failing_others))
    cell = p["data"]["c001"]["NSE"]
    cell["status"], cell["isSuccess"] = status, status == 2
    if status == 0:
        cell["error"] = "boom"
    return p


class Transitions(ApmCase):
    def feed(self, statuses):
        t0 = (timezone.now() - timedelta(minutes=5 * len(statuses))).replace(microsecond=0)
        seen = []
        for i, s in enumerate(statuses):
            t = t0 + timedelta(minutes=5 * i)
            services.ingest_matrix(self.src_a, with_status(t, s), now=t)
            c = AdapterCell.objects.get(source=self.src_a, client="c001", segment="NSE")
            seen.append((c.status, c.failure_streak, c.failure_total, c.last_success_at, c.last_failure_at, t))
        return seen

    def test_every_transition_updates_streak_totals_times_history_and_events_once(self):
        seen = self.feed([2, 0, 0, 3, 0, 1, 0, 2, 0, 2])
        self.assertEqual([(s[0], s[1], s[2]) for s in seen],
                         [(2, 0, 0), (0, 1, 1), (0, 2, 2), (3, 0, 2), (0, 1, 3), (1, 0, 3), (0, 1, 4), (2, 0, 4), (0, 1, 5), (2, 0, 5)])
        self.assertEqual(seen[0][3], seen[0][5])                 # healthy on first sight: that run is the last success
        self.assertEqual(seen[1][3], seen[0][5])                 # 2 -> 0: the previous run was the last healthy one
        self.assertEqual(seen[1][4], seen[1][5])
        self.assertEqual(seen[2][3], seen[0][5])                 # staying failed does not move last success
        self.assertEqual(seen[4][3], seen[0][5])                 # 3 -> 0 neither
        self.assertEqual(seen[7][3], seen[7][5])                 # 0 -> 2: now
        self.assertEqual(seen[8][3], seen[7][5])                 # failed again: the run before was healthy
        self.assertEqual(seen[9][3], seen[9][5])
        self.assertEqual(seen[9][4], seen[8][5])                 # last failure is kept after recovery
        # one history row per CHANGE (10 runs, the first sighting plus 8 changes; the repeated 0 adds none)
        self.assertEqual(CellEvent.objects.filter(source=self.src_a, client="c001", segment="NSE").count(), 9)
        # one event per transition into / out of failure, none for the repeated failure
        self.assertEqual(events(severity="critical").count(), 4)
        self.assertEqual(events(severity="ok").count(), 4)

    def test_flapping_raises_one_pair_per_cycle_not_one_per_run(self):
        self.feed([2, 0] * 6)
        self.assertEqual(events(severity="critical").count(), 6)
        self.assertEqual(events(severity="ok").count(), 5)
        c = AdapterCell.objects.get(source=self.src_a, client="c001", segment="NSE")
        self.assertEqual((c.failure_streak, c.failure_total), (1, 6))

    def test_first_sighting_unknown_or_warning_is_quiet(self):
        self.feed([3])
        self.assertFalse(events().exists())
        c = AdapterCell.objects.get(source=self.src_a, client="c001", segment="NSE")
        self.assertEqual((c.failure_streak, c.last_success_at, c.last_failure_at), (0, None, None))

    def test_a_failing_adapter_that_stops_being_reported_closes_its_alert(self):
        t0 = timezone.now() - timedelta(minutes=10)
        services.ingest_matrix(self.src_a, matrix(t0, failing={("c002", "NFO")}), now=t0)
        down = events(severity="critical").get()
        t1 = t0 + timedelta(minutes=5)
        out = services.ingest_matrix(self.src_a, matrix(t1, clients=("c001",)), now=t1)
        self.assertEqual(out["removed"], 2)
        closing = events(severity="ok").get()
        self.assertEqual(closing.fingerprint, down.fingerprint)
        self.assertIn("no longer reported", closing.title)
        self.assertEqual(events(severity="critical").count(), 1)

    def test_a_healthy_adapter_that_stops_being_reported_is_quiet(self):
        t0 = timezone.now() - timedelta(minutes=10)
        services.ingest_matrix(self.src_a, matrix(t0), now=t0)
        services.ingest_matrix(self.src_a, matrix(t0 + timedelta(minutes=5), clients=("c001",)), now=t0 + timedelta(minutes=5))
        self.assertFalse(events().exists())

    def test_a_failing_process_that_stops_being_reported_closes_its_alert(self):
        t0 = timezone.now() - timedelta(minutes=10)
        services.ingest_table(self.src_a, table(t0, down=("processStatus.py",)), now=t0)
        down = events(severity="critical").get()
        t1 = t0 + timedelta(minutes=5)
        services.ingest_table(self.src_a, table(t1, names=("adapterStatus.py",)), now=t1)
        self.assertEqual(events(severity="ok").get().fingerprint, down.fingerprint)
        self.assertFalse(ProcessState.objects.filter(source=self.src_a, name="processStatus.py").exists())

    def test_a_replayed_failing_report_does_not_count_twice_or_alert_again(self):
        t = timezone.now() - timedelta(minutes=5)
        for _ in range(3):
            services.ingest_matrix(self.src_a, matrix(t, failing={("c001", "NSE")}), now=t)
        c = AdapterCell.objects.get(source=self.src_a, client="c001", segment="NSE")
        self.assertEqual((c.failure_streak, c.failure_total), (1, 1))
        self.assertEqual(events().count(), 1)


class SameRunCorrected(ApmCase):
    def test_a_corrected_resend_of_a_run_does_not_inflate_the_streak(self):
        t = (timezone.now() - timedelta(minutes=5)).replace(microsecond=0)
        services.ingest_matrix(self.src_a, matrix(t, failing={("c001", "NSE")}), now=t)
        fixed = matrix(t, failing={("c001", "NSE")})
        fixed["data"]["c001"]["NSE"]["error"] = "boom, with a better message"
        out = services.ingest_matrix(self.src_a, fixed, now=t)
        self.assertEqual(out["status"], "update")
        c = AdapterCell.objects.get(source=self.src_a, client="c001", segment="NSE")
        self.assertEqual((c.failure_streak, c.failure_total, c.last_error), (1, 1, "boom, with a better message"))
        # ... while a resend of the same run that turns a healthy adapter into a failure counts it once
        again = matrix(t)
        out = services.ingest_matrix(self.src_a, again, now=t)
        self.assertEqual(AdapterCell.objects.get(source=self.src_a, client="c001", segment="NSE").failure_streak, 0)
        out = services.ingest_matrix(self.src_a, matrix(t, failing={("c001", "NSE")}), now=t)
        self.assertEqual(AdapterCell.objects.get(source=self.src_a, client="c001", segment="NSE").failure_streak, 1)


class Cost(ApmCase):
    def test_ingest_queries_grow_with_batches_not_with_rows(self):
        t = timezone.now() - timedelta(minutes=10)
        services.ingest_matrix(self.src_a, matrix(t, clients=("c001", "c002")), now=t)      # warm
        with CaptureQueriesContext(connection) as small:
            services.ingest_matrix(self.src_a, matrix(t + timedelta(minutes=1), clients=("c001", "c002"),
                                                      failing={("c001", "NSE")}), now=t + timedelta(minutes=1), emit_events=False)
        clients = tuple(f"k{i:03d}" for i in range(200))
        segs = ("NSE", "NFO", "CDS", "BSE", "BFO", "SLBM", "MSE", "NIPO", "BSTAR", "NCOM")
        t2 = t + timedelta(minutes=2)
        with CaptureQueriesContext(connection) as first:
            services.ingest_matrix(self.src_a, matrix(t2, clients=clients, segments=segs), now=t2, emit_events=False)
        t3 = t2 + timedelta(minutes=1)
        with CaptureQueriesContext(connection) as big:
            services.ingest_matrix(self.src_a, matrix(t3, clients=clients, segments=segs, failing={(c, "NSE") for c in clients[:100]}), now=t3,
                                  emit_events=False)
        self.assertEqual(AdapterCell.objects.filter(source=self.src_a).count(), 2000)
        self.assertLess(len(small), 25)
        self.assertLess(len(first), 200)                   # 2000 inserted rows in chunks (SQLite limits the parameters), not one query per row
        self.assertLess(len(big), 300)


class RetryOnce(ApmCase):
    def test_two_operators_asking_for_the_same_adapter_share_one_request(self):
        services.ingest_matrix(self.src_a, matrix(failing={("c001", "NSE")}))
        cell = AdapterCell.objects.get(source=self.src_a, client="c001", segment="NSE")
        a, made_a = services.request_retry(cell, self.mum_op)
        b, made_b = services.request_retry(cell, self.root)
        self.assertEqual((made_a, made_b, a.pk), (True, False, b.pk))
        self.assertEqual(RetryRequest.objects.filter(source=self.src_a).count(), 1)
