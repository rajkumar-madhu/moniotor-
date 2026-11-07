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
| Permission check | `from core.access import can, require, visible_sites, scope_by_site, role_for`. Perms: `view`, `operate` (run, acknowledge, add/edit records), `configure` (same rank as operate), `admin` (delete, settings, users). Decorate views with `@require("operate")`; in templates use `{% if perms_operate %}` – the base context provides `le_can` (see below). |
| Site scoping | Records store the site **name** in a `site` CharField (`"All Sites"` = global). Filter querysets with `scope_by_site(qs, request.user, field="site")`. Sites live in `core.models.Site` (name, code, region, timezone, criticality). |
| Audit | `from core.audit import audit`; call `audit(request, "create"|"update"|"delete"|"run"|..., obj, **details)` after every mutation. |
| Events | `from core.events import emit, event_emitted`; `emit(source="<app>", title=..., severity="critical"|"warning"|"info"|"ok", message=..., site=..., obj=..., fingerprint="<app>:<pk>", **data)` on every state change. Listen with `@receiver(event_emitted)` (args: `sender, event`). Never raise from a listener. |
| Periodic jobs | `from core.jobs import register`; in `AppConfig.ready()` call `register("<app>.<job>", interval_seconds, func)`; `manage.py run_probes` runs them. Jobs must be idempotent, bounded (timeouts) and must not raise. |
| Prometheus | `from core import prometheus`; `prometheus.configured()`, `query(promql)`, `query_range(promql, seconds, step)`, `targets()`, `alerts()`; errors raise `prometheus.PrometheusError` (show its message, never a stack trace). |
| Network probes | `from probes import checks`; `checks.http_check(url, ...)`, `checks.dns_check(host)`, `checks.tls_check(host, port)`, `checks.fetch_bytes(url, max_bytes, ...)` (https-only, size and time limited download of an asset), `checks.resolve_allowed(host, port)` (SSRF policy), `checks.check_address(ip)`. Any new socket code must go through `resolve_allowed` / `check_address`. |
| Site health overview | Import lazily inside the view: `from core import overview`. `overview.site_names(user)` (visible site names), `overview.site_matrix(user, selected_site="", names=None)` (per-site / per-kind counts; an out-of-scope `selected_site` returns no rows). Set `request.le_site_names = names` so the base context reuses them. Render with `{% include "core/_legacy_heatmap.html" %}` + `linkedeye/pages/site-heatmap.css`. |
| Legacy backend summary | `{% include "core/_backend_health.html" %}` + `linkedeye/pages/backend-health.css` / `backend-health.js`; the page fetches `/api/backend-summary/` (`core.backend_summary.summary`) asynchronously, so never call it in a page view. |
| Notifications | Automatic for critical/warning events (core/notify.py); nothing to do. |
| Tenant | `settings.LINKEDEYE_TENANT`; models carry `tenant_key` where data could belong to more than one customer. |
| Ops Agent | `agent` at `/agent/`. Threads are per-user. Tools call `core.overview` and other apps lazily and are always `scope_by_site`. The model key is `LINKEDEYE_AGENT_API_KEY` (server-side only). |

`base.html` context: `le_can` is not provided yet – use `core.access.can(request.user, "operate")` in the view
and pass a boolean (e.g. `can_operate`) to the template.

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

## Cross-module interfaces

Modules may depend on each other only through these names, imported lazily inside functions
(`from django.apps import apps; M = apps.get_model("servicedesk", "Ticket")`) and guarded so the caller still
works if the other module is absent.

| Provider | Interface |
|---|---|
| servicedesk | `servicedesk.services.open_incident(title, description="", priority="P3", site="", source="", fingerprint="", ci_name="") -> Ticket` (dedupes open incidents by fingerprint); `servicedesk.services.resolve_by_fingerprint(fingerprint, note="")`. Model `Ticket` (kind: incident/problem/change/request; number like INC-1287; status; priority P1-P4; site). |
| cmdb | Model `ConfigurationItem` (name, ci_type, site, status, environment, owner, business_domain, attributes JSON). `cmdb.services.find_ci(name)`. |
| network | Model `Device` (name, device_type: switch/router/firewall/load_balancer/wireless/physical_server/virtual_machine, vendor, model, mgmt_ip, site, status: up/down/unknown, last_seen, `image` FK to a stored photo). Photos: `network.images.store_image(bytes, source=...)` is the only way bytes enter `DeviceImage` (validated by `network.imaging.inspect_image`); `network.images.fetch_and_store(url)` downloads through `probes.checks.fetch_bytes`. Device card: `network.live.card_data(devices)` and the partial `network/_device_card.html` (see docs/NETWORK.md). |
| tcpcheck | Model `TcpCheck` (name, host, port, site, last_status: online/down/unknown, last_checked_at, last_response_ms). |
| automation | `automation.services.trigger_stackstorm(trigger, payload) -> Execution`. |
| urlchecker / domainmonitor | Existing models `UrlCheck`, `UrlResult`, `Domain`, `DomainCheck` (read-only for other modules). |

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
