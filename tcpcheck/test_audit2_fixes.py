"""Audit round 2 regressions: scheduler drift, unbounded / non-ASCII numeric parameters, site-scoped Related panel
and the delete redirect from a check's own pages."""
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import Membership, Site

from . import services
from .models import TcpCheck, TcpResult
from .probe import ProbeResult

LOCAL = override_settings(PROBE_ALLOWED_PRIVATE_CIDRS=["127.0.0.0/8", "10.0.0.0/8"])


def make_check(**kw):
    defaults = {"name": "Probe", "host": "127.0.0.1", "port": 1, "site": "All Sites", "timeout_seconds": 2,
                "degraded_ms": 500}
    defaults.update(kw)
    return TcpCheck.objects.create(**defaults)


class SchedulerDriftTests(TestCase):
    def test_last_checked_at_is_the_pass_time_not_the_end_of_the_batch(self):
        c = make_check(interval_seconds=60)
        t0 = timezone.now().replace(microsecond=0)
        ok = ProbeResult(ok=True, response_ms=1.0, ip="127.0.0.1")
        # the pool takes a while (slow targets, timeouts): the clock has moved on by the time results are stored
        with mock.patch.object(services, "tcp_probe", return_value=ok), \
             mock.patch.object(services.timezone, "now", return_value=t0 + timedelta(seconds=5)):
            self.assertIn("checked 1", services.run_due(now=t0))
        c.refresh_from_db()
        self.assertEqual(c.last_checked_at, t0)
        self.assertEqual(TcpResult.objects.get(tcp_check=c).checked_at, t0)
        # so the pass one interval later runs it again instead of waiting for the following tick
        self.assertTrue(c.is_due(t0 + timedelta(seconds=60)))
        self.assertFalse(c.is_due(t0 + timedelta(seconds=45)))


class NumericParamTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("admin", "a@x", "x")
        self.client.force_login(self.admin)
        self.c = make_check(name="ssh", port=22)

    def test_sla_update_rejects_huge_object_id(self):
        r = self.client.post(reverse("tcpcheck:sla"), {"kind": "tcp", "object_id": "99999999999999999999999", "target": "99"})
        self.assertEqual(r.status_code, 302)
        msgs = [str(m) for m in r.wsgi_request._messages]
        self.assertTrue(any("SLA target not saved" in m for m in msgs), msgs)

    def test_port_filter_ignores_non_ascii_and_out_of_range_values(self):
        for value in ("²", "99999999999999999999999", "0", "70000", "-1"):
            r = self.client.get(reverse("tcpcheck:index") + "?port=" + value)
            self.assertEqual(r.status_code, 200, value)
            self.assertEqual([x.pk for x in r.context["page"]], [self.c.pk], value)   # filter ignored
            r = self.client.get(reverse("tcpcheck:export") + "?port=" + value)
            self.assertEqual(r.status_code, 200, value)
            self.assertIn("ssh", r.content.decode())
        r = self.client.post(reverse("tcpcheck:run_all") + "?port=²", {}, HTTP_ACCEPT="application/json")
        self.assertEqual(r.status_code, 200)

    def test_port_filter_still_filters(self):
        make_check(name="https", port=443)
        r = self.client.get(reverse("tcpcheck:index") + "?port=443")
        self.assertEqual([x.name for x in r.context["page"]], ["https"])


@LOCAL
class RelatedPanelScopeTests(TestCase):
    def setUp(self):
        from cmdb.models import ConfigurationItem
        from network.models import Device
        self.a = Site.objects.create(name="A", code="a")
        Site.objects.create(name="B", code="b")
        self.check = make_check(name="db", host="10.0.0.5", port=5432, site="All Sites")
        Device.objects.create(name="sw-b", mgmt_ip="10.0.0.5", site="B")
        ConfigurationItem.objects.create(name="10.0.0.5", site="B")

    def test_related_hides_records_of_sites_the_viewer_cannot_see(self):
        v = User.objects.create_user("v", password="x")
        Membership.objects.create(user=v, site=self.a, role="viewer")
        self.client.force_login(v)
        r = self.client.get(reverse("tcpcheck:detail", args=[self.check.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["related"], [])
        self.assertNotContains(r, "sw-b")

    def test_related_shown_to_tenant_wide_viewer(self):
        self.client.force_login(User.objects.create_user("all", password="x"))
        r = self.client.get(reverse("tcpcheck:detail", args=[self.check.pk]))
        self.assertEqual({x["kind"] for x in r.context["related"]}, {"Network device", "Configuration item"})


class DeleteRedirectTests(TestCase):
    def setUp(self):
        self.client.force_login(User.objects.create_superuser("admin", "a@x", "x"))

    def test_delete_from_own_edit_page_lands_on_the_list(self):
        for i, suffix in enumerate(("edit/", "?page=2", "")):     # edit page, detail page with a query, detail page
            c = make_check(name=f"c{i}")
            nxt = reverse("tcpcheck:detail", args=[c.pk]) + suffix
            r = self.client.post(reverse("tcpcheck:delete", args=[c.pk]), {"next": nxt})
            self.assertEqual(r.status_code, 302)
            self.assertEqual(r["Location"], reverse("tcpcheck:index"), nxt)
            self.assertFalse(TcpCheck.objects.filter(pk=c.pk).exists())

    def test_delete_from_another_page_keeps_next(self):
        c, other = make_check(name="one"), make_check(name="two")
        nxt = reverse("tcpcheck:detail", args=[other.pk])
        r = self.client.post(reverse("tcpcheck:delete", args=[c.pk]), {"next": nxt})
        self.assertEqual(r["Location"], nxt)
        r = self.client.post(reverse("tcpcheck:delete", args=[other.pk]), {"next": reverse("tcpcheck:index") + "?page=2"})
        self.assertEqual(r["Location"], reverse("tcpcheck:index") + "?page=2")
