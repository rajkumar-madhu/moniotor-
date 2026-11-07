---
name: run-linkedeye
description: Run, start, drive, screenshot and test LinkedEye (this Django ITSM and monitoring console) locally. It starts a throwaway lab with the real web server and run_probes worker plus a stub Prometheus, and drives signed-in pages with a headless-Chrome driver. Use when asked to run or start LinkedEye, open or check a page in a browser, take a screenshot, verify live device monitoring (network:live, graphs, ranges, status changes), simulate a device outage, or smoke-test a change in the running app.
---

LinkedEye is driven with three tools:

- **`lab.sh`** starts the processes: a throwaway SQLite database, a stub Prometheus, a TCP port that serves as a device's check target, and the **real** `runserver` and `run_probes`.
- **`driver.mjs`** drives signed-in pages in a real headless Chrome over the DevTools protocol.
- **`curl`** calls JSON endpoints with the lab's session cookie.

All paths are relative to the repo root. The lab never touches `db.sqlite3`.

## Prerequisites

Verified on macOS (Darwin 25.6) with:
- Google Chrome in `/Applications`
- Node v26.12.1 (the driver needs Node 22 or later for its built-in `WebSocket`; nothing to install from npm)
- Python 3.15.2 in the repo's `.venv`

Linux is not verified. On Linux, point `CHROME` at a Chrome or Chromium binary.

```bash
.venv/bin/pip install -q -r requirements.txt
```

## Run (agent path)

Start everything: create the lab database on first use, start the 4 processes, and sign in as `admin`.

```bash
.claude/skills/run-linkedeye/lab.sh up
```

The lab database comes with two devices, a 24-port switch and a VM:

| Device | ID | Page | Data source |
|---|---|---|---|
| `lab-sw-01` switch | 1 | http://127.0.0.1:8001/network/devices/1/ | snmp_exporter-style series |
| `lab-vm-01` VM | 2 | http://127.0.0.1:8001/network/devices/2/ | node_exporter-style series |

Both come from the stub, and the worker stores real reachability checks and interface rates every 60 s. Ports 7 and 14 are down, port 20 is admin-down, and port 5 shows errors.

Drive pages with steps, run in order:

```bash
node .claude/skills/run-linkedeye/driver.mjs goto '/network/devices/1/?range=1h' shot switch wait-poll live click-nav 'a[title="Last 6 hours"]' click-nav 'nav.nw-tabs a[href*="tab=traffic"]' eval '!!document.querySelector(".chart[data-src=nw-port-bars] svg rect")' shot traffic
node .claude/skills/run-linkedeye/driver.mjs goto /network/devices/2/ wait-poll eval 'document.querySelector("[data-live=cpu]").textContent' shot vm
node .claude/skills/run-linkedeye/driver.mjs goto / shot dashboard
```

To simulate an outage, close the switch's check port. Keep a page open: the worker marks the switch Down after 2 failed checks, and the open page shows the banner.

```bash
.claude/skills/run-linkedeye/lab.sh outage
node .claude/skills/run-linkedeye/driver.mjs goto /network/devices/1/ wait-for '!document.getElementById("nw-live-notice").hidden' 300 live shot switch-down
.claude/skills/run-linkedeye/lab.sh restore
```

The outage also runs the event stream: a critical "lab-sw-01 is down" event, a service-desk incident (INC-…) opened from its fingerprint, then "is up again". All of it appears on the dashboard's Recent events.

Call a JSON endpoint with the lab session:

```bash
curl -s -b "${TMPDIR%/}/linkedeye-run/cookies.txt" -H "Accept: application/json" "http://127.0.0.1:8001/network/devices/1/live/?range=1h"
```

Housekeeping:

```bash
.claude/skills/run-linkedeye/lab.sh status    # processes, /healthz, device states
.claude/skills/run-linkedeye/lab.sh down      # stop everything (keeps the lab DB, so the next `up` is fast)
```

`lab.sh login` refreshes the cookie. `lab.sh reset` stops everything and deletes the lab directory.

Files land in `$TMPDIR/linkedeye-run/`:
- `shots/<name>.png`: screenshots. Look at them.
- `web.log`, `worker.log`, `prometheus.log`, `checkport.log`
- `admin.pw`: the admin password. Don't paste it into chat.
- `lab.sqlite3`: the lab database

Ports are 8001 (web), 9099 (stub Prometheus) and 8099 (switch check port). Override them with `LE_WEB_PORT`, `LE_PROM_PORT`, `LE_CHECK_PORT`, and the directory with `LE_RUN_DIR`.

| driver step | what it does |
|---|---|
| `goto <path\|url>` | load a page and wait for the load event |
| `click <css>` / `click-nav <css>` | click; `click-nav` also waits for the next page load |
| `wait <s>` | sleep |
| `wait-poll [timeout_s]` | wait for the next `network:live` response (default 45 s) |
| `wait-for <js> [timeout_s]` | poll every 2 s until the expression is truthy (default 240 s) |
| `eval <js>` | print the expression's value as JSON |
| `live` | print a device page's live state: `data-live` values, tones, badge, notice, chart count |
| `shot <name>` | screenshot to `$TMPDIR/linkedeye-run/shots/<name>.png` |
| `size <w>x<h>` | viewport (default 1440x1700) |

At exit the driver lists the live polls it saw, plus any JS exception, `console.error` or same-origin HTTP status of 400 or above. It exits with code 1 if there were any, or if a step failed.

## Direct invocation

Call app code against the lab database without the browser. Start the lab first (`lab.sh up`): with it down, the call still works, but the traffic and errors charts come back empty because the stub Prometheus is off.

```bash
LINKEDEYE_DEBUG=true LINKEDEYE_DB_NAME="${TMPDIR%/}/linkedeye-run/lab.sqlite3" LINKEDEYE_PROMETHEUS_URL=http://127.0.0.1:9099 .venv/bin/python manage.py shell -v0 -c "
from network.models import Device
from network.live import device_snapshot
snap = device_snapshot(Device.objects.get(name='lab-sw-01'), '1h')
print(snap['status'], snap['values']['traffic_in'], sorted(k for k, v in snap['charts'].items() if v))"
```

## Run (human path)

Run the web server on the developer database (`db.sqlite3`) and sign in with your own account:

```bash
LINKEDEYE_DEBUG=true .venv/bin/python manage.py runserver 127.0.0.1:8002 --noreload
```

## Test

```bash
LINKEDEYE_DEBUG=true .venv/bin/python manage.py test network
```

Result: 107 tests, OK, in about 15 s. The full suite is `manage.py test` (1074 tests, OK, 2 skipped).

## Gotchas

- **Don't start `run_probes` against `db.sqlite3` casually.** The developer devices are on private 10.x addresses. Without the user's `LINKEDEYE_PROBE_ALLOWED_PRIVATE_CIDRS`, the SSRF probe policy refuses every check and the stored statuses flip to "unknown". The lab avoids this by using `LINKEDEYE_DB_NAME`.
- **Loopback targets are refused too.** The lab devices are 127.0.0.1, so `lab.sh` exports `LINKEDEYE_PROBE_ALLOWED_PRIVATE_CIDRS=127.0.0.0/8`.
- **An outage takes 90–150 s to show.** A device goes Down only after 2 failed checks in a row, and checks run every 60 s. Use `wait-for … 300`, not a fixed `wait`. Recovery after `restore` happens on the next check (60 s or less).
- **The first live poll comes 30 s after page load,** because the page was rendered with fresh data. `live.js` also pauses while `document.hidden`, so the driver calls `Page.bringToFront`.
- **Only part of the page updates live.** KPIs, the header badge, charts and front-panel ports update in place. The Recent Events table and the Top-10 interface table don't; they need the reload that the banner offers.
- **Prometheus range answers are a few ms apart per query.** A stub that returns identical timestamps for every query hides chart bugs: inbound and outbound lines used to render as dots. The bundled stub computes timestamps per request like the real server, and `network.metrics.align_series` handles the offset.
- **Some stub output is not an app bug.** The errors chart zigzags at the 24h and 7d ranges because the stub's sine changes faster than the step. Uptime shifts a little when the stub restarts.
- **macOS `$TMPDIR` ends in `/`.** Build paths as `${TMPDIR%/}/linkedeye-run`.
- **Port 8000 can be taken** by unrelated local apps (on this machine a `main.py` held it), which is why the lab defaults to 8001.
- **Stop processes through their pid files** (`lab.sh down`), not `pkill -f <path>`. A process started from its own directory with a relative script path doesn't match the pattern.
- **`lab.sh down` sends SIGTERM,** and `run_probes` releases its scheduler lease. The next `up` logs `scheduler lease scheduler:default acquired` right away (see `worker.log`).
- **The Claude-in-Chrome extension asks the user to pick a browser** when several are connected, and stalls if nobody clicks Connect. The headless driver needs nothing from the user and doesn't touch their Chrome.

## Troubleshooting

- **`curl` prints `000` right after starting a server:** it isn't listening yet. Use `curl --retry 30 --retry-connrefused --retry-delay 1` (`lab.sh up` does).
- **`Error: ENOTEMPTY … chrome-profile-*` when the driver exits:** Chrome was still writing its profile when the driver deleted it. The driver now waits for Chrome to exit. Delete `${TMPDIR%/}/linkedeye-run/chrome-profile-*` left over from an interrupted run.
- **Traffic or errors lines drawn as separate dots:** the series' timestamps aren't aligned. Make sure the chart goes through `network.metrics._range_chart` / `align_series`, or `vm_metrics._chart_from_series(..., step=)`.
- **`manage.py shell` prints `N objects imported automatically`:** pass `-v0`.
