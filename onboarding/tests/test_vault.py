"""The credential vault: round trip, fail-closed, rotation in place, audited reads."""
from unittest import mock

from django.test import override_settings

from core import secrets
from core.models import AuditEvent
from onboarding.models import Credential, CredentialKind

from .base import KEY, KEY2, SECRET, OnboardingTestCase, with_key, without_key


@with_key
class VaultRoundTripTests(OnboardingTestCase):
    def test_store_encrypts_and_use_returns_the_plaintext(self):
        cred = secrets.store(CredentialKind.SSH_PASSWORD, "svc-le", SECRET, self.site_a.name, self.admin_a,
                             name="mumbai-ssh")
        cred.refresh_from_db()
        self.assertNotIn(SECRET, cred.secret)
        self.assertTrue(cred.secret.startswith("gAAAAA"))
        with secrets.use(cred) as plaintext:
            self.assertEqual(plaintext, SECRET)

    def test_use_records_the_read_and_stamps_last_used(self):
        cred = secrets.store(CredentialKind.SSH_PASSWORD, "svc", SECRET, self.site_a.name, self.admin_a)
        self.assertIsNone(cred.last_used_at)
        with secrets.use(cred, reason="ssh facts"):
            pass
        cred.refresh_from_db()
        self.assertIsNotNone(cred.last_used_at)
        event = AuditEvent.objects.filter(action="credential.use").first()
        self.assertIsNotNone(event)
        self.assertEqual(event.details.get("reason"), "ssh facts")
        self.assertNotIn(SECRET, str(event.details))

    def test_rotation_replaces_in_place_and_keeps_no_history(self):
        cred = secrets.store(CredentialKind.SSH_PASSWORD, "svc", SECRET, self.site_a.name, self.admin_a)
        pk, old_blob = cred.pk, cred.secret
        secrets.store(CredentialKind.SSH_PASSWORD, "svc", "second-secret", self.site_a.name, self.admin_a,
                      credential=cred)
        self.assertEqual(Credential.objects.count(), 1)
        cred.refresh_from_db()
        self.assertEqual(cred.pk, pk)
        self.assertNotEqual(cred.secret, old_blob)
        with secrets.use(cred) as plaintext:
            self.assertEqual(plaintext, "second-secret")
        self.assertIsNotNone(cred.rotated_at)
        self.assertTrue(AuditEvent.objects.filter(action="credential.rotate").exists())

    def test_mask_never_contains_the_secret(self):
        cred = secrets.store(CredentialKind.SSH_PASSWORD, "svc-le", SECRET, self.site_a.name, self.admin_a)
        self.assertNotIn(SECRET, secrets.mask(cred))
        self.assertIn("svc-le", secrets.mask(cred))
        self.assertIn(secrets.MASK, secrets.mask(cred))

    def test_empty_secret_is_refused(self):
        with self.assertRaises(ValueError):
            secrets.store(CredentialKind.BEARER, "", "", self.site_a.name, self.admin_a)
        self.assertEqual(Credential.objects.count(), 0)

    @override_settings(LINKEDEYE_CREDENTIAL_KEY=f"{KEY2},{KEY}")
    def test_several_keys_decrypt_the_old_value(self):
        """A value written with the second key still reads after a new primary key is prepended."""
        with override_settings(LINKEDEYE_CREDENTIAL_KEY=KEY):
            cred = secrets.store(CredentialKind.BEARER, "p", SECRET, self.site_a.name, self.admin_a)
        with secrets.use(cred) as plaintext:
            self.assertEqual(plaintext, SECRET)


class VaultFailClosedTests(OnboardingTestCase):
    @without_key
    def test_without_a_key_storing_fails_and_writes_nothing(self):
        self.assertFalse(secrets.available())
        reason = secrets.unavailable_reason()
        self.assertIn("LINKEDEYE_CREDENTIAL_KEY", reason)
        with self.assertRaises(secrets.VaultUnavailable):
            secrets.store(CredentialKind.SSH_PASSWORD, "svc", SECRET, self.site_a.name, self.admin_a)
        self.assertEqual(Credential.objects.count(), 0)

    @override_settings(LINKEDEYE_CREDENTIAL_KEY="not-a-fernet-key")
    def test_a_malformed_key_fails_closed_without_echoing_it(self):
        self.assertFalse(secrets.available())
        with self.assertRaises(secrets.VaultUnavailable) as ctx:
            secrets.encrypt(SECRET)
        self.assertNotIn("not-a-fernet-key", str(ctx.exception))

    @with_key
    def test_without_the_library_storing_fails(self):
        def boom(name, *args, **kwargs):
            if name.startswith("cryptography"):
                raise ImportError("No module named 'cryptography'")
            return _real_import(name, *args, **kwargs)

        _real_import = __import__
        with mock.patch("builtins.__import__", side_effect=boom):
            with self.assertRaises(secrets.VaultUnavailable) as ctx:
                secrets.encrypt(SECRET)
        self.assertIn("cryptography", str(ctx.exception))
        self.assertEqual(Credential.objects.count(), 0)

    @with_key
    def test_a_value_from_another_key_raises_vault_error_not_plaintext(self):
        cred = secrets.store(CredentialKind.BEARER, "p", SECRET, self.site_a.name, self.admin_a)
        with override_settings(LINKEDEYE_CREDENTIAL_KEY=KEY2):
            with self.assertRaises(secrets.VaultError) as ctx:
                secrets.decrypt(cred.secret)
            self.assertNotIn(SECRET, str(ctx.exception))
