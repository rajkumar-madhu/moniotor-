# FinSpot dev Kubernetes

## FinSpot dev client (laptop)

One entry point for **FortiClient VPN**, **SSH API tunnel**, **portal `/etc/hosts`**, and **kubectl**:

```bash
cp deploy/overlays/finspot-dev/finspot-access.env.example deploy/overlays/finspot-dev/finspot-access.env
./deploy/overlays/finspot-dev/finspot-dev-client.sh up      # VPN + tunnel + health
./deploy/overlays/finspot-dev/finspot-dev-client.sh hosts # fix old login (Cloudflare DNS)
./deploy/overlays/finspot-dev/finspot-dev-client.sh apply # push overlay when cluster is ready
./deploy/overlays/finspot-dev/finspot-dev-client.sh local-env  # Django + lab Prometheus on VPN
```

Kubeconfig on the laptop: copy bastion admin credentials into
`~/.kube/finspot-dev-tunnel-config` using
`deploy/overlays/finspot-dev/kubeconfig.tunnel.example.yaml` (API
`https://127.0.0.1:18443`). SSH: append
`deploy/overlays/finspot-dev/ssh-config.example` to `~/.ssh/config`, then
`ssh -N finspot-bastion`.

FinSpot **Client** row in LinkedEye (Lesites / imports): code **`finspot`**,
sites **`fs-le-dev-finspot`** (portal **https://fs-le-dev1.finspot.in/**),
**`fs-dr-le`**, **`fs-dx-le`**. Create/update all of them and grant a user:

```bash
python manage.py ensure_finspot_site --all --grant-user admin
# or one site: python manage.py ensure_finspot_site fs-le-dev-finspot --grant-user admin
```

---

LinkedEye on the FinSpot **dev** cluster is deployed with the base manifests under
`deploy/kubernetes/` plus the overlay `deploy/overlays/finspot-dev/`. Namespace
**`fs-linkedeye`**. Portal **https://fs-le-dev1.finspot.in/** (ingress LB `172.16.0.77`).
The overlay pins **web**, **probes**, **migrate**, and **Postgres** to worker **`fs-dev1-le`**
(`172.16.0.74`, `fs-le-dev1.finspot.in`). Lesites slug **`fs-le-dev-finspot`**. Prometheus
`https://fs-le-dev1-prometheus.finspot.in`, and probe private-CIDR allowlists for lab networks.

## VPN (always on for laptop access)

FinSpot lab networks are behind **FortiClient SSL-VPN**:

| Field       | Value                                                  |
| ----------- | ------------------------------------------------------ |
| **Gateway** | `49.249.92.126:10443`                                  |
| **User**    | `you@finspot.in` (corp account)                        |
| **Client**  | FortiClient on macOS (`/Applications/FortiClient.app`) |

**Stay connected:** In FortiClient enable **Connect on startup** / save the VPN password in the **macOS keychain** so reconnect is one click.

One-shot workflow from the repo (no passwords in git):

```bash
cp deploy/overlays/finspot-dev/finspot-access.env.example deploy/overlays/finspot-dev/finspot-access.env
chmod +x deploy/overlays/finspot-dev/finspot-access.sh
./deploy/overlays/finspot-dev/finspot-access.sh
```

The script opens FortiClient if the bastion (`172.16.0.71`) is unreachable, waits until VPN is up, starts `ssh -f -N -L 18443:…`, then runs `kubectl get nodes`.

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

| Item               | FinSpot dev notes                                                                                                                                                                 |
| ------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Image**          | Build, tag, and push to production Harbor (`harbor.finspot.in/linkedeye/linkedeye:9.3` in the overlay).                                                                           |
| **Platform login** | URLs in `deploy/finspot-platform.env.example`; secrets in local `deploy/finspot-platform.env` (gitignored).                                                                       |
| **Secrets**        | Create `linkedeye-secrets` from `secret.template.yaml` in namespace `fs-linkedeye`.                                                                                               |
| **TLS**            | Copy wildcard cert into the namespace, e.g. `kubectl get secret finspot-wildcard-tls -n argus -o yaml \| sed 's/namespace: argus/namespace: fs-linkedeye/' \| kubectl apply -f -` |
| **Ingress**        | Host **`fs-le-dev1.finspot.in`** (DNS or `/etc/hosts` → **`172.16.0.77`**).                                                                                                       |
| **Brand**          | Overlay sets Argus LinkedEye chrome (`LINKEDEYE_BRAND_*`: vendor Argus, suite `argus.itsm`). No third-party vendor name in the UI.                                                |
| **Database**       | Overlay includes Postgres on `fs-dev1-le` (PVC `local-path`, 10Gi).                                                                                                               |
| **Site data**      | After migrate: `ensure_finspot_site fs-le-dev-finspot` and Lesites import (see AGENTS.md).                                                                                        |

## FinSpot platform (production URLs)

| Service     | URL                        | Notes                                                                     |
| ----------- | -------------------------- | ------------------------------------------------------------------------- |
| **Harbor**  | https://harbor.finspot.in  | Container registry; overlay pulls `harbor.finspot.in/linkedeye/linkedeye` |
| **Argo CD** | https://argocd.finspot.in  | GitOps sync for `deploy/overlays/finspot-dev`                             |
| **Jenkins** | https://jenkins.finspot.in | CI build/push pipelines                                                   |

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
# FinSpot nodes are linux/amd64; build on Apple Silicon with --platform linux/amd64.
docker build --platform linux/amd64 -t linkedeye:local .
docker tag linkedeye:local harbor.finspot.in/linkedeye/linkedeye:9.3
docker push harbor.finspot.in/linkedeye/linkedeye:9.3
```

Create the Harbor project `linkedeye` if push reports **401** or “repository not found”. The overlay copies
`finspot-registry1` into `fs-linkedeye` for pulls (same as `finspot-dev`).

## Apply

Map the portal on your laptop until DNS exists:

```bash
grep -q 'fs-le-dev1.finspot.in' /etc/hosts || echo '172.16.0.77 fs-le-dev1.finspot.in' | sudo tee -a /etc/hosts
```

```bash
export KUBECONFIG=~/.kube/finspot-dev-tunnel-config   # ssh -N -L 18443:172.16.0.70:8443 finadmin@172.16.0.71
kubectl get secret finspot-wildcard-tls -n argus -o yaml \
  | sed 's/namespace: argus/namespace: fs-linkedeye/' | kubectl apply -f -
kubectl create secret docker-registry finspot-registry1 \
  --docker-server=harbor.finspot.in --docker-username=admin \
  --docker-password='<from finspot-platform.env>' -n fs-linkedeye \
  --dry-run=client -o yaml | kubectl apply -f -
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

## Worker node knowledge (`fs-dev1-le`)

| Field             | Value                                                                                                    |
| ----------------- | -------------------------------------------------------------------------------------------------------- |
| **Node name**     | `fs-dev1-le` (Kubernetes hostname; use this in `nodeSelector`)                                           |
| **Server name**   | `fs-le-dev1.finspot.in`                                                                                  |
| **Mgmt / SSH IP** | `172.16.0.74`                                                                                            |
| **Pod CIDR**      | `192.168.3.0/24`                                                                                         |
| **Ingress**       | Not on this node — nginx runs on **`fs-dev-cs-tool1`** (`172.16.0.77`).                                  |
| **API / bastion** | API `172.16.0.70:8443`, SSH bastion `172.16.0.71` (`finadmin@…`).                                        |
| **Tunnel (Mac)**  | `ssh -N -L 18443:172.16.0.70:8443 finadmin@172.16.0.71` + `KUBECONFIG=~/.kube/finspot-dev-tunnel-config` |

Pods scheduled on **`fs-dev1-le`** (overlay):

| Workload             | Replicas        | Notes                                         |
| -------------------- | --------------- | --------------------------------------------- |
| `linkedeye-web`      | 1               | Gunicorn; ingress **`fs-le-dev1.finspot.in`** |
| `linkedeye-probes`   | 1 only          | **Never scale** — single scheduler per DB     |
| `linkedeye-postgres` | 1 (StatefulSet) | 10 Gi `local-path` PVC on this node           |
| `linkedeye-migrate`  | Job             | Runs on same node before web/probes start     |

## Continual deployment plan (Mac → cluster)

Run phases in order; repeat **Phase 4** whenever you change app code.

### Phase 0 — Access

```bash
# Keep one tunnel session open
ssh -N -L 18443:172.16.0.70:8443 finadmin@172.16.0.71
export KUBECONFIG=~/.kube/finspot-dev-tunnel-config
kubectl get nodes -l kubernetes.io/hostname=fs-dev1-le
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
# FinSpot nodes are linux/amd64
docker build --platform linux/amd64 --provenance=false --sbom=false -t linkedeye:local .
docker tag linkedeye:local harbor.finspot.in/linkedeye/linkedeye:9.3
docker push harbor.finspot.in/linkedeye/linkedeye:9.3
```

If push fails with **401**, complete Harbor login. If **repository not found**, create project `linkedeye` in Harbor UI.

### Phase 3 — Cluster secrets (once per namespace)

Re-run after namespace wipe:

- `finspot-wildcard-tls`, `finspot-registry1`, `linkedeye-secrets` in `fs-linkedeye`.

### Phase 4 — Apply overlay

```bash
kubectl apply -k deploy/overlays/finspot-dev
kubectl -n fs-linkedeye delete job linkedeye-migrate --ignore-not-found
kubectl apply -k deploy/overlays/finspot-dev   # recreates migrate Job
kubectl -n fs-linkedeye wait --for=condition=complete job/linkedeye-migrate --timeout=15m
kubectl -n fs-linkedeye rollout status deploy/linkedeye-web deploy/linkedeye-probes
kubectl get pods -n fs-linkedeye -o wide   # all on fs-dev1-le, Ready 1/1
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

| Check                   | Command / URL                                                                                                  |
| ----------------------- | -------------------------------------------------------------------------------------------------------------- |
| Pods on worker          | `kubectl get pods -n fs-linkedeye -o wide \| grep fs-dev1-le`                                                  |
| Health (in cluster/VPN) | `curl -sk https://fs-le-dev1.finspot.in/healthz` (with `172.16.0.77 fs-le-dev1.finspot.in` in hosts if needed) |
| Browser URL             | **https://fs-le-dev1.finspot.in/**                                                                             |
| Local console           | http://127.0.0.1:8014/ (parallel stack; separate DB unless you restore dump)                                   |

### Phase 7 — Ongoing updates

1. Rebuild + push image (Phases 1–2).
2. `kubectl -n fs-linkedeye delete job linkedeye-migrate --ignore-not-found && kubectl apply -k deploy/overlays/finspot-dev`
3. Wait for migrate + rollout (Phase 4 tail).

**Rollback:** re-push previous image tag (or retag in Harbor) and repeat Phase 7 with the old tag in `deploy/overlays/finspot-dev/kustomization.yaml` `images.newTag`.

### Old login UI on `fs-le-dev1.finspot.in`

Symptoms: Google-login bootstrap page, `/static/app/css/login_style.css`, ~24 KiB HTML — not the Argus split-panel login.

| Cause                                                                                                                 | What to do                                                                                                                                                                                           |
| --------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Public DNS** points `fs-le-dev1.finspot.in` at Cloudflare, not the lab ingress                                      | For local use, map **`172.16.0.77 fs-le-dev1.finspot.in`** and **`172.16.0.77 fs-le-dev.finspot.in`** in `/etc/hosts` (sudo).                                                                        |
| **Ingress** on `.77` still routes host `fs-le-dev1` to the **legacy** app; only `fs-le-dev` hits `fs-linkedeye` today | From a machine with kube access: `kubectl apply -k deploy/overlays/finspot-dev` then `kubectl -n fs-linkedeye rollout restart deploy/linkedeye-web`. Inspect: `kubectl get ingress -A \| grep dev1`. |
| **Quick check**                                                                                                       | `curl -sk --resolve fs-le-dev1.finspot.in:443:172.16.0.77 https://fs-le-dev1.finspot.in/accounts/login/ \| head -5` — new UI starts with `<html lang="en" class="login-page">`.                      |

Until ingress is updated, use **https://fs-le-dev.finspot.in/** with `/etc/hosts` → `172.16.0.77` for the new UI.

### Old login UI on `fs-le-dev1.finspot.in`

If the login page still loads `/static/app/css/loginpage.css` (legacy carousel UI) instead of `/static/linkedeye/pages/login.css` (Argus split panel), the **hostname is not hitting `linkedeye-web` in `fs-linkedeye`**. Another ingress often still owns `fs-le-dev1.finspot.in`.

Quick check (LB `172.16.0.77`):

```bash
curl -sk --resolve fs-le-dev1.finspot.in:443:172.16.0.77 https://fs-le-dev1.finspot.in/accounts/login/ | head -5
# New UI: <!doctype html> and class="login-page"
# Old UI: id="login-html" and static/app/css/login_style.css
```

**Workaround:** open **https://fs-le-dev.finspot.in/** (same LB; overlay already serves the new image there).

**Fix:** nginx admission blocks two ingresses with the same host + `/`. Legacy **`finspot-dev/le-site`** owned `fs-le-dev1.finspot.in/` → service `le` (old login). Keep its `/websocket` and `/apprise` paths; remove only the root `/` rule, then apply LinkedEye:

```bash
export KUBECONFIG=~/.kube/finspot-dev-tunnel-config
# Drop le-site root path (leave websocket + apprise on fs-le-dev1)
kubectl patch ingress le-site -n finspot-dev --type=json -p='[
  {"op": "replace", "path": "/spec/rules/0/http/paths", "value": [
    {"backend": {"service": {"name": "mq", "port": {"number": 15674}}},
     "path": "/websocket(/|$)(.*)", "pathType": "ImplementationSpecific"},
    {"backend": {"service": {"name": "apprise-notify-service", "port": {"number": 80}}},
     "path": "/apprise(/|$)(.*)", "pathType": "ImplementationSpecific"}
  ]}
]'
kubectl apply -k deploy/overlays/finspot-dev
kubectl get ingress linkedeye -n fs-linkedeye
curl -sk --resolve fs-le-dev1.finspot.in:443:172.16.0.77 https://fs-le-dev1.finspot.in/healthz
```

If GitOps manages `finspot-dev/le-site`, mirror the same path list there so the change is not reverted.

### Current blocker (as of last check)

Cluster **web/probes/migrate** stay in **ImagePullBackOff** until **`harbor.finspot.in/linkedeye/linkedeye:9.3`** exists and `finspot-registry1` can authenticate to that registry. **Postgres** on `fs-dev1-le` is already **Running**.
