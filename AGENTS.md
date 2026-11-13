# Repository Guidelines

## Project Structure & Module Organization

Follow `docs/MODULES.md` for app boundaries and shared APIs. Keep cross-app imports lazy and use the documented service interfaces. Scope site data through `core.access`, audit mutations, and route network probes through `probes.checks`.

## Build, Test, and Development Commands

Set `LINKEDEYE_DEBUG=true` locally.

- `.venv/bin/python manage.py migrate` applies schema changes; use `bootstrap_sites` for initial site setup.
- After migrate, create a sign-in user (there is **no** default username/password in the tree):
  - Interactive: `.venv/bin/python manage.py createsuperuser`
  - Non-interactive (scripts/CI): `DJANGO_SUPERUSER_PASSWORD='your-secure-password' LINKEDEYE_DEBUG=true .venv/bin/python manage.py createsuperuser --noinput --username admin --email admin@example.com`
  Grant site access under Administration → Users if pages are empty or permission-denied.

### Docker Compose (full stack)

Full stack: see **`docs/DEPLOYMENT.md`** (copy `deploy/compose.env.example` to `.env` and set at least `LINKEDEYE_SECRET_KEY` and `LINKEDEYE_DB_PASSWORD` first).
- Import Lesites FinSpot **Devices** sheet into network inventory: `manage.py import_lesites_devices_xlsx /path/to/file.xlsx --site-code fs-dr-le --environment dr` (use `--dry-run` first). App containers are read-only; mount the workbook on a one-off run, e.g. `docker-compose run --rm --no-deps -v /path/file.xlsx:/tmp/file.xlsx:ro web python manage.py import_lesites_devices_xlsx /tmp/file.xlsx …`.
- `.venv/bin/python manage.py run_probes` starts the background probe and jobs worker; `--once` runs one pass.
- `LINKEDEYE_DEBUG=true .venv/bin/python manage.py test` runs Django tests.
- `python manage.py check` and `python manage.py makemigrations --check --dry-run` match key CI checks.
- `python deploy/validate.py` and `python -m unittest discover -s deploy/tests` validate deployment assets.

## Coding Style & Naming Conventions

Keep templates under `<app>/templates/<app>/` and CSS under `<app>/static/<app>/`, scoped to `.pg-<app>`. No formatter or linter is configured; follow neighboring code.

## Commit & Pull Request Guidelines

Recent commits use concise, imperative summaries, often prefixed by the affected area (for example, `Sidebar: move Service Desk...` or `Dashboard: stop clipping...`). Keep commits similarly specific. A pull request should explain the behavior change and rationale, link related issues when available, note migrations or configuration changes, and include screenshots for UI changes. Ensure CI checks pass.

## Security & Configuration

Keep credentials in environment variables; use `.env.example` as the settings reference and never commit secrets. Preserve site permission checks, audit logging, and the SSRF protections described in `README.md` when changing access or network behavior.
