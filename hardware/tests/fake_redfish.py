"""A tiny in-process fake Redfish BMC for the tests and for the browser check. It listens on 127.0.0.1 ONLY (random port unless
asked), serves a {path: document} dictionary over HTTPS (self-signed certificate made on the fly) or plain HTTP, and records
every request so a test can assert that nothing but GET was ever sent.

Behaviours a test can switch on: Basic authentication, a slow answer, a redirect, an oversized body, malformed JSON, an
arbitrary status for one path, a drip-feed. Not a general HTTP server: just enough to exercise ``hardware.redfish``.

    with FakeRedfish(fixtures.dell_idrac9(), tls=True) as bmc:
        ...bmc.port, bmc.fingerprint, bmc.requests...

Run it by hand for the browser check:  python -m hardware.tests.fake_redfish dell 8443
"""
import base64
import hashlib
import json
import socket
import ssl
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_CERT_CACHE = {}


def self_signed_cert(common_name="fake-bmc.example.test"):
    """(certfile, keyfile, sha256 hex of the DER certificate), cached per process. Needs the 'cryptography' package."""
    if common_name in _CERT_CACHE:
        return _CERT_CACHE[common_name]
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=30)).sign(key, hashes.SHA256()))
    folder = Path(tempfile.mkdtemp(prefix="fake-bmc-"))
    certfile, keyfile = folder / "cert.pem", folder / "key.pem"
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                          serialization.NoEncryption()))
    fp = hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()
    _CERT_CACHE[common_name] = (str(certfile), str(keyfile), fp)
    return _CERT_CACHE[common_name]


class _Server(ThreadingHTTPServer):
    def handle_error(self, request, client_address):      # a client that hangs up mid-answer is part of the tests: stay quiet
        pass


class FakeRedfish:
    def __init__(self, docs, tls=True, username="monitor", password="correct-horse-battery", port=0, auth=True):
        self.docs, self.tls, self.username, self.password, self.auth = docs, tls, username, password, auth
        self.requests = []                       # [(method, path, authorization header present)]
        self.delay = 0.0                         # seconds to wait before every answer
        self.drip = False                        # answer one byte at a time, slowly
        self.redirects = {}                      # path -> Location
        self.statuses = {}                       # path -> http status to answer with
        self.raw = {}                            # path -> (content type, bytes) to answer with as it is
        self.oversize = set()                    # paths answered with a 2 MB body
        self._lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):        # silence
                pass

            def _serve(self):
                path = self.path
                with fake._lock:
                    fake.requests.append((self.command, path, "Authorization" in self.headers))
                if self.command != "GET":
                    return self._send(405, {"error": "read-only fake"})
                if fake.delay:
                    time.sleep(fake.delay)
                if fake.auth and not fake._authorized(self.headers.get("Authorization", "")):
                    return self._send(401, {"error": "unauthorized"})
                bare = path.split("?", 1)[0]
                key = bare.rstrip("/") or "/"
                if key in fake.redirects:
                    self.send_response(302)
                    self.send_header("Location", fake.redirects[key])
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if key in fake.statuses:
                    return self._send(fake.statuses[key], {"error": "status"})
                if key in fake.raw:
                    ctype, body = fake.raw[key]
                    return self._raw(200, ctype, body)
                if key in fake.oversize:
                    return self._raw(200, "application/json", b'{"x":"' + b"a" * (2 * 1024 * 1024) + b'"}')
                for k in (bare, key, key + "/"):
                    if k in fake.docs:
                        return self._send(200, fake.docs[k])
                return self._send(404, {"error": "not found"})

            def _send(self, status, doc):
                self._raw(status, "application/json; charset=utf-8", json.dumps(doc).encode())

            def _raw(self, status, ctype, body):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    if fake.drip:
                        for i in range(len(body)):
                            self.wfile.write(body[i:i + 1])
                            self.wfile.flush()
                            time.sleep(0.25)
                    else:
                        self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, ssl.SSLError, OSError):
                    pass

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _serve

        self._server = _Server(("127.0.0.1", port), Handler)
        self._server.daemon_threads = True
        self.fingerprint = ""
        if tls:
            certfile, keyfile, self.fingerprint = self_signed_cert()
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(certfile, keyfile)
            self._server.socket = ctx.wrap_socket(self._server.socket, server_side=True)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def _authorized(self, header):
        if not header.startswith("Basic "):
            return False
        try:
            user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
        except Exception:                                   # noqa: BLE001
            return False
        return user == self.username and pw == self.password

    @property
    def methods(self):
        return {m for m, _p, _a in self.requests}

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()


if __name__ == "__main__":                                   # pragma: no cover - manual use for the browser check
    from . import fixtures
    kind = sys.argv[1] if len(sys.argv) > 1 else "dell"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8443
    docs = fixtures.hpe_ilo5() if kind == "hpe" else fixtures.dell_idrac9()
    bmc = FakeRedfish(docs, tls=True, port=port).start()
    print(f"fake {kind} BMC on https://127.0.0.1:{bmc.port}  user=monitor password=correct-horse-battery  sha256={bmc.fingerprint}", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        bmc.stop()
