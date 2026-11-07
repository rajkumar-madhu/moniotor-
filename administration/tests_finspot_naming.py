from django.test import SimpleTestCase

from administration.finspot_naming import (
    admin_lesites_url,
    entity_host,
    lesites_url,
    portal_host,
    portal_url,
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
        self.assertEqual(portal_host("fs-le-dev-finspot"), "fs-le-dev.finspot.in")
        self.assertEqual(portal_host("fs-le-isv"), "fs-le-isv.finspot.in")
        self.assertEqual(portal_host("fs-dx-le"), "fs-le-dx.finspot.in")

    def test_tool_hosts_prefix_pattern(self):
        self.assertEqual(tool_host("fs-dr-le", "grafana"), "grafana-fs-le-dr.finspot.in")
        self.assertEqual(tool_url("fs-dr-le", "grafana"), "https://grafana-fs-le-dr.finspot.in")
        self.assertEqual(tool_host("fs-dr-le", "prometheus"), "prometheus-fs-le-dr.finspot.in")
        self.assertEqual(entity_host("fs-dr-le"), "domain-fs-le-dr.finspot.in")
        self.assertEqual(
            websocket_url("fs-dr-le"),
            "wss://fs-le-dr.finspot.in/websocket/ws/",
        )
