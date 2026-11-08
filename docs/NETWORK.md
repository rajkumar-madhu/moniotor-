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
needs no media volume. Columns: tenant, SHA-256 (unique per tenant and candidate owner, so identical files are stored once), content type,
width, height, size, source (`upload` | `fetched` | `catalog` | `datasheet`), source URL (fetched only), uploader, created,
where it was found (`origin`) and the candidate flag and owner (see below). The database also enforces the type list, the
size cap and the dimension cap.

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

## Getting real device photos

The drawn illustrations are only a fallback. The photo you want is the one printed in the vendor's datasheet, and LinkedEye
can take it from there: **Get photo from datasheet**, on the device (edit page, More menu, or the "Add real photo" link on an
illustration card), on a catalog row, and for whole models on **Devices missing a photo** (`/network/photos/`).

### Three ways in, one picker

One page, one form, three kinds of input:

1. **A datasheet PDF from your computer (the main route).** Search for "<vendor> <model> datasheet" (the page has a link that
   opens that web search in a new tab; LinkedEye fetches nothing from it), download the PDF in your own browser, and choose
   it. LinkedEye reads the first pages of the PDF locally and pulls out the embedded pictures.
2. **A product page or datasheet address.** LinkedEye fetches that one page and the pictures it names (`og:image`,
   `twitter:image`, `image_src`, JSON-LD `Product` images, then `<img>`, `<source srcset>` and lazy-load attributes).
   An address that turns out to be a PDF is read as route 1; one that is a picture becomes a single candidate.
3. **A picture address, or a picture file.** The same way the device form already worked, but shown as a candidate first.

Why the PDF route exists: some vendors refuse automated downloads. From the development sandbox Cisco and Aruba answered
403, Fortinet answered 200 and Juniper redirected. A fetch-by-URL feature alone would work for some vendors and silently
not for others. The datasheet the operator already has in their browser always works.

Every route ends in the same place: a grid of **candidates**, best first, each with its pixel size, byte size and where it
came from ("datasheet page 1", "og:image"). Click one to use it.

### Using the photo

* **For this device**: needs `operate` on the device's site.
* **For every device of the model** (checkbox "Use this photo for every Cisco Catalyst 9500-48Y4C device"): writes a
  product catalog row (vendor, any type, model), gives the photo to the model's other devices that have no photo yet
  (a device that already has its own photo is left alone) and makes new devices of that model pick it up. The catalog is
  tenant-wide, so this needs the **admin** role; an operator who posts it anyway gets a 403 and nothing changes.
* On `/network/photos/` a group is vendor + model: one datasheet covers every device of the model ("Cisco Catalyst
  9500-48Y4C, 3 devices"), and where the catalog already has a matching photo there is a one-click **Apply catalog photo to
  3 devices**. The KPI strip shows devices with a real photo (stored, or an older linked picture), devices with an
  illustration only, and models still missing. Virtual machines are left out (no hardware datasheet); devices without a
  vendor or model are listed so you can fix them first.

### Candidates

A candidate is a `DeviceImage` row flagged `is_candidate` that belongs to the person who searched (`candidate_owner`). It is
never attached to a device and not served by the normal photo route; its owner gets the bytes from
`/network/photos/candidate/<id>/` (`Cache-Control: private, no-store`, the same fixed security headers as photos). Nobody
else can view or apply it, not another operator, not an admin, not another tenant. A new search replaces your previous
candidates. They live for one hour (an expired one is already invisible and cannot be applied; the `network.photo_candidates`
job, every 15 minutes, deletes them). At most 60 candidates exist per tenant: when a search would pass that, the oldest
candidates are dropped first. Picking one turns the row into a normal photo in place (source "From a datasheet"); if the same
bytes are already a photo, the existing photo is used.

### What is read from a PDF (`network/datasheet_pdf.py`)

The PDF is untrusted input. Limits: 25 MB file; the first 6 pages; at most 40 image objects; form XObjects followed two
levels deep; each image's declared size is checked before anything is allocated (4096 px per side, 20 MB decoded) and a Flate
stream is inflated with an output limit equal to its declared size, so a small file cannot expand into a large one; a
12 second wall-clock budget checked between objects; any parser exception becomes "could not be read", never an error page;
the file is read in memory and never written to disk (the upload handler for this one view keeps the upload in memory
whatever its size). Encrypted PDFs that open with an empty password are read; others ask for an unprotected copy.

* JPEG streams (DCTDecode) pass through as the original bytes.
* Flate images in DeviceGray / DeviceRGB (also Cal and ICC-based with 1 or 3 components) at 8 bits become a PNG that
  LinkedEye encodes itself with `zlib`; PNG-predictor streams are reused as they are. A soft mask of the same size is merged
  as an alpha channel when the data uses no PNG filter bytes (otherwise the picture is kept without transparency and
  the message says so).
* Skipped and counted in the message: JPEG 2000, CCITT, JBIG2, CMYK, indexed, 1-bit images, stencil masks, TIFF predictors,
  pictures under 200 x 120 px, shapes wider than 5:1 either way (logos and banners), near-uniform pictures.
* A picture whose bytes would exceed the 2 MB photo limit cannot be shrunk (there is no imaging library), so it is skipped
  and the message says so. Save a smaller copy and upload that.
* Ranking: pictures drawn as page decoration or at logo size come last. LinkedEye reads where the page draws each picture (its
  content stream: `cm`, `q`, `Q`, `Do`, through form XObjects, bounded in operators and forms) and demotes one that reaches
  two or more page edges (a header band, a full-bleed background, a corner graphic) or covers under 1.2 % of the page (a
  vendor badge, a logo), because on a datasheet the largest picture is often a graphic, not the product. Within each group:
  earlier page first, then larger area, then closeness to a product-shot aspect ratio (1.6:1). At most 12 candidates. A chart
  can still outrank a product photo; that is why you pick. If positions cannot be read the old order applies.
* Print-oriented datasheets often store photos in CMYK, which cannot be converted without an imaging library. When that is all
  a PDF has, the message says so and tells the operator to save or screenshot the photo from the PDF viewer and upload it.

`pypdf` is a normal dependency (`requirements.txt`), not an optional extra: the main route needs it, it is pure Python with
no compiler and no Pillow, and `Dockerfile` installs `requirements.txt`. It is still imported lazily, so a deployment built
without it keeps working and the datasheet form says exactly how to install it. The minimum version is 6.4: older releases
have published denial-of-service problems with crafted streams, so keep it updated like any parser of untrusted files.

What was attacked (see `network/tests_datasheet_security.py`): a 300 MB object stream hidden in a small file, Flate bombs
declared as an ordinary picture or as a soft mask, a picture declaring 65535 x 65535 px, reference cycles, a page tree
that contains itself, absurd or negative numbers, 100,000-deep nesting, garbage after the header. Each ends in a message
within seconds; none allocates more than the declared, capped size of one picture. What the file is is decided by its bytes,
not its name: a PDF called `.png` is read as a PDF, a picture called `.pdf` as a picture, and a picture that merely contains
the text `%PDF-` is still a picture. A person without the operator role is refused before the upload is read.

Candidate writes of one tenant are serialised with a PostgreSQL advisory lock (released at commit), so the 60-per-tenant cap
and the one-row-per-person-and-picture rule hold when two requests run at once; two people choosing the same picture at the
same moment end up sharing one stored photo.

### What is read from a page, and the fetch policy (`network/datasheet_page.py`)

Every fetch is started by a person for **one address**: LinkedEye reads that page once, picks picture addresses out of the
markup with the standard library's `html.parser` and downloads those pictures (at most 12, four at a time, 20 s in total,
each at most 2 MB and validated like any photo). It does not follow links, crawl or scrape.

* Honest identity: the `User-Agent` is `LinkedEye/<version> (product photo fetch)`. It never imitates a browser and sends no
  cookies or referrer. It does not try to get past bot protection.
* Everything goes through `probes.checks.fetch_bytes`: https only, port 443, public addresses only, redirects re-checked,
  no compressed responses, a hard deadline. HTML up to 6 MB (vendor product-guide pages inline whole guides; 3 to 4 MB is normal), PDFs up to 25 MB, pictures up to 2 MB.
* Dropped from the page: SVG, GIF, ICO, AVIF, tracking pixels, sprites, spacers, and anything whose file name, directory,
  alt text, title or class looks like a logo, icon, banner, avatar or flag. Pictures whose alt text, file name or caption
  contain the device model (the whole model, or its model number) are ranked first; the page's own og:image / twitter:image
  wins a tie when it names the model too. Among equals, a front, angled or hero view comes before rear, internal, diagram,
  callout and drawing views, and squarer shots come before banner-like crops (wider than 2.6:1). News tiles, award, ebook,
  video and webinar images and rendered PDF covers are dropped. When a model was given and nothing on the page names it
  (a family landing page), the result carries a note to check the pictures. Addresses inside JSON-LD are unescaped
  (`&amp;`) so they match the same address in the markup.
* When a site answers 403, 429 or 401, shows a captcha / "checking your browser" page, or refuses every picture, the message
  is: *This site blocks automated downloads. Download the datasheet PDF yourself and upload it here.* and the upload box is
  right there; so does a redirect loop (the site sends automated clients round in circles) and a server error (the message
  says it may be a refusal). Other failures are specific: not found, no answer in time, not a web page or PDF or picture, the page or file
  is too big, the secure connection could not be verified, no usable pictures (and how many were too small or refused).

### Limits to know

* The photo is stored as is (at most 2 MB, 4096 px). There are no thumbnails, so a list page loads the full photo of each
  model once (browsers cache them for a year; devices of one model share one stored photo).
* Vector artwork and scanned PDFs have no embedded product picture to extract; use a product page or the picture itself.
* Behind nginx, allow larger bodies on the picker only: `location = /network/photos/find/ { client_max_body_size 26m; }`
  (see DEPLOYMENT.md). Without it a datasheet over the proxy's limit is rejected before LinkedEye sees it.

### Rights

LinkedEye ships **no vendor photos and no vendor addresses**, and downloads nothing on its own: every picture is one a person
on your installation uploaded or asked it to read. **You are responsible for having the right to use the images you add**;
vendors often allow product photos in their datasheets for identifying their own equipment, but check their terms.

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

Deployment: with nginx or an ingress in front, allow request bodies of about 3 MB (see DEPLOYMENT.md); the datasheet upload
page needs up to 26 MB, see "Getting real device photos".

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
