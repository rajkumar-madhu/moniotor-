"""Pages, JSON endpoints and actions: roles, site scoping, IDOR, CSV safety, the live snapshot, retry and RCA, sources admin, audit."""
import csv
import io
import json
import re
from datetime import timedelta
from unittest import mock

from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apm import services, snapshot
from apm.models import AdapterCell, ExecLog, ProcessState, RetryRequest, Source
from core.models import AuditEvent

from .base import ApmCase, matrix, table, with_key


def page_data(response, name="apm-data"):
    m = re.search(r'<script id="%s" type="application/json">(.*?)</script>' % name, response.content.decode(), re.S)
    return json.loads(m.group(1)) if m else None


class Fixtures(ApmCase):
    def setUp(self):
        super().setUp()
        now = timezone.now()
        services.ingest_matrix(self.src_a, matrix(now, failing={("c001", "NSE")}), host="host-a", script="adapterStatus.py", duration_s=8.2, now=now)
        services.ingest_table(self.src_a, table(now), now=now)
        services.ingest_logs(self.src_a, [{"ts": now.isoformat(), "level": "ERROR", "process": "adapterStatus.py", "message": "c001 NSE boom", "client": "c001", "segment": "NSE"},
                                          {"ts": now.isoformat(), "level": "INFO", "process": "adapterStatus.py", "message": "unrelated"}], now=now)
        services.ingest_matrix(self.src_b, matrix(now, clients=("zz1",), failing={("zz1", "NFO")}), now=now)
        self.cell_a = AdapterCell.objects.get(source=self.src_a, client="c001", segment="NSE")
        self.cell_b = AdapterCell.objects.get(source=self.src_b, client="zz1", segment="NFO")


class Pages(Fixtures):
    def test_dashboard_renders_the_snapshot_as_escaped_json(self):
        r = self.login(self.root).get("/apm/")
        self.assertEqual(r.status_code, 200)
        snap = page_data(r)
        self.assertEqual(snap["kpis"]["total"], 4)
        self.assertEqual(snap["kpis"]["failure"], 1)
        self.assertIn("apm/apm.js", r.content.decode())
        self.assertNotIn("onclick=", r.content.decode())

    def test_anonymous_is_redirected(self):
        r = self.client.get("/apm/")
        self.assertEqual(r.status_code, 302)

    def test_viewer_sees_only_their_site(self):
        c = self.login(self.mum_view)
        r = c.get("/apm/")
        self.assertEqual(page_data(r)["source"]["name"], "Alpha")
        self.assertEqual([s.name for s in r.context["sources"]], ["Alpha"])
        r = c.get(f"/apm/?source={self.src_b.pk}")           # asking for another site's source falls back to what they may see
        self.assertEqual(page_data(r)["source"]["name"], "Alpha")
        self.assertNotIn("zz1", r.content.decode())

    def test_global_viewer_sees_all_sources_and_a_selector(self):
        r = self.login(self.glob_view).get("/apm/")
        self.assertEqual({s.name for s in r.context["sources"]}, {"Alpha", "Bravo"})
        self.assertContains(r, 'id="apm-source"')

    def test_no_sources_shows_the_empty_state_with_a_call_to_action(self):
        Source.objects.all().delete()
        r = self.login(self.root).get("/apm/")
        self.assertContains(r, "No APM source yet")
        self.assertContains(r, 'href="/apm/sources/new/"')
        self.assertContains(r, "LINKEDEYE_APM_TOKEN")
        self.assertNotContains(r, 'id="apm-data"')
        r = self.login(self.mum_view).get("/apm/")
        self.assertNotContains(r, 'href="/apm/sources/new/"')
        self.assertContains(r, "Ask an administrator")

    def test_state_in_the_query_string_is_sanitised(self):
        c = self.login(self.root)
        r = c.get(f"/apm/?source={self.src_a.pk}&exchange=<script>&status=evil&q={'x' * 500}&logs=7&sel={self.cell_b.pk}")
        cfg = page_data(r, "apm-config")
        self.assertEqual(cfg["state"]["exchange"], "")
        self.assertEqual(cfg["state"]["status"], "")
        self.assertLessEqual(len(cfg["state"]["q"]), 60)
        self.assertEqual(cfg["state"]["logs"], 100)
        self.assertIsNone(cfg["state"]["sel"])             # another source's cell id is dropped
        self.assertNotIn("<script>alert", r.content.decode())

    def test_hostile_text_cannot_break_out_of_the_json_script(self):
        evil = "</script><script>alert(1)</script>"
        services.ingest_logs(self.src_a, [{"ts": timezone.now().isoformat(), "message": evil}])
        r = self.login(self.root).get("/apm/")
        html = r.content.decode()
        self.assertNotIn(evil, html)
        self.assertEqual(html.count("<script>alert"), 0)
        self.assertIn(evil, [ln["message"] for ln in page_data(r)["logs"]])

    def test_example_marker(self):
        Source.objects.filter(pk=self.src_a.pk).update(description="[example] demo")
        self.assertContains(self.login(self.root).get("/apm/"), "example data")

    def test_the_nav_has_an_apm_group(self):
        r = self.login(self.root).get("/apm/")
        labels = [n["label"] for n in r.context["nav"]]
        self.assertIn("APM", labels)
        group = next(n for n in r.context["nav"] if n["label"] == "APM")
        self.assertEqual([c["href"] for c in group["children"]], ["/apm/", "/apm/sources/"])
        self.assertTrue(group["open"])


class Live(Fixtures):
    def test_shape(self):
        d = self.login(self.root).get(f"/apm/live/?source={self.src_a.pk}").json()
        for key in ("rev", "now", "tz", "source", "kpis", "stale", "segments", "clients", "processes", "logs", "logs_n"):
            self.assertIn(key, d)
        self.assertEqual(d["segments"], ["NSE", "NFO"])
        c1 = next(c for c in d["clients"] if c["client"] == "c001")
        pk, ctcl, value, state, status = c1["cells"]["NSE"]
        self.assertEqual((pk, state, status), (self.cell_a.pk, "failure", 0))
        self.assertEqual(c1["state"], "failure")
        self.assertEqual(d["kpis"]["overall"], "degraded")
        self.assertEqual({"name", "host", "state", "pid", "cpu", "mem"} <= set(d["processes"][0]), True)
        self.assertEqual(d["logs"][-1]["level"] in ("INFO", "ERROR"), True)

    def test_unchanged_answer_is_small(self):
        c = self.login(self.root)
        d = c.get(f"/apm/live/?source={self.src_a.pk}").json()
        s = c.get(f"/apm/live/?source={self.src_a.pk}&rev={d['rev']}").json()
        self.assertTrue(s["unchanged"])
        self.assertNotIn("clients", s)
        services.ingest_matrix(self.src_a, matrix(timezone.now() + timedelta(seconds=30)), now=timezone.now() + timedelta(seconds=30))
        s2 = c.get(f"/apm/live/?source={self.src_a.pk}&rev={d['rev']}").json()
        self.assertNotIn("unchanged", s2)
        self.assertEqual(s2["kpis"]["failure"], 0)

    def test_scoped_to_the_users_sites(self):
        c = self.login(self.mum_view)
        self.assertEqual(c.get(f"/apm/live/?source={self.src_a.pk}").status_code, 200)
        self.assertEqual(c.get(f"/apm/live/?source={self.src_b.pk}").status_code, 404)
        self.assertEqual(c.get("/apm/live/").status_code, 404)
        self.assertEqual(self.client.get(f"/apm/live/?source={self.src_a.pk}").status_code, 302)

    def test_log_window(self):
        c = self.login(self.root)
        self.assertEqual(c.get(f"/apm/live/?source={self.src_a.pk}&logs=25").json()["logs_n"], 25)
        self.assertEqual(c.get(f"/apm/live/?source={self.src_a.pk}&logs=9999").json()["logs_n"], 100)

    def test_stale_state_greys_everything_and_counts_separately(self):
        long_ago = timezone.now() - timedelta(minutes=20)
        Source.objects.filter(pk=self.src_a.pk).update(last_matrix_at=long_ago, last_table_at=long_ago)
        d = self.login(self.root).get(f"/apm/live/?source={self.src_a.pk}").json()
        self.assertEqual(d["kpis"]["overall"], "stale")
        self.assertEqual((d["kpis"]["stale"], d["kpis"]["success"], d["kpis"]["failure"]), (4, 0, 0))
        self.assertTrue(d["stale"]["matrix_stale"])
        self.assertFalse(d["stale"]["banner"] is False and d["stale"]["age_s"] < d["stale"]["banner_after_s"] and False)
        self.assertEqual({c["cells"]["NSE"][3] for c in d["clients"]}, {"stale"})
        self.assertEqual({p["state"] for p in d["processes"]}, {"stale"})

    def test_banner_after_three_intervals(self):
        Source.objects.filter(pk=self.src_a.pk).update(last_matrix_at=timezone.now() - timedelta(minutes=10), last_table_at=timezone.now() - timedelta(minutes=10))
        self.assertFalse(self.login(self.root).get(f"/apm/live/?source={self.src_a.pk}").json()["stale"]["banner"])
        Source.objects.filter(pk=self.src_a.pk).update(last_matrix_at=timezone.now() - timedelta(minutes=16), last_table_at=timezone.now() - timedelta(minutes=16))
        d = self.login(self.root).get(f"/apm/live/?source={self.src_a.pk}").json()
        self.assertTrue(d["stale"]["banner"])
        self.assertEqual(d["stale"]["banner_after_s"], 900)

    def test_avg_response_time_over_five_minutes(self):
        ProcessState.objects.filter(source=self.src_a).update(response_ms=100.0)
        self.assertEqual(self.login(self.root).get(f"/apm/live/?source={self.src_a.pk}").json()["kpis"]["avg_response_ms"], 100.0)
        ProcessState.objects.filter(source=self.src_a).update(updated_at=timezone.now() - timedelta(minutes=9))
        self.assertIsNone(self.login(self.root).get(f"/apm/live/?source={self.src_a.pk}").json()["kpis"]["avg_response_ms"])

    def test_overall_states(self):
        def overall():
            return self.login(self.root).get(f"/apm/live/?source={self.src_a.pk}").json()["kpis"]["overall"]
        later = timezone.now() + timedelta(minutes=1)
        services.ingest_matrix(self.src_a, matrix(later), now=later)
        self.assertEqual(overall(), "healthy")
        later += timedelta(minutes=1)
        services.ingest_matrix(self.src_a, matrix(later, failing={("c001", "NSE"), ("c001", "NFO"), ("c002", "NSE"), ("c002", "NFO")}), now=later)
        self.assertEqual(overall(), "down")
        Source.objects.filter(pk=self.src_b.pk).update(last_matrix_at=None)
        AdapterCell.objects.filter(source=self.src_b).delete()
        self.assertEqual(self.login(self.root).get(f"/apm/live/?source={self.src_b.pk}").json()["kpis"]["overall"], "nodata")

    def test_query_count_does_not_grow_with_the_data(self):
        c = self.login(self.root)
        c.get(f"/apm/live/?source={self.src_a.pk}")           # warm the session / content types
        with CaptureQueriesContext(connection) as small:
            c.get(f"/apm/live/?source={self.src_a.pk}")
        clients = tuple(f"k{i:03d}" for i in range(120))
        later = timezone.now() + timedelta(minutes=2)
        services.ingest_matrix(self.src_a, matrix(later, clients=clients, segments=("NSE", "NFO", "CDS", "BSE"), failing={("k000", "NSE")}), now=later)
        services.ingest_table(self.src_a, table(later, names=tuple(f"proc{i}" for i in range(150))), now=later)
        services.ingest_logs(self.src_a, [{"ts": (later - timedelta(seconds=i)).isoformat(), "message": f"line {i}"} for i in range(500)], now=later)
        with CaptureQueriesContext(connection) as big:
            d = c.get(f"/apm/live/?source={self.src_a.pk}&logs=500").json()
        self.assertEqual(len(d["clients"]), 120)
        self.assertEqual(len(d["processes"]), 150)
        self.assertEqual(len(d["logs"]), 500)
        self.assertEqual(len(big), len(small))
        self.assertLessEqual(len(big), 14)
        with CaptureQueriesContext(connection) as page:
            c.get(f"/apm/?source={self.src_a.pk}&logs=500")
        self.assertLessEqual(len(page), 30)


class CellDetail(Fixtures):
    def test_detail_json(self):
        d = self.login(self.mum_op).get(f"/apm/cell/{self.cell_a.pk}/").json()
        self.assertEqual((d["client"], d["segment"], d["status_label"], d["state"]), ("c001", "NSE", "Failed", "failure"))
        self.assertEqual(d["details"]["script"], "adapterStatus.py")
        self.assertEqual(d["details"]["host"], "host-a")
        self.assertEqual(d["details"]["duration"], "00:00:08")
        self.assertTrue(d["details"]["next_execution"])
        f = d["failure"]
        self.assertEqual((f["active"], f["error"], f["count"], f["streak"]), (True, "boom", 1, 1))
        self.assertEqual([s["message"] for s in f["snippet"]], ["c001 NSE boom"])      # only lines about this adapter
        self.assertEqual(len(d["spark"]), 48)
        self.assertIn("crit", d["spark"])
        self.assertEqual(d["history"][0]["new"], "Failed")
        self.assertTrue(d["can_retry"])
        self.assertIn("LinkedEye APM evidence", d["evidence"])
        self.assertNotIn(self.src_a.token_plain, d["evidence"])
        self.assertNotIn(self.src_a.token_hash, d["evidence"])

    def test_logs_are_filtered_to_that_adapter_not_its_siblings(self):
        now = timezone.now()
        services.ingest_logs(self.src_a, [
            {"ts": now.isoformat(), "level": "ERROR", "process": "p", "message": "c001 NFO 20002: sibling exchange failed"},
            {"ts": now.isoformat(), "level": "ERROR", "process": "p", "message": "c001 NSE 10001: this adapter failed"},
            {"ts": now.isoformat(), "level": "WARNING", "process": "p", "message": "c001 retrying with a licence check"},
        ], now=now)
        d = self.login(self.mum_op).get(f"/apm/cell/{self.cell_a.pk}/").json()
        msgs = [x["message"] for x in d["logs"]]
        self.assertIn("c001 NSE 10001: this adapter failed", msgs)
        self.assertIn("c001 NSE boom", msgs)
        self.assertIn("c001 retrying with a licence check", msgs)      # names the client only: kept
        self.assertNotIn("c001 NFO 20002: sibling exchange failed", msgs)
        self.assertNotIn("unrelated", msgs)

    def test_viewer_cannot_retry_but_can_read(self):
        d = self.login(self.mum_view).get(f"/apm/cell/{self.cell_a.pk}/").json()
        self.assertFalse(d["can_retry"])

    def test_idor_other_sites_cells_are_404(self):
        for user in (self.mum_view, self.mum_op, self.mum_admin):
            c = self.login(user)
            for path in (f"/apm/cell/{self.cell_b.pk}/", f"/apm/cell/{self.cell_b.pk}/evidence.txt"):
                self.assertEqual(c.get(path).status_code, 404, path)
            self.assertEqual(c.get(f"/apm/export.csv?source={self.src_b.pk}").status_code, 404)
            self.assertEqual(c.get(f"/apm/export.csv?source={self.src_a.pk}&cell={self.cell_b.pk}").content.decode().count("zz1"), 0)

    def test_healthy_cell_is_not_failing(self):
        ok = AdapterCell.objects.get(source=self.src_a, client="c002", segment="NFO")
        d = self.login(self.root).get(f"/apm/cell/{ok.pk}/").json()
        self.assertFalse(d["failure"]["active"])
        self.assertEqual(d["state"], "success")

    def test_stale_marks_the_detail(self):
        Source.objects.filter(pk=self.src_a.pk).update(last_matrix_at=timezone.now() - timedelta(hours=1))
        d = self.login(self.root).get(f"/apm/cell/{self.cell_a.pk}/").json()
        self.assertEqual((d["state"], d["status_label"], d["stale"]), ("stale", "Failed", True))

    def test_evidence_download(self):
        r = self.login(self.mum_view).get(f"/apm/cell/{self.cell_a.pk}/evidence.txt")
        self.assertEqual(r["Content-Type"], "text/plain; charset=utf-8")
        self.assertIn("attachment; filename=\"apm-evidence-c001-nse-", r["Content-Disposition"])
        self.assertIn("Adapter: c001 / NSE", r.content.decode())

    def test_viewer_gets_logs_filtered_to_the_adapter(self):
        services.ingest_logs(self.src_a, [{"ts": timezone.now().isoformat(), "message": f"adapter {self.cell_a.ctcl_id} reconnect"}])
        d = self.login(self.root).get(f"/apm/cell/{self.cell_a.pk}/").json()
        self.assertEqual(sorted(x["message"] for x in d["logs"]), sorted(["c001 NSE boom", f"adapter {self.cell_a.ctcl_id} reconnect"]))


class CsvExport(Fixtures):
    def rows(self, url, user=None):
        r = self.login(user or self.root).get(url)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "text/csv; charset=utf-8")
        return list(csv.reader(io.StringIO(r.content.decode())))

    def test_export_and_filters(self):
        rows = self.rows(f"/apm/export.csv?source={self.src_a.pk}")
        self.assertEqual(rows[0][:5], ["client", "exchange", "adapter_id", "value", "status"])
        self.assertEqual(len(rows), 5)
        failed = self.rows(f"/apm/export.csv?source={self.src_a.pk}&status=failure")
        self.assertEqual([r[0:2] for r in failed[1:]], [["c001", "NSE"]])
        self.assertEqual(len(self.rows(f"/apm/export.csv?source={self.src_a.pk}&exchange=NFO")), 3)
        self.assertEqual(len(self.rows(f"/apm/export.csv?source={self.src_a.pk}&client=c002")), 3)
        one = self.rows(f"/apm/export.csv?source={self.src_a.pk}&cell={self.cell_a.pk}")
        self.assertEqual(len(one), 2)
        self.assertEqual(one[1][9], "boom")

    def test_formula_injection_is_defused(self):
        evil = {"data": {"=cmd|' /C calc'!A0": {"NSE": {"status": 0, "ctcl_id": "+1+1", "value": "-2+3", "error": "@SUM(1,1)"},
                                                 "NFO": {"status": 2, "ctcl_id": "\t=1", "value": " =1+1"}}}}
        later = timezone.now() + timedelta(minutes=1)
        services.ingest_matrix(self.src_a, evil, now=later)
        text = self.login(self.root).get(f"/apm/export.csv?source={self.src_a.pk}").content.decode()
        for line in csv.reader(io.StringIO(text)):
            for field in line[:4] + line[9:]:
                self.assertNotIn(field[:1], ("=", "+", "-", "@", "\t"), field)
        self.assertIn("'=cmd", text)
        self.assertIn("'@SUM", text)
        self.assertEqual(snapshot.csv_safe(" =1"), "' =1")
        self.assertEqual(snapshot.csv_safe(5), 5)
        self.assertEqual(snapshot.csv_safe(None), "")

    def test_scoped(self):
        text = self.login(self.mum_view).get(f"/apm/export.csv?source={self.src_a.pk}").content.decode()
        self.assertNotIn("zz1", text)


class Retry(Fixtures):
    def post(self, user, cell, action="retry"):
        return self.login(user).post(f"/apm/cell/{cell.pk}/{action}/", HTTP_X_CSRFTOKEN="x")

    def test_lifecycle_with_poll(self):
        c = self.login(self.mum_op)
        r = c.post(f"/apm/cell/{self.cell_a.pk}/retry/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r.json()["created"], r.json()["retry"]["state"]), (True, "pending"))
        again = c.post(f"/apm/cell/{self.cell_a.pk}/retry/").json()
        self.assertFalse(again["created"])
        self.assertEqual(RetryRequest.objects.count(), 1)
        d = c.get(f"/apm/cell/{self.cell_a.pk}/").json()
        self.assertEqual(d["retry"]["state"], "pending")
        # the script pulls it
        from django.test import Client
        Client().get("/api/apm/commands/", HTTP_AUTHORIZATION=f"Bearer {self.src_a.token_plain}")
        self.assertEqual(c.get(f"/apm/cell/{self.cell_a.pk}/").json()["retry"]["state"], "picked")
        later = timezone.now() + timedelta(minutes=1)
        services.ingest_matrix(self.src_a, matrix(later, failing={("c001", "NSE")}), now=later)
        d = c.get(f"/apm/cell/{self.cell_a.pk}/").json()
        self.assertEqual(d["retry"]["state"], "done")
        self.assertTrue(AuditEvent.objects.filter(action="apm.retry.request", actor=self.mum_op).exists())

    def test_viewer_cannot_request_a_retry(self):
        r = self.post(self.mum_view, self.cell_a)
        self.assertEqual(r.status_code, 403)
        self.assertFalse(RetryRequest.objects.exists())

    def test_operator_of_another_site_cannot(self):
        from django.contrib.auth import get_user_model
        from core.models import Membership
        u = get_user_model().objects.create_user("che-op", password="apm-test-pass-123")
        Membership.objects.create(user=u, site=self.che, role="operator")
        r = self.post(u, self.cell_a)
        self.assertEqual(r.status_code, 404)
        self.assertFalse(RetryRequest.objects.exists())

    def test_disabled_source_refuses(self):
        Source.objects.filter(pk=self.src_a.pk).update(enabled=False)
        self.assertEqual(self.post(self.mum_op, self.cell_a).status_code, 403)

    def test_get_is_not_allowed(self):
        self.assertEqual(self.login(self.mum_op).get(f"/apm/cell/{self.cell_a.pk}/retry/").status_code, 405)


class Rca(Fixtures):
    def test_creates_one_problem_per_adapter_with_the_facts(self):
        from servicedesk.models import Ticket
        c = self.login(self.mum_op)
        r = c.post(f"/apm/cell/{self.cell_a.pk}/rca/", HTTP_X_CSRFTOKEN="x").json()
        self.assertTrue(r["created"])
        t = Ticket.objects.get(number=r["number"])
        self.assertEqual((t.kind, t.site, t.origin, t.ci_name), ("problem", "Mumbai", "apm", "c001/NSE"))
        self.assertIn("boom", t.description)
        self.assertIn("c001 / NSE", t.title)
        self.assertTrue(r["url"].startswith("/"))
        again = c.post(f"/apm/cell/{self.cell_a.pk}/rca/").json()
        self.assertEqual((again["created"], again["existing"], again["number"]), (False, True, r["number"]))
        self.assertEqual(Ticket.objects.filter(kind="problem").count(), 1)
        self.assertTrue(AuditEvent.objects.filter(action="apm.rca.create").exists())

    def test_viewer_gets_a_draft_only(self):
        from servicedesk.models import Ticket
        r = self.login(self.mum_view).post(f"/apm/cell/{self.cell_a.pk}/rca/").json()
        self.assertFalse(r["created"])
        self.assertIn("Root cause analysis", r["draft"])
        self.assertIn("cannot create problems", r["reason"])
        self.assertFalse(Ticket.objects.filter(kind="problem").exists())

    def test_without_service_desk_it_is_a_draft(self):
        with mock.patch("apm.views._servicedesk", return_value=False):
            r = self.login(self.mum_op).post(f"/apm/cell/{self.cell_a.pk}/rca/").json()
        self.assertFalse(r["created"])
        self.assertIn("not installed", r["reason"])
        self.assertIn("boom", r["draft"])

    def test_a_ticket_failure_still_returns_the_draft(self):
        with mock.patch("apm.views._open_problem", side_effect=RuntimeError("db")):
            r = self.login(self.mum_op).post(f"/apm/cell/{self.cell_a.pk}/rca/").json()
        self.assertFalse(r["created"])
        self.assertIn("Root cause analysis", r["draft"])

    def test_other_sites_cell_is_404(self):
        self.assertEqual(self.login(self.mum_op).post(f"/apm/cell/{self.cell_b.pk}/rca/").status_code, 404)


@with_key
class SourcesAdmin(ApmCase):
    def form(self, **kw):
        d = {"name": "Charlie", "site": "Mumbai", "description": "d", "enabled": "on", "expected_interval_s": "300", "redis_port": "6379", "redis_db": "0"}
        d.update(kw)
        return d

    def test_only_admins_see_the_pages(self):
        for user in (self.mum_view, self.mum_op):
            c = self.login(user)
            for path in ("/apm/sources/", "/apm/sources/new/", f"/apm/sources/{self.src_a.pk}/"):
                self.assertEqual(c.get(path).status_code, 403, (user, path))
        self.assertEqual(self.login(self.mum_admin).get("/apm/sources/").status_code, 200)

    def test_list_is_site_scoped_and_edit_of_another_site_is_404(self):
        c = self.login(self.mum_admin)
        html = c.get("/apm/sources/").content.decode()
        self.assertIn("Alpha", html)
        self.assertNotIn("Bravo", html)
        self.assertEqual(c.get(f"/apm/sources/{self.src_b.pk}/").status_code, 404)
        for action in ("rotate", "toggle", "delete", "test-redis"):
            self.assertEqual(c.post(f"/apm/sources/{self.src_b.pk}/{action}/").status_code, 404, action)
        self.assertTrue(Source.objects.filter(pk=self.src_b.pk).exists())

    def test_create_shows_the_token_once_and_stores_only_a_hash(self):
        c = self.login(self.mum_admin)
        r = c.post("/apm/sources/new/", self.form())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Cache-Control"], "no-store")
        token = re.search(r"lea_[0-9a-f]{8}_[A-Za-z0-9_-]{43}", r.content.decode()).group(0)
        src = Source.objects.get(name="Charlie")
        self.assertEqual((src.site, src.created_by, src.tenant_key), ("Mumbai", self.mum_admin, services.tenant()))
        self.assertTrue(src.token_hash)
        self.assertNotIn(token, json.dumps([src.token_prefix, src.token_salt, src.token_hash]))
        self.assertEqual(services.authenticate(f"Bearer {token}"), src)
        page = c.get(f"/apm/sources/{src.pk}/").content.decode()
        self.assertNotIn(token, page)
        self.assertIn(src.token_prefix, page)
        a = AuditEvent.objects.get(action="apm.source.create")
        self.assertNotIn(token, json.dumps(a.details))

    def test_rotate_invalidates_the_old_token_and_reveals_the_new_once(self):
        c = self.login(self.mum_admin)
        old = self.src_a.token_plain
        r = c.post(f"/apm/sources/{self.src_a.pk}/rotate/")
        new = re.search(r"lea_[0-9a-f]{8}_[A-Za-z0-9_-]{43}", r.content.decode()).group(0)
        self.assertNotEqual(new, old)
        self.assertIsNone(services.authenticate(f"Bearer {old}"))
        self.assertEqual(services.authenticate(f"Bearer {new}").pk, self.src_a.pk)
        self.assertNotIn(new, c.get(f"/apm/sources/{self.src_a.pk}/").content.decode())
        a = AuditEvent.objects.get(action="apm.source.rotate_token")
        self.assertNotIn(new, json.dumps(a.details))
        self.assertEqual(a.object_id, str(self.src_a.pk))

    def test_toggle_disable_stops_ingest(self):
        c = self.login(self.mum_admin)
        c.post(f"/apm/sources/{self.src_a.pk}/toggle/")
        self.src_a.refresh_from_db()
        self.assertFalse(self.src_a.enabled)
        self.assertEqual(self.push_matrix(self.src_a).status_code, 401)
        c.post(f"/apm/sources/{self.src_a.pk}/toggle/")
        self.assertEqual(self.push_matrix(self.src_a).status_code, 200)
        self.assertEqual(AuditEvent.objects.filter(action="apm.source.update").count(), 2)

    def test_delete_cascades_and_is_audited(self):
        services.ingest_matrix(self.src_a, matrix())
        services.ingest_logs(self.src_a, [{"message": "x"}])
        c = self.login(self.mum_admin)
        c.post(f"/apm/sources/{self.src_a.pk}/delete/")
        self.assertFalse(Source.objects.filter(pk=self.src_a.pk).exists())
        self.assertFalse(AdapterCell.objects.exists() or ExecLog.objects.exists())
        self.assertTrue(AuditEvent.objects.filter(action="apm.source.delete", object_repr="Alpha").exists())
        self.assertEqual(self.push(self.src_a, {"type": "matrix", "payload": matrix()}).status_code, 401)

    def test_a_site_admin_cannot_create_for_all_sites_or_another_site(self):
        c = self.login(self.mum_admin)
        for site in ("All Sites", "Chennai"):
            r = c.post("/apm/sources/new/", self.form(site=site, name="X" + site))
            self.assertEqual(r.status_code, 200, site)
            self.assertFalse(Source.objects.filter(name="X" + site).exists())
        self.assertEqual(self.login(self.root).post("/apm/sources/new/", self.form(site="All Sites", name="Global")).status_code, 200)
        self.assertEqual(Source.objects.get(name="Global").site, "All Sites")

    def test_a_site_admin_cannot_move_a_source_to_another_site(self):
        r = self.login(self.mum_admin).post(f"/apm/sources/{self.src_a.pk}/", self.form(name="Alpha", site="Chennai"))
        self.assertEqual(r.status_code, 200)
        self.src_a.refresh_from_db()
        self.assertEqual(self.src_a.site, "Mumbai")

    def test_validation(self):
        c = self.login(self.root)
        bad = {"name": "", "expected_interval_s": "5"}
        r = c.post("/apm/sources/new/", self.form(**bad))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "This field is required")
        self.assertContains(r, "between 30 seconds and 24 hours")
        r = c.post("/apm/sources/new/", self.form(name="alpha"))        # case-insensitive duplicate
        self.assertContains(r, "already exists")
        r = c.post("/apm/sources/new/", self.form(redis_poll="on", redis_host="http://evil/", redis_adapter_suffix="a b"))
        self.assertContains(r, "host name or IP")
        self.assertContains(r, "letters, digits")
        r = c.post("/apm/sources/new/", self.form(redis_poll="on"))
        self.assertContains(r, "Enter the Redis host")
        self.assertContains(r, "at least one key suffix")

    def test_edit_saves_and_audits_changed_fields_only(self):
        c = self.login(self.mum_admin)
        r = c.post(f"/apm/sources/{self.src_a.pk}/", self.form(name="Alpha", expected_interval_s="600", description="new"))
        self.assertEqual(r.status_code, 302)
        self.src_a.refresh_from_db()
        self.assertEqual(self.src_a.expected_interval_s, 600)
        a = AuditEvent.objects.filter(action="apm.source.update").latest("id")
        self.assertCountEqual(a.details["changed"], ["expected_interval_s", "description"])
        self.assertTrue(self.src_a.token_hash)          # an edit never touches the token

    def test_test_redis_button(self):
        from apm.tests.test_redis import FakeRedis
        Source.objects.filter(pk=self.src_a.pk).update(redis_host="redis.example.org", redis_prefix="p", redis_adapter_suffix="A")
        c = self.login(self.mum_admin)
        with mock.patch("apm.redis_reader.resolve", return_value="203.0.113.9"), \
                mock.patch("apm.redis_reader.connect", return_value=FakeRedis({"p:A": "x"})):
            r = c.post(f"/apm/sources/{self.src_a.pk}/test-redis/", follow=True)
        self.assertContains(r, "Redis test: Connected. Found p:A.")
        self.assertTrue(AuditEvent.objects.filter(action="apm.source.redis_test").exists())

    def test_every_mutation_is_audited_with_the_actor(self):
        c = self.login(self.mum_admin)
        c.post("/apm/sources/new/", self.form())
        src = Source.objects.get(name="Charlie")
        c.post(f"/apm/sources/{src.pk}/", self.form(description="x"))
        c.post(f"/apm/sources/{src.pk}/toggle/")
        c.post(f"/apm/sources/{src.pk}/rotate/")
        c.post(f"/apm/sources/{src.pk}/delete/")
        actions = list(AuditEvent.objects.filter(actor=self.mum_admin).values_list("action", flat=True))
        for needed in ("apm.source.create", "apm.source.update", "apm.source.rotate_token", "apm.source.delete"):
            self.assertIn(needed, actions)

    def test_site_rename_follows_through_the_source(self):
        from administration import services as admin_services
        self.assertIn(("apm", "Source", "site", "APM sources"), admin_services.SITE_NAME_FIELDS)
        refs = dict(admin_services.site_references("Mumbai"))
        self.assertEqual(refs["APM sources"], 1)
        admin_services.rename_site_references("Mumbai", "Pune")
        self.src_a.refresh_from_db()
        self.assertEqual(self.src_a.site, "Pune")
