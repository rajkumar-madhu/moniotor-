"""Monitoring onboarding checklist and the Prometheus discovery settings on the Integrations page."""
from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from core.models import Membership
from telemetry.models import Alert, DiscoveredTarget, DiscoveryRun, TargetKind

from . import integration_config as ic
from . import services

PW = "Corr3ct-Horse-Battery-77"
FAST_HASHER = override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])


def steps(checklist):
    return {s["key"]: s for s in checklist["steps"]}


@FAST_HASHER
class ChecklistTests(TestCase):
    def setUp(self):
        ic.invalidate_cache()
        self.addCleanup(ic.invalidate_cache)

    def _host(self, instance, linked=False, **kw):
        ci = None
        if linked:
            from django.apps import apps
            ci = apps.get_model("cmdb", "ConfigurationItem").objects.create(
                name=instance, site="All Sites", prometheus_instance=instance)
        return DiscoveredTarget.objects.create(instance=instance, job="node", kind=TargetKind.NODE, ci=ci, **kw)

    @override_settings(PROMETHEUS_URL="")
    def test_nothing_configured(self):
        c = services.onboarding_checklist()
        self.assertEqual(c["done"], 0)
        self.assertEqual(c["total"], 6)
        self.assertFalse(c["complete"])
        self.assertEqual(c["pct"], 0)
        s = steps(c)
        self.assertFalse(s["prometheus"]["done"])
        self.assertIn("Set the server URL", s["prometheus"]["detail"])
        self.assertIn("Connect Prometheus first", s["targets"]["detail"])

    @override_settings(PROMETHEUS_URL="http://prom.test:9090")
    def test_prometheus_connected_but_nothing_discovered(self):
        s = steps(services.onboarding_checklist())
        self.assertTrue(s["prometheus"]["done"])
        self.assertFalse(s["targets"]["done"])
        self.assertIn("has not found a target yet", s["targets"]["detail"])
        self.assertFalse(s["cmdb"]["done"])

    @override_settings(PROMETHEUS_URL="http://prom.test:9090")
    def test_targets_discovered_but_not_in_the_cmdb(self):
        self._host("a:9100")
        self._host("b:9100")
        s = steps(services.onboarding_checklist())
        self.assertTrue(s["targets"]["done"])
        self.assertIn("2 target(s)", s["targets"]["detail"])
        self.assertFalse(s["cmdb"]["done"])
        self.assertEqual(s["cmdb"]["detail"], "0 of 2 discovered host(s)")

    @override_settings(PROMETHEUS_URL="http://prom.test:9090")
    def test_cmdb_step_completes_when_every_host_is_linked(self):
        self._host("a:9100", linked=True)
        self._host("b:9100", linked=True)
        s = steps(services.onboarding_checklist())
        self.assertTrue(s["cmdb"]["done"])
        self.assertEqual(s["cmdb"]["detail"], "2 of 2 discovered host(s)")

    @override_settings(PROMETHEUS_URL="http://prom.test:9090")
    def test_ignored_and_missing_hosts_do_not_block_the_cmdb_step(self):
        self._host("a:9100", linked=True)
        self._host("ignored:9100", state=DiscoveredTarget.IGNORED)
        s = steps(services.onboarding_checklist())
        self.assertTrue(s["cmdb"]["done"])

    @override_settings(PROMETHEUS_URL="http://prom.test:9090", ALERTMANAGER_WEBHOOK_TOKEN="tok-1234567890abcdef")
    def test_alertmanager_step(self):
        s = steps(services.onboarding_checklist())
        self.assertTrue(s["alertmanager"]["done"])
        self.assertIn("Bearer token set", s["alertmanager"]["detail"])

    @override_settings(PROMETHEUS_URL="http://prom.test:9090", NOTIFY_WEBHOOK_URLS=["https://hooks.example/a"],
                       NOTIFY_SEVERITIES=["critical"])
    def test_notification_channel_step(self):
        s = steps(services.onboarding_checklist())
        self.assertTrue(s["channels"]["done"])

    @override_settings(PROMETHEUS_URL="http://prom.test:9090")
    def test_first_alert_step(self):
        s = steps(services.onboarding_checklist())
        self.assertFalse(s["alert"]["done"])
        Alert.objects.create(fingerprint="f1", alertname="HostDown", instance="a:9100")
        s = steps(services.onboarding_checklist())
        self.assertTrue(s["alert"]["done"])
        self.assertIsNotNone(s["alert"]["when"])

    @override_settings(PROMETHEUS_URL="http://prom.test:9090", ALERTMANAGER_WEBHOOK_TOKEN="tok-1234567890abcdef",
                       NOTIFY_WEBHOOK_URLS=["https://hooks.example/a"], NOTIFY_SEVERITIES=["critical"])
    def test_everything_done(self):
        self._host("a:9100", linked=True)
        Alert.objects.create(fingerprint="f1", alertname="HostDown", instance="a:9100")
        c = services.onboarding_checklist()
        self.assertTrue(c["complete"])
        self.assertEqual(c["done"], 6)
        self.assertEqual(c["pct"], 100)

    @override_settings(PROMETHEUS_URL="http://prom.test:9090")
    def test_every_step_links_somewhere(self):
        for s in services.onboarding_checklist()["steps"]:
            self.assertTrue(s["url"], s["key"])
            self.assertTrue(s["cta"], s["key"])

    def test_another_tenants_rows_are_not_counted(self):
        DiscoveredTarget.objects.create(tenant_key="other", instance="x:9100", job="node", kind=TargetKind.NODE)
        self.assertEqual(services.discovery_status()["targets"], 0)


@FAST_HASHER
class DiscoveryStatusTests(TestCase):
    def test_summary_without_a_run(self):
        st = services.discovery_status()
        self.assertTrue(st["available"])
        self.assertEqual(st["summary"], "None yet")
        self.assertEqual(st["when"], "Never")

    def test_summary_with_a_run(self):
        DiscoveredTarget.objects.create(instance="a:9100", job="node", kind=TargetKind.NODE)
        run = DiscoveryRun.objects.create(ok=True, targets=1, up=1)
        st = services.discovery_status()
        self.assertEqual(st["last_run"].pk, run.pk)
        self.assertIn("1 target(s)", st["summary"])
        self.assertEqual(st["when"], run.started_at)
        self.assertEqual(st["error"], "")

    def test_a_failed_run_is_reported(self):
        DiscoveryRun.objects.create(ok=False, error="Prometheus unreachable")
        self.assertEqual(services.discovery_status()["error"], "Prometheus unreachable")


@FAST_HASHER
class IntegrationSettingsTests(TestCase):
    def setUp(self):
        ic.invalidate_cache()
        self.addCleanup(ic.invalidate_cache)
        self.admin = User.objects.create_superuser("root2", "r@example.com", PW)
        self.c = Client()
        self.c.force_login(self.admin)

    def test_defaults(self):
        self.assertEqual(ic.prometheus_site_label(), "site")
        self.assertFalse(ic.prometheus_autocreate_ci())
        self.assertFalse(ic.prometheus_autocreate_devices())
        self.assertFalse(ic.prometheus_autoregister_exporters())

    def test_saving_discovery_settings(self):
        r = self.c.post(reverse("administration:integration_save", args=["prometheus"]),
                        {"url": "http://prom.test:9090", "timeout": "8", "site_label": "dc",
                         "autocreate_ci": "on", "autoregister_exporters": "on"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(ic.prometheus_site_label(), "dc")
        self.assertTrue(ic.prometheus_autocreate_ci())
        self.assertFalse(ic.prometheus_autocreate_devices())
        self.assertTrue(ic.prometheus_autoregister_exporters())

    def test_unchecking_a_flag_turns_it_off(self):
        self.c.post(reverse("administration:integration_save", args=["prometheus"]),
                    {"url": "http://prom.test:9090", "autocreate_ci": "on"})
        self.assertTrue(ic.prometheus_autocreate_ci())
        self.c.post(reverse("administration:integration_save", args=["prometheus"]),
                    {"url": "http://prom.test:9090"})
        self.assertFalse(ic.prometheus_autocreate_ci())

    def test_an_invalid_label_name_is_refused(self):
        self.c.post(reverse("administration:integration_save", args=["prometheus"]),
                    {"url": "http://prom.test:9090", "site_label": "not a label!"})
        self.assertEqual(ic.prometheus_site_label(), "site")

    @override_settings(PROMETHEUS_URL="http://prom.test:9090")
    def test_integrations_page_shows_the_checklist_and_discover_button(self):
        r = self.c.get(reverse("administration:integrations"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Monitoring Onboarding")
        self.assertContains(r, "Targets discovered")
        self.assertContains(r, "Discover now")
        self.assertEqual(r.context["checklist"]["total"], 6)
        prom = next(i for i in r.context["items"] if i["key"] == "prometheus")
        self.assertTrue(prom["discover"])
        self.assertIn(("Site label", "site"), prom["details"])
        self.assertIn(("Auto-create", "Off"), prom["details"])

    @override_settings(PROMETHEUS_URL="")
    def test_discover_button_is_hidden_without_prometheus(self):
        r = self.c.get(reverse("administration:integrations"))
        prom = next(i for i in r.context["items"] if i["key"] == "prometheus")
        self.assertFalse(prom["discover"])

    def test_a_non_admin_cannot_open_integrations(self):
        user = User.objects.create_user("plain", "p@example.com", PW)
        Membership.objects.create(user=user, site=None, role="operator")
        c = Client()
        c.force_login(user)
        self.assertNotEqual(c.get(reverse("administration:integrations")).status_code, 200)


@FAST_HASHER
class SiteRenameTests(TestCase):
    """DiscoveredTarget.site must be registered in SITE_NAME_FIELDS, or a site rename leaves it behind."""

    def test_discovered_target_is_registered(self):
        self.assertIn(("telemetry", "DiscoveredTarget", "site", "Discovered Prometheus targets"),
                      services.SITE_NAME_FIELDS)

    def test_site_references_counts_discovered_targets(self):
        DiscoveredTarget.objects.create(instance="a:9100", job="node", site="Mumbai")
        labels = dict(services.site_references("Mumbai"))
        self.assertEqual(labels.get("Discovered Prometheus targets"), 1)
