"""Review round: site-scoped associated services, a hard deadline for security scans, and closing the
auto-incident of a domain that stops being monitored."""
import json
import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import Event, Membership, Site
from probes import runner
from servicedesk.models import Ticket
from urlchecker.models import UrlCheck

from . import security
from .models import Domain


# ---------------------------------------------------------------------------- associated services
class AssociatedServicesScopeTests(TestCase):
    def test_other_sites_url_checks_are_not_listed(self):
        a = Site.objects.create(name="SiteA", code="A")
        Site.objects.create(name="SiteB", code="B")
        u = User.objects.create_user("va", password="pw")
        Membership.objects.create(user=u, site=a, role="viewer")
        dom = Domain.objects.create(tenant_key="default", hostname="corp.example.com", site_key="All Sites")
        UrlCheck.objects.create(tenant_key="default", site="SiteB", name="SECRET-B-PAYROLL",
                                url="https://payroll.corp.example.com/admin")
        UrlCheck.objects.create(tenant_key="default", site="SiteA", name="A-PORTAL", url="https://www.corp.example.com/")
        UrlCheck.objects.create(tenant_key="default", site="All Sites", name="SHARED-WWW", url="https://corp.example.com/")
        self.client.force_login(u)
        r = self.client.get(reverse("domains:list") + f"?selected={dom.pk}")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "A-PORTAL")
        self.assertContains(r, "SHARED-WWW")
        self.assertNotContains(r, "SECRET-B-PAYROLL")
        self.assertNotContains(r, "payroll.corp.example.com")


# ---------------------------------------------------------------------------- scan deadline
def make_cert(tmp):
    key, crt = os.path.join(tmp, "k.pem"), os.path.join(tmp, "c.pem")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", crt,
                    "-days", "1", "-subj", "/CN=localhost"], check=True, capture_output=True)
    return key, crt


class TricklingServer:
    """TLS server: completes the handshake, then answers the status line and one header every 0.3 s for `secs`."""

    def __init__(self, key, crt, secs=8):
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(crt, key)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.secs = secs
        self.stop = False
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while not self.stop:
            try:
                raw, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self.handle, args=(raw,), daemon=True).start()

    def handle(self, raw):
        try:
            conn = self.ctx.wrap_socket(raw, server_side=True)
            conn.recv(65536)
            conn.sendall(b"HTTP/1.1 200 OK\r\n")
            end = time.monotonic() + self.secs
            while time.monotonic() < end and not self.stop:
                conn.sendall(b"X-A: b\r\n")
                time.sleep(0.3)
            conn.close()
        except OSError:
            pass

    def close(self):
        self.stop = True
        self.sock.close()


@override_settings(PROBE_TIMEOUT=2, PROBE_ALLOWED_PRIVATE_CIDRS=["127.0.0.0/8"])
class ScanDeadlineTests(TestCase):
    def setUp(self):
        if not shutil.which("openssl"):
            self.skipTest("openssl is not available")
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.server = TricklingServer(*make_cert(self.tmp))
        self.addCleanup(self.server.close)

    def test_header_trickling_server_cannot_hold_a_scan(self):
        t0 = time.monotonic()
        data = security.scan_host("localhost", port=self.server.port)
        took = time.monotonic() - t0
        self.assertLess(took, 6.5, f"the scan ran {took:.1f}s")
        self.assertIn("Timed out", data.error)

    def test_plain_http_probe_has_a_deadline_too(self):
        # a plain-HTTP peer that trickles headers: _http_redirects must give up at its budget
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(4)
        port = srv.getsockname()[1]
        stop = threading.Event()

        def serve():
            conn, _ = srv.accept()
            conn.recv(4096)
            conn.sendall(b"HTTP/1.1 200 OK\r\n")
            while not stop.is_set():
                try:
                    conn.sendall(b"X-A: b\r\n")
                except OSError:
                    return
                time.sleep(0.3)

        threading.Thread(target=serve, daemon=True).start()
        self.addCleanup(stop.set)
        self.addCleanup(srv.close)
        with mock.patch.object(security.checks, "resolve_allowed", return_value="127.0.0.1"), \
                mock.patch.object(security.checks._PinnedHTTPConnection, "__init__",
                                  lambda self_, host, ip, p, timeout: security.http.client.HTTPConnection.__init__(
                                      self_, host, port, timeout=timeout) or setattr(self_, "_ip", ip)):
            t0 = time.monotonic()
            result, note = security._http_redirects("localhost", 1.5)
            took = time.monotonic() - t0
        self.assertLess(took, 4)
        self.assertIsNone(result)


class RunScansBudgetTests(TestCase):
    def test_scans_not_started_before_the_budget_ends_are_skipped(self):
        doms = [Domain.objects.create(tenant_key="default", hostname=f"d{i}.example.com", site_key="All Sites")
                for i in range(4)]

        def slow(domain):
            time.sleep(0.4)
            return security.ScanData(hostname=domain.hostname, error="x")

        with override_settings(PROBE_WORKERS=1), mock.patch.object(security, "_safe_scan", side_effect=slow):
            scans = security.run_scans(doms, budget=0.5)
        self.assertLess(len(scans), 4)
        self.assertGreaterEqual(len(scans), 1)
        self.assertEqual(security.DomainSecurityScan.objects.count(), len(scans))

    def test_manual_scan_all_is_bounded(self):
        from . import views_insights
        self.assertLessEqual(views_insights.MAX_MANUAL_SCANS, 30)


# ---------------------------------------------------------------------------- incident closing
@override_settings(INCIDENT_AUTO_CREATE=True, NOTIFY_WEBHOOK_URLS=[], NOTIFY_EMAILS=[], LINKEDEYE_TENANT="default")
class DomainIncidentClosingTests(TestCase):
    def setUp(self):
        self.client.force_login(User.objects.create_superuser("admin", "a@x", "x"))

    def down_domain(self, host="down.example.com"):
        d = Domain.objects.create(tenant_key="default", hostname=host, site_key="All Sites")
        dns = mock.Mock(ok=False, error="no dns", ips=[])
        runner.save_domain_result(d, (dns, None, None), timezone.now())
        self.assertEqual(Ticket.objects.get(fingerprint=f"domain:{d.pk}").status, "new")
        return d

    def status(self, d):
        return Ticket.objects.get(fingerprint=f"domain:{d.pk}").status

    def test_deactivate_closes_the_incident(self):
        d = self.down_domain()
        self.client.post(reverse("domains:toggle", args=[d.pk]))
        d.refresh_from_db()
        self.assertFalse(d.is_active)
        self.assertEqual(self.status(d), "mitigated")

    def test_reactivating_does_not_close_anything(self):
        d = self.down_domain()
        d.is_active = False
        d.save()
        self.client.post(reverse("domains:toggle", args=[d.pk]))
        self.assertEqual(self.status(d), "new")

    def test_turning_monitoring_off_closes_the_incident(self):
        d = self.down_domain()
        r = self.client.post(reverse("domains:list"), {
            "pk": d.pk, "hostname": d.hostname, "domain_type": "Production", "site_key": "All Sites",
            "check_interval_seconds": 300})        # monitor_enabled omitted: unchecked
        self.assertEqual(r.status_code, 302, getattr(r, "context", None) and r.context["form"].errors)
        d.refresh_from_db()
        self.assertFalse(d.monitor_enabled)
        self.assertEqual(self.status(d), "mitigated")

    def test_rename_closes_the_incident_of_the_old_host(self):
        d = self.down_domain()
        with mock.patch("probes.runner.run_domain_checks"):
            r = self.client.post(reverse("domains:list"), {
                "pk": d.pk, "hostname": "renamed.example.com", "domain_type": "Production", "site_key": "All Sites",
                "check_interval_seconds": 300, "monitor_enabled": "on"})
        self.assertEqual(r.status_code, 302, getattr(r, "context", None) and r.context["form"].errors)
        d.refresh_from_db()
        self.assertEqual((d.hostname, d.status), ("renamed.example.com", "unknown"))
        self.assertEqual(self.status(d), "mitigated")

    def test_api_delete_and_patch_inactive_close_the_incident(self):
        d = self.down_domain("api1.example.com")
        r = self.client.delete(f"/configuration/domains/api/{d.pk}/?tenant=default")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.status(d), "mitigated")
        d2 = self.down_domain("api2.example.com")
        r = self.client.patch(f"/configuration/domains/api/{d2.pk}/?tenant=default", json.dumps({"active": False}),
                              content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(self.status(d2), "mitigated")

    def test_up_domain_emits_nothing_when_deactivated(self):
        d = Domain.objects.create(tenant_key="default", hostname="up.example.com", site_key="All Sites", status="up")
        self.client.post(reverse("domains:toggle", args=[d.pk]))
        self.assertFalse(Event.objects.filter(fingerprint=f"domain:{d.pk}").exists())
