from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from core import notify
from core.models import Event
from core.notify_templates import digest_context, event_context, render_digest_email, render_event_email


class NotifyTemplateTests(TestCase):
    def test_event_context_includes_site_and_time(self):
        ev = Event(title="Down", message="detail", severity="critical", source="urlchecker", site="Mumbai",
                   occurred_at=timezone.now(), kind="state_change", object_type="urlchecker.UrlCheck", object_id="1")
        ctx = event_context(ev)
        self.assertEqual(ctx["title"], "Down")
        self.assertEqual(ctx["site"], "Mumbai")
        self.assertIn("Critical", ctx["severity_label"])
        text, html = render_event_email(ev)
        self.assertIn("Down", text)
        self.assertIn("LinkedEye", html)
        self.assertIn("Mumbai", html)

    def test_digest_context_lists_titles(self):
        ctx = digest_context({"severity": "critical", "count": 5, "titles": ["A", "B"], "window": 60})
        self.assertIn("5 more critical", ctx["title"])
        self.assertEqual(len(ctx["lines"]), 3)
        text, html = render_digest_email({"severity": "critical", "count": 5, "titles": ["A", "B"], "window": 60})
        self.assertIn("A", text)
        self.assertIn("digest", html.lower())


@override_settings(NOTIFY_WEBHOOK_URLS=["http://hook.example/x"], NOTIFY_EMAILS=["ops@example.com"],
                   NOTIFY_SEVERITIES=["critical"])
class NotifyDeliveryTemplateTests(TestCase):
    def test_deliver_adds_html_to_webhook_and_sends_multipart_mail(self):
        ev = Event(title="Alert", message="body", severity="critical", source="test", occurred_at=timezone.now())
        with mock.patch("core.notify._post") as post, \
                mock.patch("core.notify.EmailMultiAlternatives") as em_cls:
            em = em_cls.return_value
            notify.deliver(ev)
        payload = post.call_args[0][1]
        self.assertIn("html", payload)
        self.assertIn("LinkedEye", payload["html"])
        em_cls.assert_called_once()
        em.attach_alternative.assert_called_once()
        alt = em.attach_alternative.call_args[0][0]
        self.assertIn("Alert", alt)
