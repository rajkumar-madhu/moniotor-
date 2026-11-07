# OTP paths and route gating

LinkedEye uses **email OTP** (and optional **Google Authenticator / TOTP**) on the **legacy Flask/Django backend** under `backend/`. The **Django console** (`linkedeye/` at repo root) supports optional **e-mail OTP after password** when `LINKEDEYE_LOGIN_OTP` is enabled (default: on in production, off when `LINKEDEYE_DEBUG=true` unless `LINKEDEYE_LOGIN_OTP=true`).

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

## Django console — OTP paths

| Path | Name | Purpose |
|------|------|---------|
| `/accounts/login/` | `login` | Password step (`OTPLoginView`); sends OTP when enabled |
| `/accounts/verify-otp/` | `verify_otp` | Enter 6-digit e-mail code; completes `auth.login()` |
| `/accounts/resend-otp/` | `resend_otp` | POST: send a new code |

Implementation: `core/auth_views.py`, `core/login_otp.py`, templates `registration/verify_otp.html` and `notify/login_otp.*`.

## Django console — paths while `otp_pending_user_id` is in session

Same idea as legacy: **all routes redirect to `/accounts/verify-otp/`** except:

| Prefix / path | Notes |
|---------------|--------|
| `/accounts/login/` | Start over |
| `/accounts/verify-otp/` | OTP form |
| `/accounts/resend-otp/` | Resend (POST) |
| `/accounts/logout/` | Cancel (session flush) |
| `/static/` | Assets |
| `/healthz` | Health probe |
| `/api/v1/alertmanager/` | Webhook (own auth) |
| `/api/v1/logs/` | Log ingest (own auth) |

Configured in `LoginRequiredMiddleware` (`core/middleware.py`).

## Django console — other public paths (no session)

When not in OTP-pending state, unauthenticated users may also only hit the rows above (minus verify/resend unless pending).

**Admin one-time password** (not login OTP): when an admin creates a user with a generated password, the password is shown once via session key `administration_one_time_password` on the user edit page (`administration/views.py`).

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
