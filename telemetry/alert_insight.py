"""Group EDR/SIEM-style alert fields and build a short narrative summary for the detail page."""
from __future__ import annotations

import json
import re

# (section key, title, key prefixes — first match wins; longer prefixes checked first via sort)
_SECTION_RULES = (
    ("endpoint", "Endpoint details", ("account.", "agent.", "endpoint.")),
    ("tgt_file", "Target file details", ("tgt.file.",)),
    ("task", "Task details", ("task.",)),
    ("src_parent", "Source process parent details", ("src.process.parent.",)),
    ("src_process", "Source process details", ("src.process.",)),
    ("dst_process", "Destination process details", ("dst.process.",)),
    ("registry", "Registry details", ("registry.", "tgt.registry.")),
    ("network", "Network details", ("net.", "network.", "dst.ip.", "src.ip.")),
)


def _first(d: dict, *keys, default=""):
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return str(v)
    return default


def flatten_fields(labels: dict | None, annotations: dict | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for src in (labels or {}, annotations or {}):
        for k, v in src.items():
            if k in ("alertname", "severity", "summary", "description"):
                continue
            if v is None:
                continue
            s = str(v).strip()
            if s:
                out[k] = s
    return out


def group_fields(flat: dict[str, str]) -> list[dict]:
    """Return ordered sections with title and [(key, value), ...]. Unmatched keys go to 'Additional fields'."""
    if not flat:
        return []
    remaining = dict(flat)
    sections: list[dict] = []
    for _key, title, prefixes in _SECTION_RULES:
        pairs: list[tuple[str, str]] = []
        for field in sorted(remaining):
            if not any(field.startswith(p) for p in prefixes):
                continue
            if _key == "src_process" and field.startswith("src.process.parent."):
                continue
            pairs.append((field, remaining.pop(field)))
        if pairs:
            sections.append({"title": title, "fields": pairs})
    if remaining:
        sections.append({"title": "Additional fields", "fields": sorted(remaining.items())})
    return sections


def context_path(alert, flat: dict[str, str]) -> str:
    parts = [
        _first(flat, "account.name"),
        alert.site or _first(flat, "site", "endpoint.site"),
        _first(flat, "endpoint.name", "agent.uuid", "instance"),
    ]
    parts = [p for p in parts if p]
    return " / ".join(parts) if parts else (alert.site or alert.instance or "")


def analyst_verdict(alert, flat: dict[str, str]) -> str:
    for k in ("analystVerdict", "analyst_verdict", "verdict", "indicator.category"):
        if flat.get(k):
            return flat[k]
    if alert.acknowledged:
        return "Acknowledged"
    return "Undefined"


def build_insight_summary(alert, flat: dict[str, str]) -> str:
    """Narrative paragraph from annotations or structured fields (no external AI)."""
    desc = (alert.description or "").strip()
    ann_desc = (alert.annotations or {}).get("description", "").strip()
    purple = (alert.annotations or {}).get("purple_ai_summary", "").strip() or (alert.annotations or {}).get("insight", "").strip()
    if purple:
        return purple
    if ann_desc and len(ann_desc) > 60:
        return ann_desc
    if desc and len(desc) > 60 and "example alert" not in desc.lower():
        return desc

    name = alert.alertname.replace("_", " ")
    host = _first(flat, "endpoint.name", "agent.uuid", "instance", default=alert.instance or "the host")
    proc = _first(flat, "src.process.displayName", "src.process.name", "process.name")
    parent = _first(flat, "src.process.parent.displayName", "src.process.parent.name")
    target = _first(flat, "tgt.file.path", "task.path", "file.path")
    rule = _first(flat, "rule.name", "detection.name", "alertname", default=alert.alertname)

    parts = [f'This alert "{name}" was reported on {host}.']
    if target:
        parts.append(f"The activity involved {target}.")
    if proc:
        chain = f"Process {proc}"
        if parent:
            chain += f" (parent: {parent})"
        parts.append(f"{chain}.")
    if rule and rule != alert.alertname:
        parts.append(f"Detection: {rule}.")
    mitre = _first(flat, "mitre.technique", "threat.mitreTechnique")
    if mitre:
        parts.append(f"MITRE ATT&CK: {mitre}.")
    if alert.summary:
        parts.append(alert.summary.strip())
    return " ".join(parts)


_INDICATOR_COUNTS = (
    ("src.process.indicatorEvasionCount", "Evasion"),
    ("src.process.indicatorExploitationCount", "Exploitation"),
    ("src.process.indicatorGeneralCount", "General"),
    ("src.process.indicatorPersistenceCount", "Persistence"),
    ("src.process.indicatorPostExploitationCount", "Post-exploitation"),
    ("src.process.indicatorReconnaissanceCount", "Reconnaissance"),
    ("src.process.indicatorConfigurationUpdateCount", "Configuration update"),
)


def extract_indicators(flat: dict[str, str], annotations: dict | None) -> list[dict]:
    """Behavioral indicator rows for the Indicators tab."""
    items: list[dict] = []
    seen: set[str] = set()
    for key, label in _INDICATOR_COUNTS:
        raw = flat.get(key, "")
        if str(raw).isdigit() and int(raw) > 0:
            items.append({"label": label, "count": int(raw), "detail": key})
            seen.add(label)
    ann = annotations or {}
    for blob_key in ("indicators_json", "indicators"):
        blob = ann.get(blob_key, "")
        if not blob:
            continue
        try:
            parsed = json.loads(blob)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, list):
            for row in parsed[:50]:
                if isinstance(row, dict):
                    name = str(row.get("name") or row.get("category") or row.get("type") or "Indicator")[:120]
                    if name in seen:
                        continue
                    seen.add(name)
                    items.append({
                        "label": name,
                        "count": row.get("count") or 1,
                        "detail": str(row.get("description") or row.get("id") or "")[:500],
                    })
                elif isinstance(row, str) and row not in seen:
                    seen.add(row)
                    items.append({"label": row, "count": 1, "detail": ""})
    mitre = _first(flat, "mitre.technique", "threat.mitreTechnique", "mitreTechniques")
    if mitre:
        for part in re.split(r"[,;|]", mitre):
            part = part.strip()
            if part and part not in seen:
                seen.add(part)
                items.append({"label": f"MITRE {part}", "count": 1, "detail": "ATT&CK technique"})
    return items


def attack_graph(flat: dict[str, str]) -> dict:
    """Nodes for the Graph tab (endpoint → parent → process → target)."""
    endpoint = _first(flat, "endpoint.name", "agent.uuid", "instance")
    parent = _first(flat, "src.process.parent.displayName", "src.process.parent.name")
    process = _first(flat, "src.process.displayName", "src.process.name")
    target = _first(flat, "tgt.file.path", "task.path", "tgt.file.name")
    if target and "/" in target and not flat.get("tgt.file.name"):
        target_name = target.rsplit("/", 1)[-1]
    else:
        target_name = _first(flat, "tgt.file.name") or (target.rsplit("/", 1)[-1] if target and "/" in target else target)
    return {
        "endpoint": endpoint,
        "parent": parent,
        "process": process,
        "target": target,
        "target_short": target_name,
        "has_graph": bool(endpoint or process or target),
    }


def alert_detail_context(alert) -> dict:
    flat = flatten_fields(alert.labels, alert.annotations)
    indicators = extract_indicators(flat, alert.annotations)
    graph = attack_graph(flat)
    return {
        "context_path": context_path(alert, flat),
        "analyst_verdict": analyst_verdict(alert, flat),
        "insight_summary": build_insight_summary(alert, flat),
        "detail_sections": group_fields(flat),
        "has_structured_fields": bool(flat),
        "indicators": indicators,
        "indicator_count": len(indicators),
        "attack_graph": graph,
    }
