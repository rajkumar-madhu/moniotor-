"""Review additions for the per-port page: the live poll's Prometheus budget, query counts of the pages that gained port
links, the stored port facts across repeated collections, and no port link on a virtual machine. Prometheus is mocked."""
from unittest import mock

from django.core.cache import cache
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from core.models import Event

from . import jobs, metrics, portmetrics
from .models import Interface
from . import test_port_detail as base
from .test_port_detail import INSTANT, PROM, RANGE, fake_instant, fake_range
from .tests import T, dev, fake_query, prom_row


class LivePollBudgetTests(base.PortCase):
    """The docs promise a bounded number of Prometheus calls per live poll."""

    def poll(self, **params):
        cache.clear()
        url = reverse("network:interface_live", args=[self.d.pk, 1])
        with mock.patch(INSTANT, side_effect=fake_instant) as q, mock.patch(RANGE, side_effect=fake_range) as qr:
            r = self.c.get(url, params)
        self.assertEqual(r.status_code, 200)
        return q.call_count, qr.call_count

    def test_a_poll_asks_no_instant_query_and_a_bounded_number_of_range_queries(self):
        for key in metrics.RANGES:
            instant, ranged = self.poll(range=key)
            self.assertEqual(instant, 0, key)
            # traffic, packets, errors, oper state (+ optical power once the page found sensors) and, away from the
            # 24 h range, the three 24 h sparkline queries
            self.assertLessEqual(ranged, 4 + 1 + (0 if key == "24h" else 3), key)

    def test_a_second_poll_inside_the_cache_window_asks_nothing(self):
        url = reverse("network:interface_live", args=[self.d.pk, 1])
        with mock.patch(INSTANT, side_effect=fake_instant), mock.patch(RANGE, side_effect=fake_range):
            self.c.get(url, {"range": "6h"})
        with mock.patch(INSTANT, side_effect=fake_instant) as q, mock.patch(RANGE, side_effect=fake_range) as qr:
            self.c.get(url, {"range": "6h"})
        self.assertEqual(q.call_count + qr.call_count, 0)


@override_settings(LINKEDEYE_TENANT=T, PROMETHEUS_URL=PROM)
class PagesWithPortLinksStayConstantTests(base.PortCase):
    def make(self, name, ip, n, links):
        from architecture.models import Link
        d = dev(name, mgmt_ip=ip, site="Mumbai (NSE)", prometheus_instance="sw1")
        Interface.objects.bulk_create([Interface(device=d, if_index=i, name=f"Gi1/0/{i}", utilization=float(i % 100), oper_status="up",
                                                 if_type="Ethernet", updated_at=timezone.now()) for i in range(1, n + 1)])
        for i in range(1, links + 1):
            peer = dev(f"{name}-peer{i}", mgmt_ip=f"198.51.{d.pk % 250}.{i}", site="Mumbai (NSE)")
            Link.objects.create(tenant_key=T, a_device=d, a_port=f"Gi1/0/{i}", b_device=peer, b_port="Eth1")
        for i in range(1, min(n, links * 2) + 1):
            Event.objects.create(tenant_key=T, source="network", title=f"Interface x Gi1/0/{i} is down", severity="warning",
                                 object_type="network.device", object_id=str(d.pk), fingerprint=f"network:{d.pk}:if{i}")
        return d

    def count(self, d, tab):
        cache.clear()
        with mock.patch(INSTANT, side_effect=fake_instant), mock.patch(RANGE, side_effect=fake_range):
            self.c.get(reverse("network:detail", args=[d.pk]), {"tab": tab})   # warm anything lazy
            with CaptureQueriesContext(connection) as ctx:
                r = self.c.get(reverse("network:detail", args=[d.pk]), {"tab": tab})
        self.assertEqual(r.status_code, 200)
        return len(ctx), r

    def test_neighbours_events_and_interfaces_tabs_do_not_grow_with_ports_links_or_events(self):
        small, big = self.make("tiny", "192.0.2.111", 4, 1), self.make("huge", "192.0.2.112", 300, 40)
        for tab in ("neighbours", "events", "interfaces"):
            (n_small, _), (n_big, r) = self.count(small, tab), self.count(big, tab)
            self.assertEqual(n_small, n_big, tab)
        self.assertContains(r, reverse("network:interface", args=[big.pk, 1]))

    def test_no_port_link_on_a_virtual_machine(self):
        vm = dev("vm-01", mgmt_ip="192.0.2.113", device_type="virtual_machine", site="Mumbai (NSE)")
        Interface.objects.create(device=vm, if_index=1, name="eth0", oper_status="up", updated_at=timezone.now())
        Event.objects.create(tenant_key=T, source="network", title="Interface x eth0 is down", severity="warning",
                             object_type="network.device", object_id=str(vm.pk), fingerprint=f"network:{vm.pk}:if1")
        with mock.patch(RANGE, return_value=[]), mock.patch(INSTANT, return_value=[]):
            r = self.c.get(reverse("network:detail", args=[vm.pk]), {"tab": "overview"})
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, "/interfaces/1/")
        self.assertEqual(self.c.get(reverse("network:interface", args=[vm.pk, 1])).status_code, 404)


class FactsAcrossCollectionsTests(base.PortCase):
    """Repeated collections keep the stored port facts stable, and each fact clears on its own."""
    collect = base.PortFactsCollectionTests.collect
    facts_query = staticmethod(base.PortFactsCollectionTests.facts_query)

    def test_collecting_twice_is_idempotent(self):
        d = dev("sw-idem", prometheus_instance="sw1", mgmt_ip="192.0.2.60")
        self.collect(self.facts_query)
        first = list(d.interfaces.order_by("if_index").values("if_index", "mtu", "mac", "duplex", "last_change_at"))
        self.collect(self.facts_query)
        second = list(d.interfaces.order_by("if_index").values("if_index", "mtu", "mac", "duplex", "last_change_at"))
        self.assertEqual(d.interfaces.count(), 2)
        for a, b in zip(first, second):
            self.assertEqual({k: v for k, v in a.items() if k != "last_change_at"}, {k: v for k, v in b.items() if k != "last_change_at"})
        # the derived timestamp may move by the seconds between two runs, never by more
        self.assertLess(abs((first[0]["last_change_at"] - second[0]["last_change_at"]).total_seconds()), 30)

    def test_one_vanished_series_clears_only_its_own_fact(self):
        d = dev("sw-part", prometheus_instance="sw1", mgmt_ip="192.0.2.61")
        self.collect(self.facts_query)

        def no_mac(q):
            return [] if "ifPhysAddress" in q else self.facts_query(q)
        self.collect(no_mac)
        g1 = d.interfaces.get(if_index=1)
        self.assertEqual((g1.mtu, g1.mac, g1.duplex), (9216, "", "full"))
        self.assertIsNotNone(g1.last_change_at)

    def test_unusable_facts_are_not_reported(self):
        d = dev("sw-bad", prometheus_instance="sw1", mgmt_ip="192.0.2.62")

        def bad(q):
            base = {"instance": "sw1"}
            if "ifMtu" in q:
                return [prom_row(0, ifIndex="1", **base), prom_row(float("nan"), ifIndex="2", **base)]
            if "ifPhysAddress" in q:
                return [prom_row(1, ifIndex="1", ifPhysAddress="zz", **base)]
            if "dot3StatsDuplexStatus" in q:   # a state set: the inactive state first, then the active one
                return [prom_row(0, dot3StatsIndex="1", dot3StatsDuplexStatus="halfDuplex", **base),
                        prom_row(1, dot3StatsIndex="1", dot3StatsDuplexStatus="fullDuplex", **base),
                        prom_row(1, dot3StatsIndex="99999999999999", dot3StatsDuplexStatus="fullDuplex", **base)]
            if "ifLastChange" in q:
                return [prom_row(-5, ifIndex="1", **base)]
            return fake_query(q)
        self.collect(bad)
        g1, g2 = d.interfaces.order_by("if_index")
        self.assertEqual((g1.mtu, g1.mac, g1.duplex), (None, "", "full"))
        self.assertEqual((g2.mtu, g2.duplex), (None, ""))
        self.assertIsNone(g1.last_change_at)   # a negative tick count is no timestamp
        self.assertEqual(d.interfaces.count(), 2)

    def test_the_job_asks_four_more_batched_queries_not_one_per_port(self):
        dev("sw-batch", prometheus_instance="sw1", mgmt_ip="192.0.2.63")
        with mock.patch(INSTANT, side_effect=self.facts_query) as q:
            jobs.collect_metrics()
        asked = " ".join(c.args[0] for c in q.call_args_list)
        for series in ("ifMtu", "ifPhysAddress", "dot3StatsDuplexStatus", "ifLastChange"):
            self.assertEqual(asked.count(series + "{"), 1, series)
        self.assertEqual(q.call_count, len(metrics.queries("x")))


@override_settings(LINKEDEYE_TENANT=T)
class SeedPortsTests(base.PortCase):
    def setUp(self):
        super().setUp()
        type(self.d).objects.filter(pk=self.d.pk).update(name="other-sw")   # the fixture switch carries the demo name
        Interface.objects.filter(device=self.d).delete()

    def run_cmd(self, *args):
        from io import StringIO

        from django.core.management import call_command
        out = StringIO()
        call_command("seed_ports", *args, stdout=out)
        return out.getvalue()

    def test_only_an_example_device_is_touched_and_the_seed_is_idempotent_and_removable(self):
        real = dev("core-sw-01x", mgmt_ip="192.0.2.77")
        self.assertIn("Nothing changed", self.run_cmd())          # no example switch yet
        example = dev("core-sw-01", mgmt_ip="192.0.2.10", tags="example, core", site="Mumbai (NSE)")
        self.run_cmd()
        self.run_cmd()
        self.assertEqual(example.interfaces.count(), 52)
        self.assertFalse(real.interfaces.exists())
        port = example.interfaces.get(if_index=1)
        self.assertTrue(port.mtu and port.mac and port.last_change_at)
        self.assertEqual(self.c.get(reverse("network:interface", args=[example.pk, 1])).status_code, 200)
        self.run_cmd("--remove")
        self.assertFalse(example.interfaces.exists())

    def test_a_device_named_like_the_example_but_not_tagged_is_left_alone(self):
        real = dev("core-sw-01", mgmt_ip="192.0.2.78", tags="prod")
        self.assertIn("Nothing changed", self.run_cmd())
        self.assertFalse(real.interfaces.exists())
