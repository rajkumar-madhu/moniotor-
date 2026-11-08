"""Walk one BMC's Redfish tree with GET requests only and hand the documents to ``hardware.parsers``.

``collect`` only needs an object with ``get(path) -> dict``, so the same code runs against the real client, the local fake
BMC of the tests and plain fixture dictionaries.

What is read each poll: service root, the first ComputerSystem, its Chassis (Thermal + Power), its Manager and the newest
entries of the BMC's logs. The slower-changing inventory (processors, DIMMs, storage controllers and drives, NICs, licence)
is read when ``inventory=True`` (the first poll and then every ``INVENTORY_EVERY`` seconds).

Failure policy: a login failure or an unreachable BMC at the service root aborts the poll. After that a section that cannot
be read is recorded as ``missing`` (404 / not in this BMC), or ``error`` and the rest is still collected, so one slow or
unsupported resource never hides everything else. If NOTHING could be read the last error is raised.
"""
from . import parsers, redfish

INVENTORY_EVERY = 1800          # seconds between inventory reads
ROOT = "/redfish/v1/"
LIMITS = {"controllers": 8, "drives": 64, "dimms": 64, "cpus": 8, "nics": 16, "log_services": 6, "logs_read": 3}
PREFERRED_LOGS = {"dell": ("sel", "lclog"), "hpe": ("iml", "iel"), "generic": ("sel", "iml", "log", "event")}


def link(doc, *keys):
    """The path of a link in a BMC document, or None. The result is only used if it passes ``redfish.valid_path``."""
    cur = doc
    for k in keys:
        cur = cur.get(k) if isinstance(cur, dict) else None
    if isinstance(cur, dict):
        cur = cur.get("@odata.id")
    return cur if isinstance(cur, str) and redfish.valid_path(cur) else None


def _first_link(doc, key):
    """First @odata.id of a list of links (Links.Chassis, Links.ManagedBy ...)."""
    items = doc.get("Links", {}).get(key) if isinstance(doc.get("Links"), dict) else None
    if isinstance(items, list):
        for it in items:
            p = it.get("@odata.id") if isinstance(it, dict) else None
            if isinstance(p, str) and redfish.valid_path(p):
                return p
    return None


class _Reader:
    def __init__(self, client):
        self.client, self.sections, self.last_error, self.ok_any = client, {}, None, False

    def get(self, section, path):
        """A document, or None when it could not be read (recorded in `sections`). Login errors always propagate."""
        if not path:
            self.sections.setdefault(section, "missing")
            return None
        try:
            doc = self.client.get(path)
        except redfish.AuthFailed:
            raise
        except redfish.NotFound:
            self.sections.setdefault(section, "missing")
            return None
        except redfish.RedfishError as exc:
            self.last_error = exc
            self.sections[section] = "error"
            return None
        self.ok_any = True
        self.sections[section] = "ok"
        return doc

    def members(self, section, path, limit):
        """Documents of the members of a collection (at most `limit`). Links in the collection are validated one by one."""
        coll = self.get(section, path)
        if coll is None:
            return None
        out = []
        raw = coll.get("Members")
        for m in (raw if isinstance(raw, list) else [])[:limit]:
            p = m.get("@odata.id") if isinstance(m, dict) else None
            if isinstance(p, str) and redfish.valid_path(p):
                doc = self._sub(p)
                if doc is not None:
                    out.append(doc)
        return out

    def _sub(self, path):
        try:
            doc = self.client.get(path)
        except redfish.AuthFailed:
            raise
        except redfish.RedfishError as exc:
            if not isinstance(exc, redfish.NotFound):
                self.last_error = exc
            return None
        self.ok_any = True
        return doc


def collect(client, vendor_hint="", inventory=True, log_limit=50):
    """Read the BMC. Returns the dict that ``parsers.parse`` takes. Raises RedfishError when nothing could be read."""
    root = client.get(ROOT)                                     # a login or connection problem stops here, loudly
    rd = _Reader(client)
    rd.ok_any = True
    c = {"root": root, "sections": rd.sections, "inventory": bool(inventory)}

    system = None
    for doc in (rd.members("system", link(root, "Systems") or "/redfish/v1/Systems", 4) or [])[:1]:
        system = doc
    c["system"] = system

    chassis = None
    chassis_path = _first_link(system, "Chassis") if system else None
    if chassis_path:
        chassis = rd.get("chassis", chassis_path)
    else:
        for doc in (rd.members("chassis", link(root, "Chassis") or "/redfish/v1/Chassis", 4) or [])[:1]:
            chassis = doc
    c["chassis"] = chassis

    manager = None
    mgr_path = _first_link(system, "ManagedBy") if system else None
    if mgr_path:
        manager = rd.get("manager", mgr_path)
    else:
        for doc in (rd.members("manager", link(root, "Managers") or "/redfish/v1/Managers", 4) or [])[:1]:
            manager = doc
    c["manager"] = manager

    vendor = vendor_hint if vendor_hint in ("dell", "hpe", "generic") and vendor_hint else parsers.detect_vendor(root, system, manager)
    c["vendor"] = vendor

    c["thermal"] = rd.get("thermal", link(chassis, "Thermal") if chassis else None)
    c["power"] = rd.get("power", link(chassis, "Power") if chassis else None)

    if inventory and system:
        c["processors"] = rd.members("processors", link(system, "Processors"), LIMITS["cpus"])
        c["memory"] = rd.members("memory", link(system, "Memory"), LIMITS["dimms"])
        c["nics"] = rd.members("nics", link(system, "EthernetInterfaces"), LIMITS["nics"])
        c["storage"] = _storage(rd, link(system, "Storage"))
        if vendor == "dell":
            lic = rd.members("license", "/redfish/v1/LicenseService/Licenses", 1)
            c["license"] = lic[0] if lic else None
    else:
        c["processors"] = c["memory"] = c["nics"] = c["storage"] = None
        for s in ("processors", "memory", "nics", "storage"):
            rd.sections.setdefault(s, "skipped")

    c["logs"] = _logs(rd, vendor, system, manager, log_limit)
    if not rd.ok_any and rd.last_error is not None:
        raise rd.last_error
    return c


def _storage(rd, path):
    ctrls = rd.members("storage", path, LIMITS["controllers"])
    if ctrls is None:
        return None
    out, budget = [], LIMITS["drives"]
    for ctrl in ctrls:
        drives = []
        for d in (ctrl.get("Drives") if isinstance(ctrl.get("Drives"), list) else [])[: max(budget, 0)]:
            p = d.get("@odata.id") if isinstance(d, dict) else None
            if isinstance(p, str) and redfish.valid_path(p):
                doc = rd._sub(p)
                if doc is not None:
                    drives.append(doc)
                    budget -= 1
        out.append({"ctrl": ctrl, "drives": drives})
    return out


def _logs(rd, vendor, system, manager, limit):
    """Newest entries of the BMC logs that matter: Dell SEL + lifecycle log, HPE IML (+ IEL)."""
    found = []
    for owner in (manager, system):
        path = link(owner, "LogServices") if owner else None
        for svc in rd.members("logs", path, LIMITS["log_services"]) or []:
            found.append(svc)
    want = PREFERRED_LOGS.get(vendor, PREFERRED_LOGS["generic"])
    chosen = []
    for key in want:
        for svc in found:
            ident = parsers.text(svc.get("Id") or svc.get("Name"), 40).lower()
            if (ident == key or key in ident) and svc not in chosen:
                chosen.append(svc)
                break
    out = []
    rd.sections["logs"] = "ok" if chosen else "missing"
    for svc in chosen[: LIMITS["logs_read"]]:
        entries = link(svc, "Entries")
        if not entries:
            continue
        docs = _newest(rd, entries, limit)
        out.append({"id": parsers.text(svc.get("Id") or svc.get("Name"), 40).lower(), "entries": docs})
    return out


def _newest(rd, entries_path, limit):
    """The newest `limit` entries. Entries are listed oldest first by most BMCs, so when the collection holds more than one
    page we ask for the tail with $skip/$top (a BMC that ignores them just repeats the first page: harmless, entries are
    de-duplicated by id)."""
    first = rd.get("logentries", f"{entries_path.rstrip('/')}?$top={limit}")
    if first is None:
        return []
    docs = [e for e in (first.get("Members") if isinstance(first.get("Members"), list) else []) if isinstance(e, dict)]
    total = parsers.num(first.get("Members@odata.count"), 0, 1e7)
    if total and total > len(docs) and total > limit:
        tail = rd.get("logentries", f"{entries_path.rstrip('/')}?$skip={int(total - limit)}&$top={limit}")
        if tail is not None and isinstance(tail.get("Members"), list):
            docs += [e for e in tail["Members"] if isinstance(e, dict)]
    return docs[-limit * 2:]
