"""Parsing and validation: both formats, every limit, and hostile input. Pure functions, no database."""
import json

from django.test import SimpleTestCase
from django.utils import timezone

from apm import payloads
from apm.payloads import PayloadError

from .base import matrix, table


class ParseFormats(SimpleTestCase):
    def test_json_and_python_dict_string_give_the_same_matrix(self):
        m = matrix(failing={("c001", "NSE")})
        a = payloads.parse_request(json.dumps({"type": "matrix", "payload": m}))
        b = payloads.parse_request(json.dumps({"type": "matrix", "payload_str": str(m)}))
        pa, pb = payloads.parse_matrix(a.payload), payloads.parse_matrix(b.payload)
        self.assertEqual(pa.payload_hash, pb.payload_hash)
        self.assertEqual([(c.client, c.segment, c.status) for c in pa.cells], [(c.client, c.segment, c.status) for c in pb.cells])

    def test_python_literals_true_none_are_accepted(self):
        text = str({"data": {"c001": {"NSE": {"isSuccess": True, "ctcl_id": "1", "status": 2}, "NFO": None}}, "executedOn": "01-02-2026 10:00:00 [ Total: 1 ; Success: 1 ; Failure: 0 ]"})
        m = payloads.parse_matrix(payloads.sanitize_tree(payloads.parse_literal(text)))
        self.assertEqual(len(m.cells), 1)   # None = not configured, like "-"

    def test_dash_cells_are_not_adapters(self):
        m = payloads.parse_matrix({"data": {"c001": {"NSE": "-", "NFO": {"status": 2, "ctcl_id": "7"}}}})
        self.assertEqual([(c.segment, c.ctcl_id) for c in m.cells], [("NFO", "7")])

    def test_status_comes_from_status_then_issuccess(self):
        m = payloads.parse_matrix({"data": {"c": {"A": {"status": 1}, "B": {"isSuccess": False}, "C": {"isSuccess": True}, "D": {}}}})
        self.assertEqual({c.segment: c.status for c in m.cells}, {"A": 1, "B": 0, "C": 2, "D": 3})

    def test_bad_status_is_a_warning_not_an_error(self):
        m = payloads.parse_matrix({"data": {"c": {"A": {"status": 9, "ctcl_id": "1"}}}})
        self.assertEqual(m.cells[0].status, 3)
        self.assertTrue(any("not 0-3" in w for w in m.warnings))

    def test_executed_on_formats(self):
        dt, key, declared = payloads.parse_executed("27-08-2026 15:02:01 [ Total: 212 ; Success: 204 ; Failure: 8 ]")
        self.assertEqual((dt.day, dt.month, dt.year, dt.hour), (27, 8, 2026, 15))
        self.assertEqual(declared, (212, 204, 8))
        self.assertEqual(key, "27-08-2026 15:02:01")
        dt2, _, _ = payloads.parse_executed("2026-08-27 15:02:01 [ Total: 4 ; Success: 4 ; Failure: 0 ]")
        self.assertEqual((dt2.day, dt2.month), (27, 8))

    def test_executed_on_is_read_in_the_project_time_zone(self):
        dt, _, _ = payloads.parse_executed("27-08-2026 15:02:01")
        self.assertEqual(timezone.localtime(dt).hour, 15)

    def test_declared_count_mismatch_warns(self):
        m = payloads.parse_matrix({"executedOn": "01-02-2026 10:00:00 [ Total: 5 ; Success: 5 ; Failure: 0 ]",
                                   "data": {"c": {"A": {"status": 2}}}})
        self.assertTrue(any("declares 5" in w for w in m.warnings))

    def test_future_executed_on_falls_back_to_now(self):
        m = payloads.parse_matrix({"executedOn": "01-01-2099 00:00:00", "data": {"c": {"A": {"status": 2}}}})
        self.assertLess(m.executed_at, timezone.now() + timezone.timedelta(minutes=1))

    def test_table_optional_extra_fields(self):
        t = payloads.parse_table({"data": [{"name": "p", "pid": "12", "status": 2, "cpu_percent": 2.5, "memory_percent": "0.7",
                                            "uptime": "02:18:33", "response_time": 152, "exit_code": 0, "remarks": "OK",
                                            "last_heartbeat": "2026-08-27 15:12:11", "host": "h"}]}, host="x")
        r = t.rows[0]
        self.assertEqual((r.host, r.pid, r.uptime_s, r.response_ms, r.cpu_pct, r.mem_pct), ("h", 12, 8313, 152.0, 2.5, 0.7))
        self.assertEqual(r.last_heartbeat.second, 11)

    def test_table_na_values(self):
        t = payloads.parse_table({"data": [{"pid": "NA", "name": "p", "cpu_percent": "NA", "memory_percent": "NA", "status": 0, "Status_": "not running"}]})
        r = t.rows[0]
        self.assertEqual((r.pid, r.cpu_pct, r.mem_pct, r.status), (None, None, None, 0))

    def test_table_status_from_text(self):
        t = payloads.parse_table({"data": [{"name": "a", "Status_": "running"}, {"name": "b", "Status_": "not running"}, {"name": "c"}]})
        self.assertEqual([r.status for r in t.rows], [2, 0, 3])

    def test_table_duplicate_rows_last_wins(self):
        t = payloads.parse_table({"data": [{"name": "a", "pid": 1}, {"name": "a", "pid": 2}]}, host="h")
        self.assertEqual([r.pid for r in t.rows], [2])
        self.assertTrue(t.warnings)

    def test_logs_levels_and_timestamps(self):
        lines, warns, _ = payloads.parse_logs([
            {"ts": "2026-08-27 15:01:59,123", "level": "warn", "process": "p", "message": "m1"},
            {"ts": 1788000000, "level": "FATAL", "message": "m2"},
            {"ts": "nonsense", "level": "weird", "message": "m3"}])
        self.assertEqual([l.level for l in lines], ["WARNING", "ERROR", "INFO"])
        self.assertEqual(lines[0].ts.microsecond, 123000)
        self.assertTrue(warns)


class Limits(SimpleTestCase):
    def test_too_many_brokers(self):
        data = {f"c{i}": {"A": {"status": 2}} for i in range(payloads.MAX_BROKERS + 1)}
        with self.assertRaises(PayloadError) as cm:
            payloads.parse_matrix({"data": data})
        self.assertEqual(cm.exception.code, "limit_brokers")

    def test_exactly_the_broker_limit_is_fine(self):
        data = {f"c{i}": {"A": {"status": 2}} for i in range(payloads.MAX_BROKERS)}
        self.assertEqual(len(payloads.parse_matrix({"data": data}).cells), payloads.MAX_BROKERS)

    def test_too_many_segments(self):
        row = {f"S{i}": {"status": 2} for i in range(payloads.MAX_SEGMENTS + 1)}
        with self.assertRaises(PayloadError) as cm:
            payloads.parse_matrix({"data": {"c": row}})
        self.assertEqual(cm.exception.code, "limit_segments")

    def test_too_many_rows(self):
        with self.assertRaises(PayloadError) as cm:
            payloads.parse_table({"data": [{"name": f"p{i}"} for i in range(payloads.MAX_ROWS + 1)]})
        self.assertEqual(cm.exception.code, "limit_rows")

    def test_too_many_logs(self):
        with self.assertRaises(PayloadError) as cm:
            payloads.parse_logs([{"message": "x"}] * (payloads.MAX_LOGS + 1))
        self.assertEqual(cm.exception.code, "limit_logs")

    def test_logs_limit_on_the_request_envelope(self):
        body = json.dumps({"type": "logs", "logs": [{"message": "x"}] * (payloads.MAX_LOGS + 1)})
        with self.assertRaises(PayloadError):
            payloads.parse_request(body)

    def test_string_lengths_are_capped(self):
        m = payloads.parse_matrix({"data": {"c" * 500: {"S" * 500: {"status": 2, "ctcl_id": "i" * 500, "value": "v" * 500, "error": "e" * 900}}}})
        c = m.cells[0]
        self.assertEqual((len(c.client), len(c.segment), len(c.ctcl_id), len(c.value)), (40, 20, 64, 120))
        self.assertLessEqual(len(c.error), 240)
        lines, _, _ = payloads.parse_logs([{"message": "m" * 5000, "process": "p" * 500}])
        self.assertEqual((len(lines[0].message), len(lines[0].process)), (1000, 80))

    def test_payload_str_size_limit(self):
        with self.assertRaises(PayloadError) as cm:
            payloads.parse_literal("'" + "a" * (payloads.MAX_LITERAL + 1) + "'")
        self.assertEqual(cm.exception.http, 413)

    def test_payload_and_payload_str_together_are_refused(self):
        with self.assertRaises(PayloadError):
            payloads.parse_request(json.dumps({"type": "matrix", "payload": {}, "payload_str": "{}"}))
        with self.assertRaises(PayloadError):
            payloads.parse_request(json.dumps({"type": "matrix"}))

    def test_unknown_type(self):
        with self.assertRaises(PayloadError):
            payloads.parse_request(json.dumps({"type": "shell", "payload": {}}))

    def test_duration_must_be_a_number(self):
        with self.assertRaises(PayloadError):
            payloads.parse_request(json.dumps({"type": "matrix", "payload": {}, "duration_s": "soon"}))


class Hostile(SimpleTestCase):
    def test_huge_integers_are_refused(self):
        with self.assertRaises(PayloadError):
            payloads.sanitize_tree({"a": 10 ** 40})
        with self.assertRaises(PayloadError):
            payloads.parse_request('{"type":"logs","logs":[{"message":"x","ts":' + "9" * 5000 + "}]}")

    def test_nan_and_infinity_are_refused(self):
        for text in ('{"type":"matrix","payload":{"a":NaN}}', '{"type":"matrix","payload":{"a":Infinity}}', '{"type":"matrix","payload":{"a":-Infinity}}'):
            with self.assertRaises(PayloadError) as cm:
                payloads.parse_request(text)
            self.assertEqual(cm.exception.code, "bad_json")
        with self.assertRaises(PayloadError):
            payloads.sanitize_tree({"a": float("nan")})

    def test_deep_nesting_json_and_literal(self):
        deep = "[" * 3000 + "]" * 3000
        with self.assertRaises(PayloadError):
            payloads.parse_request('{"type":"matrix","payload":' + deep + "}")
        with self.assertRaises(PayloadError):
            payloads.parse_literal(deep)
        nested = {}
        cur = nested
        for _ in range(payloads.MAX_DEPTH + 3):
            cur["x"] = {}
            cur = cur["x"]
        with self.assertRaises(PayloadError):
            payloads.sanitize_tree(nested)

    def test_literal_never_evaluates_code(self):
        for evil in ("__import__('os').system('true')", "[x for x in range(3)]", "open('/etc/passwd')", "1+1", "{}.__class__"):
            with self.assertRaises(PayloadError):
                payloads.parse_literal(evil)

    def test_literal_rejects_non_json_types(self):
        for text in ("{'a': {1, 2}}", "{'a': b'x'}", "{'a': 1j}"):
            with self.assertRaises(PayloadError):
                payloads.sanitize_tree(payloads.parse_literal(text))

    def test_control_characters_and_bidi_are_stripped(self):
        s = payloads.clean_text("a\x00b\x1b[31mc\r\nd‮evil⁦x", 100)
        for ch in ("\x00", "\x1b", "\r", "\n", "‮", "⁦"):
            self.assertNotIn(ch, s)
        self.assertIn("evil", s)

    def test_lone_surrogates_are_replaced(self):
        s = payloads.clean_text("ok\ud800bad", 50)
        s.encode("utf-8")      # must not raise: PostgreSQL would reject a surrogate

    def test_unicode_is_kept(self):
        self.assertEqual(payloads.clean_text("नमस्ते 株式会社 ✓", 50), "नमस्ते 株式会社 ✓")

    def test_html_sql_and_formulas_are_stored_as_text(self):
        m = payloads.parse_matrix({"data": {"<script>alert(1)</script>": {"NSE": {"status": 0, "ctcl_id": "'; DROP TABLE x;--", "value": "=HYPERLINK(\"http://x\")", "error": "<img src=x onerror=alert(1)>"}}}})
        c = m.cells[0]
        self.assertEqual(c.client, "<script>alert(1)</script>"[:40])
        self.assertEqual(c.ctcl_id, "'; DROP TABLE x;--")
        self.assertEqual(c.value, '=HYPERLINK("http://x")')

    def test_non_object_inputs(self):
        for bad in ([], "x", 5, None):
            with self.assertRaises(PayloadError):
                payloads.parse_matrix(bad)
            with self.assertRaises(PayloadError):
                payloads.parse_table(bad)
        with self.assertRaises(PayloadError):
            payloads.parse_matrix({"data": []})
        with self.assertRaises(PayloadError):
            payloads.parse_request("[1,2]")

    def test_bools_are_not_numbers(self):
        self.assertIsNone(payloads._int_or_none(True))
        self.assertIsNone(payloads._float_or_none(True))
        t = payloads.parse_table({"data": [{"name": "p", "pid": True, "cpu_percent": True}]})
        self.assertEqual((t.rows[0].pid, t.rows[0].cpu_pct), (None, None))

    def test_numbers_out_of_range_become_none(self):
        t = payloads.parse_table({"data": [{"name": "p", "pid": 2 ** 40, "cpu_percent": 1e30, "exit_code": 2 ** 40}]})
        r = t.rows[0]
        self.assertEqual((r.pid, r.cpu_pct, r.exit_code), (None, None, None))
