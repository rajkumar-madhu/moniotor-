"""SSH for onboarding: host facts and the exporter install, with paramiko (an optional extra).

Rules, all enforced here and nowhere else:

* paramiko is imported lazily. When it is missing, ``available()`` is False and every entry point
  raises ``SSHUnavailable``; the wizard then shows the copy-and-paste install command instead.
* the address is resolved and checked by ``probes.checks`` (the SSRF policy) and the connection is made
  to that validated address; the whole session runs under the ``_Deadline`` watchdog, so a peer that
  stalls cannot hold a request open.
* the host key is checked BEFORE any credential is sent. The first connect pins it (SHA-256
  fingerprint, returned to the caller to store); a different key afterwards raises ``HostKeyChanged``
  and nothing is sent. There is no "accept anyway" path in code.
* a command is never text from a form. ``run()`` takes the NAME of an entry in ``FACT_COMMANDS`` and
  ``install()`` builds its command from ``onboarding.install``; both are fixed strings (the only
  variable part of an install is a validated port number).
* the secret is only ever handed to paramiko. Output that comes back is cleaned of it before anyone
  sees it, and no message produced here contains it.
"""
import base64
import hashlib
import importlib.util
import io
import logging
import socket
import time

from probes import checks

from . import install

log = logging.getLogger("linkedeye.onboarding")

SSH_PORT = 22
CONNECT_TIMEOUT = 8
FACTS_BUDGET = 30            # seconds for the whole fact-gathering session
INSTALL_BUDGET = 240         # seconds for the whole install session (a download is in there)
MAX_OUTPUT = 32 * 1024       # bytes kept of any command's output


class SSHUnavailable(RuntimeError):
    """paramiko is not installed (it is an optional extra)."""


class SSHFailure(Exception):
    """Something went wrong; ``kind`` says what and ``message`` is safe to show (never a secret)."""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind
        self.message = message


class HostKeyChanged(SSHFailure):
    def __init__(self, pinned, seen):
        super().__init__(
            "hostkey_changed",
            f"The SSH host key changed (pinned {pinned}, host now presents {seen}). LinkedEye refused to "
            "send any credential. If the host was rebuilt on purpose, confirm that with its owner, then "
            "remove this target and add it again to pin the new key.")
        self.pinned, self.seen = pinned, seen


def available():
    return importlib.util.find_spec("paramiko") is not None


def _paramiko():
    try:
        import paramiko
    except Exception:
        raise SSHUnavailable(
            "SSH support (paramiko) is not installed on this LinkedEye server. Install it with "
            "'pip install -r requirements-ssh.txt' and restart, or run the install command on the host.") from None
    return paramiko


def fingerprint(key_blob):
    """OpenSSH-style SHA256 fingerprint of a host key's wire blob."""
    digest = hashlib.sha256(key_blob).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


# ----------------------------------------------------------------------------- the allow-list
#: Facts commands, by name. Every one is read-only. Nothing else is ever executed for facts.
LINUX_FACT_COMMANDS = {
    "uid": "id -u",
    "hostname": "hostname",
    "os_release": "cat /etc/os-release",
    "kernel": "uname -r",
    "arch": "uname -m",
    "cpus": "nproc",
    "memory": "grep -m1 MemTotal /proc/meminfo",
    "exporter_service": "systemctl is-active node_exporter prometheus-node-exporter",
    "exporter_path": ('for p in "$(command -v node_exporter)" /usr/local/bin/node_exporter /usr/bin/node_exporter; '
                      'do [ -n "$p" ] && [ -x "$p" ] && { echo "$p"; break; }; done'),
    "exporter_version": ('for p in "$(command -v node_exporter)" /usr/local/bin/node_exporter /usr/bin/node_exporter; '
                         'do [ -n "$p" ] && [ -x "$p" ] && { "$p" --version 2>&1 | head -n 1; break; }; done'),
}

_WINDOWS_FACTS_SCRIPT = """$ErrorActionPreference = 'SilentlyContinue'
$os = Get-CimInstance Win32_OperatingSystem
$cs = Get-CimInstance Win32_ComputerSystem
Write-Output ('hostname=' + $env:COMPUTERNAME)
Write-Output ('os=' + $os.Caption)
Write-Output ('kernel=' + $os.Version)
Write-Output ('arch=' + $env:PROCESSOR_ARCHITECTURE)
Write-Output ('cpus=' + $cs.NumberOfLogicalProcessors)
Write-Output ('memory_bytes=' + $cs.TotalPhysicalMemory)
$svc = Get-Service -Name windows_exporter
if ($svc) { Write-Output ('exporter_service=' + $svc.Status) } else { Write-Output 'exporter_service=absent' }
"""


def _powershell(script):
    return ("powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand "
            + install.powershell_encoded(script))


WINDOWS_FACT_COMMANDS = {"facts": _powershell(_WINDOWS_FACTS_SCRIPT)}


def allowed_commands(target_type):
    """The complete list of fact commands for a target type (what the operator can audit)."""
    if target_type == "windows":
        return dict(WINDOWS_FACT_COMMANDS)
    return dict(LINUX_FACT_COMMANDS)


def _sudo_wrap(script, mode):
    """The command line that runs an install script as root. The script is passed base64-encoded inside
    single quotes (only [A-Za-z0-9+/=] can appear), so no quoting can break out of it."""
    blob = base64.b64encode(script.encode()).decode("ascii")
    assert blob.replace("+", "").replace("/", "").replace("=", "").isalnum()
    inner = f"sh -c \"$(printf %s '{blob}' | base64 -d)\""
    if mode == "root":
        return inner
    if mode == "sudo_stdin":
        return f"sudo -S -p '' {inner}"
    return f"sudo -n {inner}"


# ----------------------------------------------------------------------------- session
class _Pin:
    """paramiko host key policy. It runs before authentication, so a changed key stops the session
    before any credential is sent."""

    def __init__(self, pinned):
        self.pinned = pinned or ""
        self.seen = ""

    def missing_host_key(self, client, hostname, key):
        self.seen = fingerprint(key.asbytes())
        if self.pinned and self.pinned != self.seen:
            raise HostKeyChanged(self.pinned, self.seen)
        # first connect: accepted and pinned (the caller stores ``seen``)


class Session:
    """An open SSH connection. Use ``open_session()``."""

    def __init__(self, client, sock, deadline, secret, policy, key_type, ip):
        self._client, self._sock, self._deadline = client, sock, deadline
        self._secret = secret
        self.policy = policy
        self.host_key_type = key_type
        self.ip = ip

    @property
    def host_key(self):
        return self.policy.seen

    def clean(self, text):
        """`text` with the secret removed (defence in depth for anything that came back from the host)."""
        text = text or ""
        if self._secret and len(self._secret) >= 3:
            text = text.replace(self._secret, "••••")
        return text

    def _exec(self, command, stdin_text=None, timeout=60):
        chan = self._client.get_transport().open_session()
        chan.settimeout(timeout)
        chan.exec_command(command)
        if stdin_text is not None:
            chan.sendall(stdin_text.encode())
        try:
            chan.shutdown_write()
        except Exception:                      # noqa: BLE001 - some servers close early; harmless
            pass
        out, err = bytearray(), bytearray()
        while True:
            got = False
            if chan.recv_ready():
                out += chan.recv(4096)
                got = True
            if chan.recv_stderr_ready():
                err += chan.recv_stderr(4096)
                got = True
            if chan.exit_status_ready() and not chan.recv_ready() and not chan.recv_stderr_ready():
                break
            if not got:
                time.sleep(0.05)
            if len(out) > MAX_OUTPUT or len(err) > MAX_OUTPUT:
                break
            if self._deadline.expired:
                raise SSHFailure("timeout", "The SSH session timed out.")
        code = chan.recv_exit_status() if chan.exit_status_ready() else -1
        chan.close()
        return (code, self.clean(out[:MAX_OUTPUT].decode("utf-8", "replace")),
                self.clean(err[:MAX_OUTPUT].decode("utf-8", "replace")))

    def run(self, name, target_type="linux"):
        """Run one command from the allow-list by NAME. An unknown name is a programming error."""
        commands = allowed_commands(target_type)
        if name not in commands:
            raise KeyError("not an allowed command")
        return self._exec(commands[name])

    def run_install(self, target_type, port, sudo_mode):
        """Run the pinned install script. The command comes from onboarding.install only."""
        port = install.valid_port(port)
        if target_type == "windows":
            command = _powershell(install.windows_install_script(port))
            stdin_text = None
        else:
            command = _sudo_wrap(install.linux_install_script(port), sudo_mode)
            stdin_text = (self._secret + "\n") if sudo_mode == "sudo_stdin" else None
        return self._exec(command, stdin_text=stdin_text, timeout=INSTALL_BUDGET)

    def close(self):
        self._deadline.cancel()
        try:
            self._client.close()
        except Exception:                      # noqa: BLE001
            pass
        try:
            self._sock.close()
        except OSError:
            pass


def _load_key(paramiko, text):
    try:
        return paramiko.PKey.from_private_key(io.StringIO(text))
    except paramiko.PasswordRequiredException:
        raise SSHFailure("key", "The private key is passphrase-protected. Store an unencrypted key "
                                "for a dedicated monitoring account instead.") from None
    except Exception:                          # noqa: BLE001 - the message could echo key material
        raise SSHFailure("key", "The stored private key could not be read (is it a complete OpenSSH/PEM key?).") from None


def open_session(host, username, secret, kind="ssh_password", pinned="", budget=FACTS_BUDGET, port=SSH_PORT):
    """Connect and authenticate. Returns a Session (close it, or use it as a context manager via
    ``with closing(...)``). Raises SSHUnavailable or SSHFailure; never a message with the secret."""
    paramiko = _paramiko()
    started = time.monotonic()
    try:
        ip = checks.resolve_allowed(host, port)
    except checks.ProbeBlocked:
        raise SSHFailure("policy", "Blocked by the probe policy: the address is private or reserved. "
                                   "Add its network to LINKEDEYE_PROBE_ALLOWED_PRIVATE_CIDRS to manage internal hosts.") from None
    except socket.gaierror as exc:
        raise SSHFailure("dns", f"DNS resolution failed: {exc.strerror or 'no such host'}") from None
    pkey = _load_key(paramiko, secret) if kind == "ssh_key" else None
    try:
        sock = socket.create_connection((ip, port), CONNECT_TIMEOUT)
    except socket.timeout:
        raise SSHFailure("connect", f"SSH port {port} did not answer within {CONNECT_TIMEOUT}s.") from None
    except ConnectionRefusedError:
        raise SSHFailure("connect", f"SSH port {port} refused the connection.") from None
    except OSError as exc:
        raise SSHFailure("connect", f"Could not connect to SSH port {port}: {type(exc).__name__}.") from None
    deadline = checks._Deadline(max(budget - (time.monotonic() - started), 1), lambda: sock)
    policy = _Pin(pinned)
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(policy)
    try:
        client.connect(
            hostname=host, port=port, username=username, sock=sock,
            password=None if pkey else secret, pkey=pkey,
            allow_agent=False, look_for_keys=False, timeout=CONNECT_TIMEOUT,
            banner_timeout=CONNECT_TIMEOUT, auth_timeout=CONNECT_TIMEOUT)
        key_type = ""
        try:
            key_type = client.get_transport().get_remote_server_key().get_name()
        except Exception:                      # noqa: BLE001
            pass
    except HostKeyChanged:
        _quiet_close(client, sock, deadline)
        raise
    except paramiko.AuthenticationException:
        _quiet_close(client, sock, deadline)
        raise SSHFailure("auth", "SSH login was rejected: the stored credential is not accepted by the host.") from None
    except paramiko.SSHException as exc:
        expired = deadline.expired
        _quiet_close(client, sock, deadline)
        if expired:
            raise SSHFailure("timeout", "The SSH session timed out.") from None
        raise SSHFailure("protocol", f"SSH negotiation failed: {type(exc).__name__}.") from None
    except (socket.timeout, TimeoutError):
        _quiet_close(client, sock, deadline)
        raise SSHFailure("timeout", "The SSH session timed out.") from None
    except OSError as exc:
        _quiet_close(client, sock, deadline)
        raise SSHFailure("connect", f"SSH connection failed: {type(exc).__name__}.") from None
    return Session(client, sock, deadline, secret, policy, key_type, ip)


def _quiet_close(client, sock, deadline):
    deadline.cancel()
    for closer in (client.close, sock.close):
        try:
            closer()
        except Exception:                      # noqa: BLE001
            pass


# ----------------------------------------------------------------------------- facts
def gather_facts(session, target_type="linux"):
    """Read-only facts over the open session: {hostname, os, kernel, arch, cpus, memory_mb, exporter_*}.
    Only allow-listed commands run; a command that fails just leaves its fact out."""
    facts = {}
    if target_type == "windows":
        code, out, _err = session.run("facts", "windows")
        for line in out.splitlines():
            key, _, value = line.strip().partition("=")
            if key and value:
                facts[key] = value.strip()
        facts = _windows_facts(facts)
        return facts
    outputs = {}
    for name in LINUX_FACT_COMMANDS:
        try:
            _code, out, _err = session.run(name, "linux")
        except SSHFailure:
            raise                              # a timeout ends the session
        except Exception as exc:               # noqa: BLE001 - one command failing only drops its fact
            log.info("ssh fact %s unavailable: %s", name, type(exc).__name__)
            continue
        outputs[name] = out.strip()
    return _linux_facts(outputs)


def _linux_facts(out):
    facts = {}
    if out.get("hostname"):
        facts["hostname"] = out["hostname"].splitlines()[0][:255]
    for line in out.get("os_release", "").splitlines():
        if line.startswith("PRETTY_NAME="):
            facts["os"] = line.split("=", 1)[1].strip().strip('"')[:120]
            break
    if out.get("kernel"):
        facts["kernel"] = out["kernel"].splitlines()[0][:60]
    if out.get("arch"):
        facts["arch"] = out["arch"].splitlines()[0][:20]
    if out.get("cpus", "").strip().isdigit():
        facts["cpus"] = int(out["cpus"].strip())
    for token in out.get("memory", "").split():
        if token.isdigit():
            facts["memory_mb"] = int(token) // 1024      # /proc/meminfo is in kB
            break
    if out.get("uid", "").strip().isdigit():
        facts["ssh_uid"] = int(out["uid"].strip())
    states = out.get("exporter_service", "").split()
    facts["exporter_service"] = "active" if "active" in states else ("inactive" if states else "absent")
    facts["exporter_path"] = out.get("exporter_path", "").splitlines()[0][:120] if out.get("exporter_path") else ""
    version = out.get("exporter_version", "").splitlines()[0] if out.get("exporter_version") else ""
    facts["exporter_installed"] = bool(facts["exporter_path"] or facts["exporter_service"] == "active")
    if version:
        # "node_exporter, version 1.8.2 (branch: ...)"
        parts = version.replace(",", " ").split()
        if "version" in parts and parts.index("version") + 1 < len(parts):
            facts["exporter_installed_version"] = parts[parts.index("version") + 1][:30]
    return facts


def _windows_facts(raw):
    facts = {}
    for src, dst in (("hostname", "hostname"), ("os", "os"), ("kernel", "kernel"), ("arch", "arch")):
        if raw.get(src):
            facts[dst] = raw[src][:120]
    if raw.get("cpus", "").isdigit():
        facts["cpus"] = int(raw["cpus"])
    if raw.get("memory_bytes", "").isdigit():
        facts["memory_mb"] = int(raw["memory_bytes"]) // (1024 * 1024)
    state = raw.get("exporter_service", "absent")
    facts["exporter_service"] = "active" if state == "Running" else ("absent" if state == "absent" else "inactive")
    facts["exporter_installed"] = state != "absent"
    return facts


def sudo_mode(facts, kind):
    """How to get root for an install: already root, sudo with the stored password, or sudo -n."""
    if facts.get("ssh_uid") == 0:
        return "root"
    return "sudo_stdin" if kind == "ssh_password" else "sudo_n"
