# FinSpot dev Kubernetes

LinkedEye on the FinSpot **dev** cluster is deployed with the base manifests under
`deploy/kubernetes/` plus the overlay `deploy/overlays/finspot-dev/`. That overlay
pins **web**, **probes**, **migrate**, and **Postgres** to worker node **`fs-le-dev-finspot`**
(`172.16.0.56`) so probes can reach `172.16.0.0/16`, sets the **FinSpot Dev** portal host
(`fs-le-dev.finspot.in`, Lesites slug `fs-le-dev-finspot`), Prometheus
`https://fs-le-dev1-prometheus.finspot.in`, and probe private-CIDR allowlists for lab networks.

## Kubeconfig (do not commit credentials)

Admin kubeconfig is issued on the bastion **`fs-dev-cp-ps`**. Keep the real file **off git**:

1. On the bastion, save the admin kubeconfig to a private path, e.g. `~/.kube/config`, mode `600`.
2. On your laptop, copy it similarly or set `KUBECONFIG` to a single file outside the repo.
3. Use `deploy/overlays/finspot-dev/kubeconfig.example.yaml` only as a **shape**
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
| **Image** | Build, tag, and push to production Harbor (`harbor.finspot.in/linkedeye/linkedeye:9.0` in the overlay). |
| **Platform login** | URLs in `deploy/finspot-platform.env.example`; secrets in local `deploy/finspot-platform.env` (gitignored). |
| **Secrets** | Create `linkedeye-secrets` from `secret.template.yaml` in namespace `fs-linkedeye`. |
| **TLS** | Copy wildcard cert into the namespace, e.g. `kubectl get secret finspot-wildcard-tls -n argus -o yaml \| sed 's/namespace: argus/namespace: fs-linkedeye/' \| kubectl apply -f -` |
| **Ingress** | Overlay uses host `fs-le-dev.finspot.in` (DNS must point at the ingress LB `172.16.0.77` or your front door). |
| **Database** | Overlay includes Postgres on `fs-le-dev-finspot` (PVC `local-path`, 10Gi). |
| **Site data** | After migrate: `ensure_finspot_site fs-le-dev-finspot` and Lesites import (see AGENTS.md). |

## FinSpot platform (production URLs)

| Service | URL | Notes |
|---------|-----|--------|
| **Harbor** | https://harbor.finspot.in | Container registry; overlay pulls `harbor.finspot.in/linkedeye/linkedeye` |
| **Argo CD** | https://argocd.finspot.in | GitOps sync for `deploy/overlays/finspot-dev` |
| **Jenkins** | https://jenkins.finspot.in | CI build/push pipelines |

Store admin passwords in **`deploy/finspot-platform.env`** (copy from `deploy/finspot-platform.env.example`).
Do not commit that file or paste passwords into git, tickets, or chat.

```bash
cp deploy/finspot-platform.env.example deploy/finspot-platform.env
# edit deploy/finspot-platform.env locally
set -a && source deploy/finspot-platform.env && set +a
argocd login "$ARGOCD_SERVER" --username "$ARGOCD_USER" --grpc-web
```

If the cluster’s `finspot-registry1` pull secret was created for another registry host, recreate it
after switching to `harbor.finspot.in` (Harbor → Projects → linkedeye → Robot Account or `kubectl create secret docker-registry`).

## Image (Harbor)

```bash
set -a && source deploy/finspot-platform.env && set +a
echo "$HARBOR_PASSWORD" | docker login "$HARBOR_REGISTRY" -u "$HARBOR_USER" --password-stdin
docker tag linkedeye:local harbor.finspot.in/linkedeye/linkedeye:9.0
docker push harbor.finspot.in/linkedeye/linkedeye:9.0
```

Create the Harbor project `linkedeye` if push reports “repository not found”. The overlay copies
`finspot-registry1` into `fs-linkedeye` for pulls (same as `finspot-dev`).

## Apply

```bash
kubectl get secret finspot-wildcard-tls -n argus -o yaml \
  | sed 's/namespace: argus/namespace: fs-linkedeye/' | kubectl apply -f -
kubectl get secret finspot-registry1 -n finspot-dev -o yaml \
  | sed 's/namespace: finspot-dev/namespace: fs-linkedeye/' | kubectl apply -f -
# linkedeye-secrets: see secret.template.yaml
kubectl apply -k deploy/overlays/finspot-dev
```

Run the migrate Job to completion before expecting web/probes to be healthy.

## Local Docker console (same CIDRs)

For the full console stack on port 8014, you can add the same probe allowlist to `.env`:

```bash
LINKEDEYE_PROBE_ALLOWED_PRIVATE_CIDRS=10.10.0.0/16,10.20.0.0/16,172.16.0.0/16
```

Use `deploy/compose.console.env` for publish port and CSRF origins; see DEPLOYMENT.md (Docker Compose).

## Worker node knowledge (`fs-le-dev-finspot`)

| Field | Value |
|--------|--------|
| **Node name** | `fs-le-dev-finspot` |
| **Mgmt / SSH IP** | `172.16.0.56` |
| **Pod CIDR** | `192.168.5.0/24` |
| **Runtime** | containerd 1.7, Ubuntu 24.04 |
| **Why this node** | On `172.16.0.0/16`; **probes** must run here to poll switches/firewalls in the lab (overlay `nodeSelector`). |
| **Ingress** | Not on this node — nginx runs on **`fs-dev-cs-tool1`** (`172.16.0.77`). |
| **API / bastion** | API `172.16.0.70:8443`, SSH bastion `172.16.0.71` (`finadmin@…`). |
| **Tunnel (Mac)** | `ssh -N -L 18443:172.16.0.70:8443 finadmin@172.16.0.71` + `KUBECONFIG=~/.kube/finspot-dev-tunnel-config` |

Pods scheduled on **`fs-le-dev-finspot`** (overlay):

| Workload | Replicas | Notes |
|----------|----------|--------|
| `linkedeye-web` | 1 | Gunicorn; behind ingress `fs-le-dev.finspot.in` |
| `linkedeye-probes` | 1 only | **Never scale** — single scheduler per DB |
| `linkedeye-postgres` | 1 (StatefulSet) | 10 Gi `local-path` PVC on this node |
| `linkedeye-migrate` | Job | Runs on same node before web/probes start |

## Continual deployment plan (Mac → cluster)

Run phases in order; repeat **Phase 4** whenever you change app code.

### Phase 0 — Access

```bash
# Keep one tunnel session open
ssh -N -L 18443:172.16.0.70:8443 finadmin@172.16.0.71
export KUBECONFIG=~/.kube/finspot-dev-tunnel-config
kubectl get nodes -l kubernetes.io/hostname=fs-le-dev-finspot
```

### Phase 1 — Build (laptop)

```bash
cd /path/to/moniotor-
docker build -t linkedeye:local .
# Local console (optional):
docker-compose -p linkedeye-console --env-file .env --env-file deploy/compose.console.env up -d web probes
```

### Phase 2 — Publish image (Harbor)

```bash
set -a && source deploy/finspot-platform.env && set +a
echo "$HARBOR_PASSWORD" | docker login "$HARBOR_REGISTRY" -u "$HARBOR_USER" --password-stdin
docker tag linkedeye:local harbor.finspot.in/linkedeye/linkedeye:9.0
docker push harbor.finspot.in/linkedeye/linkedeye:9.0
```

If push fails with **401**, complete Harbor login. If **repository not found**, create project `linkedeye` in Harbor UI.

### Phase 3 — Cluster secrets (once per namespace)

Already applied in dev if you followed earlier steps; re-run only after namespace wipe:

- `finspot-wildcard-tls`, `finspot-registry1`, `linkedeye-secrets` in `fs-linkedeye`.

### Phase 4 — Apply overlay

```bash
kubectl apply -k deploy/overlays/finspot-dev
kubectl -n fs-linkedeye delete job linkedeye-migrate --ignore-not-found
kubectl apply -k deploy/overlays/finspot-dev   # recreates migrate Job
kubectl -n fs-linkedeye wait --for=condition=complete job/linkedeye-migrate --timeout=15m
kubectl -n fs-linkedeye rollout status deploy/linkedeye-web deploy/linkedeye-probes
kubectl get pods -n fs-linkedeye -o wide   # all on fs-le-dev-finspot, Ready 1/1
```

### Phase 5 — Bootstrap data

```bash
kubectl -n fs-linkedeye exec -it deploy/linkedeye-web -- python manage.py createsuperuser
kubectl -n fs-linkedeye exec -it deploy/linkedeye-web -- python manage.py ensure_finspot_site fs-le-dev-finspot
# Import workbook: copy xlsx into pod or use UI import (see AGENTS.md).
```

Set **Administration → Integrations → Prometheus** to `https://fs-le-dev1-prometheus.finspot.in` and vault basic auth (`apikey`). Set **`LINKEDEYE_PROMETHEUS_TIMEOUT=30`** when the API is reached over VPN.

After import, align device `instance` labels with live targets:

```bash
docker-compose -p linkedeye-console exec web python manage.py sync_prometheus_instances
```

**SNMP interfaces** (switch/router tabs) need `ifOperStatus` / `if_mib` series in Prometheus (snmp_exporter). Node-exporter targets alone populate VM guest charts (`172.16.0.x:9100`) but not interface inventory.

### Phase 6 — Verify

| Check | Command / URL |
|--------|----------------|
| Pods on worker | `kubectl get pods -n fs-linkedeye -o wide \| grep fs-le-dev-finspot` |
| Health (in cluster/VPN) | `curl -sk https://fs-le-dev.finspot.in/healthz -H 'Host: fs-le-dev.finspot.in'` |
| Local console | http://127.0.0.1:8014/ (parallel stack; separate DB unless you restore dump) |

### Phase 7 — Ongoing updates

1. Rebuild + push image (Phases 1–2).
2. `kubectl -n fs-linkedeye delete job linkedeye-migrate --ignore-not-found && kubectl apply -k deploy/overlays/finspot-dev`
3. Wait for migrate + rollout (Phase 4 tail).

**Rollback:** re-push previous image tag (or retag in Harbor) and repeat Phase 7 with the old tag in `deploy/overlays/finspot-dev/kustomization.yaml` `images.newTag`.

### Current blocker (as of last check)

Cluster **web/probes/migrate** stay in **ImagePullBackOff** until **`harbor.finspot.in/linkedeye/linkedeye:9.0`** exists and `finspot-registry1` can authenticate to that registry. **Postgres** on `fs-le-dev-finspot` is already **Running**.
