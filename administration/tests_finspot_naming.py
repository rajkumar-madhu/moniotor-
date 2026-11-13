from django.test import SimpleTestCase

from administration.finspot_naming import (
    admin_lesites_url,
    device_environment_for_slug,
    entity_host,
    infer_slug_from_workbook_path,
    lesites_environment_for_slug,
    lesites_url,
    portal_host,
    portal_url,
    site_display_name,
    tool_host,
    tool_url,
    websocket_url,
)


class FinspotNamingTests(SimpleTestCase):
    def test_dr_site_matches_legacy_portal(self):
        self.assertEqual(portal_host("fs-dr-le"), "fs-le-dr.finspot.in")
        self.assertEqual(portal_url("fs-dr-le"), "https://fs-le-dr.finspot.in")
        self.assertEqual(
            lesites_url("fs-dr-le"),
            "https://fs-le-dr.finspot.in/lesites/?site=fs-dr-le",
        )
        self.assertEqual(
            admin_lesites_url("fs-dr-le"),
            "https://gle.finspot.in/lesites/?site=fs-dr-le",
        )

    def test_dev_site_seeds(self):
        self.assertEqual(portal_host("fs-le-dev-finspot"), "fs-le-dev1.finspot.in")
        self.assertEqual(portal_host("fs-le-isv"), "fs-le-isv.finspot.in")
        self.assertEqual(portal_host("fs-dx-le"), "fs-le-dx.finspot.in")

    def test_dev_workbook_and_client_labels(self):
        self.assertEqual(site_display_name("fs-le-dev-finspot"), "FinSpot Dev")
        self.assertEqual(lesites_environment_for_slug("fs-le-dev-finspot"), "DEV")
        self.assertEqual(device_environment_for_slug("fs-le-dev-finspot"), "dev")
        self.assertEqual(portal_url("fs-le-dev-finspot"), "https://fs-le-dev1.finspot.in")
        self.assertEqual(
            tool_host("fs-le-dev-finspot", "prometheus"),
            "fs-le-dev1-prometheus.finspot.in",
        )
        self.assertEqual(
            websocket_url("fs-le-dev-finspot"),
            "wss://fs-le-dev1.finspot.in/websocket/ws/",
        )
        self.assertEqual(
            infer_slug_from_workbook_path("/tmp/fs-le-dev_Restore_Devices.xlsx"),
            "fs-le-dev-finspot",
        )

    def test_dx_dr_site_labels(self):
        self.assertEqual(site_display_name("fs-dx-le"), "FinSpot DX DR")
        self.assertEqual(lesites_environment_for_slug("fs-dx-le"), "DX")
        self.assertEqual(device_environment_for_slug("fs-dx-le"), "dr")
        self.assertEqual(
            infer_slug_from_workbook_path("/tmp/fs-dx-le_Restore_Devices.xlsx"),
            "fs-dx-le",
        )

    def test_tool_hosts_prefix_pattern(self):
        self.assertEqual(tool_host("fs-dr-le", "grafana"), "grafana-fs-le-dr.finspot.in")
        self.assertEqual(tool_url("fs-dr-le", "grafana"), "https://grafana-fs-le-dr.finspot.in")
        self.assertEqual(tool_host("fs-dr-le", "prometheus"), "prometheus-fs-le-dr.finspot.in")
        self.assertEqual(entity_host("fs-dr-le"), "domain-fs-le-dr.finspot.in")
        self.assertEqual(
            websocket_url("fs-dr-le"),
            "wss://fs-le-dr.finspot.in/websocket/ws/",
        )
