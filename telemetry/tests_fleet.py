"""Fleet home tests. Prometheus is always mocked; nothing here touches the network."""
import time
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core import prometheus
from core.models import Membership, Site

from . import fleet
from .models import Alert

PROM = "http://prom.test:9090"

# instance: (up, cpu, memory, disk, load1, cores)
HOSTS = {
    "web-01:9100": (1, 20.0, 40.0, 30.0, 0.5, 4),
    "db-01:9100": (1, 95.0, 50.0, 97.0, 2.0, 8),
    "app-02:9100": (0, None, None, None, None, None),
    "ghost-01:9100": (None, None, None, None, None, None),  # a target with no `up` sample -> stale
}
NOW_ISO = None  # filled per test run by _targets()


def _row(inst, v, **labels):
    return {"metric": {"instance": inst, **labels}, "value": v}


def fake_query(q, at=None):
    def each(i):
        return [_row(inst, vals[i]) for inst, vals in HOSTS.items() if vals[i] is not None]
    if q.startswith("up{"):
        return [_row(i, float(v[0]), job="node", __name__="up") for i, v in HOSTS.items() if v[0] is not None]
    if q == fleet.SERIES_QUERY:
        return [{"metric": {}, "value": 54321.0}]
    if "node_os_info" in q:
        return [_row(i, 1.0, pretty_name="Debian GNU/Linux 12") for i in ("web-01:9100", "db-01:9100")]
    if "node_uname_info" in q:
        return [_row(i, 1.0, sysname="Linux", release="6.1.0") for i in HOSTS]
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
    return []


def fake_targets():
    """Node targets for every host in HOSTS (all freshly scraped), plus one blackbox target that is down."""
    now = timezone.now()
    out = []
    for inst in HOSTS:
        out.append({"labels": {"instance": inst, "job": "node"}, "scrapePool": "node",
                    "health": "down" if HOSTS[inst][0] == 0 else "up", "scrapeInterval": "15s",
                    "lastScrape": now.isoformat().replace("+00:00", "Z"), "lastError": "",
                    "scrapeUrl": f"http://{inst}/metrics", "lastScrapeDuration": 0.02})
    out.append({"labels": {"instance": "https://example.com", "job": "blackbox"}, "scrapePool": "blackbox",
                "health": "down", "scrapeInterval": "30s", "lastScrape": now.isoformat().replace("+00:00", "Z"),
                "lastError": "timeout", "scrapeUrl": "http://bb:9115/probe", "lastScrapeDuration": 1.0})
    return out


def prom_patches():
    return [mock.patch("core.prometheus.query", side_effect=fake_query),
            mock.patch("core.prometheus.targets", side_effect=fake_targets),
            mock.patch("core.prometheus.query_range", return_value=[])]


class FleetMixin:
    def start_prom(self):
        for p in prom_patches():
            p.start()
            self.addCleanup(p.stop)

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)


def ci(name, instance, site="Mumbai", environment="prod", owner=""):
    from django.apps import apps
    CI = apps.get_model("cmdb", "ConfigurationItem")
    return CI.objects.create(name=name, prometheus_instance=instance, site=site, environment=environment,
                             owner=owner, ci_type="server")


def alert(name="HostDown", instance="db-01:9100", severity="critical", minutes_ago=30, duration=600,
          status=Alert.FIRING, site="", fire_count=1):
    now = timezone.now()
    last = now - timedelta(minutes=minutes_ago)
    return Alert.objects.create(
        fingerprint=f"{name}-{instance}-{severity}-{minutes_ago}-{time.monotonic_ns()}", alertname=name,
        instance=instance, severity=severity, status=status, site=site, fire_count=fire_count,
        starts_at=last - timedelta(seconds=duration), ends_at=last if status == Alert.RESOLVED else None,
        last_received_at=last)


# ============================================================================= the four states
class HostStateTests(TestCase):
    def setUp(self):
        self.now = timezone.now()

    def target(self, health="up", interval="15s", age=0):
        return {"instance": "x:9100", "job": "node", "health": health, "interval": interval,
                "last_scrape": self.now - timedelta(seconds=age)}

    def test_live(self):
        self.assertEqual(fleet.host_state(1, self.target(), now=self.now), fleet.LIVE)
        self.assertEqual(fleet.host_state(1, None, now=self.now), fleet.LIVE)

    def test_down_from_up_zero_and_from_target_health(self):
        self.assertEqual(fleet.host_state(0, self.target(), now=self.now), fleet.DOWN)
        self.assertEqual(fleet.host_state(0, None, now=self.now), fleet.DOWN)
        # a failed scrape is authoritative even when the `up` sample has already expired
        self.assertEqual(fleet.host_state(None, self.target(health="down"), now=self.now), fleet.DOWN)

    def test_stale_rule_is_twice_the_scrape_interval(self):
        self.assertEqual(fleet.host_state(1, self.target(age=29), now=self.now), fleet.LIVE)   # 29s < 2 x 15s
        self.assertEqual(fleet.host_state(1, self.target(age=30), now=self.now), fleet.LIVE)   # exactly 2x: not yet
        self.assertEqual(fleet.host_state(1, self.target(age=31), now=self.now), fleet.STALE)  # past 2x
        self.assertEqual(fleet.host_state(0, self.target(age=400), now=self.now), fleet.STALE)

    def test_grace_covers_the_target_cache_age(self):
        # a 15 s target read from the 30 s target cache can look 40 s old while being perfectly healthy
        t = self.target(age=40)
        self.assertEqual(fleet.host_state(1, t, now=self.now), fleet.STALE)              # no allowance
        self.assertEqual(fleet.host_state(1, t, now=self.now, grace=30), fleet.LIVE)     # cache allowance
        self.assertEqual(fleet.host_state(1, self.target(age=61), now=self.now, grace=30), fleet.STALE)
        rows = fleet.annotate_states([{"instance": "x:9100", "up": 1}], {"x:9100": t}, now=self.now)
        self.assertEqual(rows[0]["state"], fleet.LIVE)   # annotate_states applies the cache allowance itself

    def test_stale_when_up_is_missing_but_the_target_exists(self):
        self.assertEqual(fleet.host_state(None, self.target(), now=self.now), fleet.STALE)

    def test_unknown_scrape_interval_is_never_judged_stale_on_age(self):
        self.assertEqual(fleet.host_state(1, self.target(interval="", age=99999), now=self.now), fleet.LIVE)

    def test_never_seen_without_a_sample_or_a_target(self):
        self.assertEqual(fleet.host_state(None, None, now=self.now), fleet.NEVER_SEEN)

    def test_every_state_is_one_of_four(self):
        for up in (1, 0, None):
            for t in (None, self.target(), self.target(health="down"), self.target(age=10_000)):
                self.assertIn(fleet.host_state(up, t, now=self.now), fleet.STATES)

    def test_parse_interval(self):
        self.assertEqual(fleet.parse_interval("15s"), 15)
        self.assertEqual(fleet.parse_interval("1m30s"), 90)
        self.assertEqual(fleet.parse_interval("500ms"), 0.5)
        self.assertIsNone(fleet.parse_interval(""))
        self.assertIsNone(fleet.parse_interval("soon"))
        self.assertIsNone(fleet.parse_interval(None))


@override_settings(PROMETHEUS_URL=PROM)
class HostStatesTests(FleetMixin, TestCase):
    def test_merges_prometheus_targets_and_cmdb(self):
        self.start_prom()
        ci("Paper CI", "paper-01:9100")           # CMDB only: never produced a sample
        data = fleet.host_states()
        states = {h["instance"]: h["state"] for h in data["hosts"]}
        self.assertEqual(states["web-01:9100"], fleet.LIVE)
        self.assertEqual(states["db-01:9100"], fleet.LIVE)
        self.assertEqual(states["app-02:9100"], fleet.DOWN)
        self.assertEqual(states["ghost-01:9100"], fleet.STALE)
        self.assertEqual(states["paper-01:9100"], fleet.NEVER_SEEN)
        self.assertEqual(data["errors"], {})
        by = {h["instance"]: h for h in data["hosts"]}
        self.assertEqual(by["web-01:9100"]["os"], "Debian GNU/Linux 12")
        self.assertEqual(by["ghost-01:9100"]["os"], "Linux 6.1.0")
        self.assertEqual(by["paper-01:9100"]["name"], "Paper CI")
        self.assertEqual(by["paper-01:9100"]["environment_label"], "Production")

    def test_prometheus_down_still_returns_the_cmdb_inventory(self):
        ci("Paper CI", "paper-01:9100")
        with mock.patch("core.prometheus.query", side_effect=prometheus.PrometheusError("boom")), \
             mock.patch("core.prometheus.targets", side_effect=prometheus.PrometheusError("boom")):
            data = fleet.host_states()
        self.assertEqual([h["state"] for h in data["hosts"]], [fleet.NEVER_SEEN])
        self.assertEqual(data["errors"]["hosts"], "boom")
        self.assertEqual(data["errors"]["targets"], "boom")

    def test_without_prometheus_there_is_no_error(self):
        ci("Paper CI", "paper-01:9100")
        data = fleet.host_states(with_prometheus=False)
        self.assertEqual(data["errors"], {})
        self.assertEqual(len(data["hosts"]), 1)

    def test_series_count_and_its_failure(self):
        self.start_prom()
        self.assertEqual(fleet.series_count(), (54321, ""))
        cache.clear()
        with mock.patch("core.prometheus.query", side_effect=prometheus.PrometheusError("nope")):
            self.assertEqual(fleet.series_count(), (None, "nope"))
        # a failure is cached only briefly, so a recovered Prometheus is picked up well before 5 minutes
        with mock.patch("telemetry.fleet.cache.set") as cset:
            with mock.patch("core.prometheus.query", side_effect=prometheus.PrometheusError("nope")):
                fleet.series_count(use_cache=False)
        self.assertEqual(cset.call_args[0][2], fleet.CACHE_SECONDS)


# ============================================================================= group by / colour by
class GroupAndColourTests(TestCase):
    def hosts(self):
        return [
            {"instance": "a:9100", "state": fleet.LIVE, "state_label": "Live", "job": "node", "site": "Mumbai",
             "environment": "prod", "environment_label": "Production", "os": "Debian 12", "client": "Acme",
             "cpu": 10.0, "memory": 85.0, "disk": None, "alerts_firing": 0, "alerts_critical": 0, "alerts_warning": 0},
            {"instance": "b:9100", "state": fleet.DOWN, "state_label": "Down", "job": "node-dr", "site": "",
             "environment": "", "environment_label": fleet.UNKNOWN, "os": "", "client": "",
             "cpu": 95.0, "memory": None, "disk": 20.0, "alerts_firing": 2, "alerts_critical": 1, "alerts_warning": 1},
            {"instance": "c:9100", "state": fleet.STALE, "state_label": "Stale", "job": "node", "site": "Chennai",
             "environment": "dr", "environment_label": "DR", "os": "Debian 12", "client": "Acme",
             "cpu": 80.0, "memory": 10.0, "disk": 10.0, "alerts_firing": 1, "alerts_critical": 0, "alerts_warning": 1},
        ]

    def test_group_key_and_colour_key_validation(self):
        self.assertEqual(fleet.group_key("site"), "site")
        self.assertEqual(fleet.group_key("SITE"), "site")
        self.assertEqual(fleet.group_key("nonsense"), "")
        self.assertEqual(fleet.group_key(None), "")
        self.assertEqual(fleet.colour_key("memory"), "memory")
        self.assertEqual(fleet.colour_key("nonsense"), "state")

    def test_every_group_by_option_buckets_every_host(self):
        rows = self.hosts()
        for key, _label in fleet.GROUPS:
            groups = fleet.grouped(rows, key)
            self.assertEqual(sum(len(hs) for _l, hs in groups), len(rows), key)
            if key:
                self.assertTrue(all(label for label, _hs in groups), key)

    def test_group_by_each_dimension(self):
        rows = self.hosts()
        self.assertEqual([l for l, _ in fleet.grouped(rows, "site")], ["Chennai", "Mumbai", fleet.UNKNOWN])
        self.assertEqual([l for l, _ in fleet.grouped(rows, "job")], ["node", "node-dr"])
        self.assertEqual([l for l, _ in fleet.grouped(rows, "environment")], ["DR", "Production", fleet.UNKNOWN])
        self.assertEqual([l for l, _ in fleet.grouped(rows, "os")], ["Debian 12", fleet.UNKNOWN])
        self.assertEqual([l for l, _ in fleet.grouped(rows, "client")], ["Acme", fleet.UNKNOWN])
        self.assertEqual([l for l, _ in fleet.grouped(rows, "state")], ["Live", "Down", "Stale"])
        self.assertEqual(fleet.grouped(rows, ""), [("", rows)])

    def test_every_colour_by_option_gives_a_colour_and_a_legend(self):
        rows = self.hosts()
        for key, _label in fleet.COLOURS:
            legend = fleet.colour_legend(key)
            self.assertTrue(legend)
            hues = {hue for _l, hue in legend}
            for h in rows:
                hue, text = fleet.colour_of(h, key)
                self.assertIn(hue, hues, f"{key} {h['instance']}")
                self.assertTrue(text)

    def test_colour_by_metric_uses_the_host_thresholds(self):
        rows = self.hosts()
        from . import palette as pal
        self.assertEqual(fleet.colour_of(rows[0], "cpu")[0], pal.GREEN)       # 10 %
        self.assertEqual(fleet.colour_of(rows[2], "cpu")[0], pal.AMBER)       # 80 % -> warn
        self.assertEqual(fleet.colour_of(rows[1], "cpu")[0], pal.CRIT)        # 95 % -> crit
        self.assertEqual(fleet.colour_of(rows[1], "memory")[0], pal.STONE_LIGHT)   # no data
        self.assertEqual(fleet.colour_of(rows[0], "memory")[1], "85.0%")

    def test_colour_by_state_and_alerts(self):
        rows = self.hosts()
        from . import palette as pal
        self.assertEqual(fleet.colour_of(rows[1], "state")[0], fleet.STATE_COLOR[fleet.DOWN])
        self.assertEqual(fleet.colour_of(rows[1], "alerts")[0], pal.CRIT)
        self.assertEqual(fleet.colour_of(rows[2], "alerts")[0], pal.AMBER)
        self.assertEqual(fleet.colour_of(rows[0], "alerts"), (pal.GREEN, "0 firing alerts"))

    def test_bar_segment_labels_stay_legible(self):
        from . import palette as pal
        self.assertEqual(fleet.ink_on(pal.STONE_LIGHT), fleet.DARK_INK)   # pale stone needs dark text
        self.assertEqual(fleet.ink_on(pal.AMBER), fleet.DARK_INK)
        self.assertEqual(fleet.ink_on(pal.CRIT), fleet.LIGHT_INK)
        self.assertEqual(fleet.ink_on(pal.DOWN), fleet.LIGHT_INK)
        for s in fleet.state_bar(self.hosts()):
            self.assertIn(s["ink"], (fleet.DARK_INK, fleet.LIGHT_INK))

    def test_bar_segments_fill_the_bar(self):
        bar = fleet.state_bar(self.hosts())
        self.assertTrue(all(s["wide"] for s in bar))     # three equal thirds all carry their label
        thin = fleet.bar_segments([("a", "Big", 99, "#000000"), ("b", "Thin", 1, "#ffffff")])
        self.assertEqual([s["wide"] for s in thin], [True, False])
        self.assertEqual([s["key"] for s in bar], [fleet.LIVE, fleet.DOWN, fleet.STALE])
        self.assertEqual(round(sum(s["pct"] for s in bar), 2), 100.0)
        self.assertEqual(fleet.state_bar([]), [])


# ============================================================================= alert leaderboards
class AlertLeaderboardTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.start = self.now - timedelta(hours=24)
        self.hosts = [{"instance": "db-01:9100", "name": "Database", "url": "/db"},
                      {"instance": "web-01:9100", "name": "web-01:9100", "url": "/web"}]

    def test_leaders_sorted_by_total_then_criticals_then_name(self):
        alert(name="A", instance="db-01:9100", severity="critical")
        alert(name="B", instance="db-01:9100", severity="warning")
        alert(name="C", instance="web-01:9100", severity="critical")
        alert(name="D", instance="zz-99:9100", severity="warning")
        rows = fleet.host_alert_leaders(Alert.objects.all(), self.hosts, self.start, now=self.now)
        self.assertEqual([r["instance"] for r in rows], ["db-01:9100", "web-01:9100", "zz-99:9100"])
        self.assertEqual(rows[0]["total"], 2)
        self.assertEqual((rows[0]["critical"], rows[0]["warning"]), (1, 1))
        self.assertEqual(rows[0]["name"], "Database")
        self.assertEqual(rows[0]["pct"], 100.0)
        self.assertEqual(rows[1]["pct"], 50.0)
        self.assertEqual(rows[2]["url"], "")   # not a known host: still listed, with no link

    def test_ties_break_on_criticals_then_instance(self):
        alert(name="A", instance="b-02:9100", severity="warning")
        alert(name="A", instance="a-01:9100", severity="warning")
        alert(name="A", instance="c-03:9100", severity="critical")
        rows = fleet.host_alert_leaders(Alert.objects.all(), [], self.start, now=self.now)
        self.assertEqual([r["instance"] for r in rows], ["c-03:9100", "a-01:9100", "b-02:9100"])

    def test_occurrences_count_alert_rows_and_duration_is_clipped_to_the_window(self):
        alert(name="Flapping", instance="db-01:9100", severity="warning", minutes_ago=5,
              duration=48 * 3600, fire_count=7)
        rows = fleet.host_alert_leaders(Alert.objects.all(), self.hosts, self.start, now=self.now)
        self.assertEqual(rows[0]["total"], 1)     # fire_count is a lifetime counter: it says nothing about this window
        # started 48 h ago but the window is 24 h: at most 24 h of firing time is counted
        self.assertLessEqual(rows[0]["seconds"], 24 * 3600)
        self.assertGreater(rows[0]["seconds"], 23 * 3600)

    def test_top_alerts_group_by_name_and_instance(self):
        alert(name="HostDown", instance="db-01:9100", severity="warning", fire_count=2)
        alert(name="HostDown", instance="db-01:9100", severity="critical", fire_count=1, minutes_ago=31)
        alert(name="HostDown", instance="web-01:9100", severity="warning")
        alert(name="DiskFull", instance="db-01:9100", severity="critical")
        rows = fleet.top_alerts(Alert.objects.all(), self.start, now=self.now)
        self.assertEqual((rows[0]["alertname"], rows[0]["instance"], rows[0]["occurrences"]),
                         ("HostDown", "db-01:9100", 2))
        self.assertEqual(rows[0]["tone"], "crit")   # the worst severity of the group wins
        self.assertEqual(len(rows), 4 - 1)

    def test_resolved_alerts_stop_counting_time_at_their_end(self):
        alert(name="Done", instance="db-01:9100", severity="warning", minutes_ago=60, duration=1800,
              status=Alert.RESOLVED)
        rows = fleet.top_alerts(Alert.objects.all(), self.start, now=self.now)
        self.assertEqual(rows[0]["seconds"], 1800)

    def test_window_boundary_is_inclusive(self):
        user = User.objects.create_superuser("rootx", "r@example.com", "pw")
        a = alert(name="Edge", instance="db-01:9100", minutes_ago=0)
        start = self.now - timedelta(hours=6)
        Alert.objects.filter(pk=a.pk).update(last_received_at=start)
        self.assertEqual(list(fleet.alerts_in_window(user, start)), [a])
        self.assertEqual(list(fleet.alerts_in_window(user, start + timedelta(seconds=1))), [])

    def test_alert_window_falls_back_to_24h(self):
        self.assertEqual(fleet.alert_window("6h")[0], "6h")
        self.assertEqual(fleet.alert_window("nonsense")[0], "24h")


# ============================================================================= the page
@override_settings(PROMETHEUS_URL=PROM)
class FleetPageTests(FleetMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("telemetry:fleet")
        self.admin = User.objects.create_superuser("root", "root@example.com", "pw")
        self.client = Client()
        self.client.force_login(self.admin)
        ci("Database", "db-01:9100", site="Mumbai", environment="prod")
        ci("Web", "web-01:9100", site="Chennai", environment="uat")
        ci("Paper only", "paper-01:9100", site="Mumbai", environment="dr")

    def test_page_renders_every_panel(self):
        self.start_prom()
        alert(name="HostDown", instance="app-02:9100", severity="critical")
        alert(name="HighCPU", instance="db-01:9100", severity="warning")
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 200)
        for text in ("Fleet", "Node status", "Hosts with the most alerts", "Top alerts", "Sites",
                     "Never seen", "Group by", "Colour by", "HostDown", "Database"):
            self.assertContains(r, text)
        counters = r.context["counters"]
        self.assertEqual(counters["hosts"], 5)           # 4 from Prometheus + 1 CMDB-only
        self.assertEqual(counters["series"], 54321)
        self.assertEqual(counters["targets_down"], 2)    # app-02 + the blackbox probe
        self.assertEqual(counters["sites"], 2)
        self.assertEqual(counters["no_cmdb"], 2)         # app-02 and ghost-01 have no CI
        self.assertEqual({s["key"]: s["count"] for s in r.context["states"]},
                         {"live": 2, "down": 1, "stale": 1, "never_seen": 1})

    def test_group_and_colour_parameters_are_server_rendered(self):
        self.start_prom()
        r = self.client.get(self.url, {"group": "site", "colour": "cpu"})
        self.assertEqual((r.context["group"], r.context["colour"]), ("site", "cpu"))
        self.assertEqual([g["label"] for g in r.context["grid"]], ["Chennai", "Mumbai", fleet.UNKNOWN])
        self.assertContains(r, "Mumbai")
        r = self.client.get(self.url, {"group": "junk", "colour": "junk"})
        self.assertEqual((r.context["group"], r.context["colour"]), ("", "state"))

    def test_state_segment_filters_the_host_table(self):
        self.start_prom()
        r = self.client.get(self.url, {"state": "down"})
        self.assertEqual([h["instance"] for h in r.context["page"]], ["app-02:9100"])
        self.assertEqual(r.context["hosts_total"], 5)    # the bars still describe the whole fleet
        self.assertEqual(r.context["grid_shown"], 1)

    def test_csv_export_and_escaping(self):
        self.start_prom()
        ci("=cmd|' /c calc", "evil-01:9100", site="Mumbai")
        r = self.client.get(self.url, {"format": "csv"})
        self.assertEqual(r["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("attachment; filename=\"fleet-hosts.csv\"", r["Content-Disposition"])
        body = r.content.decode()
        self.assertIn("instance,name,state,job", body)
        self.assertIn("db-01:9100", body)
        self.assertIn(",'=cmd|' /c calc,", body)         # formula neutralised by csv_response
        self.assertIn("never_seen", body)
        rows = [l for l in body.strip().splitlines() if l]
        self.assertEqual(len(rows), 1 + 6)

    def test_csv_export_follows_the_filters(self):
        self.start_prom()
        body = self.client.get(self.url, {"format": "csv", "state": "live"}).content.decode()
        self.assertIn("web-01:9100", body)
        self.assertNotIn("app-02:9100", body)

    @override_settings(PROMETHEUS_URL="")
    def test_page_without_prometheus(self):
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.context["prom_configured"])
        self.assertContains(r, "Prometheus not connected")
        self.assertEqual(r.context["errors"], {})
        self.assertEqual(r.context["counters"]["hosts"], 3)          # the CMDB inventory
        self.assertEqual({s["key"]: s["count"] for s in r.context["states"]}["never_seen"], 3)

    def test_page_when_prometheus_raises(self):
        with mock.patch("core.prometheus.query", side_effect=prometheus.PrometheusError("prom exploded")), \
             mock.patch("core.prometheus.targets", side_effect=prometheus.PrometheusError("prom exploded")):
            r = self.client.get(self.url)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Some fleet data is unavailable")
        self.assertContains(r, "prom exploded")
        self.assertEqual(r.context["counters"]["hosts"], 3)
        self.assertIsNone(r.context["counters"]["series"])

    def test_hostile_parameters_fall_back_to_safe_defaults(self):
        self.start_prom()
        evil = "');DROP TABLE x;--{__name__=~\".*\"}<script>alert(1)</script>"
        r = self.client.get(self.url, {"group": evil, "colour": evil, "range": evil, "state": evil,
                                       "env": evil, "job": evil, "site": evil, "q": evil, "per": evil,
                                       "page": evil})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["group"], "")
        self.assertEqual(r.context["colour"], "state")
        self.assertEqual(r.context["range_key"], "24h")
        self.assertNotContains(r, "<script>alert(1)</script>")
        self.assertEqual(self.client.get(self.url, {"format": "csv", "group": evil, "colour": evil}).status_code, 200)

    def test_scraped_label_values_are_escaped(self):
        self.start_prom()
        bad = "<img src=x onerror=alert(1)>"

        def q(query, at=None):
            rows = fake_query(query, at)
            if "node_os_info" in query:
                return [_row("web-01:9100", 1.0, pretty_name=bad)]
            return rows
        with mock.patch("core.prometheus.query", side_effect=q):
            r = self.client.get(self.url, {"group": "os", "per": 100})
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, bad)
        self.assertContains(r, "&lt;img src=x onerror=alert(1)&gt;")

    def test_viewer_may_read_and_anonymous_may_not(self):
        c = Client()
        c.force_login(User.objects.create_user("nobody", password="pw"))
        self.assertEqual(c.get(self.url).status_code, 200)  # a signed-in user is a viewer everywhere
        self.assertEqual(Client().get(self.url).status_code, 302)  # anonymous: login redirect

    def test_the_alert_counter_and_the_active_alerts_panel_agree(self):
        self.start_prom()
        alert(name="OnAHost", instance="db-01:9100", severity="critical")
        alert(name="Elsewhere", instance="not-in-the-fleet:9100", severity="warning")
        alert(name="Old", instance="db-01:9100", severity="warning", minutes_ago=60 * 30)  # outside every range
        alert(name="Gone", instance="db-01:9100", severity="critical", status=Alert.RESOLVED)
        r = self.client.get(self.url)
        self.assertEqual(r.context["counters"]["alerts_firing"], 3)      # every firing alert, resolved excluded
        self.assertEqual(r.context["alert_split"]["total"], 3)           # the "Active alerts" panel agrees
        self.assertEqual(r.context["alerts_on_hosts"], 2)                # two of them sit on a fleet host
        self.assertEqual(sum(s["value"] for s in r.context["alert_bar"]), 3)

    def test_range_selector_drives_the_alert_panels(self):
        self.start_prom()
        alert(name="Old", instance="db-01:9100", severity="warning", minutes_ago=120)
        r = self.client.get(self.url, {"range": "1h"})
        self.assertEqual(r.context["range_key"], "1h")
        self.assertEqual(r.context["top_alerts"], [])
        r = self.client.get(self.url, {"range": "24h"})
        self.assertEqual([a["alertname"] for a in r.context["top_alerts"]], ["Old"])

    def test_fleet_and_infrastructure_agree_on_every_host(self):
        self.start_prom()
        fleet_states = {h["instance"]: h["state"] for h in self.client.get(self.url).context["page"].object_list}
        infra = self.client.get(reverse("telemetry:infrastructure"), {"per": 100})
        infra_states = {h["instance"]: h["state"] for h in infra.context["page"]}
        self.assertTrue(infra_states)
        for instance, state in infra_states.items():
            self.assertEqual(fleet_states[instance], state, instance)


# ============================================================================= site scoping
@override_settings(PROMETHEUS_URL=PROM)
class FleetScopingTests(FleetMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("telemetry:fleet")
        mumbai = Site.objects.create(name="Mumbai", code="mumbai")
        Site.objects.create(name="Chennai", code="chennai")
        self.user = User.objects.create_user("single", password="pw")
        Membership.objects.create(user=self.user, site=mumbai, role="viewer")
        ci("Database", "db-01:9100", site="Mumbai")
        ci("Web", "web-01:9100", site="Chennai")
        ci("Paper only", "paper-01:9100", site="Mumbai")
        alert(name="Mine", instance="db-01:9100", severity="critical", site="Mumbai")
        alert(name="Theirs", instance="web-01:9100", severity="critical", site="Chennai")
        alert(name="Global", instance="app-02:9100", severity="warning", site="")
        self.client = Client()
        self.client.force_login(self.user)

    def test_one_site_user_sees_only_their_hosts_counts_and_alerts(self):
        self.start_prom()
        r = self.client.get(self.url, {"per": 100})
        instances = [h["instance"] for h in r.context["page"]]
        self.assertIn("db-01:9100", instances)
        self.assertIn("paper-01:9100", instances)
        self.assertNotIn("web-01:9100", instances)          # Chennai is not theirs
        self.assertIn("app-02:9100", instances)             # no CMDB item: a global host stays visible
        self.assertEqual(r.context["counters"]["hosts"], 4)
        self.assertEqual(r.context["counters"]["sites"], 1)
        names = {a["alertname"] for a in r.context["top_alerts"]}
        self.assertEqual(names, {"Mine", "Global"})
        self.assertEqual([s["site"] for s in r.context["sites"]], ["Mumbai", fleet.UNKNOWN])

    def test_superuser_sees_everything(self):
        self.start_prom()
        c = Client()
        c.force_login(User.objects.create_superuser("root2", "r2@example.com", "pw"))
        r = c.get(self.url, {"per": 100})
        instances = [h["instance"] for h in r.context["page"]]
        self.assertIn("web-01:9100", instances)
        self.assertEqual({a["alertname"] for a in r.context["top_alerts"]}, {"Mine", "Theirs", "Global"})

    def test_a_site_parameter_cannot_reach_another_sites_data(self):
        self.start_prom()
        r = self.client.get(self.url, {"site": "Chennai", "per": 100})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("web-01:9100", [h["instance"] for h in r.context["page"]])
        self.assertEqual(r.context["top_alerts"], [])
        self.assertEqual(r.context["alert_leaders"], [])
        self.assertNotContains(r, "Theirs")

    def test_csv_export_is_scoped_too(self):
        self.start_prom()
        body = self.client.get(self.url, {"format": "csv"}).content.decode()
        self.assertIn("db-01:9100", body)
        self.assertNotIn("web-01:9100", body)


# ============================================================================= scale
@override_settings(PROMETHEUS_URL=PROM)
class FleetScaleTests(FleetMixin, TestCase):
    def test_500_hosts_render(self):
        admin = User.objects.create_superuser("rootbig", "b@example.com", "pw")
        c = Client()
        c.force_login(admin)
        now = timezone.now()
        hosts = []
        for i in range(500):
            s = fleet.STATES[i % 4]
            hosts.append({"instance": f"host-{i:03d}:9100", "job": "node", "up": 1, "health": "healthy",
                          "cpu": float(i % 100), "memory": 50.0, "disk": 10.0, "load1": 1.0, "cores": 4,
                          "rx": None, "tx": None, "uptime": 3600.0, "state": s, "state_label": fleet.STATE_LABEL[s],
                          "site": f"Site {i % 7}", "environment": ["prod", "uat", "dr", "dev"][i % 4],
                          "environment_label": "Production", "client": "", "owner": "", "os": "Debian 12",
                          "ci_name": "", "ci_url": "", "name": f"host-{i:03d}:9100", "ci": None,
                          "url": f"/monitoring/infrastructure/host-{i:03d}:9100/",
                          "last_scrape": now, "interval": "15s"})
        with mock.patch("telemetry.fleet.host_states", return_value={"hosts": hosts, "errors": {}}), \
             mock.patch("telemetry.fleet.all_targets", return_value=([], "")), \
             mock.patch("telemetry.fleet.series_count", return_value=(1000, "")):
            started = time.monotonic()
            r = c.get(reverse("telemetry:fleet"), {"group": "site", "colour": "cpu", "per": 100})
            elapsed = time.monotonic() - started
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["grid_shown"], 500)
        self.assertEqual(len(r.context["grid"]), 7)
        self.assertLess(elapsed, 1.0, f"500-host fleet took {elapsed:.2f}s to render")


# ============================================================================= discovery and tones
@override_settings(PROMETHEUS_URL=PROM)
class FleetDiscoveryTests(FleetMixin, TestCase):
    def test_discovered_host_that_lost_its_target_is_stale_not_never_seen(self):
        from .models import DiscoveredTarget, TargetKind
        self.start_prom()
        DiscoveredTarget.objects.create(instance="gone-01:9100", job="node", kind=TargetKind.NODE,
                                        missing_since=timezone.now(), site="Pune",
                                        details={"os": "Ubuntu 22.04"})
        ci("Paper", "paper-01:9100")
        by = {h["instance"]: h for h in fleet.host_states()["hosts"]}
        self.assertEqual(by["gone-01:9100"]["state"], fleet.STALE)
        self.assertEqual(by["gone-01:9100"]["os"], "Ubuntu 22.04")
        self.assertEqual(by["gone-01:9100"]["site"], "Pune")
        self.assertEqual(by["paper-01:9100"]["state"], fleet.NEVER_SEEN)

    def test_ignored_discovered_hosts_are_left_out(self):
        from .models import DiscoveredTarget, TargetKind
        self.start_prom()
        DiscoveredTarget.objects.create(instance="skip-01:9100", job="node", kind=TargetKind.NODE,
                                        state=DiscoveredTarget.IGNORED, missing_since=timezone.now())
        self.assertNotIn("skip-01:9100", {h["instance"] for h in fleet.host_states()["hosts"]})

    def test_state_badge_tones_are_the_fleet_ones(self):
        from .templatetags.telemetry_tags import state_tone
        self.assertEqual([state_tone(s) for s in fleet.STATES], ["ok", "crit", "warn", "mute"])
