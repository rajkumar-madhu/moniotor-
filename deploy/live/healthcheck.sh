#!/usr/bin/env bash
# Is the live stack up and answering? Safe to run any time, from cron or a monitor (exit status 0 = all good).
#
#   ./deploy/live/healthcheck.sh                       # URL from deploy/live/.env
#   ./deploy/live/healthcheck.sh https://linkedeye.example.com
#   ./deploy/live/healthcheck.sh --quiet               # print only problems
#   ./deploy/live/healthcheck.sh --insecure            # accept a self-signed certificate (mode internal does this by itself)
#
# Checks: every container (db, web, worker, caddy healthy; migrate exited 0), then over HTTP(S): /healthz (database),
# the sign-in page, a static file, the HTTP -> HTTPS redirect and the Strict-Transport-Security header in the HTTPS modes.
set -uo pipefail
# shellcheck source=deploy/live/lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

QUIET=0; URL=""; INSECURE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --quiet) QUIET=1; shift ;;
    --insecure) INSECURE=1; shift ;;
    -h|--help) sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) die "unknown option: $1" ;;
    *) URL="$1"; shift ;;
  esac
done
need_cmd curl "Install curl."

failures=0
pass() { [ "$QUIET" -eq 1 ] || ok "$*"; }
bad()  { printf '%s[FAIL]%s %s\n' "$C_ERR" "$C_OFF" "$*" >&2; failures=$((failures + 1)); }

mode="$(get_env LINKEDEYE_MODE https)"
[ -n "$URL" ] || URL="$(public_url)"
[ "$mode" != internal ] || INSECURE=1
curl_base=(-sS --max-time 15)
[ "$INSECURE" -eq 0 ] || curl_base+=(-k)

# ---- containers
if docker info >/dev/null 2>&1 && [ -f "$ENV_FILE" ]; then
  services="db web worker caddy"
  printf '%s' "$(get_env COMPOSE_PROFILES)" | grep -q observability && services="$services prometheus alertmanager node-exporter"
  for svc in $services; do
    st="$(service_state "$svc")"
    if [ "$st" = healthy ]; then pass "$svc healthy"; else bad "$svc is '$st' (docker compose -f deploy/live/docker-compose.live.yml logs $svc)"; fi
  done
  st="$(service_state migrate)"
  if [ "$st" = "exited:0" ]; then pass "migrate finished"; else bad "migrate is '$st'"; fi
else
  [ "$QUIET" -eq 1 ] || note "(Docker not reachable here: only the HTTP checks run)"
fi

# ---- HTTP
body="$(curl "${curl_base[@]}" "$URL/healthz" 2>&1)" || true
case "$body" in
  *'"status"'*'"ok"'*) pass "GET $URL/healthz -> ok (database reachable)" ;;
  *) bad "GET $URL/healthz did not answer ok: ${body:-no response} (DNS, firewall, certificate, or LINKEDEYE_ALLOWED_HOSTS?)" ;;
esac

code_of() { curl "${curl_base[@]}" -o /dev/null -w '%{http_code}' "$@" 2>/dev/null || true; }
code="$(code_of "$URL/accounts/login/")"
if [ "$code" = 200 ]; then pass "sign-in page -> 200"; else bad "sign-in page -> HTTP ${code:-000}"; fi
code="$(code_of "$URL/static/linkedeye/app.css")"
if [ "$code" = 200 ]; then pass "static files -> 200"; else bad "static file -> HTTP ${code:-000} (pages would be unstyled)"; fi

if [ "$mode" != http ]; then
  hdrs="$(curl "${curl_base[@]}" -D - -o /dev/null "$URL/accounts/login/" 2>/dev/null | tr -d '\r')" || hdrs=""
  if printf '%s' "$hdrs" | grep -qi '^strict-transport-security:'; then pass "HSTS header present"
  else printf '%s[warn]%s no Strict-Transport-Security header (set LINKEDEYE_HSTS_SECONDS)\n' "$C_WARN" "$C_OFF" >&2; fi
  host="${URL#https://}"; host="${host%%/*}"; host="${host%%:*}"
  if [ "$(get_env LINKEDEYE_HTTP_PORT 80)" = 80 ] && [ "$mode" = https ]; then
    redirect="$(curl -sS --max-time 15 -o /dev/null -w '%{http_code} %{redirect_url}' "http://$host/" 2>/dev/null || true)"
    case "$redirect" in 30[1278]\ https://*) pass "http://$host redirects to HTTPS" ;; *) bad "http://$host does not redirect to HTTPS (got: ${redirect:-no answer})" ;; esac
  fi
fi

if [ "$failures" -eq 0 ]; then
  [ "$QUIET" -eq 1 ] || ok "all checks passed"
  exit 0
fi
exit 1
