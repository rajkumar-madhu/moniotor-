"""Parse Lesites / FinSpot device workbooks (Devices sheet only) for network inventory import."""
from __future__ import annotations

import re
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any

from django.core.exceptions import ValidationError

from network.models import Device, DeviceType, validate_host

_NS = {
    "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "pr": "http://schemas.openxmlformats.org/package/2006/relationships",
}

# Expected Devices sheet columns (FinSpot / Lesites export).
_DEVICES_COLUMNS = (
    "selecthost",
    "ipaddress",
    "subipaddress",
    "servertype",
    "emailid",
    "servicename",
    "textname",
    "pathhost",
    "physical_ip",
)


@dataclass
class LesitesDeviceRow:
    selecthost: str = ""
    ipaddress: str = ""
    subipaddress: str = ""
    servertype: str = ""
    emailid: str = ""
    servicename: str = ""
    textname: str = ""
    pathhost: str = ""
    physical_ip: str = ""


@dataclass
class ImportResult:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    warnings: list[str] = field(default_factory=list)
    actions: list[tuple[str, str, str, str]] = field(default_factory=list)


def classify_device_type(selecthost: str, pathhost: str) -> str:
    """Map legacy selecthost/pathhost hints to network.DeviceType values."""
    sh = (selecthost or "").lower()
    ph = (pathhost or "").lower()

    if "switch" in ph or "switch" in sh:
        return DeviceType.SWITCH
    if "firewall" in ph or "firewall" in sh or "fortigate" in ph or "fortigate" in sh:
        return DeviceType.FIREWALL
    if "router" in ph or "router" in sh:
        return DeviceType.ROUTER
    if "load" in ph and "bal" in ph:
        return DeviceType.LOAD_BALANCER
    if "wireless" in ph or "wlc" in sh:
        return DeviceType.WIRELESS
    if "vm" in ph:
        return DeviceType.VIRTUAL_MACHINE
    if "physical" in ph:
        return DeviceType.PHYSICAL_SERVER
    if any(x in sh for x in ("ubuntu", "centos", "windows", "server")):
        return DeviceType.PHYSICAL_SERVER

    return DeviceType.SWITCH


def _vendor_from_selecthost(selecthost: str, pathhost: str) -> str:
    sh = (selecthost or "").lower()
    ph = (pathhost or "").lower()
    if "fortigate" in ph or "fortigate" in sh:
        return "Fortinet"
    if sh.startswith("cisco") or "catalyst" in sh:
        return "Cisco"
    if "aruba" in sh:
        return "Aruba"
    if "juniper" in sh:
        return "Juniper"
    if "ubuntu" in sh:
        return ""
    return ""


def _check_port(device_type: str) -> int:
    if device_type in (DeviceType.FIREWALL, DeviceType.LOAD_BALANCER, DeviceType.WIRELESS):
        return 443
    return 22


def _merge_tags(existing: str, add: str) -> str:
    seen: set[str] = set()
    out: list[str] = []
    for part in (existing or "").split(",") + add.split(","):
        t = part.strip()
        if not t:
            continue
        key = t.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return ",".join(out)


def row_to_device_fields(
    row: LesitesDeviceRow,
    *,
    site: str,
    environment: str,
    lesites_tag: str = "fs-dr-le",
) -> dict[str, Any] | None:
    """Return Device model fields for one row, or None if the row should be skipped."""
    mgmt_ip = (row.ipaddress or "").strip()
    if not mgmt_ip:
        return None
    try:
        validate_host(mgmt_ip)
    except ValidationError:
        return None

    name = (row.textname or "").strip() or mgmt_ip
    name = name[:150]
    device_type = classify_device_type(row.selecthost, row.pathhost)
    vendor = _vendor_from_selecthost(row.selecthost, row.pathhost)

    parts = ["Imported from Lesites workbook (Devices sheet)."]
    if row.selecthost:
        parts.append(f"selecthost={row.selecthost.strip()}")
    if row.pathhost:
        parts.append(f"pathhost={row.pathhost.strip()}")
    phys = (row.physical_ip or "").strip()
    if phys and phys not in ("[]", "{}"):
        parts.append(f"physical_ip={phys}")

    desc = " ".join(parts)

    tags = _merge_tags("", f"imported,lesites,{lesites_tag}")
    role = (row.servicename or "").strip()[:120]
    contact = (row.emailid or "").strip()[:150]

    return {
        "name": name,
        "device_type": device_type,
        "vendor": vendor,
        "mgmt_ip": mgmt_ip,
        "check_port": _check_port(device_type),
        "site": site,
        "role": role,
        "environment": environment,
        "contact": contact,
        "description": desc,
        "tags": tags,
        "enabled": True,
    }


def _col_letters(ref: str) -> str:
    return re.sub(r"\d+", "", ref or "A")


def _load_shared_strings(zf: zipfile.ZipFile) -> list[str]:
    try:
        root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    strings: list[str] = []
    for si in root.findall("m:si", _NS):
        strings.append("".join(t.text or "" for t in si.findall(".//m:t", _NS)))
    return strings


def _cell_value(cell: ET.Element, shared: list[str]) -> str:
    v = cell.find("m:v", _NS)
    if v is None or v.text is None:
        return ""
    if cell.get("t") == "s":
        idx = int(v.text)
        return shared[idx] if 0 <= idx < len(shared) else ""
    return v.text


def _sheet_path(zf: zipfile.ZipFile, sheet_name: str) -> str:
    wb = ET.fromstring(zf.read("xl/workbook.xml"))
    sheets = wb.findall("m:sheets/m:sheet", _NS)
    target_rid = None
    for sh in sheets:
        if (sh.get("name") or "") == sheet_name:
            target_rid = sh.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
            break
    if not target_rid:
        names = [sh.get("name") for sh in sheets]
        raise ValueError(f"Sheet {sheet_name!r} not found. Workbook has: {names!r}")

    rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    target = None
    for rel in rels.findall("pr:Relationship", _NS):
        if rel.get("Id") == target_rid:
            target = rel.get("Target")
            break
    if not target:
        raise ValueError("Could not resolve worksheet path in workbook relationships.")
    if not target.startswith("xl/"):
        target = "xl/" + target.lstrip("/")
    return target


def parse_devices_workbook(path: str, sheet_name: str = "Devices") -> list[LesitesDeviceRow]:
    """Read only the named Devices sheet; never opens credential sheets."""
    rows: list[LesitesDeviceRow] = []
    with zipfile.ZipFile(path) as zf:
        shared = _load_shared_strings(zf)
        sheet_xml = _sheet_path(zf, sheet_name)
        root = ET.fromstring(zf.read(sheet_xml))
        header_map: dict[str, int] = {}
        first = True

        for row_el in root.findall("m:sheetData/m:row", _NS):
            cells: dict[int, str] = {}
            for cell in row_el.findall("m:c", _NS):
                ref = cell.get("r", "A1")
                col = _col_letters(ref)
                col_idx = 0
                for ch in col:
                    col_idx = col_idx * 26 + (ord(ch.upper()) - ord("A") + 1)
                col_idx -= 1
                cells[col_idx] = _cell_value(cell, shared)

            if first:
                for idx, val in cells.items():
                    key = (val or "").strip().lower()
                    if key in _DEVICES_COLUMNS:
                        header_map[key] = idx
                if "ipaddress" not in header_map:
                    raise ValueError("Devices sheet is missing required column: ipaddress")
                first = False
                continue

            def get(col: str) -> str:
                idx = header_map.get(col)
                if idx is None:
                    return ""
                return (cells.get(idx) or "").strip()

            entry = LesitesDeviceRow(
                selecthost=get("selecthost"),
                ipaddress=get("ipaddress"),
                subipaddress=get("subipaddress"),
                servertype=get("servertype"),
                emailid=get("emailid"),
                servicename=get("servicename"),
                textname=get("textname"),
                pathhost=get("pathhost"),
                physical_ip=get("physical_ip"),
            )
            if not any((entry.ipaddress, entry.textname, entry.selecthost)):
                continue
            rows.append(entry)

    return rows


def import_devices(
    rows: list[LesitesDeviceRow],
    tenant_key: str,
    *,
    site: str,
    environment: str,
    lesites_tag: str = "fs-dr-le",
    dry_run: bool = False,
    update_existing: bool = True,
    force: bool = False,
) -> ImportResult:
    result = ImportResult()

    for row in rows:
        fields = row_to_device_fields(row, site=site, environment=environment, lesites_tag=lesites_tag)
        if fields is None:
            result.skipped += 1
            result.warnings.append(f"Skipped row without valid mgmt IP (textname={row.textname!r})")
            continue

        name = fields["name"]
        mgmt_ip = fields["mgmt_ip"]
        existing = Device.objects.filter(tenant_key=tenant_key, name__iexact=name).first()
        if existing is None:
            existing = Device.objects.filter(tenant_key=tenant_key, mgmt_ip=mgmt_ip).first()

        if existing is not None and existing.is_example and not force:
            result.skipped += 1
            result.actions.append((name, fields["device_type"], mgmt_ip, "skipped_example"))
            continue

        dtype = fields["device_type"]
        if existing is None:
            if dtype == DeviceType.SWITCH and not any(
                k in (row.pathhost + row.selecthost).lower() for k in ("switch", "router", "firewall", "fortigate", "vm", "physical")
            ):
                result.warnings.append(f"Defaulted device type to switch for {name!r} (pathhost={row.pathhost!r})")

            if dry_run:
                result.created += 1
                result.actions.append((name, fields["device_type"], mgmt_ip, "create"))
                continue
            Device.objects.create(tenant_key=tenant_key, **fields)
            result.created += 1
            result.actions.append((name, fields["device_type"], mgmt_ip, "created"))
            continue

        if not update_existing:
            result.skipped += 1
            result.actions.append((name, fields["device_type"], mgmt_ip, "skipped_exists"))
            continue

        update_fields = dict(fields)
        update_fields["tags"] = _merge_tags(existing.tags, update_fields["tags"])

        if dry_run:
            result.updated += 1
            result.actions.append((name, fields["device_type"], mgmt_ip, "update"))
            continue

        for key, val in update_fields.items():
            setattr(existing, key, val)
        existing.save()
        result.updated += 1
        result.actions.append((name, fields["device_type"], mgmt_ip, "updated"))

    return result
