"""Regression tests for the code-review findings in the deploy-docs area (network-6, deploy-docs-4, -6, -7, -11, -12, -13).

    python -m unittest discover -s deploy/tests -v

No Docker daemon, cluster or Django database needed.
"""
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ["deploy", "docker-compose.yml", "Dockerfile", ".github", "docs/DEPLOYMENT.md", "docs/GO_LIVE.md", "linkedeye/settings.py",
          "administration/services.py", "telemetry/nagios.py", ".env.example"]


def read(rel):
    return (ROOT / rel).read_text()


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MutatedBundle(unittest.TestCase):
    """deploy/validate.py run against a temporary copy of the bundle."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="le-review-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        for rel in BUNDLE:
            src, dst = ROOT / rel, self.tmp / rel
            if not src.exists():
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "tests"))
            else:
                shutil.copy2(src, dst)

    def run_validate(self):
        proc = subprocess.run([sys.executable, str(self.tmp / "deploy" / "validate.py")],
                              capture_output=True, text=True, timeout=60)
        self.assertNotIn("Traceback", proc.stderr, proc.stderr)
        return proc.returncode, proc.stdout

    def edit(self, rel, old, new):
        path = self.tmp / rel
        text = path.read_text()
        self.assertIn(old, text, f"fixture drift in {rel}")
        path.write_text(text.replace(old, new))

    def assertFails(self, needle):
        rc, out = self.run_validate()
        self.assertEqual(rc, 1, out)
        self.assertIn(needle, out)


# ------------------------------------------------------------------ deploy-docs-7 / network-6
def ingresses():
    docs = list(yaml.safe_load_all(read("deploy/kubernetes/ingress.yaml")))
    return [d for d in docs if d and d.get("kind") == "Ingress"]


def megabytes(value):
    m = re.fullmatch(r"(\d+)([kKmMgG]?)", str(value).strip())
    scale = {"": 1, "k": 1 / 1024, "m": 1, "g": 1024}[m.group(2).lower()]
    return int(m.group(1)) * scale


class IngressBodySizeTests(MutatedBundle):
    PATH = "/network/photos/find/"

    def _for_path(self, path):
        for ing in ingresses():
            for rule in ing["spec"]["rules"]:
                for p in rule["http"]["paths"]:
                    if p["path"] == path:
                        return ing
        return None

    def test_datasheet_upload_path_allows_a_26m_body(self):
        ing = self._for_path(self.PATH)
        self.assertIsNotNone(ing, "no Ingress routes /network/photos/find/ separately")
        ann = ing["metadata"]["annotations"]
        self.assertGreaterEqual(megabytes(ann["nginx.ingress.kubernetes.io/proxy-body-size"]), 26)
        self.assertEqual(ann["nginx.ingress.kubernetes.io/proxy-read-timeout"], "90")
        self.assertTrue(ing["spec"].get("tls"))

    def test_everything_else_keeps_the_small_limit(self):
        ing = self._for_path("/")
        ann = ing["metadata"]["annotations"]
        self.assertLessEqual(megabytes(ann["nginx.ingress.kubernetes.io/proxy-body-size"]), 3)

    def test_validate_rejects_a_small_limit_on_the_datasheet_path(self):
        self.edit("deploy/kubernetes/ingress.yaml", 'proxy-body-size: "26m"', 'proxy-body-size: "3m"')
        self.assertFails("/network/photos/find/")

    def test_validate_rejects_a_missing_datasheet_ingress(self):
        self.edit("deploy/kubernetes/ingress.yaml", "path: /network/photos/find/", "path: /network/photos/other/")
        self.assertFails("/network/photos/find/")


# ------------------------------------------------------------------ deploy-docs-4
class TrustedProxiesTests(MutatedBundle):
    def test_every_bundle_that_sets_the_gunicorn_peers_sets_the_audit_peers(self):
        self.assertRegex(read("deploy/compose.env.example"), r"(?m)^LINKEDEYE_TRUSTED_PROXIES=")
        self.assertIn("LINKEDEYE_TRUSTED_PROXIES", read("docker-compose.yml"))
        cm = next(d for d in yaml.safe_load_all(read("deploy/kubernetes/configmap.yaml"))
                  if d and d.get("kind") == "ConfigMap")
        self.assertIn("LINKEDEYE_TRUSTED_PROXIES", cm["data"])

    def test_validate_fails_when_the_configmap_drops_it(self):
        self.edit("deploy/kubernetes/configmap.yaml", "LINKEDEYE_TRUSTED_PROXIES:", "# LINKEDEYE_TRUSTED_PROXIES_x:")
        self.assertFails("LINKEDEYE_TRUSTED_PROXIES")

    def test_validate_fails_when_the_compose_env_example_drops_it(self):
        self.edit("deploy/compose.env.example", "LINKEDEYE_TRUSTED_PROXIES=", "# LINKEDEYE_TRUSTED_PROXIES_x=")
        self.assertFails("LINKEDEYE_TRUSTED_PROXIES")

    def test_nginx_walkthrough_mentions_it(self):
        doc = read("docs/DEPLOYMENT.md")
        section = doc[doc.index("Minimal nginx server block"):doc.index("**Using an external database.**")]
        self.assertIn("LINKEDEYE_TRUSTED_PROXIES", section)

    def test_gunicorn_warns_when_proxies_are_not_trusted_in_production(self):
        from test_bundle import gunicorn_conf
        server = mock.Mock()
        env = {"LINKEDEYE_SECRET_KEY": "k" * 60, "LINKEDEYE_DEBUG": "false", "LINKEDEYE_TRUSTED_PROXIES": ""}
        conf = gunicorn_conf(env)
        with mock.patch.dict(os.environ, env):
            conf["on_starting"](server)
        self.assertTrue(any("TRUSTED_PROXIES" in str(c) for c in server.log.warning.call_args_list))
        server = mock.Mock()
        env = {"LINKEDEYE_SECRET_KEY": "k" * 60, "LINKEDEYE_DEBUG": "false", "LINKEDEYE_TRUSTED_PROXIES": "10.0.0.1"}
        with mock.patch.dict(os.environ, env):
            gunicorn_conf(env)["on_starting"](server)
        server.log.warning.assert_not_called()


# ------------------------------------------------------------------ deploy-docs-6
class DockerignoreTests(unittest.TestCase):
    def lines(self):
        return [ln.strip() for ln in read(".dockerignore").splitlines() if ln.strip() and not ln.startswith("#")]

    def test_legacy_backend_docs_and_notes_stay_out_of_the_image(self):
        lines = self.lines()
        self.assertIn("backend/*", lines)
        self.assertIn("docs", lines)
        self.assertIn("*.md", lines)

    def test_assets_the_console_reads_stay_in(self):
        lines = self.lines()
        # network/legacy_panels.py reads iframeGraphs/ and falls back to backend/template/DIRECT
        self.assertNotIn("iframeGraphs", lines)
        self.assertIn("!backend/template", lines)
        self.assertLess(lines.index("backend/*"), lines.index("!backend/template"))

    def test_ci_asserts_the_image_has_no_legacy_tree(self):
        ci = read(".github/workflows/ci.yml")
        self.assertIn("/app/backend/SECRETS_AUDIT.md", ci)


# ------------------------------------------------------------------ deploy-docs-11
class SupplyChainTests(unittest.TestCase):
    def test_cryptography_floor_is_past_the_known_advisories(self):
        m = re.search(r"(?m)^cryptography>=(\d+)(?:\.(\d+))?(?:\.(\d+))?", read("requirements.txt"))
        floor = tuple(int(x or 0) for x in m.groups())
        self.assertGreaterEqual(floor, (44, 0, 1))

    def test_ci_audits_dependencies(self):
        ci = yaml.safe_load(read(".github/workflows/ci.yml"))
        text = yaml.dump(ci)
        self.assertIn("pip-audit", text)

    def test_dependabot_watches_pip_docker_and_actions(self):
        cfg = yaml.safe_load(read(".github/dependabot.yml"))
        self.assertEqual(cfg["version"], 2)
        self.assertGreaterEqual({u["package-ecosystem"] for u in cfg["updates"]}, {"pip", "docker", "github-actions"})


# ------------------------------------------------------------------ deploy-docs-12
SETTINGS_PROBE = "import runpy,sys; ns = runpy.run_path('linkedeye/settings.py'); print(ns['SECRET_KEY'], ns.get('SESSION_COOKIE_AGE'), ns.get('SESSION_SAVE_EVERY_REQUEST'))"


class SecretKeyTests(unittest.TestCase):
    def settings(self, **env):
        clean = {k: v for k, v in os.environ.items() if not k.startswith(("LINKEDEYE_", "DJANGO_"))}
        clean.update(env)
        return subprocess.run([sys.executable, "-c", SETTINGS_PROBE], cwd=ROOT, env=clean, capture_output=True, text=True)

    def test_placeholder_and_weak_keys_are_refused_in_production(self):
        for key in ("change-me-to-a-long-random-string", "dev-only-insecure-key", "short", "a" * 60,
                    "Change-Me-" + "x1y2z3" * 12):
            proc = self.settings(LINKEDEYE_DEBUG="false", LINKEDEYE_SECRET_KEY=key)
            self.assertNotEqual(proc.returncode, 0, key)
            self.assertIn("SECRET_KEY", proc.stderr, key)

    def test_a_long_random_key_is_accepted(self):
        key = "q7Jw9xKp2LmZr4TvB8nYc1HdFs6GuEa3Xo5PiRtVkMb0WzNjDy"
        proc = self.settings(LINKEDEYE_DEBUG="false", LINKEDEYE_SECRET_KEY=key)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(proc.stdout.startswith(key))

    def test_debug_keeps_the_short_development_key(self):
        proc = self.settings(LINKEDEYE_DEBUG="true", LINKEDEYE_SECRET_KEY="short")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_sessions_expire_after_eight_hours_of_inactivity_by_default(self):
        key = "q7Jw9xKp2LmZr4TvB8nYc1HdFs6GuEa3Xo5PiRtVkMb0WzNjDy"
        proc = self.settings(LINKEDEYE_DEBUG="false", LINKEDEYE_SECRET_KEY=key)
        self.assertEqual(proc.stdout.split()[1:], ["28800", "True"], proc.stderr)
        proc = self.settings(LINKEDEYE_DEBUG="false", LINKEDEYE_SECRET_KEY=key, LINKEDEYE_SESSION_COOKIE_AGE="3600",
                             LINKEDEYE_SESSION_SAVE_EVERY_REQUEST="false")
        self.assertEqual(proc.stdout.split()[1:], ["3600", "False"], proc.stderr)

    def test_example_files_and_build_use_acceptable_keys(self):
        # the example file must not ship a usable-looking placeholder; the Docker build key must pass the same check
        self.assertRegex(read(".env.example"), r"(?m)^LINKEDEYE_SECRET_KEY=\s*$")
        dockerfile = read("Dockerfile")
        key = re.search(r'LINKEDEYE_SECRET_KEY="([^"]+)"', dockerfile).group(1)
        proc = self.settings(LINKEDEYE_DEBUG="false", LINKEDEYE_SECRET_KEY=key)
        self.assertEqual(proc.returncode, 0, f"{key!r}: {proc.stderr}")


# ------------------------------------------------------------------ deploy-docs-13
class DocsAgreeWithCodeTests(MutatedBundle):
    def test_env_example_lists_every_setting(self):
        validate = load_module("le_validate", ROOT / "deploy" / "validate.py")
        text = read(".env.example")
        missing = sorted(n for n in validate.SETTINGS if not re.search(rf"(?m)^#?\s*{n}=", text))
        self.assertEqual(missing, [])

    def test_validate_fails_when_env_example_misses_a_setting(self):
        self.edit(".env.example", "LINKEDEYE_LOGIN_MAX_FAILURES_PER_IP=", "LINKEDEYE_LOGIN_MAX_FAILURES_PER_IPX=")
        rc, out = self.run_validate()
        self.assertEqual(rc, 1, out)
        self.assertIn(".env.example", out)
        self.assertIn("LINKEDEYE_LOGIN_MAX_FAILURES_PER_IP", out)

    def test_integration_md_is_described_as_the_porting_guide(self):
        line = next(ln for ln in read("docs/STRUCTURE.md").splitlines() if "INTEGRATION.md" in ln)
        self.assertNotIn("Alertmanager", line)
        self.assertIn("LinkedEyeWebProject", line)

    def test_modules_md_does_not_promise_template_permission_variables(self):
        doc = read("docs/MODULES.md")
        self.assertNotIn("perms_operate", doc)
        self.assertNotIn("le_can", doc)
        self.assertIn("can_operate", doc)

    def test_otp_paths_lists_the_console_public_paths(self):
        doc = read("docs/OTP_PATHS.md")
        for path in ("/welcome/", "/api/sd/", "/api/v1/alertmanager/", "/api/apm/"):
            self.assertIn(path, doc)

    def test_app_lists_cover_every_installed_app(self):
        settings = read("linkedeye/settings.py")
        block = settings[settings.index("INSTALLED_APPS"):settings.index("]", settings.index("INSTALLED_APPS"))]
        apps = [a for a in re.findall(r'"([a-z_]+)"', block) if not a.startswith("django")]
        for doc in ("README.md", "docs/STRUCTURE.md"):
            text = read(doc)
            for app in apps:
                self.assertRegex(text, rf"(?m)^(?:[│├└─ ]*){app}/", f"{doc} lacks {app}/")

    def test_heartbeat_interval_comment_matches_the_code(self):
        self.assertNotIn("every 60 s", read("deploy/kubernetes/probes-deployment.yaml"))
        self.assertIn("every 30 s", read("deploy/kubernetes/probes-deployment.yaml"))


if __name__ == "__main__":
    unittest.main()
