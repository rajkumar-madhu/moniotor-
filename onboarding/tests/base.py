"""Shared fixtures. No test may open a socket: every hook that would probe is either not wired up
yet (it raises StepPending) or is patched."""
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, TestCase, override_settings

from core.models import Client as LeClient
from core.models import Membership, Site

try:
    from cryptography.fernet import Fernet
    KEY = Fernet.generate_key().decode()
    KEY2 = Fernet.generate_key().decode()
except Exception:  # pragma: no cover - cryptography is a hard requirement
    KEY = KEY2 = ""

PASSWORD = "onboarding-test-pass-123"
SECRET = "sup3r-secret-ssh-password-xyz"

with_key = override_settings(LINKEDEYE_CREDENTIAL_KEY=KEY)
without_key = override_settings(LINKEDEYE_CREDENTIAL_KEY="")


class OnboardingTestCase(TestCase):
    """A client, two sites and one user per role on site A."""

    def setUp(self):
        super().setUp()
        cache.clear()
        User = get_user_model()
        self.client_row = LeClient.objects.create(name="NSE", code="nse")
        self.site_a = Site.objects.create(name="Mumbai", code="mumbai", client=self.client_row)
        self.site_b = Site.objects.create(name="Chennai", code="chennai")
        self.admin_a = User.objects.create_user("admin-a", password=PASSWORD)
        Membership.objects.create(user=self.admin_a, site=self.site_a, role="admin")
        self.admin_b = User.objects.create_user("admin-b", password=PASSWORD)
        Membership.objects.create(user=self.admin_b, site=self.site_b, role="admin")
        self.operator = User.objects.create_user("operator-a", password=PASSWORD)
        Membership.objects.create(user=self.operator, site=self.site_a, role="operator")
        self.viewer = User.objects.create_user("viewer-a", password=PASSWORD)
        Membership.objects.create(user=self.viewer, site=self.site_a, role="viewer")
        self.c = Client()

    def login(self, user):
        self.c = Client()
        self.assertTrue(self.c.login(username=user.get_username(), password=PASSWORD))
        return self.c

    def make_run(self, site=None, **kwargs):
        from onboarding.models import OnboardingRun
        return OnboardingRun.objects.create(
            site=site or self.site_a.name, client=self.client_row, created_by=self.admin_a,
            step=kwargs.pop("step", "scope"), **kwargs)

    def make_target(self, run, hostname="db-01", **kwargs):
        from onboarding.models import OnboardingTarget
        return OnboardingTarget.objects.create(run=run, hostname=hostname, **kwargs)
