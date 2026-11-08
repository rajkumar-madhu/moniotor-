# LinkedEye Secrets Audit

_Last updated: 2026-11-08_

> **Rotate before anything else.** Earlier revisions of this file and of
> `lib/LinkedEyeValidation/Read.me` printed live-looking credentials (an Office 365 SMTP
> app password, an iLO login and a SNMP v2c community string). They are redacted below
> (`<rotate-and-remove>`) but **remain readable in git history** (commit `751df6e` and
> later). Treat all of them as compromised: rotate/revoke them at the source, and purge
> history (`git filter-repo`) if this repository is or will be shared.

This document lists hardcoded or embedded secrets found in the codebase, what was
remediated, and how to run production-grade Vault for application secrets.

---

## Two Vault use cases in LinkedEye

| Use case | Path | Purpose |
|----------|------|---------|
| **Device credentials** | `secret/{service}/{ip}/{username}` | SNMP/SSH automation passwords (existing Admin Vault UI) |
| **Application secrets** | `secret/linkedeye/app` | Django DB, Redis, email, OAuth, TOTP, etc. (new) |

---

## Hardcoded secrets found (remediated in code)

| Location | Secret type | Remediation |
|----------|-------------|-------------|
| `LinkedEyeWebProject/settings.py` | `SECRET_KEY`, DB passwords, Redis, Neo4j, email app key, Google OAuth | `get_app_secret()` from Vault with env fallback |
| `app/views.py` | `DEFAULT_MASTER_KEY`, `ADMIN_DEFAULT_PASSWORD` | Removed literals; use `TOTP_MASTER_KEY` / settings |
| `notification/views.py` | SMTP password `<rotate-and-remove>` | `settings.LINKEDEYE_EMAIL_APPKEY` |
| `notification/tasks.py` | SMTP password default | `settings.LINKEDEYE_EMAIL_APPKEY` |
| `allonboard/services.py` | SMTP password | `settings.LINKEDEYE_EMAIL_APPKEY` |
| `lib/LinkedEyeEntity/Node.py` | Neo4j password default | `get_app_secret('NEO4J_PASS')` |
| `lib/LinkedEyeNotification/Notification.py` | MySQL password default | `get_app_secret('MYSQL_DB_PASS')` |
| `analytics/views.py` | Elasticsearch `changeme` default | `settings.ELASTIC_PASS` |
| `entrypoint.py` | MySQL `rootpassword` default | Still uses env; seed via Vault bundle |

---

## Files that still contain secrets (action required)

State of the tree on 2026-11-08 (re-check with a scanner, see below):

| File | State | Action |
|------|-------|--------|
| `login/aad.config.json` | `client_credential` is empty in the tree | Keep it empty; inject the Azure AD secret from Vault key `AZURE_CLIENT_SECRET` or a K8s secret. Rotate the old value if it was ever committed. |
| `LinkedEyeWebProject.pyproj` | No credentials found | Local IDE project file only |
| `lib/LinkedEyeValidation/Read.me` | Credentials replaced by placeholders | Rotate the iLO account and the SNMP community that were printed there |
| `SECRETS_AUDIT.md` (this file) | SMTP password redacted | Rotate the SMTP app password |

Add a secret scanner (gitleaks or detect-secrets) to CI so these values cannot return.

------|------|--------|
| `login/aad.config.json` | Azure AD `client_credential` in JSON | Move to Vault key `AZURE_CLIENT_SECRET` or K8s secret; remove from git |
| `LinkedEyeWebProject.pyproj` | Dev env vars with real passwords | Local IDE only — never deploy; add to `.gitignore` if possible |
| `DEV_SETUP_MEMORY.md` | Historical dev credentials | Documentation only — rotate if exposed |

---

## Production Vault setup (application secrets)

### 1. Bootstrap Vault (container entrypoint or ops runbook)

Existing `entrypoint.py` initializes Vault for StackStorm device secrets.
Ensure Vault is unsealed and KV engine `secret/` is enabled.

### 2. Create Django AppRole (one-time)

```bash
export VAULT_TOKEN=<admin-token>
python manage.py vault_setup_django_approle
```

Store output as Kubernetes secrets:

- `VAULT_APP_ROLE_ID`
- `VAULT_APP_SECRET_ID`

### 3. Seed application secrets from env (one-time migration)

```bash
export SECRET_KEY='...'
export MYSQL_DB_PASS='...'
export REDIS_PASSWORD='...'
# ... all keys in APP_SECRET_KEYS
export VAULT_TOKEN=<admin-token>
python manage.py vault_seed_app_secrets
```

### 4. Enable Vault in Django pods

```bash
VAULT_APP_SECRETS_ENABLED=true
VAULT_HOST=vault.fs-linkedeye
VAULT_PORT=8200
VAULT_APP_SECRETS_PATH=secret/linkedeye/app
VAULT_APP_ROLE_ID=<from step 2>
VAULT_APP_SECRET_ID=<from step 2>
```

Remove plaintext secret env vars from the deployment manifest after seeding.

### 5. Local development

Keep `VAULT_APP_SECRETS_ENABLED=false` and use shell env vars (as today).
No Vault required for local dev.

---

## Security hardening included

- Vault API endpoints (`vaultOperation`, `getallsecrets`, etc.) require Admin login
- Django uses **AppRole** (read-only policy) instead of root token at runtime
- Secret bundle cached in memory with TTL (`VAULT_SECRETS_CACHE_TTL`, default 300s)
- Fixed `Vault.getSecret()` (was using wrong HTTP method)
- Fixed `Vault._mapRole()` (policies were never attached)

---

## Remaining production recommendations

1. Set `DEBUG=False` and `SECRET_KEY` only via Vault/env
2. Rotate credentials that were ever committed to git
3. Replace `login/aad.config.json` credentials with Vault-backed values
4. Use Vault KV v2 + audit logging for compliance (future upgrade)
5. Never mount `/vault/keys.json` root token into Django pods
