"""Review of the sidebar, dashboard and badges: active state on every route, unique menu keys, badges and sparkline
caches that cannot cross users or sites, the fleet panel under the pickers, and markup that stays inert when site,
client or host names are hostile."""
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify

from core import dashboard, overview
from core.context_processors import build_nav
from core.models import Client, Membership, Site
from core.nav import NAV
from servicedesk.models import Ticket
from telemetry.models import Alert
from urlchecker.models import UrlCheck

PW = "Corr3ct-horse-battery"
EVIL = '"><img src=x onerror=alert(1)>'
EVIL2 = "<script>alert(1)</script>"


def leaves():
    for item in NAV:
        for c in item.get("children") or [item]:
            yield item, c


class NavRoutesTests(SimpleTestCase):
    def test_every_link_lights_up_exactly_itself(self):
        """Opening any menu link marks that link (and no other) as the current page, whatever prefixes its URL."""
        for _g, c in leaves():
            href = reverse(c["url"])
            nav, sections = build_nav(href)
            on = [(e["label"], x["label"]) for e in nav for x in (e["children"] or [e]) if x["active"]]
            self.assertEqual(len(on), 1, (c["url"], on))
            self.assertEqual(on[0][1], c["label"], c["url"])
            self.assertEqual(sum(1 for s in sections if s["active"]), 1, c["url"])

    def test_sub_pages_of_every_link_keep_that_link_current(self):
        for _g, c in leaves():
            href = reverse(c["url"])
            if href == "/":
                continue
            nav, _s = build_nav(href + "123/edit/")
            on = [x["label"] for e in nav for x in (e["children"] or [e]) if x["active"]]
            self.assertEqual(on, [c["label"]], c["url"])

    def test_resolved_links_are_unique(self):
        hrefs = [reverse(c["url"]) for _g, c in leaves()]
        self.assertEqual(len(hrefs), len(set(hrefs)))

    def test_json_and_export_endpoints_of_overview_keep_it_current(self):
        for path in ("/monitoring/overview/data.json", "/monitoring/overview/?format=csv"):
            nav, _s = build_nav(path.split("?")[0])
            self.assertEqual([e["label"] for e in nav if e["active"]], ["Overview"], path)

    def test_the_dashboard_data_endpoint_is_not_the_dashboard_link(self):
        nav, _s = build_nav("/dashboard/data/")
        self.assertFalse(any(x["active"] for e in nav for x in [e, *e["children"]]))

    def test_storage_keys_are_unique(self):
        """Pins and remembered groups are stored by key: two entries sharing one would open and pin together."""
        keys = []
        for item in NAV:
            keys.append(item["url"] if item.get("url") else slugify(item["label"]))
            keys.extend(c["url"] for c in item.get("children", []))
        self.assertEqual(len(keys), len(set(keys)), sorted(k for k in keys if keys.count(k) > 1))
        ids = [item["section"] + slugify(item["label"]) for item in NAV]
        self.assertEqual(len(ids), len(set(ids)))


class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.mum = Site.objects.create(name="Mumbai", code="mum")
        self.dr = Site.objects.create(name="DR Site", code="dr")
        self.root = User.objects.create_superuser("root", "r@example.com", PW)
        self.one = User.objects.create_user("one", "o@example.com", PW)
        Membership.objects.create(user=self.one, site=self.mum, role="viewer")
        now = timezone.now()
        Alert.objects.create(fingerprint="a-m", alertname="MumAlert", severity="critical", site="Mumbai", starts_at=now)
        for i in range(3):
            Alert.objects.create(fingerprint=f"a-d{i}", alertname=f"DrAlert{i}", severity="critical", site="DR Site", starts_at=now)
        UrlCheck.objects.create(name="MumDown", url="https://m.example.com/", site="Mumbai", last_status="down")
        for i in range(4):
            UrlCheck.objects.create(name=f"DrDown{i}", url=f"https://d{i}.example.com/", site="DR Site", last_status="down")
        for i in range(5):
            Ticket.objects.create(kind="incident", title=f"DR incident {i}", site="DR Site", priority="P1")
        Ticket.objects.create(kind="incident", title="Mum incident", site="Mumbai", priority="P3")

    def badges(self, user, path="/monitoring/fleet/"):
        self.client.force_login(user)
        r = self.client.get(path)
        self.assertEqual(r.status_code, 200)
        return {e["label"]: e["badge"] for e in r.context["nav"] if e["badge"]}


class BadgeScopeTests(Base):
    def test_badges_count_only_the_users_sites(self):
        self.assertEqual(self.badges(self.one), {"Service Desk": 1, "Alert Manager": 1, "Checks": 1})

    def test_a_cached_admin_count_never_reaches_a_site_limited_user(self):
        self.assertEqual(self.badges(self.root), {"Service Desk": 6, "Alert Manager": 4, "Checks": 5})
        self.assertEqual(self.badges(self.one), {"Service Desk": 1, "Alert Manager": 1, "Checks": 1})
        self.assertEqual(self.badges(self.root), {"Service Desk": 6, "Alert Manager": 4, "Checks": 5})

    def test_the_dashboard_hands_over_the_same_numbers_the_sidebar_computes_itself(self):
        for user in (self.root, self.one):
            cache.clear()
            via_dash = self.badges(user, reverse("dashboard"))
            cache.clear()
            via_other = self.badges(user, "/monitoring/fleet/")
            self.assertEqual(via_dash, via_other, user.username)

    def test_a_picker_selection_does_not_change_the_sidebar_badges(self):
        """The badges are the user's totals; the site picker narrows the page, not the menu."""
        full = self.badges(self.root, reverse("dashboard"))
        cache.clear()
        picked = self.badges(self.root, reverse("dashboard") + "?site=Mumbai")
        self.assertEqual(full, picked)

    def test_a_site_the_user_cannot_see_adds_nothing(self):
        self.client.force_login(self.one)
        r = self.client.get(reverse("dashboard"), {"site": "DR Site"})
        self.assertEqual([k["value"] for k in r.context["kpis"] if k["key"] in ("incidents", "alerts")], ["0", "0"])
        self.assertEqual(r.context["problems"], [])
        self.assertNotContains(r, "DrAlert")
        self.assertNotContains(r, "DR incident")

    def test_the_site_limited_dashboard_never_names_another_site(self):
        self.client.force_login(self.one)
        for url in (reverse("dashboard"), reverse("dashboard_data") + "?range=24h"):
            body = self.client.get(url).content.decode()
            for needle in ("DR Site", "DrAlert", "DrDown", "DR incident"):
                if url.startswith("/dashboard/data/") or needle != "DR Site":
                    self.assertNotIn(needle, body, (url, needle))
        r = self.client.get(reverse("dashboard"))
        self.assertNotIn("DR Site", [row["site"] for row in r.context["matrix"]["rows"]])
        self.assertNotIn("DR Site", r.context["heat"]["empty"])


class SparkCacheTests(Base):
    def test_cache_keys_hold_no_site_names(self):
        keys = []

        class Spy:
            def get(self, k, d=None):
                keys.append(k)
                return None

            def set(self, k, v, t=None):
                keys.append(k)

        with mock.patch("core.dashboard.cache", Spy()):
            dashboard.spark_data(self.root, "1h", {"DR Site", "Mumbai\n", "x" * 400})
        self.assertTrue(keys)
        for k in keys:
            self.assertRegex(k, r"^[A-Za-z0-9:_-]{1,200}$")

    def test_two_pickers_do_not_share_a_cached_series(self):
        self.client.force_login(self.root)
        a = self.client.get(reverse("dashboard_data"), {"site": "Mumbai"}).json()["spark"]
        b = self.client.get(reverse("dashboard_data"), {"site": "DR Site"}).json()["spark"]
        self.assertNotEqual(a["alerts"][-1], b["alerts"][-1])
        self.assertEqual((a["alerts"][-1], b["alerts"][-1]), (1, 3))


class FleetPanelPickerTests(Base):
    HOSTS = [
        {"instance": "m1:9100", "name": "m1", "state": "live", "cpu": 80.0, "memory": 10.0, "site": "Mumbai", "url": "/x/m1/"},
        {"instance": "drhost-one:9100", "name": "drhost-one", "state": "down", "cpu": None, "memory": None, "site": "DR Site", "url": "/x/drhost-one/"},
        {"instance": "drhost-two:9100", "name": "drhost-two", "state": "live", "cpu": 99.0, "memory": 99.0, "site": "DR Site", "url": "/x/drhost-two/"},
    ]

    def warm(self):
        for k in dashboard.FLEET_CACHE_KEYS:
            cache.set(k, [] if "os" not in k else ({}, ""), 60)
        return mock.patch("telemetry.fleet.host_states", return_value={"hosts": list(self.HOSTS), "errors": {}})

    def panel(self, user, **params):
        self.client.force_login(user)
        with self.warm(), mock.patch("telemetry.fleet.scope_hosts", side_effect=lambda u, rows: rows):
            r = self.client.get(reverse("dashboard"), params)
        return r.context["fleet"], r

    def test_the_site_picker_narrows_the_fleet_panel_and_the_hosts_card(self):
        f, r = self.panel(self.root, site="Mumbai")
        self.assertEqual((f["total"], f["live"]), (1, 1))
        self.assertEqual([h["name"] for h in f["top_cpu"]], ["m1"])
        card = next(k for k in r.context["kpis"] if k["key"] == "hosts")
        self.assertEqual(card["value"], "1/1")

    def test_without_a_picker_every_visible_host_counts(self):
        f, _r = self.panel(self.root)
        self.assertEqual((f["total"], f["live"]), (3, 2))

    def test_an_unreachable_site_shows_no_hosts(self):
        f, _r = self.panel(self.one, site="DR Site")
        self.assertEqual(f["total"], 0)
        self.assertEqual(f["top_cpu"], [])

    def test_a_site_limited_user_gets_only_their_hosts_through_the_real_scope(self):
        from telemetry import tests_fleet as tf
        tf.ci("m1", "m1:9100", site="Mumbai")
        tf.ci("drhost-one", "drhost-one:9100", site="DR Site")
        tf.ci("drhost-two", "drhost-two:9100", site="DR Site")
        self.client.force_login(self.one)
        with self.warm():
            r = self.client.get(reverse("dashboard"))
            j = self.client.get(reverse("dashboard_data")).json()
        for f in (r.context["fleet"], j["fleet"]):
            self.assertEqual(f["total"], 1)
            self.assertEqual([h["name"] for h in f["top_cpu"]], ["m1"])
        self.assertNotContains(r, "drhost-")   # a unique marker: a short name like "d2" can occur inside a CSRF token


class HostileNamesTests(TestCase):
    """Site, client and host names come from imports and scraped labels: every page must print them escaped."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.root = User.objects.create_superuser("root", "r@example.com", PW)
        for name in (EVIL, EVIL2):
            Site.objects.create(name=name, code=slugify(name)[:20] or "x")
            UrlCheck.objects.create(name=name, url="https://evil.example.com/", site=name, last_status="down")
        Alert.objects.create(fingerprint="e", alertname=EVIL2, severity="critical", site=EVIL, starts_at=timezone.now(),
                             summary=EVIL, instance=EVIL2)
        self.client.force_login(self.root)

    def assertInert(self, body, where):
        for raw in ("<img src=x", "<script>alert(1)", "onerror=alert(1)>"):
            self.assertNotIn(raw, body, (where, raw))

    def test_dashboard_in_every_state(self):
        for params in ({}, {"site": EVIL}, {"site": EVIL2, "range": "6h"}, {EVIL: EVIL, "range": EVIL2}):
            r = self.client.get(reverse("dashboard"), params)
            self.assertEqual(r.status_code, 200)
            self.assertInert(r.content.decode(), params)

    def test_every_menu_page_prints_the_pickers_escaped(self):
        for path in ("/monitoring/fleet/", "/monitoring/overview/", "/monitoring/alerts/", reverse("urlchecker:index")):
            r = self.client.get(path, {"site": EVIL, "client": EVIL2})
            self.assertEqual(r.status_code, 200, path)
            self.assertInert(r.content.decode(), path)

    def test_the_json_carries_names_as_data_only(self):
        r = self.client.get(reverse("dashboard_data"))
        self.assertEqual(r["Content-Type"], "application/json")
        self.assertIn(EVIL2, [p["name"] for p in r.json()["problems"]] + [p["detail"] for p in r.json()["problems"]]
                      + [p["site"] for p in r.json()["problems"]])

    def test_a_client_named_like_markup(self):
        c = Client.objects.create(name=EVIL)
        r = self.client.get(reverse("dashboard"), {"client": str(c.pk)})
        self.assertEqual(r.status_code, 200)
        self.assertInert(r.content.decode(), "client picker")
        self.assertContains(r, "&lt;img src=x onerror=alert(1)&gt;")


class RangeLinkTests(TestCase):
    def test_extra_query_keys_survive_encoded(self):
        cache.clear()
        root = User.objects.create_superuser("root", "r@example.com", PW)
        self.client.force_login(root)
        r = self.client.get(reverse("dashboard"), {"site": "A&b=c d", "x y": "1"})
        body = r.content.decode()
        self.assertIn("site=A%26b%3Dc%20d&amp;", body)
        self.assertIn("x%20y=1&amp;", body)
        self.assertIn("range=15m", body)
