from django.conf import settings
from django.shortcuts import redirect
from django.urls import reverse

PUBLIC_PREFIXES = ("/accounts/login/", "/accounts/verify-otp/", "/accounts/resend-otp/", "/static/", "/healthz",
                   # machine-to-machine endpoints; they authenticate with their own bearer token
                   "/api/v1/alertmanager/", "/api/v1/logs/")


class LoginRequiredMiddleware:
    """Every page needs a signed-in user; the login page, static files and the
    health endpoint are public. JSON API calls get 401 instead of a redirect."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.session.get("otp_pending_user_id"):
            path = request.path
            if path.startswith(PUBLIC_PREFIXES) or path == reverse("logout"):
                pass
            else:
                return redirect(reverse("verify_otp"))
        elif not request.user.is_authenticated and not request.path.startswith(PUBLIC_PREFIXES):
            if "/api/" in request.path:
                from django.http import JsonResponse
                return JsonResponse({"error": "authentication required"}, status=401)
            return redirect(f"{reverse(settings.LOGIN_URL)}?next={request.get_full_path()}")
        response = self.get_response(request)
        return response


class ClientScopeMiddleware:
    """Persist ?client= in session and expose request.le_client for templates and queries."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from core.client_scope import sync_client_session

        if request.user.is_authenticated:
            sync_client_session(request)
        else:
            request.le_client = ""
        return self.get_response(request)
