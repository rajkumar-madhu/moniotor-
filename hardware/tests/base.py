"""Shared fixtures: two sites, one user per role, physical servers with BMC endpoints, and helpers for the fake BMC."""
from contextlib import contextmanager
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from core.models import Membership, Site
from hardware import collector, parsers, services
from hardware.models import BmcEndpoint
from hardware.tests import fixtures
from hardware.tests.fake_redfish import FakeRedfish
from network.models import Device

PASSWORD = "hw-test-pass-123"
BMC_USER, BMC_PASS = "monitor", "correct-horse-battery"
try:
    from cryptography.fernet import Fernet
    KEY = Fernet.generate_key().decode()
except Exception:  # pragma: no cover
    KEY = ""
with_key = override_settings(LINKEDEYE_CREDENTIAL_KEY=KEY)
loopback = override_settings(PROBE_ALLOWED_PRIVATE_CIDRS=["127.0.0.0/8"])


def snapshot_of(docs=None, vendor_hint="", inventory=True):
    return parsers.parse(collector.collect(fixtures.FixtureClient(docs or fixtures.dell_idrac9()), vendor_hint, inventory=inventory))


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class HwCase(TestCase):
    """Sites Mumbai and Chennai; users: root (superuser), mum_admin / mum_op / mum_view (Mumbai only), che_admin (Chennai only)."""

    def setUp(self):
        super().setUp()
        cache.clear()
        User = get_user_model()
        self.mum = Site.objects.create(name="Mumbai", code="mum")
        self.che = Site.objects.create(name="Chennai", code="che")
        self.root = User.objects.create_superuser("root", "r@example.com", PASSWORD)
        self.mum_admin = self._user("mum-admin", self.mum, "admin")
        self.mum_op = self._user("mum-op", self.mum, "operator")
        self.mum_view = self._user("mum-view", self.mum, "viewer")
        self.che_admin = self._user("che-admin", self.che, "admin")
        self.srv_m = self.make_device("srv-mum", "Mumbai")
        self.srv_c = self.make_device("srv-che", "Chennai")
        self.ep_m = self.make_endpoint(self.srv_m)
        self.ep_c = self.make_endpoint(self.srv_c)

    def _user(self, name, site, role):
        u = get_user_model().objects.create_user(name, password=PASSWORD)
        Membership.objects.create(user=u, site=site, role=role)
        return u

    def make_device(self, name, site, ip="198.51.100.10", device_type="physical_server"):
        return Device.objects.create(tenant_key=services.tenant(), name=name, device_type=device_type, site=site, mgmt_ip=ip,
                                     vendor="Dell", model="PowerEdge R740")

    def make_endpoint(self, device, host="192.0.2.50", port=443, **kw):
        kw.setdefault("verify_mode", BmcEndpoint.VERIFY_PIN)
        kw.setdefault("pinned_sha256", "a" * 64)
        return BmcEndpoint.objects.create(tenant_key=services.tenant(), device=device, host=host, port=port, **kw)

    def login(self, user):
        c = Client()
        self.assertTrue(c.login(username=user.get_username(), password=PASSWORD))
        return c

    def store_login(self, ep, user=BMC_USER, password=BMC_PASS):
        return services.store_login(None, ep, user, password)

    def applied(self, ep=None, docs=None, now=None, **kw):
        """Apply a fixture snapshot to an endpoint (no network)."""
        ep = ep or self.ep_m
        return services.apply_snapshot(ep, snapshot_of(docs), now or timezone.now(), inventory=True, **kw)

    def ago(self, **kw):
        return timezone.now() - timedelta(**kw)

    @contextmanager
    def bmc(self, docs=None, tls=True, ep=None, pin=True, **kw):
        """A fake BMC on localhost wired into an endpoint (default: srv_m's) with a stored login."""
        with loopback, FakeRedfish(docs or fixtures.dell_idrac9(), tls=tls, **kw) as srv:
            ep = ep or self.ep_m
            ep.host, ep.port = "127.0.0.1", srv.port
            ep.pinned_sha256 = srv.fingerprint if pin else ""
            ep.insecure_http = not tls
            ep.save()
            self.store_login(ep)
            yield srv, ep
