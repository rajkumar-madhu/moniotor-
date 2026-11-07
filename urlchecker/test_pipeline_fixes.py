"""Regression tests: CSV export neutralises spreadsheet formulas (F15); "Run all" is bounded per request (F24)."""
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import Membership, Site

from . import views
from .models import UrlCheck


@override_settings(INCIDENT_AUTO_CREATE=False, NOTIFY_WEBHOOK_URLS=[], NOTIFY_EMAILS=[])
class UrlPipelineTests(TestCase):
    def setUp(self):
        self.site = Site.objects.create(name="SiteA", code="a")
        self.op = User.objects.create_user("op", password="pw")
        Membership.objects.create(user=self.op, site=None, role="operator")
        self.client.force_login(self.op)

    def test_csv_export_neutralises_formulas(self):
        UrlCheck.objects.create(name='=HYPERLINK("http://x","y")', url="https://example.com/", site="All Sites",
                                last_error="@SUM(1)")
        body = self.client.get(reverse("urlchecker:export")).content.decode()
        row = body.splitlines()[1]
        self.assertTrue(row.startswith('"\'=HYPERLINK('), row)
        self.assertIn("'@SUM(1)", row)

    def test_run_all_is_capped_oldest_first(self):
        now = timezone.now()
        for i in range(views.RUN_ALL_LIMIT + 10):
            UrlCheck.objects.create(name=f"c{i:03}", url="https://example.com/", site="All Sites",
                                    last_checked_at=now - timedelta(minutes=i))
        UrlCheck.objects.create(name="never", url="https://example.com/", site="All Sites")
        with mock.patch("probes.runner.run_url_checks", side_effect=lambda items: ["online"] * len(items)) as run:
            r = self.client.post(reverse("urlchecker:run_all"))
        items = run.call_args.args[0]
        self.assertEqual(len(items), views.RUN_ALL_LIMIT)
        self.assertEqual(items[0].name, "never")               # never checked first, then the oldest
        self.assertEqual(items[1].name, f"c{views.RUN_ALL_LIMIT + 9:03}")
        data = r.json()
        self.assertEqual((data["checked"], data["skipped"]), (views.RUN_ALL_LIMIT, 11))
        self.assertIn("11 more were not checked now", data["summary"])

    def test_run_all_small_set_unchanged(self):
        UrlCheck.objects.create(name="one", url="https://example.com/", site="All Sites")
        with mock.patch("probes.runner.run_url_checks", return_value=["online"]):
            data = self.client.post(reverse("urlchecker:run_all")).json()
        self.assertEqual((data["checked"], data["skipped"], data["summary"]), (1, 0, "Checked 1 URL(s)"))
