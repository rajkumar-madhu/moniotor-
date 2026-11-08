# OTP paths and route gating

LinkedEye uses **email OTP** (and optional **Google Authenticator / TOTP**) on the **legacy Flask/Django backend** under `backend/`. The **Django console** (`linkedeye/` at repo root) supports optional **e-mail OTP after password** when `LINKEDEYE_LOGIN_OTP` is enabled (default: on in production, off when `LINKEDEYE_DEBUG=true` unless `LINKEDEYE_LOGIN_OTP=true`).

### Django console: how the code is kept and limited

The pending code is a row in `core.LoginOtp` (keyed hash only, never the code), not a cache entry, so every worker and pod sees it. A code lives 5 minutes and is burnt after 5 wrong guesses (the user must request a new one); a new code is mailed at most once a minute and 6 times an hour per account, and 20 times an hour per client address (`core.LoginOtpCounter`). Wrong codes are also counted per client address with the same limit as the password step (`LINKEDEYE_LOGIN_MAX_FAILURES_PER_IP` / `LINKEDEYE_LOGIN_WINDOW_MINUTES`). Signing in again inside the one-minute cooldown reuses the code that was just mailed.

### Django console: paths reachable without a session

`core.middleware.PUBLIC_PREFIXES` is the list; every other URL redirects to the sign-in page (JSON `/api/` calls get 401). While a code is pending (`otp_pending_user_id` in the session) the same list stays reachable and every other path redirects to `/accounts/verify-otp/`.

| Prefix | Purpose |
|---|---|
| `/welcome/` | public welcome page |
| `/accounts/login/`, `/accounts/verify-otp/`, `/accounts/resend-otp/` | password step, code entry, code resend |
| `/static/` | static files |
| `/healthz` | liveness and readiness (checks the database) |
| `/api/v1/alertmanager/`, `/api/v1/logs/` | machine endpoints with their own bearer token |
| `/api/sd/` | Prometheus `http_sd` for onboarded targets (bearer token from the credential vault) |
| `/api/apm/` | APM push and command API (per-source bearer token; a browser session never authenticates it) |

Logout (`/accounts/logout/`) is also allowed while a code is pending.

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
