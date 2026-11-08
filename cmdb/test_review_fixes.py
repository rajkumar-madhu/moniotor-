"""Regression tests for the code-review findings in cmdb (servicedesk-cmdb-2, -5, -6, -7, -8)."""
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import Membership, Site

from . import jobs, services
from .models import ConfigurationItem, License, Relationship, Software

T = "default"


def ci(name, ci_type="server", **kw):
    kw.setdefault("tenant_key", T)
    return ConfigurationItem.objects.create(name=name, ci_type=ci_type, **kw)


@override_settings(LINKEDEYE_TENANT=T)
class SharedLicensePoolTests(TestCase):
    """servicedesk-cmdb-2 and -8: usage and seats are compared per software; retired CIs hold no seat."""

    def setUp(self):
        self.admin = User.objects.create_superuser("admin", "a@example.com", "pw")
        self.client.force_login(self.admin)
        self.sw = Software.objects.create(tenant_key=T, name="Acme DB", version="1")
        self.sw.installs.set([ci(f"h{i}") for i in range(150)])
        self.p1 = License.objects.create(tenant_key=T, product="p1", software=self.sw, seats=100)
        self.p2 = License.objects.create(tenant_key=T, product="p2", software=self.sw, seats=100)

    def test_two_licences_together_cover_the_installs(self):
        for lic in License.objects.all():
            self.assertFalse(lic.over_allocated)
            self.assertEqual(lic.state, "ok")
            self.assertEqual((lic.used, lic.capacity), (150, 200))
        self.assertEqual(jobs.license_flag(self.p1), ("", None))
        r = self.client.get(reverse("cmdb:licenses"))
        self.assertEqual([l.state for l in r.context["page"]], ["ok", "ok"])
        self.assertEqual((r.context["over"], r.context["used"], r.context["seats"]), ([], 150, 200))   # shared installs counted once
        self.assertFalse(self.client.get(reverse("cmdb:software_detail", args=[self.sw.pk])).context["over"])

    def test_really_over_the_pool_is_flagged_everywhere(self):
        self.sw.installs.add(*[ci(f"x{i}") for i in range(60)])           # 210 installs for 200 seats
        self.assertEqual(jobs.license_flag(self.p1), ("over", None))
        self.assertTrue(self.client.get(reverse("cmdb:software_detail", args=[self.sw.pk])).context["over"])
        self.assertEqual({l.state for l in self.client.get(reverse("cmdb:licenses")).context["page"]}, {"over"})

    def test_expired_licence_seats_do_not_cover_installs(self):
        License.objects.filter(pk=self.p2.pk).update(expiry=timezone.localdate() - timedelta(days=1))
        p1 = License.objects.get(pk=self.p1.pk)
        self.assertEqual((p1.capacity, p1.state), (100, "over"))           # 150 installs, only 100 current seats
        detail = self.client.get(reverse("cmdb:software_detail", args=[self.sw.pk])).context
        self.assertEqual(detail["seats"], 100)
        self.assertTrue(detail["over"])
        row = next(s for s in self.client.get(reverse("cmdb:software")).context["page"] if s.pk == self.sw.pk)
        self.assertTrue(row.over)
        self.assertEqual(License.objects.get(pk=self.p2.pk).state, "expired")

    def test_lifecycle_job_emits_nothing_for_a_compliant_pool(self):
        with mock.patch("cmdb.jobs.emit") as emit:
            jobs.lifecycle_scan()
        self.assertFalse([c for c in emit.call_args_list if c.kwargs.get("kind") == "license"])
        self.assertEqual(set(License.objects.values_list("alert_flag", flat=True)), {""})

    def test_retired_ci_frees_its_seat(self):
        sw = Software.objects.create(tenant_key=T, name="Small", version="1")
        a, b = ci("live-1"), ci("old-1")
        sw.installs.set([a, b])
        lic = License.objects.create(tenant_key=T, product="one seat", software=sw, seats=1)
        self.assertEqual((lic.used, lic.state), (2, "over"))
        ConfigurationItem.objects.filter(pk=b.pk).update(status="retired")
        lic = License.objects.get(pk=lic.pk)
        self.assertEqual((lic.used, lic.state), (1, "ok"))
        self.assertEqual(jobs.license_flag(lic), ("", None))
        row = next(s for s in self.client.get(reverse("cmdb:software")).context["page"] if s.pk == sw.pk)
        self.assertEqual((row.all_installs, row.over), (1, False))
        self.assertEqual(self.client.get(reverse("cmdb:software_detail", args=[sw.pk])).context["used"], 1)
        self.assertEqual(self.client.get(reverse("cmdb:licenses") + "?q=one+seat").context["page"][0].state, "ok")


@override_settings(LINKEDEYE_TENANT=T)
class AttributesNanTests(TestCase):
    """servicedesk-cmdb-5: NaN / Infinity are a field error, not a database error."""

    def test_non_finite_numbers_are_rejected(self):
        self.client.force_login(User.objects.create_superuser("admin", "a@example.com", "pw"))
        base = {"name": "nan-ci", "ci_type": "server", "status": "active", "environment": "prod", "site": "All Sites"}
        for bad in ('{"a": NaN}', '{"a": Infinity}', '{"a": -Infinity}', '{"a": 1e999}', '{"a": %s}' % ("9" * 5000)):
            r = self.client.post(reverse("cmdb:item_create"), dict(base, attributes=bad))
            self.assertEqual(r.status_code, 200, bad[:20])
            self.assertContains(r, "Enter a valid JSON")
        self.assertFalse(ConfigurationItem.objects.filter(name="nan-ci").exists())
        r = self.client.post(reverse("cmdb:item_create"), dict(base, attributes='{"a": 1.5, "b": [1, 2]}'))
        self.assertEqual(r.status_code, 302)


@override_settings(LINKEDEYE_TENANT=T)
class CiFormSiteScopeTests(TestCase):
    """servicedesk-cmdb-6: the CI form lists only the user's own sites."""

    def test_site_select_names_only_own_sites(self):
        mum = Site.objects.create(name="Mumbai", code="mum")
        Site.objects.create(name="SecretDRSite", code="dr")
        ci("hidden", site="SecretCISite")
        u = User.objects.create_user("mop", password="x")
        Membership.objects.create(user=u, site=mum, role="operator")
        self.client.force_login(u)
        r = self.client.get(reverse("cmdb:item_create"))
        self.assertContains(r, 'value="Mumbai"')
        for leaked in ("SecretDRSite", "SecretCISite", 'value="All Sites"'):
            self.assertNotContains(r, leaked)
        # posting an outside site still gets the permission message, and nothing is saved
        r = self.client.post(reverse("cmdb:item_create"), {"name": "x1", "ci_type": "server", "status": "active",
                                                           "environment": "prod", "site": "SecretDRSite"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "You cannot manage items on this site")
        self.assertFalse(ConfigurationItem.objects.filter(name="x1").exists())
        # a tenant-wide operator still sees every site
        staff = User.objects.create_user("staff", password="x", is_staff=True)
        self.client.force_login(staff)
        r = self.client.get(reverse("cmdb:item_create"))
        for seen in ("SecretDRSite", "SecretCISite", 'value="All Sites"'):
            self.assertContains(r, seen)


@override_settings(LINKEDEYE_TENANT=T)
class ImpactRetiredTests(TestCase):
    """servicedesk-cmdb-7: decommissioned CIs are not part of an outage's blast radius."""

    def test_retired_cis_are_not_impacted(self):
        pg = ci("PostgreSQL", "database")
        live, old, behind = ci("live-app", "application"), ci("legacy-app", "application", status="retired"), ci("behind-legacy", "application")
        for src in (live, old):
            Relationship.objects.create(source=src, target=pg, rel_type="depends_on")
        Relationship.objects.create(source=behind, target=old, rel_type="depends_on")
        names = [r["ci"].name for r in services.impacted(pg)]
        self.assertEqual(names, ["live-app"])
        # CIs that only depend on a retired one are not reached through it either
        self.assertNotIn("behind-legacy", names)
