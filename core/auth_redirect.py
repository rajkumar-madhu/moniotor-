"""Where to send a user after sign-in (respects ``?next=`` when safe)."""
from django.conf import settings
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme

from .access import role_for


def post_login_path(user, next_url=None):
    """Return a path (with query string) for the post-login redirect."""
    next_url = (next_url or "").strip()
    if next_url and url_has_allowed_host_and_scheme(
        next_url, allowed_hosts=settings.ALLOWED_HOSTS, require_https=settings.SECURE_SSL_REDIRECT
    ):
        return next_url
    role = role_for(user)
    if user.is_superuser or role in ("operator", "admin") or user.is_staff:
        return reverse("telemetry:fleet") + "?range=15m"
    return reverse("dashboard")


def post_login_redirect(user, next_url=None):
    return post_login_path(user, next_url)
