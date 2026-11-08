# APM: adapter status matrix, process status and execution logs

The `apm` module shows the data a customer's cron toolkit already produces: an **adapter status matrix** (rows are clients or
brokers, columns are exchange segments, each cell is the adapter or CTCL id coloured green, red, amber or grey), a **process
status** table and an **execution log** viewer, with a detail panel for the selected adapter (failure summary, history, logs,
evidence, retry, RCA). It is at **APM** in the sidebar (`/apm/`).

Data reaches LinkedEye in one of two ways. You may use both.

| Path | How | Needs |
|---|---|---|
| **A. Push** (recommended) | The cron scripts send their result to `POST /api/apm/ingest/` over HTTPS with a per-source bearer token. | Nothing extra: `apm/clients/linkedeye_apm.py` is a single stdlib-only file. |
| **B. Read Redis** | LinkedEye connects to the Redis that the scripts already write to, every minute (`apm.redis_poll`, run by `manage.py run_probes`). | The optional `redis` package (`requirements-apm.txt`), the Redis host allowed by `LINKEDEYE_PROBE_ALLOWED_PRIVATE_CIDRS` if it is private, and `LINKEDEYE_CREDENTIAL_KEY` for a password. |

Both paths call the same service functions (`apm.services.ingest_matrix`, `ingest_table`, `ingest_logs`), so validation, history,
events and limits are identical. **LinkedEye never connects into the customer's hosts**: a "Retry Now" is a request that the
script pulls.

## Set up a source

1. **APM > Sources > Add a source** (administrators). Pick the site, a name and the interval the producer reports at.
2. Copy the token on the next page: it is shown **once**. Only a salted SHA-256 and a short prefix are stored, so it cannot be shown
   again; **Rotate token** issues a new one and the old one stops working immediately.
3. On the host that runs the scripts, set the environment (the page shows the exact block):

   ```sh
   export LINKEDEYE_APM_URL=https://linkedeye.example.com
   export LINKEDEYE_APM_TOKEN=lea_xxxxxxxx_...
   ```

4. Copy `apm/clients/linkedeye_apm.py` next to `adapterStatus.py` and `processStatus.py` and add the lines below.

TLS certificate verification is always on. For a private CA set `LINKEDEYE_APM_CA_BUNDLE=/path/to/ca.pem`. Plain `http://` is
refused unless `LINKEDEYE_APM_ALLOW_HTTP=1` (testing only).

### The change to `adapterStatus.py` (after the existing Redis write)

`data` is the dict the script already builds; keep the existing `Redis().set(...)` and add:

```python
import linkedeye_apm                                                  # 1
linkedeye_apm.push_matrix(data, script="adapterStatus.py",            # 2  every run, not only when the data changed
                          duration_s=time.time() - started)           # 3
for cmd in linkedeye_apm.poll_commands().get("commands", []):         # 4  "Retry Now" requests from the dashboard
    pass                                                              # 5  re-probe cmd["client"], cmd["segment"] here
    linkedeye_apm.command_done(cmd["id"])                             # 6
```

Call `push_matrix` on **every** run, not only when the toolkit decides the data changed: the toolkit writes Redis only on a change or
every 20 minutes, but a push every run is what proves the cron job is alive (the dashboard shows data as stale otherwise).

Treat `cmd["client"]`, `cmd["segment"]` and `cmd["note"]` as data: look the adapter up in your own configuration, and never put them
into a shell command line or an SQL string. The reference client sends the token as an *unredirected* header, so a redirect from the
server can never forward it to another host.

### The change to `processStatus.py`

```python
import linkedeye_apm
linkedeye_apm.push_table(data, script="processStatus.py")            # data = the dict built for Redis
```

You can pass the toolkit's `str(dict)` instead of the dict: `push_matrix(str(data))` sends it as `payload_str`, which LinkedEye
parses with `ast.literal_eval` only (never `eval`).

Optional log lines (they appear in Execution Logs and, with `client` and `segment`, in that adapter's detail panel):

```python
linkedeye_apm.push_logs([{"ts": "2026-08-27 15:01:59,789", "level": "ERROR", "process": "adapterStatus.py",
                          "message": "adapter 12345 failed: ...", "client": "c001", "segment": "NSE"}])
```

### Cron

```cron
*/5 * * * *  . /etc/linkedeye-apm.env; /usr/bin/python3 /opt/apm/adapterStatus.py
*/5 * * * *  . /etc/linkedeye-apm.env; /usr/bin/python3 /opt/apm/processStatus.py
```

Keep the env file readable by the cron user only (`chmod 600`). The client never raises into the cron job: every call returns
`{"ok": True, ...}` or `{"ok": False, "error": "..."}`, retries with exponential backoff and honours `Retry-After`.

## The API

All requests carry `Authorization: Bearer <token>`. A browser session never authenticates these endpoints (they are CSRF-exempt
**only** because a token is required). Every failure to authenticate (no header, malformed, unknown prefix, wrong secret, disabled
source, rotated token) returns the same `401` body, and every response carries `X-Request-Id` and `request_id`.

| Request | Meaning |
|---|---|
| `POST /api/apm/ingest/` | Body (JSON, at most 2 MB): `{"type": "matrix" or "table" or "logs", "payload": {...}` **or** `"payload_str": "..."`, `"host"`, `"script"`, `"duration_s"`, `"logs": [...], "completed_commands": [ids]}`. Answers `200` with counts, `duplicate`, `warnings` (row-level, up to 50) and `pending_commands`. |
| `GET /api/apm/commands/` | Pending "Retry Now" requests for this source; they are marked picked up. |
| `POST /api/apm/commands/<id>/done/` | Report one finished. The next matrix that contains the adapter also completes a picked-up retry. |

Limits: body 2 MB (`413`); matrix at most 200 brokers x 40 segments; table at most 2000 rows; at most 1000 log lines per request;
strings are length-capped and stripped of control characters (`422` on a limit breach, nothing is applied). Rate limit 60 requests
per minute per source (`429` with `Retry-After`; counted per web worker, like the other public endpoints); repeated failed authentication from one address is answered with `429` too, but a request with a valid token is never turned away because of it. Bodies must carry a `Content-Length` (chunked uploads are read as empty and refused). A row that is
wrong becomes a **warning** and is skipped, the rest is applied; the whole payload is applied atomically.

**A matrix or table is a complete snapshot** of one source: adapters (or, per host, processes) missing from a report are removed from the current state (their history stays), and an empty matrix is ignored rather than wiping the page. Send one source's whole matrix in each report, and use one source per deployment.

**Idempotent**: the same `executedOn` with the same content is accepted but adds nothing and does not count as fresh data. The same
`executedOn` with different content updates the state without a new history row. A report older than the current state is kept in
the history only: this also holds for a replay of an old `executedOn` with other content, so a stale or hostile resend can never overwrite newer state. An `executedOn` more than 15 minutes ahead of the server clock (a host in the wrong time zone) is replaced by the receive time with a warning; naive times are read in the server time zone (`TIME_ZONE`, Asia/Kolkata by default). Run and history rows are also capped when written (`APM_CAP_RUNS`, `APM_CAP_CELL_EVENTS`), not only by the retention job.

### Payloads (the toolkit's own format)

```text
matrix: {'overallStatus': bool, 'status': 0|2, 'executedOn': 'DD-MM-YYYY HH:MM:SS [ Total: N ; Success: S ; Failure: F ]',
         'type': 'matrix', 'data': {client: {segment: {'isSuccess': bool, 'ctcl_id': '12208', 'value': '12208', 'status': 0|1|2|3} | "-"}}}
table:  {'overallStatus': bool, 'status': 0|2, 'executedOn': 'YYYY-MM-DD HH:MM:SS [ ... ]', 'type': 'table',
         'data': [{'pid': int|'NA', 'name': str, 'cpu_percent': float|'NA', 'memory_percent': float|'NA', 'status': 0|2, 'Status_': 'running'|'not running'}]}
```

Status: 0 failed (red), 1 warning (amber), 2 healthy (green), 3 unknown. `"-"` (or `null`) means the adapter is not configured
for that cell: it is not stored. Optional extras per matrix cell: `error` (the message shown in Failure Details) and `exit_code`.
Optional extras per process row: `host`, `uptime_s` (or `uptime` as `HH:MM:SS`), `last_heartbeat`, `last_execution`,
`next_execution` (ISO 8601 or the toolkit's formats), `response_ms`, `exit_code`, `remarks`. Times without a zone are read in the
project time zone (`LINKEDEYE_TIME_ZONE`, default Asia/Kolkata).

## What the pages do

* **Dashboard** `/apm/`: overall status (healthy, degraded, down, stale, no data), total adapters across N exchanges, success and
  failure counts and percentages, average response time over the last 5 minutes (from the process rows that carry one), last
  executed, duration, and an Auto Refresh switch with a 10 / 30 / 60 s interval (remembered in the browser). The exchange tabs, the
  client filter, search and status filter and the selected adapter are all in the URL.
* **Adapter detail panel**: Adapter Details, Failure Details (summary, error message, exit code, failure time, count, streak, log
  snippet), History (status changes and a 24 hour strip), Logs, Evidence (a text bundle with timestamps; Copy or Download).
  Quick actions: **Retry Now** (a request the script pulls; shows pending, picked up, done), **View Script** (the script and host the
  report named; nothing is executed), **View Logs**, **Export** (CSV), **Copy Evidence**, **Generate RCA** (a Service Desk
  *Problem* prefilled with the facts when you may create problems, otherwise a draft to copy).
* **Process Status** and **Execution Logs** (levels coloured; Pause holds new lines back; "Clear view" clears only your screen,
  the stored logs are not deleted; Show last 25 / 100 / 500).
* The page patches itself from `/apm/live/` without a reload. It pauses while the tab is hidden, backs off after errors, and shows a
  **stale banner** when no report arrived within 3 times the expected interval (the cron job may have stopped).
* **Sources** `/apm/sources/`: add, edit, rotate the token, enable or disable, test the Redis connection, delete.

## Permissions and scoping

| Action | Needs |
|---|---|
| See the dashboard, detail, CSV, evidence | `view` on the source's site |
| Retry Now, create the RCA problem | `operate` on the source's site (and the source enabled) |
| Sources admin | `admin` on the source's site (`All Sites` sources need admin on every site) |

A source has a site; its adapters, history, processes and logs have none of their own and are scoped through the source, so a
site rename (Administration) follows through `apm.Source.site`. Every mutation is audited (`apm.source.create`, `update`,
`rotate_token`, `delete`, `redis_test`, `password_clear`, `apm.retry.request`, `apm.rca.create`). Ingest itself is not audited
per request (it is high-frequency machine traffic): see `Source.last_seen` and the Recent reports table.

## Events, stale detection and retention

* A transition of an adapter or a process **into failure** emits a `critical` event and the transition back emits an `ok` event with
  the same fingerprint, so alerts and (with `LINKEDEYE_INCIDENT_AUTO_CREATE`) incidents open and mitigate. At most 25 events are
  emitted per report, plus one summary.
  An adapter or process that was failing and is then left out of the report (removed from the producer's configuration) also emits
  an `ok` event with its fingerprint, so its alert or incident does not stay open for ever.
* A source with no new matrix or table for longer than its expected interval (plus a short grace) is shown stale (grey) and counted
  separately, and emits **one** `warning` event when it goes silent and an `ok` event when it reports again (`apm.stale`, every minute).
* `apm.retention` (every 6 hours) deletes runs and status history older than `LINKEDEYE_APM_RETENTION_DAYS` (30) and logs older than
  `LINKEDEYE_APM_LOG_RETENTION_DAYS` (7), and keeps at most 5000 runs, 20000 status changes and 20000 log lines per source.

## Reading Redis (path B)

Fill the **Read the toolkit's Redis keys** section of the source: host, port, database, the key prefix (the toolkit's `SITE_NAME`)
and the adapter and process key suffixes (`ADAPTER_KEY_SUFFIX`, `PROCESS_KEY_SUFFIX`); the keys read are
`<prefix>:<suffix>`. The password is stored in the credential vault (write-only, never shown or logged). The host goes through the
probe network policy (loopback, private and metadata addresses are refused unless listed in
`LINKEDEYE_PROBE_ALLOWED_PRIVATE_CIDRS`); a plain connection is made to the address that passed the check, and TLS uses the host
name so the certificate is verified. Reads use short socket timeouts and a 1 MB size cap, and an error is recorded on the source.

The toolkit refreshes the Redis key only when the data changed or every 20 minutes, so a Redis-read source should use an **expected
interval of at least 1500 seconds** (otherwise it is shown stale between refreshes). Prefer pushing every run (path A) when you want
a 5 minute heartbeat.

Without the `redis` package nothing else breaks: the form says to install `requirements-apm.txt` (the Docker build argument is
`DB_EXTRAS="postgres apm"`).

## Example data

`python manage.py seed_apm` creates a clearly marked **example** source with synthetic clients `c001` to `c012`, adapter ids,
4 processes and 100 log lines (4 adapters failing, one warning), with a short history. It prints a demo token once. It is
idempotent (a re-run rebuilds only the example source), emits no events, and `python manage.py seed_apm --remove` deletes it.
The example source expects a report every hour; run the command again to refresh it.

## Security notes

* No `eval`, `exec` or `pickle`; the toolkit's `str(dict)` goes through `ast.literal_eval` after a size and nesting check.
* Everything the producer sends is stored as text or numbers and rendered with `textContent` (never as HTML); CSV export defuses
  formulas; the JSON the page embeds cannot close its own `<script>` tag.
* Token comparison is constant-time and the failure path does the same work for every kind of failure.
* The Redis password and the token are never logged, audited or echoed.
