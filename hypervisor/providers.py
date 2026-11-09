"""Optional data that the stable vSphere REST API does not offer: snapshots, event history, creation date and notes.

``Provider`` is the small interface the sync uses; ``PyvmomiProvider`` (``hypervisor.pyvmomi_provider``) implements it with the
optional ``pyvmomi`` package (``requirements-vsphere.txt``), imported lazily and never required: without it everything else
works and the pages say "not reported / install requirements-vsphere.txt". Tests use ``FakeProvider``.

A provider is READ-ONLY by contract: ``fetch`` returns data and changes nothing in vCenter.
"""
from datetime import timezone as dt_tz

from . import parsers

INSTALL_HINT = "The 'pyvmomi' Python package is not installed. Install requirements-vsphere.txt and restart LinkedEye."


class ProviderError(Exception):
    """The provider could not run; the message is shown to the operator and never contains a secret."""


class Provider:
    name = "none"

    def fetch(self, morefs, since):
        """-> {"snapshots": {moref: [snapshot dict]}, "events": {moref: [event dict]}, "extras": {moref: {...}}}.

        Only the VMs it could read appear in the answer (a VM missing from "snapshots" means "not reported", an empty list means
        "no snapshots"). Snapshot dict: snap_id, name, description, created_at (aware datetime), size_bytes|None, is_current.
        Event dict: key, ts (aware datetime), severity (info|warning|error), kind, message. Extras: created_on, description."""
        raise NotImplementedError

    def close(self):
        pass


def pyvmomi_available():
    try:
        import pyVmomi  # noqa: F401
        import pyVim  # noqa: F401
    except ImportError:
        return False
    return True


def clean_snapshot(raw):
    """A validated snapshot dict from a provider (None when it has no usable id or time)."""
    snap_id = parsers.clean_text(raw.get("snap_id"), 60)
    created = raw.get("created_at")
    if not snap_id or created is None or not hasattr(created, "astimezone"):
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=dt_tz.utc)
    size = parsers.to_int(raw.get("size_bytes"))
    return {"snap_id": snap_id, "name": parsers.clean_text(raw.get("name"), 200) or snap_id,
            "description": parsers.clean_text(raw.get("description"), 300), "created_at": created,
            "size_bytes": size, "is_current": bool(raw.get("is_current"))}


def clean_event(raw):
    key = parsers.clean_text(raw.get("key"), 80)
    ts = raw.get("ts")
    if not key or ts is None or not hasattr(ts, "astimezone"):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt_tz.utc)
    sev = raw.get("severity") if raw.get("severity") in ("info", "warning", "error") else "info"
    message = parsers.clean_text(raw.get("message"), 400)
    if not message:
        return None
    return {"key": key, "ts": ts, "severity": sev, "kind": parsers.clean_text(raw.get("kind"), 80), "message": message}


def get_provider(source, password_reader):
    """The provider a source asks for, or None. ``password_reader()`` -> (username, password), called only when needed.
    Raises ProviderError with the install hint when pyvmomi is wanted but missing. Tests replace this function."""
    if not source.use_provider:
        return None
    if not pyvmomi_available():
        raise ProviderError(INSTALL_HINT)
    from .pyvmomi_provider import PyvmomiProvider
    user, password = password_reader()
    return PyvmomiProvider(source.host, source.port, user, password, source.tls_mode, source.pinned_sha256)
