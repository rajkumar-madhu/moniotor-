"""Prometheus discovery: the security / RBAC / tenancy review's regression tests.

One test per fix made while attacking telemetry.discovery and the Discovery page:
  * a crafted `target` id must not 500 (non-ASCII digits, ids wider than the column);
  * "Discover now" reads every site, so it needs the admin role tenant-wide, not on one site;
  * linking writes back to the chosen record, so a record that belongs to every site needs the role everywhere;
  * scraped label values are bounded before they reach a JSON column (label count, string length, boot time);
  * an auto-created item or device goes through the model's own validators.

Every Prometheus call is mocked – nothing touches the network.
"""
from unittest import mock

from django.apps import apps
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from core.models import Membership, Site

from . import discovery as svc
from .models import DiscoveredTarget, DiscoveryRun
from .tests_discovery import PROM, PromMixin, _rows, _user, target


@override_settings(PROMETHEUS_URL=PROM)
class CraftedIdTests(PromMixin, TestCase):
    """A posted id is user input: it must never reach the database as something int() or the column refuses."""

    def setUp(self):
        super().setUp()
        svc.run()
        self.client = Client()
        self.client.force_login(_user("crafted-admin", superuser=True))
        self.url = reverse("telemetry:discovery_action")

    def test_a_non_ascii_digit_is_refused_not_a_500(self):
        # "²".isdigit() is True but int("²") raises, so an isdigit() guard alone is a 500
        r = self.client.post(self.url, {"action": "ignore", "target": ["²"]})
        self.assertEqual(r.status_code, 302)
        self.assertFalse(DiscoveredTarget.objects.for_tenant().filter(state=DiscoveredTarget.IGNORED).exists())

    def test_an_id_too_wide_for_the_column_is_refused(self):
        # a 40-digit id is out of range for an integer primary key: a DataError on PostgreSQL
        r = self.client.post(self.url, {"action": "ignore", "target": ["9" * 40]})
        self.assertEqual(r.status_code, 302)

    def test_arabic_indic_digits_select_nothing(self):
        row = DiscoveredTarget.objects.for_tenant().first()
        r = self.client.post(self.url, {"action": "ignore", "target": ["١٢٣", str(row.pk)]})
        self.assertEqual(r.status_code, 302)
        row.refresh_from_db()
        self.assertEqual(row.state, DiscoveredTarget.IGNORED)   # the real id still works

    def test_pk_parsing(self):
        from .views.discovery import _pk
        for bad in ("", "0", "-1", "1.5", "²", "١٢", "9" * 40, "abc", None):
            self.assertEqual(_pk(bad), 0, bad)
        self.assertEqual(_pk("42"), 42)


@override_settings(PROMETHEUS_URL=PROM)
class DiscoverNowRbacTests(PromMixin, TestCase):
    """"Discover now" drives a tenant-wide pass that may create records on every site."""

    def setUp(self):
        super().setUp()
        Site.objects.get_or_create(name="Mumbai", defaults={"code": "mumbai"})
        self.site_admin = _user("mumbai-admin", role="admin", site="Mumbai")
        self.global_admin = _user("all-sites-admin")
        Membership.objects.create(user=self.global_admin, site=None, role="admin")
        self.url = reverse("telemetry:discovery_run")

    def _as(self, user):
        c = Client()
        c.force_login(user)
        return c

    def test_an_admin_for_one_site_cannot_discover(self):
        r = self._as(self.site_admin).post(self.url)
        self.assertEqual(r.status_code, 302)
        self.assertFalse(DiscoveryRun.objects.exists())
        self.assertFalse(DiscoveredTarget.objects.exists())

    def test_an_admin_for_one_site_is_not_offered_the_button(self):
        r = self._as(self.site_admin).get(reverse("telemetry:discovery"))
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.context["can_discover"])
        self.assertNotContains(r, f'action="{self.url}"')    # no form that would post a pass

    def test_an_admin_on_all_sites_can_discover(self):
        r = self._as(self.global_admin).post(self.url)
        self.assertEqual(r.status_code, 302)
        self.assertTrue(DiscoveryRun.objects.filter(ok=True).exists())
        self.assertTrue(DiscoveredTarget.objects.exists())


@override_settings(PROMETHEUS_URL=PROM)
class LinkPermissionTests(PromMixin, TestCase):
    """Linking writes the instance into the chosen record, so the user must be able to change that record."""

    def setUp(self):
        super().setUp()
        svc.run()
        self.CI = apps.get_model("cmdb", "ConfigurationItem")
        Site.objects.get_or_create(name="Mumbai", defaults={"code": "mumbai"})
        self.op = _user("link-op", role="operator", site="Mumbai")
        self.db01 = DiscoveredTarget.objects.for_tenant().get(instance="db-01:9100")    # Mumbai
        self.url = reverse("telemetry:discovery_action")

    def _as(self, user):
        c = Client()
        c.force_login(user)
        return c

    def test_a_site_operator_cannot_link_to_an_all_sites_item(self):
        ci = self.CI.objects.create(name="shared-ci", site="All Sites")
        r = self._as(self.op).post(self.url, {"action": "link", "target": [self.db01.pk], "ci": ci.pk})
        self.assertEqual(r.status_code, 302)
        self.db01.refresh_from_db()
        ci.refresh_from_db()
        self.assertIsNone(self.db01.ci_id)
        self.assertEqual(ci.prometheus_instance, "")        # the record was not written to either

    def test_an_all_sites_item_is_not_offered_to_a_site_operator(self):
        self.CI.objects.create(name="shared-ci", site="All Sites")
        mine = self.CI.objects.create(name="mumbai-ci", site="Mumbai")
        r = self._as(self.op).get(self.db01.get_absolute_url())
        self.assertEqual([c.pk for c in r.context["cis"]], [mine.pk])

    def test_an_admin_on_all_sites_may_link_to_an_all_sites_item(self):
        ci = self.CI.objects.create(name="shared-ci", site="All Sites")
        admin = _user("link-admin", superuser=True)
        self._as(admin).post(self.url, {"action": "link", "target": [self.db01.pk], "ci": ci.pk})
        self.db01.refresh_from_db()
        self.assertEqual(self.db01.ci_id, ci.pk)


# ----------------------------------------------------------------------------- bounded scraped data
WIDE = "w" * 4000

LABEL_FLOOD = [{
    "labels": {"instance": "flood:9100", "job": "node", **{f"extra_{n:03d}": WIDE for n in range(400)}},
    "discoveredLabels": {"__address__": "flood:9100", **{f"d_{n:03d}": WIDE for n in range(400)}},
    "health": "up", "scrapePool": "node", "lastError": "", "scrapeUrl": "http://flood:9100/metrics",
    "lastScrape": "2026-11-07T08:00:00Z",
}]


def wide_node_query(q):
    """A node_exporter answering with absurd label values and a nonsense boot time."""
    inst = {"instance": "flood:9100", "job": "node"}
    if q == "node_uname_info":
        return _rows([({**inst, "nodename": WIDE, "sysname": "Linux", "release": WIDE, "machine": WIDE}, 1.0)])
    if q == "node_os_info":
        return _rows([({**inst, "pretty_name": WIDE, "id": "debian"}, 1.0)])
    if q == "node_exporter_build_info":
        return _rows([({**inst, "version": WIDE}, 1.0)])
    if q == "node_network_info":
        return _rows([({**inst, "device": WIDE, "address": WIDE, "operstate": WIDE}, 1.0)
                      for _ in range(200)])
    if q == "node_boot_time_seconds":
        return _rows([({**inst}, 1.0e18)])      # datetime.fromtimestamp() raises on this
    if q.startswith("node_filesystem_size_bytes"):
        return _rows([({**inst, "mountpoint": WIDE, "device": WIDE, "fstype": WIDE}, 5.0e10)])
    return []


@override_settings(PROMETHEUS_URL=PROM)
class BoundedDetailsTests(PromMixin, TestCase):
    """A Prometheus label value has no length limit, and `details` is a JSON column in our database."""
    targets_data = LABEL_FLOOD
    query_fns = (wide_node_query,)

    def _row(self):
        svc.run()
        return DiscoveredTarget.objects.for_tenant().get(instance="flood:9100")

    def test_the_label_set_is_capped(self):
        row = self._row()
        self.assertLessEqual(len(row.labels), svc.MAX_LABELS)
        self.assertLessEqual(len(row.discovered_labels), svc.MAX_LABELS)
        self.assertTrue(all(len(v) <= svc.MAX_TEXT for v in row.labels.values()))
        self.assertTrue(all(len(v) <= svc.MAX_TEXT for v in row.discovered_labels.values()))

    def test_detail_strings_are_capped(self):
        d = self._row().details
        for key in ("hostname", "os", "kernel", "arch", "exporter_version"):
            self.assertLessEqual(len(d.get(key, "")), 2 * svc.MAX_TEXT + 1, key)
        self.assertLessEqual(len(d["interfaces"]), svc.MAX_DETAIL_ROWS)
        for iface in d["interfaces"]:
            self.assertLessEqual(len(iface["name"]), svc.MAX_TEXT)
            self.assertLessEqual(len(iface["mac"]), svc.MAX_TEXT)
        for fs in d["filesystems"]:
            self.assertLessEqual(len(fs["mountpoint"]), svc.MAX_TEXT)
            self.assertLessEqual(len(fs["fstype"]), svc.MAX_TEXT)

    def test_a_nonsense_boot_time_is_not_stored(self):
        self.assertNotIn("boot_time", self._row().details)

    def test_the_detail_page_still_renders(self):
        row = self._row()
        c = Client()
        c.force_login(_user("bounded-admin", superuser=True))
        self.assertEqual(c.get(row.get_absolute_url()).status_code, 200)

    def test_an_auto_created_item_carries_bounded_attributes(self):
        CI = apps.get_model("cmdb", "ConfigurationItem")
        with mock.patch("telemetry.discovery._flags",
                        return_value={"ci": True, "device": False, "exporter": True}):
            svc.run()
        ci = CI.objects.get()
        self.assertLessEqual(len(ci.name), 150)
        self.assertTrue(all(not isinstance(v, str) or len(v) <= 2 * svc.MAX_TEXT + 1
                            for v in ci.attributes.values()))
        exporter = apps.get_model("observe", "Exporter").objects.get()
        self.assertLessEqual(len(exporter.name), 120)   # the column is 120, not the 150 of a CI or a device


# ----------------------------------------------------------------------------- model validation
BAD_SNMP = [target("not a host:161", "snmp", site="Mumbai")]


def bad_snmp_query(q):
    if q == "sysName":
        return _rows([({"instance": "not a host:161", "sysName": "weird"}, 1.0)])
    return []


@override_settings(PROMETHEUS_URL=PROM)
class AutoCreateValidationTests(PromMixin, TestCase):
    """Model.save() skips field validators, so auto-create must run them itself."""
    targets_data = BAD_SNMP
    query_fns = (bad_snmp_query,)

    def test_a_device_with_an_address_the_validators_reject_is_not_created(self):
        Device = apps.get_model("network", "Device")
        with mock.patch("telemetry.discovery._flags",
                        return_value={"ci": False, "device": True, "exporter": False}):
            run = svc.run()
        self.assertTrue(run.ok)                         # and the pass still finishes
        self.assertEqual(Device.objects.count(), 0)     # network.models.validate_host refuses "not a host"
        self.assertEqual(DiscoveredTarget.objects.for_tenant().count(), 1)

    def test_a_usable_address_is_still_created(self):
        Device = apps.get_model("network", "Device")
        self.targets_data = [target("10.0.0.1", "snmp", site="Mumbai")]
        self.query_fns = (lambda q: _rows([({"instance": "10.0.0.1", "sysName": "core-sw"}, 1.0)])
                          if q == "sysName" else [],)
        with mock.patch("telemetry.discovery._flags",
                        return_value={"ci": False, "device": True, "exporter": False}):
            svc.run()
        self.assertEqual(Device.objects.get().mgmt_ip, "10.0.0.1")


# ----------------------------------------------------------------------------- linked records on another site
@override_settings(PROMETHEUS_URL=PROM)
class ForeignLinkVisibilityTests(PromMixin, TestCase):
    """Linking by name is not site-scoped, so a viewer must not be shown a record that sits on a site they cannot see."""

    def setUp(self):
        super().setUp()
        self.CI = apps.get_model("cmdb", "ConfigurationItem")
        Site.objects.get_or_create(name="Chennai", defaults={"code": "chennai"})
        # db-01 is a Mumbai target, but its item lives on Chennai (the names match, the sites do not)
        self.ci = self.CI.objects.create(name="chn-legacy-box", site="Chennai", prometheus_instance="db-01:9100")
        svc.run()
        self.viewer = _user("mumbai-viewer", role="viewer", site="Mumbai")
        self.row = DiscoveredTarget.objects.for_tenant().get(instance="db-01:9100")

    def _get(self, user, url):
        c = Client()
        c.force_login(user)
        return c.get(url)

    def test_the_row_is_linked_in_the_database(self):
        self.assertEqual(self.row.ci_id, self.ci.pk)

    def test_the_list_does_not_name_it(self):
        r = self._get(self.viewer, reverse("telemetry:discovery"))
        self.assertContains(r, "db-01:9100")
        self.assertNotContains(r, self.ci.get_absolute_url())
        self.assertNotContains(r, "chn-legacy-box")

    def test_the_csv_and_search_do_not_name_it(self):
        url = reverse("telemetry:discovery")
        self.assertNotContains(self._get(self.viewer, url + "?format=csv"), "chn-legacy-box")
        self.assertNotContains(self._get(self.viewer, url), "chn-legacy-box")
        # searching for the hidden name must not reveal that the record exists
        self.assertNotContains(self._get(self.viewer, url + "?q=chn-legacy"), "db-01:9100")

    def test_the_detail_page_does_not_name_it(self):
        r = self._get(self.viewer, self.row.get_absolute_url())
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, self.ci.get_absolute_url())
        self.assertNotContains(r, "chn-legacy-box")

    def test_nothing_is_written_back(self):
        self._get(self.viewer, self.row.get_absolute_url())
        self.row.refresh_from_db()
        self.assertEqual(self.row.ci_id, self.ci.pk)

    def test_a_user_who_sees_every_site_still_sees_it(self):
        r = self._get(_user("all-sites-viewer", superuser=True), self.row.get_absolute_url())
        self.assertContains(r, self.ci.get_absolute_url())


# ----------------------------------------------------------------------------- scrape URL credentials
class RedactUrlTests(TestCase):
    def test_userinfo_is_removed(self):
        self.assertEqual(svc.redact_url("http://bob:s3cret@db-01:9100/metrics?x=1"), "http://db-01:9100/metrics?x=1")
        self.assertEqual(svc.redact_url("https://tok@host/m"), "https://host/m")

    def test_urls_without_credentials_are_untouched(self):
        for u in ("http://db-01:9100/metrics", "", "http://h/p?mail=a@b.c", "http://h/p#a@b"):
            self.assertEqual(svc.redact_url(u), u)

    def test_a_malformed_url_is_dropped_not_raised(self):
        self.assertEqual(svc.redact_url("http://u:p@[::1/metrics"), "")


@override_settings(PROMETHEUS_URL=PROM)
class StoredScrapeUrlTests(PromMixin, TestCase):
    targets_data = [{**LABEL_FLOOD[0], "labels": {"instance": "cred:9100", "job": "node"},
                     "discoveredLabels": {"__address__": "cred:9100"},
                     "scrapeUrl": "http://bob:s3cret@cred:9100/metrics"}]

    def test_the_stored_url_has_no_password(self):
        svc.run()
        row = DiscoveredTarget.objects.for_tenant().get(instance="cred:9100")
        self.assertEqual(row.scrape_url, "http://cred:9100/metrics")


# ----------------------------------------------------------------------------- alert rules are not site-scoped
@override_settings(PROMETHEUS_URL=PROM)
class RulesVisibilityTests(PromMixin, TestCase):
    def setUp(self):
        super().setUp()
        from django.core.cache import cache
        cache.clear()
        self.rules = [{"name": "chennai.rules", "file": "/r.yml", "interval": 30,
                       "rules": [{"name": "ChennaiDbDown", "type": "alerting", "query": 'up{instance="chennai-db"} == 0',
                                  "state": "inactive", "health": "ok", "labels": {}, "annotations": {}, "alerts": []}]}]
        p = mock.patch("telemetry.promapi.rules", return_value=self.rules)
        p.start()
        self.addCleanup(p.stop)

    def _page(self, user):
        c = Client()
        c.force_login(user)
        return c.get(reverse("telemetry:prometheus"))

    def test_a_site_limited_user_does_not_see_the_rules(self):
        r = self._page(_user("rules-mumbai", role="viewer", site="Mumbai"))
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, "ChennaiDbDown")
        self.assertNotContains(r, "chennai-db")
        self.assertContains(r, "shown only to users who can see every site")

    def test_a_user_who_sees_every_site_does(self):
        r = self._page(_user("rules-all", superuser=True))
        self.assertContains(r, "ChennaiDbDown")
