"""Map SentinelOne (and similar EDR) webhook JSON into Alertmanager v4 alerts for ingest()."""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from .alerts import PayloadError, _clip, _str_map

log = logging.getLogger("linkedeye.telemetry.s1")

# Nested paths -> flat dot keys used by alert_insight grouping
_NESTED_ALIASES = (
    (("agentRealtimeInfo", "agentComputerName"), "endpoint.name"),
    (("agentDetectionInfo", "agentId"), "agent.uuid"),
    (("agentDetectionInfo", "accountName"), "account.name"),
    (("accountInfo", "accountName"), "account.name"),
    (("threatInfo", "threatName"), "alertname"),
    (("threatInfo", "classification"), "threat.classification"),
    (("ruleInfo", "name"), "rule.name"),
    (("ruleInfo", "severity"), "severity"),
    (("sourceProcessInfo", "name"), "src.process.displayName"),
    (("sourceProcessInfo", "filePath"), "src.process.image.path"),
    (("sourceProcessInfo", "commandline"), "src.process.cmdline"),
    (("sourceProcessInfo", "storyline", "id"), "src.process.storyline.id"),
    (("parentProcessInfo", "name"), "src.process.parent.displayName"),
    (("parentProcessInfo", "pid"), "src.process.parent.pid"),
    (("targetProcessInfo", "name"), "dst.process.displayName"),
    (("filePath",), "tgt.file.path"),
    (("fileName",), "tgt.file.name"),
)


def _dig(obj: Any, path: tuple[str, ...]) -> Any:
    cur = obj
    for part in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def flatten_nested(obj: Any, prefix: str = "", out: dict[str, str] | None = None, depth: int = 0) -> dict[str, str]:
    """Flatten nested dicts to dot keys (max depth 6). Lists and scalars become string values."""
    out = out or {}
    if depth > 6:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, dict):
                flatten_nested(v, key, out, depth + 1)
            elif isinstance(v, list):
                out[key] = json.dumps(v)[:4000]
            elif v is not None:
                out[key] = _clip(v, 4000)
    return out


def _map_severity(raw: str) -> str:
    s = (raw or "").strip().lower()
    mapping = {
        "critical": "critical",
        "high": "critical",
        "medium": "warning",
        "low": "info",
        "info": "info",
    }
    return mapping.get(s, s or "warning")


def _apply_aliases(flat: dict[str, str], root: dict) -> None:
    for path, dest in _NESTED_ALIASES:
        val = _dig(root, path) if len(path) > 1 else root.get(path[0])
        if val is not None and str(val).strip() and dest not in flat:
            flat[dest] = _clip(val, 4000)


def s1_event_to_am_alert(body: dict) -> dict:
    """One SentinelOne-style event -> one Alertmanager alert dict."""
    if not isinstance(body, dict):
        raise PayloadError("SentinelOne payload must be a JSON object")
    flat = flatten_nested(body)
    _apply_aliases(flat, body)
    # Some integrations send dot keys at top level already
    for k, v in body.items():
        if isinstance(v, (str, int, float, bool)) and "." in k and k not in flat:
            flat[k] = _clip(v, 4000)

    alertname = (
        flat.get("alertname")
        or flat.get("rule.name")
        or flat.get("threatInfo.threatName")
        or body.get("alertName")
        or "SentinelOne detection"
    )[:200]
    severity = _map_severity(flat.get("severity") or flat.get("ruleInfo.severity") or body.get("severity") or "medium")
    instance = (flat.get("endpoint.name") or flat.get("agent.uuid") or flat.get("instance") or "")[:255]

    labels = dict(flat)
    labels["alertname"] = alertname
    labels["severity"] = severity
    labels["source"] = "sentinelone"
    if instance:
        labels["instance"] = instance

    annotations: dict[str, str] = {}
    for src_key, dest in (
        ("description", "description"),
        ("purpleAiSummary", "purple_ai_summary"),
        ("purple_ai_summary", "purple_ai_summary"),
        ("insight", "insight"),
        ("mitreTechniques", "mitre.technique"),
    ):
        val = body.get(src_key) or flat.get(src_key)
        if val:
            annotations[dest] = _clip(val, 4000)
    if body.get("indicators"):
        annotations["indicators_json"] = _clip(json.dumps(body["indicators"]), 4000)

    fp_src = (
        flat.get("id")
        or flat.get("threatInfo.threatId")
        or body.get("id")
        or json.dumps({"n": alertname, "i": instance, "t": flat.get("event.time")}, sort_keys=True)
    )
    fingerprint = hashlib.sha256(str(fp_src).encode()).hexdigest()[:16]

    starts = flat.get("event.time") or flat.get("createdAt") or flat.get("src.process.startTime")
    return {
        "status": "firing" if str(body.get("status", "firing")).lower() not in ("resolved", "closed") else "resolved",
        "labels": _str_map(labels),
        "annotations": _str_map(annotations),
        "startsAt": starts if isinstance(starts, str) else None,
        "fingerprint": fingerprint,
        "generatorURL": _clip(body.get("consoleUrl") or body.get("deepLink") or flat.get("alert.url"), 1000),
    }


def parse_sentinelone_payload(raw: bytes) -> tuple[dict, list[dict]]:
    """bytes -> (synthetic AM v4 payload, alerts). Accepts one event or {alerts|data|events: [...]}."""
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise PayloadError("body is not valid JSON") from None

    alerts_in: list[dict]
    if isinstance(body, list):
        alerts_in = body
    elif isinstance(body, dict):
        for key in ("alerts", "data", "events", "items"):
            if isinstance(body.get(key), list):
                alerts_in = body[key]
                break
        else:
            alerts_in = [body]
    else:
        raise PayloadError("payload must be a JSON object or array")

    if len(alerts_in) > 1000:
        raise PayloadError("too many events in one notification (limit 1000)")

    am_alerts = [s1_event_to_am_alert(a) for a in alerts_in]
    payload = {"version": "4", "receiver": "sentinelone", "status": "firing", "alerts": am_alerts}
    return payload, am_alerts
