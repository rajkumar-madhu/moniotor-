#!/usr/bin/env bash
# FinSpot laptop workflow: wait for VPN → start API SSH tunnel → kubectl sanity check.
# Credentials live in FortiClient and SSH only (not in this repo).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${FINSPOT_ACCESS_ENV:-$SCRIPT_DIR/finspot-access.env}"
if [[ -f "$ENV_FILE" ]]; then
  # shellcheck source=/dev/null
  set -a && source "$ENV_FILE" && set +a
fi

BASTION_IP="${FINSPOT_BASTION_IP:-172.16.0.71}"
BASTION_SSH="${FINSPOT_BASTION_SSH:-finadmin@${BASTION_IP}}"
TUNNEL_PORT="${FINSPOT_API_TUNNEL_PORT:-18443}"
API_TARGET="${FINSPOT_API_TARGET:-172.16.0.70:8443}"
VPN_GATEWAY="${FINSPOT_VPN_GATEWAY:-49.249.92.126:10443}"
WAIT_SECS="${FINSPOT_VPN_WAIT_SECS:-180}"
export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/finspot-dev-tunnel-config}"

bastion_reachable() {
  nc -z -G 3 "$BASTION_IP" 22 2>/dev/null
}

tunnel_up() {
  nc -z -G 2 127.0.0.1 "$TUNNEL_PORT" 2>/dev/null
}

ensure_vpn() {
  if bastion_reachable; then
    return 0
  fi
  if [[ "${FINSPOT_AUTO_OPEN_FORTICLIENT:-1}" == "1" ]] && [[ -d /Applications/FortiClient.app ]]; then
    open -a FortiClient
  fi
  echo "VPN required: connect FortiClient SSL-VPN to ${VPN_GATEWAY}"
  echo "  User: your @finspot.in account (save password in FortiClient keychain for one-click reconnect)"
  echo "Waiting for bastion ${BASTION_IP}:22 (up to ${WAIT_SECS}s)..."
  local elapsed=0
  while (( elapsed < WAIT_SECS )); do
    if bastion_reachable; then
      echo "VPN path to bastion is up."
      return 0
    fi
    sleep 2
    elapsed=$((elapsed + 2))
  done
  echo "Bastion still unreachable — connect VPN and re-run: $0" >&2
  return 1
}

ensure_tunnel() {
  if tunnel_up; then
    echo "API tunnel already listening on 127.0.0.1:${TUNNEL_PORT}"
    return 0
  fi
  echo "Starting SSH tunnel: ${TUNNEL_PORT} -> ${API_TARGET} via ${BASTION_SSH}"
  ssh -f -N -o ExitOnForwardFailure=yes \
    -L "127.0.0.1:${TUNNEL_PORT}:${API_TARGET}" \
    "$BASTION_SSH"
  sleep 1
  if ! tunnel_up; then
    echo "Tunnel failed to bind on 127.0.0.1:${TUNNEL_PORT}" >&2
    return 1
  fi
  echo "API tunnel is up."
}

warn_portal_hosts() {
  local ingress_ip="172.16.0.77"
  local first_ip
  first_ip="$(dig +short fs-le-dev1.finspot.in A 2>/dev/null | head -1 || true)"
  local hosts_ok=0
  if grep -qE "^${ingress_ip//./\\.}[[:space:]]+fs-le-dev1\\.finspot\\.in" /etc/hosts 2>/dev/null; then
    hosts_ok=1
  fi
  if [[ "$hosts_ok" != "1" && "$first_ip" != "$ingress_ip" ]]; then
    echo ""
    echo "Portal DNS: fs-le-dev1.finspot.in -> ${first_ip:-?} (public). That serves the LEGACY login."
    echo "On VPN, point the hostname at lab ingress ${ingress_ip} in /etc/hosts, then hard-refresh the browser:"
    echo "  echo '${ingress_ip} fs-le-dev1.finspot.in fs-le-dev.finspot.in' | sudo tee -a /etc/hosts"
    echo "Verify: curl -sk --resolve fs-le-dev1.finspot.in:443:${ingress_ip} https://fs-le-dev1.finspot.in/healthz"
  fi
  if bastion_reachable && curl -sk --max-time 6 --resolve "fs-le-dev1.finspot.in:443:${ingress_ip}" \
    "https://fs-le-dev1.finspot.in/healthz" 2>/dev/null | grep -q '"ok"'; then
    echo "LinkedEye on ingress ${ingress_ip} is up (new UI after /etc/hosts or correct DNS)."
  fi
}

main() {
  ensure_vpn
  ensure_tunnel
  warn_portal_hosts
  if [[ ! -f "$KUBECONFIG" ]]; then
    echo "Missing $KUBECONFIG — see docs/FINSPOT_DEV_K8S.md" >&2
    exit 1
  fi
  kubectl config current-context
  kubectl get nodes -o wide
}

main "$@"
