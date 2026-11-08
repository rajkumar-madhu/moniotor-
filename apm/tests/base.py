"""Shared fixtures: two sites, one user per role, two sources with tokens, and builders for the toolkit's payloads. No test opens
a socket (a real connection attempt fails the test), and the cache is cleared so rate limits start fresh."""
import json
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from apm import services
from apm.models import Source
from core.models import Membership, Site

PASSWORD = "apm-test-pass-123"
try:
    from cryptography.fernet import Fernet
    KEY = Fernet.generate_key().decode()
except Exception:  # pragma: no cover
    KEY = ""
with_key = override_settings(LINKEDEYE_CREDENTIAL_KEY=KEY)


def stamp(at=None, fmt="%d-%m-%Y %H:%M:%S"):
    at = at or timezone.now()
    return timezone.localtime(at).strftime(fmt)


def cell(ctcl="10001", status=2, **extra):
    d = {"isSuccess": status == 2, "ctcl_id": ctcl, "value": ctcl, "status": status}
    d.update(extra)
    return d


def matrix(at=None, failing=(), warning=(), clients=("c001", "c002"), segments=("NSE", "NFO"), extra=None):
    """The toolkit's adapter matrix dict (synthetic codes)."""
    data = {}
    n = 0
    for c in clients:
        row = {}
        for s in segments:
            n += 1
            key = (c, s)
            status = 0 if key in failing else 1 if key in warning else 2
            row[s] = cell(str(10000 + n), status, **({"error": "boom"} if status == 0 else {}))
        data[c] = row
    out = {"overallStatus": not failing, "status": 0 if failing else 2,
           "executedOn": stamp(at) + f" [ Total: {n} ; Success: {n - len(failing)} ; Failure: {len(failing)} ]",
           "type": "matrix", "data": data}
    out.update(extra or {})
    return out


def table(at=None, down=(), names=("adapterStatus.py", "processStatus.py"), host="demo-host-01"):
    rows = []
    for i, name in enumerate(names):
        up = name not in down
        rows.append({"pid": 100 + i if up else "NA", "name": name, "cpu_percent": 1.5 if up else "NA",
                     "memory_percent": 0.5 if up else "NA", "status": 2 if up else 0,
                     "Status_": "running" if up else "not running", "host": host})
    return {"overallStatus": not down, "status": 0 if down else 2,
            "executedOn": stamp(at, "%Y-%m-%d %H:%M:%S") + f" [ Total: {len(rows)} ; Success: {len(rows) - len(down)} ; Failure: {len(down)} ]",
            "type": "table", "data": rows}


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ApmCase(TestCase):
    """Sites Mumbai and Chennai; users: root (superuser), mum_admin / mum_op / mum_view (Mumbai only), che_admin (Chennai only),
    glob_view (viewer on every site)."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self._no_network()
        User = get_user_model()
        self.mum = Site.objects.create(name="Mumbai", code="mum")
        self.che = Site.objects.create(name="Chennai", code="che")
        self.root = User.objects.create_superuser("root", "r@example.com", PASSWORD)
        self.mum_admin = self._user("mum-admin", self.mum, "admin")
        self.mum_op = self._user("mum-op", self.mum, "operator")
        self.mum_view = self._user("mum-view", self.mum, "viewer")
        self.che_admin = self._user("che-admin", self.che, "admin")
        self.glob_view = User.objects.create_user("glob-view", password=PASSWORD)
        self.src_a = self.make_source("Alpha", "Mumbai")
        self.src_b = self.make_source("Bravo", "Chennai")

    def _user(self, name, site, role):
        u = get_user_model().objects.create_user(name, password=PASSWORD)
        Membership.objects.create(user=u, site=site, role=role)
        return u

    def _no_network(self):
        def refuse(*args, **kwargs):
            raise AssertionError("a test tried to open a network connection")
        for target in ("socket.socket.connect", "socket.create_connection", "socket.getaddrinfo"):
            p = mock.patch(target, side_effect=refuse)
            p.start()
            self.addCleanup(p.stop)

    def make_source(self, name, site="All Sites", **kw):
        src = Source(tenant_key=services.tenant(), name=name, site=site, **kw)
        src.token_plain = services.issue_token(src)       # test-only attribute; never stored
        src.save()
        return src

    def login(self, user):
        c = Client()
        self.assertTrue(c.login(username=user.get_username(), password=PASSWORD))
        return c

    # ---- the machine API
    def push(self, src_or_token, body, token=None, client=None, **extra):
        tok = token if token is not None else getattr(src_or_token, "token_plain", src_or_token)
        c = client or Client()
        headers = {"HTTP_AUTHORIZATION": f"Bearer {tok}"} if tok else {}
        return c.post("/api/apm/ingest/", data=json.dumps(body), content_type="application/json", **headers, **extra)

    def push_matrix(self, src, payload=None, **kw):
        return self.push(src, {"type": "matrix", "payload": payload or matrix(), "host": "h1", "script": "adapterStatus.py", **kw})

    def push_table(self, src, payload=None, **kw):
        return self.push(src, {"type": "table", "payload": payload or table(), **kw})

    def ago(self, **kw):
        return timezone.now() - timedelta(**kw)
