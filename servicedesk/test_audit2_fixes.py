"""Second audit round: the CI autocomplete stays inside the user's tenant and sites, and linking from the
incident side needs `operate` on the problem's site as well."""
from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from cmdb.models import ConfigurationItem
from core.models import Membership, Site

from . import services
from .models import Ticket, TicketCI

T = "default"


@override_settings(LINKEDEYE_TENANT=T)
class ScopedAutocompleteTests(TestCase):
    def setUp(self):
        self.mum = Site.objects.create(name="Mumbai", code="mum")
        self.dr = Site.objects.create(name="DR", code="dr")
        self.mop = User.objects.create_user("mop", password="x")
        Membership.objects.create(user=self.mop, site=self.mum, role="operator")
        self.c = Client()
        self.c.force_login(self.mop)

    def ci(self, name, site, tenant_key=T):
        return ConfigurationItem.objects.create(tenant_key=tenant_key, name=name, site=site)

    def test_ci_datalist_leaves_out_other_sites_and_other_tenants(self):
        self.ci("mum-app-01", "Mumbai")
        self.ci("dr-secret-ci", "DR")
        self.ci("other-tenant-ci", "Mumbai", tenant_key="acme")
        inc = services.create_ticket(user=self.mop, kind="incident", title="Mumbai outage", site="Mumbai")
        chg = services.create_ticket(user=self.mop, kind="change", title="Mumbai patch", site="Mumbai")
        for url in (reverse("servicedesk:create"), reverse("servicedesk:edit", args=[inc.number]), chg.get_absolute_url()):
            r = self.c.get(url)
            self.assertEqual(r.status_code, 200, url)
            self.assertContains(r, "mum-app-01", msg_prefix=url)
            self.assertNotContains(r, "dr-secret-ci", msg_prefix=url)
            self.assertNotContains(r, "other-tenant-ci", msg_prefix=url)

    def test_ci_names_already_on_tickets_leave_out_other_sites_and_other_tenants(self):
        mine = services.create_ticket(user=self.mop, kind="incident", title="Mumbai disk", site="Mumbai", ci_name="mum-ticket-ci")
        dr = services.create_ticket(user=self.mop, kind="incident", title="DR disk", site="DR", ci_name="dr-ticket-ci")
        other = services.create_ticket(user=self.mop, kind="incident", title="Acme disk", site="Mumbai",
                                       ci_name="other-tenant-ticket-ci", tenant_key="acme")
        TicketCI.objects.create(ticket=mine, ci_name="mum-affected-ci")
        TicketCI.objects.create(ticket=dr, ci_name="dr-affected-ci")
        TicketCI.objects.create(ticket=other, ci_name="other-tenant-affected-ci")
        r = self.c.get(reverse("servicedesk:create"))
        self.assertEqual(r.status_code, 200)
        for shown in ("mum-ticket-ci", "mum-affected-ci"):
            self.assertContains(r, shown)
        for hidden in ("dr-ticket-ci", "dr-affected-ci", "other-tenant-ticket-ci", "other-tenant-affected-ci"):
            self.assertNotContains(r, hidden)


@override_settings(LINKEDEYE_TENANT=T)
class IncidentSideLinkAuthzTests(TestCase):
    def setUp(self):
        self.mum = Site.objects.create(name="Mumbai", code="mum")
        self.dr = Site.objects.create(name="DR", code="dr")
        self.admin = User.objects.create_superuser("admin", "a@x.com", "x")
        self.user = User.objects.create_user("mixed", password="x")   # operator on Mumbai, viewer on DR
        Membership.objects.create(user=self.user, site=self.mum, role="operator")
        Membership.objects.create(user=self.user, site=self.dr, role="viewer")
        self.c = Client()
        self.c.force_login(self.user)
        self.inc = services.create_ticket(user=self.admin, kind="incident", title="Mumbai outage", site="Mumbai")
        self.prb = services.create_ticket(user=self.admin, kind="problem", title="DR root cause", site="DR")

    def test_link_from_the_incident_refuses_a_problem_the_user_may_only_view(self):
        self.c.post(reverse("servicedesk:link", args=[self.inc.number]), {"problem": self.prb.number})
        self.inc.refresh_from_db()
        self.assertIsNone(self.inc.problem)
        self.assertFalse(self.prb.timeline.filter(kind="link").exists())

    def test_create_form_refuses_a_problem_the_user_may_only_view(self):
        r = self.c.post(reverse("servicedesk:create"), {"kind": "incident", "title": "New one", "priority": "P3",
                                                        "site": "Mumbai", "problem": self.prb.pk})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(Ticket.objects.filter(title="New one").exists())
        self.assertFalse(self.prb.timeline.filter(kind="link").exists())

    def test_unlinking_also_needs_operate_on_the_old_problem(self):
        services.link_problem(self.inc, self.prb, user=self.admin)
        self.c.post(reverse("servicedesk:link", args=[self.inc.number]), {"problem": ""})
        self.inc.refresh_from_db()
        self.assertEqual(self.inc.problem, self.prb)


@override_settings(LINKEDEYE_TENANT=T)
class ProblemSideLinkAuthzTests(TestCase):
    """The same rule from the problem's page: the user needs `operate` on the incident being (un)linked."""

    def setUp(self):
        self.mum = Site.objects.create(name="Mumbai", code="mum")
        self.dr = Site.objects.create(name="DR", code="dr")
        self.admin = User.objects.create_superuser("admin", "a@x.com", "x")
        self.user = User.objects.create_user("mixed", password="x")   # operator on Mumbai, viewer on DR
        Membership.objects.create(user=self.user, site=self.mum, role="operator")
        Membership.objects.create(user=self.user, site=self.dr, role="viewer")
        self.c = Client()
        self.c.force_login(self.user)
        self.prb = services.create_ticket(user=self.admin, kind="problem", title="Mumbai root cause", site="Mumbai")
        self.dr_inc = services.create_ticket(user=self.admin, kind="incident", title="DR outage", site="DR")
        self.mum_inc = services.create_ticket(user=self.admin, kind="incident", title="Mumbai outage", site="Mumbai")

    def test_link_from_the_problem_refuses_an_incident_the_user_may_only_view(self):
        self.c.post(reverse("servicedesk:link", args=[self.prb.number]), {"incident": self.dr_inc.number})
        self.dr_inc.refresh_from_db()
        self.assertIsNone(self.dr_inc.problem)

    def test_unlink_from_the_problem_refuses_an_incident_the_user_may_only_view(self):
        services.link_problem(self.dr_inc, self.prb, user=self.admin)
        self.c.post(reverse("servicedesk:link", args=[self.prb.number]), {"incident": self.dr_inc.number, "unlink": "1"})
        self.dr_inc.refresh_from_db()
        self.assertEqual(self.dr_inc.problem, self.prb)

    def test_link_from_the_problem_still_works_on_the_users_own_site(self):
        self.c.post(reverse("servicedesk:link", args=[self.prb.number]), {"incident": self.mum_inc.number})
        self.mum_inc.refresh_from_db()
        self.assertEqual(self.mum_inc.problem, self.prb)
