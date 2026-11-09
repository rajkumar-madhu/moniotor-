"""Go-live readiness: the checks (administration/golive.py), `manage.py go_live_check`, the System health card."""
import http.server
import io
import json
import tempfile
import threading
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import Membership, Site

from . import golive
from .golive import FAIL, PASS, WARN, Context
from .models import Heartbeat

GOOD_KEY = "k8Qz-" + "".join(chr(ord("a") + (i * 7) % 26) + str(i % 10) for i in range(40))      # 85 chars, many distinct
FERNET = "ZmDfcTF7_60GrrY167zsiPd67pEvs0aGOv2oasOM1Pg="           # a valid Fernet key (32 url-safe base64 bytes)
SECRET_VALUES = ["Sup3r-Secret-SMTP-Pass-9183", "p0stgres-Pass-77213-xyz"]


class FreshCache(TestCase):
    """integration_config memoises the database overrides in the process cache for a minute: another test's rows must not leak in."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)


def result(key, **overrides):
    """Run one built-in check under the given settings and return its Result."""
    ctx = overrides.pop("ctx", None) or Context(env={})
    with override_settings(**overrides):
        out = golive.run_checks(only=[key], ctx=ctx)
    assert len(out) == 1, out
    return out[0]


class CheckTests(FreshCache):
    def test_debug(self):
        self.assertEqual(result("debug", DEBUG=True).status, FAIL)
        self.assertEqual(result("debug", DEBUG=False).status, PASS)

    def test_secret_key(self):
        for weak in ("dev-only-insecure-key", "short", "a" * 80, "change-me-" + "xY3" * 30, "<REPLACE: " + "q1Z" * 20):
            self.assertEqual(result("secret_key", SECRET_KEY=weak).status, FAIL, weak)
        r = result("secret_key", SECRET_KEY=GOOD_KEY)
        self.assertEqual(r.status, PASS)
        self.assertNotIn(GOOD_KEY, r.detail)

    def test_allowed_hosts(self):
        self.assertEqual(result("allowed_hosts", ALLOWED_HOSTS=[]).status, FAIL)
        self.assertEqual(result("allowed_hosts", ALLOWED_HOSTS=["*"]).status, FAIL)
        self.assertEqual(result("allowed_hosts", ALLOWED_HOSTS=["localhost", "web"]).status, WARN)
        self.assertEqual(result("allowed_hosts", ALLOWED_HOSTS=["linkedeye.example.com", "web"]).status, PASS)
        # the domain the deployment says it serves must be accepted, or every user gets 400
        ctx = Context(env={"LINKEDEYE_DOMAIN": "eye.example.org"})
        self.assertEqual(result("allowed_hosts", ALLOWED_HOSTS=["other.example.com"], ctx=ctx).status, FAIL)
        self.assertEqual(result("allowed_hosts", ALLOWED_HOSTS=["eye.example.org"], ctx=ctx).status, PASS)
        self.assertEqual(result("allowed_hosts", ALLOWED_HOSTS=[".example.org"], ctx=ctx).status, PASS)
        # localhost deployments (mode http) are a legitimate setup
        local = Context(env={"LINKEDEYE_DOMAIN": "localhost"})
        self.assertEqual(result("allowed_hosts", ALLOWED_HOSTS=["localhost", "web"], ctx=local).status, PASS)

    def test_csrf_origins(self):
        self.assertEqual(result("csrf_origins", DEBUG=False, CSRF_TRUSTED_ORIGINS=[]).status, WARN)
        self.assertEqual(result("csrf_origins", DEBUG=True, CSRF_TRUSTED_ORIGINS=[]).status, PASS)
        self.assertEqual(result("csrf_origins", CSRF_TRUSTED_ORIGINS=["linkedeye.example.com"]).status, FAIL)
        self.assertEqual(result("csrf_origins", CSRF_TRUSTED_ORIGINS=["https://a.example.com/path"]).status, FAIL)
        self.assertEqual(result("csrf_origins", CSRF_TRUSTED_ORIGINS=["https://a.example.com"]).status, PASS)
        ctx = Context(env={"LINKEDEYE_DOMAIN": "b.example.com"})
        self.assertEqual(result("csrf_origins", CSRF_TRUSTED_ORIGINS=["https://a.example.com"], ctx=ctx).status, WARN)
        self.assertEqual(result("csrf_origins", CSRF_TRUSTED_ORIGINS=["https://b.example.com:8443"], ctx=ctx).status, PASS)

    def test_https_cookies(self):
        self.assertEqual(result("https_cookies", SESSION_COOKIE_SECURE=True, CSRF_COOKIE_SECURE=True).status, PASS)
        self.assertEqual(result("https_cookies", SESSION_COOKIE_SECURE=True, CSRF_COOKIE_SECURE=False).status, WARN)
        r = result("https_cookies", SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False, ctx=Context(env={"LINKEDEYE_MODE": "http"}))
        self.assertEqual(r.status, WARN)
        self.assertIn("plain-HTTP mode", r.detail)

    def test_hsts(self):
        self.assertEqual(result("hsts", SECURE_HSTS_SECONDS=31536000).status, PASS)
        self.assertEqual(result("hsts", SECURE_HSTS_SECONDS=86400).status, PASS)
        self.assertEqual(result("hsts", SECURE_HSTS_SECONDS=60).status, WARN)
        self.assertEqual(result("hsts", SECURE_HSTS_SECONDS=0).status, WARN)
        self.assertIn("plain HTTP", result("hsts", SECURE_HSTS_SECONDS=0, ctx=Context(env={"LINKEDEYE_MODE": "http"})).detail)

    def test_trusted_proxies(self):
        both = Context(env={"GUNICORN_FORWARDED_ALLOW_IPS": "*"})
        self.assertEqual(result("trusted_proxies", DEBUG=False, TRUSTED_PROXIES=[]).status, WARN)
        self.assertEqual(result("trusted_proxies", DEBUG=True, TRUSTED_PROXIES=[]).status, PASS)
        self.assertEqual(result("trusted_proxies", TRUSTED_PROXIES=["*"], ctx=both).status, FAIL)
        self.assertEqual(result("trusted_proxies", TRUSTED_PROXIES=["10.0.0.0/8", "nonsense"], ctx=both).status, FAIL)
        self.assertEqual(result("trusted_proxies", TRUSTED_PROXIES=["10.0.0.0/8"]).status, WARN)          # gunicorn side missing
        self.assertEqual(result("trusted_proxies", TRUSTED_PROXIES=["10.0.0.0/8", "::1"], ctx=both).status, PASS)

    def test_trusted_proxies_must_not_be_everyone(self):
        both = Context(env={"GUNICORN_FORWARDED_ALLOW_IPS": "*"})
        for everyone in (["0.0.0.0/0"], ["::/0"], ["10.0.0.0/8", "0.0.0.0/1"], ["4.0.0.0/6"]):
            r = result("trusted_proxies", TRUSTED_PROXIES=everyone, ctx=both)
            self.assertEqual(r.status, FAIL, everyone)
            self.assertIn("forge", r.detail)
        for proxy_ranges in (["10.0.0.0/8"], ["172.16.0.0/12", "192.168.0.0/16", "fc00::/7", "127.0.0.1", "::1"]):
            self.assertEqual(result("trusted_proxies", TRUSTED_PROXIES=proxy_ranges, ctx=both).status, PASS, proxy_ranges)

    def test_database_password_must_not_be_a_default(self):
        from django.db.backends.sqlite3.base import DatabaseWrapper
        for pw, expected in (("", WARN), ("postgres", WARN), ("short-pw-1", WARN), ("Change-Me-Please-123", WARN),
                             ("Zq8vN2rT5yLw9XkP3mB7", PASS)):
            with mock.patch.object(DatabaseWrapper, "vendor", "postgresql"), \
                    mock.patch("administration.services._db_version", return_value="PostgreSQL 16"), \
                    mock.patch.dict("django.db.connection.settings_dict", {"PASSWORD": pw}):
                r = golive.run_checks(only=["database"], ctx=Context(env={}))[0]
            self.assertEqual(r.status, expected, pw)
            self.assertNotIn(pw or "\0", r.detail)          # the value itself is never printed

    def test_sample_pages_switch(self):
        self.assertEqual(result("sample_pages", DEBUG=False, SHOW_SAMPLE_PAGES=True).status, WARN)
        self.assertEqual(result("sample_pages", DEBUG=False, SHOW_SAMPLE_PAGES=False).status, PASS)
        self.assertEqual(result("sample_pages", DEBUG=True, SHOW_SAMPLE_PAGES=True).status, PASS)

    def test_database_on_sqlite_is_a_warning(self):
        r = golive.run_checks(only=["database"], ctx=Context(env={}))[0]
        self.assertIn(r.status, (WARN, PASS))          # SQLite or PostgreSQL depending on the CI job; never FAIL while reachable
        self.assertNotEqual(r.status, FAIL)

    def test_database_unreachable(self):
        with mock.patch("django.db.backends.base.base.BaseDatabaseWrapper.cursor", side_effect=RuntimeError("boom")):
            r = golive.run_checks(only=["database"], ctx=Context(env={}))[0]
        self.assertEqual(r.status, FAIL)
        self.assertNotIn("boom", r.detail)

    def test_migrations(self):
        self.assertEqual(golive.run_checks(only=["migrations"], ctx=Context(env={}))[0].status, PASS)
        with mock.patch("administration.services.pending_migrations", return_value=[("core", "0099_x")]):
            r = golive.run_checks(only=["migrations"], ctx=Context(env={}))[0]
        self.assertEqual(r.status, FAIL)
        self.assertIn("core", r.detail)
        with mock.patch("administration.services.pending_migrations", side_effect=RuntimeError):
            self.assertEqual(golive.run_checks(only=["migrations"], ctx=Context(env={}))[0].status, FAIL)

    def test_credential_key(self):
        self.assertEqual(result("credential_key", LINKEDEYE_CREDENTIAL_KEY="").status, WARN)
        bad = result("credential_key", LINKEDEYE_CREDENTIAL_KEY="not-a-fernet-key")
        self.assertEqual(bad.status, FAIL)
        self.assertNotIn("not-a-fernet-key", bad.detail + bad.fix)
        good = result("credential_key", LINKEDEYE_CREDENTIAL_KEY=FERNET)
        self.assertEqual(good.status, PASS)
        self.assertNotIn(FERNET, good.detail)
        self.assertIn("1 key", result("credential_key", LINKEDEYE_CREDENTIAL_KEY=FERNET).detail)

    def test_smtp(self):
        console = "django.core.mail.backends.console.EmailBackend"
        smtp = "django.core.mail.backends.smtp.EmailBackend"
        self.assertEqual(result("smtp", LINKEDEYE_LOGIN_OTP=True, EMAIL_BACKEND=console).status, FAIL)
        self.assertEqual(result("smtp", LINKEDEYE_LOGIN_OTP=False, EMAIL_BACKEND=console).status, WARN)
        self.assertEqual(result("smtp", LINKEDEYE_LOGIN_OTP=True, EMAIL_BACKEND=smtp, EMAIL_HOST="localhost").status, FAIL)
        self.assertEqual(result("smtp", LINKEDEYE_LOGIN_OTP=False, EMAIL_BACKEND=smtp, EMAIL_HOST="").status, WARN)
        placeholder_sender = result("smtp", LINKEDEYE_LOGIN_OTP=True, EMAIL_BACKEND=smtp, EMAIL_HOST="smtp.example.net",
                                    DEFAULT_FROM_EMAIL="linkedeye@localhost")
        self.assertEqual(placeholder_sender.status, WARN)
        ok = result("smtp", LINKEDEYE_LOGIN_OTP=True, EMAIL_BACKEND=smtp, EMAIL_HOST="smtp.example.net", EMAIL_PORT=587,
                    EMAIL_HOST_PASSWORD=SECRET_VALUES[0], DEFAULT_FROM_EMAIL="eye@corp.example.net")
        self.assertEqual(ok.status, PASS)
        self.assertNotIn(SECRET_VALUES[0], ok.detail + ok.fix)

    def test_otp_mode(self):
        self.assertEqual(result("otp_mode", DEBUG=False, LINKEDEYE_LOGIN_OTP=False).status, WARN)
        self.assertEqual(result("otp_mode", DEBUG=True, LINKEDEYE_LOGIN_OTP=False).status, PASS)
        self.assertEqual(result("otp_mode", LINKEDEYE_LOGIN_OTP=True).status, PASS)
        User.objects.create_superuser("root", "", "pw-123456-xyz")          # an administrator without a mailbox cannot receive the code
        r = result("otp_mode", LINKEDEYE_LOGIN_OTP=True)
        self.assertEqual(r.status, WARN)
        self.assertIn("no e-mail", r.detail)

    def test_admin_user_and_sites(self):
        self.assertEqual(golive.run_checks(only=["admin_user"], ctx=Context(env={}))[0].status, FAIL)
        User.objects.create_superuser("root", "root@example.com", "pw-123456-xyz")
        self.assertEqual(golive.run_checks(only=["admin_user"], ctx=Context(env={}))[0].status, WARN)      # no site yet
        Site.objects.create(name="HQ", code="hq")
        self.assertEqual(golive.run_checks(only=["admin_user"], ctx=Context(env={}))[0].status, PASS)

    def test_a_site_admin_counts_as_administrator(self):
        site = Site.objects.create(name="HQ", code="hq")
        u = User.objects.create_user("siteadmin", "s@example.com", "pw-123456-xyz")
        Membership.objects.create(user=u, site=site, role="admin")
        self.assertEqual(golive.run_checks(only=["admin_user"], ctx=Context(env={}))[0].status, PASS)
        User.objects.filter(pk=u.pk).update(is_active=False)
        self.assertEqual(golive.run_checks(only=["admin_user"], ctx=Context(env={}))[0].status, FAIL)

    def test_prometheus(self):
        from core.prometheus import PrometheusError
        self.assertEqual(result("prometheus", PROMETHEUS_URL="").status, WARN)
        with mock.patch.object(golive, "_tcp_ok", return_value=(False, "Connection refused")):
            r = result("prometheus", PROMETHEUS_URL="http://user:pw-9988@prom.example.net:9090")
        self.assertEqual(r.status, WARN)
        self.assertNotIn("pw-9988", r.detail)          # never echo credentials embedded in the URL
        with mock.patch.object(golive, "_tcp_ok", return_value=(True, "")), \
                mock.patch("core.prometheus.query", return_value=[{"metric": {}, "value": 1.0}]) as q:
            self.assertEqual(result("prometheus", PROMETHEUS_URL="http://prom:9090").status, PASS)
        q.assert_called_once_with("vector(1)")
        with mock.patch.object(golive, "_tcp_ok", return_value=(True, "")), \
                mock.patch("core.prometheus.query", side_effect=PrometheusError("Prometheus returned HTTP 401")):
            r = result("prometheus", PROMETHEUS_URL="http://prom:9090")
        self.assertEqual(r.status, WARN)
        self.assertIn("401", r.detail)

    def test_worker(self):
        ctx = Context(env={})
        self.assertEqual(golive.run_checks(only=["worker"], ctx=ctx)[0].status, WARN)          # never reported
        Heartbeat.objects.create(key="scheduler", host="worker-1", pid=1, data={"lease": "held"})
        r = golive.run_checks(only=["worker"], ctx=ctx)[0]
        self.assertEqual(r.status, PASS)
        self.assertIn("worker-1", r.detail)
        Heartbeat.objects.filter(key="scheduler").update(seen_at=timezone.now() - timedelta(minutes=10))
        self.assertEqual(golive.run_checks(only=["worker"], ctx=ctx)[0].status, FAIL)          # it was alive and stopped

    def test_static_files(self):
        self.assertEqual(result("static_files", DEBUG=True).status, PASS)
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(result("static_files", DEBUG=False, STATIC_ROOT=root).status, FAIL)
            (Path(root) / "linkedeye").mkdir()
            (Path(root) / "linkedeye" / "app.css").write_text("x{}")
            self.assertEqual(result("static_files", DEBUG=False, STATIC_ROOT=root).status, PASS)
        self.assertEqual(result("static_files", DEBUG=False, STATIC_ROOT="/nonexistent/static").status, FAIL)

    def test_probe_allowlist(self):
        self.assertEqual(result("probe_allowlist", PROBE_ALLOWED_PRIVATE_CIDRS=[]).status, WARN)
        self.assertEqual(result("probe_allowlist", PROBE_ALLOWED_PRIVATE_CIDRS=["10.0.0.0/8"]).status, PASS)
        self.assertEqual(result("probe_allowlist", PROBE_ALLOWED_PRIVATE_CIDRS=["10.0.0.0/8", "lan"]).status, FAIL)

    def test_alertmanager_token(self):
        self.assertEqual(result("alertmanager_token", PROMETHEUS_URL="").status, PASS)
        self.assertEqual(result("alertmanager_token", PROMETHEUS_URL="http://prom:9090", ALERTMANAGER_WEBHOOK_TOKEN="").status, WARN)
        self.assertEqual(result("alertmanager_token", PROMETHEUS_URL="http://prom:9090", ALERTMANAGER_WEBHOOK_TOKEN="t0ken").status, PASS)

    def test_django_deploy_checks_ignore_what_other_checks_cover(self):
        r = result("django_deploy", DEBUG=False, SECRET_KEY=GOOD_KEY, ALLOWED_HOSTS=["a.example.com"],
                   SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False, SECURE_HSTS_SECONDS=0)
        self.assertNotIn("W012", r.detail)
        self.assertNotIn("W016", r.detail)
        self.assertNotIn("W004", r.detail)


class RegistryTests(FreshCache):
    def tearDown(self):
        super().tearDown()
        golive.unregister("plugin_ok")
        golive.unregister("plugin_boom")
        golive.unregister("plugin_many")

    def test_other_apps_can_add_checks_without_editing_this_module(self):
        @golive.register("plugin_ok", "Example data")
        def plugin(ctx):
            return golive.warn("plugin_ok", "Example data", "12 example records", "manage.py remove_examples --yes")

        results = {r.key: r for r in golive.run_checks()}
        self.assertEqual(results["plugin_ok"].status, WARN)
        self.assertIn("plugin_ok", golive.check_keys())
        self.assertEqual(golive.overall([results["plugin_ok"]]), WARN)

    def test_a_check_may_return_several_results(self):
        golive.register("plugin_many")(lambda ctx: [golive.ok("plugin_many", "A", "x"), golive.fail("plugin_many", "B", "y")])
        out = golive.run_checks(only=["plugin_many"])
        self.assertEqual([r.status for r in out], [PASS, FAIL])

    def test_a_crashing_check_is_reported_not_raised(self):
        def boom(ctx):
            raise ValueError("secret detail Sup3r-Secret")
        golive.register("plugin_boom", "Boom")(boom)
        out = golive.run_checks(only=["plugin_boom"])
        self.assertEqual(out[0].status, FAIL)
        self.assertIn("ValueError", out[0].detail)
        self.assertNotIn("Sup3r-Secret", out[0].detail)          # only the class name, never the message

    def test_only_and_skip(self):
        keys = [r.key for r in golive.run_checks(only=["debug", "hsts"], ctx=Context(env={}))]
        self.assertEqual(keys, ["debug", "hsts"])
        self.assertNotIn("debug", [r.key for r in golive.run_checks(skip=["debug"], ctx=Context(env={}))])

    def test_counts_and_overall(self):
        rs = [golive.ok("a", "A", ""), golive.warn("b", "B", ""), golive.warn("c", "C", "")]
        self.assertEqual(golive.counts(rs), {PASS: 1, WARN: 2, FAIL: 0})
        self.assertEqual(golive.overall(rs), WARN)
        self.assertEqual(golive.overall(rs + [golive.fail("d", "D", "")]), FAIL)
        self.assertEqual(golive.overall([]), PASS)


class CommandTests(FreshCache):
    def run_cmd(self, *args, **kwargs):
        out = io.StringIO()
        try:
            call_command("go_live_check", *args, stdout=out, stderr=io.StringIO(), no_color=True, **kwargs)
            return out.getvalue(), 0
        except CommandError as exc:
            return out.getvalue(), exc.returncode

    @override_settings(DEBUG=True)
    def test_fails_with_status_1_when_something_fails(self):
        out, rc = self.run_cmd()
        self.assertEqual(rc, 1)
        self.assertIn("FAIL  DEBUG off", out)
        self.assertRegex(out, r"\d+ PASS, \d+ WARN, \d+ FAIL")
        self.assertIn("fix: set LINKEDEYE_DEBUG=false", out)

    @override_settings(DEBUG=False, SECRET_KEY=GOOD_KEY)
    def test_json_and_strict(self):
        out, rc = self.run_cmd("--json", "--only", "debug,secret_key,hsts")
        data = json.loads(out)
        self.assertEqual(rc, 0)
        self.assertEqual(data["overall"], WARN)                      # hsts is 0 here
        self.assertEqual({r["key"] for r in data["results"]}, {"debug", "secret_key", "hsts"})
        self.assertEqual(self.run_cmd("--only", "debug,secret_key,hsts", "--strict")[1], 1)
        self.assertEqual(self.run_cmd("--only", "debug,secret_key")[1], 0)

    def test_list_and_unknown_key(self):
        out, rc = self.run_cmd("--list")
        self.assertEqual(rc, 0)
        for key in ("debug", "secret_key", "worker", "smtp", "prometheus", "migrations", "static_files", "otp_mode"):
            self.assertIn(key, out.split())
        with self.assertRaises(CommandError):
            call_command("go_live_check", "--only", "no_such_check", stdout=io.StringIO())

    @override_settings(DEBUG=True, SECRET_KEY="x" * 10, EMAIL_HOST_PASSWORD=SECRET_VALUES[0], LINKEDEYE_CREDENTIAL_KEY=FERNET)
    def test_never_prints_secret_values(self):
        out, _ = self.run_cmd()
        json_out, _ = self.run_cmd("--json")
        for secret in (SECRET_VALUES[0], FERNET, "xxxxxxxxxx"):
            self.assertNotIn(secret, out)
            self.assertNotIn(secret, json_out)

    def test_read_only_for_the_database(self):
        from core.models import AuditEvent
        before = (User.objects.count(), Site.objects.count(), AuditEvent.objects.count(), Heartbeat.objects.count())
        self.run_cmd()
        self.assertEqual(before, (User.objects.count(), Site.objects.count(), AuditEvent.objects.count(), Heartbeat.objects.count()))

    def test_wait_worker_returns_once_the_heartbeat_exists(self):
        Heartbeat.objects.create(key="scheduler", host="w", pid=1)
        with mock.patch("administration.management.commands.go_live_check.time") as clock:
            clock.monotonic.return_value = 0
            self.run_cmd("--wait-worker", "30", "--only", "worker")
        clock.sleep.assert_not_called()

    def test_wait_worker_gives_up_after_the_deadline(self):
        with mock.patch("administration.management.commands.go_live_check.time") as clock:
            clock.monotonic.side_effect = [0, 1, 2, 100]
            out, _ = self.run_cmd("--wait-worker", "5", "--only", "worker")
        self.assertTrue(clock.sleep.called)
        self.assertIn("Worker (run_probes)", out)

    def test_send_test_mail_uses_the_configured_backend(self):
        from django.core import mail
        with override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend", DEFAULT_FROM_EMAIL="eye@corp.example.net"):
            out, rc = self.run_cmd("--send-test-mail", "me@corp.example.net", "--only", "debug")
        self.assertIn("sent to me@corp.example.net", out)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["me@corp.example.net"])

    def test_send_test_mail_failure_is_a_fail_without_the_smtp_message(self):
        with mock.patch("django.core.mail.EmailMultiAlternatives.send", side_effect=OSError("auth failed for user eye pw Sup3r")):
            out, rc = self.run_cmd("--send-test-mail", "me@corp.example.net", "--only", "debug")
        self.assertEqual(rc, 1)
        self.assertIn("OSError", out)
        self.assertNotIn("Sup3r", out)


def serve(routes):
    """A throw-away HTTP server on 127.0.0.1; routes = {path: (status, headers, body)}."""
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            status, headers, body = routes.get(self.path, (404, {}, b""))
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


class UrlCheckTests(SimpleTestCase):
    def setUp(self):
        self.good = {"/healthz": (200, {}, b'{"status": "ok"}'), "/accounts/login/": (200, {}, b"<form>"),
                     "/static/linkedeye/app.css": (200, {}, b"a{}")}

    def run_url(self, routes):
        server, url = serve(routes)
        self.addCleanup(server.shutdown)
        return {r.key: r for r in golive.check_url(url, timeout=3)}

    def test_everything_up(self):
        res = self.run_url(self.good)
        self.assertEqual([res[k].status for k in ("url_healthz", "url_login", "url_static")], [PASS, PASS, PASS])
        self.assertNotIn("url_tls", res)          # localhost: plain HTTP is expected, no TLS warning

    def test_unhealthy_database_and_missing_static(self):
        res = self.run_url({**self.good, "/healthz": (500, {}, b"{}"), "/static/linkedeye/app.css": (404, {}, b"")})
        self.assertEqual(res["url_healthz"].status, FAIL)
        self.assertEqual(res["url_static"].status, FAIL)

    def test_bad_host_header_is_explained(self):
        res = self.run_url({**self.good, "/healthz": (400, {}, b"")})
        self.assertIn("LINKEDEYE_ALLOWED_HOSTS", res["url_healthz"].detail)

    def test_redirects_are_not_followed(self):
        res = self.run_url({**self.good, "/healthz": (301, {"Location": "https://elsewhere.example/healthz"}, b"")})
        self.assertEqual(res["url_healthz"].status, FAIL)

    def test_unreachable(self):
        server, url = serve({})
        server.shutdown()
        server.server_close()
        out = golive.check_url(url, timeout=2)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].status, FAIL)

    def test_command_reports_url_results(self):
        server, url = serve(self.good)
        self.addCleanup(server.shutdown)
        out = io.StringIO()
        try:
            call_command("go_live_check", "--only", "debug", "--url", url, stdout=out, stderr=io.StringIO(), no_color=True)
        except CommandError:
            pass
        self.assertIn("GET /healthz", out.getvalue())


@override_settings(DEBUG=False, SECRET_KEY=GOOD_KEY, ALLOWED_HOSTS=["testserver", "localhost"])
class HealthCardTests(FreshCache):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user("boss", "boss@example.com", "Corr3ct-Horse-Battery-77")
        Membership.objects.create(user=cls.admin, site=None, role="admin")
        cls.viewer = User.objects.create_user("view", "view@example.com", "Corr3ct-Horse-Battery-77")
        site = Site.objects.create(name="HQ", code="hq")
        Membership.objects.create(user=cls.viewer, site=site, role="viewer")

    def get(self, user):
        c = Client()
        c.force_login(user)
        return c.get(reverse("administration:health"))

    def test_card_is_on_the_health_page_for_administrators(self):
        r = self.get(self.admin)
        self.assertContains(r, "Go-live readiness")
        self.assertContains(r, "manage.py go_live_check")
        self.assertContains(r, "Worker (run_probes)")
        self.assertContains(r, "Secret key")
        self.assertEqual(r.context["golive"]["counts"][PASS] + r.context["golive"]["counts"][WARN] + r.context["golive"]["counts"][FAIL],
                         len(r.context["golive"]["results"]))

    def test_viewers_cannot_see_it(self):
        self.assertEqual(self.get(self.viewer).status_code, 403)

    def test_card_shows_plugin_checks(self):
        golive.register("plugin_ok", "Example data")(lambda ctx: golive.warn("plugin_ok", "Example data", "3 example records", "remove them"))
        self.addCleanup(golive.unregister, "plugin_ok")
        r = self.get(self.admin)
        self.assertContains(r, "Example data")
        self.assertContains(r, "3 example records")
        self.assertContains(r, "Fix: remove them")

    @override_settings(EMAIL_HOST_PASSWORD=SECRET_VALUES[0], LINKEDEYE_CREDENTIAL_KEY=FERNET, ALERTMANAGER_WEBHOOK_TOKEN="tok-AAAA-1234")
    def test_card_never_renders_secrets(self):
        html = self.get(self.admin).content.decode()
        for secret in (SECRET_VALUES[0], FERNET, GOOD_KEY, "tok-AAAA-1234"):
            self.assertNotIn(secret, html)

    def test_hostile_detail_is_escaped(self):
        golive.register("plugin_ok", "<script>alert(1)</script>")(
            lambda ctx: golive.warn("plugin_ok", "<script>alert(1)</script>", "<img src=x onerror=alert(2)>", "<b>fix</b>"))
        self.addCleanup(golive.unregister, "plugin_ok")
        html = self.get(self.admin).content.decode()
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;img src=x onerror=alert(2)&gt;", html)

    def test_a_dead_prometheus_does_not_hang_the_page(self):
        # fast mode: an unreachable server is found by a 1.5 s connect, the query is not even tried
        with override_settings(PROMETHEUS_URL="http://prom.invalid:9090"), \
                mock.patch.object(golive, "_tcp_ok", return_value=(False, "Name or service not known")) as tcp, \
                mock.patch("core.prometheus.query") as query:
            r = self.get(self.admin)
        self.assertContains(r, "not reachable from here")
        self.assertEqual(tcp.call_args.args[1], 1.5)
        query.assert_not_called()
