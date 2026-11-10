"""Regression tests for the verified review findings HV-1 .. HV-5 (one class per finding)."""
import http.client
import json
import socket
import ssl
import sys
import threading
import time
import types
from datetime import datetime, timezone as dt_tz
from types import SimpleNamespace as NS
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from django.test import SimpleTestCase, override_settings
from django.urls import reverse

from hypervisor import pyvmomi_provider, services, vsphere
from hypervisor.models import HvHost, VCenterSource, VmInfo
from hypervisor.providers import ProviderError
from hypervisor.tests.base import HvCase
from hypervisor.tests.fake_vcenter import FakeVCenter, make_cert


def _global_source(name="vc-global"):
    return VCenterSource.objects.create(tenant_key=services.tenant(), name=name, site="All Sites", host="127.0.0.1", port=443,
                                        tls_mode="skip")


@override_settings(PROMETHEUS_URL="")
class HV1CrossSiteDeviceNames(HvCase):
    """A global source's VM candidates and host links must not show another site's device to a site-scoped viewer."""

    def setUp(self):
        super().setUp()
        self.src = _global_source()
        self.mum_dev = self.make_vm_device("app01", site="Mumbai", ip="10.0.0.5")
        self.che_dev = self.make_vm_device("secret-chennai-vm", site="Chennai", ip="10.0.0.9")
        self.vm = VmInfo.objects.create(tenant_key=services.tenant(), source=self.src, moref="vm-1", name="app01",
                                        match_state="ambiguous", match_reason="two devices fit",
                                        match_candidates=[self.mum_dev.pk, self.che_dev.pk])
        self.srv = self.make_vm_device("che-esx-secret", site="Chennai", ip="10.0.0.20", device_type="physical_server")
        HvHost.objects.create(tenant_key=services.tenant(), source=self.src, moref="host-1", name="esx-01.example",
                              device=self.srv, match_state="matched")

    def test_unmatched_page_hides_out_of_scope_candidates(self):
        c = self.login(self.mum_view)
        r = c.get(reverse("hypervisor:unmatched"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "app01")
        self.assertNotContains(r, "secret-chennai-vm")
        self.assertNotContains(r, reverse("network:detail", args=[self.che_dev.pk]))

    def test_unmatched_page_still_shows_candidates_to_a_global_viewer(self):
        r = self.login(self.root).get(reverse("hypervisor:unmatched"))
        self.assertContains(r, "secret-chennai-vm")

    def test_datastores_page_hides_out_of_scope_host_device(self):
        r = self.login(self.mum_view).get(reverse("hypervisor:datastores"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "esx-01.example")                # the host of a visible source is still listed
        self.assertNotContains(r, "che-esx-secret")
        self.assertNotContains(r, reverse("network:detail", args=[self.srv.pk]))

    def test_datastores_page_shows_in_scope_host_device(self):
        r = self.login(self.che_admin).get(reverse("hypervisor:datastores"))
        self.assertContains(r, "che-esx-secret")


@override_settings(PROMETHEUS_URL="")
class HV4HugeIntegerParameters(HvCase):
    """Over-long digit strings in ?source= and device= are ignored instead of raising (HTTP 500)."""

    def setUp(self):
        super().setUp()
        self.src = self.make_source()
        self.vm = VmInfo.objects.create(tenant_key=services.tenant(), source=self.src, moref="vm-1", name="lonely",
                                        match_state="unmatched")

    def test_unmatched_source_filter(self):
        c = self.login(self.mum_view)
        c.raise_request_exception = False
        for n in (5000, 4300, 30):
            r = c.get(reverse("hypervisor:unmatched"), {"source": "1" * n})
            self.assertEqual(r.status_code, 200, n)

    def test_match_link_device(self):
        c = self.login(self.mum_op)
        c.raise_request_exception = False
        for n in (5000, 4300, 30):
            r = c.post(reverse("hypervisor:match_link", args=[self.vm.pk]), {"device": "1" * n})
            self.assertEqual(r.status_code, 302, n)
        self.vm.refresh_from_db()
        self.assertIsNone(self.vm.device_id)


# ----------------------------------------------------------------------------- the pyvmomi provider (HV-2, HV-5)


def stand_in_pyvmomi(vm_obj=None, calls=None):
    """pyVim / pyVmomi stand-ins whose SmartConnect opens a real HTTPS connection the way pyvmomi does: http.client with the
    host NAME (so it resolves the name again), the caller's sslContext and httpConnectionTimeout as the socket timeout."""
    calls = calls if calls is not None else []
    vim = types.ModuleType("pyVmomi.vim")
    vim.VirtualMachine = lambda ref, stub: vm_obj(ref) if callable(vm_obj) else vm_obj
    spec = type("EventFilterSpec", (), {"__init__": lambda self, **kw: self.__dict__.update(kw)})
    spec.ByEntity = lambda **kw: NS(**kw)
    spec.ByTime = lambda **kw: NS(**kw)
    vim.event = NS(EventFilterSpec=spec)
    pyvmomi = types.ModuleType("pyVmomi")
    pyvmomi.vim = vim
    content = NS(eventManager=NS(QueryEvents=lambda s: []))

    def smart_connect(**kw):
        calls.append(kw)
        conn = http.client.HTTPSConnection(kw["host"], kw["port"], timeout=kw.get("httpConnectionTimeout"), context=kw["sslContext"])
        try:
            conn.request("GET", "/sdk/vimServiceVersions.xml")
            conn.getresponse().read()
        finally:
            conn.close()
        return NS(RetrieveContent=lambda: content, _stub=object())

    connect = types.ModuleType("pyVim.connect")
    connect.SmartConnect = smart_connect
    connect.Disconnect = lambda si: None
    pyvim = types.ModuleType("pyVim")
    pyvim.connect = connect
    return {"pyVmomi": pyvmomi, "pyVim": pyvim, "pyVim.connect": connect}


def _rebinding_getaddrinfo(first, then):
    """getaddrinfo that answers ``first`` once and ``then`` afterwards (a DNS rebinding attacker)."""
    real = socket.getaddrinfo
    seen = []

    def fake(host, port, *a, **kw):
        if host != "vc.example.test":
            return real(host, port, *a, **kw)
        ip = first if not seen else then
        seen.append(ip)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, int(port)))]
    return fake


class HV5SecondLookup(SimpleTestCase):
    """The address pyvmomi actually connects to must pass the SSRF policy, not just the first DNS answer."""

    def setUp(self):
        self.fake = FakeVCenter().start()
        self.addCleanup(self.fake.stop)

    def test_a_rebound_name_never_reaches_the_refused_address(self):
        # first answer: an allowed address (127.0.0.2 is listed); second answer: 127.0.0.1, which the policy refuses
        with override_settings(PROBE_ALLOWED_PRIVATE_CIDRS=["127.0.0.2/32"]), mock.patch.dict(sys.modules, stand_in_pyvmomi()):
            for mode in ("skip", "verify", "pin"):
                with mock.patch("socket.getaddrinfo", _rebinding_getaddrinfo("127.0.0.2", "127.0.0.1")), \
                        self.assertRaises(ProviderError, msg=mode):
                    pyvmomi_provider.PyvmomiProvider("vc.example.test", self.fake.port, "u", "Sup3r", mode, self.fake.fingerprint)
        self.assertEqual(self.fake.requests, [])

    def test_an_allowed_address_still_works(self):
        with override_settings(PROBE_ALLOWED_PRIVATE_CIDRS=["127.0.0.0/8"]), mock.patch.dict(sys.modules, stand_in_pyvmomi()):
            p = pyvmomi_provider.PyvmomiProvider("127.0.0.1", self.fake.port, "u", "p", "pin", self.fake.fingerprint)
            p.close()
        self.assertEqual([r[1] for r in self.fake.requests], ["/sdk/vimServiceVersions.xml"])

    def test_the_pin_is_still_enforced(self):
        with override_settings(PROBE_ALLOWED_PRIVATE_CIDRS=["127.0.0.0/8"]), mock.patch.dict(sys.modules, stand_in_pyvmomi()):
            with self.assertRaises(ProviderError):
                pyvmomi_provider.PyvmomiProvider("127.0.0.1", self.fake.port, "u", "p", "pin", "ab" * 32)
        self.assertEqual(self.fake.requests, [])


class _SilentTLS:
    """Completes the TLS handshake, reads the request and never answers (a stalled vCenter)."""

    def __init__(self):
        cert, key, _ = make_cert()
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(cert, key)
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(5)
        self.port = self.srv.getsockname()[1]
        self.held = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                c, _ = self.srv.accept()
                c = self.ctx.wrap_socket(c, server_side=True)
                c.recv(65536)
                self.held.append(c)
            except OSError:
                return

    def stop(self):
        self.srv.close()
        for c in self.held:
            c.close()


@override_settings(PROBE_ALLOWED_PRIVATE_CIDRS=["127.0.0.0/8"])
class HV2Deadline(SimpleTestCase):
    """A stalled or slow vCenter cannot hold the provider (and so the job scheduler) past its deadline."""

    def test_smartconnect_gets_a_socket_timeout(self):
        calls = []
        fake = FakeVCenter().start()
        self.addCleanup(fake.stop)
        with mock.patch.dict(sys.modules, stand_in_pyvmomi(calls=calls)):
            pyvmomi_provider.PyvmomiProvider("127.0.0.1", fake.port, "u", "p", "skip").close()
        timeout = calls[0].get("httpConnectionTimeout")
        self.assertIsNotNone(timeout)
        self.assertLessEqual(timeout, 30)

    def test_a_vcenter_that_refuses_connections_fails_fast_before_pyvmomi_runs(self):
        import socket as _s
        probe = _s.socket(); probe.bind(("127.0.0.1", 0)); port = probe.getsockname()[1]; probe.close()   # nothing listens
        calls = []
        t = time.monotonic()
        with mock.patch.dict(sys.modules, stand_in_pyvmomi(calls=calls)), self.assertRaises(ProviderError) as cm:
            pyvmomi_provider.PyvmomiProvider("127.0.0.1", port, "u", "p", "skip", deadline=5)
        self.assertIn("did not accept a connection", str(cm.exception))
        self.assertEqual(calls, [])                         # SmartConnect (and its untimed version probe) never ran
        self.assertLess(time.monotonic() - t, 3)

    def test_the_preflight_connect_is_bounded(self):
        seen = {}

        def fake_connect(addr, timeout=None):
            seen["timeout"] = timeout
            raise TimeoutError("timed out")
        with mock.patch.dict(sys.modules, stand_in_pyvmomi()), \
                mock.patch.object(pyvmomi_provider.socket, "create_connection", fake_connect), self.assertRaises(ProviderError):
            pyvmomi_provider.PyvmomiProvider("127.0.0.1", 443, "u", "p", "skip", deadline=3)
        self.assertLessEqual(seen["timeout"], 3)

    def test_a_stalled_login_ends_at_the_run_deadline(self):
        srv = _SilentTLS()
        self.addCleanup(srv.stop)
        result = {}

        def go():
            t = time.monotonic()
            try:
                with mock.patch.dict(sys.modules, stand_in_pyvmomi()), mock.patch.object(pyvmomi_provider, "SOCKET_TIMEOUT", 60):
                    pyvmomi_provider.PyvmomiProvider("127.0.0.1", srv.port, "u", "p", "skip", deadline=1)
                result["r"] = "connected"
            except ProviderError as exc:
                result["r"] = str(exc)
            result["t"] = time.monotonic() - t
        th = threading.Thread(target=go, daemon=True)
        th.start()
        th.join(10)
        self.assertFalse(th.is_alive(), "the provider is still blocked on a silent vCenter")
        self.assertIn("could not log in", result["r"])
        self.assertLess(result["t"], 5)

    def test_fetch_stops_at_the_deadline_with_a_partial_answer(self):
        fake = FakeVCenter().start()
        self.addCleanup(fake.stop)

        def slow_vm(ref):
            class VM:
                @property
                def snapshot(self):
                    time.sleep(0.3)
                    return None
                config = None
            return VM()
        with mock.patch.dict(sys.modules, stand_in_pyvmomi(vm_obj=slow_vm)):
            p = pyvmomi_provider.PyvmomiProvider("127.0.0.1", fake.port, "u", "p", "skip", deadline=1)
            t = time.monotonic()
            out = p.fetch([f"vm-{i}" for i in range(20)], datetime.now(dt_tz.utc))
            took = time.monotonic() - t
            p.close()
        self.assertLess(took, 2.5)
        self.assertGreater(len(out["snapshots"]), 0)
        self.assertLess(len(out["snapshots"]), 20)


# ----------------------------------------------------------------------------- large estates (HV-3)


class BigVCenter(vsphere.VSphereClient):
    """No network: a vCenter answering like vSphere 7/8 (the full VM list errors above ``list_limit`` matches), with a simulated
    per-request latency charged against the real deadline logic. vm-i runs on host-(i % hosts) and datastore-(i % datastores)."""

    def __init__(self, hosts=3, datastores=3, vms=10, list_limit=4000, per_request=0.0, failing_hosts=(), deadline=90):
        super().__init__("vc.test", 443, "u", "p", deadline=deadline)
        self.n_hosts, self.n_ds, self.n_vms, self.limit, self.per = hosts, datastores, vms, list_limit, per_request
        self.failing_hosts = set(failing_hosts)
        self.clock = 0.0

    def remaining(self):
        return super().remaining() - self.clock

    def _vm(self, i):
        return {"vm": f"vm-{i}", "name": f"v{i}", "power_state": "POWERED_OFF", "cpu_count": 1, "memory_size_MiB": 1024}

    def _send(self, method, path, headers=None, _retry=True):
        if self.remaining() <= 0:
            raise vsphere.VSphereError("The sync ran out of time.", "timeout")
        self.clock += self.per
        self.requests += 1
        if method == "POST":
            return 201, json.dumps("session-token-123456").encode()
        if method == "DELETE":
            return 204, b""
        parts = urlsplit(path)
        p, q = parts.path, {k: v[0] for k, v in parse_qs(parts.query).items()}
        if p == "/api/vcenter/host":
            return 200, json.dumps([{"host": f"host-{i}", "name": f"h{i}", "connection_state": "CONNECTED", "power_state": "POWERED_ON"}
                                    for i in range(self.n_hosts)] if not q else []).encode()
        if p == "/api/vcenter/cluster":
            return 200, b"[]"
        if p == "/api/vcenter/datastore":
            return 200, json.dumps([{"datastore": f"datastore-{i}", "name": f"ds{i}", "type": "VMFS", "capacity": 10, "free_space": 5}
                                    for i in range(self.n_ds)]).encode()
        if p.startswith("/api/vcenter/datastore/"):
            return 200, b"{}"
        if p == "/api/vcenter/vm":
            if "hosts" in q:
                if q["hosts"] in self.failing_hosts:
                    return 500, b'{"error_type": "INTERNAL_SERVER_ERROR"}'
                rows = [self._vm(i) for i in range(self.n_vms) if f"host-{i % self.n_hosts}" == q["hosts"]]
            elif "datastores" in q:
                rows = [self._vm(i) for i in range(self.n_vms) if f"datastore-{i % self.n_ds}" == q["datastores"]]
            else:
                rows = [self._vm(i) for i in range(self.n_vms)]
            if len(rows) > self.limit:
                return 500, b'{"error_type": "UNABLE_TO_ALLOCATE_RESOURCE"}'
            return 200, json.dumps(rows).encode()
        return 404, b"{}"


class HV3LargeEstates(HvCase):
    def test_more_vms_than_the_list_limit_are_read_per_host(self):
        with BigVCenter(hosts=4, vms=40, list_limit=25) as c:
            inv = vsphere.collect_inventory(c)
        self.assertEqual(len(inv["vms"]), 40)
        self.assertTrue(any("per host" in w for w in inv["warnings"]), inv["warnings"])
        self.assertEqual({v["moref"]: v.get("host_moref") for v in inv["vms"]}["vm-5"], "host-1")

    def test_the_sync_of_an_estate_over_the_list_limit_succeeds(self):
        src = self.make_source()
        r = self.run_sync(src, client_factory=lambda s, u, p: BigVCenter(hosts=4, vms=40, list_limit=25))
        self.assertNotIn("failed", r)
        self.assertEqual(src.vms.filter(missing_since__isnull=True).count(), 40)

    def test_a_failed_host_list_does_not_mark_its_vms_missing(self):
        src = self.make_source()
        self.run_sync(src, client_factory=lambda s, u, p: BigVCenter(hosts=4, vms=40))
        r = self.run_sync(src, client_factory=lambda s, u, p: BigVCenter(hosts=4, vms=40, list_limit=25, failing_hosts={"host-2"}))
        self.assertNotIn("failed", r)
        self.assertEqual(src.vms.filter(missing_since__isnull=False).count(), 0)

    def test_the_placement_fan_out_stops_at_the_deadline_and_keeps_earlier_placement(self):
        src = self.make_source()
        self.run_sync(src, client_factory=lambda s, u, p: BigVCenter(hosts=50, datastores=30, vms=200))
        before = dict(src.vms.values_list("moref", "host_moref"))
        self.assertTrue(all(before.values()))
        # 500 hosts and 300 datastores at 0.12 s a request: the per-host and per-datastore lists alone need ~130 s
        r = self.run_sync(src, client_factory=lambda s, u, p: BigVCenter(hosts=500, datastores=300, vms=200, per_request=0.12))
        self.assertNotIn("failed", r)
        self.assertIn("partial", r)
        src.refresh_from_db()
        self.assertEqual(src.consecutive_failures, 0)
        self.assertEqual(src.vms.filter(missing_since__isnull=True).count(), 200)
        after = dict(src.vms.values_list("moref", "host_moref"))
        self.assertTrue(all(after.values()), "a VM lost its host because the fan-out was cut short")

    def test_hosts_past_the_cap_do_not_mark_their_vms_missing(self):
        # without the full list, VMs on hosts past MAX_HOSTS (and on datastores past MAX_DATASTORES) are in no list read
        src = self.make_source()
        self.run_sync(src, client_factory=lambda s, u, p: BigVCenter(hosts=600, datastores=400, vms=1200))
        r = self.run_sync(src, client_factory=lambda s, u, p: BigVCenter(hosts=600, datastores=400, vms=1200, list_limit=1000))
        self.assertNotIn("failed", r)
        self.assertEqual(src.vms.filter(missing_since__isnull=False).count(), 0)
