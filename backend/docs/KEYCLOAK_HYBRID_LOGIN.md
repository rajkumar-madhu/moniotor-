# Hybrid Keycloak + MySQL login

The portal supports **both** Keycloak SSO and the existing MySQL username/password
login at the same time, with automatic fallback.

- **Source of truth = app onboarding.** When an admin onboards a user in the
  portal, the user is created in MySQL (`auth_user`) *and* mirrored into Keycloak
  with the **same password**, the mapped realm role, and site/subsite attributes.
- **One password, both systems.** Because the admin sets the password at
  onboarding, it is stored in MySQL and set in Keycloak — so SSO and MySQL
  fallback use the same credential.
- **Automatic fallback.** The login page shows the "Sign in with SSO" button only
  when Keycloak is reachable. If Keycloak is down, only the username/password form
  is shown (with a short notice), so users are never locked out.

```
Admin onboards user ─┬─▶ MySQL auth_user (password, group, sites)        ← fallback login
                     └─▶ Keycloak user (same password, realm role, attrs) ← SSO login
```

## What to create in Keycloak (realm: the SSO realm, e.g. Finspot-Nextalpha)

### 1. Realm roles — names MUST match the Django groups exactly
`Admin`, `ViewOnly`, `Management`, `Onboard`, `UserView`, `TechInfra`, `Risk`,
`TradeSupport`, `Google`, `O365`, `DjangoAdmin`

The app assigns exactly one of these to each user (the onboarding "role").

### 2. Clients
- **`linkedeye-web`** — the login client (interactive users):
  - confidential, **Standard flow (authorization code) ON**
  - Valid Redirect URIs: `<PORTAL_URL>/auth/oidc/callback/`
  - Web Origins: the portal origin
  - Protocol mappers to emit into the token: **realm roles**, and User-Attribute
    mappers for `linkedeye_site_ids`, `linkedeye_sites`, `linkedeye_subsites`,
    plus standard email / given name / family name.
- **`linkedeye-admin`** — the user-mirroring client (machine-to-machine):
  - confidential, **Service Accounts ON**, Standard flow OFF
  - Service-account realm-management roles: **`manage-users`, `view-users`,
    `query-users`** (add `manage-realm` only if you also want role/realm edits)

### 3. User attributes (set by the app; nothing to pre-create)
`linkedeye_site_ids` (multivalued), `linkedeye_subsites` (JSON string),
optionally `linkedeye_sites`.

## Config (env / Vault)

```
# Login SSO (interactive)
KEYCLOAK_ENABLED=true
KEYCLOAK_SERVER_URL=https://keycloak.finspot.in
KEYCLOAK_REALM=Finspot-Nextalpha
KEYCLOAK_CLIENT_ID=linkedeye-web
KEYCLOAK_CLIENT_SECRET=            # Vault
PORTAL_URL=https://<portal-domain>
KEYCLOAK_ALLOWED_DOMAINS=["finspot.in"]

# User mirroring (admin API)
KEYCLOAK_ADMIN_CLIENT_ID=linkedeye-admin
KEYCLOAK_ADMIN_CLIENT_SECRET=      # Vault
KEYCLOAK_ADMIN_SYNC_ENABLED=true   # auto-on when the admin secret is set
KEYCLOAK_DELETE_MODE=delete        # 'delete' mirrors deletes; 'disable' keeps disabled
```

## Behaviour

| App onboarding op | Keycloak effect |
|---|---|
| register | create user (same password) + assign realm role + set site/subsite attrs |
| update   | update name/attrs + reconcile to the single mapped realm role |
| delete   | delete (or disable, per `KEYCLOAK_DELETE_MODE`) |
| changestatus | enable/disable the Keycloak user to mirror `is_active` |

All mirroring is **best-effort and non-fatal**: if Keycloak is unreachable, the
MySQL operation still succeeds and the failure is logged. Re-running the same
onboarding op re-syncs (create is idempotent — it updates in place if the user
already exists).

On SSO login, the existing `sync_keycloak_user` reads the realm role → Django
group and the attributes → sites, so both sides stay consistent round-trip.

## Code touch points
- `lib/LinkedEyeKeycloakAdmin/sync.py` — Admin API client + `sync_*` operations,
  `keycloak_reachable()` health check.
- `useronboard/views.py` `useroperations` — calls the `sync_*` functions.
- `app/views.py` `home` — passes `keycloak_available` for the login fallback.
- `login/keycloak_backend.py` / `keycloak_utils.py` — the SSO login side (roles/
  sites read back from claims).
- `LinkedEyeWebProject/settings.py` — `KEYCLOAK_*` config.
