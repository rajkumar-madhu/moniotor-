# OTP paths and route gating

LinkedEye uses **email OTP** (and optional **Google Authenticator / TOTP**) on the **legacy Flask/Django backend** under `backend/`. The **Django console** (`linkedeye/` at repo root) uses username/password login only today — no login OTP yet.

## Legacy backend — OTP HTTP paths

| Path | Name | Purpose |
|------|------|---------|
| `/verify-otps/` | `verify_otps` | POST JSON: verify email OTP after password or Finspot SSO |
| `/resend-otps/` | `resend_otps` | POST JSON: resend email OTP |
| `/login/generateOtp` | `generateotp` | Forgot-password: generate OTP e-mail |
| `/login/verifyOTP` | `verifyOTP` | Forgot-password: verify OTP before reset |
| `/setup-google-authenticator/` | `setup_google_authenticator` | TOTP setup (QR / secret) |
| `/verify-google-authenticator-setup/` | `verify_google_authenticator_setup` | Confirm TOTP during setup |
| `/disable-google-authenticator/` | `disable_google_authenticator` | Turn off TOTP |
| `/check-google-authenticator-status/` | `check_google_authenticator_status` | Whether TOTP is enabled |
| `/verify-google-authenticator-login/` | `verify_google_authenticator_login` | Login step with TOTP code |

Related login (password check, often returns **202** until OTP completes):

| Path | Name |
|------|------|
| `/login/verify` | `verify` |
| `/login/keycloak_verify` | `keycloak_verify` |
| `/login/google_verify` | `google_verify` |
| `/` | login page (OTP modal) |
| `/logout/` | logout |

Implementation: `backend/app/views.py`, models `Userotp` / `UserTOTP` in `backend/useronboard/models.py`, e-mail helper `send_otp_email()`.

## Legacy — paths allowed while `otp_pending` (all other paths redirect to `/`)

`KeycloakOTPGateMiddleware` (`backend/LinkedEyeWebProject/middleware.py`) blocks **every** URL until OTP succeeds when the session flag `otp_pending` is set.

**Exact paths allowed**

- `/`
- `/logout/`

**Prefix allowlist**

- `/verify-otps/`
- `/resend-otps/`
- `/verify-google-authenticator-login/`
- `/static/`

Everything else (dashboard, API, admin, etc.) redirects to `/` until OTP is cleared in `verify_otps` or `verify_google_authenticator_login`.

## Django console — auth public paths (no OTP gate)

`LoginRequiredMiddleware` (`core/middleware.py`) treats these as **unauthenticated** entry points:

| Prefix / path | Notes |
|---------------|--------|
| `/accounts/login/` | Login form |
| `/static/` | Static assets |
| `/healthz` | Health probe |
| `/api/v1/alertmanager/` | Webhook (own auth) |
| `/api/v1/logs/` | Log ingest (own auth) |

All other Django routes require a signed-in user (401 for `/api/` JSON, redirect to login otherwise).

**Admin one-time password** (not login OTP): when an admin creates a user with a generated password, the password is shown once via session key `administration_one_time_password` on the user edit page (`administration/views.py`).

## Parity note

To apply the same **“all paths blocked until OTP”** behaviour on the Django console, you would add middleware similar to `KeycloakOTPGateMiddleware`, OTP verify/resend views under e.g. `/accounts/verify-otp/`, and defer `auth.login()` until OTP passes — mirroring the legacy allowlist above plus `/healthz` and webhook prefixes.

## Dump all Django URL paths

From repo root:

```bash
LINKEDEYE_DEBUG=true .venv/bin/python -c "
import os; os.environ.setdefault('DJANGO_SETTINGS_MODULE','linkedeye.settings')
import django; django.setup()
from django.urls import get_resolver
def walk(patterns, prefix=''):
    for p in patterns:
        if hasattr(p, 'url_patterns'):
            walk(p.url_patterns, prefix + str(p.pattern))
        else:
            print(prefix + str(p.pattern), p.name or '')
walk(get_resolver().url_patterns)
"
```

This lists **500+** named routes for the current `linkedeye/urls.py` tree.
