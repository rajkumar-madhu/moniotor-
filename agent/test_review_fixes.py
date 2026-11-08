"""Regression tests for the code-review findings in agent/."""
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from core.models import Site
from telemetry.models import Alert

from . import knowledge, runner, tools
from .models import KnowledgeChunk, Message, Thread
from .tests import user_with


class HistoryTests(TestCase):
    """core-agent-4"""

    def setUp(self):
        self.site = Site.objects.create(name="HQ", code="hq")
        self.user = user_with("hist", "viewer", self.site)

    @override_settings(AGENT_API_KEY="k")
    def test_model_sees_the_newest_messages_and_the_new_question_last(self):
        thread = Thread.objects.create(owner=self.user, site="")
        base = timezone.now() - timedelta(hours=1)
        for i in range(30):
            role = Message.USER if i % 2 == 0 else Message.ASSISTANT
            Message.objects.create(thread=thread, role=role, content=f"old-{i}",
                                   created_at=base + timedelta(seconds=i))
        seen = {}

        def fake_complete(messages, tools=None, model=None):
            seen["messages"] = messages
            return {"content": "done"}

        with mock.patch("agent.llm.complete", side_effect=fake_complete):
            reply = runner.run_turn(self.user, thread, "the brand new question")
        self.assertEqual(reply.content, "done")
        history = [m for m in seen["messages"] if m["role"] in ("user", "assistant")]
        self.assertEqual(history[-1], {"role": "user", "content": "the brand new question"})
        self.assertEqual(len(history), runner.HISTORY_LIMIT)
        self.assertNotIn("old-0", [m["content"] for m in history])
        # oldest-first inside the window
        contents = [m["content"] for m in history]
        self.assertEqual(contents[-2], "old-29")


class ReindexTests(TestCase):
    def _alert(self, instance, fp):
        return Alert.objects.create(fingerprint=fp, alertname="HighCPU", severity="critical", status="firing",
                                    instance=instance, site="HQ", summary="CPU is high",
                                    starts_at=timezone.now())

    def test_same_alertname_on_two_instances_gives_two_chunks(self):
        """core-agent-5"""
        Site.objects.create(name="HQ", code="hq")
        self._alert("web0:9100", "f0")
        self._alert("web1:9100", "f1")
        with mock.patch("agent.llm.configured", return_value=False):
            result = knowledge.reindex(embed_vectors=False)
        self.assertIn("indexed 2", result)
        self.assertEqual(KnowledgeChunk.objects.filter(source="problem").count(), 2)
        # a second pass is stable: still two, no error
        knowledge.reindex(embed_vectors=False)
        self.assertEqual(KnowledgeChunk.objects.filter(source="problem").count(), 2)

    def test_identical_problem_keys_in_one_pass_do_not_abort(self):
        rows = [{"source": "problem", "source_id": "dup", "site": "", "title": f"t{i}", "body": "b", "href": ""}
                for i in range(3)]
        rows.append({"source": "ticket", "source_id": "7", "site": "", "title": "T", "body": "b", "href": ""})
        with mock.patch.object(knowledge, "_collect", return_value=rows), \
                mock.patch("agent.llm.configured", return_value=False):
            result = knowledge.reindex(embed_vectors=False)
        self.assertIn("indexed", result)
        self.assertTrue(KnowledgeChunk.objects.filter(source="ticket").exists())

    def test_unchanged_rows_are_not_rewritten(self):
        """core-agent-12"""
        rows = [{"source": "ticket", "source_id": str(i), "site": "", "title": f"T{i}", "body": "same", "href": "/x"}
                for i in range(3)]
        with mock.patch.object(knowledge, "_collect", return_value=rows), \
                mock.patch("agent.llm.configured", return_value=False):
            knowledge.reindex(embed_vectors=False)
            before = dict(KnowledgeChunk.objects.values_list("pk", "updated_at"))
            KnowledgeChunk.objects.update(embedding=[0.1, 0.2])
            with mock.patch.object(KnowledgeChunk, "save", autospec=True) as save:
                knowledge.reindex(embed_vectors=False)
            self.assertEqual(save.call_count, 0)
        self.assertEqual(before, dict(KnowledgeChunk.objects.values_list("pk", "updated_at")))

    def test_changed_row_is_updated(self):
        rows = [{"source": "ticket", "source_id": "1", "site": "", "title": "T", "body": "v1", "href": "/x"}]
        with mock.patch.object(knowledge, "_collect", return_value=rows), \
                mock.patch("agent.llm.configured", return_value=False):
            knowledge.reindex(embed_vectors=False)
            rows[0]["body"] = "v2"
            knowledge.reindex(embed_vectors=False)
        self.assertEqual(KnowledgeChunk.objects.get().body, "v2")

    def test_lexical_retrieval_does_not_load_embeddings(self):
        site = Site.objects.create(name="HQ", code="hq")
        user = user_with("rv", "viewer", site)
        KnowledgeChunk.objects.create(source="ticket", source_id="1", title="disk full", body="disk on db1 is full",
                                      embedding=[0.5] * 50)
        with mock.patch("agent.llm.configured", return_value=False), CaptureQueriesContext(connection) as q:
            hits = knowledge.retrieve(user, "", "disk full")
        self.assertEqual(len(hits), 1)
        selects = [x["sql"] for x in q.captured_queries if "agent_knowledgechunk" in x["sql"]]
        self.assertTrue(selects)
        self.assertFalse(any('"embedding"' in s.split(" FROM ")[0] for s in selects), selects)


class RunToolTests(SimpleTestCase):
    def test_failing_tool_is_logged_and_reported(self):
        """core-agent-14"""
        def boom(ctx, **_kw):
            raise RuntimeError("schema drift")

        boom.le_name, boom.le_schema = "boom", {"properties": {}}
        with mock.patch.object(tools, "_TOOLS", [boom]), self.assertLogs("linkedeye.agent", "ERROR") as logs:
            out = tools.run_tool("boom", {}, mock.Mock())
        self.assertEqual(out, {"error": "boom failed"})
        self.assertTrue(any("boom" in line for line in logs.output))


class TurnLimitTests(TestCase):
    """core-agent-13"""

    def setUp(self):
        self.site = Site.objects.create(name="HQ", code="hq")
        self.user = user_with("lim", "viewer", self.site)
        self.thread = Thread.objects.create(owner=self.user)

    @override_settings(AGENT_API_KEY="k")
    def test_time_budget_stops_tool_rounds(self):
        calls = []
        tick = iter(range(0, 1000, 20))   # the monotonic clock advances 20 s per reading

        def fake_complete(messages, tools=None, model=None):
            calls.append(1)
            return {"content": "", "tool_calls": [{"id": "1", "function": {"name": "site_summary", "arguments": "{}"}}]}

        with mock.patch("agent.llm.complete", side_effect=fake_complete), \
                mock.patch("agent.runner._monotonic", side_effect=lambda: next(tick)):
            reply = runner.run_turn(self.user, self.thread, "status?")
        self.assertLess(len(calls), runner.TOOL_ROUNDS)
        self.assertEqual(reply.role, Message.ASSISTANT)
        self.assertTrue(reply.content)

    def test_tool_rows_do_not_fill_the_thread(self):
        for i in range(runner.THREAD_MESSAGE_CAP):
            Message.objects.create(thread=self.thread, role=Message.TOOL, content="{}", tool_name="x")
        reply = runner.run_turn(self.user, self.thread, "still room?")
        self.assertEqual(reply.role, Message.ASSISTANT)

    def test_user_and_assistant_rows_still_cap_the_thread(self):
        for i in range(runner.THREAD_MESSAGE_CAP):
            Message.objects.create(thread=self.thread, role=Message.USER if i % 2 == 0 else Message.ASSISTANT,
                                   content="x")
        with self.assertRaises(ValueError):
            runner.run_turn(self.user, self.thread, "full?")

    @override_settings(AGENT_TURNS_PER_MINUTE=3)
    def test_per_user_rate_limit(self):
        self.client.force_login(self.user)
        url = reverse("agent:message", args=[self.thread.pk])
        codes = [self.client.post(url, {"content": f"q{i}"}, HTTP_ACCEPT="application/json").status_code
                 for i in range(5)]
        self.assertEqual(codes[:3], [200, 200, 200])
        self.assertEqual(codes[3:], [429, 429])

    @override_settings(AGENT_TURNS_PER_MINUTE=3)
    def test_rate_limit_is_per_user(self):
        other = user_with("lim2", "viewer", self.site)
        t2 = Thread.objects.create(owner=other)
        for _i in range(3):
            runner.run_turn(self.user, self.thread, "q")
        self.assertEqual(runner.run_turn(other, t2, "hello").role, Message.ASSISTANT)


class AgentJsTests(SimpleTestCase):
    def test_enter_during_ime_composition_does_not_submit(self):
        """frontend-11"""
        js = (Path(__file__).resolve().parent / "static" / "agent" / "agent.js").read_text()
        self.assertIn("isComposing", js)
        self.assertIn("229", js)
