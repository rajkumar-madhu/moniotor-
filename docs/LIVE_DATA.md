# Live data only: what is real, what was fabricated, and how example data is removed

A live LinkedEye shows what your monitors, devices and records report, and nothing else. A page with nothing connected
says what to connect; a metric nobody reports is "not reported", never a made-up number.

This page is the audit of every page and URL for fabricated content, the fixes, and the example-data lifecycle.

## 1. Audit

Method: every sidebar entry and every URL in `linkedeye/urls.py` was opened as an administrator and as an operator on an
empty database (no records, no Prometheus) and again on a database holding only real records without metrics, and the
rendered text was searched for sample, example, demo, synthetic and `seed_` wording; the templates, view code and static
scripts were searched for hard-coded values. `core/test_examples.py` repeats this on every run.

| Page / URL | What was wrong | Fix |
|---|---|---|
| `/legacy/heatmap/`, `/legacy/exporters/`, `/legacy/incidents/`, `/legacy/fg-01/` | The original design mock-ups. Every number (sessions, threats blocked, incident ids, exporter lists, log lines) was generated from a fixed random seed in `core/samples/*`. They carried a "Sample data" pill but were reachable on any install. | Answer **404** unless `LINKEDEYE_SHOW_SAMPLE_PAGES=true` (default false). The URL names (`heatmap`, `exporters`, `incidents`, `firewall`) still reverse, so no reference breaks; nothing links to them unless the flag is on. The real pages are Monitoring > Heatmap, All Exporters, Service Desk > Incidents and Network Devices > Firewalls. |
| `/welcome/` (public landing page) | The hero card showed "Monitored devices 3,987", "Active incidents 3", "Availability (7d) 99.97%" under the heading "live". Hard-coded, visible to anyone. | Replaced by facts that are true of every install (metrics from your Prometheus, per-site roles, audited changes). "live" removed from the label; the picture is captioned "Product screenshot". |
| `/soon/<slug>/` (sidebar items without a page) | Listed the four sample pages as "layouts with sample data". | Lists the real pages; the sample pages appear only with the flag. It states plainly that nothing is shown because nothing is connected. |
| Empty states of CMDB (items, heatmap, topology), Compute, IPAM, Data Centre, APM, Server Hardware, Service Catalog, Automation (runbooks, templates, rules), BOD / EOD (status, checklist) | Told the visitor to run `manage.py seed_*` "for a demo". | Removed. Each empty state names what to add or connect and links to the page that does it. |
| BOD / EOD > Holidays | A permanent "Example Entries" tile that pointed at `seed_bodeod`. | Shown only while example holidays exist ("Example data: verify or remove them"). |
| Architecture overview | A "Load example fabric" button (19 servers, 12 switches ...) on every empty install. | Hidden unless `LINKEDEYE_SHOW_SAMPLE_PAGES=true`; the POST endpoint answers 404 too while the setting is off. |
| Records created by `manage.py seed_*` | Example rows (devices, tickets, CMDB items, racks, subnets ...) look like real records. They were marked, but nothing found or removed them across apps. | `manage.py remove_examples`, the Administration "Demo data" card, the admin banner and the go-live warning (section 3). |

Checked and found to read real data only (nothing to fix): Dashboard (site heat-map, market-day strip, desk summary, events),
Monitoring > Heatmap, Fleet, Infrastructure, Discovery, Nagios, Prometheus, Alert Manager, Metrics Explorer, Telnet / Uptime
checks, all Network Devices lists and the device pages (Ports, Hardware, VM tabs), Compute, Hypervisor, Kubernetes, IPAM,
Data Centre, APM, Server Hardware, Domains, URL Checker, Templates, Exporters, Logs & Events, BOD / EOD (the trading-hours
bands are configuration, not measurements), Automation, Analytics, Reports, Administration, Onboarding, Service Desk, search.
Without a Prometheus (or an exporter, a BMC, a vCenter) these pages show "not reported", "Not connected" or an empty state.

Known and intentional:

* The landing page screenshot (`Monitors.png`) is a marketing picture of the product, captioned as such.
* A record that is example data keeps its "Example data" chip / pill until it is removed; that label is the honest part.
* Events, check results and audit entries already recorded **about** an example record: events about a removed record are
  deleted with it (see below); check history and the audit log are kept (the audit trail is never rewritten).
* `seed_bodeod` also creates the BOD / EOD / ADP modes. `--remove` keeps the modes (they are configuration you will want);
  it removes the example holidays and checklist items.

## 2. Sample pages flag

`LINKEDEYE_SHOW_SAMPLE_PAGES` (default `false`): serve the four `/legacy/*` design mock-ups and show the architecture "Load
example fabric" button. Meant for design reviews and screenshots, never for a live console. Read at request time.

## 3. Example data lifecycle

Example records exist only if somebody ran a `seed_*` command (or pressed the architecture loader). Each command marks its
rows (an `example` tag, an `[example]` / `Example:` description, an `example` label, a `seed_automation` creator ...). The
registry in `core/examples.py` knows all of them: what each area counts, and how it is removed.

| Area key | Created by | Counted and removed |
|---|---|---|
| `monitoring` | `seed_examples`, `seed_domainmonitor` | the seed URL checks (tag `example,`) and domains (description `Example:`) |
| `administration` | `seed_administration`, `seed_servicedesk` agents | example sites (kept when a real user is a member), `example.*` users with no usable password (an agent who was given a password is kept) |
| `servicedesk` | `seed_servicedesk` | tickets (fingerprint `example:`), catalog items (the seed names, description starting `Example:`) |
| `cmdb` | `seed_cmdb` | CIs (`attributes.example`), software, licenses (`Example:`) |
| `network` | `seed_network`, `seed_ports` | the seed devices tagged `example`, and their interfaces |
| `compute` | `seed_compute` | seed servers and VMs tagged `example`, clusters tagged `example` |
| `hypervisor` | `seed_hypervisor` | the synthetic vCenter source (`[example]` description) and its VM device |
| `hardware` | `seed_hardware` | the example server (and its BMC readings) |
| `architecture` | `seed_architecture`, the page's loader | devices tagged `arch-example`, the example racks |
| `dcim` | `seed_dcim` | in the example rooms (description `[example]`): the seed's racks, items, feeds and cooling units that still carry `[example]` in their notes. A rack that holds an item or feed of yours, and a room that still holds a rack or cooling unit of yours, are kept (only their example contents go); your own records there are never touched |
| `ipam` | `seed_ipam` | the exact seed VLANs, subnets, addresses (seed description) and zones |
| `apm` | `seed_apm` | the example source and all of its data |
| `automation` | `seed_automation` | runbooks, rules, templates created by `seed_automation` with the seed names |
| `bodeod` | `seed_bodeod` | holidays with source `example`, checklist items with the seed's mode and name whose instructions still start `Example` (an edited or self-written item is kept) |
| `telemetry` | `seed_telemetry` | alerts labelled `example=true`, shared saved queries `Example: ...` |
| `observe` | `seed_observe` | the seed exporters (seed description) and log lines with `attributes.example` |
| `tcpcheck` | `seed_tcpcheck` | the seed checks that are still tagged `example` |

Safety rules: only the current tenant; a real record that reuses an example name but lacks the mark is never touched (a
runbook someone wrote under the same title, a vCenter source an operator adopted, a TCP check retagged `prod`); each area is
removed in its own transaction (if one fails its rows stay, the others still go, and the failure is reported and audited);
stream events about a removed record go with it.

### Command line

```bash
python manage.py remove_examples                 # dry run (default): counts per area, changes nothing
python manage.py remove_examples --yes           # remove everything, audited
python manage.py remove_examples --only cmdb,network --yes
```

The older seed commands that had no way back gained one: `seed_examples --remove`, `seed_administration --remove`,
`seed_observe --remove`. `seed_hypervisor`, `seed_automation` and `seed_tcpcheck` now remove only rows that still carry
their mark.

### Administration > System health > Demo data

A card (administrators on All sites only) with the counts per area, each linked to its page, and a confirm-protected
**Remove all example data** button: POST, CSRF, a confirmation tick plus a browser confirmation, audited as
`delete core.exampledata` with the per-area counts. A site-scoped administrator cannot reach it.

### Banner

While example data exists, administrators see a slim "Showing example data. Remove it under Administration" notice at the
top of every page; **Dismiss for this session** hides it until the next sign-in. The check runs at sign-in and whenever the
Demo data card is opened and is stored as one integer in the session, so page views make no extra database query. Nobody
else ever sees it.

### Go-live check

`manage.py go_live_check` (and the Go-live readiness card) warns "Example data: N example records (...)" with the fix while
any example data exists. It is a warning, not a failure: a trial instance with examples is a legitimate state.

## 4. Before you go live

1. `python manage.py remove_examples` and read the list.
2. `python manage.py remove_examples --yes`.
3. `python manage.py go_live_check` shows "Example data PASS".
4. Leave `LINKEDEYE_SHOW_SAMPLE_PAGES` unset.
