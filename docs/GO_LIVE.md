# Going live with LinkedEye

This is the runbook for running the real console for real users: a server you control, HTTPS, e-mail sign-in codes, a
scheduler that actually runs, backups, and a way to prove it all works. A public URL cannot be created from a build
environment, so the pack in `deploy/live/` is built to get you from a blank Linux server to a verified, backed-up
installation in minutes, and to tell you plainly what is still open.

| You have | Use | Time |
|---|---|---|
| A Linux VM and a DNS name you control | [Path A](#path-a-one-linux-vm-with-a-domain) | about 10 minutes |
| A Linux VM, no domain (an IP address) | [Path B](#path-b-a-vm-without-a-domain) | about 10 minutes |
| A Kubernetes cluster | [Path C](#path-c-kubernetes) | an hour, mostly reading |
| A laptop, to try it | [Path D](#path-d-your-laptop) | 5 minutes |

Everything below uses these files:

| File | What it is |
|---|---|
| `deploy/live/docker-compose.live.yml` | The stack: **caddy** (HTTPS reverse proxy), **web** (gunicorn), **worker** (`run_probes`, the scheduler), **db** (PostgreSQL 16), one-shot **migrate**; optional profile `observability` (Prometheus, Alertmanager, node-exporter). |
| `deploy/live/.env.live.example` | Every value the stack reads, with generation commands. Copied to `deploy/live/.env` (mode 600) by the bootstrap script. |
| `deploy/live/bootstrap.sh` | Idempotent first start: checks Docker, writes `.env` and generates secrets, builds, migrates, starts, creates the first administrator, runs `go_live_check`. |
| `deploy/live/backup.sh` / `restore.sh` | Database dump plus a copy of `.env` in one archive; restore it on this or a new server. |
| `deploy/live/healthcheck.sh` | Containers and the public URL; exit status 0 = good. For cron and monitors. |
| `deploy/live/update.sh` | Pull, back up, build, migrate, restart, verify; prints rollback steps. |
| `deploy/live/caddy/` | Caddy configuration for the three modes (`https`, `internal`, `http`). |
| `manage.py go_live_check` | Read-only PASS / WARN / FAIL report of the running configuration. The same report is the **Go-live readiness** card on *Administration > System health*. |

## Pre-flight checklist

Do these before you start. Each line says what breaks if you skip it.

- [ ] **A server.** Linux, 2 vCPU, 4 GB RAM, 20 GB disk is a comfortable start for a few hundred monitored targets. Docker Engine
      24+ with the Compose plugin 2.24+ (`docker compose version`). Nothing else on ports 80 and 443.
- [ ] **DNS (Path A).** An `A` (and `AAAA`) record for the name users will type, pointing at the server's public address. Check from
      your laptop: `dig +short linkedeye.example.com`. *Without it Let's Encrypt cannot issue a certificate and Caddy keeps retrying.*
- [ ] **Firewall.** TCP 80 and 443 reachable from the users (and, for Let's Encrypt, from the internet); UDP 443 is optional (HTTP/3).
      Nothing else needs to be open: the database and the console container are not published.
      `sudo ufw allow 80,443/tcp` or your cloud provider's security group. **Docker bypasses `ufw`/`firewalld` for published ports**: a port the
      compose file publishes is reachable whatever `ufw status` says, which is why only Caddy publishes any (and the optional Prometheus and
      Alertmanager ports are bound to 127.0.0.1). Use the cloud security group, or `DOCKER-USER` iptables rules, if you need to restrict who reaches 80/443.
- [ ] **SMTP, for the sign-in code.** By default every sign-in is *password, then a six-digit code by e-mail*. Without a working SMTP server
      nobody can sign in, so `bootstrap.sh` refuses to continue in the HTTPS modes until `LINKEDEYE_EMAIL_HOST` is set (or you choose
      password-only with `LINKEDEYE_LOGIN_OTP=false`, which is acceptable on a trusted network only). You need: host, port (587 with
      STARTTLS is typical), user, password, and a sender address your SMTP server may send as (`LINKEDEYE_DEFAULT_FROM_EMAIL`).
      The first administrator's e-mail address must be a mailbox you can read.
- [ ] **Host names and origins.** `LINKEDEYE_ALLOWED_HOSTS` (what the console answers to), `LINKEDEYE_CSRF_TRUSTED_ORIGINS` (the
      `https://` origin of the sign-in form) and `LINKEDEYE_TRUSTED_PROXIES` (so the audit log and the sign-in throttle see real client
      addresses). `bootstrap.sh` derives all three from `LINKEDEYE_DOMAIN` and `LINKEDEYE_MODE`. Wrong values show up as `400 Bad Request`,
      `403 CSRF verification failed`, or every user sharing one throttle: see [Troubleshooting](#troubleshooting).
- [ ] **Credential key.** `LINKEDEYE_CREDENTIAL_KEY` encrypts the SSH / SNMP / HTTP secrets and the Prometheus login stored by
      Onboarding. `bootstrap.sh` generates one. **Losing it makes those stored secrets unreadable**, so it travels in every backup (`backup.sh`
      copies `.env`) and you should also keep it in your password manager.
- [ ] **Probe allow-list.** Checks against private addresses are refused unless their range is listed in
      `LINKEDEYE_PROBE_ALLOWED_PRIVATE_CIDRS` (for example `10.0.0.0/8,192.168.0.0/16`). Public targets work without it. The worker
      container must also be able to *route* to the networks you monitor.
- [ ] **Prometheus.** Optional but most pages (host metrics, heat map, fleet, ports) read from it. Either start the bundled one
      (`--observability`, below) or set `LINKEDEYE_PROMETHEUS_URL` to your own. Without it those pages say *not connected*.
- [ ] **Where backups go.** A second place (another host, object storage) you will copy `backup.sh` archives to. A backup on the same disk
      is not a backup.

## Path A: one Linux VM with a domain

```bash
# 1. get the code on the server (or copy a release archive there)
git clone <your repository URL> linkedeye && cd linkedeye

# 2. start it: one command
export LINKEDEYE_EMAIL_HOST=smtp.example.com      # also LINKEDEYE_EMAIL_PORT / _HOST_USER / _HOST_PASSWORD / _USE_TLS, copied into .env
./deploy/live/bootstrap.sh --domain linkedeye.example.com --email ops@example.com --admin-email you@example.com
```

`bootstrap.sh` prints each of its eight steps. In order it: checks Docker; writes `deploy/live/.env` with mode 600 and generates
`LINKEDEYE_SECRET_KEY`, `LINKEDEYE_DB_PASSWORD` and `LINKEDEYE_CREDENTIAL_KEY` (only blank values, never an existing one); derives host names
and origins; builds the image; starts the stack (migrations run first); runs `bootstrap_sites`; creates the first administrator; runs
`go_live_check`; checks the public URL; and prints the address. Re-run it any time: it changes nothing that is already right.

*Note on SMTP:* SMTP variables exported in your shell (`LINKEDEYE_EMAIL_HOST`, `_PORT`, `_HOST_USER`, `_HOST_PASSWORD`, `_USE_TLS`,
`LINKEDEYE_DEFAULT_FROM_EMAIL`) are copied into `deploy/live/.env` by `bootstrap.sh`, so they never appear on a command line. You can equally edit
`deploy/live/.env` (`cp deploy/live/.env.live.example deploy/live/.env` first) and run `bootstrap.sh` again, or enter them later on
*Administration > Integrations*.

SMTP passwords and other values with `$`, `#`, spaces or double quotes are written to `.env` single-quoted, which Docker Compose keeps literally; a value
containing a single quote cannot be stored there (enter it on *Administration > Integrations*). If you edit `.env` by hand, quote such values yourself:
`LINKEDEYE_EMAIL_HOST_PASSWORD='p$ss #1'`.

The administrator's password is, in this order: `LINKEDEYE_ADMIN_PASSWORD` from your shell, a prompt, or a generated one printed once. It is never
written to `.env`.

**After it finishes:**

1. Open `https://linkedeye.example.com`. The first request may take a few seconds while Caddy obtains the certificate.
2. Sign in, enter the code that arrives by e-mail.
3. [Verify](#first-hour-verification) and [back up](#day-2-backups-updates-monitoring).
4. Raise HSTS once HTTPS is proven: set `LINKEDEYE_HSTS_SECONDS=31536000` in `.env`, then `./deploy/live/bootstrap.sh`.

**Add the monitoring stack** (Prometheus, Alertmanager, node-exporter, wired to the console, loopback-only ports):
`./deploy/live/bootstrap.sh --observability`.

## Path B: a VM without a domain

Two honest options, depending on who reaches the console.

**B1. HTTPS with Caddy's own certificate authority** (an IP address or an internal name; users get a browser warning until they trust the CA):

```bash
./deploy/live/bootstrap.sh --domain 192.0.2.10 --mode internal --admin-email you@example.com
```

Caddy creates a private root CA on first start. To remove the browser warning, install that root certificate on the users' machines:

```bash
docker compose -f deploy/live/docker-compose.live.yml cp caddy:/data/caddy/pki/authorities/local/root.crt ./linkedeye-root.crt
```

(then import `linkedeye-root.crt` as a trusted root: your OS keychain, or your MDM). `healthcheck.sh` and `go_live_check --insecure --url …` accept
the private CA by themselves in this mode. If users type a name instead of the IP, use that name as `--domain` and map it in your internal DNS.

**B2. Plain HTTP on a trusted network** (a private LAN or a VPN where nobody can sniff the traffic):

```bash
./deploy/live/bootstrap.sh --domain 10.0.0.5 --mode http --admin-email you@example.com
```

`bootstrap.sh` accepts `--mode http` only for localhost, private addresses (10/8, 172.16/12, 192.168/16, 100.64/10 for Tailscale, link-local), IPv6
local addresses and names such as `box`, `*.lan`, `*.local`, `*.internal`, `*.test`; for anything that looks public it stops and asks for `--allow-plain-http`.
For a `localhost` domain the ports are published on `127.0.0.1` only (`LINKEDEYE_BIND_ADDRESS`), so nobody else on the same Wi-Fi can reach it.
`bootstrap.sh --domain <another name>` clears that `127.0.0.1` again (with a warning); a `127.0.0.1` you set yourself for any other name (an SSH
tunnel, a proxy on the same host) is kept on every re-run.
In this mode session cookies are not `Secure` (`LINKEDEYE_SECURE_COOKIES=false`), HSTS is off, and if no SMTP server is set the e-mail code is switched
off with a warning. **Passwords and cookies cross the network in clear text.** Do not use this mode across the internet. `go_live_check` keeps
reporting this as WARN, on purpose, until you move to HTTPS.

## Path C: Kubernetes

The pack in `deploy/kubernetes/` is the supported route; `deploy/live/` is for a single Docker host. Follow *Kubernetes → First installation* in
[DEPLOYMENT.md](DEPLOYMENT.md) (placeholders, namespace and Secret, `kubectl apply -k deploy/kubernetes`). Then, for going live:

| Concern | Where in the Kubernetes pack |
|---|---|
| Host names, origins, proxies | `configmap.yaml`: `LINKEDEYE_ALLOWED_HOSTS`, `LINKEDEYE_CSRF_TRUSTED_ORIGINS`, `LINKEDEYE_TRUSTED_PROXIES` (the ingress controller's pod CIDR). |
| TLS | `ingress.yaml` (cert-manager issuer or a `linkedeye-tls` Secret). |
| HSTS | Set `LINKEDEYE_HSTS_SECONDS` in the ConfigMap, or add the header at the ingress. |
| Credential key, SMTP password | Keys in the `linkedeye-secrets` Secret: `LINKEDEYE_CREDENTIAL_KEY`, `LINKEDEYE_EMAIL_HOST_PASSWORD`. |
| Scheduler | `linkedeye-probes`: exactly one replica, strategy `Recreate`. |
| Backups | `deploy/kubernetes/optional/backup-cronjob.yaml`; keep the Secret's `LINKEDEYE_CREDENTIAL_KEY` in your secret store. |

Verify with the same report the Compose pack uses:

```bash
kubectl -n fs-linkedeye exec deploy/linkedeye-web -- python manage.py go_live_check --url http://localhost:8000
curl -fsS https://linkedeye.example.com/healthz
```

(The `deploy/live` scripts are not used on Kubernetes.)

## Path D: your laptop

Fastest, containers, plain HTTP on localhost:

```bash
./deploy/live/bootstrap.sh --domain localhost --mode http --admin-email you@example.com
# -> http://localhost  (change the port with LINKEDEYE_HTTP_PORT=8080 in deploy/live/.env, then run bootstrap.sh again)
```

Without Docker (development mode, SQLite, no HTTPS, no sign-in code):

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
export LINKEDEYE_DEBUG=true
.venv/bin/python manage.py migrate
.venv/bin/python manage.py createsuperuser
.venv/bin/python manage.py runserver &                # http://127.0.0.1:8000
.venv/bin/python manage.py run_probes                 # the scheduler, in a second terminal
```

`go_live_check` will (correctly) report FAIL for DEBUG on this setup: it is for development only.

## First-hour verification

Run these in order on the server. Each should end well; if one does not, the [troubleshooting table](#troubleshooting) has the usual causes.

```bash
# 1. every container healthy, migrate exited 0, /healthz, sign-in page, static files, HSTS header, HTTP -> HTTPS redirect
./deploy/live/healthcheck.sh

# 2. the configuration report; fix every FAIL, read every WARN
docker compose -f deploy/live/docker-compose.live.yml exec web python manage.py go_live_check --url http://localhost:8000

# 3. the same from outside (run it on your laptop; --insecure only for a private CA)
curl -sS -o /dev/null -w '%{http_code} %{ssl_verify_result}\n' https://linkedeye.example.com/accounts/login/    # 200 0
curl -sSI http://linkedeye.example.com/ | head -3                                                                 # 301/308 -> https
curl -sSI https://linkedeye.example.com/ | grep -i strict-transport

# 4. sign-in really works: send yourself a test mail, then sign in and enter the code
docker compose -f deploy/live/docker-compose.live.yml exec web python manage.py go_live_check --only smtp --send-test-mail you@example.com

# 5. the scheduler is alive: Administration > System health shows a recent heartbeat
docker compose -f deploy/live/docker-compose.live.yml logs --tail=20 worker

# 6. a backup that can be read back, and a copy of it elsewhere
./deploy/live/backup.sh && ls -l deploy/live/backups/
```

Then, in the console: add your sites (*Administration > Sites*), invite users (*Administration > Users*), add the first monitors, connect Prometheus
(*Administration > Integrations*) and watch *Administration > System health* turn green.

## Day 2: backups, updates, monitoring

**Backups.** `./deploy/live/backup.sh` writes `deploy/live/backups/linkedeye-<UTC time>.tar` (mode 600; it contains secrets) holding the PostgreSQL dump
(read back with `pg_restore --list` before it is kept), a copy of `.env`, a manifest and checksums. Uploads are stored in the database, so there is no
separate media directory. `--with-caddy` also archives the certificates. Archives older than `LINKEDEYE_BACKUP_KEEP_DAYS` (default 14) are removed,
but the newest three are always kept.

```cron
# /etc/cron.d/linkedeye-backup   (run as the user that runs docker)
17 1 * * * deploy cd /opt/linkedeye && ./deploy/live/backup.sh --quiet && rclone copy deploy/live/backups remote:linkedeye-backups --max-age 2d
```

`--with-caddy` is for avoiding a fresh Let's Encrypt issuance after a rebuild: `restore.sh` does not unpack `caddy-data.tgz`; to use it, stop caddy and run
`docker run --rm -v linkedeye-live_caddy-data:/data -v "$PWD":/in:ro --entrypoint tar caddy:2.10.0-alpine xzf /in/caddy-data.tgz -C /data` (after extracting it from the archive).
The archive holds the database password and the credential key in clear text: keep it off-host **encrypted** (`rclone crypt`, `age`, or a bucket with
server-side encryption and restricted access).

Test a restore on a scratch server at least once a quarter. **Restore:** `./deploy/live/restore.sh deploy/live/backups/linkedeye-….tar` (asks you to type
`RESTORE`; takes a safety backup first; migrates an older dump to the running release). The database is replaced in **one transaction**: the
`public` schema is emptied (so tables a newer release added, and their foreign keys, cannot block an older dump) and the dump restored into it; if
`pg_restore` fails half-way (a dump from a newer PostgreSQL, a damaged file) everything is rolled back, the emptying included, and the database is exactly as before; web, worker and caddy stay stopped until you start them. `--restore-env` brings back the archived `.env` but
keeps this server's database login, because the PostgreSQL role lives in the database volume and not in the dump. **Disaster recovery on a new server:** install Docker, clone the
repository, copy the archive over, run `./deploy/live/restore.sh linkedeye-….tar --yes`; with no `.env` present it uses the archived one, which
initialises the new database with the same password and brings back the credential key. DNS must point at the new server before Caddy can obtain a
certificate.

**Updates.** `./deploy/live/update.sh` (or `--ref v9.1` for a tag): fast-forward pull, backup, build, migrate, restart, verify. Migrations only move
forward, so a rollback means *old code plus the backup taken here*; the script prints the exact commands and keeps the previous image tagged
`linkedeye-live-rollback:<revision>`.

**Monitoring the console itself.** `./deploy/live/healthcheck.sh --quiet` exits 1 on any problem and prints only the problem; run it from cron or point an
external monitor at `https://your.host/healthz`. Container logs: `docker compose -f deploy/live/docker-compose.live.yml logs -f web worker caddy`
(rotated at 10 MB × 5 files).

**One scheduler only.** `worker` must run exactly once per database. Never `--scale worker=2` and never start a second stack against the same database
(a second instance idles as a standby and shows up as a warning, but it is a mistake).

## Security hardening checklist

- [ ] HTTPS mode, certificate valid, `go_live_check` reports no FAIL; HSTS raised to `31536000` after a week of clean operation.
- [ ] E-mail sign-in code on (`LINKEDEYE_LOGIN_OTP` blank or `true`), SMTP verified with `--send-test-mail`.
- [ ] Administrators have a real e-mail address; day-to-day users are *operators* or *viewers* scoped to their sites (Administration > Users).
- [ ] `deploy/live/.env` is mode 600 and owned by the deploy user; it is in no repository, ticket or chat. Backups are mode 600 and stored encrypted off-host.
- [ ] The containers are separated: only `caddy` publishes ports and it shares a network (`proxy`) with `web` alone, so a compromised proxy process cannot reach PostgreSQL
      or the scheduler. web, worker, migrate and caddy run with `no-new-privileges`, all capabilities dropped (Caddy keeps `NET_BIND_SERVICE`), a read-only root filesystem, a memory
      limit (`LINKEDEYE_WEB_MEMORY` 2g, `_WORKER_MEMORY` 1g, `_CADDY_MEMORY` 512m) and a process limit; web, worker and migrate run as a non-root user. PostgreSQL uses the
      official image's defaults (password authentication, scram-sha-256, not published).
- [ ] Only ports 80 and 443 are open (`ss -tlnp`; Prometheus and Alertmanager listen on 127.0.0.1 only and have no authentication: keep it that way or put them behind the proxy with authentication).
- [ ] `LINKEDEYE_PROBE_ALLOWED_PRIVATE_CIDRS` lists exactly the ranges you monitor, nothing broader.
- [ ] Docker and the host get security updates (`unattended-upgrades`), and you run `update.sh` when a release is published.
- [ ] Rotate secrets after staff changes: the admin password, SMTP password, `LINKEDEYE_SECRET_KEY` (signs everyone out), webhook / ingest tokens, and the
      credential key (prepend the new key, run `manage.py rewrap_credentials`, drop the old key: see DEPLOYMENT.md, *Rotating secrets*).
- [ ] The audit log (Administration > Audit Log) is reviewed, and the backup restore has been tested.

## What `go_live_check` looks at

Run `manage.py go_live_check` (inside the web container for Compose: `docker compose -f deploy/live/docker-compose.live.yml exec web python manage.py go_live_check`).
Options: `--strict` (WARN also exits 1), `--json`, `--only KEY,KEY`, `--skip KEY`, `--list`, `--url https://host` (probe the running site; `--insecure` for a private CA),
`--wait-worker SECONDS`, `--send-test-mail ADDRESS` (the one action that is not read-only). It never prints a secret value.

| Key | PASS means | The usual fix |
|---|---|---|
| `debug` | `LINKEDEYE_DEBUG` is off | `LINKEDEYE_DEBUG=false` |
| `secret_key` | 50+ characters, not a placeholder | `python3 -c 'import secrets; print(secrets.token_urlsafe(50))'` |
| `allowed_hosts` | the public name is listed, no `*` | `LINKEDEYE_ALLOWED_HOSTS` |
| `csrf_origins` | origins have a scheme and match `LINKEDEYE_DOMAIN` | `LINKEDEYE_CSRF_TRUSTED_ORIGINS=https://host` |
| `https_cookies` | session and CSRF cookies are `Secure` | `LINKEDEYE_SECURE_COOKIES=true` and use HTTPS |
| `hsts` | `Strict-Transport-Security` is sent (at least a day) | `LINKEDEYE_HSTS_SECONDS` |
| `trusted_proxies` | proxy addresses valid, none wider than a /8 (an "everyone" network is a FAIL: clients could forge their address), gunicorn trusts the same proxy | `LINKEDEYE_TRUSTED_PROXIES` + `GUNICORN_FORWARDED_ALLOW_IPS` |
| `database` | PostgreSQL or MySQL, reachable, with a password that is not empty, short or a default (SQLite is a WARN) | `LINKEDEYE_DB_*` |
| `migrations` | none pending | `manage.py migrate` |
| `credential_key` | a valid Fernet key is loaded (unset is a WARN, invalid a FAIL) | `LINKEDEYE_CREDENTIAL_KEY` |
| `smtp` | a real SMTP host and a sender it accepts; FAIL when sign-in codes are on but no mail can leave | `LINKEDEYE_EMAIL_*` |
| `otp_mode` | password, then an e-mail code, and every administrator has an address | `LINKEDEYE_LOGIN_OTP` |
| `admin_user` | an active administrator and at least one site | `createsuperuser`, Administration > Sites |
| `prometheus` | the configured Prometheus answers a query | `LINKEDEYE_PROMETHEUS_URL` |
| `worker` | the scheduler's heartbeat is fresh (stale is a FAIL, never seen a WARN) | start / restart `worker` |
| `static_files` | collected static files exist | rebuild the image |
| `probe_allowlist` | private ranges you monitor are listed | `LINKEDEYE_PROBE_ALLOWED_PRIVATE_CIDRS` |
| `alertmanager_token` | webhook token set when Prometheus is connected | `LINKEDEYE_ALERTMANAGER_WEBHOOK_TOKEN` |
| `django_deploy` | Django's own `check --deploy` has no findings besides the ones covered above | `manage.py check --deploy` |
| `example_data` | no example (demo) records remain | *Administration > System health* or `manage.py remove_examples` |
| `sample_pages` | the `/legacy/` design mock-ups (invented numbers) answer 404 | unset `LINKEDEYE_SHOW_SAMPLE_PAGES` |

Before real users arrive, also run `manage.py remove_examples` (dry run by default, `--yes` deletes): see [LIVE_DATA.md](LIVE_DATA.md).

Other apps add checks without editing `administration/golive.py`: a module `golive_checks.py` inside the app registers them with
`administration.golive.register` (the `example_data` row above works this way).

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `bootstrap.sh`: "Let's Encrypt does not issue certificates for IP addresses" | Use `--mode internal` (an IP or an internal name) or `--mode http`. |
| `bootstrap.sh`: "no SMTP server is configured, so nobody could log in" | Set `LINKEDEYE_EMAIL_HOST` (and port, user, password) in `deploy/live/.env` (SMTP saved on *Administration > Integrations* counts too while the console runs), or choose password-only with `LINKEDEYE_LOGIN_OTP=false` on a trusted network, then re-run. In mode `http` a blank `LINKEDEYE_LOGIN_OTP` is switched to `false` instead, but an explicit `true` is never downgraded. |
| Browser: certificate error on `https://…` right after the first start | Caddy is still obtaining the certificate (a few seconds). If it persists: `docker compose -f deploy/live/docker-compose.live.yml logs caddy`; usual causes are DNS not yet pointing at this host, port 80 blocked, or Let's Encrypt rate limits (use `--mode internal` for testing). |
| `400 Bad Request` | The `Host` is not in `LINKEDEYE_ALLOWED_HOSTS`. Fix `LINKEDEYE_DOMAIN` and re-run `bootstrap.sh`. |
| `403 CSRF verification failed` on the sign-in form | `LINKEDEYE_CSRF_TRUSTED_ORIGINS` must be the exact origin the browser uses, including a non-default port. Re-run `bootstrap.sh` after fixing `LINKEDEYE_DOMAIN` / the port. |
| Sign-in returns to the sign-in page | Secure cookies over plain HTTP. Use HTTPS, or mode `http` (which turns `Secure` off). |
| No sign-in code e-mail arrives | `go_live_check --only smtp --send-test-mail you@…`; check spam, the sender address, SMTP user / password / STARTTLS. The web logs say "login OTP email … failed" with the reason. |
| "Too many failed sign-ins" for users who typed the right password | The console sees the proxy's address for everyone: `LINKEDEYE_TRUSTED_PROXIES` / `GUNICORN_FORWARDED_ALLOW_IPS` are missing. The live stack sets both; check `go_live_check --only trusted_proxies`. |
| Pages unstyled | Static files missing: build the image with this repository's Dockerfile. `curl -I https://host/static/linkedeye/app.css` must be 200. |
| `worker` unhealthy | `docker compose -f deploy/live/docker-compose.live.yml logs worker`. A hung pass or a standby (a second scheduler) is described in DEPLOYMENT.md, *The probe scheduler runs exactly once*. |
| "Credential key … not usable" | `LINKEDEYE_CREDENTIAL_KEY` is not a Fernet key. Generate one (`go_live_check` prints the command). If secrets were stored with another key, restore that key first (comma-separated keys are allowed). |
| `Prometheus … not reachable` | With the bundled one use `http://prometheus:9090` and start the profile (`--observability`). The worker and web must be on the same compose network. |
| Checks on private addresses all fail with "refused" | The range is not in `LINKEDEYE_PROBE_ALLOWED_PRIVATE_CIDRS`. |
| `docker compose` complains about a missing variable | You ran compose without `deploy/live/.env`. Run `bootstrap.sh`, or `docker compose -f deploy/live/docker-compose.live.yml --env-file deploy/live/.env …`. |
| Disk filling up | Logs are rotated; check `docker system df` and `deploy/live/backups/`. Lower `LINKEDEYE_BACKUP_KEEP_DAYS`, copy archives off-host. |
| Port 80 or 443 already in use | Stop the other web server, or set `LINKEDEYE_HTTP_PORT` / `LINKEDEYE_HTTPS_PORT` (then Let's Encrypt cannot validate: use `internal` or `http`). In `internal` mode the HTTP-to-HTTPS redirect keeps `LINKEDEYE_HTTPS_PORT`, and `healthcheck.sh` checks it. |

## What is tested, and what is not

**Tested automatically (unit tests, `python -m unittest discover -s deploy/tests`, and `manage.py test administration`):** the compose file's structure
(one scheduler, no published database or web port, forced `DEBUG=false`, required secrets, pinned images, locked-down proxy container), rendering with
`docker compose config` for both profiles, `deploy/validate.py` against deliberately broken copies, the scripts against a stub `docker` (secret generation,
file modes, idempotence, domain and mode handling, refusal cases, backup contents and retention, restore verification, health checks, update ordering), and
every `go_live_check` rule, the command, `--url`, and the System health card, including that no secret value is ever printed.

**Exercised once on a real Docker engine during development:** `bootstrap.sh` (build skipped, plain-HTTP mode, a stand-in proxy because the Caddy image
could not be pulled in that environment) bringing up PostgreSQL, migrate, web and worker; creating the administrator; `go_live_check`; `healthcheck.sh`;
`backup.sh` and `restore.sh` (including restoring onto an empty volume without a `.env`); `update.sh`; backup retention.

**Tested against a real PostgreSQL server** whenever its binaries are installed (`deploy/tests/test_restore_postgres.py`: a private throw-away cluster,
stub `docker` forwarding `pg_dump` / `pg_restore` / `psql` to it): backup, restoring an older dump onto a newer schema whose new tables have foreign
keys into the old ones (the `update.sh` rollback), and restores that fail half-way (rolled back, data intact).

**In CI:** the `live-stack` job builds the image and brings the stack up with the real Caddy image in plain-HTTP mode on a dummy domain, signs in with
curl, runs `go_live_check`, `backup.sh`, `restore.sh` and `update.sh`, and renders the `observability` profile.

**Not tested anywhere yet** (you will be the first, so run the verification section): Let's Encrypt issuance and renewal for a real DNS name; Caddy's internal
CA mode and HTTP-to-HTTPS redirect (the Caddyfiles are small and standard, but no Caddy binary ran them); the observability profile running (only
rendered); a real SMTP server; the Kubernetes pack on a cluster; non-Linux Docker hosts; `shellcheck` on the scripts.
