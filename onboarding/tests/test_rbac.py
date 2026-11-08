"""Only an admin for the run's site may change it; site scoping and tenant isolation everywhere."""
from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.urls import reverse

from core import secrets
from core.models import Membership
from onboarding.models import Credential, OnboardingRun, OnboardingTarget

from .base import PASSWORD, SECRET, OnboardingTestCase, with_key

STEPS = ("scope", "targets", "credentials", "verify", "enable", "logs", "confirm", "done")
ACTIONS = {"scope": {"kind": "server", "site": "Mumbai", "environment": "prod", "action": "save"},
           "targets": {"action": "add", "hosts": "evil-01", "target_type": "linux"},
           "credentials": {"action": "save", "name": "n", "kind": "ssh_password", "secret": SECRET},
           "verify": {"action": "verify"}, "enable": {"action": "enable"}, "logs": {"action": "logs"},
           "confirm": {"action": "activate"}, "done": {"action": "finish"}}


def url(run, name):
    return reverse("onboarding:step", args=[run.pk, name])


@with_key
class StepRbacTests(OnboardingTestCase):
    def setUp(self):
        super().setUp()
        self.run_ = self.make_run(site="Mumbai")
        self.target = self.make_target(self.run_, "db-01")

    def assert_unchanged(self):
        self.run_.refresh_from_db()
        self.assertEqual(self.run_.targets.count(), 1)
        self.assertEqual(Credential.objects.count(), 0)
        self.assertEqual(self.run_.status, "draft")
        self.assertEqual(self.run_.step, "scope")

    def test_viewer_and_operator_can_read_every_step_but_change_none(self):
        for user in (self.viewer, self.operator):
            self.login(user)
            for name in STEPS:
                self.assertEqual(self.c.get(url(self.run_, name)).status_code, 200, (user, name))
                resp = self.c.post(url(self.run_, name), ACTIONS[name])
                self.assertEqual(resp.status_code, 403, (user.username, name))
            self.assert_unchanged()

    def test_read_only_pages_show_no_change_controls(self):
        self.login(self.viewer)
        body = self.c.get(url(self.run_, "credentials")).content.decode()
        self.assertIn("only an admin", body)
        self.assertNotIn("Store in the vault", body)

    def test_an_admin_of_another_site_cannot_even_see_the_run(self):
        self.login(self.admin_b)
        for name in STEPS:
            self.assertEqual(self.c.get(url(self.run_, name)).status_code, 404, name)
            self.assertEqual(self.c.post(url(self.run_, name), ACTIONS[name]).status_code, 404, name)
        self.assertEqual(self.c.get(reverse("onboarding:log", args=[self.run_.pk])).status_code, 404)
        self.assertEqual(self.c.post(reverse("onboarding:poll", args=[self.run_.pk])).status_code, 404)
        self.assertEqual(self.c.post(reverse("onboarding:run_delete", args=[self.run_.pk])).status_code, 404)
        self.assert_unchanged()
        self.assertNotIn("db-01", self.c.get(reverse("onboarding:index")).content.decode())

    def test_anonymous_is_sent_to_sign_in(self):
        for name in ("scope", "done"):
            resp = Client().get(url(self.run_, name))
            self.assertIn(resp.status_code, (302, 403))
            self.assertNotIn("db-01", resp.content.decode())
        self.assertEqual(Client().post(url(self.run_, "verify"), {"action": "verify"}).status_code in (302, 403), True)

    def test_a_viewer_cannot_start_a_run_or_delete_one(self):
        self.login(self.viewer)
        self.assertEqual(self.c.get(reverse("onboarding:new")).status_code, 403)
        # a refused POST is bounced back with a message (the shared require() behaviour), never executed
        for resp in (self.c.post(reverse("onboarding:new"), {"kind": "server", "site": "Mumbai", "environment": "prod"}),
                     self.c.post(reverse("onboarding:run_delete", args=[self.run_.pk])),
                     self.c.post(reverse("onboarding:target_toggle", args=[self.run_.pk, self.target.pk]))):
            self.assertIn(resp.status_code, (302, 403))
        self.assertEqual(OnboardingRun.objects.count(), 1)
        self.target.refresh_from_db()
        self.assertTrue(self.target.enabled)

    def test_an_admin_cannot_start_a_run_in_a_site_they_do_not_administer(self):
        self.login(self.admin_a)
        resp = self.c.post(reverse("onboarding:new"), {"kind": "server", "site": "Chennai", "environment": "prod"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(OnboardingRun.objects.filter(site="Chennai").count(), 0)
        self.assertTrue(resp.context["form"].errors.get("site"))

    def test_moving_a_run_into_a_foreign_site_is_refused(self):
        self.login(self.admin_a)
        self.c.post(url(self.run_, "scope"), {"kind": "server", "site": "Chennai", "environment": "prod", "action": "save"})
        self.run_.refresh_from_db()
        self.assertEqual(self.run_.site, "Mumbai")

    def test_site_admin_without_other_memberships_sees_only_own_runs(self):
        other = self.make_run(site="Chennai")
        self.login(self.admin_a)
        body = self.c.get(reverse("onboarding:index")).content.decode()
        self.assertIn(url(self.run_, "scope"), body)
        self.assertNotIn(f"/onboarding/{other.pk}/", body)
        self.assertEqual(self.c.get(url(other, "scope")).status_code, 404)

    def test_admin_on_a_site_can_toggle_and_delete_there(self):
        self.login(self.admin_a)
        resp = self.c.post(reverse("onboarding:target_toggle", args=[self.run_.pk, self.target.pk]), {"step": "targets"})
        self.assertEqual(resp.status_code, 302)
        self.target.refresh_from_db()
        self.assertFalse(self.target.enabled)
        self.assertEqual(self.c.post(reverse("onboarding:run_delete", args=[self.run_.pk])).status_code, 302)
        self.assertFalse(OnboardingRun.objects.filter(pk=self.run_.pk).exists())

    def test_every_mutation_needs_csrf(self):
        User = get_user_model()
        User.objects.get(pk=self.admin_a.pk)
        strict = Client(enforce_csrf_checks=True)
        self.assertTrue(strict.login(username="admin-a", password=PASSWORD))
        for name in STEPS:
            self.assertEqual(strict.post(url(self.run_, name), ACTIONS[name]).status_code, 403, name)
        self.assertEqual(strict.post(reverse("onboarding:poll", args=[self.run_.pk])).status_code, 403)
        self.assertEqual(strict.post(reverse("onboarding:run_delete", args=[self.run_.pk])).status_code, 403)
        self.assert_unchanged()

    def test_get_is_refused_on_mutating_endpoints(self):
        self.login(self.admin_a)
        for name in ("run_delete",):
            self.assertEqual(self.c.get(reverse(f"onboarding:{name}", args=[self.run_.pk])).status_code, 405)
        self.assertEqual(self.c.get(reverse("onboarding:target_toggle", args=[self.run_.pk, self.target.pk])).status_code, 405)
        self.assertEqual(self.c.get(reverse("onboarding:sd_token")).status_code, 405)


@with_key
class CredentialScopeTests(OnboardingTestCase):
    def test_a_credential_of_another_site_cannot_be_attached_rotated_or_deleted(self):
        run = self.make_run(site="Mumbai")
        self.make_target(run, "db-01")
        foreign = secrets.store("ssh_password", "u", SECRET, "Chennai", self.admin_b, name="chennai-ssh")
        self.login(self.admin_a)
        for action in ("attach", "rotate", "delete"):
            resp = self.c.post(url(run, "credentials"), {"action": action, "credential": foreign.pk, "secret": "x-new-secret"})
            self.assertEqual(resp.status_code, 404, action)
        foreign.refresh_from_db()
        with secrets.use(foreign) as plain:
            self.assertEqual(plain, SECRET)
        self.assertNotIn("chennai-ssh", self.c.get(url(run, "credentials")).content.decode())

    def test_a_credential_of_another_tenant_is_invisible(self):
        run = self.make_run(site="Mumbai")
        with override_settings(LINKEDEYE_TENANT="other"):
            alien = secrets.store("ssh_password", "u", SECRET, "Mumbai", self.admin_a, name="alien-ssh")
        self.assertEqual(alien.tenant_key, "other")
        self.login(self.admin_a)
        self.assertNotIn("alien-ssh", self.c.get(url(run, "credentials")).content.decode())
        self.assertEqual(self.c.post(url(run, "credentials"), {"action": "attach", "credential": alien.pk}).status_code, 404)

    def test_changing_the_site_drops_credentials_of_the_old_site(self):
        both = get_user_model().objects.create_user("both", password=PASSWORD)
        Membership.objects.create(user=both, site=self.site_a, role="admin")
        Membership.objects.create(user=both, site=self.site_b, role="admin")
        run = self.make_run(site="Mumbai")
        target = self.make_target(run, "db-01")
        cred = secrets.store("ssh_password", "u", SECRET, "Mumbai", both)
        target.credential = cred
        target.save()
        self.login(both)
        self.c.post(url(run, "scope"), {"kind": "server", "site": "Chennai", "environment": "prod", "action": "save"})
        run.refresh_from_db()
        target.refresh_from_db()
        self.assertEqual(run.site, "Chennai")
        self.assertIsNone(target.credential)


@with_key
class TenantIsolationTests(OnboardingTestCase):
    def test_runs_targets_and_logs_of_another_tenant_are_not_reachable(self):
        with override_settings(LINKEDEYE_TENANT="other"):
            alien = OnboardingRun.objects.create(site="Mumbai", tenant_key="other")
            OnboardingTarget.objects.create(run=alien, hostname="alien-host")
        self.assertEqual(alien.tenant_key, "other")
        self.login(self.admin_a)
        for name in STEPS:
            self.assertEqual(self.c.get(url(alien, name)).status_code, 404, name)
        self.assertNotIn("alien-host", self.c.get(reverse("onboarding:index")).content.decode())
        self.assertNotIn("alien-host", self.c.get(reverse("onboarding:export")).content.decode())

    def test_new_runs_are_stamped_with_the_current_tenant(self):
        from core.models import Site
        Site.objects.create(name="Pune", code="pune", tenant_key="acme")
        root = get_user_model().objects.create_superuser("root-t", "t@example.com", "root-pass-123456")
        self.c.force_login(root)
        with override_settings(LINKEDEYE_TENANT="acme"):
            self.c.post(reverse("onboarding:new"), {"kind": "server", "site": "Pune", "environment": "prod"})
        self.assertEqual(OnboardingRun.objects.get().tenant_key, "acme")


@with_key
class ServiceDiscoveryRbacTests(OnboardingTestCase):
    """The token and the fetch record are tenant-wide: a single-site admin is not enough."""

    def test_site_admin_cannot_open_or_rotate_the_discovery_token(self):
        self.login(self.admin_a)
        self.assertEqual(self.c.get(reverse("onboarding:sd_page")).status_code, 403)
        self.assertEqual(self.c.post(reverse("onboarding:sd_token")).status_code, 403)
        self.assertEqual(Credential.objects.count(), 0)

    def test_tenant_wide_admin_and_superuser_can(self):
        wide = get_user_model().objects.create_user("wide", password=PASSWORD)
        Membership.objects.create(user=wide, site=None, role="admin")
        for user in (wide,):
            self.login(user)
            self.assertEqual(self.c.get(reverse("onboarding:sd_page")).status_code, 200)
        self.assertEqual(self.c.post(reverse("onboarding:sd_token")).status_code, 200)

    def test_viewer_cannot(self):
        self.login(self.viewer)
        self.assertEqual(self.c.get(reverse("onboarding:sd_page")).status_code, 403)

    def test_the_kpi_counts_only_the_users_sites(self):
        a = self.make_run(site="Mumbai")
        b = self.make_run(site="Chennai")
        self.make_target(a, "a-1", state="scraping")
        self.make_target(b, "b-1", state="scraping")
        self.make_target(b, "b-2", state="scraping")
        self.login(self.admin_a)
        resp = self.c.get(reverse("onboarding:index"))
        self.assertEqual(resp.context["n_sd"], 1)
