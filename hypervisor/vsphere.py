"""vSphere Automation REST client: standard library only, READ-ONLY.

The guarantee, enforced in code and by tests:

* the only HTTP method this module ever sends is ``GET``, with ONE documented exception: the session handshake,
  ``POST /api/session`` (Basic auth, returns a session id) and ``DELETE /api/session`` (ends it). ``_send`` refuses any other
  (method, path) pair before a byte leaves the process, and ``hypervisor/tests/test_client.py`` fails if another method
  appears in this file or on the wire. There is no write helper to call;
* no redirect is followed (a 3xx is an error), every body is capped at 1 MB, one run has a wall-clock deadline that a trickling
  server cannot stretch, and a JSON body that is not JSON is an error;
* the host goes through the probe SSRF policy (``probes.checks.resolve_allowed``): private ranges must be listed in
  ``LINKEDEYE_PROBE_ALLOWED_PRIVATE_CIDRS``. The connection is made to the address that passed the check and the original
  name is used for SNI and certificate verification (no second DNS lookup);
* certificate handling is configured per source: ``verify`` (system trust store), ``pin`` (SHA-256 of the leaf certificate;
  nothing, not even the password, is sent before it matches) or ``skip`` (explicitly unsafe);
* credentials are used for one Basic header and then forgotten; error text is built from fixed strings and never contains the
  password, the Authorization header or a response body.
"""
import base64
import hashlib
import hmac
import http.client
import json
import logging
import re
import socket
import ssl
import threading
import time
from urllib.parse import urlencode

from django.views.decorators.debug import sensitive_variables

from . import parsers

log = logging.getLogger("linkedeye.hypervisor.client")

SESSION_PATH = "/api/session"
READ_METHODS = frozenset({"GET"})
#: the ONLY non-GET requests this client may send: log in and log out
SESSION_REQUESTS = frozenset({("POST", SESSION_PATH), ("DELETE", SESSION_PATH)})
SESSION_HEADER = "vmware-api-session-id"
SESSION_RE = re.compile(r"^[A-Za-z0-9_\-+=/.]{8,300}$")

MAX_BODY = 1024 * 1024
CONNECT_TIMEOUT = 5
READ_TIMEOUT = 10
REQUEST_DEADLINE = 25
RUN_DEADLINE = 90
MAX_HOSTS = 500
MAX_CLUSTERS = 200
MAX_DATASTORES = 300
MAX_VMS = 5000
MAX_DETAIL = 150
MAX_LIST = 20000
FANOUT_RESERVE = 15          # seconds of the run deadline kept free of the placement fan-out (for the per-VM details)
USER_AGENT = "LinkedEye-Hypervisor/1.0"
REJECTED_PREFIX = "vCenter rejected the account"
HINT_PRIVATE = "A private address must be listed in LINKEDEYE_PROBE_ALLOWED_PRIVATE_CIDRS."


class VSphereError(Exception):
    """A problem the operator can read. ``kind``: network, timeout, tls, pin, policy, auth, http, protocol, limit, refused."""

    def __init__(self, message, kind="network", status=None):
        super().__init__(message)
        self.kind = kind
        self.status = status


def describe(exc):
    """A short, secret-free description of a socket / TLS / HTTP exception."""
    if isinstance(exc, VSphereError):
        return str(exc)
    if isinstance(exc, ssl.SSLCertVerificationError):
        return "the certificate could not be verified: " + parsers.clean_text(getattr(exc, "verify_message", "") or "untrusted", 120)
    if isinstance(exc, ssl.SSLError):
        return "TLS handshake failed (" + parsers.clean_text(getattr(exc, "reason", "") or type(exc).__name__, 60) + ")"
    if isinstance(exc, socket.gaierror):
        return "the host name does not resolve"
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "the connection timed out"
    if isinstance(exc, ConnectionRefusedError):
        return "connection refused"
    if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, http.client.RemoteDisconnected, BrokenPipeError)):
        return "the connection was closed by the server"
    if isinstance(exc, http.client.HTTPException):
        return "the server sent a malformed HTTP answer"
    if isinstance(exc, OSError):
        return f"network error ({type(exc).__name__})"
    return type(exc).__name__


class _Watchdog:
    """Shuts the socket down once the budget is spent, which unblocks a pending read: a socket timeout alone bounds each
    recv separately, so a server sending one byte per timeout window would otherwise never finish."""

    def __init__(self, seconds, sock_of):
        self.expired = False
        self._sock_of = sock_of
        self._timer = threading.Timer(max(seconds, 0.01), self._fire)
        self._timer.daemon = True
        self._timer.start()

    def _fire(self):
        self.expired = True
        sock = self._sock_of()
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def cancel(self):
        self._timer.cancel()


def _context(tls_mode):
    if tls_mode == "verify":
        ctx = ssl.create_default_context()
    else:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def normalise_fingerprint(value):
    """64 lower-case hex characters from 'AB:CD:..' / 'sha256:abcd..' input, or '' when it is not a SHA-256 fingerprint."""
    value = re.sub(r"^sha-?256[:=]?", "", (value or "").strip().lower())
    value = value.replace(":", "").replace(" ", "")
    return value if re.fullmatch(r"[0-9a-f]{64}", value) else ""


def format_fingerprint(value):
    value = normalise_fingerprint(value)
    return ":".join(value[i:i + 2] for i in range(0, 64, 2)).upper() if value else ""


class _Conn(http.client.HTTPSConnection):
    """HTTPS to an address that already passed the SSRF policy, with the original name for SNI / verification. In pin mode the
    leaf certificate's SHA-256 is compared with the pinned value right after the handshake, before anything is sent."""

    def __init__(self, host, ip, port, timeout, context, pin=None):
        super().__init__(host, port, timeout=timeout, context=context)
        self._ip = ip
        self._pin = pin
        self.fingerprint = ""

    def connect(self):
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        # the handshake is a separate step so the watchdog can already shut the socket down while it runs
        tls = self._context.wrap_socket(sock, server_hostname=self.host, do_handshake_on_connect=False)
        self.sock = tls
        tls.do_handshake()
        der = tls.getpeercert(binary_form=True)
        self.fingerprint = hashlib.sha256(der).hexdigest() if der else ""
        if self._pin is not None and not (self.fingerprint and hmac.compare_digest(self.fingerprint, self._pin)):
            self.close()
            raise VSphereError("The certificate fingerprint does not match the pinned one. Nothing was sent. If the certificate "
                               "was replaced on purpose, test the connection and confirm the new fingerprint.", "pin")


class AddressRefused(OSError):
    """The socket a library opened by itself reached an address the SSRF policy refuses (a second DNS answer, another record)."""


class _GuardedContext(ssl.SSLContext):
    """A TLS context for a library that opens its own connections and resolves the host name again (pyvmomi).

    Before the handshake, the address the socket actually reached is checked against the SSRF policy, so a rebound name or a
    second (private, link-local, metadata) record never receives a byte. In pin mode the leaf certificate is then compared with
    the pinned SHA-256 on the very connection that goes on to carry the login; no separate pre-check connection is needed, so
    there is nothing to swap in between. Every socket it wraps is remembered in ``sockets`` so a run deadline can shut them
    down (``shut()``), after which it refuses new connections."""
    pin = ""

    def wrap_socket(self, sock, *args, do_handshake_on_connect=True, **kwargs):
        sockets = self.__dict__.setdefault("sockets", [])
        try:
            if self.__dict__.get("closed"):
                raise AddressRefused("the run deadline has passed")
            from probes import checks
            try:
                checks.check_address(str(sock.getpeername()[0]).split("%", 1)[0])
            except (checks.ProbeBlocked, ValueError, IndexError, TypeError) as exc:
                raise AddressRefused(f"the connected address is refused by the network policy ({type(exc).__name__})") from None
            sockets.append(sock)                                  # the handshake below can be cut short by shut()
            tls = super().wrap_socket(sock, *args, do_handshake_on_connect=False, **kwargs)
        except BaseException:
            sock.close()
            raise
        sockets.append(tls)
        try:
            if do_handshake_on_connect or self.pin:
                tls.do_handshake()
            if self.pin:
                der = tls.getpeercert(binary_form=True)
                seen = hashlib.sha256(der).hexdigest() if der else ""
                if not (seen and hmac.compare_digest(seen, self.pin)):
                    raise ssl.SSLError("the certificate does not match the pinned fingerprint")
        except BaseException:
            tls.close()
            raise
        return tls

    def shut(self):
        """Refuse new connections and shut every open one down (unblocks a pending read on a stalled server)."""
        self.__dict__["closed"] = True
        for sock in list(self.__dict__.get("sockets", [])):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


_PinnedContext = _GuardedContext


def shut_after(ctx, seconds):
    """Start (and return) a daemon timer that calls ``ctx.shut()`` once ``seconds`` have passed; ``cancel()`` it when done."""
    timer = threading.Timer(max(seconds, 0.01), ctx.shut)
    timer.daemon = True
    timer.start()
    return timer


def guarded_context(tls_mode, pin=""):
    """The context handed to pyvmomi: ``verify`` (system trust store and host name), ``pin`` (only the certificate whose
    SHA-256 is ``pin``; no CA or host name check: the pin is the trust) or ``skip``. Every mode re-checks the connected address."""
    ctx = _GuardedContext(ssl.PROTOCOL_TLS_CLIENT)
    if tls_mode not in ("pin", "skip"):                                 # verify, and any unknown mode: fail closed
        ctx.load_default_certs(ssl.Purpose.SERVER_AUTH)               # PROTOCOL_TLS_CLIENT: CERT_REQUIRED and check_hostname
        ctx.verify_flags = ssl.create_default_context().verify_flags  # the same chain checks as ssl.create_default_context()
    else:
        if tls_mode == "pin":
            pin = normalise_fingerprint(pin)
            if not pin:
                raise VSphereError("No certificate fingerprint is pinned yet. Test the connection and confirm the fingerprint first.",
                                   "pin")
            ctx.pin = pin
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def pinned_context(pin):
    """A context that accepts only the certificate whose SHA-256 is ``pin`` (no CA or host name check: the pin is the trust)."""
    return guarded_context("pin", pin)


def _resolve(host, port):
    from probes import checks
    try:
        return checks.resolve_allowed(host, int(port))
    except checks.ProbeBlocked as exc:
        raise VSphereError(f"Host refused by the network policy: {parsers.clean_text(str(exc), 160)}. {HINT_PRIVATE}", "policy") from None
    except (socket.gaierror, OSError) as exc:
        raise VSphereError(describe(exc), "network") from None


def fetch_fingerprint(host, port=443, timeout=CONNECT_TIMEOUT + 3):
    """SHA-256 of the certificate the server presents (64 hex chars). TLS handshake only: no HTTP request, no credentials."""
    ip = _resolve(host, port)
    conn = _Conn(host, ip, int(port), timeout, _context("skip"))
    wd = _Watchdog(timeout + 2, lambda: conn.sock)
    try:
        conn.connect()
        if wd.expired:
            raise VSphereError("The connection timed out.", "timeout")
        return conn.fingerprint
    except VSphereError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise VSphereError(describe(exc), "timeout" if wd.expired else "tls" if isinstance(exc, ssl.SSLError) else "network") from None
    finally:
        wd.cancel()
        conn.close()


class VSphereClient:
    """One authenticated session. Use as a context manager (logs in on entry, logs out on exit)."""

    @sensitive_variables("password")
    def __init__(self, host, port, username, password, *, tls_mode="verify", pinned_sha256="", deadline=RUN_DEADLINE):
        self.host, self.port = host, int(port)
        self.tls_mode = tls_mode if tls_mode in ("verify", "pin", "skip") else "verify"
        self._pin = normalise_fingerprint(pinned_sha256) if self.tls_mode == "pin" else None
        if self.tls_mode == "pin" and not self._pin:
            raise VSphereError("No certificate fingerprint is pinned yet. Test the connection and confirm the fingerprint first.", "pin")
        self._auth = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
        self._secrets = [s for s in (password, username) if s]
        self._deadline = time.monotonic() + deadline
        self._ip = None
        self._conn = None
        self._session = ""
        self.fingerprint = ""
        self.requests = 0

    def __repr__(self):
        return f"<VSphereClient {self.host}:{self.port}>"

    def __enter__(self):
        self.login()
        return self

    def __exit__(self, *exc):
        self.logout()
        return False

    # ------------------------------------------------------------------ transport
    def remaining(self):
        return self._deadline - time.monotonic()

    def _scrub(self, text):
        for s in self._secrets:
            text = text.replace(s, "***")
        return text

    def _connect(self):
        remaining = self.remaining()
        if remaining <= 0:
            raise VSphereError("The sync ran out of time.", "timeout")
        if self._ip is None:
            self._ip = _resolve(self.host, self.port)
        self._conn = _Conn(self.host, self._ip, self.port, min(READ_TIMEOUT, max(1, remaining)), _context(self.tls_mode), self._pin)

    def _close(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except OSError:
                pass
        self._conn = None

    def _send(self, method, path, headers=None, _retry=True):
        """The single place a request leaves the process. Anything but GET (and the two session requests) is refused here."""
        if method not in READ_METHODS and (method, path) not in SESSION_REQUESTS:
            raise VSphereError(f"{method} {path} is not allowed: this integration is read-only.", "refused")
        if not path.startswith("/api/") or any(c in path for c in "\r\n \x00") or ".." in path:
            raise VSphereError("Refused a malformed request path.", "refused")
        remaining = self.remaining()
        if remaining <= 0:
            raise VSphereError("The sync ran out of time.", "timeout")
        fresh = self._conn is None
        if fresh:
            self._connect()
        conn = self._conn
        wd = _Watchdog(min(remaining, REQUEST_DEADLINE), lambda: conn.sock)
        hdrs = {"Accept": "application/json", "User-Agent": USER_AGENT, "Connection": "keep-alive"}
        hdrs.update(headers or {})
        try:
            conn.request(method, path, headers=hdrs)
            self.requests += 1
            if conn.fingerprint:
                self.fingerprint = conn.fingerprint
            resp = conn.getresponse()
            length = resp.getheader("Content-Length")
            if length and length.isdigit() and int(length) > MAX_BODY:
                self._close()
                raise VSphereError("The answer is larger than the 1 MB limit.", "limit")
            body = resp.read(MAX_BODY + 1)
            if len(body) > MAX_BODY:
                self._close()
                raise VSphereError("The answer is larger than the 1 MB limit.", "limit")
            if resp.will_close:
                self._close()
            return resp.status, body
        except VSphereError:
            self._close()
            raise
        except Exception as exc:  # noqa: BLE001 - mapped to a readable, secret-free message
            expired = wd.expired
            self._close()
            if (not expired and not fresh and _retry and method in READ_METHODS
                    and isinstance(exc, (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError))):
                wd.cancel()
                return self._send(method, path, headers, _retry=False)       # the server dropped an idle connection
            if expired:
                raise VSphereError("The vCenter did not answer in time.", "timeout") from None
            kind = "tls" if isinstance(exc, ssl.SSLError) else "timeout" if isinstance(exc, (socket.timeout, TimeoutError)) else "network"
            raise VSphereError(self._scrub(describe(exc)), kind) from None
        finally:
            wd.cancel()

    @staticmethod
    def _json(body):
        try:
            data = json.loads(body.decode("utf-8")) if body else None
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise VSphereError("vCenter sent an answer that is not valid JSON.", "protocol") from None
        if isinstance(data, dict) and set(data) == {"value"}:       # the older /rest envelope
            data = data["value"]
        return data

    # ------------------------------------------------------------------ session (the only non-GET requests)
    def login(self):
        auth, self._auth = self._auth, None
        if auth is None:
            raise VSphereError("This client already logged in once.", "refused")
        status, body = self._send("POST", SESSION_PATH, {"Authorization": auth})
        del auth
        if status in (401, 403):
            raise VSphereError(f"{REJECTED_PREFIX} (HTTP {status}). Check the user name and password and that the "
                               "account may log in to the API.", "auth", status)
        if status not in (200, 201):
            raise VSphereError(f"vCenter answered the login with HTTP {status}. The REST API (/api/session) needs vSphere 7.0 or later.",
                               "http", status)
        token = self._json(body)
        if not isinstance(token, str) or not SESSION_RE.fullmatch(token):
            raise VSphereError("vCenter sent no usable session id.", "protocol")
        self._session = token

    def logout(self):
        if not self._session:
            return
        try:
            self._send("DELETE", SESSION_PATH, {SESSION_HEADER: self._session}, _retry=False)
        except VSphereError:
            pass
        finally:
            self._session = ""
            self._close()

    # ------------------------------------------------------------------ reads
    def get(self, path, params=None, optional=False):
        """GET ``path`` and return the parsed JSON. ``optional=True`` returns None for an HTTP error status (a powered-off VM has
        no guest data, an older vCenter lacks an endpoint) but still raises for network, TLS, timeout and policy errors."""
        if not self._session:
            raise VSphereError("Not logged in.", "refused")
        full = path + ("?" + urlencode(params) if params else "")
        status, body = self._send("GET", full, {SESSION_HEADER: self._session})
        if status == 401:
            raise VSphereError("vCenter ended the session (HTTP 401).", "auth", status)
        if 300 <= status < 400:
            raise VSphereError(f"vCenter answered with a redirect (HTTP {status}); redirects are never followed.", "http", status)
        if status != 200:
            if optional:
                return None
            raise VSphereError(f"vCenter answered HTTP {status} for {path.split('?')[0]}.", "http", status)
        return self._json(body)

    def get_list(self, path, params=None, optional=False):
        data = self.get(path, params, optional)
        if data is None:
            return []
        if not isinstance(data, list):
            raise VSphereError(f"vCenter sent an unexpected answer for {path}.", "protocol")
        return data[:MAX_LIST]


# ---------------------------------------------------------------------- the inventory pass
def collect_inventory(client, detail_order=(), max_detail=MAX_DETAIL):
    """Read hosts, clusters, datastores, VM summaries and placement, then per-VM details for ``detail_order`` (VM morefs, most
    overdue first; or a function of the VM summaries returning them) until ``max_detail`` or the run deadline. Returns plain dicts (see hypervisor.parsers); no database access."""
    warnings = []
    version = client.get("/api/appliance/system/version", optional=True)
    api = parsers.clean_text((version or {}).get("version") if isinstance(version, dict) else "", 40)

    # the same id listed twice (a buggy or hostile answer) is kept once: a duplicate would break the unique rows we store
    hosts = list({h["moref"]: h for h in (parsers.parse_host(r) for r in client.get_list("/api/vcenter/host")) if h}.values())
    hosts_capped, hosts = len(hosts) > MAX_HOSTS, hosts[:MAX_HOSTS]      # past the cap, the per-host lists cannot cover every VM
    clusters = list({c["moref"]: c for c in (parsers.parse_cluster(r) for r in client.get_list("/api/vcenter/cluster", optional=True))
                     if c}.values())[:MAX_CLUSTERS]
    # Placement costs one request per datastore, cluster and host. On a large or slow estate that fan-out can outlast the run
    # deadline: it stops FANOUT_RESERVE seconds before the end (what was not read keeps its earlier value) and starts at a
    # different point every few minutes, so successive passes cover the whole estate.
    cut = []

    def time_left():
        if client.remaining() < FANOUT_RESERVE:
            cut.append(True)
        return not cut

    datastores, seen_ds = [], set()
    for raw in client.get_list("/api/vcenter/datastore")[:MAX_DATASTORES]:
        ref = parsers.moref(raw.get("datastore")) if isinstance(raw, dict) else ""
        if ref in seen_ds:
            continue
        extra = client.get(f"/api/vcenter/datastore/{ref}", optional=True) if ref and time_left() else None
        ds = parsers.parse_datastore(raw, extra)
        if ds:
            seen_ds.add(ref)
            datastores.append(ds)

    # vSphere 7/8 answer the unfiltered VM list with an error (unable_to_allocate_resource) when more than 4000 VMs match; it is
    # never silently truncated. Then the VMs are read per host and per cluster instead.
    vms, full_list = {}, True
    try:
        rows = client.get_list("/api/vcenter/vm")
    except VSphereError as exc:
        if exc.kind != "http" or not exc.status or exc.status < 400 or not (hosts or clusters):
            raise
        rows, full_list = [], False
        warnings.append(f"vCenter refused the full VM list (HTTP {exc.status}; it does so above 4000 VMs), so VMs are read per host "
                        "and cluster.")
    for row in rows:
        vm = parsers.parse_vm_summary(row)
        if vm:
            vms[vm["moref"]] = vm
    lists_ok = []

    def add_from(filter_name, ref, key, many=False):
        data = client.get("/api/vcenter/vm", {filter_name: ref}, optional=True)
        if not isinstance(data, list):
            lists_ok.append(False)
            return
        for row in data[:MAX_LIST]:
            vm = parsers.parse_vm_summary(row)
            if not vm:
                continue
            cur = vms.setdefault(vm["moref"], vm)
            if many:
                cur.setdefault(key, [])
                if ref not in cur[key]:
                    cur[key].append(ref)
            else:
                cur[key] = ref

    def rotated(items):
        if not items:
            return items
        start = int(time.time() // 300) % len(items)
        return items[start:] + items[:start]

    by_host = {h["moref"]: h for h in hosts}
    for c in rotated(clusters):
        if not time_left():
            break
        for row in client.get_list("/api/vcenter/host", {"clusters": c["moref"]}, optional=True):
            h = parsers.parse_host(row)
            if h and h["moref"] in by_host:
                by_host[h["moref"]]["cluster_moref"], by_host[h["moref"]]["cluster_name"] = c["moref"], c["name"]
        add_from("clusters", c["moref"], "cluster_moref")
    for h in rotated(hosts):
        if not time_left():
            break
        add_from("hosts", h["moref"], "host_moref")
    ds_rank = {d["moref"]: i for i, d in enumerate(datastores)}
    for d in rotated(datastores):
        if not time_left():
            break
        add_from("datastores", d["moref"], "datastore_morefs", many=True)
    for vm in vms.values():
        if len(vm.get("datastore_morefs", ())) > 1:
            vm["datastore_morefs"].sort(key=lambda r: ds_rank.get(r, 0))
    if cut:
        warnings.append("The run deadline came before the placement of every host, cluster and datastore was read; the rest keeps "
                        "its earlier placement and is read on later passes.")
    # without the full list, the per-host lists are the VM list: complete only when every one of them was read
    vms_complete = full_list or (not cut and not hosts_capped and not any(ok is False for ok in lists_ok))
    if len(vms) > MAX_VMS:
        warnings.append(f"Only the first {MAX_VMS} of {len(vms)} VMs are kept.")
        vms = dict(list(vms.items())[:MAX_VMS])

    order = list(detail_order(list(vms.values())) if callable(detail_order) else detail_order)[:max_detail]
    details, partial = {}, bool(cut) or not vms_complete
    for ref in order:
        if ref not in vms:
            continue
        if client.remaining() < 8:
            partial = True
            break
        raw = client.get(f"/api/vcenter/vm/{ref}", optional=True)
        if raw is None:
            continue
        detail = parsers.parse_vm_detail(raw)
        tools = identity = ifaces = None
        if vms[ref]["power_state"] == "on":
            tools = parsers.parse_tools(client.get(f"/api/vcenter/vm/{ref}/tools", optional=True) or {})
            ident_raw = client.get(f"/api/vcenter/vm/{ref}/guest/identity", optional=True)
            identity = parsers.parse_guest_identity(ident_raw) if isinstance(ident_raw, dict) else None
            ifaces_raw = client.get(f"/api/vcenter/vm/{ref}/guest/networking/interfaces", optional=True)
            ifaces = parsers.parse_guest_interfaces(ifaces_raw) if isinstance(ifaces_raw, list) else None
        details[ref] = parsers.merge_guest(detail, tools if tools and tools.get("run_state") else None, identity, ifaces)
    if len(details) < len([r for r in order if r in vms]):
        partial = True
    return {"api": api, "hosts": hosts, "clusters": clusters, "datastores": datastores, "vms": list(vms.values()),
            "details": details, "partial": partial, "warnings": warnings, "requests": client.requests,
            "vms_complete": vms_complete, "placement_partial": bool(cut)}
