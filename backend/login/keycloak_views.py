"""
Keycloak OIDC views — callback redirects into LinkedEye post-login flow.
"""

import os

from django.urls import reverse
from mozilla_django_oidc.views import (
    OIDCAuthenticationCallbackView,
    OIDCAuthenticationRequestView,
)


class FinspotStaffOIDCAuthenticationRequestView(OIDCAuthenticationRequestView):
    """Force finspot-management staff broker + fresh login (no stale KC SSO)."""

    def get(self, request):
        self.extra_params = dict(getattr(self, 'extra_params', None) or {})
        hint = os.getenv('KEYCLOAK_STAFF_IDP_HINT', 'finspot-management')
        if hint:
            self.extra_params['kc_idp_hint'] = hint
        self.extra_params['prompt'] = 'login'
        return super().get(request)


class KeycloakCallbackView(OIDCAuthenticationCallbackView):
    """After Keycloak auth, run LinkedEye session bootstrap (mirrors google_verify)."""

    @property
    def success_url(self):
        # Parent login_success() calls auth.login() then redirects here.
        return reverse('keycloak_verify')
