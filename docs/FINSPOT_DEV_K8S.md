# FinSpot dev Kubernetes

LinkedEye on the FinSpot **dev** cluster is deployed with the base manifests under
`deploy/kubernetes/` plus the overlay `deploy/kubernetes/overlays/finspot-dev/`. That overlay
sets the **FinSpot DX DR** portal host (`fs-le-dx.finspot.in`, Lesites slug `fs-dx-le`) and probe
private-CIDR allowlists for lab networks (`10.10.0.0/16`, `10.20.0.0/16`, `172.16.0.0/16`).

## Kubeconfig (do not commit credentials)

Admin kubeconfig is issued on the bastion **`fs-dev-cp-ps`**. Keep the real file **off git**:

1. On the bastion, save the admin kubeconfig to a private path, e.g. `~/.kube/config`, mode `600`.
2. On your laptop, copy it similarly or set `KUBECONFIG` to a single file outside the repo.
3. Use `deploy/kubernetes/overlays/finspot-dev/kubeconfig.example.yaml` only as a **shape**
   reference (API server is typically `https://172.16.0.70:8443`).

If client certificate material was shared in chat, email, or tickets, **rotate** cluster client
credentials per your platform runbook.

```bash
export KUBECONFIG=~/.kube/finspot-dev-config   # example
kubectl config current-context
kubectl get nodes
```

## Before `kubectl apply`

Follow [DEPLOYMENT.md](DEPLOYMENT.md) (Kubernetes → First installation). In addition:

| Item | FinSpot dev notes |
|------|-------------------|
| **Image** | Edit `deploy/kubernetes/kustomization.yaml` `images[]` (overlay inherits the base). |
| **Secrets** | Create `linkedeye-secrets` from `secret.template.yaml` in namespace `fs-linkedeye`. |
| **Ingress** | Set host `fs-le-dx.finspot.in` (both Ingress objects) and TLS secret `linkedeye-tls`. |
| **Trusted proxies** | Set `LINKEDEYE_TRUSTED_PROXIES` to the ingress controller pod CIDR (required). |
| **Database** | Point `LINKEDEYE_DB_HOST` at your in-cluster or managed Postgres. |
| **Site data** | After migrate: `ensure_finspot_site fs-dx-le` and Lesites import (see AGENTS.md). |

## Apply

```bash
kubectl apply -k deploy/kubernetes/overlays/finspot-dev
```

Run the migrate Job to completion before expecting web/probes to be healthy.

## Local Docker console (same CIDRs)

For the full console stack on port 8014, you can add the same probe allowlist to `.env`:

```bash
LINKEDEYE_PROBE_ALLOWED_PRIVATE_CIDRS=10.10.0.0/16,10.20.0.0/16,172.16.0.0/16
```

Use `deploy/compose.console.env` for publish port and CSRF origins; see DEPLOYMENT.md (Docker Compose).
