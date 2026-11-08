"""The bearer-token API: authentication, the public prefix, limits, rate limiting and the retry command channel."""
import json
from datetime import timedelta

from django.core.cache import cache
from django.test import Client, override_settings
from django.utils import timezone

from apm import payloads, services
from apm.models import AdapterCell, AdapterRun, ExecLog, ProcessState, Source

from .base import ApmCase, matrix, table


class Auth(ApmCase):
    def test_happy_path_json_returns_a_request_id(self):
        r = self.push_matrix(self.src_a)
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual((body["type"], body["total"], body["duplicate"]), ("matrix", 4, False))
        self.assertEqual(r["X-Request-Id"], body["request_id"])
        self.assertEqual(r["Cache-Control"], "no-store")
        self.assertEqual(AdapterCell.objects.filter(source=self.src_a).count(), 4)
        self.src_a.refresh_from_db()
        self.assertIsNotNone(self.src_a.last_seen)

    def test_happy_path_python_dict_string(self):
        r = self.push(self.src_a, {"type": "matrix", "payload_str": str(matrix()), "script": "adapterStatus.py"})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(AdapterCell.objects.filter(source=self.src_a).count(), 4)
        r = self.push(self.src_a, {"type": "table", "payload_str": str(table())})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(ProcessState.objects.filter(source=self.src_a).count(), 2)

    def test_logs_only_and_logs_alongside_a_matrix(self):
        r = self.push(self.src_a, {"type": "logs", "logs": [{"ts": "2026-08-27 15:02:01,741", "level": "INFO", "process": "p", "message": "hi"}]})
        self.assertEqual((r.status_code, r.json()["accepted"]), (200, 1))
        r = self.push(self.src_a, {"type": "matrix", "payload": matrix(), "logs": [{"message": "with matrix"}]})
        self.assertEqual(r.json()["logs_accepted"], 1)
        self.assertEqual(ExecLog.objects.filter(source=self.src_a).count(), 2)

    def test_replay_is_accepted_and_flagged(self):
        p = matrix()
        self.assertFalse(self.push_matrix(self.src_a, p).json()["duplicate"])
        r = self.push_matrix(self.src_a, p)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["duplicate"])
        self.assertEqual(AdapterRun.objects.filter(source=self.src_a).count(), 1)

    def test_every_failure_is_the_same_401(self):
        self.src_b.enabled = False
        self.src_b.save()
        rotated = self.src_a.token_plain
        services.issue_token(self.src_a)
        self.src_a.save()
        cases = {
            "missing": self.push(None, {"type": "matrix", "payload": matrix()}, token=""),
            "garbage": self.push(None, {"type": "matrix", "payload": matrix()}, token="nope"),
            "wrong secret": self.push(None, {"type": "matrix", "payload": matrix()}, token=f"lea_{self.src_a.token_prefix}_" + "x" * 43),
            "unknown prefix": self.push(None, {"type": "matrix", "payload": matrix()}, token="lea_deadbeef_" + "x" * 43),
            "disabled": self.push(self.src_b, {"type": "matrix", "payload": matrix()}),
            "rotated away": self.push(None, {"type": "matrix", "payload": matrix()}, token=rotated),
        }
        seen = set()
        for name, r in cases.items():
            self.assertEqual(r.status_code, 401, name)
            body = r.json()
            body.pop("request_id")
            seen.add(json.dumps(body, sort_keys=True))
            self.assertIn("Bearer", r["WWW-Authenticate"])
        self.assertEqual(len(seen), 1, "401 bodies differ, which would leak what exists")
        self.assertFalse(AdapterCell.objects.exists())

    def test_wrong_scheme_and_basic_auth(self):
        tok = self.src_a.token_plain
        for header in (f"Basic {tok}", f"Token {tok}", tok, f"Bearer"):
            r = Client().post("/api/apm/ingest/", data=json.dumps({"type": "matrix", "payload": matrix()}),
                              content_type="application/json", HTTP_AUTHORIZATION=header)
            self.assertEqual(r.status_code, 401, header)

    def test_a_rotated_token_works_and_the_old_one_does_not(self):
        old = self.src_a.token_plain
        new = services.issue_token(self.src_a)
        self.src_a.save()
        self.assertEqual(self.push(None, {"type": "matrix", "payload": matrix()}, token=old).status_code, 401)
        self.assertEqual(self.push(None, {"type": "matrix", "payload": matrix()}, token=new).status_code, 200)

    def test_the_token_of_source_a_cannot_write_source_b(self):
        self.push_matrix(self.src_a, matrix(failing={("c001", "NSE")}))
        self.assertTrue(AdapterCell.objects.filter(source=self.src_a).exists())
        self.assertFalse(AdapterCell.objects.filter(source=self.src_b).exists())
        r = self.push(self.src_a, {"type": "matrix", "payload": matrix(), "source": self.src_b.pk, "source_id": self.src_b.pk})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(AdapterCell.objects.filter(source=self.src_b).exists())

    def test_only_a_hash_is_stored_never_the_token(self):
        row = Source.objects.get(pk=self.src_a.pk)
        for value in (row.token_prefix, row.token_salt, row.token_hash):
            self.assertNotIn(self.src_a.token_plain, value)
        self.assertEqual(len(row.token_hash), 64)
        self.assertNotEqual(row.token_hash, services._hash_token("", self.src_a.token_plain))   # salted
        self.assertTrue(self.src_a.token_plain.startswith(f"lea_{row.token_prefix}_"))

    def test_two_sources_with_the_same_token_text_are_impossible_but_salts_differ(self):
        self.assertNotEqual(self.src_a.token_salt, self.src_b.token_salt)

    def test_a_browser_session_does_not_authenticate_the_api(self):
        c = self.login(self.root)
        r = c.post("/api/apm/ingest/", data=json.dumps({"type": "matrix", "payload": matrix()}), content_type="application/json")
        self.assertEqual(r.status_code, 401)
        r = c.get("/api/apm/commands/")
        self.assertEqual(r.status_code, 401)
        self.assertFalse(AdapterCell.objects.exists())

    def test_csrf_is_not_required_only_here_and_only_with_a_token(self):
        c = Client(enforce_csrf_checks=True)
        r = self.push(self.src_a, {"type": "matrix", "payload": matrix()}, client=c)
        self.assertEqual(r.status_code, 200)
        c2 = Client(enforce_csrf_checks=True)
        r = c2.post("/api/apm/ingest/", data="{}", content_type="application/json")
        self.assertEqual(r.status_code, 401)             # refused for the missing token, not for CSRF

    def test_methods(self):
        r = Client().get("/api/apm/ingest/")
        self.assertEqual((r.status_code, r["Allow"]), (405, "POST"))
        r = Client().post("/api/apm/commands/")
        self.assertEqual(r.status_code, 405)
        self.assertEqual(Client().put("/api/apm/ingest/").status_code, 405)

    def test_failed_authentication_attempts_are_throttled_per_address(self):
        for _ in range(30):
            self.assertEqual(self.push(None, {}, token="bad").status_code, 401)
        r = self.push(None, {}, token="bad")
        self.assertEqual(r.status_code, 429)
        # a valid token is still served: failures from an address (shared NAT, spoofed header) must not lock producers out
        self.assertEqual(self.push_matrix(self.src_a).status_code, 200)


class PublicPrefix(ApmCase):
    """The middleware lets /api/apm/ through without a session; nothing else may become public."""

    def test_the_apm_api_is_reachable_without_a_login_but_refuses_without_a_token(self):
        self.assertEqual(Client().post("/api/apm/ingest/", data="{}", content_type="application/json").status_code, 401)

    def test_other_api_paths_still_need_a_session(self):
        for path in ("/api/backend-summary/", "/api/apmx/", "/api/v1/anything/", "/apm/live/", "/apm/export.csv", "/apm/cell/1/",
                     "/apm/sources/", "/apm/"):
            r = Client().get(path)
            self.assertIn(r.status_code, (302, 401), path)
            if r.status_code == 302:
                self.assertIn("/accounts/login/", r["Location"])

    def test_the_prefix_list_is_exactly_what_we_expect(self):
        from core.middleware import PUBLIC_PREFIXES
        added = [p for p in PUBLIC_PREFIXES if "apm" in p]
        self.assertEqual(added, ["/api/apm/"])

    def test_pages_that_post_from_a_browser_are_not_csrf_exempt(self):
        c = Client(enforce_csrf_checks=True)
        self.assertTrue(c.login(username="root", password="apm-test-pass-123"))
        for path in ("/apm/sources/new/", f"/apm/sources/{self.src_a.pk}/rotate/", f"/apm/sources/{self.src_a.pk}/toggle/",
                     f"/apm/sources/{self.src_a.pk}/delete/", f"/apm/sources/{self.src_a.pk}/test-redis/"):
            self.assertEqual(c.post(path, {}).status_code, 403, path)
        services.ingest_matrix(self.src_a, matrix())
        cell = AdapterCell.objects.filter(source=self.src_a).first()
        self.assertEqual(c.post(f"/apm/cell/{cell.pk}/retry/").status_code, 403)
        self.assertEqual(c.post(f"/apm/cell/{cell.pk}/rca/").status_code, 403)


class Limits(ApmCase):
    def test_body_over_2_mb_is_413(self):
        big = json.dumps({"type": "logs", "logs": [{"message": "x" * 900}] * 1000, "pad": "p" * (2 * 1024 * 1024)})
        r = Client().post("/api/apm/ingest/", data=big, content_type="application/json",
                          HTTP_AUTHORIZATION=f"Bearer {self.src_a.token_plain}")
        self.assertEqual(r.status_code, 413)
        self.assertEqual(r.json()["error"], "too_large")
        self.assertFalse(ExecLog.objects.exists())

    def test_matrix_limits_are_422(self):
        data = {f"c{i}": {"A": {"status": 2}} for i in range(payloads.MAX_BROKERS + 1)}
        r = self.push_matrix(self.src_a, {"data": data})
        self.assertEqual((r.status_code, r.json()["error"]), (422, "limit_brokers"))
        row = {f"S{i}": {"status": 2} for i in range(payloads.MAX_SEGMENTS + 1)}
        r = self.push_matrix(self.src_a, {"data": {"c": row}})
        self.assertEqual((r.status_code, r.json()["error"]), (422, "limit_segments"))
        self.assertFalse(AdapterCell.objects.exists())

    def test_table_and_log_limits(self):
        r = self.push_table(self.src_a, {"data": [{"name": f"p{i}"} for i in range(payloads.MAX_ROWS + 1)]})
        self.assertEqual((r.status_code, r.json()["error"]), (422, "limit_rows"))
        r = self.push(self.src_a, {"type": "logs", "logs": [{"message": "x"}] * (payloads.MAX_LOGS + 1)})
        self.assertEqual((r.status_code, r.json()["error"]), (422, "limit_logs"))
        # exactly at the limit is accepted
        r = self.push(self.src_a, {"type": "logs", "logs": [{"message": f"m{i}", "ts": f"2026-01-01 00:00:{i % 60:02d},{i:03d}"} for i in range(payloads.MAX_LOGS)]})
        self.assertEqual(r.status_code, 200)

    def test_a_bad_log_line_refuses_the_whole_request_before_the_matrix_is_applied(self):
        r = self.push(self.src_a, {"type": "matrix", "payload": matrix(), "logs": "not a list"})
        self.assertEqual(r.status_code, 422)
        self.assertFalse(AdapterCell.objects.exists())

    def test_malformed_bodies_are_4xx_never_500(self):
        tok = self.src_a.token_plain
        bodies = [b"", b"{", b"[1]", b'"x"', b"null", b'{"type":"matrix"}', b'{"type":"matrix","payload":[1]}',
                  b'{"type":"matrix","payload":{"data":{"c":{"A":{"status":NaN}}}}}', b"\xff\xfe\x00bad",
                  ("[" * 5000).encode(), b'{"type":"matrix","payload_str":"__import__(\'os\')"}',
                  b'{"type":"table","payload":{"data":"x"}}', b'{"type":"matrix","payload":{"data":{"c":{"A":{"status":' + b"9" * 6000 + b"}}}}}"]
        for body in bodies:
            r = Client().post("/api/apm/ingest/", data=body, content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {tok}")
            self.assertIn(r.status_code, (400, 413, 422), body[:40])
            self.assertIn("error", r.json())
            cache.clear()
        self.assertFalse(AdapterCell.objects.exists())

    def test_hostile_values_are_accepted_as_text_with_warnings(self):
        p = {"data": {"<script>1</script>": {"NSE": {"status": 0, "ctcl_id": "=cmd|' /C calc'!A0", "value": "\x00\x1b[31m", "error": "'; DROP TABLE x;--"}},
                      "ok": {"NSE": {"status": 7}}}}
        r = self.push_matrix(self.src_a, p)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertGreaterEqual(r.json()["warnings_total"], 1)
        c = AdapterCell.objects.get(source=self.src_a, ctcl_id="=cmd|' /C calc'!A0")
        self.assertEqual(c.value, "[31m")      # the NUL and ESC characters are gone, the rest is plain text


class RateLimit(ApmCase):
    def test_per_source_limit_is_429_with_retry_after(self):
        with override_settings():
            for i in range(services.RATE_LIMIT):
                self.assertEqual(self.push(self.src_a, {"type": "logs", "logs": [{"message": f"m{i}", "ts": "2026-01-01 00:00:00"}]}).status_code, 200)
            r = self.push(self.src_a, {"type": "logs", "logs": [{"message": "one too many"}]})
            self.assertEqual(r.status_code, 429)
            self.assertEqual(r["Retry-After"], "60")
            self.assertEqual(r.json()["error"], "rate_limited")
            self.assertEqual(self.push(self.src_b, {"type": "logs", "logs": [{"message": "other source is fine"}]}).status_code, 200)


class Commands(ApmCase):
    def setUp(self):
        super().setUp()
        services.ingest_matrix(self.src_a, matrix(failing={("c001", "NSE")}))
        self.cell = AdapterCell.objects.get(source=self.src_a, client="c001", segment="NSE")
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {self.src_a.token_plain}"}

    def test_poll_returns_pending_and_marks_them_picked(self):
        req, _ = services.request_retry(self.cell, self.mum_op)
        r = Client().get("/api/apm/commands/", **self.auth)
        self.assertEqual(r.status_code, 200)
        cmds = r.json()["commands"]
        self.assertEqual([(c["id"], c["client"], c["segment"], c["action"]) for c in cmds], [(req.pk, "c001", "NSE", "retry")])
        self.assertEqual(Client().get("/api/apm/commands/", **self.auth).json()["commands"], [])
        req.refresh_from_db()
        self.assertEqual(req.state, "picked")

    def test_done_endpoint(self):
        req, _ = services.request_retry(self.cell, self.mum_op)
        Client().get("/api/apm/commands/", **self.auth)
        r = Client().post(f"/api/apm/commands/{req.pk}/done/", **self.auth)
        self.assertEqual((r.status_code, r.json()["ok"]), (200, True))
        req.refresh_from_db()
        self.assertEqual(req.state, "done")
        self.assertEqual(Client().post(f"/api/apm/commands/{req.pk}/done/", **self.auth).status_code, 404)

    def test_another_sources_token_cannot_see_or_finish_my_commands(self):
        req, _ = services.request_retry(self.cell, self.mum_op)
        other = {"HTTP_AUTHORIZATION": f"Bearer {self.src_b.token_plain}"}
        self.assertEqual(Client().get("/api/apm/commands/", **other).json()["commands"], [])
        self.assertEqual(Client().post(f"/api/apm/commands/{req.pk}/done/", **other).status_code, 404)
        req.refresh_from_db()
        self.assertEqual(req.state, "pending")

    def test_commands_need_a_token_too(self):
        self.assertEqual(Client().get("/api/apm/commands/").status_code, 401)
        self.assertEqual(Client().post("/api/apm/commands/1/done/").status_code, 401)

    def test_ingest_response_tells_how_many_commands_wait(self):
        services.request_retry(self.cell, self.mum_op)
        self.assertEqual(self.push_matrix(self.src_a).json()["pending_commands"], 1)

    def test_completed_commands_in_the_push_body(self):
        req, _ = services.request_retry(self.cell, self.mum_op)
        Client().get("/api/apm/commands/", **self.auth)
        later = timezone.now() + timedelta(minutes=1)
        r = self.push(self.src_a, {"type": "matrix", "payload": matrix(later), "completed_commands": [req.pk]})
        self.assertEqual(r.status_code, 200)
        req.refresh_from_db()
        self.assertEqual(req.state, "done")
