"""Verify / enable dispatch for network devices, Prometheus servers, Kubernetes nodes and URL services,
and the four-column pasted list. Every probe is mocked: nothing opens a socket."""
from unittest import mock

from onboarding import others, services, steps
from onboarding.models import TargetState

from .base import OnboardingTestCase


class DispatchTests(OnboardingTestCase):
    def setUp(self):
        super().setUp()
        self.run_ = self.make_run(site="Mumbai")

    def test_verify_routes_each_type_to_its_owner(self):
        cases = {"device": "onboarding.devices.verify_device",
                 "prometheus": "onboarding.prometheus_step.verify_prometheus",
                 "k8s_node": "onboarding.servers.verify", "url": "onboarding.servers.verify",
                 "linux": "onboarding.servers.verify"}
        for ttype, path in cases.items():
            target = self.make_target(self.run_, f"h-{ttype}", target_type=ttype)
            with mock.patch(path, return_value={"state": "exporter_found"}) as hook:
                self.assertEqual(steps.verify_target(target)["state"], "exporter_found", ttype)
            hook.assert_called_once_with(target)

    def test_no_type_is_left_pending(self):
        for ttype in ("device", "prometheus", "k8s_node", "url", "linux", "windows"):
            target = self.make_target(self.run_, f"p-{ttype}", target_type=ttype)
            with mock.patch("onboarding.servers.verify", return_value={}), \
                    mock.patch("onboarding.servers.enable", return_value={}), \
                    mock.patch("onboarding.devices.verify_device", return_value={}), \
                    mock.patch("onboarding.prometheus_step.verify_prometheus", return_value={}):
                steps.verify_target(target)
                steps.enable_collection(target, {})        # never raises StepPending

    def test_enable_device_saves_the_module_and_returns_a_secret_free_snippet(self):
        target = self.make_target(self.run_, "sw-1", target_type="device", port=9116)
        seen = {}

        def fake_verify(t):
            seen["module"] = t.facts["snmp_settings"]["module"]
            return {"state": TargetState.EXPORTER_FOUND, "exporter": "snmp_exporter"}

        with mock.patch("onboarding.devices.verify_device", side_effect=fake_verify):
            out = steps.enable_collection(target, {"snmp_module": "cisco_ios"})
        self.assertEqual(seen["module"], "cisco_ios")
        self.assertEqual(out["state"], TargetState.EXPORTER_FOUND)
        self.assertIn("auths:", out["snmp_snippet"])
        self.assertIn("<community string>", out["snmp_snippet"])
        self.assertIn("cisco_ios", out["note"])

    def test_enable_device_ignores_an_unsafe_module_name(self):
        target = self.make_target(self.run_, "sw-2", target_type="device", port=9116)
        with mock.patch("onboarding.devices.verify_device", return_value={"state": TargetState.UNREACHABLE,
                                                                          "error": "no"}) as hook:
            out = steps.enable_collection(target, {"snmp_module": "x&auth=evil"})
        self.assertEqual(hook.call_args[0][0].facts["snmp_settings"]["module"], "if_mib")
        self.assertIn("ignored", out["note"])
        self.assertEqual(out["state"], TargetState.UNREACHABLE)

    def test_kubernetes_node_gets_the_helm_commands_and_a_url_service_does_not(self):
        node = self.make_target(self.run_, "n1", target_type="k8s_node", port=9100)
        svc = self.make_target(self.run_, "api", target_type="url", port=9100)
        with mock.patch("onboarding.servers.verify", side_effect=lambda t: {"state": TargetState.REACHABLE}):
            k = others.enable(node, {})
            u = others.enable(svc, {})
        self.assertIn("kube-state-metrics", k["install_command"])
        self.assertNotIn("install_command", u)
        self.assertIn("does not expose", u["note"])

    def test_enable_prometheus_only_checks_the_api(self):
        prom = self.make_target(self.run_, "prom", target_type="prometheus", port=9090)
        with mock.patch("onboarding.prometheus_step.verify_prometheus",
                        return_value={"state": TargetState.EXPORTER_FOUND}):
            out = others.enable(prom, {})
        self.assertIn("nothing to install", out["note"])


class PasteTests(OnboardingTestCase):
    def test_four_columns_without_a_header_are_host_address_type_port(self):
        rows, bad = services.parse_target_list("db-01,,linux,9100\nsw,10.0.0.5,device,9116\nx", "linux")
        self.assertEqual(bad, [])
        self.assertEqual([(r["hostname"], r["address"], r["target_type"], r["port"]) for r in rows],
                         [("db-01", "", "linux", 9100), ("sw", "10.0.0.5", "device", 9116), ("x", "", "linux", 9100)])
