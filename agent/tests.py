from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import Event, Membership, Site
from urlchecker.models import UrlCheck

from . import knowledge, llm, runner
from .models import KnowledgeChunk, Message, Thread


def user_with(name, role, site=None, **extra):
    u = User.objects.create_user(name, password="x" * 14, **extra)
    Membership.objects.create(user=u, site=site, role=role)
    return u


class AgentPageTests(TestCase):
    def setUp(self):
        self.site_a = Site.objects.create(name="SiteA", code="site-a")
        self.site_b = Site.objects.create(name="SiteB", code="site-b")
        self.viewer = user_with("v", "viewer", self.site_a)
        self.other = user_with("o", "viewer", self.site_b)
        UrlCheck.objects.create(name="gw-a", url="https://a.example.com/", site="SiteA",
                                last_status="down", last_error="Connection refused", enabled=True)
        UrlCheck.objects.create(name="secret-b", url="https://b.internal/", site="SiteB",
                                last_status="down", last_error="timed out", enabled=True)

    def test_login_required(self):
        r = self.client.get(reverse("agent:chat"))
        self.assertEqual(r.status_code, 302)
        self.assertIn("/accounts/login/", r["Location"])

    @override_settings(AGENT_API_KEY="")
    def test_page_shows_not_connected_and_live_retrieval(self):
        self.client.force_login(self.viewer)
        r = self.client.get(reverse("agent:chat"))
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn("LINKEDEYE_AGENT_API_KEY", html)
        self.assertIn("Language model is not connected", html)
        self.assertNotIn("sk-secret", html)
        r = self.client.post(reverse("agent:new"), {"content": "What's down right now?"})
        self.assertEqual(r.status_code, 302)
        thread = Thread.objects.get(owner=self.viewer)
        page = self.client.get(thread.get_absolute_url())
        body = page.content.decode()
        self.assertIn("gw-a", body)
        self.assertIn("Connection refused", body)
        self.assertNotIn("secret-b", body)
        self.assertNotIn("b.internal", body)

    def test_one_site_viewer_cannot_read_another_users_thread(self):
        t = Thread.objects.create(owner=self.other, site="SiteB", title="DR outage")
        Message.objects.create(thread=t, role=Message.ASSISTANT, content="secret-b is down")
        self.client.force_login(self.viewer)
        r = self.client.get(reverse("agent:thread", args=[t.pk]))
        self.assertEqual(r.status_code, 404)

    def test_owner_can_delete_own_thread(self):
        self.client.force_login(self.viewer)
        t = Thread.objects.create(owner=self.viewer, title="mine")
        r = self.client.post(reverse("agent:delete", args=[t.pk]))
        self.assertEqual(r.status_code, 302)
        self.assertFalse(Thread.objects.filter(pk=t.pk).exists())

    def test_empty_message_is_rejected(self):
        self.client.force_login(self.viewer)
        t = Thread.objects.create(owner=self.viewer)
        r = self.client.post(reverse("agent:message", args=[t.pk]), {"content": "  "},
                             HTTP_ACCEPT="application/json")
        self.assertEqual(r.status_code, 400)

    def test_json_send_returns_title_from_first_message(self):
        self.client.force_login(self.viewer)
        t = Thread.objects.create(owner=self.viewer)
        r = self.client.post(
            reverse("agent:message", args=[t.pk]),
            {"content": "Open incidents"},
            HTTP_ACCEPT="application/json",
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["title"], "Open incidents")
        self.assertIn("assistant", data)
        self.assertIn("gw-a", data["assistant"]["content"])
        t.refresh_from_db()
        self.assertEqual(t.title, "Open incidents")


class AgentAuthzTests(TestCase):
    def setUp(self):
        self.site_a = Site.objects.create(name="SiteA", code="site-a")
        Site.objects.create(name="SiteB", code="site-b")
        self.op = user_with("op", "operator", self.site_a)

    def test_csrf_required(self):
        c = self.client_class(enforce_csrf_checks=True)
        c.force_login(self.op)
        r = c.post(reverse("agent:new"), {"content": "hello"})
        self.assertEqual(r.status_code, 403)


class KnowledgeAndModelTests(TestCase):
    def setUp(self):
        self.site_a = Site.objects.create(name="SiteA", code="site-a")
        self.site_b = Site.objects.create(name="SiteB", code="site-b")
        self.viewer = user_with("v", "viewer", self.site_a)
        Event.objects.create(source="urlchecker", severity="critical", title="gw-a down",
                             message="probe failed", site="SiteA")
        Event.objects.create(source="urlchecker", severity="critical", title="secret-b down",
                             message="other site", site="SiteB")

    def test_reindex_and_retrieve_are_site_scoped(self):
        knowledge.reindex(embed_vectors=False)
        self.assertTrue(KnowledgeChunk.objects.filter(title="gw-a down").exists())
        hits = knowledge.retrieve(self.viewer, "", "gw-a down")
        titles = [h["title"] for h in hits]
        self.assertTrue(any("gw-a" in t for t in titles))
        self.assertFalse(any("secret-b" in t for t in titles))

    @override_settings(AGENT_API_KEY="sk-test-key", AGENT_MODEL="test-model")
    def test_model_turn_persists_reply_and_never_echoes_the_key(self):
        def fake_complete(messages, tools=None, model=None):
            return {"role": "assistant", "content": "gw-a is down on SiteA."}

        self.client.force_login(self.viewer)
        with mock.patch("agent.runner.llm.complete", side_effect=fake_complete):
            r = self.client.post(reverse("agent:new"), {"content": "status of gw-a"})
        self.assertEqual(r.status_code, 302)
        t = Thread.objects.get(owner=self.viewer)
        reply = t.messages.filter(role=Message.ASSISTANT).get()
        self.assertIn("gw-a", reply.content)
        page = self.client.get(t.get_absolute_url()).content.decode()
        self.assertNotIn("sk-test-key", page)

    @override_settings(AGENT_API_KEY="sk-test-key", AGENT_BASE_URL="http://169.254.169.254/v1")
    def test_metadata_url_is_blocked(self):
        with self.assertRaises(llm.LLMError) as cm:
            llm.complete([{"role": "user", "content": "hi"}])
        self.assertIn("blocked", str(cm.exception).lower())

    @override_settings(AGENT_API_KEY="")
    def test_ping_without_key_does_not_call_the_network(self):
        with mock.patch("agent.llm._post") as post:
            ok, msg = llm.ping()
        post.assert_not_called()
        self.assertFalse(ok)
        self.assertIn("LINKEDEYE_AGENT_API_KEY", msg)

    def test_cosine_and_lexical_helpers(self):
        self.assertGreater(knowledge.lexical_score("gateway down", "gw down", "the gateway is down"), 0)
        self.assertAlmostEqual(knowledge.cosine([1, 0], [1, 0]), 1.0)
        self.assertEqual(knowledge.cosine([1, 0], [0, 1]), 0.0)


class AgentJobsTests(TestCase):
    def test_prune_and_reindex_never_raise(self):
        from . import jobs
        old = Thread.objects.create(owner=User.objects.create_user("x", password="x" * 14), title="old")
        Thread.objects.filter(pk=old.pk).update(updated_at=timezone.now() - timedelta(days=400))
        with override_settings(AGENT_RETENTION_DAYS=90):
            self.assertIn("pruned", jobs.prune_threads())
        self.assertFalse(Thread.objects.filter(pk=old.pk).exists())
        self.assertIn("indexed", jobs.reindex_knowledge())
