"""Telemetry tests. Every Prometheus and Nagios call is mocked; nothing touches the network."""
import base64
import hashlib
import io
import json
import re
import urllib.error
from datetime import timedelta
from unittest import mock

from django.apps import apps
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core import jobs as core_jobs
from core import prometheus
from core.models import AuditEvent, Event, Membership, Site

from . import alerts as alert_svc
from . import explorer, fmt, hosts, nagios, targets
from .models import Alert, SavedQuery


def _has_model(app_label, model):
    """True when the other module is present with the model (main may carry an empty stub app)."""
    try:
        apps.get_model(app_label, model)
        return True
    except LookupError:
        return False

PROM = "http://prom.test:9090"
TOKEN = "s3cret-token-value"

# ----------------------------------------------------------------------------- fake Prometheus
HOSTS = {  # instance: (up, cpu, mem, disk, load1, cores)
    "web-01:9100": (1, 20.0, 40.0, 30.0, 0.5, 4),
    "db-01:9100": (1, 50.0, 95.0, 60.0, 2.0, 8),
    "app-01:9100": (1, 80.0, 50.0, 40.0, 1.0, 4),
    "old-01:9100": (0, None, None, None, None, None),
}


def _row(inst, v, **labels):
    return {"metric": {"instance": inst, **labels}, "value": v}


def fake_query(q, at=None):
    def each(i):
        return [_row(inst, vals[i]) for inst, vals in HOSTS.items() if vals[i] is not None]
    if q.startswith("up{"):
        return [_row(inst, float(vals[0]), job="node", __name__="up") for inst, vals in HOSTS.items()]
    if "count by (instance)" in q:
        return each(5)
    if 'mode="idle"' in q:
        return each(1)
    if "MemAvailable" in q:
        return each(2)
    if "node_filesystem_avail_bytes" in q:
        return each(3)
    if "node_load1" in q:
        return each(4)
    if "receive_bytes" in q:
        return [_row(i, 1024.0 * 1024) for i in HOSTS if HOSTS[i][0]]
    if "transmit_bytes" in q:
        return [_row(i, 2048.0) for i in HOSTS if HOSTS[i][0]]
    if "node_time_seconds" in q:
        return [_row(i, 90000.0) for i in HOSTS if HOSTS[i][0]]
    if "prometheus_tsdb_head_series" in q:
        return [{"metric": {}, "value": 1234.0}]
    return []


def fake_range(q, seconds=3600, step=None):
    now = 1_700_000_000.0
    return [{"metric": {"instance": inst}, "values": [(now + k * 60, 10.0 + k) for k in range(5)]}
            for inst, vals in HOSTS.items() if vals[0]]


def prom_patches():
    return [mock.patch("core.prometheus.query", side_effect=fake_query),
            mock.patch("core.prometheus.query_range", side_effect=fake_range)]


class PromMixin:
    def start_prom(self):
        ps = prom_patches()
        mocks = [p.start() for p in ps]
        for p in ps:
            self.addCleanup(p.stop)
        return mocks


def users():
    viewer = User.objects.create_user("viewer", password="pw")
    operator = User.objects.create_user("operator", password="pw", is_staff=True)
    admin = User.objects.create_superuser("root", "root@example.com", "pw")
    return viewer, operator, admin


def am_payload(*alerts, status="firing", version="4"):
    return {"version": version, "groupKey": "{}:{alertname=\"X\"}", "status": status, "receiver": "linkedeye",
            "groupLabels": {}, "commonLabels": {}, "commonAnnotations": {}, "externalURL": "http://am:9093",
            "alerts": list(alerts)}


def am_alert(fp="abc123", status="firing", severity="critical", name="HostDown", instance="db-01:9100", **labels):
    return {"status": status, "fingerprint": fp,
            "labels": {"alertname": name, "severity": severity, "instance": instance, "job": "node", **labels},
            "annotations": {"summary": f"{name} summary", "description": "details"},
            "startsAt": "2026-10-31T10:00:00.123456789Z",
            "endsAt": "2026-10-31T11:00:00Z" if status == "resolved" else "0001-01-01T00:00:00Z",
            "generatorURL": "http://prometheus:9090/graph?g0.expr=up"}


# ============================================================================= helpers / models
class FmtTests(TestCase):
    def test_parse_ts(self):
        dt = fmt.parse_ts("2026-10-31T10:00:00.123456789Z")
        self.assertEqual((dt.year, dt.hour, dt.microsecond), (2026, 10, 123456))
        self.assertIsNone(fmt.parse_ts("0001-01-01T00:00:00Z"))
        self.assertIsNone(fmt.parse_ts("garbage"))
        self.assertIsNone(fmt.parse_ts(None))

    def test_human_formats(self):
        self.assertEqual(fmt.human_bytes(1536), "1.5 KB")
        self.assertEqual(fmt.human_rate(None), "–")
        self.assertEqual(fmt.human_duration(90061), "1d 1h")
        self.assertEqual(fmt.human_number(12345678.0), "12.3M")
        self.assertEqual(fmt.human_number(""), "–")
        self.assertEqual(fmt.human_number(float("nan")), "NaN")

    def test_promql_quoting_and_series_names(self):
        self.assertEqual(fmt.promql_string('a"b\\c'), '"a\\"b\\\\c"')
        self.assertEqual(fmt.series_name({"__name__": "up", "job": "node"}), 'up{job="node"}')
        self.assertEqual(fmt.series_name({}), "{}")


class ModelTests(TestCase):
    def test_alert_helpers(self):
        a = Alert.objects.create(fingerprint="f1", alertname="X", severity="critical",
                                 labels={"alertname": "X", "severity": "critical", "team": "db", "instance": "h"},
                                 starts_at=timezone.now() - timedelta(minutes=5))
        self.assertEqual(a.severity_tone, "crit")
        self.assertEqual(a.extra_labels, [("team", "db")])
        self.assertEqual(a.event_fingerprint, "am:f1")
        self.assertGreaterEqual(a.duration_seconds, 299)
        self.assertFalse(a.acknowledged)
        self.assertEqual(str(a), "X")

    def test_fingerprint_unique(self):
        from django.db import IntegrityError
        Alert.objects.create(fingerprint="dup", alertname="A")
        with self.assertRaises(IntegrityError):
            Alert.objects.create(fingerprint="dup", alertname="B")

    def test_saved_query_unique_per_owner(self):
        from django.db import IntegrityError
        u = User.objects.create_user("u")
        SavedQuery.objects.create(name="cpu", promql="up", owner=u)
        with self.assertRaises(IntegrityError):
            SavedQuery.objects.create(name="cpu", promql="up", owner=u)


# ============================================================================= explorer policy
class ExplorerPolicyTests(TestCase):
    def test_length_limit(self):
        with self.assertRaises(explorer.PolicyError):
            explorer.check("x" * 2001, 3600)
        self.assertEqual(explorer.check("x" * 2000, 3600)[0], "x" * 2000)

    def test_range_limit(self):
        with self.assertRaises(explorer.PolicyError):
            explorer.check("up", 7 * 86400 + 1)
        explorer.check("up", 7 * 86400)

    def test_step_bounded_to_1100_points(self):
        for seconds in (300, 3600, 86400, 7 * 86400):
            _q, s, step, _n = explorer.check("up", seconds, 1)
            self.assertLessEqual(s // step + 1, explorer.MAX_POINTS)
        _q, _s, step, notes = explorer.check("up", 7 * 86400, 15)
        self.assertEqual(step, 551)
        self.assertTrue(notes)
        self.assertEqual(explorer.check("up", 3600, 60)[2], 60)

    def test_rejects_empty_and_control_chars(self):
        for bad in ("", "   ", "up\x00"):
            with self.assertRaises(explorer.PolicyError):
                explorer.check(bad, 3600)

    def test_parse_duration(self):
        self.assertEqual(explorer.parse_duration("5m"), 300)
        self.assertEqual(explorer.parse_duration("30"), 30)
        self.assertIsNone(explorer.parse_duration("5 minutes"))

    def test_run_caps_series_at_40(self):
        rows = [{"metric": {"__name__": "m", "i": str(i)}, "values": [(1.0, float(i)), (2.0, float(i) + 1)]} for i in range(55)]
        with mock.patch("core.prometheus.query_range", return_value=rows) as qr:
            res = explorer.run("m", 3600, 60)
        qr.assert_called_once_with("m", 3600, 60)
        self.assertEqual(res["total"], 55)
        self.assertEqual(len(res["series"]), 40)
        self.assertEqual(res["series"][0]["last"], 55.0)  # highest last value first
        self.assertEqual(len(res["chart"]["series"]), 40)
        self.assertTrue(res["notes"])


# ============================================================================= hosts / targets services
@override_settings(PROMETHEUS_URL=PROM)
class HostServiceTests(PromMixin, TestCase):
    def setUp(self):
        cache.clear()

    def test_fetch_hosts_health(self):
        self.start_prom()
        hs = {h["instance"]: h for h in hosts.fetch_hosts()}
        self.assertEqual(hs["old-01:9100"]["health"], "down")
        self.assertEqual(hs["db-01:9100"]["health"], "critical")   # memory 95%
        self.assertEqual(hs["app-01:9100"]["health"], "warning")   # cpu 80%
        self.assertEqual(hs["web-01:9100"]["health"], "healthy")
        self.assertAlmostEqual(hs["db-01:9100"]["load_per_core"], 0.25)
        s = hosts.summarize(list(hs.values()))
        self.assertEqual((s["total"], s["up"], s["down"], s["critical"]), (4, 3, 1, 1))
        self.assertEqual(list(hosts.fetch_hosts())[0]["health"], "down")  # sorted worst first

    def test_hosts_are_cached(self):
        q, _r = self.start_prom()
        hosts.fetch_hosts()
        n = q.call_count
        hosts.fetch_hosts()
        self.assertEqual(q.call_count, n)
        hosts.fetch_hosts(use_cache=False)
        self.assertGreater(q.call_count, n)

    def test_instance_is_escaped_in_detail_queries(self):
        qs = hosts.detail_queries('evil"} or vector(1) #')
        self.assertIn('instance="evil\\"} or vector(1) #"', qs["cpu"])

    def test_cmdb_join(self):
        if not _has_model("cmdb", "ConfigurationItem"):
            self.skipTest("cmdb not installed")
        CI = apps.get_model("cmdb", "ConfigurationItem")
        try:
            CI.objects.create(name="DB Server 1", ci_type="server", prometheus_instance="db-01:9100", site="DR Site")
        except Exception as e:  # pragma: no cover - other module's required fields
            self.skipTest(f"cannot create CI: {e}")
        m = hosts.cmdb_map(["db-01:9100", "web-01:9100"])
        self.assertEqual(m["db-01:9100"].name, "DB Server 1")
        self.assertNotIn("web-01:9100", m)


class RangeGridTests(TestCase):
    """core.prometheus.query_range snaps the window to the step grid.

    Prometheus evaluates a range query at exactly start + k*step, so two queries issued milliseconds apart would
    otherwise come back on different grids. hosts.align unions the timestamps of a chart's series, so misaligned
    grids leave every series full of gaps and break the shared `ts` axis the charts sync their crosshair on."""

    def _calls(self, *specs):
        with mock.patch("core.prometheus._get", return_value={"result": []}) as g:
            for seconds, step in specs:
                prometheus.query_range("node_load1", seconds, step)
        return [c.args[1] for c in g.call_args_list]

    def test_window_is_snapped_to_whole_steps(self):
        p = self._calls((3600, 15))[0]
        self.assertEqual(p["step"], 15)
        self.assertEqual(p["end"] % 15, 0)
        self.assertEqual(p["end"] - p["start"], 3600)
        self.assertIsInstance(p["end"], int)

    def test_queries_issued_apart_share_one_grid(self):
        # the four range queries of one chart run concurrently; they must land on identical timestamps
        # 1_699_999_995 is the step grid point the three clock readings fall into
        with mock.patch("core.prometheus._get", return_value={"result": []}) as g, \
             mock.patch("core.prometheus.time.time", side_effect=[1_699_999_995.0, 1_699_999_998.4, 1_700_000_009.9]):
            for _ in range(3):
                prometheus.query_range("node_load1", 3600, 15)
        windows = {(c.args[1]["start"], c.args[1]["end"]) for c in g.call_args_list}
        self.assertEqual(windows, {(1_699_999_995 - 3600, 1_699_999_995)})

    def test_step_defaults_and_never_divides_by_zero(self):
        self.assertEqual(self._calls((3600, None))[0]["step"], 30)
        self.assertEqual(self._calls((3600, 0))[0]["step"], 30)
        p = self._calls((7 * 86400, 1800))[0]
        self.assertEqual((p["step"], p["end"] % 1800, p["end"] - p["start"]), (1800, 0, 7 * 86400))


class TargetsServiceTests(TestCase):
    def test_normalize_and_summaries(self):
        raw = [
            {"labels": {"job": "node", "instance": "a:9100", "env": "prod"}, "scrapePool": "node", "scrapeUrl": "http://a:9100/metrics",
             "health": "up", "lastError": "", "lastScrape": "2026-10-31T10:00:00.5Z", "lastScrapeDuration": 0.02},
            {"labels": {"job": "node", "instance": "b:9100"}, "scrapePool": "node", "scrapeUrl": "http://b:9100/metrics",
             "health": "down", "lastError": "connection refused", "lastScrape": "2026-10-31T10:00:00Z", "lastScrapeDuration": 0.04},
            {"labels": {"job": "bb", "instance": "https://x"}, "health": "unknown", "lastScrape": "0001-01-01T00:00:00Z"},
        ]
        rows = [targets.normalize(t) for t in raw]
        self.assertEqual(rows[0]["labels"], [("env", "prod")])
        self.assertIsNone(rows[2]["last_scrape"])
        k = targets.kpis(rows)
        self.assertEqual((k["total"], k["up"], k["down"], k["unknown"], k["jobs"]), (3, 1, 1, 1, 2))
        self.assertAlmostEqual(k["avg_duration"], 0.03)
        jobs = {j["job"]: j for j in targets.jobs_summary(rows)}
        self.assertEqual(jobs["node"]["health"], "down")
        self.assertEqual(jobs["bb"]["health"], "unknown")


# ============================================================================= Nagios
class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def nagios_body(query):
    if query == "hostlist":
        data = {"hostlist": {"web": {"name": "web", "status": 2, "plugin_output": "PING OK", "last_check": 1_700_000_000_000},
                             "db": {"name": "db", "status": 4, "plugin_output": "CRITICAL - down", "problem_has_been_acknowledged": True},
                             "dr": {"name": "dr", "status": "unreachable"}}}
    else:
        data = {"servicelist": {"web": {"HTTP": {"status": 2, "plugin_output": "OK"}, "Disk": {"status": 4, "plugin_output": "WARN"}},
                                "db": {"MySQL": {"status": 16, "plugin_output": "CRIT"}, "Backup": {"status": "unknown"}}}}
    return json.dumps({"format_version": 0, "result": {"query_time": 1_700_000_000_000, "type_code": 0, "type_text": "Success",
                                                       "program_version": "4.4.14"}, "data": data}).encode()


def fake_urlopen(req, timeout=None):
    from urllib.parse import parse_qs, urlparse
    q = parse_qs(urlparse(req.full_url).query)["query"][0]
    return FakeResp(nagios_body(q))


@override_settings(NAGIOS_URL="https://nagios.test/nagios", NAGIOS_USER="nagiosadmin", NAGIOS_PASSWORD="pw")
class NagiosTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_fetch_and_summarize(self):
        with mock.patch("telemetry.nagios._urlopen", side_effect=fake_urlopen) as uo:
            st = nagios.fetch_status()
        self.assertEqual(uo.call_count, 2)
        req = uo.call_args_list[0][0][0]
        self.assertTrue(req.full_url.startswith("https://nagios.test/nagios/cgi-bin/statusjson.cgi?query=hostlist"))
        self.assertIn("details=true", req.full_url)
        self.assertEqual(req.get_header("Authorization"), "Basic " + base64.b64encode(b"nagiosadmin:pw").decode())
        self.assertEqual(st["hosts"][0]["state"], "down")  # worst first
        self.assertEqual({h["name"]: h["state"] for h in st["hosts"]}, {"web": "up", "db": "down", "dr": "unreachable"})
        self.assertEqual(st["services"][0]["state"], "critical")
        s = nagios.summarize(st)
        self.assertEqual(s["host_counts"]["down"], 1)
        self.assertEqual(s["service_counts"], {"critical": 1, "warning": 1, "unknown": 1, "pending": 0, "ok": 1})
        self.assertEqual(s["unhandled"], 1 + 3)  # db is acknowledged

    def test_cached_for_30_seconds(self):
        with mock.patch("telemetry.nagios._urlopen", side_effect=fake_urlopen) as uo:
            nagios.fetch_status()
            nagios.fetch_status()
        self.assertEqual(uo.call_count, 2)

    def test_errors(self):
        err = urllib.error.HTTPError("https://nagios.test", 401, "Unauthorized", {}, None)
        with mock.patch("telemetry.nagios._urlopen", side_effect=err):
            with self.assertRaisesRegex(nagios.NagiosError, "credentials"):
                nagios.fetch_status()
        cache.clear()
        with mock.patch("telemetry.nagios._urlopen", side_effect=urllib.error.URLError("timed out")):
            with self.assertRaisesRegex(nagios.NagiosError, "unreachable"):
                nagios.fetch_status()
        cache.clear()
        with mock.patch("telemetry.nagios._urlopen", return_value=FakeResp(b"<html>login</html>")):
            with self.assertRaisesRegex(nagios.NagiosError, "JSON"):
                nagios.fetch_status()

    def test_url_variants(self):
        for base, expected in (("https://n/nagios", "https://n/nagios/cgi-bin/statusjson.cgi"),
                               ("https://n/nagios/cgi-bin/", "https://n/nagios/cgi-bin/statusjson.cgi"),
                               ("https://n/x/statusjson.cgi", "https://n/x/statusjson.cgi")):
            with override_settings(NAGIOS_URL=base):
                self.assertEqual(nagios.statusjson_url(), expected)

    @override_settings(NAGIOS_URL="")
    def test_not_configured(self):
        with mock.patch.dict("os.environ", {"LINKEDEYE_NAGIOS_URL": ""}):
            self.assertFalse(nagios.configured())
            with self.assertRaises(nagios.NagiosError):
                nagios.fetch_status()


# ============================================================================= webhook
@override_settings(ALERTMANAGER_WEBHOOK_TOKEN=TOKEN, INCIDENT_AUTO_CREATE=False)
class WebhookTests(TestCase):
    url = "/api/v1/alertmanager/"

    def setUp(self):
        self.c = Client(enforce_csrf_checks=True)

    def post(self, payload, token=TOKEN, raw=None, **extra):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token is not None else {}
        body = raw if raw is not None else json.dumps(payload)
        return self.c.post(self.url, data=body, content_type="application/json", **headers, **extra)

    def test_url_name(self):
        self.assertEqual(reverse("telemetry:alertmanager_webhook"), self.url)

    @override_settings(ALERTMANAGER_WEBHOOK_TOKEN="")
    def test_disabled_without_token(self):
        r = self.post(am_payload(am_alert()), token="")
        self.assertEqual(r.status_code, 503)
        self.assertIn("LINKEDEYE_ALERTMANAGER_WEBHOOK_TOKEN", r.json()["error"])
        self.assertEqual(Alert.objects.count(), 0)

    def test_rejects_bad_tokens(self):
        with self.assertLogs("linkedeye.telemetry", "WARNING"):
            for tok in (None, "wrong", TOKEN + "x", "", "ünïcode"):
                r = self.post(am_payload(am_alert()), token=tok)
                self.assertEqual(r.status_code, 401, tok)
            r = self.c.post(self.url, data="{}", content_type="application/json", HTTP_AUTHORIZATION=f"Basic {TOKEN}")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(Alert.objects.count(), 0)

    def test_uses_constant_time_compare(self):
        with mock.patch("telemetry.views.webhook.hmac.compare_digest", return_value=False) as cd, \
                self.assertLogs("linkedeye.telemetry", "WARNING"):
            self.assertEqual(self.post(am_payload(am_alert())).status_code, 401)
        cd.assert_called_once()

    def test_method_and_size_and_format(self):
        self.assertEqual(self.c.get(self.url).status_code, 405)
        big = json.dumps(am_payload(am_alert(description="x" * (1024 * 1024))))
        self.assertEqual(self.post(None, raw=big).status_code, 413)
        self.assertEqual(self.post(None, raw="not json").status_code, 400)
        self.assertEqual(self.post(am_payload(am_alert(), version="3")).status_code, 400)
        self.assertEqual(self.post({"version": "4", "alerts": "nope"}).status_code, 400)
        self.assertEqual(self.post(am_payload(am_alert(status="weird"))).status_code, 400)

    def test_firing_creates_alert_event_and_audit(self):
        r = self.post(am_payload(am_alert(team="dba")))
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["created"], 1)
        a = Alert.objects.get(fingerprint="abc123")
        self.assertEqual((a.status, a.alertname, a.severity, a.instance, a.job), ("firing", "HostDown", "critical", "db-01:9100", "node"))
        self.assertEqual(a.labels["team"], "dba")
        self.assertEqual(a.summary, "HostDown summary")
        self.assertEqual(a.starts_at.microsecond, 123456)
        self.assertIsNone(a.ends_at)
        ev = Event.objects.get(fingerprint="am:abc123")
        self.assertEqual((ev.severity, ev.source, ev.kind), ("critical", "telemetry", "alert"))
        self.assertEqual(ev.data["host"], "db-01")
        self.assertTrue(AuditEvent.objects.filter(action="ingest", object_type="telemetry.alert").exists())

    def test_state_changes_emit_once(self):
        self.post(am_payload(am_alert()))
        self.post(am_payload(am_alert()))  # repeat notification: no new event
        self.assertEqual(Event.objects.filter(fingerprint="am:abc123").count(), 1)
        a = Alert.objects.get()
        self.assertEqual(a.receive_count, 2)
        a.acknowledged_at = timezone.now()
        a.save()
        self.post(am_payload(am_alert(status="resolved"), status="resolved"))
        a.refresh_from_db()
        self.assertEqual(a.status, "resolved")
        self.assertIsNotNone(a.ends_at)
        self.assertEqual(Event.objects.filter(fingerprint="am:abc123").latest("pk").severity, "ok")
        # re-fires (a new firing has a newer startsAt): new event, acknowledgement cleared
        self.post(am_payload(dict(am_alert(), startsAt="2026-10-31T12:00:00Z")))
        a.refresh_from_db()
        self.assertEqual((a.status, a.fire_count, a.acknowledged_at), ("firing", 2, None))
        self.assertEqual(Event.objects.filter(fingerprint="am:abc123").count(), 3)
        self.post(am_payload(am_alert(severity="warning")))  # severity change while firing
        self.assertEqual(Event.objects.filter(fingerprint="am:abc123").latest("pk").severity, "warning")

    def test_severity_mapping(self):
        self.assertEqual(alert_svc.event_severity("firing", "page"), "critical")
        self.assertEqual(alert_svc.event_severity("firing", "Critical"), "critical")
        self.assertEqual(alert_svc.event_severity("firing", "warning"), "warning")
        self.assertEqual(alert_svc.event_severity("firing", "info"), "info")
        self.assertEqual(alert_svc.event_severity("resolved", "critical"), "ok")
        self.post(am_payload(am_alert(fp="p1", severity="page")))
        self.assertEqual(Event.objects.get(fingerprint="am:p1").data["priority"], "P1")

    def test_new_resolved_alert_stored_without_event(self):
        self.post(am_payload(am_alert(fp="r1", status="resolved"), status="resolved"))
        self.assertEqual(Alert.objects.get(fingerprint="r1").status, "resolved")
        self.assertFalse(Event.objects.filter(fingerprint="am:r1").exists())

    def test_fingerprint_derived_when_missing(self):
        al = am_alert()
        del al["fingerprint"]
        self.post(am_payload(al))
        self.post(am_payload(al))
        self.assertEqual(Alert.objects.count(), 1)
        self.assertEqual(len(Alert.objects.get().fingerprint), 16)

    @override_settings(INCIDENT_AUTO_CREATE=True)
    def test_critical_alert_opens_service_desk_incident(self):
        if not _has_model("servicedesk", "Ticket"):
            self.skipTest("servicedesk not installed")
        Ticket = apps.get_model("servicedesk", "Ticket")
        self.post(am_payload(am_alert(fp="inc1")))
        self.assertTrue(Ticket.objects.filter(fingerprint="am:inc1", kind="incident").exists())


# ============================================================================= pages + permissions
class PageTests(PromMixin, TestCase):
    pages = ["telemetry:infrastructure", "telemetry:nagios", "telemetry:prometheus", "telemetry:alertmanager", "telemetry:explorer"]

    def setUp(self):
        cache.clear()
        self.viewer, self.operator, self.admin = users()

    def test_url_paths(self):
        self.assertEqual(reverse("telemetry:infrastructure"), "/monitoring/infrastructure/")
        self.assertEqual(reverse("telemetry:nagios"), "/monitoring/nagios/")
        self.assertEqual(reverse("telemetry:prometheus"), "/monitoring/prometheus/")
        self.assertEqual(reverse("telemetry:alertmanager"), "/monitoring/alerts/")
        self.assertEqual(reverse("telemetry:explorer"), "/monitoring/metrics-explorer/")

    def test_login_required(self):
        r = Client().get(reverse("telemetry:infrastructure"))
        self.assertEqual(r.status_code, 302)

    @override_settings(PROMETHEUS_URL="", NAGIOS_URL="", ALERTMANAGER_WEBHOOK_TOKEN="")
    def test_not_connected_states(self):
        self.client.force_login(self.viewer)
        with mock.patch.dict("os.environ", {"LINKEDEYE_NAGIOS_URL": ""}), \
                mock.patch("core.prometheus.query", side_effect=AssertionError("must not query")):
            for name in self.pages:
                r = self.client.get(reverse(name))
                self.assertEqual(r.status_code, 200, name)
                self.assertContains(r, "not connected")
        self.assertContains(self.client.get(reverse("telemetry:infrastructure")), "LINKEDEYE_PROMETHEUS_URL")
        self.assertContains(self.client.get(reverse("telemetry:prometheus")), "LINKEDEYE_PROMETHEUS_URL")
        self.assertContains(self.client.get(reverse("telemetry:nagios")), "NAGIOS_URL")
        self.assertContains(self.client.get(reverse("telemetry:alertmanager")), "LINKEDEYE_ALERTMANAGER_WEBHOOK_TOKEN")

    @override_settings(PROMETHEUS_URL=PROM)
    def test_infrastructure_renders_hosts(self):
        self.start_prom()
        self.client.force_login(self.viewer)
        r = self.client.get(reverse("telemetry:infrastructure"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "db-01:9100")
        self.assertContains(r, "Critical")
        self.assertContains(r, 'data-chart="spark"')
        r = self.client.get(reverse("telemetry:infrastructure") + "?health=down")
        self.assertContains(r, "old-01:9100")
        self.assertContains(r, "Hosts (1)")
        r = self.client.get(reverse("telemetry:infrastructure") + "?format=csv")
        self.assertEqual(r["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("db-01:9100", r.content.decode())

    @override_settings(PROMETHEUS_URL=PROM)
    def test_prometheus_error_is_shown(self):
        self.client.force_login(self.viewer)
        with mock.patch("core.prometheus.query", side_effect=prometheus.PrometheusError("Prometheus unreachable: boom")):
            r = self.client.get(reverse("telemetry:infrastructure"))
        self.assertContains(r, "Prometheus unreachable: boom")
        self.assertNotContains(r, "Traceback")

    @override_settings(PROMETHEUS_URL=PROM)
    def test_host_detail(self):
        self.start_prom()
        self.client.force_login(self.viewer)
        r = self.client.get(reverse("telemetry:host_detail", args=["db-01:9100"]) + "?range=6h")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Memory Used")
        self.assertContains(r, 'id="c-cpu"')
        with mock.patch("core.prometheus.query", return_value=[]):
            r = self.client.get(reverse("telemetry:host_detail", args=["gone:9100"]))
        self.assertEqual(r.status_code, 404)
        self.assertContains(r, "Host not found", status_code=404)

    @override_settings(PROMETHEUS_URL=PROM)
    def test_prometheus_page(self):
        tgt = [{"labels": {"job": "node", "instance": "a:9100"}, "scrapePool": "node", "scrapeUrl": "http://a:9100/metrics",
                "health": "down", "lastError": "connection refused", "lastScrape": "2026-10-31T10:00:00Z", "lastScrapeDuration": 0.5}]
        self.client.force_login(self.viewer)
        with mock.patch("core.prometheus.targets", return_value=tgt), \
                mock.patch("core.prometheus.query", side_effect=fake_query), \
                mock.patch("core.prometheus.alerts", return_value=[{"state": "firing"}, {"state": "pending"}]):
            r = self.client.get(reverse("telemetry:prometheus"))
            self.assertContains(r, "connection refused")
            self.assertContains(r, "http://a:9100/metrics")
            self.assertContains(r, "1 pending")
            r = self.client.get(reverse("telemetry:prometheus") + "?format=csv")
            self.assertIn("a:9100", r.content.decode())

    @override_settings(NAGIOS_URL="https://nagios.test/nagios")
    def test_nagios_page(self):
        self.client.force_login(self.viewer)
        with mock.patch("telemetry.nagios._urlopen", side_effect=fake_urlopen):
            r = self.client.get(reverse("telemetry:nagios"))
            self.assertContains(r, "Host Problems")
            self.assertContains(r, "CRITICAL - down")
            r = self.client.get(reverse("telemetry:nagios") + "?tab=hosts&state=unreachable")
            self.assertContains(r, ">dr<")
        cache.clear()
        with mock.patch("telemetry.nagios._urlopen", side_effect=urllib.error.URLError("refused")):
            r = self.client.get(reverse("telemetry:nagios"))
        self.assertContains(r, "Nagios unreachable")

    def test_alert_pages_and_filters(self):
        a = Alert.objects.create(fingerprint="f1", alertname="DiskFull", severity="warning", instance="db-01:9100",
                                 labels={"alertname": "DiskFull", "team": "dba"}, starts_at=timezone.now())
        Alert.objects.create(fingerprint="f2", alertname="CpuHigh", severity="critical", labels={"team": "web"})
        Alert.objects.create(fingerprint="f3", alertname="Old", status="resolved", labels={})
        self.client.force_login(self.viewer)
        r = self.client.get(reverse("telemetry:alertmanager"))
        self.assertContains(r, "DiskFull")
        self.assertContains(r, "CpuHigh")
        self.assertNotContains(r, "<b>Old</b>")
        self.assertNotContains(r, "Acknowledge selected")  # viewer
        r = self.client.get(reverse("telemetry:alertmanager") + "?label=team%3Ddba")
        self.assertContains(r, "DiskFull")
        self.assertNotContains(r, "CpuHigh</b>")
        r = self.client.get(reverse("telemetry:alertmanager") + "?label=team__contains%3Dx")  # nested lookup refused
        self.assertEqual(r.status_code, 200)
        r = self.client.get(reverse("telemetry:alertmanager") + "?tab=resolved")
        self.assertContains(r, "Old")
        r = self.client.get(reverse("telemetry:alert_detail", args=[a.pk]))
        self.assertContains(r, "am:f1")
        r = self.client.get(reverse("telemetry:alertmanager") + "?format=csv&tab=all")
        self.assertIn("DiskFull", r.content.decode())

    def test_alerts_scoped_by_site_membership(self):
        site = Site.objects.create(name="Mumbai", code="mum")
        Site.objects.create(name="DR", code="dr")
        u = User.objects.create_user("scoped")
        Membership.objects.create(user=u, site=site, role="viewer")
        Alert.objects.create(fingerprint="m", alertname="MumbaiAlert", site="Mumbai")
        dr = Alert.objects.create(fingerprint="d", alertname="DrAlert", site="DR")
        self.client.force_login(u)
        r = self.client.get(reverse("telemetry:alertmanager"))
        self.assertContains(r, "MumbaiAlert")
        self.assertNotContains(r, "DrAlert")
        self.assertEqual(self.client.get(reverse("telemetry:alert_detail", args=[dr.pk])).status_code, 404)


# ============================================================================= mutations
class MutationTests(TestCase):
    def setUp(self):
        self.viewer, self.operator, self.admin = users()
        self.alert = Alert.objects.create(fingerprint="f1", alertname="DiskFull", severity="warning", instance="db-01:9100")

    def csrf_client(self, user):
        c = Client(enforce_csrf_checks=True)
        c.force_login(user)
        c.get(reverse("telemetry:alertmanager"))
        return c, c.cookies["csrftoken"].value

    def test_acknowledge_requires_csrf(self):
        c, _tok = self.csrf_client(self.operator)
        r = c.post(reverse("telemetry:alert_ack", args=[self.alert.pk]))
        self.assertEqual(r.status_code, 403)
        self.alert.refresh_from_db()
        self.assertIsNone(self.alert.acknowledged_at)

    def test_acknowledge_requires_operate(self):
        c, tok = self.csrf_client(self.viewer)
        r = c.post(reverse("telemetry:alert_ack", args=[self.alert.pk]), {"csrfmiddlewaretoken": tok})
        self.assertEqual(r.status_code, 302)
        self.alert.refresh_from_db()
        self.assertIsNone(self.alert.acknowledged_at)
        self.assertFalse(AuditEvent.objects.filter(action="acknowledge").exists())

    def test_get_is_not_allowed(self):
        self.client.force_login(self.operator)
        self.assertEqual(self.client.get(reverse("telemetry:alert_ack", args=[self.alert.pk])).status_code, 405)

    def test_operator_acknowledges(self):
        c, tok = self.csrf_client(self.operator)
        r = c.post(reverse("telemetry:alert_ack", args=[self.alert.pk]), {"csrfmiddlewaretoken": tok, "next": "/monitoring/alerts/?tab=all"})
        self.assertRedirects(r, "/monitoring/alerts/?tab=all", fetch_redirect_response=False)
        self.alert.refresh_from_db()
        self.assertEqual(self.alert.acknowledged_by, self.operator)
        self.assertTrue(AuditEvent.objects.filter(action="acknowledge", object_id=str(self.alert.pk), actor=self.operator).exists())
        self.assertTrue(Event.objects.filter(fingerprint="am:f1", kind="ack").exists())
        r = c.post(reverse("telemetry:alert_unack", args=[self.alert.pk]), {"csrfmiddlewaretoken": tok})
        self.alert.refresh_from_db()
        self.assertIsNone(self.alert.acknowledged_at)
        self.assertTrue(AuditEvent.objects.filter(action="unacknowledge").exists())

    def test_open_redirect_refused(self):
        c, tok = self.csrf_client(self.operator)
        r = c.post(reverse("telemetry:alert_ack", args=[self.alert.pk]), {"csrfmiddlewaretoken": tok, "next": "https://evil.example/"})
        self.assertEqual(r["Location"], reverse("telemetry:alertmanager"))

    def test_bulk_acknowledge(self):
        b = Alert.objects.create(fingerprint="f2", alertname="Other")
        c, tok = self.csrf_client(self.admin)
        c.post(reverse("telemetry:alert_ack_bulk"), {"csrfmiddlewaretoken": tok, "ids": [self.alert.pk, b.pk, "x"]})
        self.assertEqual(Alert.objects.filter(acknowledged_at__isnull=False).count(), 2)
        self.assertEqual(AuditEvent.objects.filter(action="acknowledge").count(), 2)

    def test_saved_queries(self):
        c, tok = self.csrf_client(self.operator)
        url = reverse("telemetry:query_save")
        r = c.post(url, {"csrfmiddlewaretoken": tok, "name": "cpu", "promql": "up", "range": "6h", "shared": "on"})
        self.assertEqual(r.status_code, 302)
        q = SavedQuery.objects.get(name="cpu")
        self.assertEqual((q.owner, q.range_key, q.shared), (self.operator, "6h", True))
        self.assertTrue(AuditEvent.objects.filter(action="create", object_id=str(q.pk)).exists())
        c.post(url, {"csrfmiddlewaretoken": tok, "name": "cpu", "promql": "up == 0", "range": "1h"})
        q.refresh_from_db()
        self.assertEqual((q.promql, q.shared), ("up == 0", False))
        self.assertTrue(AuditEvent.objects.filter(action="update", object_id=str(q.pk)).exists())
        c.post(url, {"csrfmiddlewaretoken": tok, "name": "huge", "promql": "x" * 2001})
        self.assertFalse(SavedQuery.objects.filter(name="huge").exists())
        # viewer cannot save
        vc, vtok = self.csrf_client(self.viewer)
        vc.post(url, {"csrfmiddlewaretoken": vtok, "name": "mine", "promql": "up"})
        self.assertFalse(SavedQuery.objects.filter(name="mine").exists())
        # another operator cannot delete it; an admin can
        other = User.objects.create_user("op2", is_staff=True)
        oc, otok = self.csrf_client(other)
        oc.post(reverse("telemetry:query_delete", args=[q.pk]), {"csrfmiddlewaretoken": otok})
        self.assertTrue(SavedQuery.objects.filter(pk=q.pk).exists())
        ac, atok = self.csrf_client(self.admin)
        ac.post(reverse("telemetry:query_delete", args=[q.pk]), {"csrfmiddlewaretoken": atok})
        self.assertFalse(SavedQuery.objects.filter(pk=q.pk).exists())
        self.assertTrue(AuditEvent.objects.filter(action="delete", object_type="telemetry.savedquery").exists())

    def test_owner_deletes_own_query(self):
        q = SavedQuery.objects.create(name="mine", promql="up", owner=self.operator)
        c, tok = self.csrf_client(self.operator)
        c.post(reverse("telemetry:query_delete", args=[q.pk]), {"csrfmiddlewaretoken": tok})
        self.assertFalse(SavedQuery.objects.filter(pk=q.pk).exists())


@override_settings(PROMETHEUS_URL=PROM)
class ExplorerViewTests(TestCase):
    def setUp(self):
        self.viewer, self.operator, _admin = users()
        self.client.force_login(self.viewer)

    def test_runs_query_and_shows_last_values(self):
        rows = [{"metric": {"__name__": "up", "job": "node"}, "values": [(1.0, 1.0), (2.0, 0.0)]}]
        with mock.patch("core.prometheus.query_range", return_value=rows) as qr:
            r = self.client.get(reverse("telemetry:explorer"), {"expr": "up", "range": "24h", "step": "1s"})
        self.assertEqual(r.status_code, 200)
        args = qr.call_args[0]
        self.assertEqual(args[1], 86400)
        self.assertLessEqual(86400 // args[2] + 1, 1100)
        self.assertContains(r, 'up{job=&quot;node&quot;}')
        self.assertContains(r, "Step raised")
        self.assertContains(r, 'id="r-chart"')

    def test_policy_blocks_before_prometheus(self):
        with mock.patch("core.prometheus.query_range") as qr:
            r = self.client.get(reverse("telemetry:explorer"), {"expr": "x" * 2001})
            self.assertContains(r, "the limit is 2,000")
            r = self.client.get(reverse("telemetry:explorer"), {"expr": "up", "step": "soon"})
            self.assertContains(r, "Step must be a duration")
        qr.assert_not_called()

    def test_prometheus_error_message(self):
        with mock.patch("core.prometheus.query_range", side_effect=prometheus.PrometheusError("parse error at char 3")):
            r = self.client.get(reverse("telemetry:explorer"), {"expr": "up{"})
        self.assertContains(r, "parse error at char 3")

    def test_load_saved_query_visibility(self):
        mine = SavedQuery.objects.create(name="private", promql="node_load1", owner=self.operator)
        shared = SavedQuery.objects.create(name="team", promql="node_load5", owner=self.operator, shared=True)
        with mock.patch("core.prometheus.query_range", return_value=[]):
            r = self.client.get(reverse("telemetry:explorer"), {"saved": shared.pk})
            self.assertContains(r, "node_load5")
            r = self.client.get(reverse("telemetry:explorer"), {"saved": mine.pk})
        self.assertNotContains(r, "node_load1</textarea>")
        self.assertContains(r, "not shared with you")


# ============================================================================= jobs / seed
class JobAndSeedTests(TestCase):
    def test_job_registered(self):
        self.assertIn("telemetry.prune_alerts", core_jobs.JOBS)

    @override_settings(TELEMETRY_ALERT_RETENTION_DAYS=30)
    def test_prune_keeps_firing_and_recent(self):
        old = timezone.now() - timedelta(days=40)
        Alert.objects.create(fingerprint="a", alertname="A", status="resolved", last_received_at=old)
        Alert.objects.create(fingerprint="b", alertname="B", status="firing", last_received_at=old)
        Alert.objects.create(fingerprint="c", alertname="C", status="resolved")
        self.assertEqual(core_jobs.JOBS["telemetry.prune_alerts"]["func"](), "pruned 1 resolved alert(s)")
        self.assertEqual(sorted(Alert.objects.values_list("fingerprint", flat=True)), ["b", "c"])

    def test_job_never_raises(self):
        from . import jobs
        with mock.patch("telemetry.alerts.prune", side_effect=RuntimeError("db gone")), \
                self.assertLogs("linkedeye.telemetry", "ERROR"):
            self.assertEqual(jobs.prune_alerts(), "error")

    def test_seed_is_idempotent_and_marked(self):
        out = io.StringIO()
        call_command("seed_telemetry", stdout=out)
        call_command("seed_telemetry", stdout=out)
        self.assertEqual(Alert.objects.count(), 6)
        self.assertTrue(all(a.is_example for a in Alert.objects.all()))
        self.assertEqual(SavedQuery.objects.filter(name__startswith="Example: ").count(), 4)
        self.assertFalse(Event.objects.filter(source="telemetry").exists())
        call_command("seed_telemetry", "--remove", stdout=out)
        self.assertEqual(Alert.objects.count(), 0)
        self.assertEqual(SavedQuery.objects.count(), 0)


# ============================================================================= adversarial QA regressions
class QaRegressionTests(PromMixin, TestCase):
    """Bugs found by the end-to-end QA pass; each test failed before its fix."""

    def setUp(self):
        cache.clear()
        self.viewer, self.operator, self.admin = users()

    def test_label_filter_does_not_crash_on_sqlite(self):
        # KeyTextTransform kept the JSON "exact" lookup -> JSON('dba') -> "malformed JSON" -> HTTP 500
        Alert.objects.create(fingerprint="l1", alertname="DiskFull", labels={"team": "dba", "contains": "x"})
        Alert.objects.create(fingerprint="l2", alertname="CpuHigh", labels={"team": "web"})
        self.client.force_login(self.viewer)
        r = self.client.get(reverse("telemetry:alertmanager") + "?tab=all&label=team%3Ddba")
        self.assertEqual(r.status_code, 200)
        self.assertEqual([a.alertname for a in r.context["page"]], ["DiskFull"])
        r = self.client.get(reverse("telemetry:alertmanager") + "?tab=all&label=contains%3Dx")  # a lookup-named label
        self.assertEqual([a.alertname for a in r.context["page"]], ["DiskFull"])
        r = self.client.get(reverse("telemetry:alertmanager") + "?tab=all&label=team%3Dnobody")
        self.assertEqual(len(r.context["page"]), 0)

    def test_remove_label_link_keeps_other_filters(self):
        Alert.objects.create(fingerprint="l1", alertname="DiskFull", severity="critical", labels={"team": "dba"})
        self.client.force_login(self.viewer)
        r = self.client.get(reverse("telemetry:alertmanager") + "?tab=all&severity=critical&label=team%3Ddba&page=1")
        self.assertContains(r, 'href="?tab=all&amp;severity=critical" title="Remove label filter"')

    @override_settings(PROMETHEUS_URL=PROM)
    def test_host_detail_alerts_are_site_scoped(self):
        # a host without a CMDB CI is global, but its alerts carry their own site: a Mumbai-only viewer
        # must not see the DR alert on the host page (the alert list and detail already 404 for it)
        mum = Site.objects.create(name="Mumbai", code="mum")
        Site.objects.create(name="DR", code="dr")
        u = User.objects.create_user("mumviewer")
        Membership.objects.create(user=u, site=mum, role="viewer")
        Alert.objects.create(fingerprint="a-dr", alertname="DrOnlyAlert", instance="db-01:9100", site="DR")
        Alert.objects.create(fingerprint="a-mum", alertname="MumbaiAlert", instance="db-01:9100", site="Mumbai")
        self.start_prom()
        self.client.force_login(u)
        r = self.client.get(reverse("telemetry:host_detail", args=["db-01:9100"]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "MumbaiAlert")
        self.assertNotContains(r, "DrOnlyAlert")
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(reverse("telemetry:host_detail", args=["db-01:9100"])), "DrOnlyAlert")

    @override_settings(NAGIOS_URL="https://nagios.test/nagios")
    def test_nagios_non_object_json_is_an_error_not_a_500(self):
        self.client.force_login(self.viewer)
        for body in (b"[1, 2, 3]", b'"hello"', b'{"result": "oops", "data": []}'):
            cache.clear()
            with mock.patch("telemetry.nagios._urlopen", return_value=FakeResp(body)):
                with self.assertRaises(nagios.NagiosError):
                    nagios.fetch_status()
            cache.clear()
            with mock.patch("telemetry.nagios._urlopen", return_value=FakeResp(body)):
                r = self.client.get(reverse("telemetry:nagios"))
            self.assertEqual(r.status_code, 200, body)
            self.assertContains(r, "Nagios query failed")

    @override_settings(NAGIOS_URL="https://nagios.test/nagios")
    def test_nagios_odd_values_do_not_crash(self):
        def odd(req, timeout=None):
            if "hostlist" in req.full_url:
                data = {"hostlist": {"h1": {"name": 5, "status": "down"}, "h2": None}}
            else:
                data = {"servicelist": {"h1": {"s": {"status": 16, "description": 7, "host_name": 9}}}}
            return FakeResp(json.dumps({"result": "not-a-dict", "data": data}).encode())
        with mock.patch("telemetry.nagios._urlopen", side_effect=odd):
            st = nagios.fetch_status()
        self.assertEqual({h["name"] for h in st["hosts"]}, {"5", "h2"})
        self.assertEqual(st["services"][0]["service"], "7")
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.get(reverse("telemetry:nagios")).status_code, 200)

    @override_settings(ALERTMANAGER_WEBHOOK_TOKEN=TOKEN, INCIDENT_AUTO_CREATE=False)
    def test_concurrent_insert_of_same_fingerprint_is_retried_as_update(self):
        # Alertmanager HA peers deliver the same notification in parallel: the peer's INSERT lands between our
        # SELECT ... FOR UPDATE (no row yet) and our INSERT -> IntegrityError, previously an HTTP 500
        Alert.objects.create(fingerprint="race1", alertname="HostDown", status="firing", severity="critical",
                             instance="db-01:9100", fire_count=1)
        real = Alert.objects.select_for_update
        calls = {"n": 0}

        def first_blind(*a, **k):
            calls["n"] += 1
            return Alert.objects.none() if calls["n"] == 1 else real(*a, **k)
        with mock.patch.object(Alert.objects, "select_for_update", side_effect=first_blind):
            r = self.client.post("/api/v1/alertmanager/", data=json.dumps(am_payload(am_alert(fp="race1"))),
                                 content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {TOKEN}")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["created"], 0)
        self.assertEqual(r.json()["updated"], 1)
        self.assertEqual(r.json()["events"], 0)  # still firing, same severity: no duplicate event
        self.assertEqual(Alert.objects.filter(fingerprint="race1").count(), 1)

    @override_settings(PROMETHEUS_URL=PROM)
    def test_non_prometheus_responses_become_prometheus_errors(self):
        from . import promapi
        with mock.patch("core.prometheus.query", side_effect=ValueError("Expecting value")):
            with self.assertRaisesRegex(prometheus.PrometheusError, "unexpected response"):
                promapi.query("up")
        with mock.patch("core.prometheus.targets", return_value="not a list"):
            with self.assertRaises(prometheus.PrometheusError):
                promapi.targets()
        with mock.patch("core.prometheus.query", side_effect=TypeError("'NoneType' object is not subscriptable")):
            self.client.force_login(self.viewer)
            r = self.client.get(reverse("telemetry:infrastructure"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "unexpected response")

    def test_nagios_auth_header_not_forwarded_cross_origin(self):
        import urllib.request
        h = nagios._SameOriginAuthRedirect()
        req = urllib.request.Request("https://nagios.test/nagios/cgi-bin/statusjson.cgi")
        req.add_header("Authorization", "Basic abc")
        same = h.redirect_request(req, None, 302, "Found", {}, "https://nagios.test/other")
        other = h.redirect_request(req, None, 302, "Found", {}, "http://evil.test/x")
        self.assertEqual(same.get_header("Authorization"), "Basic abc")
        self.assertIsNone(other.get_header("Authorization"))


# ============================================================================= full host metrics (Netdata-style)
from . import hosts_full  # noqa: E402

FULL_INST = "db-01:9100"


def full_query(q, at=None):
    """Instant queries for one host: some node_exporter metrics present, others (dmi, tz, carrier, cAdvisor…) absent."""
    i = FULL_INST
    if q.startswith("up{"):
        return [_row(i, 1.0, job="node", __name__="up")]
    if "node_cpu_seconds_total" in q and 'mode="idle"' in q and "count" not in q and "avg" not in q:
        return [_row(i, 10.0 * k, cpu=str(k)) for k in range(4)]            # per-core busy %
    if "count by (instance)" in q:
        return [_row(i, 4.0)]
    if 'mode="idle"' in q:
        return [_row(i, 35.0)]
    if "MemAvailable" in q:
        return [_row(i, 60.0)]
    if "node_memory_MemTotal_bytes" in q:
        return [_row(i, 8e9)]
    if "node_filesystem_avail_bytes" in q and "node_filesystem_size_bytes" in q:
        return [_row(i, 40.0)]
    if "node_filesystem_size_bytes" in q:
        return [_row(i, 100e9, mountpoint="/", device="/dev/sda1", fstype="ext4")]
    if "node_filesystem_avail_bytes" in q:
        return [_row(i, 60e9, mountpoint="/", device="/dev/sda1", fstype="ext4")]
    if "node_filesystem_files_free" in q:
        return [_row(i, 900000.0, mountpoint="/", device="/dev/sda1")]
    if "node_filesystem_files{" in q:
        return [_row(i, 1000000.0, mountpoint="/", device="/dev/sda1")]
    if "node_filesystem_device_error" in q:
        return [_row(i, 0.0, mountpoint="/", device="/dev/sda1")]
    if "node_network_up" in q:
        return [_row(i, 1.0, device="eth0")]
    if "node_network_speed_bytes" in q:
        return [_row(i, 125e6, device="eth0")]
    if "node_network_info" in q:
        return [_row(i, 1.0, device="eth0", operstate="up", address="aa:bb:cc:dd:ee:ff", duplex="full")]
    if "node_uname_info" in q:
        return [_row(i, 1.0, nodename="db-01", sysname="Linux", release="6.1.0", machine="x86_64")]
    if "node_exporter_build_info" in q:
        return [_row(i, 1.0, version="1.8.2", goversion="go1.22.5")]
    if q.startswith("node_load1{"):
        return [_row(i, 2.0)]
    if "node_time_seconds" in q:
        return [_row(i, 90000.0)]
    if "receive_bytes" in q or "transmit_bytes" in q:
        return [_row(i, 1000.0)]
    return []


def full_range(q, seconds=3600, step=None):
    now = 1_700_000_000.0

    def ser(vals, **labels):
        return {"metric": {"instance": FULL_INST, **labels}, "values": [(now + k * 60, v) for k, v in enumerate(vals)]}
    if q.startswith("node_load1{"):
        return [ser([1.0, 2.0, 3.0])]
    if q.startswith("node_load5{") or q.startswith("node_load15{"):
        return [ser([1.5, 1.5, 1.5])]
    if "by (mode)" in q:
        return [ser([10.0, 20.0, 30.0], mode="user"), ser([5.0, 5.0, 5.0], mode="system")]
    if "node_disk_read_bytes_total" in q:
        return [ser([1000.0, 2000.0], device="sda"), ser([10.0, 20.0], device="sdb")]
    if "node_disk_written_bytes_total" in q:
        return [ser([500.0, 500.0], device="sda")]
    if "node_memory_MemTotal_bytes" in q:
        return [ser([4e9, 4e9])]
    if "node_memory_Cached_bytes" in q or "node_memory_Buffers_bytes" in q or "node_memory_MemFree_bytes" in q:
        return [ser([1e9, 1e9])]
    if "node_network_receive_bytes_total" in q and "by (instance)" not in q:
        return [ser([100.0, 200.0], device="eth0")]
    if "node_cpu_seconds_total" in q or "MemAvailable" in q:
        return [ser([10.0, 11.0])]                                            # host_detail's overview charts
    return []                                                                 # PSI, huge pages, conntrack, …


def full_patches():
    return [mock.patch("core.prometheus.query", side_effect=full_query),
            mock.patch("core.prometheus.query_range", side_effect=full_range)]


class HostMetricsServiceTests(TestCase):
    def setUp(self):
        cache.clear()
        ps = full_patches()
        self.q, self.qr = [p.start() for p in ps]
        for p in ps:
            self.addCleanup(p.stop)

    def test_legend_stats_and_formatting(self):
        st = hosts_full.legend_stats([(1, 10.0), (2, 20.0), (3, None), (4, 30.0)])
        self.assertEqual((st["last"], st["mean"], st["min"], st["max"]), (30.0, 20.0, 10.0, 30.0))
        self.assertIsNone(hosts_full.legend_stats([(1, None)]))
        self.assertIsNone(hosts_full.legend_stats([]))
        self.assertEqual(hosts_full.fmt_value(42.123, "%"), "42.1%")
        self.assertEqual(hosts_full.fmt_value(2048, "B"), "2.0 KB")
        self.assertEqual(hosts_full.fmt_value(2048, "B/s"), "2.0 KB/s")
        self.assertEqual(hosts_full.fmt_value(1.5, "ms"), "1.50 ms")
        self.assertEqual(hosts_full.fmt_value(0.0025, "s"), "2.5 ms")
        self.assertEqual(hosts_full.fmt_value(1234.0, "/s"), "1,234/s")
        self.assertEqual(hosts_full.fmt_value(None, "%"), "–")
        self.assertEqual(hosts_full._utc_offset(19800), "UTC+05:30")
        self.assertEqual(hosts_full._utc_offset(-18000), "UTC-05:00")

    def test_queries_only_embed_escaped_instance(self):
        evil = 'evil"} or vector(1) #'
        for sec in hosts_full.sections_spec(evil):
            for c in sec["charts"]:
                for g in c["groups"]:
                    self.assertIn('instance="evil\\"} or vector(1) #"', g["q"])
                    self.assertNotIn('instance="evil"}', g["q"])
        for q in hosts_full.instant_queries(evil).values():
            self.assertIn('instance="evil\\"} or vector(1) #"', q)

    def test_fetch_full_sections_legend_and_missing(self):
        host = hosts.fetch_host(FULL_INST, "1h")
        full = hosts_full.fetch_host_full(host, "1h")
        self.assertEqual([s["id"] for s in full["sections"]], ["compute", "memory", "storage", "network"])
        charts = {c["key"]: c for s in full["sections"] for c in s["charts"]}
        load = charts["load"]
        self.assertTrue(load["has"])
        one = next(r for r in load["legend"] if r["name"] == "1 min")
        self.assertEqual((one["raw"]["last"], one["raw"]["mean"], one["raw"]["min"], one["raw"]["max"]), (3.0, 2.0, 1.0, 3.0))
        self.assertEqual((one["last"], one["mean"], one["min"], one["max"]), ("3", "2", "1", "3"))
        # one query_range per series group, step from hosts.step_for, stacked charts as coarser bars
        self.assertEqual(load["data"]["labels"].__len__(), 3)
        self.assertEqual(charts["cpu_modes"]["kind"], "bars")
        self.assertTrue(charts["cpu_modes"]["data"]["stacked"])
        self.assertEqual(charts["cpu_modes"]["data"]["yMax"], 100)
        self.assertEqual([s["name"] for s in charts["cpu_modes"]["data"]["series"]], ["user", "system"])
        user = next(r for r in charts["cpu_modes"]["legend"] if r["name"] == "user")
        self.assertEqual((user["last"], user["mean"]), ("30.0%", "20.0%"))
        # per-device series are named from the label
        self.assertEqual(sorted(r["name"] for r in charts["disk_bw"]["legend"]), ["sda read", "sda write", "sdb read"])
        self.assertAlmostEqual(full["disk_read"], 2020.0)
        self.assertAlmostEqual(full["disk_write"], 500.0)
        # metrics the host does not export are reported, never faked
        for key in ("psi", "hugepages", "conntrack", "tcp_estab", "disk_lat"):
            self.assertFalse(charts[key]["has"], key)
            self.assertEqual(charts[key]["legend"], [])
        self.assertIn(charts["psi"], next(s for s in full["sections"] if s["id"] == "compute")["missing"])
        # instant tables
        self.assertEqual([c["cpu"] for c in full["cores"]], ["3", "2", "1", "0"])     # busiest first
        self.assertAlmostEqual(full["cores"][0]["busy"], 30.0)
        self.assertEqual(len(full["mounts"]), 1)
        self.assertAlmostEqual(full["mounts"][0]["inode_pct"], 10.0)
        self.assertFalse(full["mounts"][0]["error"])
        self.assertTrue(full["mounts"][0]["error_known"])
        self.assertEqual(full["ifaces"][0]["device"], "eth0")
        self.assertTrue(full["ifaces"][0]["up"])
        self.assertIsNone(full["ifaces"][0]["carrier"])                               # node_network_carrier absent
        self.assertEqual(full["ifaces"][0]["operstate"], "up")
        self.assertIsNone(full["containers"])                                          # cAdvisor absent
        self.assertEqual(full["system"]["exporter_version"], "1.8.2")
        self.assertEqual(full["system"]["vendor"], "")                                 # node_dmi_info absent
        self.assertEqual(full["system"]["nodename"], "db-01")
        self.assertIsNone(full["system"]["boot_time"])
        # step: one query_range per group; bars use the coarser step
        steps = {call.args[2] for call in self.qr.call_args_list if "by (mode)" in call.args[0]}
        self.assertEqual(steps, {hosts.step_for(3600, hosts_full.STACKED_POINTS)})
        n_groups = sum(len(c["groups"]) for s in hosts_full.sections_spec(FULL_INST) for c in s["charts"])
        self.assertEqual(full["query_count"], n_groups + len(hosts_full.instant_queries(FULL_INST))
                         + len(hosts_full.gauge_queries(FULL_INST)))
        # summary strip: gauges with the change over the range, sparklines from series already fetched
        S = full["summary"]
        cpu = S["gauges"]["cpu"]
        self.assertEqual((cpu["value"], cpu["unit"], cpu["max"], cpu["label"], cpu["text"]), (35.0, "%", 100, "CPU", "35.0%"))
        self.assertEqual(cpu["delta"], {"value": 1.0, "text": "+1.0 pts in 1h"})      # cpu range series 10 -> 11
        self.assertEqual([t["to"] for t in cpu["thresholds"]], [75.0, 90.0, 100])
        self.assertEqual(S["gauges"]["memory"]["tone"], "ok")
        self.assertIsNone(S["gauges"]["disk"]["delta"])                                 # no root-disk range rows in the fake
        self.assertEqual(S["gauges"]["disk"]["value"], 40.0)
        self.assertEqual(S["sparks"]["load"]["values"], [1.0, 2.0, 3.0])
        self.assertEqual(S["sparks"]["diskio"]["values"], [1510.0, 2520.0])            # sda read + sdb read + sda write
        self.assertEqual(S["kpis"]["cpu_sub"], "4 cores")
        self.assertEqual(S["kpis"]["diskio"], "2.0 KB/s")
        self.assertEqual(S["tones"]["load1"], "ok")
        self.assertEqual(full["cores"][0]["grade"], "ok")
        self.assertEqual(full["cores"][0]["text"], "30.0%")

    def test_full_is_cached_and_refresh_bypasses(self):
        host = hosts.fetch_host(FULL_INST, "6h")
        hosts_full.fetch_host_full(host, "6h")
        n = self.qr.call_count
        hosts_full.fetch_host_full(host, "6h")
        self.assertEqual(self.qr.call_count, n)
        hosts_full.fetch_host_full(host, "6h", use_cache=False)
        self.assertGreater(self.qr.call_count, n)

    def test_series_cap_keeps_busiest(self):
        spec = hosts_full._c("many", "Many", "", [hosts_full._g("x", label="device")], "x")
        rows = [{"metric": {"device": f"d{k}"}, "values": [(1, float(k))]} for k in range(20)]
        c = hosts_full.build_chart(spec, {("many", 0): rows}, 3600)
        self.assertTrue(c["truncated"])
        self.assertEqual(c["total"], 20)
        self.assertEqual(len(c["legend"]), hosts_full.MAX_SERIES_PER_CHART)
        self.assertEqual(c["legend"][0]["name"], "d19")


class HostMetricsPageTests(TestCase):
    def setUp(self):
        cache.clear()
        self.viewer, self.operator, self.admin = users()
        ps = full_patches()
        self.q, self.qr = [p.start() for p in ps]
        for p in ps:
            self.addCleanup(p.stop)
        self.url = reverse("telemetry:host_metrics", args=[FULL_INST])

    def test_route_is_not_swallowed_by_host_detail(self):
        from django.urls import resolve
        self.assertEqual(self.url, "/monitoring/infrastructure/db-01:9100/metrics/")
        self.assertEqual(resolve(self.url).url_name, "host_metrics")
        self.assertEqual(resolve(self.url).kwargs["instance"], FULL_INST)
        self.assertEqual(resolve("/monitoring/infrastructure/db-01:9100/").url_name, "host_detail")

    def test_login_required(self):
        self.assertEqual(Client().get(self.url).status_code, 302)

    @override_settings(PROMETHEUS_URL="")
    def test_not_connected(self):
        self.client.force_login(self.viewer)
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "not connected")
        self.assertContains(r, "LINKEDEYE_PROMETHEUS_URL")
        self.q.assert_not_called()

    @override_settings(PROMETHEUS_URL=PROM)
    def test_page_renders_sections_legend_and_missing_lines(self):
        self.client.force_login(self.viewer)
        r = self.client.get(self.url + "?range=6h")
        self.assertEqual(r.status_code, 200)
        for sid in ("summary", "compute", "memory", "storage", "network", "system", "workloads"):
            self.assertContains(r, f'<section id="{sid}"')
            self.assertContains(r, f'href="#{sid}"')
        self.assertContains(r, "CPU utilisation by mode")
        self.assertContains(r, 'data-chart="bars" data-src="hm-cpu_modes"')
        self.assertContains(r, 'id="hm-load"')
        # legend table: Last / Mean / Min / Max from the known 1, 2, 3 series
        self.assertContains(r, '1 min</td><td class="num" data-col="last">3</td><td class="num" data-col="mean">2</td>'
                               '<td class="num" data-col="min">1</td><td class="num" data-col="max">3</td>')
        self.assertContains(r, 'user</td><td class="num" data-col="last">30.0%</td><td class="num" data-col="mean">20.0%</td>'
                               '<td class="num" data-col="min">10.0%</td><td class="num" data-col="max">30.0%</td>')
        # honest lines for metrics the host does not export, instead of empty charts
        self.assertContains(r, "No <code>node_pressure_* metrics</code> for this host")
        self.assertContains(r, "No <code>node_memory_HugePages_* metrics</code> for this host")
        self.assertContains(r, "No <code>node_nf_conntrack_* metrics</code> for this host")
        self.assertNotContains(r, 'data-src="hm-psi"')
        self.assertContains(r, "cAdvisor is not scraped for this host")
        self.assertContains(r, "no node_dmi_info")
        # summary strip and tables
        self.assertContains(r, "2.0 KB/s")                      # disk read = 2000 + 20 B/s
        self.assertContains(r, "cpu3")
        self.assertContains(r, "aa:bb:cc:dd:ee:ff")
        self.assertContains(r, "v1.8.2")
        self.assertContains(r, "Per-core utilisation")
        # range picker and explorer deep links carry the range and the PromQL
        self.assertContains(r, 'href="?range=6h" class="on"')
        self.assertContains(r, "/monitoring/metrics-explorer/?expr=node_load1%7Binstance%3D%22db-01%3A9100%22%7D&amp;range=6h")
        # the overview page links here
        d = self.client.get(reverse("telemetry:host_detail", args=[FULL_INST]))
        self.assertContains(d, f'href="{self.url}?range=1h"')
        self.assertContains(d, "All metrics")

    @override_settings(PROMETHEUS_URL=PROM)
    def test_bad_range_falls_back(self):
        self.client.force_login(self.viewer)
        r = self.client.get(self.url + "?range=99y")
        self.assertEqual(r.context["range_key"], "1h")

    @override_settings(PROMETHEUS_URL=PROM)
    def test_prometheus_error_renders_panel(self):
        self.client.force_login(self.viewer)
        with mock.patch("core.prometheus.query_range", side_effect=prometheus.PrometheusError("Prometheus unreachable: boom")):
            r = self.client.get(self.url)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Prometheus unreachable: boom")
        self.assertContains(r, "Prometheus query failed")
        self.assertNotContains(r, "Traceback")
        self.assertNotContains(r, 'id="compute"')
        with mock.patch("core.prometheus.query", side_effect=ValueError("not json")):
            r = self.client.get(self.url + "?refresh=1")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "unexpected response")

    @override_settings(PROMETHEUS_URL=PROM)
    def test_unknown_instance_404(self):
        self.client.force_login(self.viewer)
        with mock.patch("core.prometheus.query", return_value=[]):
            r = self.client.get(reverse("telemetry:host_metrics", args=["gone:9100"]))
        self.assertEqual(r.status_code, 404)
        self.assertContains(r, "Host not found", status_code=404)
        self.qr.assert_not_called()

    @override_settings(PROMETHEUS_URL=PROM)
    def test_site_scoped_user_denied_like_host_detail(self):
        if not _has_model("cmdb", "ConfigurationItem"):
            self.skipTest("cmdb not installed")
        CI = apps.get_model("cmdb", "ConfigurationItem")
        try:
            CI.objects.create(name="DB Server 1", ci_type="server", prometheus_instance=FULL_INST, site="DR Site")
        except Exception as e:  # pragma: no cover
            self.skipTest(f"cannot create CI: {e}")
        mum = Site.objects.create(name="Mumbai", code="mum")
        Site.objects.create(name="DR Site", code="dr")
        u = User.objects.create_user("mumviewer")
        Membership.objects.create(user=u, site=mum, role="viewer")
        self.client.force_login(u)
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(self.client.get(reverse("telemetry:host_detail", args=[FULL_INST])).status_code, 404)
        self.q.assert_not_called()
        self.client.force_login(self.admin)
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "CMDB: DB Server 1")
        self.assertContains(r, "DR Site")

    @override_settings(PROMETHEUS_URL=PROM)
    def test_get_never_mutates(self):
        self.client.force_login(self.admin)
        before = AuditEvent.objects.count()
        self.client.get(self.url)
        self.client.get(self.url + "?refresh=1")
        self.assertEqual(AuditEvent.objects.count(), before)

    @override_settings(PROMETHEUS_URL=PROM)
    def test_vm_placements_listed_and_site_scoped(self):
        if not (_has_model("compute", "VMPlacement") and _has_model("network", "Device")):
            self.skipTest("compute or network not installed")
        Device = apps.get_model("network", "Device")
        VMP = apps.get_model("compute", "VMPlacement")
        try:
            hostdev = Device.objects.create(name="hv-01", device_type="physical_server", mgmt_ip="10.0.0.1",
                                            prometheus_instance=FULL_INST, site="Mumbai")
            vm1 = Device.objects.create(name="vm-alpha", device_type="virtual_machine", mgmt_ip="10.0.0.2", site="Mumbai")
            vm2 = Device.objects.create(name="vm-dr-only", device_type="virtual_machine", mgmt_ip="10.0.0.3", site="DR Site")
            VMP.objects.create(vm=vm1, host=hostdev, vcpus=4, os="Debian 12")
            VMP.objects.create(vm=vm2, host=hostdev, vcpus=2)
        except Exception as e:  # pragma: no cover - other module's required fields changed
            self.skipTest(f"cannot create placement: {e}")
        self.client.force_login(self.admin)
        r = self.client.get(self.url)
        self.assertContains(r, "vm-alpha")
        self.assertContains(r, "vm-dr-only")
        self.assertContains(r, "Debian 12")
        mum = Site.objects.create(name="Mumbai", code="mum")
        Site.objects.create(name="DR Site", code="dr")
        u = User.objects.create_user("mumviewer2")
        Membership.objects.create(user=u, site=mum, role="viewer")
        self.client.force_login(u)
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "vm-alpha")
        self.assertNotContains(r, "vm-dr-only")


# ============================================================================= charts v2: ts, sync, gauges, Live mode
class ChartPayloadTests(TestCase):
    def test_align_emits_epoch_ts_aligned_with_labels(self):
        d = hosts.align([("a", "#111", [(1_700_000_000.0, 1.0), (1_700_000_060.0, 2.0)]),
                         ("b", "#222", [(1_700_000_060.0, 5.0), (1_700_000_120.5, None)])], 3600)
        self.assertEqual(d["ts"], [1_700_000_000, 1_700_000_060, 1_700_000_120.5])
        self.assertEqual(len(d["labels"]), len(d["ts"]))
        self.assertEqual(len(d["tipLabels"]), len(d["ts"]))
        self.assertEqual([s["values"] for s in d["series"]], [[1.0, 2.0, None], [None, 5.0, None]])
        self.assertTrue(all(isinstance(t, (int, float)) for t in d["ts"]))

    def test_delta_text(self):
        self.assertEqual(hosts_full._delta([10.0, None, 11.25], "6h"), {"value": 1.25, "text": "+1.2 pts in 6h"})
        self.assertEqual(hosts_full._delta([50.0, 48.0], "24h")["text"], "-2.0 pts in 24h")
        self.assertEqual(hosts_full._delta([3.0, 3.01], "1h"), {"value": 0.0, "text": "no change in 1h"})
        self.assertIsNone(hosts_full._delta([3.0], "1h"))
        self.assertIsNone(hosts_full._delta([], "1h"))

    def test_explorer_chart_carries_ts(self):
        rows = [{"metric": {"__name__": "up"}, "values": [(1_700_000_000.0, 1.0), (1_700_000_060.0, 1.0)]}]
        with mock.patch("core.prometheus.query_range", return_value=rows):
            res = explorer.run("up", 3600, 15)
        self.assertEqual(res["chart"]["ts"], [1_700_000_000, 1_700_000_060])


AXES_KINDS = "line,area,bars"


def chart_hosts(response):
    """{data-src: {attribute: value}} for every charts.js host div in a rendered page.

    Attributes are compared one by one so the assertions do not depend on the order the template writes them in."""
    html = response.content.decode()
    out = {}
    for tag in re.findall(r"<div\b[^>]*\bdata-chart=\"[^\"]*\"[^>]*>", html):
        attrs = dict(re.findall(r'([\w-]+)="([^"]*)"', tag))
        out[attrs.get("data-src", "")] = attrs
    return out


class HostMetricsLiveTests(TestCase):
    """Page markup for the charts.js v2 contract and the metrics.json endpoint behind Live mode."""

    def assert_axes_chart(self, hosts_by_src, src, kind, group):
        """One chart host opts into the engine's sync group and kind switch (contract: data-sync + data-kinds)."""
        self.assertIn(src, hosts_by_src, sorted(hosts_by_src))
        a = hosts_by_src[src]
        self.assertEqual(a.get("data-chart"), kind, src)
        self.assertEqual(a.get("data-sync"), group, src)
        self.assertEqual(a.get("data-kinds"), AXES_KINDS, src)

    def setUp(self):
        cache.clear()
        self.viewer, self.operator, self.admin = users()
        ps = full_patches()
        self.q, self.qr = [p.start() for p in ps]
        for p in ps:
            self.addCleanup(p.stop)
        self.url = reverse("telemetry:host_metrics", args=[FULL_INST])
        self.json_url = reverse("telemetry:host_metrics_json", args=[FULL_INST])

    def test_json_route(self):
        from django.urls import resolve
        self.assertEqual(self.json_url, "/monitoring/infrastructure/db-01:9100/metrics.json")
        self.assertEqual(resolve(self.json_url).url_name, "host_metrics_json")
        self.assertEqual(resolve(self.json_url).kwargs["instance"], FULL_INST)
        self.assertEqual(resolve("/monitoring/infrastructure/db-01:9100/").url_name, "host_detail")
        self.assertEqual(Client().get(self.json_url).status_code, 302)                 # login required

    @override_settings(PROMETHEUS_URL=PROM)
    def test_page_markup_sync_kinds_legend_gauges_and_live_button(self):
        self.client.force_login(self.viewer)
        r = self.client.get(self.url + "?range=6h")
        self.assertEqual(r.status_code, 200)
        page = chart_hosts(r)
        self.assert_axes_chart(page, "hm-load", "line", "host")
        self.assert_axes_chart(page, "hm-cpu_modes", "bars", "host")
        self.assertContains(r, 'data-chart-legend="hm-load"')
        self.assertContains(r, '<tr data-series="1 min">')
        self.assertContains(r, '<tr data-series="sda read">')
        self.assertContains(r, 'data-col="last"')
        self.assertContains(r, "Hover for values · drag to zoom · click a series to hide it · shift-click to isolate")
        # gauges + sparklines in the summary strip, with their payloads
        for dom in ("hm-g-cpu", "hm-g-memory", "hm-g-disk"):
            self.assertEqual(page[dom]["data-chart"], "gauge")
            self.assertContains(r, f'<script id="{dom}" type="application/json">')
        self.assertEqual(page["hm-s-load"]["data-chart"], "spark")
        self.assertEqual(page["hm-s-diskio"]["data-chart"], "spark")
        self.assertNotIn("hm-s-net", page)                                            # no rx samples in range: no spark
        self.assertContains(r, "+1.0 pts in 6h")
        self.assertContains(r, 'data-kpi="cpu"')
        self.assertContains(r, 'data-kpi="load1"')
        self.assertContains(r, '<div class="core" data-cpu="3">')
        # Live toggle, poll URL with the range, status text, error pill, page script
        self.assertContains(r, 'id="hm-live" aria-pressed="false" data-live-url="/monitoring/infrastructure/db-01:9100/metrics.json?range=6h"')
        self.assertContains(r, 'id="hm-updated"')
        self.assertContains(r, 'id="hm-live-err" role="alert" hidden')
        self.assertContains(r, "telemetry/host_metrics.js")
        # Explore links still carry the PromQL
        self.assertContains(r, "/monitoring/metrics-explorer/?expr=node_load1%7Binstance%3D%22db-01%3A9100%22%7D&amp;range=6h")
        # every chart host on the page is in the "host" sync group with the kind switch
        for src, a in page.items():
            if a.get("data-chart") in ("line", "area", "bars"):
                self.assert_axes_chart(page, src, a["data-chart"], "host")
        # the other chart pages opt in to one sync group each
        d = self.client.get(reverse("telemetry:host_detail", args=[FULL_INST]))
        detail = chart_hosts(d)
        for src in ("c-cpu", "c-mem", "c-load"):
            self.assert_axes_chart(detail, src, "line", "host")
        # (c-net is not rendered for this fixture: its rx/tx range query aggregates "by (instance)" and full_range
        # answers that with no series, so host_detail shows "No network samples" instead of a chart.)
        for src, a in detail.items():
            if a.get("data-chart") in ("line", "area", "bars"):
                self.assert_axes_chart(detail, src, a["data-chart"], "host")
        self.assertContains(d, '"ts": [1700000000')

    @override_settings(PROMETHEUS_URL=PROM)
    def test_json_ok_for_visible_host(self):
        self.client.force_login(self.viewer)
        r = self.client.get(self.json_url)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "application/json")
        self.assertEqual(r["Cache-Control"], "no-store")
        j = r.json()
        self.assertEqual(set(j), {"generated", "instance", "range", "health", "summary", "tones", "charts", "legends", "cores"})
        self.assertEqual(j["instance"], FULL_INST)
        self.assertEqual(j["range"], "1h")
        self.assertTrue(j["generated"].startswith("20"))
        # every metric of the fixture host grades ok: cpu 35, memory 60, disk 40 (warn at 75/80/80) and
        # load 2.0 over 4 cores = 0.5 per core (warn at 1.5) -> hosts.health_of returns "healthy"
        self.assertEqual(j["health"], {"key": "healthy", "label": "Healthy", "tone": "ok"})
        self.assertEqual(j["charts"]["hm-load"]["ts"], [1700000000, 1700000060, 1700000120])
        # the load chart carries the three averages plus the core-count reference line (sections_spec)
        self.assertEqual([s["name"] for s in j["charts"]["hm-load"]["series"]], ["1 min", "5 min", "15 min", "CPU cores"])
        self.assertNotIn("hm-psi", j["charts"])                                         # absent metrics are not faked
        self.assertEqual(j["charts"]["hm-g-cpu"]["delta"]["text"], "+1.0 pts in 1h")
        self.assertEqual(j["charts"]["hm-g-disk"]["value"], 40.0)
        self.assertEqual(j["charts"]["hm-s-load"]["values"], [1.0, 2.0, 3.0])
        self.assertEqual(j["summary"]["cpu"], "35.0%")
        self.assertEqual(j["summary"]["diskio"], "2.0 KB/s")
        self.assertEqual(j["tones"]["cpu"], "ok")
        self.assertEqual(j["legends"]["hm-load"][0], {"name": "1 min", "last": "3", "mean": "2", "min": "1", "max": "3"})
        self.assertEqual([c["cpu"] for c in j["cores"]], ["3", "2", "1", "0"])
        self.assertEqual(j["cores"][0]["text"], "30.0%")

    @override_settings(PROMETHEUS_URL=PROM)
    def test_json_respects_range_and_cache(self):
        self.client.force_login(self.viewer)
        j = self.client.get(self.json_url + "?range=6h").json()
        self.assertEqual(j["range"], "6h")
        self.assertEqual(j["charts"]["hm-g-cpu"]["delta"]["text"], "+1.0 pts in 6h")
        self.assertEqual({c.args[1] for c in self.qr.call_args_list}, {6 * 3600})
        n = self.qr.call_count
        self.assertEqual(self.client.get(self.json_url + "?range=6h").status_code, 200)
        self.assertEqual(self.qr.call_count, n)                                        # cached
        self.assertEqual(self.client.get(self.json_url + "?range=6h&refresh=1").status_code, 200)
        self.assertGreater(self.qr.call_count, n)                                      # refresh=1 bypasses
        self.assertEqual(self.client.get(self.json_url + "?range=99y").json()["range"], "1h")

    @override_settings(PROMETHEUS_URL=PROM)
    def test_json_unknown_host_404(self):
        self.client.force_login(self.viewer)
        with mock.patch("core.prometheus.query", return_value=[]):
            r = self.client.get(reverse("telemetry:host_metrics_json", args=["gone:9100"]))
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json(), {"error": "No such host"})
        self.qr.assert_not_called()

    @override_settings(PROMETHEUS_URL=PROM)
    def test_json_site_scoped_like_the_page(self):
        if not _has_model("cmdb", "ConfigurationItem"):
            self.skipTest("cmdb not installed")
        CI = apps.get_model("cmdb", "ConfigurationItem")
        try:
            CI.objects.create(name="DB Server 1", ci_type="server", prometheus_instance=FULL_INST, site="DR Site")
        except Exception as e:  # pragma: no cover
            self.skipTest(f"cannot create CI: {e}")
        mum = Site.objects.create(name="Mumbai", code="mum")
        Site.objects.create(name="DR Site", code="dr")
        u = User.objects.create_user("mumviewer3")
        Membership.objects.create(user=u, site=mum, role="viewer")
        self.client.force_login(u)
        r = self.client.get(self.json_url)
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json(), {"error": "No such host"})
        self.q.assert_not_called()                                                     # denied before any query
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(self.json_url).status_code, 200)

    @override_settings(PROMETHEUS_URL="")
    def test_json_not_configured_503(self):
        self.client.force_login(self.viewer)
        r = self.client.get(self.json_url)
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json(), {"error": "Prometheus is not configured"})
        self.q.assert_not_called()

    @override_settings(PROMETHEUS_URL=PROM)
    def test_json_prometheus_error_503(self):
        self.client.force_login(self.viewer)
        with mock.patch("core.prometheus.query_range", side_effect=prometheus.PrometheusError("Prometheus unreachable: boom")):
            r = self.client.get(self.json_url)
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json(), {"error": "Prometheus unreachable: boom"})

    @override_settings(PROMETHEUS_URL=PROM)
    def test_a_cache_entry_from_before_the_summary_strip_is_not_served(self):
        """fetch_host_full's cache key is versioned, so a payload cached by the previous release is ignored.

        live_payload() reads full["summary"] and the per-core rows' grade/text directly; a v1 entry still in a
        shared cache (Redis) after a deploy would 500 the Live poll and blank the gauges on the page."""
        self.client.force_login(self.viewer)
        stale = {"instance": FULL_INST, "range": "1h", "seconds": 3600, "step": 30, "bar_step": 60,
                 "sections": [], "disk_read": None, "disk_write": None, "cores": [{"cpu": "0", "busy": 1.0}],
                 "mounts": [], "ifaces": [], "containers": [], "system": {}, "query_count": 0}
        key = "telemetry:hostfull:v1:" + hashlib.sha1(f"{FULL_INST}|1h".encode()).hexdigest()
        cache.set(key, stale, 300)
        j = self.client.get(self.json_url)
        self.assertEqual(j.status_code, 200)
        self.assertIn("hm-g-cpu", j.json()["charts"])
        self.assertEqual(j.json()["cores"][0]["text"], "30.0%")
        self.assertEqual(self.client.get(self.url).status_code, 200)

    @override_settings(PROMETHEUS_URL=PROM)
    def test_json_is_strict_json_when_prometheus_sends_nan_and_inf(self):
        """A "+Inf" / "NaN" sample (a rate over a counter reset, a 0/0 ratio) must reach the page as a gap or a dash.

        JsonResponse and json_script both dump float('nan') as the bare token NaN, which JSON.parse rejects: the
        Live poll would then show "unexpected response" and the page's charts would silently not draw."""
        def bad(x):
            raise AssertionError(f"non-JSON constant {x} in the response")
        inf, nan = float("inf"), float("nan")

        def q(promql, at=None):
            rows = full_query(promql, at)
            return [dict(r, value=inf if k % 3 == 0 else nan) if k % 2 == 0 else r for k, r in enumerate(rows)]

        def qr(promql, seconds=3600, step=None):
            out = []
            for r in full_range(promql, seconds, step):
                vals = [(t, inf if k == 1 else nan if k == 2 else v) for k, (t, v) in enumerate(r["values"])]
                out.append(dict(r, values=vals))
            return out
        self.client.force_login(self.viewer)
        with mock.patch("core.prometheus.query", side_effect=q), mock.patch("core.prometheus.query_range", side_effect=qr):
            r = self.client.get(self.json_url + "?refresh=1")
            self.assertEqual(r.status_code, 200)
            json.loads(r.content, parse_constant=bad)
            page = self.client.get(self.url + "?refresh=1")
        self.assertEqual(page.status_code, 200)
        for m in re.finditer(r'<script id="[^"]+" type="application/json">(.*?)</script>', page.content.decode(), re.S):
            json.loads(m.group(1), parse_constant=bad)

    @override_settings(PROMETHEUS_URL=PROM)
    def test_json_get_never_mutates(self):
        self.client.force_login(self.admin)
        before = AuditEvent.objects.count()
        self.client.get(self.json_url)
        self.client.get(self.json_url + "?refresh=1")
        self.assertEqual(AuditEvent.objects.count(), before)
