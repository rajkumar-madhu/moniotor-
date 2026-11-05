"""
Production middleware for LinkedEye.
- MemoryGuardMiddleware: monitors process memory and logs warnings/critical alerts
- KeycloakOTPGateMiddleware: enforces the post-Finspot-SSO email-OTP step
"""

import logging
import threading
import gc

from django.shortcuts import redirect

logger = logging.getLogger('linkedeye.memory')

try:
    import psutil
    import os
    _process = psutil.Process(os.getpid())
    _psutil_available = True
except ImportError:
    _psutil_available = False


class MemoryGuardMiddleware:
    """Middleware that monitors process memory and alerts on growth."""

    MEMORY_LIMIT_MB = int(os.getenv('LE_MEMORY_LIMIT_MB', '1024')) if _psutil_available else 1024
    WARN_THRESHOLD_MB = int(os.getenv('LE_MEMORY_WARN_MB', '768')) if _psutil_available else 768
    CHECK_INTERVAL = 50  # Check every N requests

    def __init__(self, get_response):
        self.get_response = get_response
        self.request_count = 0

    def __call__(self, request):
        self.request_count += 1
        response = self.get_response(request)

        if _psutil_available and self.request_count % self.CHECK_INTERVAL == 0:
            try:
                rss_mb = _process.memory_info().rss / 1024 / 1024
                thread_count = threading.active_count()

                if rss_mb > self.MEMORY_LIMIT_MB:
                    logger.critical(
                        "MEMORY CRITICAL: RSS=%.0fMB > %dMB limit, threads=%d, requests=%d",
                        rss_mb, self.MEMORY_LIMIT_MB, thread_count, self.request_count
                    )
                    gc.collect()
                elif rss_mb > self.WARN_THRESHOLD_MB:
                    logger.warning(
                        "MEMORY WARNING: RSS=%.0fMB > %dMB warn, threads=%d, requests=%d",
                        rss_mb, self.WARN_THRESHOLD_MB, thread_count, self.request_count
                    )
            except Exception:
                pass

        return response


class KeycloakOTPGateMiddleware:
    """
    mozilla-django-oidc calls auth.login() as part of the Finspot SSO
    callback, before app.views.keycloak_verify gets a chance to run — so by
    the time our own code executes, the Django session is already
    authenticated. keycloak_verify marks the session as otp_pending instead
    of finalizing it, and this middleware blocks every other page until
    /verify-otps/ (or the Google Authenticator equivalent) clears that flag,
    keeping Finspot SSO to the same email-OTP bar as normal login.
    """

    ALLOWED_PATH_PREFIXES = (
        '/verify-otps/',
        '/resend-otps/',
        '/verify-google-authenticator-login/',
        '/static/',
    )
    ALLOWED_EXACT_PATHS = ('/', '/logout/')

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if (
            request.session.get('otp_pending')
            and request.path not in self.ALLOWED_EXACT_PATHS
            and not request.path.startswith(self.ALLOWED_PATH_PREFIXES)
        ):
            return redirect('/')
        return self.get_response(request)
