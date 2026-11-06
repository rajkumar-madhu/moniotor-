# SentinelOne webhook → LinkedEye alerts

LinkedEye can ingest SentinelOne (and similar EDR) detections without Alertmanager in the middle.

## Endpoint

| URL | Auth |
|-----|------|
| `POST /api/v1/sentinelone/` | `Authorization: Bearer <token>` |

Token: `LINKEDEYE_SENTINELONE_WEBHOOK_TOKEN`, or the same value as `LINKEDEYE_ALERTMANAGER_WEBHOOK_TOKEN` if the SentinelOne-specific variable is unset.

## Payload shapes

Accepted JSON:

- A **single event object** (nested SentinelOne fields)
- `{ "events": [ ... ] }`, `{ "alerts": [ ... ] }`, `{ "data": [ ... ] }`, or a **top-level array**

Nested objects are flattened to dot keys (`sourceProcessInfo.name` → `src.process.displayName` where mapped). Pre-flattened Deep Visibility keys (`src.process.*`, `tgt.file.*`, `endpoint.name`, …) are stored as-is.

## Purple / Ops insight

Set either on the webhook body or in annotations after mapping:

| Field | Maps to |
|-------|---------|
| `purpleAiSummary` | `purple_ai_summary` annotation (shown in **Ops insight**) |
| `insight` | Same tab, fallback |
| `indicators` | JSON list → **Indicators** tab (also stored as `indicators_json`) |

## UI

**Monitoring → Alert Manager →** open an alert:

- **Overview** — Ops insight + collapsible field groups  
- **Indicators** — evasion/exploitation counts and MITRE techniques  
- **Graph** — endpoint → parent process → process → target file  
- **Timeline** / **Raw data**

## Example

```bash
curl -sS -X POST "https://linkedeye.example/api/v1/sentinelone/" \
  -H "Authorization: Bearer $LINKEDEYE_ALERTMANAGER_WEBHOOK_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "demo-1",
    "severity": "Medium",
    "agentRealtimeInfo": { "agentComputerName": "rbl-mum-le" },
    "threatInfo": { "threatName": "Log Rotation Config Tampering" },
    "sourceProcessInfo": { "name": "containerd" },
    "parentProcessInfo": { "name": "systemd" },
    "filePath": "/var/lib/containerd/tmpmounts/m1/etc/logrotate.d/alternatives",
    "purpleAiSummary": "Unauthorized logrotate.d write under containerd tmp mount.",
    "indicators": [{ "name": "Defense evasion", "count": 1 }]
  }'
```

Local demo: `manage.py seed_telemetry` includes example alert `Log Rotation Config Tampering`.

## Alertmanager path

You can still route SentinelOne → Alertmanager → `/api/v1/alertmanager/`; put dot-keys in labels/annotations. The dedicated mapper is only needed when Alertmanager is not in the path.
