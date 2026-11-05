"""
OIDC authentication backend for Keycloak (mozilla-django-oidc).

Used only for the OIDC authorization-code callback. Username/password login
continues to use django.contrib.auth.backends.ModelBackend via /login/verify.
"""

from __future__ import annotations

import logging

from mozilla_django_oidc.auth import OIDCAuthenticationBackend

from login.keycloak_utils import (
    email_domain_allowed,
    find_user_for_keycloak_claims,
    send_keycloak_welcome_message,
    sync_keycloak_user,
)

logger = logging.getLogger(__name__)


class LinkedEyeKeycloakBackend(OIDCAuthenticationBackend):
    """Authenticate via Keycloak and sync LinkedEye User + Group records."""

    def authenticate(self, request, **kwargs):
        # mozilla_django_oidc's authenticate() calls get_token() and
        # verify_token() with no error handling at all -- any failure there
        # (token exchange rejected, signature/nonce/claims check failed) was
        # propagating straight through as an unhandled 500 instead of a normal
        # "login failed" outcome. Catch it here, log the real exception (this
        # is the first place any of this has actually been logged), and fail
        # the same way an ordinary rejected login already does.
        try:
            return super().authenticate(request, **kwargs)
        except Exception:
            logger.exception('Keycloak OIDC authenticate() failed')
            if request is not None:
                request.session['keycloak_login_error'] = (
                    'Finspot SSO sign-in failed. Please try again, or sign in '
                    'with your username and password.'
                )
            return None

    def verify_claims(self, claims):
        if not super().verify_claims(claims):
            return False
        email = claims.get('email') or claims.get('preferred_username') or ''
        if email and not email_domain_allowed(email):
            logger.warning('Keycloak login rejected for disallowed domain: %s', email)
            return False
        return True

    def get_username(self, claims):
        return claims.get('email') or claims.get('preferred_username')

    def create_user(self, claims):
        user = super().create_user(claims)
        user = sync_keycloak_user(user, claims)
        send_keycloak_welcome_message(user)
        return user

    def update_user(self, user, claims):
        user = super().update_user(user, claims)
        return sync_keycloak_user(user, claims)

    def filter_users_by_claims(self, claims):
        user = find_user_for_keycloak_claims(claims)
        if user:
            return self.UserModel.objects.filter(pk=user.pk)
        username = self.get_username(claims)
        if not username:
            return self.UserModel.objects.none()
        return self.UserModel.objects.filter(username=username)
