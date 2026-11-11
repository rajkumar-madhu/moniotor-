#!/usr/bin/env bash
# FinSpot dev laptop client: FortiClient VPN, SSH API tunnel, portal DNS, kubectl, optional local .env.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
INGRESS_IP="${FINSPOT_INGRESS_IP:-172.16.0.77}"
PORTAL_HOST="${FINSPOT_PORTAL_HOST:-fs-le-dev1.finspot.in}"
PORTAL_URL="https://${PORTAL_HOST}/"
LOCAL_ENV_EXAMPLE="$SCRIPT_DIR/.env.finspot-dev.example"
LOCAL_ENV="${FINSPOT_LOCAL_ENV:-$SCRIPT_DIR/.env.finspot-dev.local}"

usage() {
  cat <<EOF
FinSpot dev client (FortiClient + tunnel + lab portal)

  $0 up          VPN wait, SSH tunnel, portal health, kubectl nodes
  $0 portal      Print portal URL and lab health (run 'up' first)
  $0 hosts       Check /etc/hosts; print fix if DNS still points at Cloudflare
  $0 apply       kubectl apply -k deploy/overlays/finspot-dev
  $0 local-env   Show how to run Django locally with FinSpot integrations
  $0 help

Docs: docs/FINSPOT_DEV_K8S.md
EOF
}

hosts_ok() {
  grep -qE "^${INGRESS_IP//./\\.}[[:space:]]+${PORTAL_HOST//./\\.}" /etc/hosts 2>/dev/null
}

portal_health() {
  curl -sk --max-time 8 --resolve "${PORTAL_HOST}:443:${INGRESS_IP}" \
    "https://${PORTAL_HOST}/healthz" 2>/dev/null || true
}

cmd_hosts() {
  local dns
  dns="$(dig +short "$PORTAL_HOST" A 2>/dev/null | head -1 || true)"
  if hosts_ok; then
    echo "OK: /etc/hosts maps ${PORTAL_HOST} -> ${INGRESS_IP}"
  else
    echo "DNS: ${PORTAL_HOST} -> ${dns:-?} (public UI if not ${INGRESS_IP})"
    echo "Fix (VPN on):"
    echo "  echo '${INGRESS_IP} fs-le-dev1.finspot.in fs-le-dev.finspot.in' | sudo tee -a /etc/hosts"
  fi
}

cmd_up() {
  "$SCRIPT_DIR/finspot-access.sh"
  cmd_hosts
  local health
  health="$(portal_health)"
  if [[ "$health" == *'"ok"'* ]]; then
    echo "Portal (lab ingress): ${PORTAL_URL} — LinkedEye health OK"
  else
    echo "Portal health via ${INGRESS_IP}: ${health:-unreachable}"
    echo "Open ${PORTAL_URL} after fixing hosts (see: $0 hosts)"
  fi
}

cmd_portal() {
  cmd_hosts
  portal_health | grep -q '"ok"' && echo "${PORTAL_URL}" || echo "Not healthy yet — run: $0 up"
}

cmd_apply() {
  export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/finspot-dev-tunnel-config}"
  kubectl apply -k "$REPO_ROOT/deploy/overlays/finspot-dev"
  kubectl -n fs-linkedeye rollout status deploy/linkedeye-web --timeout=5m || true
}

cmd_local_env() {
  echo "# FinSpot dev client — local Django against lab Prometheus / gle (VPN + hosts required)"
  echo ""
  if [[ -f "$LOCAL_ENV" ]]; then
    echo "Using: $LOCAL_ENV"
    echo "  cd $REPO_ROOT"
    echo "  set -a && source $LOCAL_ENV && set +a"
  else
    echo "  cp $LOCAL_ENV_EXAMPLE $LOCAL_ENV"
    echo "  # add LINKEDEYE_BACKEND_SUMMARY_TOKEN in $LOCAL_ENV"
    echo "  cd $REPO_ROOT"
    echo "  set -a && source $LOCAL_ENV && set +a"
  fi
  echo "  LINKEDEYE_DEBUG=true .venv/bin/python manage.py migrate"
  echo "  LINKEDEYE_DEBUG=true .venv/bin/python manage.py ensure_finspot_site fs-le-dev-finspot"
  echo "  LINKEDEYE_DEBUG=true .venv/bin/python manage.py runserver 127.0.0.1:8014"
}

main() {
  local cmd="${1:-up}"
  case "$cmd" in
    up) cmd_up ;;
    portal) cmd_portal ;;
    hosts) cmd_hosts ;;
    apply) cmd_apply ;;
    local-env) cmd_local_env ;;
    help|-h|--help) usage ;;
    *) echo "Unknown command: $cmd" >&2; usage >&2; exit 2 ;;
  esac
}

main "$@"
