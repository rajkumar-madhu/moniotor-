# UI: shell, dashboard and shared components

The console keeps its warm paper + coral theme (light and dark, tokens at the top of `core/static/linkedeye/app.css`). This
page records what the 2026-11 redesign changed, how the sidebar and dashboard work, and how to add to them.

## Before / after

| Area | Before | After |
| --- | --- | --- |
| Sidebar structure | 27 top-level items in one flat list, several single-link groups, `Overview`, `Dashboard`, `Kubernetes` and `Discovery` each twice | six labelled, collapsible sections (Operate, Observe, Infrastructure, Assets, Insights, Platform); 98 links (97 old plus the new Overview page), every old URL name present once; ambiguous names renamed (`Kubernetes (live)` / `Kubernetes`, `Target Discovery` / `Network Discovery`, `Compute Overview`, ...) |
| Sidebar behaviour | hover expanded the whole rail; groups closed by default | filter box (`/`), pins (star, top of the menu), remembered section and group state, keyboard navigation, badges, icon rail with a flyout that lists the group's links, mobile drawer with a focus trap |
| Dashboard | heat-map table, a mostly empty "Prod env / Legacy backend" box, four figures, donuts, BOD/EOD strip, table | status line (operational / N need attention / critical) with scope, time and a 15 min / 1 h / 6 h / 24 h range; six figure cards with sparklines; Needs attention queue beside recent events; fleet health; compact heat-map; service health; BOD/EOD; empty-install checklist |
| Dashboard cost | 39 queries (demo database, admin) | 36 queries; flat in the amount of data; no Prometheus and no backend call while rendering |
| Type scale | page titles 18px serif, 22px sans and 28px depending on the page; panel titles 15 / 16.5 / 18px | tokens `--fs-title` 22px, `--fs-h2` 15px, `--fs-body` 14px, `--fs-small` 12.5px, `--fs-micro` 11px |
| Tables | zebra and hover only | tables longer than 15 rows scroll inside their card with a sticky header (`.table-wrap` + `:has()`), no markup change |
| Feedback | `.empty`, `.messages` | plus `.notice.ok/warn/crit`, `.state` / `.state.is-error`, `.skeleton`, `ul.errorlist`, `.has-error` fields |
| Accessibility | contrast unchecked | `core/test_theme_contrast.py` asserts 4.5:1 for chips, badges, heat cells, buttons, body text and the sidebar in both themes; focus rings on inputs and buttons; reduced-motion honoured globally |
| Print | rail and bar hidden | plus toolbars, actions, tabs and pagers hidden, cards do not split, long tables print in full with a repeating header |

## Sidebar

`core/nav.py` is the single source of truth. A top-level entry is a link (`"url"`) or a group (`"children"`); the optional
`"section"` files it under a section of `SECTIONS` and `"badge"` (`incidents`, `alerts`, `checks`) puts a count on it.

```python
{"section": "observe", "label": "Fleet", "icon": "radar", "url": "telemetry:fleet"},
{"section": "observe", "label": "Checks", "icon": "check-circle", "badge": "checks", "children": [
    {"label": "Uptime Checks", "url": "tcpcheck:uptime"}, ...]},
```

To add a page: add the entry in the right section (icons come from `core/templatetags/le.py` `ICONS`), keep labels unique within a
section (a test enforces it), and make sure the URL name resolves. A name that does not resolve yet opens the "not built yet"
page, so an entry can land before its view. `core/test_nav.py` holds the list of URL names of the old menu and fails if one
disappears or appears twice.

`core.context_processors.build_nav(path, badges)` computes the active link (the most specific href that prefixes the path), the
open group and the active section with no queries. `nav` (flat list, as before) and `nav_sections` are in every template
context. Badge counts come from `core.overview.nav_badges` (up to four COUNT queries, cached 30 s per user, capped at `99+`); the
dashboard hands over the numbers it computed anyway (`request.le_badges`), so it costs nothing extra there.

`core/static/linkedeye/menu.js` adds the behaviour; without it every section is open and every link reachable.

* Sections and groups remember their state in `localStorage` (`linkedeye-nav-sections`, `linkedeye-nav-groups`); the section
  and the group holding the current page are always open. A small script in `<head>` re-applies collapsed sections before first
  paint. Every storage access is in `try/catch`.
* Filter (`/` focuses it, `Escape` clears): matches a link's label or its group's label, opens the groups with matches while
  filtering and restores them after. Pins (`linkedeye-nav-pins`) are rebuilt from the stars as clones of the rendered links.
* Keyboard: Up/Down/Home/End move, Right opens a group or enters it, Left closes or goes to the parent, Enter/Space toggle,
  Escape closes a flyout. Phone drawer: Tab stays inside, Escape closes and returns focus to the menu button.
* Icon rail (menu button unpinned): no expand-on-hover; hovering or focusing an icon opens a flyout with the group's links
  (or the single link's name); section headings become dividers; badges become dots.

Browser check (Playwright, 29 assertions on sections, memory, filter, pins, keyboard, rail, flyout, drawer):
`LE_BASE=... LE_USER=... LE_PASS=... node docs/ui-checks/nav-check.js`.

## Dashboard (`core/dashboard.py`, `core/templates/core/dashboard.html`, `pages/dashboard.css|js`)

`dashboard.build(request)` returns plain dicts (banner, `kpis`, `problems`, `events`, `fleet`, heat-map rows, BOD/EOD, the
get-started checklist); the template renders them and `GET /dashboard/data/` (`core.views.dashboard_data`) returns the same
figures as JSON for the page to patch (`textContent` only) every 60 s while the tab is visible, plus sparkline history. Right
after it renders the page asks for the history alone (`?only=spark`: a few cached queries, not a second build of every figure).

* Scope: the user's sites (`core.access`), narrowed by the site and client pickers (`overview.focus_sites`); a site the user cannot see
  shows nothing. Problems, KPIs, events and BOD/EOD all follow the picker.
* Range (`?range=15m|1h|6h|24h`, default 1 h) drives the sparklines only. History comes from the databases LinkedEye keeps:
  open incidents and firing alerts from their start/end times, check health from `UrlResult` / `TcpResult` (one aggregate query each),
  cached 30 s per user, range and picker. A figure without history (needs attention, hosts, SLA) has no sparkline.
* Fleet panel and the Hosts card read the caches the Fleet page fills (`telemetry:hosts:v1`, targets, OS); when they are empty the
  panel says "Open Fleet". Rendering never calls Prometheus. The panel also links to the Overview charts (`telemetry:overview`)
  when Prometheus is connected.
* Needs attention: severity chip, source icon, name, site, since, and Acknowledge for firing alerts the user may operate on
  (the existing `telemetry:alert_ack`).
* Legacy backend: a muted footer line unless `core.dashboard.legacy_backend_configured()`; then a compact card that loads
  `/api/backend-summary/` (unchanged, cached) on its own. The CMDB heat-map page keeps the original card.
* Empty install: tenant administrators see "Get started" (site, onboarding, Prometheus steps from
  `administration.onboarding_checklist`, inventory import) while nothing is monitored.

## Shared components

Tokens only, both themes. Page header: `.crumbs` + `.head` (`.glyph`, `h1`, `p`, `.actions`). Cards: `.panel` + `.panel-h`.
Tables: `.table-wrap > table`. Chips and badges: `.chip.*`, `{% badge %}`. Notices: `.notice.*`. Empty / error / loading:
`.empty`, `.state`, `.state.is-error`, `.skeleton`. Forms: `.form-grid > .field`, `ul.errorlist`, `.field.has-error`.
Screen-reader text: `.sr`.
