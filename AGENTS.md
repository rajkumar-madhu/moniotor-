# Repository Guidelines

## Project Structure & Module Organization

LinkedEye is a Django project. `linkedeye/` contains settings and root URL wiring; `core/` provides shared access, audit, events, jobs, and UI; `probes/` owns network checks and the worker. Product areas live in apps such as `servicedesk/`, `cmdb/`, `telemetry/`, and `reports/`. Apps own their models, migrations, views, templates, assets, and tests. Deployment assets are in `deploy/`; maps and contracts are in `docs/`.

Follow `docs/MODULES.md` for app boundaries and shared APIs. Keep cross-app imports lazy and use the documented service interfaces. Scope site data through `core.access`, audit mutations, and route network probes through `probes.checks`.

## Build, Test, and Development Commands

Set up a virtual environment with `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`. Add the database-specific requirements file as needed. Set `LINKEDEYE_DEBUG=true` locally.

- `.venv/bin/python manage.py migrate` applies schema changes; use `bootstrap_sites` for initial site setup.
- After migrate, create a sign-in user (there is **no** default username/password in the tree):
  - Interactive: `.venv/bin/python manage.py createsuperuser`
  - Non-interactive (scripts/CI): `DJANGO_SUPERUSER_PASSWORD='your-secure-password' LINKEDEYE_DEBUG=true .venv/bin/python manage.py createsuperuser --noinput --username admin --email admin@example.com`
  Grant site access under Administration → Users if pages are empty or permission-denied.
- `.venv/bin/python manage.py runserver` starts the web app.

### Docker Compose (full stack)

The repo root already has **`docker-compose.yml`** (PostgreSQL, migrate, web, probes). Copy and fill **`deploy/compose.env.example`** → `.env` (at least `LINKEDEYE_SECRET_KEY` and `LINKEDEYE_DB_PASSWORD`), then:

```bash
docker compose build && docker compose up -d
docker compose exec web python manage.py createsuperuser
```

Or a non-interactive superuser in the `web` container:

```bash
docker compose exec -e DJANGO_SUPERUSER_PASSWORD='your-secure-password' web \
  python manage.py createsuperuser --noinput --username admin --email admin@example.com
```

Details: **`docs/DEPLOYMENT.md`**.
- `.venv/bin/python manage.py run_probes` starts the background probe and jobs worker; `--once` runs one pass.
- `LINKEDEYE_DEBUG=true .venv/bin/python manage.py test` runs Django tests.
- `python manage.py check` and `python manage.py makemigrations --check --dry-run` match key CI checks.
- `python deploy/validate.py` and `python -m unittest discover -s deploy/tests` validate deployment assets.

## Coding Style & Naming Conventions

Use 4-space indentation and Django naming: `snake_case` for functions, variables, and modules; `CapWords` for classes. Keep templates under `<app>/templates/<app>/` and CSS under `<app>/static/<app>/`, scoped to `.pg-<app>`. No formatter or linter is configured; follow neighboring code.

## Testing Guidelines

Tests use Django's built-in runner in app `tests.py` or `test_*.py`; deployment regression tests use `unittest`. Add tests for behavior, permission, or integration changes. Run relevant tests and the full suite before submitting; check migrations when models change.

## Commit & Pull Request Guidelines

Recent commits use concise, imperative summaries, often prefixed by the affected area (for example, `Sidebar: move Service Desk...` or `Dashboard: stop clipping...`). Keep commits similarly specific. A pull request should explain the behavior change and rationale, link related issues when available, note migrations or configuration changes, and include screenshots for UI changes. Ensure CI checks pass.

## Security & Configuration

Keep credentials in environment variables; use `.env.example` as the settings reference and never commit secrets. Preserve site permission checks, audit logging, and the SSRF protections described in `README.md` when changing access or network behavior.
