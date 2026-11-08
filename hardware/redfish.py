"""A READ-ONLY Redfish client on the standard library (``http.client`` + ``ssl``). No new dependency.

The read-only guarantee is structural, not a convention:

* the class has exactly one way to talk to the BMC, :meth:`RedfishClient.get`, and the HTTP verb is the module constant
  ``METHOD``; there is no write method to call, and ``tests/test_readonly.py`` fails if one appears (it also inspects every
  verb the fake BMC receives);
* no session is created (that would be a POST to ``SessionService``): every request carries HTTP Basic authentication, so
  there is no session token to cache, persist or leak.

Safety rules, all enforced here and not left to callers:

* the host goes through the probe SSRF policy (``probes.checks.resolve_allowed``): private ranges (BMCs usually are) need
  ``LINKEDEYE_PROBE_ALLOWED_PRIVATE_CIDRS``; the connection is made to the address that passed the check, with the original
  name as TLS SNI / Host (no second DNS lookup, so the answer cannot change in between);
* certificate handling is per endpoint: ``verify`` (system CAs), ``pin`` (SHA-256 of the leaf certificate, compared after the
  handshake and BEFORE any credential is sent) or ``skip`` (labelled unsafe in the UI);
* HTTP Basic credentials go over HTTPS only; plain HTTP needs the explicit ``insecure_http`` override (labelled in the UI);
* redirects are never followed (a redirect is an error), JSON only, every response is capped at 1 MB, the whole poll has one
  wall-clock deadline enforced by a watchdog that shuts the socket down, and the number of requests per poll is capped;
* paths are validated: only ``/redfish/v1...`` on the same host, never a URL, never ``..``. Links found inside BMC documents
  are untrusted and pass the same check before they are requested;
* errors are built from fixed phrases. Nothing a BMC sent and nothing from the request headers ever appears in a message,
  and ``scrub`` removes the user name and password from any text before it leaves this module.
"""
import base64
import hashlib
import http.client
import ipaddress
import json
import re
import socket
import ssl
import threading
import time
from urllib.parse import unquote

METHOD = "GET"                       # the only HTTP verb this module sends
ALLOWED_METHODS = frozenset({METHOD})
MAX_BODY = 1024 * 1024               # 1 MB per response
MAX_REQUESTS = 220                   # per client (one poll)
MAX_MEMBERS = 64                     # members of one collection that are followed
REQUEST_TIMEOUT = 10.0
DEFAULT_DEADLINE = 25.0
USER_AGENT = "LinkedEye-Redfish/1.0 (read-only)"

PATH_RE = re.compile(r"^/redfish/v1(?:/[A-Za-z0-9_.\-:@%+()]{1,200}){0,12}/?(?:\?[A-Za-z0-9_.\-=&$%,*()]{0,120})?$")


class RedfishError(Exception):
    """A problem the operator can read. The text is built from fixed phrases and is safe to show and to store."""
    state = "unreachable"


class AuthFailed(RedfishError):
    state = "auth_failed"


class LoginUnavailable(RedfishError):
    """No usable stored login (missing row, vault key missing). Not a rejected login, so it never pauses polling."""
    state = "auth_failed"


class Unreachable(RedfishError):
    state = "unreachable"


class CertificateError(Unreachable):
    """The certificate was refused (verification failed or the pinned fingerprint did not match)."""

    def __init__(self, message, fingerprint=""):
        super().__init__(message)
        self.fingerprint = fingerprint


class NotFound(RedfishError):
    """HTTP 404: the resource does not exist on this BMC (normal for optional sections)."""


class ProtocolError(RedfishError):
    """The BMC answered, but not with something usable (redirect, not JSON, too large, malformed)."""


def scrub(text, *secrets):
    """`text` without any of the secrets (user name, password) in it."""
    text = str(text)
    for s in secrets:
        if s and len(s) >= 3:
            text = text.replace(s, "***")
    return text


def valid_path(path):
    """True for a same-host Redfish path: /redfish/v1/..., no scheme or host, no dot segments."""
    if not isinstance(path, str) or len(path) > 400 or not PATH_RE.fullmatch(path):
        return False
    segs = unquote(path.split("?", 1)[0]).split("/")
    return not any(s in (".", "..") or "\\" in s or "/" in s for s in segs[1:])


def _is_ip(host):
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


class _Watchdog:
    """Shuts the current socket down when the wall-clock deadline passes, which unblocks any pending read (a socket
    timeout alone applies to each read separately, so a peer that drips one byte per timeout would never time out)."""

    def __init__(self, seconds):
        self.expired = False
        self.sock = None
        self._timer = threading.Timer(max(seconds, 0.01), self._fire)
        self._timer.daemon = True

    def start(self):
        self._timer.start()

    def _fire(self):
        self.expired = True
        s = self.sock
        if s is not None:
            try:
                socket.socket.shutdown(s, socket.SHUT_RDWR)      # bypass SSLSocket.shutdown (not thread-safe)
            except OSError:
                pass

    def cancel(self):
        self._timer.cancel()


class _Conn(http.client.HTTPSConnection):
    """HTTPS to a validated address, name kept for SNI and the certificate check; reports the leaf fingerprint."""

    def __init__(self, host, ip, port, timeout, context, watchdog):
        super().__init__(host, port, timeout=timeout, context=context)
        self._ip, self._watchdog = ip, watchdog
        self.fingerprint = ""

    def connect(self):
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        tls = None
        try:
            tls = self._context.wrap_socket(sock, server_hostname=self.host, do_handshake_on_connect=False)
            self._watchdog.sock = tls
            tls.do_handshake()
        except BaseException:
            (tls or sock).close()
            raise
        self.sock = tls
        der = tls.getpeercert(binary_form=True)
        self.fingerprint = hashlib.sha256(der).hexdigest() if der else ""


class _PlainConn(http.client.HTTPConnection):
    def __init__(self, host, ip, port, timeout, watchdog):
        super().__init__(host, port, timeout=timeout)
        self._ip, self._watchdog = ip, watchdog
        self.fingerprint = ""

    def connect(self):
        self.sock = socket.create_connection((self._ip, self.port), self.timeout)
        self._watchdog.sock = self.sock


def _context(verify_mode):
    ctx = ssl.create_default_context()
    if verify_mode != "verify":
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    # BMC firmware is often old: keep the library defaults for ciphers rather than guessing, but never below TLS 1.2
    return ctx


def resolve(host, port):
    """The address to connect to, after the SSRF policy. Raises Unreachable with an operator-readable reason."""
    from probes import checks
    try:
        return checks.resolve_allowed(host, int(port))
    except checks.ProbeBlocked as exc:
        raise Unreachable(f"Host refused by the network policy: {str(exc)[:160]}. A private range must be listed in "
                          "LINKEDEYE_PROBE_ALLOWED_PRIVATE_CIDRS.") from None
    except (socket.gaierror, UnicodeError):
        raise Unreachable("The BMC host name does not resolve.") from None
    except OSError:
        raise Unreachable("The BMC host name could not be resolved.") from None


def _translate(exc, secrets=()):
    """A transport exception as one of ours, with a fixed message."""
    if isinstance(exc, RedfishError):
        return exc
    if isinstance(exc, ssl.SSLCertVerificationError):
        return CertificateError("TLS certificate verification failed: " + scrub(exc.verify_message or "untrusted certificate", *secrets)[:120])
    if isinstance(exc, ssl.SSLError):
        return Unreachable("TLS handshake failed: " + scrub(str(getattr(exc, "reason", "") or "error"), *secrets)[:80])
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return Unreachable("The BMC did not answer in time.")
    if isinstance(exc, ConnectionRefusedError):
        return Unreachable("Connection refused by the BMC.")
    if isinstance(exc, (ConnectionResetError, BrokenPipeError, http.client.RemoteDisconnected)):
        return Unreachable("The BMC closed the connection.")
    if isinstance(exc, socket.gaierror):
        return Unreachable("The BMC host name does not resolve.")
    if isinstance(exc, (http.client.HTTPException, ValueError)):
        return ProtocolError("The BMC sent a malformed HTTP response.")
    if isinstance(exc, OSError):
        return Unreachable("Network error talking to the BMC.")
    return ProtocolError("Unexpected error talking to the BMC.")


class RedfishClient:
    """GET-only client for one BMC. Use as a context manager (it owns a watchdog thread and a connection)."""

    def __init__(self, host, port=443, username="", password="", verify_mode="pin", pinned_sha256="",
                 insecure_http=False, deadline_s=DEFAULT_DEADLINE, request_timeout=REQUEST_TIMEOUT):
        self.host, self.port = host.strip().strip("[]"), int(port)
        self.verify_mode = verify_mode
        self.pinned = (pinned_sha256 or "").lower()
        self.plain = bool(insecure_http)    # plain HTTP is an explicit, labelled override; credentials then travel in clear
        self._user, self._pw = username or "", password or ""
        self._auth = ("Basic " + base64.b64encode(f"{self._user}:{self._pw}".encode("utf-8")).decode("ascii")
                      if self._user or self._pw else "")
        self.deadline_s, self.request_timeout = deadline_s, request_timeout
        self.requests = 0
        self.fingerprint = ""          # of the certificate seen on the last connection
        self._conn = None
        self._ip = None
        self._wd = None
        self._started = None

    def __repr__(self):                                 # never include credentials
        return f"<RedfishClient {self.host}:{self.port} {self.verify_mode}>"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except OSError:
                pass
            self._conn = None
        if self._wd is not None:
            self._wd.cancel()

    # ------------------------------------------------------------------ connection
    def _remaining(self):
        left = self.deadline_s - (time.monotonic() - self._started)
        if left <= 0:
            raise Unreachable("The poll took longer than its deadline.")
        return left

    def _connect(self):
        if self._ip is None:
            self._ip = resolve(self.host, self.port)
        timeout = min(self.request_timeout, self._remaining())
        if self.plain:
            conn = _PlainConn(self.host, self._ip, self.port, timeout, self._wd)
        else:
            conn = _Conn(self.host, self._ip, self.port, timeout, _context(self.verify_mode), self._wd)
        try:
            conn.connect()
        except BaseException as exc:
            if self._wd.expired:
                raise Unreachable("The poll took longer than its deadline.") from None
            raise _translate(exc, (self._user, self._pw)) from None
        if not self.plain:
            self.fingerprint = conn.fingerprint
            if self.verify_mode == "pin":
                # compared before a single request (and so a credential) is sent
                if not self.pinned:
                    conn.close()
                    raise CertificateError("No certificate is pinned yet. Run Test connection and confirm the fingerprint.",
                                           conn.fingerprint)
                if conn.fingerprint != self.pinned:
                    conn.close()
                    raise CertificateError("The certificate does not match the pinned fingerprint. If the BMC certificate "
                                           "was replaced on purpose, pin the new one.", conn.fingerprint)
        self._conn = conn

    # ------------------------------------------------------------------ the one request method
    def get(self, path):
        """GET `path` (must be a valid /redfish/v1 path) and return the parsed JSON object."""
        if not valid_path(path):
            raise ProtocolError("A link in the BMC's answer was not a valid Redfish path and was not followed.")
        if self.requests >= MAX_REQUESTS:
            raise ProtocolError("The request limit for one poll was reached.")
        if self._started is None:
            self._started = time.monotonic()
            self._wd = _Watchdog(self.deadline_s)
            self._wd.start()
        self.requests += 1
        try:
            return self._get(path)
        except RedfishError:
            if self._wd.expired:                    # a truncated answer after the deadline is a timeout, not "malformed"
                raise Unreachable("The poll took longer than its deadline.") from None
            raise
        except BaseException as exc:
            if self._wd.expired:
                raise Unreachable("The poll took longer than its deadline.") from None
            raise _translate(exc, (self._user, self._pw)) from None

    def _get(self, path):
        for attempt in (0, 1):
            fresh = self._conn is None
            if fresh:
                self._connect()
            conn = self._conn
            conn.timeout = min(self.request_timeout, self._remaining())
            if conn.sock is not None:
                conn.sock.settimeout(conn.timeout)
            headers = {"Accept": "application/json", "OData-Version": "4.0", "User-Agent": USER_AGENT,
                       "Accept-Encoding": "identity", "Connection": "keep-alive"}
            if self._auth:
                headers["Authorization"] = self._auth
            try:
                conn.request(METHOD, path, headers=headers)
                resp = conn.getresponse()
                length = resp.getheader("Content-Length")
                if length and length.isdigit() and int(length) > MAX_BODY:
                    self._drop()
                    raise ProtocolError("A response was larger than 1 MB and was refused.")
                body = resp.read(MAX_BODY + 1)
            except (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError):
                self._drop()
                if fresh or attempt:
                    raise
                continue                                    # a kept-alive connection the BMC had closed: reconnect once
            if len(body) > MAX_BODY:
                self._drop()
                raise ProtocolError("A response was larger than 1 MB and was refused.")
            if resp.will_close:
                self._drop()
            return self._interpret(resp.status, resp.getheader("Content-Type", ""), body)
        raise Unreachable("The BMC closed the connection.")      # pragma: no cover

    def _drop(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except OSError:
                pass
        self._conn = None

    @staticmethod
    def _interpret(status, ctype, body):
        if status in (301, 302, 303, 307, 308):
            raise ProtocolError("The BMC answered with a redirect, which is never followed.")
        if status in (401, 403):
            raise AuthFailed(f"The BMC rejected the login (HTTP {status}).")
        if status == 404:
            raise NotFound("Not found on this BMC.")
        if status != 200:
            raise RedfishError(f"The BMC answered HTTP {status if 100 <= status < 600 else 0}.")
        if "json" not in (ctype or "").lower() and not body.lstrip().startswith(b"{"):
            raise ProtocolError("The BMC did not answer with JSON.")
        try:
            doc = json.loads(body.decode("utf-8", "replace"))
        except (ValueError, RecursionError):
            raise ProtocolError("The BMC sent malformed JSON.") from None
        if not isinstance(doc, dict):
            raise ProtocolError("The BMC sent JSON that is not an object.")
        return doc


def make_client(host, port=443, username="", password="", verify_mode="pin", pinned_sha256="", insecure_http=False, **kw):
    """The client for an endpoint's settings (plain HTTP only when `insecure_http` is set)."""
    return RedfishClient(host, port, username, password, verify_mode, pinned_sha256, insecure_http=insecure_http, **kw)


# ---------------------------------------------------------------------- certificate inspection (no credentials)
def inspect_certificate(host, port=443, timeout=8.0):
    """Handshake only (no HTTP request, nothing secret is sent) and report what the BMC presents:
    {"fingerprint": sha256 hex, "trusted": bool (would verify against the system CAs), "tls": version}.
    Raises Unreachable / CertificateError-free: a failure to connect is an Unreachable with a fixed message."""
    ip = resolve(host.strip().strip("[]"), port)
    out = {"fingerprint": "", "trusted": False, "tls": ""}
    wd = _Watchdog(timeout)
    wd.start()
    try:
        for mode in ("skip", "verify"):
            sock = None
            try:
                sock = socket.create_connection((ip, int(port)), timeout)
                wd.sock = sock
                tls = _context(mode).wrap_socket(sock, server_hostname=host.strip().strip("[]"), do_handshake_on_connect=False)
                wd.sock = tls
                sock = tls
                tls.do_handshake()
                if mode == "skip":
                    der = tls.getpeercert(binary_form=True)
                    out["fingerprint"] = hashlib.sha256(der).hexdigest() if der else ""
                    out["tls"] = tls.version() or ""
                else:
                    out["trusted"] = True
            except ssl.SSLCertVerificationError:
                if mode == "skip":
                    raise
            except BaseException as exc:
                if mode == "skip":
                    if wd.expired:
                        raise Unreachable("The BMC did not answer in time.") from None
                    raise _translate(exc) from None
            finally:
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
    finally:
        wd.cancel()
    if not out["fingerprint"]:
        raise Unreachable("The BMC did not present a certificate (is it serving plain HTTP?).")
    return out
