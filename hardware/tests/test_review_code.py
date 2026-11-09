"""Code / data / authz review: a poll never puts back what an administrator changed meanwhile, and no page runs more queries
because there is more data."""
from datetime import timedelta

from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from hardware import redfish, services
from hardware.models import BmcEndpoint, ControlRequest, HwComponent, HwEvent, HwReading
from hardware.tests.base import HwCase, with_key


@with_key
class StalePollNeverReverts(HwCase):
    """A poll reads the BMC for seconds with the endpoint loaded before it started. Saving the whole row afterwards would undo an
    administrator's edit made in between (switching polling off, changing the vendor, the alert limits, the pin)."""

    def edit_while_polling(self):
        stale = BmcEndpoint.objects.get(pk=self.ep_m.pk)        # what the poll loaded
        BmcEndpoint.objects.filter(pk=stale.pk).update(
            enabled=False, vendor_override="hpe", temp_warn_c=41.0, fan_crit_rpm=1234.0, pinned_sha256="b" * 64,
            poll_interval_s=300, alerts_enabled=False)
        return stale

    def assert_edit_survived(self):
        ep = BmcEndpoint.objects.get(pk=self.ep_m.pk)
        self.assertEqual((ep.enabled, ep.vendor_override, ep.temp_warn_c, ep.fan_crit_rpm, ep.pinned_sha256, ep.poll_interval_s,
                          ep.alerts_enabled), (False, "hpe", 41.0, 1234.0, "b" * 64, 300, False))
        return ep

    def test_a_successful_poll_keeps_the_edit_and_stores_its_own_state(self):
        stale = self.edit_while_polling()
        self.applied(ep=stale)
        ep = self.assert_edit_survived()
        self.assertEqual((ep.conn_state, ep.health), ("connected", "healthy"))
        self.assertIsNotNone(ep.last_ok_at)

    def test_a_failed_poll_keeps_the_edit_and_stores_its_own_state(self):
        stale = self.edit_while_polling()
        services.apply_failure(stale, redfish.Unreachable("no route"), timezone.now())
        ep = self.assert_edit_survived()
        self.assertEqual((ep.conn_state, ep.consecutive_failures), ("unreachable", 1))


@with_key
class QueryCounts(HwCase):
    """The number of queries a page runs must not depend on how much data it shows."""

    def grow(self, sensors, events, requests, start=0):
        ep = self.ep_m
        now = timezone.now()
        HwComponent.objects.bulk_create([
            HwComponent(tenant_key=ep.tenant_key, endpoint=ep, kind=("temperature", "fan", "psu", "voltage", "drive", "dimm", "cpu")[i % 7],
                        key=f"k{start + i}", name=f"part {start + i}", reading=20.0 + i % 30, unit="Cel", status="ok", position=start + i,
                        attrs={"model": "x", "serial": f"s{i}"}) for i in range(sensors)])
        for comp in HwComponent.objects.filter(endpoint=ep, key__startswith="k", kind__in=("temperature", "fan"))[:20]:
            HwReading.objects.bulk_create([HwReading(tenant_key=ep.tenant_key, component=comp, ts=now - timedelta(minutes=m), value=30.0 + m % 5)
                                           for m in range(0, 30, 3)])
        HwEvent.objects.bulk_create([HwEvent(tenant_key=ep.tenant_key, endpoint=ep, entry_id=f"e{start + i}", ts=now - timedelta(minutes=start + i),
                                             severity="info", message=f"event {i}") for i in range(events)])
        for i in range(requests):
            ControlRequest.objects.create(tenant_key=ep.tenant_key, endpoint=ep, action="power_on", reason="because", requested_by=self.mum_op,
                                          requested_by_name="mum-op", expires_at=now + timedelta(days=3))

    def more_servers(self, n, start=0):
        for i in range(n):
            dev = self.make_device(f"extra-{start + i}", "Mumbai", ip=f"198.51.100.{100 + (start + i) % 100}")
            self.make_endpoint(dev, host=f"192.0.2.{(start + i) % 200 + 1}")

    def count(self, client, url):
        client.get(url)
        with CaptureQueriesContext(connection) as ctx:
            resp = client.get(url)
        self.assertEqual(resp.status_code, 200, url)
        return len(ctx.captured_queries)

    def test_pages_run_the_same_queries_for_little_and_for_lots_of_data(self):
        c = self.login(self.mum_admin)
        dev = self.srv_m.pk
        self.applied()
        self.grow(10, 5, 2)
        self.more_servers(3)
        urls = {
            "tab": reverse("network:detail", args=[dev]) + "?tab=hardware",
            "tab_24h": reverse("network:detail", args=[dev]) + "?tab=hardware&hw_range=24h",
            "live": reverse("hardware:live", args=[dev]) + "?range=24h",
            "inventory": reverse("hardware:inventory", args=[dev]),
            "events": reverse("hardware:events", args=[dev]) + "?per=100",
            "csv": reverse("hardware:export", args=[dev]),
            "endpoints": reverse("hardware:endpoints") + "?per=100",
            "requests": reverse("hardware:requests") + "?state=all&per=100",
        }
        first = {name: self.count(c, url) for name, url in urls.items()}
        self.grow(80, 60, 25, start=1000)
        self.more_servers(30, start=50)
        for name, url in urls.items():
            self.assertEqual(self.count(c, url), first[name], f"{name}: the number of queries grows with the data")


@with_key
class ChartKeepsTheNewestPoints(HwCase):
    def test_when_there_are_more_readings_than_the_cap_the_oldest_are_dropped_not_the_newest(self):
        from unittest import mock

        from hardware import snapshot
        self.applied()
        now = timezone.now()
        comp = HwComponent.objects.filter(endpoint=self.ep_m, kind="temperature").first()
        HwReading.objects.filter(component__endpoint=self.ep_m).delete()
        HwReading.objects.bulk_create([HwReading(tenant_key=self.ep_m.tenant_key, component=comp, ts=now - timedelta(minutes=m), value=100.0 - m)
                                       for m in range(0, 50)])
        with mock.patch.object(snapshot, "READING_ROW_CAP", 10):
            data = snapshot.charts(self.ep_m, list(HwComponent.objects.filter(endpoint=self.ep_m)), "1h", now)
        series = {s["name"]: s["values"] for s in data["temp"]["series"]}[comp.name]
        last = [v for v in series if v is not None][-1]
        self.assertGreater(last, 95)                       # the newest minutes (value near 100), not the oldest (near 50)
        self.assertEqual(sum(1 for v in series if v is not None) > 0, True)


@with_key
class EventsAfterCommit(HwCase):
    def test_the_event_listeners_run_after_the_poll_is_stored_not_inside_its_transaction(self):
        from unittest import mock

        from hardware.tests import fixtures
        self.applied()                                          # a healthy first poll: remembered, not announced
        depth = len(connection.savepoint_ids)                   # the test's own transaction
        seen = []
        d = fixtures.dell_idrac9()
        d["/redfish/v1/Chassis/System.Embedded.1/Power"]["PowerSupplies"][1]["Status"] = {"Health": "Critical", "State": "Enabled"}
        with mock.patch.object(services, "_emit", side_effect=lambda *a, **k: seen.append(len(connection.savepoint_ids))):
            out = self.applied(docs=d)
        self.assertEqual(out["transition"], "critical")
        self.assertEqual(seen, [depth])                         # one event, outside the transaction that stored the poll
        self.assertEqual(BmcEndpoint.objects.get(pk=self.ep_m.pk).alert_state, "critical")
