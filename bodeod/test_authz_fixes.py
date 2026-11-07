"""F06: holidays cancel BOD/EOD on every site, so adding, uploading and deleting them needs admin on every site."""
from datetime import date

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse

from core.models import Membership, Site

from .models import Holiday


class HolidayAuthzTests(TestCase):
    def setUp(self):
        self.a = Site.objects.create(name="SiteA", code="A")
        self.b = Site.objects.create(name="SiteB", code="B")

    def client_for(self, name, **roles):
        u = User.objects.create_user(name, password="pw")
        for site, role in roles.items():
            Membership.objects.create(user=u, site=None if site == "all" else getattr(self, site), role=role)
        c = Client()
        c.force_login(u)
        return c

    def test_site_roles_cannot_add_upload_or_delete_holidays(self):
        data = {"date": "2026-12-25", "exchange": "NSE", "description": "x"}
        for c in (self.client_for("opA", a="operator", b="viewer"), self.client_for("admA", a="admin")):
            c.post(reverse("bodeod:holidays"), data)
            f = SimpleUploadedFile("nse.csv", b"date,description\n2026-03-03,Holi\n", "text/csv")
            c.post(reverse("bodeod:holiday_upload"), {"file": f, "default_exchange": "NSE"})
            self.assertFalse(Holiday.objects.exists())
            r = c.get(reverse("bodeod:holidays"))
            self.assertFalse(r.context["can_configure"])
            self.assertFalse(r.context["can_admin"])
            self.assertNotContains(r, 'href="#holiday-form"')
        h = Holiday.objects.create(exchange="NSE", date=date(2026, 1, 26), description="Republic Day")
        self.client_for("admA2", a="admin").post(reverse("bodeod:holiday_delete", args=[h.pk]))
        self.assertTrue(Holiday.objects.filter(pk=h.pk).exists())

    def test_tenant_wide_admin_can(self):
        c = self.client_for("boss", all="admin")
        r = c.post(reverse("bodeod:holidays"), {"date": "2026-12-25", "exchange": "NSE", "description": "Christmas"})
        self.assertEqual(r.status_code, 302)
        f = SimpleUploadedFile("nse.csv", b"date,description\n2026-03-03,Holi\n", "text/csv")
        c.post(reverse("bodeod:holiday_upload"), {"file": f, "default_exchange": "NSE"})
        self.assertEqual(Holiday.objects.count(), 2)
        self.assertTrue(c.get(reverse("bodeod:holidays")).context["can_configure"])
        c.post(reverse("bodeod:holiday_delete", args=[Holiday.objects.first().pk]))
        self.assertEqual(Holiday.objects.count(), 1)


class GlobalModeAuthzTests(TestCase):
    """Global ("All Sites") modes need the role on every site: a one-site operator must not run or sign off
    the whole tenant's BOD, and a one-site admin must not reconfigure it."""

    def setUp(self):
        from datetime import time

        from .models import ChecklistItem, Mode
        self.a = Site.objects.create(name="SiteA", code="A")
        self.mode = Mode.objects.create(code="BOD", name="Beginning of Day", site="All Sites", window_start=time(7),
                                        window_end=time(9), refresh_checks=False)
        ChecklistItem.objects.create(mode=self.mode, name="Feed", kind="manual", instructions="Check the feed")

    def client_for(self, name, site, role):
        u = User.objects.create_user(name, password="pw")
        Membership.objects.create(user=u, site=site, role=role)
        c = Client()
        c.force_login(u)
        return u, c

    def test_user_can_on_global_mode_needs_tenant_wide_role(self):
        from . import services
        u, _c = self.client_for("opA", self.a, "admin")
        for site in ("All Sites", ""):
            self.assertFalse(services.user_can(u, "operate", site))
        self.assertTrue(services.user_can(u, "admin", "SiteA"))
        g, _c = self.client_for("gop", None, "operator")
        self.assertTrue(services.user_can(g, "operate", "All Sites"))
        self.assertFalse(services.user_can(g, "admin", "All Sites"))

    def test_one_site_operator_cannot_run_or_sign_off_global_mode(self):
        from . import services
        from .models import Run
        _u, c = self.client_for("opA", self.a, "operator")
        c.post(reverse("bodeod:run", args=[self.mode.pk]))
        self.assertFalse(Run.objects.exists())
        g, gc = self.client_for("gop", None, "operator")
        run = services.start_run(self.mode, user=g, trigger="manual", refresh=False)
        item = run.results.get(name="Feed")
        c.post(reverse("bodeod:signoff", args=[item.pk]), {"note": "looked fine"})
        item.refresh_from_db()
        self.assertNotEqual(item.status, "signed_off")
        gc.post(reverse("bodeod:signoff", args=[item.pk]), {"note": "verified"})
        item.refresh_from_db()
        self.assertEqual(item.status, "signed_off")

    def test_one_site_admin_cannot_edit_global_mode(self):
        _u, c = self.client_for("admA", self.a, "admin")
        c.post(reverse("bodeod:mode_save"), {"pk": self.mode.pk, "code": "BOD", "name": "hacked", "site": "All Sites",
                                             "window_start": "07:00", "window_end": "09:00"})
        self.mode.refresh_from_db()
        self.assertEqual(self.mode.name, "Beginning of Day")
