# LinkedEye: one entry point for the commands documented in AGENTS.md, README.md and the CI workflow.
#   make help        list targets
#   make install     virtualenv + dependencies
#   make check       system check, migrations and deploy bundle (CI also runs pip-audit, compose/promtool/amtool, image build)
#   make test        full Django test suite
# Override the interpreter or extras:  make install DB_EXTRAS="-r requirements-postgres.txt"

VENV      ?= .venv
# The Dockerfile and CI both pin Python 3.12; a plain `python3` may be a newer interpreter the project is not tested on.
PYTHON    ?= python3.12
PY        := $(VENV)/bin/python
PIP       := $(VENV)/bin/pip
DB_EXTRAS ?=
# Everything the CI PostgreSQL job installs on top of requirements.txt (the optional Pillow, redis and pyvmomi clients),
# so the whole suite can run locally. The PostgreSQL driver is left out: local runs use SQLite.
CI_EXTRAS := -r requirements-images.txt -r requirements-apm.txt -r requirements-vsphere.txt
# Development-only tools, installed the way the CI deploy-config job installs them: deploy/validate.py (make check,
# make deploy-check) needs PyYAML, which the application itself does not, so no requirements file lists it.
DEV_TOOLS := pyyaml
TEST      ?=
export LINKEDEYE_DEBUG ?= true

.DEFAULT_GOAL := help
.PHONY: help install rebuild-venv migrate superuser run worker worker-once test check migrations-check migrations \
        deploy-check docker-build up down logs clean

help:  ## List the targets
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z_-]+:.*## / {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

$(PY):
	$(PYTHON) -m venv $(VENV)

install: $(PY)  ## Create .venv (Python 3.12; PYTHON=... applies only when .venv is created) and install requirements (DB_EXTRAS adds e.g. -r requirements-postgres.txt)
	$(PIP) install -r requirements.txt $(DB_EXTRAS) $(DEV_TOOLS)

rebuild-venv:  ## Delete .venv and recreate it from scratch with everything the full test suite needs
	@test -n "$(VENV)" && test "$(VENV)" != "/" && test "$(VENV)" != "." || { echo "refusing to remove '$(VENV)'"; exit 1; }
	rm -rf $(VENV)
	$(MAKE) install DB_EXTRAS="$(CI_EXTRAS) $(DB_EXTRAS)"
	$(PY) --version
	$(PY) -c "import cryptography, django; print('django', django.get_version(), '| cryptography', cryptography.__version__)"

migrate: $(PY)  ## Apply database migrations
	$(PY) manage.py migrate

superuser: $(PY)  ## Create a sign-in user interactively (there is no default account)
	$(PY) manage.py createsuperuser

run: $(PY)  ## Start the web app on http://127.0.0.1:8000
	$(PY) manage.py runserver

worker: $(PY)  ## Start the probe and jobs worker (run exactly one per database)
	$(PY) manage.py run_probes

worker-once: $(PY)  ## Run one worker pass and exit
	$(PY) manage.py run_probes --once

test: $(PY)  ## Run the Django tests; narrow with TEST=app.tests.ClassName
	$(PY) manage.py test $(TEST)

check: $(PY) migrations-check deploy-check  ## System check, makemigrations --check, deploy/validate.py and deploy/tests
	$(PY) manage.py check

migrations-check: $(PY)  ## Fail if a model change has no migration
	$(PY) manage.py makemigrations --check --dry-run

migrations: $(PY)  ## Generate migrations after a model change
	$(PY) manage.py makemigrations

deploy-check: $(PY)  ## Validate compose, Kubernetes, Prometheus, Alertmanager and settings docs
	$(PY) deploy/validate.py
	$(PY) -m unittest discover -s deploy/tests

docker-build:  ## Build the production image (PostgreSQL driver)
	docker build -t linkedeye:dev .

up:  ## Start the full stack (needs .env, see deploy/compose.env.example)
	docker compose up -d --build

down:  ## Stop the full stack
	docker compose down

logs:  ## Follow web and worker logs
	docker compose logs -f web probes

clean:  ## Remove bytecode and the collected static directory (keeps .venv, .env and db.sqlite3)
	find . -path ./$(VENV) -prune -o -name __pycache__ -type d -print0 | xargs -0 rm -rf
	rm -rf staticfiles
