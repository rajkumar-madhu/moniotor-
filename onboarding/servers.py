"""Verify and enable collection for Linux and Windows servers.

``verify(target)`` and ``enable(target, options)`` are what ``steps.verify_target`` and
``steps.enable_collection`` call for a ``linux`` or ``windows`` target; they follow that module's
contract (a plain dict, ``state`` / ``error`` as the verdict, everything else a fact, no secret in
anything returned, never raising for an expected failure).

What verify does, in order, recording every attempt with its outcome and duration:

1. DNS (``probes.checks.dns_check``) and the SSRF policy (``checks.check_address``);
2. a TCP connect to the metrics port;
3. ``GET /metrics`` over a connection pinned to the validated address (``checks._open_connection`` under
   the ``checks._Deadline`` watchdog), parsing the exposition for the exporter, its version and what
   the metrics already say about the host (hostname, OS, kernel, CPUs, memory);
4. when an SSH credential is attached and paramiko is installed: SSH facts, with the host key pinned.

What enable does: if an exporter already answers it only says so. Otherwise it returns the exact
install command; only when the operator ticked "install over SSH" does it run the pinned install
script over SSH, and only with a fixed allow-list (see ``ssh.py`` and ``install.py``).
"""
import base64
import http.client
import logging
import re
import socket
import ssl
import time

from django.utils import timezone

from core import secrets as vault
from probes import checks

from . import install, ssh
from .models import CredentialKind, TargetState, TargetType
from .services import log_step

log = logging.getLogger("linkedeye.onboarding")

TCP_TIMEOUT = 5.0
HTTP_TIMEOUT = 8.0
MAX_METRICS_BYTES = 4 * 1024 * 1024
MAX_ATTEMPTS = 40
SSH_KINDS = (CredentialKind.SSH_PASSWORD, CredentialKind.SSH_KEY)
HTTP_KINDS = (CredentialKind.BASIC_AUTH, CredentialKind.BEARER)

POLICY_MESSAGE = ("Blocked by the probe policy: this name resolves to a private or reserved address. "
                  "To monitor internal hosts add their network to LINKEDEYE_PROBE_ALLOWED_PRIVATE_CIDRS.")

#: facts a verify always writes, so a stale value from an earlier run can never survive a new one
_FACT_DEFAULTS = {
    "dns": None, "ip": "", "tcp": None, "tcp_detail": "", "http_status": None, "exporter": "",
    "exporter_version": "", "metrics_ok": False, "hostname": "", "os": "", "kernel": "", "arch": "",
    "cpus": None, "memory_mb": None, "ssh": "", "ssh_user": "", "ssh_error": "", "exporter_installed": None,
    "exporter_service": "", "exporter_installed_version": "", "creds_accepted": None, "warning": "",
    "note": "", "facts_source": "",
}


# ----------------------------------------------------------------------------- attempts
class Attempts:
    """Collects one line per probe: shown on the page (facts.attempts) and written to the step log."""

    def __init__(self, target, step):
        self.target, self.step, self.rows = target, step, []

    def add(self, probe, ok, ms, detail="", level=None):
        detail = (detail or "")[:300]
        self.rows.append({"probe": probe, "ok": bool(ok), "ms": round(ms, 1), "detail": detail})
        status = level or ("ok" if ok else "warn")
        log_step(self.target.run, self.step, status, f"{self.target.hostname}: {probe} - {detail}"[:500],
                 target=self.target, duration_ms=round(ms, 1))

    def facts(self):
        return self.rows[-MAX_ATTEMPTS:]


def _ms(start):
    return (time.monotonic() - start) * 1000


# ----------------------------------------------------------------------------- probes
def _host(target):
    return (target.address or target.hostname or "").strip().strip("[]")


def probe_dns(host):
    """(ok, ip, detail, blocked). The first resolved address the SSRF policy allows is the one used."""
    res = checks.dns_check(host)
    if not res.ok:
        return False, "", res.error or "DNS resolution failed", False
    for ip in res.ips:
        try:
            checks.check_address(ip)
        except checks.ProbeBlocked:
            continue
        return True, ip, f"resolved to {ip}", False
    return False, "", POLICY_MESSAGE, True


def probe_tcp(ip, port, timeout=TCP_TIMEOUT):
    """(state, detail) where state is open | refused | timeout | error."""
    try:
        sock = socket.create_connection((ip, port), timeout)
    except socket.timeout:
        return "timeout", f"port {port} did not answer within {timeout:g}s (host down, or a firewall drops it)"
    except ConnectionRefusedError:
        return "refused", f"port {port} refused the connection (the host is up, nothing listens there)"
    except OSError as exc:
        return "error", f"port {port}: {type(exc).__name__}"
    try:
        sock.close()
    except OSError:
        pass
    return "open", f"port {port} is open"


def fetch_metrics(target, ip, headers=None, timeout=HTTP_TIMEOUT):
    """GET /metrics from the validated address. Returns {status, body, error}. One hop, no redirects (so a
    credential is never forwarded), bounded in size and time. Goes through probes.checks only."""
    scheme = "https" if target.port == 443 else "http"
    host = _host(target)
    conn = checks._open_connection(scheme, host, ip, target.port, timeout)
    watchdog = checks._Deadline(timeout, lambda c=conn: getattr(c, "_probe_sock", None))
    out = {"status": None, "body": b"", "error": "", "content_type": ""}
    try:
        send = {"User-Agent": checks.USER_AGENT, "Accept": "text/plain;version=0.0.4, */*;q=0.5",
                "Accept-Encoding": "identity", "Connection": "close"}
        send.update(headers or {})
        conn.request("GET", "/metrics", headers=send)
        if watchdog.expired:
            raise socket.timeout()
        resp = conn.getresponse()
        out["status"] = resp.status
        out["content_type"] = (resp.getheader("Content-Type") or "").split(";")[0].strip().lower()
        body = bytearray()
        while len(body) <= MAX_METRICS_BYTES:
            chunk = resp.read(65536)
            if watchdog.expired:
                raise socket.timeout()
            if not chunk:
                break
            body += chunk
        if len(body) > MAX_METRICS_BYTES:
            out["error"] = "The /metrics response is larger than 4 MB; refusing to read it all."
        out["body"] = bytes(body[:MAX_METRICS_BYTES])
    except ssl.SSLCertVerificationError as exc:
        out["error"] = f"TLS certificate invalid: {exc.verify_message or 'verification failed'}"
    except ssl.SSLError as exc:
        out["error"] = f"TLS error: {exc.reason or 'handshake failed'}"
    except socket.timeout:
        out["error"] = f"Timed out after {timeout:g}s"
    except ConnectionRefusedError:
        out["error"] = "Connection refused"
    except (OSError, http.client.HTTPException) as exc:
        out["error"] = f"{type(exc).__name__} while reading /metrics"
    finally:
        watchdog.cancel()
        try:
            conn.close()
        except Exception:                      # noqa: BLE001
            pass
    return out


# ----------------------------------------------------------------------------- exposition parsing
_SAMPLE = re.compile(r"^([A-Za-z_:][A-Za-z0-9_:]*)(?:\{(.*)\})?\s+(\S+)")
_LABEL = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)="((?:[^"\\]|\\.)*)"')
_KNOWN_BUILD_INFO = ("node_exporter", "windows_exporter", "wmi_exporter")


def _labels(raw):
    return {k: v.replace('\\"', '"').replace("\\\\", "\\") for k, v in _LABEL.findall(raw or "")}


def _number(text):
    try:
        return float(text)
    except ValueError:
        return None


def parse_exposition(text):
    """Identify the exporter and read what the metrics say about the host.

    Returns {looks_like_metrics, exporter, exporter_version, hostname, os, kernel, arch, cpus, memory_mb}.
    Only the lines that matter are looked at; the body is bounded by the caller."""
    found = {"looks_like_metrics": False, "exporter": "", "exporter_version": ""}
    builds = {}                               # metric prefix -> version
    cpus = set()
    seen_prefix = set()
    for line in text.splitlines():
        if not line:
            continue
        if line[0] == "#":
            if line.startswith(("# HELP ", "# TYPE ")):
                found["looks_like_metrics"] = True
            continue
        m = _SAMPLE.match(line)
        if not m:
            continue
        found["looks_like_metrics"] = True
        name, raw, value = m.group(1), m.group(2), m.group(3)
        if name.endswith("_build_info") and name != "go_build_info":
            prefix = name[:-len("_build_info")]
            builds.setdefault(prefix, _labels(raw).get("version", ""))
        elif name == "node_uname_info":
            lab = _labels(raw)
            found.setdefault("hostname", lab.get("nodename", ""))
            found.setdefault("kernel", lab.get("release", ""))
            found.setdefault("arch", lab.get("machine", ""))
            seen_prefix.add("node_exporter")
        elif name == "node_os_info":
            lab = _labels(raw)
            found.setdefault("os", lab.get("pretty_name") or lab.get("name", ""))
        elif name == "node_memory_MemTotal_bytes":
            num = _number(value)
            if num:
                found["memory_mb"] = int(num // (1024 * 1024))
        elif name == "node_cpu_seconds_total":
            lab = _labels(raw)
            if lab.get("mode") == "idle" and "cpu" in lab:
                cpus.add(lab["cpu"])
        elif name == "windows_cs_hostname":
            lab = _labels(raw)
            found.setdefault("hostname", lab.get("hostname", ""))
            seen_prefix.add("windows_exporter")
        elif name == "windows_os_info":
            lab = _labels(raw)
            found.setdefault("os", lab.get("product", ""))
            found.setdefault("kernel", lab.get("version", ""))
        elif name == "windows_cs_logical_processors":
            num = _number(value)
            if num:
                found["cpus"] = int(num)
        elif name == "windows_cs_physical_memory_bytes":
            num = _number(value)
            if num:
                found["memory_mb"] = int(num // (1024 * 1024))
    if cpus and "cpus" not in found:
        found["cpus"] = len(cpus)
    chosen = next((p for p in _KNOWN_BUILD_INFO if p in builds), "")
    if not chosen:
        chosen = next((p for p in builds if p.endswith("_exporter")), "") or ("prometheus" if "prometheus" in builds else "")
    if chosen:
        found["exporter"] = "windows_exporter" if chosen == "wmi_exporter" else chosen
        found["exporter_version"] = builds[chosen]
    elif seen_prefix:
        found["exporter"] = sorted(seen_prefix)[0]
    return {k: v for k, v in found.items() if v not in ("", None) or k in ("exporter", "exporter_version",
                                                                           "looks_like_metrics")}


# ----------------------------------------------------------------------------- credentials
def _credential(target):
    """(credential, problem). A credential from another site or tenant is refused."""
    cred = target.credential
    if cred is None:
        return None, ""
    run = target.run
    # exactly this run's site: a tenant-wide row (site "": the Prometheus login, the discovery token) is
    # never a host credential, so it can never be sent to a host an operator names
    if cred.tenant_key != run.tenant_key or cred.site != run.site:
        return None, "The attached credential belongs to another site, so it was not used."
    return cred, ""


def _fetch_with_credential(target, ip, cred, attempts):
    """GET /metrics, sending an HTTP basic / bearer credential when one is attached. Returns the fetch dict."""
    headers = {}
    try:
        if cred is not None and cred.kind in HTTP_KINDS:
            with vault.use(cred, None, reason=f"verify {target.hostname}: exporter authentication") as secret:
                if cred.kind == CredentialKind.BEARER:
                    headers["Authorization"] = "Bearer " + secret
                else:
                    raw = f"{cred.username}:{secret}".encode()
                    headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
                return fetch_metrics(target, ip, headers)
    except (vault.VaultUnavailable, vault.VaultError) as exc:
        return {"status": None, "body": b"", "error": str(exc), "content_type": "", "vault": True}
    return fetch_metrics(target, ip, headers)


def _classify(fetch, sent_credential):
    """(metrics, error, creds_accepted) for a fetch result."""
    if fetch["error"] and fetch["status"] is None:
        return None, fetch["error"], None
    status = fetch["status"]
    if status in (401, 403):
        if sent_credential:
            return None, f"The exporter answered HTTP {status}: the stored credential was rejected.", False
        return None, (f"The exporter requires authentication (HTTP {status}). Attach an HTTP basic or "
                      "bearer credential to this target."), None
    if status != 200:
        return None, f"GET /metrics answered HTTP {status}: that is not a Prometheus exporter.", None
    text = fetch["body"].decode("utf-8", "replace")
    parsed = parse_exposition(text)
    if not parsed.get("looks_like_metrics"):
        return None, "Port answers HTTP 200, but the body is not Prometheus metrics.", True if sent_credential else None
    return parsed, "", True if sent_credential else None


# ----------------------------------------------------------------------------- the shared probe
def probe_metrics(target, attempts):
    """DNS, TCP and /metrics for one target. Returns a dict of facts plus ``reason`` (why it is not
    healthy, '' when it is) and ``host_up`` (True when the host itself answered something)."""
    cred, cred_problem = _credential(target)
    out = {"dns": None, "tcp": None, "reason": "", "host_up": False, "blocked": False}
    host = _host(target)
    started = time.monotonic()
    ok, ip, detail, blocked = probe_dns(host)
    attempts.add("DNS", ok, _ms(started), detail, level="ok" if ok else "warn")
    out["dns"] = ok
    if not ok:
        out["reason"] = detail
        out["blocked"] = blocked
        return out
    out["ip"] = ip

    started = time.monotonic()
    state, tcp_detail = probe_tcp(ip, target.port)
    attempts.add(f"TCP {target.port}", state == "open", _ms(started), tcp_detail)
    out["tcp"] = state == "open"
    out["tcp_detail"] = tcp_detail
    if state == "refused":
        out["host_up"] = True
    if state != "open":
        out["reason"] = tcp_detail
        return out
    out["host_up"] = True

    started = time.monotonic()
    sent = cred is not None and cred.kind in HTTP_KINDS
    fetch = _fetch_with_credential(target, ip, cred, attempts)
    out["http_status"] = fetch["status"]
    if fetch.get("vault"):
        attempts.add("GET /metrics", False, _ms(started), "The credential vault is unavailable; the stored credential could not be used.")
        out["reason"] = "The credential vault is unavailable, so the exporter credential could not be used."
        return out
    parsed, error, accepted = _classify(fetch, sent)
    if accepted is not None:
        out["creds_accepted"] = accepted
    if parsed is None:
        attempts.add("GET /metrics", False, _ms(started), error)
        out["reason"] = error
        return out
    label = parsed["exporter"] or "Prometheus metrics"
    version = f" {parsed['exporter_version']}" if parsed.get("exporter_version") else ""
    attempts.add("GET /metrics", True, _ms(started), f"HTTP {fetch['status']}, {label}{version}")
    out["metrics_ok"] = True
    for key in ("exporter", "exporter_version", "hostname", "os", "kernel", "arch", "cpus", "memory_mb"):
        if parsed.get(key) not in (None, ""):
            out[key] = parsed[key]
    out["facts_source"] = "metrics"
    return out


# ----------------------------------------------------------------------------- SSH
def ssh_wanted(target):
    cred, _ = _credential(target)
    return cred is not None and cred.kind in SSH_KINDS


def _pinned(target):
    return str((target.facts or {}).get("ssh_host_key") or "")


class _SSH:
    """Context manager: vault read -> SSH session -> close. Only SSHFailure / SSHUnavailable /
    vault errors escape, and none of their messages contain a secret."""

    def __init__(self, target, cred, reason, budget):
        self.target, self.cred, self.reason, self.budget = target, cred, reason, budget
        self._use = None
        self.session = None

    def __enter__(self):
        self._use = vault.use(self.cred, None, reason=self.reason)
        secret = self._use.__enter__()
        try:
            self.session = ssh.open_session(
                _host(self.target), self.cred.username, secret, self.cred.kind,
                pinned=_pinned(self.target), budget=self.budget)
        except BaseException:
            self._use.__exit__(None, None, None)
            raise
        return self.session

    def __exit__(self, *exc):
        if self.session is not None:
            self.session.close()
        if self._use is not None:
            self._use.__exit__(None, None, None)
        return False


def _ssh_failure_facts(exc):
    """Facts + verdict contribution for an SSH failure (the messages are safe by construction)."""
    if isinstance(exc, ssh.SSHUnavailable):
        return "unavailable", str(exc)
    if isinstance(exc, ssh.SSHFailure):
        return exc.kind, exc.message
    if isinstance(exc, (vault.VaultUnavailable, vault.VaultError)):
        return "vault", str(exc)
    return "error", f"SSH failed ({type(exc).__name__})."


def _ssh_facts(target, cred, attempts, out, budget=ssh.FACTS_BUDGET, then=None):
    """Open SSH, pin the host key, gather facts; with `then`, run it on the same session afterwards.
    Fills `out`; returns the outcome kind (ok | hostkey_changed | auth | connect | ...)."""
    started = time.monotonic()
    try:
        with _SSH(target, cred, f"{target.hostname}: host facts over SSH", budget) as session:
            fp = session.host_key
            pinned = _pinned(target)
            attempts.add("SSH login", True, _ms(started),
                         f"logged in as {cred.username}; host key {fp} "
                         + ("matches the pin" if pinned else "pinned on first connect"))
            out["ssh"] = "ok"
            out["ssh_user"] = cred.username
            out["ssh_host_key"] = fp
            out["ssh_host_key_type"] = session.host_key_type
            gathered = time.monotonic()
            facts = ssh.gather_facts(session, target.target_type)
            attempts.add("SSH facts", bool(facts), _ms(gathered),
                         f"{len(facts)} fact(s): " + ", ".join(sorted(facts)[:8]))
            out["ssh_facts"] = facts
            out["ssh_mode"] = ssh.sudo_mode(facts, cred.kind)
            if then is not None:
                out["then_started"] = True
                out["then"] = then(session, out)
            return "ok"
    except ssh.HostKeyChanged as exc:
        attempts.add("SSH login", False, _ms(started), exc.message, level="error")
        out["ssh"] = "hostkey_changed"
        out["ssh_error"] = exc.message
        return "hostkey_changed"
    except (ssh.SSHUnavailable, ssh.SSHFailure, vault.VaultUnavailable, vault.VaultError) as exc:
        kind, message = _ssh_failure_facts(exc)
        attempts.add("SSH session", False, _ms(started), message)
        out["ssh"] = kind if not out.get("then_started") else out.get("ssh", kind)
        out["ssh_error"] = message
        return kind
    except Exception as exc:                   # noqa: BLE001 - never echo a message that could carry a secret
        log.error("onboarding ssh failed for %s: %s", target.hostname, type(exc).__name__)
        message = f"SSH failed unexpectedly ({type(exc).__name__}); see the server log."
        attempts.add("SSH session", False, _ms(started), message)
        out["ssh"] = "error"
        out["ssh_error"] = message
        return "error"


# ----------------------------------------------------------------------------- verify
def _base_facts():
    return dict(_FACT_DEFAULTS)


def _merge_ssh(facts, out):
    """SSH facts are authoritative for the host; the metrics fill the gaps."""
    ssh_facts = out.pop("ssh_facts", {}) or {}
    for key in ("hostname", "os", "kernel", "arch", "cpus", "memory_mb"):
        value = ssh_facts.get(key)
        if value not in (None, ""):
            facts[key] = value
    for key in ("exporter_installed", "exporter_service", "exporter_installed_version"):
        if key in ssh_facts:
            facts[key] = ssh_facts[key]
    if ssh_facts:
        facts["facts_source"] = "ssh+metrics" if out.get("metrics_ok") else "ssh"


def verify(target):
    """The verify_target hook for Linux and Windows servers. See the module docstring."""
    started = time.monotonic()
    attempts = Attempts(target, "verify")
    out = probe_metrics(target, attempts)
    facts = _base_facts()
    for key in ("dns", "ip", "tcp", "tcp_detail", "http_status", "exporter", "exporter_version", "metrics_ok",
                "hostname", "os", "kernel", "arch", "cpus", "memory_mb", "creds_accepted", "facts_source"):
        if key in out:
            facts[key] = out[key]
    cred, cred_problem = _credential(target)
    if cred_problem:
        facts["ssh"], facts["ssh_error"] = "refused", cred_problem
    ssh_kind = ""
    if out["dns"] and cred is not None and cred.kind in SSH_KINDS:
        ssh_kind = _ssh_facts(target, cred, attempts, out)
        for key in ("ssh", "ssh_user", "ssh_error", "ssh_host_key", "ssh_host_key_type"):
            if key in out:
                facts[key] = out[key]
        if ssh_kind == "ok":
            _merge_ssh(facts, out)
    elif cred is not None and cred.kind in SSH_KINDS and not out["dns"]:
        facts["ssh"], facts["ssh_error"] = "skipped", "Skipped: the host name did not resolve."
    elif cred is None and not cred_problem:
        facts["ssh"] = "no_credential"

    facts["attempts"] = attempts.facts()
    facts["duration_ms"] = round(_ms(started), 1)
    facts["verified_at"] = timezone.now().isoformat(timespec="seconds")
    if target.target_type == TargetType.LINUX and facts["exporter"] == "windows_exporter":
        facts["warning"] = "This looks like a Windows host, but the target is typed as a Linux server."
    elif target.target_type == TargetType.WINDOWS and facts["exporter"] == "node_exporter":
        facts["warning"] = "This looks like a Linux host, but the target is typed as a Windows server."

    # ---- the verdict
    if ssh_kind == "hostkey_changed":
        return {**facts, "state": TargetState.FAILED, "error": facts["ssh_error"]}
    if facts["metrics_ok"]:
        facts["note"] = ""
        return {**facts, "state": TargetState.EXPORTER_FOUND}
    if ssh_kind == "ok":
        facts["note"] = (f"No exporter answers on :{target.port}. The host is reachable over SSH, so the next "
                         "step can install it.")
        return {**facts, "state": TargetState.REACHABLE}
    if out["dns"] is False:
        return {**facts, "state": TargetState.UNREACHABLE, "error": out["reason"]}
    if out["tcp"] is False and out["host_up"]:
        facts["note"] = (f"The host answers but nothing listens on :{target.port} yet. The next step shows the "
                         "install command" + (" and can run it over SSH." if ssh.available() else "."))
        return {**facts, "state": TargetState.REACHABLE}
    if out["tcp"] is False:
        return {**facts, "state": TargetState.UNREACHABLE, "error": out["reason"]}
    # the port is open but /metrics is not usable
    return {**facts, "state": TargetState.REACHABLE, "error": out["reason"]}


# ----------------------------------------------------------------------------- enable
def install_details(target):
    """What the operator needs to install the exporter by hand (or None for other target types)."""
    return install.details(target.target_type, target.port)


def _short(text, limit=300):
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _recheck(target, attempts, tries=3, pause=2.0):
    """After an install: does /metrics answer from here? Returns the probe dict of the last try."""
    out = {}
    for i in range(tries):
        out = probe_metrics(target, attempts)
        if out.get("metrics_ok"):
            break
        if i + 1 < tries:
            time.sleep(pause)
    return out


def enable(target, options=None):
    """The enable_collection hook for Linux and Windows servers. See the module docstring."""
    options = options or {}
    info = install_details(target)
    started = time.monotonic()
    attempts = Attempts(target, "enable")
    result = {"install_command": info["command"], "install_url": info["download_url"],
              "install_port": info["port"], "install_output": ""}

    # 1. is an exporter already answering? then there is nothing to install (idempotent)
    out = probe_metrics(target, attempts)
    if out.get("metrics_ok"):
        result.update({k: out[k] for k in ("exporter", "exporter_version") if k in out})
        result.update({"install_command": "", "state": TargetState.EXPORTER_FOUND, "metrics_ok": True,
                       "attempts": attempts.facts(), "warning": "",
                       "note": f"{out['exporter'] or 'An exporter'} already answers on :{target.port}; nothing to install."})
        return result

    # 2. no exporter: without the operator's tick the answer is the command
    if not options.get("install_over_ssh"):
        result.update({"attempts": attempts.facts(),
                       "note": ("No exporter answers yet. Run the command on the host (or tick the SSH install) "
                                "and press Enable collection again.")})
        return result

    # 3. the operator ticked "install over SSH"
    cred, cred_problem = _credential(target)
    state_now = target.state
    if cred is None or cred.kind not in SSH_KINDS:
        result.update({"state": state_now, "attempts": attempts.facts(),
                       "error": cred_problem or "Install over SSH needs an SSH password or key credential on this target. "
                                                "Use the command instead, or attach one on the Credentials step."})
        return result
    if not ssh.available():
        result.update({"state": state_now, "attempts": attempts.facts(),
                       "error": "SSH support (paramiko) is not installed on this server, so nothing was installed. "
                                "Run the command on the host, or install requirements-ssh.txt."})
        return result

    ssh_out = {}

    def do_install(session, out):
        began = time.monotonic()
        code, stdout, stderr = session.run_install(target.target_type, target.port, out.get("ssh_mode", "sudo_n"))
        out["install_ms"] = _ms(began)
        return code, stdout, stderr

    kind = _ssh_facts(target, cred, attempts, ssh_out, budget=ssh.INSTALL_BUDGET, then=do_install)
    result["attempts"] = attempts.facts()
    for key in ("ssh", "ssh_user", "ssh_error", "ssh_host_key", "ssh_host_key_type"):
        if key in ssh_out:
            result[key] = ssh_out[key]
    host_facts = ssh_out.get("ssh_facts", {})
    for key in ("hostname", "os", "kernel", "arch", "cpus", "memory_mb"):
        if host_facts.get(key) not in (None, ""):
            result[key] = host_facts[key]
    if kind == "hostkey_changed":
        result.update({"state": TargetState.FAILED, "error": ssh_out["ssh_error"]})
        return result
    if "then" not in ssh_out:
        if ssh_out.get("then_started"):
            # the session ended while the install script ran: the host may be partly changed
            result.update({"state": TargetState.FAILED,
                           "error": (f"The SSH session ended before the install finished ({ssh_out.get('ssh_error', 'no detail')}). "
                                     "The host may be partly changed: check the service, or run the install command.")})
        else:
            result.update({"state": state_now, "error": f"Nothing was installed. {ssh_out.get('ssh_error', '')}".strip()})
        return result
    code, stdout, stderr = ssh_out["then"]
    t_install_ms = ssh_out.get("install_ms", 0.0)
    output = (stdout + ("\n" + stderr if stderr else "")).strip()[-2000:]
    result["install_output"] = output
    if code != 0:
        reason = _short(stderr or stdout or f"the install script exited with status {code}")
        attempts.add("SSH install", False, t_install_ms, f"exit {code}: {reason}", level="error")
        result.update({"state": TargetState.FAILED, "attempts": attempts.facts(),
                       "error": f"Install failed; the host was left as it was. {reason}"})
        return result
    attempts.add("SSH install", True, t_install_ms, _short(stdout.strip().splitlines()[-1] if stdout.strip() else "installed"))

    # 4. confirm from here
    after = _recheck(target, attempts)
    result["attempts"] = attempts.facts()
    result.update({k: after[k] for k in ("exporter", "exporter_version", "http_status") if k in after})
    result["install_command"] = ""
    result["duration_ms"] = round(_ms(started), 1)
    if after.get("metrics_ok"):
        result.update({"state": TargetState.INSTALLED, "metrics_ok": True, "warning": "",
                       "note": f"{info['exporter']} installed over SSH and answering on :{target.port}."})
    else:
        result.update({"state": TargetState.INSTALLED, "metrics_ok": False,
                       "warning": (f"{info['exporter']} was installed and started, but LinkedEye cannot reach "
                                   f"http://{_host(target)}:{target.port}/metrics from here ({after.get('reason') or 'no answer'}). "
                                   "Open the port to Prometheus; scraping may still work from its network.")})
    return result
