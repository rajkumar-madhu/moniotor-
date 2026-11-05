import json
from unittest import mock

from django.test import RequestFactory, TestCase, override_settings
from dashboard.views import backend_summary_api

# Create your tests here.


class BackendSummaryApiTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    @override_settings(DASHBOARD_API_TOKEN="test-secret")
    @mock.patch("dashboard.views.SiteModel.objects.filter")
    @mock.patch("dashboard.views.Node")
    def test_requires_bearer_token_before_querying_sites(self, node, site_filter):
        request = self.factory.get("/dashboard/api/summary", {"site": "Mumbai"})
        response = backend_summary_api(request)
        self.assertEqual(response.status_code, 401)
        site_filter.assert_not_called()
        node.assert_not_called()

    @override_settings(DASHBOARD_API_TOKEN="test-secret")
    @mock.patch("dashboard.views.SiteModel.objects.filter")
    @mock.patch("dashboard.views.Node")
    def test_returns_aggregated_statuses_for_requested_enabled_sites(self, node, site_filter):
        site = mock.Mock(entity_host="neo4j.internal", entity_port="7687", is_URLSecured=True)
        site_filter.return_value = [site]
        node.return_value.overallStats.return_value = {
            "status": 200,
            "data": {
                "hardware": {0: 2, 1: 1, 2: 3, 3: 4},
                "software": {0: 0, 1: 1, 2: 5, 3: 0},
                "application": {0: 1, 1: 0, 2: 2, 3: 0},
            },
        }
        request = self.factory.get("/dashboard/api/summary", [("site", "Mumbai")])
        request.META["HTTP_AUTHORIZATION"] = "Bearer test-secret"
        response = backend_summary_api(request)
        data = json.loads(response.content)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["data"]["hardware"]["CRITICAL"], 2)
        self.assertEqual(data["data"]["hardware"]["UNKNOWN"], 4)
        self.assertEqual(data["data"]["software"]["OK"], 5)
        self.assertEqual(data["data"]["application"]["WARNING"], 0)
        self.assertEqual(data["site_count"], 1)
        site_filter.assert_called_once_with(sitename__in=["Mumbai"], is_enable=True)
