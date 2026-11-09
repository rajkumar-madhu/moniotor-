"""BOD / EOD live integration: JSON endpoint, in-place patching contract, re-check actions.

Nothing here probes a network: the referenced checks are rows with stored results, and every re-check goes through a
mocked `services.refresh_references` (the real one is covered by the existing refresh tests)."""
import json
import re
from concurrent.futures import Future
from datetime import datetime, time, timedelta, timezone as dt_timezone
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from core.models import AuditEvent, Membership, Site
from domainmonitor.models import Domain
from tcpcheck.models import TcpCheck
from urlchecker.models import UrlCheck

from . import live, services
from .models import ChecklistItem, Mode, Run, RunItem
from .tests import client_for, make_mode, url_check, users

STATIC_JS = Path(__file__).resolve().parent / "static" / "bodeod" / "bodeod.js"


class ImmediateExecutor:
    """Runs a re-check job on the calling thread (the real one uses a worker thread with its own DB connection)."""

    def submit(self, fn, /, *args, **kwargs):
        fut = Future()
        try:
            fut.set_result(fn(*args, **kwargs))
        except Exception as e:  # noqa: BLE001
            fut.set_exception(e)
        return fut


class HangingExecutor:
    """Accepts the job and never finishes it: a probe that is still busy."""

    def __init__(self):
        self.jobs = []

    def submit(self, fn, /, *args, **kwargs):
        self.jobs.append((fn, args, kwargs))
        return Future()


def domain(host="nseindia.com", days=41, **kw):
    fields = {"tenant_key": services.tenant(), "site_key": "All Sites", "hostname": host, "status": "up", "dns_status": "Resolved",
              "ssl_status": "Valid", "ssl_expires_at": timezone.now() + timedelta(days=days, hours=1),
              "last_checked_at": timezone.now() - timedelta(seconds=40)}
    fields.update(kw)
    return Domain.objects.create(**fields)


def api(client, **params):
    return client.get(reverse("bodeod:api_live"), params)


class LiveBase(TestCase):
    def setUp(self):
        cache.clear()
        self.viewer, self.operator, self.admin = users()
        self.mode = make_mode()
        self.domain = domain()
        self.url = url_check("Portal")
        self.tcp = TcpCheck.objects.create(name="Gateway", host="10.1.1.1", port=22, site="All Sites", last_status="online",
                                           last_checked_at=timezone.now(), last_response_ms=3)
        self.i_domain = ChecklistItem.objects.create(mode=self.mode, name="nseindia.com DNS & SSL", kind="domain",
                                                     ref_name="nseindia.com", order=10)
        self.i_url = ChecklistItem.objects.create(mode=self.mode, name="Portal up", kind="url_check", ref_name="Portal", order=20)
        self.i_tcp = ChecklistItem.objects.create(mode=self.mode, name="Gateway open", kind="tcp_check", ref_name="Gateway",
                                                  order=30, critical=False)
        self.i_manual = ChecklistItem.objects.create(mode=self.mode, name="Feed", kind="manual", order=40)

    def items(self, response):
        return {i["name"]: i for i in response.json()["modes"][0]["items"]}


# ------------------------------------------------------------------ the JSON endpoint
class ApiShapeTests(LiveBase):
    def test_shape_and_values(self):
        r = api(client_for(self.viewer))
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r["Content-Type"].startswith("application/json"))
        self.assertIn("no-store", r["Cache-Control"])
        body = r.json()
        self.assertEqual(set(body), {"generated_at", "interval", "date", "scope", "status", "tone", "label", "counts", "modes"})
        self.assertEqual(body["scope"], "live")
        self.assertTrue(15 <= body["interval"] <= 30)
        mode = body["modes"][0]
        self.assertEqual((mode["code"], mode["enabled"], mode["rechecking"], mode["run"]), ("BOD", True, False, None))
        self.assertEqual(set(mode), {"code", "id", "name", "enabled", "site", "status", "tone", "label", "counts", "run", "rechecking", "items"})
        d = self.items(r)["nseindia.com DNS & SSL"]
        self.assertEqual(set(d), {"key", "id", "name", "kind", "kind_label", "ref", "critical", "status", "tone", "code", "label", "detail",
                                  "detail_long", "link", "checked_at", "checked_at_ist", "age_s", "age"})
        self.assertEqual((d["key"], d["id"], d["kind"], d["status"], d["tone"], d["code"], d["label"]),
                         (f"i{self.i_domain.pk}", self.i_domain.pk, "domain", "ok", "ok", 0, "OK"))
        self.assertEqual(d["detail"], "DNS resolves; certificate valid, 41 days left")
        self.assertEqual(d["link"], reverse("domains:list") + f"?selected={self.domain.pk}")
        self.assertIn(d["age_s"], range(39, 46))
        self.assertTrue(d["age"].endswith(" s"))
        self.assertTrue(d["critical"])
        self.assertEqual(self.items(r)["Portal up"]["link"], reverse("urlchecker:detail", args=[self.url.pk]))

    def test_only_enabled_automated_items(self):
        ChecklistItem.objects.create(mode=self.mode, name="Off", kind="url_check", ref_name="Portal", enabled=False, order=50)
        r = api(client_for(self.viewer))
        names = set(self.items(r))
        self.assertEqual(names, {"nseindia.com DNS & SSL", "Portal up", "Gateway open"})
        self.assertEqual(r.json()["modes"][0]["counts"]["total"], 3)

    def test_status_vocabulary_and_mode_rules(self):
        UrlCheck.objects.filter(pk=self.url.pk).update(last_status="down", last_error="HTTP 503")
        TcpCheck.objects.filter(pk=self.tcp.pk).update(last_status="degraded", last_error="slow")
        Domain.objects.filter(pk=self.domain.pk).update(ssl_status="Expiring", ssl_expires_at=timezone.now() + timedelta(days=9, hours=1))
        r = api(client_for(self.viewer))
        items = self.items(r)
        self.assertEqual(items["Portal up"]["status"], "failed")            # stored "critical" is "failed" live
        self.assertEqual((items["Portal up"]["tone"], items["Portal up"]["code"]), ("crit", 2))
        self.assertEqual(items["Portal up"]["detail"], "Down: HTTP 503")
        self.assertEqual(items["Gateway open"]["status"], "warning")
        self.assertEqual(items["nseindia.com DNS & SSL"]["status"], "warning")
        self.assertEqual(items["nseindia.com DNS & SSL"]["detail"], "DNS resolves; certificate expiring in 9 days")
        mode = r.json()["modes"][0]
        self.assertEqual(mode["status"], "failed")                          # a critical item failed
        self.assertEqual(mode["counts"], {"ok": 0, "warning": 2, "failed": 1, "unknown": 0, "total": 3})
        self.assertEqual(r.json()["status"], "failed")

    def test_unknown_for_missing_references_and_never_checked(self):
        ChecklistItem.objects.create(mode=self.mode, name="Ghost", kind="url_check", ref_name="No such check", order=60)
        UrlCheck.objects.filter(pk=self.url.pk).update(last_checked_at=None, last_status="unknown")
        items = self.items(api(client_for(self.viewer)))
        self.assertEqual(items["Ghost"]["status"], "unknown")
        self.assertEqual(items["Ghost"]["detail"], "URL check not found")
        self.assertEqual(items["Ghost"]["link"], "")
        self.assertIsNone(items["Ghost"]["age_s"])
        self.assertEqual(items["Portal up"]["status"], "unknown")
        self.assertEqual(items["Portal up"]["detail"], "URL check has not run yet")

    def test_mode_status_without_automated_items_is_unknown(self):
        ChecklistItem.objects.exclude(kind="manual").delete()
        mode = api(client_for(self.viewer)).json()["modes"][0]
        self.assertEqual((mode["status"], mode["counts"]["total"], mode["items"]), ("unknown", 0, []))

    def test_summarize_rules(self):
        e = lambda status, critical=True: {"status": status, "critical": critical}  # noqa: E731
        s = lambda *entries: live.summarize_entries(list(entries))[0]  # noqa: E731
        self.assertEqual(s(e("ok"), e("ok")), "ok")
        self.assertEqual(s(e("ok"), e("unknown")), "unknown")
        self.assertEqual(s(e("ok"), e("warning"), e("unknown")), "warning")
        self.assertEqual(s(e("ok"), e("failed", critical=False)), "warning")  # a non-critical failure only warns
        self.assertEqual(s(e("warning"), e("failed")), "failed")
        self.assertEqual(s(), "unknown")

    def test_run_scope_has_recorded_status_and_row_keys(self):
        run = services.start_run(self.mode, refresh=False)
        UrlCheck.objects.filter(pk=self.url.pk).update(last_status="down", last_error="HTTP 503")
        r = api(client_for(self.viewer), run=run.pk)
        body = r.json()
        self.assertEqual(body["scope"], "run")
        items = self.items(r)
        row = run.results.get(name="Portal up")
        self.assertEqual(set(items), {"nseindia.com DNS & SSL", "Portal up", "Gateway open"})   # manual rows are not live
        self.assertEqual((items["Portal up"]["key"], items["Portal up"]["status"], items["Portal up"]["recorded"]),
                         (f"r{row.pk}", "failed", "ok"))
        self.assertEqual(body["modes"][0]["run"]["id"], run.pk)
        self.assertEqual(body["modes"][0]["run"]["url"], run.get_absolute_url())

    def test_latest_run_identity_follows_new_runs(self):
        first = services.start_run(self.mode, refresh=False)
        c = client_for(self.viewer)
        self.assertEqual(api(c).json()["modes"][0]["run"]["id"], first.pk)
        second = services.start_run(self.mode, refresh=False)
        run = api(c).json()["modes"][0]["run"]
        self.assertEqual((run["id"], run["status"]), (second.pk, second.status))

    def test_bad_parameters_are_json_404s(self):
        c = client_for(self.viewer)
        for params in ({"mode": "XYZ"}, {"run": "abc"}, {"run": "0"}, {"run": "-3"}, {"run": "99999"}):
            r = api(c, **params)
            self.assertEqual(r.status_code, 404, params)
            self.assertTrue(r["Content-Type"].startswith("application/json"))
        self.assertEqual(api(c, mode="BOD").status_code, 200)

    def test_anonymous_is_turned_away(self):
        r = Client().get(reverse("bodeod:api_live"))
        self.assertIn(r.status_code, (301, 302, 401, 403))
        self.assertNotIn("modes", r.content.decode() if r.status_code in (401, 403) else "")

    def test_post_is_not_allowed(self):
        self.assertEqual(client_for(self.viewer).post(reverse("bodeod:api_live")).status_code, 405)


class GetNeverProbesTests(LiveBase):
    def test_get_and_pages_make_no_probe(self):
        run = services.start_run(self.mode, refresh=False)
        with mock.patch("bodeod.services.refresh_references") as refresh, \
                mock.patch("probes.runner.run_url_checks") as urls, mock.patch("probes.runner.run_domain_checks") as domains, \
                mock.patch("tcpcheck.services.run_checks") as tcp, mock.patch("probes.checks.http_check") as http, \
                mock.patch("probes.checks.dns_check") as dns, mock.patch("probes.checks.tls_check") as tls:
            c = client_for(self.operator)
            for url in (reverse("bodeod:api_live"), reverse("bodeod:api_live") + f"?run={run.pk}", reverse("bodeod:status"),
                        reverse("bodeod:keys"), reverse("bodeod:checklist"), run.get_absolute_url()):
                self.assertEqual(c.get(url).status_code, 200, url)
        for m in (refresh, urls, domains, tcp, http, dns, tls):
            m.assert_not_called()

    def test_live_failure_never_signs_off_or_edits_a_run(self):
        run = services.start_run(self.mode, refresh=False)
        before = list(run.results.values_list("pk", "status", "detail", "signed_off_at"))
        run_before = Run.objects.filter(pk=run.pk).values_list("status", "summary", "finished_at").get()
        UrlCheck.objects.filter(pk=self.url.pk).update(last_status="down", last_error="HTTP 503")
        Domain.objects.filter(pk=self.domain.pk).update(status="down", last_error="boom")
        c = client_for(self.operator)
        api(c)
        api(c, run=run.pk)
        with mock.patch.object(live, "_executor", ImmediateExecutor()), \
                mock.patch("bodeod.services.refresh_references", return_value=services.RefreshOutcome(ran=3)):
            self.assertEqual(c.post(reverse("bodeod:api_recheck"), {"mode": self.mode.pk, "run": run.pk}).status_code, 200)
        self.assertEqual(list(run.results.values_list("pk", "status", "detail", "signed_off_at")), before)
        self.assertEqual(Run.objects.filter(pk=run.pk).values_list("status", "summary", "finished_at").get(), run_before)
        self.assertFalse(RunItem.objects.filter(status="signed_off").exists())


# ------------------------------------------------------------------ scoping per site and role
class ScopingTests(TestCase):
    def setUp(self):
        cache.clear()
        self.mumbai = Site.objects.create(name="Mumbai", code="mumbai")
        self.delhi = Site.objects.create(name="Delhi", code="delhi")
        self.m_mode = make_mode("BOD", site="Mumbai")
        self.d_mode = make_mode("EOD", site="Delhi", window_start=time(15, 30), window_end=time(16, 0), auto_run_time=None)
        self.g_mode = make_mode("ADP", window_start=time(16, 0), window_end=time(20, 0), auto_run_time=None)
        UrlCheck.objects.create(name="Mumbai portal", url="https://m.example.com/", site="Mumbai", last_status="online",
                                last_checked_at=timezone.now())
        UrlCheck.objects.create(name="Delhi portal", url="https://d.example.com/", site="Delhi", last_status="down",
                                last_error="refused", last_checked_at=timezone.now())
        ChecklistItem.objects.create(mode=self.m_mode, name="Mumbai up", kind="url_check", ref_name="Mumbai portal")
        ChecklistItem.objects.create(mode=self.m_mode, name="Peek at Delhi", kind="url_check", ref_name="Delhi portal", order=20)
        ChecklistItem.objects.create(mode=self.d_mode, name="Delhi up", kind="url_check", ref_name="Delhi portal")
        ChecklistItem.objects.create(mode=self.g_mode, name="Global up", kind="url_check", ref_name="Mumbai portal")
        self.m_user = User.objects.create_user("m", password="pw")
        Membership.objects.create(user=self.m_user, site=self.mumbai, role="operator")
        self.m_viewer = User.objects.create_user("mv", password="pw")
        Membership.objects.create(user=self.m_viewer, site=self.mumbai, role="viewer")
        self.root = User.objects.create_user("root", password="pw")
        Membership.objects.create(user=self.root, site=None, role="operator")

    def test_site_member_sees_own_and_global_modes_only(self):
        body = api(client_for(self.m_user)).json()
        self.assertEqual(sorted(m["code"] for m in body["modes"]), ["ADP", "BOD"])
        self.assertNotIn("Delhi up", json.dumps(body))
        self.assertEqual(api(client_for(self.m_user), mode="EOD").status_code, 404)
        self.assertEqual(len(api(client_for(self.root)).json()["modes"]), 3)

    def test_site_mode_never_reads_another_sites_check(self):
        items = {i["name"]: i for i in api(client_for(self.m_user), mode="BOD").json()["modes"][0]["items"]}
        self.assertEqual(items["Mumbai up"]["status"], "ok")
        self.assertEqual(items["Peek at Delhi"]["status"], "unknown")
        self.assertEqual(items["Peek at Delhi"]["detail"], "URL check not found")
        self.assertNotIn("refused", json.dumps(items))

    def test_runs_of_another_site_are_404(self):
        run = services.start_run(self.d_mode, refresh=False)
        self.assertEqual(api(client_for(self.m_user), run=run.pk).status_code, 404)
        self.assertEqual(api(client_for(self.root), run=run.pk).status_code, 200)

    def test_recheck_needs_operate_on_the_modes_own_site(self):
        post = lambda c, mode: c.post(reverse("bodeod:api_recheck"), {"mode": mode.pk})  # noqa: E731
        with mock.patch.object(live, "_executor", ImmediateExecutor()), \
                mock.patch("bodeod.services.refresh_references", return_value=services.RefreshOutcome(ran=1)) as refresh:
            self.assertEqual(post(client_for(self.m_user), self.m_mode).status_code, 200)
            self.assertEqual(post(client_for(self.m_user), self.d_mode).status_code, 404)            # not visible
            self.assertEqual(post(client_for(self.m_user), self.g_mode).status_code, 403)            # global needs tenant-wide
            self.assertEqual(post(client_for(self.m_viewer), self.m_mode).status_code, 403)          # viewer
            self.assertEqual(post(client_for(self.root), self.g_mode).status_code, 200)
        self.assertEqual(refresh.call_count, 2)

    def test_buttons_follow_permissions(self):
        self.assertContains(client_for(self.m_user).get(reverse("bodeod:keys") + "?mode=BOD"), "data-recheck")
        self.assertNotContains(client_for(self.m_viewer).get(reverse("bodeod:keys") + "?mode=BOD"), "data-recheck")
        # a one-site operator cannot re-check the global mode: no button there either
        page = client_for(self.m_user).get(reverse("bodeod:keys") + "?mode=ADP")
        self.assertNotContains(page, "data-recheck")


# ------------------------------------------------------------------ re-check
class RecheckTests(LiveBase):
    def setUp(self):
        super().setUp()
        self.url_ = reverse("bodeod:api_recheck")
        self.c = client_for(self.operator)
        patch = mock.patch.object(live, "_executor", ImmediateExecutor())
        patch.start()
        self.addCleanup(patch.stop)

    def post(self, data=None, client=None):
        return (client or self.c).post(self.url_, data if data is not None else {"mode": self.mode.pk})

    def refreshed(self, outcome=None):
        return mock.patch("bodeod.services.refresh_references", return_value=outcome or services.RefreshOutcome(ran=3))

    def test_mode_recheck_refreshes_only_automated_enabled_items(self):
        ChecklistItem.objects.create(mode=self.mode, name="Off", kind="url_check", ref_name="Portal", enabled=False, order=50)
        with self.refreshed() as refresh:
            r = self.post()
        self.assertEqual(r.status_code, 200)
        refs = refresh.call_args.args[0]
        self.assertEqual(sorted(i.name for i in refs), ["Gateway open", "Portal up", "nseindia.com DNS & SSL"])
        self.assertTrue(all(i.kind != "manual" for i in refs))
        body = r.json()
        self.assertEqual((body["state"], body["ran"], body["failed"]), ("done", 3, []))
        self.assertEqual(body["mode"]["code"], "BOD")
        self.assertEqual(len(body["mode"]["items"]), 3)
        self.assertFalse(live.is_rechecking(self.mode))                 # the lock is gone

    def test_single_item_recheck_and_manual_items_refused(self):
        with self.refreshed(services.RefreshOutcome(ran=1)) as refresh:
            r = self.post({"mode": self.mode.pk, "item": f"i{self.i_url.pk}"})
            self.assertEqual(r.status_code, 200)
            self.assertEqual([i.name for i in refresh.call_args.args[0]], ["Portal up"])
            bad = self.post({"mode": self.mode.pk, "item": f"i{self.i_manual.pk}"})
            self.assertEqual(bad.status_code, 400)
            self.assertEqual(refresh.call_count, 1)                        # a manual row is never refreshed

    def test_item_keys_are_validated(self):
        with self.refreshed() as refresh:
            for bad in ("x1", "i", "i-4", "i1;drop", "r1\n", "../1"):
                self.assertEqual(self.post({"mode": self.mode.pk, "item": bad}).status_code, 400, bad)
            self.assertEqual(self.post({"mode": "abc"}).status_code, 404)
            self.assertEqual(self.post({"mode": "0"}).status_code, 404)
            self.assertEqual(self.post({}).status_code, 404)
            self.assertEqual(self.post({"mode": self.mode.pk, "run": "9999"}).status_code, 404)
        refresh.assert_not_called()

    def test_run_scope_rechecks_the_runs_rows(self):
        run = services.start_run(self.mode, refresh=False)
        row = run.results.get(name="Portal up")
        with self.refreshed(services.RefreshOutcome(ran=1)) as refresh:
            r = self.post({"mode": self.mode.pk, "run": run.pk, "item": f"r{row.pk}"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual([i.pk for i in refresh.call_args.args[0]], [row.pk])
        self.assertEqual([i["key"] for i in r.json()["mode"]["items"]], [f"r{x.pk}" for x in run.results.filter(kind__in=live.AUTOMATED)])
        self.assertEqual(Run.objects.get(pk=run.pk).status, run.status)

    def test_permission_viewer_forbidden_json(self):
        with self.refreshed() as refresh:
            r = self.post(client=client_for(self.viewer))
        self.assertEqual(r.status_code, 403)
        self.assertTrue(r["Content-Type"].startswith("application/json"))
        refresh.assert_not_called()
        self.assertFalse(AuditEvent.objects.filter(action="recheck").exists())

    def test_csrf_is_enforced(self):
        c = Client(enforce_csrf_checks=True)
        c.force_login(self.operator)
        with self.refreshed() as refresh:
            self.assertEqual(c.post(self.url_, {"mode": self.mode.pk}).status_code, 403)
        refresh.assert_not_called()

    def test_get_is_not_allowed(self):
        self.assertEqual(self.c.get(self.url_).status_code, 405)

    def test_audited(self):
        with self.refreshed():
            self.post({"mode": self.mode.pk, "item": f"i{self.i_domain.pk}"})
        e = AuditEvent.objects.get(action="recheck")
        self.assertEqual((e.actor_name, e.object_type, e.object_id), ("operator", "bodeod.mode", str(self.mode.pk)))
        self.assertEqual(e.details["state"], "done")
        self.assertEqual(e.details["items"], ["nseindia.com DNS & SSL"])
        self.assertEqual(e.details["mode"], "BOD")

    def test_rate_limit(self):
        with self.refreshed(), mock.patch.object(live, "RECHECK_LIMIT", 2):
            self.assertEqual(self.post().status_code, 200)
            self.assertEqual(self.post().status_code, 200)
            r = self.post()
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r["Retry-After"], "30")
        self.assertEqual(AuditEvent.objects.filter(action="recheck").count(), 2)
        # another user has their own budget
        with self.refreshed():
            self.assertEqual(self.post(client=client_for(self.admin)).status_code, 200)

    def test_live_polling_is_rate_limited_per_user(self):
        c = client_for(self.viewer)
        with mock.patch.object(live, "LIVE_LIMIT", 3):
            codes = [api(c).status_code for _ in range(5)]
        self.assertEqual(codes, [200, 200, 200, 429, 429])
        self.assertEqual(api(client_for(self.operator)).status_code, 200)

    def test_disabled_mode_is_refused(self):
        Mode.objects.filter(pk=self.mode.pk).update(enabled=False)
        with self.refreshed() as refresh:
            self.assertEqual(self.post().status_code, 409)
        refresh.assert_not_called()

    def test_outcomes(self):
        cases = [(services.RefreshOutcome(ran=2, failed={"domain"}), "partial"),
                 (services.RefreshOutcome(ran=0, failed={"url_check"}), "failed"),
                 (services.RefreshOutcome(ran=0), "skipped")]
        for outcome, state in cases:
            with self.refreshed(outcome):
                r = self.post()
            self.assertEqual((r.status_code, r.json()["state"]), (200, state))
        with mock.patch("bodeod.services.refresh_references", side_effect=RuntimeError("probe pool down")):
            self.assertEqual(self.post().json()["state"], "failed")
        self.assertFalse(live.is_rechecking(self.mode))                 # even a crashing job frees the mode

    def test_nothing_eligible_is_skipped_not_run(self):
        ChecklistItem.objects.exclude(kind="manual").delete()
        with self.refreshed() as refresh:
            r = self.post()
        self.assertEqual(r.json()["state"], "skipped")
        refresh.assert_not_called()

    def test_slow_probe_is_bounded_and_reported_as_running(self):
        hang = HangingExecutor()
        with mock.patch.object(live, "_executor", hang), mock.patch.object(live, "RECHECK_WAIT", 0.05), self.refreshed():
            r = self.post()
            self.assertEqual((r.status_code, r.json()["state"]), (202, "running"))
            self.assertTrue(r.json()["mode"]["rechecking"])
            self.assertTrue(api(self.c).json()["modes"][0]["rechecking"])           # other viewers see it too
            again = self.post()                                                      # no second probe is started
            self.assertEqual((again.status_code, again.json()["state"]), (202, "busy"))
        self.assertEqual(len(hang.jobs), 1)
        fn, args, kwargs = hang.jobs[0]
        with self.refreshed():
            fn(*args, **kwargs)                                                      # the background job finishes
        self.assertFalse(api(self.c).json()["modes"][0]["rechecking"])

    def test_the_real_executor_runs_jobs_on_a_worker_thread(self):
        import threading
        ex = live._DbThreadExecutor(max_workers=1)
        try:
            self.assertNotEqual(ex.submit(threading.get_ident).result(timeout=5), threading.get_ident())
        finally:
            ex.shutdown()


# ------------------------------------------------------------------ page integration and the JS contract
class PageTests(LiveBase):
    def test_automated_rows_carry_hooks_manual_and_disabled_do_not(self):
        ChecklistItem.objects.create(mode=self.mode, name="Off", kind="url_check", ref_name="Portal", enabled=False, order=50)
        for name in ("bodeod:keys", "bodeod:checklist"):
            html = client_for(self.operator).get(reverse(name)).content.decode()
            self.assertEqual(len(re.findall(r'data-live-row="i\d+"', html)), 3, name)
            self.assertIn(f'data-live-row="i{self.i_domain.pk}"', html)
            self.assertNotIn(f'data-live-row="i{self.i_manual.pk}"', html)
            self.assertIn('id="bod-live-cfg"', html)
            self.assertIn("data-bod-live", html)
            self.assertIn('data-live="age"', html)
        html = client_for(self.operator).get(reverse("bodeod:keys")).content.decode()
        self.assertIn("DNS resolves; certificate valid, 41 days left", html)

    def test_status_page_has_no_reload_loop_but_a_noscript_fallback(self):
        r = client_for(self.viewer).get(reverse("bodeod:status"))
        html = r.content.decode()
        self.assertNotIn("data-bod-refresh", html)
        self.assertIn('<noscript><meta http-equiv="refresh" content="60"></noscript>', html)
        self.assertIn("data-live-mode=\"BOD\"", html)
        self.assertIn('data-live="mode-pill"', html)
        js = STATIC_JS.read_text()
        self.assertNotIn("setInterval(function () { if (!hold", js)
        self.assertEqual(len(re.findall(r"location\.reload", js)), 1)          # only the explicit "Reload the page" link
        self.assertIn("data-live-reload", js)

    def test_keys_totals_tiles_are_flagged_so_the_script_can_say_they_are_out_of_date(self):
        html = client_for(self.operator).get(reverse("bodeod:keys")).content.decode()
        self.assertIn("data-live-kpis", html)
        js = STATIC_JS.read_text()
        self.assertIn("[data-live-kpis]", js)
        # a status card without a run shows live counts, which go stale the same way; a card with a run shows recorded counts
        self.assertIn("data-live-kpis", client_for(self.viewer).get(reverse("bodeod:status")).content.decode())
        services.start_run(self.mode, refresh=False)
        self.assertNotIn("data-live-kpis", client_for(self.viewer).get(reverse("bodeod:status")).content.decode())
        self.assertIn("totals on this page are out of date", js)

    def test_status_cards_without_a_run_patch_cells_cards_with_a_run_do_not(self):
        html = client_for(self.viewer).get(reverse("bodeod:status")).content.decode()
        self.assertEqual(len(re.findall(r'<li class="cell[^"]*" data-live-row="i\d+"', html)), 3)
        services.start_run(self.mode, refresh=False)
        html = client_for(self.viewer).get(reverse("bodeod:status")).content.decode()
        self.assertNotIn("data-live-row", html)
        self.assertIn('data-live-mode="BOD"', html)                           # the card still shows the live summary

    def test_detail_live_column_only_for_the_open_run(self):
        run = services.start_run(self.mode, refresh=False)
        c = client_for(self.operator)
        html = c.get(run.get_absolute_url()).content.decode()
        self.assertEqual(len(re.findall(r'data-live-row="r\d+"', html)), 3)
        self.assertIn("Live now", html)
        self.assertIn('data-recheck', html)
        self.assertIn(f'"run": {run.pk}', html)
        Run.objects.filter(pk=run.pk).update(trading_date=run.trading_date - timedelta(days=3))
        old = c.get(run.get_absolute_url()).content.decode()
        self.assertNotIn("Live now", old)
        self.assertNotIn("data-bod-live", old)
        self.assertNotIn("bod-live-cfg", old)

    def test_detail_marks_rows_that_differ_from_the_run(self):
        run = services.start_run(self.mode, refresh=False)
        UrlCheck.objects.filter(pk=self.url.pk).update(last_status="down", last_error="HTTP 503")
        html = client_for(self.viewer).get(run.get_absolute_url()).content.decode()
        row = re.search(r'<tr class="[^"]*" data-live-row="r%d".*?</tr>' % run.results.get(name="Portal up").pk, html, re.S).group(0)
        self.assertIn('data-recorded="ok"', row)
        self.assertIn('data-live-status="failed"', row)
        self.assertNotRegex(row, r'data-live="diff"[^>]* hidden')
        ok_row = re.search(r'<tr class="[^"]*" data-live-row="r%d".*?</tr>' % run.results.get(name="Gateway open").pk, html, re.S).group(0)
        self.assertRegex(ok_row, r'data-live="diff"[^>]* hidden')

    def test_viewers_see_live_data_but_no_buttons(self):
        for name in ("bodeod:keys", "bodeod:checklist", "bodeod:status"):
            html = client_for(self.viewer).get(reverse(name)).content.decode()
            self.assertNotIn("data-recheck", html, name)
            self.assertIn("data-bod-live", html, name)

    def test_live_bar_markup_for_assistive_tech(self):
        html = client_for(self.viewer).get(reverse("bodeod:keys")).content.decode()
        self.assertIn('aria-live="polite"', html)
        self.assertIn('data-live-stale', html)
        self.assertRegex(html, r'data-live-stale role="status" hidden')
        self.assertIn('data-live-toggle aria-pressed="false"', html)
        self.assertIn("Changes since you opened this page", html)
        self.assertIn("<noscript>", html)

    def test_mode_save_error_page_still_renders_without_live(self):
        r = client_for(self.admin).post(reverse("bodeod:mode_save"), {"code": "BOD", "name": ""})
        self.assertEqual(r.status_code, 400)

    def test_hostile_names_are_escaped_in_pages_and_inert_in_json(self):
        bad = '<img src=x onerror=alert(1)>"\'</script><script>alert(2)</script>'
        UrlCheck.objects.create(name=bad, url="https://example.com/", last_status="down", last_error=bad,
                                last_checked_at=timezone.now())
        ChecklistItem.objects.create(mode=self.mode, name=bad, kind="url_check", ref_name=bad, order=70)
        c = client_for(self.operator)
        for url in (reverse("bodeod:keys"), reverse("bodeod:checklist"), reverse("bodeod:status")):
            html = c.get(url).content.decode()
            self.assertNotIn("<img src=x onerror", html, url)
            self.assertNotIn("<script>alert(2)", html, url)
        r = api(c)
        self.assertTrue(r["Content-Type"].startswith("application/json"))
        self.assertEqual(r["X-Content-Type-Options"], "nosniff")
        item = [i for i in r.json()["modes"][0]["items"] if i["name"] == bad][0]
        self.assertEqual((item["ref"], item["detail"]), (bad, f"Down: {bad}"))         # raw text; the JS only uses textContent
        cfg = re.search(r'<script id="bod-live-cfg" type="application/json">(.*?)</script>', c.get(reverse("bodeod:keys")).content.decode(), re.S)
        self.assertNotIn("alert(", cfg.group(1))

    def test_javascript_contract(self):
        js = STATIC_JS.read_text()
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "$.html("):
            self.assertNotIn(banned, js, banned)
        self.assertIn("MIN_MS = 15000", js)                                     # polls every 15-30 s
        self.assertIn("MAX_MS = 30000", js)
        self.assertIn("document.hidden", js)                                    # paused in a hidden tab
        self.assertIn("formFocused", js)                                        # and while a field has focus
        self.assertIn("age > 3 * interval", js)                                 # stale banner after three missed intervals
        self.assertIn("Retry-After", js)                                        # honours the server's back-off
        self.assertIn("MAX_BACKOFF_MS", js)
        self.assertIn("X-CSRFToken", js)                                        # re-check is a CSRF-protected POST
        self.assertIn("textContent", js)
        self.assertIn("aria-busy", js)
        self.assertIn("MAX_CHANGES", js)
        self.assertLessEqual(js.count("aria-live"), 0)                          # the live region lives in the template
        self.assertIn('"interval"', json.dumps({"interval": live.INTERVAL}))
        self.assertTrue(15 <= live.INTERVAL <= 30)


# ------------------------------------------------------------------ queries and time
class QueryCountTests(LiveBase):
    def count(self, **params):
        c = client_for(self.viewer)
        api(c, **params)                                    # warm caches (content types, sessions…)
        with CaptureQueriesContext(connection) as ctx:
            r = api(c, **params)
        self.assertEqual(r.status_code, 200)
        return len(ctx)

    def test_query_count_is_independent_of_item_count(self):
        few = self.count()
        for n in range(30):
            kind = ("url_check", "domain", "tcp_check")[n % 3]
            ref = {"url_check": "Portal", "domain": "nseindia.com", "tcp_check": "Gateway"}[kind]
            ChecklistItem.objects.create(mode=self.mode, name=f"Extra {n}", kind=kind, ref_name=ref, order=100 + n)
        self.assertEqual(self.count(), few)
        run = services.start_run(self.mode, refresh=False)
        run_few = self.count(run=run.pk)
        self.assertEqual(run_few, self.count(run=run.pk))
        self.assertLessEqual(few, 20)

    def test_status_page_queries_do_not_grow_with_items(self):
        c = client_for(self.viewer)
        c.get(reverse("bodeod:keys"))
        with CaptureQueriesContext(connection) as a:
            c.get(reverse("bodeod:keys"))
        for n in range(20):
            ChecklistItem.objects.create(mode=self.mode, name=f"Extra {n}", kind="url_check", ref_name="Portal", order=100 + n)
        with CaptureQueriesContext(connection) as b:
            c.get(reverse("bodeod:keys"))
        self.assertEqual(len(a), len(b))


class TimeTests(LiveBase):
    def test_checked_at_is_reported_in_ist_and_age_is_relative_to_now(self):
        checked = datetime(2026, 11, 2, 3, 30, 0, tzinfo=dt_timezone.utc)            # 09:00 IST
        Domain.objects.filter(pk=self.domain.pk).update(last_checked_at=checked)
        resolver = services.Resolver([self.i_domain], sites=None)
        e = live.entry("i1", 1, "x", "domain", "nseindia.com", True, resolver, checked + timedelta(seconds=125))
        self.assertEqual(e["checked_at_ist"], "02 Nov 2026 09:00:00 IST")
        self.assertEqual(e["checked_at"], "2026-11-02T03:30:00+00:00")
        self.assertEqual((e["age_s"], e["age"]), (125, "2 min"))
        e = live.entry("i1", 1, "x", "domain", "nseindia.com", True, resolver, checked - timedelta(seconds=5))   # clock skew
        self.assertEqual(e["age_s"], 0)

    def test_date_is_the_ist_trading_date(self):
        late = datetime(2026, 11, 2, 19, 0, 0, tzinfo=dt_timezone.utc)                # 00:30 IST on 3 Nov
        with mock.patch("django.utils.timezone.now", return_value=late):
            body = api(client_for(self.viewer)).json()
        self.assertEqual(body["date"], "2026-11-03")

    def test_human_age(self):
        self.assertEqual([live.human_age(s) for s in (None, 0, 12, 89, 90, 3600, 5399, 5400, 86400, 172800, 400000)],
                         ["", "0 s", "12 s", "89 s", "2 min", "60 min", "90 min", "2 h", "24 h", "2 d", "5 d"])

    def test_short_detail_wording(self):
        d = self.domain
        cases = [({"ssl_status": "Valid"}, "DNS resolves; certificate valid, 41 days left"),
                 ({"ssl_status": "Expired"}, "DNS resolves; certificate expired"),
                 ({"ssl_status": "Invalid"}, "DNS resolves; certificate invalid"),
                 ({"ssl_status": "Unreachable"}, "DNS resolves; the certificate could not be checked"),
                 ({"dns_status": "Failed", "last_error": "NXDOMAIN"}, "DNS failed: NXDOMAIN"),
                 ({"status": "down", "last_error": "timeout"}, "Down: timeout")]
        for fields, text in cases:
            for k, v in fields.items():
                setattr(d, k, v)
            status, long = services.status_of("domain", d)
            self.assertEqual(live.short_detail("domain", d, status, long), text, fields)
            d.refresh_from_db()
        d.monitor_enabled = False
        status, long = services.status_of("domain", d)
        self.assertEqual(live.short_detail("domain", d, status, long), "Domain monitoring is paused")
        u = self.url
        for st, err, text in (("online", "", "Online (120 ms)"), ("degraded", "slow", "Degraded: slow (120 ms)"), ("down", "HTTP 500", "Down: HTTP 500 (120 ms)")):
            u.last_status, u.last_error = st, err
            status, long = services.status_of("url_check", u)
            short = live.short_detail("url_check", u, status, long)
            self.assertEqual(short, text if st != "down" else "Down: HTTP 500")
