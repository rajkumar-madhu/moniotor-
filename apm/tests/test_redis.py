"""Path B, the Redis reader: parsing, errors, the SSRF policy, the optional package and the vault. A fake client replaces redis."""
import sys
from datetime import timedelta
from unittest import mock

from django.test import override_settings
from django.utils import timezone

from apm import redis_reader, services
from apm.models import AdapterCell, ProcessState, Source
from core import secrets as vault
from onboarding.models import Credential

from .base import ApmCase, matrix, table, with_key


class FakeRedis:
    """The few commands the reader uses; values are bytes like the real client returns."""

    def __init__(self, data=None, fail=None, auth_error=None):
        self.data = {k: (v.encode() if isinstance(v, str) else v) for k, v in (data or {}).items()}
        self.fail = fail
        self.calls = []
        self.closed = False

    def _guard(self, name, *args):
        self.calls.append((name, *args))
        if self.fail:
            raise self.fail

    def ping(self):
        self._guard("ping")
        return True

    def exists(self, key):
        self._guard("exists", key)
        return 1 if key in self.data else 0

    def strlen(self, key):
        self._guard("strlen", key)
        return len(self.data.get(key, b""))

    def get(self, key):
        self._guard("get", key)
        return self.data.get(key)

    def close(self):
        self.closed = True


def configure(src, **kw):
    values = dict(redis_poll=True, redis_host="redis.example.org", redis_prefix="site1", redis_adapter_suffix="ADP",
                  redis_process_suffix="PS")
    values.update(kw)
    for k, v in values.items():
        setattr(src, k, v)
    src.save()
    return src


class Reader(ApmCase):
    def setUp(self):
        super().setUp()
        configure(self.src_a)
        self.resolve = mock.patch("apm.redis_reader.resolve", return_value="203.0.113.9")
        self.resolve.start()
        self.addCleanup(self.resolve.stop)

    def poll(self, fake, **kw):
        with mock.patch("apm.redis_reader.connect", return_value=fake) as conn:
            out = redis_reader.poll_source(self.src_a, **kw)
        self.conn = conn
        return out

    def test_python_dict_string_values_feed_the_same_ingest(self):
        fake = FakeRedis({"site1:ADP": str(matrix(failing={("c001", "NSE")})), "site1:PS": str(table())})
        out = self.poll(fake)
        self.assertTrue(out["ok"], out)
        self.assertEqual(AdapterCell.objects.filter(source=self.src_a).count(), 4)
        self.assertEqual(ProcessState.objects.filter(source=self.src_a).count(), 2)
        self.assertTrue(fake.closed)
        self.src_a.refresh_from_db()
        self.assertEqual(self.src_a.redis_last_error, "")
        self.assertIsNotNone(self.src_a.redis_last_polled_at)

    def test_json_values_work_too_and_a_repeat_poll_is_a_duplicate(self):
        import json
        m = matrix()
        self.poll(FakeRedis({"site1:ADP": json.dumps(m)}))
        out = self.poll(FakeRedis({"site1:ADP": json.dumps(m)}))
        self.assertTrue(out["results"]["matrix"]["duplicate"])

    def test_events_are_emitted_like_the_push_path(self):
        from core.events import Event
        self.poll(FakeRedis({"site1:ADP": str(matrix(failing={("c001", "NSE")}))}))
        self.assertEqual(Event.objects.filter(source="apm", severity="critical").count(), 1)

    def test_missing_key_and_bad_value_are_recorded_not_raised(self):
        out = self.poll(FakeRedis({"site1:PS": "__import__('os').getcwd()"}))
        self.assertFalse(out["ok"])
        self.src_a.refresh_from_db()
        self.assertIn("key site1:ADP not found", self.src_a.redis_last_error)
        self.assertIn("site1:PS", self.src_a.redis_last_error)
        self.assertFalse(AdapterCell.objects.exists())

    def test_oversized_value_is_not_downloaded(self):
        fake = FakeRedis({"site1:ADP": b"x" * (1024 * 1024 + 5)})
        out = self.poll(fake)
        self.assertFalse(out["ok"])
        self.assertNotIn(("get", "site1:ADP"), fake.calls)

    def test_connection_errors_are_recorded_without_a_traceback(self):
        out = self.poll(FakeRedis(fail=ConnectionError("Error 111 connecting to 203.0.113.9:6379. Connection refused.")))
        self.assertFalse(out["ok"])
        self.src_a.refresh_from_db()
        self.assertIn("ConnectionError", self.src_a.redis_last_error)
        self.assertLessEqual(len(self.src_a.redis_last_error), 300)

    def test_the_error_never_contains_the_password(self):
        configure(self.src_a)
        secret = "hunter2-not-a-real-password"
        err = ConnectionError(f"AUTH failed for password {secret}")
        with mock.patch("apm.redis_reader._password", return_value=secret):
            out = self.poll(FakeRedis(fail=err))
        self.assertFalse(out["ok"])
        self.src_a.refresh_from_db()
        self.assertNotIn(secret, self.src_a.redis_last_error)
        self.assertNotIn(secret, str(out))
        self.assertIn("***", self.src_a.redis_last_error)

    def test_key_names_follow_prefix_and_suffix(self):
        self.assertEqual(redis_reader.key_names(self.src_a), [("matrix", "site1:ADP"), ("table", "site1:PS")])
        s = Source(redis_prefix="", redis_adapter_suffix="K")
        self.assertEqual(redis_reader.key_names(s), [("matrix", "K")])

    def test_hostile_key_names_are_refused(self):
        self.src_a.redis_adapter_suffix = "ADP\r\nFLUSHALL"
        self.src_a.redis_process_suffix = ""
        self.src_a.save()
        fake = FakeRedis()
        out = self.poll(fake)
        self.assertFalse(out["ok"])
        self.assertEqual(fake.calls, [])

    def test_not_configured_source(self):
        self.src_a.redis_host = ""
        self.src_a.save()
        out = redis_reader.poll_source(self.src_a)
        self.assertFalse(out["ok"])
        self.assertIn("host", out["errors"][0])

    def test_the_job_polls_only_enabled_sources_with_polling_on(self):
        configure(self.src_b, redis_poll=False)
        calls = []
        with mock.patch("apm.redis_reader.connect", side_effect=lambda *a: calls.append(a) or FakeRedis({"site1:ADP": str(matrix()), "site1:PS": str(table())})):
            summary = redis_reader.poll_all()
        self.assertEqual(len(calls), 1)
        self.assertIn("polled 1 source(s): 1 ok", summary)
        Source.objects.filter(pk=self.src_a.pk).update(enabled=False)
        self.assertEqual(redis_reader.poll_all(), "no sources to poll")

    def test_a_crash_in_one_source_does_not_stop_the_others(self):
        configure(self.src_b, redis_poll=True)
        real = redis_reader.poll_source

        def flaky(src, **kw):
            if src.pk == self.src_a.pk:
                raise RuntimeError("unexpected")
            return real(src, **kw)
        with mock.patch("apm.redis_reader.poll_source", side_effect=flaky), \
                mock.patch("apm.redis_reader.connect", return_value=FakeRedis({"site1:ADP": str(matrix()), "site1:PS": str(table())})), \
                mock.patch("apm.redis_reader.resolve", return_value="203.0.113.9"):
            summary = redis_reader.poll_all()
        self.assertIn("1 with errors", summary)
        self.assertTrue(AdapterCell.objects.filter(source=self.src_b).exists())

    def test_test_connection_reports_missing_keys(self):
        with mock.patch("apm.redis_reader.connect", return_value=FakeRedis({"site1:ADP": "x"})):
            ok, msg = redis_reader.test_source(self.src_a)
        self.assertFalse(ok)
        self.assertIn("site1:PS", msg)
        with mock.patch("apm.redis_reader.connect", return_value=FakeRedis({"site1:ADP": "x", "site1:PS": "y"})):
            self.assertTrue(redis_reader.test_source(self.src_a)[0])


class Policy(ApmCase):
    """The real resolve(): the same SSRF policy as every other module (nothing is connected: DNS is not available either)."""

    def setUp(self):
        super().setUp()
        configure(self.src_a)

    def test_loopback_private_and_metadata_hosts_are_refused(self):
        for host in ("127.0.0.1", "10.1.2.3", "169.254.169.254", "192.168.0.5", "100.100.100.200"):
            self.src_a.redis_host = host
            with mock.patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", (host, 6379))]):
                with self.assertRaises(redis_reader.ReaderError) as cm:
                    redis_reader.resolve(self.src_a)
            self.assertIn("network policy", str(cm.exception), host)

    def test_a_listed_private_range_is_allowed(self):
        self.src_a.redis_host = "10.1.2.3"
        with override_settings(PROBE_ALLOWED_PRIVATE_CIDRS=["10.1.0.0/16"]):
            with mock.patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("10.1.2.3", 6379))]):
                self.assertEqual(redis_reader.resolve(self.src_a), "10.1.2.3")

    def test_a_refused_host_is_recorded_and_never_connected(self):
        self.src_a.redis_host = "127.0.0.1"
        self.src_a.save()
        with mock.patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 6379))]), \
                mock.patch("apm.redis_reader.connect") as conn:
            out = redis_reader.poll_source(self.src_a)
        self.assertFalse(out["ok"])
        conn.assert_not_called()
        self.src_a.refresh_from_db()
        self.assertIn("LINKEDEYE_PROBE_ALLOWED_PRIVATE_CIDRS", self.src_a.redis_last_error)

    def test_unresolvable_host(self):
        import socket
        with mock.patch("socket.getaddrinfo", side_effect=socket.gaierror("no")):
            with self.assertRaises(redis_reader.ReaderError):
                redis_reader.resolve(self.src_a)

    def test_the_plain_connection_goes_to_the_checked_address_not_the_name(self):
        fake_module = mock.Mock()
        with mock.patch.dict(sys.modules, {"redis": fake_module}):
            redis_reader.connect(self.src_a, "203.0.113.9", "pw")
        kwargs = fake_module.Redis.call_args.kwargs
        self.assertEqual((kwargs["host"], kwargs["socket_timeout"], kwargs["socket_connect_timeout"]), ("203.0.113.9", 3, 3))
        self.assertNotIn("ssl", kwargs)
        self.src_a.redis_tls = True
        with mock.patch.dict(sys.modules, {"redis": fake_module}):
            redis_reader.connect(self.src_a, "203.0.113.9", "pw")
        kwargs = fake_module.Redis.call_args.kwargs
        self.assertEqual((kwargs["host"], kwargs["ssl"], kwargs["ssl_cert_reqs"]), ("redis.example.org", True, "required"))


class OptionalPackage(ApmCase):
    def setUp(self):
        super().setUp()
        configure(self.src_a)

    def test_without_the_package_nothing_else_breaks(self):
        with mock.patch.dict(sys.modules, {"redis": None}):
            self.assertFalse(redis_reader.available())
            with self.assertRaises(redis_reader.ReaderError) as cm:
                redis_reader.redis_module()
            self.assertIn("requirements-apm.txt", str(cm.exception))
            with mock.patch("apm.redis_reader.resolve", return_value="203.0.113.9"):
                out = redis_reader.poll_source(self.src_a)
            self.assertFalse(out["ok"])
            self.src_a.refresh_from_db()
            self.assertIn("requirements-apm.txt", self.src_a.redis_last_error)
            self.assertEqual(redis_reader.poll_all()[:6], "polled")
        # the push API and the pages work the same
        self.assertEqual(self.push_matrix(self.src_a).status_code, 200)
        self.assertEqual(self.login(self.root).get("/apm/").status_code, 200)

    def test_the_source_form_says_what_to_install(self):
        c = self.login(self.root)
        with mock.patch.dict(sys.modules, {"redis": None}):
            html = c.get(f"/apm/sources/{self.src_a.pk}/").content.decode()
            self.assertIn("requirements-apm.txt", html)
            import re
            self.assertRegex(html, re.compile(r"<button[^>]*disabled[^>]*>(?:(?!</button>).)*Test Redis connection", re.S))

    def test_the_module_is_not_imported_at_startup(self):
        import subprocess
        code = "import django,os;os.environ['LINKEDEYE_DEBUG']='true';os.environ['DJANGO_SETTINGS_MODULE']='linkedeye.settings';django.setup();import sys;print('redis' in sys.modules)"
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(__import__("pathlib").Path(redis_reader.__file__).resolve().parent.parent))
        self.assertEqual(out.stdout.strip().splitlines()[-1], "False", out.stderr[-400:])


@with_key
class Vault(ApmCase):
    def setUp(self):
        super().setUp()
        configure(self.src_a)
        self.c = self.login(self.root)

    def form(self, **kw):
        data = {"name": "Alpha", "site": "Mumbai", "description": "", "enabled": "on", "expected_interval_s": "300", "redis_poll": "on",
                "redis_host": "redis.example.org", "redis_port": "6379", "redis_db": "0", "redis_prefix": "site1",
                "redis_adapter_suffix": "ADP", "redis_process_suffix": "PS", "redis_username": "", "redis_password": ""}
        data.update(kw)
        return data

    def test_the_password_goes_to_the_vault_and_is_never_echoed(self):
        secret = "s3cret-redis-pass-9"
        r = self.c.post(f"/apm/sources/{self.src_a.pk}/", self.form(redis_password=secret))
        self.assertEqual(r.status_code, 302, r.content[:300])
        self.src_a.refresh_from_db()
        row = Credential.objects.get(pk=self.src_a.redis_credential_id)
        self.assertNotIn(secret, row.secret)
        self.assertEqual(vault.decrypt(row.secret), secret)
        self.assertEqual(row.site, "Mumbai")
        page = self.c.get(f"/apm/sources/{self.src_a.pk}/").content.decode()
        self.assertNotIn(secret, page)
        self.assertNotIn(row.secret, page)
        self.assertIn("A password is stored", page)
        # a failed validation never writes the typed password back into the form
        bad = self.c.post(f"/apm/sources/{self.src_a.pk}/", self.form(redis_password="typed-but-invalid", redis_host="http://nope")).content.decode()
        self.assertNotIn("typed-but-invalid", bad)

    def test_poll_uses_the_decrypted_password_and_logs_nothing_secret(self):
        secret = "s3cret-redis-pass-9"
        self.c.post(f"/apm/sources/{self.src_a.pk}/", self.form(redis_password=secret))
        self.src_a.refresh_from_db()
        seen = {}

        def fake_connect(src, ip, password):
            seen["pw"] = password
            return FakeRedis({"site1:ADP": str(matrix())})
        with mock.patch("apm.redis_reader.resolve", return_value="203.0.113.9"), mock.patch("apm.redis_reader.connect", side_effect=fake_connect), \
                self.assertLogs("linkedeye", level="DEBUG") as logs:
            import logging
            logging.getLogger("linkedeye.apm.redis").info("poll")
            redis_reader.poll_source(self.src_a)
        self.assertEqual(seen["pw"], secret)
        self.assertNotIn(secret, "\n".join(logs.output))
        from core.models import AuditEvent
        self.assertFalse(any(secret in str(e.details) for e in AuditEvent.objects.all()))

    @override_settings(LINKEDEYE_CREDENTIAL_KEY="")
    def test_without_a_vault_key_the_password_is_refused(self):
        r = self.c.post(f"/apm/sources/{self.src_a.pk}/", self.form(redis_password="whatever"))
        self.assertEqual(r.status_code, 200)
        self.assertIn("LINKEDEYE_CREDENTIAL_KEY", r.content.decode())
        self.assertFalse(Credential.objects.exists())

    def test_clearing_and_deleting_remove_the_vault_row(self):
        self.c.post(f"/apm/sources/{self.src_a.pk}/", self.form(redis_password="pw-pw-pw-1"))
        self.assertEqual(Credential.objects.count(), 1)
        self.c.post(f"/apm/sources/{self.src_a.pk}/", self.form(clear_password="on"))
        self.assertEqual(Credential.objects.count(), 0)
        self.c.post(f"/apm/sources/{self.src_a.pk}/", self.form(redis_password="pw-pw-pw-2"))
        self.assertEqual(Credential.objects.count(), 1)
        self.c.post(f"/apm/sources/{self.src_a.pk}/delete/")
        self.assertEqual(Credential.objects.count(), 0)

    def test_an_undecryptable_password_is_a_readable_error(self):
        self.c.post(f"/apm/sources/{self.src_a.pk}/", self.form(redis_password="pw-pw-pw-1"))
        self.src_a.refresh_from_db()
        with override_settings(LINKEDEYE_CREDENTIAL_KEY=vault.new_key()):
            out = redis_reader.poll_source(self.src_a)
        self.assertFalse(out["ok"])
        self.assertIn("LINKEDEYE_CREDENTIAL_KEY", out["errors"][0])


# ----------------------------------------------------------------------------- over a real loopback socket
from django.test import TestCase  # noqa: E402

from .base import PASSWORD  # noqa: E402,F401
from .fake_redis import FakeRedisServer, stub_module  # noqa: E402


@with_key
@override_settings(PROBE_ALLOWED_PRIVATE_CIDRS=["127.0.0.0/8"])
class OverASocket(TestCase):
    """The reader end to end against a RESP server on loopback, with a stand-in ``redis`` module (the real package is optional)."""

    def setUp(self):
        from django.contrib.auth import get_user_model
        from core.models import Site
        self.root = get_user_model().objects.create_superuser("root", "r@example.com", "x")
        self.src = Source(tenant_key=services.tenant(), name="Sock", site="All Sites", redis_poll=True, redis_host="127.0.0.1",
                          redis_prefix="s", redis_adapter_suffix="ADP", redis_process_suffix="PS")
        services.issue_token(self.src)
        self.src.save()
        patch = mock.patch.dict(sys.modules, {"redis": stub_module()})
        patch.start()
        self.addCleanup(patch.stop)

    def server(self, data=None, password=None):
        srv = FakeRedisServer(data, password).start()
        self.addCleanup(srv.stop)
        self.src.redis_port = srv.port
        self.src.save()
        return srv

    def test_reads_both_keys_and_ingests(self):
        srv = self.server({"s:ADP": str(matrix(failing={("c001", "NSE")})), "s:PS": str(table())})
        out = redis_reader.poll_source(self.src)
        self.assertTrue(out["ok"], out)
        self.assertEqual(AdapterCell.objects.filter(source=self.src).count(), 4)
        self.assertEqual(ProcessState.objects.filter(source=self.src).count(), 2)
        self.assertEqual(sorted(set(srv.commands)), ["GET", "STRLEN"])      # nothing else is ever sent
        self.assertTrue(redis_reader.test_source(self.src)[0])

    def test_password_from_the_vault_is_sent_and_a_wrong_one_is_reported_safely(self):
        from onboarding.models import Credential
        srv = self.server({"s:ADP": str(matrix()), "s:PS": str(table())}, password="right-pass-1")
        row = vault.store("bearer", "redis", "right-pass-1", "All Sites", None, name="APM Redis password: Sock")
        self.src.redis_credential_id = row.pk
        self.src.save()
        self.assertTrue(redis_reader.poll_source(self.src)["ok"])
        self.assertIn("AUTH", srv.commands)
        row.secret = vault.encrypt("WRONG-pass-2")
        row.save()
        out = redis_reader.poll_source(self.src)
        self.assertFalse(out["ok"])
        self.src.refresh_from_db()
        self.assertIn("AuthenticationError", self.src.redis_last_error)
        self.assertNotIn("WRONG-pass-2", self.src.redis_last_error)
        self.assertNotIn("right-pass-1", self.src.redis_last_error)

    def test_missing_password_is_a_clear_error(self):
        self.server({"s:ADP": "x"}, password="needed")
        out = redis_reader.poll_source(self.src)
        self.assertFalse(out["ok"])
        self.assertIn("NOAUTH", "; ".join(out["errors"]))

    def test_a_server_that_is_down_is_recorded(self):
        self.src.redis_port = 1          # nothing listens here
        self.src.save()
        out = redis_reader.poll_source(self.src)
        self.assertFalse(out["ok"])
        self.src.refresh_from_db()
        self.assertIn("ConnectionError", self.src.redis_last_error)

    def test_loopback_is_refused_without_the_opt_in(self):
        self.server({"s:ADP": "x"})
        with override_settings(PROBE_ALLOWED_PRIVATE_CIDRS=[]):
            out = redis_reader.poll_source(self.src)
        self.assertFalse(out["ok"])
        self.assertIn("network policy", out["errors"][0])
