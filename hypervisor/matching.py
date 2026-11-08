"""Map a vCenter VM (or host) onto an existing network.Device. Pure functions: no Django, no network.

The rule is "explicit, auditable, never silently merge":

1. A hardware identifier is authoritative: the VM's BIOS or instance UUID equal to the device's serial number, or listed in a
   ``uuid:<value>`` tag.
2. Otherwise the name and the addresses are evidence: the VM name equal to the device name (case-insensitive, also the guest's
   host name), or one of the guest's IP addresses equal to the device's management IP. The devices found by name and by address
   are united. Exactly one device -> matched. More than one (a name that fits one device and an address that fits another, or
   two devices with the same address) -> ambiguous. None -> unmatched.
3. A device that is the candidate of more than one VM in the same pass is never given to any of them (``resolve_claims``): both
   are reported ambiguous, so the result does not depend on the order the VMs arrived in.

The result carries a plain-language reason and the candidate ids, so the unmatched page can show *why*.
"""
from collections import defaultdict

MATCHED, AMBIGUOUS, UNMATCHED = "matched", "ambiguous", "unmatched"


def _norm(value):
    return (value or "").strip().lower()


def _short(value):
    return _norm(value).split(".")[0]


class DeviceIndex:
    """Lookup tables over the candidate devices. ``devices``: iterable of dicts with id, name, mgmt_ip, serial, tags (list)."""

    def __init__(self, devices):
        self.by_name, self.by_ip, self.by_uuid = defaultdict(set), defaultdict(set), defaultdict(set)
        self.devices = {}
        for d in devices:
            self.devices[d["id"]] = d
            self.by_name[_norm(d["name"])].add(d["id"])
            self.by_ip[_norm(d.get("mgmt_ip"))].add(d["id"])
            if _norm(d.get("serial")):
                self.by_uuid[_norm(d["serial"])].add(d["id"])
            for tag in d.get("tags") or []:
                key, _, val = tag.partition(":")
                if key.strip().lower() == "uuid" and val.strip():
                    self.by_uuid[_norm(val)].add(d["id"])


def match_vm(vm, index, host_names=False):
    """-> (state, device_id or None, reason, sorted candidate ids). ``vm``: dict with name, bios_uuid, instance_uuid,
    guest_hostname, guest_ips."""
    uuids = {u for u in (_norm(vm.get("bios_uuid")), _norm(vm.get("instance_uuid"))) if u}
    hits = set().union(*(index.by_uuid.get(u, set()) for u in uuids)) if uuids else set()
    if len(hits) == 1:
        return MATCHED, next(iter(hits)), "UUID equals the device's serial number or uuid tag", sorted(hits)
    if len(hits) > 1:
        return AMBIGUOUS, None, "the UUID is listed on several devices", sorted(hits)

    by_name, by_ip = set(), set()
    names = [_norm(vm.get("name"))]
    if vm.get("guest_hostname"):
        names += [_norm(vm["guest_hostname"]), _short(vm["guest_hostname"])]
    for n in names:
        if n:
            by_name |= index.by_name.get(n, set())
    for ip in vm.get("guest_ips") or []:
        by_ip |= index.by_ip.get(_norm(ip), set())
    union = by_name | by_ip
    if not union:
        return UNMATCHED, None, "no device has this name, UUID or IP address", []
    if len(union) == 1:
        (dev,) = union
        why = " and ".join(w for w, ok in (("name", dev in by_name), ("IP address", dev in by_ip)) if ok)
        return MATCHED, dev, f"{why} equal the device's", [dev]
    parts = []
    if by_name:
        parts.append(f"the name fits {len(by_name)} device{'s' if len(by_name) != 1 else ''}")
    if by_ip:
        parts.append(f"the IP address fits {len(by_ip)} device{'s' if len(by_ip) != 1 else ''}")
    return AMBIGUOUS, None, "; ".join(parts) + ": more than one device could be this VM", sorted(union)


def match_host(host, index):
    """A vCenter host (ESXi) onto a physical server: its name (the FQDN or its first label) or an IP address used as its name."""
    full, short = _norm(host.get("name")), _short(host.get("name"))
    found = index.by_name.get(full, set()) | index.by_name.get(short, set()) | index.by_ip.get(full, set())
    if len(found) == 1:
        return MATCHED, next(iter(found)), "host name equals the device's name or management IP", sorted(found)
    if len(found) > 1:
        return AMBIGUOUS, None, "the host name fits several devices", sorted(found)
    return UNMATCHED, None, "no physical server has this name or address", []


def _strong(res):
    return res[0] == MATCHED and res[2].startswith("UUID")


def resolve_claims(results):
    """``results``: {key: (state, device_id, reason, candidates)}. A device claimed (matched or candidate) by more than one key
    is withdrawn from all of them: those keys become ambiguous. The one exception is a UUID match, which is authoritative: it keeps
    the device when no other key matched it by UUID too. Returns the corrected dict."""
    claims = defaultdict(set)
    for key, (state, dev, _r, cands) in results.items():
        for c in ([dev] if state == MATCHED and dev is not None else cands):
            claims[c].add(key)
    out = {}
    for key, res in results.items():
        state, dev, reason, cands = res
        if state == MATCHED and dev is not None and len(claims[dev]) > 1:
            rivals = [k for k in claims[dev] if k != key and _strong(results[k])]
            if _strong(res) and not rivals:
                out[key] = res
            else:
                out[key] = (AMBIGUOUS, None, "another VM or host in vCenter fits the same device", [dev])
        else:
            out[key] = res
    return out
