# Hypervisor (vCenter), read-only

The `hypervisor` app fills the virtual-machine device page (Overview, Storage, Network, Snapshots, Guest OS, Alerts) with what
vCenter says about the VM, and adds a datastore page, a page for VMs LinkedEye could not match to a device, and a list of control
requests. **It is read-only.** LinkedEye logs in with a read-only vCenter account, only reads, and never changes anything in vCenter or
on a host. The buttons "Power off", "Take snapshot", "Migrate" and the rest **record a request**; an administrator approves or rejects
it inside LinkedEye, and approving shows the manual step for a person to carry out. Nothing is sent.

## What it reads

The vSphere Automation REST API (vCenter 7.0 or later, `/api/...`), standard library only, no new required dependency:

| Request | Used for |
|---|---|
| `POST /api/session` (Basic auth), `DELETE /api/session` | the only two non-GET requests, the session handshake |
| `GET /api/appliance/system/version` | vCenter version (optional) |
| `GET /api/vcenter/host`, `/cluster`, `/datastore`, `/datastore/{id}` | hosts, clusters (HA / DRS flags), datastores (type, capacity, free, accessible, thin provisioning) |
| `GET /api/vcenter/vm`, also with `?hosts=`, `?clusters=`, `?datastores=` | VM list and where each VM runs (the REST list has no host, so placement comes from the filtered lists) |
| `GET /api/vcenter/vm/{id}` | power state, vCPU, memory, BIOS / instance UUID, disks (with datastore file), NICs (MAC, port group, state) |
| `GET /api/vcenter/vm/{id}/tools`, `/guest/identity`, `/guest/networking/interfaces` | VMware Tools state and version, guest OS, host name, IP addresses (powered-on VMs with Tools only) |

Not in the stable REST API, so **not reported** unless the optional provider is on: snapshots, creation date, notes, event history,
HA protection per VM beyond the cluster flag, uptime (the page uses Prometheus for uptime and guest metrics as before).

### Optional provider: pyvmomi

`pip install -r requirements-vsphere.txt` (Docker: `DB_EXTRAS="postgres vsphere"`), restart, then tick "Read snapshots and events with
pyvmomi" on the source. It reads snapshots (with size), creation date, notes and recent events using property reads and
`EventManager.QueryEvents` only; a test lists the allowed calls and fails on any task or write method. The package is imported lazily
and never required: without it the tabs say "not reported" and what to install. Per run it reads at most 200 VMs, oldest first.

## The read-only guarantee

* `hypervisor/vsphere.py` sends only `GET`, plus `POST /api/session` and `DELETE /api/session`. `_send` refuses any other (method, path)
  before a byte leaves the process. `tests/test_client.py` fails if another method literal appears in the file, if the client has a
  write-style method, or if the fake vCenter ever receives anything but GET and the two session requests.
* Control requests (`VmControlRequest`) are database rows. `services.decide` stores the decision and the manual step; there is no code
  path from it to the client (a test runs the approval with the network patched to fail).
* The pyvmomi provider calls no task method (tested).

## Safety rules

* **SSRF**: the host goes through `probes.checks.resolve_allowed`. vCenter is normally private, so list its range in
  `LINKEDEYE_PROBE_ALLOWED_PRIVATE_CIDRS`. The connection goes to the address that passed the check; the original name is used for SNI and
  certificate verification (no second lookup). Metadata and link-local addresses are always refused.
* **TLS**, per source: *verify* (system trust store, default), *pin* (SHA-256 of the leaf certificate, trust on first use: "Test
  connection" shows the fingerprint, you compare it with vCenter and confirm; until then **nothing, not even the password, is sent**;
  a different certificate later is refused before any request) or *skip* (labelled unsafe). Changing host or port clears the pin.
* **Credentials**: a read-only account (the `Read-only` role is enough). The username and password live in the credential vault
  (`core.secrets`, `LINKEDEYE_CREDENTIAL_KEY`); the form never renders them, an empty password keeps the stored one, and rotation is its
  own action. They never appear in logs, error text, audit details, events, templates or JSON (a test captures all log output during a
  failed login). The vault fails closed: without a key nothing is stored.
* **Limits**: 1 MB per answer, a 90 s run deadline enforced by a watchdog (a server that trickles bytes cannot stretch it), no redirects,
  at most 5000 VMs, 500 hosts, 300 datastores, 150 per-VM detail reads per pass (most overdue first, so a large estate is covered over
  several passes), disks, NICs and addresses per VM capped.
* **Untrusted data**: names, notes, guest host names and messages are length-capped, stripped of control and formatting characters,
  escaped on output, and never used as a URL, host or path. The "Open in vCenter" link is built from the configured host and a moref
  that matches `[A-Za-z0-9-]{1,40}`.

## Matching VMs to devices

Every VM vCenter lists gets one of: **matched** (linked to exactly one `network.Device` of type virtual machine), **ambiguous**,
**unmatched**, **ignored**. Rules, in order (`hypervisor/matching.py`):

1. BIOS or instance UUID equal to the device's serial number or a `uuid:<value>` tag: authoritative.
2. Otherwise the VM name, the guest's host name (also its first label) and the guest's IP addresses are evidence. Exactly one device
   fits: matched. Two or more (a name that fits one device and an address that fits another): ambiguous. None: unmatched.
3. A device that fits more than one VM is given to none of them (both ambiguous), so the result does not depend on order; a device that
   another vCenter VM already uses is never taken. The one exception is a device whose VM vCenter **no longer lists** (the VM was
   rebuilt under the same name, or moved to another vCenter): the device follows the VM that is listed now, the old record lets go of
   it, and the change is audited (`hypervisor.match.release`). A VM that is still listed never loses its device this way.

Nothing merges silently: every automatic match is audited (`hypervisor.match.auto` with the reason); a link an operator made is never
replaced by the sync (the sync saves a VM record without touching the link, and re-reads the link right before it matches, so a
link made while a sync is storing survives); the **Unmatched VMs** page lets an operator link a candidate, **create a device** in one
click (named like the VM, site of the source, never over an existing name), ignore, or unlink. An unlinked VM is not matched
automatically again (the same wrong match would otherwise come straight back); link it, create a device, or use "Ignore" and then "Stop
ignoring" to hand it back to the sync. ESXi hosts are matched to physical-server devices the same way and
shown on the Datastores page.

### compute.VMPlacement

For matched VMs the sync fills `compute.VMPlacement` (host when the ESXi host matches a physical server, cluster, power state, OS, vCPU,
memory, disk) and creates the `compute.Cluster` (kind VMware) when none of that name exists. A field is overwritten only while it still
holds what the sync wrote last time or is empty: **a value an operator typed by hand wins**. Set `LINKEDEYE_HYPERVISOR_OVERWRITE_PLACEMENT=true`
to let vCenter win. A cluster of another kind with the same name is left alone.

## Events

`core.events.emit` once per **transition**, never per sync (state kept on the record): VM powered off (a warning, or info when an approved
power request exists from the last 24 hours) and powered on again; VMware Tools not running for more than 5 minutes while the VM is on,
and recovered; datastore used space crossing 90 % (warning) and 95 % (critical), with 2 points of hysteresis on the way down; snapshots
older than 7 days (needs the provider), and cleared; vCenter unreachable after 3 failed syncs in a row, and reachable again. A source
that keeps failing is retried with back-off up to an hour. The sync also records its own observations (power, tools, host moves) as VM
events shown on the tabs.

## Pages, permissions and scoping

| Page | Needs |
|---|---|
| VM tabs, datastores, unmatched VMs, requests, live JSON, CSV | `view`, scoped to the sites you may see (the vCenter's site and, for a matched VM, the device's site) |
| Request an action, link / create device / ignore / unlink | `operate` on the VM's / vCenter's site |
| Approve or reject a request | `admin` on the VM's site (no two-person rule) |
| vCenter sources: add, edit, test, pin, rotate, enable / disable, delete, sync now | `admin` on the source's site |

Out-of-scope records are a 404, not a 403. Every mutation is a POST with CSRF and an audit entry. After **3 rejected logins in a row**
syncing stops (also for "Sync now") until the password is replaced, so a wrong password cannot lock the vCenter account; a "Test
connection" that sends the login is limited to one per 10 seconds, and to one per minute after a rejected login. The stored password
never follows an edited address: changing the host or port removes the stored login, and it has to be entered again under "Rotate
password". Deleting the source, however it happens, deletes the vault row. A vCenter source only matches devices of its own site
(or of every site), so it cannot link, or write placement data onto, a device of another site. A pending request expires after
7 days (`LINKEDEYE_HYPERVISOR_REQUEST_TTL_DAYS`). "Sync now" queues a sync (the worker starts it within a minute) and is rate limited to
one per 30 seconds.

Site scoping decision: `VCenterSource.site` is a site name and is in `administration.services.SITE_NAME_FIELDS`, so a site rename
follows. Hosts, datastores, VM records, snapshots, events and requests have no site field of their own; they are scoped through
`source__site`. A test asserts both.

## Jobs, retention, demo

* `hypervisor.sync` every 60 s (picks due sources, one at a time, 45 s pass budget), `hypervisor.retention` every 6 h. Both run in
  `manage.py run_probes`: run exactly one per database.
* Retention: VM events 30 days (`LINKEDEYE_HYPERVISOR_EVENT_RETENTION_DAYS`, at most 500 per VM), VMs / hosts / datastores vCenter no
  longer lists 30 days (`LINKEDEYE_HYPERVISOR_MISSING_DAYS`), decided requests 180 days.
* `manage.py seed_hypervisor [--remove]` creates the marked example source (disabled, never synced, no events), three datastores, two hosts
  and one example VM "VM-APP-01" with disks, NICs, snapshots and events, so the tabs can be demoed. Idempotent; `--remove` deletes only
  what it created. `hypervisor/tests/fake_vcenter.py` is a small in-process HTTPS vCenter used by the tests.

## Honest limits

* **Never run against a real vCenter.** Everything was built against the public vSphere Automation REST documentation and a fake server
  written from it (`tests/fake_vcenter.py`). Field names, optional fields and error statuses of a real vCenter 7.0 / 8.0 may differ in
  ways the fake cannot show; unknown fields are ignored and missing ones are shown as "not reported", never as OK.
* The pyvmomi provider was tested only against stand-in `pyVim` / `pyVmomi` modules (the package is optional and not installed in CI's
  main jobs). In `pin` mode every handshake pyvmomi makes is compared with the pinned fingerprint before anything is sent on it, so a swapped
  or rebound server never receives the login; pyvmomi still resolves the host name again after the SSRF check (the REST client does not),
  and `skip` mode is unverified by definition.
* The "Open in vCenter" URL (`/ui/app/vm;nav=v/urn:vmomi:VirtualMachine:<moref>/summary`) is the documented deep-link shape without the
  vCenter instance UUID; some vCenter builds may land on the inventory home instead of the VM.
* vCenter's REST VM list has no host or datastore, so placement costs one extra list per host, cluster and datastore (bounded above).
* Guest data, tools state and IPs exist only for powered-on VMs with VMware Tools running. Processes, services and logs of the guest are
  not read from vCenter (they stay Prometheus / node_exporter driven, or "not connected").
* The guest's host name and IP addresses come from VMware Tools, which the guest's own administrator controls: a guest can claim another
  device's address. A name or address match is therefore evidence, not proof. A second VM or device that fits the same device turns the
  result ambiguous instead of linking, and every automatic link is audited with its reason; where that is not enough, put the VM's BIOS
  UUID in the device's serial number or a `uuid:<value>` tag (rule 1 wins over names and addresses) and review the Matched list.
* Per-VM details are read in batches (150 per pass), so on a large estate the first full picture takes several passes.
* Standalone ESXi hosts without vCenter, other hypervisors and VM control (any write) are out of scope for this release.
