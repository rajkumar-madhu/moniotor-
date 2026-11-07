"""DNS name and record validation (no lookups: nothing here touches the network)."""
import ipaddress
import re

LABEL_RE = re.compile(r"^(?!-)[a-z0-9_-]{1,63}(?<!-)$")
REVERSE_V4 = "in-addr.arpa"
REVERSE_V6 = "ip6.arpa"
FORWARD_TYPES = {"A", "AAAA", "CNAME", "MX", "TXT", "SRV"}
REVERSE_TYPES = {"PTR", "CNAME", "TXT"}


class DNSError(ValueError):
    pass


def _labels_ok(name):
    return all(LABEL_RE.match(label) for label in name.split("."))


def hostname(text, field="name"):
    """A fully qualified host name, lowercase, without the trailing dot."""
    text = (text or "").strip().lower().rstrip(".")
    if not text:
        raise DNSError(f"Enter a {field}.")
    if len(text) > 253 or not _labels_ok(text):
        raise DNSError(f"“{text[:80]}” is not a valid DNS name.")
    if text.replace(".", "").isdigit():
        raise DNSError("A host name cannot be only digits; use an A or AAAA record for addresses.")
    return text


def reverse_zone_for(net):
    """in-addr.arpa / ip6.arpa zone for an octet- (IPv4) or nibble- (IPv6) aligned network."""
    if net.version == 4:
        if net.prefixlen not in (8, 16, 24):
            raise DNSError("IPv4 reverse zones must be /8, /16 or /24 (octet boundaries).")
        octets = str(net.network_address).split(".")[: net.prefixlen // 8]
        return ".".join(reversed(octets)) + "." + REVERSE_V4
    if net.prefixlen % 4 or net.prefixlen == 0:
        raise DNSError("IPv6 reverse zones must be on a nibble boundary (/4, /8 … /124).")
    nibbles = net.network_address.exploded.replace(":", "")[: net.prefixlen // 4]
    return ".".join(reversed(nibbles)) + "." + REVERSE_V6


def reverse_zone_network(name):
    """The network a reverse zone name covers (None when the name is not a valid reverse zone)."""
    if name.endswith("." + REVERSE_V4):
        parts = name[: -len(REVERSE_V4) - 1].split(".")
        if not 1 <= len(parts) <= 3 or not all(p.isdigit() and str(int(p)) == p and int(p) <= 255 for p in parts):
            return None
        octets = list(reversed(parts)) + ["0"] * (4 - len(parts))
        return ipaddress.IPv4Network((".".join(octets)) + f"/{8 * len(parts)}")
    if name.endswith("." + REVERSE_V6):
        parts = name[: -len(REVERSE_V6) - 1].split(".")
        if not 1 <= len(parts) <= 31 or not all(len(p) == 1 and p in "0123456789abcdef" for p in parts):
            return None
        hexs = "".join(reversed(parts)).ljust(32, "0")
        addr = ipaddress.IPv6Address(int(hexs, 16))
        return ipaddress.IPv6Network(f"{addr}/{4 * len(parts)}")
    return None


def zone_name(text, kind):
    """Normalised zone name. For reverse zones a network (10.1.2.0/24, 2001:db8::/32) is converted."""
    raw = (text or "").strip().lower().rstrip(".")
    if not raw:
        raise DNSError("Enter the zone name.")
    if kind == "reverse" and "/" in raw:
        try:
            net = ipaddress.ip_network(raw, strict=True)
        except ValueError:
            raise DNSError("Enter a reverse zone name (…in-addr.arpa / …ip6.arpa) or an aligned network such as 10.1.2.0/24.")
        return reverse_zone_for(net)
    if len(raw) > 253 or not _labels_ok(raw):
        raise DNSError(f"“{raw[:80]}” is not a valid zone name.")
    is_reverse = raw.endswith("." + REVERSE_V4) or raw.endswith("." + REVERSE_V6)
    if kind == "reverse":
        if not is_reverse or reverse_zone_network(raw) is None:
            raise DNSError("A reverse zone ends in in-addr.arpa (IPv4 octets) or ip6.arpa (IPv6 nibbles), "
                           "e.g. 2.1.10.in-addr.arpa – or enter the network, e.g. 10.1.2.0/24.")
    else:
        if raw.endswith(".arpa") or raw == "arpa":
            raise DNSError("Names under .arpa are reverse zones: choose the Reverse kind.")
        if raw.replace(".", "").isdigit():
            raise DNSError("A zone name cannot be only digits.")
    return raw


def record_name(text, zone):
    """Owner name relative to the zone: '@' for the apex; an FQDN ending in the zone with a trailing dot is
    made relative. A leading '*' label (wildcard) is allowed."""
    raw = (text or "").strip().lower()
    if raw in ("", "@"):
        return "@"
    if raw.endswith("."):
        fq = raw.rstrip(".")
        if fq == zone:
            return "@"
        if not fq.endswith("." + zone):
            raise DNSError(f"The name “{raw[:80]}” is not inside the zone {zone}.")
        raw = fq[: -len(zone) - 1]
    labels = raw.split(".")
    check = labels[1:] if labels[0] == "*" else labels
    if (check and not all(LABEL_RE.match(label) for label in check)) or len(raw) + len(zone) + 1 > 253:
        raise DNSError(f"“{raw[:80]}” is not a valid record name.")
    return raw


def _u16(text, what):
    if not text.isdigit() or int(text) > 65535:
        raise DNSError(f"{what} must be a number from 0 to 65535.")
    return int(text)


def record_value(rtype, text):
    """Validated, normalised RDATA for the record type."""
    raw = (text or "").strip()
    if not raw:
        raise DNSError("Enter the record value.")
    if any(ord(c) < 32 for c in raw):
        raise DNSError("The value cannot contain line breaks or control characters.")
    if rtype == "A":
        try:
            return str(ipaddress.IPv4Address(raw))
        except ValueError:
            raise DNSError("An A record needs an IPv4 address, e.g. 10.0.1.20.")
    if rtype == "AAAA":
        try:
            return str(ipaddress.IPv6Address(raw))
        except ValueError:
            raise DNSError("An AAAA record needs an IPv6 address, e.g. 2001:db8::20.")
    if rtype in ("CNAME", "PTR"):
        return hostname(raw, "target name")
    if rtype == "MX":
        parts = raw.split()
        if len(parts) != 2:
            raise DNSError("An MX value is “preference host”, e.g. 10 mail.example.com.")
        return f"{_u16(parts[0], 'MX preference')} {hostname(parts[1], 'mail host')}"
    if rtype == "SRV":
        parts = raw.split()
        if len(parts) != 4:
            raise DNSError("An SRV value is “priority weight port target”, e.g. 10 5 5060 sip.example.com.")
        prio, weight, port = (_u16(parts[0], "Priority"), _u16(parts[1], "Weight"), _u16(parts[2], "Port"))
        target = "." if parts[3] == "." else hostname(parts[3], "target")
        return f"{prio} {weight} {port} {target}"
    if rtype == "TXT":
        if len(raw) > 500:
            raise DNSError("TXT values are limited to 500 characters here.")
        return raw
    raise DNSError("Unknown record type.")


def ptr_address(name, zone):
    """The address a PTR owner name stands for, or None when the name is not a complete reverse name."""
    fq = zone if name == "@" else f"{name}.{zone}"
    if fq.endswith("." + REVERSE_V4):
        parts = fq[: -len(REVERSE_V4) - 1].split(".")
        if len(parts) == 4 and all(p.isdigit() and str(int(p)) == p and int(p) <= 255 for p in parts):
            return ipaddress.IPv4Address(".".join(reversed(parts)))
    elif fq.endswith("." + REVERSE_V6):
        parts = fq[: -len(REVERSE_V6) - 1].split(".")
        if len(parts) == 32 and all(len(p) == 1 and p in "0123456789abcdef" for p in parts):
            return ipaddress.IPv6Address(int("".join(reversed(parts)), 16))
    return None


def ptr_name(address, zone):
    """Relative PTR owner name for an address inside a reverse zone (None when it is outside)."""
    net = reverse_zone_network(zone)
    if net is None or address.version != net.version or address not in net:
        return None
    full = address.reverse_pointer
    return full[: -len(zone) - 1] if full.endswith("." + zone) else None
