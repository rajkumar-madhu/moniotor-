"""F01: global tickets and the tenant-wide SLA policies / service catalog need roles on every site."""
from unittest import mock

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from core.access import can_on
from core.models import Membership, Site

from . import services
from .models import CatalogItem, SlaPolicy, Ticket


@override_settings(NOTIFY_WEBHOOK_URLS=[], NOTIFY_EMAILS=[],
                   EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class GlobalRecordAuthzTests(TestCase):
    def setUp(self):
        p = mock.patch("core.notify._post", side_effect=AssertionError("no network in tests"))
        p.start()
        self.addCleanup(p.stop)
        self.mum = Site.objects.create(name="Mumbai", code="mum")
        self.dr = Site.objects.create(name="DR", code="dr")
        self.mop = User.objects.create_user("mop", password="x")
        Membership.objects.create(user=self.mop, site=self.mum, role="operator")
        self.madm = User.objects.create_user("madm", password="x")
        Membership.objects.create(user=self.madm, site=self.mum, role="admin")
        self.gadm = User.objects.create_user("gadm", "g@x.com", "x")
        Membership.objects.create(user=self.gadm, site=None, role="admin")
        self.gop = User.objects.create_user("gop", password="x")
        Membership.objects.create(user=self.gop, site=None, role="operator")

    def c(self, user):
        c = Client()
        c.force_login(user)
        return c

    def test_services_can_on_delegates_to_core(self):
        for user in (self.mop, self.madm, self.gadm):
            for site in (None, "", "All Sites", "Mumbai", "DR"):
                for perm in ("operate", "admin"):
                    self.assertEqual(services.can_on(user, perm, site), can_on(user, perm, site), (user, site, perm))

    def test_single_site_operator_cannot_transition_or_edit_global_ticket(self):
        g = services.create_ticket(user=self.gadm, kind="incident", title="Global P1", priority="P1", site="All Sites")
        c = self.c(self.mop)
        c.post(reverse("servicedesk:transition", args=[g.number]), {"to": "resolved", "note": "x"})
        c.post(reverse("servicedesk:edit", args=[g.number]), {
            "title": g.title, "priority": "P1", "site": "Mumbai", "description": "", "category": "", "client": "",
            "ci_name": "", "resolution": ""})
        g.refresh_from_db()
        self.assertNotEqual(g.status, "resolved")
        self.assertEqual(g.site, "All Sites")
        # a tenant-wide operator may
        self.c(self.gop).post(reverse("servicedesk:transition", args=[g.number]), {"to": "acknowledged"})
        g.refresh_from_db()
        self.assertEqual(g.status, "acknowledged")

    def test_single_site_admin_cannot_delete_global_ticket(self):
        g = services.create_ticket(user=self.gadm, kind="incident", title="Global 2", site="")
        self.c(self.madm).post(reverse("servicedesk:delete", args=[g.number]))
        self.assertTrue(Ticket.objects.filter(pk=g.pk).exists())
        self.c(self.gadm).post(reverse("servicedesk:delete", args=[g.number]))
        self.assertFalse(Ticket.objects.filter(pk=g.pk).exists())

    def test_single_site_operator_cannot_create_global_ticket(self):
        r = self.c(self.mop).post(reverse("servicedesk:create"), {"kind": "incident", "title": "x", "priority": "P3",
                                                                   "site": "All Sites"})
        self.assertContains(r, "You cannot raise or move tickets for this site.")
        self.assertFalse(Ticket.objects.filter(title="x").exists())
        self.c(self.mop).post(reverse("servicedesk:create"), {"kind": "incident", "title": "y", "priority": "P3",
                                                               "site": "Mumbai"})
        self.assertTrue(Ticket.objects.filter(title="y", site="Mumbai").exists())

    def test_global_ticket_assignee_must_operate_on_every_site(self):
        g = services.create_ticket(user=self.gadm, kind="incident", title="Global 3", site="All Sites")
        self.c(self.gadm).post(reverse("servicedesk:assign", args=[g.number]), {"assignee": str(self.mop.pk)})
        g.refresh_from_db()
        self.assertIsNone(g.assignee)
        self.c(self.gadm).post(reverse("servicedesk:assign", args=[g.number]), {"assignee": str(self.gop.pk)})
        g.refresh_from_db()
        self.assertEqual(g.assignee, self.gop)

    def _sla_post(self, user, p, minutes):
        return self.c(user).post(reverse("servicedesk:sla"), {
            "pk": p.pk, f"p{p.pk}-name": "Crit", f"p{p.pk}-response_minutes": minutes,
            f"p{p.pk}-resolution_minutes": minutes, f"p{p.pk}-active": "on"})

    def test_single_site_admin_cannot_change_sla_policy(self):
        p = SlaPolicy.objects.filter(priority="P1").first() or SlaPolicy.objects.create(
            priority="P1", name="Critical", response_minutes=15, resolution_minutes=240)
        before = p.response_minutes
        self._sla_post(self.madm, p, 9999)
        p.refresh_from_db()
        self.assertEqual(p.response_minutes, before)
        self.assertFalse(self.c(self.madm).get(reverse("servicedesk:sla")).context["can_admin"])
        self._sla_post(self.gadm, p, 9999)
        p.refresh_from_db()
        self.assertEqual(p.response_minutes, 9999)

    def test_catalog_changes_need_tenant_wide_role(self):
        item = CatalogItem.objects.create(name="VPN access", fulfilment_hours=8)
        data = {"name": "Hacked", "category": "", "description": "", "fulfilment_hours": 1, "default_priority": "P4",
                "active": "on"}
        self.assertEqual(self.c(self.madm).get(reverse("servicedesk:catalog_new")).status_code, 403)
        self.c(self.madm).post(reverse("servicedesk:catalog_edit", args=[item.pk]), data)
        self.c(self.madm).post(reverse("servicedesk:catalog_delete", args=[item.pk]))
        item.refresh_from_db()
        self.assertEqual(item.name, "VPN access")
        r = self.c(self.madm).get(reverse("servicedesk:catalog"))
        self.assertFalse(r.context["can_configure"])
        self.assertFalse(r.context["can_admin"])
        self.c(self.gadm).post(reverse("servicedesk:catalog_edit", args=[item.pk]), data)
        item.refresh_from_db()
        self.assertEqual(item.name, "Hacked")

    def test_catalog_request_for_all_sites_needs_tenant_wide_operate(self):
        item = CatalogItem.objects.create(name="Laptop", fulfilment_hours=8)
        url = reverse("servicedesk:catalog_request", args=[item.pk])
        r = self.c(self.mop).post(url, {"details": "need one", "site": "", "client": ""})
        self.assertContains(r, "You cannot raise requests for this site.")
        self.assertFalse(Ticket.objects.filter(kind="request").exists())
        self.c(self.mop).post(url, {"details": "need one", "site": "Mumbai", "client": ""})
        self.assertTrue(Ticket.objects.filter(kind="request", site="Mumbai").exists())
