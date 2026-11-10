"""Create clearly-marked example DCIM records ("[example]" in descriptions and notes). Idempotent; never runs
automatically. Loads are manual example readings, labelled as such; nothing is presented as measured live."""
from django.apps import apps
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand
from django.db import transaction

from dcim.models import EXAMPLE_MARK, CoolingUnit, PowerFeed, Rack, RackItem, Room

MARK = EXAMPLE_MARK

ROOMS = [
    {"key": "hall", "name": "Example – Data Hall A", "floor": "Ground",
     "description": f"{MARK} Demo data hall with hot/cold aisle containment."},
    {"key": "comms", "name": "Example – Comms Room", "floor": "L1",
     "description": f"{MARK} Demo comms room: carrier and core network gear."},
]

RACKS = [
    ("hall", "A01", 42, 8.0, 10.0, "active"),
    ("hall", "A02", 42, 6.0, 8.0, "active"),
    ("hall", "A03", 48, 10.0, 12.0, "planned"),
    ("comms", "C01", 24, 3.0, 3.5, "active"),
]

# rack, name, kind, lowest U, height, face, typical draw (W), device type to link (optional)
ITEMS = [
    ("A01", "Patch panel A01-1", "patch_panel", 42, 1, "front", None, None),
    ("A01", "ToR switch A01", "switch", 41, 1, "front", 180, "switch"),
    ("A01", "Cable manager", "other", 40, 1, "front", None, None),
    ("A01", "db-node-01", "server", 30, 2, "both", 520, "physical_server"),
    ("A01", "db-node-02", "server", 28, 2, "both", 520, "physical_server"),
    ("A01", "app-node-01", "server", 24, 1, "both", 310, None),
    ("A01", "app-node-02", "server", 23, 1, "both", 310, None),
    ("A01", "SAN array", "storage", 10, 4, "both", 900, None),
    ("A01", "PDU-A (vertical)", "pdu", 1, 1, "rear", None, None),
    ("A02", "Patch panel A02-1", "patch_panel", 42, 1, "front", None, None),
    ("A02", "ToR switch A02", "switch", 41, 1, "front", 180, None),
    ("A02", "GPU node 01", "server", 33, 4, "both", 2400, None),
    ("A02", "GPU node 02", "server", 29, 4, "both", 2400, None),
    ("A02", "Backup appliance", "storage", 20, 2, "both", 450, None),
    ("C01", "Core router", "switch", 22, 2, "both", 650, "router"),
    ("C01", "Edge firewall", "switch", 20, 1, "both", 300, "firewall"),
    ("C01", "Carrier patch panel", "patch_panel", 24, 1, "front", None, None),
    ("C01", "Core switch", "switch", 17, 2, "both", 900, None),
    ("C01", "UPS 3 kVA", "pdu", 1, 3, "both", 120, None),
]

FEEDS = [
    ("A01", "PDU-A", "pdu", 7.4, 1.6),
    ("A01", "PDU-B", "pdu", 7.4, 1.5),
    ("A02", "PDU-A", "pdu", 5.0, 2.8),
    ("A02", "PDU-B", "pdu", 5.0, 2.7),
    ("C01", "UPS output", "ups", 2.7, None),
]

COOLING = [
    ("hall", "CRAH-01", "crah", "active", 40.0, 18.0, 29.5),
    ("hall", "CRAH-02", "crah", "standby", 40.0, None, None),
    ("comms", "In-row 1", "in_row", "active", 1.8, 20.0, 31.0),
]


def removal_plan(tenant):
    """What `--remove` deletes, and what core.examples counts (the same predicate). Only records carrying the mark go: the
    marked items and feeds of the seed's racks, the marked cooling units of the seed's rooms, a seed rack once nothing
    unmarked (an operator's item or feed) is in it, and a seed room once no rack and no unmarked cooling unit is left in it.
    An operator's own rack, item, feed or cooling unit inside an example room is never touched, and keeps its container."""
    names = [r["name"] for r in ROOMS]
    rooms = list(Room.objects.filter(tenant_key=tenant, name__in=names, description__icontains=MARK))
    racks = list(Rack.objects.filter(room__in=rooms, name__in=[r[1] for r in RACKS], notes__icontains=MARK))
    keep = set(RackItem.objects.filter(rack__in=racks).exclude(notes__icontains=MARK).values_list("rack_id", flat=True))
    keep |= set(PowerFeed.objects.filter(rack__in=racks).exclude(notes__icontains=MARK).values_list("rack_id", flat=True))
    gone = [r for r in racks if r.pk not in keep]
    gone_pks = [r.pk for r in gone]
    rooms_gone = [room for room in rooms
                  if not Rack.objects.filter(room=room).exclude(pk__in=gone_pks).exists()
                  and not CoolingUnit.objects.filter(room=room).exclude(notes__icontains=MARK).exists()]
    return {
        "rooms": rooms_gone,
        "racks": gone,
        "items": RackItem.objects.filter(rack__in=racks, notes__icontains=MARK),
        "feeds": PowerFeed.objects.filter(rack__in=racks, notes__icontains=MARK),
        "cooling": CoolingUnit.objects.filter(room__in=rooms, notes__icontains=MARK),
    }


class Command(BaseCommand):
    help = "Create example DCIM rooms, racks, items, feeds and cooling units marked [example] (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument("--remove", action="store_true",
                            help="Delete the example records (an operator's own records in the example rooms are kept).")

    def handle(self, *args, remove=False, **opts):
        tenant = settings.LINKEDEYE_TENANT
        if remove:
            plan = removal_plan(tenant)
            n = 0
            with transaction.atomic():
                for qs in (plan["items"], plan["feeds"], plan["cooling"]):
                    n += qs.delete()[0]
                for obj in plan["racks"] + plan["rooms"]:
                    n += obj.delete()[0]
            self.stdout.write(self.style.SUCCESS(f"Removed example DCIM records ({n} row(s))."))
            return
        Site = apps.get_model("core", "Site")
        sites = list(Site.objects.filter(tenant_key=tenant).order_by("name").values_list("name", flat=True)) or ["All Sites"]
        created = {"rooms": 0, "racks": 0, "items": 0, "feeds": 0, "cooling": 0, "skipped": 0}
        rooms = {}
        for i, spec in enumerate(ROOMS):
            site = sites[min(i, len(sites) - 1)]
            room = Room.objects.filter(tenant_key=tenant, name=spec["name"]).first()
            if room is None:
                room = Room.objects.create(tenant_key=tenant, site=site, name=spec["name"], floor=spec["floor"],
                                           description=spec["description"])
                created["rooms"] += 1
            rooms[spec["key"]] = room
        racks = {}
        for key, name, height, power, cooling, status in RACKS:
            rack, was = Rack.objects.get_or_create(
                tenant_key=tenant, room=rooms[key], name=name,
                defaults={"u_height": height, "max_power_kw": power, "max_cooling_kw": cooling, "status": status,
                          "notes": f"{MARK} Demo rack."})
            created["racks"] += was
            racks[name] = rack
        devices = self._devices(tenant)
        for rack_name, name, kind, u, h, face, draw, dev_type in ITEMS:
            rack = racks[rack_name]
            if RackItem.objects.filter(rack=rack, name=name).exists():
                continue
            item = RackItem(tenant_key=tenant, rack=rack, name=name, kind=kind, u_position=u, u_height=h, face=face,
                            power_draw_w=draw, notes=f"{MARK} typical draw is an example figure")
            dev = self._pick(devices, dev_type, rack.site)
            if dev is not None:
                item.device = dev
            try:
                item.full_clean()
            except ValidationError:
                created["skipped"] += 1   # the slot is taken by a real item: never overwrite it
                continue
            item.save()
            created["items"] += 1
        for rack_name, name, kind, cap, measured in FEEDS:
            _, was = PowerFeed.objects.get_or_create(
                tenant_key=tenant, rack=racks[rack_name], name=name,
                defaults={"kind": kind, "capacity_kw": cap, "measured_kw": measured,
                          "notes": f"{MARK} manual example reading" if measured is not None else f"{MARK} not measured"})
            created["feeds"] += was
        for key, name, kind, status, cap, supply, ret in COOLING:
            _, was = CoolingUnit.objects.get_or_create(
                tenant_key=tenant, room=rooms[key], name=name,
                defaults={"kind": kind, "status": status, "capacity_kw": cap, "supply_temp_c": supply,
                          "return_temp_c": ret, "notes": f"{MARK} manual example temperatures"})
            created["cooling"] += was
        self.stdout.write(self.style.SUCCESS(
            "Example DCIM data: " + ", ".join(f"{v} {k}" for k, v in created.items()) + " created/skipped."))

    def _devices(self, tenant):
        """Unmounted example devices from seed_network, so the elevation can link to real Device records."""
        try:
            Device = apps.get_model("network", "Device")
        except LookupError:
            return []
        return [d for d in Device.objects.filter(tenant_key=tenant, tags__icontains="example", rack_items__isnull=True)]

    @staticmethod
    def _pick(devices, dev_type, site):
        if not dev_type:
            return None
        for d in devices:
            if d.device_type == dev_type and (not d.site or d.site == "All Sites" or d.site == site or site == "All Sites"):
                devices.remove(d)
                return d
        return None
