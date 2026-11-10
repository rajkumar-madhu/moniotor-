# Server hardware over Redfish (Dell iDRAC, HPE iLO)

The `hardware` app reads the management controller (BMC) of a physical server over the Redfish API and shows its sensors, power
supplies, fans, inventory and event log on the server's **Hardware** tab. **It is read-only.** LinkedEye never sends a write,
action, power or firmware command to a BMC: the buttons for those record a *request* that an administrator approves or rejects
in LinkedEye, and a person carries it out in the BMC.

## What you get

* **Tab "Hardware (iDRAC/iLO)"** on a physical server (Network Devices, Physical Servers, the server, Hardware): vendor switch
  (auto-detected, can be overridden), range selector (1 h / 6 h / 24 h), Refresh (queues an immediate poll, rate limited),
  overview card, system health roll-up with tiles (power supplies, fans, temperature, storage, memory, voltage, intrusion, CPUs,
  batteries), temperature / fan / power-supply / voltage tables, temperature and fan trend charts, recent BMC log entries, quick
  actions. A section the BMC does not report is shown as **not reported**, never as OK.
* **Pages** `hardware:endpoints` (all BMC endpoints, with their state), `hardware:endpoint_new|edit|test|pin|toggle|delete`,
  `hardware:inventory` (CPUs, DIMMs, drives, controllers, NICs, PSUs, batteries with serials and firmware), `hardware:events`
  (the BMC log with filters), `hardware:export` (CSV of current sensors and events), `hardware:alerts`,
  `hardware:requests` (control requests). Sidebar group **Server Hardware**.
* **Events** `core.events.emit("hardware", ...)` once per transition of the overall health (ok, warning, critical) and of the
  connection (unreachable after 3 failed polls in a row, login rejected after 2), with the fingerprint `hardware:<endpoint id>`
  so a recovery event closes the incident a critical event opened. The event is attached to the server device, so it also
  appears on the device's Alerts tab. Nothing is emitted per reading.
* **Jobs** `hardware.poll` (every 30 s; each endpoint has its own interval, default 60 s, minimum 30 s; a few endpoints are read
  at once in worker threads that never touch the database) and `hardware.retention` (hourly).

## Set up an endpoint

1. Create a user on the iDRAC / iLO with the **read-only** role (Dell: "Read Only"; HPE: only "Login").
2. Allow the BMC network for probes: BMCs normally have private addresses, which the network policy refuses unless you list the
   range, for example `LINKEDEYE_PROBE_ALLOWED_PRIVATE_CIDRS=10.10.1.0/24`. Loopback, link-local and metadata addresses are never
   allowed.
3. Make sure the credential vault is configured (`LINKEDEYE_CREDENTIAL_KEY`, see docs/DEPLOYMENT.md). Without it nothing is stored.
4. Hardware, BMC endpoints, Add endpoint (needs the admin role on the server's site): host, port, the login, the certificate mode.
5. Press **Test connection**. The test first looks at the certificate and only then sends the login (see below), so the login is
   never sent to a server whose certificate has not passed the check. With pinning, it shows the SHA-256 fingerprint: compare it
   with the one in the BMC's certificate page and press **Pin this certificate**. Polling starts by itself.

### Certificate modes

| Mode | Meaning |
|---|---|
| `pin` (default, recommended) | the SHA-256 of the BMC's leaf certificate must equal the fingerprint you confirmed (trust on first use with a confirm step, like SSH host-key pinning). Compared after the handshake and **before any request is sent**, on every connection the poll opens (a reconnect included). Changing the host or port clears the pin. A replaced certificate is refused until an admin pins the new one. |
| `verify` | the certificate must chain to the server's CA bundle and match the host name. |
| `skip` | not verified. Needs an explicit confirmation, is labelled "unsafe" in the lists and on the tab. |

Plain HTTP exists only as an explicit, labelled override (the login would travel in clear). Authentication is HTTP Basic, per request. No Redfish *session* is created: that would be a POST to `SessionService`, the one thing this client does not do. There is therefore no session token to cache.

## Read-only guarantees (and how they are enforced)

* `hardware/redfish.py` has one request method, `get`, and the HTTP verb is the module constant `METHOD = "GET"`. There is no write
  method to call. `tests/test_readonly.py` fails if a write-style method or verb literal appears, if a second `.request(` call
  appears anywhere in the app, or if the fake BMC ever receives anything but GET during a poll or a test connection.
* Control requests (`Power on`, `Graceful shutdown`, `Force power off`, `Update firmware`) are rows in `ControlRequest`: who, what,
  when, why, and the decision. Approving one **records** the decision and shows the manual step. States: pending, approved,
  rejected, expired (after 7 days). Operators request, site admins decide; no two-person rule.
* Responses are JSON only, capped at 1 MB, redirects are refused, one wall-clock deadline per poll is enforced by a watchdog
  that shuts the socket down, at most 220 requests and 64 members per collection are read, and links found in BMC documents are
  untrusted: only same-host `/redfish/v1/...` paths without dot segments are ever requested.
* The login is decrypted only for the duration of a poll (through `vault.decrypt`, so the poll job does not write an audit row
  every minute), is never logged, never put in an error message (`scrub` removes it) and never returned by a page, JSON, CSV or
  audit entry. Polling stops after **3 rejected logins in a row** so a wrong password cannot lock the BMC account; saving the
  password again, or a successful test, resumes it. A "Test connection" that sends the login is limited to one per 10 seconds per
  endpoint, and to one per minute after the BMC rejected the login (a rejected test counts like a rejected poll). Looking at the
  certificate sends no login and is not limited.
* The stored password never follows an edited address: changing the host, the port or switching to plain HTTP needs the password
  to be typed again, so nobody can aim the (write-only) login at a server of their own. Deleting the endpoint, or the server
  device it belongs to, deletes the vault row too.
* Everything a BMC sends (names, messages, serials) is length-capped, stripped of control and bidi characters, stored as text
  and escaped on output. The CSV defuses formula cells. Numbers are kept finite and in range (a PSU load is computed only from a
  capacity of at least 1 W). A poll whose answer still cannot be stored is recorded on the endpoint as a failed poll with that
  reason (back-off and the unreachable alert apply), not only in the server log.

## Data model

`BmcEndpoint` (1:1 with the device; vault credential id; pinned fingerprint; poll state), `HwComponent` (current state of each
sensor, PSU, fan, drive, DIMM, CPU, NIC, battery), `HwReading` (time series of temperature, fan, PSU output and voltage;
7 days, readings older than 24 h thinned to one per 10 minutes), `HwEvent` (BMC log entries, unique per endpoint and entry id,
90 days), `ControlRequest`. All carry `tenant_key`.

**Site scoping.** An endpoint has no site of its own: it belongs to a device, and the device's site decides who may see or manage
it (`core.access`, tested for every page and JSON endpoint; another site's server is a 404). For that reason the app has no
entry in `administration.services.SITE_NAME_FIELDS`: a site rename follows through the device. The vault row's site is kept equal to the device's site:
saving the server device on another site (its page, an import, any `save()`) moves the BMC login's vault row with it, so the old site's
admins no longer see it under stored credentials (a `post_save` handler on `network.Device`; a bulk `QuerySet.update()` of device
sites bypasses it).

## Settings

`LINKEDEYE_HARDWARE_READING_RETENTION_DAYS` (7), `LINKEDEYE_HARDWARE_EVENT_RETENTION_DAYS` (90), `LINKEDEYE_HARDWARE_POLL_WORKERS`
(4), and `LINKEDEYE_PROBE_ALLOWED_PRIVATE_CIDRS` for the BMC network. No new dependency.

## Demo

`manage.py seed_hardware [--vendor dell|hpe] [--site NAME] [--remove]` creates the server `example-srv-01` (tag `example`, address
from the documentation range) with a SYNTHETIC endpoint state and 24 hours of invented readings. The endpoint is marked
`is_example`: it is never polled, never raises events, and has no stored login. `python -m hardware.tests.fake_redfish dell 8443`
starts a local fake BMC (HTTPS, self-signed, user `monitor`) that the tests also use.

## Honest limits

* **No real iDRAC or iLO was available.** The parsers were written against the public Redfish schema and the shape of what iDRAC9
  and iLO 5 return, and are tested against hand-written fixtures (`hardware/tests/fixtures.py`) and a local fake server. Expect
  firmware-specific differences; the code ignores unknown fields and reports missing ones as "not reported".
* Assumed API: Redfish 1.6 or newer; Dell: `Systems/System.Embedded.1`, `Chassis/System.Embedded.1` (+ `Thermal`, `Power`),
  `Managers/iDRAC.Embedded.1`, log services `Sel` and `Lclog`, `LicenseService/Licenses`; HPE iLO 5/6: `Systems/1`, `Chassis/1`,
  `Managers/1`, log services `IML` (system) and `IEL` (manager). Links are followed from the documents, these are only fallbacks.
* Not read: the newer `ThermalSubsystem` / `PowerSubsystem` model (a BMC that only offers it shows those sections as not
  reported), volumes and RAID state, GPUs, OEM-only data. Batteries are best effort (HPE `SmartStorageBattery` in the Power
  resource; Dell controller battery when inline). The Dell lifecycle controller version comes from `Oem.Dell.DellSystem`.
* The vendor switch only chooses which parser reads the BMC; a wrongly forced vendor yields "not reported" parts, not wrong values.
* Power and firmware are requests only. There is deliberately no "execute" step in this release.
