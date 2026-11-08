"""Verify and enable for Linux and Windows servers (onboarding/servers.py, ssh.py, install.py).

No test opens a socket: DNS, TCP, HTTP and SSH are all replaced by in-memory fakes (the base class
makes any real connection attempt fail the test). paramiko is a fake module too, so these tests run
whether or not the optional extra is installed.
"""
import base64
import re
import subprocess
from unittest import mock

from django.test import override_settings
from django.urls import reverse

from core import secrets
from core.models import AuditEvent
from onboarding import install, servers, ssh, steps
from onboarding.models import Credential, OnboardingTarget, StepLog, TargetState, TargetType

from .base import SECRET, OnboardingTestCase, with_key, without_key

IP = "10.0.0.5"
KEY_BLOB = b"host-key-blob-one"
OTHER_KEY_BLOB = b"host-key-blob-two"
PRIVATE_KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nQUJDREVGRw==\n-----END OPENSSH PRIVATE KEY-----\n"

NODE_BODY = """\
# HELP node_exporter_build_info A metric with a constant '1' value labeled by version, revision, branch.
# TYPE node_exporter_build_info gauge
node_exporter_build_info{branch="HEAD",goversion="go1.23.6",revision="abc",version="1.9.1"} 1
node_uname_info{domainname="(none)",machine="x86_64",nodename="db-01.example.test",release="6.8.0-31-generic",sysname="Linux",version="#31"} 1
node_os_info{id="ubuntu",name="Ubuntu",pretty_name="Ubuntu 24.04 LTS",version_id="24.04"} 1
node_memory_MemTotal_bytes 8.589934592e+09
node_cpu_seconds_total{cpu="0",mode="idle"} 1234.5
node_cpu_seconds_total{cpu="0",mode="user"} 12.5
node_cpu_seconds_total{cpu="1",mode="idle"} 1300.5
node_cpu_seconds_total{cpu="2",mode="idle"} 1301.5
node_cpu_seconds_total{cpu="3",mode="idle"} 1302.5
go_build_info{path="github.com/prometheus/node_exporter",version="(devel)"} 1
"""

WINDOWS_BODY = """\
# HELP windows_exporter_build_info A metric with a constant '1' value labeled by version.
# TYPE windows_exporter_build_info gauge
windows_exporter_build_info{branch="master",version="0.30.5"} 1
windows_cs_hostname{domain="corp.test",fqdn="win-01.corp.test",hostname="WIN-01"} 1
windows_os_info{build_number="17763",product="Microsoft Windows Server 2019 Standard",version="10.0.17763"} 1
windows_cs_logical_processors 8
windows_cs_physical_memory_bytes 1.7179869184e+10
"""


# ----------------------------------------------------------------------------- fake network
class FakeResponse:
    def __init__(self, status, body, content_type):
        self.status, self._body, self._ct = status, body, content_type
        self._pos = 0

    def getheader(self, name, default=None):
        return self._ct if name.lower() == "content-type" else default

    def read(self, n=-1):
        chunk = self._body[self._pos:self._pos + (n if n and n > 0 else len(self._body))]
        self._pos += len(chunk)
        return chunk


class FakeConn:
    def __init__(self, net, scheme, host, ip, port):
        self.net, self.scheme, self.host, self.ip, self.port = net, scheme, host, ip, port
        self._probe_sock = None

    def request(self, method, path, headers=None):
        self.net.requests.append({"method": method, "path": path, "headers": dict(headers or {}),
                                  "ip": self.ip, "port": self.port, "scheme": self.scheme})

    def getresponse(self):
        net = self.net
        if net.http_error is not None:
            raise net.http_error
        up = net.exporter_up
        return FakeResponse(net.status if up else 404, net.body.encode() if up else b"", net.content_type)

    def close(self):
        pass


class FakeSock:
    def close(self):
        pass


class Net:
    """DNS, TCP and HTTP of one fake world."""

    def __init__(self, ips=(IP,), tcp="open", status=200, body=NODE_BODY, content_type="text/plain",
                 ssh_port="open"):
        self.ips, self.tcp, self.status, self.body, self.content_type = list(ips), tcp, status, body, content_type
        self.ssh_port = ssh_port
        self.exporter_up = True
        self.http_error = None
        self.requests = []
        self.connects = []

    # replaces probes.checks.resolve
    def resolve(self, host, port=443):
        import socket
        if not self.ips:
            raise socket.gaierror(-2, "Name or service not known")
        return list(self.ips)

    # replaces socket.create_connection
    def create_connection(self, addr, timeout=None, *a, **k):
        import socket
        ip, port = addr
        self.connects.append((ip, port))
        mode = self.ssh_port if port == ssh.SSH_PORT else self.tcp
        if mode == "refused":
            raise ConnectionRefusedError()
        if mode == "timeout":
            raise socket.timeout()
        return FakeSock()

    def open_connection(self, scheme, host, ip, port, timeout):
        return FakeConn(self, scheme, host, ip, port)


# ----------------------------------------------------------------------------- fake paramiko
class FakeKey:
    def __init__(self, blob):
        self._blob = blob

    def asbytes(self):
        return self._blob

    def get_name(self):
        return "ssh-ed25519"


class World:
    """What the fake SSH server is, and what it was asked."""

    def __init__(self, net=None):
        self.net = net
        self.host_key = KEY_BLOB
        self.password = SECRET
        self.auth_fail = False
        self.connects = []
        self.credentials_sent = []
        self.commands = []
        self.unexpected = []
        self.stdin = []
        self.installs = []
        self.facts_out = {"uid": "0", "hostname": "db-01", "os_release": 'NAME="Ubuntu"\nPRETTY_NAME="Ubuntu 24.04 LTS"\n',
                          "kernel": "6.8.0-31-generic", "arch": "x86_64", "cpus": "4",
                          "memory": "MemTotal:        8388608 kB", "exporter_service": "inactive\ninactive",
                          "exporter_path": "", "exporter_version": ""}
        self.windows_out = ("hostname=WIN-01\nos=Microsoft Windows Server 2019 Standard\nkernel=10.0.17763\n"
                            "arch=AMD64\ncpus=8\nmemory_bytes=17179869184\nexporter_service=absent\n")
        self.install_result = (0, "node_exporter 1.9.1 installed and listening on :9100\n", "")
        self.on_install = lambda: None

    def respond(self, command):
        for name, known in ssh.LINUX_FACT_COMMANDS.items():
            if command == known:
                return 0, self.facts_out.get(name, ""), ""
        if command == ssh.WINDOWS_FACT_COMMANDS["facts"]:
            return 0, self.windows_out, ""
        if "-EncodedCommand" in command or "base64 -d" in command:
            self.installs.append(command)
            self.on_install()
            return self.install_result
        self.unexpected.append(command)
        return 127, "", "not allowed"


def fake_paramiko(world):
    class SSHException(Exception):
        pass

    class AuthenticationException(SSHException):
        pass

    class PasswordRequiredException(SSHException):
        pass

    class PKey:
        @staticmethod
        def from_private_key(handle, password=None):
            text = handle.read()
            if "ENCRYPTED" in text:
                raise PasswordRequiredException("passphrase")
            if "BEGIN" not in text:
                raise SSHException("not a key")
            return ("pkey", text)

    class Channel:
        def __init__(self):
            self._out, self._err, self.code = b"", b"", 0

        def settimeout(self, t):
            pass

        def exec_command(self, command):
            world.commands.append(command)
            code, out, err = world.respond(command)
            self.code, self._out, self._err = code, out.encode(), err.encode()

        def sendall(self, data):
            world.stdin.append(data)

        def shutdown_write(self):
            pass

        def recv_ready(self):
            return bool(self._out)

        def recv(self, n):
            chunk, self._out = self._out[:n], self._out[n:]
            return chunk

        def recv_stderr_ready(self):
            return bool(self._err)

        def recv_stderr(self, n):
            chunk, self._err = self._err[:n], self._err[n:]
            return chunk

        def exit_status_ready(self):
            return True

        def recv_exit_status(self):
            return self.code

        def close(self):
            pass

    class Transport:
        def __init__(self, key):
            self._key = key

        def open_session(self):
            return Channel()

        def get_remote_server_key(self):
            return self._key

    class SSHClient:
        def set_missing_host_key_policy(self, policy):
            self._policy = policy

        def connect(self, hostname=None, port=22, username=None, password=None, pkey=None, sock=None, **kw):
            world.connects.append({"hostname": hostname, "username": username, "password": password is not None,
                                   "key": pkey is not None})
            key = FakeKey(world.host_key)
            self._policy.missing_host_key(self, hostname, key)       # a changed key raises here, before auth
            world.credentials_sent.append(password if password is not None else "key")
            if world.auth_fail or (password is not None and password != world.password):
                raise AuthenticationException("denied")
            self._key = key

        def get_transport(self):
            return Transport(self._key)

        def close(self):
            pass

    module = mock.Mock()
    module.SSHException, module.AuthenticationException = SSHException, AuthenticationException
    module.PasswordRequiredException, module.PKey, module.SSHClient = PasswordRequiredException, PKey, SSHClient
    return module


# ----------------------------------------------------------------------------- the test case
@with_key
@override_settings(PROBE_ALLOWED_PRIVATE_CIDRS=["10.0.0.0/8"])
class ServersCase(OnboardingTestCase):
    def setUp(self):
        super().setUp()
        self.net = Net()
        self.world = World(self.net)
        self.paramiko = fake_paramiko(self.world)
        self.run_obj = self.make_run()
        self._patch("probes.checks.resolve", side_effect=lambda h, p=443: self.net.resolve(h, p))
        self._patch("probes.checks.resolve_allowed", side_effect=self._resolve_allowed)
        self._patch("socket.create_connection", side_effect=lambda *a, **k: self.net.create_connection(*a, **k))
        self._patch("probes.checks._open_connection", side_effect=lambda *a: self.net.open_connection(*a))
        self._patch("onboarding.ssh._paramiko", return_value=self.paramiko)
        self._patch("onboarding.ssh.available", return_value=True)
        self._patch("time.sleep")

    def _patch(self, target, **kwargs):
        patcher = mock.patch(target, **kwargs)
        started = patcher.start()
        self.addCleanup(patcher.stop)
        return started

    def _resolve_allowed(self, host, port):
        from probes import checks
        ips = self.net.resolve(host, port)
        for ip in ips:
            checks.check_address(ip)            # the real policy runs
            return ip
        raise checks.ProbeBlocked("none")

    def host(self, hostname="db-01", **kw):
        kw.setdefault("target_type", TargetType.LINUX)
        kw.setdefault("port", 9100)
        return self.make_target(self.run_obj, hostname, **kw)

    def cred(self, kind="ssh_password", username="svc-le", secret=SECRET, site=None):
        return secrets.store(kind, username, secret, site or self.site_a.name, self.admin_a, name=f"{kind}-cred")

    def logs_text(self):
        return "\n".join(f"{log.detail}" for log in StepLog.objects.all())

    def assertNoSecret(self, *things):
        for thing in things:
            self.assertNotIn(SECRET, str(thing))
            self.assertNotIn(PRIVATE_KEY.splitlines()[1], str(thing))


# ----------------------------------------------------------------------------- exposition parsing
class ParseTests(OnboardingTestCase):
    def test_node_exporter(self):
        out = servers.parse_exposition(NODE_BODY)
        self.assertEqual(out["exporter"], "node_exporter")
        self.assertEqual(out["exporter_version"], "1.9.1")
        self.assertEqual(out["hostname"], "db-01.example.test")
        self.assertEqual(out["kernel"], "6.8.0-31-generic")
        self.assertEqual(out["arch"], "x86_64")
        self.assertEqual(out["os"], "Ubuntu 24.04 LTS")
        self.assertEqual(out["cpus"], 4)
        self.assertEqual(out["memory_mb"], 8192)

    def test_windows_exporter(self):
        out = servers.parse_exposition(WINDOWS_BODY)
        self.assertEqual((out["exporter"], out["exporter_version"]), ("windows_exporter", "0.30.5"))
        self.assertEqual(out["hostname"], "WIN-01")
        self.assertEqual(out["os"], "Microsoft Windows Server 2019 Standard")
        self.assertEqual(out["kernel"], "10.0.17763")
        self.assertEqual(out["cpus"], 8)
        self.assertEqual(out["memory_mb"], 16384)

    def test_old_wmi_exporter_is_reported_as_windows_exporter(self):
        out = servers.parse_exposition('wmi_exporter_build_info{version="0.20.0"} 1\n')
        self.assertEqual(out["exporter"], "windows_exporter")

    def test_other_exporter_and_prometheus(self):
        self.assertEqual(servers.parse_exposition('redis_exporter_build_info{version="1.5"} 1\n')["exporter"],
                         "redis_exporter")
        self.assertEqual(servers.parse_exposition('prometheus_build_info{version="2.51.0"} 1\n')["exporter"],
                         "prometheus")

    def test_go_build_info_alone_is_not_an_exporter(self):
        out = servers.parse_exposition('# HELP go_info x\ngo_build_info{version="(devel)"} 1\n')
        self.assertTrue(out["looks_like_metrics"])
        self.assertEqual(out["exporter"], "")

    def test_html_is_not_metrics(self):
        out = servers.parse_exposition("<html><body>Welcome</body></html>\n")
        self.assertFalse(out["looks_like_metrics"])

    def test_garbage_does_not_raise(self):
        servers.parse_exposition("\x00\x01 {{{ \n" * 50 + "node_cpu_seconds_total{cpu=\n")


# ----------------------------------------------------------------------------- install text
class InstallTextTests(OnboardingTestCase):
    def test_linux_script_is_pinned_and_valid_sh(self):
        script = install.linux_install_script(9101)
        self.assertIn(f"VERSION='{install.NODE_EXPORTER_VERSION}'", script)
        for sha in install.NODE_EXPORTER_SHA256.values():
            self.assertIn(sha, script)
        self.assertIn("--web.listen-address=:9101", script)
        self.assertIn("sha256sum -c", script)
        self.assertIn("rolled back", script)
        proc = subprocess.run(["sh", "-n"], input=script.encode(), capture_output=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = subprocess.run(["sh", "-n"], input=install.linux_command(9100).encode(), capture_output=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_windows_script_is_pinned(self):
        script = install.windows_install_script(9182)
        self.assertIn(install.WINDOWS_EXPORTER_VERSION, script)
        for sha in install.WINDOWS_EXPORTER_SHA256.values():
            self.assertIn(sha, script)
        self.assertIn("LISTEN_PORT=$port", script)
        self.assertIn("Get-FileHash", script)

    def test_port_must_be_a_port(self):
        for bad in ("9100; rm -rf /", 0, 70000, "x", None, "9100 && id"):
            with self.assertRaises(ValueError):
                install.linux_install_script(bad)
            with self.assertRaises(ValueError):
                install.details("linux", bad)

    def test_details_for_the_copy_paste_page(self):
        info = install.details(TargetType.LINUX, 9100)
        self.assertEqual(info["exporter"], "node_exporter")
        self.assertIn("github.com/prometheus/node_exporter/releases/download/v", info["download_url"])
        self.assertIn("[Service]", info["unit"])
        self.assertEqual(info["port"], 9100)
        self.assertTrue(info["firewall"])
        win = install.details(TargetType.WINDOWS, 9182)
        self.assertEqual(win["exporter"], "windows_exporter")
        self.assertTrue(win["download_url"].endswith(".msi"))
        self.assertIsNone(install.details(TargetType.DEVICE, 9116))

    def test_unit_lines_refuse_a_quote(self):
        with self.assertRaises(ValueError):
            install._sh_quote("it's")


# ----------------------------------------------------------------------------- verify
class VerifyTests(ServersCase):
    def test_exporter_found_with_every_attempt_recorded(self):
        t = self.host()
        out = steps.verify_target(t)
        self.assertEqual(out["state"], TargetState.EXPORTER_FOUND)
        self.assertEqual((out["exporter"], out["exporter_version"]), ("node_exporter", "1.9.1"))
        self.assertTrue(out["dns"] and out["tcp"] and out["metrics_ok"])
        self.assertEqual(out["http_status"], 200)
        self.assertEqual((out["hostname"], out["cpus"], out["memory_mb"]), ("db-01.example.test", 4, 8192))
        probes = [a["probe"] for a in out["attempts"]]
        self.assertEqual(probes, ["DNS", "TCP 9100", "GET /metrics"])
        self.assertTrue(all(a["ok"] and a["ms"] >= 0 for a in out["attempts"]))
        lines = StepLog.objects.filter(target=t, step="verify")
        self.assertEqual(lines.count(), 3)
        self.assertFalse(lines.filter(duration_ms__isnull=True).exists())
        # the request went to the validated address, as a GET of /metrics
        req = self.net.requests[0]
        self.assertEqual((req["method"], req["path"], req["ip"], req["port"]), ("GET", "/metrics", IP, 9100))
        self.assertNotIn("Authorization", req["headers"])

    def test_address_field_is_probed_instead_of_the_hostname(self):
        t = self.host(address="10.0.0.9")
        seen = []
        self.net.resolve = lambda host, port=443: seen.append(host) or [IP]
        steps.verify_target(t)
        self.assertEqual(seen[0], "10.0.0.9")

    def test_dns_failure_is_unreachable_and_stops(self):
        self.net.ips = []
        out = steps.verify_target(self.host())
        self.assertEqual(out["state"], TargetState.UNREACHABLE)
        self.assertIn("DNS resolution failed", out["error"])
        self.assertIs(out["dns"], False)
        self.assertEqual(self.net.requests, [])
        self.assertEqual(self.net.connects, [])

    def test_private_address_is_refused_by_the_policy(self):
        with override_settings(PROBE_ALLOWED_PRIVATE_CIDRS=[]):
            out = steps.verify_target(self.host())
        self.assertEqual(out["state"], TargetState.UNREACHABLE)
        self.assertIn("LINKEDEYE_PROBE_ALLOWED_PRIVATE_CIDRS", out["error"])
        self.assertNotIn(IP, out["error"])
        self.assertEqual(self.net.connects, [])           # nothing was connected to

    def test_link_local_metadata_address_is_always_refused(self):
        self.net.ips = ["169.254.169.254"]
        out = steps.verify_target(self.host())
        self.assertEqual(out["state"], TargetState.UNREACHABLE)
        self.assertEqual(self.net.connects, [])

    def test_refused_port_means_the_host_is_up_but_has_no_exporter(self):
        self.net.tcp = "refused"
        out = steps.verify_target(self.host())
        self.assertEqual(out["state"], TargetState.REACHABLE)
        self.assertNotIn("error", out)
        self.assertIs(out["tcp"], False)
        self.assertIn("nothing listens", out["note"])
        self.assertEqual(self.net.requests, [])

    def test_silent_port_is_unreachable(self):
        self.net.tcp = "timeout"
        out = steps.verify_target(self.host())
        self.assertEqual(out["state"], TargetState.UNREACHABLE)
        self.assertIn("did not answer", out["error"])

    def test_open_port_that_is_not_an_exporter(self):
        self.net.body = "<html>hello</html>"
        out = steps.verify_target(self.host())
        self.assertEqual(out["state"], TargetState.REACHABLE)
        self.assertIn("not Prometheus metrics", out["error"])
        self.assertFalse(out["metrics_ok"])

    def test_404_on_metrics(self):
        self.net.status = 404
        out = steps.verify_target(self.host())
        self.assertEqual(out["state"], TargetState.REACHABLE)
        self.assertIn("HTTP 404", out["error"])

    def test_http_timeout_and_error_text_has_no_url(self):
        import socket
        self.net.http_error = socket.timeout()
        out = steps.verify_target(self.host())
        self.assertEqual(out["state"], TargetState.REACHABLE)
        self.assertIn("Timed out", out["error"])

    def test_401_without_a_credential_says_to_attach_one(self):
        self.net.status = 401
        out = steps.verify_target(self.host())
        self.assertIn("requires authentication", out["error"])
        self.assertIsNone(out["creds_accepted"])

    def test_bearer_credential_is_sent_and_accepted_and_never_stored(self):
        cred = self.cred("bearer", "", "tok-en-secret-value-123")
        t = self.host(credential=cred, scheme="https")        # an HTTP login only travels over TLS
        out = steps.verify_target(t)
        self.assertEqual(out["state"], TargetState.EXPORTER_FOUND)
        self.assertIs(out["creds_accepted"], True)
        self.assertEqual(self.net.requests[0]["headers"]["Authorization"], "Bearer tok-en-secret-value-123")
        self.assertNotIn("tok-en-secret-value-123", str(out) + self.logs_text())
        # reading the secret was audited
        self.assertTrue(AuditEvent.objects.filter(action="credential.use").exists())

    def test_basic_credential_header_and_rejection(self):
        cred = self.cred("basic_auth", "scrape", SECRET)
        t = self.host(credential=cred, scheme="https")
        self.net.status = 401
        out = steps.verify_target(t)
        expected = "Basic " + base64.b64encode(f"scrape:{SECRET}".encode()).decode()
        self.assertEqual(self.net.requests[0]["headers"]["Authorization"], expected)
        self.assertIs(out["creds_accepted"], False)
        self.assertIn("rejected", out["error"])
        self.assertNoSecret(out, self.logs_text())
        self.assertNotIn(expected.split()[1], str(out))

    def test_stale_facts_are_reset_on_a_second_verify(self):
        t = self.host()
        first = steps.verify_target(t)
        self.assertEqual(first["exporter"], "node_exporter")
        self.net.tcp = "refused"
        second = steps.verify_target(t)
        self.assertEqual(second["exporter"], "")
        self.assertEqual(second["hostname"], "")

    def test_windows_target(self):
        self.net.body = WINDOWS_BODY
        out = steps.verify_target(self.host("win-01", target_type=TargetType.WINDOWS, port=9182))
        self.assertEqual(out["state"], TargetState.EXPORTER_FOUND)
        self.assertEqual(out["exporter"], "windows_exporter")
        self.assertEqual(self.net.requests[0]["port"], 9182)

    def test_type_mismatch_is_flagged(self):
        self.net.body = WINDOWS_BODY
        out = steps.verify_target(self.host())
        self.assertIn("Windows host", out["warning"])

    def test_verify_is_idempotent(self):
        t = self.host()
        a = steps.verify_target(t)
        b = steps.verify_target(t)
        for key in ("state", "exporter", "exporter_version", "hostname", "cpus"):
            self.assertEqual(a[key], b[key])
        self.assertEqual(OnboardingTarget.objects.count(), 1)

    def test_other_target_types_are_handed_to_their_own_module(self):
        with mock.patch("onboarding.others.verify", return_value={"state": TargetState.REACHABLE}) as hook:
            out = steps.verify_target(self.host("sw-01", target_type=TargetType.URL))
        self.assertEqual(out["state"], TargetState.REACHABLE)
        hook.assert_called_once()


# ----------------------------------------------------------------------------- verify over SSH
class VerifySSHTests(ServersCase):
    def setUp(self):
        super().setUp()
        self.net.exporter_up = False
        self.net.tcp = "refused"

    def test_facts_over_ssh_and_the_host_key_is_pinned(self):
        t = self.host(credential=self.cred())
        out = steps.verify_target(t)
        self.assertEqual(out["state"], TargetState.REACHABLE)
        self.assertEqual(out["ssh"], "ok")
        self.assertEqual(out["ssh_user"], "svc-le")
        self.assertEqual(out["ssh_host_key"], ssh.fingerprint(KEY_BLOB))
        self.assertTrue(out["ssh_host_key"].startswith("SHA256:"))
        self.assertEqual((out["hostname"], out["os"], out["kernel"], out["cpus"], out["memory_mb"]),
                         ("db-01", "Ubuntu 24.04 LTS", "6.8.0-31-generic", 4, 8192))
        self.assertIs(out["exporter_installed"], False)
        self.assertIn("reachable over SSH", out["note"])
        self.assertEqual(out["facts_source"], "ssh")
        self.assertEqual(self.world.unexpected, [])
        self.assertEqual(set(self.world.commands), set(ssh.LINUX_FACT_COMMANDS.values()))
        self.assertEqual(self.world.connects[0]["username"], "svc-le")
        self.assertEqual(self.world.credentials_sent, [SECRET])
        self.assertNoSecret(out, self.logs_text())
        probes = [a["probe"] for a in out["attempts"]]
        self.assertIn("SSH login", probes)
        self.assertIn("SSH facts", probes)

    def test_ssh_goes_to_the_validated_address_and_port_22(self):
        t = self.host(credential=self.cred())
        steps.verify_target(t)
        self.assertIn((IP, 22), self.net.connects)

    def test_pinned_key_matching_is_accepted_again(self):
        t = self.host(credential=self.cred())
        first = steps.verify_target(t)
        t.facts = {"ssh_host_key": first["ssh_host_key"]}
        t.save()
        again = steps.verify_target(t)
        self.assertEqual(again["ssh"], "ok")
        self.assertIn("matches the pin", [a for a in again["attempts"] if a["probe"] == "SSH login"][0]["detail"])

    def test_a_changed_host_key_is_a_hard_failure_and_no_credential_is_sent(self):
        t = self.host(credential=self.cred(), facts={"ssh_host_key": ssh.fingerprint(KEY_BLOB)})
        self.world.host_key = OTHER_KEY_BLOB
        out = steps.verify_target(t)
        self.assertEqual(out["state"], TargetState.FAILED)
        self.assertEqual(out["ssh"], "hostkey_changed")
        self.assertIn("host key changed", out["error"])
        self.assertEqual(self.world.credentials_sent, [])            # stopped before authentication
        self.assertEqual(self.world.commands, [])
        self.assertNotIn("ssh_host_key", out)                         # the pin is not overwritten
        self.assertNoSecret(out, self.logs_text())

    def test_a_changed_key_fails_even_when_the_exporter_answers(self):
        self.net.exporter_up, self.net.tcp = True, "open"
        t = self.host(credential=self.cred(), facts={"ssh_host_key": ssh.fingerprint(KEY_BLOB)})
        self.world.host_key = OTHER_KEY_BLOB
        out = steps.verify_target(t)
        self.assertEqual(out["state"], TargetState.FAILED)

    def test_rejected_login_is_reported_without_the_secret(self):
        self.world.auth_fail = True
        out = steps.verify_target(self.host(credential=self.cred()))
        self.assertEqual(out["ssh"], "auth")
        self.assertIn("rejected", out["ssh_error"])
        self.assertEqual(out["state"], TargetState.REACHABLE)
        self.assertNoSecret(out, self.logs_text())

    def test_a_rejected_login_does_not_hide_a_working_exporter(self):
        self.net.exporter_up, self.net.tcp = True, "open"
        self.world.auth_fail = True
        out = steps.verify_target(self.host(credential=self.cred()))
        self.assertEqual(out["state"], TargetState.EXPORTER_FOUND)
        self.assertEqual(out["ssh"], "auth")

    def test_without_paramiko_ssh_is_skipped_with_a_clear_message(self):
        self._patch("onboarding.ssh._paramiko", side_effect=ssh.SSHUnavailable(
            "SSH support (paramiko) is not installed on this LinkedEye server. Install it with "
            "'pip install -r requirements-ssh.txt'"))
        out = steps.verify_target(self.host(credential=self.cred()))
        self.assertEqual(out["ssh"], "unavailable")
        self.assertIn("requirements-ssh.txt", out["ssh_error"])
        self.assertEqual(out["state"], TargetState.REACHABLE)
        self.assertEqual(self.world.connects, [])

    def test_ssh_to_a_private_address_outside_the_policy_is_blocked(self):
        t = self.host(credential=self.cred())
        with override_settings(PROBE_ALLOWED_PRIVATE_CIDRS=[]):
            out = steps.verify_target(t)
        self.assertEqual(out["state"], TargetState.UNREACHABLE)
        self.assertEqual(self.world.connects, [])

    def test_ssh_port_closed(self):
        self.net.ssh_port = "refused"
        out = steps.verify_target(self.host(credential=self.cred()))
        self.assertEqual(out["ssh"], "connect")
        self.assertIn("refused", out["ssh_error"])

    def test_private_key_credential(self):
        cred = self.cred("ssh_key", "deploy", PRIVATE_KEY)
        out = steps.verify_target(self.host(credential=cred))
        self.assertEqual(out["ssh"], "ok")
        self.assertTrue(self.world.connects[0]["key"])
        self.assertFalse(self.world.connects[0]["password"])
        self.assertNoSecret(out, self.logs_text())

    def test_passphrase_protected_key_is_explained_not_echoed(self):
        cred = self.cred("ssh_key", "deploy", PRIVATE_KEY.replace("OPENSSH", "ENCRYPTED OPENSSH"))
        out = steps.verify_target(self.host(credential=cred))
        self.assertEqual(out["ssh"], "key")
        self.assertIn("passphrase", out["ssh_error"])
        self.assertEqual(self.world.connects, [])

    def test_unreadable_key_message_does_not_echo_it(self):
        cred = self.cred("ssh_key", "deploy", "not really a key but secret-ish text")
        out = steps.verify_target(self.host(credential=cred))
        self.assertEqual(out["ssh"], "key")
        self.assertNotIn("secret-ish", str(out))

    @without_key
    def test_vault_without_its_key_is_reported(self):
        cred = Credential.objects.create(name="x", kind="ssh_password", username="u", secret="gAAAAAB-not-decryptable",
                                         site=self.site_a.name)
        out = steps.verify_target(self.host(credential=cred))
        self.assertEqual(out["ssh"], "vault")
        self.assertIn("LINKEDEYE_CREDENTIAL_KEY", out["ssh_error"])
        self.assertEqual(self.world.connects, [])

    def test_a_credential_from_another_site_is_not_used(self):
        cred = self.cred(site=self.site_b.name)
        out = steps.verify_target(self.host(credential=cred))
        self.assertEqual(out["ssh"], "refused")
        self.assertEqual(self.world.connects, [])

    def test_reading_the_secret_is_audited(self):
        steps.verify_target(self.host(credential=self.cred()))
        event = AuditEvent.objects.filter(action="credential.use").first()
        self.assertIsNotNone(event)
        self.assertNotIn(SECRET, str(event.__dict__))

    def test_windows_facts_over_ssh(self):
        t = self.host("win-01", target_type=TargetType.WINDOWS, port=9182, credential=self.cred(username="Administrator"))
        out = steps.verify_target(t)
        self.assertEqual(out["ssh"], "ok")
        self.assertEqual((out["hostname"], out["cpus"], out["memory_mb"]), ("WIN-01", 8, 16384))
        self.assertEqual(self.world.unexpected, [])
        self.assertTrue(self.world.commands[0].startswith("powershell.exe -NoProfile"))


# ----------------------------------------------------------------------------- the SSH allow-list
class AllowListTests(ServersCase):
    def test_only_named_commands_run(self):
        with self._session() as session:
            with self.assertRaises(KeyError):
                session.run("rm -rf /")
            with self.assertRaises(KeyError):
                session.run("hostname; id")
            self.assertEqual(self.world.commands, [])

    def _session(self):
        from contextlib import closing
        return closing(ssh.open_session(IP, "svc-le", SECRET))

    def test_install_refuses_a_bad_port(self):
        with self._session() as session:
            with self.assertRaises(ValueError):
                session.run_install("linux", "9100; id", "root")
            self.assertEqual(self.world.commands, [])

    def test_the_install_command_is_the_pinned_script_only(self):
        with self._session() as session:
            session.run_install("linux", 9100, "root")
        cmd = self.world.commands[0]
        blob = re.search(r"printf %s '([A-Za-z0-9+/=]+)'", cmd).group(1)
        self.assertEqual(base64.b64decode(blob).decode(), install.linux_install_script(9100))
        self.assertTrue(cmd.startswith("sh -c "))

    def test_sudo_modes(self):
        with self._session() as session:
            session.run_install("linux", 9100, "sudo_stdin")
            session.run_install("linux", 9100, "sudo_n")
        self.assertTrue(self.world.commands[0].startswith("sudo -S -p '' sh -c "))
        self.assertTrue(self.world.commands[1].startswith("sudo -n sh -c "))
        self.assertEqual(self.world.stdin, [(SECRET + "\n").encode()])

    def test_windows_install_is_an_encoded_command(self):
        with self._session() as session:
            session.run_install("windows", 9182, "root")
        cmd = self.world.commands[0]
        blob = cmd.rsplit(" ", 1)[1]
        self.assertEqual(base64.b64decode(blob).decode("utf-16-le"), install.windows_install_script(9182))

    def test_output_has_the_secret_removed(self):
        self.world.install_result = (1, f"out {SECRET}\n", f"sudo said {SECRET}\n")
        with self._session() as session:
            code, out, err = session.run_install("linux", 9100, "root")
        self.assertNotIn(SECRET, out + err)
        self.assertIn("••••", err)


# ----------------------------------------------------------------------------- enable
class EnableTests(ServersCase):
    def setUp(self):
        super().setUp()
        self.net.exporter_up = False
        self.net.tcp = "refused"

    def install_brings_exporter_up(self):
        def up():
            self.net.exporter_up, self.net.tcp = True, "open"
        self.world.on_install = up

    def test_an_exporter_that_already_answers_is_left_alone(self):
        self.net.exporter_up, self.net.tcp = True, "open"
        out = steps.enable_collection(self.host(credential=self.cred()), {"install_over_ssh": True})
        self.assertEqual(out["state"], TargetState.EXPORTER_FOUND)
        self.assertEqual(out["install_command"], "")
        self.assertEqual(self.world.connects, [])
        self.assertEqual(self.world.installs, [])

    def test_without_the_tick_it_returns_the_exact_command_and_touches_nothing(self):
        out = steps.enable_collection(self.host(credential=self.cred()), {"install_over_ssh": False})
        self.assertEqual(out["install_command"], install.linux_command(9100))
        self.assertIn("node_exporter-1.9.1.linux-amd64.tar.gz", install.node_exporter_url("amd64"))
        self.assertNotIn("state", out)                                # the target keeps its state
        self.assertEqual(self.world.connects, [])
        self.assertEqual(self.world.installs, [])

    def test_windows_command(self):
        out = steps.enable_collection(self.host("win-01", target_type=TargetType.WINDOWS, port=9182), None)
        self.assertEqual(out["install_command"], install.windows_command(9182))
        self.assertIn("msiexec", out["install_command"])

    def test_the_command_uses_the_targets_port(self):
        out = steps.enable_collection(self.host(port=9111), {})
        self.assertIn("--web.listen-address=:9111", out["install_command"])
        self.assertEqual(out["install_port"], 9111)

    def test_ssh_install_as_root(self):
        self.install_brings_exporter_up()
        t = self.host(credential=self.cred())
        out = steps.enable_collection(t, {"install_over_ssh": True})
        self.assertEqual(out["state"], TargetState.INSTALLED)
        self.assertEqual(out["exporter"], "node_exporter")
        self.assertTrue(out["metrics_ok"])
        self.assertEqual(out["install_command"], "")
        self.assertEqual(len(self.world.installs), 1)
        self.assertTrue(self.world.installs[0].startswith("sh -c "))
        self.assertEqual(self.world.stdin, [])
        self.assertEqual(self.world.unexpected, [])
        self.assertEqual(len(self.world.connects), 1)                  # one login for facts and install
        self.assertEqual(out["ssh_host_key"], ssh.fingerprint(KEY_BLOB))
        probes = [a["probe"] for a in out["attempts"]]
        self.assertIn("SSH install", probes)
        self.assertNoSecret(out, self.logs_text())

    def test_ssh_install_as_a_normal_user_uses_sudo_with_the_password_on_stdin(self):
        self.install_brings_exporter_up()
        self.world.facts_out["uid"] = "1000"
        out = steps.enable_collection(self.host(credential=self.cred()), {"install_over_ssh": True})
        self.assertEqual(out["state"], TargetState.INSTALLED)
        self.assertTrue(self.world.installs[0].startswith("sudo -S -p '' sh -c "))
        self.assertNotIn(SECRET, self.world.installs[0])               # never on a command line
        self.assertEqual(self.world.stdin, [(SECRET + "\n").encode()])

    def test_ssh_install_with_a_key_uses_passwordless_sudo(self):
        self.install_brings_exporter_up()
        self.world.facts_out["uid"] = "1000"
        cred = self.cred("ssh_key", "deploy", PRIVATE_KEY)
        out = steps.enable_collection(self.host(credential=cred), {"install_over_ssh": True})
        self.assertEqual(out["state"], TargetState.INSTALLED)
        self.assertTrue(self.world.installs[0].startswith("sudo -n sh -c "))
        self.assertEqual(self.world.stdin, [])

    def test_a_failed_install_reports_stderr_and_leaves_the_state_failed(self):
        self.world.install_result = (1, "", "checksum mismatch for node_exporter-1.9.1.linux-amd64.tar.gz; nothing was installed\n")
        out = steps.enable_collection(self.host(credential=self.cred()), {"install_over_ssh": True})
        self.assertEqual(out["state"], TargetState.FAILED)
        self.assertIn("checksum mismatch", out["error"])
        self.assertIn("left as it was", out["error"])
        self.assertIn("checksum mismatch", out["install_output"])

    def test_sudo_needing_a_password_is_reported(self):
        self.world.facts_out["uid"] = "1000"
        cred = self.cred("ssh_key", "deploy", PRIVATE_KEY)
        self.world.install_result = (1, "", "sudo: a password is required\n")
        out = steps.enable_collection(self.host(credential=cred), {"install_over_ssh": True})
        self.assertEqual(out["state"], TargetState.FAILED)
        self.assertIn("password is required", out["error"])

    def test_the_secret_never_reaches_the_error_or_the_stored_output(self):
        self.world.facts_out["uid"] = "1000"
        self.world.install_result = (1, "", f"sudo: incorrect password {SECRET}\n")
        out = steps.enable_collection(self.host(credential=self.cred()), {"install_over_ssh": True})
        self.assertNoSecret(out, self.logs_text())

    def test_installed_but_not_reachable_from_here_is_a_warning(self):
        out = steps.enable_collection(self.host(credential=self.cred()), {"install_over_ssh": True})
        self.assertEqual(out["state"], TargetState.INSTALLED)
        self.assertFalse(out["metrics_ok"])
        self.assertIn("cannot reach", out["warning"])
        self.assertNotIn("error", out)

    def test_no_ssh_credential_means_nothing_is_installed(self):
        t = self.host()
        out = steps.enable_collection(t, {"install_over_ssh": True})
        self.assertIn("SSH password or key", out["error"])
        self.assertEqual(out["state"], t.state)
        self.assertEqual(out["install_command"], install.linux_command(9100))
        self.assertEqual(self.world.connects, [])

    def test_http_credential_is_not_an_ssh_credential(self):
        t = self.host(credential=self.cred("bearer", "", "bearer-secret-xyz"))
        out = steps.enable_collection(t, {"install_over_ssh": True})
        self.assertIn("SSH password or key", out["error"])
        self.assertEqual(self.world.connects, [])

    def test_without_paramiko_the_command_is_offered_and_nothing_runs(self):
        self._patch("onboarding.ssh.available", return_value=False)
        out = steps.enable_collection(self.host(credential=self.cred()), {"install_over_ssh": True})
        self.assertIn("paramiko", out["error"])
        self.assertIn("requirements-ssh.txt", out["error"])
        self.assertEqual(out["install_command"], install.linux_command(9100))
        self.assertEqual(self.world.connects, [])

    def test_a_changed_host_key_blocks_the_install(self):
        t = self.host(credential=self.cred(), facts={"ssh_host_key": ssh.fingerprint(KEY_BLOB)})
        self.world.host_key = OTHER_KEY_BLOB
        out = steps.enable_collection(t, {"install_over_ssh": True})
        self.assertEqual(out["state"], TargetState.FAILED)
        self.assertIn("host key changed", out["error"])
        self.assertEqual(self.world.credentials_sent, [])
        self.assertEqual(self.world.installs, [])

    def test_a_rejected_login_installs_nothing(self):
        self.world.auth_fail = True
        t = self.host(credential=self.cred())
        out = steps.enable_collection(t, {"install_over_ssh": True})
        self.assertIn("Nothing was installed", out["error"])
        self.assertEqual(out["state"], t.state)
        self.assertEqual(self.world.installs, [])

    def test_install_over_ssh_on_windows(self):
        self.install_brings_exporter_up()
        self.net.body = WINDOWS_BODY
        self.world.install_result = (0, "windows_exporter 0.30.5 installed and listening on :9182\n", "")
        t = self.host("win-01", target_type=TargetType.WINDOWS, port=9182, credential=self.cred(username="Administrator"))
        out = steps.enable_collection(t, {"install_over_ssh": True})
        self.assertEqual(out["state"], TargetState.INSTALLED)
        self.assertIn("-EncodedCommand", self.world.installs[0])
        self.assertEqual(out["exporter"], "windows_exporter")

    def test_enable_twice_does_not_install_twice(self):
        self.install_brings_exporter_up()
        t = self.host(credential=self.cred())
        steps.enable_collection(t, {"install_over_ssh": True})
        again = steps.enable_collection(t, {"install_over_ssh": True})
        self.assertEqual(again["state"], TargetState.EXPORTER_FOUND)
        self.assertEqual(len(self.world.installs), 1)

    def test_other_target_types_are_handed_to_their_own_module(self):
        with mock.patch("onboarding.others.enable", return_value={"note": "ok"}) as hook:
            out = steps.enable_collection(self.host("sw-01", target_type=TargetType.URL), {})
        self.assertEqual(out["note"], "ok")
        hook.assert_called_once()


# ----------------------------------------------------------------------------- through the wizard
class WizardTests(ServersCase):
    def url(self, name):
        return reverse("onboarding:step", args=[self.run_obj.pk, name])

    def test_verify_then_enable_pages_end_to_end(self):
        t = self.host(credential=self.cred())
        self.net.exporter_up, self.net.tcp = False, "refused"
        c = self.login(self.admin_a)
        resp = c.post(self.url("verify"), {"action": "verify"}, follow=True)
        self.assertEqual(resp.status_code, 200)
        t.refresh_from_db()
        self.assertEqual(t.state, TargetState.REACHABLE)
        self.assertEqual(t.facts["hostname"], "db-01")
        self.assertEqual(t.facts["ssh_host_key"], ssh.fingerprint(KEY_BLOB))
        body = resp.content.decode()
        self.assertIn("SSH as svc-le", body)
        self.assertIn("Every probe attempt", body)
        self.assertIn("Ubuntu 24.04 LTS", body)
        self.assertNotIn(SECRET, body)

        page = c.get(self.url("enable")).content.decode()
        self.assertIn("node_exporter-1.9.1.linux-amd64.tar.gz", page)
        self.assertIn("--web.listen-address=:9100", page)
        self.assertIn("LINKEDEYE_INSTALL", page)
        self.assertIn("9100/tcp", page)
        self.assertNotIn(SECRET, page)

        self.world.on_install = lambda: (setattr(self.net, "exporter_up", True), setattr(self.net, "tcp", "open"))
        resp = c.post(self.url("enable"), {"action": "enable", "install_over_ssh": "1"}, follow=True)
        t.refresh_from_db()
        self.assertEqual(t.state, TargetState.INSTALLED)
        self.assertIn("node_exporter", t.facts["exporter"])
        self.assertNotIn(SECRET, resp.content.decode())
        # the installed target is now served by service discovery
        self.assertTrue(t.served)
        self.assertEqual(len(self.world.installs), 1)

    def test_enable_without_the_tick_does_not_install(self):
        self.net.exporter_up, self.net.tcp = False, "refused"
        t = self.host(credential=self.cred())
        c = self.login(self.admin_a)
        c.post(self.url("enable"), {"action": "enable"}, follow=True)
        t.refresh_from_db()
        self.assertEqual(t.state, TargetState.NEW)
        self.assertEqual(self.world.connects, [])
        self.assertEqual(self.world.installs, [])
        self.assertIn("install_command", t.facts)

    def test_operator_and_viewer_cannot_verify_or_install(self):
        self.host(credential=self.cred())
        for user in (self.operator, self.viewer, self.admin_b):
            c = self.login(user)
            for name, action in (("verify", "verify"), ("enable", "enable")):
                resp = c.post(self.url(name), {"action": action, "install_over_ssh": "1"})
                self.assertIn(resp.status_code, (403, 404))
        self.assertEqual(self.world.connects, [])
        self.assertEqual(self.net.requests, [])

    def test_pages_render_for_a_viewer_without_a_form(self):
        self.host()
        c = self.login(self.viewer)
        for name in ("verify", "enable"):
            resp = c.get(self.url(name))
            self.assertEqual(resp.status_code, 200)
            self.assertNotIn('name="install_over_ssh"', resp.content.decode())

    def test_enable_page_without_paramiko_disables_the_tick(self):
        self.host()
        self._patch("onboarding.views.importlib.util.find_spec", return_value=None)
        body = self.login(self.admin_a).get(self.url("enable")).content.decode()
        self.assertIn("requirements-ssh.txt", body)
        self.assertRegex(body, r'id="ssh-install"[^>]*disabled')

    def test_get_does_not_probe(self):
        self.host(credential=self.cred())
        c = self.login(self.admin_a)
        c.get(self.url("verify"))
        c.get(self.url("enable"))
        self.assertEqual(self.net.requests, [])
        self.assertEqual(self.net.connects, [])
        self.assertEqual(self.world.connects, [])

    def test_a_changed_host_key_is_visible_on_the_page(self):
        t = self.host(credential=self.cred())
        c = self.login(self.admin_a)
        c.post(self.url("verify"), {"action": "verify"})
        self.world.host_key = OTHER_KEY_BLOB
        resp = c.post(self.url("verify"), {"action": "verify"}, follow=True)
        t.refresh_from_db()
        self.assertEqual(t.state, TargetState.FAILED)
        self.assertEqual(t.facts["ssh_host_key"], ssh.fingerprint(KEY_BLOB))     # still the old pin
        self.assertIn("SSH host key changed", resp.content.decode())
        self.assertEqual(self.world.credentials_sent, [SECRET])                    # only the first login

    def test_secret_in_no_log_audit_or_page_after_a_whole_walk(self):
        self.net.exporter_up, self.net.tcp = False, "refused"
        self.world.facts_out["uid"] = "1000"                          # sudo with the password on stdin
        self.world.install_result = (1, f"out {SECRET}", f"boom {SECRET}")
        t = self.host(credential=self.cred())
        c = self.login(self.admin_a)
        pages = [c.post(self.url("verify"), {"action": "verify"}, follow=True),
                 c.post(self.url("enable"), {"action": "enable", "install_over_ssh": "1"}, follow=True),
                 c.get(reverse("onboarding:log", args=[self.run_obj.pk]))]
        for resp in pages:
            self.assertNotIn(SECRET, resp.content.decode())
        self.assertEqual(len(self.world.installs), 1)
        dump = self.logs_text() + str(list(AuditEvent.objects.values_list("details", "action"))) + str(
            OnboardingTarget.objects.get(pk=t.pk).facts)
        self.assertNotIn(SECRET, dump)
