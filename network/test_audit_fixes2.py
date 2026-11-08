"""Regression tests for the second network audit round:

* discovery housekeeping (tick) must only touch this deployment's tenant,
* a Down device that passes through Unknown must still emit the recovery event,
* a failed ifOperStatus/ifAdminStatus query must not reset the stored interface state,
* approving the same discovered host twice must not create two devices,
* a device whose monitoring is off must not show frozen rates as current traffic,
* a discovery run must not show the names of devices on sites the viewer cannot access.
"""
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import Event, Membership, Site

from . import discovery, jobs, metrics, views
from .models import Device, DeviceStatus, DiscoveredHost, DiscoveryRun, Interface
from .probe import TcpResult

T = "default"


def dev(name="sw-01", **kw):
    kw.setdefault("tenant_key", T)
    kw.setdefault("mgmt_ip", "192.0.2.10")
    kw.setdefault("device_type", "switch")
    return Device.objects.create(name=name, **kw)


def ok(ms=1.5):
    return TcpResult(True, ms, ip="192.0.2.10")


def fail(msg="Connection refused on port 22"):
    return TcpResult(False, error=msg, ip="192.0.2.10")


def blocked():
    return TcpResult(False, error="Blocked by probe policy: x", blocked=True)


# ------------------------------------------------------------ 1. tenant-scoped discovery housekeeping
@override_settings(LINKEDEYE_TENANT=T)
class DiscoveryTickTenantTests(TestCase):
    def test_tick_ignores_other_tenants_runs(self):
        now = timezone.now()
        mine = DiscoveryRun.objects.create(tenant_key=T, cidr="192.0.2.0/30", ports="22",
                                           created_at=now - timedelta(minutes=1))
        other = DiscoveryRun.objects.create(tenant_key="other", cidr="10.9.9.0/30", ports="22",
                                            created_at=now - timedelta(minutes=1))
        other_stale = DiscoveryRun.objects.create(tenant_key="other", cidr="10.9.8.0/30", ports="22", status="running",
                                                  started_at=now - timedelta(minutes=30))
        launched = []
        msg = discovery.tick(launcher=lambda run: launched.append(run.pk))
        self.assertEqual(launched, [mine.pk])
        other.refresh_from_db()
        other_stale.refresh_from_db()
        self.assertEqual(other.status, "queued")
        self.assertEqual(other_stale.status, "running")
        self.assertIn("started 1", msg)
        self.assertIn("failed 0", msg)

    def test_tick_still_fails_own_stale_runs(self):
        run = DiscoveryRun.objects.create(tenant_key=T, cidr="192.0.2.0/30", ports="22", status="running",
                                          started_at=timezone.now() - timedelta(minutes=30))
        discovery.tick(launcher=lambda r: None)
        run.refresh_from_db()
        self.assertEqual(run.status, "failed")


# ------------------------------------------------------------ 2. recovery through Unknown
@override_settings(LINKEDEYE_TENANT=T)
class RecoveryThroughUnknownTests(TestCase):
    def _down(self, d):
        for _ in range(jobs.DOWN_AFTER_FAILURES):
            jobs.record(d, fail())
        self.assertEqual(d.status, DeviceStatus.DOWN)

    def _oks(self, d):
        return Event.objects.filter(fingerprint=f"network:{d.pk}", severity="ok").count()

    def test_recovery_after_blocked_check(self):
        d = dev("rtr-y", status=DeviceStatus.UP)
        self._down(d)
        jobs.record(d, blocked())
        self.assertEqual(d.status, DeviceStatus.UNKNOWN)
        jobs.record(d, ok())
        self.assertEqual(d.status, DeviceStatus.UP)
        self.assertEqual(self._oks(d), 1)

    def test_recovery_after_monitoring_turned_off_and_on(self):
        u = User.objects.create_superuser("admin-recov", "a@example.com", "pw")
        c = Client()
        c.force_login(u)
        d = dev("rtr-x", status=DeviceStatus.UP)
        self._down(d)
        data = {"name": "rtr-x", "device_type": "switch", "mgmt_ip": "192.0.2.10", "check_port": 22, "site": ""}
        self.assertEqual(c.post(reverse("network:edit", args=[d.pk]), data).status_code, 302)
        d.refresh_from_db()
        self.assertEqual((d.enabled, d.status), (False, DeviceStatus.UNKNOWN))
        with mock.patch("network.probe.tcp_probe", return_value=ok()):
            c.post(reverse("network:edit", args=[d.pk]), dict(data, enabled="on"))
        d.refresh_from_db()
        self.assertEqual(d.status, DeviceStatus.UP)
        self.assertEqual(self._oks(d), 1)

    def test_direct_recovery_still_emits_one_event(self):
        d = dev("rtr-z", status=DeviceStatus.UP)
        self._down(d)
        jobs.record(d, ok())
        self.assertEqual(self._oks(d), 1)
        # an Up device that stays up must not emit the recovery again
        jobs.record(d, ok())
        self.assertEqual(self._oks(d), 1)

    def test_unknown_to_up_without_a_down_alert_emits_nothing(self):
        d = dev("rtr-w", status=DeviceStatus.UNKNOWN)
        jobs.record(d, ok())
        self.assertEqual(self._oks(d), 0)


# ------------------------------------------------------------ 3. failed oper/admin query
@override_settings(LINKEDEYE_TENANT=T)
class InterfaceStateQueryFailureTests(TestCase):
    def setUp(self):
        self.d = dev("sw-metrics", prometheus_instance="sw1")

    def row(self, **kw):
        base = {"name": "Gi1", "alias": "", "in": 1.0, "out": 2.0, "speed": 1000.0}
        base.update(kw)
        return {"interfaces": {1: base}, "uptime": 3600.0}

    def test_failed_oper_query_keeps_state_and_next_down_is_reported(self):
        metrics.apply(self.d, self.row(oper="up", admin="up"), [])
        i = Interface.objects.get(device=self.d)
        self.assertEqual(i.oper_status, "up")
        # the ifOperStatus query timed out: the other series still came back
        t = metrics.apply(self.d, self.row(admin="up"), ["oper: HTTPConnectionPool: read timed out"])
        i.refresh_from_db()
        self.assertEqual(i.oper_status, "up")
        self.assertEqual(t, [])
        # next cycle the link is really down: the transition must be reported
        t = metrics.apply(self.d, self.row(oper="down", admin="up"), [])
        i.refresh_from_db()
        self.assertEqual(i.oper_status, "down")
        self.assertEqual([(old, new) for _i, old, new in t], [("up", "down")])

    def test_failed_admin_query_keeps_state_so_the_transition_is_reported(self):
        metrics.apply(self.d, self.row(oper="up", admin="up"), [])
        t = metrics.apply(self.d, self.row(oper="down"), ["admin: boom"])
        i = Interface.objects.get(device=self.d)
        self.assertEqual((i.admin_status, i.oper_status), ("up", "down"))
        self.assertEqual([(old, new) for _i, old, new in t], [("up", "down")])

    def test_missing_series_without_an_error_still_stores_unknown(self):
        metrics.apply(self.d, self.row(oper="up", admin="up"), [])
        metrics.apply(self.d, self.row(), [])
        i = Interface.objects.get(device=self.d)
        self.assertEqual((i.admin_status, i.oper_status), ("unknown", "unknown"))

    def test_fetch_names_the_failed_query_in_its_errors(self):
        def query(promql):
            if "ifOperStatus" in promql and "changes(" not in promql:
                raise metrics.prometheus.PrometheusError("read timed out")
            return []
        with mock.patch("core.prometheus.query", side_effect=query):
            _data, errors = metrics.fetch(["sw1"])
        self.assertTrue(any(e.startswith("oper:") for e in errors), errors)
        self.assertEqual(metrics.failed_keys(errors), {"oper"})


# ------------------------------------------------------------ 4. concurrent approval of one host
@override_settings(LINKEDEYE_TENANT=T, PROBE_ALLOWED_PRIVATE_CIDRS=["192.0.2.0/24"])
class ApproveRaceTests(TestCase):
    def setUp(self):
        self.run = DiscoveryRun.objects.create(tenant_key=T, cidr="192.0.2.0/29", ports="22")
        self.host = DiscoveredHost.objects.create(run=self.run, ip="192.0.2.5", open_ports=[22], status="new")
        self.url = reverse("network:discovery_approve", args=[self.run.pk])
        self.data = {"host": str(self.host.pk), "device_type": "switch", "site": "", "name_prefix": ""}
        self.a, self.b = Client(), Client()
        for n, c in (("op-a", self.a), ("op-b", self.b)):
            c.force_login(User.objects.create_superuser(n, f"{n}@example.com", "pw"))

    def test_overlapping_approvals_create_one_device(self):
        fired = []
        real = views._unique_name

        def interleave(base):
            if not fired:
                fired.append(1)
                # the second operator (or a double-click) completes the whole request here
                self.b.post(self.url, self.data)
            return real(base)

        with mock.patch.object(views, "_unique_name", side_effect=interleave):
            self.a.post(self.url, self.data)
        self.assertEqual(Device.objects.filter(mgmt_ip="192.0.2.5").count(), 1)
        self.host.refresh_from_db()
        self.assertEqual(self.host.status, "approved")
        self.assertEqual(self.host.device, Device.objects.get(mgmt_ip="192.0.2.5"))

    def test_name_taken_between_the_check_and_the_insert_is_not_a_500(self):
        """The other half of the finding: _unique_name picks a free name, someone else inserts it
        first, and the INSERT raises IntegrityError on network_device_tenant_name_unique."""
        real = views._unique_name
        calls = []

        def steal(base):
            name = real(base)
            if not calls:
                calls.append(name)
                # another request inserts exactly this name between our check and our INSERT
                Device.objects.create(tenant_key=T, name=name, device_type="switch", mgmt_ip="192.0.2.9", check_port=22)
            return name

        with mock.patch.object(views, "_unique_name", side_effect=steal):
            r = self.a.post(self.url, self.data)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Device.objects.filter(mgmt_ip="192.0.2.5").count(), 1)
        self.host.refresh_from_db()
        self.assertEqual(self.host.status, "approved")

    def test_address_that_is_already_a_device_is_linked_not_duplicated(self):
        """The device appeared (another run, or by hand) after the scan: the re-read after the claim links it.
        A legacy row stored with stray whitespace in mgmt_ip is still recognised."""
        existing = Device.objects.create(tenant_key=T, name="by-hand", device_type="switch", mgmt_ip="192.0.2.5 ", check_port=22)
        r = self.a.post(self.url, self.data)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Device.objects.filter(mgmt_ip__contains="192.0.2.5").count(), 1)
        self.host.refresh_from_db()
        self.assertEqual((self.host.status, self.host.device_id), ("existing", existing.pk))

    def test_single_approval_still_creates_the_device(self):
        r = self.a.post(self.url, self.data)
        self.assertEqual(r.status_code, 302)
        dev_ = Device.objects.get(mgmt_ip="192.0.2.5")
        self.assertEqual(dev_.name, "192.0.2.5")
        self.host.refresh_from_db()
        self.assertEqual((self.host.status, self.host.device_id), ("approved", dev_.pk))


# ------------------------------------------------------------ 5. disabled device keeps stale rates
@override_settings(LINKEDEYE_TENANT=T, PROMETHEUS_URL="http://prom.example:9090")
class DisabledDeviceMetricsTests(TestCase):
    def setUp(self):
        self.u = User.objects.create_superuser("admin-dis", "a@example.com", "pw")
        self.c = Client()
        self.c.force_login(self.u)
        self.d = dev("sw-stale", prometheus_instance="sw1", uptime_seconds=86400, metrics_updated_at=timezone.now())
        Interface.objects.create(device=self.d, if_index=1, name="Gi1", oper_status="up", admin_status="up",
                                 speed_mbps=1000, in_mbps=800.0, out_mbps=300.0, utilization=80.0)

    def test_turning_monitoring_off_clears_the_rates(self):
        data = {"name": "sw-stale", "device_type": "switch", "mgmt_ip": "192.0.2.10", "check_port": 22, "site": "",
                "prometheus_instance": "sw1"}
        self.assertEqual(self.c.post(reverse("network:edit", args=[self.d.pk]), data).status_code, 302)
        self.d.refresh_from_db()
        self.assertFalse(self.d.enabled)
        i = Interface.objects.get(device=self.d)
        self.assertEqual((i.in_mbps, i.out_mbps, i.utilization), (None, None, None))
        self.assertIsNone(self.d.uptime_seconds)
        # the interface inventory is kept
        self.assertEqual(i.name, "Gi1")
        r = self.c.get(reverse("network:detail", args=[self.d.pk]))
        self.assertEqual(r.context["total_in"], None)
        self.assertEqual(r.context["total_out"], None)
        self.assertIn("Monitoring is disabled", r.context["metrics_msg"])

    def test_device_disabled_outside_the_form_still_hides_the_rates(self):
        """Devices disabled before this shipped, or through the Django admin (where `enabled` is
        editable), never had their rates cleared: the detail page must not present them as current."""
        Device.objects.filter(pk=self.d.pk).update(enabled=False, status=DeviceStatus.UNKNOWN)
        r = self.c.get(reverse("network:detail", args=[self.d.pk]))
        self.assertIsNone(r.context["total_in"])
        self.assertIsNone(r.context["total_out"])
        self.assertFalse(r.context["has_rates"])
        self.assertNotContains(r, "800 Mbps")
        self.assertIn("Monitoring is disabled", r.context["metrics_msg"])
        # the interface itself is still listed, and the stored row is not rewritten
        self.assertContains(r, "Gi1")
        self.assertEqual(Interface.objects.get(device=self.d).in_mbps, 800.0)

    def test_exports_of_a_disabled_device_carry_no_rates(self):
        """The CSV paths read the same stored rows as the detail page."""
        Device.objects.filter(pk=self.d.pk).update(enabled=False, status=DeviceStatus.UNKNOWN)
        r = self.c.get(reverse("network:interfaces_export", args=[self.d.pk]))
        row = r.content.decode().splitlines()[1].split(",")
        self.assertEqual(row[1], "Gi1")
        self.assertEqual(row[7:10], ["", "", ""])          # in_mbps, out_mbps, utilization_pct
        r = self.c.get(reverse("network:export"))
        sw = next(l for l in r.content.decode().splitlines() if l.startswith("sw-stale"))
        self.assertNotIn("86400", sw)                       # uptime_seconds
        # an enabled device still exports both
        Device.objects.filter(pk=self.d.pk).update(enabled=True)
        r = self.c.get(reverse("network:interfaces_export", args=[self.d.pk]))
        self.assertEqual(r.content.decode().splitlines()[1].split(",")[7:9], ["800.0", "300.0"])
        sw = next(l for l in self.c.get(reverse("network:export")).content.decode().splitlines() if l.startswith("sw-stale"))
        self.assertIn("86400", sw)

    def test_detail_of_a_disabled_device_shows_no_uptime(self):
        Device.objects.filter(pk=self.d.pk).update(enabled=False, status=DeviceStatus.UNKNOWN)
        r = self.c.get(reverse("network:detail", args=[self.d.pk]))
        self.assertIsNone(r.context["device"].uptime_seconds)
        self.assertEqual(Device.objects.get(pk=self.d.pk).uptime_seconds, 86400)   # stored row untouched

    def test_live_snapshot_of_a_disabled_device_carries_no_rates_or_uptime(self):
        """The live poll feeds the same detail page, so it must not resurrect the frozen traffic."""
        live = reverse("network:live", args=[self.d.pk])
        on = self.c.get(live).json()["values"]
        self.assertEqual((on["traffic_in"], on["traffic_out"]), ("800 Mbps", "300 Mbps"))
        self.assertEqual(on["uptime_short"], "1 day")
        Device.objects.filter(pk=self.d.pk).update(enabled=False, status=DeviceStatus.UNKNOWN)
        off = self.c.get(live).json()["values"]
        for key in ("traffic_in", "traffic_out", "legend_in", "legend_out", "uptime_short"):
            self.assertEqual(off[key], "–", key)
        self.assertEqual(off["uptime_display"], "")
        # nothing is written back
        self.assertEqual(Interface.objects.get(device=self.d).in_mbps, 800.0)
        self.assertEqual(Device.objects.get(pk=self.d.pk).uptime_seconds, 86400)

    def test_enabled_device_keeps_its_rates(self):
        data = {"name": "sw-stale", "device_type": "switch", "mgmt_ip": "192.0.2.10", "check_port": 22, "site": "",
                "prometheus_instance": "sw1", "enabled": "on"}
        with mock.patch("network.probe.tcp_probe", return_value=ok()):
            self.c.post(reverse("network:edit", args=[self.d.pk]), dict(data, model="C9300"))
        i = Interface.objects.get(device=self.d)
        self.assertEqual((i.in_mbps, i.out_mbps), (800.0, 300.0))


# ------------------------------------------------------------ 6. foreign-site device names on the run page
@override_settings(LINKEDEYE_TENANT=T, PROBE_ALLOWED_PRIVATE_CIDRS=["192.0.2.0/24"])
class DiscoveryRunDeviceScopeTests(TestCase):
    def setUp(self):
        self.a = Site.objects.create(name="Site A", code="a")
        Site.objects.create(name="Site B", code="b")
        self.secret = dev("secret-core-B", mgmt_ip="192.0.2.5", site="Site B")
        self.mine = dev("my-sw-A", mgmt_ip="192.0.2.6", site="Site A")
        self.run = DiscoveryRun.objects.create(tenant_key=T, cidr="192.0.2.0/29", ports="22", site="Site A")
        DiscoveredHost.objects.create(run=self.run, ip="192.0.2.5", open_ports=[22], status="existing", device=self.secret)
        DiscoveredHost.objects.create(run=self.run, ip="192.0.2.6", open_ports=[22], status="existing", device=self.mine)
        u = User.objects.create_user("viewer-a", password="pw")
        Membership.objects.create(user=u, site=self.a, role="viewer")
        self.c = Client()
        self.c.force_login(u)

    def test_run_page_hides_devices_the_viewer_cannot_see(self):
        self.assertEqual(self.c.get(reverse("network:detail", args=[self.secret.pk])).status_code, 404)
        r = self.c.get(reverse("network:discovery_run", args=[self.run.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, "secret-core-B")
        self.assertNotContains(r, reverse("network:detail", args=[self.secret.pk]))
        self.assertContains(r, "my-sw-A")
        self.assertContains(r, "Already a device")

    def test_tenant_wide_viewer_sees_both_names(self):
        c = Client()
        c.force_login(User.objects.create_user("viewer-all", password="pw", is_staff=True))
        r = c.get(reverse("network:discovery_run", args=[self.run.pk]))
        self.assertContains(r, "secret-core-B")
        self.assertContains(r, "my-sw-A")
