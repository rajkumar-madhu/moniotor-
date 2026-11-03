"""Regression tests for F17: a deeply nested log entry must be rejected on its own, never 500 the whole batch."""
import json

from django.test import TestCase, override_settings
from django.utils import timezone

from . import services
from .models import LogEntry

TOKEN = "l" * 32


def nested(depth):
    root = cur = {}
    for _ in range(depth):
        cur["a"] = {}
        cur = cur["a"]
    return root


@override_settings(LOG_INGEST_TOKEN=TOKEN)
class DeepNestingTests(TestCase):
    def post(self, entries):
        self.client.raise_request_exception = False
        return self.client.post("/api/v1/logs/", json.dumps(entries), content_type="application/json",
                                HTTP_AUTHORIZATION="Bearer " + TOKEN)

    def test_deep_attributes_reject_only_that_entry(self):
        entries = [{"source": "s", "level": "info", "message": f"ok {i}"} for i in range(50)]
        entries.append({"source": "s", "level": "info", "message": "bad", "attributes": nested(900)})
        r = self.post(entries)
        self.assertEqual(r.status_code, 200, r.content[:200])
        body = r.json()
        self.assertEqual((body["accepted"], body["rejected"]), (50, 1))
        self.assertIn("nested", body["errors"][0]["error"])
        self.assertEqual(LogEntry.objects.count(), 50)

    def test_deep_message_object_rejected(self):
        out = services.ingest([{"source": "s", "level": "info", "message": nested(500)},
                               {"source": "s", "level": "info", "message": "fine"}])
        self.assertEqual((out["accepted"], out["rejected"]), (1, 1))

    def test_no_nul_is_iterative_and_capped(self):
        self.assertEqual(services._no_nul({"k\x00": ["a\x00b", {"x": "\x00"}], "n": 1}), {"k": ["ab", {"x": ""}], "n": 1})
        services._no_nul(nested(31))   # within the cap
        with self.assertRaises(ValueError):
            services._no_nul(nested(5000))   # no RecursionError, whatever the depth

    def test_recursion_error_in_validation_is_per_entry(self):
        from unittest import mock
        real = services.validate_entry

        def flaky(entry, now):
            if entry.get("message") == "boom":
                raise RecursionError("maximum recursion depth exceeded")
            return real(entry, now)

        with mock.patch("observe.services.validate_entry", side_effect=flaky):
            out = services.ingest([{"source": "s", "level": "info", "message": "boom"},
                                   {"source": "s", "level": "info", "message": "fine"}], now=timezone.now())
        self.assertEqual((out["accepted"], out["rejected"]), (1, 1))
