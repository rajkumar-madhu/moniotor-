"""Regression tests for the deploy findings of the 12f4bbb verification (DEP-2 .. DEP-8; DEP-1 is test_restore_postgres)."""
import ipaddress
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from test_live import BASH, LIVE, ROOT, LiveCase, serve_routes

K8S = ROOT / "deploy" / "kubernetes"


class InternalModeCaddyTests(unittest.TestCase):
    def setUp(self):
        self.text = (LIVE / "caddy" / "internal.Caddyfile").read_text()
        self.global_block = self.text[self.text.index("{"):self.text.index("\n}\n")]

    def test_dep2_a_client_without_sni_still_gets_the_certificate(self):
        # https://<IP>: browsers and curl send no SNI; behind Docker NAT the local address is the bridge IP, so Caddy needs a default
        self.assertRegex(self.global_block, r"\n\s*default_sni \{\$LINKEDEYE_DOMAIN\}\n")

    def test_dep7_the_http_redirect_keeps_the_published_https_port(self):
        block = re.search(r"\nhttp://\{\$LINKEDEYE_DOMAIN\} \{\n(.*?)\n\}", self.text, re.S)
        self.assertIsNotNone(block, "internal.Caddyfile needs an explicit http:// site that redirects with the port")
        self.assertRegex(block.group(1), r"redir https://\{host\}:\{\$LINKEDEYE_HTTPS_PORT(:443)?\}\{uri\} (308|permanent)")
        caddy = yaml.safe_load((LIVE / "docker-compose.live.yml").read_text())["services"]["caddy"]
        self.assertEqual(caddy["environment"]["LINKEDEYE_HTTPS_PORT"], "${LINKEDEYE_HTTPS_PORT:-443}")


class InternalModeRedirectHealthcheckTests(LiveCase):
    """healthcheck.sh checks the HTTP -> HTTPS redirect in mode internal too, including the port (DEP-7)."""

    def check(self, location):
        routes = {"/healthz": (200, {}, b'{"status": "ok"}'), "/accounts/login/": (200, {"Strict-Transport-Security": "max-age=1"}, b"<form>"),
                  "/static/linkedeye/app.css": (200, {}, b"a{}"), "/": (308, {"Location": location}, b"")}
        port = serve_routes(self, routes)
        self.env_file.write_text(f"LINKEDEYE_MODE=internal\nLINKEDEYE_DOMAIN=127.0.0.1\nLINKEDEYE_HTTP_PORT={port}\nLINKEDEYE_HTTPS_PORT=8443\n")
        return self.run_script("healthcheck.sh", f"http://127.0.0.1:{port}", env={"STUB_INFO": "down"})

    def test_a_redirect_that_drops_the_port_fails(self):
        proc = self.check("https://127.0.0.1/")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("without the port 8443", proc.stderr)

    def test_a_redirect_to_the_published_port_passes(self):
        proc = self.check("https://127.0.0.1:8443/")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("redirects to HTTPS", proc.stdout)


class BindAddressTests(LiveCase):
    """DEP-4: a 127.0.0.1 the operator chose is never cleared by a re-run."""

    def pin_to_loopback(self):
        self.env_file.write_text(re.sub(r"(?m)^LINKEDEYE_BIND_ADDRESS=.*$", "LINKEDEYE_BIND_ADDRESS=127.0.0.1", self.env_file.read_text()))

    def test_an_operator_choice_survives_a_rerun(self):
        cases = [(["--domain", "linkedeye.local", "--mode", "http"], {}),
                 (["--domain", "linkedeye.corp.example.com", "--email", "ops@corp.example.com"], {"LINKEDEYE_EMAIL_HOST": "smtp.corp.example.com"})]
        for args, env in cases:
            with self.subTest(args[1]):
                if self.env_file.exists():
                    self.env_file.unlink()
                self.assertEqual(self.prepare(*args, env=env).returncode, 0)
                self.assertEqual(self.env_values()["LINKEDEYE_BIND_ADDRESS"], "")
                self.pin_to_loopback()
                proc = self.prepare(env=env)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(self.env_values()["LINKEDEYE_BIND_ADDRESS"], "127.0.0.1")
                self.assertIn("127.0.0.1 only", proc.stdout)

    def test_leaving_a_localhost_trial_still_widens_it_but_says_so(self):
        self.assertEqual(self.prepare("--domain", "localhost", "--mode", "http").returncode, 0)
        self.assertEqual(self.env_values()["LINKEDEYE_BIND_ADDRESS"], "127.0.0.1")
        proc = self.prepare("--domain", "10.0.0.5", "--mode", "http")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.env_values()["LINKEDEYE_BIND_ADDRESS"], "")
        self.assertIn("every interface", proc.stderr)


class IntegrationsSmtpTests(LiveCase):
    """DEP-5: SMTP saved on Administration > Integrations counts, and an explicit LINKEDEYE_LOGIN_OTP=true is never downgraded."""

    def set_line(self, key, value):
        self.env_file.write_text(re.sub(rf"(?m)^{key}=.*$", f"{key}={value}", self.env_file.read_text()))

    def test_https_rerun_accepts_smtp_from_the_integrations_page(self):
        args = ["--domain", "linkedeye.corp.example.com", "--email", "ops@corp.example.com"]
        self.assertEqual(self.prepare(*args, env={"LINKEDEYE_EMAIL_HOST": "smtp.corp.example.com"}).returncode, 0)
        self.set_line("LINKEDEYE_EMAIL_HOST", "")                          # SMTP moved to the database
        proc = self.prepare(env={"STUB_SMTP_HOST": "smtp.corp.example.com"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Administration > Integrations", proc.stdout)
        self.assertTrue(any("integration_config" in c for c in self.calls()))
        # and with nothing configured anywhere it still refuses
        proc = self.prepare(env={"STUB_SMTP_HOST": ""})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("no SMTP server is configured", proc.stderr)

    def test_http_mode_keeps_an_explicit_true(self):
        self.assertEqual(self.prepare("--domain", "linkedeye.local", "--mode", "http").returncode, 0)
        self.set_line("LINKEDEYE_LOGIN_OTP", "true")
        proc = self.prepare(env={"STUB_SMTP_HOST": "smtp.corp.example.com"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.env_values()["LINKEDEYE_LOGIN_OTP"], "true")
        # no SMTP anywhere: refuse, but never rewrite the operator's true to false
        proc = self.prepare()
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self.env_values()["LINKEDEYE_LOGIN_OTP"], "true")

    def test_a_blank_value_in_http_mode_is_still_switched_off(self):
        proc = self.prepare("--domain", "linkedeye.local", "--mode", "http")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.env_values()["LINKEDEYE_LOGIN_OTP"], "false")


class FinspotOverlayProbeRangeTests(unittest.TestCase):
    """DEP-3: no overlay may let probes reach the cluster's own API server."""

    def test_api_server_is_outside_every_probe_allow_list(self):
        checked = 0
        for overlay in sorted((K8S / "overlays").iterdir()):
            kubeconfig = overlay / "kubeconfig.example.yaml"
            patch = overlay / "configmap-patch.yaml"
            if not (kubeconfig.exists() and patch.exists()):
                continue
            server = yaml.safe_load(kubeconfig.read_text())["clusters"][0]["cluster"]["server"]
            api = ipaddress.ip_address(re.match(r"https://([0-9.]+):", server).group(1))
            cidrs = [c.strip() for c in yaml.safe_load(patch.read_text())["data"]["LINKEDEYE_PROBE_ALLOWED_PRIVATE_CIDRS"].split(",") if c.strip()]
            for cidr in cidrs:
                self.assertNotIn(api, ipaddress.ip_network(cidr), f"{overlay.name}: {cidr} contains the API server {api}")
            checked += 1
        self.assertGreaterEqual(checked, 1)


STUB_KUBECTL = r'''#!/usr/bin/env bash
printf '%s\n' "$*" >> "$STUB_DIR/kubectl.argv"
case "$*" in
  *" get secret "*) exit 1 ;;
  *"create secret"*) cat > "$STUB_DIR/kubectl.stdin"; sleep 0.2 ;;
esac
exit 0
'''


class CreateSecretsTests(unittest.TestCase):
    """DEP-6: generated secrets never appear in kubectl's argv (ps, /proc/<pid>/cmdline, audit logs)."""

    def test_secrets_go_through_stdin(self):
        if not BASH:
            self.skipTest("bash is required")
        if not shutil.which("python3"):
            self.skipTest("python3 is required by the script")
        tmp = Path(tempfile.mkdtemp(prefix="le-k8s-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "bin").mkdir()
        (tmp / "bin" / "kubectl").write_text(STUB_KUBECTL)
        (tmp / "bin" / "kubectl").chmod(0o755)
        env = {**os.environ, "PATH": f"{tmp / 'bin'}:{os.environ['PATH']}", "STUB_DIR": str(tmp), "DB_PASSWORD": "Db-Pass-Not-On-Argv-42"}
        proc = subprocess.run([BASH, str(K8S / "overlays" / "argus" / "create-secrets.sh")], capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        argv = (tmp / "kubectl.argv").read_text()
        values = dict(line.split("=", 1) for line in (tmp / "kubectl.stdin").read_text().splitlines())
        self.assertEqual(sorted(values), ["LINKEDEYE_ALERTMANAGER_WEBHOOK_TOKEN", "LINKEDEYE_CREDENTIAL_KEY", "LINKEDEYE_DB_PASSWORD",
                                          "LINKEDEYE_SECRET_KEY"])
        self.assertEqual(values["LINKEDEYE_DB_PASSWORD"], "Db-Pass-Not-On-Argv-42")
        self.assertGreaterEqual(len(values["LINKEDEYE_SECRET_KEY"]), 50)
        self.assertRegex(values["LINKEDEYE_CREDENTIAL_KEY"], r"^[A-Za-z0-9_-]{43}=$")
        for value in values.values():
            self.assertNotIn(value, argv)
        self.assertNotIn("--from-literal", argv)
        self.assertNotIn("Db-Pass-Not-On-Argv-42", proc.stdout + proc.stderr)


class HealthzDocTests(unittest.TestCase):
    """DEP-8: the health route is /healthz (no slash); /healthz/ answers 404."""

    def test_docs_never_send_operators_to_healthz_with_a_slash(self):
        for doc in sorted((ROOT / "docs").glob("*.md")):
            for n, line in enumerate(doc.read_text().splitlines(), 1):
                self.assertNotRegex(line, r"https?://\S*/healthz/(\s|$|[`'\"])", f"{doc.name}:{n}")


if __name__ == "__main__":
    unittest.main()
