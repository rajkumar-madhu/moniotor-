"""Client / secret / protocol security review of the vCenter integration: the extra attacks tried against the code.

Covers: ids that end in a newline, absurdly nested JSON, the SSRF policy for IPv6 / link-local / metadata addresses, DNS
rebinding between the check and the connect, a login that is rejected over and over (the account must not be locked), the stored
login never following an edited address, rendered pages and logs during failed logins, hostile text in pages, CSRF on every POST,
open redirects, the connection-test rate limit and the request workflow (replay, expiry, no outbound traffic on approval).
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
from hypervisor import parsers, services, sync, vsphere
from hypervisor.models import VCenterSource, VmControlRequest, VmInfo
from hypervisor.tests.base import HvCase, capture_logs
from hypervisor.tests.fake_vcenter import PASSWORD as VC_PASSWORD, USER as VC_USER, FakeVCenter, default_inventory
from onboarding.models import Credential

EVERYTHING = override_settings(PROBE_ALLOWED_PRIVATE_CIDRS=["0.0.0.0/0", "::/0"])


class Strictness(SimpleTestCase):
    def test_an_id_that_ends_in_a_newline_is_not_an_id(self):
        # `$` matches before a trailing newline; such a "moref" would be put into a request path
        self.assertEqual(parsers.moref("vm-101\n"), "")
        self.assertEqual(parsers.moref("vm-101"), "vm-101")
        self.assertIsNone(vsphere.SESSION_RE.fullmatch("abcdefghijkl\n"))
        self.assertIsNone(parsers.parse_vm_summary({"vm": "vm-1\n", "name": "x"}))

    def test_deeply_nested_json_is_a_protocol_error_not_a_crash(self):
        with self.assertRaises(vsphere.VSphereError) as cm:
            vsphere.VSphereClient._json(b"[" * 200000)
        self.assertEqual(cm.exception.kind, "protocol")

    def test_hostile_values_in_every_parser_end_up_as_none_or_clean_text(self):
        nasty = "\x1b[31mred\x1b[0m ‮evil‬\r\n<b>x</b>" + "A" * 5000
        vm = parsers.parse_vm_summary({"vm": "vm-1", "name": nasty, "power_state": "<x>", "cpu_count": float("nan"),
                                       "memory_size_MiB": 10 ** 400})
        self.assertNotIn("\x1b", vm["name"])
        self.assertNotIn("‮", vm["name"])
        self.assertNotIn("\n", vm["name"])
        self.assertLessEqual(len(vm["name"]), 255)
        self.assertEqual((vm["power_state"], vm["cpu_count"], vm["memory_mib"]), ("unknown", None, None))
        ds = parsers.parse_datastore({"datastore": "datastore-1", "name": "d", "capacity": float("inf"), "free_space": -5})
        self.assertEqual((ds["capacity_bytes"], ds["free_bytes"]), (None, None))
        for junk in (None, 5, "x", [], [1, 2], {"vm": ["vm-1"]}, {"vm": {"a": 1}}):
            self.assertIsNone(parsers.parse_vm_summary(junk))
            self.assertIsNone(parsers.parse_host(junk))
            self.assertIsNone(parsers.parse_datastore(junk))
        detail = parsers.parse_vm_detail({"disks": {str(i): {"capacity": 1} for i in range(500)}, "nics": [1, 2, 3],
                                          "identity": "nope", "cpu": [], "memory": 5})
        self.assertLessEqual(len(detail["disks"]), parsers.MAX_DISKS)
        self.assertEqual(detail["nics"], [])


class Policy(SimpleTestCase):
    @override_settings(PROBE_ALLOWED_PRIVATE_CIDRS=[])
    def test_private_loopback_and_cgnat_hosts_are_refused_by_default(self):
        for host in ("127.0.0.1", "10.1.2.3", "192.168.0.9", "::1", "fd00::1", "100.100.100.200", "::ffff:10.0.0.1"):
            with self.assertRaises(vsphere.VSphereError, msg=host) as cm:
                vsphere._resolve(host, 443)
            self.assertEqual(cm.exception.kind, "policy", host)
            self.assertIn("LINKEDEYE_PROBE_ALLOWED_PRIVATE_CIDRS", str(cm.exception))

    @EVERYTHING
    def test_link_local_metadata_unspecified_and_multicast_are_refused_even_with_everything_allowed(self):
        for host in ("169.254.169.254", "fe80::1", "::ffff:169.254.169.254", "64:ff9b::a9fe:a9fe", "0.0.0.0", "::", "224.0.0.1",
                     "ff02::1", "::169.254.169.254", "255.255.255.255"):
            with self.assertRaises(vsphere.VSphereError, msg=host) as cm:
                vsphere._resolve(host, 443)
            self.assertEqual(cm.exception.kind, "policy", host)
            with self.assertRaises(vsphere.VSphereError, msg=host):
                vsphere.fetch_fingerprint(host, 443)

    def test_the_client_is_https_only(self):
        src = VCenterSource(host="vc.example.test", port=8443)
        self.assertTrue(src.base_url.startswith("https://"))
        self.assertFalse(hasattr(vsphere, "_PlainConn"))


@EVERYTHING
class Rebinding(SimpleTestCase):
    def test_a_host_name_is_resolved_once_and_the_connection_goes_to_that_address(self):
        fake = FakeVCenter().start()
        self.addCleanup(fake.stop)
        real, answers = socket.getaddrinfo, []

        def gai(host, port, *a, **k):
            if host == "rebind.example.test":
                answers.append(host)
                ip = "127.0.0.1" if len(answers) == 1 else "169.254.169.254"          # the second answer would be the metadata service
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]
            return real(host, port, *a, **k)

        with mock.patch("socket.getaddrinfo", gai):
            with vsphere.VSphereClient("rebind.example.test", fake.port, VC_USER, VC_PASSWORD, tls_mode="skip") as c:
                c.get("/api/vcenter/host")
                c._close()                                                     # force a reconnect: still no second lookup
                c.get("/api/vcenter/datastore")
        self.assertEqual(len(answers), 1)
        self.assertGreaterEqual(len(fake.requests), 4)


class Lockout(HvCase):
    """A wrong password must not be tried again and again: vCenter locks the account after a few failures."""

    def setUp(self):
        super().setUp()
        self.fake = FakeVCenter().start()
        self.addCleanup(self.fake.stop)
        self.src = self.make_source(self.fake)
        self.fake.fail_login = True

    def test_sync_stops_after_three_rejected_logins_even_for_sync_now(self):
        t = timezone.now()
        for i in range(3):
            self.run_sync(self.src, now=t + timedelta(minutes=30 * i))
        self.src.refresh_from_db()
        self.assertTrue(services.auth_paused(self.src))
        logins = self.fake.logins + len(self.fake.auth_headers)
        self.src.sync_requested_at = t + timedelta(hours=3)
        self.assertFalse(sync.due(self.src, t + timedelta(days=2)))
        self.assertEqual(sync.sync_due(t + timedelta(days=2)), "nothing due")
        self.assertEqual(self.fake.logins + len(self.fake.auth_headers), logins)

    def test_a_new_password_resumes_it(self):
        t = timezone.now()
        for i in range(3):
            self.run_sync(self.src, now=t + timedelta(minutes=30 * i))
        c = self.login(self.mum_admin)
        self.fake.fail_login = False
        c.post(reverse("hypervisor:source_rotate", args=[self.src.pk]), {"username": VC_USER, "password": VC_PASSWORD})
        self.src.refresh_from_db()
        self.assertFalse(services.auth_paused(self.src))
        self.assertTrue(sync.due(self.src, t + timedelta(days=2)))

    def test_a_missing_login_is_not_a_rejected_login(self):
        Credential.objects.all().delete()
        for _ in range(4):
            self.run_sync(self.src)
        self.src.refresh_from_db()
        self.assertFalse(services.auth_paused(self.src))                      # nothing was rejected: fixing the vault resumes it

    def test_a_rejected_login_test_counts_and_the_next_one_has_to_wait_a_minute(self):
        c = self.login(self.mum_admin)
        url = reverse("hypervisor:source_test", args=[self.src.pk])
        c.post(url)
        self.src.refresh_from_db()
        self.assertEqual((self.src.consecutive_failures, self.src.error_kind), (1, "auth"))
        sent = len(self.fake.auth_headers)
        for forget_the_cache in (False, True):                                  # the second time as another worker process
            if forget_the_cache:
                cache.clear()
            r = c.post(url, follow=True)
            self.assertContains(r, "cannot lock the account")
        self.assertEqual(len(self.fake.auth_headers), sent)                     # no second login went out
        with mock.patch.object(services, "TEST_MIN_GAP", 0):
            cache.clear()
            c.post(url)
        self.assertEqual(len(self.fake.auth_headers), sent + 1)                 # with the gap over, it may try again

    def test_the_connection_test_is_rate_limited(self):
        self.fake.fail_login = False
        c = self.login(self.mum_admin)
        url = reverse("hypervisor:source_test", args=[self.src.pk])
        c.post(url)
        n = len(self.fake.requests)
        r = c.post(url, follow=True)
        self.assertContains(r, "just ran")
        self.assertEqual(len([x for x in self.fake.requests if x[0] == "POST"]), 1)           # no second login
        # another worker process has no cache entry of ours: the audit trail still blocks it
        cache.clear()
        r = c.post(url, follow=True)
        self.assertContains(r, "just ran")
        self.assertEqual(len([x for x in self.fake.requests if x[0] == "POST"]), 1)
        self.assertGreaterEqual(len(self.fake.requests), n)


class StoredLogin(HvCase):
    def setUp(self):
        super().setUp()
        self.src = self.make_source(None, tls_mode="verify")
        self.src.port = 443
        self.src.save()
        self.edit = reverse("hypervisor:source_edit", args=[self.src.pk])

    def fields(self, **kw):
        base = {"name": "vc-mumbai", "site": "Mumbai", "host": "127.0.0.1", "port": 443, "username": VC_USER, "tls_mode": "verify",
                "sync_interval_s": 300, "enabled": "on"}
        base.update(kw)
        return base

    def test_changing_the_address_removes_the_stored_login_even_if_a_password_field_is_posted(self):
        cred = self.src.credential_id
        c = self.login(self.mum_admin)
        c.post(self.edit, self.fields(host="attacker.example.test", password="whatever"))
        self.src.refresh_from_db()
        self.assertEqual(self.src.host, "attacker.example.test")
        self.assertIsNone(self.src.credential_id)
        self.assertFalse(Credential.objects.filter(pk=cred).exists())
        a = AuditEvent.objects.filter(action="hypervisor.source.update").latest("occurred_at")
        self.assertTrue(a.details["login_dropped"])
        with self.assertRaises(vsphere.VSphereError) as cm:
            services.read_login(self.src)
        self.assertIn("No login is stored", str(cm.exception))

    def test_changing_the_port_does_too_and_an_unchanged_address_keeps_it(self):
        c = self.login(self.mum_admin)
        c.post(self.edit, self.fields(description="only a note"))
        self.src.refresh_from_db()
        self.assertIsNotNone(self.src.credential_id)
        c.post(self.edit, self.fields(port=8443))
        self.src.refresh_from_db()
        self.assertIsNone(self.src.credential_id)

    def test_a_password_in_the_edit_form_is_never_used(self):
        row = Credential.objects.get(pk=self.src.credential_id)
        before = row.secret
        self.login(self.mum_admin).post(self.edit, self.fields(password="Injected-Pass-9"))
        self.assertEqual(Credential.objects.get(pk=row.pk).secret, before)


class Leaks(HvCase):
    SENTINEL = "Zq9-sentinel-Pass-77"

    def setUp(self):
        super().setUp()
        self.fake = FakeVCenter().start()
        self.addCleanup(self.fake.stop)
        self.src = self.make_source(self.fake, password=self.SENTINEL)
        self.fake.fail_login = True

    def test_nothing_secret_in_logs_pages_json_audit_events_or_the_source_row(self):
        b64 = base64.b64encode(f"{VC_USER}:{self.SENTINEL}".encode()).decode()
        c = self.login(self.mum_admin)
        pages = []
        with capture_logs() as buf:
            for i in range(3):
                self.run_sync(self.src, now=timezone.now() + timedelta(minutes=30 * i))
            cache.clear()
            pages.append(c.post(reverse("hypervisor:source_test", args=[self.src.pk]), follow=True).content.decode())
            for name, args in (("sources", ()), ("source_edit", (self.src.pk,)), ("datastores", ()), ("datastores_live", ()),
                               ("unmatched", ()), ("requests", ()), ("datastores_export", ()), ("requests_export", ())):
                pages.append(c.get(reverse(f"hypervisor:{name}", args=args)).content.decode())
        self.src.refresh_from_db()
        blob = "\n".join([buf.getvalue(), *pages, self.src.last_error, self.src.error_kind,
                          " ".join(str(a.details) + a.object_repr for a in AuditEvent.objects.all()),
                          " ".join(f"{e.title}{e.message}{e.data}" for e in Event.objects.all())])
        for needle in (self.SENTINEL, b64, f"Basic {b64}"):
            self.assertNotIn(needle, blob)
        self.assertNotIn(self.SENTINEL, repr(self.src.__dict__))
        self.assertNotIn(self.SENTINEL, Credential.objects.get(pk=self.src.credential_id).secret)

    def test_only_the_login_request_carries_the_basic_header(self):
        self.fake.fail_login = False
        self.run_sync(self.src)
        posts = [r for r in self.fake.requests if r[0] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(len(self.fake.auth_headers), 1)


class HostileText(HvCase):
    EVIL = "<script>alert(1)</script>"

    def test_text_from_vcenter_is_escaped_everywhere_and_never_becomes_a_link(self):
        fake = FakeVCenter().start()
        self.addCleanup(fake.stop)
        inv = default_inventory()
        inv["vms"]["vm-104"]["name"] = self.EVIL
        inv["vms"]["vm-104"]["hostname"] = "javascript:alert(2)"
        inv["datastores"]["datastore-22"]["name"] = "<img src=x onerror=alert(3)>"
        inv["hosts"]["host-12"]["name"] = "esx‮-evil\x1b[31m.example.test"
        fake.inv.update(inv)
        src = self.make_source(fake)
        self.run_sync(src)
        c = self.login(self.mum_admin)
        vm = VmInfo.objects.get(source=src, moref="vm-104")
        r = c.post(reverse("hypervisor:match_create", args=[vm.pk]), follow=True)
        vm.refresh_from_db()
        dev = vm.device
        pages = [r.content.decode()]
        for url in (reverse("hypervisor:unmatched") + "?state=matched", reverse("hypervisor:datastores"), reverse("hypervisor:sources")):
            pages.append(c.get(url).content.decode())
        for url in (reverse("hypervisor:datastores_live"), reverse("hypervisor:vm_live", args=[dev.pk])):
            r = c.get(url)                      # JSON is never rendered as HTML by a browser (and nosniff is on)
            self.assertTrue(r["Content-Type"].startswith("application/json"), url)
            self.assertEqual(r["X-Content-Type-Options"], "nosniff")
        for tab in ("overview", "storage", "network", "snapshots", "guest", "alerts"):
            pages.append(c.get(reverse("network:detail", args=[dev.pk]) + f"?tab={tab}").content.decode())
        blob = "\n".join(pages)
        self.assertNotIn(self.EVIL, blob)
        self.assertNotIn("<img src=x", blob)
        self.assertNotIn("‮", blob)
        self.assertNotIn("\x1b", blob)
        self.assertNotIn('href="javascript:', blob)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", blob)
        # the "Open in vCenter" link is built from the configured host and a validated id, never from text vCenter sent
        self.assertEqual(src.vm_console_url("vm-1/../../x"), "")
        self.assertEqual(src.vm_console_url("vm-1\n"), "")
        self.assertTrue(src.vm_console_url("vm-104").startswith(src.base_url + "/ui/"))


class Duplicates(HvCase):
    def test_the_same_datastore_and_host_listed_twice_do_not_break_the_sync(self):
        fake = FakeVCenter().start()
        self.addCleanup(fake.stop)
        src = self.make_source(fake)
        real = vsphere.VSphereClient.get_list

        def doubled(self, path, params=None, optional=False):
            rows = real(self, path, params, optional)
            return rows + rows if path in ("/api/vcenter/datastore", "/api/vcenter/host", "/api/vcenter/cluster", "/api/vcenter/vm") else rows

        with mock.patch.object(vsphere.VSphereClient, "get_list", doubled):
            out = self.run_sync(src)
        self.assertNotIn("failed", out)
        src.refresh_from_db()
        self.assertEqual((src.datastore_count, src.host_count, src.vm_count), (2, 2, 5))


class Requests(HvCase):
    def setUp(self):
        super().setUp()
        self.fake = FakeVCenter().start()
        self.addCleanup(self.fake.stop)
        self.src = self.make_source(self.fake)
        self.dev = self.make_vm_device("app-01", ip="10.20.1.15")
        self.run_sync(self.src)
        self.vm = VmInfo.objects.get(source=self.src, moref="vm-101")
        self.assertEqual(self.vm.device_id, self.dev.pk)
        self.req, _ = services.create_request(self.mum_op, self.vm, "power_off", "maintenance window")

    def decide(self, user, decision="approve", pk=None, **kw):
        return self.login(user).post(reverse("hypervisor:request_decide", args=[pk or self.req.pk]), {"decision": decision, **kw})

    def test_approval_sends_nothing_to_anything(self):
        with mock.patch("socket.create_connection", side_effect=AssertionError("outbound connection")), \
                mock.patch("http.client.HTTPConnection.request", side_effect=AssertionError("outbound request")), \
                mock.patch("http.client.HTTPSConnection.request", side_effect=AssertionError("outbound request")):
            r = self.decide(self.mum_admin)
        self.assertEqual(r.status_code, 302)
        self.req.refresh_from_db()
        self.assertEqual(self.req.state, "approved")

    def test_replay_and_a_second_administrator_cannot_decide_twice(self):
        self.decide(self.mum_admin)
        self.decide(self.root, decision="reject", note="late")
        self.decide(self.mum_admin)
        self.req.refresh_from_db()
        self.assertEqual((self.req.state, self.req.decided_by_id), ("approved", self.mum_admin.pk))
        self.assertEqual(AuditEvent.objects.filter(action__startswith="hypervisor.request.").exclude(action="hypervisor.request.create").count(), 1)

    def test_an_old_pending_request_cannot_be_approved_before_the_retention_job_runs(self):
        VmControlRequest.objects.filter(pk=self.req.pk).update(requested_at=timezone.now() - timedelta(days=9))
        self.decide(self.mum_admin)
        self.req.refresh_from_db()
        self.assertEqual(self.req.state, "expired")

    def test_the_permission_matrix(self):
        self.assertEqual(self.decide(self.mum_view).status_code, 403)
        self.assertEqual(self.decide(self.mum_op).status_code, 403)
        self.assertEqual(self.decide(self.che_admin).status_code, 404)                   # another site: not even visible
        self.assertEqual(self.decide(self.glob_view).status_code, 403)
        self.req.refresh_from_db()
        self.assertEqual(self.req.state, "pending")
        url = reverse("hypervisor:vm_request", args=[self.dev.pk])
        self.assertIn(self.login(self.mum_view).post(url, {"action": "power_on", "reason": "x"}).status_code, (302, 403))
        self.assertEqual(self.login(self.che_admin).post(url, {"action": "power_on", "reason": "x"}).status_code, 404)
        self.assertFalse(VmControlRequest.objects.filter(action="power_on").exists())

    def test_next_can_only_be_a_path_on_this_site(self):
        for nxt in ("//evil.example", "https://evil.example/x", "/\\evil.example", "javascript:alert(1)", "///evil.example", "\\\\evil.example"):
            VmControlRequest.objects.filter(pk=self.req.pk).update(state="pending", decided_by=None)
            r = self.decide(self.mum_admin, next=nxt)
            self.assertEqual(r.status_code, 302, nxt)
            self.assertEqual(r["Location"], reverse("hypervisor:requests"), nxt)
        vm = VmInfo.objects.create(tenant_key="default", source=self.src, moref="vm-999", name="loose")
        for nxt in ("//evil.example", "https://evil.example/x"):
            r = self.login(self.mum_op).post(reverse("hypervisor:match_ignore", args=[vm.pk]), {"next": nxt})
            self.assertEqual(r["Location"], reverse("hypervisor:unmatched"), nxt)

    def test_non_ascii_digits_in_query_and_post_data_do_not_crash(self):
        c = self.login(self.mum_op)
        self.assertEqual(c.get(reverse("hypervisor:unmatched") + "?source=%C2%B2").status_code, 200)
        vm = VmInfo.objects.create(tenant_key="default", source=self.src, moref="vm-998", name="loose")
        r = c.post(reverse("hypervisor:match_link", args=[vm.pk]), {"device": "²"})
        self.assertEqual(r.status_code, 302)


class Csrf(HvCase):
    def test_every_post_endpoint_refuses_a_request_without_the_token(self):
        fake = FakeVCenter().start()
        self.addCleanup(fake.stop)
        src = self.make_source(fake)
        dev = self.make_vm_device("app-01", ip="10.20.1.15")
        self.run_sync(src)
        vm = VmInfo.objects.get(source=src, moref="vm-101")
        req, _ = services.create_request(self.mum_op, vm, "power_off", "because")
        c = Client(enforce_csrf_checks=True)
        self.assertTrue(c.login(username="root", password="hv-test-pass-123"))
        posts = [("source_new", ()), ("source_edit", (src.pk,)), ("source_rotate", (src.pk,)), ("source_toggle", (src.pk,)),
                 ("source_delete", (src.pk,)), ("source_test", (src.pk,)), ("source_pin", (src.pk,)), ("source_sync", (src.pk,)),
                 ("match_create", (vm.pk,)), ("match_link", (vm.pk,)), ("match_ignore", (vm.pk,)), ("match_unignore", (vm.pk,)),
                 ("match_unlink", (vm.pk,)), ("request_decide", (req.pk,)), ("vm_request", (dev.pk,))]
        for name, args in posts:
            r = c.post(reverse(f"hypervisor:{name}", args=args), {"decision": "approve", "action": "power_on", "reason": "x"})
            self.assertEqual(r.status_code, 403, name)
        src.refresh_from_db()
        req.refresh_from_db()
        self.assertEqual((src.enabled, req.state), (True, "pending"))
        self.assertTrue(VCenterSource.objects.filter(pk=src.pk).exists())


class Cascade(HvCase):
    def test_deleting_the_source_by_any_route_removes_the_vault_row(self):
        src = self.make_source(None)
        cred = src.credential_id
        self.assertTrue(Credential.objects.filter(pk=cred).exists())
        VCenterSource.objects.filter(pk=src.pk).delete()                       # not through the page
        self.assertFalse(Credential.objects.filter(pk=cred).exists())


@EVERYTHING
class PinnedContext(SimpleTestCase):
    """The TLS context handed to pyvmomi: the pin is checked on the connection that carries the login."""

    def setUp(self):
        self.fake = FakeVCenter().start()
        self.addCleanup(self.fake.stop)

    def get(self, ctx):
        import http.client
        conn = http.client.HTTPSConnection("127.0.0.1", self.fake.port, context=ctx, timeout=5)
        try:
            conn.request("GET", "/api/appliance/system/version")
            return conn.getresponse().status
        finally:
            conn.close()

    def test_the_right_certificate_is_accepted(self):
        self.assertIn(self.get(vsphere.pinned_context(self.fake.fingerprint)), (200, 401))

    def test_any_other_certificate_is_refused_before_a_single_request_is_sent(self):
        import ssl
        with self.assertRaises(ssl.SSLError):
            self.get(vsphere.pinned_context("ab" * 32))
        self.assertEqual(self.fake.requests, [])

    def test_no_pin_means_no_context(self):
        for bad in ("", "xyz", None, "ab" * 31):
            with self.assertRaises(vsphere.VSphereError):
                vsphere.pinned_context(bad)


class CrossSite(HvCase):
    def test_a_vcenter_of_one_site_never_links_a_device_of_another_site(self):
        fake = FakeVCenter().start()
        self.addCleanup(fake.stop)
        elsewhere = self.make_vm_device("app-01", site="Chennai", ip="10.20.1.15")       # same name and IP as vCenter's vm-101
        src = self.make_source(fake)                                                      # a Mumbai source
        self.run_sync(src)
        vm = VmInfo.objects.get(source=src, moref="vm-101")
        self.assertIsNone(vm.device_id)
        self.assertEqual(vm.match_state, "unmatched")
        from compute.models import VMPlacement
        self.assertFalse(VMPlacement.objects.filter(vm=elsewhere).exists())
        everywhere = self.make_vm_device("db-01", site="", ip="10.20.1.16")               # a device of every site may be matched
        self.run_sync(src)
        self.assertEqual(VmInfo.objects.get(source=src, moref="vm-102").device_id, everywhere.pk)
