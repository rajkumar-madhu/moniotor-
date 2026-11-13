# Contributing to LinkedEye

LinkedEye is one Django project made of small apps. Read these first:

- [docs/STRUCTURE.md](docs/STRUCTURE.md): repository layout and how the parts fit together
- [docs/MODULES.md](docs/MODULES.md): the module contract (shared APIs, UI kit, data rules, cross-app interfaces)
- [AGENTS.md](AGENTS.md): the short version of the conventions below

## Set up

```bash
make install            # .venv + requirements.txt (add DB_EXTRAS="-r requirements-postgres.txt" for PostgreSQL)
make migrate
make superuser          # there is no default account
make run                # web, http://127.0.0.1:8000
make worker             # probes and jobs; run exactly one per database
```

The Makefile creates `.venv` with Python 3.12, the version the Dockerfile and CI pin. `PYTHON=` only matters when `.venv`
is created; to switch an existing one, run `make rebuild-venv PYTHON=python3.13`. If the suite fails in bulk with
`No module named 'cryptography'`, the virtualenv is missing packages or was built on another interpreter:
`make rebuild-venv` recreates it with everything the full suite needs.

`LINKEDEYE_DEBUG=true` is set by the Makefile for local work. Copy [.env.example](.env.example) for the full settings list.
Run `make help` for every target. The Docker stack is described in [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## Before you open a pull request

```bash
make check              # system check, makemigrations --check, deploy/validate.py and deploy/tests
make test               # full suite; narrow with: make test TEST=network.tests
```

CI runs the system check, `makemigrations --check` and the suite on Django 5.2 and 4.2, the suite again on PostgreSQL, a
dependency audit (`pip-audit`), a deployment job (`deploy/validate.py`, `deploy/tests`, compose, Prometheus and Alertmanager
config) and a Docker image build with a smoke test. A pull request needs all of them green.

## Rules that reviewers hold you to

- **App boundaries.** An app owns its models, migrations, views, templates, static files and tests. Other apps reach it
  only through the interfaces in MODULES.md, imported lazily inside functions.
- **Site scoping and permissions.** Scope querysets with `core.access.scope_by_site`. `@require(...)` only gates a view (the
  role on any site); authorise each change with `can_on(user, perm, record.site)`, and never trust a role held on one site
  for another.
- **Audit and events.** Every mutation calls `core.audit.audit(...)`; every state change calls `core.events.emit(...)`.
- **Network code.** New socket code goes through `probes.checks` (`resolve_allowed` / `check_address`), as MODULES.md requires.
  A few older clients that call an administrator-configured URL connect directly (`core/prometheus.py`, `telemetry/nagios.py`,
  `core/backend_summary.py`, the integration test buttons in `administration/services.py`); do not copy that pattern for
  anything that reaches a user-supplied host. Do not weaken the SSRF policy.
- **Real data only.** Pages show database or integration data, or a "not connected" state naming the setting. Never fabricate numbers.
- **Settings.** Read a new `LINKEDEYE_*` setting in `linkedeye/settings.py` with `env(...)`, list it in `.env.example`
  and document it in `docs/DEPLOYMENT.md`; `deploy/validate.py` fails the build when a setting read there is missing from
  either. It does not see `os.environ` reads in app code, so do not add those. Read integration settings through
  `administration.integration_config`, never the Django setting directly, or a value saved in the console is ignored.
- **Secrets.** Credentials live in the environment or the credential vault, never in the repository. Customer inventories,
  workbooks, host names and addresses do not belong here; tests use synthetic fixtures (example.com, RFC 5737 addresses).
- **Migrations.** A model change ships with its migration (`make migrations`); `make migrations-check` must pass.
- **Tests.** Add tests for behaviour, permission and integration changes, next to the app (`tests.py` or `test_*.py`).

## Style

Python: 4 spaces, `snake_case` functions and modules, `CapWords` classes. Templates live in `<app>/templates/<app>/`;
CSS in `<app>/static/<app>/`, scoped to `.pg-<app>`, using the theme tokens from `core/static/linkedeye/app.css` instead of
hex values. No formatter or linter is enforced; follow the neighbouring code. [.editorconfig](.editorconfig) covers whitespace.

## Commits and pull requests

Commit messages are concise and imperative, prefixed with the affected area, for example
`Sidebar: move Service Desk above Monitoring` or `Dashboard: stop clipping the site map`.

A pull request explains the behaviour change and why, links the issue, and notes any migration or configuration change.
Include screenshots for UI changes. Keep it to one concern. The template in `.github/pull_request_template.md` lists the checks.

## The legacy `backend/` tree

`backend/` is the earlier LinkedEyeWebProject, a separate Django project with its own Dockerfile and tests. The console
does not import or read it, and the console's Docker image excludes it. The console has its own copies of the two assets
it uses from that project, the device front-panel templates (`iframeGraphs/`) and the onboarding templates
(`template/DIRECT/`), in the network app at `network/legacy/`, read by `network/legacy_panels.py`. Do not delete
`backend/iframeGraphs/` or `backend/template/`: the legacy code reads them by relative path (see `network/legacy/README.md`).
New console work goes in the apps, not in `backend/`.

## Security

Do not open a public issue for a vulnerability. See [SECURITY.md](SECURITY.md).
