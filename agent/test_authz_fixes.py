"""Site-scoped retrieval must not leak another site's records into the answer."""
from django.test import TestCase
from django.urls import reverse

from core.models import Site
from urlchecker.models import UrlCheck

from .models import Thread
from .tests import user_with


class AgentSiteLeakTests(TestCase):
    def setUp(self):
        self.a = Site.objects.create(name="Mumbai", code="mum")
        Site.objects.create(name="DR", code="dr")
        self.user = user_with("mop", "operator", self.a)
        UrlCheck.objects.create(name="nse-gw", url="https://nse.example/", site="Mumbai",
                                last_status="down", last_error="reset", enabled=True)
        UrlCheck.objects.create(name="dr-gw", url="https://dr.internal/", site="DR",
                                last_status="down", last_error="secret-timeout", enabled=True)

    def test_retrieval_answer_omits_out_of_scope_hosts(self):
        self.client.force_login(self.user)
        self.client.post(reverse("agent:new"), {"content": "What's down?"})
        t = Thread.objects.get(owner=self.user)
        text = " ".join(t.messages.filter(role="assistant").values_list("content", flat=True))
        self.assertIn("nse-gw", text)
        self.assertNotIn("dr-gw", text)
        self.assertNotIn("secret-timeout", text)
        self.assertNotIn("dr.internal", text)

    def test_json_message_stays_on_the_owner_thread(self):
        other = user_with("spy", "admin", None)
        t = Thread.objects.create(owner=other, title="admin thread")
        self.client.force_login(self.user)
        r = self.client.post(reverse("agent:message", args=[t.pk]), {"content": "leak?"},
                             HTTP_ACCEPT="application/json")
        self.assertEqual(r.status_code, 404)
