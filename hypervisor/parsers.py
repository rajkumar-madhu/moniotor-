"""Turn the vSphere Automation REST JSON into clean, bounded dicts. Pure functions: no Django, no network.

Everything in vCenter's answer is untrusted text (VM names, notes, guest host names, messages): it is length-capped, stripped of
control and formatting characters (so a name cannot carry a newline, an ANSI escape or a right-to-left override into a table or
a CSV) and never used as a URL, host or path. Unknown fields are ignored; a field that is missing or the wrong type is reported
as ``None`` / empty ("not reported"), never as a made-up zero.
"""
import ipaddress
import re
import unicodedata

MOREF_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,40}$")
MAX_NICS = 20
MAX_DISKS = 60
MAX_IPS = 24
MAX_INT = 2 ** 62
UUID_RE = re.compile(r"^[0-9A-Fa-f\-]{8,64}$")

POWER = {"POWERED_ON": "on", "POWERED_OFF": "off", "SUSPENDED": "suspended"}


def clean_text(value, limit=200):
    """A printable single-line string of at most ``limit`` characters ('' for anything that is not text or a number)."""
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str):
        return ""
    out = []
    for ch in value[: limit * 4]:
        cat = unicodedata.category(ch)
        if cat in ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"):
            out.append(" " if ch in "\t\r\n\x0b\x0c" else "")
        else:
            out.append(ch)
    text = re.sub(r" {2,}", " ", "".join(out)).strip()
    return text[:limit]


def to_int(value, lo=0, hi=MAX_INT):
    """An int inside [lo, hi] or None (bool, text, NaN and out-of-range numbers are not numbers here)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None
        value = int(value)
    if not isinstance(value, int):
        return None
    return value if lo <= value <= hi else None


def moref(value):
    value = value if isinstance(value, str) else ""
    return value if MOREF_RE.fullmatch(value) else ""


def _dict(value):
    return value if isinstance(value, dict) else {}


def _name_of(value):
    """vSphere sends localisable strings either as text or as {"default_message": ..., "localized": ...}."""
    if isinstance(value, dict):
        value = value.get("default_message") or value.get("localized") or ""
    return clean_text(value, 200)


def power(value):
    return POWER.get(value if isinstance(value, str) else "", "unknown")


def ip_text(value):
    """A normalised IP address string, or '' for anything that is not an IP address."""
    if not isinstance(value, str):
        return ""
    try:
        return str(ipaddress.ip_address(value.strip().split("%")[0]))
    except ValueError:
        return ""


def usable_ip(ip):
    """False for link-local, loopback and unspecified addresses: not useful to match a VM on."""
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (a.is_link_local or a.is_loopback or a.is_unspecified or a.is_multicast)


def parse_host(raw):
    raw = _dict(raw)
    ref = moref(raw.get("host"))
    if not ref:
        return None
    return {"moref": ref, "name": clean_text(raw.get("name"), 255) or ref,
            "connection_state": clean_text(raw.get("connection_state"), 20),
            "power_state": clean_text(raw.get("power_state"), 20)}


def parse_cluster(raw):
    raw = _dict(raw)
    ref = moref(raw.get("cluster"))
    if not ref:
        return None
    return {"moref": ref, "name": clean_text(raw.get("name"), 120) or ref,
            "ha_enabled": raw.get("ha_enabled") if isinstance(raw.get("ha_enabled"), bool) else None,
            "drs_enabled": raw.get("drs_enabled") if isinstance(raw.get("drs_enabled"), bool) else None}


def parse_datastore(raw, detail=None):
    raw, detail = _dict(raw), _dict(detail)
    ref = moref(raw.get("datastore"))
    if not ref:
        return None
    cap, free = to_int(raw.get("capacity")), to_int(raw.get("free_space"))
    if cap is not None and free is not None and free > cap:
        free = cap
    return {"moref": ref, "name": clean_text(raw.get("name"), 255) or ref, "ds_type": clean_text(raw.get("type"), 20),
            "capacity_bytes": cap, "free_bytes": free,
            "accessible": detail.get("accessible") if isinstance(detail.get("accessible"), bool) else None,
            "thin_provisioning": (detail.get("thin_provisioning_supported")
                                  if isinstance(detail.get("thin_provisioning_supported"), bool) else None)}


def parse_vm_summary(raw):
    raw = _dict(raw)
    ref = moref(raw.get("vm"))
    if not ref:
        return None
    return {"moref": ref, "name": clean_text(raw.get("name"), 255) or ref, "power_state": power(raw.get("power_state")),
            "cpu_count": to_int(raw.get("cpu_count"), 1, 4096), "memory_mib": to_int(raw.get("memory_size_MiB"), 1, 2 ** 31)}


def _disk_datastore(vmdk):
    m = re.match(r"^\[([^\]]{1,120})\]\s*(.*)$", vmdk or "")
    return (clean_text(m.group(1), 120), clean_text(m.group(2), 200)) if m else ("", clean_text(vmdk, 200))


def parse_vm_detail(raw):
    """The fields of GET /api/vcenter/vm/{vm} LinkedEye uses (hardware, identity, guest OS code, disks, nics)."""
    raw = _dict(raw)
    identity = _dict(raw.get("identity"))
    cpu, mem = _dict(raw.get("cpu")), _dict(raw.get("memory"))
    disks = []
    for key, d in list(_dict(raw.get("disks")).items())[:MAX_DISKS]:
        d = _dict(d)
        backing = _dict(d.get("backing"))
        ds, fname = _disk_datastore(backing.get("vmdk_file") if isinstance(backing.get("vmdk_file"), str) else "")
        disks.append({"id": clean_text(key, 20), "label": clean_text(d.get("label"), 80), "type": clean_text(d.get("type"), 20),
                      "capacity_bytes": to_int(d.get("capacity")), "datastore": ds, "file": fname})
    nics = []
    for key, n in list(_dict(raw.get("nics")).items())[:MAX_NICS]:
        n = _dict(n)
        backing = _dict(n.get("backing"))
        mac = clean_text(n.get("mac_address"), 17).lower()
        nics.append({"id": clean_text(key, 20), "label": clean_text(n.get("label"), 80), "type": clean_text(n.get("type"), 20),
                     "mac": mac if re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", mac) else "",
                     "state": clean_text(n.get("state"), 20), "network": clean_text(backing.get("network_name"), 120),
                     "backing": clean_text(backing.get("type"), 30), "ips": []})
    uuid = lambda v: clean_text(v, 64) if isinstance(v, str) and UUID_RE.fullmatch(v) else ""  # noqa: E731
    return {"name": clean_text(raw.get("name") or identity.get("name"), 255),
            "bios_uuid": uuid(identity.get("bios_uuid")).lower(), "instance_uuid": uuid(identity.get("instance_uuid")).lower(),
            "power_state": power(raw.get("power_state")), "guest_id": clean_text(raw.get("guest_OS"), 60),
            "cpu_count": to_int(cpu.get("count"), 1, 4096), "cores_per_socket": to_int(cpu.get("cores_per_socket"), 1, 4096),
            "memory_mib": to_int(mem.get("size_MiB"), 1, 2 ** 31), "disks": disks, "nics": nics}


def parse_tools(raw):
    raw = _dict(raw)
    return {"run_state": clean_text(raw.get("run_state"), 20), "version_status": clean_text(raw.get("version_status"), 30),
            "version": clean_text(raw.get("version") if raw.get("version") is not None else raw.get("version_number"), 40)}


def parse_guest_identity(raw):
    raw = _dict(raw)
    return {"full_name": _name_of(raw.get("full_name")) or _name_of(raw.get("name")),
            "family": clean_text(raw.get("family"), 20), "host_name": clean_text(raw.get("host_name"), 255),
            "ip": ip_text(raw.get("ip_address"))}


def parse_guest_interfaces(raw):
    """[(mac, [ips])] from GET .../guest/networking/interfaces (needs running VMware Tools)."""
    out = []
    for item in (raw if isinstance(raw, list) else [])[:MAX_NICS]:
        item = _dict(item)
        mac = clean_text(item.get("mac_address"), 17).lower()
        ips = []
        for a in _dict(item.get("ip")).get("ip_addresses") or []:
            ip = ip_text(_dict(a).get("ip_address"))
            if ip and usable_ip(ip) and ip not in ips:
                ips.append(ip)
        out.append((mac, ips[:MAX_IPS]))
    return out


def merge_guest(detail, tools, identity, interfaces):
    """One VM record from the per-VM answers; any of ``tools`` / ``identity`` / ``interfaces`` may be None (not reported)."""
    out = dict(detail)
    out["tools"] = tools
    ips = []
    for mac, addrs in interfaces or []:
        for nic in out["nics"]:
            if mac and nic["mac"] == mac:
                nic["ips"] = addrs
        for ip in addrs:
            if ip not in ips:
                ips.append(ip)
    if identity and identity.get("ip") and usable_ip(identity["ip"]) and identity["ip"] not in ips:
        ips.append(identity["ip"])
    out["guest_ips"] = ips[:MAX_IPS]
    out["guest_full_name"] = (identity or {}).get("full_name", "")
    out["guest_family"] = (identity or {}).get("family", "")
    out["guest_hostname"] = (identity or {}).get("host_name", "")
    return out
