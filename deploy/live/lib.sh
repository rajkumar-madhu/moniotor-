# Shared helpers for the deploy/live scripts. Sourced, never executed.
# shellcheck shell=bash

LIVE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="$LIVE_DIR/docker-compose.live.yml"
ENV_FILE="$LIVE_DIR/.env"
# shellcheck disable=SC2034  # read by bootstrap.sh, which sources this file
ENV_EXAMPLE="$LIVE_DIR/.env.live.example"

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_DIM=$'\033[2m'; C_OFF=$'\033[0m'
else
  C_OK=""; C_WARN=""; C_ERR=""; C_DIM=""; C_OFF=""
fi

say()  { printf '%s\n' "$*"; }
ok()   { printf '%s[ ok ]%s %s\n' "$C_OK" "$C_OFF" "$*"; }
warn() { printf '%s[warn]%s %s\n' "$C_WARN" "$C_OFF" "$*" >&2; }
note() { printf '%s%s%s\n' "$C_DIM" "$*" "$C_OFF"; }
die()  { printf '%s[fail]%s %s\n' "$C_ERR" "$C_OFF" "$*" >&2; exit "${DIE_CODE:-1}"; }

# docker compose against the live file; .env next to it feeds both ${...} substitution and env_file.
dc() { docker compose -f "$COMPOSE_FILE" "$@"; }

need_cmd() { command -v "$1" >/dev/null 2>&1 || die "$1 is required but was not found in PATH. $2"; }

# ---- .env helpers: values are never put on a command line (they could be secrets) ----------------------------
get_env() {  # get_env KEY [default]  -> value of KEY in .env (empty and missing both give the default)
  local v=""
  if [ -f "$ENV_FILE" ]; then v="$(grep -m1 "^$1=" "$ENV_FILE" | cut -d= -f2- || true)"; fi
  case "$v" in
    \"*\") v="${v#\"}"; v="${v%\"}" ;;
    \'*\') v="${v#\'}"; v="${v%\'}" ;;
  esac
  printf '%s' "${v:-${2:-}}"
}

# Docker Compose reads .env too: an unquoted $ is interpolated, " #" starts a comment, spaces and quotes are mangled.
# Anything that is not plainly safe is written single-quoted (compose keeps those literally). A value containing a single
# quote or a line break cannot be stored that way.
dotenv_quote() {
  case "$1" in
    *[!A-Za-z0-9_@%+=:,./-]*)
      case "$1" in *\'*|*$'\n'*|*$'\r'*) return 1 ;; esac
      printf "'%s'" "$1" ;;
    *) printf '%s' "$1" ;;
  esac
}

set_env() {  # set_env KEY VALUE  -> replace the KEY= line (or append one); keeps mode 600
  local key="$1" val tmp
  val="$(dotenv_quote "$2")" || die "The value for $key contains a single quote or a line break and cannot be written to $ENV_FILE safely. Set it on Administration > Integrations instead."
  tmp="$(mktemp "$ENV_FILE.XXXXXX")"
  K="$key" V="$val" awk 'BEGIN { k = ENVIRON["K"]; v = ENVIRON["V"]; done = 0 }
    index($0, k "=") == 1 { if (!done) { print k "=" v; done = 1 } ; next }
    { print }
    END { if (!done) print k "=" v }' "$ENV_FILE" > "$tmp"
  chmod 600 "$tmp"
  mv "$tmp" "$ENV_FILE"
}

set_env_if_blank() {  # set_env_if_blank KEY VALUE
  [ -n "$(get_env "$1")" ] || set_env "$1" "$2"
}

# ---- random values (no dependency beyond coreutils + base64) ---------------------------------------------------
rand_alnum() {  # rand_alnum LENGTH
  head -c 512 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | cut -c1-"$1"
}
rand_fernet_key() {  # 32 random bytes, url-safe base64: the format cryptography.fernet.Fernet expects
  head -c 32 /dev/urandom | base64 | tr '+/' '-_' | tr -d '\n'
}

# ---- compose helpers ---------------------------------------------------------------------------------------------
container_of() { dc ps -a -q "$1" 2>/dev/null | head -n 1; }

service_state() {  # service_state web -> healthy | unhealthy | starting | running | exited:0 | missing
  local cid status health code
  cid="$(container_of "$1")"
  [ -n "$cid" ] || { echo missing; return; }
  status="$(docker inspect -f '{{.State.Status}}' "$cid" 2>/dev/null || echo missing)"
  health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$cid" 2>/dev/null || true)"
  if [ "$status" = exited ]; then
    code="$(docker inspect -f '{{.State.ExitCode}}' "$cid" 2>/dev/null || echo '?')"
    echo "exited:$code"
  elif [ -n "$health" ]; then echo "$health"
  else echo "$status"; fi
}

# Polling interval and a divisor for every timeout: tests set LIVE_POLL_SECONDS=0.1 and LIVE_TIMEOUT_DIV=100; normal runs leave them alone.
wait_healthy() {  # wait_healthy SERVICE SECONDS
  local svc="$1" deadline state=""
  deadline=$(( $(date +%s) + ${2} / ${LIVE_TIMEOUT_DIV:-1} + 1 ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    state="$(service_state "$svc")"
    case "$state" in
      healthy) return 0 ;;
      exited:*|missing) break ;;
    esac
    sleep "${LIVE_POLL_SECONDS:-3}"
  done
  warn "$svc is '$state' (waited up to $2 s). Look at: docker compose -f deploy/live/docker-compose.live.yml logs $svc"
  return 1
}

# The URL users type, from .env (MODE https|internal|http, DOMAIN, ports).
public_url() {
  local mode domain scheme port default_port
  mode="$(get_env LINKEDEYE_MODE https)"; domain="$(get_env LINKEDEYE_DOMAIN localhost)"
  if [ "$mode" = http ]; then scheme=http; port="$(get_env LINKEDEYE_HTTP_PORT 80)"; default_port=80
  else scheme=https; port="$(get_env LINKEDEYE_HTTPS_PORT 443)"; default_port=443; fi
  if [ "$port" = "$default_port" ]; then printf '%s://%s' "$scheme" "$domain"
  else printf '%s://%s:%s' "$scheme" "$domain" "$port"; fi
}

# Host names and addresses that can only be reached on a private network (loopback, RFC 1918, link-local, CGNAT/Tailscale,
# IPv6 local, reserved suffixes, single-label names). Plain HTTP is acceptable there and nowhere else.
is_private_host() {
  local h a b
  h="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
  case "$h" in
    localhost|*.localhost|*.local|*.internal|*.lan|*.localdomain|*.home.arpa|*.test|*.invalid|*.example|*.intranet|*.corp|*.private) return 0 ;;
    ::1|fe80:*|fc[0-9a-f][0-9a-f]:*|fd[0-9a-f][0-9a-f]:*) return 0 ;;
  esac
  if [[ "$h" =~ ^([0-9]{1,3})\.([0-9]{1,3})\.[0-9]{1,3}\.[0-9]{1,3}$ ]]; then
    a=$((10#${BASH_REMATCH[1]})); b=$((10#${BASH_REMATCH[2]}))
    if [ "$a" -eq 10 ] || [ "$a" -eq 127 ]; then return 0; fi
    if [ "$a" -eq 192 ] && [ "$b" -eq 168 ]; then return 0; fi
    if [ "$a" -eq 172 ] && [ "$b" -ge 16 ] && [ "$b" -le 31 ]; then return 0; fi
    if [ "$a" -eq 169 ] && [ "$b" -eq 254 ]; then return 0; fi
    if [ "$a" -eq 100 ] && [ "$b" -ge 64 ] && [ "$b" -le 127 ]; then return 0; fi
    return 1
  fi
  [[ "$h" != *.* && "$h" != *:* ]]
}

is_loopback_host() {
  local h
  h="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
  case "$h" in localhost|*.localhost|::1|127.*) return 0 ;; *) return 1 ;; esac
}

is_ip_address() { [[ "$1" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || [[ "$1" == *:* ]]; }

timestamp() { date -u +%Y%m%dT%H%M%SZ; }

# Run a command, show its output only when it fails (compose prints one line per container, which buries real problems).
quiet() {
  local out rc=0
  out="$("$@" 2>&1)" || rc=$?
  if [ "$rc" -ne 0 ]; then printf '%s\n' "$out" >&2; fi
  return "$rc"
}
