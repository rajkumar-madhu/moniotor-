"""The Redfish client against the local fake BMC: success, login failure, certificate pinning, redirects, size and time limits,
malformed answers, the SSRF policy, and that no credential ever reaches an error text."""
import logging
import time

from django.test import SimpleTestCase, TestCase, override_settings

from hardware import collector, parsers, redfish
from hardware.tests import fixtures
from hardware.tests.base import BMC_PASS, BMC_USER, loopback
from hardware.tests.fake_redfish import FakeRedfish


def client(srv, **kw):
    kw.setdefault("username", BMC_USER)
    kw.setdefault("password", BMC_PASS)
    kw.setdefault("verify_mode", "pin")
    kw.setdefault("pinned_sha256", srv.fingerprint)
    return redfish.make_client("127.0.0.1", srv.port, **kw)


@loopback
class ClientAgainstFakeBmc(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.srv = FakeRedfish(fixtures.dell_idrac9()).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()
        super().tearDownClass()

    def setUp(self):
        s = self.srv
        s.requests.clear()
        s.delay, s.drip, s.redirects, s.statuses, s.raw, s.oversize = 0, False, {}, {}, {}, set()

    def test_full_collection_over_tls_with_a_pinned_certificate(self):
        with client(self.srv) as c:
            snap = parsers.parse(collector.collect(c))
        self.assertEqual(snap.vendor, "dell")
        self.assertEqual(sum(1 for x in snap.comps if x.kind == "fan"), 6)
        self.assertTrue(all(m == "GET" for m, _p, _a in self.srv.requests))
        self.assertTrue(all(a for _m, _p, a in self.srv.requests))        # every request carried the login

    def test_keep_alive_reuses_one_connection(self):
        with client(self.srv) as c:
            c.get("/redfish/v1/")
            first = c._conn
            c.get("/redfish/v1/Systems")
            self.assertIs(c._conn, first)

    def test_wrong_password_is_auth_failed_and_the_password_is_not_in_the_message(self):
        with client(self.srv, password="wrong-password-xyz") as c, self.assertRaises(redfish.AuthFailed) as cm:
            c.get("/redfish/v1/")
        self.assertNotIn("wrong-password-xyz", str(cm.exception))
        self.assertNotIn(BMC_USER, str(cm.exception))

    def test_pin_mismatch_stops_before_any_request_is_sent(self):
        with client(self.srv, pinned_sha256="b" * 64) as c, self.assertRaises(redfish.CertificateError) as cm:
            c.get("/redfish/v1/")
        self.assertEqual(cm.exception.fingerprint, self.srv.fingerprint)
        self.assertEqual(self.srv.requests, [])                           # the login was never sent

    def test_pin_mode_without_a_pin_refuses(self):
        with client(self.srv, pinned_sha256="") as c, self.assertRaises(redfish.CertificateError):
            c.get("/redfish/v1/")
        self.assertEqual(self.srv.requests, [])

    def test_verify_mode_rejects_a_self_signed_certificate(self):
        with client(self.srv, verify_mode="verify") as c, self.assertRaises(redfish.CertificateError):
            c.get("/redfish/v1/")
        self.assertEqual(self.srv.requests, [])

    def test_skip_mode_connects_anyway(self):
        with client(self.srv, verify_mode="skip") as c:
            self.assertIn("Name", c.get("/redfish/v1/"))

    def test_redirect_is_refused_not_followed(self):
        self.srv.redirects["/redfish/v1"] = "http://evil.example/redfish/v1/"
        with client(self.srv) as c, self.assertRaises(redfish.ProtocolError) as cm:
            c.get("/redfish/v1/")
        self.assertIn("redirect", str(cm.exception))
        self.assertEqual([p for _m, p, _a in self.srv.requests], ["/redfish/v1/"])

    def test_oversized_body_is_refused(self):
        self.srv.oversize.add("/redfish/v1/Systems")
        with client(self.srv) as c, self.assertRaises(redfish.ProtocolError) as cm:
            c.get("/redfish/v1/Systems")
        self.assertIn("1 MB", str(cm.exception))

    def test_malformed_json_non_json_and_non_object(self):
        self.srv.raw["/redfish/v1/Systems"] = ("application/json", b"{not json")
        self.srv.raw["/redfish/v1/Chassis"] = ("text/html", b"<html>login</html>")
        self.srv.raw["/redfish/v1/Managers"] = ("application/json", b"[1,2,3]")
        self.srv.raw["/redfish/v1/deep"] = ("application/json", b"[" * 100000 + b"]" * 100000)
        with client(self.srv) as c:
            for p in ("/redfish/v1/Systems", "/redfish/v1/Chassis", "/redfish/v1/Managers", "/redfish/v1/deep"):
                with self.assertRaises(redfish.ProtocolError, msg=p):
                    c.get(p)

    def test_http_errors_map_to_fixed_messages(self):
        self.srv.statuses.update({"/redfish/v1/Systems": 503, "/redfish/v1/Chassis": 403})
        with client(self.srv) as c:
            with self.assertRaises(redfish.RedfishError) as cm:
                c.get("/redfish/v1/Systems")
            self.assertNotIsInstance(cm.exception, redfish.AuthFailed)
            with self.assertRaises(redfish.AuthFailed):
                c.get("/redfish/v1/Chassis")
            with self.assertRaises(redfish.NotFound):
                c.get("/redfish/v1/Nothing")

    def test_slow_server_hits_the_poll_deadline(self):
        self.srv.delay = 3
        t0 = time.monotonic()
        with client(self.srv, deadline_s=1.0, request_timeout=5) as c, self.assertRaises(redfish.Unreachable):
            c.get("/redfish/v1/")
        self.assertLess(time.monotonic() - t0, 2.5)

    def test_drip_feed_cannot_outlast_the_deadline(self):
        self.srv.drip = True
        t0 = time.monotonic()
        with client(self.srv, deadline_s=1.5, request_timeout=5) as c, self.assertRaises(redfish.Unreachable):
            c.get("/redfish/v1/")
        self.assertLess(time.monotonic() - t0, 3.5)

    def test_request_limit(self):
        with client(self.srv) as c:
            c.requests = redfish.MAX_REQUESTS
            with self.assertRaises(redfish.ProtocolError):
                c.get("/redfish/v1/")

    def test_invalid_paths_never_leave_the_process(self):
        with client(self.srv) as c:
            for bad in ("http://evil.example/redfish/v1/", "/etc/passwd", "/redfish/v1/../x", "//evil/redfish/v1"):
                with self.assertRaises(redfish.ProtocolError):
                    c.get(bad)
        self.assertEqual(self.srv.requests, [])

    def test_inspect_certificate_reports_the_fingerprint_and_sends_no_http(self):
        info = redfish.inspect_certificate("127.0.0.1", self.srv.port)
        self.assertEqual(info["fingerprint"], self.srv.fingerprint)
        self.assertFalse(info["trusted"])
        self.assertEqual(self.srv.requests, [])

    def test_repr_has_no_credentials(self):
        c = client(self.srv)
        self.assertNotIn(BMC_PASS, repr(c))
        self.assertNotIn(BMC_USER, repr(c))


class PlainHttpAndPolicy(TestCase):
    def test_plain_http_works_only_with_the_explicit_override(self):
        with loopback, FakeRedfish(fixtures.dell_idrac9(), tls=False) as srv:
            with redfish.make_client("127.0.0.1", srv.port, BMC_USER, BMC_PASS, "skip", insecure_http=True) as c:
                self.assertIn("Name", c.get("/redfish/v1/"))
            with redfish.make_client("127.0.0.1", srv.port, BMC_USER, BMC_PASS, "skip", insecure_http=False) as c, \
                    self.assertRaises(redfish.Unreachable):
                c.get("/redfish/v1/")                                  # TLS handshake against a plain server fails

    def test_loopback_and_private_hosts_are_refused_without_the_allow_list(self):
        with FakeRedfish(fixtures.dell_idrac9()) as srv:
            with redfish.make_client("127.0.0.1", srv.port, BMC_USER, BMC_PASS, "skip") as c, self.assertRaises(redfish.Unreachable) as cm:
                c.get("/redfish/v1/")
            self.assertIn("LINKEDEYE_PROBE_ALLOWED_PRIVATE_CIDRS", str(cm.exception))
            self.assertEqual(srv.requests, [])

    def test_link_local_metadata_address_is_always_refused(self):
        with override_settings(PROBE_ALLOWED_PRIVATE_CIDRS=["169.254.0.0/16", "0.0.0.0/0"]):
            with redfish.make_client("169.254.169.254", 443, "a", "b", "skip") as c, self.assertRaises(redfish.Unreachable):
                c.get("/redfish/v1/")

    def test_unresolvable_host(self):
        with redfish.make_client("no-such-host.invalid", 443, "a", "b", "skip") as c, self.assertRaises(redfish.Unreachable):
            c.get("/redfish/v1/")

    def test_nothing_secret_in_the_logs_of_a_failed_login(self):
        records = []

        class Grab(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage() + " " + str(record.exc_info))

        root, handler, old = logging.getLogger(), Grab(level=0), logging.getLogger().level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        try:
            with loopback, FakeRedfish(fixtures.dell_idrac9()) as srv:
                with redfish.make_client("127.0.0.1", srv.port, BMC_USER, "s3cr3t-pass-word", "skip") as c, self.assertRaises(redfish.AuthFailed) as cm:
                    c.get("/redfish/v1/")
        finally:
            root.removeHandler(handler)
            root.setLevel(old)
        self.assertNotIn("s3cr3t-pass-word", "\n".join(records) + str(cm.exception))
