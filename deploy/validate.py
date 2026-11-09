#!/usr/bin/env python3
"""Static checks for the deployment bundle (no cluster or Docker daemon needed).

    pip install pyyaml && python deploy/validate.py

Parses every YAML file under deploy/, docker-compose.yml and .github/workflows/, then checks the rules the
deployment depends on: one probe scheduler, health checks on /healthz, non-root / read-only pods, labels and
selectors that line up, placeholder-only secrets, and that every LINKEDEYE_* variable used in a manifest is
a real setting and every setting is documented in docs/DEPLOYMENT.md. Exit status 1 lists every problem.
"""
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("PyYAML is required: pip install pyyaml")

ROOT = Path(__file__).resolve().parents[1]
K8S = ROOT / "deploy" / "kubernetes"
NAMESPACE = "fs-linkedeye"
WORKLOAD_KINDS = {"Deployment", "Job", "StatefulSet", "DaemonSet", "CronJob"}

errors = []


def err(where, msg):
    errors.append(f"{where}: {msg}")


def load_all(path):
    try:
        return [d for d in yaml.safe_load_all(path.read_text()) if d is not None]
    except yaml.YAMLError as exc:
        err(path.relative_to(ROOT), f"invalid YAML: {exc}")
        return []


# ------------------------------------------------------------------ settings: which LINKEDEYE_* names exist
def known_settings():
    text = (ROOT / "linkedeye" / "settings.py").read_text()
    names = set(re.findall(r"\benv(?:_list)?\(\s*\"([A-Z0-9_]+)\"", text))
    # read straight from the environment by telemetry.nagios / administration.services (_setting helper)
    for path in ROOT.glob("*/*.py"):
        if ".venv" in path.parts:
            continue
        try:
            src = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        if 'os.environ.get(f"LINKEDEYE_{name}"' in src:
            names.update(re.findall(r"_setting\(\s*\"([A-Z0-9_]+)\"", src))
    return {f"LINKEDEYE_{n}" for n in names}


SETTINGS = known_settings()
COMPOSE_ONLY = {"LINKEDEYE_IMAGE", "LINKEDEYE_DB_EXTRAS"}
# read by deploy/live (compose, the scripts, go_live_check) and not by settings.py
LIVE_ONLY = {"LINKEDEYE_DOMAIN", "LINKEDEYE_MODE", "LINKEDEYE_ACME_EMAIL", "LINKEDEYE_HTTP_PORT", "LINKEDEYE_HTTPS_PORT",
             "LINKEDEYE_ADMIN_USER", "LINKEDEYE_ADMIN_EMAIL", "LINKEDEYE_ADMIN_PASSWORD", "LINKEDEYE_CADDY_IMAGE",
             "LINKEDEYE_BACKUP_DIR", "LINKEDEYE_BACKUP_KEEP_DAYS", "LINKEDEYE_VALIDATE_COMPOSE", "LINKEDEYE_BIND_ADDRESS",
             "LINKEDEYE_WEB_MEMORY", "LINKEDEYE_WORKER_MEMORY", "LINKEDEYE_APP_MEMORY", "LINKEDEYE_CADDY_MEMORY",
             "LINKEDEYE_ALLOW_PLAIN_HTTP"}


def check_env_names(where, names):
    for name in names:
        if name.startswith("LINKEDEYE_") and name not in SETTINGS | COMPOSE_ONLY:
            err(where, f"{name} is not read by the application (typo?)")


# ------------------------------------------------------------------ Kubernetes
def pod_template(doc):
    spec = doc.get("spec", {})
    if doc.get("kind") == "CronJob":
        spec = spec.get("jobTemplate", {}).get("spec", {})
    return spec.get("template", {})


def pod_spec(doc):
    return pod_template(doc).get("spec", {})


def pod_labels(doc):
    return pod_template(doc).get("metadata", {}).get("labels", {}) or {}


def matches(selector, labels):
    for k, v in (selector.get("matchLabels") or {}).items():
        if labels.get(k) != v:
            return False
    for expr in selector.get("matchExpressions") or []:
        val = labels.get(expr["key"])
        if expr["operator"] == "In" and val not in expr["values"]:
            return False
        if expr["operator"] == "NotIn" and val in expr["values"]:
            return False
        if expr["operator"] == "Exists" and val is None:
            return False
    return True


def check_container(where, c, need_probes=False, app=True):
    sc = c.get("securityContext") or {}
    if sc.get("allowPrivilegeEscalation") is not False:
        err(where, "allowPrivilegeEscalation must be false")
    if sc.get("readOnlyRootFilesystem") is not True:
        err(where, "readOnlyRootFilesystem must be true")
    if "ALL" not in ((sc.get("capabilities") or {}).get("drop") or []):
        err(where, "capabilities.drop must include ALL")
    res = c.get("resources") or {}
    if not res.get("requests") or not (res.get("limits") or {}).get("memory"):
        err(where, "resources.requests and limits.memory are required")
    if not any(m.get("mountPath") == "/tmp" for m in c.get("volumeMounts") or []):
        err(where, "mount a writable emptyDir at /tmp (root filesystem is read-only)")
    refs = {(k, v.get("name")) for e in c.get("envFrom") or [] for k, v in e.items()}
    if app and (("configMapRef", "linkedeye-config") not in refs or ("secretRef", "linkedeye-secrets") not in refs):
        err(where, "envFrom must reference ConfigMap linkedeye-config and Secret linkedeye-secrets")
    if need_probes:
        for probe in ("readinessProbe", "livenessProbe"):
            http = (c.get(probe) or {}).get("httpGet") or {}
            if http.get("path") != "/healthz":
                err(where, f"{probe} must be httpGet /healthz")


def check_workload(d, web=None, app=True):
    """Pod-level and container-level security / resource rules. Returns the image names used."""
    where = f"{d['kind']}/{d['metadata']['name']}"
    spec = pod_spec(d)
    psc = spec.get("securityContext") or {}
    if psc.get("runAsNonRoot") is not True or not psc.get("runAsUser"):
        err(where, "pod securityContext needs runAsNonRoot: true and a runAsUser")
    if (psc.get("seccompProfile") or {}).get("type") != "RuntimeDefault":
        err(where, "pod seccompProfile must be RuntimeDefault")
    if spec.get("automountServiceAccountToken") is not False:
        err(where, "automountServiceAccountToken must be false")
    volumes = {v["name"] for v in spec.get("volumes") or []}
    images = set()
    for c in (spec.get("initContainers") or []) + (spec.get("containers") or []):
        cw = f"{where} container {c.get('name')}"
        check_container(cw, c, need_probes=(d is web and c in spec.get("containers", [])), app=app)
        for m in c.get("volumeMounts") or []:
            if m["name"] not in volumes:
                err(cw, f"mounts undefined volume {m['name']}")
        images.add(image_name(c.get("image", "")))
    return images


def image_name(ref):
    """Repository part of an image reference: drops ":tag" and "@digest", keeps a registry ":port"."""
    ref = ref.split("@", 1)[0]
    head, sep, tail = ref.rpartition(":")
    return head if sep and "/" not in tail else ref


def runs_preflight(container):
    return "healthcheck.py preflight" in " ".join(container.get("command") or []) + " ".join(container.get("args") or [])


def check_optional():
    for path in sorted((K8S / "optional").glob("*.yaml")):
        for d in load_all(path):
            if d.get("metadata", {}).get("namespace") != NAMESPACE:
                err(str(path.relative_to(ROOT)), f"{d.get('kind')} must be in namespace {NAMESPACE}")
            if d.get("kind") in WORKLOAD_KINDS:
                check_workload(d, app=False)
                if d["kind"] == "CronJob" and d["spec"].get("concurrencyPolicy") != "Forbid":
                    err(str(path.relative_to(ROOT)), "backup CronJob must not overlap (concurrencyPolicy: Forbid)")


DATASHEET_PATH = "/network/photos/find/"
DATASHEET_MIN_MB = 26     # network/datasheet_pdf.MAX_PDF_BYTES is 25 MB, plus form overhead


def size_in_mb(value):
    """ingress-nginx proxy-body-size ("3m", "26m", "512k", "1g", plain bytes) in megabytes; None if unreadable."""
    m = re.fullmatch(r"(\d+)\s*([kKmMgG]?)", str(value).strip())
    if not m:
        return None
    return int(m.group(1)) * {"": 1 / 1048576, "k": 1 / 1024, "m": 1, "g": 1024}[m.group(2).lower()]


PROXY_PAIR_MESSAGE = ("GUNICORN_FORWARDED_ALLOW_IPS is set without LINKEDEYE_TRUSTED_PROXIES: behind the proxy every client "
                      "would share the proxy's address (audit source IPs, one shared sign-in throttle for everyone)")


def check_datasheet_ingress(ingresses):
    """Datasheet PDFs (25 MB) are uploaded to /network/photos/find/. A global body limit of a few MB makes the controller
    answer 413 before the application can, so an nginx Ingress must raise the limit for that path."""
    nginx = [i for i in ingresses if (i.get("spec") or {}).get("ingressClassName", "nginx") == "nginx"]
    if not nginx:
        return          # another controller: its own annotations are not checked here
    for ing in nginx:
        for rule in ing["spec"].get("rules", []):
            for p in rule["http"]["paths"]:
                if p["path"].rstrip("/") + "/" == DATASHEET_PATH:
                    size = size_in_mb((ing["metadata"].get("annotations") or {}).get("nginx.ingress.kubernetes.io/proxy-body-size"))
                    if size is not None and size >= DATASHEET_MIN_MB:
                        return
    err("ingress.yaml", f"an nginx Ingress must route {DATASHEET_PATH} with proxy-body-size of at least "
                        f"{DATASHEET_MIN_MB}m (datasheet PDFs are up to 25 MB; a small global limit answers 413)")


def check_kubernetes():
    kust = yaml.safe_load((K8S / "kustomization.yaml").read_text())
    listed = kust.get("resources", [])
    if "secret.template.yaml" in listed:
        err("kustomization.yaml", "the Secret template must not be applied (placeholders only)")
    docs = []
    for name in listed:
        path = K8S / name
        if not path.exists():
            err("kustomization.yaml", f"lists missing file {name}")
            continue
        docs += [(name, d) for d in load_all(path)]
    image_names = {i["name"] for i in kust.get("images", [])}

    by_kind = {}
    for fname, d in docs:
        where = f"{fname} {d.get('kind')}/{d.get('metadata', {}).get('name')}"
        for key in ("apiVersion", "kind", "metadata"):
            if key not in d:
                err(where, f"missing {key}")
        if d.get("kind") != "Namespace" and d.get("metadata", {}).get("namespace") != NAMESPACE:
            err(where, f"metadata.namespace must be {NAMESPACE}")
        by_kind.setdefault(d.get("kind"), []).append(d)

    if [n["metadata"]["name"] for n in by_kind.get("Namespace", [])] != [NAMESPACE]:
        err("namespace.yaml", f"must define exactly the {NAMESPACE} namespace")

    deployments = {d["metadata"]["name"]: d for d in by_kind.get("Deployment", [])}
    web, probes = deployments.get("linkedeye-web"), deployments.get("linkedeye-probes")
    if not web or not probes:
        err("kubernetes", "Deployments linkedeye-web and linkedeye-probes are required")
        return
    if (web["spec"].get("replicas") or 0) < 2:
        err("web-deployment.yaml", "web needs at least 2 replicas")
    if probes["spec"].get("replicas") != 1:
        err("probes-deployment.yaml", "the probe scheduler must run exactly 1 replica")
    if (probes["spec"].get("strategy") or {}).get("type") != "Recreate":
        err("probes-deployment.yaml", "the probe scheduler must use strategy Recreate (no overlapping pods)")
    if any(h["spec"]["scaleTargetRef"]["name"] == "linkedeye-probes" for h in by_kind.get("HorizontalPodAutoscaler", [])):
        err("kubernetes", "never autoscale linkedeye-probes")
    probe_cmd = probes["spec"]["template"]["spec"]["containers"][0].get("command") or []
    if probe_cmd[-1:] != ["run_probes"]:
        err("probes-deployment.yaml", "container must run manage.py run_probes")
    # unedited secret.template.yaml placeholders must stop a release before anything uses them
    for dep in (web, probes):
        if not any(runs_preflight(c) for c in dep["spec"]["template"]["spec"].get("initContainers") or []):
            err(f"Deployment/{dep['metadata']['name']}", "an init container must run healthcheck.py preflight")
    for job in by_kind.get("Job", []):
        if not any(runs_preflight(c) for c in pod_spec(job).get("containers") or []):
            err(f"Job/{job['metadata']['name']}", "must run healthcheck.py preflight before migrating")

    workloads = [d for k in WORKLOAD_KINDS for d in by_kind.get(k, [])]
    images = set()
    for d in workloads:
        images |= check_workload(d, web)
    if len(images) != 1:
        err("kubernetes", f"all containers should run the same image, found {sorted(images)}")
    elif not images <= image_names:
        err("kustomization.yaml", f"images[].name must match the manifests' image {images.pop()}")

    web_labels = pod_labels(web)
    for svc in by_kind.get("Service", []):
        if not matches({"matchLabels": svc["spec"]["selector"]}, web_labels):
            err(f"Service/{svc['metadata']['name']}", "selector does not match the web pods")
    for pdb in by_kind.get("PodDisruptionBudget", []):
        if not matches(pdb["spec"]["selector"], web_labels):
            err(f"PodDisruptionBudget/{pdb['metadata']['name']}", "selector does not match the web pods")
        if matches(pdb["spec"]["selector"], pod_labels(probes)):
            err(f"PodDisruptionBudget/{pdb['metadata']['name']}", "must not cover the single probes pod")
    if not by_kind.get("PodDisruptionBudget"):
        err("kubernetes", "web needs a PodDisruptionBudget")

    services = {s["metadata"]["name"]: s for s in by_kind.get("Service", [])}
    for ing in by_kind.get("Ingress", []):
        if not ing["spec"].get("tls"):
            err(f"Ingress/{ing['metadata']['name']}", "TLS section required")
        for rule in ing["spec"].get("rules", []):
            for p in rule["http"]["paths"]:
                b = p["backend"]["service"]
                s = services.get(b["name"])
                if not s:
                    err(f"Ingress/{ing['metadata']['name']}", f"backend Service {b['name']} not defined")
                elif b["port"].get("name") not in {x.get("name") for x in s["spec"]["ports"]} and \
                        b["port"].get("number") not in {x.get("port") for x in s["spec"]["ports"]}:
                    err(f"Ingress/{ing['metadata']['name']}", "backend port not exposed by the Service")

    check_datasheet_ingress(by_kind.get("Ingress", []))

    pols = {p["metadata"]["name"]: p for p in by_kind.get("NetworkPolicy", [])}
    if not any(p["spec"]["podSelector"] == {} and "Ingress" in p["spec"].get("policyTypes", []) for p in pols.values()):
        err("networkpolicy.yaml", "a default-deny ingress policy is required")
    if not any(matches(p["spec"]["podSelector"], web_labels) and p["spec"].get("ingress") for p in pols.values()):
        err("networkpolicy.yaml", "no policy admits traffic to the web pods")
    db_pol = [p for p in pols.values() if any(
        any(port.get("port") in (5432, 3306) for port in r.get("ports", [])) for r in p["spec"].get("ingress", []))]
    if not db_pol:
        err("networkpolicy.yaml", "no policy admits web/probes to the database")
    else:
        for comp in ("web", "probes", "migrate"):
            labels = {"app.kubernetes.io/name": "linkedeye", "app.kubernetes.io/component": comp}
            if not any(matches(f["podSelector"], labels) for p in db_pol for r in p["spec"]["ingress"]
                       for f in r.get("from", []) if "podSelector" in f):
                err("networkpolicy.yaml", f"{comp} pods cannot reach the database")

    cm = next((c for c in by_kind.get("ConfigMap", []) if c["metadata"]["name"] == "linkedeye-config"), None)
    if not cm:
        err("configmap.yaml", "ConfigMap linkedeye-config missing")
    else:
        data = cm.get("data") or {}
        check_env_names("configmap.yaml", data)
        for k, v in data.items():
            if not isinstance(v, str):
                err("configmap.yaml", f"{k} must be a quoted string")
        if data.get("LINKEDEYE_DEBUG", "").lower() not in ("false", "0", "no"):
            err("configmap.yaml", "LINKEDEYE_DEBUG must be false in production")
        if "GUNICORN_FORWARDED_ALLOW_IPS" in data and "LINKEDEYE_TRUSTED_PROXIES" not in data:
            err("configmap.yaml", PROXY_PAIR_MESSAGE)
        if "LINKEDEYE_SECRET_KEY" in data or "LINKEDEYE_DB_PASSWORD" in data:
            err("configmap.yaml", "secrets belong in the Secret, not the ConfigMap")
        hosts = [h.strip() for h in data.get("LINKEDEYE_ALLOWED_HOSTS", "").split(",")]
        for c in web["spec"]["template"]["spec"]["containers"]:
            for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
                hdrs = ((c.get(probe) or {}).get("httpGet") or {}).get("httpHeaders") or []
                host = next((h["value"] for h in hdrs if h["name"].lower() == "host"), None)
                if host is None:
                    err("web-deployment.yaml", f"{probe} must send a Host header from LINKEDEYE_ALLOWED_HOSTS")
                elif host not in hosts:
                    err("configmap.yaml", f"LINKEDEYE_ALLOWED_HOSTS lacks {host!r} used by the web {probe}")

    secret = load_all(K8S / "secret.template.yaml")
    if len(secret) != 1 or secret[0].get("kind") != "Secret" or secret[0]["metadata"]["name"] != "linkedeye-secrets":
        err("secret.template.yaml", "must hold exactly Secret linkedeye-secrets")
    else:
        values = secret[0].get("stringData") or {}
        check_env_names("secret.template.yaml", values)
        for k, v in values.items():
            if not str(v).startswith("<REPLACE"):
                err("secret.template.yaml", f"{k} must be a <REPLACE ...> placeholder, never a real value")
        for required in ("LINKEDEYE_SECRET_KEY", "LINKEDEYE_DB_PASSWORD"):
            if required not in values:
                err("secret.template.yaml", f"{required} missing")


# ------------------------------------------------------------------ docker compose
def check_compose():
    path = ROOT / "docker-compose.yml"
    docs = load_all(path)
    if not docs:
        return
    services = docs[0].get("services", {})

    def deps_of(name):  # depends_on in short (list) or long (mapping) syntax
        deps = services.get(name, {}).get("depends_on") or {}
        return {d: {} for d in deps} if isinstance(deps, list) else deps

    missing = [name for name in ("db", "web", "probes", "migrate") if name not in services]
    for name in missing:
        err("docker-compose.yml", f"service {name} missing")
    if missing:
        return
    if (services["probes"].get("deploy") or {}).get("replicas") != 1:
        err("docker-compose.yml", "probes must declare deploy.replicas: 1")
    if services["probes"].get("command", [])[-1:] != ["run_probes"]:
        err("docker-compose.yml", "probes must run manage.py run_probes")
    for name in ("db", "web", "probes"):
        hc = services[name].get("healthcheck") or {}
        if hc.get("disable") or not hc.get("test"):
            err("docker-compose.yml", f"{name} needs a healthcheck")
    for name in ("web", "probes", "migrate"):
        if services[name].get("env_file") not in (".env", [".env"]):
            err("docker-compose.yml", f"{name} must use env_file .env")
        if (deps_of(name).get("db") or {}).get("condition") != "service_healthy":
            err("docker-compose.yml", f"{name} must wait for a healthy db")
    for name in ("web", "probes"):
        if (deps_of(name).get("migrate") or {}).get("condition") != "service_completed_successfully":
            err("docker-compose.yml", f"{name} must start after migrate completed")
    if not str(services["db"].get("image", "")).startswith("postgres:16"):
        err("docker-compose.yml", "db must be PostgreSQL 16")
    for name in ("prometheus", "alertmanager", "node-exporter"):
        svc = services.get(name)
        if not svc:
            err("docker-compose.yml", f"observability service {name} missing")
        elif svc.get("profiles") != ["observability"]:
            err("docker-compose.yml", f"{name} must be in the observability profile only")
        elif not svc.get("healthcheck"):
            err("docker-compose.yml", f"{name} needs a healthcheck")
    am_test = " ".join(((services.get("alertmanager") or {}).get("healthcheck") or {}).get("test") or [])
    if services.get("alertmanager") and "/run/secrets/alertmanager_webhook_token" not in am_test:
        err("docker-compose.yml", "alertmanager healthcheck must fail while the webhook token file is missing")
    if "pgdata" not in (docs[0].get("volumes") or {}):
        err("docker-compose.yml", "the database needs the named volume pgdata")
    env_example = ROOT / "deploy" / "compose.env.example"
    names = re.findall(r"^#?\s*(LINKEDEYE_[A-Z0-9_]+)=", env_example.read_text(), re.M)
    check_env_names("deploy/compose.env.example", names)
    check_env_names("docker-compose.yml", re.findall(r"\b(LINKEDEYE_[A-Z0-9_]+)", path.read_text()))
    example_text = env_example.read_text()
    for where, text in (("deploy/compose.env.example", example_text), ("docker-compose.yml", path.read_text())):
        if re.search(r"^\s*#?\s*GUNICORN_FORWARDED_ALLOW_IPS[=:]", text, re.M) and \
                not re.search(r"^\s*#?\s*LINKEDEYE_TRUSTED_PROXIES[=:]", text, re.M):
            err(where, PROXY_PAIR_MESSAGE)
    hosts = re.search(r"^LINKEDEYE_ALLOWED_HOSTS=(.*)$", env_example.read_text(), re.M)
    if not hosts or "web" not in [h.strip() for h in hosts.group(1).split(",")]:
        err("deploy/compose.env.example", "LINKEDEYE_ALLOWED_HOSTS must include 'web' (Alertmanager webhook)")


# ------------------------------------------------------------------ deploy/live (single-host pack with HTTPS proxy)
LIVE = ROOT / "deploy" / "live"
LIVE_SCRIPTS = ["bootstrap.sh", "backup.sh", "restore.sh", "healthcheck.sh", "update.sh"]
LIVE_SECRET_KEYS = ("LINKEDEYE_SECRET_KEY", "LINKEDEYE_DB_PASSWORD", "LINKEDEYE_CREDENTIAL_KEY",
                    "LINKEDEYE_ALERTMANAGER_WEBHOOK_TOKEN", "LINKEDEYE_LOG_INGEST_TOKEN")


def check_live():
    if not LIVE.is_dir():
        err("deploy/live", "directory missing")
        return
    path = LIVE / "docker-compose.live.yml"
    docs = load_all(path) if path.exists() else []
    if not docs:
        err("deploy/live/docker-compose.live.yml", "missing or empty")
        return
    where = "deploy/live/docker-compose.live.yml"
    services = docs[0].get("services", {})
    for name in ("db", "migrate", "web", "worker", "caddy"):
        if name not in services:
            err(where, f"service {name} missing")
    if any(n not in services for n in ("db", "migrate", "web", "worker", "caddy")):
        return

    def deps_of(name):
        deps = services.get(name, {}).get("depends_on") or {}
        return {d: {} for d in deps} if isinstance(deps, list) else deps

    if (services["worker"].get("deploy") or {}).get("replicas") != 1:
        err(where, "worker must declare deploy.replicas: 1 (one scheduler per database)")
    if services["worker"].get("command", [])[-1:] != ["run_probes"]:
        err(where, "worker must run manage.py run_probes")
    for name in ("db", "web", "worker", "caddy"):
        hc = services[name].get("healthcheck") or {}
        if hc.get("disable") or not hc.get("test"):
            err(where, f"{name} needs a healthcheck")
    for name in ("web", "worker", "migrate"):
        if services[name].get("env_file") not in (".env", [".env"]):
            err(where, f"{name} must use env_file .env")
        if (deps_of(name).get("db") or {}).get("condition") != "service_healthy":
            err(where, f"{name} must wait for a healthy db")
    for name in ("web", "worker"):
        if (deps_of(name).get("migrate") or {}).get("condition") != "service_completed_successfully":
            err(where, f"{name} must start after migrate completed")
    if (deps_of("caddy").get("web") or {}).get("condition") != "service_healthy":
        err(where, "caddy must wait for a healthy web")
    published = sorted(n for n, svc in services.items() if svc.get("ports") and "observability" not in (svc.get("profiles") or []))
    if published != ["caddy"]:
        err(where, f"only caddy may publish ports (found {published}); db and web stay on the internal network")
    for n in ("prometheus", "alertmanager"):
        for port in (services.get(n) or {}).get("ports") or []:
            if not str(port).startswith("${"):
                err(where, f"{n} must publish on a configurable loopback address, not {port}")
    app_env = services["web"].get("environment") or {}
    if str(app_env.get("LINKEDEYE_DEBUG")).lower() != "false":
        err(where, "LINKEDEYE_DEBUG must be forced to \"false\" in the web environment")
    for key in ("LINKEDEYE_SECRET_KEY", "LINKEDEYE_DB_PASSWORD", "LINKEDEYE_ALLOWED_HOSTS"):
        if ":?" not in str(app_env.get(key, "")):
            err(where, f"{key} must be required with ${{{key}:?message}} so compose refuses to start without it")
    if "GUNICORN_FORWARDED_ALLOW_IPS" in app_env and "LINKEDEYE_TRUSTED_PROXIES" not in app_env:
        err(where, PROXY_PAIR_MESSAGE)
    if not str(services["db"].get("image", "")).startswith("postgres:16"):
        err(where, "db must be PostgreSQL 16")
    caddy = services["caddy"]
    if "NET_BIND_SERVICE" not in (caddy.get("cap_add") or []) or "ALL" not in (caddy.get("cap_drop") or []):
        err(where, "caddy must drop ALL capabilities and add back only NET_BIND_SERVICE")
    if caddy.get("networks") != ["proxy"]:
        err(where, "caddy must sit on the proxy network alone, so it cannot reach the database or the worker")
    for name in ("db", "worker", "migrate"):
        if "proxy" in (services[name].get("networks") or []):
            err(where, f"{name} must not be on the proxy network")
    for name in ("web", "worker", "migrate", "caddy"):
        if not services[name].get("mem_limit") or not services[name].get("pids_limit"):
            err(where, f"{name} needs mem_limit and pids_limit (a runaway container must not take the host down)")
    for name, svc in services.items():
        image = str(svc.get("image", ""))
        tag = re.sub(r"^\$\{[A-Z_]+:-(.*)\}$", r"\1", image)
        if name not in ("web", "worker", "migrate") and (":" not in tag.rsplit("/", 1)[-1] or tag.endswith(":latest")):
            err(where, f"{name}: image {image!r} must be pinned to a version tag")
    # the observability profile must stay in step with the repository's docker-compose.yml
    base = load_all(ROOT / "docker-compose.yml")
    base_services = base[0].get("services", {}) if base else {}
    for name in ("prometheus", "alertmanager", "node-exporter"):
        svc = services.get(name)
        if not svc:
            err(where, f"observability service {name} missing")
            continue
        if svc.get("profiles") != ["observability"]:
            err(where, f"{name} must be in the observability profile only")
        if not svc.get("healthcheck"):
            err(where, f"{name} needs a healthcheck")
        if base_services.get(name) and base_services[name].get("image") != svc.get("image"):
            err(where, f"{name} image differs from docker-compose.yml ({svc.get('image')} vs {base_services[name].get('image')})")
    am_test = " ".join(((services.get("alertmanager") or {}).get("healthcheck") or {}).get("test") or [])
    if "/run/secrets/alertmanager_webhook_token" not in am_test:
        err(where, "alertmanager healthcheck must fail while the webhook token file is missing")
    for vol in ("pgdata", "caddy-data"):
        if vol not in (docs[0].get("volumes") or {}):
            err(where, f"named volume {vol} missing")

    # Caddy: one Caddyfile per mode, all through the shared snippet to web:8000
    snippet = LIVE / "caddy" / "linkedeye.snippet"
    if not snippet.exists() or "reverse_proxy web:8000" not in snippet.read_text():
        err("deploy/live/caddy/linkedeye.snippet", "must reverse_proxy to web:8000")
    for mode, needle in (("https", "{$LINKEDEYE_DOMAIN}"), ("internal", "local_certs"), ("http", "auto_https off")):
        f = LIVE / "caddy" / f"{mode}.Caddyfile"
        if not f.exists():
            err(f"deploy/live/caddy/{mode}.Caddyfile", "missing")
        elif needle not in f.read_text() or "import linkedeye.snippet" not in f.read_text():
            err(f"deploy/live/caddy/{mode}.Caddyfile", f"must contain {needle!r} and import linkedeye.snippet")
    if "caddy/" not in (path.read_text()):
        err(where, "caddy must mount ./caddy")

    # the environment template
    example = LIVE / ".env.live.example"
    if not example.exists():
        err("deploy/live/.env.live.example", "missing")
        return
    text = example.read_text()
    names = re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", text, re.M)
    for n in names:
        if n.startswith("LINKEDEYE_") and n not in SETTINGS | COMPOSE_ONLY | LIVE_ONLY:
            err("deploy/live/.env.live.example", f"{n} is not read by the application (typo?)")
    check_env_names(where, [n for n in re.findall(r"\b(LINKEDEYE_[A-Z0-9_]+)", path.read_text()) if n not in LIVE_ONLY])
    for key in LIVE_SECRET_KEYS:
        m = re.search(rf"^{key}=(.*)$", text, re.M)
        if not m:
            err("deploy/live/.env.live.example", f"{key} missing")
        elif m.group(1).strip():
            err("deploy/live/.env.live.example", f"{key} must be blank (bootstrap.sh generates it), never a value")
    for required in ("LINKEDEYE_DOMAIN", "LINKEDEYE_MODE", "LINKEDEYE_ACME_EMAIL", "LINKEDEYE_ALLOWED_HOSTS",
                     "LINKEDEYE_CSRF_TRUSTED_ORIGINS", "LINKEDEYE_SECURE_COOKIES", "LINKEDEYE_HSTS_SECONDS", "LINKEDEYE_LOGIN_OTP"):
        if not re.search(rf"^{required}=", text, re.M):
            err("deploy/live/.env.live.example", f"{required} missing")
    if re.search(r"^LINKEDEYE_ADMIN_PASSWORD=", text, re.M):
        err("deploy/live/.env.live.example", "the admin password must not live in .env (it would reach the containers' environment)")

    # the scripts
    for name in LIVE_SCRIPTS + ["lib.sh"]:
        f = LIVE / name
        if not f.exists():
            err(f"deploy/live/{name}", "missing")
            continue
        body = f.read_text()
        if name != "lib.sh":
            if not body.startswith("#!/usr/bin/env bash\n"):
                err(f"deploy/live/{name}", "must start with #!/usr/bin/env bash")
            if not re.search(r"^set -[a-z]*u[a-z]*o pipefail|^set -euo pipefail", body, re.M):
                err(f"deploy/live/{name}", "must set -u and pipefail")
            if not f.stat().st_mode & 0o111:
                err(f"deploy/live/{name}", "must be executable")
        if re.search(r"(?<![A-Za-z_])eval\s", body):
            err(f"deploy/live/{name}", "must not use eval")
        if re.search(r"rm\s+-[a-z]*r[a-z]*f?\s+(/|\$HOME|~)\s*$", body, re.M):
            err(f"deploy/live/{name}", "dangerous rm")
    guide = ROOT / "docs" / "GO_LIVE.md"
    if not guide.exists():
        err("docs/GO_LIVE.md", "missing")
    else:
        g = guide.read_text()
        for name in LIVE_SCRIPTS + ["go_live_check", "docker-compose.live.yml", ".env.live.example"]:
            if name not in g:
                err("docs/GO_LIVE.md", f"does not mention {name}")
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    if "deploy/live" not in ci:
        err(".github/workflows/ci.yml", "no job exercises deploy/live")


# ------------------------------------------------------------------ Prometheus / Alertmanager / CI / Dockerfile
def check_observability():
    prom = load_all(ROOT / "deploy" / "prometheus" / "prometheus.yml")
    if prom and not prom[0].get("scrape_configs"):
        err("deploy/prometheus/prometheus.yml", "no scrape_configs")
    for rules in (ROOT / "deploy" / "prometheus" / "rules").glob("*.yml"):
        for d in load_all(rules):
            for g in d.get("groups", []):
                for r in g.get("rules", []):
                    if "alert" in r and "severity" not in (r.get("labels") or {}):
                        err(str(rules.relative_to(ROOT)), f"alert {r['alert']} needs a severity label")
    am = load_all(ROOT / "deploy" / "alertmanager" / "alertmanager.yml")
    if am:
        hooks = [w for r in am[0].get("receivers", []) for w in r.get("webhook_configs", [])]
        if not any(w.get("url", "").endswith("/api/v1/alertmanager/") for w in hooks):
            err("deploy/alertmanager/alertmanager.yml", "no webhook to the console's /api/v1/alertmanager/")
        for w in hooks:
            auth = (w.get("http_config") or {}).get("authorization") or {}
            if auth.get("type") != "Bearer" or "credentials" in auth:
                err("deploy/alertmanager/alertmanager.yml", "use Bearer credentials_file, never an inline token")
    for wf in (ROOT / ".github" / "workflows").glob("*.yml"):
        for d in load_all(wf):
            jobs = d.get("jobs", {})
            if wf.name == "ci.yml":
                if "test" not in jobs:
                    err(".github/workflows/ci.yml", "test job missing")
                if not any("docker" in j for j in jobs):
                    err(".github/workflows/ci.yml", "docker build job missing")
    dockerfile = (ROOT / "Dockerfile").read_text()
    if not re.search(r"^USER\s+(?!root|0\b)\S+", dockerfile, re.M):
        err("Dockerfile", "must switch to a non-root USER")
    if "HEALTHCHECK" not in dockerfile or "/healthz" not in (ROOT / "deploy/docker/healthcheck.py").read_text():
        err("Dockerfile", "HEALTHCHECK on /healthz missing")
    if not re.search(r"^FROM python:\$\{PYTHON_VERSION\}-slim|^FROM python:3\.12-slim", dockerfile, re.M) or \
            "ARG PYTHON_VERSION=3.12" not in dockerfile:
        err("Dockerfile", "base image must be python:3.12-slim")


def check_docs():
    doc = (ROOT / "docs" / "DEPLOYMENT.md").read_text()
    for name in sorted(SETTINGS):
        if f"`{name}`" not in doc:
            err("docs/DEPLOYMENT.md", f"environment variable {name} is not documented")
    env_example = (ROOT / ".env.example").read_text()
    for name in sorted(SETTINGS):   # commented-out lines count: the file is the reference for every knob
        if not re.search(rf"^#?\s*{name}=", env_example, re.M):
            err(".env.example", f"{name} is not listed (commented out is fine)")
    # and the other way round: nothing documented that the application does not read
    check_env_names("docs/DEPLOYMENT.md", set(re.findall(r"\b(LINKEDEYE_[A-Z0-9_]+)", doc)))


def main():
    check_kubernetes()
    check_optional()
    check_compose()
    check_live()
    check_observability()
    check_docs()
    if errors:
        print(f"{len(errors)} problem(s):")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(f"deployment bundle OK ({len(SETTINGS)} LINKEDEYE_* settings documented)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
