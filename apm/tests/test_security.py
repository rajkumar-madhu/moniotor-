"""Attacks on the machine API found by the security review: ordering and replay, lock-out, parser bombs, scope of CSRF
exemption, write-time bounds and the Redis reader's deadline. (Basic auth, limits and public-prefix tests live in test_api.)"""
import json
import time
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.test import Client, override_settings
from django.urls import get_resolver
from django.utils import timezone

from apm import payloads, redis_reader, services
from apm.models import AdapterCell, AdapterRun, CellEvent, ProcessState, RetryRequest

from .base import ApmCase, matrix, table
from .test_redis import FakeRedis, configure


class OrderingAndReplay(ApmCase):
    def test_a_replayed_old_run_with_other_content_does_not_overwrite_newer_state(self):
        old_at = self.ago(hours=2)
        new_at = self.ago(minutes=1)
        self.assertEqual(self.push_matrix(self.src_a, matrix(new_at, clients=("c001", "c002"))).status_code, 200)
        r = self.push_matrix(self.src_a, matrix(old_at, failing={("c001", "NSE")}))
        self.assertEqual(r.json()["status"], "older")
        # the same old executedOn again, but with different content and fewer clients: still history only
        r = self.push_matrix(self.src_a, matrix(old_at, clients=("zz9",), failing={("zz9", "NSE")}))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "older", r.content)
        self.assertEqual(AdapterCell.objects.filter(source=self.src_a).count(), 4)
        self.assertFalse(AdapterCell.objects.filter(source=self.src_a, client="zz9").exists())
        self.assertFalse(AdapterCell.objects.filter(source=self.src_a, status=0).exists())

    def test_the_same_for_the_process_table(self):
        old_at = self.ago(hours=2)
        self.assertEqual(self.push_table(self.src_a, table(self.ago(minutes=1))).status_code, 200)
        self.assertEqual(self.push_table(self.src_a, table(old_at)).json()["status"], "older")
        r = self.push_table(self.src_a, table(old_at, down=("adapterStatus.py",), names=("other.py",)))
        self.assertEqual(r.json()["status"], "older")
        self.assertEqual(sorted(ProcessState.objects.filter(source=self.src_a).values_list("name", flat=True)),
                         ["adapterStatus.py", "processStatus.py"])

    def test_a_corrected_resend_of_the_latest_run_still_applies(self):
        at = self.ago(minutes=3)
        self.push_matrix(self.src_a, matrix(at))
        r = self.push_matrix(self.src_a, matrix(at, failing={("c001", "NSE")}))
        self.assertEqual(r.json()["status"], "update")
        self.assertEqual(AdapterCell.objects.get(source=self.src_a, client="c001", segment="NSE").status, 0)

    def test_a_date_beyond_the_clock_skew_cannot_freeze_the_state(self):
        far = self.ago(hours=-5)                      # e.g. a UTC host sending IST-less local times, or a hostile clock
        r = self.push_matrix(self.src_a, matrix(far, failing={("c001", "NSE")}))
        self.assertEqual(r.status_code, 200)
        self.assertTrue(any("future" in w for w in r.json()["warnings"]))
        run = AdapterRun.objects.get(source=self.src_a)
        self.assertLess(run.executed_at, timezone.now() + timedelta(minutes=1))
        # a real report afterwards is not "older"
        r = self.push_matrix(self.src_a, matrix(timezone.now() + timedelta(seconds=1)))
        self.assertIn(r.json()["status"], ("new", "update"))
        self.assertFalse(AdapterCell.objects.filter(source=self.src_a, status=0).exists())

    def test_small_clock_skew_is_accepted(self):
        r = self.push_matrix(self.src_a, matrix(self.ago(minutes=-5)))
        self.assertFalse(any("future" in w for w in r.json()["warnings"]))

    def test_future_log_lines_use_the_receive_time(self):
        lines, warns, _ = payloads.parse_logs([{"message": "x", "ts": "2099-01-01 00:00:00"}])
        self.assertLess(lines[0].ts, timezone.now() + timedelta(minutes=1))
        self.assertTrue(warns)


class Throttle(ApmCase):
    def test_failed_attempts_never_lock_out_a_valid_producer(self):
        for _ in range(40):
            self.push(None, {}, token="bad")
        self.assertEqual(self.push(None, {}, token="bad").status_code, 429)
        r = self.push_matrix(self.src_a)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(self.push(None, {}, token="bad").status_code, 429)

    def test_head_does_not_pick_commands(self):
        services.ingest_matrix(self.src_a, matrix(failing={("c001", "NSE")}))
        cell = AdapterCell.objects.get(source=self.src_a, client="c001", segment="NSE")
        services.request_retry(cell, self.mum_op)
        auth = {"HTTP_AUTHORIZATION": f"Bearer {self.src_a.token_plain}"}
        self.assertEqual(Client().head("/api/apm/commands/", **auth).status_code, 405)
        self.assertEqual(RetryRequest.objects.get().state, RetryRequest.PENDING)
        self.assertEqual(len(Client().get("/api/apm/commands/", **auth).json()["commands"]), 1)


class ParserBombs(ApmCase):
    """Every hostile literal fails fast with a 4xx; none is a 500 and none takes long."""

    EXPRESSIONS = {
        "unary": "-" * 300000 + "1", "tilde": "~" * 300000 + "1", "binop": "1+" * 200000 + "1", "power": "2**" * 100000 + "2",
        "not": "not " * 100000 + "1", "lambda": "lambda:" * 50000 + "1", "ternary": "1 if 1 else " * 40000 + "1",
        "attr": "a." * 200000 + "a", "brackets": "[" * 300000, "braces": "{1:" * 100000, "calls": "f(" * 100000,
        "huge int": "9" * 800000, "big mul": "'a'*10**10", "big pow": "9**9**9", "shift": "1<<10**10", "inf": "1e999",
        "set": "{1,2,3}", "bytes": "b'abc'", "complex": "1+2j", "walrus": "(x:=1)", "ellipsis": "...",
    }

    def test_hostile_literals_are_4xx_and_quick(self):
        tok = self.src_a.token_plain
        started = time.monotonic()
        for name, expr in self.EXPRESSIONS.items():
            body = json.dumps({"type": "matrix", "payload_str": "{'data': {'c': {'A': {'status': " + expr + "}}}}"})
            r = Client().post("/api/apm/ingest/", data=body, content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {tok}")
            self.assertIn(r.status_code, (400, 413, 422), name)
            self.assertIn("error", r.json(), name)
            cache.clear()
        self.assertLess(time.monotonic() - started, 30)
        self.assertFalse(AdapterCell.objects.exists())

    def test_json_bombs(self):
        tok = self.src_a.token_plain
        for name, body in {"deep array": "[" * 1_500_000, "deep object": '{"a":' * 300000, "deep payload":
                           '{"type":"matrix","payload":' + "[" * 100000 + "]" * 100000 + "}",
                           "huge int": '{"type":"matrix","payload":{"data":{"c":{"A":{"status":' + "9" * 100000 + "}}}}}",
                           "infinity": '{"type":"matrix","payload":{"data":{"c":{"A":{"status":Infinity}}}}}',
                           "float overflow": '{"type":"matrix","payload":{"data":{"c":{"A":{"status":1e999}}}}}'}.items():
            r = Client().post("/api/apm/ingest/", data=body, content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {tok}")
            self.assertIn(r.status_code, (400, 413, 422), name)
            cache.clear()

    def test_duplicate_json_keys_last_one_wins_and_cannot_smuggle_a_second_type(self):
        body = '{"type":"logs","type":"matrix","payload":{"data":{"c":{"A":{"status":2}}}}}'
        r = Client().post("/api/apm/ingest/", data=body, content_type="application/json",
                          HTTP_AUTHORIZATION=f"Bearer {self.src_a.token_plain}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["type"], "matrix")

    def test_no_content_length_is_an_empty_body_not_a_hang(self):
        r = Client().generic("POST", "/api/apm/ingest/", data=b"", HTTP_AUTHORIZATION=f"Bearer {self.src_a.token_plain}")
        self.assertEqual(r.status_code, 400)


class Text(ApmCase):
    def test_invisible_format_characters_are_removed(self):
        self.assertEqual(payloads.clean_text("c0​0﻿1", 40), "c001")
        self.assertEqual(payloads.clean_text("a‮b\r\nc d", 40), "a b c d")

    def test_look_alike_client_codes_do_not_create_a_second_cell(self):
        self.push_matrix(self.src_a, {"data": {"c001": {"NSE": {"status": 2}}, "c0​01": {"NSE": {"status": 0}}}})
        self.assertEqual(AdapterCell.objects.filter(source=self.src_a).count(), 1)

    def test_log_injection_is_flattened_to_one_line(self):
        r = self.push(self.src_a, {"type": "logs", "logs": [{"message": "ok\r\n2026-01-01 ERROR forged line\x1b[2J",
                                                              "process": "p\nq", "level": "ERROR\n"}]})
        self.assertEqual(r.status_code, 200)
        from apm.models import ExecLog
        row = ExecLog.objects.get()
        for value in (row.message, row.process, row.level):
            self.assertNotRegex(value, r"[\r\n\x00-\x1f\x7f]")


class CsrfScope(ApmCase):
    def test_only_the_three_machine_endpoints_are_csrf_exempt(self):
        found = set()

        def walk(patterns, prefix=""):
            for p in patterns:
                if hasattr(p, "url_patterns"):
                    walk(p.url_patterns, prefix + str(p.pattern))
                else:
                    cb = p.callback
                    if getattr(cb, "csrf_exempt", False) and cb.__module__.startswith("apm."):
                        found.add(prefix + str(p.pattern))
        walk(get_resolver().url_patterns)
        self.assertEqual(found, {"api/apm/ingest/", "api/apm/commands/", "api/apm/commands/<int:pk>/done/"})

    def test_path_variants_do_not_reach_the_api_unauthenticated(self):
        for path in ("/api/apmx/ingest/", "/api/apm//ingest/", "/api/apm/../apm/", "/api/apm/ingest/../../../apm/sources/",
                     "/api/apm/%2e%2e/accounts/login/", "/APi/apm/ingest/"):
            r = Client().post(path, data="{}", content_type="application/json")
            self.assertIn(r.status_code, (302, 401, 404), path)       # 302: sent to the login page, i.e. not public
            if r.status_code == 302:
                self.assertIn("/accounts/login/", r["Location"])
        r = Client().get("/api/apm/zzz/")
        self.assertIn(r.status_code, (401, 404))
        r = Client().post("/api/apm/ingest/", data="{}", content_type="application/json", HTTP_X_HTTP_METHOD_OVERRIDE="GET",
                          HTTP_AUTHORIZATION=f"Bearer {self.src_a.token_plain}")
        self.assertIn(r.status_code, (400, 422))        # still handled as the POST it is

    def test_the_token_never_appears_in_logs_or_error_bodies(self):
        tok = self.src_a.token_plain
        with self.assertLogs("linkedeye.apm", level="DEBUG") as logs, mock.patch(
                "apm.services.ingest_matrix", side_effect=RuntimeError(f"boom {tok}")):
            import logging
            logging.getLogger("linkedeye.apm").debug("anchor")
            r = self.push_matrix(self.src_a)
        self.assertEqual(r.status_code, 503)
        self.assertNotIn(tok, r.content.decode())
        self.assertNotIn(tok, "\n".join(logs.output))


@override_settings(APM_CAP_RUNS=5, APM_CAP_CELL_EVENTS=10)
class WriteTimeBounds(ApmCase):
    def test_runs_and_cell_history_are_capped_without_waiting_for_the_retention_job(self):
        base = timezone.now() - timedelta(hours=3)
        for i in range(12):
            failing = {("c001", "NSE"), ("c002", "NFO")} if i % 2 else set()
            services.ingest_matrix(self.src_a, matrix(base + timedelta(minutes=i), failing=failing))
        self.assertEqual(AdapterRun.objects.filter(source=self.src_a).count(), 5)
        self.assertLessEqual(CellEvent.objects.filter(source=self.src_a).count(), 10)
        newest = AdapterRun.objects.filter(source=self.src_a).order_by("-executed_at").first()
        self.assertEqual(timezone.localtime(newest.executed_at).minute, timezone.localtime(base + timedelta(minutes=11)).minute)

    def test_retention_touches_only_old_rows(self):
        services.ingest_matrix(self.src_b, matrix(self.ago(days=60)))
        services.ingest_matrix(self.src_a, matrix(self.ago(minutes=1)))
        services.retention()
        self.assertEqual(AdapterRun.objects.filter(source=self.src_a).count(), 1)
        self.assertEqual(AdapterCell.objects.filter(source=self.src_b).count(), 4)      # current state is not history


class RedisDeadline(ApmCase):
    def test_a_poll_that_outlives_the_deadline_drops_the_connection(self):
        configure(self.src_a)

        class Slow(FakeRedis):
            def __init__(self):
                super().__init__({"site1:ADP": "{}"})
                self.connection_pool = mock.Mock()

            def strlen(self, key):
                time.sleep(0.4)
                return super().strlen(key)

        fake = Slow()
        with mock.patch("apm.redis_reader.resolve", return_value="203.0.113.9"), \
                mock.patch("apm.redis_reader.connect", return_value=fake), mock.patch.object(redis_reader, "DEADLINE", 0.1):
            redis_reader.poll_source(self.src_a)
        fake.connection_pool.disconnect.assert_called()

    def test_a_fast_poll_cancels_the_watchdog(self):
        configure(self.src_a)
        fake = FakeRedis({})
        fake.connection_pool = mock.Mock()
        with mock.patch("apm.redis_reader.resolve", return_value="203.0.113.9"), \
                mock.patch("apm.redis_reader.connect", return_value=fake), mock.patch.object(redis_reader, "DEADLINE", 0.2):
            redis_reader.poll_source(self.src_a)
            time.sleep(0.4)
        fake.connection_pool.disconnect.assert_not_called()

    def test_hostile_values_become_a_recorded_error_not_a_crash(self):
        configure(self.src_a)
        for value in ("[1, 2, 3]", "'just a string'", "None", "{'data': 5}", "\x00\x01", "9" * 5000, "{'data': {'c': 7}}"):
            fake = FakeRedis({"site1:ADP": value, "site1:PS": value})
            with mock.patch("apm.redis_reader.resolve", return_value="203.0.113.9"), \
                    mock.patch("apm.redis_reader.connect", return_value=fake):
                res = redis_reader.poll_source(self.src_a)
            self.assertIsInstance(res, dict)
            self.src_a.refresh_from_db()
            self.assertLessEqual(len(self.src_a.redis_last_error), 300)


class ClientToken(ApmCase):
    def test_the_reference_client_does_not_forward_the_token_on_a_redirect(self):
        import urllib.request
        from apm.clients import linkedeye_apm as mod
        seen = {}

        def fake(req, timeout=None, context=None):
            seen["req"] = req
            raise urllib.error.URLError("stop")
        import urllib.error
        env = {"LINKEDEYE_APM_URL": "https://le.example.org", "LINKEDEYE_APM_TOKEN": "lea_00000000_" + "A" * 43,
               "LINKEDEYE_APM_RETRIES": "1"}
        with mock.patch.dict("os.environ", env), mock.patch.object(urllib.request, "urlopen", side_effect=fake):
            mod.push_logs([{"message": "x"}])
        req = seen["req"]
        self.assertEqual(req.get_header("Authorization"), "Bearer " + env["LINKEDEYE_APM_TOKEN"])
        self.assertNotIn("Authorization", {k.title() for k in req.headers})        # i.e. not copied by HTTPRedirectHandler
