from unittest import mock
from urllib.parse import urlencode

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from core import backend_summary
from core.models import Membership, Site


class BackendSummaryTests(TestCase):
    def setUp(self):
        self.visible = Site.objects.create(name="Mumbai", code="mumbai")
        self.hidden = Site.objects.create(name="DR", code="dr")
        self.user = User.objects.create_user("viewer")
        Membership.objects.create(user=self.user, site=self.visible, role="viewer")

    def test_dashboard_env_badge(self):
        self.assertEqual(backend_summary.dashboard_env_badge(""), "ALL SITES")
        self.assertEqual(backend_summary.dashboard_env_badge("FinSpot Dev"), "DEV")
        self.assertEqual(backend_summary.dashboard_env_badge("DR Site"), "DR")

    def test_modules_normalize_status_counts_and_ring_stops(self):
        modules = backend_summary._modules({
            "hardware": {"CRITICAL": 2, "WARNING": 1, "OK": 5, "UNKNOWN": 2},
            "software": {"CRITICAL": 0, "WARNING": 0, "OK": 0, "UNKNOWN": 0},
        })
        self.assertEqual(modules[0]["total"], 10)
        self.assertEqual(modules[0]["critical_end_pct"], 20)
        self.assertEqual(modules[0]["warning_end_pct"], 30)
        self.assertEqual(modules[0]["ok_end_pct"], 80)
        self.assertEqual(modules[0]["ok_pct"], 50)
        self.assertEqual(modules[0]["label"], "Hardware")
        self.assertEqual(modules[1]["total"], 0)
        self.assertEqual(len(modules), 2)

    def test_all_critical_module_reports_zero_ok_share(self):
        module = backend_summary._modules({"hardware": {"CRITICAL": 10}})[0]
        self.assertEqual(module["ok_end_pct"], 100)   # cumulative ring stop
        self.assertEqual(module["ok_pct"], 0)          # what the ring centre shows

    def test_cache_key_is_fixed_length_and_memcached_safe(self):
        names = ["Site %d with a long name\twith control chars" % i for i in range(200)]
        key = backend_summary._cache_key(names)
        self.assertLessEqual(len(key), 250)
        self.assertFalse(any(c.isspace() or ord(c) < 33 for c in key))
        self.assertEqual(key, backend_summary._cache_key(list(reversed(names))))
        self.assertNotEqual(key, backend_summary._cache_key(names[:-1]))

    def test_chunks_bound_query_length_and_count(self):
        names = ["x" * 300 + str(i) for i in range(20)] + ["s%d" % i for i in range(120)]
        chunks = list(backend_summary._chunks(names))
        self.assertEqual(sum(len(c) for c in chunks), len(names))
        for chunk in chunks:
            self.assertLessEqual(len(chunk), backend_summary.MAX_SITES_PER_REQUEST)
            self.assertLessEqual(len(urlencode([("site", n) for n in chunk])), backend_summary.MAX_QUERY_CHARS)
        self.assertEqual(list(backend_summary._chunks(["y" * 5000])), [])   # cannot be sent at all

    @override_settings(
        LINKEDEYE_BACKEND_SUMMARY_URL="https://legacy.example.test/dashboard/api/summary",
        LINKEDEYE_BACKEND_SUMMARY_TOKEN="test-token",
    )
    @mock.patch("core.backend_summary._fetch")
    def test_request_only_includes_sites_visible_to_user(self, fetch):
        fetch.return_value = {"configured": True, "available": True, "partial": False,
                              "modules": [], "site_count": 1}
        result = backend_summary.summary(self.user)
        self.assertTrue(result["available"])
        fetch.assert_called_once_with(["Mumbai"])

    @override_settings(
        LINKEDEYE_BACKEND_SUMMARY_URL="https://legacy.example.test/dashboard/api/summary",
        LINKEDEYE_BACKEND_SUMMARY_TOKEN="test-token",
    )
    @mock.patch("core.backend_summary._fetch")
    def test_rejects_a_site_outside_user_scope(self, fetch):
        result = backend_summary.summary(self.user, "DR")
        self.assertTrue(result["configured"])
        self.assertFalse(result["available"])
        fetch.assert_not_called()

    @override_settings(
        LINKEDEYE_BACKEND_SUMMARY_URL="https://legacy.example.test/dashboard/api/summary",
        LINKEDEYE_BACKEND_SUMMARY_TOKEN="test-token",
    )
    @mock.patch("core.backend_summary.build_opener")
    def test_fetch_batches_site_scopes_at_legacy_api_limit(self, build_opener):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, _limit):
                return b'{"status":200,"data":{"hardware":{"OK":1}},"site_count":1}'

        opener = mock.Mock()
        opener.open.return_value = Response()
        build_opener.return_value = opener
        result = backend_summary._fetch(["Site %02d" % index for index in range(51)])
        self.assertTrue(result["available"])
        self.assertEqual(result["site_count"], 2)
        self.assertEqual(result["modules"][0]["counts"]["ok"], 2)
        self.assertEqual(opener.open.call_count, 2)

    def test_summary_api_is_login_protected_and_returns_json(self):
        response = self.client.get(reverse("backend_summary_api"))
        self.assertEqual(response.status_code, 401)
        self.client.force_login(self.user)
        response = self.client.get(reverse("backend_summary_api"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/json")
