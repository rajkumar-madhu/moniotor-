"""Credential vault: secrets encrypted at rest with Fernet, fail-closed.

Design rules (the onboarding spec calls these non-negotiable):

* the encryption key comes from ``LINKEDEYE_CREDENTIAL_KEY`` (one Fernet key, or several separated
  by commas for key rotation: the first encrypts, every one of them may decrypt);
* if the key or the ``cryptography`` library is missing, storing a secret **fails** with a clear
  message. There is no plaintext fallback and no "store it for now" path;
* a stored secret is write-only: it is never rendered, never returned by a form, never logged,
  never exported and never put into an error message or a traceback. Only :func:`use` hands the
  plaintext out, inside a ``with`` block, and every such read is audited;
* rotation replaces the ciphertext in place. No history of values is kept.

Nothing in this module logs or formats a secret or a ciphertext. Exceptions raised here carry a
fixed message, so a traceback that escapes to a 500 page cannot contain key material.
"""
import logging
import secrets as _secrets
from contextlib import contextmanager

from django.conf import settings

log = logging.getLogger("linkedeye.secrets")

#: how a masked secret is rendered everywhere in the UI
MASK = "••••••••"


class VaultUnavailable(RuntimeError):
    """The vault cannot be used: the library or the key is missing or unusable."""


class VaultError(RuntimeError):
    """A stored value could not be decrypted (wrong or rotated-away key, damaged row)."""


def _fernet_module():
    try:
        from cryptography.fernet import Fernet, InvalidToken, MultiFernet
    except Exception:  # pragma: no cover - exercised by the "library missing" test via monkeypatch
        raise VaultUnavailable(
            "The credential vault needs the 'cryptography' package. Install it "
            "(pip install -r requirements.txt) and restart LinkedEye."
        )
    return Fernet, MultiFernet, InvalidToken


def keys():
    """The configured raw keys, primary first. Never logged."""
    raw = getattr(settings, "LINKEDEYE_CREDENTIAL_KEY", "") or ""
    return [k.strip() for k in raw.split(",") if k.strip()]


def _vault():
    Fernet, MultiFernet, InvalidToken = _fernet_module()
    configured = keys()
    if not configured:
        raise VaultUnavailable(
            "The credential vault is not configured: set LINKEDEYE_CREDENTIAL_KEY to a Fernet key "
            "on the server (see docs/DEPLOYMENT.md) and restart LinkedEye. "
            "Credentials are never stored unencrypted."
        )
    try:
        boxes = [Fernet(k.encode() if isinstance(k, str) else k) for k in configured]
    except Exception:
        # never echo the key or the exception text: a malformed key would appear in it
        raise VaultUnavailable(
            "LINKEDEYE_CREDENTIAL_KEY is not a valid Fernet key (44 url-safe base64 characters). "
            "Generate one with: python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\""
        )
    return (MultiFernet(boxes) if len(boxes) > 1 else boxes[0]), InvalidToken


def available():
    """True when a secret could be stored right now."""
    try:
        _vault()
    except VaultUnavailable:
        return False
    return True


def unavailable_reason():
    """The operator-facing reason the vault cannot be used, or "" when it can."""
    try:
        _vault()
    except VaultUnavailable as exc:
        return str(exc)
    return ""


def new_key():
    """A fresh Fernet key for the operator to put in LINKEDEYE_CREDENTIAL_KEY."""
    Fernet, _multi, _invalid = _fernet_module()
    return Fernet.generate_key().decode()


def encrypt(plaintext):
    """Ciphertext (str) for `plaintext`. Raises VaultUnavailable when the vault is not usable."""
    box, _invalid = _vault()
    if plaintext is None or plaintext == "":
        raise ValueError("Refusing to store an empty secret.")
    data = plaintext.encode() if isinstance(plaintext, str) else bytes(plaintext)
    return box.encrypt(data).decode()


def decrypt(blob):
    """Plaintext for `blob`. Raises VaultUnavailable / VaultError, never echoing the value."""
    box, InvalidToken = _vault()
    try:
        return box.decrypt((blob or "").encode() if isinstance(blob, str) else blob).decode()
    except InvalidToken:
        raise VaultError(
            "This stored secret cannot be decrypted with the current LINKEDEYE_CREDENTIAL_KEY. "
            "Restore the previous key (keys may be listed comma-separated) or re-enter the credential."
        )
    except Exception:
        raise VaultError("This stored secret is damaged and cannot be decrypted. Re-enter the credential.")


def generate_token(nbytes=32):
    """A high-entropy bearer token (url-safe)."""
    return _secrets.token_urlsafe(nbytes)


def mask(credential=None):
    """What the UI shows instead of a secret. Takes a Credential (or nothing) and never reads it."""
    if credential is None:
        return MASK
    kind = getattr(credential, "kind", "") or ""
    user = (getattr(credential, "username", "") or "").strip()
    if kind in ("ssh_key",):
        return f"{MASK} (private key)"
    if user:
        return f"{user} / {MASK}"
    return MASK


# ----------------------------------------------------------------------------- the tiny API
def store(kind, username, secret, site, user, name="", notes="", request=None, credential=None,
          client=None, tenant_key=None):
    """Create (or rotate, when `credential` is given) a vault row and return it.

    Fails closed: without the library or the key nothing is written and VaultUnavailable is raised.
    `secret` is encrypted here and never kept, logged or returned.
    """
    from onboarding.models import Credential

    blob = encrypt(secret)                      # raises before anything is written
    from .audit import audit
    if credential is not None:
        credential.kind = kind or credential.kind
        if username is not None:
            credential.username = username or ""
        credential.secret = blob
        if name:
            credential.name = name
        if notes:
            credential.notes = notes
        credential.rotated_at = _now()
        credential.save(update_fields=["kind", "username", "secret", "name", "notes", "rotated_at"])
        audit(request, "credential.rotate", credential, site=credential.site, kind=credential.kind)
        return credential
    row = Credential.objects.create(
        tenant_key=tenant_key or settings.LINKEDEYE_TENANT,
        name=name or f"{kind}:{username or 'anonymous'}",
        kind=kind,
        username=username or "",
        secret=blob,
        site=site or "",
        client=client,
        notes=notes or "",
        created_by=user if user is not None and getattr(user, "is_authenticated", False) else None,
    )
    audit(request, "credential.store", row, site=row.site, kind=row.kind)
    return row


def _now():
    from django.utils import timezone
    return timezone.now()


@contextmanager
def use(credential, request=None, reason=""):
    """Context manager yielding the plaintext of `credential`; the read is audited.

        with secrets.use(cred, request, reason="ssh facts") as password:
            client.connect(..., password=password)

    The plaintext only exists inside the block and is dropped at the end. It is never logged: this
    module has no log call that takes the value, and the audit entry records only who, what and why.
    """
    from .audit import audit

    plaintext = decrypt(credential.secret)
    audit(request, "credential.use", credential, site=getattr(credential, "site", ""),
          kind=getattr(credential, "kind", ""), reason=(reason or "")[:120])
    type(credential).objects.filter(pk=credential.pk).update(last_used_at=_now())
    try:
        yield plaintext
    finally:
        del plaintext
