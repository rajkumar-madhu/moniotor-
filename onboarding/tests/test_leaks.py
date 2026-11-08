"""A stored secret is never rendered, returned by a form, logged, exported or put in an error."""
import logging
from unittest import mock

from django.test import override_settings
from django.urls import reverse

from core import secrets
from core.models import AuditEvent
from onboarding import steps
from onboarding.forms import CredentialForm, RotateForm
from onboarding.models import Credential, CredentialKind, StepLog

from .base import SECRET, OnboardingTestCase, with_key, without_key

KEY_PEM = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAABG5vbmU-pem-body-zz\n-----END OPENSSH PRIVATE KEY-----"


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.lines = []

    def emit(self, record):
        try:
            self.lines.append(record.getMessage())
            if record.exc_info:
                self.lines.append(logging.Formatter().formatException(record.exc_info))
        except Exception:        # a bad format string must not hide a leak from the test
            self.lines.append(str(record.msg))


@with_key
class NoLeakTests(OnboardingTestCase):
    def setUp(self):
        super().setUp()
        self.capture = Capture()
        logging.getLogger().addHandler(self.capture)
        self.addCleanup(logging.getLogger().removeHandler, self.capture)
        self.run_ = self.make_run(step="credentials")
        self.make_target(self.run_, "db-01")
        self.login(self.admin_a)

    def _step(self, name):
        return reverse("onboarding:step", args=[self.run_.pk, name])

    def store_via_wizard(self, secret=SECRET, **extra):
        data = {"action": "save", "name": "mumbai-ssh", "kind": "ssh_password", "username": "svc",
                "secret": secret, "apply_to": "all", **extra}
        return self.c.post(self._step("credentials"), data)

    def every_page(self):
        urls = [reverse("onboarding:index"), reverse("onboarding:new"), reverse("onboarding:sd_page"),
                reverse("onboarding:log", args=[self.run_.pk]), reverse("onboarding:export")]
        urls += [self._step(n) for n in ("scope", "targets", "credentials", "verify", "enable", "logs", "confirm", "done")]
        return urls

    def assert_nowhere(self, secret):
        for url in self.every_page():
            body = self.c.get(url).content.decode()
            self.assertNotIn(secret, body, url)
        for line in self.capture.lines:
            self.assertNotIn(secret, line)
        self.assertFalse(any(secret in str(e.details) for e in AuditEvent.objects.all()))
        self.assertFalse(any(secret in e.detail for e in StepLog.objects.all()))
        for cred in Credential.objects.all():
            self.assertNotIn(secret, cred.secret)
            self.assertNotIn(secret, str(cred) + repr(cred.masked))

    def test_a_stored_password_never_appears_anywhere(self):
        resp = self.store_via_wizard()
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(Credential.objects.count(), 1)
        self.assert_nowhere(SECRET)

    def test_the_post_response_and_messages_do_not_echo_it(self):
        resp = self.store_via_wizard(follow=False)
        self.assertNotIn(SECRET, resp.content.decode())
        page = self.c.get(self._step("credentials")).content.decode()
        self.assertNotIn(SECRET, page)
        self.assertIn(secrets.MASK, page)

    def test_a_rejected_form_does_not_write_the_typed_secret_back(self):
        resp = self.c.post(self._step("credentials"), {"action": "save", "name": "", "kind": "ssh_password",
                                                       "secret": SECRET})
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(SECRET, resp.content.decode())
        resp = self.c.post(self._step("credentials"), {"action": "save", "name": "", "kind": "ssh_key",
                                                       "secret_multiline": KEY_PEM})
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("pem-body-zz", resp.content.decode())

    def test_form_initial_and_rendering_never_carry_a_secret(self):
        form = CredentialForm({"name": "x", "kind": "ssh_password", "secret": SECRET, "secret_multiline": KEY_PEM})
        html = form.as_p()
        self.assertNotIn(SECRET, html)
        self.assertNotIn("pem-body-zz", html)
        self.assertNotIn("secret", form.initial)
        self.assertNotIn(SECRET, repr(RotateForm({"secret": SECRET}).as_p()))
        self.assertIsNone(form.fields["secret_multiline"].widget.format_value(KEY_PEM))

    def test_an_ssh_key_alone_in_the_big_box_is_accepted_and_hidden(self):
        resp = self.c.post(self._step("credentials"), {"action": "save", "name": "key", "kind": "ssh_key",
                                                       "username": "root", "secret_multiline": KEY_PEM})
        self.assertEqual(resp.status_code, 302)
        cred = Credential.objects.get(name="key")
        with secrets.use(cred) as plain:
            self.assertEqual(plain, KEY_PEM)
        self.assert_nowhere("pem-body-zz")

    def test_rotation_leaks_nothing_and_keeps_one_row(self):
        self.store_via_wizard()
        cred = Credential.objects.get()
        resp = self.c.post(self._step("credentials"), {"action": "rotate", "credential": cred.pk,
                                                       "secret": "rotated-secret-xyz-987"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(Credential.objects.count(), 1)
        self.assert_nowhere("rotated-secret-xyz-987")
        self.assert_nowhere(SECRET)

    def test_the_vault_error_messages_carry_no_key_material(self):
        with override_settings(LINKEDEYE_CREDENTIAL_KEY="definitely-not-valid-key-material"):
            with self.assertRaises(secrets.VaultUnavailable) as ctx:
                secrets.encrypt(SECRET)
            err = ctx.exception
            self.assertNotIn("definitely-not-valid-key-material", str(err))
            self.assertIsNone(err.__cause__)
            self.assertTrue(err.__suppress_context__)
            resp = self.store_via_wizard()
            body = resp.content.decode() + "".join(str(m) for m in resp.wsgi_request._messages)
            self.assertNotIn("definitely-not-valid-key-material", body)
            self.assertNotIn(SECRET, body)
        self.assertEqual(Credential.objects.count(), 0)

    def test_the_csv_export_has_no_credentials(self):
        self.store_via_wizard()
        body = self.c.get(reverse("onboarding:export")).content.decode()
        self.assertNotIn(SECRET, body)
        self.assertNotIn("gAAAAA", body)

    def test_the_django_admin_never_shows_the_ciphertext(self):
        from django.contrib.auth import get_user_model
        root = get_user_model().objects.create_superuser("root-ob", "r@example.com", "root-pass-123456")
        self.store_via_wizard()
        cred = Credential.objects.get()
        self.c.force_login(root)
        for url in (reverse("admin:onboarding_credential_changelist"),
                    reverse("admin:onboarding_credential_change", args=[cred.pk])):
            body = self.c.get(url).content.decode()
            self.assertNotIn("gAAAAA", body)
            self.assertNotIn(SECRET, body)

    def test_the_django_admin_cannot_create_a_secretless_credential(self):
        from django.contrib.auth import get_user_model
        root = get_user_model().objects.create_superuser("root-add", "a@example.com", "root-pass-123456")
        self.c.force_login(root)
        self.assertEqual(self.c.get(reverse("admin:onboarding_credential_add")).status_code, 403)

    def test_the_hook_return_value_is_scrubbed_before_it_is_stored(self):
        target = self.run_.targets.get()
        leaky = {"hostname": "db-01", "os": "Linux", "password": SECRET, "snmp_community": SECRET,
                 "nested": {"api_key": SECRET, "ok": 1}, "exporter": "node_exporter"}
        with mock.patch.object(steps, "verify_target", return_value=leaky):
            resp = self.c.post(self._step("verify"), {"action": "verify"})
        self.assertEqual(resp.status_code, 302)
        target.refresh_from_db()
        self.assertEqual(target.facts["os"], "Linux")
        self.assertEqual(target.facts["nested"], {"ok": 1})
        self.assertNotIn("password", target.facts)
        self.assert_nowhere(SECRET)

    def test_a_hook_that_raises_is_logged_by_type_only(self):
        with mock.patch.object(steps, "verify_target", side_effect=RuntimeError(f"boom {SECRET}")):
            resp = self.c.post(self._step("verify"), {"action": "verify"})
        self.assertEqual(resp.status_code, 302)
        entry = StepLog.objects.filter(step="verify", status="error").first()
        self.assertIn("RuntimeError", entry.detail)
        self.assert_nowhere(SECRET)

    def test_the_log_shipper_token_is_shown_once_and_stored_redacted(self):
        token = "ingest-token-abc-123-xyz"
        cfg = {"shipper": "promtail", "token": token, "verified": False, "lines": 0,
               "config": f"clients:\n  - url: https://le.example/api/v1/logs/\n    bearer_token: {token}\n"}
        from django.contrib.auth import get_user_model
        self.c.force_login(get_user_model().objects.create_superuser("root-lk", "lk@example.com", "root-pass-123456"))
        with mock.patch.object(steps, "logs_config", return_value=cfg):
            resp = self.c.post(self._step("logs"), {"action": "logs"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn(token, resp.content.decode())
        self.assertEqual(resp["Cache-Control"], "no-store")
        target = self.run_.targets.get()
        self.assertNotIn(token, str(target.facts))
        self.assertIn("<LOG_INGEST_TOKEN>", target.facts["logs"]["config"])
        self.assert_nowhere(token)

    def test_the_sd_token_is_shown_once_and_never_stored_readably(self):
        from django.contrib.auth import get_user_model
        root = get_user_model().objects.create_superuser("root-sd", "r2@example.com", "root-pass-123456")
        self.c.force_login(root)
        resp = self.c.post(reverse("onboarding:sd_token"))
        self.assertEqual(resp.status_code, 200)
        token = resp.context["fresh_token"]
        self.assertIn(token, resp.content.decode())
        self.assertEqual(resp["Cache-Control"], "no-store")
        self.assertNotIn(token, str(dict(self.c.session)))
        row = Credential.objects.get(name="Prometheus service discovery")
        self.assertNotIn(token, row.secret)
        self.assertNotIn(token, self.c.get(reverse("onboarding:sd_page")).content.decode())
        self.assertFalse(any(token in str(e.details) for e in AuditEvent.objects.all()))
        self.assertFalse(any(token in line for line in self.capture.lines))
        self.assert_nowhere(token)


class NoKeyTests(OnboardingTestCase):
    @without_key
    def test_the_wizard_refuses_to_store_without_a_key_and_says_why(self):
        run = self.make_run(step="credentials")
        self.make_target(run, "db-01")
        self.login(self.admin_a)
        url = reverse("onboarding:step", args=[run.pk, "credentials"])
        page = self.c.get(url).content.decode()
        self.assertIn("LINKEDEYE_CREDENTIAL_KEY", page)
        resp = self.c.post(url, {"action": "save", "name": "x", "kind": "ssh_password", "username": "u",
                                 "secret": SECRET}, follow=True)
        self.assertEqual(Credential.objects.count(), 0)
        text = resp.content.decode()
        self.assertIn("LINKEDEYE_CREDENTIAL_KEY", text)
        self.assertNotIn(SECRET, text)
        self.assertTrue(StepLog.objects.filter(run=run, status="error", detail__contains="NOT stored").exists())

    @without_key
    def test_the_sd_token_cannot_be_issued_without_a_key(self):
        from django.contrib.auth import get_user_model
        root = get_user_model().objects.create_superuser("root-nk", "r3@example.com", "root-pass-123456")
        self.c.force_login(root)
        resp = self.c.post(reverse("onboarding:sd_token"), follow=True)
        self.assertEqual(Credential.objects.count(), 0)
        self.assertIn("LINKEDEYE_CREDENTIAL_KEY", resp.content.decode())

    @with_key
    def test_kinds_are_the_ones_the_spec_names(self):
        self.assertEqual({k for k, _ in CredentialKind.choices},
                         {"ssh_password", "ssh_key", "snmp_v2c", "snmp_v3", "basic_auth", "bearer"})
