"""IPAM read/write helpers: scoping, address arithmetic, subnet placement, utilisation and the address map.

Public functions other modules may call (import lazily):
  subnet_for(address, vrf="")  -> Subnet | None   most specific subnet holding an address
  address_record(address, vrf="") -> IPAddress | None
"""
import ipaddress

from django.apps import apps
from django.conf import settings
from django.db.models import Count, Q

from core.access import PERMS, RANK, can, scope_by_site, visible_sites

from .models import GLOBAL_SITE, DNSRecord, DNSZone, IPAddress, Role, Subnet, VLAN, ip_key, key_int

MAP_MAX_PREFIX_V4 = 22          # address-per-cell map for IPv4 /22 … /32 (at most 1024 cells)
BLOCK_MAP_MIN_PREFIX_V4 = 16    # /16 … /21 get a map of /24 blocks (at most 256 cells); bigger: no map


def tenant():
    return settings.LINKEDEYE_TENANT


# ------------------------------------------------------------------ access
def _tenant_wide(user, perm):
    from core.access import _memberships
    if user.is_superuser:
        return True
    ms = _memberships(user)
    if not ms:  # users without memberships hold their default role everywhere
        return True
    return any(m.site is None and RANK[m.role] >= PERMS[perm] for m in ms)


def can_manage(user, perm, site):
    """`perm` on a record's site. Global records ("All Sites" or blank) need the permission tenant-wide."""
    if site and site != GLOBAL_SITE:
        return can(user, perm, site_name=site)
    return can(user, perm) and _tenant_wide(user, perm)


def operate_sites(user):
    """None when the user may operate everywhere, else the set of site names they may operate on."""
    if can_manage(user, "operate", GLOBAL_SITE):
        return None
    return {s for s in (visible_sites(user) or set()) if can(user, "operate", site_name=s)}


def site_filter(qs, site, field="site"):
    """The page-level ?site= filter (top-bar site picker): that site plus global records."""
    if site:
        return qs.filter(**{f"{field}__in": [site, GLOBAL_SITE, ""]})
    return qs


def vlans_for(user):
    return scope_by_site(VLAN.objects.filter(tenant_key=tenant()), user)


def subnets_for(user):
    return scope_by_site(Subnet.objects.filter(tenant_key=tenant()), user)


def addresses_for(user):
    return scope_by_site(IPAddress.objects.filter(tenant_key=tenant()), user)


def zones_for(user):
    return scope_by_site(DNSZone.objects.filter(tenant_key=tenant()), user)


def records_for(user):
    return scope_by_site(DNSRecord.objects.filter(tenant_key=tenant()), user, field="zone__site")


def _model(app_label, name):
    try:
        return apps.get_model(app_label, name)
    except LookupError:
        return None


def devices_for(user):
    Device = _model("network", "Device")
    if Device is None:
        return None
    return scope_by_site(Device.objects.filter(tenant_key=tenant()), user)


def cis_for(user):
    CI = _model("cmdb", "ConfigurationItem")
    if CI is None:
        return None
    return scope_by_site(CI.objects.filter(tenant_key=tenant()), user)


def site_names(user):
    """Sites the user can pick: core.Site rows they can see plus sites already used by IPAM records."""
    from core.models import Site
    t = tenant()
    names = set(Site.objects.filter(tenant_key=t).values_list("name", flat=True))
    for model in (Subnet, VLAN, DNSZone):
        names |= set(model.objects.filter(tenant_key=t).order_by().values_list("site", flat=True).distinct())
    names.discard("")
    names.discard(GLOBAL_SITE)
    allowed = visible_sites(user)
    if allowed is not None:
        names &= allowed
    return sorted(names, key=str.lower)


def vrf_names():
    t = tenant()
    names = set(Subnet.objects.filter(tenant_key=t).order_by().values_list("vrf", flat=True).distinct())
    return sorted(n for n in names if n)


# ------------------------------------------------------------------ parsing
def parse_address(text):
    """An IPv4/IPv6 address in canonical form. Rejects prefixes, zone ids and legacy IPv4 spellings."""
    text = (text or "").strip()
    if not text:
        raise ValueError("Enter an IP address.")
    if "/" in text:
        raise ValueError("Enter a single address without a prefix length.")
    if "%" in text:
        raise ValueError("Zone identifiers (%…) are not stored in IPAM.")
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        raise ValueError(f"“{text[:60]}” is not a valid IPv4 or IPv6 address.")


def parse_network(text):
    """A network in canonical CIDR form. Host bits must be zero; the error names the network that was meant."""
    text = (text or "").strip()
    if not text:
        raise ValueError("Enter a network in CIDR notation, e.g. 10.0.1.0/24 or 2001:db8::/48.")
    if "/" not in text:
        raise ValueError("Add the prefix length, e.g. 10.0.1.0/24.")
    try:
        return ipaddress.ip_network(text, strict=True)
    except ValueError:
        try:
            loose = ipaddress.ip_network(text, strict=False)
        except ValueError:
            raise ValueError(f"“{text[:60]}” is not a valid IPv4 or IPv6 network.")
        raise ValueError(f"Host bits are set; the network is {loose}.")


def fmt_count(n):
    """Large IPv6 sizes as powers of two, everything else with thousands separators."""
    if n is None:
        return "–"
    if n >= 10 ** 9:
        exp = n.bit_length() - 1
        if n == 1 << exp:
            return f"2^{exp}"
        return f"≈2^{exp + 1 if n - (1 << exp) > (1 << exp) // 2 else exp}"
    return f"{n:,}"


# ------------------------------------------------------------------ subnet placement
def overlapping(tenant_key, vrf, net, exclude_pk=None):
    """Subnets in the same tenant + VRF whose range intersects `net` (with CIDR blocks: equal or nested)."""
    start, end = ip_key(net.network_address), ip_key(net.broadcast_address)
    qs = Subnet.objects.filter(tenant_key=tenant_key, vrf=vrf, version=net.version, net_start__lte=end, net_end__gte=start)
    if exclude_pk:
        qs = qs.exclude(pk=exclude_pk)
    return list(qs.order_by("prefixlen"))


def placement_error(tenant_key, vrf, net, role, exclude_pk=None):
    """Why `net` cannot be stored, or None. CIDR blocks can only overlap by nesting: that is allowed only
    when the larger block is a Container (supernet). Duplicates and any other overlap are rejected."""
    for s in overlapping(tenant_key, vrf, net, exclude_pk):
        where = f" in VRF “{vrf}”" if vrf else ""
        if s.cidr == str(net):
            return f"{net} already exists{where}."
        if s.prefixlen < net.prefixlen and not s.is_container:
            return (f"{net} overlaps {s.cidr}{where}. Mark {s.cidr} as a Container (supernet) to nest subnets in it, "
                    f"or choose a free range.")
        if s.prefixlen > net.prefixlen and role != Role.CONTAINER:
            return (f"{net} overlaps the existing subnet {s.cidr}{where}. Set the purpose to Container (supernet) "
                    f"to hold it, or choose a free range.")
    return None


def subnet_for(address, vrf="", tenant_key=None):
    """Most specific subnet holding `address` in the VRF (None when no subnet contains it)."""
    ip = address if isinstance(address, (ipaddress.IPv4Address, ipaddress.IPv6Address)) else parse_address(address)
    key = ip_key(ip)
    return (Subnet.objects.filter(tenant_key=tenant_key or tenant(), vrf=vrf, version=ip.version,
                                  net_start__lte=key, net_end__gte=key)
            .order_by("-prefixlen").first())


def address_record(address, vrf="", tenant_key=None):
    try:
        ip = parse_address(str(address))
    except ValueError:
        return None
    return IPAddress.objects.filter(tenant_key=tenant_key or tenant(), vrf=vrf, address=str(ip)).first()


def reassign(tenant_key, vrf, version, start_key, end_key):
    """Point every address in [start, end] of the VRF at its most specific subnet (and that subnet's site).
    Called after a subnet is created, moved, resized or deleted. Returns the number of rows changed."""
    subnets = list(Subnet.objects.filter(tenant_key=tenant_key, vrf=vrf, version=version, net_start__lte=end_key,
                                         net_end__gte=start_key).order_by("-prefixlen"))
    changed = []
    for a in IPAddress.objects.filter(tenant_key=tenant_key, vrf=vrf, version=version, addr_key__gte=start_key,
                                      addr_key__lte=end_key).select_related("subnet"):
        best = next((s for s in subnets if s.net_start <= a.addr_key <= s.net_end), None)
        site = best.site if best else a.site
        if a.subnet_id != (best.pk if best else None) or a.site != site:
            a.subnet, a.site = best, site
            changed.append(a)
    if changed:
        IPAddress.objects.bulk_update(changed, ["subnet", "site"], batch_size=500)
    return len(changed)


def addresses_in(subnet):
    """Address rows inside the subnet's range in its VRF (including those assigned to child subnets)."""
    return IPAddress.objects.filter(tenant_key=subnet.tenant_key, vrf=subnet.vrf, version=subnet.version,
                                    addr_key__gte=subnet.net_start, addr_key__lte=subnet.net_end)


def used_count(subnet):
    return addresses_in(subnet).count()


def utilisation(subnet, used):
    """% of usable addresses that are recorded (float; tiny for big IPv6 ranges, never enumerated)."""
    usable = subnet.usable
    if usable <= 0:
        return 0.0
    return min(100.0, 100.0 * used / usable)


def util_tone(pct):
    if pct is None:
        return "mute"
    return "crit" if pct >= 90 else "warn" if pct >= 75 else "ok"


def contained(subnet):
    """Subnets nested inside this one (same tenant + VRF), largest first."""
    return list(Subnet.objects.filter(tenant_key=subnet.tenant_key, vrf=subnet.vrf, version=subnet.version,
                                      net_start__gte=subnet.net_start, net_end__lte=subnet.net_end,
                                      prefixlen__gt=subnet.prefixlen).order_by("net_start", "prefixlen"))


def children(subnet, nested=None):
    """Direct children: nested subnets that are not inside another nested subnet."""
    nested = contained(subnet) if nested is None else nested
    out = []
    for s in nested:
        if not any(o.prefixlen < s.prefixlen and o.net_start <= s.net_start and s.net_end <= o.net_end for o in nested):
            out.append(s)
    return out


def parent(subnet):
    return (Subnet.objects.filter(tenant_key=subnet.tenant_key, vrf=subnet.vrf, version=subnet.version,
                                  net_start__lte=subnet.net_start, net_end__gte=subnet.net_end,
                                  prefixlen__lt=subnet.prefixlen).order_by("-prefixlen").first())


def depth(subnet):
    return Subnet.objects.filter(tenant_key=subnet.tenant_key, vrf=subnet.vrf, version=subnet.version,
                                 net_start__lte=subnet.net_start, net_end__gte=subnet.net_end,
                                 prefixlen__lt=subnet.prefixlen).count()


def _blocked_ranges(subnet, nested=None):
    """Sorted [start, end] integer ranges that are not free inside the subnet: recorded addresses, the gateway
    and the ranges of child subnets (an address there belongs to the child)."""
    ranges = [(key_int(k), key_int(k)) for k in addresses_in(subnet).order_by("addr_key").values_list("addr_key", flat=True)]
    if subnet.gateway:
        g = int(ipaddress.ip_address(subnet.gateway))
        ranges.append((g, g))
    for c in children(subnet, nested):
        ranges.append((key_int(c.net_start), key_int(c.net_end)))
    ranges.sort()
    return ranges


def next_free(subnet, nested=None):
    """First assignable address that is not recorded, not the gateway and not inside a child subnet.
    Walks the sorted blocked ranges only, so it is fast on any prefix length. None when the subnet is full."""
    candidate, last = subnet.first_host(), subnet.last_host()
    for lo, hi in _blocked_ranges(subnet, nested):
        if hi < candidate:
            continue
        if lo > candidate:
            break
        candidate = hi + 1
        if candidate > last:
            return None
    if candidate > last:
        return None
    return ipaddress.ip_address(candidate) if subnet.version == 4 else ipaddress.IPv6Address(candidate)


def child_coverage(subnet, nested=None):
    """% of the range covered by direct children (for containers)."""
    kids = children(subnet, nested)
    if not kids:
        return None
    covered = sum(c.size for c in kids)
    return min(100.0, 100.0 * covered / subnet.size)


# ------------------------------------------------------------------ address map
def address_map(subnet, can_add=False):
    """Cells for the subnet detail map, or (None, reason) when no map is drawn.

    IPv4 /22 … /32: one cell per address (≤ 1024). IPv4 /16 … /21: one cell per /24 block (≤ 64) coloured by how many
    addresses are recorded in it. Anything bigger, and every IPv6 subnet, gets no map – sizes are never enumerated."""
    if subnet.version != 4:
        return None, "IPv6 subnets have no address map: their ranges are far too large to draw. Use the address list."
    if subnet.prefixlen < BLOCK_MAP_MIN_PREFIX_V4:
        return None, (f"/{subnet.prefixlen} is bigger than /{BLOCK_MAP_MIN_PREFIX_V4}: no address map is drawn. "
                      "Open a child subnet for its map.")
    start, end = key_int(subnet.net_start), key_int(subnet.net_end)
    nested = contained(subnet)
    kids = children(subnet, nested)
    if subnet.prefixlen >= MAP_MAX_PREFIX_V4:
        rows = {key_int(r["addr_key"]): r for r in addresses_in(subnet).values("pk", "addr_key", "address", "status", "hostname")}
        gateway = int(ipaddress.ip_address(subnet.gateway)) if subnet.gateway else None
        cells = []
        for n in range(start, end + 1):
            ip = str(ipaddress.IPv4Address(n))
            row = rows.get(n)
            child = next((c for c in kids if c.contains_int(n)), None)
            if row:
                kind, title, pk = row["status"], f"{ip} · {row['status']}" + (f" · {row['hostname']}" if row["hostname"] else ""), row["pk"]
            elif subnet.prefixlen < 31 and n == start:
                kind, title, pk = "net", f"{ip} · network address", None
            elif subnet.prefixlen < 31 and n == end:
                kind, title, pk = "net", f"{ip} · broadcast address", None
            elif n == gateway:
                kind, title, pk = "gateway", f"{ip} · gateway (not recorded as an address)", None
            elif child:
                kind, title, pk = "child", f"{ip} · in child subnet {child.cidr}", None
            else:
                kind, title, pk = "free", f"{ip} · free", None
            cells.append({"ip": ip, "kind": kind, "title": title, "pk": pk,
                          "child": child.pk if child and not row else None,
                          "add": can_add and kind == "free", "last_octet": n & 0xFF})
        return {"mode": "address", "cells": cells, "cols": 32 if len(cells) >= 32 else len(cells)}, ""
    # /16 … /21: /24 blocks
    counts = {}
    for k in addresses_in(subnet).values_list("addr_key", flat=True):
        b = key_int(k) >> 8
        counts[b] = counts.get(b, 0) + 1
    cells = []
    for b in range(start >> 8, (end >> 8) + 1):
        net = ipaddress.IPv4Network((b << 8, 24))
        used = counts.get(b, 0)
        child = next((c for c in nested if c.prefixlen <= 24 and c.contains_int(b << 8)), None)
        pct = 100.0 * used / 254
        kind = ("child" if child else "free") if not used else ("crit" if pct >= 90 else "warn" if pct >= 75 else "used")
        cells.append({"ip": str(net), "kind": kind, "used": used, "pct": pct, "child": child.pk if child else None,
                      "title": f"{net} · {used} address{'es' if used != 1 else ''} recorded" + (f" · {child.cidr}" if child else "")})
    return {"mode": "block", "cells": cells, "cols": 16 if len(cells) >= 16 else len(cells)}, ""


# ------------------------------------------------------------------ dashboard numbers
def used_counts(subnets):
    """{subnet pk: recorded addresses in its range}. Leaf subnets cannot hold other subnets (only containers
    can), so every address in a leaf's range is assigned to it: one grouped query covers all leaves.
    Containers count their whole range (children included) with one indexed range query each."""
    subnets = list(subnets)
    leaves = [s.pk for s in subnets if not s.is_container]
    out = {pk: 0 for pk in leaves}
    if leaves:
        for row in (IPAddress.objects.filter(subnet_id__in=leaves).order_by().values("subnet_id")
                    .annotate(n=Count("id"))):
            out[row["subnet_id"]] = row["n"]
    for s in subnets:
        if s.is_container:
            out[s.pk] = used_count(s)
    return out


def subnet_stats(subnets):
    """[(subnet, used, pct)] for the given subnets."""
    subnets = list(subnets)
    used = used_counts(subnets)
    return [(s, used[s.pk], utilisation(s, used[s.pk])) for s in subnets]


def leaf_ipv4(qs):
    return qs.filter(version=4).exclude(role=Role.CONTAINER)


def ipv4_capacity(qs):
    """(used, usable) over IPv4 leaf subnets (containers are excluded so nothing is counted twice)."""
    used = usable = 0
    for s, u, _ in subnet_stats(leaf_ipv4(qs)):
        used += u
        usable += s.usable
    return used, usable


def search_q(text):
    text = (text or "").strip()
    if not text:
        return Q()
    return Q(address__icontains=text) | Q(hostname__icontains=text) | Q(description__icontains=text)
