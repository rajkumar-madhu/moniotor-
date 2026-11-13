from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.auth_redirect import post_login_path


class PostLoginRedirectTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.viewer = User.objects.create_user("viewer", "v@example.com", "Good-Pass-Word-9!")
        self.op = User.objects.create_user("op", "o@example.com", "Good-Pass-Word-9!", is_staff=True)

    def test_viewer_lands_on_dashboard(self):
        self.assertEqual(post_login_path(self.viewer), reverse("dashboard"))

    def test_operator_lands_on_fleet_with_15m(self):
        self.assertTrue(post_login_path(self.op).startswith(reverse("telemetry:fleet")))
        self.assertIn("range=15m", post_login_path(self.op))

    def test_respects_safe_next(self):
        dest = reverse("search") + "?q=fw"
        self.assertEqual(post_login_path(self.viewer, dest), dest)
