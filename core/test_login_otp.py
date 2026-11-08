from django.contrib.auth.models import User
from django.core import mail
from django.test import Client, TestCase, override_settings
from django.urls import reverse

import re

from core.login_otp import store_otp
from core.models import Membership, Site


@override_settings(LINKEDEYE_LOGIN_OTP=True, EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class LoginOTPFlowTests(TestCase):
    def setUp(self):
        self.site = Site.objects.create(name="HQ", code="hq")
        self.user = User.objects.create_user("otpuser", "otp@example.com", "Good-Pass-Word-9!")
        Membership.objects.create(user=self.user, site=self.site, role="viewer")
        self.client = Client()

    def test_password_then_otp_completes_login(self):
        r = self.client.post(reverse("login"), {"username": "otpuser", "password": "Good-Pass-Word-9!"})
        self.assertRedirects(r, reverse("verify_otp"))
        self.assertEqual(self.client.session.get("otp_pending_user_id"), self.user.pk)
        self.assertEqual(len(mail.outbox), 1)
        otp = re.search(r"\b(\d{6})\b", mail.outbox[0].body).group(1)
        r2 = self.client.post(reverse("verify_otp"), {"otp": otp})
        self.assertEqual(r2.status_code, 302)
        self.assertNotIn("otp_pending_user_id", self.client.session)
        r3 = self.client.get(reverse("dashboard"))
        self.assertEqual(r3.status_code, 200)

    def test_pending_otp_blocks_dashboard(self):
        store_otp(self.user.pk, "123456")
        session = self.client.session
        session["otp_pending_user_id"] = self.user.pk
        session.save()
        r = self.client.get(reverse("dashboard"))
        self.assertRedirects(r, reverse("verify_otp"))

    def test_wrong_otp_shows_error(self):
        self.client.post(reverse("login"), {"username": "otpuser", "password": "Good-Pass-Word-9!"})
        r = self.client.post(reverse("verify_otp"), {"otp": "000000"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "incorrect")

    @override_settings(LINKEDEYE_LOGIN_OTP=False)
    def test_otp_disabled_uses_normal_login(self):
        r = self.client.post(reverse("login"), {"username": "otpuser", "password": "Good-Pass-Word-9!"})
        self.assertEqual(r.status_code, 302)
        self.assertNotIn("otp_pending_user_id", self.client.session)
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)

    def test_resend_otp(self):
        self.client.post(reverse("login"), {"username": "otpuser", "password": "Good-Pass-Word-9!"})
        n = len(mail.outbox)
        from datetime import timedelta
        from unittest import mock

        from django.utils import timezone

        with mock.patch("django.utils.timezone.now", return_value=timezone.now() + timedelta(seconds=90)):
            r = self.client.post(reverse("resend_otp"))
        self.assertRedirects(r, reverse("verify_otp"))
        self.assertEqual(len(mail.outbox), n + 1)

    def test_login_without_email_rejected_when_otp_on(self):
        User.objects.create_user("noemail", email="", password="Good-Pass-Word-9!")
        r = self.client.post(reverse("login"), {"username": "noemail", "password": "Good-Pass-Word-9!"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "no e-mail")
