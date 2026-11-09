FinSpot dev Kustomize overlay moved to **`deploy/overlays/finspot-dev/`** (avoids a Kustomize cycle when the overlay lived under `deploy/kubernetes/`).

Use:

```bash
kubectl apply -k deploy/overlays/finspot-dev
```

See [docs/FINSPOT_DEV_K8S.md](../../../docs/FINSPOT_DEV_K8S.md).
