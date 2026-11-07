from django.test import TestCase

from telemetry.alert_insight import alert_detail_context, group_fields
from telemetry.models import Alert


class AlertInsightTests(TestCase):
    def test_log_rotation_style_grouping_and_summary(self):
        labels = {
            "alertname": "LogRotationConfigTampering",
            "severity": "medium",
            "account.name": "Religare Broking Limited",
            "endpoint.name": "rbl-mum-le",
            "agent.uuid": "bb994181-c955-2cc7-0c2e-e4c5ec4bf392",
            "src.process.displayName": "containerd",
            "src.process.cmdline": "/usr/bin/containerd",
            "src.process.image.sha256": "1ac5a10ab0ba5d254118018799969e0c9952e4a907fdbbe4b998223a58f3183e",
            "src.process.parent.displayName": "systemd",
            "src.process.parent.pid": "1",
            "tgt.file.path": "/var/lib/containerd/tmpmounts/containerd-mount1896012141/etc/logrotate.d/alternatives",
            "tgt.file.name": "alternatives",
            "task.path": "/var/lib/containerd/tmpmounts/containerd-mount1896012141/etc/logrotate.d/alternatives",
        }
        a = Alert.objects.create(
            fingerprint="edr1", alertname="Log Rotation Config Tampering", severity="medium",
            summary="Tampering with log rotation configuration files",
            description="", labels=labels, annotations={},
        )
        ctx = alert_detail_context(a)
        titles = [s["title"] for s in ctx["detail_sections"]]
        self.assertIn("Endpoint details", titles)
        self.assertIn("Target file details", titles)
        self.assertIn("Source process details", titles)
        self.assertIn("Source process parent details", titles)
        self.assertIn("containerd", ctx["insight_summary"])
        self.assertIn("rbl-mum-le", ctx["insight_summary"])
        self.assertIn("Religare", ctx["context_path"])
        labels["src.process.indicatorEvasionCount"] = "2"
        a.labels = labels
        ctx2 = alert_detail_context(a)
        self.assertTrue(any(i["label"] == "Evasion" for i in ctx2["indicators"]))
        self.assertTrue(ctx2["attack_graph"]["has_graph"])

    def test_purple_annotation_preferred(self):
        a = Alert.objects.create(
            fingerprint="p1", alertname="X", labels={"endpoint.name": "host"},
            annotations={"purple_ai_summary": "Custom narrative from the EDR platform."},
        )
        self.assertEqual(alert_detail_context(a)["insight_summary"], "Custom narrative from the EDR platform.")
