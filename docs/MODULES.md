# Module contract

Every LinkedEye module is a Django app that owns its directory. Shared code lives in `core/`, `probes/`,
`urlchecker/` and `domainmonitor/`; modules use it but do not edit it.

## Wiring (already done for each module)

* App listed in `INSTALLED_APPS`; root `urls.py` includes `"<app>.urls"` at `""`, so **the app's `urls.py`
  declares full paths** (e.g. `path("service-desk/incidents/", ...)`) with `app_name = "<app>"`.
* Sidebar entries (core/nav.py) point at URL names; a name that does not resolve yet falls back to a
  placeholder page. Required URL names per module are listed below.
* Page CSS: `<app>/static/<app>/page.css`, linked from templates with
  `{% block page_css %}<link rel="stylesheet" href="{% static '<app>/page.css' %}">{% endblock %}`.
  Scope rules under a wrapper class, e.g. `<div class="pg-<app>">`.

## Shared platform API

| Need | Use |
|---|---|
| Permission check | `from core.access import can, require, visible_sites, scope_by_site, role_for`. Perms: `view`, `operate` (run, acknowledge, add/edit records), `configure` (same rank as operate), `admin` (delete, settings, users). Decorate views with `@require("operate")`; templates get no permission variables from the base context: compute a boolean in the view with `can(request.user, "operate")` and pass it (e.g. `can_operate`, see below). |
| Site scoping | Records store the site **name** in a `site` CharField (`"All Sites"` = global). Filter querysets with `scope_by_site(qs, request.user, field="site")`. Sites live in `core.models.Site` (name, code, region, timezone, criticality). |
| Audit | `from core.audit import audit`; call `audit(request, "create"|"update"|"delete"|"run"|..., obj, **details)` after every mutation. |
| Events | `from core.events import emit, event_emitted`; `emit(source="<app>", title=..., severity="critical"|"warning"|"info"|"ok", message=..., site=..., obj=..., fingerprint="<app>:<pk>", **data)` on every state change. Listen with `@receiver(event_emitted)` (args: `sender, event`). Never raise from a listener. |
| Periodic jobs | `from core.jobs import register`; in `AppConfig.ready()` call `register("<app>.<job>", interval_seconds, func)`; `manage.py run_probes` runs them. Jobs must be idempotent, bounded (timeouts) and must not raise. |
| Prometheus | `from core import prometheus`; `prometheus.configured()`, `query(promql)`, `query_range(promql, seconds, step)`, `targets()`, `alerts()`; errors raise `prometheus.PrometheusError` (show its message, never a stack trace). |
| Network probes | `from probes import checks`; `checks.http_check(url, ...)`, `checks.dns_check(host)`, `checks.tls_check(host, port)`, `checks.fetch_bytes(url, max_bytes, ...)` (https-only, size and time limited download of an asset), `checks.resolve_allowed(host, port)` (SSRF policy), `checks.check_address(ip)`. Any new socket code must go through `resolve_allowed` / `check_address`. |
| Site health overview | Import lazily inside the view: `from core import overview`. `overview.site_names(user)` (visible site names), `overview.site_matrix(user, selected_site="", names=None)` (per-site / per-kind counts; an out-of-scope `selected_site` returns no rows). Set `request.le_site_names = names` so the base context reuses them. Render with `{% include "core/_legacy_heatmap.html" %}` + `linkedeye/pages/site-heatmap.css`. |
| Legacy backend summary | `{% include "core/_backend_health.html" %}` + `linkedeye/pages/backend-health.css` / `backend-health.js`; the page fetches `/api/backend-summary/` (`core.backend_summary.summary`) asynchronously, so never call it in a page view. |
| Notifications | Automatic for critical/warning events (core/notify.py); nothing to do. |
| Example (demo) data | A `seed_*` command must mark what it writes and remove exactly that with its own `--remove`; register it as an `Area` in `core/examples.py` (counts, removal) so `manage.py remove_examples`, the Administration > System health "Demo data" card, the admin banner and the go-live warning know it, and add it to the mixed real/example test in `core/test_examples.py`. A live page never shows an invented number: show "not reported" or an empty state that names what to connect. Audit and contract: [LIVE_DATA.md](LIVE_DATA.md). |
| Tenant | `settings.LINKEDEYE_TENANT`; models carry `tenant_key` where data could belong to more than one customer. |
| Ops Agent | `agent` at `/agent/`. Threads are per-user. Tools call `core.overview` and other apps lazily and are always `scope_by_site`. The model key is `LINKEDEYE_AGENT_API_KEY` (server-side only). |

`base.html` context (`core.context_processors.shell`) carries navigation, the site and client selectors and the problem count, but no
permission variables: use `core.access.can(request.user, "operate")` in the view and pass a boolean (e.g. `can_operate`) to the template.

## UI kit (core/static/linkedeye/app.css, core/templatetags/le.py, charts.js)

KPI cards (`.kpi.stacked > .kpi-top > .tile.s-* + .kpi-body`), panels (`.panel`, `.panel-h`), grids (`.grid .g-2
.g-3 .g-4 .g-main .g-charts`), tables (`.table-wrap > table`, `th.cb`/`td.cb`, `.row-actions`), chips
(`.chip.ok|warn|crit|purple|mute|teal`), `{% badge status %}`, `{% icon name %}`, `{% ficon name %}`,
`{% delta value good="up|down" %}`, tabs (`nav.tabs > a.on`), pager (`{% include "core/_pager.html" %}` with
`page` + `query`), charts (`div.chart[data-chart=line|bars|donut|spark][data-src]` + `json_script`),
`.empty` for empty states, `.messages` via Django messages. Forms: `.form-grid > .field` with labels.
Follow the existing pages (urlchecker/templates/urlchecker/index.html, domainmonitor/templates/…) for markup.

Theme: the classic LinkedEye look (near-black page, `#1f1f1f` rail and cards, one amber accent), defined by the
`:root` tokens at the top of `app.css`. Page CSS and Python use tokens, never hex values: `--panel`, `--ink`, `--muted`,
`--primary`, status families `--ok|warn|crit|info|purple|teal` with `-bg`, `-ink` and `-line`, `--on-solid` for text on a
solid status fill, `--active-fill`/`--active-ink` for selected pills. Chart colours from Python are strings such as
`"var(--chart-1)"` (`--chart-1` to `--chart-10`, `--chart-ok|warn|crit`); `charts.js` resolves them before writing SVG
attributes. No web font is loaded. The shell is an icon rail (`.side`) that expands on hover or keyboard focus and is
pinned open by the menu button (`.shell.rail-open`). Nav labels sit in `.lbl`, clipped rather than removed while collapsed.

## Data rules

* Pages show **real data from the database or a configured integration**. When an integration is not
  configured, show a clear "not connected" state naming the setting to set; never fabricate numbers.
* Each module may ship `manage.py seed_<app>` that creates clearly-marked example records (description or tag
  "example") for demos. It must be idempotent and never run automatically.
* Every list view: search/filter where useful, pagination (`?per=`), CSV export where useful.
* Every mutation: POST + CSRF, permission check, `audit(...)`, success message, redirect.
* **Integration settings** (Alertmanager and log-ingest tokens, StackStorm, Nagios, SMTP, webhooks, Prometheus) are saved in
  Administration › Integrations and read **only** through `administration.integration_config` (`ic.alertmanager_webhook_token()`,
  `ic.log_ingest_token()`, `ic.stackstorm_url()`/`stackstorm_api_key()`, `ic.nagios_*()`, `ic.email_*()`, `ic.default_from_email()`).
  A console value wins; the `LINKEDEYE_*` environment / Django setting is the fallback. Never read those settings directly in a
  consumer, or the card says "Source: Console" while the code ignores it. Scheduled report e-mail uses the same SMTP rule as
  `core.notify` (`reports.scheduler`).
* **Automation and StackStorm** (`automation`): StackStorm runs tenant-wide remediation under one API key, so creating, editing,
  test-running (`Run the action for real`) or re-running anything that triggers it needs `operate` on **every site**, the same as the
  manual trigger page. Site operators keep the other rule actions for their site. A rule's cooldown counts only executions that
  succeeded or are still pending: a failed attempt (StackStorm or the webhook was down) never silences the next matching event.
  Webhook URLs may not contain whitespace or control characters, and webhook errors never quote the URL path (Slack/Teams secrets).
* **Report exports** (`reports.registry.export_rows`) are complete or refused: a CSV download or scheduled e-mail with more than
  `MAX_ROWS` (50,000) rows fails with a "narrow the period or site" message instead of being cut off. A schedule's `site` must be a
  site its owner can operate (checked in `ScheduleForm` and again by the scheduler).

## Kubernetes inventory (`k8s`)

Holds the **client Kubernetes estate** as a read-only `kubectl` discovery recorded it, delivered as a
workbook. It is inventory, not live health: live cluster health stays in `compute` (Compute → Kubernetes,
read from kube-state-metrics through Prometheus). The two link up — a `KubeCluster` can point at a
`compute.Cluster`, and a `Node` at the `cmdb.ConfigurationItem` / `network.Device` / `compute.ClusterMember`
that already describe the same machine.

* **Pages** `k8s:overview` (`/kubernetes/`), `k8s:clients`, `k8s:namespaces`, `k8s:nodes`,
  `k8s:node_detail`, `k8s:import_inventory`, `k8s:imports`, `k8s:import_detail`, plus a CSV export beside
  each list. Sidebar: **Kubernetes**, after Compute.
* **Tab strip** `k8s/tabs.py` is the single list of sections. A page for one of the remaining workbook
  sheets (`k8s:workloads`, `k8s:services`, `k8s:ingresses`, `k8s:volumes`, `k8s:certificates`, `k8s:tools`,
  `k8s:issues`) adds its path to `k8s/urls.py` and flips `"enabled": True` on its row; nothing else changes
  and the strip never links to a URL name that does not resolve.
* **Import engine** `k8s/importer.py`. `register_sheet(name, order, fn, namespaces=False)` maps one workbook sheet to one
  importer callable (`namespaces=True` when it attaches rows to namespaces via `resolve_namespace()`), registered from its own module under `k8s/sheets/` (listed in `k8s/sheets/__init__.py`).
  `run_import()` calls them in order, each in its own transaction, and records per-sheet counts, warnings
  and errors on the `ImportRun`. Sheet importers use the shared `ImportContext`: `upsert()` (idempotent
  create-or-update plus the created/updated counts), `track()` (which rows the run owns, so ones it did not
  see are marked missing), `resolve_client()`, `resolve_namespace()`, `cluster_for()`, `resolve_site()`,
  `warn()` / `error()`. Sheets 04–12 should register at order 60 or above, after the backbone sheets.
* **Versioning** an import is idempotent: the same workbook twice changes nothing. A row the new snapshot
  no longer contains gets `missing_since` (cleared if it comes back) — never deleted, never silently kept
  as current. `dry_run=True` reports the same numbers and writes nothing.
* **Entry points** `manage.py import_k8s_inventory <path> [--dry-run] [--create-compute] [--site NAME]`,
  and `/kubernetes/import/` for admins on every site (dry-run diff first, then a confirm button). Import runs are
  site-scoped for viewing: a site-limited user sees the runs filed under their sites and under All Sites.
* **Data rules** the workbook describes a real customer estate, so **none of it belongs in this
  repository** — not a row, host name, address, client name, URL or Secret name. Tests use synthetic
  fixtures (`k8s/tests/fixtures.py`: "Acme"/"Globex", RFC 5737 addresses, example.com). The sheets carry
  Kubernetes Secret **names** only and this module has no field that could hold a value. The collector's
  placeholder strings ("NOT DISCOVERED", "NOT APPLICABLE", "MANUAL INPUT REQUIRED",
  "MULTIPLE / SEE NODE INVENTORY") mean *no value*: `k8s.parsing.clean()` normalises them to empty and the
  pages show a dash. See **docs/IMPORT.md** for the workbook format and the discovery commands.

## APM (`apm`)

Adapter status matrix, process status and execution logs from a customer's cron toolkit, pushed over HTTPS (or read from their
Redis). Full description, API and setup: **docs/APM.md**.

* **Pages** `apm:index` (`/apm/`), `apm:live` (JSON the page patches itself from), `apm:cell` (one adapter's detail as JSON),
  `apm:cell_retry`, `apm:cell_rca`, `apm:cell_evidence`, `apm:export`, and the sources admin `apm:sources`, `apm:source_new`,
  `apm:source_edit`, `apm:source_rotate`, `apm:source_toggle`, `apm:source_delete`, `apm:source_test_redis`. Sidebar group **APM**.
* **Machine API** `/api/apm/ingest/`, `/api/apm/commands/`, `/api/apm/commands/<id>/done/`: per-source bearer token, no session, no
  CSRF; `/api/apm/` is the one new entry in `core.middleware.PUBLIC_PREFIXES`.
* **Service** `apm.services.ingest_matrix / ingest_table / ingest_logs` is the only way data enters (the push API, the Redis reader
  `apm.redis_reader` and `seed_apm` all call it). Parsing and limits live in `apm.payloads`; the dashboard JSON in `apm.snapshot`.
* **Site scoping** `apm.Source.site` is a site *name* and is listed in `administration.services.SITE_NAME_FIELDS`. Adapter cells,
  runs, status history, processes, logs and retry requests carry no site: they belong to one source and are scoped through
  `source__site` (a test keeps it that way).
* **Events** `core.events.emit("apm", ...)` on an adapter or process going down and recovering (or a failing one dropping out of the report) (fingerprints
  `apm:cell:<source>:<client>:<segment>` and `apm:process:<source>:<host>:<name>`) and when a source goes silent (`apm:source:<id>`).
* **Cross-app** the credential vault (`core.secrets`, row in `onboarding.Credential`, referenced by id) for the Redis password; Service
  Desk (`servicedesk.services.create_ticket`, lazily) for Generate RCA; `probes.checks.resolve_allowed` for the Redis host.
* **Jobs** `apm.redis_poll` (60 s), `apm.stale` (60 s), `apm.retention` (6 h). Optional dependency: `redis` (`requirements-apm.txt`),
  imported lazily.
* **Seed** `manage.py seed_apm [--remove] [--rotate-token]`: synthetic, idempotent, no events.

## Server hardware (`hardware`)

Dell iDRAC / HPE iLO over Redfish, **read-only** (no write call is ever made to a BMC). Full description, setup and honest limits:
**docs/HARDWARE.md**.

* **Pages** the **Hardware (iDRAC/iLO)** tab of a physical server (`network:detail?tab=hardware`, wired lazily from `network.views.detail`
  through `hardware.tab.context`), `hardware:live` (JSON the tab patches itself from), `hardware:refresh`, `hardware:vendor`,
  `hardware:inventory`, `hardware:events`, `hardware:export` (CSV), `hardware:alerts`, `hardware:request_new`, `hardware:requests`,
  `hardware:request_decide`, and the endpoints admin `hardware:endpoints`, `endpoint_new`, `endpoint_edit`, `endpoint_test`,
  `endpoint_pin`, `endpoint_toggle`, `endpoint_delete`. Sidebar group **Server Hardware**.
* **Client** `hardware/redfish.py`: standard library only, GET only, SSRF policy through `probes.checks.resolve_allowed`, certificate
  verify / pin / skip, 1 MB cap, deadline, no redirects. `hardware/collector.py` walks the tree, `hardware/parsers.py` turns it into
  bounded plain values. A test fails if a write method or verb appears.
* **Site scoping** `BmcEndpoint` belongs to a `network.Device` and has no site of its own, so it is deliberately NOT in
  `SITE_NAME_FIELDS` (a rename follows through the device); everything else is scoped through `endpoint__device__site`.
* **Events** `core.events.emit("hardware", ...)` once per transition of overall health or connection, fingerprint `hardware:<endpoint id>`,
  attached to the server device.
* **Cross-app** the credential vault (`core.secrets`, row in `onboarding.Credential`, referenced by id), `network.Device`, `probes.checks`.
* **Jobs** `hardware.poll` (30 s), `hardware.retention` (1 h). No new dependency.
* **Seed** `manage.py seed_hardware [--vendor dell|hpe] [--site NAME] [--remove]`: synthetic example server, never polled, no events.

## Hypervisor (`hypervisor`)

vCenter, **read-only** (no write call is ever made to a vCenter or ESXi host). Fills the stub tabs of a virtual machine's device page
(Overview, Storage, Network, Snapshots, Guest OS, Alerts) from the vSphere Automation REST API. Full description, setup and honest limits:
**docs/HYPERVISOR.md**.

* **Pages** the VM tabs of `network:detail` (wired lazily from `network.views.detail` through `hypervisor.vmview.vm_context`),
  `hypervisor:vm_live` (JSON they patch themselves from), `hypervisor:vm_request`, `hypervisor:datastores` (+ `datastores_live`,
  `datastores_export`), `hypervisor:unmatched` (+ `match_create`, `match_link`, `match_ignore`, `match_unignore`, `match_unlink`),
  `hypervisor:requests` (+ `request_decide`, `requests_export`) and the sources admin `hypervisor:sources`, `source_new`, `source_edit`,
  `source_rotate`, `source_toggle`, `source_delete`, `source_test`, `source_pin`, `source_sync`. Sidebar group **Hypervisor**.
* **Client** `hypervisor/vsphere.py`: standard library only, GET only plus the one documented session POST / DELETE, SSRF policy through
  `probes.checks.resolve_allowed`, certificate verify / pin / skip, 1 MB cap, deadline, no redirects. `hypervisor/parsers.py` turns the
  answers into bounded plain values. A test fails if another method appears. Snapshots, creation date and event history come from an
  OPTIONAL provider (`hypervisor/pyvmomi_provider.py`, `requirements-vsphere.txt`, imported lazily).
* **Site scoping** `VCenterSource.site` is a site *name* and is listed in `SITE_NAME_FIELDS`. Hosts, datastores, VM records, snapshots,
  events and control requests carry no site: they belong to one source and are scoped through `source__site` (a matched VM must also be
  visible through its device's site). A test keeps it that way.
* **Matching** `hypervisor/matching.py`: UUID, then name / guest host name / IP; matched, ambiguous or unmatched, never merged silently.
* **Events** `core.events.emit("hypervisor", ...)` once per transition (VM powered off, tools not running, datastore at 90 % / 95 %,
  snapshot older than 7 days, vCenter unreachable), fingerprints `hypervisor:vm:<id>:<kind>`, `hypervisor:datastore:<id>`,
  `hypervisor:source:<id>`, attached to the VM's device when it has one.
* **Cross-app** the credential vault (`core.secrets`, row in `onboarding.Credential`, referenced by id), `network.Device`,
  `compute.VMPlacement` / `compute.Cluster` (filled from the sync; values an operator typed by hand are never overwritten),
  `probes.checks`.
* **Jobs** `hypervisor.sync` (60 s, picks due sources), `hypervisor.retention` (6 h). Optional dependency: `pyvmomi`.
* **Seed** `manage.py seed_hypervisor [--remove]`: synthetic example VM, never synced, no events.

## Data Center Architecture (`architecture`)

Draws the spine-leaf view of the data centre (Internet / WAN, firewalls, core / border, spine, leaf, server groups) from
`network.Device`, the new `architecture.Link` (physical cables between two devices) and `dcim` racks, live. Full description:
**docs/ARCHITECTURE_PAGE.md**.

* **Pages** `architecture:overview` (`/network/architecture/`), `architecture:live` (JSON), `architecture:links` (+ `links_export`,
  `links_import`, `suggest` for LLDP, `link_create`, `link_edit`, `link_delete`, `links_bulk`), `architecture:placement_edit`,
  `architecture:example_load`. Sidebar: **Network Devices › Architecture**.
* **Code** `tiers.py` (the one tier / group inference function), `layout.py` (pure layout engine, no Django), `faces.py` (pure,
  generated front-panel faces as SVG symbols, no Django, no images), `services.py`
  (scoping, link state, page and live payloads), `csvio.py`, `lldp.py`, `example.py` (used by `manage.py seed_architecture`).
* **Scoping** a Link has no site name: it is visible only when both devices are visible and editable only when both devices'
  sites are operable, so it is deliberately **not** in `SITE_NAME_FIELDS`.

## BOD / EOD live checks (`bodeod`)

The automated checklist items (kind `domain`, `url_check`, `tcp_check`) show the **live** state of the check they reference. The
pages (Status Dashboard cards, Key Validation, Checklist, and the Live-now column of the open run) patch themselves in place;
there is no reload loop.

* **Read path** `GET /bod-eod/api/live/` (`bodeod:api_live`, optional `?mode=BOD` or `?run=<id>`) returns, per mode, the overall live
  status and counts plus each automated item's `status` (`ok | warning | failed | unknown`; the stored "critical" is "failed"),
  a short `detail` ("DNS resolves; certificate valid, 41 days left"), the long wording, the referenced object's `link`, `checked_at`
  (ISO) / `checked_at_ist` and `age_s`. It reads the **stored** results of the referenced checks (`bodeod.live.evaluate`): a GET never
  probes anything. Site-scoped like the pages (`_modes`, resolver scoped to the mode's own site), rate limited per user (120 per
  minute, 429 with `Retry-After`), `Cache-Control: no-store`. The number of queries depends on the number of modes, never of items.
* **Re-check** `POST /bod-eod/api/recheck/` (`bodeod:api_recheck`; form fields `mode`, optional `run`, optional repeated `item`
  keys `i<checklist item id>` / `r<run row id>`): operator on the mode's site (a global mode needs the role on every site), CSRF
  (`X-CSRFToken`), audited (`recheck`), 12 per user per minute, one re-check per mode at a time (cache lock, so with several
  gunicorn workers the lock is per process). It calls `services.refresh_references` (so `probes.checks` and its SSRF policy) on a
  worker thread and waits at most `live.RECHECK_WAIT` (6 s): the answer is `state` = `done | partial | failed | skipped`, or
  `running` / `busy` with HTTP 202 when the probes are still going (they finish in the background and the page keeps polling).
  Manual rows are never refreshed. The run itself is never edited: a live failure changes what the page shows, sign-off stays manual,
  and "Re-validate" is still the only way to record the current state in a run.
* **Page script** `bodeod/static/bodeod/bodeod.js`, configured by a `json_script` (`#bod-live-cfg`). Rows carry
  `data-live-row="<key>"`, `data-live-status`, and children `data-live="pill|detail|age|diff"`; modes carry `data-live-mode`. It polls
  every 15 to 30 s (jittered), not while the tab is hidden or a text field has focus, backs off after errors (and on 429), shows a
  "stale" banner when the data is three intervals old, flags changed rows (highlight, "Changes since you opened this page" list,
  one throttled `aria-live` message), tells the viewer when a newer run exists, and writes everything with `textContent`. Without
  JavaScript the status page falls back to `<noscript><meta http-equiv="refresh">` and the other pages to a "Reload" link.
* **Tests** `bodeod/test_live.py` (shape, scoping, GET makes no probe, re-check permission/CSRF/audit/rate limit/bounded wait, query
  count, IST, hostile names, the JS contract). The in-browser check with a fake clock and stub servers is
  `bodeod/browser/live_check.js` (manual; needs Playwright and a running server, see the header of the file).

## Go-live readiness (`administration.golive`)

`manage.py go_live_check` and the "Go-live readiness" card on Administration > System health share one registry of read-only
checks in `administration/golive.py` (PASS / WARN / FAIL, a fix hint each, never a secret value). **Other apps add a check
without editing that file**: put `golive_checks.py` in the app (imported automatically) and decorate a function with
`administration.golive.register(key, label)`; it receives a `Context` and returns a `Result` (`ok` / `warn` / `fail`) or a list
(`core/golive_checks.py` does this for example data). A check that raises is reported as FAIL, not raised. The deployment pack
that uses it is `deploy/live/` (docs/GO_LIVE.md).

## Cross-module interfaces

Modules may depend on each other only through these names, imported lazily inside functions
(`from django.apps import apps; M = apps.get_model("servicedesk", "Ticket")`) and guarded so the caller still
works if the other module is absent.

| Provider | Interface |
|---|---|
| servicedesk | `servicedesk.services.open_incident(title, description="", priority="P3", site="", source="", fingerprint="", ci_name="") -> Ticket` (dedupes open incidents by fingerprint); `servicedesk.services.resolve_by_fingerprint(fingerprint, note="")`. Model `Ticket` (kind: incident/problem/change/request; number like INC-1287; status; priority P1-P4; site). |
| cmdb | Model `ConfigurationItem` (name, ci_type, site, status, environment, owner, business_domain, attributes JSON). `cmdb.services.find_ci(name)`. |
| network | Model `Device` (name, device_type: switch/router/firewall/load_balancer/wireless/physical_server/virtual_machine, vendor, model, mgmt_ip, site, status: up/down/unknown, last_seen, `image` FK to a stored photo). Photos: `network.images.store_image(bytes, source=...)` is the only way bytes enter `DeviceImage` (validated by `network.imaging.inspect_image`); `network.images.fetch_and_store(url)` downloads through `probes.checks.fetch_bytes`. Device card: `network.live.card_data(devices)` and the partial `network/_device_card.html` (see docs/NETWORK.md). Per-port page: `network:interface` (`/network/devices/<pk>/interfaces/<ifIndex>/`, plus `interface_live` and `interface_export`), built by `network.portviews` from `network.portmetrics` (graphs, optics, PoE, LLDP/CDP, VLAN, LAG, STP read live from Prometheus, cached); `Interface` also stores mtu / mac / duplex / last_change_at from the metrics job. `Interface` has no site of its own (visibility comes from its device), so it is not in `SITE_NAME_FIELDS`. See docs/NETWORK.md. |
| architecture | Model `Link` (`a_device`, `b_device`, `a_port`, `b_port`, `medium`, `speed_mbps`, `kind`, `origin`; ends stored in canonical order) and `Placement`; `architecture.services.links_queryset(user, request)` (links whose both ends the user can see). |
| tcpcheck | Model `TcpCheck` (name, host, port, site, last_status: online/down/unknown, last_checked_at, last_response_ms). |
| automation | `automation.services.trigger_stackstorm(trigger, payload) -> Execution`. |
| urlchecker / domainmonitor | Existing models `UrlCheck`, `UrlResult`, `Domain`, `DomainCheck` (read-only for other modules). |
| k8s | Models `KubeCluster`, `Node`, `Namespace`, `ClientProfile`, `ImportRun`. `k8s.services.clusters_for(user)` / `nodes_for(user)` / `namespaces_for(user)` / `profiles_for(user)` (site-scoped querysets), `k8s.services.can_import(user)`, `k8s.services.last_run()`. Import engine: `k8s.importer.run_import(source, user=None, dry_run=False, site="All Sites", create_compute=False)` and `k8s.importer.register_sheet(name, order, fn)`; cell parsing in `k8s.parsing`. |

## IPAM, DCIM and Compute: behaviour notes

* **IPAM site scoping inside a global container.** A subnet marked *All Sites* is visible to everyone, but its detail
  page lists only the addresses, child subnets, counts and map cells of sites the viewer can see
  (`services.addresses_in(subnet, user)`, `services.address_map(subnet, user=user)`); hidden ranges are drawn as
  anonymous "in a child subnet you cannot see" cells. "Reserve next free" still avoids every recorded address.
* **IPAM writes** (address, subnet, VLAN, zone, record, CSV import, reserve) turn a unique-constraint race into a form
  error (reserve retries with the next free address) instead of an HTTP 500. Zone ids (`fe80::%eth0/64`) are
  rejected; two blocks with the same prefix and range are duplicates. `manage.py seed_ipam --remove` deletes only the
  rows `seed_ipam` writes (exact CIDR / address / VID plus the exact seed description).
* **DCIM live readings** are read by `dcim.readings` every 300 s. A reading older than 15 minutes
  (`dcim.models.LIVE_MAX_AGE`) is no longer "live": the manual value is used again. Temperature queries use the
  mean of the matching series, power queries the sum; a scalar result must be wrapped in `vector(...)`.
* **Prometheus outages** are paid for once: after the first "unreachable / timeout / 5xx" error, `dcim.services.read_all`,
  `compute.jobs.collect_k8s` and the Kubernetes page stop asking (the page remembers the outage for 15 s), and both
  jobs stop after a 240 s wall-clock budget. A failing single query does not short-circuit anything.

## Prometheus discovery

Once Prometheus is connected, the product gathers its own inventory instead of waiting for an operator to type
each host into the CMDB. `telemetry/discovery.py` is the whole service; `telemetry.DiscoveredTarget` is the
record; **Monitoring › Discovery** (`/monitoring/discovery/`) is the page.

### What is gathered

A pass reads `/api/v1/targets` and then asks Prometheus a fixed set of **bulk** instant queries — one query per
metric across every instance, never one query per host — so the cost does not grow with the estate:

* phase 1, always run: `node_uname_info`, `windows_cs_hostname`, `sysName`. These say which instances are Linux
  hosts, Windows hosts and SNMP devices (a series counts only when it carries the label its text lives in);
* phase 2, only for the families found: Linux ≤ 10 queries, Windows ≤ 8, SNMP ≤ 2, and kube-state-metrics 7 when a
  target's job or scrape pool is named like one (`kube-state`, `kube_state`, `ksm`). The exact queries are the
  tables below.

So a pass is 3 queries when nothing matches and at most 30 when every family is present.

#### What is gathered, field by field

Everything below lands on `DiscoveredTarget` (columns) or in its `details` JSON, and is shown on the target's
detail page. "Targets API" means `/api/v1/targets` itself, which costs no extra query.

| Field | Where it lives | Source |
|---|---|---|
| instance, job, scrape pool | columns | Targets API: `labels.instance`, `labels.job`, `scrapePool` (instance falls back to `discoveredLabels.__address__`) |
| target labels, discovered labels | `labels`, `discovered_labels` | Targets API: `labels`, `discoveredLabels` (service-discovery metadata such as `__meta_*`), capped at 60 each |
| health, last error, scrape URL, last scrape | columns | Targets API: `health`, `lastError`, `scrapeUrl` (credentials in the URL are removed), `lastScrape` |
| scrape interval, timeout, last scrape duration | `details.scrape` (`interval`, `timeout`, `duration`, seconds) | Targets API: `scrapeInterval`, `scrapeTimeout` (older Prometheus omits them), `lastScrapeDuration` |
| address, IP | `details.address`, `details.ip` | the instance label |
| site | column | the configured target label (default `site`) |
| kind | column | metric evidence, then job / pool name (see Kind detection) |
| **Linux** hostname, kernel, architecture | `details.hostname`, `kernel`, `arch` | `node_uname_info` (`nodename`, `sysname` + `release`, `machine`) |
| OS | `details.os` | `node_os_info` (`pretty_name`, else `name`, `id`) |
| exporter version | `details.exporter_version` | `node_exporter_build_info` (`version`) |
| vendor, model | `details.vendor`, `model` | `node_dmi_info` (`system_vendor`, `product_name`; board / chassis fallbacks) – only where the exporter's `dmi` collector is on |
| CPU cores | `details.cores` | `count by (instance) (node_cpu_seconds_total{mode="idle"})` |
| memory | `details.memory_bytes` | `node_memory_MemTotal_bytes` |
| boot time | `details.boot_time` | `node_boot_time_seconds` |
| interfaces (name, MAC, state, speed) | `details.interfaces` | `node_network_info` (`device`, `address`, `operstate`) and `node_network_speed_bytes` |
| filesystems (mount, device, type, size, available) | `details.filesystems` | `node_filesystem_size_bytes` / `node_filesystem_avail_bytes`, pseudo filesystems filtered out |
| **Windows** hostname | `details.hostname` | `windows_cs_hostname` (`hostname`, else `fqdn`, `netbios`) |
| OS, build | `details.os`, `kernel` | `windows_os_info` (`product`; `version`) |
| exporter version | `details.exporter_version` | `windows_exporter_build_info` (`version`) |
| CPU cores, memory | `details.cores`, `memory_bytes` | `windows_cs_logical_processors`, `windows_cs_physical_memory_bytes` |
| boot time | `details.boot_time` | `windows_system_boot_time_timestamp`, else `windows_system_system_up_time` (the older name; also a timestamp) |
| network adapters | `details.interfaces` | `windows_net_nic_address_info` (`friendly_name` / `nic`, `address`) |
| volumes | `details.filesystems` | `windows_logical_disk_size_bytes` / `windows_logical_disk_free_bytes` (`HarddiskVolume*` filtered out) |
| **SNMP** name, description, location | `details.sysName` (also `hostname`), `sysDescr`, `sysLocation` | `sysName`, `sysDescr`, `sysLocation` (snmp_exporter's `system` module; the text is in the label of the same name) |
| **kube-state-metrics** version | `details.exporter_version` | `kube_state_metrics_build_info` (`version`) |
| cluster size | `details.cluster` (`nodes`, `nodes_ready`, `pods`, `pods_running`, `namespaces`, `deployments`) | `count(kube_node_info)`, `sum(kube_node_status_condition{condition="Ready",status="true"})`, `count(kube_pod_info)`, `sum(kube_pod_status_phase{phase="Running"})`, `count(kube_namespace_created)`, `count(kube_deployment_created)`, each `by (instance)` |
| links | `ci`, `device`, `exporter`, `state` | discovery's own matching (see Linking) |
| first seen, last seen, gone since | columns | discovery's own bookkeeping (`missing_since` is set when Prometheus stops listing the target) |

A field is stored only when the exporter reports it; a query that fails keeps what an earlier pass learned.
`details.scrape` and `details.cluster` are not copied into an auto-created CMDB item's `attributes`.

**Not gathered yet** (each needs a model or a per-target query that does not fit the bulk budget): pods and
containers (kube-state-metrics / cAdvisor inventory is unbounded and needs its own table), MySQL / PostgreSQL /
Redis / nginx server versions, SNMP interface tables, blackbox probe results (`probe_success`, certificate
expiry), Alertmanager silences and alert groups, and linking alert rules to tickets. Prometheus' own build,
runtime and TSDB status (`/api/v1/status/*`) and remote-write senders (Prometheus does not list them) are also
not read.

Everything in those two JSON columns is scraped text, which Prometheus does not bound, so discovery does:
`MAX_LABELS` (60) labels per set, `MAX_TEXT` (200) characters per label value and per string in `details`
(500 for `sysDescr`), `MAX_DETAIL_ROWS` (50) interfaces and filesystems, and a reported boot time is dropped
unless it is a usable epoch second (`MAX_EPOCH`) — the detail page renders it through
`datetime.fromtimestamp`, which raises on a wild value. A name generated for an auto-created record is cut to
that model's own `name` column (`observe.Exporter` holds 120 characters, a CI or a device 150), and both
`create_ci` and `create_device` run the model's validators — `network.models.validate_host` on `mgmt_ip`
among them — which `Model.save()` would skip, so a scraped address the reachability check could not use never
becomes a device.

**Kind detection** (`TargetKind`: node, windows, snmp, blackbox, kube_state, cadvisor, mysql, postgres, redis,
nginx, other): metric evidence wins; when two families claim an instance the job name decides between them;
with no evidence the job name, then the scrape-pool name, is matched against `KIND_HINTS` (so `windows-prod`
is windows, `blackbox_http_2xx` is blackbox, `pg-exporter` is postgres); otherwise `other`.

**Site** comes from a configurable target label (default `site`), read from `labels` then `discoveredLabels`;
a target without it is `All Sites`. A site set by hand on the Discovery page sets `site_manual` and discovery
never overwrites it.

**Linking** is tenant-scoped throughout — another tenant's records are never matched. For each target:
`prometheus_instance` equal to the instance or the bare address, else the record's own name, else its IP /
management address, and for exporters the scrape URL. An item added by hand later is linked on the next pass.

Matching is deliberately **not** scoped to the target's site: a host's Prometheus `site` label and its CMDB
site can legitimately disagree, and refusing the link there would create a duplicate item instead. The cost is
that a viewer who may see a target also sees the name of the record behind it, which may sit on another site.
The operator's own "Link to…" list *is* site-scoped, because that writes the instance back into the record.

### Settings (Administration › Integrations › Prometheus)

| Setting | Default | Effect |
|---|---|---|
| Site label | `site` | Target label the site is read from. |
| Auto-create CMDB items for new hosts | off | Each new Linux/Windows target becomes a `ConfigurationItem` (ci_type server, name = hostname or address, `ip_address` when the address is an IP, site, `prometheus_instance`, `attributes` copied from `details`, description "Discovered from Prometheus job X"). |
| Auto-create devices for SNMP targets | off | Each new SNMP target becomes a `network.Device` (name, `mgmt_ip`, `prometheus_instance`, site, tag `discovered`; vendor and model are left blank, `device_type` is the model default `switch` for an operator to correct). |
| Auto-register exporters | off | Each new target with a scrape URL becomes an `observe.Exporter` (type and category from the kind, `metrics_url` = the scrape URL). A target Prometheus gives no scrape URL for is skipped, because the Exporter inventory is built around a URL it can scrape itself. |

All three are **idempotent**: a target that is already linked is never duplicated, and a record that appears
later is linked rather than re-created.

### Job, command and buttons

* job `telemetry.discovery`, every **300 s** (registered in `telemetry/apps.py`). It never raises; a Prometheus
  failure is recorded on the `DiscoveryRun` row and logged.
* `manage.py discover_targets [--now] [--list]` runs one pass and prints the summary.
* "Discover now" (POST, admin on **All Sites**) on the Discovery page and on the Integrations page's
  Prometheus card.
* The last 50 `DiscoveryRun` rows per tenant are kept (time, counts, duration, error).

### Permissions

| Action | Needs |
|---|---|
| See the Discovery page and target details | `view`; rows are scoped with `core.access.scope_by_site`, so a user sees only their sites' targets (plus `All Sites`). |
| Add to CMDB, Create device, Link to…, Ignore, Un-ignore, Set site | `operate` **on that target's own site** (`core.access.can_on`), exactly as adding a CI or device does elsewhere. A target on `All Sites` needs the role tenant-wide, so a one-site operator cannot add it. |
| Discover now | `admin` **tenant-wide** (`core.access.tenant_wide`) — one pass reads every target in the tenant and may create records on every site, so an admin for a single site must not drive it. |

Every action is a POST with a CSRF token and writes an audit event; the discovery job itself audits nothing,
because it is a system job and not a user action.

### Alert rules

`core.prometheus.rules()` / `telemetry.discovery.rule_groups()` read `/api/v1/rules` and are shown read-only in
the **Alert & Recording Rules** section of Monitoring › Prometheus Metrics (groups, rule name, type, state,
severity label, active alert count, expression, last evaluation). Cached for 60 s; **nothing is persisted** —
the rules live in Prometheus' own configuration.

### Onboarding checklist

`administration.services.onboarding_checklist()` drives the panel at the top of Administration › Integrations:
Prometheus connected → targets discovered (N) → hosts in the CMDB (N of M) → Alertmanager webhook configured →
notification channel configured → first alert received. Each step links to the page that fixes it. Counts only.

## Monitoring pages: site scoping, refresh, alert expiry

* **Which site a host belongs to** (`telemetry.hosts.instance_sites`): the site of the CMDB item whose
  `prometheus_instance` is the host's `instance`; else the site of its Prometheus discovery record
  (`DiscoveredTarget.site`); else the site of the network device that names it. A host nothing knows, or one on
  `All Sites`, is visible to everyone. Infrastructure, host detail / metrics / metrics.json, Prometheus targets and
  Fleet (`scope_hosts`, `scope_targets`) all use this one rule. If the inventory cannot be read, a site-limited user
  sees nothing (fail closed); users who see every site are not affected.
* **Nagios** has no sites of its own: a host is shown to a site-limited user only when a CMDB item or device
  with that name, IP address or Prometheus instance sits on one of the user's sites (or on `All Sites`). Hosts and
  services no record claims are shown only to users who see every site.
* **Acknowledging an alert** needs `operate` on the alert's site (`core.access.can_on`); an alert on `All Sites`
  (or with no site) needs it tenant-wide.
* **`?refresh=1`** (cache bypass) is honoured only for users with `operate`, and never skips the 300 s series-count
  cache on Fleet; the Refresh buttons are shown to operators only.
* **Fleet alert panels** count one occurrence per alert row notified in the selected window (`fire_count` is a
  lifetime counter and is not used).
* **Stale firing alerts**: the hourly `telemetry.prune_alerts` job first resolves firing alerts that have had no
  repeat notification for `LINKEDEYE_TELEMETRY_ALERT_STALE_HOURS` (default 48, `0` = off; keep it well above
  Alertmanager's `repeat_interval`), emitting the resolve event with reason `stale` so the Service Desk incident
  moves on. A later firing notification re-opens the alert. Then resolved alerts older than the retention are
  deleted.
* **Discovery pass cost**: link lookups use one in-memory index per pass and known targets are written in one batched
  UPDATE, so an unchanged pass costs a constant number of queries rather than several per target.
* **Probes**: `checks.http_check` / `fetch_bytes` percent-encode non-ASCII path and query (and UTF-8 redirect
  targets); `resolve_allowed(host, port, timeout=None)` and `dns_check(host, timeout=None)` bound DNS by the probe
  deadline (a timeout abandons the lookup in a daemon thread).
