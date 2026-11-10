"""Regression tests for verified findings in the telemetry UI area. Prometheus is always mocked."""
import io
import json
import urllib.parse
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings

from core import prometheus

from . import overview as ov

PROM = "http://prom.test:9090"
REQUEST_LINE_LIMIT = 8 * 1024        # nginx's default large_client_header_buffers (4 8k): a longer request line is a 414


def _instances(n):
    return [f"10.20.{i // 250}.{i % 250 + 1}:9100" for i in range(n)]


def _queries(sel):
    out = []
    for spec in ov.chart_specs(sel, "300s") + ov.chart_specs(sel, "300s", " offset 900s"):
        out += [g["q"] for g in spec["dims"]]
        out += [q for q in (spec["node"], spec["total"]) if q]
    return out


class _Answer(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@override_settings(PROMETHEUS_URL=PROM)
class LongPromQLTransportTests(TestCase):
    """UI-1: the Fleet Overview's instance-list PromQL (up to MAX_MATCH instances, repeated up to four times per
    query) must not be sent as a GET query string that a reverse proxy in front of Prometheus refuses with 414."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.sent = []

    def fake_urlopen(self, req, timeout=None):
        self.sent.append(req)
        return _Answer(json.dumps({"status": "success", "data": {"resultType": "matrix", "result": []}}).encode())

    def sent_params(self, req):
        if req.data is not None:
            return urllib.parse.parse_qs(req.data.decode())
        return urllib.parse.parse_qs(urllib.parse.urlsplit(req.full_url).query)

    def request_line(self, req):
        parts = urllib.parse.urlsplit(req.full_url)
        target = parts.path + ("?" + parts.query if parts.query else "")
        return f"{req.get_method()} {target} HTTP/1.1"

    def test_large_instance_matchers_fit_a_proxy_request_line(self):
        for n in (50, ov.MAX_NODES, ov.MAX_MATCH):
            sel = ov.matcher_for(_instances(n))
            for q in _queries(sel):
                self.sent.clear()
                with mock.patch("core.prometheus._auth_header", return_value=None), \
                        mock.patch("core.prometheus.urllib.request.urlopen", self.fake_urlopen):
                    prometheus.query_range(q, 900, 15)
                    prometheus.query(q)
                for req in self.sent:
                    self.assertLessEqual(len(self.request_line(req)), REQUEST_LINE_LIMIT,
                                         f"{n} nodes: request line of {len(self.request_line(req))} bytes")
                    self.assertEqual(self.sent_params(req)["query"], [q])   # the exact PromQL still reaches Prometheus

    def test_long_query_is_a_form_post_with_the_login_attached(self):
        q = _queries(ov.matcher_for(_instances(ov.MAX_MATCH)))[0]
        with mock.patch("core.prometheus._auth_header", return_value="Basic Zm9vOmJhcg=="), \
                mock.patch("core.prometheus.urllib.request.urlopen", self.fake_urlopen):
            prometheus.query_range(q, 900, 15)
        req = self.sent[0]
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.full_url, PROM + "/api/v1/query_range")
        self.assertEqual(req.get_header("Content-type"), "application/x-www-form-urlencoded")
        params = self.sent_params(req)
        self.assertEqual(params["query"], [q])
        self.assertEqual(params["step"], ["15"])
        # still an unredirected header: a redirect never forwards the login
        self.assertEqual(req.unredirected_hdrs.get("Authorization"), "Basic Zm9vOmJhcg==")
        self.assertNotIn("Authorization", req.headers)

    def test_short_queries_stay_plain_gets(self):
        with mock.patch("core.prometheus._auth_header", return_value=None), \
                mock.patch("core.prometheus.urllib.request.urlopen", self.fake_urlopen):
            prometheus.query("up")
            prometheus.query_range('node_load1{instance="a:9100"}', 900, 15)
            prometheus.targets()
        for req in self.sent:
            self.assertEqual(req.get_method(), "GET")
            self.assertIsNone(req.data)
        self.assertEqual(self.sent_params(self.sent[0])["query"], ["up"])
        self.assertEqual(self.sent[2].full_url, PROM + "/api/v1/targets")

    def test_the_overview_comment_no_longer_claims_the_url_stays_small(self):
        src = open(ov.__file__, encoding="utf-8").read()
        self.assertNotIn("the URL stays well under 16 KB", src)
