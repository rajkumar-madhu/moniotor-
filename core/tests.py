from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from core import access, jobs
from core.audit import audit
from core.events import emit, event_emitted
from core.models import AuditEvent, Event, Membership, Site


class B2BLandingTests(TestCase):
    @override_settings(LINKEDEYE_LOGIN_OTP=False)
    def test_welcome_page_public(self):
        r = self.client.get("/welcome/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Sign in")

    @override_settings(LINKEDEYE_LOGIN_OTP=False)
    def test_welcome_page_authenticated_shows_sign_out(self):
        u = User.objects.create_user("landing-tester", password="secret-for-test")
        self.assertTrue(self.client.login(username="landing-tester", password="secret-for-test"))
        r = self.client.get("/welcome/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Sign out")
        self.assertNotContains(r, ">Sign in</a>")


class AccessTests(TestCase):
    def setUp(self):
        self.mum = Site.objects.create(name="Mumbai (NSE)", code="mumbai-nse")
        self.dr = Site.objects.create(name="DR Site", code="dr")

    def test_defaults_without_memberships(self):
        viewer = User.objects.create_user("v")
        staff = User.objects.create_user("s", is_staff=True)
        root = User.objects.create_superuser("r", "r@example.com", "x")
        self.assertEqual(access.role_for(viewer), "viewer")
        self.assertEqual(access.role_for(staff), "operator")
        self.assertEqual(access.role_for(root), "admin")
        self.assertFalse(access.can(viewer, "operate"))
        self.assertTrue(access.can(staff, "operate"))
        self.assertIsNone(access.visible_sites(viewer))

    def test_site_scoped_membership(self):
        u = User.objects.create_user("op")
        Membership.objects.create(user=u, site=self.mum, role="operator")
        self.assertEqual(access.visible_sites(u), {"Mumbai (NSE)"})
        self.assertTrue(access.can(u, "operate", "Mumbai (NSE)"))
        self.assertFalse(access.can(u, "operate", "DR Site"))
        self.assertFalse(access.can(u, "admin"))

    def test_global_membership(self):
        u = User.objects.create_user("adm")
        Membership.objects.create(user=u, site=None, role="admin")
        self.assertIsNone(access.visible_sites(u))
        self.assertTrue(access.can(u, "admin", "DR Site"))


class EventAuditTests(TestCase):
    @override_settings(INCIDENT_AUTO_CREATE=False)  # isolate from the Service Desk listener
    def test_emit_sends_signal_and_survives_broken_listener(self):
        seen = []

        def good(sender, event, **kw):
            seen.append(event.title)

        def bad(sender, event, **kw):
            raise RuntimeError("boom")
        event_emitted.connect(good)
        event_emitted.connect(bad)
        try:
            ev = emit("test", "Thing down", severity="critical", site="DR Site", fingerprint="t:1", extra=1)
        finally:
            event_emitted.disconnect(good)
            event_emitted.disconnect(bad)
        self.assertEqual(seen, ["Thing down"])
        self.assertEqual(Event.objects.get().data, {"extra": 1})
        self.assertEqual(ev.severity, "critical")

    def test_audit_records_actor(self):
        u = User.objects.create_user("a")
        req = mock.Mock(user=u, META={"REMOTE_ADDR": "10.0.0.5"})
        audit(req, "create", object_type="thing", name="x")
        a = AuditEvent.objects.get()
        self.assertEqual((a.actor_name, a.source_ip, a.details), ("a", "10.0.0.5", {"name": "x"}))

    @override_settings(NOTIFY_WEBHOOK_URLS=["http://hook.example/x"], NOTIFY_EMAILS=[], INCIDENT_AUTO_CREATE=False,
                       NOTIFY_ASYNC=False)
    def test_webhook_notification_for_critical(self):
        # notifications are delivered after the event's transaction commits
        with mock.patch("core.notify._post") as post, self.captureOnCommitCallbacks(execute=True):
            emit("test", "Down", severity="critical")
            emit("test", "Just info", severity="info")
        self.assertEqual(post.call_count, 1)


class JobTests(TestCase):
    def test_registry_runs_due_jobs_once_per_interval(self):
        calls = []
        jobs.register("t.job", 60, lambda: calls.append(1) or "ran")
        try:
            self.assertEqual(jobs.run_due(now=1000.0).get("t.job"), "ran")
            self.assertNotIn("t.job", jobs.run_due(now=1030.0))
            self.assertIn("t.job", jobs.run_due(now=1061.0))
        finally:
            jobs.JOBS.pop("t.job")
        self.assertEqual(len(calls), 2)


class OverviewTests(TestCase):
    """Dashboard, global search and the shell context draw on every installed module, limited to the user's sites."""

    def setUp(self):
        from network.models import Device
        from tcpcheck.models import TcpCheck
        from urlchecker.models import UrlCheck
        self.mum = Site.objects.create(name="Mumbai", code="mum")
        self.dr = Site.objects.create(name="DR Site", code="dr")
        self.admin = User.objects.create_superuser("root", "root@example.com", "Corr3ct-horse-battery")
        self.viewer = User.objects.create_user("viewer", "v@example.com", "Corr3ct-horse-battery")
        Membership.objects.create(user=self.viewer, site=self.mum, role="viewer")
        self.op = User.objects.create_user("op", "op@example.com", "Corr3ct-horse-battery")
        Membership.objects.create(user=self.op, site=None, role="operator")
        UrlCheck.objects.create(name="Portal", url="https://portal.example.com/", site="Mumbai", last_status="down")
        Device.objects.create(name="core-sw-01", device_type="switch", mgmt_ip="10.0.0.1", site="Mumbai", status="down",
                              serial="SN-777")
        Device.objects.create(name="dr-fw-01", device_type="firewall", mgmt_ip="10.9.0.1", site="DR Site", status="down")
        TcpCheck.objects.create(name="Oracle", host="db.example.com", port=1521, site="Mumbai", last_status="degraded")

    def test_problem_count_and_sites_are_scoped(self):
        from . import overview
        self.assertEqual(overview.problem_count(self.admin), 4)
        self.assertEqual(overview.problem_count(self.viewer), 3)   # the DR firewall is outside Mumbai
        self.assertEqual(overview.site_names(self.admin), ["DR Site", "Mumbai"])
        self.assertEqual(overview.site_names(self.viewer), ["Mumbai"])
        kinds = {p["kind"] for p in overview.problems(self.viewer)}
        self.assertEqual(kinds, {"URL", "Device", "TCP"})

    def test_dashboard_lists_cross_module_problems(self):
        self.client.force_login(self.viewer)
        resp = self.client.get(reverse("dashboard"))
        self.assertContains(resp, "core-sw-01")
        self.assertContains(resp, "Oracle (db.example.com:1521)")
        self.assertNotContains(resp, "dr-fw-01")
        self.assertContains(resp, 'class="ov-health-card"')
        self.assertContains(resp, 'class="legacy-heatmap-card"')
        self.assertContains(resp, '<th>Devices</th>')  # site matrix column for the device kind

    def test_recent_events_render_in_one_wrapping_cell(self):
        # The events panel is a narrow column: the time sits on the source line and the title cell may wrap,
        # instead of three nowrap cells that were clipped by the table's scroll box.
        emit("bodeod", "BOD validation completed with warnings", severity="warning", site="Mumbai")
        self.client.force_login(self.admin)
        resp = self.client.get(reverse("dashboard"))
        self.assertContains(resp, '<td class="wrap ev"><span class="t-mute small">bodeod · ')
        self.assertContains(resp, "BOD validation completed with warnings</td>")

    def test_service_desk_is_the_last_sidebar_section(self):
        from core.nav import NAV
        labels = [item["label"] for item in NAV]
        self.assertEqual(labels[-1], "Service Desk")
        self.assertEqual(labels[0], "Dashboard")
        self.client.force_login(self.admin)
        html = self.client.get(reverse("dashboard")).content.decode()
        side = html[html.index('class="nav"'):html.index("</nav>")]
        self.assertGreater(side.index("Service Desk"), side.index("Administration"))

    def test_sidebar_labels_render_and_every_path_opens(self):
        # Django does not parse {{ }} or {% %} across a line break, so a wrapped tag shows up as source text.
        import html as html_lib
        import re
        from core.nav import NAV
        self.client.force_login(self.admin)
        html = self.client.get(reverse("dashboard")).content.decode()
        self.assertNotIn("{% ficon", html)
        side = html[html.index('<nav class="nav"'):html.index("</nav>")]
        self.assertNotIn("{{", side)
        self.assertNotIn("{%", side)
        labels = [item["label"] for item in NAV]
        for item in NAV:
            labels.extend(child["label"] for child in item.get("children", []))
        for label in labels:
            self.assertIn(f">{html_lib.escape(label)}<", side, label)
        link_labels = []
        for item in NAV:
            if item.get("children"):
                link_labels.extend(child["label"] for child in item["children"])
            else:
                link_labels.append(item["label"])
        hrefs = re.findall(r'href="([^"]+)"', side)
        self.assertEqual(len(hrefs), len(link_labels))
        for href in hrefs:
            response = self.client.get(href)
            self.assertLess(response.status_code, 500, href)
            self.assertNotIn("{{ item.label }}", response.content.decode(), href)
            self.assertNotIn("{{ c.label }}", response.content.decode(), href)

    def test_search_covers_devices_and_respects_sites(self):
        self.client.force_login(self.viewer)
        resp = self.client.get(reverse("search"), {"q": "SN-777"})
        self.assertContains(resp, "core-sw-01")
        resp = self.client.get(reverse("search"), {"q": "fw-01"})
        self.assertNotContains(resp, "dr-fw-01")
        self.assertContains(resp, "Network devices (0)")

    def test_shell_shows_membership_role_and_badge(self):
        self.client.force_login(self.op)
        resp = self.client.get(reverse("dashboard"))
        self.assertEqual(resp.context["le_role"], "Operator")
        self.assertEqual(resp.context["le_problem_count"], 4)
        self.assertTrue(resp.context["le_admin_url"])

    def test_sign_in_ignores_another_apps_csrf_cookie(self):
        # A second Django app on the same host shares the default csrftoken cookie (cookies have no port).
        from django.conf import settings
        from django.test import Client
        self.assertEqual(settings.CSRF_COOKIE_NAME, "linkedeyecsrftoken")
        client = Client(enforce_csrf_checks=True)
        page = client.get(reverse("login"))
        token = page.cookies[settings.CSRF_COOKIE_NAME].value
        client.cookies["csrftoken"] = "token-from-another-app-on-this-host"
        response = client.post(reverse("login"), {
            "username": "root",
            "password": "Corr3ct-horse-battery",
            "csrfmiddlewaretoken": token,
        }, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "CSRF token from POST incorrect")
        self.assertContains(response, 'name="csrf-cookie-name" content="linkedeyecsrftoken"')
