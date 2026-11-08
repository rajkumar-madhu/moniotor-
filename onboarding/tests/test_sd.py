"""Prometheus http_sd: GET /api/sd/targets.json with a bearer token from the vault."""
import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, override_settings
from django.urls import reverse

from core import secrets
from onboarding import sd
from onboarding.models import Credential, OnboardingRun, OnboardingTarget, SDFetch

from .base import KEY, KEY2, OnboardingTestCase, with_key

URL = "/api/sd/targets.json"


def bearer(token):
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


@with_key
class SdEndpointTests(OnboardingTestCase):
    def setUp(self):
        super().setUp()
        self.root = get_user_model().objects.create_superuser("root-sd", "sd@example.com", "root-pass-123456")
        _row, self.token = sd.issue_token(self.root)
        self.run_ = self.make_run(site="Mumbai", environment="uat")
        self.ready = self.make_target(self.run_, "db-01", state="scraping", port=9100)
        self.live = self.make_target(self.run_, "web-01", state="live", port=9100, address="10.0.3.20")
        self.snmp = self.make_target(self.run_, "sw-1", state="exporter_found", port=9116, target_type="device")
        self.new = self.make_target(self.run_, "new-01", state="new")
        self.down = self.make_target(self.run_, "down-01", state="unreachable")
        self.held = self.make_target(self.run_, "held-01", state="scraping", enabled=False)
        self.anon = Client()

    def fetch(self, token=None, **extra):
        headers = bearer(token) if token else {}
        return self.anon.get(URL, **headers, **extra)

    def test_good_token_returns_http_sd_for_every_enabled_ready_target(self):
        resp = self.fetch(self.token)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "application/json")
        self.assertEqual(resp["Cache-Control"], "no-store")
        data = resp.json()
        self.assertIsInstance(data, list)
        got = {t["targets"][0]: t["labels"] for t in data}
        self.assertEqual(set(got), {"db-01:9100", "10.0.3.20:9100", "sw-1:9116"})
        for entry in data:
            self.assertEqual(len(entry["targets"]), 1)
            self.assertEqual(set(entry), {"targets", "labels"})

    def test_label_content(self):
        got = {t["targets"][0]: t["labels"] for t in self.fetch(self.token).json()}
        self.assertEqual(got["db-01:9100"], {"job": "node", "site": "Mumbai", "client": "NSE", "env": "uat",
                                             "le_target": str(self.ready.pk), "le_hostname": "db-01"})
        self.assertEqual(got["sw-1:9116"]["job"], "snmp")
        self.assertEqual(got["10.0.3.20:9100"]["le_hostname"], "web-01")
        for labels in got.values():
            self.assertTrue(all(isinstance(v, str) for v in labels.values()))

    def test_a_device_is_scraped_through_the_snmp_exporter_when_one_is_set(self):
        with mock.patch("onboarding.devices.exporter_url", return_value="http://snmp-exp.example:9116"):
            data = self.fetch(self.token).json()
        entry = next(t for t in data if t["labels"]["le_hostname"] == "sw-1")
        self.assertEqual(entry["targets"], ["snmp-exp.example:9116"])
        labels = entry["labels"]
        self.assertEqual((labels["job"], labels["instance"]), ("snmp", "sw-1"))
        self.assertEqual((labels["__metrics_path__"], labels["__scheme__"]), ("/snmp", "http"))
        self.assertEqual((labels["__param_target"], labels["__param_module"], labels["__param_auth"]),
                         ("sw-1", "if_mib", "public_v2"))
        # servers are untouched
        self.assertIn("db-01:9100", {t["targets"][0] for t in data})

    def test_no_secret_or_credential_detail_in_the_payload(self):
        cred = secrets.store("ssh_password", "svcuser", "payload-secret-xyz", "Mumbai", self.root, name="payload-cred")
        OnboardingTarget.objects.filter(pk=self.ready.pk).update(credential=cred)
        body = self.fetch(self.token).content.decode()
        for needle in ("payload-secret-xyz", "svcuser", "payload-cred", "gAAAAA", self.token):
            self.assertNotIn(needle, body)

    def test_run_without_a_client_has_an_empty_client_label(self):
        run = OnboardingRun.objects.create(site="Chennai")
        t = OnboardingTarget.objects.create(run=run, hostname="solo", state="live")
        got = {x["targets"][0]: x["labels"] for x in self.fetch(self.token).json()}
        self.assertEqual(got["solo:9100"]["client"], "")
        self.assertEqual(got["solo:9100"]["le_target"], str(t.pk))

    def test_failed_runs_are_not_served(self):
        OnboardingRun.objects.filter(pk=self.run_.pk).update(status="failed")
        self.assertEqual(self.fetch(self.token).json(), [])

    def test_a_disabled_target_leaves_and_rejoins(self):
        self.assertNotIn("held-01:9100", {t["targets"][0] for t in self.fetch(self.token).json()})
        OnboardingTarget.objects.filter(pk=self.held.pk).update(enabled=True)
        self.assertIn("held-01:9100", {t["targets"][0] for t in self.fetch(self.token).json()})

    def test_bad_missing_and_malformed_tokens_get_401_never_500(self):
        cases = [{}, bearer("wrong-token"), bearer(""), {"HTTP_AUTHORIZATION": "Bearer"},
                 {"HTTP_AUTHORIZATION": f"Basic {self.token}"}, {"HTTP_AUTHORIZATION": self.token},
                 bearer(self.token + "x"), bearer(self.token[:-1]), bearer("é" * 40),
                 bearer("A" * 5000)]
        for headers in cases:
            cache.clear()
            resp = self.anon.get(URL, **headers)
            self.assertEqual(resp.status_code, 401, headers)
            self.assertEqual(resp.json(), {"error": "Invalid or missing bearer token"})
            self.assertIn("Bearer", resp["WWW-Authenticate"])
            self.assertNotIn(self.token, resp.content.decode())

    def test_a_token_in_the_query_string_is_not_accepted(self):
        self.assertEqual(self.anon.get(URL, {"token": self.token, "access_token": self.token}).status_code, 401)

    def test_no_token_configured_is_a_401_not_a_500(self):
        Credential.objects.all().delete()
        self.assertEqual(self.fetch("anything").status_code, 401)

    def test_another_tenants_token_does_not_open_this_tenant(self):
        with override_settings(LINKEDEYE_TENANT="other"):
            _row, alien_token = sd.issue_token(self.root)
            OnboardingTarget.objects.create(run=OnboardingRun.objects.create(site="X", tenant_key="other"),
                                            hostname="alien-host", state="live")
            self.assertEqual(self.fetch(alien_token).status_code, 200)          # valid in its own tenant
            self.assertNotIn("db-01:9100", json.dumps(self.fetch(alien_token).json()))
        self.assertEqual(Credential.objects.filter(name=sd.TOKEN_NAME).count(), 2)
        resp = self.fetch(alien_token)                                           # this tenant
        self.assertEqual(resp.status_code, 401)
        body = self.fetch(self.token).content.decode()
        self.assertNotIn("alien-host", body)

    def test_the_other_tenant_only_ever_sees_its_own_targets(self):
        with override_settings(LINKEDEYE_TENANT="other"):
            _row, alien_token = sd.issue_token(self.root)
            OnboardingTarget.objects.create(run=OnboardingRun.objects.create(site="X", tenant_key="other"),
                                            hostname="alien-host", state="live")
            targets = [t["targets"][0] for t in self.fetch(alien_token).json()]
        self.assertEqual(targets, ["alien-host:9100"])

    def test_rotation_invalidates_the_old_token(self):
        self.assertEqual(self.fetch(self.token).status_code, 200)
        row, new = sd.issue_token(self.root)
        self.assertEqual(Credential.objects.filter(name=sd.TOKEN_NAME).count(), 1)
        self.assertNotEqual(new, self.token)
        self.assertIsNotNone(row.rotated_at)
        self.assertEqual(self.fetch(self.token).status_code, 401)
        self.assertEqual(self.fetch(new).status_code, 200)

    def test_every_fetch_is_recorded_with_time_source_and_count(self):
        self.fetch(self.token, REMOTE_ADDR="203.0.113.9", HTTP_USER_AGENT="Prometheus/2.53")
        self.fetch("nope", REMOTE_ADDR="203.0.113.10")
        ok, denied = SDFetch.objects.order_by("id")
        self.assertEqual((ok.result, ok.target_count, ok.source_ip, ok.user_agent), ("ok", 3, "203.0.113.9", "Prometheus/2.53"))
        self.assertEqual((denied.result, denied.target_count), ("denied", 0))
        self.assertIsNotNone(ok.fetched_at)
        self.assertEqual(ok.tenant_key, "default")

    def test_rate_limit_throttles_a_flooding_source_with_429(self):
        with mock.patch.object(sd, "RATE_LIMIT", 3):
            codes = [self.fetch(self.token, REMOTE_ADDR="198.51.100.7").status_code for _ in range(6)]
        self.assertEqual(codes[:3], [200, 200, 200])
        self.assertEqual(set(codes[3:]), {429})
        self.assertTrue(SDFetch.objects.filter(result="throttled").exists())
        # another source is unaffected
        self.assertEqual(self.fetch(self.token, REMOTE_ADDR="198.51.100.8").status_code, 200)

    def test_a_broken_cache_does_not_block_discovery(self):
        with mock.patch("onboarding.sd.cache.add", side_effect=RuntimeError("cache down")):
            self.assertEqual(self.fetch(self.token).status_code, 200)

    def test_methods_other_than_get_are_refused(self):
        for method in ("post", "put", "delete", "patch"):
            resp = getattr(self.anon, method)(URL, **bearer(self.token))
            self.assertEqual(resp.status_code, 405, method)
            self.assertIn("GET", resp["Allow"])
        self.assertEqual(self.anon.head(URL, **bearer(self.token)).status_code, 200)

    def test_an_internal_error_is_a_503_not_a_traceback(self):
        with mock.patch.object(sd, "targets_payload", side_effect=RuntimeError("db exploded " + self.token)):
            resp = self.fetch(self.token)
        self.assertEqual(resp.status_code, 503)
        self.assertNotIn(self.token, resp.content.decode())
        self.assertNotIn("exploded", resp.content.decode())

    def test_csrf_does_not_apply_to_the_machine_endpoint(self):
        strict = Client(enforce_csrf_checks=True)
        self.assertEqual(strict.get(URL, **bearer(self.token)).status_code, 200)

    def test_a_key_that_cannot_decrypt_gives_503_with_no_detail(self):
        with override_settings(LINKEDEYE_CREDENTIAL_KEY=KEY2):
            resp = self.fetch(self.token)
        self.assertEqual(resp.status_code, 503)
        self.assertNotIn(KEY, resp.content.decode())
        with override_settings(LINKEDEYE_CREDENTIAL_KEY=""):
            self.assertEqual(self.fetch(self.token).status_code, 503)

    def test_the_old_key_still_works_after_a_rotation_of_the_vault_key(self):
        with override_settings(LINKEDEYE_CREDENTIAL_KEY=f"{KEY2},{KEY}"):
            self.assertEqual(self.fetch(self.token).status_code, 200)

    def test_the_token_is_stored_encrypted_in_the_vault(self):
        row = sd.token_row()
        self.assertEqual(row.kind, "bearer")
        self.assertTrue(row.secret.startswith("gAAAAA"))
        self.assertNotIn(self.token, row.secret)


@with_key
class SdPageTests(OnboardingTestCase):
    def setUp(self):
        super().setUp()
        self.root = get_user_model().objects.create_superuser("root-pg", "pg@example.com", "root-pass-123456")
        self.c.force_login(self.root)

    def test_scrape_config_has_the_exact_block_and_the_token_only_once(self):
        page = self.c.get(reverse("onboarding:sd_page"))
        self.assertContains(page, "http_sd_configs:")
        self.assertContains(page, "/api/sd/targets.json")
        self.assertContains(page, "&lt;paste the token shown when you generated it&gt;")
        resp = self.c.post(reverse("onboarding:sd_token"))
        token = resp.context["fresh_token"]
        block = resp.context["scrape_config"]
        self.assertIn(f"credentials: {token}", block)
        self.assertIn("job_name: linkedeye-sd", block)
        self.assertIn("type: Bearer", block)
        self.assertIn("refresh_interval: 60s", block)
        try:
            import yaml
        except ImportError:          # PyYAML is not a runtime dependency; parse only where it is installed
            yaml = None
        if yaml is not None:
            cfg = yaml.safe_load(block)["scrape_configs"][0]
            self.assertEqual(cfg["http_sd_configs"][0]["authorization"], {"type": "Bearer", "credentials": token})
            self.assertTrue(cfg["http_sd_configs"][0]["url"].endswith("/api/sd/targets.json"))
        again = self.c.get(reverse("onboarding:sd_page"))
        self.assertNotIn(token, again.content.decode())

    def test_the_page_lists_targets_served_and_recorded_fetches(self):
        run = self.make_run()
        self.make_target(run, "db-01", state="live")
        SDFetch.objects.create(source_ip="203.0.113.5", target_count=1, result="ok")
        body = self.c.get(reverse("onboarding:sd_page")).content.decode()
        self.assertIn("db-01:9100", body)
        self.assertIn("203.0.113.5", body)

    def test_rotating_twice_keeps_one_row(self):
        for _ in range(2):
            self.c.post(reverse("onboarding:sd_token"))
        self.assertEqual(Credential.objects.filter(name=sd.TOKEN_NAME).count(), 1)

    def test_prune_drops_only_old_fetches(self):
        from datetime import timedelta

        from django.utils import timezone

        from onboarding import services
        old = SDFetch.objects.create(target_count=1)
        SDFetch.objects.filter(pk=old.pk).update(fetched_at=timezone.now() - timedelta(days=45))
        SDFetch.objects.create(target_count=2)
        self.assertEqual(services.prune_fetches(30), 1)
        self.assertEqual(SDFetch.objects.count(), 1)


@with_key
class SdThrottleRecordTests(OnboardingTestCase):
    def test_a_flood_writes_one_throttled_record_per_window(self):
        root = get_user_model().objects.create_superuser("root-th", "th@example.com", "root-pass-123456")
        _row, token = sd.issue_token(root)
        with mock.patch.object(sd, "RATE_LIMIT", 1):
            for _ in range(8):
                Client().get(URL, **bearer(token), REMOTE_ADDR="192.0.2.50")
        self.assertEqual(SDFetch.objects.filter(result="throttled").count(), 1)
        self.assertEqual(SDFetch.objects.filter(result="ok").count(), 1)
