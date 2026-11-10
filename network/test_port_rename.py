"""A port whose ifAlias / ifName / ifDescr changes inside the window keeps its whole graph history.

snmp_exporter's if_mib module puts the lookup labels on every counter, so a renamed port has two series for the same
instance and ifIndex: the old one (until the rate window lets it go) and the new one. The port page must show both
segments, not only the series that happens to sort first.
"""
import re

from django.test import SimpleTestCase

from network import portmetrics as pm

END = 1_800_000_000.0
STEP = 60
N = 61
STAMPS = [END - (N - 1 - i) * STEP for i in range(N)]
RENAME = END - 600            # 10 minutes before the end
OLD_UNTIL = RENAME + 300      # the 5m rate window keeps the old series alive a little longer


def split_rows(tag, value, old_labels, new_labels):
    """Two series with the same m tag, split at the rename, in Prometheus' label-set order."""
    base = {"instance": "sw1", "ifIndex": "7", "m": tag}
    old = {"metric": {**base, **old_labels}, "values": [[t, float(value)] for t in STAMPS if t <= OLD_UNTIL]}
    new = {"metric": {**base, **new_labels}, "values": [[t, float(value)] for t in STAMPS if t >= RENAME]}
    return sorted([old, new], key=lambda r: sorted(r["metric"].items()))


def non_null(chart, name):
    series = next(s for s in chart["series"] if s["name"] == name)
    return sum(v is not None for v in series["values"])


class RenamedPortTests(SimpleTestCase):
    RENAMES = (
        ({}, {"ifAlias": "uplink-core1"}),                  # first description: the new series sorts first
        ({"ifAlias": "b-old"}, {"ifAlias": "a-new"}),
        ({"ifAlias": "a-old"}, {"ifAlias": "b-new"}),       # the old series sorts first: the graph used to stop early
    )

    def test_traffic_and_utilisation_keep_the_whole_window(self):
        for old, new in self.RENAMES:
            with self.subTest(old=old, new=new):
                rows = pm.normalise_range(split_rows("in", 125e6, old, new) + split_rows("out", 62.5e6, old, new))
                traffic, util = pm.traffic_charts(rows, 3600, 10000)
                self.assertEqual(non_null(traffic, "Inbound"), N)
                self.assertEqual(non_null(traffic, "Outbound"), N)
                self.assertEqual(non_null(util, "Utilisation"), N)
                # the overlap is the same counter seen twice: never added up
                self.assertEqual(max(v for v in traffic["series"][0]["values"] if v is not None), 1000.0)

    def test_packets_and_errors_keep_the_whole_window(self):
        old, new = self.RENAMES[0]
        rows = []
        for tag, _metric in pm.PACKET_TAGS:
            rows += split_rows(tag, 10, old, new)
        packets = pm.packets_chart(pm.normalise_range(rows), 3600)
        self.assertEqual(non_null(packets, "Unicast"), N)
        self.assertEqual(max(v for v in packets["series"][0]["values"] if v is not None), 20.0)
        rows = []
        for tag, _metric in pm.ERROR_TAGS:
            rows += split_rows(tag, 2, old, new)
        errors = pm.errors_chart(pm.normalise_range(rows), 3600)
        for name in ("In errors", "Out errors", "In discards", "Out discards"):
            self.assertEqual(non_null(errors, name), N, name)

    def test_queries_fold_the_lookup_labels_away(self):
        """Prometheus answers one series per port and tag: every rate() is aggregated by instance and ifIndex."""
        sel = 'instance="sw1",ifIndex="7"'
        for q in (pm.traffic_query(sel), pm.packets_query(sel), pm.errors_query(sel)):
            rates = q.count("rate(")
            self.assertGreater(rates, 0)
            self.assertEqual(len(re.findall(r"max by \(instance, ifIndex\) \(rate\(", q)), rates, q)
