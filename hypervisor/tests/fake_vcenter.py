"""A tiny in-process vCenter (vSphere Automation REST) for tests and the browser check. HTTPS on 127.0.0.1 with a throw-away
self-signed certificate; it records every request (method, path, whether the session header was present) so tests can prove the
client only ever sends GET plus the session POST / DELETE.

Not a vCenter: it answers the handful of endpoints ``hypervisor.vsphere`` reads, from a plain dict that tests edit between syncs.
Behaviour switches: ``fail_login`` (401), ``redirect_prefix`` (302 on matching GETs), ``oversize_prefix`` (a body over 1 MB),
``malformed_prefix`` (not JSON), ``slow_prefix`` (sleeps ``slow_seconds`` before answering), ``drop_prefix`` (closes the socket),
``expire_after`` (401 for the session after N GETs).
"""
import datetime
import hashlib
import json
import ssl
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

USER, PASSWORD = "svc-linkedeye-ro", "Correct-Horse-Battery-9"
TOKEN = "fake-session-0123456789abcdef"


_CERT = []


def make_cert():
    """(cert_path, key_path, sha256 hex of the DER certificate), generated once per process. Needs `cryptography`."""
    if _CERT:
        return _CERT[0]
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "vcsa.example.test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
            .sign(key, hashes.SHA256()))
    tmp = Path(tempfile.mkdtemp(prefix="fake-vcenter-"))
    cert_path, key_path = tmp / "cert.pem", tmp / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    _CERT.append((cert_path, key_path, hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()))
    return _CERT[0]


def default_inventory():
    GB = 1024 ** 3
    vms = {
        "vm-101": dict(name="app-01", power="POWERED_ON", cpu=4, mem=16384, host="host-11", cluster="domain-c8", datastores=["datastore-21"],
                       bios="4237a1b2-0000-4000-8000-000000000101", ips=["10.20.1.15", "fe80::1"], hostname="app-01.example.test",
                       os="Ubuntu Linux (64-bit)", family="LINUX", tools="RUNNING"),
        "vm-102": dict(name="db-01", power="POWERED_ON", cpu=8, mem=32768, host="host-11", cluster="domain-c8", datastores=["datastore-21"],
                       bios="4237a1b2-0000-4000-8000-000000000102", ips=["10.20.1.16"], hostname="db-01", os="Red Hat Enterprise Linux 9",
                       family="LINUX", tools="RUNNING"),
        "vm-103": dict(name="web-01", power="POWERED_OFF", cpu=2, mem=4096, host="host-12", cluster="domain-c8", datastores=["datastore-22"],
                       bios="4237a1b2-0000-4000-8000-000000000103", ips=[], hostname="", os="", family="", tools=""),
        "vm-104": dict(name="orphan-01", power="POWERED_ON", cpu=2, mem=2048, host="host-12", cluster="domain-c8", datastores=["datastore-22"],
                       bios="4237a1b2-0000-4000-8000-000000000104", ips=["10.99.9.9"], hostname="orphan-01", os="Windows Server 2022",
                       family="WINDOWS", tools="NOT_RUNNING"),
        "vm-105": dict(name="tools-down-01", power="POWERED_ON", cpu=2, mem=2048, host="host-11", cluster="domain-c8", datastores=["datastore-21"],
                       bios="4237a1b2-0000-4000-8000-000000000105", ips=["10.20.1.19"], hostname="tools-down-01", os="Debian GNU/Linux 12",
                       family="LINUX", tools="NOT_RUNNING"),
    }
    for ref, vm in vms.items():
        n = ref.split("-")[1]
        vm["disks"] = {"2000": {"label": "Hard disk 1", "type": "SCSI", "capacity": 100 * GB,
                                "backing": {"type": "VMDK_FILE", "vmdk_file": f"[{'DS01' if vm['datastores'] == ['datastore-21'] else 'DS02'}] {vm['name']}/{vm['name']}.vmdk"}}}
        vm["nics"] = {"4000": {"label": "Network adapter 1", "type": "VMXNET3", "mac_address": f"00:50:56:aa:bb:{int(n) % 100:02x}",
                               "state": "CONNECTED", "backing": {"type": "STANDARD_PORTGROUP", "network": "network-1", "network_name": "VM Network"}}}
    return {
        "version": "8.0.2.00000",
        "hosts": {"host-11": {"name": "esxi-host-01.example.test", "connection_state": "CONNECTED", "power_state": "POWERED_ON"},
                  "host-12": {"name": "esxi-host-02.example.test", "connection_state": "CONNECTED", "power_state": "POWERED_ON"}},
        "clusters": {"domain-c8": {"name": "Compute-01", "ha_enabled": True, "drs_enabled": True, "hosts": ["host-11", "host-12"]}},
        "datastores": {"datastore-21": {"name": "DS01", "type": "VMFS", "capacity": 1200 * GB, "free": 458 * GB, "accessible": True, "thin": True},
                       "datastore-22": {"name": "DS02", "type": "NFS", "capacity": 2000 * GB, "free": 1500 * GB, "accessible": True, "thin": False}},
        "vms": vms,
    }


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    disable_nagle_algorithm = True
    server_version = "fake-vcenter"

    def log_message(self, *a):
        pass

    @property
    def fake(self):
        return self.server.fake

    def _send(self, status, body=None, raw=None, headers=None):
        data = raw if raw is not None else (b"" if body is None else json.dumps(body).encode())
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _record(self):
        f = self.fake
        f.requests.append((self.command, self.path, bool(self.headers.get("vmware-api-session-id"))))
        self.headers_seen = dict(self.headers)
        f.auth_headers.append(self.headers.get("Authorization", ""))

    def do_POST(self):
        self._record()
        f = self.fake
        if self.path != "/api/session":
            return self._send(405, {"error": "read-only"})
        import base64
        want = "Basic " + base64.b64encode(f"{f.user}:{f.password}".encode()).decode()
        if f.fail_login or self.headers.get("Authorization") != want:
            return self._send(401, {"error_type": "UNAUTHENTICATED"})
        f.logins += 1
        f.gets_since_login = 0
        return self._send(201, TOKEN)

    def do_DELETE(self):
        self._record()
        if self.path != "/api/session":
            return self._send(405, {"error": "read-only"})
        self.fake.logouts += 1
        return self._send(204)

    def do_PUT(self):
        self._record()
        self._send(405)

    do_PATCH = do_PUT

    def do_GET(self):
        self._record()
        f = self.fake
        url = urlsplit(self.path)
        path, query = url.path, parse_qs(url.query)
        if self.headers.get("vmware-api-session-id") != TOKEN:
            return self._send(401, {"error_type": "UNAUTHENTICATED"})
        f.gets_since_login += 1
        if f.expire_after and f.gets_since_login > f.expire_after:
            return self._send(401, {"error_type": "UNAUTHENTICATED"})
        for prefix, mode in ((f.redirect_prefix, "redirect"), (f.oversize_prefix, "oversize"), (f.malformed_prefix, "malformed"),
                             (f.slow_prefix, "slow"), (f.drop_prefix, "drop")):
            if prefix and path.startswith(prefix):
                if mode == "redirect":
                    return self._send(302, None, headers={"Location": "https://127.0.0.1:1/elsewhere"})
                if mode == "oversize":
                    return self._send(200, raw=b'["' + b"x" * (1024 * 1024 + 10) + b'"]')
                if mode == "malformed":
                    return self._send(200, raw=b"<html>not json")
                if mode == "slow":
                    import time
                    for _ in range(int(f.slow_seconds * 10)):
                        time.sleep(0.1)
                        try:
                            self.wfile.write(b"")
                        except OSError:
                            return
                if mode == "drop":
                    self.close_connection = True
                    return
        body = f.route(path, query)
        if body is None:
            return self._send(404, {"error_type": "NOT_FOUND"})
        return self._send(200, body)


class _Server(ThreadingHTTPServer):
    def handle_error(self, request, client_address):   # a client hanging up on purpose (size cap, deadline) is not an error here
        pass


class FakeVCenter:
    def __init__(self, inventory=None, user=USER, password=PASSWORD):
        self.inv = inventory or default_inventory()
        self.user, self.password = user, password
        self.requests, self.auth_headers = [], []
        self.logins = self.logouts = self.gets_since_login = 0
        self.fail_login = False
        self.redirect_prefix = self.oversize_prefix = self.malformed_prefix = self.slow_prefix = self.drop_prefix = ""
        self.slow_seconds = 5
        self.expire_after = 0
        self.cert_path, self.key_path, self.fingerprint = make_cert()
        self._server = self._thread = None
        self.host, self.port = "127.0.0.1", 0

    # ---- lifecycle
    def start(self):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.cert_path, self.key_path)
        self._server = _Server(("127.0.0.1", 0), _Handler)
        self._server.daemon_threads = True
        self._server.socket = ctx.wrap_socket(self._server.socket, server_side=True)
        self._server.fake = self
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *a):
        self.stop()

    @property
    def methods(self):
        return sorted({m for m, _p, _s in self.requests})

    # ---- the REST surface
    def route(self, path, q):
        inv = self.inv
        if path == "/api/appliance/system/version":
            return {"version": inv["version"], "product": "VMware vCenter Server", "build": "1"}
        if path == "/api/vcenter/host":
            refs = inv["hosts"].keys()
            if "clusters" in q:
                refs = [h for c in q["clusters"] for h in inv["clusters"].get(c, {}).get("hosts", [])]
            return [{"host": r, **{k: inv["hosts"][r][k] for k in ("name", "connection_state", "power_state")}} for r in refs if r in inv["hosts"]]
        if path == "/api/vcenter/cluster":
            return [{"cluster": r, "name": c["name"], "ha_enabled": c["ha_enabled"], "drs_enabled": c["drs_enabled"]} for r, c in inv["clusters"].items()]
        if path == "/api/vcenter/datastore":
            return [{"datastore": r, "name": d["name"], "type": d["type"], "free_space": d["free"], "capacity": d["capacity"]} for r, d in inv["datastores"].items()]
        if path.startswith("/api/vcenter/datastore/"):
            d = inv["datastores"].get(path.rsplit("/", 1)[1])
            return None if d is None else {"name": d["name"], "type": d["type"], "accessible": d["accessible"], "thin_provisioning_supported": d["thin"],
                                           "multiple_host_access": True, "free_space": d["free"], "capacity": d["capacity"]}
        if path == "/api/vcenter/vm":
            vms = inv["vms"]
            sel = list(vms)
            if "hosts" in q:
                sel = [r for r in sel if vms[r]["host"] in q["hosts"]]
            if "clusters" in q:
                sel = [r for r in sel if vms[r]["cluster"] in q["clusters"]]
            if "datastores" in q:
                sel = [r for r in sel if set(vms[r]["datastores"]) & set(q["datastores"])]
            return [{"vm": r, "name": vms[r]["name"], "power_state": vms[r]["power"], "cpu_count": vms[r]["cpu"], "memory_size_MiB": vms[r]["mem"]} for r in sel]
        if path.startswith("/api/vcenter/vm/"):
            parts = path[len("/api/vcenter/vm/"):].split("/")
            vm = inv["vms"].get(parts[0])
            if vm is None:
                return None
            rest = "/".join(parts[1:])
            if rest == "":
                return {"name": vm["name"], "guest_OS": "UBUNTU_64", "power_state": vm["power"],
                        "identity": {"name": vm["name"], "bios_uuid": vm["bios"], "instance_uuid": vm["bios"].replace("4237", "5037")},
                        "cpu": {"count": vm["cpu"], "cores_per_socket": 2}, "memory": {"size_MiB": vm["mem"]}, "disks": vm["disks"], "nics": vm["nics"]}
            if vm["power"] != "POWERED_ON":
                return None
            if rest == "tools":
                return {"run_state": vm["tools"], "version_number": 12352, "version": "12352", "version_status": "CURRENT", "upgrade_policy": "MANUAL"} if vm["tools"] else None
            if rest == "guest/identity":
                return {"name": vm["os"], "family": vm["family"], "full_name": {"default_message": vm["os"], "id": "x"},
                        "host_name": vm["hostname"], "ip_address": (vm["ips"] or [""])[0]} if vm["os"] else None
            if rest == "guest/networking/interfaces":
                nic = next(iter(vm["nics"].values()))
                return [{"mac_address": nic["mac_address"], "nic": "4000", "ip": {"ip_addresses": [{"ip_address": ip, "prefix_length": 24, "state": "PREFERRED"} for ip in vm["ips"]]}}]
        return None
