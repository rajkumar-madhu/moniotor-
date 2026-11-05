"""Import LinkedEye client-wise tracker spreadsheets (FinSpot DEV template).

Passwords and tokens from the file are not stored — only URLs, ports, status, and auth method labels."""
import csv
import io
import re
from dataclasses import dataclass, field

from django.conf import settings
from django.utils.text import slugify

TITLE_RE = re.compile(r"LinkedEye\s*[—–\-]\s*(.+?)(?:\s+\[[^\]]+\])?\s*$", re.I)
META_PAIR_RE = re.compile(r"([A-Za-z][A-Za-z /]+):\s*([^,|]+)")


@dataclass
class TrackerSite:
    name: str
    code: str
    server_ip: str = ""
    environment: str = ""
    fqdn: str = ""
    status: str = ""
    lead: str = ""
    notes: str = ""


@dataclass
class TrackerTool:
    name: str
    url: str = ""
    port: str = ""
    status: str = ""
    auth_method: str = ""
    notes: str = ""


@dataclass
class ParsedTracker:
    client_name: str = ""
    client_code: str = ""
    status: str = "active"
    contact_name: str = ""
    environment: str = ""
    server_ip: str = ""
    base_url: str = ""
    header_line: str = ""
    tools: list = field(default_factory=list)
    sites: list = field(default_factory=list)
    errors: list = field(default_factory=list)


def _cell(row, idx, default=""):
    if idx >= len(row):
        return default
    return (row[idx] or "").strip()


def _status_key(label):
    raw = (label or "").strip().lower()
    mapping = {"active": "active", "pilot": "pilot", "on hold": "on_hold", "on_hold": "on_hold", "churned": "churned"}
    return mapping.get(raw.replace("-", " "), "active")


def _parse_title_row(row):
    text = _cell(row, 0)
    m = TITLE_RE.match(text)
    if m:
        return m.group(1).strip()
    if text:
        return text.split(",")[0].strip()
    return ""


def _parse_meta_rows(rows, start=1, end=4):
    meta = {}
    for i in range(start, min(end, len(rows))):
        line = ",".join(rows[i])
        for m in META_PAIR_RE.finditer(line):
            key = m.group(1).strip().lower().replace(" ", "_")
            meta[key] = m.group(2).strip()
    return meta


def _section_start(row, needle):
    text = _cell(row, 0)
    return needle.lower() in text.lower()


def _parse_tools(rows, start_idx):
    tools = []
    i = start_idx
    while i < len(rows):
        row = rows[i]
        if not any(c.strip() for c in row):
            i += 1
            continue
        if _section_start(row, "SITE / ORGANIZATION"):
            break
        if _cell(row, 0) in ("#", "") and "Tool" in _cell(row, 1):
            i += 1
            continue
        num = _cell(row, 0)
        if not num.isdigit():
            i += 1
            continue
        tools.append(TrackerTool(
            name=_cell(row, 1),
            url=_cell(row, 2),
            port=_cell(row, 6),
            status=_cell(row, 7),
            auth_method=_cell(row, 5),
            notes=_cell(row, 8),
        ))
        i += 1
    return tools, i


def _parse_sites(rows, start_idx):
    sites = []
    i = start_idx
    while i < len(rows):
        row = rows[i]
        if not any(c.strip() for c in row):
            i += 1
            continue
        if _cell(row, 0) in ("#", "") and "Site Name" in _cell(row, 1):
            i += 1
            continue
        num = _cell(row, 0)
        if not num.isdigit():
            i += 1
            continue
        name = _cell(row, 1)
        code = _cell(row, 2) or slugify(name)[:60]
        sites.append(TrackerSite(
            name=name,
            code=code,
            server_ip=_cell(row, 3),
            environment=_cell(row, 4),
            fqdn=_cell(row, 5),
            status=_cell(row, 6),
            lead=_cell(row, 8),
            notes=_cell(row, 9),
        ))
        i += 1
    return sites


def parse_client_tracker_csv(content):
    """Parse tracker CSV text into structured data (no DB writes)."""
    parsed = ParsedTracker()
    try:
        rows = list(csv.reader(io.StringIO(content)))
    except csv.Error as exc:
        parsed.errors.append(str(exc))
        return parsed
    if not rows:
        parsed.errors.append("File is empty.")
        return parsed

    parsed.client_name = _parse_title_row(rows[0])
    meta = _parse_meta_rows(rows)
    parsed.client_code = (meta.get("slug") or slugify(parsed.client_name)[:60] or "client").lower()
    parsed.server_ip = meta.get("server_ip", "")
    parsed.base_url = meta.get("base_url", "")
    parsed.contact_name = meta.get("lead", "")
    parsed.environment = meta.get("env", meta.get("environment", ""))
    parsed.status = _status_key(meta.get("status", "active"))
    parsed.header_line = _cell(rows[1], 0) if len(rows) > 1 else ""

    tool_start = None
    site_start = None
    for idx, row in enumerate(rows):
        if tool_start is None and _section_start(row, "TOOL & SERVICE"):
            tool_start = idx + 1
        if site_start is None and _section_start(row, "SITE / ORGANIZATION"):
            site_start = idx + 1

    if tool_start is not None:
        parsed.tools, end = _parse_tools(rows, tool_start)
        if site_start is None:
            site_start = end
    if site_start is not None:
        parsed.sites = _parse_sites(rows, site_start)

    if not parsed.client_name and parsed.sites:
        parsed.client_name = parsed.sites[0].name.rsplit(" ", 1)[0] or parsed.sites[0].name
    if not parsed.client_name:
        parsed.errors.append("Could not determine client name (expected title row).")
    return parsed


def _notes_markdown(parsed):
    lines = ["# Client tracker import", ""]
    if parsed.header_line:
        lines.append(parsed.header_line)
        lines.append("")
    if parsed.environment or parsed.server_ip or parsed.base_url:
        lines.append(f"- Environment: {parsed.environment or '—'}")
        lines.append(f"- Server IP: {parsed.server_ip or '—'}")
        lines.append(f"- Base URL: {parsed.base_url or '—'}")
        lines.append("")
    if parsed.tools:
        lines.append("## Tools & services (credentials not stored)")
        lines.append("")
        lines.append("| Tool | URL | Port | Status | Auth |")
        lines.append("| --- | --- | --- | --- | --- |")
        for t in parsed.tools:
            url = t.url.replace("|", "\\|")
            lines.append(f"| {t.name} | {url} | {t.port or '—'} | {t.status or '—'} | {t.auth_method or '—'} |")
        lines.append("")
    return "\n".join(lines)


def apply_client_tracker_csv(content, user=None):
    """Create or update Client + Sites from a tracker CSV. Returns a result dict."""
    from core.models import Client, Site

    parsed = parse_client_tracker_csv(content)
    if parsed.errors:
        return {"ok": False, "errors": parsed.errors}

    t = settings.LINKEDEYE_TENANT
    client, created = Client.objects.get_or_create(
        tenant_key=t,
        code=parsed.client_code,
        defaults={"name": parsed.client_name, "status": parsed.status,
                  "contact_name": parsed.contact_name, "notes": _notes_markdown(parsed)},
    )
    if not created:
        client.name = parsed.client_name
        client.status = parsed.status
        if parsed.contact_name:
            client.contact_name = parsed.contact_name
        client.notes = _notes_markdown(parsed)
        client.save()

    site_results = []
    for s in parsed.sites:
        desc_parts = []
        if s.fqdn:
            desc_parts.append(f"FQDN: {s.fqdn}")
        if s.server_ip:
            desc_parts.append(f"IP: {s.server_ip}")
        if s.environment:
            desc_parts.append(f"Env: {s.environment}")
        if s.notes:
            desc_parts.append(s.notes)
        description = " · ".join(desc_parts)
        site, s_created = Site.objects.get_or_create(
            tenant_key=t,
            code=s.code,
            defaults={"name": s.name, "client": client, "description": description,
                      "criticality": "high" if s.environment.upper() == "PROD" else "standard"},
        )
        if not s_created:
            site.name = s.name
            site.client = client
            site.description = description
            site.save(update_fields=["name", "client", "description"])
        if s.lead and not client.contact_name:
            client.contact_name = s.lead
            client.save(update_fields=["contact_name"])
        site_results.append({"name": site.name, "code": site.code, "created": s_created})

    return {
        "ok": True,
        "client": client.name,
        "client_code": client.code,
        "client_created": created,
        "sites": site_results,
        "tools": len(parsed.tools),
    }
