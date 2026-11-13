# Project structure

LinkedEye is one Django 5.2 LTS project (`linkedeye/`) made of small apps. Each app owns one area of the sidebar.
Shared code lives in `core/` and `probes/`. Apps talk to each other only through the interfaces in
[MODULES.md](MODULES.md).

```
moniotor-/
├── manage.py
├── Makefile                    one entry point: make help · install · run · worker · test · check · up
├── requirements.txt            Django 5.2 LTS, gunicorn, whitenoise, cryptography (credential vault), openpyxl, pypdf
├── requirements-*.txt          optional extras: postgres, mysql, ssh, images, apm (redis), vsphere (pyvmomi)
├── Dockerfile, docker-compose.yml, gunicorn.conf.py   production image and stack (see docs/DEPLOYMENT.md)
├── .env.example                every LINKEDEYE_* setting (commented out where optional); deploy/validate.py enforces it
├── .editorconfig, .dockerignore, .gitignore
├── README.md                   what the product is and how to run it
├── CONTRIBUTING.md             set-up, pre-PR checks, review rules, commit and PR conventions
├── SECURITY.md                 how to report a vulnerability; the guarantees the console enforces
├── AGENTS.md                   the same conventions, condensed for coding agents
│
├── .github/
│   ├── workflows/ci.yml        check · makemigrations --check · tests (Django 5.2 + 4.2, PostgreSQL) · pip-audit ·
│   │                           deploy bundle · Docker image build and smoke test
│   ├── dependabot.yml          weekly pip, docker, docker-compose and GitHub Actions updates
│   ├── CODEOWNERS              review ownership, with the security-critical paths listed separately
│   └── pull_request_template.md
│
├── deploy/                     docker/ (WSGI entry, healthcheck), kubernetes/ (base manifests), overlays/finspot-dev/
│                               (Kustomize overlay, see docs/FINSPOT_DEV_K8S.md), prometheus/, alertmanager/,
│                               compose.env.example, compose.console.env, validate.py (checks the bundle and settings
│                               docs), tests/ (python -m unittest discover -s deploy/tests)
│
├── docs/
│   ├── STRUCTURE.md            this file
│   ├── MODULES.md              module contract: shared APIs, UI kit, data rules, cross-module interfaces
│   ├── INTEGRATION.md          guide for porting the apps into LinkedEyeWebProject
│   │                           (Prometheus, Alertmanager and log ingest are configured in Administration › Integrations)
│   ├── DEPLOYMENT.md           Docker Compose, Kubernetes, settings reference, worker, backups
│   ├── FINSPOT_DEV_K8S.md      the finspot-dev Kubernetes overlay and kubeconfig handling
│   ├── adr/                    architecture decision records (0001 scheduler and event delivery, 0002 Django 5.2 LTS)
│   └── APM · HARDWARE · HYPERVISOR · NETWORK · ARCHITECTURE_PAGE · IMPORT · ONBOARDING · OTP_PATHS ·
│       NOTIFICATION_TEMPLATES     one document per module or feature that needs more than a MODULES.md section
│
├── backend/                    LEGACY PROJECT (LinkedEyeWebProject): a separate Django project with its own Dockerfile,
│                               settings and legacy_tests/. The console never imports or reads it, and .dockerignore keeps it
│                               out of the console image (CI asserts it; see SECURITY.md). It keeps its own iframeGraphs/ and
│                               template/DIRECT/, which its code reads by relative path; the console's copies are in
│                               network/legacy/ (see the module list below)
│
├── linkedeye/                  project package
│   ├── settings.py             env-driven settings (LINKEDEYE_*), INSTALLED_APPS, security headers
│   ├── urls.py                 dashboard, login, /monitoring/url-checker/, /domains/, /agent/, then every module app at ""
│   │                           (modules declare full paths), /legacy/ sample pages (404 unless LINKEDEYE_SHOW_SAMPLE_PAGES), /soon/, /admin/
│   └── wsgi.py
│
├── core/                       PLATFORM: shared by every module
│   ├── models.py               Site, Membership (role per site), AuditEvent, Event
│   ├── access.py               can() / require() / visible_sites() / scope_by_site()  (view·operate·configure·admin)
│   ├── audit.py                audit(request, action, obj, **details)
│   ├── events.py               emit(...) → Event row + event_emitted signal
│   ├── notify.py               webhook / e-mail for critical & warning events
│   ├── jobs.py                 register(name, interval, func) · run_due()
│   ├── prometheus.py           query / query_range / targets / alerts client
│   ├── middleware.py           login required (except /healthz and token-auth APIs); active client scope
│   ├── auth_views.py, login_otp.py   sign-in with an e-mailed one-time code (docs/OTP_PATHS.md)
│   ├── secrets.py              credential vault (Fernet), referenced by id from other apps
│   ├── overview.py             problems, KPIs, site matrix and market-day strip behind the dashboard
│   ├── backend_summary.py      optional read-only bridge to the legacy dashboard API (never called in a page view)
│   ├── client_scope.py, client_sites.py   per-client scoping of sites (?client=)
│   ├── notify_templates.py, welcome_mail.py   notification and welcome e-mail rendering
│   ├── nav.py                  sidebar menu → URL names (unbuilt names fall back to "coming soon")
│   ├── context_processors.py   nav state, site selector, problem count
│   ├── views.py                dashboard, global search, healthz, legacy sample pages
│   ├── samples/                fabricated data for the gated /legacy/ design mock-ups only (never on a live console)
│   ├── templatetags/le.py      {% icon %} {% ficon %} {% badge %} {% delta %}
│   ├── templates/core/         base.html (shell: sidebar, top bar, site selector), dashboard, pager
│   ├── static/linkedeye/       app.css (UI kit), app.js, charts.js (SVG line/bars/donut/spark)
│   └── management/commands/    bootstrap_sites, seed_examples
│
├── probes/                     PLATFORM: network probes + scheduler
│   ├── checks.py               http_check / dns_check / tls_check, SSRF guard (resolve → check_address → pinned connect)
│   ├── runner.py               runs due URL & domain checks, emits events on state change
│   └── management/commands/run_probes.py   the worker loop: probes + every registered job
│
│   ── MODULES (one app per sidebar section) ──────────────────────────────
├── urlchecker/                 Monitoring › URL Checker          /monitoring/url-checker/  UrlCheck, UrlResult
├── domainmonitor/              Domains (overview, list, DNS, SSL, uptime, performance, security, export)
│                                                                 /domains/…  /configuration/domains/api/   Domain, DomainCheck
├── servicedesk/                Service Desk                      /service-desk/…   Ticket (INC/PRB/CHG/REQ), SLA, catalog
├── cmdb/                       Assets & CMDB, Heatmap            /assets/… /cmdb/… /monitoring/heatmap/
├── network/                    Network devices & discovery       /network/…        Device, Interface, DiscoveryRun
│   └── legacy/                 iframeGraphs/ (device front-panel .j2 + query JSON) and template/DIRECT/ (Neo4j onboard
│                               templates) from the earlier Global LinkedEye, read by network/legacy_panels.py (see its README)
├── telemetry/                  Infrastructure, Nagios, Prometheus, Alerts, Metrics explorer
│                                                                 /monitoring/…  /api/v1/alertmanager/
├── observe/                    Exporters, Logs & Events          /monitoring/exporters/  /logs/  /api/v1/logs/
├── tcpcheck/                   Telnet/TCP checks, unified Uptime /monitoring/telnet/  /monitoring/uptime/
├── bodeod/                     BOD / EOD market-day validation   /bod-eod/…
├── automation/                 Runbooks, StackStorm, workflows, templates   /automation/…  /templates/
├── administration/             Users & roles, sites, audit log, integrations, system health   /administration/…
├── reports/                    Report library (CSV, preview, scheduled e-mail), cross-module Analytics
│                                                                 /reports/…  /analytics/
├── agent/                      Ops Agent (threads, RAG, tools)   /agent/
├── ipam/                       IP address management (prefixes, addresses)
├── dcim/                       Data centre inventory (racks, rooms, equipment)
├── compute/                    Compute & virtualization (hosts, VMs, host metrics)
├── onboarding/                 Onboarding wizard: sites, servers, devices, exporters, Prometheus discovery
├── k8s/                        Kubernetes inventory imported from the client workbook (read-only)
├── apm/                        APM: adapter matrix, process status, execution logs from the cron toolkit
├── architecture/               Data Center Architecture: spine-leaf view drawn from devices, links and racks
├── hardware/                   Server hardware over Redfish (Dell iDRAC, HPE iLO), read-only: Hardware tab, BMC endpoints, control requests
└── hypervisor/                 vCenter (read-only): VM detail tabs, datastores, VM-to-device matching, control requests
```

## Inside a module app

Every module follows the same layout, so they are easy to navigate:

```
<app>/
├── apps.py            AppConfig.ready(): register periodic jobs and event listeners
├── models.py          data (site-scoped via a `site` field; tenant_key where data can be multi-customer)
├── migrations/
├── services.py        public functions other apps may call (e.g. servicedesk.services.open_incident)
├── forms.py
├── views.py | views/  one module per page group when there are many pages
├── urls.py            app_name = "<app>", full paths
├── jobs.py            periodic work run by `manage.py run_probes`
├── admin.py
├── templates/<app>/   page templates extending core/base.html; partials start with "_"
├── static/<app>/page.css   page styles, scoped under .pg-<app>
├── templatetags/      app-specific filters (optional)
├── management/commands/seed_<app>.py   idempotent, clearly-marked example data (never runs automatically)
└── tests.py
```

## How the parts fit together

```
               ┌──────────── manage.py run_probes (worker) ────────────┐
               │  probes.runner (URL, domain)   core.jobs.run_due()     │
               │      │        tcpcheck · network · observe · telemetry │
               │      │        bodeod · cmdb · reports (scheduled e-mail) │
               └──────┼─────────────────────────────────────────────────┘
                      ▼
 Alertmanager ──► core.events.emit() ──► Event table ──► Logs & Events page
 log shippers  ──►        │
                          ├──► core.notify  → webhook / e-mail (critical, warning)
                          ├──► servicedesk listener → open / resolve incident by fingerprint
                          └──► automation rules → StackStorm / webhook execution

 Browser ──► core.middleware (login) ──► app view ──► core.access (role & site scope)
                                               └──► core.audit on every change
```

* **Two processes:** the web app (gunicorn) and the worker (`manage.py run_probes`). The worker runs every
  probe and every job that an app registered with `core.jobs.register`.
* **One event stream:** each state change in any module goes through `core.events.emit`, so notifications,
  incident creation and automation rules work the same way everywhere.
* **Real data only:** pages read the database or a configured integration. When an integration is not
  configured, the page says so and names the setting. `seed_<app>` commands create labelled example records
  for demos.
* **Access:** `core.models.Membership` gives each user a role (admin / operator / viewer) for one site or
  for all sites. Views check permissions with `@require(...)` and filter records with `scope_by_site(...)`.

## Commands

```bash
python manage.py migrate
python manage.py bootstrap_sites          # create the sites from settings
python manage.py createsuperuser
python manage.py runserver                # web
python manage.py run_probes               # worker (probes + jobs)
python manage.py seed_examples            # optional demo data (also seed_<app> per module)
python manage.py test
```
