"""Review round: tenant scoping of the scheduler jobs, NDJSON with Unicode line separators, the CSV attributes
column, the ingest throttle / event budget and the site names offered to site-limited users."""
import csv
import io
import json
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from core.models import Event, Membership, Site

from . import jobs, services
from .models import Exporter, ExporterStatus, LogEntry

T = "default"
TOKEN = "s3cret-token"
INGEST = "/api/v1/logs/"


def post(body, ctype="application/json", token=TOKEN, **extra):
    c = Client()
    c.raise_request_exception = False
    if not isinstance(body, (str, bytes)):
        body = json.dumps(body)
    return c.post(INGEST, body, content_type=ctype, HTTP_AUTHORIZATION=f"Bearer {token}", **extra)


@override_settings(LINKEDEYE_TENANT=T)
class TenantScopedJobsTests(TestCase):
    def test_jobs_leave_other_tenants_rows_alone(self):
        foreign_live = Exporter.objects.create(tenant_key="other", name="theirs", exporter_type="Node Exporter",
                                               category="Servers", metrics_url="http://127.0.0.1:9/metrics",
                                               enabled=True)
        foreign_off = Exporter.objects.create(tenant_key="other", name="theirs-off", exporter_type="Node Exporter",
                                              category="Servers", enabled=False, status=ExporterStatus.ERROR,
                                              last_error="boom")
        old = LogEntry.objects.create(tenant_key="other", ts=timezone.now() - timedelta(days=20), source="s",
                                      level="INFO", message="m")
        mine_old = LogEntry.objects.create(tenant_key=T, ts=timezone.now() - timedelta(days=20), source="s",
                                           level="INFO", message="m")
        self.assertEqual(jobs.exporter_health(), "scraped 0 exporter(s): 0 active, 0 error")
        self.assertEqual(jobs.log_retention(), "pruned 1 log entry")
        foreign_live.refresh_from_db()
        foreign_off.refresh_from_db()
        self.assertEqual(foreign_live.last_checked_at, None)
        self.assertEqual((foreign_off.status, foreign_off.last_error), (ExporterStatus.ERROR, "boom"))
        self.assertTrue(LogEntry.objects.filter(pk=old.pk).exists())
        self.assertFalse(LogEntry.objects.filter(pk=mine_old.pk).exists())


@override_settings(LINKEDEYE_TENANT=T, LOG_INGEST_TOKEN=TOKEN)
class NdjsonSeparatorTests(TestCase):
    def test_unicode_line_separators_inside_a_message_do_not_split_the_line(self):
        a = {"source": "app", "level": "INFO", "message": "first"}
        b = {"source": "app", "level": "INFO", "message": "multi line stack\u0085end\x1cx"}
        c = {"source": "app", "level": "INFO", "message": "third"}
        body = "\n".join(json.dumps(x, ensure_ascii=False) for x in (a, b, c)) + "\n"
        r = post(body.encode("utf-8"), ctype="application/x-ndjson")
        self.assertEqual(r.status_code, 200, r.content[:300])
        self.assertEqual((r.json()["accepted"], r.json()["rejected"]), (3, 0))
        self.assertEqual(LogEntry.objects.get(message__startswith="multi").message, b["message"])

    def test_crlf_lines_still_work(self):
        body = '{"source":"a","level":"INFO","message":"x"}\r\n{"source":"a","level":"INFO","message":"y"}\r\n'
        r = post(body, ctype="application/x-ndjson")
        self.assertEqual(r.json()["accepted"], 2)


@override_settings(LINKEDEYE_TENANT=T, LOG_INGEST_TOKEN=TOKEN)
class CsvAttributesTests(TestCase):
    def test_attributes_column_is_json(self):
        r = post([{"source": "a", "level": "INFO", "message": "m", "attributes": {"ok": True, "x": None, "n": [1]}}])
        self.assertEqual(r.status_code, 200)
        su = User.objects.create_superuser("root", "r@example.com", "pw")
        c = Client()
        c.force_login(su)
        resp = c.get("/logs/export.csv")
        rows = list(csv.reader(io.StringIO(b"".join(resp.streaming_content).decode())))
        self.assertEqual(json.loads(rows[1][6]), {"ok": True, "x": None, "n": [1]})

    def test_empty_attributes_stay_blank(self):
        post([{"source": "a", "level": "INFO", "message": "m"}])
        su = User.objects.create_superuser("root", "r@example.com", "pw")
        c = Client()
        c.force_login(su)
        resp = c.get("/logs/export.csv")
        rows = list(csv.reader(io.StringIO(b"".join(resp.streaming_content).decode())))
        self.assertEqual(rows[1][6], "")


@override_settings(LINKEDEYE_TENANT=T, LOG_INGEST_TOKEN=TOKEN, INCIDENT_AUTO_CREATE=False, NOTIFY_WEBHOOK_URLS=[],
                   NOTIFY_EMAILS=[])
class IngestThrottleTests(TestCase):
    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def crit(self, words, source="app"):
        return [{"source": source, "level": "CRITICAL", "message": f"login failed for user {w}"} for w in words]

    def test_source_event_budget_holds_across_requests(self):
        first = post(self.crit([f"alpha{chr(97 + i)}" for i in range(25)])).json()
        self.assertEqual(first["events"], 25)
        # same source, new wording, inside the window: no further events (the budget is spent)
        second = post(self.crit([f"beta{chr(97 + i)}" for i in range(25)])).json()
        self.assertEqual(second["accepted"], 25)
        self.assertEqual(second["events"], 0)
        self.assertEqual(second["events_suppressed"], 25)
        self.assertEqual(Event.objects.filter(source="observe", severity="critical").count(), 25)
        # one summary event tells the operators that events were held back, and only one per window
        summaries = Event.objects.filter(source="observe", fingerprint__startswith="observe:log:suppressed")
        self.assertEqual(summaries.count(), 1)
        self.assertEqual(summaries[0].severity, "warning")
        post(self.crit([f"gamma{chr(97 + i)}" for i in range(5)]))
        self.assertEqual(summaries.count(), 1)

    def test_other_sources_keep_their_own_budget(self):
        post(self.crit([f"w{chr(97 + i)}" for i in range(25)], source="a"))
        d = post(self.crit(["only"], source="b")).json()
        self.assertEqual(d["events"], 1)

    def test_budget_resets_after_the_window(self):
        post(self.crit([f"alpha{chr(97 + i)}" for i in range(25)]))
        Event.objects.update(occurred_at=timezone.now() - services.EVENT_WINDOW - timedelta(minutes=1))
        self.assertEqual(post(self.crit(["fresh"])).json()["events"], 1)

    @override_settings(LOG_INGEST_AUTH_FAIL_LIMIT=3)
    def test_failed_tokens_are_throttled(self):
        for _ in range(3):
            self.assertEqual(post([], token="wrong").status_code, 401)
        r = post([], token="wrong")
        self.assertEqual(r.status_code, 429)
        self.assertTrue(r["Retry-After"])
        # a valid token is never turned away because of earlier failures from the same address
        self.assertEqual(post([{"source": "a", "level": "INFO", "message": "m"}]).status_code, 200)

    @override_settings(LOG_INGEST_RATE_PER_MINUTE=3)
    def test_request_rate_is_limited(self):
        ok = [{"source": "a", "level": "INFO", "message": "m"}]
        for _ in range(3):
            self.assertEqual(post(ok).status_code, 200)
        r = post(ok)
        self.assertEqual(r.status_code, 429)
        self.assertTrue(r["Retry-After"])
        self.assertEqual(LogEntry.objects.count(), 3)


@override_settings(LINKEDEYE_TENANT=T)
class SiteNameDisclosureTests(TestCase):
    def setUp(self):
        self.a = Site.objects.create(name="Site Alpha", code="A", tenant_key=T)
        self.g = Site.objects.create(name="Site Gamma Secret", code="G", tenant_key=T)
        self.viewer = User.objects.create_user("va", password="pw")
        Membership.objects.create(user=self.viewer, site=self.a, role="viewer")
        self.operator = User.objects.create_user("oa", password="pw")
        Membership.objects.create(user=self.operator, site=self.a, role="operator")

    def test_site_names_helper_is_scoped_to_the_user(self):
        self.assertEqual(services.site_names(user=self.viewer), ["Site Alpha"])
        su = User.objects.create_superuser("root", "r@example.com", "pw")
        self.assertEqual(services.site_names(user=su), ["Site Alpha", "Site Gamma Secret"])

    def test_pages_do_not_list_foreign_sites(self):
        for user, urls in ((self.viewer, ["/monitoring/exporters/", "/logs/"]),
                           (self.operator, ["/monitoring/exporters/new/"])):
            c = Client()
            c.force_login(user)
            for url in urls:
                r = c.get(url)
                self.assertEqual(r.status_code, 200, url)
                self.assertContains(r, "Site Alpha")
                self.assertNotContains(r, "Site Gamma Secret", msg_prefix=url)
