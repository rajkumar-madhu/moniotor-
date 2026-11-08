"""The Logs step: the Vector configuration, the token handling and the arrival check."""
import logging
import tomllib
from datetime import timedelta
from pathlib import Path

from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import AuditEvent
from observe.models import LogEntry
from onboarding import logs, steps
from onboarding.models import StepLog, TargetType

from .base import OnboardingTestCase, with_key

TOKEN = "le-log-token-Zq81xYp-uniq-4f2a"
HOSTILE_TOKEN = 'tok"en$with${VAR}\\and-more-9Zq'


def url(run, name="logs"):
    return reverse("onboarding:step", args=[run.pk, name])


def entry(host, minutes_ago=1, **kw):
    now = timezone.now() - timedelta(minutes=minutes_ago)
    kw.setdefault("site", "Mumbai")
    kw.setdefault("source", "sshd")
    kw.setdefault("level", "INFO")
    kw.setdefault("message", "Accepted publickey for svc")
    return LogEntry.objects.create(host=host, ts=now, received_at=now, tenant_key="default", **kw)


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())
        if record.exc_info:
            self.lines.append(logging.Formatter().formatException(record.exc_info))


@override_settings(LOG_INGEST_TOKEN=TOKEN, LINKEDEYE_PORTAL_URL="https://console.example.test/welcome/")
@with_key
class ConfigTests(OnboardingTestCase):
    def setUp(self):
        super().setUp()
        self.run_ = self.make_run(step="logs")

    def cfg(self, target, os_name=""):
        if os_name:
            target.facts = {"os": os_name}
        return logs.logs_config(target)

    def test_linux_config_is_prefilled_and_valid_toml(self):
        t = self.make_target(self.run_, "db-01.corp.local", target_type=TargetType.LINUX)
        out = self.cfg(t, "Ubuntu 22.04.4 LTS")
        self.assertEqual(out["shipper"], "vector")
        text = out["config"]
        parsed = tomllib.loads(text)
        sink = parsed["sinks"]["linkedeye"]
        self.assertEqual(sink["uri"], "https://console.example.test/api/v1/logs/")
        self.assertEqual(sink["auth"], {"strategy": "bearer", "token": TOKEN})
        self.assertEqual(sink["encoding"]["codec"], "json")
        self.assertEqual(parsed["sources"]["le_journald"]["type"], "journald")
        self.assertIn("/var/log/syslog", parsed["sources"]["le_files"]["include"])
        self.assertIn('"host": "db-01.corp.local"', parsed["transforms"]["le_shape"]["source"])
        self.assertIn('"site": "Mumbai"', parsed["transforms"]["le_shape"]["source"])
        self.assertEqual(out["token"], TOKEN)
        self.assertFalse(out["verified"])
        self.assertEqual(out["lines"], 0)

    def test_rhel_family_reads_messages_and_secure(self):
        t = self.make_target(self.run_, "db-02")
        files = tomllib.loads(self.cfg(t, "Rocky Linux 9.3")["config"])["sources"]["le_files"]["include"]
        self.assertIn("/var/log/messages", files)
        self.assertIn("/var/log/secure", files)
        self.assertNotIn("/var/log/syslog", files)

    def test_windows_device_and_kubernetes_variants(self):
        win = self.make_target(self.run_, "win-01", target_type=TargetType.WINDOWS)
        dev = self.make_target(self.run_, "sw-core-1", target_type=TargetType.DEVICE)
        k8s = self.make_target(self.run_, "node-1", target_type=TargetType.K8S_NODE)
        w = tomllib.loads(self.cfg(win)["config"])
        self.assertIn("C:/inetpub/logs/LogFiles/**/*.log", w["sources"]["le_files"]["include"])
        d = tomllib.loads(self.cfg(dev)["config"])
        self.assertEqual(d["sources"]["le_syslog"]["type"], "syslog")
        self.assertIn('string(.hostname) ?? "sw-core-1"', d["transforms"]["le_shape"]["source"])
        k = tomllib.loads(self.cfg(k8s)["config"])
        self.assertEqual(k["sources"]["le_k8s"]["type"], "kubernetes_logs")
        for parsed in (w, d, k):
            self.assertEqual(parsed["sinks"]["linkedeye"]["auth"]["token"], TOKEN)

    def test_hostile_values_cannot_break_the_file(self):
        site = self.site_a
        site.name = 'Mum"bai${HOME}\\\nx'
        site.save()
        run = self.make_run(site=site.name)
        t = self.make_target(run, "db-03")
        with override_settings(LOG_INGEST_TOKEN=HOSTILE_TOKEN):
            text = logs.logs_config(t)["config"]
        parsed = tomllib.loads(text)
        # Vector turns "$$" into "$"; the file never contains a live ${...} interpolation
        self.assertNotIn("${", text.replace("$${", ""))
        self.assertEqual(parsed["sinks"]["linkedeye"]["auth"]["token"], HOSTILE_TOKEN.replace("$", "$$"))
        self.assertEqual(len(parsed["transforms"]["le_shape"]["source"].splitlines()),
                         len(tomllib.loads(logs.logs_config(self.make_target(self.run_, "db-04"))["config"])
                             ["transforms"]["le_shape"]["source"].splitlines()))

    def test_url_and_prometheus_targets_have_no_shipper(self):
        for kind, host in ((TargetType.URL, "shop.example.test"), (TargetType.PROMETHEUS, "prom.example.test")):
            t = self.make_target(self.run_, host, target_type=kind)
            out = logs.logs_config(t)
            self.assertFalse(out["applicable"])
            self.assertEqual(out["config"], "")
            self.assertNotIn("token", out)
            self.assertTrue(out["note"])

    @override_settings(LOG_INGEST_TOKEN="")
    def test_ingest_disabled_gives_a_clear_error_and_no_config(self):
        t = self.make_target(self.run_, "db-05")
        out = logs.logs_config(t)
        self.assertEqual(out["config"], "")
        self.assertNotIn("token", out)
        self.assertIn("LINKEDEYE_LOG_INGEST_TOKEN", out["error"])

    @override_settings(LINKEDEYE_PORTAL_URL="")
    def test_without_a_portal_url_the_address_is_a_flagged_placeholder(self):
        out = logs.logs_config(self.make_target(self.run_, "db-06"))
        self.assertIn(logs.PLACEHOLDER_URL, out["config"])
        self.assertIn("LINKEDEYE_PORTAL_URL", out["note"])

    def test_the_hook_is_wired_into_steps(self):
        t = self.make_target(self.run_, "db-07")
        self.assertEqual(steps.logs_config(t)["shipper"], "vector")


@override_settings(LOG_INGEST_TOKEN=TOKEN, LINKEDEYE_PORTAL_URL="https://console.example.test/")
@with_key
class ArrivalTests(OnboardingTestCase):
    def setUp(self):
        super().setUp()
        self.run_ = self.make_run(step="logs")
        self.t = self.make_target(self.run_, "db-01.corp.local")

    def test_nothing_yet_is_waiting(self):
        out = logs.logs_config(self.t)
        self.assertFalse(out["verified"])
        self.assertEqual(out["lines"], 0)
        self.assertEqual(logs.status_for(self.t)["samples"], [])

    def test_lines_in_the_last_five_minutes_verify(self):
        entry("db-01.corp.local", 1, message="first line")
        entry("DB-01.CORP.LOCAL", 2, message="second line", level="ERROR")
        entry("db-01", 3, message="short name line")
        out = logs.logs_config(self.t)
        self.assertTrue(out["verified"])
        self.assertEqual(out["lines"], 3)
        st = logs.status_for(self.t)
        self.assertEqual(len(st["samples"]), 3)
        self.assertEqual(st["samples"][0]["message"], "first line")
        self.assertTrue(out["last_seen"])

    def test_old_lines_other_hosts_other_sites_and_other_tenants_do_not_count(self):
        entry("db-01.corp.local", 30)                                 # too old
        entry("db-99", 1)                                             # another host
        entry("db-01.corp.local", 1, site="Chennai")                  # another site's label
        row = entry("db-01.corp.local", 1)
        LogEntry.objects.filter(pk=row.pk).update(tenant_key="other")  # another tenant
        out = logs.logs_config(self.t)
        self.assertFalse(out["verified"])
        self.assertEqual(out["lines"], 0)

    def test_lines_without_a_site_label_still_count(self):
        entry("db-01.corp.local", 1, site="")
        self.assertTrue(logs.logs_config(self.t)["verified"])

    def test_nearby_hosts_hint_when_the_label_differs(self):
        entry("db-1", 1)
        entry("db-1", 1)
        entry("web-09", 1)
        entry("elsewhere", 1, site="Chennai")
        st = logs.status_for(self.t)
        self.assertFalse(st["arriving"])
        self.assertEqual(st["nearby"], ["db-1", "web-09"])

    def test_the_token_in_a_log_line_is_redacted_in_samples(self):
        entry("db-01.corp.local", 1, message=f"curl -H 'Authorization: Bearer {TOKEN}' https://x")
        sample = logs.status_for(self.t)["samples"][0]["message"]
        self.assertNotIn(TOKEN, sample)
        self.assertIn("[redacted]", sample)

    def test_the_query_count_does_not_grow_with_the_number_of_targets(self):
        targets = [self.make_target(self.run_, f"h{i:03d}") for i in range(40)]
        for t in targets[:5]:
            entry(t.hostname, 1)
        logs.ingest_token()   # warm the Integrations-settings cache (one query per 60 s, not per target)
        with self.assertNumQueries(7):         # counts, up to 5 sample reads (those with lines), nearby hosts
            out = logs.target_statuses(self.run_, targets)
        self.assertEqual(sum(1 for s in out.values() if s["arriving"]), 5)
        entry("zzz-unknown", 1)
        with self.assertNumQueries(7):
            logs.target_statuses(self.run_, targets)


@override_settings(LOG_INGEST_TOKEN=TOKEN, LINKEDEYE_PORTAL_URL="https://console.example.test/")
@with_key
class WizardTests(OnboardingTestCase):
    def setUp(self):
        super().setUp()
        self.capture = Capture()
        logging.getLogger().addHandler(self.capture)
        self.addCleanup(logging.getLogger().removeHandler, self.capture)
        self.run_ = self.make_run(step="logs")
        self.t = self.make_target(self.run_, "db-01")
        # the ingest token is shared by every site: only an admin on every site is shown it
        from django.contrib.auth import get_user_model
        self.root = get_user_model().objects.create_superuser("root-logs", "rl@example.com", "root-pass-123456")
        self.c.force_login(self.root)

    def assert_token_nowhere(self):
        for line in self.capture.lines:
            self.assertNotIn(TOKEN, line)
        self.assertFalse(any(TOKEN in str(e.details) for e in AuditEvent.objects.all()))
        self.assertFalse(any(TOKEN in e.detail for e in StepLog.objects.all()))
        self.t.refresh_from_db()
        self.assertNotIn(TOKEN, str(self.t.facts))
        self.assertNotIn(TOKEN, self.t.last_error)

    def test_generate_shows_the_token_once_and_stores_the_placeholder(self):
        resp = self.c.post(url(self.run_), {"action": "logs"})
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn(TOKEN, body)
        self.assertIn("no-store", resp["Cache-Control"])
        self.assertIn("https://console.example.test/api/v1/logs/", body)
        self.t.refresh_from_db()
        self.assertIn("<LOG_INGEST_TOKEN>", self.t.facts["logs"]["config"])
        self.assertEqual(self.t.facts["logs"]["shipper"], "vector")
        self.assertNotIn("token", self.t.facts["logs"])
        self.assert_token_nowhere()
        again = self.c.get(url(self.run_)).content.decode()
        self.assertNotIn(TOKEN, again)
        self.assertIn("&lt;LOG_INGEST_TOKEN&gt;", again)
        self.assertTrue(AuditEvent.objects.filter(action="onboarding.logs").exists())

    def test_the_page_says_waiting_then_arriving(self):
        self.c.post(url(self.run_), {"action": "logs"})
        page = self.c.get(url(self.run_)).content.decode()
        self.assertIn("still waiting", page)
        self.assertIn("Troubleshooting", page)
        self.assertIn("data-ob-logs-wait", page)
        self.assertIn("Rotate the log-ingest token", page)
        self.assertIn(reverse("administration:integrations"), page)
        entry("db-01", 1, message="Accepted publickey for svc from 10.0.0.9")
        page = self.c.get(url(self.run_)).content.decode()
        self.assertIn("lines arriving", page)
        self.assertIn("Accepted publickey for svc from 10.0.0.9", page)
        self.assertNotIn("Troubleshooting", page)
        self.assertNotIn("data-ob-logs-wait", page)
        self.assertNotIn(TOKEN, page)

    def test_a_log_line_with_markup_is_escaped(self):
        entry("db-01", 1, message="<script>alert(1)</script>")
        page = self.c.get(url(self.run_)).content.decode()
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)

    def test_the_fresh_page_does_not_auto_reload(self):
        resp = self.c.post(url(self.run_), {"action": "logs"})
        self.assertIn("data-ob-logs-fresh", resp.content.decode())

    @override_settings(LOG_INGEST_TOKEN="")
    def test_ingest_disabled_page_explains_and_generates_nothing(self):
        page = self.c.get(url(self.run_)).content.decode()
        self.assertIn("Log ingest is switched off", page)
        self.c.post(url(self.run_), {"action": "logs"})
        self.t.refresh_from_db()
        self.assertEqual(self.t.facts["logs"]["config"], "")
        self.assertIn("LINKEDEYE_LOG_INGEST_TOKEN", self.t.facts["logs"]["error"])

    def test_viewer_and_operator_cannot_generate_and_see_no_token(self):
        for user in (self.viewer, self.operator):
            self.login(user)
            self.assertEqual(self.c.post(url(self.run_), {"action": "logs"}).status_code, 403)
            self.assertNotIn(TOKEN, self.c.get(url(self.run_)).content.decode())
        self.t.refresh_from_db()
        self.assertNotIn("logs", self.t.facts)

    def test_another_sites_admin_cannot_use_the_step(self):
        self.login(self.admin_b)
        self.assertIn(self.c.post(url(self.run_), {"action": "logs"}).status_code, (403, 404))
        self.assertIn(self.c.get(url(self.run_)).status_code, (403, 404))

    def test_a_url_target_shows_not_applicable(self):
        self.make_target(self.run_, "shop.example.test", target_type=TargetType.URL)
        self.c.post(url(self.run_), {"action": "logs"})
        page = self.c.get(url(self.run_)).content.decode()
        self.assertIn("not applicable", page)
        self.assert_token_nowhere()

    def test_a_failing_hook_leaks_nothing(self):
        from unittest import mock
        with mock.patch.object(logs, "vector_config", side_effect=RuntimeError(f"boom {TOKEN}")):
            self.c.post(url(self.run_), {"action": "logs"})
        self.assertFalse(any(TOKEN in line for line in StepLog.objects.values_list("detail", flat=True)))
        for line in self.capture.lines:
            self.assertNotIn(TOKEN, line)


class StaticTests(OnboardingTestCase):
    def test_the_poll_script_only_reloads_and_writes_text(self):
        js = (Path(__file__).resolve().parents[1] / "static" / "onboarding" / "logs.js").read_text()
        for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
            self.assertNotIn(bad, js)
        self.assertIn("textContent", js)
