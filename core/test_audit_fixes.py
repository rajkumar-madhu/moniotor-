"""Regression test for the second core audit round: a failed webhook delivery must not log the URL's path or
query, because Slack, Teams and Apprise webhooks carry their token there."""
import contextlib
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from core import notify
from core.models import Event

SECRET = "https://hooks.slack.com/services/T00000000/B00000000/XXXXsecretTOKEN"


@override_settings(NOTIFY_WEBHOOK_URLS=[SECRET], NOTIFY_EMAILS=[], NOTIFY_SEVERITIES=["critical"])
class WebhookLoggingTests(TestCase):
    def _deliver_failing(self, patch_post=True):
        ev = Event(title="Alert", message="body", severity="critical", source="test", occurred_at=timezone.now())
        fail = mock.patch("core.notify._post", side_effect=OSError("HTTP Error 404: Not Found")) if patch_post \
            else contextlib.nullcontext()
        with fail, self.assertLogs("linkedeye.notify", level="WARNING") as logs:
            notify.deliver(ev)
        return "\n".join(logs.output)

    def test_failed_delivery_logs_only_the_host(self):
        logged = self._deliver_failing()
        self.assertNotIn("XXXXsecretTOKEN", logged)
        self.assertNotIn("/services/", logged)
        self.assertIn("https://hooks.slack.com", logged)
        self.assertIn("404", logged)

    @override_settings(NOTIFY_WEBHOOK_URLS=["https://hook.example.test:8443/t/abc?token=sek"])
    def test_port_is_kept_and_query_dropped(self):
        logged = self._deliver_failing()
        self.assertIn("https://hook.example.test:8443", logged)
        self.assertNotIn("sek", logged)
        self.assertNotIn("/t/abc", logged)

    @override_settings(NOTIFY_WEBHOOK_URLS=["hooks.slack.com/services/T0/B0/XXXXsecretTOKEN"])
    def test_a_url_without_a_scheme_does_not_leak_through_the_error_text(self):
        """The admin form stores webhook URLs verbatim, so a pasted "hooks.slack.com/..." reaches urllib, which
        puts the whole URL into its ValueError message."""
        with mock.patch("core.notify.urllib.request.urlopen") as op:   # never go near the network
            op.side_effect = AssertionError("urlopen must not be reached")
            logged = self._deliver_failing(patch_post=False)
        self.assertNotIn("XXXXsecretTOKEN", logged)
        self.assertNotIn("/services/", logged)

    def test_an_error_quoting_only_the_path_is_redacted_too(self):
        """http.client raises "URL can't contain control characters. '<path>'" with the path alone."""
        e = ValueError("URL can't contain control characters. '/services/T0/B0/XXXXsecretTOKEN' (found '\\t')")
        text = notify._safe_error("https://hooks.slack.com/services/T0/B0/XXXXsecretTOKEN", e)
        self.assertNotIn("XXXXsecretTOKEN", text)
        self.assertIn("control characters", text)

    def test_userinfo_is_dropped_from_the_logged_url(self):
        self.assertEqual(notify._log_url("https://user:hunter2@h.example:8443/a/b"), "https://h.example:8443")

    def test_an_error_quoting_the_whole_url_does_not_leak_its_credentials(self):
        url = "https://user:hunter2@hook.example.test/t/abc"
        text = notify._safe_error(url, OSError(f"cannot reach '{url}': timed out"))
        self.assertNotIn("hunter2", text)
        self.assertNotIn("/t/abc", text)
        self.assertIn("timed out", text)

    def test_invalid_url_is_labelled(self):
        self.assertEqual(notify._log_url("not a url"), "(invalid URL)")
        self.assertEqual(notify._log_url(""), "(invalid URL)")
        self.assertEqual(notify._log_url("https://h.example/a/b?c=d"), "https://h.example")
