"""Email OTP step after password authentication (Django console sign-in)."""
import logging
import secrets
import time

from django.conf import settings
from django.core.cache import cache
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string

log = logging.getLogger("linkedeye.login_otp")

OTP_TTL_SECONDS = 300
CACHE_KEY = "login_otp:{user_id}"


def _otp_enabled():
    return getattr(settings, "LINKEDEYE_LOGIN_OTP", False)


def cache_key(user_id):
    return CACHE_KEY.format(user_id=user_id)


def generate_otp():
    return f"{secrets.randbelow(1_000_000):06d}"


def store_otp(user_id, otp):
    cache.set(cache_key(user_id), {"otp": otp, "at": time.time()}, OTP_TTL_SECONDS)


def clear_otp(user_id):
    cache.delete(cache_key(user_id))


def otp_record(user_id):
    return cache.get(cache_key(user_id))


def otp_expired(record):
    if not record:
        return True
    try:
        age = time.time() - float(record["at"])
    except (TypeError, ValueError):
        return True
    return age > OTP_TTL_SECONDS


def verify_otp_code(user_id, entered):
    record = otp_record(user_id)
    if otp_expired(record):
        return False, "expired"
    if secrets.compare_digest(str(record.get("otp", "")), str(entered).strip()):
        clear_otp(user_id)
        return True, ""
    return False, "invalid"


def send_login_otp_email(user):
    """Generate OTP, store it, e-mail the user. Returns (ok, message)."""
    if not (user.email or "").strip():
        return False, "This account has no e-mail address; contact an administrator."
    otp = generate_otp()
    store_otp(user.pk, otp)
    name = (user.first_name or "").strip() or user.get_username()
    ctx = {"greeting_name": name, "otp": otp, "valid_minutes": OTP_TTL_SECONDS // 60}
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
