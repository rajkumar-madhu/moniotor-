"""pyvmomi (optional) provider: snapshots, event history, creation date and notes. READ-ONLY.

Only property reads and ``EventManager.QueryEvents`` are used. No task method (power, reconfigure, snapshot create / revert /
remove, clone, migrate...) is called anywhere in this file; ``hypervisor/tests/test_provider.py`` lists the allowed attribute
names and fails when another call appears.

Honest limits: written against the public vSphere Web Services API from its documentation and NOT run against a real vCenter
(none was available). In ``pin`` mode every TLS handshake pyvmomi makes is compared with the pinned fingerprint before anything
is sent on it (``vsphere.pinned_context``), so a rebound or swapped server never receives the login. The host name is still
resolved a second time by pyvmomi: the SSRF check protects the first lookup only, and ``skip`` mode is unverified by definition.
"""
import ssl
from datetime import datetime, timezone as dt_tz

from . import parsers, vsphere
from .providers import ProviderError, clean_event, clean_snapshot

MAX_VMS_PER_RUN = 200
MAX_EVENTS_PER_VM = 30


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

    def __init__(self, host, port, username, password, tls_mode, pinned_sha256=""):
        try:
            from pyVim.connect import Disconnect, SmartConnect
            from pyVmomi import vim
        except ImportError:
            raise ProviderError("The 'pyvmomi' Python package is not installed. Install requirements-vsphere.txt.") from None
        self._vim, self._disconnect = vim, Disconnect
        self.host = host
        vsphere._resolve(host, port)                      # SSRF policy (pyvmomi resolves again: see the module docstring)
        if tls_mode == "pin":
            try:
                ctx = vsphere.pinned_context(pinned_sha256)       # checks the certificate of the connection that carries the login
            except vsphere.VSphereError:
                raise ProviderError("No certificate fingerprint is pinned yet. Test the connection and confirm it first.") from None
        else:
            ctx = ssl.create_default_context() if tls_mode == "verify" else ssl._create_unverified_context()
        try:
            self._si = SmartConnect(host=host, port=int(port), user=username, pwd=password, sslContext=ctx, connectionPoolTimeout=10)
        except Exception as exc:  # noqa: BLE001
            msg = f"{type(exc).__name__}"
            for secret in (password, username):
                msg = msg.replace(secret or "\0", "***")
            raise ProviderError(f"pyvmomi could not log in ({msg}).") from None

    def close(self):
        try:
            self._disconnect(self._si)
        except Exception:  # noqa: BLE001
            pass

    def fetch(self, morefs, since):
        vim = self._vim
        content = self._si.RetrieveContent()
        stub = self._si._stub
        out = {"snapshots": {}, "events": {}, "extras": {}}
        begin = since if getattr(since, "tzinfo", None) else datetime.now(dt_tz.utc)   # the sync always passes an aware time
        for ref in list(morefs)[:MAX_VMS_PER_RUN]:
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
