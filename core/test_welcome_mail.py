from unittest import mock

from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase, override_settings

from core.welcome_mail import render_welcome_email, send_welcome_email, welcome_context


class WelcomeMailTemplateTests(TestCase):
    def test_personalized_context_and_html(self):
        user = User(username="alex", first_name="Alex", email="alex@example.com")
        ctx = welcome_context(
            user, password="Temp-Pass-123", invited_by="boss", portal_url="https://ops.example/login",
            role="Operator", site_scope="Mumbai (NSE)",
        )
        self.assertEqual(ctx["greeting_name"], "Alex")
        self.assertTrue(ctx["show_credentials"])
        text, html = render_welcome_email(
            user, password="Temp-Pass-123", invited_by="boss", portal_url="https://ops.example/login",
            role="Operator", site_scope="Mumbai (NSE)",
        )
        self.assertIn("Alex", text)
        self.assertIn("Temp-Pass-123", text)
        self.assertIn("boss", html)
        self.assertIn("Open LinkedEye", html)

    @override_settings(LINKEDEYE_WELCOME_EMAIL=False)
    def test_disabled_skips_send(self):
        user = User(username="a", email="a@example.com")
        self.assertFalse(send_welcome_email(user, password="x", role="Viewer", site_scope="All sites"))

    @override_settings(LINKEDEYE_WELCOME_EMAIL=True, EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_send_uses_multipart(self):
        user = User.objects.create_user("newbie", "newbie@example.com", "pw")
        user.first_name = "New"
        user.save(update_fields=["first_name"])
        req = RequestFactory().get("/")
        req.META["HTTP_HOST"] = "ops.example.com"
        with mock.patch("core.welcome_mail.EmailMultiAlternatives") as em_cls:
            em_cls.return_value.send.return_value = None
            self.assertTrue(send_welcome_email(user, password="Gen-123", role="Viewer", site_scope="All sites", request=req))
        em_cls.assert_called_once()
        subject = em_cls.call_args[0][0]
        self.assertIn("New", subject)
