"""The vSphere client against the local fake vCenter: read-only guarantee, TLS, deadlines, caps, secrets."""
import re
import time
from pathlib import Path
from unittest import mock

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings

from hypervisor import parsers, services, sync, vsphere
from hypervisor.tests.base import HvCase, capture_logs
from hypervisor.tests.fake_vcenter import PASSWORD, USER, FakeVCenter

ALLOW = override_settings(PROBE_ALLOWED_PRIVATE_CIDRS=["127.0.0.0/8"])


def client(fake, **kw):
    kw.setdefault("tls_mode", "skip")
    return vsphere.VSphereClient(fake.host, fake.port, kw.pop("user", USER), kw.pop("password", PASSWORD), **kw)


@ALLOW
class ClientTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fake = FakeVCenter().start()

    @classmethod
    def tearDownClass(cls):
        cls.fake.stop()
        super().tearDownClass()

    def setUp(self):
        f = self.fake
        f.requests.clear()
        f.auth_headers.clear()
        f.fail_login = False
        f.redirect_prefix = f.oversize_prefix = f.malformed_prefix = f.slow_prefix = f.drop_prefix = ""
        f.expire_after = 0
        f.logins = f.logouts = f.gets_since_login = 0
        f.inv.update(__import__("hypervisor.tests.fake_vcenter", fromlist=["x"]).default_inventory())

    # ---- the read-only guarantee
    def test_full_inventory_uses_only_get_and_the_session_requests(self):
        with client(self.fake) as c:
            inv = vsphere.collect_inventory(c, lambda s: [x["moref"] for x in s])
        self.assertEqual(len(inv["vms"]), 5)
        self.assertEqual(set(self.fake.methods), {"GET", "POST", "DELETE"})
        for method, path, _s in self.fake.requests:
            if method != "GET":
                self.assertEqual(path, "/api/session", (method, path))
        self.assertEqual(self.fake.logouts, 1)
        self.assertEqual(sum(1 for m, _p, _s in self.fake.requests if m == "POST"), 1)

    def test_client_refuses_every_other_method_before_sending(self):
        with client(self.fake) as c:
            n = len(self.fake.requests)
            for method in ("PUT", "PATCH", "POST", "DELETE", "HEAD", "OPTIONS"):
                with self.assertRaises(vsphere.VSphereError) as cm:
                    c._send(method, "/api/vcenter/vm/vm-101/power?action=stop")
                self.assertEqual(cm.exception.kind, "refused")
            with self.assertRaises(vsphere.VSphereError):
                c._send("GET", "/etc/passwd")
            with self.assertRaises(vsphere.VSphereError):
                c._send("GET", "/api/vcenter/../x")
            self.assertEqual(len(self.fake.requests), n)         # nothing went on the wire

    def test_source_code_has_no_write_method_beyond_the_session(self):
        text = Path(vsphere.__file__).read_text()
        self.assertEqual(len(re.findall(r'"PUT"|"PATCH"', text)), 0)
        # POST and DELETE appear only in the SESSION_REQUESTS definition and in login() / logout()
        for word in ("POST", "DELETE"):
            lines = [i for i, l in enumerate(text.splitlines(), 1) if f'"{word}"' in l]
            self.assertEqual(len(lines), 2, (word, lines))
        self.assertEqual(vsphere.READ_METHODS, frozenset({"GET"}))
        self.assertEqual(vsphere.SESSION_REQUESTS, frozenset({("POST", "/api/session"), ("DELETE", "/api/session")}))
        for name in ("put", "patch", "post", "delete", "power", "reconfigure", "snapshot_create"):
            self.assertFalse(hasattr(vsphere.VSphereClient, name), name)

    # ---- login and secrets
    def test_wrong_password_is_an_auth_error_and_nothing_else_is_requested(self):
        with self.assertRaises(vsphere.VSphereError) as cm:
            with client(self.fake, password="Wrong-Password-1"):
                pass
        self.assertEqual(cm.exception.kind, "auth")
        self.assertNotIn("Wrong-Password-1", str(cm.exception))
        self.assertEqual([m for m, _p, _s in self.fake.requests], ["POST"])

    def test_password_never_in_error_text_or_logs(self):
        self.fake.fail_login = True
        with capture_logs() as buf:
            with self.assertRaises(vsphere.VSphereError) as cm:
                with client(self.fake):
                    pass
        self.assertNotIn(PASSWORD, str(cm.exception))
        self.assertNotIn(PASSWORD, buf.getvalue())
        self.assertNotIn(USER, str(cm.exception))

    def test_http_basic_only_on_the_session_request(self):
        with client(self.fake) as c:
            c.get("/api/vcenter/host")
        auth = [(m, a) for (m, _p, _s), a in zip(self.fake.requests, self.fake.auth_headers)]
        self.assertTrue(auth[0][1].startswith("Basic "))
        self.assertTrue(all(a == "" for _m, a in auth[1:]))

    # ---- TLS
    def test_verify_mode_rejects_a_self_signed_certificate(self):
        with self.assertRaises(vsphere.VSphereError) as cm:
            with client(self.fake, tls_mode="verify"):
                pass
        self.assertEqual(cm.exception.kind, "tls")
        self.assertEqual(self.fake.requests, [])

    def test_verify_mode_accepts_a_trusted_certificate(self):
        import ssl
        real = ssl.create_default_context
        with mock.patch.object(vsphere.ssl, "create_default_context", lambda: real(cafile=str(self.fake.cert_path))):
            with client(self.fake, tls_mode="verify") as c:
                self.assertTrue(c.get_list("/api/vcenter/host"))

    def test_pin_matches(self):
        with client(self.fake, tls_mode="pin", pinned_sha256=self.fake.fingerprint.upper()) as c:
            self.assertTrue(c.get_list("/api/vcenter/host"))
        self.assertEqual(c.fingerprint, self.fake.fingerprint)

    def test_pin_mismatch_sends_nothing_not_even_the_password(self):
        with self.assertRaises(vsphere.VSphereError) as cm:
            with client(self.fake, tls_mode="pin", pinned_sha256="ab" * 32):
                pass
        self.assertEqual(cm.exception.kind, "pin")
        self.assertEqual(self.fake.requests, [])
        self.assertEqual(self.fake.auth_headers, [])

    def test_pin_mode_needs_a_pin(self):
        with self.assertRaises(vsphere.VSphereError) as cm:
            client(self.fake, tls_mode="pin", pinned_sha256="")
        self.assertEqual(cm.exception.kind, "pin")

    def test_fetch_fingerprint_is_a_handshake_only(self):
        self.assertEqual(vsphere.fetch_fingerprint(self.fake.host, self.fake.port), self.fake.fingerprint)
        self.assertEqual(self.fake.requests, [])
        self.assertEqual(vsphere.format_fingerprint(self.fake.fingerprint).count(":"), 31)
        self.assertEqual(vsphere.normalise_fingerprint("sha256:" + vsphere.format_fingerprint(self.fake.fingerprint)), self.fake.fingerprint)
        self.assertEqual(vsphere.normalise_fingerprint("zz"), "")

    # ---- hostile servers
    def test_redirect_is_an_error_and_never_followed(self):
        self.fake.redirect_prefix = "/api/vcenter/host"
        with client(self.fake) as c:
            with self.assertRaises(vsphere.VSphereError) as cm:
                c.get("/api/vcenter/host")
        self.assertEqual(cm.exception.kind, "http")
        self.assertIn("redirect", str(cm.exception))
        self.assertEqual([p for _m, p, _s in self.fake.requests].count("/elsewhere"), 0)

    def test_oversized_body_is_refused(self):
        self.fake.oversize_prefix = "/api/vcenter/host"
        with client(self.fake) as c:
            with self.assertRaises(vsphere.VSphereError) as cm:
                c.get("/api/vcenter/host")
        self.assertEqual(cm.exception.kind, "limit")

    def test_malformed_json(self):
        self.fake.malformed_prefix = "/api/vcenter/host"
        with client(self.fake) as c:
            with self.assertRaises(vsphere.VSphereError) as cm:
                c.get("/api/vcenter/host")
        self.assertEqual(cm.exception.kind, "protocol")

    def test_slow_server_hits_the_deadline(self):
        self.fake.slow_prefix = "/api/vcenter/host"
        self.fake.slow_seconds = 8
        started = time.monotonic()
        with client(self.fake, deadline=2) as c:
            with self.assertRaises(vsphere.VSphereError) as cm:
                c.get("/api/vcenter/host")
        self.assertEqual(cm.exception.kind, "timeout")
        self.assertLess(time.monotonic() - started, 6)

    def test_connection_dropped_mid_run(self):
        self.fake.drop_prefix = "/api/vcenter/host"
        with client(self.fake) as c:
            with self.assertRaises(vsphere.VSphereError):
                c.get("/api/vcenter/host")

    def test_session_ended_by_the_server(self):
        self.fake.expire_after = 2
        with client(self.fake) as c:
            c.get("/api/vcenter/host")
            c.get("/api/vcenter/host")
            with self.assertRaises(vsphere.VSphereError) as cm:
                c.get("/api/vcenter/host")
        self.assertEqual(cm.exception.kind, "auth")

    def test_optional_endpoint_missing_is_none(self):
        with client(self.fake) as c:
            self.assertIsNone(c.get("/api/vcenter/vm/vm-999/tools", optional=True))
            with self.assertRaises(vsphere.VSphereError):
                c.get("/api/vcenter/vm/vm-999/tools")

    def test_unexpected_list_shape(self):
        with client(self.fake) as c:
            with self.assertRaises(vsphere.VSphereError):
                c.get_list("/api/appliance/system/version")

    def test_connection_refused(self):
        port = self.fake.port
        self.fake.stop()
        try:
            with self.assertRaises(vsphere.VSphereError) as cm:
                with vsphere.VSphereClient("127.0.0.1", port, USER, PASSWORD, tls_mode="skip"):
                    pass
            self.assertEqual(cm.exception.kind, "network")
        finally:
            self.__class__.fake = FakeVCenter().start()

    # ---- inventory limits
    def test_detail_budget_is_respected_and_reported_partial(self):
        with client(self.fake) as c:
            inv = vsphere.collect_inventory(c, lambda s: [x["moref"] for x in s], max_detail=2)
        self.assertEqual(len(inv["details"]), 2)

    def test_placement_comes_from_filtered_lists(self):
        with client(self.fake) as c:
            inv = vsphere.collect_inventory(c, ())
        by = {v["moref"]: v for v in inv["vms"]}
        self.assertEqual(by["vm-101"]["host_moref"], "host-11")
        self.assertEqual(by["vm-101"]["cluster_moref"], "domain-c8")
        self.assertEqual(by["vm-103"]["datastore_morefs"], ["datastore-22"])
        self.assertEqual({h["moref"]: h["cluster_name"] for h in inv["hosts"]}, {"host-11": "Compute-01", "host-12": "Compute-01"})

    def test_a_powered_off_vm_gets_no_guest_calls(self):
        with client(self.fake) as c:
            vsphere.collect_inventory(c, ["vm-103"])
        paths = [p for _m, p, _s in self.fake.requests]
        self.assertIn("/api/vcenter/vm/vm-103", paths)
        self.assertNotIn("/api/vcenter/vm/vm-103/tools", paths)


class PolicyTests(HvCase):
    @override_settings(PROBE_ALLOWED_PRIVATE_CIDRS=[])
    def test_loopback_is_refused_by_the_probe_policy(self):
        with self.assertRaises(vsphere.VSphereError) as cm:
            vsphere.fetch_fingerprint("127.0.0.1", 443)
        self.assertEqual(cm.exception.kind, "policy")
        self.assertIn("LINKEDEYE_PROBE_ALLOWED_PRIVATE_CIDRS", str(cm.exception))

    @override_settings(PROBE_ALLOWED_PRIVATE_CIDRS=[])
    def test_metadata_address_always_refused(self):
        with self.assertRaises(vsphere.VSphereError):
            vsphere.fetch_fingerprint("169.254.169.254", 443)
