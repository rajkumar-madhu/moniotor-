# Vault Docker — Quick Start

## 1. Start Vault

```powershell
cd backend
docker compose -f docker-compose.vault.yml up -d
```

| Item | Value |
|------|-------|
| UI | http://127.0.0.1:18200 |
| Dev root token | Set via `VAULT_DEV_ROOT_TOKEN_ID` env |
| Container | `linkedeye-vault` |

> Port **18200** is used because **8200** may be taken by other Vault instances (e.g. `argus-vault`).

## 2. Bootstrap (one-time)

Set your real secrets in the shell, then run:

```powershell
$env:VAULT_HOST="127.0.0.1"
$env:VAULT_PORT="18200"
$env:VAULT_TOKEN="<set-your-dev-token>"
$env:VAULT_KV_VERSION="2"

# Your app secrets (examples — use real values)
$env:SECRET_KEY="your-django-secret-key"
$env:MYSQL_DB_PASS="your-mysql-password"
$env:REDIS_PASSWORD="your-redis-password"
$env:NEO4J_PASS="your-neo4j-password"
$env:LINKEDEYE_EMAIL_APPKEY="your-email-app-password"

python manage.py vault_bootstrap
```

This will:
- Enable KV v2 at `secret/`
- Create Django **AppRole** (read-only)
- Seed secrets to `secret/linkedeye/app`
- Save env snippet → `docker/vault-django.env.ps1`

## 3. Run Django with Vault

```powershell
. .\docker\vault-django.env.ps1
# Also keep your non-secret env (MYSQL host, REDIS host, etc.)
python manage.py runserver 0.0.0.0:8000
```

## 4. Useful commands

```powershell
# Vault status
docker ps --filter name=linkedeye-vault
Invoke-RestMethod http://127.0.0.1:18200/v1/sys/seal-status

# Stop / start
docker compose -f docker-compose.vault.yml down
docker compose -f docker-compose.vault.yml up -d

# Re-seed secrets after env change
python manage.py vault_seed_app_secrets
```

## Production note

Dev mode (`-dev`) stores data in memory — **not for production**.

For production use `docker/vault.hcl` with file storage, proper Shamir unseal, and AppRole credentials in K8s secrets (never the root token).
