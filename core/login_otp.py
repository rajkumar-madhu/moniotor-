"""Email OTP step after password authentication (Django console sign-in).

The pending code lives in the database (core.models.LoginOtp), not in Django's cache: the default cache is private to
each process, so with several gunicorn workers or pods the verify request would usually reach a worker that never saw
the code. Only a keyed hash of the code is stored.

Limits (all counted in the database, so they hold across workers):

* a code is valid for OTP_TTL_SECONDS and allowed MAX_ATTEMPTS wrong guesses, after which it is burnt and a new one
  has to be requested;
* a new code can be mailed at most once per RESEND_COOLDOWN_SECONDS and MAX_SENDS_PER_HOUR times per hour per account,
  and MAX_SENDS_PER_IP_PER_HOUR times per hour per client address;
* wrong codes are also counted per client address (settings LOGIN_MAX_FAILURES_PER_IP / LOGIN_WINDOW_MINUTES, the same
  limits as the password step), so one address cannot walk the key space of many accounts.
"""
import hashlib
import hmac
import logging
import secrets
from datetime import timedelta

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import IntegrityError, transaction
from django.template.loader import render_to_string
from django.utils import timezone

log = logging.getLogger("linkedeye.login_otp")

OTP_TTL_SECONDS = 300
MAX_ATTEMPTS = 5
RESEND_COOLDOWN_SECONDS = 60
MAX_SENDS_PER_HOUR = 6
MAX_SENDS_PER_IP_PER_HOUR = 20
HOUR = timedelta(hours=1)


def _otp_enabled():
    return getattr(settings, "LINKEDEYE_LOGIN_OTP", False)


def generate_otp():
    return f"{secrets.randbelow(1_000_000):06d}"


def _hash(user_id, code):
    key = settings.SECRET_KEY.encode()
    return hmac.new(key, f"login-otp:{user_id}:{code}".encode(), hashlib.sha256).hexdigest()


def _model():
    from .models import LoginOtp

    return LoginOtp


def _locked_row(user_id):
    """The user's LoginOtp row, locked for update (inside a transaction), created when missing."""
    LoginOtp = _model()
    for _attempt in range(2):
        row = LoginOtp.objects.select_for_update().filter(user_id=user_id).first()
        if row is not None:
            return row
        try:
            with transaction.atomic():
                LoginOtp.objects.create(user_id=user_id)
        except IntegrityError:
            continue
    return LoginOtp.objects.select_for_update().get(user_id=user_id)


def _locked_counter(kind, value, now, window):
    """The (kind, value) counter, locked, with its window rolled over when it has ended."""
    from .models import LoginOtpCounter

    value = str(value or "-")[:191]
    row = None
    for _attempt in range(2):
        row = LoginOtpCounter.objects.select_for_update().filter(kind=kind, value=value).first()
        if row is not None:
            break
        try:
            with transaction.atomic():
                LoginOtpCounter.objects.create(kind=kind, value=value, window_started_at=now)
        except IntegrityError:
            continue
    if row is None:
        row = LoginOtpCounter.objects.select_for_update().get(kind=kind, value=value)
    if row.window_started_at <= now - window:
        row.count, row.window_started_at = 0, now
    return row


def _address_limits():
    """(wrong codes allowed per address, window) - the password step's per-address limits."""
    try:
        from administration import services

        _user, per_ip, window = services.login_limits()
        return per_ip, window
    except Exception:  # pragma: no cover - administration always present
        return 20, timedelta(minutes=15)


def store_otp(user_id, otp):
    """Make `otp` the user's pending code (resets the wrong-guess counter, not the send counters)."""
    now = timezone.now()
    with transaction.atomic():
        row = _locked_row(user_id)
        row.code_hash = _hash(user_id, otp)
        row.expires_at = now + timedelta(seconds=OTP_TTL_SECONDS)
        row.failures = 0
        row.updated_at = now
        row.save(update_fields=["code_hash", "expires_at", "failures", "updated_at"])


def clear_otp(user_id):
    _model().objects.filter(user_id=user_id).update(code_hash="", expires_at=None, failures=0)


def has_pending_code(user_id, now=None):
    now = now or timezone.now()
    row = _model().objects.filter(user_id=user_id).first()
    return bool(row and row.code_hash and row.expires_at and row.expires_at > now and row.failures < MAX_ATTEMPTS)


def _normalise(entered):
    code = str(entered or "").strip()
    return code if (len(code) == 6 and code.isascii() and code.isdigit()) else ""


def verify_otp_code(user_id, entered, ip=None):
    """(ok, reason). reason: "" | "expired" | "invalid" | "locked" (code burnt by too many guesses) |
    "throttled" (too many wrong codes from this client address)."""
    now = timezone.now()
    per_ip, window = _address_limits()
    with transaction.atomic():
        if ip:
            counter = _locked_counter("verify", ip, now, window)
            if counter.count >= per_ip:
                return False, "throttled"
        row = _locked_row(user_id)
        if not row.code_hash or not row.expires_at or row.expires_at <= now:
            return False, "expired"
        if row.failures >= MAX_ATTEMPTS:
            return False, "locked"
        code = _normalise(entered)
        if code and hmac.compare_digest(row.code_hash, _hash(user_id, code)):
            row.code_hash, row.expires_at, row.failures, row.updated_at = "", None, 0, now
            row.save(update_fields=["code_hash", "expires_at", "failures", "updated_at"])
            return True, ""
        row.failures += 1
        burnt = row.failures >= MAX_ATTEMPTS
        if burnt:
            row.code_hash, row.expires_at = "", None
        row.updated_at = now
        row.save(update_fields=["code_hash", "expires_at", "failures", "updated_at"])
        if ip:
            counter.count += 1
            counter.save(update_fields=["count", "window_started_at"])
        return False, "locked" if burnt else "invalid"


def _reserve_send(user, ip, now):
    """Decide whether a code may be mailed now and, if so, record it. Returns (code, "") or (None, reason) with
    reason "cooldown:<seconds>" or "limit"."""
    with transaction.atomic():
        row = _locked_row(user.pk)
        if row.last_sent_at and (now - row.last_sent_at).total_seconds() < RESEND_COOLDOWN_SECONDS:
            wait = int(RESEND_COOLDOWN_SECONDS - (now - row.last_sent_at).total_seconds()) + 1
            return None, f"cooldown:{wait}"
        if not row.send_window_started_at or row.send_window_started_at <= now - HOUR:
            row.send_count, row.send_window_started_at = 0, now
        if row.send_count >= MAX_SENDS_PER_HOUR:
            return None, "limit"
        if ip:
            counter = _locked_counter("send", ip, now, HOUR)
            if counter.count >= MAX_SENDS_PER_IP_PER_HOUR:
                return None, "limit"
            counter.count += 1
            counter.save(update_fields=["count", "window_started_at"])
        code = generate_otp()
        row.code_hash = _hash(user.pk, code)
        row.expires_at = now + timedelta(seconds=OTP_TTL_SECONDS)
        row.failures = 0
        row.send_count += 1
        row.last_sent_at = now
        row.updated_at = now
        row.save()
        return code, ""


def send_login_otp_email(user, ip=None, reuse_pending=False):
    """Generate a code, store it hashed, e-mail the user. Returns (ok, message).

    Inside the resend cooldown no new code is made. With `reuse_pending` (the password step) a still-valid earlier
    code counts as success, so signing in again right after the first attempt does not mail a second code."""
    if not (user.email or "").strip():
        return False, "This account has no e-mail address; contact an administrator."
    now = timezone.now()
    code, why = _reserve_send(user, ip, now)
    if code is None:
        if why.startswith("cooldown:"):
            if reuse_pending and has_pending_code(user.pk, now):
                return True, "A sign-in code was sent a moment ago. Use it, or request a new one in a minute."
            return False, f"A code was just sent. Wait {why.split(':')[1]} seconds before requesting another."
        return False, "Too many sign-in codes were requested. Try again later."
    name = (user.first_name or "").strip() or user.get_username()
    ctx = {"greeting_name": name, "otp": code, "valid_minutes": OTP_TTL_SECONDS // 60}
    text_body = render_to_string("notify/login_otp.txt", ctx)
    html_body = render_to_string("notify/login_otp.html", ctx)
    from administration import integration_config as ic

    recipients = [user.email.strip()]
    try:
        conn = None
        if ic.has_override("email"):
            from django.core.mail import get_connection

            conn = get_connection(
                host=ic.email_host() or settings.EMAIL_HOST,
                port=ic.email_port(),
                username=ic.email_host_user() or None,
                password=ic.email_host_password() or None,
                use_tls=ic.email_use_tls(),
            )
        from_addr = ic.default_from_email() or settings.DEFAULT_FROM_EMAIL
        subject = "LinkedEye sign-in code"
        msg = EmailMultiAlternatives(subject, text_body, from_addr, recipients, connection=conn)
        msg.attach_alternative(html_body, "text/html")
        msg.send(fail_silently=False)
        return True, ""
    except Exception as e:
        clear_otp(user.pk)
        log.warning("login OTP email to %s failed: %s", user.email, e)
        return False, "Could not send the sign-in code. Check e-mail settings or try again later."


def login_otp_required():
    return _otp_enabled()
