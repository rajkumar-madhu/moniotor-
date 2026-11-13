# Security policy

## Reporting a vulnerability

Do not open a public issue or pull request for a security problem.

Use GitHub's private reporting: open the repository's **Security** tab and choose **Report a vulnerability**
(<https://github.com/rajkumar-madhu/moniotor-/security/advisories/new>). Include the affected URL or module, the steps to
reproduce, and the impact you observed. If you found a credential, say where, but do not paste its value.

The button is there only when the repository owner has turned on private vulnerability reporting in the repository's
code security settings, which GitHub offers for public repositories. If it is missing, contact a maintainer directly and privately,
never through an issue or pull request. Maintainers: turn the setting on, or list a private security contact here.

## Supported versions

Fixes are made on `main`. Older tags and the legacy `backend/` tree do not receive fixes.

## What the console already enforces

These are design guarantees. A change that weakens one is a security bug, and a report against it is in scope.

- **Authentication.** Sign-in is required for every page except the login and one-time-code pages, the public landing page
  (`/welcome/`), static files, `/healthz` and the machine endpoints that check their own bearer token (`PUBLIC_PREFIXES` in
  `core/middleware.py`). After the password, a one-time code is e-mailed (`core/login_otp.py`); this is on by default
  whenever `LINKEDEYE_DEBUG` is off, and `LINKEDEYE_LOGIN_OTP` turns it on or off.
- **Authorisation.** Roles (viewer, operator, admin) are held per site or for all sites; a role on one site never counts for
  another (`core/access.py`). Records on "All Sites" need the role tenant-wide to change.
- **Audit.** Sign-ins, sign-outs and every mutation are recorded (`core/audit.py`).
- **SSRF.** Probes resolve a host, check the address, then connect to that pinned address. Link-local and cloud-metadata
  ranges are always refused; private ranges only when listed in `LINKEDEYE_PROBE_ALLOWED_PRIVATE_CIDRS`
  (`probes/checks.py`, described in the README). The same policy guards webhooks and device, BMC and vCenter connections.
  Clients that call an administrator-configured integration URL (Prometheus, Nagios, the legacy summary bridge and the
  integration test buttons) connect directly and are not covered by it. Those URLs can be set only by an admin on All
  sites (Administration › Integrations, or the onboarding Prometheus step) or through the deployment's environment.
- **Secrets.** Credentials for monitored systems (SNMP, SSH, BMC, vCenter, Redis) and the Prometheus password are stored
  encrypted (Fernet) in the credential vault (`core.secrets`); the encryption key comes from `LINKEDEYE_CREDENTIAL_KEY`.
  Without a key, storing such a secret fails; there is no plaintext fallback.
- **Not encrypted at rest: integration secrets typed into Administration › Integrations.** The Alertmanager and log-ingest
  tokens, the StackStorm API key, the Nagios and SMTP passwords and the summary-bridge token are kept in the database as
  plain values (`administration.IntegrationSetting`). They are never shown again in the console, but anyone with database
  or backup access can read them. To keep them out of the database, set the matching `LINKEDEYE_*` environment variables
  and never type them into the console (the environment is the fallback when no console value is saved). Once saved, a
  console secret stays: an empty field keeps the earlier value, and the console has no button that removes it.
- **Read-only integrations.** The hardware (Redfish) and hypervisor (vCenter) clients make no write call to the managed
  system, and tests fail if one appears.
- **Container.** The image runs as a non-root user, and CI asserts that the legacy tree, secrets notes and docs are not in it.

## Secrets that were printed in git history

The legacy tree's [backend/SECRETS_AUDIT.md](backend/SECRETS_AUDIT.md) records credentials (an SMTP app password, an iLO
login and an SNMP community string) that were printed in earlier revisions. They are redacted in the working tree but remain
readable in git history, and that history is already on GitHub (commit `751df6e` is on `main` and other branches).
Treat them as compromised and revoke them at the source: that is the remedy that works. Rewriting history
(`git filter-repo` and a force-push of every branch) cannot recall copies already in clones and forks, and GitHub Support has
to be asked to drop cached views and pull-request references.

## Handling secrets in a deployment

Keep `LINKEDEYE_SECRET_KEY`, database and integration credentials in the environment or a secret store. Never commit a
`.env` file that holds a secret; `.gitignore` excludes `.env` and `deploy/finspot-platform.env`. The env files that are
tracked are templates (`.env.example`, `deploy/compose.env.example`, `deploy/finspot-platform.env.example`, and
`backend/.env.example` / `backend/config/local.env.example` for the legacy project) plus `deploy/compose.console.env`,
which sets only a published port and the trusted CSRF origins. A new tracked env file must hold no secret.
See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).
