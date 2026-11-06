import json

from django.test import Client, TestCase, override_settings

from telemetry.models import Alert
from telemetry.s1_mapper import parse_sentinelone_payload, s1_event_to_am_alert
from telemetry.tests import TOKEN


class SentinelOneMapperTests(TestCase):
    def test_nested_threat_maps_to_flat_labels(self):
        body = {
            "id": "evt-99",
            "severity": "Medium",
            "agentRealtimeInfo": {"agentComputerName": "rbl-mum-le"},
            "agentDetectionInfo": {"agentId": "agent-uuid-1", "accountName": "Religare Broking Limited"},
            "threatInfo": {"threatName": "Log Rotation Config Tampering"},
            "sourceProcessInfo": {"name": "containerd", "commandline": "/usr/bin/containerd"},
            "parentProcessInfo": {"name": "systemd", "pid": 1},
            "filePath": "/var/lib/containerd/tmpmounts/m1/etc/logrotate.d/alternatives",
            "purpleAiSummary": "Suspicious logrotate change under containerd mount.",
            "indicators": [{"name": "Config tampering", "count": 1}],
        }
        am = s1_event_to_am_alert(body)
        labels = am["labels"]
        self.assertEqual(labels["endpoint.name"], "rbl-mum-le")
        self.assertEqual(labels["src.process.displayName"], "containerd")
        self.assertEqual(labels["src.process.parent.displayName"], "systemd")
        self.assertEqual(labels["tgt.file.path"], body["filePath"])
        self.assertEqual(labels["source"], "sentinelone")
        self.assertEqual(am["annotations"]["purple_ai_summary"], body["purpleAiSummary"])
        self.assertIn("indicators_json", am["annotations"])

    def test_webhook_payload_ingest_shape(self):
        raw = json.dumps({"events": [{"id": "a1", "severity": "high", "threatInfo": {"threatName": "X"}}]}).encode()
        payload, alerts = parse_sentinelone_payload(raw)
        self.assertEqual(payload["receiver"], "sentinelone")
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["labels"]["severity"], "critical")

    def test_flat_dot_keys_preserved(self):
        am = s1_event_to_am_alert({
            "endpoint.name": "host1",
            "src.process.indicatorEvasionCount": "2",
            "alertname": "Custom rule",
        })
        self.assertEqual(am["labels"]["endpoint.name"], "host1")
        self.assertEqual(am["labels"]["src.process.indicatorEvasionCount"], "2")

    @override_settings(ALERTMANAGER_WEBHOOK_TOKEN=TOKEN, SENTINELONE_WEBHOOK_TOKEN="")
    def test_http_webhook_creates_alert(self):
        body = {"id": "wh1", "severity": "low", "threatInfo": {"threatName": "S1 Test"}}
        r = Client().post(
            "/api/v1/sentinelone/",
            data=json.dumps(body),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {TOKEN}",
        )
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["mapped"], 1)
        self.assertTrue(Alert.objects.filter(alertname="S1 Test", labels__source="sentinelone").exists())
