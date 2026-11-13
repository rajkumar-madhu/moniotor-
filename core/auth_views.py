"""Sign-in views with optional e-mail OTP after password verification."""
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login as auth_login
from django.contrib.auth.views import LoginView
from django.http import HttpResponseRedirect
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods

from administration.forms import ThrottledAuthenticationForm

from .audit import _ip

from .auth_redirect import post_login_path, post_login_redirect
from .login_otp import login_otp_required, send_login_otp_email, verify_otp_code

User = get_user_model()
SESSION_USER = "otp_pending_user_id"
SESSION_NEXT = "otp_pending_next"
OTP_ERRORS = {
    "invalid": "That code is incorrect.",
    "expired": "That code has expired. Request a new one.",
    "locked": "Too many incorrect codes. Request a new code.",
    "throttled": "Too many incorrect codes from this address. Try again later.",
}


class OTPLoginView(LoginView):
    authentication_form = ThrottledAuthenticationForm
    template_name = "registration/login.html"

    def get_success_url(self):
        field = self.redirect_field_name
        explicit = self.request.GET.get(field) or self.request.POST.get(field) or ""
        return post_login_redirect(self.request.user, explicit)

    def form_valid(self, form):
        if not login_otp_required():
            return super().form_valid(form)
        user = form.get_user()
        ok, err = send_login_otp_email(user, ip=_ip(self.request), reuse_pending=True)
        if not ok:
            form.add_error(None, err)
            return self.form_invalid(form)
        if err:
            messages.info(self.request, err)
        self.request.session[SESSION_USER] = user.pk
        nxt = self.request.GET.get("next") or self.request.POST.get("next") or ""
        self.request.session[SESSION_NEXT] = nxt or post_login_path(user)
        return HttpResponseRedirect(reverse("verify_otp"))


def _pending_user(request):
    uid = request.session.get(SESSION_USER)
    if not uid:
        return None
    return User.objects.filter(pk=uid, is_active=True).first()


def _mask_email(email):
    email = (email or "").strip()
    if "@" not in email:
        return email
    local, domain = email.split("@", 1)
    if len(local) <= 2:
        masked = local[0] + "*"
    else:
        masked = local[0] + "*" * (len(local) - 2) + local[-1]
    return f"{masked}@{domain}"


@never_cache
@csrf_protect
@require_http_methods(["GET", "POST"])
def verify_otp(request):
    if not login_otp_required():
        return redirect(settings.LOGIN_URL)
    user = _pending_user(request)
    if user is None:
        request.session.pop(SESSION_USER, None)
        request.session.pop(SESSION_NEXT, None)
        return redirect(settings.LOGIN_URL)
    error = ""
    if request.method == "POST":
        code = (request.POST.get("otp") or "").strip()
        if not code:
            error = "Enter the code from your e-mail."
        else:
            ok, reason = verify_otp_code(user.pk, code, ip=_ip(request))
            if ok:
                auth_login(request, user, backend="django.contrib.auth.backends.ModelBackend")
                dest = post_login_redirect(user, request.session.pop(SESSION_NEXT, None))
                request.session.pop(SESSION_USER, None)
                return redirect(dest)
            error = OTP_ERRORS.get(reason, OTP_ERRORS["expired"])
    return render(
        request,
        "registration/verify_otp.html",
        {"email_hint": _mask_email(user.email), "error": error, "next": request.session.get(SESSION_NEXT, "")},
    )


@never_cache
@csrf_protect
@require_http_methods(["POST"])
def resend_otp(request):
    if not login_otp_required():
        return redirect(settings.LOGIN_URL)
    user = _pending_user(request)
    if user is None:
        return redirect(settings.LOGIN_URL)
    ok, err = send_login_otp_email(user, ip=_ip(request))
    if ok:
        messages.success(request, f"A new code was sent to {_mask_email(user.email)}.")
    else:
        messages.error(request, err)
    return redirect("verify_otp")
