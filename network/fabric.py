"""Spine–leaf data-center fabric: classify devices into diagram layers from inventory fields."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.urls import reverse

from network.models import Device, DeviceType


@dataclass(frozen=True)
class FabricLayerSpec:
    key: str
    title: str
    subtitle: str
    icon: str
    rack_class: str


FABRIC_LAYERS: tuple[FabricLayerSpec, ...] = (
    FabricLayerSpec("wan", "WAN / edge routers", "Internet and wide-area paths", "share", ""),
    FabricLayerSpec("firewall", "Firewalls", "Perimeter and internal security", "brick", ""),
    FabricLayerSpec("core", "Core / border", "Aggregation to the fabric", "network", ""),
    FabricLayerSpec("spine", "Spine", "Non-blocking spine switches", "network", ""),
    FabricLayerSpec("leaf", "Leaf", "Access layer to servers", "network", ""),
    FabricLayerSpec("tier_web", "Web tier", "Front-end and presentation", "globe", "nw-fabric-rack-web"),
    FabricLayerSpec("tier_app", "Application tier", "Business logic and APIs", "layers", "nw-fabric-rack-app"),
    FabricLayerSpec("tier_db", "Database tier", "Transactional data stores", "database", "nw-fabric-rack-db"),
    FabricLayerSpec("tier_storage", "Storage (SAN)", "Shared block/file storage", "box", "nw-fabric-rack-storage"),
    FabricLayerSpec("other", "Other devices", "Unclassified inventory", "help", ""),
)


def fabric_layer(device: Device) -> str:
    """Return a FABRIC_LAYERS key for a device (heuristic from type, name, role, tags)."""
    name = (device.name or "").lower()
    role = (device.role or "").lower()
    blob = " ".join([name, role, " ".join(device.tag_list)]).lower()
    dt = device.device_type

    if dt == DeviceType.FIREWALL:
        return "firewall"
    if dt == DeviceType.ROUTER:
        return "wan"
    if dt == DeviceType.SWITCH:
        if "spine" in blob:
            return "spine"
        if any(k in blob for k in ("core", "border", "dist", "aggregation")):
            return "core"
        return "leaf"
    if dt == DeviceType.LOAD_BALANCER:
        return "tier_web"

    if dt in (DeviceType.PHYSICAL_SERVER, DeviceType.VIRTUAL_MACHINE, DeviceType.WIRELESS):
        if any(k in blob for k in ("web", "www", "frontend", "http", "portal", "proxy", "cdn")):
            return "tier_web"
        if any(k in blob for k in ("db", "database", "sql", "oracle", "mysql", "postgres", "mongo")):
            return "tier_db"
        if any(k in blob for k in ("san", "storage", "nas", "ceph", "nfs", "filer")):
            return "tier_storage"
        if any(k in blob for k in ("app", "api", "service", "bo", "backoffice", "core-ns", "core-ps", "analyzer")):
            return "tier_app"
        if "ns" in name and "ps" not in name:
            return "tier_web"
        if name.endswith("-ps") or "physical" in blob:
            return "tier_app"
        return "tier_app"

    return "other"


def fabric_layer_label(layer_key: str) -> str:
    for spec in FABRIC_LAYERS:
        if spec.key == layer_key:
            return spec.title
    return layer_key


def _card(device: Device) -> dict[str, Any]:
    return {
        "pk": device.pk,
        "name": device.name,
        "mgmt_ip": device.mgmt_ip,
        "status": device.status,
        "device_type": device.device_type,
        "type_display": device.get_device_type_display(),
        "type_icon": device.type_icon,
        "vendor": device.vendor,
        "latency": device.last_latency_ms,
        "last_seen": device.last_seen.isoformat() if device.last_seen else None,
        "detail_url": reverse("network:detail", args=[device.pk]),
        "layer": fabric_layer(device),
    }


def build_fabric(devices) -> dict[str, Any]:
    """Group devices into layers with live status roll-ups."""
    layers: dict[str, list[dict[str, Any]]] = {spec.key: [] for spec in FABRIC_LAYERS}
    totals = {"up": 0, "down": 0, "unknown": 0, "total": 0}
    for device in devices:
        key = fabric_layer(device)
        if key not in layers:
            key = "other"
        layers[key].append(_card(device))
        totals["total"] += 1
        st = device.status or "unknown"
        if st in totals:
            totals[st] += 1
        else:
            totals["unknown"] += 1

    for key in layers:
        layers[key].sort(key=lambda c: c["name"].lower())

    always_show = {"firewall", "core", "spine", "leaf", "tier_web", "tier_app", "tier_db", "tier_storage"}
    layer_rows = []
    for spec in FABRIC_LAYERS:
        items = layers[spec.key]
        if not items and spec.key not in always_show:
            if spec.key in ("wan", "other"):
                continue
            if not items:
                continue
        counts = {"up": 0, "down": 0, "unk": 0}
        for it in items:
            st = it["status"] or "unknown"
            if st == "unknown":
                counts["unk"] += 1
            elif st in counts:
                counts[st] += 1
            else:
                counts["unk"] += 1
        layer_rows.append(
            {
                "spec": spec,
                "nodes": items,
                "counts": counts,
                "has_counts": bool(items),
            }
        )

    return {"layers": layer_rows, "totals": totals, "layers_map": layers}
