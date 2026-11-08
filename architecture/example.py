"""The example fabric: the drawing the page was designed after (2 edge routers, 2 firewalls, 2 core / border switches,
4 spines, 4 leaves and four server groups with a rack each), created as real devices, links and racks.

Used by `manage.py seed_architecture` and by the page's "Load example fabric" button, so both run the same code.
Idempotent. Every device is tagged "example, arch-example" and addressed from the documentation ranges (RFC 5737), so
the reachability probe refuses them and they stay "Unknown": no status, metric or traffic figure is invented. `remove()`
deletes only what `load()` created (devices carrying the arch-example tag, the example room and its racks)."""
from django.apps import apps
from django.core.exceptions import ValidationError

from . import services
from .forms import duplicate_of
from .models import EXAMPLE_TAG, Link, Origin

MARK = "[example]"
ROOM = "Example – Fabric Hall"
NOTE = "Example record created by manage.py seed_architecture – documentation IP range, not a real device."
TAGS = f"example, {EXAMPLE_TAG}"

ROUTERS = [("dc1-edge-rtr-01", "198.51.100.11"), ("dc1-edge-rtr-02", "198.51.100.12")]
FIREWALLS = [("dc1-fw-01", "198.51.100.21"), ("dc1-fw-02", "198.51.100.22")]
CORES = [("dc1-core-01", "192.0.2.31"), ("dc1-core-02", "192.0.2.32")]
SPINES = [(f"dc1-spine-{i:02d}", f"192.0.2.{40 + i}") for i in range(1, 5)]
LEAVES = [(f"dc1-leaf-{i:02d}", f"192.0.2.{50 + i}") for i in range(1, 5)]
# group: (name stem, model, role, rack, first ip octet, count, device role text)
GROUPS = [
    ("web", "Dell PowerEdge R650", "Web server", "ARCH-WEB", 10, 5),
    ("app", "Dell PowerEdge R750", "Application server", "ARCH-APP", 20, 5),
    ("db", "Dell PowerEdge R7525", "Database server", "ARCH-DB", 30, 5),
    ("san", "Dell PowerVault ME5024", "SAN storage array", "ARCH-SAN", 40, 4),
]


def _devices():
    rows = []
    for name, ip in ROUTERS:
        rows.append(dict(name=name, device_type="router", vendor="Cisco", model="ASR 1001-HX", mgmt_ip=ip, role="Internet edge router"))
    for name, ip in FIREWALLS:
        rows.append(dict(name=name, device_type="firewall", vendor="Fortinet", model="FortiGate 601F", mgmt_ip=ip, check_port=443,
                         role="Perimeter firewall"))
    for name, ip in CORES:
        rows.append(dict(name=name, device_type="switch", vendor="Cisco", model="Catalyst 9500-48Y4C", mgmt_ip=ip, role="Core / border switch"))
    for name, ip in SPINES:
        rows.append(dict(name=name, device_type="switch", vendor="Cisco", model="Nexus 9336C-FX2", mgmt_ip=ip, role="Spine switch"))
    for name, ip in LEAVES:
        rows.append(dict(name=name, device_type="switch", vendor="Cisco", model="Nexus 93180YC-FX", mgmt_ip=ip, role="Leaf switch"))
    for stem, model, role, _rack, base, count in GROUPS:
        for i in range(1, count + 1):
            rows.append(dict(name=f"dc1-{stem}-{i:02d}", device_type="physical_server", vendor="Dell", model=model,
                             mgmt_ip=f"203.0.113.{base + i}", check_port=443, role=role))
    return rows


def _links():
    """(a name, a port, b name, b port, medium, speed Mbps, kind)"""
    out = []
    for i in range(2):
        out.append((ROUTERS[i][0], "Gi0/0/0", FIREWALLS[i][0], "Eth1/1", "fibre", 10000, "wan"))
    out.append((FIREWALLS[0][0], "Eth1/49", FIREWALLS[1][0], "Eth1/49", "copper", 1000, "interconnect"))
    for f in range(2):
        for c in range(2):
            out.append((FIREWALLS[f][0], f"Eth1/{2 + c}", CORES[c][0], f"Eth1/{1 + f}", "fibre", 40000, "uplink"))
    out.append((CORES[0][0], "Eth1/49", CORES[1][0], "Eth1/49", "dac", 100000, "interconnect"))
    for c in range(2):
        for s in range(4):
            out.append((CORES[c][0], f"Eth1/{20 + s}", SPINES[s][0], f"Eth1/{31 + c}", "fibre", 100000, "uplink"))
    for s in range(4):
        for l in range(4):
            out.append((SPINES[s][0], f"Eth1/{l + 1}", LEAVES[l][0], f"Eth1/{49 + s}", "fibre", 100000, "uplink"))
    for g, (stem, _m, _r, _rack, _b, count) in enumerate(GROUPS):
        for i in range(1, count + 1):
            out.append((LEAVES[g][0], f"Eth1/{i}", f"dc1-{stem}-{i:02d}", "ens1f0", "dac", 25000, "downlink"))
    return out


def load(site, tenant=None, user=None, log=None):
    """Create the example fabric on `site`. Returns {"devices", "links", "racks", "items", "skipped"} created counts."""
    from network.models import Device
    tenant = tenant or services.tenant()
    log = log or (lambda *_: None)
    n = {"devices": 0, "links": 0, "racks": 0, "items": 0, "skipped": 0}
    by_name, untouched = {}, set()
    want_site = site if site != "All Sites" else ""
    for spec in _devices():
        existing = Device.objects.filter(tenant_key=tenant, name__iexact=spec["name"]).first()
        if existing is not None:
            ours = EXAMPLE_TAG in [t.lower() for t in existing.tag_list]
            if not ours or (user is not None and existing.site != want_site):
                # a real device, or (for a user's load) an example device an earlier load put on another site: leave it
                # alone and make no links or rack items for it
                log(f"Skipped {spec['name']}: " + ("a real device with that name exists." if not ours
                                                    else "the example device already exists on another site."))
                n["skipped"] += 1
                by_name[spec["name"]] = existing
                untouched.add(spec["name"])
                continue
        fields = dict(spec, site=want_site, environment="prod", tags=TAGS, description=NOTE)
        if existing is None:
            by_name[spec["name"]] = Device.objects.create(tenant_key=tenant, **fields)
            n["devices"] += 1
        else:
            for k, v in fields.items():
                setattr(existing, k, v)
            existing.save()
            by_name[spec["name"]] = existing
    for a, ap, b, bp, medium, speed, kind in _links():
        if a in untouched or b in untouched:
            continue
        da, db = by_name[a], by_name[b]
        if duplicate_of(da.pk, ap, db.pk, bp, tenant) is not None:
            continue
        Link(tenant_key=tenant, a_device=da, b_device=db, a_port=ap, b_port=bp, medium=medium, speed_mbps=speed, kind=kind,
             origin=Origin.MANUAL, note=f"{MARK} example link").save()
        n["links"] += 1
    _racks(site, tenant, by_name, n, untouched)
    return n


def _racks(site, tenant, by_name, n, untouched=frozenset()):
    try:
        Room = apps.get_model("dcim", "Room")
        Rack = apps.get_model("dcim", "Rack")
        RackItem = apps.get_model("dcim", "RackItem")
    except LookupError:
        return
    room = Room.objects.filter(tenant_key=tenant, name=ROOM, site=site).first() or Room.objects.filter(tenant_key=tenant, name=ROOM).first()
    if room is None:
        room = Room.objects.create(tenant_key=tenant, site=site, name=ROOM, floor="Ground",
                                   description=f"{MARK} Room of the architecture example fabric.")
    for g, (stem, _model, _role, rack_name, _base, count) in enumerate(GROUPS):
        rack, was = Rack.objects.get_or_create(tenant_key=tenant, room=room, name=rack_name,
                                               defaults={"u_height": 42, "max_power_kw": 8.0, "max_cooling_kw": 10.0,
                                                         "notes": f"{MARK} Rack of the {stem} group."})
        n["racks"] += was
        placing = [(by_name[LEAVES[g][0]], "switch", 42, 1)] + [
            (by_name[f"dc1-{stem}-{i:02d}"], "storage" if stem == "san" else "server", 38 - 3 * (i - 1), 2) for i in range(1, count + 1)]
        for dev, kind, u, h in placing:
            if dev.name in untouched or RackItem.objects.filter(rack=rack, device=dev).exists():
                continue
            item = RackItem(tenant_key=tenant, rack=rack, name=dev.name, kind=kind, u_position=u, u_height=h, face="both",
                            device=dev, power_draw_w=350 if kind != "switch" else 200, notes=f"{MARK} example mounting")
            try:
                item.full_clean()
            except ValidationError:
                n["skipped"] += 1
                continue
            item.save()
            n["items"] += 1


def remove(tenant=None):
    """Delete the example devices (and with them their links and placements), racks and room. Returns counts."""
    from network.models import Device
    tenant = tenant or services.tenant()
    names = [d["name"] for d in _devices()]
    pks = [d.pk for d in Device.objects.filter(tenant_key=tenant, name__in=names) if EXAMPLE_TAG in [t.lower() for t in d.tag_list]]
    out = {"devices": len(pks), "racks": 0}
    try:
        Room = apps.get_model("dcim", "Room")
        Rack = apps.get_model("dcim", "Rack")
        RackItem = apps.get_model("dcim", "RackItem")
    except LookupError:
        Room = None
    if Room is not None:
        rooms = Room.objects.filter(tenant_key=tenant, name=ROOM, description__icontains=MARK)
        racks = Rack.objects.filter(tenant_key=tenant, room__in=rooms, name__in=[g[3] for g in GROUPS], notes__icontains=MARK)
        # only the items this command mounted; anything an operator added to the rack is deleted with it only if the rack goes
        out["racks"] = racks.count()
        for rack in racks:
            if not RackItem.objects.filter(rack=rack).exclude(notes__icontains=MARK).exists():
                rack.delete()
            else:
                RackItem.objects.filter(rack=rack, notes__icontains=MARK).delete()
                out["racks"] -= 1
        for room in rooms:
            if not Rack.objects.filter(room=room).exists():
                room.delete()
    Device.objects.filter(pk__in=pks).delete()
    return out
