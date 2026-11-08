"""Regression tests for the telemetry-probes review: non-ASCII request targets and DNS under the probe deadline."""
import http.server
import socket
import threading
import time
from unittest import mock

from django.test import SimpleTestCase, override_settings

from probes import checks


class _Handler(http.server.BaseHTTPRequestHandler):
    seen = []

    def do_GET(self):
        type(self).seen.append(self.path)
        if self.path == "/redir":
            # a raw UTF-8 Location header (what many servers send), not percent-encoded
            self.wfile.write(b"HTTP/1.1 302 Found\r\nLocation: /caf\xc3\xa9\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            return
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


@override_settings(PROBE_ALLOWED_PRIVATE_CIDRS=["127.0.0.0/8"])
class NonAsciiTargetTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.httpd = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        super().tearDownClass()

    def setUp(self):
        _Handler.seen = []

    def test_http_check_non_ascii_path(self):
        res = checks.http_check(f"http://127.0.0.1:{self.port}/café")
        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.status, 200)
        self.assertEqual(_Handler.seen, ["/caf%C3%A9"])

    def test_http_check_non_ascii_query(self):
        res = checks.http_check(f"http://127.0.0.1:{self.port}/s?q=café&x=1")
        self.assertTrue(res.ok, res.error)
        self.assertEqual(_Handler.seen, ["/s?q=caf%C3%A9&x=1"])

    def test_existing_percent_escapes_are_not_double_encoded(self):
        res = checks.http_check(f"http://127.0.0.1:{self.port}/a%20b/%C3%A9?q=%C3%A9")
        self.assertTrue(res.ok, res.error)
        self.assertEqual(_Handler.seen, ["/a%20b/%C3%A9?q=%C3%A9"])

    def test_non_ascii_redirect_location(self):
        res = checks.http_check(f"http://127.0.0.1:{self.port}/redir")
        self.assertTrue(res.ok, res.error)
        self.assertEqual(_Handler.seen, ["/redir", "/caf%C3%A9"])

    def test_fetch_bytes_non_ascii_path(self):
        with mock.patch.object(checks, "_open_connection",
                               lambda scheme, host, ip, port, timeout: checks._PinnedHTTPConnection(host, ip, port, timeout)):
            res = checks.fetch_bytes(f"http://127.0.0.1:{self.port}/café", 1000, https_only=False, ports=(self.port,))
        self.assertTrue(res.ok, res.error)
        self.assertEqual(_Handler.seen, ["/caf%C3%A9"])


class DnsDeadlineTests(SimpleTestCase):
    def test_http_check_dns_is_inside_the_deadline(self):
        def slow(*a, **k):
            time.sleep(3)
            raise socket.gaierror(-2, "slow")
        with mock.patch("socket.getaddrinfo", side_effect=slow):
            start = time.monotonic()
            res = checks.http_check("http://x.example/", timeout=1)
        self.assertLess(time.monotonic() - start, 2.0)
        self.assertFalse(res.ok)
        self.assertIn("Timed out", res.error)

    def test_dns_check_is_bounded(self):
        def slow(*a, **k):
            time.sleep(3)
            return []
        with mock.patch("socket.getaddrinfo", side_effect=slow):
            start = time.monotonic()
            res = checks.dns_check("x.example", timeout=1)
        self.assertLess(time.monotonic() - start, 2.0)
        self.assertFalse(res.ok)
        self.assertIn("timed out", res.error.lower())

    def test_resolve_allowed_without_timeout_still_resolves_inline(self):
        info = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))]
        with mock.patch("socket.getaddrinfo", return_value=info):
            self.assertEqual(checks.resolve_allowed("x.example", 80), "93.184.216.34")
            self.assertEqual(checks.resolve_allowed("x.example", 80, timeout=2), "93.184.216.34")

    def test_resolver_error_propagates_through_the_bounded_call(self):
        with mock.patch("socket.getaddrinfo", side_effect=socket.gaierror(-2, "nope")):
            with self.assertRaises(socket.gaierror):
                checks.resolve_allowed("x.example", 80, timeout=2)
