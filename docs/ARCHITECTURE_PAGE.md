# Data Center Architecture page

**Network Devices → Architecture** (`/network/architecture/`, URL name `architecture:overview`) draws the spine-leaf view of
the data centre — Internet / WAN, firewalls, core / border, spine, leaf and the server groups — **from LinkedEye's own
data**, server side, as one SVG, and keeps it live. It is owned by the `architecture` app.

What it reads:

| Source | Used for |
|---|---|
| `network.Device` | every node: name, model line, status dot, photo (the stored `DeviceImage`, else the generated illustration; a legacy hot-linked URL is never used), tooltip (type, management IP, uptime, CPU / memory when fresh, last seen) |
| `architecture.Link` | every line: two devices, a port on each, medium, speed, kind, origin |
| `architecture.Placement` | optional per-device override of tier, group, colour, order, hidden |
| `network.Interface` | oper state and utilisation of a link's ports (the rows the Prometheus job stores) |
| `dcim.RackItem` / `Rack` / `PowerFeed` | the racks each server group sits in, with U used and power |

## The model (migration `architecture/0001`)

* **Link** (`a_device`, `b_device`, `a_port`, `b_port`, `medium`, `speed_mbps`, `kind`, `origin`, `confirmed`, `note`,
  `tenant_key`). The two ends are stored in one canonical order (the lower device id is `a`; `save()` swaps ends and ports),
  so A–B and B–A are the same link; the database enforces both the order and the uniqueness of
  (tenant, a, a_port, b, b_port). A device cannot be linked to itself; both devices must belong to the link's tenant.
  Port text is cleaned (control characters removed, 60 characters at most). Two devices may share several links when
  their ports differ (LAG members, parallel uplinks). `origin` is `manual`, `lldp` or `import`; a link found by LLDP is
  created with `confirmed = false` and drawn dashed until someone confirms it.
* **Placement** (one per device): `tier`, `group`, `colour` (orange, green, blue, red, purple, teal, stone), `sort_order`,
  `hidden`. Empty fields mean "decide automatically"; saving an all-empty form removes the row.
* **Site scoping.** Neither model has a site of its own, so neither is in
  `administration.services.SITE_NAME_FIELDS` (a test pins that decision). A link follows its devices: it is drawn and
  counted only for a user who can see **both** ends (`core.access.scope_by_site` on each device), and it can be edited or
  deleted only by a user who may *operate* on both devices' sites (a global device needs a tenant-wide role). A link whose
  other end the user cannot see is a 404, never a "hidden device" placeholder. Renaming a site renames the devices and the
  links follow by foreign key.

## How the tier and group are inferred

`architecture/tiers.py::infer()` is the only place that decides, in this order:

1. **Override** — a `Placement` with a tier (and, for servers, group / colour) wins.
2. **Device type** — router → *Internet / WAN*; firewall → *Firewalls*; wireless and unknown types → *Unclassified*;
   physical server, virtual machine and load balancer → the *server* tier.
3. **Keywords** for a *switch*, looked up in the device's **tags, then role, then name**; the first source that has a keyword
   wins. A keyword is a whole word (digits may touch it: `spine01`, `dc1-tor-a`; `motor` and `encore` do not match; camelCase joins count, so `SpineA` and `DcLeaf` match while `ToR` still does):
   `spine`; `leaf`, `leaves`, `tor`; `core`, `border`, `edge`, `agg`, `aggregation`. Inside one source a conflict is
   settled **spine > leaf > core** (so `border-leaf-01` is a leaf).
4. **Graph position, last resort**, only for a switch no keyword placed: linked to a firewall or router → *core*; linked
   to two or more switches, to no server, and not mounted in a rack → *spine*.
5. **Default** — a plain switch with a rack item or at least one link is a *leaf*; with neither it is *Unclassified*.

Nodes placed by steps 4 and 5 carry a small orange **?** ("inferred") and the reason is in the tooltip and the List view;
Placement overrides always win.

**Server groups.** A server's group is, in order: the placement's group; a keyword in tags, role or name
(**storage/san/nas > db/database/sql/oracle/postgres… > app/application > web/www**); a storage-array model (per the device
illustration); the name of the rack it is mounted in; "Other servers". Default colours follow the reference drawing: web
orange, application green, database blue, storage red, everything else stone. Group names compare case-insensitively; one
colour per group (the first device by name with an explicit colour decides).

To override: open a device in **List view → Change placement** (or `/network/architecture/devices/<id>/placement/`).
"Hide" leaves the device (and its links) out of the drawing and out of every count.

## The drawing

* Tiers are horizontal bands, top to bottom; devices inside a tier are ordered by the barycentre of their neighbours
  (four sweeps, ties by name) to cut crossings; the drawing never depends on the order the database returns rows in, and
  a placement's sort order only decides between devices whose neighbours give no better position. Spine–leaf is drawn as a full mesh of curves, each link on its own
  attachment slot. Server groups are rounded cards with the group's colour, device count, up / down counts, tiles for the
  servers, and the racks (linked to the DCIM rack page, with U used and power); between a leaf and a group the real links are
  drawn as parallel strokes, **one stroke per link** (the count is the real link count).
* **Colours.** Device dot: green up, red down, grey unknown, hollow ring "monitoring disabled" (from `Device.status` and
  `enabled`). Link: both ends up (or both ports reporting up) = healthy; an end down, or a port oper-down = red; otherwise
  grey (unknown); dashed = found by LLDP, not confirmed. The *Fabric / Health / Utilisation* switch (`?lc=`) chooses how
  healthy links are coloured: blue and group colours (like the reference), green, or by the busiest end's utilisation
  (green / orange / red, thicker when busier). Utilisation is the stored interface figure and is ignored when it is older
  than 15 minutes or the device's monitoring is disabled.
* **Interaction.** Hover or focus a device: its links and neighbours stay lit and the rest dims, a tooltip shows its facts.
  Click a link: side panel with both endpoints, ports, medium, speed, state, utilisation, origin, last change and an edit
  link. Click a device: its detail page.
* **Live.** The page polls `/network/architecture/live/` every 30 s (only while the tab is visible, with exponential
  back-off after errors) and patches dots, link colours and the summary strip without a reload. `?live=0` starts it off.
  The response is `{"generated_at", "prometheus": {"state", "message"}, "devices": {id: {"status", "state", "state_label"}},
  "links": {id: {"state", "util", "tone", "oper_a", "oper_b", "rate_a", "rate_b"}}, "summary": {...}}`, scoped to the user.
* **Summary strip.** Devices by tier, links up / down / unknown, devices down, an oversubscription hint (leaf downlink Mbps
  ÷ uplink Mbps over the leaves whose links have a speed, with the worst leaf) and the number of unclassified devices.
* **Prometheus.** When it is not configured, or does not answer a trivial query (remembered for 20 s), a banner says so; the
  page still renders and link states come from device status only.
* **Accessibility.** Every node is a link with an `aria-label` (name, type, state), the SVG has `<title>` and `<desc>`, focus
  shows a ring, and the **List view** tab shows the same tiers as tables. At phone width the SVG scrolls inside its own box.
* **Download SVG** saves a standalone file with the theme colours resolved and the photos embedded.
* Filters: `?site=`, `?env=`; state is kept in the URL (`view`, `lc`, `live`, `expand`).

## Limits

* A tier of more than **24 devices** is drawn as compact chips (a note says so; nothing is dropped) and *Open* draws it at
  full size (`?expand=leaf`). More than **300** links between two tiers are drawn only for the hovered device, with a ribbon
  showing the count for the rest. A server group shows at most five rows of tiles with a "+N more" tile (`?expand=<group>`; an opened group of more than 30 servers takes a full-width row);
  more than 16 links in one leaf-to-group bundle are counted but not all drawn. Server-to-server links are counted, not drawn.
* The layout is computed per request: under 200 ms for 8 spines × 32 leaves and under 1 s for 16 × 48 (tests assert it).
  The page runs one query per model (devices, placements, rack items and their racks' items / feeds, links, interfaces).
* More than four server groups wrap into rows; bundles to a lower row pass behind the cards above it.
* The cloud and the lines from it to the WAN routers are symbolic.

## Links: list, forms, CSV

**Links** (`/network/architecture/links/`) lists, searches and filters the links (table: A device / port, B device / port,
medium, speed, kind, origin, state, utilisation), with add / edit / delete and bulk *Confirm* / *Delete*. Viewing needs the
view permission; adding, editing, deleting, importing need *operate* on both devices' sites. Every mutation is POST + CSRF and
is audited (`architecture.link`, `architecture.placement`, `architecture.example`).

**CSV** (`links/export.csv`, `links/import/`): UTF-8, at most 5,000 rows and 2 MB, header row required.

```
a_device,a_port,b_device,b_port,medium,speed_mbps,kind,note
dc1-spine-01,Eth1/1,dc1-leaf-01,Eth1/49,fibre,100000,uplink,
```

`a_device` and `b_device` are required (exact device names you can see); `medium` is fibre, copper, dac, lag or other
(default fibre); `speed_mbps` is a whole number 1–10,000,000; `kind` ≤ 30 and `note` ≤ 300 characters; ports ≤ 60. Unknown
columns, control characters, unknown devices, devices you cannot manage, self-links and duplicates inside the file are
row-level errors (the first 50 are listed) and **nothing is imported unless every row is valid**. Upload shows a preview
(create / update / unchanged per row); a second click commits, re-validating first. A link that already exists is an error
unless *Update links that already exist* is ticked. Exported cells that start with `=`, `+`, `-`, `@`, tab or CR get a
leading quote so spreadsheets do not run them; the importer removes that quote again, so an export re-imports unchanged.

## LLDP suggestions

**Find links (LLDP)** reads, with five bulk queries through `core.prometheus`, snmp_exporter's standard LLDP-MIB series:
`lldpRemSysName`, `lldpRemPortId`, `lldpRemPortDesc` (labelled by `instance`, `lldpRemLocalPortNum`, `lldpRemIndex`),
`lldpLocPortDesc` (`lldpLocPortNum`) and `ifName` (`ifIndex`). Requirements: the `lldp` module in the snmp_exporter config
and each device's *Prometheus instance* set to the scrape target's instance label (its name, management IP or instance). A
neighbour's system name is matched case-insensitively, short or fully qualified, against device name, management host and
Prometheus instance; a name that matches none, or more than one, is only counted. Suggestions are **listed for review and
created only when a user ticks them** (the server recomputes them on accept and ignores anything it did not offer); accepted
links get `origin = lldp` and `confirmed = false` (dashed until confirmed). Remote strings are length-limited, stripped of
control characters and escaped on output. A missing metric or a Prometheus error shows a message, never a stack trace.

## Example fabric

`manage.py seed_architecture [--site NAME]` (and the empty page's **Load example fabric** button, which runs the same code
for operators) creates 2 edge routers, 2 firewalls, 2 core / border switches, 4 spines, 4 leaves, 19 servers in Web,
Application, Database and Storage groups (Cisco Nexus / Catalyst, Fortinet, Dell PowerEdge / PowerVault), 51 links
(full spine–leaf and core–spine meshes, firewall ↔ core, router ↔ firewall, leaf → server bundles) and a DCIM room with four
racks. Everything is tagged `example, arch-example`, on documentation addresses (RFC 5737), so the probe refuses them and they
stay "Unknown": no status or metric is invented. Idempotent; a real device with the same name is never touched and gets no links or rack items from the example. When a user loads it (the button), example devices an earlier load put on another site are left where they are.
`manage.py seed_architecture --remove` deletes only what it created.
