#!/usr/bin/env bash
# FinSpot dev API is private. From a laptop, forward 18443 -> 172.16.0.70:8443 via bastion .71.
# Prefer ./finspot-access.sh (waits for FortiClient VPN and starts the tunnel automatically).
set -euo pipefail
TUNNEL_PORT="${TUNNEL_PORT:-18443}"
BASTION="${BASTION:-finadmin@172.16.0.71}"
API="${API:-172.16.0.70:8443}"
export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/finspot-dev-tunnel-config}"

if [[ ! -f "$KUBECONFIG" ]]; then
  echo "Missing $KUBECONFIG — copy admin kubeconfig from the bastion and point server at https://127.0.0.1:${TUNNEL_PORT}"
  echo "See docs/FINSPOT_DEV_K8S.md and deploy/overlays/finspot-dev/kubeconfig.example.yaml"
  exit 1
fi

if ! nc -z 127.0.0.1 "$TUNNEL_PORT" 2>/dev/null; then
  echo "API tunnel not listening on 127.0.0.1:${TUNNEL_PORT}."
  echo ""
  echo "In a separate terminal, start SSH (enter password or use your key):"
  echo "  ssh -N -L ${TUNNEL_PORT}:${API} ${BASTION}"
  echo ""
  echo "Then run this script again, or:"
  echo "  export KUBECONFIG=$KUBECONFIG"
  echo "  kubectl get nodes"
  exit 1
fi

kubectl config current-context
kubectl cluster-info
kubectl get nodes -o wide
