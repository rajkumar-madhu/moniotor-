"""Tests for the live deployment pack (deploy/live): compose file, Caddy configuration, validate.py rules and the shell scripts.

    pip install pyyaml && python -m unittest discover -s deploy/tests -v

No Docker daemon is needed: the scripts run against a stub `docker` executable that records its calls and answers like a
healthy stack (or, for the failure tests, like a broken one). `docker compose config` is run for real when the Compose
plugin is installed (it needs no daemon either).
"""
import http.server
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
LIVE = ROOT / "deploy" / "live"
BASH = shutil.which("bash")

STUB_DOCKER = r'''#!/usr/bin/env bash
# Stub of the docker CLI for deploy/tests/test_live.py. Records every call; behaviour is steered by STUB_* variables.
printf 'docker %s\n' "$*" >> "$STUB_DIR/calls.log"
case "$1" in
  info) [ "${STUB_INFO:-ok}" = ok ] || exit 1; exit 0 ;;
  inspect)
    id="${*: -1}"; fmt="$3"
    case "$fmt" in
      *ExitCode*) echo 0 ;;
      *State.Status*) case "$id" in cid-migrate) echo "${STUB_MIGRATE_STATUS:-exited}" ;; *) echo running ;; esac ;;
      *Health*) case "$id" in cid-migrate) echo "" ;; cid-"${STUB_UNHEALTHY:-none}") echo unhealthy ;; *) echo healthy ;; esac ;;
      *.Image*) echo sha256:abc ;;
    esac
    exit 0 ;;
  image|tag|volume) exit 0 ;;
  compose)
    shift
    while [ "$1" = -f ] || [ "$1" = --profile ]; do shift 2; done
    case "$1" in
      version) echo "${STUB_COMPOSE_VERSION:-2.30.1}" ;;
      config) exit "${STUB_CONFIG_RC:-0}" ;;
      ps) echo "cid-${*: -1}" ;;
      exec)
        shift
        args="$*"
        case "$args" in
          *pg_dump*) if [ "${STUB_DUMP_EMPTY:-0}" = 1 ]; then :; else printf 'PGDMP-stub-dump-%s' "$RANDOM"; fi ;;
          *"pg_restore --list"*) cat > /dev/null; exit "${STUB_LIST_RC:-0}" ;;
          *"pg_restore --no-owner"*) cat > "$STUB_DIR/restored.dump"; exit "${STUB_RESTORE_RC:-0}" ;;
          *"postgres --version"*) echo "postgres (PostgreSQL) 16.0" ;;
          *"createsuperuser"*) echo created >> "$STUB_DIR/admin-created" ;;
          *"integration_config"*) echo "smtp-host=${STUB_SMTP_HOST:-}" ;;
          *"manage.py shell"*) echo "${STUB_SHELL_OUT:-0 0}" ;;
          *) : ;;
        esac
        exit 0 ;;
      *) exit 0 ;;
    esac ;;
esac
exit 0
'''


def make_env(stub_dir, extra=None):
    bin_dir = Path(stub_dir) / "bin"
    bin_dir.mkdir(exist_ok=True)
    docker = bin_dir / "docker"
    docker.write_text(STUB_DOCKER)
    docker.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("LINKEDEYE_", "STUB_", "DJANGO_"))}
    env.update({"PATH": f"{bin_dir}:{env['PATH']}", "STUB_DIR": str(stub_dir), "NO_COLOR": "1", "LC_ALL": "C",
                "LIVE_POLL_SECONDS": "0.05", "LIVE_TIMEOUT_DIV": "200"})
    env.update(extra or {})
    return env


GOOD_SITE = {"/healthz": (200, {}, b'{"status": "ok"}'), "/accounts/login/": (200, {}, b"<form>"),
             "/static/linkedeye/app.css": (200, {}, b"a{}")}


def serve_routes(test, routes):
    """A throw-away site on 127.0.0.1; routes = {path: (status, headers, body)}. Returns its port."""
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            status, headers, body = routes.get(self.path.split("?")[0], (404, {}, b""))
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    test.addCleanup(server.shutdown)
    return server.server_address[1]


class LiveCase(unittest.TestCase):
    """A scratch copy of deploy/live next to a stub docker."""

    def setUp(self):
        if not BASH:
            self.skipTest("bash is required")
        self.tmp = Path(tempfile.mkdtemp(prefix="le-live-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.live = self.tmp / "deploy" / "live"
        shutil.copytree(LIVE, self.live, ignore=shutil.ignore_patterns(".env", "backups", ".last-update"))
        self.stub = self.tmp / "stub"
        self.stub.mkdir()
        self.env_file = self.live / ".env"

    def run_script(self, name, *args, env=None, stdin=None, timeout=60):
        proc = subprocess.run([str(self.live / name), *args], capture_output=True, text=True, timeout=timeout, cwd=self.tmp,
                              env=make_env(self.stub, env), input=stdin)
        return proc

    def env_values(self):
        out = {}
        for line in self.env_file.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                out[k] = v
        return out

    def calls(self):
        log = self.stub / "calls.log"
        return log.read_text().splitlines() if log.exists() else []

    def prepare(self, *args, env=None):
        return self.run_script("bootstrap.sh", "--prepare-only", "--yes", *args, env=env)


class ComposeFileTests(unittest.TestCase):
    def setUp(self):
        self.doc = yaml.safe_load((LIVE / "docker-compose.live.yml").read_text())
        self.services = self.doc["services"]

    def test_services(self):
        self.assertTrue({"db", "migrate", "web", "worker", "caddy"} <= set(self.services))
        self.assertEqual(self.services["worker"]["deploy"]["replicas"], 1)
        self.assertEqual(self.services["worker"]["command"][-1], "run_probes")

    def test_only_caddy_publishes_application_ports(self):
        for name in ("db", "web", "worker", "migrate"):
            self.assertNotIn("ports", self.services[name], name)
        self.assertEqual(self.services["web"]["expose"], ["8000"])
        self.assertTrue(any("443" in p for p in self.services["caddy"]["ports"]))

    def test_debug_is_forced_off_and_secrets_are_required(self):
        env = self.services["web"]["environment"]
        self.assertEqual(env["LINKEDEYE_DEBUG"], "false")
        for key in ("LINKEDEYE_SECRET_KEY", "LINKEDEYE_DB_PASSWORD", "LINKEDEYE_ALLOWED_HOSTS"):
            self.assertIn(":?", env[key])
        self.assertIn(":?", self.services["db"]["environment"]["POSTGRES_PASSWORD"])

    def test_proxy_headers_are_trusted_as_a_pair(self):
        env = self.services["web"]["environment"]
        self.assertIn("GUNICORN_FORWARDED_ALLOW_IPS", env)
        self.assertIn("LINKEDEYE_TRUSTED_PROXIES", env)
        self.assertNotIn("0.0.0.0/0", env["LINKEDEYE_TRUSTED_PROXIES"])          # private ranges only, never "everyone"

    def test_images_are_pinned(self):
        for name in ("db", "caddy", "prometheus", "alertmanager", "node-exporter"):
            image = self.services[name]["image"]
            self.assertNotIn("latest", image, name)
            self.assertRegex(re.sub(r"^\$\{[A-Z_]+:-(.*)\}$", r"\1", image), r":v?\d", name)

    def test_observability_profile_matches_the_base_compose_file(self):
        base = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]
        for name in ("prometheus", "alertmanager", "node-exporter"):
            self.assertEqual(self.services[name]["profiles"], ["observability"])
            self.assertEqual(self.services[name]["image"], base[name]["image"])
        for port in (self.services["prometheus"]["ports"] + self.services["alertmanager"]["ports"]):
            self.assertIn("127.0.0.1", port)          # default: loopback only

    def test_caddy_is_locked_down(self):
        caddy = self.services["caddy"]
        self.assertEqual(caddy["cap_drop"], ["ALL"])
        self.assertEqual(caddy["cap_add"], ["NET_BIND_SERVICE"])
        self.assertTrue(caddy["read_only"])
        self.assertIn("no-new-privileges:true", caddy["security_opt"])

    def test_caddy_cannot_reach_the_database_or_the_worker(self):
        self.assertEqual(self.services["caddy"]["networks"], ["proxy"])
        self.assertIn("proxy", self.services["web"]["networks"])
        for name in ("db", "worker", "migrate"):
            self.assertNotIn("proxy", self.services[name].get("networks", []), name)

    def test_containers_have_memory_and_process_limits(self):
        for name in ("web", "worker", "migrate", "caddy"):
            self.assertIn("mem_limit", self.services[name], name)
            self.assertIn("pids_limit", self.services[name], name)

    def test_published_ports_follow_the_bind_address(self):
        for port in self.services["caddy"]["ports"]:
            self.assertTrue(port.startswith("${LINKEDEYE_BIND_ADDRESS:-0.0.0.0}:"), port)

    def test_caddy_waits_for_web_and_mounts_its_config(self):
        self.assertEqual(self.services["caddy"]["depends_on"]["web"]["condition"], "service_healthy")
        self.assertIn("./caddy:/etc/caddy:ro", self.services["caddy"]["volumes"])

    def test_caddyfiles_per_mode(self):
        snippet = (LIVE / "caddy" / "linkedeye.snippet").read_text()
        self.assertIn("reverse_proxy web:8000", snippet)
        self.assertIn("request_body", snippet)          # datasheet uploads are bounded at the proxy too
        https = (LIVE / "caddy" / "https.Caddyfile").read_text()
        self.assertIn("{$LINKEDEYE_DOMAIN}", https)
        self.assertIn("{$LINKEDEYE_ACME_EMAIL}", https)
        self.assertNotIn("local_certs", https)
        self.assertIn("local_certs", (LIVE / "caddy" / "internal.Caddyfile").read_text())
        # an IP-only site needs an explicit tls directive, or default_sni is lost and SNI-less clients get no certificate
        internal = (LIVE / "caddy" / "internal.Caddyfile").read_text()
        self.assertIn("default_sni {$LINKEDEYE_DOMAIN}", internal)
        site = internal.split("{$LINKEDEYE_DOMAIN} {", 1)[1].split("}", 1)[0]
        self.assertIn("tls internal", site)
        http = (LIVE / "caddy" / "http.Caddyfile").read_text()
        self.assertIn("auto_https off", http)
        self.assertIn(":80", http)
        for mode in ("https", "internal", "http"):
            self.assertIn("import linkedeye.snippet", (LIVE / "caddy" / f"{mode}.Caddyfile").read_text())
        # compose picks the file from LINKEDEYE_MODE
        self.assertIn("${LINKEDEYE_MODE:-https}.Caddyfile", " ".join(self.services["caddy"]["command"]))


@unittest.skipUnless(shutil.which("docker") and subprocess.run(["docker", "compose", "version"], capture_output=True).returncode == 0,
                     "the Docker Compose plugin is not installed")
class ComposeConfigTests(unittest.TestCase):
    """`docker compose config` renders the file for real (no daemon needed)."""

    def render(self, extra_env=None, *args):
        with tempfile.TemporaryDirectory() as tmp:
            live = Path(tmp) / "deploy" / "live"
            shutil.copytree(LIVE, live, ignore=shutil.ignore_patterns(".env", "backups"))
            shutil.copytree(ROOT / "deploy" / "prometheus", Path(tmp) / "deploy" / "prometheus")
            shutil.copytree(ROOT / "deploy" / "alertmanager", Path(tmp) / "deploy" / "alertmanager")
            env = {"LINKEDEYE_SECRET_KEY": "ci-only-not-a-secret-0123456789abcdefghijklmnopqrstuvwxyzABCDEF",
                   "LINKEDEYE_DB_PASSWORD": "ci-only-password", "LINKEDEYE_ALLOWED_HOSTS": "linkedeye.test,web,localhost",
                   "LINKEDEYE_DOMAIN": "linkedeye.test", "LINKEDEYE_MODE": "http", "LINKEDEYE_ALERTMANAGER_WEBHOOK_TOKEN": "ci-token"}
            env.update(extra_env or {})
            (live / ".env").write_text("".join(f"{k}={v}\n" for k, v in env.items()))
            return subprocess.run(["docker", "compose", "-f", str(live / "docker-compose.live.yml"), *args, "config"],
                                  capture_output=True, text=True, timeout=60,
                                  # the shell's own LINKEDEYE_* values win over .env in compose: render from the file alone
                                  env={**{k: v for k, v in os.environ.items() if not k.startswith("LINKEDEYE_")}, "COMPOSE_PROFILES": ""})

    def test_renders(self):
        proc = self.render()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rendered = yaml.safe_load(proc.stdout)
        self.assertEqual(rendered["services"]["web"]["environment"]["LINKEDEYE_DEBUG"], "false")
        self.assertNotIn("prometheus", rendered["services"])          # profile off by default
        self.assertIn("http.Caddyfile", " ".join(rendered["services"]["caddy"]["command"]))

    def test_observability_profile_renders(self):
        proc = self.render(None, "--profile", "observability")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("prometheus", yaml.safe_load(proc.stdout)["services"])

    def test_refuses_to_render_without_secrets(self):
        for missing in ("LINKEDEYE_SECRET_KEY", "LINKEDEYE_DB_PASSWORD", "LINKEDEYE_ALLOWED_HOSTS"):
            proc = self.render({missing: ""})
            self.assertNotEqual(proc.returncode, 0, missing)
            self.assertIn(missing, proc.stderr)


class ValidatorTests(unittest.TestCase):
    """validate.py against deliberately broken copies of the live pack."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="le-validate-live-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        for rel in ("deploy", "docker-compose.yml", "Dockerfile", ".github", "docs/DEPLOYMENT.md", "docs/GO_LIVE.md",
                    "linkedeye/settings.py", "administration/services.py", "telemetry/nagios.py", ".env.example"):
            src, dst = ROOT / rel, self.tmp / rel
            if not src.exists():
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "tests", ".env", "backups"))
            else:
                shutil.copy2(src, dst)

    def validate(self):
        proc = subprocess.run([sys.executable, str(self.tmp / "deploy" / "validate.py")], capture_output=True, text=True, timeout=60)
        self.assertNotIn("Traceback", proc.stderr, proc.stderr)
        return proc.returncode, proc.stdout

    def edit(self, rel, old, new):
        path = self.tmp / rel
        text = path.read_text()
        self.assertIn(old, text, f"fixture drift in {rel}")
        path.write_text(text.replace(old, new, 1))

    def assertFails(self, needle):
        rc, out = self.validate()
        self.assertEqual(rc, 1, out)
        self.assertIn(needle, out)

    def test_shipped_pack_is_valid(self):
        rc, out = self.validate()
        self.assertEqual(rc, 0, out)

    def test_second_scheduler(self):
        self.edit("deploy/live/docker-compose.live.yml", "replicas: 1          # the probe", "replicas: 2          # the probe")
        self.assertFails("worker must declare deploy.replicas: 1")

    def test_database_must_not_be_published(self):
        self.edit("deploy/live/docker-compose.live.yml", "    image: postgres:16-alpine\n", "    image: postgres:16-alpine\n    ports: ['5432:5432']\n")
        self.assertFails("only caddy may publish ports")

    def test_web_must_not_be_published(self):
        self.edit("deploy/live/docker-compose.live.yml", '    expose:\n      - "8000"\n', '    ports:\n      - "8000:8000"\n')
        self.assertFails("only caddy may publish ports")

    def test_debug_must_stay_forced_off(self):
        self.edit("deploy/live/docker-compose.live.yml", '    LINKEDEYE_DEBUG: "false"\n', "")
        self.assertFails("LINKEDEYE_DEBUG must be forced")

    def test_secrets_must_stay_required(self):
        self.edit("deploy/live/docker-compose.live.yml", "${LINKEDEYE_SECRET_KEY:?set LINKEDEYE_SECRET_KEY in deploy/live/.env (bootstrap.sh generates it)}", "${LINKEDEYE_SECRET_KEY:-}")
        self.assertFails("LINKEDEYE_SECRET_KEY must be required")

    def test_unpinned_proxy_image(self):
        self.edit("deploy/live/docker-compose.live.yml", "caddy:2.10.0-alpine", "caddy:latest")
        self.assertFails("must be pinned to a version tag")

    def test_caddy_capabilities(self):
        self.edit("deploy/live/docker-compose.live.yml", "    cap_add: [NET_BIND_SERVICE]\n", "")
        self.assertFails("NET_BIND_SERVICE")

    def test_caddy_must_stay_off_the_database_network(self):
        self.edit("deploy/live/docker-compose.live.yml", "    networks: [proxy]\n    # LINKEDEYE_BIND_ADDRESS", "    # LINKEDEYE_BIND_ADDRESS")
        self.assertFails("proxy network alone")

    def test_containers_must_keep_their_limits(self):
        self.edit("deploy/live/docker-compose.live.yml", "    mem_limit: ${LINKEDEYE_WORKER_MEMORY:-1g}\n", "")
        self.edit("deploy/live/docker-compose.live.yml", "  mem_limit: ${LINKEDEYE_APP_MEMORY:-1g}\n", "")
        self.assertFails("needs mem_limit and pids_limit")

    def test_observability_image_drift(self):
        self.edit("deploy/live/docker-compose.live.yml", "prom/prometheus:v3.16.0", "prom/prometheus:v3.1.0")
        self.assertFails("prometheus image differs from docker-compose.yml")

    def test_missing_caddyfile(self):
        (self.tmp / "deploy/live/caddy/internal.Caddyfile").unlink()
        self.assertFails("internal.Caddyfile: missing")

    def test_secret_value_in_the_env_template(self):
        self.edit("deploy/live/.env.live.example", "LINKEDEYE_DB_PASSWORD=\n", "LINKEDEYE_DB_PASSWORD=hunter2hunter2\n")
        self.assertFails("LINKEDEYE_DB_PASSWORD must be blank")

    def test_admin_password_must_not_live_in_env(self):
        self.edit("deploy/live/.env.live.example", "LINKEDEYE_ADMIN_USER=admin\n", "LINKEDEYE_ADMIN_USER=admin\nLINKEDEYE_ADMIN_PASSWORD=\n")
        self.assertFails("admin password must not live in .env")

    def test_unknown_variable_in_the_env_template(self):
        self.edit("deploy/live/.env.live.example", "LINKEDEYE_TENANT=default\n", "LINKEDEYE_TENNANT=default\n")
        self.assertFails("LINKEDEYE_TENNANT is not read by the application")

    def test_script_must_be_executable_and_strict(self):
        script = self.tmp / "deploy/live/backup.sh"
        script.chmod(script.stat().st_mode & ~(stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
        self.assertFails("backup.sh: must be executable")
        script.chmod(0o755)
        self.edit("deploy/live/backup.sh", "set -euo pipefail\n", "")
        self.assertFails("backup.sh: must set -u and pipefail")

    def test_eval_is_banned(self):
        self.edit("deploy/live/update.sh", "need_cmd docker", "eval \"$1\"\nneed_cmd docker")
        self.assertFails("must not use eval")

    def test_guide_must_mention_every_script(self):
        self.edit("docs/GO_LIVE.md", "restore.sh", "restore-script")
        # (the guide mentions restore.sh many times; replace only the first, then remove all to prove the rule)
        path = self.tmp / "docs/GO_LIVE.md"
        path.write_text(path.read_text().replace("restore.sh", "restore-script"))
        self.assertFails("does not mention restore.sh")

    def test_new_setting_must_be_documented(self):
        self.edit("docs/DEPLOYMENT.md", "`LINKEDEYE_HSTS_SECONDS`", "`LINKEDEYE_HSTS_SECOND`")
        rc, out = self.validate()
        self.assertEqual(rc, 1, out)
        self.assertIn("LINKEDEYE_HSTS_SECONDS is not documented", out)


class ScriptSyntaxTests(unittest.TestCase):
    def test_every_script_parses(self):
        if not BASH:
            self.skipTest("bash is required")
        for script in sorted(LIVE.glob("*.sh")):
            proc = subprocess.run([BASH, "-n", str(script)], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, f"{script.name}: {proc.stderr}")

    def test_scripts_are_executable_and_use_env_bash(self):
        for name in ("bootstrap.sh", "backup.sh", "restore.sh", "healthcheck.sh", "update.sh"):
            path = LIVE / name
            self.assertTrue(path.stat().st_mode & stat.S_IXUSR, name)
            self.assertEqual(path.read_text().splitlines()[0], "#!/usr/bin/env bash", name)

    def test_help_prints_usage_without_touching_anything(self):
        if not BASH:
            self.skipTest("bash is required")
        before = sorted(p.name for p in LIVE.iterdir())
        for name in ("bootstrap.sh", "backup.sh", "restore.sh", "healthcheck.sh", "update.sh"):
            proc = subprocess.run([str(LIVE / name), "--help"], capture_output=True, text=True, timeout=20)
            self.assertEqual(proc.returncode, 0, f"{name}: {proc.stderr}")
            self.assertIn("deploy/live/", proc.stdout, name)
        self.assertEqual(before, sorted(p.name for p in LIVE.iterdir()))          # no .env, no backups directory appeared


class BootstrapTests(LiveCase):
    def test_https_mode_generates_secrets_and_derives_the_rest(self):
        proc = self.prepare("--domain", "linkedeye.corp.example.com", "--email", "ops@corp.example.com",
                            env={"LINKEDEYE_EMAIL_HOST": "smtp.corp.example.com"})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        mode = stat.S_IMODE(self.env_file.stat().st_mode)
        self.assertEqual(mode, 0o600)
        env = self.env_values()
        self.assertGreaterEqual(len(env["LINKEDEYE_SECRET_KEY"]), 50)
        self.assertGreaterEqual(len(set(env["LINKEDEYE_SECRET_KEY"])), 20)            # passes settings.py's strength rule
        self.assertEqual(len(env["LINKEDEYE_DB_PASSWORD"]), 32)
        self.assertRegex(env["LINKEDEYE_DB_PASSWORD"], r"^[A-Za-z0-9]+$")
        self.assertEqual(len(env["LINKEDEYE_CREDENTIAL_KEY"]), 44)
        self.assertEqual(env["LINKEDEYE_ALLOWED_HOSTS"], "linkedeye.corp.example.com,web,localhost")
        self.assertEqual(env["LINKEDEYE_CSRF_TRUSTED_ORIGINS"], "https://linkedeye.corp.example.com")
        self.assertEqual(env["LINKEDEYE_SECURE_COOKIES"], "true")
        self.assertEqual(env["LINKEDEYE_HSTS_SECONDS"], "86400")
        self.assertEqual(env["LINKEDEYE_MODE"], "https")
        self.assertEqual(env["LINKEDEYE_ACME_EMAIL"], "ops@corp.example.com")
        self.assertIn("docker compose -f", " ".join(self.calls()) + " docker compose -f")        # the config was validated
        self.assertTrue(any(" config --quiet" in c for c in self.calls()))

    def test_fernet_key_is_valid(self):
        try:
            from cryptography.fernet import Fernet
        except ImportError:
            self.skipTest("cryptography is not installed")
        self.prepare("--domain", "linkedeye.corp.example.com", "--email", "ops@corp.example.com", env={"LINKEDEYE_EMAIL_HOST": "smtp.x.example"})
        Fernet(self.env_values()["LINKEDEYE_CREDENTIAL_KEY"].encode())

    def test_rerun_changes_nothing(self):
        args = ["--domain", "linkedeye.corp.example.com", "--email", "ops@corp.example.com"]
        env = {"LINKEDEYE_EMAIL_HOST": "smtp.corp.example.com"}
        self.assertEqual(self.prepare(*args, env=env).returncode, 0)
        first = self.env_file.read_text()
        self.assertEqual(self.prepare(env=env).returncode, 0)          # no arguments: reads the .env it wrote
        self.assertEqual(self.env_file.read_text(), first)
        self.assertEqual(stat.S_IMODE(self.env_file.stat().st_mode), 0o600)

    def test_existing_secrets_are_never_replaced(self):
        self.env_file.write_text((LIVE / ".env.live.example").read_text())
        text = self.env_file.read_text().replace("LINKEDEYE_SECRET_KEY=\n", "LINKEDEYE_SECRET_KEY=keep-this-secret-key-0123456789-abcdefghijklmnopqrstuvwxyz\n") \
                                        .replace("LINKEDEYE_DB_PASSWORD=\n", "LINKEDEYE_DB_PASSWORD=keepmydbpassword\n") \
                                        .replace("LINKEDEYE_CREDENTIAL_KEY=\n", "LINKEDEYE_CREDENTIAL_KEY=keepmycredentialkey\n")
        self.env_file.write_text(text)
        self.env_file.chmod(0o644)
        proc = self.prepare("--domain", "linkedeye.corp.example.com", "--email", "ops@corp.example.com", env={"LINKEDEYE_EMAIL_HOST": "smtp.x.example"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        env = self.env_values()
        self.assertEqual(env["LINKEDEYE_DB_PASSWORD"], "keepmydbpassword")
        self.assertEqual(env["LINKEDEYE_CREDENTIAL_KEY"], "keepmycredentialkey")
        self.assertTrue(env["LINKEDEYE_SECRET_KEY"].startswith("keep-this"))
        self.assertEqual(stat.S_IMODE(self.env_file.stat().st_mode), 0o600)          # loosened permissions are tightened

    def test_http_mode_with_port(self):
        proc = self.prepare("--domain", "localhost", "--mode", "http")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        env = self.env_values()
        self.assertEqual(env["LINKEDEYE_ALLOWED_HOSTS"], "localhost,web")
        self.assertEqual(env["LINKEDEYE_CSRF_TRUSTED_ORIGINS"], "http://localhost")
        self.assertEqual(env["LINKEDEYE_SECURE_COOKIES"], "false")
        self.assertEqual(env["LINKEDEYE_HSTS_SECONDS"], "0")
        self.assertEqual(env["LINKEDEYE_LOGIN_OTP"], "false")          # no SMTP: the code is switched off, loudly
        self.assertIn("e-mail sign-in code is switched OFF", proc.stderr)
        self.env_file.write_text(self.env_file.read_text().replace("LINKEDEYE_HTTP_PORT=80", "LINKEDEYE_HTTP_PORT=8080"))
        self.assertEqual(self.prepare().returncode, 0)
        self.assertEqual(self.env_values()["LINKEDEYE_CSRF_TRUSTED_ORIGINS"], "http://localhost:8080")

    def test_localhost_trial_is_published_on_the_loopback_interface_only(self):
        self.assertEqual(self.prepare("--domain", "localhost", "--mode", "http").returncode, 0)
        self.assertEqual(self.env_values()["LINKEDEYE_BIND_ADDRESS"], "127.0.0.1")
        # moving the same install to a real address must not leave it unreachable
        self.assertEqual(self.prepare("--domain", "10.0.0.5", "--mode", "http").returncode, 0)
        self.assertEqual(self.env_values()["LINKEDEYE_BIND_ADDRESS"], "")

    def test_plain_http_on_a_public_name_is_refused_unless_confirmed(self):
        for domain in ("linkedeye.corp.example.com", "203.0.113.7"):
            proc = self.prepare("--domain", domain, "--mode", "http")
            self.assertNotEqual(proc.returncode, 0, domain)
            self.assertIn("clear text", proc.stderr)
            self.assertIn("--allow-plain-http", proc.stderr)
            if self.env_file.exists():
                self.env_file.unlink()
        self.assertEqual(self.prepare("--domain", "203.0.113.7", "--mode", "http", "--allow-plain-http").returncode, 0)
        self.assertEqual(self.env_values()["LINKEDEYE_CSRF_TRUSTED_ORIGINS"], "http://203.0.113.7")

    def test_a_shell_password_with_dollar_signs_reaches_compose_unharmed(self):
        password = 'pa$word #1 "x"'
        proc = self.prepare("--domain", "linkedeye.corp.example.com", "--email", "ops@corp.example.com",
                            env={"LINKEDEYE_EMAIL_HOST": "smtp.x.example", "LINKEDEYE_EMAIL_HOST_PASSWORD": password})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.env_values()["LINKEDEYE_EMAIL_HOST_PASSWORD"], "'" + password + "'")
        self.assertNotIn(password, proc.stdout + proc.stderr)

    def test_the_acme_address_is_substituted_into_a_caddyfile_so_it_must_be_plain(self):
        proc = self.prepare("--domain", "linkedeye.corp.example.com", "--email", "x@y.example } { respond 200",
                            env={"LINKEDEYE_EMAIL_HOST": "smtp.x.example"})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("not a plain e-mail", proc.stderr)

    def test_internal_mode_on_an_ip_address(self):
        proc = self.prepare("--domain", "192.0.2.10", "--mode", "internal", env={"LINKEDEYE_EMAIL_HOST": "smtp.x.example"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        env = self.env_values()
        self.assertEqual(env["LINKEDEYE_CSRF_TRUSTED_ORIGINS"], "https://192.0.2.10")
        self.assertEqual(env["LINKEDEYE_SECURE_COOKIES"], "true")

    def test_lets_encrypt_cannot_issue_for_an_ip_or_a_local_name(self):
        for domain in ("192.0.2.10", "localhost", "intranet.lan", "box", "nas.local"):
            proc = self.prepare("--domain", domain, "--email", "ops@corp.example.com", env={"LINKEDEYE_EMAIL_HOST": "smtp.x.example"})
            self.assertNotEqual(proc.returncode, 0, domain)
            self.assertRegex(proc.stderr, r"--mode (internal|http)|Let's Encrypt", domain)
            if self.env_file.exists():
                self.env_file.unlink()

    def test_https_mode_needs_a_contact_address(self):
        proc = self.prepare("--domain", "linkedeye.corp.example.com", env={"LINKEDEYE_EMAIL_HOST": "smtp.x.example"})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--email", proc.stderr)

    def test_the_placeholder_domain_is_refused(self):
        proc = self.prepare()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Set the domain", proc.stderr)

    def test_hostile_domain_values_are_refused(self):
        for domain in ("a.example.com;rm -rf /", "$(id).example.com", "https://a.example.com", "a.example.com/x", "a b.example.com"):
            proc = self.prepare("--domain", domain, "--email", "ops@corp.example.com", env={"LINKEDEYE_EMAIL_HOST": "smtp.x.example"})
            self.assertNotEqual(proc.returncode, 0, domain)
            if self.env_file.exists():
                self.env_file.unlink()

    def test_no_smtp_means_no_sign_in_so_https_modes_refuse(self):
        proc = self.prepare("--domain", "linkedeye.corp.example.com", "--email", "ops@corp.example.com")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("no SMTP server is configured", proc.stderr)
        self.assertIn("LINKEDEYE_LOGIN_OTP=false", proc.stderr)

    def test_password_only_is_possible_when_chosen_explicitly(self):
        self.prepare("--domain", "linkedeye.corp.example.com", "--email", "ops@corp.example.com", env={"LINKEDEYE_EMAIL_HOST": "x"})
        self.env_file.write_text(self.env_file.read_text().replace("LINKEDEYE_EMAIL_HOST=x", "LINKEDEYE_EMAIL_HOST=").replace("LINKEDEYE_LOGIN_OTP=\n", "LINKEDEYE_LOGIN_OTP=false\n"))
        proc = self.prepare()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("password only", proc.stderr)

    def test_observability_flag(self):
        proc = self.prepare("--domain", "localhost", "--mode", "http", "--observability")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        env = self.env_values()
        self.assertEqual(env["COMPOSE_PROFILES"], "observability")
        self.assertEqual(env["LINKEDEYE_PROMETHEUS_URL"], "http://prometheus:9090")
        self.assertGreaterEqual(len(env["LINKEDEYE_ALERTMANAGER_WEBHOOK_TOKEN"]), 32)

    def test_old_compose_is_refused(self):
        proc = self.prepare("--domain", "localhost", "--mode", "http", env={"STUB_COMPOSE_VERSION": "2.20.3"})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("too old", proc.stderr)
        self.assertFalse(self.env_file.exists())

    def test_missing_docker_is_explained(self):
        env = make_env(self.stub)
        env["PATH"] = str(self.tmp / "emptybin")
        (self.tmp / "emptybin").mkdir()
        for tool in ("env", "bash", "dirname", "cat", "sed"):
            path = shutil.which(tool)
            if path:
                os.symlink(path, self.tmp / "emptybin" / tool)
        proc = subprocess.run([BASH, str(self.live / "bootstrap.sh"), "--prepare-only", "--yes", "--domain", "localhost", "--mode", "http"],
                              capture_output=True, text=True, env=env, timeout=30)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("docker is required", proc.stderr)

    def test_rejected_compose_config_stops_before_anything_starts(self):
        proc = self.prepare("--domain", "localhost", "--mode", "http", env={"STUB_CONFIG_RC": "1"})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("rejected the configuration", proc.stderr)

    def start_site(self):
        port = serve_routes(self, GOOD_SITE)
        self.env_file.write_text((LIVE / ".env.live.example").read_text().replace("LINKEDEYE_HTTP_PORT=80", f"LINKEDEYE_HTTP_PORT={port}"))
        return port

    def test_full_run_with_the_stub_creates_the_administrator_once(self):
        self.start_site()
        env = {"STUB_SHELL_OUT": "0 0", "LINKEDEYE_ADMIN_PASSWORD": "Pw-from-env-2026"}
        proc = self.run_script("bootstrap.sh", "--domain", "localhost", "--mode", "http", "--no-build", "--yes",
                               "--admin-email", "boss@corp.example.com", env=env, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue((self.stub / "admin-created").exists())
        self.assertNotIn("Pw-from-env-2026", proc.stdout + proc.stderr)                     # the password is never echoed
        self.assertNotIn("Pw-from-env-2026", self.env_file.read_text())                      # ... nor stored in .env
        self.assertNotIn("Pw-from-env-2026", "\n".join(self.calls()))                        # ... nor on a command line
        self.assertIn("LinkedEye is running at:  http://localhost", proc.stdout)
        (self.stub / "admin-created").unlink()
        proc = self.run_script("bootstrap.sh", "--no-build", "--yes", env={**env, "STUB_SHELL_OUT": "1 1"}, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse((self.stub / "admin-created").exists())                            # an existing administrator is never touched
        self.assertIn("already exists", proc.stdout)

    def test_a_generated_admin_password_is_printed_once_and_not_stored(self):
        self.start_site()
        proc = self.run_script("bootstrap.sh", "--domain", "localhost", "--mode", "http", "--no-build", "--yes",
                               "--admin-email", "boss@corp.example.com", timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        m = re.search(r"Generated password:\s+(\S+)", proc.stdout)
        self.assertIsNotNone(m)
        self.assertGreaterEqual(len(m.group(1)), 20)
        self.assertNotIn(m.group(1), self.env_file.read_text())
        self.assertNotIn(m.group(1), "\n".join(self.calls()))

    def test_admin_user_names_are_validated(self):
        proc = self.run_script("bootstrap.sh", "--domain", "localhost", "--mode", "http", "--no-build", "--yes", "--admin-user", 'x"; import os',
                               "--admin-email", "boss@corp.example.com", timeout=120)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("LINKEDEYE_ADMIN_USER", proc.stderr)
        self.assertFalse((self.stub / "admin-created").exists())

    def test_unhealthy_service_is_reported_and_exit_status_is_nonzero(self):
        self.start_site()
        proc = self.run_script("bootstrap.sh", "--domain", "localhost", "--mode", "http", "--no-build", "--yes",
                               "--admin-email", "boss@corp.example.com", env={"STUB_UNHEALTHY": "web"}, timeout=120)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("web", proc.stderr)


class LibTests(LiveCase):
    def lib(self, snippet, env_text=None):
        if env_text is not None:
            self.env_file.write_text(env_text)
        proc = subprocess.run([BASH, "-c", f'set -euo pipefail; . "{self.live}/lib.sh"; {snippet}'], capture_output=True, text=True,
                              env=make_env(self.stub), timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def test_public_url(self):
        self.assertEqual(self.lib("public_url", "LINKEDEYE_MODE=https\nLINKEDEYE_DOMAIN=a.example.com\n"), "https://a.example.com")
        self.assertEqual(self.lib("public_url", "LINKEDEYE_MODE=https\nLINKEDEYE_DOMAIN=a.example.com\nLINKEDEYE_HTTPS_PORT=8443\n"), "https://a.example.com:8443")
        self.assertEqual(self.lib("public_url", "LINKEDEYE_MODE=http\nLINKEDEYE_DOMAIN=localhost\nLINKEDEYE_HTTP_PORT=8080\n"), "http://localhost:8080")
        self.assertEqual(self.lib("public_url", "LINKEDEYE_MODE=http\nLINKEDEYE_DOMAIN=localhost\nLINKEDEYE_HTTP_PORT=80\n"), "http://localhost")
        self.assertEqual(self.lib("public_url", "LINKEDEYE_MODE=internal\nLINKEDEYE_DOMAIN=10.0.0.5\n"), "https://10.0.0.5")

    def test_set_env_handles_awkward_values_and_keeps_the_mode(self):
        self.env_file.write_text("A=1\nB=2\n")
        self.env_file.chmod(0o600)
        self.lib("set_env A 'x/y&z|w\\1 $HOME'; set_env C new")
        text = self.env_file.read_text()
        # compose reads .env too: a bare $HOME would be interpolated, so awkward values are single-quoted (kept literally)
        self.assertEqual(text, "A='x/y&z|w\\1 $HOME'\nB=2\nC=new\n")
        self.assertEqual(stat.S_IMODE(self.env_file.stat().st_mode), 0o600)
        self.assertEqual(self.lib("get_env A"), "x/y&z|w\\1 $HOME")          # and get_env gives the literal back

    def test_set_env_refuses_a_value_that_cannot_be_quoted(self):
        self.env_file.write_text("A=1\n")
        proc = subprocess.run([BASH, "-c", f'. "{self.live}/lib.sh"; set_env A "it\'s"'], capture_output=True, text=True,
                              env=make_env(self.stub), timeout=30)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("cannot be written", proc.stderr)
        self.assertEqual(self.env_file.read_text(), "A=1\n")

    def test_is_private_host(self):
        out = self.lib('for h in localhost 10.1.2.3 192.168.0.9 172.16.4.4 172.32.0.1 100.100.1.1 169.254.1.1 8.8.8.8 box nas.local '
                       'linkedeye.test a.example.com ::1 2001:db8::1 fd12:3456::1; do if is_private_host "$h"; then echo "$h private"; '
                       'else echo "$h public"; fi; done')
        self.assertEqual(dict(line.split() for line in out.splitlines()), {
            "localhost": "private", "10.1.2.3": "private", "192.168.0.9": "private", "172.16.4.4": "private", "172.32.0.1": "public",
            "100.100.1.1": "private", "169.254.1.1": "private", "8.8.8.8": "public", "box": "private", "nas.local": "private",
            "linkedeye.test": "private", "a.example.com": "public", "::1": "private", "2001:db8::1": "public", "fd12:3456::1": "private"})

    def test_set_env_does_not_touch_similarly_named_keys(self):
        self.env_file.write_text("LINKEDEYE_DOMAIN_EXTRA=1\nLINKEDEYE_DOMAIN=2\n")
        self.lib("set_env LINKEDEYE_DOMAIN 3")
        self.assertEqual(self.env_file.read_text(), "LINKEDEYE_DOMAIN_EXTRA=1\nLINKEDEYE_DOMAIN=3\n")

    def test_get_env_default_and_set_if_blank(self):
        self.assertEqual(self.lib("get_env MISSING fallback", "A=\nB=value=with=equals\n"), "fallback")
        self.assertEqual(self.lib("get_env B", "B=value=with=equals\n"), "value=with=equals")
        self.lib("set_env_if_blank A filled; set_env_if_blank B kept", "A=\nB=old\n")
        self.assertEqual(self.env_file.read_text(), "A=filled\nB=old\n")

    def test_random_helpers(self):
        out = [line for line in self.lib("rand_alnum 40; rand_alnum 40; rand_fernet_key").splitlines() if line]
        self.assertEqual(len(out[0]), 40)
        self.assertNotEqual(out[0], out[1])
        self.assertRegex(out[2], r"^[A-Za-z0-9_-]{43}=$")

    def test_is_ip_address(self):
        self.assertEqual(self.lib('is_ip_address 10.1.2.3 && echo yes; is_ip_address a.example.com || echo no; is_ip_address ::1 && echo v6'),
                         "yes\nno\nv6\n")

    def test_quiet_shows_output_only_on_failure(self):
        proc = subprocess.run([BASH, "-c", f'. "{self.live}/lib.sh"; quiet echo hidden; quiet sh -c "echo shown; exit 3" || echo "rc=$?"'],
                              capture_output=True, text=True, env=make_env(self.stub))
        self.assertNotIn("hidden", proc.stdout + proc.stderr)
        self.assertIn("shown", proc.stderr)
        self.assertIn("rc=3", proc.stdout)


class BackupTests(LiveCase):
    def setUp(self):
        super().setUp()
        self.env_file.write_text("LINKEDEYE_DOMAIN=localhost\nLINKEDEYE_MODE=http\nLINKEDEYE_CREDENTIAL_KEY=cred-key-AAAA\nLINKEDEYE_SECRET_KEY=sekret\n")
        self.backups = self.tmp / "backups"

    def archives(self):
        return sorted(self.backups.glob("linkedeye-*.tar"))

    def test_archive_contents_permissions_and_checksums(self):
        proc = self.run_script("backup.sh", "--dir", str(self.backups))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        [archive] = self.archives()
        self.assertEqual(stat.S_IMODE(archive.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.backups.stat().st_mode), 0o700)
        names = subprocess.run(["tar", "-tf", str(archive)], capture_output=True, text=True).stdout.split()
        for expected in ("./db.dump", "./env", "./MANIFEST", "./SHA256SUMS"):
            self.assertIn(expected, names)
        unpacked = self.tmp / "unpacked"
        unpacked.mkdir()
        subprocess.run(["tar", "-C", str(unpacked), "-xf", str(archive)], check=True)
        self.assertEqual((unpacked / "env").read_text(), self.env_file.read_text())          # the credential key travels with the dump
        self.assertTrue((unpacked / "db.dump").read_bytes().startswith(b"PGDMP"))
        sums = subprocess.run(["sha256sum", "-c", "--quiet", "SHA256SUMS"], cwd=unpacked, capture_output=True, text=True)
        self.assertEqual(sums.returncode, 0, sums.stderr)
        self.assertIn("created_utc=", (unpacked / "MANIFEST").read_text())
        self.assertFalse(list(self.backups.glob(".work-*")), "temporary work directory left behind")
        self.assertFalse((self.backups / ".backup.lock").exists())

    def test_quiet_prints_only_the_path(self):
        proc = self.run_script("backup.sh", "--dir", str(self.backups), "--quiet")
        self.assertEqual(proc.stdout.strip(), str(self.archives()[0]))

    def test_retention_keeps_the_newest_three_whatever_their_age(self):
        self.backups.mkdir()
        now = time.time()
        for i, days in enumerate((90, 60, 45, 30, 20, 1)):
            f = self.backups / f"linkedeye-2026010{i}T000000Z.tar"
            f.write_text("x")
            os.utime(f, (now - days * 86400, now - days * 86400))
        proc = self.run_script("backup.sh", "--dir", str(self.backups), "--keep-days", "14")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        left = [a.name for a in self.archives()]
        self.assertEqual(len(left), 3, left)                                          # the new one + the two newest old ones
        self.assertIn("linkedeye-20260105T000000Z.tar", left)                         # 1 day old
        self.assertIn("linkedeye-20260104T000000Z.tar", left)                         # 20 days old but within the newest three
        self.assertNotIn("linkedeye-20260100T000000Z.tar", left)

    def test_recent_archives_are_never_deleted(self):
        self.backups.mkdir()
        for i in range(6):
            (self.backups / f"linkedeye-2026010{i}T000000Z.tar").write_text("x")
        self.run_script("backup.sh", "--dir", str(self.backups), "--keep-days", "14")
        self.assertEqual(len(self.archives()), 7)

    def test_a_failed_dump_leaves_nothing_behind(self):
        proc = self.run_script("backup.sh", "--dir", str(self.backups), env={"STUB_DUMP_EMPTY": "1"})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("empty", proc.stderr)
        self.assertEqual(self.archives(), [])
        self.assertEqual(list(self.backups.glob("*.partial")), [])
        self.assertFalse(list(self.backups.glob(".work-*")))
        self.assertFalse((self.backups / ".backup.lock").exists())

    def test_an_unreadable_dump_is_not_kept(self):
        proc = self.run_script("backup.sh", "--dir", str(self.backups), env={"STUB_LIST_RC": "1"})
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self.archives(), [])

    def test_unhealthy_database_is_refused(self):
        proc = self.run_script("backup.sh", "--dir", str(self.backups), env={"STUB_UNHEALTHY": "db"})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("not healthy", proc.stderr)
        self.assertEqual(self.archives(), [])

    def test_a_second_run_while_one_is_active_is_refused(self):
        (self.backups / ".backup.lock").mkdir(parents=True)
        proc = self.run_script("backup.sh", "--dir", str(self.backups))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("another backup is running", proc.stderr)
        self.assertTrue((self.backups / ".backup.lock").exists())          # a foreign lock is not removed

    def test_a_stale_lock_is_taken_over(self):
        lock = self.backups / ".backup.lock"
        lock.mkdir(parents=True)
        os.utime(lock, (1, 1))
        proc = self.run_script("backup.sh", "--dir", str(self.backups))
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_keep_days_must_be_a_number(self):
        proc = self.run_script("backup.sh", "--dir", str(self.backups), "--keep-days", "soon")
        self.assertNotEqual(proc.returncode, 0)

    def test_no_env_file_no_backup(self):
        self.env_file.unlink()
        proc = self.run_script("backup.sh", "--dir", str(self.backups))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("nothing to back up", proc.stderr)


class RestoreTests(LiveCase):
    def setUp(self):
        super().setUp()
        self.env_file.write_text("LINKEDEYE_DOMAIN=localhost\nLINKEDEYE_MODE=http\nLINKEDEYE_CREDENTIAL_KEY=current-key\nLINKEDEYE_SECRET_KEY=s\nLINKEDEYE_DB_PASSWORD=p\n")
        self.backups = self.tmp / "backups"
        self.run_script("backup.sh", "--dir", str(self.backups), "--quiet")
        [self.archive] = list(self.backups.glob("linkedeye-*.tar"))
        (self.stub / "calls.log").unlink()

    def test_restore_pipes_the_dump_into_pg_restore_and_restarts(self):
        proc = self.run_script("restore.sh", str(self.archive), "--yes", "--no-safety-backup")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue((self.stub / "restored.dump").read_bytes().startswith(b"PGDMP"))
        calls = "\n".join(self.calls())
        self.assertLess(calls.index(" stop web worker caddy"), calls.index("pg_restore --no-owner"))
        self.assertLess(calls.index("pg_restore --no-owner"), calls.rindex(" up -d"))         # migrations and start come after

    def test_a_damaged_archive_is_refused_before_anything_is_touched(self):
        work = self.tmp / "work"
        work.mkdir()
        subprocess.run(["tar", "-C", str(work), "-xf", str(self.archive)], check=True)
        (work / "db.dump").write_bytes(b"corrupted")
        broken = self.tmp / "broken.tar"
        subprocess.run(["tar", "-C", str(work), "-cf", str(broken), "."], check=True)
        proc = self.run_script("restore.sh", str(broken), "--yes")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("checksum mismatch", proc.stderr)
        self.assertFalse((self.stub / "restored.dump").exists())
        self.assertFalse(any(" stop " in c for c in self.calls()))

    def test_not_a_backup(self):
        junk = self.tmp / "junk.tar"
        (self.tmp / "x.txt").write_text("hello")
        subprocess.run(["tar", "-C", str(self.tmp), "-cf", str(junk), "x.txt"], check=True)
        proc = self.run_script("restore.sh", str(junk), "--yes")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("does not look like a LinkedEye backup", proc.stderr)

    def test_without_yes_and_without_a_terminal_it_refuses(self):
        proc = self.run_script("restore.sh", str(self.archive), stdin="")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--yes", proc.stderr)
        self.assertFalse((self.stub / "restored.dump").exists())

    def test_a_failed_pg_restore_is_an_error(self):
        proc = self.run_script("restore.sh", str(self.archive), "--yes", "--no-safety-backup", env={"STUB_RESTORE_RC": "1"})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("pg_restore failed", proc.stderr)

    def test_pg_restore_is_one_transaction(self):
        # a failure half-way must not leave the database with its old tables dropped and no new ones: the schema is emptied and
        # refilled in ONE transaction that only commits when pg_restore read the whole dump (test_restore_postgres runs it for real)
        self.assertEqual(self.run_script("restore.sh", str(self.archive), "--yes", "--no-safety-backup").returncode, 0)
        call = "\n".join(self.calls())
        call = call[call.index("exec -T db sh -c"):]
        self.assertLess(call.index("BEGIN;"), call.index("DROP SCHEMA IF EXISTS public CASCADE"))
        self.assertLess(call.index("DROP SCHEMA IF EXISTS public CASCADE"), call.index("pg_restore --no-owner"))
        self.assertIn("then printf 'COMMIT;", call)
        self.assertIn("ON_ERROR_STOP=1", call)
        self.assertNotIn("--clean", call)                  # --clean drops only what is in the dump: newer tables would block it

    def test_a_failed_restore_says_the_database_was_rolled_back(self):
        proc = self.run_script("restore.sh", str(self.archive), "--yes", "--no-safety-backup", env={"STUB_RESTORE_RC": "1"})
        self.assertIn("rolled back", proc.stderr)

    def test_a_symlink_inside_the_archive_is_refused(self):
        work = self.tmp / "work"
        work.mkdir()
        subprocess.run(["tar", "-C", str(work), "-xf", str(self.archive)], check=True)
        (work / "env").unlink()
        (work / "env").symlink_to("/etc/hostname")
        bad = self.tmp / "symlink.tar"
        subprocess.run(["tar", "-C", str(work), "-cf", str(bad), "."], check=True)
        proc = self.run_script("restore.sh", str(bad), "--yes")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("not a plain file", proc.stderr)

    def test_restore_env_keeps_this_servers_database_login(self):
        # the role lives in the PostgreSQL volume, not in the dump: an archived password would lock web and worker out
        self.env_file.write_text(self.env_file.read_text().replace("LINKEDEYE_DB_PASSWORD=p", "LINKEDEYE_DB_PASSWORD=rotated-since"))
        proc = self.run_script("restore.sh", str(self.archive), "--yes", "--no-safety-backup", "--restore-env")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        env = self.env_values()
        self.assertEqual(env["LINKEDEYE_DB_PASSWORD"], "rotated-since")
        self.assertEqual(env["LINKEDEYE_CREDENTIAL_KEY"], "current-key")          # everything else comes from the archive

    def test_a_safety_backup_is_taken_first(self):
        proc = self.run_script("restore.sh", str(self.archive), "--yes")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(list((self.live / "backups").glob("linkedeye-*.tar"))), 1)          # default backup directory

    def test_new_server_uses_the_archived_env(self):
        archived = self.env_file.read_text()
        self.env_file.unlink()
        proc = self.run_script("restore.sh", str(self.archive), "--yes", "--no-safety-backup")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.env_file.read_text(), archived)
        self.assertEqual(stat.S_IMODE(self.env_file.stat().st_mode), 0o600)

    def test_differing_credential_key_is_called_out(self):
        self.env_file.write_text(self.env_file.read_text().replace("current-key", "a-different-key"))
        proc = self.run_script("restore.sh", str(self.archive), "--yes", "--no-safety-backup")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("LINKEDEYE_CREDENTIAL_KEY differs", proc.stderr)
        self.assertIn("unreadable", proc.stderr)
        self.assertIn("a-different-key", self.env_file.read_text())          # not overwritten without --restore-env

    def test_restore_env_keeps_the_previous_one(self):
        self.env_file.write_text(self.env_file.read_text().replace("current-key", "a-different-key"))
        proc = self.run_script("restore.sh", str(self.archive), "--yes", "--no-safety-backup", "--restore-env")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("current-key", self.env_file.read_text())
        [old] = list(self.live.glob(".env.before-restore-*"))
        self.assertIn("a-different-key", old.read_text())


class HealthcheckTests(LiveCase):
    def serve(self, routes):
        return f"http://127.0.0.1:{serve_routes(self, routes)}"

    GOOD = GOOD_SITE

    def test_all_good(self):
        url = self.serve(self.GOOD)
        self.env_file.write_text("LINKEDEYE_MODE=http\n")
        proc = self.run_script("healthcheck.sh", url, env={"STUB_INFO": "down"})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("all checks passed", proc.stdout)

    def test_unhealthy_database_fails(self):
        url = self.serve({**self.GOOD, "/healthz": (503, {}, b'{"status": "error"}')})
        self.env_file.write_text("LINKEDEYE_MODE=http\n")
        proc = self.run_script("healthcheck.sh", url, env={"STUB_INFO": "down"})
        self.assertEqual(proc.returncode, 1)
        self.assertIn("/healthz did not answer ok", proc.stderr)

    def test_missing_static_files_fail(self):
        url = self.serve({**self.GOOD, "/static/linkedeye/app.css": (404, {}, b"")})
        self.env_file.write_text("LINKEDEYE_MODE=http\n")
        proc = self.run_script("healthcheck.sh", url, env={"STUB_INFO": "down"})
        self.assertEqual(proc.returncode, 1)
        self.assertIn("unstyled", proc.stderr)

    def test_nothing_listening(self):
        self.env_file.write_text("LINKEDEYE_MODE=http\n")
        proc = self.run_script("healthcheck.sh", "http://127.0.0.1:9", env={"STUB_INFO": "down"})
        self.assertEqual(proc.returncode, 1)

    def test_a_stopped_container_fails_even_when_http_answers(self):
        url = self.serve(self.GOOD)
        self.env_file.write_text("LINKEDEYE_MODE=http\n")
        proc = self.run_script("healthcheck.sh", url, env={"STUB_UNHEALTHY": "worker"})
        self.assertEqual(proc.returncode, 1)
        self.assertIn("worker is 'unhealthy'", proc.stderr)

    def test_observability_services_are_checked_when_enabled(self):
        url = self.serve(self.GOOD)
        self.env_file.write_text("LINKEDEYE_MODE=http\nCOMPOSE_PROFILES=observability\n")
        proc = self.run_script("healthcheck.sh", url, env={"STUB_UNHEALTHY": "prometheus"})
        self.assertEqual(proc.returncode, 1)
        self.assertIn("prometheus is 'unhealthy'", proc.stderr)

    def test_quiet_prints_nothing_on_success(self):
        url = self.serve(self.GOOD)
        self.env_file.write_text("LINKEDEYE_MODE=http\n")
        proc = self.run_script("healthcheck.sh", url, "--quiet", env={"STUB_INFO": "down"})
        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "", ""))


class UpdateTests(LiveCase):
    def setUp(self):
        super().setUp()
        self.env_file.write_text("LINKEDEYE_DOMAIN=localhost\nLINKEDEYE_MODE=http\nLINKEDEYE_HTTP_PORT=1\n")

    def test_backup_comes_before_the_build_and_the_migration(self):
        self.run_script("update.sh", "--no-pull", "--yes", timeout=120)          # port 1: the final health check fails, which is not the point here
        calls = "\n".join(self.calls())
        self.assertLess(calls.index("pg_dump"), calls.index(" build web"))
        self.assertLess(calls.index(" build web"), calls.index(" up -d"))
        self.assertTrue(list((self.live / "backups").glob("linkedeye-*.tar")))
        self.assertTrue((self.live / ".last-update").exists())
        self.assertIn("backup=", (self.live / ".last-update").read_text())

    def test_failed_backup_stops_the_update_before_anything_changes(self):
        proc = self.run_script("update.sh", "--no-pull", "--yes", env={"STUB_DUMP_EMPTY": "1"}, timeout=120)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("the stack was not touched", proc.stderr)
        calls = "\n".join(self.calls())
        self.assertNotIn(" build ", calls)
        self.assertNotIn(" up -d", calls)

    def test_unhealthy_release_prints_rollback_instructions_and_exits_nonzero(self):
        proc = self.run_script("update.sh", "--no-pull", "--no-build", "--yes", env={"STUB_UNHEALTHY": "web"}, timeout=120)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("not healthy", proc.stderr)
        self.assertIn("restore.sh", proc.stdout)
        self.assertIn("git checkout", proc.stdout)

    def test_without_a_terminal_it_needs_yes(self):
        proc = self.run_script("update.sh", "--no-pull", stdin="")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--yes", proc.stderr)

    def test_a_dirty_working_tree_is_refused(self):
        repo = self.tmp
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=t", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=t", "commit", "-qm", "x", "--no-verify"], cwd=repo, check=True,
                       env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null"})
        (self.live / "bootstrap.sh").write_text("# local edit\n")
        proc = self.run_script("update.sh", "--yes")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("local changes", proc.stderr)
        self.assertFalse(any("pg_dump" in c for c in self.calls()))

    def test_a_ref_that_looks_like_an_option_is_refused(self):
        subprocess.run(["git", "init", "-q"], cwd=self.tmp, check=True)
        subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=t", "add", "."], cwd=self.tmp, check=True)
        subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=t", "commit", "-qm", "x", "--no-verify"], cwd=self.tmp, check=True,
                       env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null"})
        proc = self.run_script("update.sh", "--yes", "--ref", "--orphan=x")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("looks like an option", proc.stderr)
        self.assertFalse(any("pg_dump" in c for c in self.calls()))


if __name__ == "__main__":
    unittest.main()
