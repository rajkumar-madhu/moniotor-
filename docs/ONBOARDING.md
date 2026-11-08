# Onboarding

Onboarding takes a site, a server, a network device or a Prometheus server from "I know its name" to "everything for
it shows in LinkedEye" without hand-editing Prometheus, building a dashboard or typing a CMDB entry. The operator
hands over the connection details (including server credentials); LinkedEye does the rest.

Open **Administration → Onboarding** (`/onboarding/`). Only an administrator **for the site** can onboard into it;
viewers and operators can follow a run but not change it.

## The flow

A run is a resumable walk through eight steps. The progress rail at the top of every page shows where the run is,
and every step is idempotent: closing the browser loses nothing, and re-entering a step never duplicates a target.

| Step | What you do | What LinkedEye does |
|---|---|---|
| **Scope** | Pick the kind (site, server, device, Prometheus, bulk), the site, the client and the environment. | Creates the run, stamped with the tenant. |
| **Targets** | Paste hosts (one per line: `host`, `host:port`, `host, type`, `host, type, port`) or upload a CSV (`hostname,address,type,port`). Types: Linux, Windows, network device (SNMP), Kubernetes node, URL/service. | Adds or updates targets; a host already on the run is updated, never duplicated. Hostnames are validated (letters, digits, `.`, `-`, `_` or an IPv6 literal). |
| **Credentials** | Hand over an SSH password or key, an SNMP v2c community or v3 credentials, HTTP basic or a bearer token, per target or shared. | Encrypts it into the vault. It is never shown again. |
| **Verify** | Press *Verify*. | DNS, TCP connect, `GET /metrics` and the exporter identity, optionally host facts over SSH. Every probe goes through the SSRF-guarded `probes.checks`. Nothing is probed until you press the button. |
| **Enable collection** | Run the shown install command on the host, or tick *Install over SSH* to let LinkedEye do it with the stored credential. | Fixed command allow-list, never free text from the form. A failed install leaves the host untouched and reports the error. SNMP devices get the exporter module, Kubernetes nodes point at kube-state-metrics. |
| **Logs** | Press *Generate shipper configuration*. | promtail or vector config pre-filled with the log-ingest URL; the token appears on that page only. |
| **Confirm** | Press *Activate*. | Creates or links the CMDB record, the network device or compute server and the exporter row (each audited as the operator who pressed the button), keeps service-discovery membership as set on the Targets step, then waits for `up == 1`. The page polls every 10 s; a background job (`onboarding.liveness`, every 60 s) does the same with the browser closed. |
| **Done** | Press *Mark complete* (or let it complete itself when every target is live). | Links straight to the host's metrics page, the fleet view, the explorer, its logs, its CMDB record and its alerts. |

A run moves through the statuses *draft → verifying → installing → waiting for data → complete* (or *failed*, which
can be reopened). Each target moves through *new → reachable / unreachable → exporter found → installing →
installed → scraping → live* (or *failed*). Only an **activated** target (*scraping*) waits for `up == 1`: a target
that is merely verified or installed is never promoted to *live*, and a run cannot complete before its targets have
been activated. A target that is already *scraping* or *live* keeps that state when Verify or Enable runs again, even
if the probe fails (a DNS blip must not drop a healthy host from service discovery); the failure is recorded in its
last result and facts. The full trail of what each step did is under **Run log**; it never contains a secret.

Verify and Enable work through the targets inside one request, so each stops after a 45 second time budget and says
how many targets it did not reach; press the button again to continue with them (the least recently checked go
first). An audit row is written before the work starts, and an *Install over SSH* takes a per-host lock (it expires
after ten minutes), so a retry cannot start a second install on a host that is already being installed. Moving this
work to a background job is not done yet.

The client of a run is the client that owns its site. Only an admin on every site may label a run with another client.

## The credential vault

* Secrets are encrypted at rest with Fernet (`cryptography`). The key is `LINKEDEYE_CREDENTIAL_KEY`; see
  [DEPLOYMENT.md](DEPLOYMENT.md) for generating and rotating it.
* **Fail closed.** If the key or the `cryptography` package is missing, saving a credential fails with a message
  that says so. There is no plaintext fallback.
* **Write-only.** A stored secret is never rendered, returned by a form, logged, exported (CSV, Django admin) or
  included in an error message. The page shows `user / ••••••••`. To change one, *Rotate* it: the ciphertext is
  replaced in place and no history of values is kept.
* Every read of a secret (an SSH login, for example) is audited with who, what and why, and stamps *Last used*.
* A credential belongs to one site. It cannot be attached to a run in another site, and changing a run's site
  drops its credential attachments.
* A credential also belongs to a kind of target: an SSH password or key only to Linux and Windows servers, an SNMP
  community or v3 login only to network devices, an HTTP basic login to servers, Kubernetes nodes, URL services and
  a Prometheus server, a bearer token to servers, Kubernetes nodes and URL services. *Apply to: every target* attaches
  to the targets it fits and logs the rest as skipped, and verify and install ignore a mismatched attachment, so an
  SSH password is never offered to a URL service and an HTTP login never goes to a network device.
* An exporter's HTTP basic or bearer login is sent only over **https**. A target is https when it is on port 443 or
  was pasted as `https://host:port` (service discovery then adds `__scheme__=https`); over plain http the login is
  not sent and Verify says so. For a network you trust, `LINKEDEYE_ONBOARDING_ALLOW_CLEARTEXT_CREDENTIALS=true`
  allows clear text. The same rule applies to a Prometheus login.
* Credential labels are shown only to the site's admins; a viewer or operator sees "attached".
* Rows that serve every site (the connected Prometheus' login and the service-discovery token) are stored
  with an empty site: a site's credentials step never lists, attaches, rotates or deletes them, and no host
  check ever sends them to a host. The service-discovery endpoint accepts only such a tenant-wide row, and the
  labels `Prometheus service discovery` and `Prometheus http…` are reserved, so a site admin cannot plant a
  token of their own. Only an admin on every site connects Prometheus or issues that token. The
  log-ingest token is shared by every site too, so the Logs step shows it only to an admin on every site; a
  site admin gets a `<LOG_INGEST_TOKEN>` placeholder.
* Reading a secret from a hook (SSH, an exporter login) is audited as the operator who pressed the button.
* `DEBUG=true` does not widen any of this: `core.debug.AlwaysCleansingFilter` (set as
  `DEFAULT_EXCEPTION_REPORTER_FILTER`) keeps Django's `sensitive_post_parameters` / `sensitive_variables`
  active under DEBUG, and the views that take secrets use them, so the technical 500 page shows neither the
  POST body nor any local variable of the code that handles a secret.

For other modules the API is three calls in `core/secrets.py`:

```python
from core import secrets
cred = secrets.store("ssh_password", "svc-le", password, site="Mumbai", user=request.user)
with secrets.use(cred, request, reason="ssh facts") as plaintext:   # audited; never log plaintext
    client.connect(host, username=cred.username, password=plaintext)
secrets.mask(cred)        # "svc-le / ••••••••"
```

## Prometheus service discovery (the one-time paste)

LinkedEye serves every enabled, scrape-ready onboarded target of the tenant in Prometheus's
[`http_sd`](https://prometheus.io/docs/prometheus/latest/http_sd/) format at `GET /api/sd/targets.json`.

1. Open **Administration → Service Discovery** (needs the admin role on **every** site, because the endpoint
   serves them all) and press **Generate token**. The token is shown **once**, on that page only.
2. Paste this block into `prometheus.yml` (the page shows it with your URL and, right after generating, your token)
   and reload Prometheus:

   ```yaml
   scrape_configs:
     - job_name: linkedeye-sd
       http_sd_configs:
         - url: https://linkedeye.example.internal/api/sd/targets.json
           refresh_interval: 60s
           authorization:
             type: Bearer
             credentials: <the token>
   ```

3. That is the last time Prometheus is edited. A target you activate in the wizard is picked up on Prometheus's
   next refresh (60 s) and its `up` series then turns the target live.

The endpoint returns, for each target:

```json
[{"targets": ["db-01:9100"],
  "labels": {"job": "node", "site": "Mumbai", "client": "NSE", "env": "prod", "le_target": "12", "le_hostname": "db-01"}}]
```

`job` is `node`, `windows`, `snmp`, `kube-state-metrics`, `blackbox` or `prometheus` by target type. Disable a
target on the Targets step to take it out of discovery without deleting it.

Security properties:

* The bearer token lives in the vault, scoped to the tenant. A missing, wrong, malformed, query-string or
  other-tenant token gets `401` with a fixed body, never a `500`. If the vault key is missing or wrong the
  endpoint answers `503` without detail.
* **Rotate** replaces the token in place and the old one stops working immediately; paste the new one into
  Prometheus straight away.
* Every fetch is recorded (time, source address, user agent, target count, result) and shown on the page;
  records older than 30 days are pruned. Fetches are rate-limited per source address (60 per minute, then `429`).
* The endpoint serves only what Prometheus needs to scrape: addresses and labels, never credentials.

## Where the wizard hands over to the rest of the product

The wizard shell is complete; four functions in `onboarding/steps.py` hand the work to the modules that own it:
`verify_target`, `enable_collection`, `logs_config` and `activate` (all four are wired up). Each docstring is the
contract (idempotent; expected failures are returned as `{"state": ..., "error": ...}`, not raised; no secret in the
result, an error or a log line; probing only through `probes.checks`). Until a function is filled in it raises
`StepPending` and the wizard records a *pending* line in the run log and carries on, so the whole flow can always be
walked.

### Who answers for each target type

| Target type | verify | enable collection |
| --- | --- | --- |
| Linux / Windows server | `servers.verify` (DNS, TCP, `/metrics`, optional SSH facts with a pinned host key) | `servers.enable` (the install command, or an SSH install you tick) |
| Network device (SNMP) | `devices.verify_device` (walks the device through the snmp_exporter) | `devices.enable_device` (saves the module, re-checks, shows the `auths:` block to add to `snmp.yml`) |
| Prometheus server | `prometheus_step.verify_prometheus` (its own API, optional basic auth from the vault) | checks the API; nothing to install |
| Kubernetes node / URL service | `others.verify` (the generic exporter probe) | `others.enable` (Helm commands for node-exporter and kube-state-metrics; nothing to install for a URL) |

When an snmp_exporter address is configured, service discovery serves a device as the exporter's address with the
device in `__param_target` (plus `__param_module`, `__param_auth` and `__metrics_path__=/snmp`) and `instance` set
to the plain device address, so the single `linkedeye-sd` job in `prometheus.yml` needs no `relabel_configs`.
Without an exporter address the device is served as `host:port`.

The Add buttons on Sites, Network devices and Infrastructure open `/onboarding/new/?kind=site|device|server`.
