"""One authenticated, SSRF-safe GET for the onboarding steps.

Everything here goes through ``probes.checks``: the host is resolved and every address is validated
against the probe policy (private ranges need ``LINKEDEYE_PROBE_ALLOWED_PRIVATE_CIDRS``), the
connection is pinned to the validated address, and ``checks._Deadline`` bounds the whole request
(connect, TLS, headers and body together) so a peer that trickles bytes cannot hold a worker open.

Differences from ``checks.fetch_bytes`` (which is for public assets): plain http is allowed, any port
is allowed, request headers can be sent (an HTTP basic ``Authorization``), and a redirect is *never*
followed, so a credential cannot be forwarded to a host the operator did not name.

Nothing here logs or returns a header value. Error text is fixed wording or an exception class name;
it never carries the URL's query, a header or the response body.
"""
import http.client
import socket
import ssl
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from django.conf import settings

from probes import checks

DEFAULT_MAX_BYTES = 2 * 1024 * 1024

POLICY_HINT = ("Blocked by the probe policy: the address is private or not a permitted probe target. "
               "Private networks must be listed in LINKEDEYE_PROBE_ALLOWED_PRIVATE_CIDRS.")


@dataclass
class Response:
    ok: bool = False                 # a response was received (any HTTP status)
    status: int = None
    body: bytes = b""
    truncated: bool = False          # the body was longer than max_bytes and was cut
    error: str = ""
    duration_ms: float = None
    ip: str = None
    content_type: str = ""

    @property
    def success(self):
        return self.ok and self.status is not None and 200 <= self.status < 300


def get(url, headers=None, max_bytes=DEFAULT_MAX_BYTES, timeout=None):
    """GET `url` under the SSRF policy. Never raises; failures are reported in ``error``."""
    timeout = timeout or settings.PROBE_TIMEOUT
    res = Response()
    start = time.monotonic()
    watchdog = None
    conn = None
    try:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("Use an http:// or https:// address.")
        if parts.username or parts.password:
            raise ValueError("Put the login in the user name and password fields, not in the address.")
        port = parts.port or (443 if parts.scheme == "https" else 80)
        ip = checks.resolve_allowed(parts.hostname, port)
        res.ip = ip
        remaining = timeout - (time.monotonic() - start)
        if remaining <= 0:
            raise socket.timeout()
        conn = checks._open_connection(parts.scheme, parts.hostname, ip, port, remaining)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        sent = {"User-Agent": checks.USER_AGENT, "Accept": "*/*", "Accept-Encoding": "identity",
                "Connection": "close"}
        sent.update(headers or {})
        watchdog = checks._Deadline(remaining, lambda c=conn: getattr(c, "_probe_sock", None))
        conn.request("GET", path, headers=sent)
        if watchdog.expired:
            raise socket.timeout()
        resp = conn.getresponse()
        res.status = resp.status
        res.content_type = (resp.getheader("Content-Type") or "").split(";")[0].strip().lower()
        chunks, total = [], 0
        while True:
            chunk = resp.read(min(65536, max_bytes + 1 - total))
            if watchdog.expired:
                raise socket.timeout()
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                res.truncated = True
                break
        body = b"".join(chunks)
        res.body = body[:max_bytes]
        res.ok = True
        if resp.status in (301, 302, 303, 307, 308):
            res.ok = False
            res.error = (f"The server redirected (HTTP {resp.status}). Redirects are not followed so a login is "
                         "never sent to another host: enter the final address.")
    except checks.ProbeBlocked:
        res.error = POLICY_HINT
    except socket.gaierror:
        res.error = "DNS resolution failed: the name does not resolve."
    except ssl.SSLCertVerificationError as exc:
        res.error = f"TLS certificate invalid: {exc.verify_message or 'verification failed'}"
    except ssl.SSLError as exc:
        res.error = f"TLS error: {exc.reason or 'handshake failed'}"
    except (socket.timeout, TimeoutError):
        res.error = f"Timed out after {timeout:g}s."
    except ConnectionRefusedError:
        res.error = "Connection refused."
    except ValueError as exc:
        res.error = str(exc)
    except (OSError, http.client.HTTPException) as exc:
        if watchdog is not None and watchdog.expired:
            res.error = f"Timed out after {timeout:g}s."
        else:
            res.error = f"The connection failed ({type(exc).__name__})."
    finally:
        if watchdog is not None:
            watchdog.cancel()
        if conn is not None:
            try:
                conn.close()
            except Exception:                          # noqa: BLE001 - closing must never mask the result
                pass
    if not res.ok:
        res.body = b""
    res.duration_ms = round((time.monotonic() - start) * 1000, 1)
    return res
