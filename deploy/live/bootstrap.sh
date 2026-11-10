#!/usr/bin/env bash
# Bring LinkedEye up live on this Docker host. Safe to run again: existing secrets, data and users are never replaced.
#
#   ./deploy/live/bootstrap.sh --domain linkedeye.example.com --email ops@example.com     # public name, Let's Encrypt
#   ./deploy/live/bootstrap.sh --domain 192.0.2.10 --mode internal                       # IP address, Caddy's own CA
#   ./deploy/live/bootstrap.sh --domain localhost --mode http --yes                      # laptop / trusted network
#   ./deploy/live/bootstrap.sh --prepare-only                                            # write .env and validate; start nothing
#
# What it does: checks Docker -> creates deploy/live/.env (mode 600) and generates every blank secret -> derives the host
# names, trusted origins and cookie settings from the domain and mode -> builds the image -> starts db, migrate, web,
# worker and caddy -> bootstrap_sites -> creates the first administrator -> runs `manage.py go_live_check` -> prints the URL.
# Full runbook: docs/GO_LIVE.md
set -euo pipefail

# shellcheck source=deploy/live/lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

usage() { sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; cat <<'USAGE'

Options:
  --domain NAME        host name or IP users type (LINKEDEYE_DOMAIN)
  --mode MODE          https (default) | internal | http (LINKEDEYE_MODE)
  --email ADDRESS      Let's Encrypt contact (LINKEDEYE_ACME_EMAIL)
  --admin-user NAME    first administrator (default admin)
  --admin-email ADDR   its e-mail address: the sign-in code is sent there
  --observability      also run Prometheus, Alertmanager and node-exporter, and connect the console to them
  --pull               pull LINKEDEYE_IMAGE from its registry instead of building it from this checkout
  --no-build           use the image that already exists locally
  --prepare-only       write and validate deploy/live/.env, then stop (starts nothing)
  --allow-plain-http   with --mode http: accept a host name or address that is not obviously private (a trusted network you know)
  --yes                never prompt; fail instead of asking
  -h, --help           this text
The admin password: LINKEDEYE_ADMIN_PASSWORD in your shell, else a prompt, else a generated one printed once.
USAGE
}

DOMAIN_ARG=""; MODE_ARG=""; EMAIL_ARG=""; ADMIN_USER_ARG=""; ADMIN_EMAIL_ARG=""
OBSERVABILITY=0; PULL=0; BUILD=1; PREPARE_ONLY=0; ASSUME_YES=0; ALLOW_PLAIN_HTTP=0
while [ $# -gt 0 ]; do
  case "$1" in
    --domain) DOMAIN_ARG="${2:?--domain needs a value}"; shift 2 ;;
    --mode) MODE_ARG="${2:?--mode needs a value}"; shift 2 ;;
    --email) EMAIL_ARG="${2:?--email needs a value}"; shift 2 ;;
    --admin-user) ADMIN_USER_ARG="${2:?--admin-user needs a value}"; shift 2 ;;
    --admin-email) ADMIN_EMAIL_ARG="${2:?--admin-email needs a value}"; shift 2 ;;
    --observability) OBSERVABILITY=1; shift ;;
    --pull) PULL=1; BUILD=0; shift ;;
    --no-build) BUILD=0; shift ;;
    --prepare-only) PREPARE_ONLY=1; shift ;;
    --allow-plain-http) ALLOW_PLAIN_HTTP=1; shift ;;
    --yes|-y) ASSUME_YES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "unknown option: $1" ;;
  esac
done

interactive() { [ "$ASSUME_YES" -eq 0 ] && [ -t 0 ] && [ -t 1 ]; }

# ---------------------------------------------------------------------------------------------------------- 1. Docker
say "1/8  Checking Docker"
need_cmd docker "Install Docker Engine 24+ with the Compose plugin: https://docs.docker.com/engine/install/"
compose_version="$(docker compose version --short 2>/dev/null || true)"
[ -n "$compose_version" ] || die "'docker compose' (the Compose v2 plugin) is missing. Install docker-compose-plugin."
compose_version="${compose_version#v}"
major="${compose_version%%.*}"; rest="${compose_version#*.}"; minor="${rest%%.*}"
if [ "$major" -lt 2 ] 2>/dev/null || { [ "$major" -eq 2 ] && [ "$minor" -lt 24 ]; } 2>/dev/null; then
  die "Docker Compose $compose_version is too old; 2.24 or newer is required."
fi
if [ "$PREPARE_ONLY" -eq 0 ]; then
  docker info >/dev/null 2>&1 || die "The Docker daemon is not reachable. Start it, or add your user to the docker group."
fi
ok "Docker Compose $compose_version"

# ---------------------------------------------------------------------------------------------------------- 2. .env
say "2/8  Preparing $ENV_FILE"
umask 077
if [ ! -f "$ENV_FILE" ]; then
  [ -f "$ENV_EXAMPLE" ] || die "$ENV_EXAMPLE is missing."
  cp "$ENV_EXAMPLE" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  ok "created .env from .env.live.example (mode 600)"
else
  chmod 600 "$ENV_FILE"
  ok "using the existing .env (mode 600)"
fi

prev_domain="$(get_env LINKEDEYE_DOMAIN)"          # before --domain replaces it: was this a localhost trial?
[ -z "$DOMAIN_ARG" ]      || set_env LINKEDEYE_DOMAIN "$DOMAIN_ARG"
[ -z "$MODE_ARG" ]        || set_env LINKEDEYE_MODE "$MODE_ARG"
[ -z "$EMAIL_ARG" ]       || set_env LINKEDEYE_ACME_EMAIL "$EMAIL_ARG"
[ -z "$ADMIN_USER_ARG" ]  || set_env LINKEDEYE_ADMIN_USER "$ADMIN_USER_ARG"
[ -z "$ADMIN_EMAIL_ARG" ] || set_env LINKEDEYE_ADMIN_EMAIL "$ADMIN_EMAIL_ARG"

# Values exported in the shell for this run are copied into .env (never taken from the command line, so secrets stay out of ps).
for key in LINKEDEYE_EMAIL_HOST LINKEDEYE_EMAIL_PORT LINKEDEYE_EMAIL_HOST_USER LINKEDEYE_EMAIL_HOST_PASSWORD LINKEDEYE_EMAIL_USE_TLS \
           LINKEDEYE_DEFAULT_FROM_EMAIL LINKEDEYE_LOGIN_OTP LINKEDEYE_PROBE_ALLOWED_PRIVATE_CIDRS LINKEDEYE_PROMETHEUS_URL LINKEDEYE_TIME_ZONE; do
  [ -z "${!key:-}" ] || set_env "$key" "${!key}"
done

ask() {  # ask VAR "question" default -> sets the .env value when interactive
  local key="$1" question="$2" default="$3" answer
  if interactive; then
    read -r -p "$question [$default]: " answer || true
    set_env "$key" "${answer:-$default}"
  fi
}

domain="$(get_env LINKEDEYE_DOMAIN)"
if { [ -z "$domain" ] || [ "$domain" = linkedeye.example.com ]; } && [ -z "$DOMAIN_ARG" ]; then
  ask LINKEDEYE_DOMAIN "Host name or IP users will type" "${domain:-localhost}"
  domain="$(get_env LINKEDEYE_DOMAIN)"
fi
[ -n "$domain" ] && [ "$domain" != linkedeye.example.com ] || die "Set the domain: --domain linkedeye.yourcompany.com (or edit LINKEDEYE_DOMAIN in $ENV_FILE)."
case "$domain" in *[!A-Za-z0-9.:-]*|"") die "LINKEDEYE_DOMAIN '$domain' is not a host name or IP address (no scheme, port or path)." ;; esac

mode="$(get_env LINKEDEYE_MODE https)"
case "$mode" in
  https|internal|http) ;;
  *) die "LINKEDEYE_MODE must be https, internal or http (got '$mode')." ;;
esac
if [ "$mode" = http ] && [ "$ALLOW_PLAIN_HTTP" -eq 0 ] && [ "${LINKEDEYE_ALLOW_PLAIN_HTTP:-}" != true ] && ! is_private_host "$domain"; then
  die "Mode http sends passwords and session cookies in clear text, and '$domain' does not look like a private address or name. Use --mode https (a DNS name)
     or --mode internal (HTTPS with Caddy's own CA). On a network you really trust, add --allow-plain-http."
fi
if [ "$mode" = https ]; then
  if is_ip_address "$domain"; then
    die "Let's Encrypt does not issue certificates for IP addresses. Use --mode internal (HTTPS from Caddy's own CA) or --mode http."
  fi
  case "$domain" in
    localhost|*.local|*.internal|*.lan|*.localdomain|*.home.arpa)
      die "'$domain' is not a public DNS name, so Let's Encrypt cannot issue for it. Use --mode internal or --mode http." ;;
  esac
  [[ "$domain" == *.* ]] || die "'$domain' is not a public DNS name (no dot). Use --mode internal or --mode http."
  acme="$(get_env LINKEDEYE_ACME_EMAIL)"
  if [ -z "$acme" ] || [ "$acme" = ops@example.com ]; then
    ask LINKEDEYE_ACME_EMAIL "E-mail for Let's Encrypt expiry notices" "ops@${domain#*.}"
    acme="$(get_env LINKEDEYE_ACME_EMAIL)"
    [ -n "$acme" ] && [ "$acme" != ops@example.com ] || die "https mode needs a contact: --email you@yourcompany.com"
  fi
fi
# The address is substituted into the Caddyfile as text: only an ordinary mail address may get there.
acme="$(get_env LINKEDEYE_ACME_EMAIL)"
[ -z "$acme" ] || [[ "$acme" =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+$ ]] || die "LINKEDEYE_ACME_EMAIL '$acme' is not a plain e-mail address."

# secrets: only blank lines are filled, an existing value is never touched
set_env_if_blank LINKEDEYE_SECRET_KEY "$(rand_alnum 64)"
set_env_if_blank LINKEDEYE_DB_PASSWORD "$(rand_alnum 32)"
set_env_if_blank LINKEDEYE_CREDENTIAL_KEY "$(rand_fernet_key)"
set_env_if_blank LINKEDEYE_LOG_INGEST_TOKEN "$(rand_alnum 40)"
ok "secrets present (blank ones were generated; existing ones untouched)"

# derived from domain + mode
http_port="$(get_env LINKEDEYE_HTTP_PORT 80)"; https_port="$(get_env LINKEDEYE_HTTPS_PORT 443)"
if [ "$domain" = localhost ]; then set_env LINKEDEYE_ALLOWED_HOSTS "localhost,web"; else set_env LINKEDEYE_ALLOWED_HOSTS "$domain,web,localhost"; fi
if [ "$mode" = http ]; then
  origin="http://$domain"; [ "$http_port" = 80 ] || origin="$origin:$http_port"
  set_env LINKEDEYE_SECURE_COOKIES false
  set_env LINKEDEYE_HSTS_SECONDS 0
else
  origin="https://$domain"; [ "$https_port" = 443 ] || origin="$origin:$https_port"
  set_env LINKEDEYE_SECURE_COOKIES true
  # one day to start with: a wrong HTTPS setup is then forgotten by browsers quickly. Raise it once everything works.
  hsts="$(get_env LINKEDEYE_HSTS_SECONDS)"; { [ -n "$hsts" ] && [ "$hsts" != 0 ]; } || set_env LINKEDEYE_HSTS_SECONDS 86400
fi
set_env LINKEDEYE_CSRF_TRUSTED_ORIGINS "$origin"
# A localhost trial is published on the loopback interface only: on café Wi-Fi nobody else can reach a password-only console.
# Only the 127.0.0.1 this script wrote for such a trial is undone (when the domain moves away from localhost); a 127.0.0.1
# the operator chose for another name (SSH tunnel, a front proxy on this host) is kept: the ports are never widened silently.
if is_loopback_host "$domain"; then set_env LINKEDEYE_BIND_ADDRESS 127.0.0.1
elif [ "$(get_env LINKEDEYE_BIND_ADDRESS)" = 127.0.0.1 ]; then
  if [ -n "$prev_domain" ] && is_loopback_host "$prev_domain"; then
    set_env LINKEDEYE_BIND_ADDRESS ""
    warn "The domain moved from $prev_domain to $domain: LINKEDEYE_BIND_ADDRESS=127.0.0.1 (set for the localhost trial) is cleared,"
    warn "so ports $http_port and $https_port are now published on every interface. Set LINKEDEYE_BIND_ADDRESS in $ENV_FILE to limit that."
  else
    note "Ports are published on 127.0.0.1 only (LINKEDEYE_BIND_ADDRESS in $ENV_FILE): other machines cannot reach $origin."
  fi
fi
ok "mode $mode, address $origin"

# observability wiring
if [ "$OBSERVABILITY" -eq 1 ] || printf '%s' "$(get_env COMPOSE_PROFILES)" | grep -q observability; then
  set_env COMPOSE_PROFILES observability
  set_env_if_blank LINKEDEYE_ALERTMANAGER_WEBHOOK_TOKEN "$(rand_alnum 40)"
  set_env_if_blank LINKEDEYE_PROMETHEUS_URL "http://prometheus:9090"
  ok "observability profile on: Prometheus, Alertmanager, node-exporter; console connected to http://prometheus:9090"
fi

# sign-in code mail: without SMTP nobody could sign in
otp="$(get_env LINKEDEYE_LOGIN_OTP | tr '[:upper:]' '[:lower:]')"
smtp_host="$(get_env LINKEDEYE_EMAIL_HOST)"
case "$otp" in ""|1|true|yes) otp_on=1 ;; *) otp_on=0 ;; esac
is_real_smtp_host() { case "$1" in ""|localhost|127.0.0.1|::1) return 1 ;; *) return 0 ;; esac; }
# SMTP saved on Administration > Integrations lives in the database, not in .env: ask the console when it is already running.
# (values_for_form never returns the password; only the host name is printed.)
integrations_smtp_host() {
  [ "$(service_state web)" = healthy ] || return 0
  dc exec -T web python manage.py shell -c 'from administration import integration_config as ic; print("smtp-host=" + str(ic.values_for_form("email").get("email_host") or "").strip())' \
    2>/dev/null | tr -d '\r' | sed -n 's/^smtp-host=//p' | tail -n 1 || true
}
if [ "$otp_on" -eq 1 ] && ! is_real_smtp_host "$smtp_host"; then
  app_smtp_host="$(integrations_smtp_host)"
  if is_real_smtp_host "$app_smtp_host"; then
    ok "SMTP for the sign-in code: $app_smtp_host (set on Administration > Integrations)"
  elif [ "$mode" = http ] && [ -z "$otp" ]; then
    set_env LINKEDEYE_LOGIN_OTP false
    warn "No SMTP server configured: the e-mail sign-in code is switched OFF (LINKEDEYE_LOGIN_OTP=false). Password only is acceptable"
    warn "on localhost or a trusted network. Set LINKEDEYE_EMAIL_HOST and LINKEDEYE_LOGIN_OTP=true before exposing the console."
  else
    # https/internal, or LINKEDEYE_LOGIN_OTP=true chosen explicitly: never downgraded to password only behind the operator's back
    die "Sign-in needs an e-mail code and no SMTP server is configured, so nobody could log in. Set LINKEDEYE_EMAIL_HOST (and port, user, password) in $ENV_FILE
     (or on Administration > Integrations while the console runs), or consciously accept password-only sign-in with LINKEDEYE_LOGIN_OTP=false
     in $ENV_FILE, then run this script again."
  fi
fi
[ "$otp_on" -eq 1 ] || [ "$mode" = http ] || warn "Sign-in is password only (LINKEDEYE_LOGIN_OTP=false) on a ${mode} deployment."

if [ "$PREPARE_ONLY" -eq 0 ] || [ "${LINKEDEYE_VALIDATE_COMPOSE:-1}" = 1 ]; then
  dc config --quiet || die "docker compose rejected the configuration (see above)."
  ok "docker compose config is valid"
fi
if [ "$PREPARE_ONLY" -eq 1 ]; then
  ok "prepared. Start with: ./deploy/live/bootstrap.sh   (or: docker compose -f deploy/live/docker-compose.live.yml up -d)"
  exit 0
fi

# ---------------------------------------------------------------------------------------------------------- 3. image
say "3/8  Getting the image"
if [ "$PULL" -eq 1 ]; then
  dc pull web || die "could not pull $(get_env LINKEDEYE_IMAGE linkedeye:9.0)"
elif [ "$BUILD" -eq 1 ]; then
  dc build web || die "the image build failed (see above)."
else
  docker image inspect "$(get_env LINKEDEYE_IMAGE linkedeye:9.0)" >/dev/null 2>&1 || die "--no-build, but the image $(get_env LINKEDEYE_IMAGE linkedeye:9.0) does not exist locally."
fi
dc pull --ignore-buildable --quiet 2>/dev/null || warn "could not pull the third-party images (offline?); using the ones already on this host if any."
ok "image ready"

# ---------------------------------------------------------------------------------------------------------- 4. start
say "4/8  Starting db, migrate, web, worker and caddy (the migration runs first)"
dc up -d || die "docker compose up failed (see above)."
wait_healthy db 120 || die "the database did not become healthy."
migrate_state="exited:?"
for _ in $(seq 1 $(( 60 / ${LIVE_TIMEOUT_DIV:-1} + 1 ))); do
  migrate_state="$(service_state migrate)"
  case "$migrate_state" in exited:*) break ;; esac
  sleep "${LIVE_POLL_SECONDS:-3}"
done
[ "$migrate_state" = "exited:0" ] || die "the migration did not finish cleanly ($migrate_state): docker compose -f deploy/live/docker-compose.live.yml logs migrate"
ok "migrations applied"
wait_healthy web 180 || die "web is not healthy."
ok "web healthy"

# ---------------------------------------------------------------------------------------------------------- 5. sites + admin
say "5/8  Sites and the first administrator"
dc exec -T web python manage.py bootstrap_sites | sed 's/^/     /'
admin_user="$(get_env LINKEDEYE_ADMIN_USER admin)"
admin_email="$(get_env LINKEDEYE_ADMIN_EMAIL)"
[[ "$admin_user" =~ ^[A-Za-z0-9_.+-]{1,150}$ ]] || die "LINKEDEYE_ADMIN_USER '$admin_user' may contain only letters, digits and _ . + -"
existing="$(dc exec -T web python manage.py shell -c "from django.contrib.auth import get_user_model as g; U=g().objects; print(U.filter(is_superuser=True).count(), U.filter(username=\"$admin_user\").count())" 2>/dev/null | tail -n 1 || true)"
supers="${existing%% *}"; named="${existing##* }"
generated_password=""
if [ "${supers:-0}" != 0 ] || [ "${named:-0}" != 0 ]; then
  ok "an administrator already exists (not touched)"
else
  { [ -n "$admin_email" ] && [ "$admin_email" != admin@example.com ]; } || {
    if interactive; then read -r -p "E-mail address of the first administrator (receives the sign-in code): " admin_email || true; set_env LINKEDEYE_ADMIN_EMAIL "$admin_email"
    else die "Set the administrator's e-mail address: --admin-email you@yourcompany.com"; fi
  }
  pw="${LINKEDEYE_ADMIN_PASSWORD:-}"
  if [ -z "$pw" ] && interactive; then
    read -r -s -p "Password for $admin_user (blank = generate one): " pw || true; echo
  fi
  if [ -z "$pw" ]; then pw="$(rand_alnum 20)"; generated_password="$pw"; fi
  DJANGO_SUPERUSER_PASSWORD="$pw" dc exec -T -e DJANGO_SUPERUSER_PASSWORD web \
      python manage.py createsuperuser --noinput --username "$admin_user" --email "$admin_email" >/dev/null \
    || die "could not create the administrator."
  ok "administrator '$admin_user' created"
fi

# ---------------------------------------------------------------------------------------------------------- 6. checks
say "6/8  Go-live check (inside the web container)"
check_rc=0
dc exec -T web python manage.py go_live_check --wait-worker "$(( 150 / ${LIVE_TIMEOUT_DIV:-1} + 1 ))" --url http://localhost:8000 || check_rc=$?

say "7/8  Public check (from this host, through the proxy)"
public_rc=0
wait_healthy caddy 90 || true
if "$LIVE_DIR/healthcheck.sh" --quiet; then ok "$(public_url) answers: containers healthy, /healthz, sign-in page, static files"; else public_rc=$?; fi

# ---------------------------------------------------------------------------------------------------------- 8. summary
say "8/8  Done"
url="$(public_url)"
say ""
say "  LinkedEye is running at:  $url"
say "  Sign in as:               $admin_user"
if [ -n "$generated_password" ]; then
  say "  Generated password:       $generated_password      <- shown once; change it under Administration > Users"
fi
say ""
say "  Next:"
say "    * back up now and on a schedule:   ./deploy/live/backup.sh      (docs/GO_LIVE.md: cron line, off-site copy)"
say "    * SMTP, Prometheus, probe ranges:  Administration > Integrations, or deploy/live/.env then re-run this script"
say "    * what is still open:              docker compose -f deploy/live/docker-compose.live.yml exec web python manage.py go_live_check"
[ "$mode" != https ] || say "    * DNS for $domain must point at this host and ports 80/443 must be open, or the certificate cannot be issued"
if [ "$check_rc" -ne 0 ] || [ "$public_rc" -ne 0 ]; then
  warn "Some checks FAILED (see above). The stack is running; fix the items marked FAIL, then run ./deploy/live/healthcheck.sh."
  exit 1
fi
