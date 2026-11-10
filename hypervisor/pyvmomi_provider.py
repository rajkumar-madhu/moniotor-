"""pyvmomi (optional) provider: snapshots, event history, creation date and notes. READ-ONLY.

Only property reads and ``EventManager.QueryEvents`` are used. No task method (power, reconfigure, snapshot create / revert /
remove, clone, migrate...) is called anywhere in this file; ``hypervisor/tests/test_provider.py`` lists the allowed attribute
names and fails when another call appears.

Network safety: pyvmomi opens its own connections and resolves the host name itself, so every socket it opens goes through
``vsphere.guarded_context``: the address the socket actually reached is checked against the SSRF policy before the TLS handshake
(a rebound name or a second, private record never receives a byte), and in ``pin`` mode the certificate is compared with the
pinned fingerprint on that same connection before anything is sent. Every socket read has a timeout (``SOCKET_TIMEOUT``) and the
whole run (login, reads, logout) has a deadline (``RUN_DEADLINE``): a watchdog shuts the sockets down when it passes and ``fetch``
returns what it read so far, so a stalled vCenter cannot hold the job scheduler.

Honest limits: written against the public vSphere Web Services API from its documentation and NOT run against a real vCenter
(none was available). ``skip`` mode is unverified by definition.
"""
import socket
import time
from datetime import datetime, timezone as dt_tz

from . import parsers, vsphere
from .providers import ProviderError, clean_event, clean_snapshot

MAX_VMS_PER_RUN = 200
MAX_EVENTS_PER_VM = 30
SOCKET_TIMEOUT = 15          # seconds per connect / read (pyvmomi's own default is no timeout at all)
RUN_DEADLINE = 60            # seconds for the whole provider run: login, every read, logout


def flatten_snapshots(tree, current=None):
    """Snapshot dicts from a pyvmomi ``rootSnapshotList`` (duck-typed: id, name, description, createTime, snapshot,
    childSnapshotList). ``current`` is the moref value of ``vm.snapshot.currentSnapshot``."""
    out, stack = [], list(tree or [])
    while stack and len(out) < 200:
        node = stack.pop(0)
        ref = getattr(node, "snapshot", None)
        ref_value = getattr(ref, "_moId", None) or (str(ref) if ref is not None else "")
        out.append({"snap_id": str(getattr(node, "id", "") or ref_value), "name": getattr(node, "name", ""),
                    "description": getattr(node, "description", ""), "created_at": getattr(node, "createTime", None),
                    "size_bytes": None, "is_current": bool(current and ref_value and ref_value == current),
                    "_ref": ref_value})
        stack.extend(getattr(node, "childSnapshotList", None) or [])
    return out


def snapshot_sizes(layout_ex):
    """{snapshot moref: bytes} from ``vm.layoutEx`` (duck-typed: file[key,size], snapshot[key,dataKey,memoryKey,disk[chain[fileKey]]])."""
    sizes = {f.key: int(getattr(f, "size", 0) or 0) for f in (getattr(layout_ex, "file", None) or [])}
    out = {}
    for s in getattr(layout_ex, "snapshot", None) or []:
        keys = {getattr(s, "dataKey", -1), getattr(s, "memoryKey", -1)}
        for disk in getattr(s, "disk", None) or []:
            for chain in getattr(disk, "chain", None) or []:
                keys.update(getattr(chain, "fileKey", None) or [])
        ref = getattr(getattr(s, "key", None), "_moId", None) or str(getattr(s, "key", ""))
        out[ref] = sum(sizes.get(k, 0) for k in keys if k in sizes)
    return out


def event_severity(event):
    name = type(event).__name__
    if any(w in name for w in ("Error", "Failed", "Failure")):
        return "error"
    if any(w in name for w in ("Warning", "Alarm", "Insufficient", "Lost")):
        return "warning"
    return "info"


class PyvmomiProvider:
    name = "pyvmomi"

    def __init__(self, host, port, username, password, tls_mode, pinned_sha256="", deadline=RUN_DEADLINE):
        try:
            from pyVim.connect import Disconnect, SmartConnect
            from pyVmomi import vim
        except ImportError:
            raise ProviderError("The 'pyvmomi' Python package is not installed. Install requirements-vsphere.txt.") from None
        self._vim, self._disconnect = vim, Disconnect
        self.host = host
        self._si = None
        ip = vsphere._resolve(host, port)                 # SSRF policy up front (clear error); each socket is checked again below
        # pyvmomi's first request (the API version probe) opens its own connection with no timeout, before the guarded context
        # can register the socket, so a vCenter that stopped accepting connections would hold the login for the kernel's SYN
        # retries (~2 min). A bounded connect to the checked address first turns that into a quick, clear failure.
        try:
            socket.create_connection((ip, int(port)), timeout=min(SOCKET_TIMEOUT, max(1, deadline))).close()
        except OSError as exc:
            raise ProviderError(f"vCenter did not accept a connection ({type(exc).__name__}).") from None
        try:
            ctx = vsphere.guarded_context(tls_mode if tls_mode in ("verify", "pin", "skip") else "verify", pinned_sha256)
        except vsphere.VSphereError:
            raise ProviderError("No certificate fingerprint is pinned yet. Test the connection and confirm it first.") from None
        self._ctx = ctx
        self._deadline = time.monotonic() + deadline
        self._timer = vsphere.shut_after(ctx, deadline)                  # unblocks a read that a stalled server holds open
        try:
            self._si = SmartConnect(host=host, port=int(port), user=username, pwd=password, sslContext=ctx,
                                    httpConnectionTimeout=SOCKET_TIMEOUT, connectionPoolTimeout=10)
        except Exception as exc:  # noqa: BLE001
            self._timer.cancel()
            ctx.shut()
            msg = "the run deadline passed" if self.expired else f"{type(exc).__name__}"
            for secret in (password, username):
                msg = msg.replace(secret or "\0", "***")
            raise ProviderError(f"pyvmomi could not log in ({msg}).") from None

    @property
    def expired(self):
        return time.monotonic() >= self._deadline

    def close(self):
        try:
            if self._si is not None and not self.expired:
                self._disconnect(self._si)
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._timer.cancel()
            self._ctx.shut()

    def fetch(self, morefs, since):
        vim = self._vim
        content = self._si.RetrieveContent()
        stub = self._si._stub
        out = {"snapshots": {}, "events": {}, "extras": {}}
        begin = since if getattr(since, "tzinfo", None) else datetime.now(dt_tz.utc)   # the sync always passes an aware time
        for ref in list(morefs)[:MAX_VMS_PER_RUN]:
            if self.expired:                              # partial answer: the rest are "not reported" and come first next run
                break
            vm = vim.VirtualMachine(ref, stub)
            try:
                snap = vm.snapshot
                cfg = vm.config
                tree = flatten_snapshots(snap.rootSnapshotList if snap else [], getattr(getattr(snap, "currentSnapshot", None), "_moId", None))
                sizes = snapshot_sizes(vm.layoutEx) if tree else {}
                for item in tree:
                    item["size_bytes"] = sizes.get(item.pop("_ref"))
                out["snapshots"][ref] = [s for s in map(clean_snapshot, tree) if s]
                out["extras"][ref] = {"created_on": getattr(cfg, "createDate", None) if cfg else None,
                                      "description": parsers.clean_text(getattr(cfg, "annotation", "") if cfg else "", 500)}
                spec = vim.event.EventFilterSpec(
                    entity=vim.event.EventFilterSpec.ByEntity(entity=vm, recursion="self"),
                    time=vim.event.EventFilterSpec.ByTime(beginTime=begin), maxCount=MAX_EVENTS_PER_VM)
                events = []
                for ev in content.eventManager.QueryEvents(spec) or []:
                    events.append({"key": f"{getattr(ev, 'key', '')}", "ts": getattr(ev, "createdTime", None),
                                   "severity": event_severity(ev), "kind": type(ev).__name__,
                                   "message": getattr(ev, "fullFormattedMessage", "")})
                out["events"][ref] = [e for e in map(clean_event, events) if e]
            except Exception:  # noqa: BLE001 - one unreadable VM must not stop the others
                continue
        return out
