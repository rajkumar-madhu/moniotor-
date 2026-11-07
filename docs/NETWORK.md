# Network devices: photos and the device card

## Photos

Every device can show its real product photo next to its live data. LinkedEye **does not bundle any vendor photos** and
ships no catalog rows: the mechanism is built in, the images are yours. Nothing is guessed, and the product never loads a
picture from a vendor's site while you browse.

### How a photo gets onto a device

On the device form (Photo section) use exactly one of:

1. **Upload a file**: PNG, JPEG or WebP, at most 2 MB and 4096 x 4096 px.
2. **Fetch from a web address**: the server downloads the picture once, now, and stores its own copy. The original can
   disappear afterwards. The address is recorded without any query string or credentials and is never shown to viewers.
3. **Pick from the product catalog**: a device with no photo that is new, or whose vendor, model or type just changed,
   also gets the best matching catalog photo automatically (exact model, then prefix, then substring, then the typed
   vendor default, then the vendor default). An existing photo is never replaced behind your back.

Who may do this: whoever may edit the device (operator on the device's site). The upload or download happens only after
that check. Viewers and operators of other sites cannot add photos, and the form needs the CSRF token like every form.

### What is stored, and how it is checked

`network.DeviceImage` keeps the bytes in the database (a `BinaryField`, max 2 MB), so it works with several replicas and
needs no media volume. Columns: tenant, SHA-256 (unique per tenant, so identical files are stored once), content type,
width, height, size, source (`upload` | `fetched` | `catalog`), source URL (fetched only), uploader, created. The database
also enforces the type list, the size cap and the dimension cap.

The bytes are untrusted. `network/imaging.py` decides the type from the magic bytes, reads the dimensions from the header
the decoder would read, and walks the whole container (PNG chunk CRCs, JPEG segments, WebP RIFF chunks). Refused: SVG, HTML,
GIF, BMP and every other format, animated WebP, a declared type or file name that disagrees with the bytes, files larger
than 2 MB or 4096 x 4096 px, truncated files, and polyglots (anything appended after the image, a second image, a bad
checksum). A library of 2000 photos per tenant is the upper bound.

### Downloads (fetch from URL, CSV import)

All outbound fetches go through `probes.checks.fetch_bytes`, the same SSRF policy as the URL checker: https only, port 443
only, no `user:password@` URLs, every redirect re-validated, private / loopback / link-local / metadata ranges refused
(unless the operator lists a private range in `LINKEDEYE_PROBE_ALLOWED_PRIVATE_CIDRS`), no compressed responses, body
limited to 2 MB, 10 s overall deadline (including a peer that trickles bytes). The response's Content-Type is only an early
filter; the bytes are validated as above.
The deadline also covers the TLS handshake. A refused or unresolvable host name gives one generic message, with no
resolved address in it, so the form cannot be used to learn which internal names exist or where they point.

### Serving

`GET /network/images/<sha256>.<png|jpg|webp>` requires a signed-in user. A user receives an image only if it is attached to a
device they may see (site scoping) or to a catalog row (tenant-wide); anything else, a wrong extension or an unknown hash is
a plain 404. Headers are fixed from the stored row: `Content-Type`, `X-Content-Type-Options: nosniff`,
`Content-Security-Policy: sandbox; default-src 'none'`, `Cache-Control: private, max-age=31536000, immutable`, a strong
`ETag` (the hash), `Content-Disposition: inline; filename="device-photo.<ext>"`; `If-None-Match` gets a 304 (after the same
access check). Request values never influence the type or the name.

Older rows that only have a hotlinked `product_image_url` still display (with `referrerpolicy="no-referrer"`) until you store
a copy. A strict CSP that blocks third-party images will block those, not the stored photos.

### Fallback illustrations

A device without a photo shows an original line drawing in the product palette (router, switch, firewall, access point,
server, storage, UPS, generic). It sits in a dashed frame with an "Illustration" label so it is never mistaken for a photo.
A UPS or storage array is recognised from the role, model, name or tags; otherwise the device type decides.

## Product catalog (`/network/catalog/`, admin on every site)

List, add, edit, delete and export rows: vendor, device type (blank = any), model match (blank = vendor default), datasheet
URL, notes, and the stored photo. Older link-only rows show "Link only" with a **Store a copy** button.

### CSV import

Header row required, any column order, UTF-8, comma / semicolon / tab:

```
vendor,device_type,model_key,image_url,product_page_url
Arista,switch,DCS-7050,https://example.com/photos/7050.png,https://example.com/7050-datasheet
```

* `vendor` and `image_url` are required; `device_type` is one of `switch, router, firewall, load_balancer, wireless,
  physical_server, virtual_machine` (or its label) or empty; an empty `model_key` makes a vendor default.
* `image_url` must be https; it is fetched and stored as above. At most 50 rows and 256 KB per import; downloads run 4 at a
  time inside a 40 s budget, rows not finished are reported as "not attempted" and can be imported again.
* A row matching an existing vendor + type + model (case-insensitive) replaces its photo; an identical one is "unchanged".
* **Dry run** downloads and validates every photo and reports what would happen, but saves nothing.
* The page shows a per-row result (created / updated / unchanged / not attempted / error with the reason).
* Export writes the same columns plus notes, image hash and size, with spreadsheet formulas neutralised.

Deployment: with nginx or an ingress in front, allow request bodies of about 3 MB (see DEPLOYMENT.md).

## The device card

`network/_device_card.html` is the one component used on the device header (hero), the device list (Cards view and table
thumbnails) and the compute server rows. It shows the photo (or illustration), the live status pill, uptime and four figures:
CPU, memory, interface traffic (in / out) and temperature, each with a sparkline built from the samples seen since the page
opened.

Where the numbers come from (`network.live.card_data`):

* **Servers and VMs** (`physical_server`, `virtual_machine` with a Prometheus instance): node_exporter, live, one batched
  query set per page. Temperature is `node_hwmon_temp_celsius` (hottest sensor) or `node_thermal_zone_temp`.
* **Network gear**: what the SNMP job stores with the interface rates (`Device.cpu_pct`, `memory_pct`, `temperature_c`;
  traffic is the sum of the physical ports, logical interfaces excluded). CPU comes from `hrProcessorLoad`,
  `cpmCPUTotal5minRev`, `jnxOperatingCPU` or `fgSysCpuUsage`; memory from `hrStorageUsed/Size` (RAM), `ciscoMemoryPoolUsed/Free`,
  `jnxOperatingBuffer` or `fgSysMemUsage`; temperature from `entPhySensorValue` (celsius), `jnxOperatingTemp` or
  `ciscoEnvMonTemperatureStatusValue`. Your snmp_exporter module must expose those series; otherwise the figure is "No data".

A figure that is not collected, or whose stored value is older than 15 minutes, reads **No data** and has no line: never 0.
A genuine 0 is shown as 0. Updates: the detail page reuses the existing live endpoint (`data-live="card_cpu"` ...);
list pages poll `GET /network/devices/cards-live/?ids=...` (at most 60 ids, site-scoped) every 30 s while the tab is visible.
