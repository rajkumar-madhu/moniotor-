"""Client / secret / protocol security review of the Redfish integration: the extra attacks tried against the code.

Covers: paths that end in a newline, links inside BMC documents that point elsewhere, hostile documents (duplicate ids, deep
nesting, giant numbers, markup), the SSRF policy for IPv6 / link-local / metadata addresses, DNS rebinding between the check and
the connect, a login that is rejected over and over (the BMC account must not be locked), the stored login never following an
edited address, Basic auth never over plain HTTP without the override, rendered pages and logs during failed logins, CSRF on
every POST, open redirects, the Test / Refresh rate limits and the request workflow.
"""
import base64
import socket
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.test import Client, SimpleTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import AuditEvent, Event
from hardware import collector, parsers, redfish, services
from hardware.models import BmcEndpoint, ControlRequest, HwComponent
from hardware.tests import fixtures
from hardware.tests.base import BMC_PASS, BMC_USER, HwCase, loopback, snapshot_of, with_key
from hardware.tests.fake_redfish import FakeRedfish
from onboarding.models import Credential

EVERYTHING = override_settings(PROBE_ALLOWED_PRIVATE_CIDRS=["0.0.0.0/0", "::/0"])


class Strictness(SimpleTestCase):
    def test_a_path_that_ends_in_a_newline_is_not_a_path(self):
        for p in ("/redfish/v1/Systems\n", "/redfish/v1/Systems/1?$top=5\n", "/redfish/v1/\r\nHost: evil", "/redfish/v1/a b",
                  "/redfish/v1/%2e%2e/x", "/redfish/v1/a%2f..%2fb", "/redfish/v1/x\x00", "/redfish/v1/‮X", "http://evil/redfish/v1/"):
            self.assertFalse(redfish.valid_path(p), repr(p))
        for p in ("/redfish/v1/", "/redfish/v1/Systems/System.Embedded.1", "/redfish/v1/Managers/1/LogServices/IML/Entries?$top=50"):
            self.assertTrue(redfish.valid_path(p), p)

    def test_links_inside_a_bmc_document_that_leave_the_bmc_are_never_requested(self):
        docs = fixtures.dell_idrac9()
        evil = ["http://169.254.169.254/latest/meta-data/", "//evil.example/redfish/v1/", "/redfish/v1/../../etc/passwd",
                "https://evil.example/redfish/v1/Systems/1", "/etc/passwd", "/redfish/v1/x\n", 5, {"a": 1}, None]
        root = docs["/redfish/v1"]
        root["Systems"] = {"@odata.id": evil[0]}
        root["Chassis"] = {"@odata.id": evil[1]}
        root["Managers"] = {"@odata.id": evil[2]}
        docs["/redfish/v1/Systems"]["Members"] = [{"@odata.id": e} for e in evil] + [{"@odata.id": "/redfish/v1/Systems/System.Embedded.1"}]
        c = fixtures.FixtureClient(docs)
        parsers.parse(collector.collect(c))
        self.assertTrue(all(redfish.valid_path(p) for p in c.log), c.log)
        self.assertFalse([p for p in c.log if "evil" in p or "169.254" in p or "passwd" in p or ".." in p])

    def test_a_bmc_that_lists_the_same_id_twice_does_not_make_the_poll_unstorable(self):
        docs = fixtures.dell_idrac9()
        thermal_path = "/redfish/v1/Chassis/System.Embedded.1/Thermal"
        t = docs[thermal_path]
        t["Fans"] = t["Fans"] + t["Fans"]
        t["Temperatures"] = t["Temperatures"] + t["Temperatures"]
        snap = snapshot_of(docs)
        keys = [(c.kind, c.key) for c in snap.comps]
        self.assertEqual(len(keys), len(set(keys)))

    def test_the_same_battery_key_from_power_and_storage_stays_distinct(self):
        c = collector.collect(fixtures.FixtureClient(fixtures.dell_idrac9()))
        snap = parsers.parse(c)
        import dataclasses
        twin = dataclasses.replace(snap.comps[0])
        keys = [(x.kind, x.key) for x in parsers._distinct(list(snap.comps) + [twin])]
        self.assertEqual(len(keys), len(set(keys)))

    def test_hostile_numbers_and_text_become_none_or_clean_text(self):
        for bad in (float("nan"), float("inf"), 10 ** 400, "9" * 500, True, [1], {"a": 1}, "NaN", "1e999"):
            self.assertIsNone(parsers.num(bad), repr(bad)[:40])
        out = parsers.text("\x1b[31mred\x1b[0m ‮evil‬\r\n<b>x</b>" + "A" * 5000, 120)
        self.assertNotIn("\x1b", out)
        self.assertNotIn("‮", out)
        self.assertNotIn("\n", out)
        self.assertLessEqual(len(out), 120)


class Policy(SimpleTestCase):
    @override_settings(PROBE_ALLOWED_PRIVATE_CIDRS=[])
    def test_private_loopback_and_cgnat_bmc_hosts_are_refused_by_default(self):
        for host in ("127.0.0.1", "10.1.2.3", "192.168.0.9", "::1", "fd00::1", "100.100.100.200", "::ffff:10.0.0.1"):
            with self.assertRaises(redfish.Unreachable, msg=host) as cm:
                redfish.resolve(host, 443)
            self.assertIn("LINKEDEYE_PROBE_ALLOWED_PRIVATE_CIDRS", str(cm.exception))

    @EVERYTHING
    def test_link_local_metadata_unspecified_and_multicast_are_refused_even_with_everything_allowed(self):
        for host in ("169.254.169.254", "fe80::1", "::ffff:169.254.169.254", "64:ff9b::a9fe:a9fe", "0.0.0.0", "::", "224.0.0.1",
                     "ff02::1", "::169.254.169.254", "255.255.255.255"):
            with self.assertRaises(redfish.Unreachable, msg=host):
                redfish.resolve(host, 443)
            with self.assertRaises(redfish.Unreachable, msg=host):
                redfish.inspect_certificate(host, 443)
            with redfish.make_client(host, 443, "a", "b", "skip") as c, self.assertRaises(redfish.Unreachable, msg=host):
                c.get("/redfish/v1/")


@EVERYTHING
class Rebinding(SimpleTestCase):
    def test_a_host_name_is_resolved_once_and_the_connection_goes_to_that_address(self):
        real, answers = socket.getaddrinfo, []

        def gai(host, port, *a, **k):
            if host == "rebind.example.test":
                answers.append(host)
                ip = "127.0.0.1" if len(answers) == 1 else "169.254.169.254"
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]
            return real(host, port, *a, **k)

        with FakeRedfish(fixtures.dell_idrac9()) as srv, mock.patch("socket.getaddrinfo", gai):
            with redfish.make_client("rebind.example.test", srv.port, BMC_USER, BMC_PASS, "pin", srv.fingerprint) as c:
                c.get("/redfish/v1/")
                c._drop()                                                    # force a reconnect: still no second lookup
                c.get("/redfish/v1/Systems")
            self.assertEqual(len(answers), 1)
            self.assertEqual(len(srv.requests), 2)


@EVERYTHING
class CertificateChange(SimpleTestCase):
    def test_a_certificate_that_stops_matching_the_pin_is_refused_before_the_login_is_sent(self):
        with FakeRedfish(fixtures.dell_idrac9()) as srv:
            with redfish.make_client("127.0.0.1", srv.port, BMC_USER, BMC_PASS, "pin", srv.fingerprint) as c:
                c.get("/redfish/v1/")                                          # pinned and working
            before = len(srv.requests)
            changed = srv.fingerprint[:-1] + ("0" if srv.fingerprint[-1] != "0" else "1")     # what a replaced certificate looks like
            for _ in range(2):                                                 # and it stays refused on every later poll
                with redfish.make_client("127.0.0.1", srv.port, BMC_USER, BMC_PASS, "pin", changed) as c, \
                        self.assertRaises(redfish.CertificateError) as cm:
                    c.get("/redfish/v1/")
                self.assertEqual(cm.exception.fingerprint, srv.fingerprint)
            self.assertEqual(len(srv.requests), before)                        # nothing, and so no password, went out

    def test_basic_auth_is_not_sent_over_plain_http_unless_the_override_is_set(self):
        with FakeRedfish(fixtures.dell_idrac9(), tls=False) as srv:
            with redfish.make_client("127.0.0.1", srv.port, BMC_USER, BMC_PASS, "pin", "a" * 64) as c, self.assertRaises(redfish.RedfishError):
                c.get("/redfish/v1/")                                          # tries TLS against a plain server
            self.assertEqual([a for _m, _p, a in srv.requests if a], [])
        endpoint = BmcEndpoint(host="127.0.0.1", port=80)
        self.assertEqual(endpoint.scheme, "https")


@with_key
class Lockout(HwCase):
    """A wrong password must not be tried again and again: a BMC locks the account after a few failures."""

    def setUp(self):
        super().setUp()
        self.ep = self.make_endpoint(self.make_device("srv-lock", "Mumbai", ip="198.51.100.31"), host="127.0.0.1")
        self.store_login(self.ep, password="not-the-right-one")
        self.ep.refresh_from_db()

    @loopback
    def test_polling_stops_after_three_rejected_logins(self):
        with FakeRedfish(fixtures.dell_idrac9()) as srv:
            BmcEndpoint.objects.filter(pk=self.ep.pk).update(port=srv.port, pinned_sha256=srv.fingerprint)
            t = timezone.now()
            for i in range(5):
                BmcEndpoint.objects.filter(pk=self.ep.pk).update(next_poll_at=t)
                services.poll_due(t)
            self.assertEqual(len([1 for _m, _p, a in srv.requests if a]), 3)    # three logins, then nothing more
        self.ep.refresh_from_db()
        self.assertTrue(self.ep.auth_paused)

    @loopback
    def test_a_rejected_login_test_counts_and_the_next_one_has_to_wait_a_minute(self):
        c = self.login(self.mum_admin)
        url = reverse("hardware:endpoint_test", args=[self.ep.pk])
        with FakeRedfish(fixtures.dell_idrac9()) as srv:
            BmcEndpoint.objects.filter(pk=self.ep.pk).update(port=srv.port, pinned_sha256=srv.fingerprint)
            c.post(url)
            self.ep.refresh_from_db()
            self.assertEqual(self.ep.auth_failures, 1)
            logins = len([1 for _m, _p, a in srv.requests if a])
            self.assertEqual(logins, 1)
            for forget_the_cache in (False, True):                              # the second time as another worker process
                if forget_the_cache:
                    cache.clear()
                r = c.post(url)
                self.assertContains(r, "cannot lock the BMC account")
            self.assertEqual(len([1 for _m, _p, a in srv.requests if a]), logins)
            with mock.patch.object(services, "TEST_GAP", 0):
                cache.clear()
                c.post(url)
            self.assertEqual(len([1 for _m, _p, a in srv.requests if a]), logins + 1)
        self.ep.refresh_from_db()
        self.assertEqual(self.ep.auth_failures, 2)

    @loopback
    def test_the_login_test_is_rate_limited_per_endpoint(self):
        self.store_login(self.ep)                                              # the right password this time
        c = self.login(self.mum_admin)
        url = reverse("hardware:endpoint_test", args=[self.ep.pk])
        with FakeRedfish(fixtures.dell_idrac9()) as srv:
            BmcEndpoint.objects.filter(pk=self.ep.pk).update(port=srv.port, pinned_sha256=srv.fingerprint)
            c.post(url)
            logins = len([1 for _m, _p, a in srv.requests if a])
            self.assertGreater(logins, 0)
            r = c.post(url)
            self.assertContains(r, "just ran")
            cache.clear()                                                      # another worker process: the audit trail still blocks
            r = c.post(url)
            self.assertContains(r, "just ran")
            self.assertEqual(len([1 for _m, _p, a in srv.requests if a]), logins)
            # looking at the certificate sends no login, so it is never throttled
            BmcEndpoint.objects.filter(pk=self.ep.pk).update(pinned_sha256="")
            cache.clear()
            r = c.post(url)
            self.assertContains(r, "Pin this certificate")

    def test_refresh_is_one_atomic_update(self):
        now = timezone.now()
        self.ep.refresh_from_db()
        self.assertEqual(services.request_refresh(self.ep, now)[0], True)
        stale = BmcEndpoint.objects.get(pk=self.ep.pk)
        stale.refresh_requested_at = None                                      # a second request that read the row before the first wrote
        self.assertEqual(services.request_refresh(stale, now)[0], False)


@with_key
class StoredLogin(HwCase):
    FORM = {"host": "192.0.2.50", "port": "443", "verify_mode": "pin", "poll_interval_s": "60", "enabled": "on", "vendor_override": "",
            "username": BMC_USER}

    def setUp(self):
        super().setUp()
        self.store_login(self.ep_m)
        self.ep_m.refresh_from_db()
        self.url = reverse("hardware:endpoint_edit", args=[self.ep_m.pk])

    def post(self, **kw):
        return self.login(self.mum_admin).post(self.url, {**self.FORM, **kw})

    def test_the_stored_login_never_follows_a_changed_address_without_the_password(self):
        for change in ({"host": "attacker.example.test"}, {"port": "8443"}, {"insecure_http": "on", "confirm_unsafe": "on", "port": "80"}):
            r = self.post(**change)
            self.assertEqual(r.status_code, 200, change)                      # the form is shown again with an error
            self.assertContains(r, "enter the password again")
            self.ep_m.refresh_from_db()
            self.assertEqual((self.ep_m.host, self.ep_m.port, self.ep_m.insecure_http), ("192.0.2.50", 443, False), change)

    def test_with_a_new_password_the_address_may_change_and_the_old_pin_goes(self):
        BmcEndpoint.objects.filter(pk=self.ep_m.pk).update(pinned_sha256="c" * 64)
        r = self.post(host="new-bmc.example.test", password="brand-new-pass-1")
        self.assertEqual(r.status_code, 302)
        self.ep_m.refresh_from_db()
        self.assertEqual((self.ep_m.host, self.ep_m.pinned_sha256), ("new-bmc.example.test", ""))
        self.assertEqual(services.login_of(self.ep_m), (BMC_USER, "brand-new-pass-1"))

    def test_an_unchanged_address_keeps_the_login_without_retyping(self):
        r = self.post(poll_interval_s="120")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(services.login_of(BmcEndpoint.objects.get(pk=self.ep_m.pk)), (BMC_USER, BMC_PASS))


@with_key
class Leaks(HwCase):
    SENTINEL = "Zq9-sentinel-Pass-77"

    @loopback
    def test_nothing_secret_in_logs_pages_json_csv_audit_events_or_the_row(self):
        ep = self.make_endpoint(self.make_device("srv-leak", "Mumbai", ip="198.51.100.32"), host="127.0.0.1")
        self.store_login(ep, password=self.SENTINEL)
        b64 = base64.b64encode(f"{BMC_USER}:{self.SENTINEL}".encode()).decode()
        c = self.login(self.mum_admin)
        pages, records = [], []
        import logging

        class Grab(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage() + " " + str(record.exc_info) + " " + str(getattr(record, "args", "")))

        root, handler, old = logging.getLogger(), Grab(level=0), logging.getLogger().level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        try:
            with FakeRedfish(fixtures.dell_idrac9()) as srv:
                BmcEndpoint.objects.filter(pk=ep.pk).update(port=srv.port, pinned_sha256=srv.fingerprint)
                ep.refresh_from_db()
                with mock.patch.object(services, "TEST_GAP", 0):
                    pages.append(c.post(reverse("hardware:endpoint_test", args=[ep.pk])).content.decode())
                services.poll_endpoint(ep)
                ep.refresh_from_db()
                self.assertEqual(ep.conn_state, "auth_failed")
        finally:
            root.removeHandler(handler)
            root.setLevel(old)
        for name, args in (("endpoints", ()), ("endpoint_edit", (ep.pk,)), ("live", (ep.device_id,)), ("export", (ep.device_id,)),
                           ("inventory", (ep.device_id,)), ("events", (ep.device_id,)), ("requests", ()), ("alerts", (ep.device_id,))):
            pages.append(c.get(reverse(f"hardware:{name}", args=args)).content.decode())
        pages.append(c.get(f"/network/devices/{ep.device_id}/?tab=hardware").content.decode())
        blob = "\n".join([*records, *pages, ep.last_error, repr(ep.__dict__), repr(ep.overview),
                          " ".join(str(a.details) + a.object_repr for a in AuditEvent.objects.all()),
                          " ".join(f"{e.title}{e.message}{e.data}" for e in Event.objects.all())])
        for needle in (self.SENTINEL, b64, f"Basic {b64}"):
            self.assertNotIn(needle, blob)


@with_key
class HostileText(HwCase):
    EVIL = "<script>alert(1)</script>"

    def test_bmc_text_is_escaped_on_every_page_and_the_csv_is_defused(self):
        docs = fixtures.dell_idrac9()
        t = docs["/redfish/v1/Chassis/System.Embedded.1/Thermal"]
        t["Temperatures"][0]["Name"] = self.EVIL
        t["Fans"][0]["Name"] = "=HYPERLINK(\"http://evil.example\")"
        docs["/redfish/v1/Managers/iDRAC.Embedded.1"]["Links"] = {"OEM": {"href": "javascript:alert(1)"}}
        self.store_login(self.ep_m)
        self.applied(self.ep_m, docs=docs)
        HwComponent.objects.filter(endpoint=self.ep_m, kind="fan").update(name="=cmd|' /C calc'!A0")
        c = self.login(self.mum_admin)
        live = c.get(reverse("hardware:live", args=[self.srv_m.pk]))
        # JSON is never rendered as HTML by a browser (and nosniff is on); the page script only uses textContent
        self.assertTrue(live["Content-Type"].startswith("application/json"))
        self.assertEqual(live["X-Content-Type-Options"], "nosniff")
        pages = []
        for name in ("inventory", "events", "alerts"):
            pages.append(c.get(reverse(f"hardware:{name}", args=[self.srv_m.pk])).content.decode())
        pages.append(c.get(f"/network/devices/{self.srv_m.pk}/?tab=hardware").content.decode())
        blob = "\n".join(pages)
        self.assertNotIn(self.EVIL, blob)
        self.assertNotIn('href="javascript:', blob)
        csv = c.get(reverse("hardware:export", args=[self.srv_m.pk])).content.decode()
        self.assertNotIn("\n=", csv)
        self.assertIn("'=cmd", csv)


@with_key
class Requests(HwCase):
    def setUp(self):
        super().setUp()
        self.store_login(self.ep_m)
        self.req = services.create_request(self.ep_m, self.mum_op, "force_off", "the host is wedged")

    def decide(self, user, decision="approve", **kw):
        return self.login(user).post(reverse("hardware:request_decide", args=[self.req.pk]), {"decision": decision, **kw})

    def test_approval_sends_nothing_to_anything(self):
        with mock.patch("socket.create_connection", side_effect=AssertionError("outbound connection")), \
                mock.patch("http.client.HTTPConnection.request", side_effect=AssertionError("outbound request")), \
                mock.patch("http.client.HTTPSConnection.request", side_effect=AssertionError("outbound request")):
            r = self.decide(self.mum_admin)
        self.assertEqual(r.status_code, 302)
        self.req.refresh_from_db()
        self.assertEqual(self.req.state, "approved")

    def test_replay_cannot_decide_twice(self):
        self.decide(self.mum_admin)
        self.decide(self.root, decision="reject")
        self.decide(self.mum_admin)
        self.req.refresh_from_db()
        self.assertEqual((self.req.state, self.req.decided_by_id), ("approved", self.mum_admin.pk))
        self.assertEqual(AuditEvent.objects.filter(action__in=("hardware.request.approve", "hardware.request.reject")).count(), 1)

    def test_an_expired_request_cannot_be_approved(self):
        ControlRequest.objects.filter(pk=self.req.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
        self.decide(self.mum_admin)
        self.req.refresh_from_db()
        self.assertEqual(self.req.state, "expired")

    def test_the_permission_matrix_and_idor(self):
        for user in (self.mum_view, self.mum_op):
            r = self.decide(user)
            self.assertIn(r.status_code, (302, 403))
        self.assertEqual(self.decide(self.che_admin).status_code, 404)
        self.req.refresh_from_db()
        self.assertEqual(self.req.state, "pending")
        url = reverse("hardware:request_new", args=[self.srv_m.pk])
        before = ControlRequest.objects.count()
        self.login(self.mum_view).post(url, {"action": "power_on", "reason": "please do it now"})
        self.assertEqual(self.login(self.che_admin).post(url, {"action": "power_on", "reason": "please do it now"}).status_code, 404)
        self.assertEqual(ControlRequest.objects.count(), before)

    def test_next_can_only_be_a_path_inside_the_hardware_pages(self):
        for nxt in ("//evil.example", "https://evil.example/x", "/\\evil.example", "javascript:alert(1)", "/hardware.evil.example/",
                    "///evil.example", "\\\\evil.example"):
            ControlRequest.objects.filter(pk=self.req.pk).update(state="pending", decided_by=None)
            r = self.decide(self.mum_admin, next=nxt)
            self.assertEqual(r["Location"], reverse("hardware:requests"), nxt)

    def test_only_listed_actions_are_accepted(self):
        for action in ("power_on", "graceful_shutdown", "force_off", "update_firmware"):
            self.assertIn(action, ControlRequest.ACTION_LABELS)
        for bad in ("delete", "format", "__class__", "", "POWER_ON"):
            with self.assertRaises(ValueError):
                services.create_request(self.ep_m, self.mum_op, bad, "some good reason")


@with_key
class Csrf(HwCase):
    def test_every_post_endpoint_refuses_a_request_without_the_token(self):
        self.store_login(self.ep_m)
        req = services.create_request(self.ep_m, self.mum_op, "power_on", "boot it please")
        c = Client(enforce_csrf_checks=True)
        self.assertTrue(c.login(username="root", password="hw-test-pass-123"))
        posts = [("refresh", (self.srv_m.pk,)), ("vendor", (self.srv_m.pk,)), ("alerts", (self.srv_m.pk,)), ("request_new", (self.srv_m.pk,)),
                 ("request_decide", (req.pk,)), ("endpoint_new", ()), ("endpoint_edit", (self.ep_m.pk,)), ("endpoint_test", (self.ep_m.pk,)),
                 ("endpoint_pin", (self.ep_m.pk,)), ("endpoint_toggle", (self.ep_m.pk,)), ("endpoint_delete", (self.ep_m.pk,))]
        for name, args in posts:
            r = c.post(reverse(f"hardware:{name}", args=args), {"decision": "approve", "vendor": "hpe", "action": "power_on"})
            self.assertEqual(r.status_code, 403, name)
        self.ep_m.refresh_from_db()
        req.refresh_from_db()
        self.assertEqual((self.ep_m.enabled, self.ep_m.vendor_override, req.state), (True, "", "pending"))
        self.assertTrue(BmcEndpoint.objects.filter(pk=self.ep_m.pk).exists())
        self.assertTrue(Credential.objects.exists())


@with_key
class Cascade(HwCase):
    def test_deleting_the_server_by_any_route_removes_the_vault_row(self):
        self.store_login(self.ep_m)
        cred = self.ep_m.credential_id
        self.assertTrue(Credential.objects.filter(pk=cred).exists())
        self.srv_m.delete()                                                    # the device goes, the endpoint cascades
        self.assertFalse(BmcEndpoint.objects.filter(pk=self.ep_m.pk).exists())
        self.assertFalse(Credential.objects.filter(pk=cred).exists())
