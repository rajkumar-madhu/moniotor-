from unittest import mock

from django.test import override_settings
from django.urls import reverse

from architecture import lldp
from architecture.models import Link
from core import prometheus
from core.models import AuditEvent

from .fixtures import Base, dev, link

PROM = override_settings(PROMETHEUS_URL="http://prometheus.test:9090")


def s(**labels):
    return {"metric": labels, "value": 1.0}


def series(rows):
    """rows: (instance, local num, remote index, remote sysname, remote port id, remote port desc)"""
    out = {k: [] for k in ("sysname", "portid", "portdesc", "locdesc", "ifname")}
    for inst, num, idx, name, pid, pdesc in rows:
        base = {"instance": inst, "lldpRemLocalPortNum": num, "lldpRemIndex": idx, "lldpRemTimeMark": "0"}
        out["sysname"].append(s(**base, lldpRemSysName=name))
        if pid:
            out["portid"].append(s(**base, lldpRemPortId=pid))
        if pdesc:
            out["portdesc"].append(s(**base, lldpRemPortDesc=pdesc))
        out["ifname"].append(s(instance=inst, ifIndex=num, ifName=f"Ethernet1/{num}"))
    return out


class ParseTests(Base):
    def setUp(self):
        super().setUp()
        Device_ = type(self.spine)
        Device_.objects.filter(pk=self.spine.pk).update(prometheus_instance="192.0.2.251", mgmt_ip="192.0.2.251")
        Device_.objects.filter(pk=self.leaf.pk).update(prometheus_instance="dc1-leaf-01.corp.example", mgmt_ip="192.0.2.51")
        self.devices = list(Device_.objects.all())

    def test_both_ends_report_the_same_cable_once(self):
        data = series([("192.0.2.251", "1", "1", "DC1-LEAF-01.corp.example.com", "Eth1/49", "Ethernet1/49"),
                       ("dc1-leaf-01.corp.example", "49", "1", "dc1-spine-01", "Eth1/1", "")])
        data["locdesc"].append(s(instance="192.0.2.251", lldpLocPortNum="1", lldpLocPortDesc="Ethernet1/1"))
        res = lldp.parse(data, self.devices)
        self.assertEqual(len(res.suggestions), 1)
        sug = res.suggestions[0]
        self.assertEqual({sug.a_name, sug.b_name}, {"dc1-spine-01", "dc1-leaf-01"})
        self.assertEqual(res.rows, 2)

    def test_unknown_remote_is_counted_not_listed(self):
        res = lldp.parse(series([("192.0.2.251", "3", "1", "mystery-box", "x", "")]), self.devices)
        self.assertEqual((len(res.suggestions), res.unknown), (0, 1))

    def test_ambiguous_names_are_not_guessed(self):
        dev("dc1-leaf-01x", site="Mumbai")
        from network.models import Device
        Device.objects.filter(name="dc1-leaf-01x").update(name="DC1-LEAF-01.alt", prometheus_instance="")
        # "dc1-leaf-01" is now also the short name of another device: two candidates -> unknown
        res = lldp.parse(series([("192.0.2.251", "3", "1", "dc1-leaf-01", "x", "")]), list(Device.objects.all()))
        self.assertEqual(len(res.suggestions), 0)

    def test_self_and_known_links_are_skipped(self):
        known = {(min(self.spine.pk, self.leaf.pk), "eth1/1" if self.spine.pk < self.leaf.pk else "eth1/49", max(self.spine.pk, self.leaf.pk),
                  "eth1/49" if self.spine.pk < self.leaf.pk else "eth1/1")}
        data = series([("192.0.2.251", "1", "1", "dc1-leaf-01", "Eth1/49", "Eth1/49"), ("192.0.2.251", "2", "1", "dc1-spine-01", "x", "")])
        res = lldp.parse(data, self.devices, known)
        self.assertEqual((len(res.suggestions), res.known), (0, 1))
        self.assertEqual(res.unknown, 1)       # the neighbour that is the device itself

    def test_hostile_strings(self):
        evil = "<script>alert(1)</script>\x00\x1b[31m" + "A" * 500
        data = series([("192.0.2.251", "1", "1", "dc1-leaf-01", evil, evil)])
        data["locdesc"].append(s(instance="192.0.2.251", lldpLocPortNum="1", lldpLocPortDesc=evil))
        res = lldp.parse(data, self.devices)
        sug = res.suggestions[0]
        for text in (sug.a_port, sug.b_port):
            self.assertLessEqual(len(text), lldp.MAX_TEXT)
            self.assertNotIn("\x00", text)
            self.assertNotIn("\x1b", text)
        self.assertEqual(lldp.clean_text("a\r\nb\tc\x07"), "a b c")

    def test_suggestion_limit(self):
        from network.models import Device
        rows = []
        for i in range(lldp.MAX_SUGGESTIONS + 20):
            rows.append(("192.0.2.251", str(i + 1), "1", "dc1-leaf-01", f"p{i}", ""))
        res = lldp.parse(series(rows), self.devices)
        self.assertEqual(len(res.suggestions), lldp.MAX_SUGGESTIONS)


class FindTests(Base):
    def test_not_configured(self):
        self.assertIn("not connected", lldp.find([]).error)

    @PROM
    def test_error_and_empty(self):
        with mock.patch.object(prometheus, "query", side_effect=prometheus.PrometheusError("nope")):
            self.assertIn("could not be read", lldp.find([]).error)
        with mock.patch.object(prometheus, "query", return_value=[]):
            self.assertIn("lldpRemSysName", lldp.find([]).error)

    @PROM
    def test_optional_queries_may_fail(self):
        def q(promql, *a, **k):
            if promql == "lldpRemSysName":
                return [s(instance=self.spine.name, lldpRemLocalPortNum="1", lldpRemIndex="1", lldpRemSysName="y")]
            raise prometheus.PrometheusError("x")
        with mock.patch.object(prometheus, "query", side_effect=q):
            res = lldp.find([self.spine])
        self.assertEqual((res.error, res.unknown), ("", 1))


@PROM
class SuggestViewTests(Base):
    def setUp(self):
        super().setUp()
        from network.models import Device
        Device.objects.filter(pk=self.spine.pk).update(prometheus_instance="192.0.2.251")
        self.rows = series([("192.0.2.251", "1", "1", "dc1-leaf-01", "Eth1/49", "Eth1/49")])

    def patched(self):
        def q(promql, *a, **k):
            return {"lldpRemSysName": self.rows["sysname"], "lldpRemPortId": self.rows["portid"], "lldpRemPortDesc": self.rows["portdesc"],
                    "lldpLocPortDesc": self.rows["locdesc"], "ifName": self.rows["ifname"]}.get(promql, [{"metric": {}, "value": 1.0}])
        return mock.patch.object(prometheus, "query", side_effect=q)

    def test_listing_creates_nothing(self):
        self.login(self.operator)
        with self.patched():
            r = self.client.get(reverse("architecture:suggest"), {"find": "1"})
        self.assertEqual(len(r.context["result"].suggestions), 1)
        self.assertEqual(Link.objects.count(), 0)

    def test_accept_creates_unconfirmed_lldp_link_and_audits(self):
        self.login(self.operator)
        with self.patched():
            tok = self.client.get(reverse("architecture:suggest"), {"find": "1"}).context["result"].suggestions[0].token
            r = self.client.post(reverse("architecture:suggest"), {"pick": [tok, "forged-token"]})
        self.assertEqual(r.status_code, 302)
        l = Link.objects.get()
        self.assertEqual((l.origin, l.confirmed), ("lldp", False))
        self.assertTrue(AuditEvent.objects.filter(action="create", object_type="architecture.link", details__via="lldp").exists())

    def test_forged_token_creates_nothing(self):
        self.login(self.operator)
        with self.patched():
            self.client.post(reverse("architecture:suggest"), {"pick": ["0123456789abcdef"]})
        self.assertEqual(Link.objects.count(), 0)

    def test_viewer_refused(self):
        self.login(self.viewer)
        self.assertEqual(self.client.get(reverse("architecture:suggest")).status_code, 403)
        self.client.post(reverse("architecture:suggest"), {"pick": ["x"]})
        self.assertEqual(Link.objects.count(), 0)

    def test_site_operator_cannot_accept_for_other_site(self):
        from network.models import Device
        Device.objects.filter(pk=self.leaf.pk).update(site="London")
        self.login(self.mum_op)
        with self.patched():
            r = self.client.get(reverse("architecture:suggest"), {"find": "1"})
            self.assertEqual(r.context["result"].suggestions, [])     # London device is not even visible
