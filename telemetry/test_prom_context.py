"""The monitoring pages name the Prometheus they read; that must be the one the queries actually go to."""
from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from administration import integration_config as ic

from .views.common import prom_context


@override_settings(PROMETHEUS_URL="http://env-prom.example:9090")
class PromContextTests(TestCase):
    def setUp(self):
        ic.invalidate_cache()
        self.addCleanup(ic.invalidate_cache)

    def test_the_environment_url_is_shown_when_the_console_has_none(self):
        self.assertEqual(prom_context()["prom_host"], "env-prom.example:9090")

    def test_a_url_saved_in_the_console_wins_over_the_environment(self):
        # core.prometheus queries the console URL (integration_config); the page used to show settings.PROMETHEUS_URL
        admin = User.objects.create_superuser("adm", "adm@example.com", "pw")
        ic.save("prometheus", {"url": "https://console-prom.example:9090"}, admin)
        self.assertEqual(ic.prometheus_url(), "https://console-prom.example:9090")
        self.assertEqual(prom_context()["prom_host"], "console-prom.example:9090")
