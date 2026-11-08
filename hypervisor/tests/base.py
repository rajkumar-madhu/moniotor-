"""Shared fixtures: two sites, one user per role, a source with a vault login, devices, a fake vCenter on localhost."""
import logging
from contextlib import contextmanager
from datetime import timedelta
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from core.models import Membership, Site
from hypervisor import services, sync, vsphere
from hypervisor.models import VCenterSource
from hypervisor.tests.fake_vcenter import PASSWORD as VC_PASSWORD, USER as VC_USER, FakeVCenter
from network.models import Device

PASSWORD = "hv-test-pass-123"
try:
    from cryptography.fernet import Fernet
    KEY = Fernet.generate_key().decode()
except Exception:  # pragma: no cover
    KEY = ""
LOCAL = override_settings(PROBE_ALLOWED_PRIVATE_CIDRS=["127.0.0.0/8"], LINKEDEYE_CREDENTIAL_KEY=KEY)


@contextmanager
def capture_logs():
    """Everything logged (any logger, any level) while the block runs, as one string."""
    buf = StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.DEBUG)
    root = logging.getLogger()
    old = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield buf
    finally:
        root.removeHandler(handler)
        root.setLevel(old)


@LOCAL
@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class HvCase(TestCase):
    """Sites Mumbai and Chennai; users root (superuser), mum_admin / mum_op / mum_view (Mumbai), che_admin (Chennai), glob_view."""

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
        self.glob_view = User.objects.create_user("glob-view", password=PASSWORD)

    def _user(self, name, site, role):
        u = get_user_model().objects.create_user(name, password=PASSWORD)
        Membership.objects.create(user=u, site=site, role=role)
        return u

    def login(self, user):
        c = Client()
        self.assertTrue(c.login(username=user.get_username(), password=PASSWORD))
        return c

    def make_source(self, fake=None, name="vc-mumbai", site="Mumbai", tls_mode="skip", password=VC_PASSWORD, **kw):
        src = VCenterSource.objects.create(tenant_key=services.tenant(), name=name, site=site, host=(fake.host if fake else "127.0.0.1"),
                                           port=(fake.port if fake else 443), tls_mode=tls_mode, **kw)
        services.store_login(None, src, VC_USER, password)
        return src

    def make_vm_device(self, name, site="Mumbai", ip="10.20.1.15", **kw):
        kw.setdefault("device_type", "virtual_machine")
        return Device.objects.create(tenant_key=services.tenant(), name=name, mgmt_ip=ip, site=site, **kw)

    def run_sync(self, src, now=None, **kw):
        src.refresh_from_db()
        return sync.sync_source(src, now=now, **kw)

    def ago(self, **kw):
        return timezone.now() - timedelta(**kw)


class FakeProvider:
    """A provider with canned data (no pyvmomi): ``data`` maps moref -> {"snapshots": [...], "events": [...], "extras": {...}}."""
    name = "fake"

    def __init__(self, data=None, fail=None):
        self.data, self.fail = data if data is not None else {}, fail
        self.closed = False

    def fetch(self, morefs, since):
        if self.fail:
            raise self.fail
        out = {"snapshots": {}, "events": {}, "extras": {}}
        for ref in morefs:
            d = self.data.get(ref)
            if d is None:
                continue
            out["snapshots"][ref] = d.get("snapshots", [])
            out["events"][ref] = d.get("events", [])
            out["extras"][ref] = d.get("extras", {})
        return out

    def close(self):
        self.closed = True


def provider_factory(provider):
    return lambda source, reader: provider


def snap(snap_id, name, days_old, now=None, size=None, current=False):
    now = now or timezone.now()
    return {"snap_id": snap_id, "name": name, "description": f"{name} snapshot", "created_at": now - timedelta(days=days_old),
            "size_bytes": size, "is_current": current}
