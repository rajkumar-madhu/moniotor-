"""A tiny Redis server (RESP over loopback) and a stand-in ``redis`` module that talks to it, for tests and manual checks.

Only what the APM reader uses: AUTH, SELECT, PING, EXISTS, STRLEN, GET, QUIT. The stand-in client mimics the parts of
``redis.Redis`` the reader calls, so the whole path (policy check, vault, socket, parse, ingest) runs without the real package."""
import socket
import socketserver
import threading
import types


def _read_command(rfile):
    line = rfile.readline()
    if not line:
        return None
    if not line.startswith(b"*"):
        return line.strip().split()
    parts = []
    for _ in range(int(line[1:])):
        header = rfile.readline()
        size = int(header[1:])
        parts.append(rfile.read(size))
        rfile.read(2)
    return parts


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        server = self.server
        authed = server.password is None
        while True:
            try:
                cmd = _read_command(self.rfile)
            except (ValueError, OSError):
                return
            if cmd is None:
                return
            name = cmd[0].upper()
            args = cmd[1:]
            server.commands.append(name.decode())
            if name == b"AUTH":
                ok = server.password is not None and args[-1].decode() == server.password
                authed = authed or ok
                self.wfile.write(b"+OK\r\n" if ok else b"-WRONGPASS invalid username-password pair\r\n")
            elif not authed:
                self.wfile.write(b"-NOAUTH Authentication required.\r\n")
            elif name == b"PING":
                self.wfile.write(b"+PONG\r\n")
            elif name == b"SELECT":
                self.wfile.write(b"+OK\r\n")
            elif name == b"EXISTS":
                self.wfile.write(b":%d\r\n" % (1 if args[0].decode() in server.data else 0))
            elif name == b"STRLEN":
                self.wfile.write(b":%d\r\n" % len(server.data.get(args[0].decode(), b"")))
            elif name == b"GET":
                v = server.data.get(args[0].decode())
                self.wfile.write(b"$-1\r\n" if v is None else b"$%d\r\n%s\r\n" % (len(v), v))
            elif name == b"QUIT":
                self.wfile.write(b"+OK\r\n")
                return
            else:
                self.wfile.write(b"-ERR unknown command\r\n")


class FakeRedisServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, data=None, password=None):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.data = {k: (v.encode() if isinstance(v, str) else v) for k, v in (data or {}).items()}
        self.password = password
        self.commands = []
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)

    @property
    def port(self):
        return self.server_address[1]

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self.shutdown()
        self.server_close()


def stub_module():
    """A module that looks like ``redis`` for the reader: Redis(...) with ping / exists / strlen / get / close."""
    mod = types.ModuleType("redis")

    class RedisError(Exception):
        pass

    class AuthenticationError(RedisError):
        pass

    class ConnectionError_(RedisError):
        pass

    class Redis:
        def __init__(self, host="localhost", port=6379, db=0, username=None, password=None, socket_connect_timeout=None,
                     socket_timeout=None, ssl=False, ssl_cert_reqs=None, decode_responses=False):
            if ssl:
                raise RedisError("the stand-in client does not speak TLS")
            self._password, self._username, self._db = password, username, db
            try:
                self._sock = socket.create_connection((host, port), timeout=socket_connect_timeout)
            except OSError as exc:
                raise ConnectionError_(f"Error connecting to {host}:{port}. {exc.strerror or exc}.") from None
            self._sock.settimeout(socket_timeout)
            self._rfile = self._sock.makefile("rb")
            self._authed = False

        def _send(self, *parts):
            data = b"*%d\r\n" % len(parts)
            for p in parts:
                p = p if isinstance(p, bytes) else str(p).encode()
                data += b"$%d\r\n%s\r\n" % (len(p), p)
            self._sock.sendall(data)
            line = self._rfile.readline()
            if not line:
                raise ConnectionError_("Connection closed by server.")
            kind, rest = line[:1], line[1:].strip()
            if kind == b"-":
                text = rest.decode()
                raise (AuthenticationError if text.startswith(("WRONGPASS", "NOAUTH")) else RedisError)(text)
            if kind == b"$":
                n = int(rest)
                if n < 0:
                    return None
                value = self._rfile.read(n)
                self._rfile.read(2)
                return value
            if kind == b":":
                return int(rest)
            return rest

        def _auth(self):
            if not self._authed:
                if self._password:
                    self._send(*(["AUTH"] + ([self._username] if self._username else []) + [self._password]))
                self._authed = True

        def ping(self):
            self._auth()
            return self._send("PING") == b"PONG"

        def exists(self, key):
            self._auth()
            return self._send("EXISTS", key)

        def strlen(self, key):
            self._auth()
            return self._send("STRLEN", key)

        def get(self, key):
            self._auth()
            return self._send("GET", key)

        def close(self):
            try:
                self._sock.close()
            except OSError:
                pass

    mod.Redis, mod.RedisError, mod.AuthenticationError, mod.ConnectionError = Redis, RedisError, AuthenticationError, ConnectionError_
    return mod
