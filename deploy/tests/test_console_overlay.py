"""The second console (deploy/compose.console.env + docker-compose.console.yml) beside the main compose stack.

    pip install pyyaml && python -m unittest discover -s deploy/tests -v

Both consoles run on the same host (127.0.0.1:8000 and 127.0.0.1:8014). Browsers scope cookies by host, not port, so
the second console needs its own cookie names, and its host names / CSRF origins must reach the web container: a second
`--env-file` only feeds `${...}` substitution in the compose files, never the containers.
"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONSOLE_ENV = ROOT / "deploy" / "compose.console.env"
OVERRIDE = ROOT / "docker-compose.console.yml"
DEFAULT_COOKIES = {"LINKEDEYE_CSRF_COOKIE_NAME": "linkedeyecsrftoken", "LINKEDEYE_SESSION_COOKIE_NAME": "linkedeyesessionid"}
WEB_SETTINGS = ("LINKEDEYE_ALLOWED_HOSTS", "LINKEDEYE_CSRF_TRUSTED_ORIGINS", *DEFAULT_COOKIES)


def env_assignments(path):
    out = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


class StaticTests(unittest.TestCase):
    def test_override_sets_the_web_settings_the_second_console_needs(self):
        self.assertTrue(OVERRIDE.is_file(), "docker-compose.console.yml missing")
        web_env = yaml.safe_load(OVERRIDE.read_text())["services"]["web"]["environment"]
        for name in WEB_SETTINGS:
            self.assertIn(name, web_env)

    def test_every_console_env_value_is_used_by_a_compose_file(self):
        """A value in the env-file that no compose file substitutes is silently ignored (it never reaches a container)."""
        compose_text = (ROOT / "docker-compose.yml").read_text() + OVERRIDE.read_text()
        for name in env_assignments(CONSOLE_ENV):
            self.assertRegex(compose_text, r"\$\{" + re.escape(name) + r"\b", f"{name} is set but never used")

    def test_documented_command_loads_the_override(self):
        for path in (CONSOLE_ENV, ROOT / "docs" / "FINSPOT_DEV_K8S.md"):
            self.assertIn("-f docker-compose.console.yml", path.read_text(), path)


@unittest.skipUnless(shutil.which("docker") and subprocess.run(["docker", "compose", "version"], capture_output=True).returncode == 0,
                     "the Docker Compose plugin is not installed")
class RenderTests(unittest.TestCase):
    """`docker compose config` for both stacks, exactly as documented (no daemon needed)."""

    def render(self, console):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            shutil.copy(ROOT / "docker-compose.yml", tmp)
            shutil.copy(OVERRIDE, tmp)
            (tmp / "deploy").mkdir()
            shutil.copy(CONSOLE_ENV, tmp / "deploy")
            env = (ROOT / "deploy" / "compose.env.example").read_text()
            env = env.replace("LINKEDEYE_SECRET_KEY=\n", "LINKEDEYE_SECRET_KEY=ci-only-not-a-secret-0123456789abcdefghijklmnopqrstuvwxyzABCDEF\n")
            env = env.replace("LINKEDEYE_DB_PASSWORD=\n", "LINKEDEYE_DB_PASSWORD=ci-only-password\n")
            (tmp / ".env").write_text(env)
            args = ["docker", "compose", "--project-directory", str(tmp)]
            if console:
                args += ["-p", "linkedeye-console", "--env-file", ".env", "--env-file", "deploy/compose.console.env",
                         "-f", "docker-compose.yml", "-f", "docker-compose.console.yml"]
            proc = subprocess.run([*args, "config", "web"], capture_output=True, text=True, timeout=60, cwd=tmp,
                                  # the shell's own values win over the env files in compose: render from the files alone
                                  env={k: v for k, v in os.environ.items()
                                       if not k.startswith(("LINKEDEYE_", "CONSOLE_", "COMPOSE_", "WEB_PUBLISH"))})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        web = yaml.safe_load(proc.stdout)["services"]["web"]
        return web["environment"], web["ports"][0]

    def test_second_console_gets_its_own_cookies_hosts_and_origins(self):
        main_env, main_port = self.render(console=False)
        env, port = self.render(console=True)
        self.assertEqual(str(port["published"]), "8014")
        self.assertEqual(str(main_port["published"]), "8000")
        for name, default in DEFAULT_COOKIES.items():
            self.assertTrue(env.get(name), name)
            self.assertNotEqual(env[name], main_env.get(name, default), f"{name} collides with the main console")
        hosts = env["LINKEDEYE_ALLOWED_HOSTS"].split(",")
        for host in ("localhost", "127.0.0.1"):
            self.assertIn(host, hosts)
        origins = env["LINKEDEYE_CSRF_TRUSTED_ORIGINS"].split(",")
        for origin in ("http://localhost:8014", "http://127.0.0.1:8014"):
            self.assertIn(origin, origins)
        # the rest of the stack is untouched: same secret handling, same database settings
        for name in ("LINKEDEYE_SECRET_KEY", "LINKEDEYE_DB_HOST", "LINKEDEYE_DB_PASSWORD"):
            self.assertEqual(env[name], main_env[name])


if __name__ == "__main__":
    unittest.main()
