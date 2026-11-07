"""Second audit round: ingest payloads the database refuses, stale scrape results, CSV updates and the live tail."""
import json
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.events import emit
from core.models import Event

from . import scrape, services
from .models import Exporter, ExporterStatus, LogEntry
from .scrape import ScrapeResult

T = "default"
TOKEN = "s3cret-token"
INGEST = "/api/v1/logs/"


def exp(name="node-01", **kw):
    kw.setdefault("tenant_key", T)
    kw.setdefault("exporter_type", "Node Exporter")
    kw.setdefault("category", "Servers")
    return Exporter.objects.create(name=name, **kw)


def ok(samples=12, ms=4.2):
    return ScrapeResult(ok=True, status=200, response_ms=ms, samples=samples)


def fail(error="Connection refused"):
    return ScrapeResult(ok=False, response_ms=1.0, error=error)


# ------------------------------------------------------------------------------- ingest payloads
@override_settings(LINKEDEYE_TENANT=T, LOG_INGEST_TOKEN=TOKEN)
class IngestPayloadTests(TestCase):
    def post(self, body, content_type="application/json"):
        c = Client()
        c.raise_request_exception = False
        return c.post(INGEST, body, content_type=content_type, HTTP_AUTHORIZATION=f"Bearer {TOKEN}")

    def test_nan_attribute_is_rejected_on_its_own(self):
        body = ('[{"source":"a","level":"INFO","message":"ok"},'
                '{"source":"a","level":"INFO","message":"m","attributes":{"v":NaN}}]')
        r = self.post(body)
        self.assertEqual(r.status_code, 200, r.content[:300])
        self.assertEqual((r.json()["accepted"], r.json()["rejected"]), (1, 1))
        self.assertEqual([e.message for e in LogEntry.objects.all()], ["ok"])

    def test_infinity_attribute_is_rejected_on_its_own(self):
        body = '[{"source":"a","level":"INFO","message":"inf","attributes":{"v":Infinity}}]'
        r = self.post(body)
        self.assertEqual(r.status_code, 400, r.content[:300])
        self.assertEqual(LogEntry.objects.count(), 0)

    def test_lone_surrogate_does_not_sink_the_batch(self):
        body = json.dumps([{"source": "a", "level": "INFO", "message": "ok"},
                           {"source": "a", "level": "INFO", "message": "x\ud800y", "host": "h\udfffz",
                            "attributes": {"k\ud800": "v\ud800"}}])
        r = self.post(body)
        self.assertEqual(r.status_code, 200, r.content[:300])
        self.assertEqual((r.json()["accepted"], r.json()["rejected"]), (2, 0))
        bad = LogEntry.objects.get(message__startswith="x")
        self.assertEqual(bad.message, "x�y")
        self.assertEqual(bad.host, "h�z")
        self.assertEqual(bad.attributes, {"k�": "v�"})

    def test_huge_integer_literal_is_a_400(self):
        body = '[{"source":"a","level":"INFO","message":"m","attributes":{"n":%s}}]' % ("1" * 5000)
        r = self.post(body)
        self.assertEqual(r.status_code, 400, r.content[:300])
        self.assertIn("JSON", r.json()["error"])

    def test_huge_integer_literal_in_ndjson_is_a_400(self):
        body = '{"source":"a","level":"INFO","message":"m","attributes":{"n":%s}}' % ("1" * 5000)
        r = self.post(body, content_type="application/x-ndjson")
        self.assertEqual(r.status_code, 400, r.content[:300])


# ------------------------------------------------------------------------------- stale scrape results
@override_settings(LINKEDEYE_TENANT=T, PROBE_WORKERS=1)
class StaleScrapeResultTests(TestCase):
    def test_exporter_deleted_during_the_scrape_does_not_abort_the_batch(self):
        a = exp("a", metrics_url="http://10.0.0.1:9100/metrics")
        b = exp("b", metrics_url="http://10.0.0.2:9100/metrics")

        def scraper(url):
            if url.startswith("http://10.0.0.1"):
                Exporter.objects.filter(pk=a.pk).delete()
            return ok(7)

        with mock.patch.object(scrape, "scrape", side_effect=scraper):
            services.check_exporters([a, b])
        b.refresh_from_db()
        self.assertEqual(b.status, ExporterStatus.ACTIVE)
        self.assertEqual(b.samples, 7)

    def test_url_changed_during_the_scrape_keeps_the_old_result_out(self):
        c = exp("c", metrics_url="http://10.0.0.3:9100/metrics")

        def scraper(url):
            Exporter.objects.filter(pk=c.pk).update(metrics_url="http://10.0.0.9:9100/metrics")
            return fail("Connection refused")

        with mock.patch.object(scrape, "scrape", side_effect=scraper):
            services.check_exporters([c])
        c.refresh_from_db()
        self.assertEqual(c.status, ExporterStatus.NOT_CONFIGURED)
        self.assertFalse(Event.objects.filter(fingerprint=f"exporter:{c.pk}").exists())

    def test_check_all_view_survives_a_deleted_exporter(self):
        a = exp("a", metrics_url="http://10.0.0.1:9100/metrics")
        exp("b", metrics_url="http://10.0.0.2:9100/metrics")
        operator = User.objects.create_user("operator", password="pw", is_staff=True)
        cl = Client()
        cl.force_login(operator)
        cl.raise_request_exception = False

        def scraper(url):
            if url.startswith("http://10.0.0.1"):
                Exporter.objects.filter(pk=a.pk).delete()
            return ok(3)

        with mock.patch.object(scrape, "scrape", side_effect=scraper):
            r = cl.post(reverse("observe:exporter_check_all"))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Exporter.objects.get(name="b").status, ExporterStatus.ACTIVE)


    def test_exporter_disabled_during_the_scrape_keeps_the_result_out(self):
        d = exp("d", metrics_url="http://10.0.0.4:9100/metrics")

        def scraper(url):
            Exporter.objects.filter(pk=d.pk).update(enabled=False)
            return ok(9)

        with mock.patch.object(scrape, "scrape", side_effect=scraper):
            services.check_exporters([d])
        d.refresh_from_db()
        self.assertNotEqual(d.status, ExporterStatus.ACTIVE)
        self.assertFalse(d.samples)
        self.assertFalse(Event.objects.filter(fingerprint=f"exporter:{d.pk}").exists())

    def test_a_result_that_cannot_be_stored_does_not_cost_the_rest_of_the_batch(self):
        a = exp("a", metrics_url="http://10.0.0.1:9100/metrics")
        b = exp("b", metrics_url="http://10.0.0.2:9100/metrics")
        real = services._apply_result

        def flaky(e, res, now):
            if e.pk == a.pk:
                raise RuntimeError("boom")
            return real(e, res, now)

        with mock.patch.object(scrape, "scrape", return_value=ok(5)), \
                mock.patch.object(services, "_apply_result", side_effect=flaky), \
                self.assertLogs(services.log, level="ERROR"):
            done = services.check_exporters([a, b])
        self.assertEqual(len(done), 2)
        b.refresh_from_db()
        self.assertEqual((b.status, b.samples), (ExporterStatus.ACTIVE, 5))


# ------------------------------------------------------------------------------- CSV import
@override_settings(LINKEDEYE_TENANT=T)
class ImportUpdateTests(TestCase):
    def setUp(self):
        self.operator = User.objects.create_user("operator", password="pw", is_staff=True)
        self.c = Client()
        self.c.force_login(self.operator)

    def test_update_keeps_columns_the_file_does_not_carry(self):
        e = exp("n1", target="srv1", metrics_url="http://10.0.0.1:9100/metrics", site="Mumbai DC", enabled=False,
                description="the old note", status=ExporterStatus.INACTIVE)
        f = SimpleUploadedFile("e.csv", b"name,type,category\nn1,Node Exporter,Servers\n", content_type="text/csv")
        r = self.c.post(reverse("observe:exporters_import"), {"file": f, "update_existing": "on"})
        self.assertEqual(r.status_code, 302, getattr(r, "context", None) and r.context["report"])
        e.refresh_from_db()
        self.assertEqual((e.target, e.metrics_url, e.site, e.description), ("srv1", "http://10.0.0.1:9100/metrics",
                                                                            "Mumbai DC", "the old note"))
        self.assertFalse(e.enabled)
        self.assertEqual(e.status, ExporterStatus.INACTIVE)

    def test_update_still_applies_the_columns_that_are_present(self):
        e = exp("n1", target="srv1", site="Mumbai DC", enabled=False)
        f = SimpleUploadedFile("e.csv", b"name,type,category,target,enabled\nn1,SNMP,Network,srv2,yes\n",
                               content_type="text/csv")
        self.c.post(reverse("observe:exporters_import"), {"file": f, "update_existing": "on"})
        e.refresh_from_db()
        self.assertEqual((e.target, e.exporter_type, e.category, e.site), ("srv2", "SNMP", "Network", "Mumbai DC"))
        self.assertTrue(e.enabled)

    def test_blank_cell_still_clears_a_column_that_is_in_the_file(self):
        e = exp("n1", target="srv1")
        f = SimpleUploadedFile("e.csv", b"name,type,category,target\nn1,Node Exporter,Servers,\n", content_type="text/csv")
        self.c.post(reverse("observe:exporters_import"), {"file": f, "update_existing": "on"})
        e.refresh_from_db()
        self.assertEqual(e.target, "")


    def test_blank_enabled_cell_keeps_the_current_value(self):
        on = exp("on", target="a")
        off = exp("off", target="b", enabled=False)
        f = SimpleUploadedFile("e.csv", b"name,type,category,enabled\non,Node Exporter,Servers,\noff,Node Exporter,Servers,\n",
                               content_type="text/csv")
        self.c.post(reverse("observe:exporters_import"), {"file": f, "update_existing": "on"})
        on.refresh_from_db()
        off.refresh_from_db()
        self.assertTrue(on.enabled)
        self.assertFalse(off.enabled)

    def test_new_exporters_are_created_enabled_with_or_without_the_enabled_column(self):
        f = SimpleUploadedFile("e.csv", b"name,type,category\nfresh1,Node Exporter,Servers\n", content_type="text/csv")
        self.c.post(reverse("observe:exporters_import"), {"file": f})
        f = SimpleUploadedFile("e.csv", b"name,type,category,enabled\nfresh2,Node Exporter,Servers,\n", content_type="text/csv")
        self.c.post(reverse("observe:exporters_import"), {"file": f})
        self.assertEqual(list(Exporter.objects.filter(name__startswith="fresh").order_by("name")
                              .values_list("enabled", flat=True)), [True, True])


# ------------------------------------------------------------------------------- open failure events
@override_settings(LINKEDEYE_TENANT=T)
class CloseFailureTests(TestCase):
    def setUp(self):
        self.operator = User.objects.create_user("operator", password="pw", is_staff=True)
        self.c = Client()
        self.c.force_login(self.operator)

    def failing(self, e):
        emit(source="observe", title=f"Exporter {e.name} is failing", severity="warning", site=e.site, obj=e,
             fingerprint=f"exporter:{e.pk}")
        self.assertTrue(services._failing_event_open(e))

    def test_disabling_an_exporter_waiting_for_its_first_scrape_closes_the_failure(self):
        e = exp("x", metrics_url="http://10.0.0.1:9100/metrics", status=ExporterStatus.NOT_CONFIGURED,
                last_error="Waiting for the first scrape")
        self.failing(e)
        self.c.post(reverse("observe:exporter_edit", args=[e.pk]), {
            "name": "x", "exporter_type": "Node Exporter", "category": "Servers", "target": "",
            "metrics_url": "http://10.0.0.1:9100/metrics", "site": "", "description": ""})   # enabled unticked
        e.refresh_from_db()
        self.assertEqual(e.status, ExporterStatus.INACTIVE)
        self.assertFalse(services._failing_event_open(e))

    def test_import_that_disables_an_exporter_waiting_for_its_first_scrape_closes_the_failure(self):
        e = exp("y", metrics_url="http://10.0.0.1:9100/metrics", status=ExporterStatus.NOT_CONFIGURED,
                last_error="Waiting for the first scrape")
        self.failing(e)
        f = SimpleUploadedFile("e.csv", b"name,type,category,enabled\ny,Node Exporter,Servers,no\n",
                               content_type="text/csv")
        self.c.post(reverse("observe:exporters_import"), {"file": f, "update_existing": "on"})
        e.refresh_from_db()
        self.assertEqual(e.status, ExporterStatus.INACTIVE)
        self.assertFalse(services._failing_event_open(e))


# ------------------------------------------------------------------------------- live tail feed
@override_settings(LINKEDEYE_TENANT=T)
class LogsFeedTests(TestCase):
    def setUp(self):
        self.viewer = User.objects.create_user("viewer", password="pw")
        self.c = Client()
        self.c.force_login(self.viewer)

    def test_feed_cursor_never_skips_lines_when_more_than_the_limit_arrive(self):
        now = timezone.now()
        LogEntry.objects.bulk_create([LogEntry(tenant_key=T, source="a", level="INFO", message=f"m{i}", ts=now,
                                               received_at=now) for i in range(250)])
        first = LogEntry.objects.order_by("id").first()
        seen, after, polls = [], first.pk, 0
        while polls < 10:
            polls += 1
            body = self.c.get(reverse("observe:logs_feed"), {"after": after, "limit": 100, "range": "1h"}).json()
            if not body["rows"]:
                break
            seen += [r["id"] for r in body["rows"]]
            self.assertLessEqual(body["last_id"], max(r["id"] for r in body["rows"]))
            after = body["last_id"]
        self.assertEqual(sorted(seen), [e.pk for e in LogEntry.objects.order_by("id")[1:]])

    def test_feed_rows_stay_newest_first(self):
        now = timezone.now()
        for i in range(5):
            LogEntry.objects.create(tenant_key=T, source="a", level="INFO", message=f"m{i}", ts=now, received_at=now)
        body = self.c.get(reverse("observe:logs_feed"), {"after": 0, "limit": 3, "range": "1h"}).json()
        self.assertEqual([r["message"] for r in body["rows"]], ["m4", "m3", "m2"])

    def test_oversized_after_is_clamped(self):
        self.c.raise_request_exception = False
        r = self.c.get(reverse("observe:logs_feed"), {"after": "9" * 25, "limit": 10})
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertEqual(r.json()["rows"], [])

    def test_feed_rows_stay_newest_first_when_walking_with_a_cursor(self):
        now = timezone.now()
        for i in range(5):
            LogEntry.objects.create(tenant_key=T, source="a", level="INFO", message=f"m{i}", ts=now, received_at=now)
        first = LogEntry.objects.order_by("id").first()
        body = self.c.get(reverse("observe:logs_feed"), {"after": first.pk, "limit": 3, "range": "1h"}).json()
        self.assertEqual([r["message"] for r in body["rows"]], ["m3", "m2", "m1"])
        self.assertEqual(body["last_id"], body["rows"][0]["id"])

    def test_feed_says_when_more_lines_are_waiting(self):
        now = timezone.now()
        LogEntry.objects.bulk_create([LogEntry(tenant_key=T, source="a", level="INFO", message=f"m{i}", ts=now,
                                               received_at=now) for i in range(30)])
        first = LogEntry.objects.order_by("id").first()
        url = reverse("observe:logs_feed")
        page = self.c.get(url, {"after": first.pk, "limit": 20, "range": "1h"}).json()
        self.assertTrue(page["more"])
        rest = self.c.get(url, {"after": page["last_id"], "limit": 20, "range": "1h"}).json()
        self.assertEqual(len(rest["rows"]), 9)
        self.assertFalse(rest["more"])


# ------------------------------------------------------------------------------- ids the database cannot hold
@override_settings(LINKEDEYE_TENANT=T)
class OversizedExporterIdTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("root", "r@x.com", "pw")
        self.c = Client()
        self.c.force_login(self.admin)
        self.c.raise_request_exception = False

    def test_an_exporter_id_the_database_cannot_hold_is_a_404_on_every_endpoint(self):
        big = 10 ** 25
        for name in ("exporter_detail", "exporter_edit"):
            self.assertEqual(self.c.get(reverse(f"observe:{name}", args=[big])).status_code, 404, name)
        for name in ("exporter_delete", "exporter_check"):
            self.assertEqual(self.c.post(reverse(f"observe:{name}", args=[big])).status_code, 404, name)
        self.assertEqual(self.c.get(reverse("observe:exporter_detail", args=[exp("real").pk])).status_code, 200)
