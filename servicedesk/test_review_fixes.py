"""Regression tests for the code-review findings in servicedesk (servicedesk-cmdb-1, -3, -4, -6)."""
from django.contrib.auth.models import User
from django.test import Client, override_settings
from django.urls import reverse

from core.models import Membership, Site

from . import services
from .forms import assignable_users
from .models import Ticket
from .services import WorkflowError
from .tests import Base


class StaleTransitionTests(Base):
    """servicedesk-cmdb-1: transition() judges the row as it is in the database, not the copy the caller loaded."""

    def test_second_stale_copy_cannot_overwrite_the_first_change(self):
        r1 = User.objects.create_user("r1", password="x", is_staff=True)
        t = self.incident(kind="change", title="Patch", user=r1)
        services.transition(t, "submitted", user=r1)
        a, b = Ticket.objects.get(pk=t.pk), Ticket.objects.get(pk=t.pk)
        services.transition(a, "approved", user=self.admin)
        with self.assertRaises(WorkflowError):
            services.transition(b, "rejected", user=self.admin)
        t.refresh_from_db()
        self.assertEqual(t.status, "approved")
        self.assertIsNotNone(t.approved_at)
        self.assertEqual(t.timeline.filter(kind="status", new_value="rejected").count(), 0)

    def test_stale_acknowledge_cannot_reopen_a_closed_incident(self):
        t = self.incident()
        x, y = Ticket.objects.get(pk=t.pk), Ticket.objects.get(pk=t.pk)
        services.transition(x, "resolved", user=self.op)
        services.transition(x, "closed", user=self.op)
        with self.assertRaises(WorkflowError):
            services.transition(y, "acknowledged", user=self.op)
        t.refresh_from_db()
        self.assertEqual(t.status, "closed")
        self.assertIsNotNone(t.resolved_at)
        self.assertIsNotNone(t.closed_at)

    def test_caller_copy_is_refreshed_from_the_locked_row(self):
        t = self.incident()
        stale = Ticket.objects.get(pk=t.pk)
        services.transition(t, "acknowledged", user=self.op)
        services.transition(stale, "investigating", user=self.op)      # allowed from the real status (acknowledged)
        self.assertEqual(stale.status, "investigating")
        self.assertIsNotNone(stale.acknowledged_at)                    # taken from the row, not re-stamped


class AssignableUsersTests(Base):
    """servicedesk-cmdb-3: the operator filter runs in the database before the cap, not on the first 300 users."""

    def test_operator_beyond_the_first_300_users_is_listed(self):
        User.objects.bulk_create([User(username=f"v{i:03d}", first_name="A") for i in range(310)])      # viewers
        zed = User.objects.create_user("zed", password="x", first_name="Zed", is_staff=True)
        self.assertIn(zed, assignable_users(None))
        self.assertIn(zed, assignable_users(any_site=True))
        t = self.incident()
        r = self.login(self.op).post(reverse("servicedesk:edit", args=[t.number]),
                                     {"title": "x", "priority": "P2", "assignee": zed.pk, "problem": ""})
        self.assertEqual(r.status_code, 302, "the assignee must be a valid choice")
        t.refresh_from_db()
        self.assertEqual(t.assignee_id, zed.pk)

    def test_site_scoped_roles_are_respected(self):
        mum = Site.objects.create(name="Mumbai", code="mum")
        Site.objects.create(name="DR", code="dr")
        mop = User.objects.create_user("mop", password="x")
        Membership.objects.create(user=mop, site=mum, role="operator")
        vop = User.objects.create_user("vop", password="x", is_staff=True)
        Membership.objects.create(user=vop, site=mum, role="viewer")
        wide = User.objects.create_user("wide", password="x")
        Membership.objects.create(user=wide, site=None, role="operator")
        names = lambda site: {u.username for u in assignable_users(site)}  # noqa: E731
        self.assertIn("mop", names("Mumbai"))
        self.assertNotIn("mop", names("DR"))
        self.assertNotIn("mop", names(None))
        self.assertNotIn("vop", names("Mumbai"))
        self.assertTrue({"wide", "root", "op"} <= names(None))
        self.assertIn("mop", {u.username for u in assignable_users(any_site=True)})
        self.assertNotIn("vop", {u.username for u in assignable_users(any_site=True)})


@override_settings(LINKEDEYE_TENANT="default")
class ChangeWindowTests(Base):
    """servicedesk-cmdb-4: absurd planned dates are a field error, never a 500."""

    def test_year_0001_is_a_form_error(self):
        c = self.login(self.op)
        data = {"kind": "change", "title": "Window", "priority": "P3", "planned_start": "0001-01-01T00:00",
                "planned_end": "0001-01-02T00:00"}
        r = c.post(reverse("servicedesk:create") + "?kind=change", data)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Enter a date")
        self.assertFalse(Ticket.objects.filter(title="Window").exists())
        r = c.post(reverse("servicedesk:create") + "?kind=change", dict(data, planned_start="9999-12-30T00:00", planned_end="9999-12-31T00:00"))
        self.assertEqual(r.status_code, 200)
        r = c.post(reverse("servicedesk:create") + "?kind=change", dict(data, planned_start="2026-05-01T10:00", planned_end="2026-05-01T12:00"))
        self.assertEqual(r.status_code, 302)


class SiteNameScopeTests(Base):
    """servicedesk-cmdb-6: the site datalists list only the viewer's sites."""

    def test_forms_do_not_list_other_sites(self):
        mum = Site.objects.create(name="Mumbai", code="mum")
        Site.objects.create(name="SecretDRSite", code="dr")
        u = User.objects.create_user("mop", password="x")
        Membership.objects.create(user=u, site=mum, role="operator")
        c = self.login(u)
        for url in (reverse("servicedesk:create"), reverse("servicedesk:create") + "?kind=change"):
            r = c.get(url)
            self.assertContains(r, 'value="Mumbai"')
            self.assertNotContains(r, "SecretDRSite")
            self.assertNotContains(r, 'value="All Sites"')
        # a tenant-wide operator still sees all of them
        r = self.login(self.op).get(reverse("servicedesk:create"))
        self.assertContains(r, 'value="SecretDRSite"')
        self.assertContains(r, 'value="All Sites"')
