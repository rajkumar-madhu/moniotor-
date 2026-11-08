# Legacy backend: security changes

Scope: `backend/` (the legacy LinkedEyeWebProject). Ids refer to the product review
(`deploy-docs-N`). Regression tests: `legacy_tests/` (see "Running the tests").

> **Credentials printed earlier remain in git history.** The SMTP app password, the iLO
> login and the SNMP v2c community that were printed in `SECRETS_AUDIT.md` and
> `lib/LinkedEyeValidation/Read.me` are redacted in the tree (`<rotate-and-remove>`), but
> the old commits still contain them. Rotate/revoke them at the source and purge history
> (`git filter-repo`) before the repository is shared.

## deploy-docs-1: login view no longer creates admin accounts
* `POST /login/verify` (`app/views.py`) and `login/views.py` no longer create `admin` or
  `djangoadmin` and no longer call `auth.login()` for an unknown username. Unknown names
  get the normal "username does not exist" answer.
* An empty password never authenticates (older releases could create `admin` with an
  empty password when `ADMIN_DEFAULT_PASSWORD` was unset). **Check existing deployments:**
  if the `admin` account was auto-created, set a real password now.
* First administrators are created by an operator:
  `ADMIN_DEFAULT_PASSWORD='...' python manage.py bootstrap_admin [--djangoadmin]`
  (creates the roles, never touches existing users, refuses to run without a password).

## deploy-docs-2: notification endpoints require a session and a role
* Every view in `notification/views.py` is `@login_required`. Views that change alerting
  (services, escalation policies, device alert policies, pause/snooze of alert e-mail) also
  require a role in `NOTIFICATION_ADMIN_ROLES` (default `["Admin"]`).
* `@csrf_exempt` removed from `toggle_email_notification` and `snooze_email_notification`;
  the page script must send the CSRF token like the other endpoints (header `X-CSRFToken`).
* `sendnotifications` lets non-Admin users notify only themselves.
* The frontend templates are not in this repository: confirm the toggle/snooze calls send
  the CSRF token. If operators other than Admin must pause or snooze alerts, extend
  `NOTIFICATION_ADMIN_ROLES`.

## deploy-docs-3: secrets removed from the tree
* `SECRETS_AUDIT.md` and `lib/LinkedEyeValidation/Read.me`: values replaced by
  `<rotate-and-remove>`; the stale "still contain secrets" table now matches the tree.
* **Owner action:** rotate the Office 365 SMTP app password, the iLO account and the SNMP
  community, and purge history. Add gitleaks or detect-secrets to CI.
* `legacy_tests/leaked.sha256` holds only hashes of the old values; a test fails if one
  reappears in those files.

## deploy-docs-5: SECRET_KEY and TLS verification
* `settings.py` raises `ImproperlyConfigured` when `DJANGO_DEBUG` is not true and
  `SECRET_KEY` is missing or the published placeholder. Provide `SECRET_KEY` (Vault bundle
  or environment) before deploying this version.
* `MONITORING_VERIFY_SSL` now defaults to `true` (settings and `lib/LinkedEyeMonitoring/token.py`).
  Private CA: set `MONITORING_CA_BUNDLE=/path/ca.pem`. Opt out explicitly with
  `MONITORING_VERIFY_SSL=false` (not recommended). `.env.example` updated.

## deploy-docs-8: Vault seal/unseal is POST-only
* `vault.views.changeStatus` accepts POST only (CSRF protected) and reads `status` from
  the form body. The Vault page script must POST `status` with the CSRF token (the
  template is not in this repository). The existing meaning of `status` (current state;
  the opposite action is performed) is unchanged.

## deploy-docs-9: entrypoint
* The Vault root token is no longer written to `/stackstorm/configs/linkedeye_vault.yaml`.
  A policy-scoped periodic token (`Vault.CreateScopedToken`, policy `linkedeye_secret_acl`,
  period `LINKEDEYE_STACKSTORM_TOKEN_PERIOD`, default `768h`, must be renewed) is written
  instead; if it cannot be created the entrypoint fails and writes nothing. The old
  behaviour is available only with `LINKEDEYE_ENTRYPOINT_PERSIST_ROOT_TOKEN=true`.
* `createCountryStateTable()` no longer drops tables: `CREATE TABLE IF NOT EXISTS`, load
  only when empty, in one transaction with rollback (`LINKEDEYE_ENTRYPOINT_RELOAD_GEO=true`
  forces a reload). Note that the current start-up flow never called it.
* Module-level start-up code moved into `main()` (`python /entrypoint.py` unchanged).
* `makemigrations` still runs by default because most apps have no committed migrations;
  set `LINKEDEYE_ENTRYPOINT_MAKEMIGRATIONS=false` once migrations are committed to the image.

## deploy-docs-10: requirement files
* `requirements_win.txt` and `requirement.txt` now only contain `-r requirements.txt`, so
  nobody installs Django 3.0.2 or the old pins. They can be deleted. `ms_identity_web`
  in `requirements.txt` is still unpinned (no package index access to choose a version).

## deploy-docs-14: dev Keycloak
* `docker-compose.keycloak.yml`: the admin password is required from
  `KEYCLOAK_ADMIN_PASSWORD`, and the port is bound to 127.0.0.1.
* `scripts/keycloak-setup.ps1` requires `KEYCLOAK_ADMIN_PASSWORD` and `KEYCLOAK_CLIENT_SECRET`
  instead of falling back to well-known values.
* Not changed: the `mobikwik-` default of `KEYCLOAK_TENANT_ROLE_PREFIX`
  (`login/keycloak_utils.py`); existing deployments rely on it for role mapping. Set the
  variable explicitly per tenant.

## Running the tests
The legacy app needs MySQL, Redis, Vault and Neo4j, so these tests stub every missing
third-party module and use in-memory SQLite without migrations. From `backend/`:

    python legacy_tests/run.py            # needs only Django (4.2 or 5.2) installed
