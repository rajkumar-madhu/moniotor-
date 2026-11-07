#!/usr/bin/env bash
# LinkedEye lab: a throwaway SQLite DB, a stub Prometheus, a TCP port for reachability checks, and the REAL web
# server + run_probes worker, all on 127.0.0.1. Never touches db.sqlite3.
#
#   .claude/skills/run-linkedeye/lab.sh up        create the lab DB (first time), start everything, sign in
#   .claude/skills/run-linkedeye/lab.sh login     refresh the session cookie used by driver.mjs and curl
#   .claude/skills/run-linkedeye/lab.sh status    processes, health, device states
#   .claude/skills/run-linkedeye/lab.sh outage    close lab-sw-01's check port (Down after 2 failed checks, ~2 min)
#   .claude/skills/run-linkedeye/lab.sh restore   reopen it (Up again on the next check)
#   .claude/skills/run-linkedeye/lab.sh down      stop everything (keeps the lab DB)
#   .claude/skills/run-linkedeye/lab.sh reset     stop everything and delete the lab directory
#
# Env: LE_RUN_DIR (default $TMPDIR/linkedeye-run), LE_WEB_PORT (8001), LE_PROM_PORT (9099), LE_CHECK_PORT (8099).
set -euo pipefail
SKILL="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT="$(cd "$SKILL/../../.." && pwd)"
TMP="${TMPDIR:-/tmp}"
RUN="${LE_RUN_DIR:-${TMP%/}/linkedeye-run}"   # macOS TMPDIR ends in "/"
RUN="${RUN%/}"
WEB_PORT="${LE_WEB_PORT:-8001}"
PROM_PORT="${LE_PROM_PORT:-9099}"
CHECK_PORT="${LE_CHECK_PORT:-8099}"
PY="$UNIT/.venv/bin/python"
BASE="http://127.0.0.1:$WEB_PORT"

export LINKEDEYE_DEBUG=true                                  # settings refuse to import without it (no secret key)
export LINKEDEYE_DB_NAME="$RUN/lab.sqlite3"                  # throwaway DB, never the repo's db.sqlite3
export LINKEDEYE_PROBE_ALLOWED_PRIVATE_CIDRS=127.0.0.0/8     # lab devices are 127.0.0.1; loopback is refused otherwise
export LINKEDEYE_PROMETHEUS_URL="http://127.0.0.1:$PROM_PORT"
cd "$UNIT"

alive() { [ -f "$RUN/$1.pid" ] && kill -0 "$(cat "$RUN/$1.pid")" 2>/dev/null; }
listening() { lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }

start() {   # start <name> <port> <command...>: background process with pid + log under $RUN
  local name=$1 port=$2
  shift 2
  if alive "$name"; then echo "  $name: already running (pid $(cat "$RUN/$name.pid"))"; return; fi
  if [ "$port" != "-" ] && listening "$port"; then
    echo "ERROR: port $port is used by another process; pick another with LE_*_PORT (see lab.sh header)" >&2
    exit 1
  fi
  nohup "$@" > "$RUN/$name.log" 2>&1 &
  echo $! > "$RUN/$name.pid"
  echo "  $name: started (pid $!, log $RUN/$name.log)"
}

stop() {
  if alive "$1"; then kill "$(cat "$RUN/$1.pid")" && echo "  $1: stopped"; fi
  rm -f "$RUN/$1.pid"
}

setup_db() {
  [ -f "$RUN/lab.sqlite3" ] && return
  echo "creating the lab database $RUN/lab.sqlite3"
  "$PY" manage.py migrate -v0
  "$PY" -c "import secrets; print(secrets.token_urlsafe(12))" > "$RUN/admin.pw"
  DJANGO_SUPERUSER_PASSWORD="$(cat "$RUN/admin.pw")" \
    "$PY" manage.py createsuperuser --noinput --username admin --email admin@example.com > /dev/null
  "$PY" manage.py shell -v0 -c "
from django.conf import settings
from network.models import Device
T = settings.LINKEDEYE_TENANT
Device.objects.get_or_create(tenant_key=T, name='lab-sw-01', defaults=dict(device_type='switch', vendor='Cisco',
    model='C9300-24T', mgmt_ip='127.0.0.1', check_port=$CHECK_PORT, site='Lab', prometheus_instance='lab-sw-01:161'))
Device.objects.get_or_create(tenant_key=T, name='lab-vm-01', defaults=dict(device_type='virtual_machine', vendor='VMware',
    model='Virtual Machine', firmware='Ubuntu 24.04', mgmt_ip='127.0.0.1', check_port=$WEB_PORT, site='Lab',
    prometheus_instance='lab-vm-01:9100', tags='cluster:Compute-01, host:esxi-01, vcpu:4, memory_gb:16'))
"
}

login() {
  local jar="$RUN/cookies.txt" token location
  rm -f "$jar"
  curl -s -c "$jar" -b "$jar" "$BASE/accounts/login/" -o "$RUN/login.html"
  token=$(grep -o 'name="csrfmiddlewaretoken" value="[^"]*"' "$RUN/login.html" | head -1 | sed 's/.*value="//;s/"$//')
  location=$(curl -s -c "$jar" -b "$jar" -o /dev/null -w "%{redirect_url}" -e "$BASE/accounts/login/" \
    --data-urlencode "csrfmiddlewaretoken=$token" --data-urlencode "username=admin" \
    --data-urlencode "password=$(cat "$RUN/admin.pw")" "$BASE/accounts/login/")
  case "$location" in
    "$BASE/"*) case "$location" in *login*) echo "ERROR: sign-in failed (redirected to $location)" >&2; exit 1 ;; esac ;;
    *) echo "ERROR: sign-in failed (no redirect)" >&2; exit 1 ;;
  esac
  echo "  signed in as admin; cookie jar $jar (password in $RUN/admin.pw)"
}

devices() {
  "$PY" manage.py shell -v0 -c "
from django.utils import timezone
from network.models import Device
for d in Device.objects.order_by('pk'):
    seen = timezone.localtime(d.last_checked_at).strftime('%H:%M:%S') if d.last_checked_at else 'never'
    print(f'  {d.pk} {d.name:10} {d.status:8} last check {seen}  interfaces={d.interfaces.count()} '
          f'uptime={d.uptime_seconds} metrics_error={d.metrics_error!r}')
"
}

cmd="${1:-}"
case "$cmd" in
  up)
    mkdir -p "$RUN/empty"
    setup_db
    start prometheus "$PROM_PORT" "$PY" "$SKILL/stub_prometheus.py" "$PROM_PORT"
    start checkport "$CHECK_PORT" "$PY" -m http.server "$CHECK_PORT" --bind 127.0.0.1 --directory "$RUN/empty"
    start web "$WEB_PORT" "$PY" manage.py runserver "127.0.0.1:$WEB_PORT" --noreload
    start worker - "$PY" manage.py run_probes
    curl -s --retry 30 --retry-connrefused --retry-delay 1 -o /dev/null -w "  web: /healthz %{http_code}\n" "$BASE/healthz"
    login
    echo "  switch: $BASE/network/devices/1/   VM: $BASE/network/devices/2/   live JSON: $BASE/network/devices/1/live/"
    ;;
  login) login ;;
  status)
    for p in prometheus checkport web worker; do
      if alive "$p"; then echo "  $p: running (pid $(cat "$RUN/$p.pid"))"; else echo "  $p: not running"; fi
    done
    curl -s -o /dev/null -w "  web: /healthz %{http_code}\n" "$BASE/healthz" || true
    devices
    ;;
  outage) stop checkport; echo "  lab-sw-01 is now unreachable; the worker marks it Down after 2 failed checks (~2 min)" ;;
  restore)
    mkdir -p "$RUN/empty"
    start checkport "$CHECK_PORT" "$PY" -m http.server "$CHECK_PORT" --bind 127.0.0.1 --directory "$RUN/empty"
    echo "  lab-sw-01 answers again; Up on the next check (<= 60 s)"
    ;;
  down) for p in worker web checkport prometheus; do stop "$p"; done ;;
  reset) for p in worker web checkport prometheus; do stop "$p"; done; rm -rf "$RUN"; echo "  removed $RUN" ;;
  *) sed -n '2,13p' "$0"; exit 2 ;;
esac
