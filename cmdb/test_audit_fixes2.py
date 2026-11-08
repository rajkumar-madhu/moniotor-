"""Regression tests for the second CMDB audit round:

* the heatmap must aggregate probe results in SQL and read a bounded number of rows,
* licence over-allocation on the software list must not depend on the viewer's sites,
* a relationship belongs to its source CI's site, so creating one needs operate there.
"""
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from core.models import Membership, Site
from domainmonitor.models import Domain, DomainCheck
from urlchecker.models import UrlCheck, UrlResult

from . import heatmap as hm
from .models import ConfigurationItem, License, Relationship, Software

T = "default"


def ci(name, ci_type="server", **kw):
    kw.setdefault("tenant_key", T)
    return ConfigurationItem.objects.create(name=name, ci_type=ci_type, **kw)


# ------------------------------------------------------------ 1. bounded heatmap probe aggregation
@override_settings(LINKEDEYE_TENANT=T, PROMETHEUS_URL="")
class HeatmapProbeSamplesTests(TestCase):
    def setUp(self):
        self.check = UrlCheck.objects.create(tenant_key=T, name="OMS URL", url="https://oms.example/", degraded_ms=1000)
        self.domain = Domain.objects.create(tenant_key=T, site_key="", hostname="oms.example")
        self.now = timezone.now()
        for i in range(19):
            UrlResult.objects.create(url_check=self.check, checked_at=self.now - timedelta(minutes=i + 1),
                                     status="online", http_status=200, response_ms=100.0 * (i + 1))
        UrlResult.objects.create(url_check=self.check, checked_at=self.now - timedelta(minutes=30), status="down")
        for i in range(9):
            DomainCheck.objects.create(domain=self.domain, checked_at=self.now - timedelta(minutes=i + 1), up=True,
                                       http_status=200, response_ms=50.0)
        DomainCheck.objects.create(domain=self.domain, checked_at=self.now - timedelta(minutes=10), up=False,
                                   http_status=503, response_ms=None)

    def since(self):
        return self.now - timedelta(hours=1)

    def test_counts_are_aggregated_in_sql_and_the_sample_is_bounded(self):
        with CaptureQueriesContext(connection) as ctx:
            out = hm.probe_samples({self.check.pk}, {self.domain.pk}, self.since())
        sql = " ".join(q["sql"].lower() for q in ctx.captured_queries)
        self.assertIn("group by", sql)   # the counting happens in the database
        self.assertIn("limit", sql)      # the response times read for the p95 are capped
        url = out[("url", self.check.pk)]
        self.assertEqual((url.total, url.up, url.errors), (20, 19, 1))
        m = url.metrics()
        self.assertAlmostEqual(m["availability"], 95.0)
        self.assertAlmostEqual(m["errors"], 5.0)
        self.assertAlmostEqual(m["response"], 1000.0)
        self.assertEqual(m["latency"], 1900.0)
        dom = out[("domain", self.domain.pk)]
        self.assertEqual((dom.total, dom.up, dom.errors), (10, 9, 1))
        self.assertAlmostEqual(dom.metrics()["availability"], 90.0)
        self.assertAlmostEqual(dom.metrics()["response"], 50.0)

    def test_latency_sample_never_loads_the_whole_window(self):
        with mock.patch.object(hm, "PROBE_LATENCY_SAMPLES", 5):
            out = hm.probe_samples({self.check.pk}, {self.domain.pk}, self.since())
        url = out[("url", self.check.pk)]
        # the counts stay exact (they come from SQL) while the Python-side sample is capped
        self.assertEqual(url.total, 20)
        self.assertLessEqual(len(url.ms), 5)
        self.assertIsNotNone(url.metrics()["latency"])
        self.assertAlmostEqual(url.metrics()["response"], 1000.0)   # the average is not sampled

    def test_checks_without_results_are_absent(self):
        other = UrlCheck.objects.create(tenant_key=T, name="Other", url="https://other.example/")
        out = hm.probe_samples({self.check.pk, other.pk}, set(), self.since())
        self.assertNotIn(("url", other.pk), out)


@override_settings(LINKEDEYE_TENANT=T, PROMETHEUS_URL="")
class HeatmapProbeRulesTests(TestCase):
    """The SQL aggregation must keep the availability / error / response-time rules of the row-by-row
    code it replaced: only available rows contribute response times, and an error is "no HTTP answer at
    all, or 5xx" for URL checks and "not up, or 5xx" for domains."""

    def setUp(self):
        self.now = timezone.now()
        self.check = UrlCheck.objects.create(tenant_key=T, name="Rules URL", url="https://rules.example/")
        self.domain = Domain.objects.create(tenant_key=T, site_key="", hostname="rules.example")
        # (status, http_status, response_ms) newest first
        for i, (status, http, ms) in enumerate([
                ("online", 200, 100.0), ("online", 200, 200.0), ("online", 200, 300.0), ("online", 200, 400.0),
                ("degraded", 200, 500.0),        # slow but answered: available, no error
                ("degraded", 503, 600.0),        # answered with a 5xx: available, an error
                ("degraded", 500, 700.0),        # likewise
                ("down", None, None),            # no answer at all: unavailable, an error
                ("down", 404, 9999.0),           # content/availability failure: unavailable, NOT an error
                ("down", 503, 8888.0)]):         # unavailable, an error
            UrlResult.objects.create(url_check=self.check, checked_at=self.now - timedelta(minutes=i + 1),
                                     status=status, http_status=http, response_ms=ms)
        for i, (up, http, ms) in enumerate([
                (True, 200, 50.0), (True, 200, 70.0),
                (True, 503, 90.0),               # up but 5xx: available, an error
                (False, None, 5555.0),           # down: an error, its time does not count
                (False, 200, 4444.0)]):          # down although answered: an error
            DomainCheck.objects.create(domain=self.domain, checked_at=self.now - timedelta(minutes=i + 1),
                                       up=up, http_status=http, response_ms=ms)

    def out(self):
        return hm.probe_samples({self.check.pk}, {self.domain.pk}, self.now - timedelta(hours=1))

    def test_url_rules(self):
        s = self.out()[("url", self.check.pk)]
        self.assertEqual((s.total, s.up, s.errors), (10, 7, 4))
        m = s.metrics()
        self.assertAlmostEqual(m["response"], 400.0)        # (100..700) / 7: unavailable rows excluded
        self.assertEqual(m["latency"], 700.0)               # p95 of the available response times only
        self.assertEqual(sorted(s.ms), [100.0, 200.0, 300.0, 400.0, 500.0, 600.0, 700.0])

    def test_domain_rules(self):
        s = self.out()[("domain", self.domain.pk)]
        self.assertEqual((s.total, s.up, s.errors), (5, 3, 3))
        m = s.metrics()
        self.assertAlmostEqual(m["response"], 70.0)
        self.assertEqual(m["latency"], 90.0)

    def test_the_cap_keeps_the_newest_response_times(self):
        with mock.patch.object(hm, "PROBE_LATENCY_SAMPLES", 3):
            s = self.out()[("url", self.check.pk)]
        self.assertEqual(sorted(s.ms), [100.0, 200.0, 300.0])
        self.assertEqual(s.total, 10)                        # counts do not depend on the cap
        self.assertAlmostEqual(s.metrics()["response"], 400.0)


# ------------------------------------------------------------ 2. licence compliance on the software list
@override_settings(LINKEDEYE_TENANT=T)
class SoftwareOverAllocationTests(TestCase):
    def setUp(self):
        self.mum = Site.objects.create(name="Mumbai (NSE)", code="mum")
        Site.objects.create(name="DR Site", code="dr")
        self.sw = Software.objects.create(tenant_key=T, name="RHEL", version="9")
        License.objects.create(tenant_key=T, product="RHEL sub", software=self.sw, seats=2)
        for name, site in (("m1", "Mumbai (NSE)"), ("d1", "DR Site"), ("d2", "DR Site")):
            ci(name, site=site).software.add(self.sw)
        u = User.objects.create_user("mumviewer", password="pw")
        Membership.objects.create(user=u, site=self.mum, role="viewer")
        self.client.force_login(u)

    def row(self):
        r = self.client.get(reverse("cmdb:software"))
        self.assertEqual(r.status_code, 200)
        return r, next(s for s in r.context["page"] if s.pk == self.sw.pk)

    def test_site_scoped_viewer_sees_the_over_allocation(self):
        r, s = self.row()
        self.assertEqual(s.n_installs, 1)          # only the install the viewer may see
        self.assertEqual(s.all_installs, 3)        # licence compliance is tenant-wide
        self.assertTrue(s.over)
        self.assertContains(r, "3/2 seats")
        detail = self.client.get(reverse("cmdb:software_detail", args=[self.sw.pk]))
        self.assertTrue(detail.context["over"])
        self.assertEqual(detail.context["used"], 3)

    def test_software_within_its_seats_is_not_flagged(self):
        License.objects.filter(software=self.sw).update(seats=5)
        _r, s = self.row()
        self.assertFalse(s.over)


# ------------------------------------------------------------ 3. relationship ownership
@override_settings(LINKEDEYE_TENANT=T)
class RelationshipSiteOwnershipTests(TestCase):
    def setUp(self):
        self.a = Site.objects.create(name="Site A", code="a")
        self.b = Site.objects.create(name="Site B", code="b")
        self.u = User.objects.create_user("opA", password="pw")
        Membership.objects.create(user=self.u, site=self.a, role="operator")
        Membership.objects.create(user=self.u, site=self.b, role="viewer")
        self.a_app = ci("A-app", "application", site="Site A")
        self.b_db = ci("B-db", "database", site="Site B")
        self.client.force_login(self.u)
        self.url = reverse("cmdb:relationship_create")

    def post(self, source, target):
        return self.client.post(self.url, {"source": source.pk, "target": target.pk, "rel_type": "depends_on",
                                           "origin": "manual", "confidence": 100}, follow=True)

    def test_operator_cannot_own_an_edge_on_a_site_they_only_view(self):
        r = self.post(self.b_db, self.a_app)
        self.assertFalse(Relationship.objects.filter(source=self.b_db, target=self.a_app).exists())
        self.assertContains(r, "cannot change relationships")

    def test_operator_can_create_an_edge_from_their_own_site(self):
        r = self.post(self.a_app, self.b_db)
        self.assertTrue(Relationship.objects.filter(source=self.a_app, target=self.b_db).exists())
        self.assertContains(r, "Added:")

    def test_creator_can_manage_the_edge_it_created(self):
        self.post(self.a_app, self.b_db)
        rel = Relationship.objects.get(source=self.a_app)
        r = self.client.get(reverse("cmdb:relationships"))
        row = next(x for x in r.context["page"] if x.pk == rel.pk)
        self.assertTrue(row.can_edit)
