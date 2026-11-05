"""Personalized welcome e-mail when an administrator creates a user account."""
import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.urls import reverse

log = logging.getLogger("linkedeye.welcome")


def _portal_url(request=None):
    if request is not None:
        try:
            return request.build_absolute_uri(reverse("login"))
        except Exception:
            pass
    return getattr(settings, "LINKEDEYE_PORTAL_URL", "") or ""


def welcome_context(user, *, password="", invited_by="", portal_url="", role="", site_scope=""):
    name = (user.first_name or "").strip() or user.get_username()
    return {
        "greeting_name": name,
        "full_name": user.get_full_name() or name,
        "username": user.get_username(),
        "email": user.email or "",
        "role": role,
        "site_scope": site_scope or "All sites",
        "portal_url": portal_url,
        "invited_by": invited_by,
        "show_credentials": bool(password),
        "password": password,
    }


def render_welcome_email(user, **kwargs):
    ctx = welcome_context(user, **kwargs)
    return (
        render_to_string("notify/welcome_email.txt", ctx),
        render_to_string("notify/welcome_email.html", ctx),
    )


def send_welcome_email(user, *, password="", invited_by="", portal_url="", role="", site_scope="", request=None):
    """Best-effort welcome mail; returns True if sent. Never raises into the admin flow."""
    if not getattr(settings, "LINKEDEYE_WELCOME_EMAIL", True):
        return False
    recipient = (user.email or "").strip()
    if not recipient:
        return False
    portal_url = portal_url or _portal_url(request)
    from administration import integration_config as ic

    recipients = [recipient]
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
        subject = f"Welcome to LinkedEye, {welcome_context(user)['greeting_name']}"
        text_body, html_body = render_welcome_email(
            user, password=password, invited_by=invited_by, portal_url=portal_url,
            role=role, site_scope=site_scope,
        )
        msg = EmailMultiAlternatives(subject, text_body, from_addr, recipients, connection=conn)
        msg.attach_alternative(html_body, "text/html")
        msg.send(fail_silently=False)
        return True
    except Exception as e:
        log.warning("welcome email to %s failed: %s", recipient, e)
        return False
