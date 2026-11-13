"""Session-backed client filter narrows lists and dashboard counts."""
from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings

from core.models import Client as LeClient, Membership, Site
from network.models import Device

T = "test-tenant"


@override_settings(LINKEDEYE_TENANT=T)
class ClientScopeTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_superuser("admin", "a@example.com", "x")
        self.c1 = LeClient.objects.create(tenant_key=T, name="FinSpot", code="finspot")
        self.c2 = LeClient.objects.create(tenant_key=T, name="OtherCo", code="other")
        Site.objects.create(tenant_key=T, name="FS DR", code="fs-dr", client=self.c1)
        Site.objects.create(tenant_key=T, name="Legacy Site", code="legacy", client=self.c2)
        Device.objects.create(tenant_key=T, name="d1", site="FS DR", mgmt_ip="10.0.0.1")
        Device.objects.create(tenant_key=T, name="d2", site="Legacy Site", mgmt_ip="10.0.0.2")
        self.http = Client()

    def test_superuser_dashboard_scopes_with_session_client(self):
        self.http.force_login(self.admin)
        session = self.http.session
        session["le_client"] = str(self.c1.pk)
        session.save()
        r = self.http.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "FS DR")
        self.assertNotContains(r, "Legacy Site")
        # the site matrix cell for the one in-scope device; count=1 proves the other client's device is not counted
        self.assertContains(r, "0 healthy / 1 monitored", count=1)

    def test_client_query_sets_session_and_filters_network(self):
        from django.urls import reverse

        self.http.force_login(self.admin)
        r = self.http.get(reverse("network:all") + f"?client={self.c1.pk}")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "d1")
        self.assertNotContains(r, ">d2<")
        r2 = self.http.get(reverse("network:all"))
        self.assertContains(r2, "d1")
        self.assertNotContains(r2, ">d2<")

    def test_single_client_user_gets_auto_filter(self):
        User = get_user_model()
        u = User.objects.create_user("ops", password="x")
        site = Site.objects.get(name="FS DR")
        Membership.objects.create(user=u, site=site, role="viewer")
        self.http.force_login(u)
        r = self.http.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "FS DR")
        self.assertNotContains(r, "Legacy Site")
        # the site matrix cell for the one in-scope device; count=1 proves the other client's device is not counted
        self.assertContains(r, "0 healthy / 1 monitored", count=1)

    def test_device_detail_clears_client_when_site_outside_filter(self):
        from django.urls import reverse

        self.http.force_login(self.admin)
        session = self.http.session
        session["le_client"] = str(self.c1.pk)
        session.save()
        d2 = Device.objects.get(name="d2")
        url = reverse("network:detail", args=[d2.pk]) + "?tab=configuration"
        r = self.http.get(url)
        self.assertEqual(r.status_code, 302)
        self.assertIn("client=", r["Location"])
        self.assertIn("tab=configuration", r["Location"])
        r2 = self.http.get(r["Location"])
        self.assertEqual(r2.status_code, 200)
        self.assertContains(r2, "d2")
        self.assertContains(r2, "outside the selected client")
