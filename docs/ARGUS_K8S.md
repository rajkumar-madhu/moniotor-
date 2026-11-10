# LinkedEye at https://argus.monitoring.wecrew.in (Kubernetes)

The DNS name `argus.monitoring.wecrew.in` points to `187.127.118.240`, where a Kubernetes cluster's ingress-nginx controller
already listens on ports 80 and 443 (it answers "404" and presents its placeholder certificate until an application is
registered for the name). So LinkedEye is deployed **into that cluster** with the overlay
`deploy/kubernetes/overlays/argus/`, not with Docker Compose. The overlay changes only the host names; the rest is the base
in `deploy/kubernetes/` described in [DEPLOYMENT.md](DEPLOYMENT.md#kubernetes).

What was and was not checked when this kit was written: the files parse, the ingress patch paths exist in the base, the repo's
deployment checks pass. It was **not** rendered with `kustomize` or applied to a cluster (the build sandbox has no cluster and could
not download the tool): run `kubectl kustomize deploy/kubernetes/overlays/argus` and read the output before the first apply.

## The cluster
The target is the **FinSpot dev cluster**, the same one the `finspot-dev` overlay ([FINSPOT_DEV_K8S.md](FINSPOT_DEV_K8S.md)) deploys to.
Its admin kubeconfig is issued on the bastion `fs-dev-cp-ps` (API server on the private address `172.16.0.70:8443`, so it is only
reachable from inside that network). Keep the kubeconfig off git and run every `kubectl` command below from the bastion or a machine
that can reach it. This overlay uses the same namespace (`fs-linkedeye`) as `finspot-dev`; the public host name differs (`argus.monitoring.wecrew.in`
instead of `fs-le-dx.finspot.in`). Unlike `finspot-dev` (which lists its two lab `/16`s) it does **not** pre-list any private probe
range (see step 2): every listed address can be reached by anyone who may configure a probe, so the list is yours to make as narrow
as possible, and the cluster's own API-server and node network (`172.16.0.0/16`) must stay out of it in both overlays. The two
overlays must not be applied to the same cluster at once: they patch the same objects.

## Prerequisites
- `kubectl` with access to the cluster (admin on one namespace is enough: `fs-linkedeye`), Kubernetes 1.27+.
- ingress-nginx (already there) and a way to get a TLS certificate: **cert-manager** with a ClusterIssuer, or a certificate you
  already own for `argus.monitoring.wecrew.in` (or `*.monitoring.wecrew.in`).
- A container registry the cluster can pull from, to hold the LinkedEye image.
- A PostgreSQL: yours, or the optional in-cluster one in the overlay.

## 1. Build and push the image (from a machine with Docker and the repo)
```bash
docker build --build-arg DB_EXTRAS="postgres images ssh apm" -t <registry>/linkedeye:9.0 .
docker push <registry>/linkedeye:9.0
```
`images ssh apm` are optional extras (datasheet photos, install over SSH, APM Redis reader); `vsphere` adds the vCenter snapshot
reader. Then edit `images:` in `deploy/kubernetes/overlays/argus/kustomization.yaml` to that registry path and tag (a digest is better).
Behind a TLS-intercepting proxy see the pip CA note in DEPLOYMENT.md.

## 2. Fill in the two values that cannot be guessed
In `overlays/argus/configmap-patch.yaml`:
- `LINKEDEYE_TRUSTED_PROXIES`: the ingress controller's pod network, `kubectl -n ingress-nginx get pods -o wide`
  (the pods refuse to start while the `<REPLACE ...>` placeholder is there).
- `LINKEDEYE_PROBE_ALLOWED_PRIVATE_CIDRS`: the private subnets of the servers, iDRAC/iLO BMCs, vCenters and devices you monitor
  (probes refuse private addresses otherwise). List the narrowest subnets that cover them (a `/24` or single hosts), never a whole `/16`
  and never the cluster's control-plane or API-server addresses.

The ingress controller's namespace and pod labels in `deploy/kubernetes/networkpolicy.yaml` default to `ingress-nginx` /
`app.kubernetes.io/name=ingress-nginx`; check them with `kubectl get ns` and `kubectl -n ingress-nginx get pods --show-labels`.
Check the IngressClass name too (`kubectl get ingressclass`; the base uses `nginx`).

## 3. Namespace, secrets, certificate
```bash
kubectl apply -f deploy/kubernetes/namespace.yaml
# Optional in-cluster database: uncomment "- postgres.yaml" in the overlay's kustomization.yaml first
./deploy/kubernetes/overlays/argus/create-secrets.sh          # DB_PASSWORD='...' ./create-secrets.sh for your own database
```
`create-secrets.sh` makes `linkedeye-secrets` (secret key, database password, Alertmanager webhook token and the credential
key) without printing the values. **Back up `LINKEDEYE_CREDENTIAL_KEY`** (the script prints the command): the passwords stored for
servers, BMCs and vCenters cannot be decrypted without it.

TLS, one of:
- cert-manager: add `cert-manager.io/cluster-issuer: "<your issuer>"` under `annotations` in both Ingress objects of
  `deploy/kubernetes/ingress.yaml` (the comments show where) and it creates `linkedeye-tls` for you.
- your own certificate: `kubectl -n fs-linkedeye create secret tls linkedeye-tls --cert=fullchain.pem --key=privkey.pem`

## 4. Apply and wait
```bash
kubectl kustomize deploy/kubernetes/overlays/argus | less      # read what will be created
kubectl apply -k deploy/kubernetes/overlays/argus
kubectl -n fs-linkedeye wait --for=condition=complete job/linkedeye-migrate --timeout=15m
kubectl -n fs-linkedeye rollout status deploy/linkedeye-web deploy/linkedeye-probes
kubectl -n fs-linkedeye exec -it deploy/linkedeye-web -- python manage.py createsuperuser
```

## 5. Verify
```bash
curl -I https://argus.monitoring.wecrew.in/healthz           # 200 and a valid certificate (no trailing slash: /healthz/ is a 404)
kubectl -n fs-linkedeye exec deploy/linkedeye-web -- python manage.py go_live_check   # if your image has it (see GO_LIVE.md)
kubectl -n fs-linkedeye logs deploy/linkedeye-probes --tail=50 # the scheduler heartbeat
```
Sign in at https://argus.monitoring.wecrew.in/ . The sign-in mails a one-time code unless `LINKEDEYE_LOGIN_OTP=false`: configure SMTP
(`LINKEDEYE_EMAIL_*` in the ConfigMap and `LINKEDEYE_EMAIL_HOST_PASSWORD` in the Secret) before the first user signs in, or the
code never arrives.

## Changing settings later
The web and probes pods read the ConfigMap at start-up (`envFrom`), so editing `configmap-patch.yaml` and re-applying does **not** restart
them. After changing any setting, restart both: `kubectl -n fs-linkedeye rollout restart deploy/linkedeye-web deploy/linkedeye-probes`.
(The Secret behaves the same way.)

## Updating and rolling back
New image: build, push, change the tag in the overlay, `kubectl apply -k ...`; the migrate Job runs first and the web pods wait for it.
Roll back the pods with `kubectl -n fs-linkedeye rollout undo deploy/linkedeye-web deploy/linkedeye-probes`; a schema migration is not
undone by that, so take a database backup first (`optional/backup-cronjob.yaml`).

## Troubleshooting
| Symptom | Likely cause |
|---|---|
| Browser shows the ingress "404" page | the Ingress host does not match the name, or the Ingress is in another namespace than the Service; `kubectl -n fs-linkedeye describe ingress` |
| Certificate warning ("Fake Certificate") | `linkedeye-tls` is missing or not issued yet; `kubectl -n fs-linkedeye get certificate,secret` |
| 400 "Bad request (400)" | the host is not in `LINKEDEYE_ALLOWED_HOSTS` |
| "CSRF verification failed" after sign-in | `LINKEDEYE_CSRF_TRUSTED_ORIGINS` must be `https://argus.monitoring.wecrew.in` and the ingress must send `X-Forwarded-Proto` |
| Pods in `Init` / `CrashLoopBackOff` right after apply | a `<REPLACE ...>` placeholder is still in the ConfigMap or Secret, or the migrate Job failed: `kubectl -n fs-linkedeye logs job/linkedeye-migrate` |
| Cannot reach the database | the DB pods must carry the label `app.kubernetes.io/name=linkedeye-postgres` (the NetworkPolicy admits only those), and `LINKEDEYE_DB_HOST` must match the Service |
