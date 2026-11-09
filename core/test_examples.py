"""No sample data on live pages: gated /legacy/ mock-ups, honest empty states, and the example-data lifecycle
(core/examples.py, manage.py remove_examples, the Administration "Demo data" card, the admin banner, the go-live warning)."""
import io
import re
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, TestCase, override_settings
from django.urls import NoReverseMatch, reverse

from administration import golive
from automation.models import Runbook
from cmdb.models import ConfigurationItem
from core import examples
from core.models import AuditEvent, Event, Membership, Site
from core.nav import NAV
from domainmonitor.models import Domain
from hypervisor.models import VCenterSource
from ipam.models import Subnet
from network.models import Device
from servicedesk.models import Ticket
from tcpcheck.models import TcpCheck
from urlchecker.models import UrlCheck

PW = "Strong-pass-12345"
SEEDS = ("seed_administration", "seed_examples", "seed_domainmonitor", "seed_servicedesk", "seed_cmdb", "seed_network", "seed_ports",
         "seed_compute", "seed_hypervisor", "seed_hardware", "seed_architecture", "seed_dcim", "seed_ipam", "seed_apm",
         "seed_automation", "seed_bodeod", "seed_telemetry", "seed_observe", "seed_tcpcheck")
LEGACY = ("/legacy/heatmap/", "/legacy/exporters/", "/legacy/incidents/", "/legacy/fg-01/")

# words that would show invented or demo content on a live page (a column called "Samples" or "for example" in help text is fine)
FORBIDDEN = re.compile(r"seed_\w+|\bexample\b|\bexamples\b|\bdemo\b|sample data|illustrative|synthetic|lorem|placeholder data", re.I)


def visible_text(html):
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\b(for|e\.g\.|such as)\s+example\b", " ", text, flags=re.I)
    text = re.sub(r"[\w.-]*example\.(com|net|org|test|internal)\b[\w./:-]*", " ", text, flags=re.I)
    return text


def seed_everything():
    for name in SEEDS:
        call_command(name, stdout=io.StringIO(), stderr=io.StringIO())


def make_real_lookalikes():
    """Real records that carry the NAME of an example row. Made before seeding, the seed command leaves them alone."""
    from automation.management.commands import seed_automation as sa
    from tcpcheck.management.commands import seed_tcpcheck as st
    TcpCheck.objects.create(name=st.EXAMPLES[0]["name"], host="db.acme.test", port=5432, tags="prod")
    Runbook.objects.create(title=sa.RUNBOOKS[0]["title"], service="x", steps="1. a", created_by="alice")


def make_real_rows():
    """Real records. None may ever be removed."""
    UrlCheck.objects.create(name="Real shop", url="https://shop.acme.test/", tags="prod")
    Domain.objects.create(tenant_key="default", site_key="Mumbai", hostname="acme.test", description="Our production domain")
    Device.objects.create(name="real-sw-01", device_type="switch", mgmt_ip="10.1.1.1", site="Mumbai", tags="prod")
    ConfigurationItem.objects.create(name="Real billing", ci_type="application")
    VCenterSource.objects.create(tenant_key="default", name="Real vCenter", site="Mumbai", host="vc.acme.test")
    Subnet.objects.create(tenant_key="default", cidr="10.99.0.0/24", description="Real lab subnet")
    real_site = Site.objects.create(name="Real Plant", code="plant")
    owner = User.objects.create_user("realowner", password=PW)
    Membership.objects.create(user=owner, site=real_site, role="viewer")
    Ticket.objects.create(title="Real outage", site="Mumbai")


def real_snapshot():
    return {
        "urls": set(UrlCheck.objects.filter(name="Real shop").values_list("pk", flat=True)),
        "domains": set(Domain.objects.filter(hostname="acme.test").values_list("pk", flat=True)),
        "tcp": TcpCheck.objects.filter(host="db.acme.test").count(),
        "device": Device.objects.filter(name="real-sw-01").count(),
        "ci": ConfigurationItem.objects.filter(name="Real billing").count(),
        "runbook": Runbook.objects.filter(created_by="alice").count(),
        "vcenter": VCenterSource.objects.filter(host="vc.acme.test").count(),
        "subnet": Subnet.objects.filter(cidr="10.99.0.0/24").count(),
        "site": Site.objects.filter(code="plant").count(),
        "membership": Membership.objects.filter(user__username="realowner").count(),
        "ticket": Ticket.objects.filter(title="Real outage").count(),
        "realowner": User.objects.filter(username="realowner").count(),
    }


class LegacySamplePagesTests(TestCase):
    """/legacy/* fabricated mock-ups: gone in production, reachable only with LINKEDEYE_SHOW_SAMPLE_PAGES=true."""

    def setUp(self):
        self.admin = User.objects.create_superuser("root", "r@acme.test", PW)
        self.client.force_login(self.admin)

    def test_default_setting_is_off(self):
        from django.conf import settings
        self.assertFalse(settings.SHOW_SAMPLE_PAGES)

    def test_404_by_default_for_every_legacy_route(self):
        for url in LEGACY:
            self.assertEqual(self.client.get(url).status_code, 404, url)

    def test_reachable_only_with_the_flag(self):
        with override_settings(SHOW_SAMPLE_PAGES=True):
            for url in LEGACY:
                r = self.client.get(url)
                self.assertEqual(r.status_code, 200, url)
                self.assertContains(r, "Sample data")       # and they say so

    def test_url_names_other_code_references_still_reverse(self):
        for name in ("heatmap", "exporters", "incidents", "firewall"):
            self.assertTrue(reverse(name).startswith("/legacy/"))

    def test_anonymous_never_reaches_them(self):
        anon = Client()
        for url in LEGACY:
            self.assertIn(anon.get(url).status_code, (302, 401, 403, 404))
        with override_settings(SHOW_SAMPLE_PAGES=True):
            for url in LEGACY:
                self.assertIn(anon.get(url).status_code, (302, 401, 403))

    def test_coming_soon_page_lists_real_pages_and_hides_sample_links(self):
        r = self.client.get(reverse("coming_soon", args=["switches"]))
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, "/legacy/")
        self.assertNotContains(r, "fabricated")
        self.assertContains(r, reverse("cmdb:heatmap"))
        with override_settings(SHOW_SAMPLE_PAGES=True):
            r = self.client.get(reverse("coming_soon", args=["switches"]))
            self.assertContains(r, "/legacy/heatmap/")

    def test_nothing_links_to_the_legacy_pages_in_a_default_page(self):
        for url in ("/", "/search/?q=a", reverse("coming_soon", args=["help"])):
            self.assertNotContains(self.client.get(url), "/legacy/", msg_prefix=url)

    def test_architecture_example_loader_button_is_off_by_default(self):
        r = self.client.get(reverse("architecture:overview"))
        self.assertNotContains(r, "Load example fabric")
        Site.objects.create(name="Mumbai", code="mum")
        with override_settings(SHOW_SAMPLE_PAGES=True):
            self.assertContains(self.client.get(reverse("architecture:overview")), "Load example fabric")

    def test_landing_page_shows_no_invented_numbers(self):
        r = Client().get(reverse("b2b_landing"))
        self.assertEqual(r.status_code, 200)
        for fake in ("3,987", "99.97", "Monitored devices"):
            self.assertNotContains(r, fake)


class FreshDatabaseEmptyStateTests(TestCase):
    """A database with no records: every sidebar page answers 200 and shows nothing invented."""

    def setUp(self):
        self.admin = User.objects.create_superuser("root", "r@acme.test", PW)
        self.operator = User.objects.create_user("op", password=PW, is_staff=True)

    def urls(self):
        out = []
        for item in NAV:
            for e in [item, *item.get("children", [])]:
                if e.get("url"):
                    try:
                        out.append((e["label"], reverse(e["url"])))
                    except NoReverseMatch:
                        pass
        return out

    def check(self, user):
        self.client.force_login(user)
        urls = self.urls()
        self.assertGreater(len(urls), 60)
        for label, url in urls:
            r = self.client.get(url, follow=True)
            if user is self.operator and r.status_code == 403:      # admin-only pages (permission, not content)
                continue
            self.assertEqual(r.status_code, 200, f"{label} {url}")
            hit = FORBIDDEN.search(visible_text(r.content.decode()))
            self.assertIsNone(hit, f"{label} {url}: {hit.group(0) if hit else ''} in {visible_text(r.content.decode())[max(0, hit.start() - 60):hit.end() + 60] if hit else ''}")

    def test_admin_sees_honest_empty_pages(self):
        self.check(self.admin)

    def test_operator_sees_honest_empty_pages(self):
        self.check(self.operator)

    def test_real_records_without_any_metrics_say_no_data_and_show_nothing_invented(self):
        """Fleet, APM, hardware, hypervisor, ports and device pages read real data only: no Prometheus, BMC or vCenter, no numbers."""
        from apm import services as apm_services
        from apm.models import Source
        src = Source(name="Prod adapters", site="All Sites")
        apm_services.issue_token(src)
        src.save()
        VCenterSource.objects.create(tenant_key="default", name="vc-mum", site="Mumbai", host="vc.acme.test")
        devices = [Device.objects.create(name=n, device_type=t, mgmt_ip=ip, site="Mumbai", prometheus_instance=ip + ":9100")
                   for n, t, ip in (("mum-fw-01", "firewall", "10.20.0.1"), ("mum-sw-01", "switch", "10.20.0.2"),
                                    ("mum-srv-01", "physical_server", "10.20.0.3"), ("mum-vm-01", "virtual_machine", "10.20.0.4"))]
        self.client.force_login(self.admin)
        urls = ["/", "/monitoring/heatmap/", "/monitoring/fleet/", "/apm/", "/apm/sources/", "/hardware/endpoints/",
                "/hypervisor/sources/", "/hypervisor/datastores/", "/compute/", "/network/devices/"]
        for d in devices:
            urls += [d.get_absolute_url()] + [d.get_absolute_url() + "?tab=" + t for t in ("ports", "hardware", "vm", "metrics")]
        for url in urls:
            r = self.client.get(url, follow=True)
            self.assertEqual(r.status_code, 200, url)
            text = visible_text(r.content.decode())
            self.assertIsNone(FORBIDDEN.search(text), url)
        page = visible_text(self.client.get(devices[0].get_absolute_url()).content.decode())
        self.assertRegex(page, r"(?i)no metrics source")
        self.assertRegex(page, r"(?i)no data")

    def test_dashboard_search_and_welcome(self):
        self.client.force_login(self.admin)
        for url in ("/", "/search/?q=zz", "/welcome/"):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 200, url)
            self.assertIsNone(FORBIDDEN.search(visible_text(r.content.decode())), url)

    def test_health_page_says_clean(self):
        self.client.force_login(self.admin)
        r = self.client.get("/administration/health/")
        self.assertContains(r, "No example data.")
        self.assertNotContains(r, "Remove all example data")

    def test_fresh_database_reports_no_example_data(self):
        self.assertFalse(examples.exists())
        self.assertEqual(examples.total(), 0)
        out = io.StringIO()
        call_command("remove_examples", "--yes", stdout=out)
        self.assertIn("No example data found", out.getvalue())
        self.assertFalse(AuditEvent.objects.filter(object_type="core.exampledata").exists())


class ExampleLifecycleTests(TestCase):
    """Seed every app's examples next to real look-alike rows; removal takes the examples and only those."""

    @classmethod
    def setUpTestData(cls):
        make_real_lookalikes()
        seed_everything()
        make_real_rows()
        cls.real = real_snapshot()

    def setUp(self):
        self.admin = User.objects.create_superuser("root", "r@acme.test", PW)

    def test_every_area_has_examples_after_seeding(self):
        report = {r["key"]: r["total"] for r in examples.summary()}
        self.assertEqual(sorted(report), sorted(examples.known_keys()))
        for key, n in report.items():
            self.assertGreater(n, 0, f"{key} has nothing to remove: seed or counter drifted")
        self.assertTrue(examples.exists())

    def test_dry_run_is_the_default_and_changes_nothing(self):
        before = examples.total()
        out = io.StringIO()
        call_command("remove_examples", stdout=out)
        text = out.getvalue()
        self.assertIn("Dry run", text)
        self.assertIn("servicedesk", text)
        self.assertEqual(examples.total(), before)
        call_command("remove_examples", "--dry-run", "--yes", stdout=io.StringIO())      # --dry-run wins over --yes
        self.assertEqual(examples.total(), before)
        self.assertFalse(AuditEvent.objects.filter(object_type="core.exampledata").exists())

    def test_yes_removes_every_example_and_no_real_row(self):
        call_command("remove_examples", "--yes", stdout=io.StringIO())
        self.assertEqual(examples.total(), 0, [r for r in examples.summary() if r["total"]])
        self.assertFalse(examples.exists())
        self.assertEqual(real_snapshot(), self.real)
        # the look-alikes survive with their real attributes
        self.assertEqual(TcpCheck.objects.get(host="db.acme.test").tags, "prod")
        self.assertEqual(VCenterSource.objects.get(host="vc.acme.test").name, "Real vCenter")
        self.assertFalse(UrlCheck.objects.filter(tags__startswith="example,").exists())
        self.assertFalse(Device.objects.filter(tags__icontains="example").exists())
        self.assertFalse(Ticket.objects.filter(fingerprint__startswith="example:").exists())
        # a second run finds nothing
        out = io.StringIO()
        call_command("remove_examples", "--yes", stdout=out)
        self.assertIn("No example data found", out.getvalue())

    def test_audited(self):
        call_command("remove_examples", "--yes", stdout=io.StringIO())
        ev = AuditEvent.objects.get(object_type="core.exampledata")
        self.assertEqual(ev.action, "delete")
        self.assertEqual(ev.result, "ok")
        self.assertGreater(ev.details["removed"], 0)
        self.assertIn("servicedesk", ev.details["areas"])

    def test_only_limits_to_named_areas(self):
        cmdb_before = {r["key"]: r["total"] for r in examples.summary()}
        call_command("remove_examples", "--yes", "--only", "cmdb,tcpcheck", stdout=io.StringIO())
        after = {r["key"]: r["total"] for r in examples.summary()}
        self.assertEqual(after["cmdb"], 0)
        self.assertEqual(after["tcpcheck"], 0)
        for key in set(after) - {"cmdb", "tcpcheck"}:
            self.assertEqual(after[key], cmdb_before[key], key)
        with self.assertRaises(CommandError):
            call_command("remove_examples", "--only", "nonsense", stdout=io.StringIO())

    def test_each_area_is_removed_independently_of_the_others(self):
        """Removing one area at a time takes exactly that area's examples and leaves every other area's untouched."""
        left = {r["key"]: r["total"] for r in examples.summary()}
        for key in examples.known_keys():
            call_command("remove_examples", "--yes", "--only", key, stdout=io.StringIO())
            left[key] = 0
            now = {r["key"]: r["total"] for r in examples.summary()}
            self.assertEqual(now, left, f"after removing {key}")
        self.assertEqual(real_snapshot(), self.real)

    def test_an_area_that_fails_keeps_its_rows_and_the_others_are_removed(self):
        area = next(a for a in examples.AREAS if a.key == "dcim")
        with mock.patch.object(area, "commands", (("no_such_command",),)):
            out = io.StringIO()
            with self.assertRaises(CommandError):
                call_command("remove_examples", "--yes", stdout=out)
        after = {r["key"]: r["total"] for r in examples.summary()}
        self.assertGreater(after["dcim"], 0)
        self.assertEqual(sum(v for k, v in after.items() if k != "dcim"), 0)
        ev = AuditEvent.objects.get(object_type="core.exampledata")
        self.assertEqual(ev.result, "failed")
        self.assertEqual(ev.details["failed"], ["dcim"])

    def test_events_about_removed_example_records_go_with_them(self):
        dev = Device.objects.filter(name="FG-01").first()
        Event.objects.create(source="probes", title="FG-01 is down", object_type="network.device", object_id=str(dev.pk))
        real = Device.objects.get(name="real-sw-01")
        Event.objects.create(source="probes", title="real-sw-01 is down", object_type="network.device", object_id=str(real.pk))
        call_command("remove_examples", "--yes", stdout=io.StringIO())
        self.assertFalse(Event.objects.filter(title="FG-01 is down").exists())
        self.assertTrue(Event.objects.filter(title="real-sw-01 is down").exists())

    def test_a_source_an_operator_took_over_is_kept(self):
        from hypervisor.management.commands import seed_hypervisor as sh
        VCenterSource.objects.filter(name=sh.NAME).update(description="Adopted by the infrastructure team")
        call_command("remove_examples", "--yes", stdout=io.StringIO())
        self.assertTrue(VCenterSource.objects.filter(name=sh.NAME).exists())

    def test_another_tenant_is_not_counted_or_touched(self):
        other = UrlCheck.objects.create(tenant_key="other", name="NSE Web Portal", url="https://www.nseindia.com/", tags="example,web")
        call_command("remove_examples", "--yes", stdout=io.StringIO())
        self.assertTrue(UrlCheck.objects.filter(pk=other.pk).exists())

    def test_a_site_a_real_user_belongs_to_is_kept(self):
        site = Site.objects.get(name="Example DC Mumbai")
        Membership.objects.create(user=User.objects.get(username="realowner"), site=site, role="viewer")
        call_command("remove_examples", "--yes", stdout=io.StringIO())
        self.assertTrue(Site.objects.filter(pk=site.pk).exists())
        self.assertFalse(Site.objects.filter(name="Example DR Chennai").exists())
        self.assertEqual(real_snapshot()["membership"], 2)

    def test_an_agent_who_was_given_a_password_is_a_real_user_and_is_kept(self):
        from servicedesk.management.commands import seed_servicedesk as ss
        agent = User.objects.get(username=f"example.{ss.AGENTS[0][0]}")
        agent.set_password(PW)
        agent.save()
        call_command("remove_examples", "--yes", stdout=io.StringIO())
        self.assertTrue(User.objects.filter(pk=agent.pk).exists())
        self.assertFalse(User.objects.filter(username=f"example.{ss.AGENTS[1][0]}").exists())
        call_command("seed_servicedesk", "--remove", stdout=io.StringIO())      # the command's own removal agrees
        self.assertTrue(User.objects.filter(pk=agent.pk).exists())

    def test_real_catalog_and_checklist_items_that_reuse_an_example_wording_are_kept(self):
        from bodeod.management.commands import seed_bodeod as sb
        from bodeod.models import ChecklistItem, Mode
        from servicedesk.management.commands import seed_servicedesk as ss
        from servicedesk.models import CatalogItem
        real_cat = CatalogItem.objects.create(name="Laptop request", description="Example: what to order", category="Hardware")
        real_cat2 = CatalogItem.objects.create(name=ss.CATALOG[0][0] + " (real)", description="Example: ours", category="Hardware")
        mode = Mode.objects.get(code="BOD")
        # an operator's own items: the seed's name without the mark, and the mark on a name the seed never wrote
        mine = ChecklistItem.objects.create(mode=mode, name="Our own check", kind="manual", instructions="Example: ours")
        name = next(i[2] for i in sb.ITEMS if i[0] == "BOD")
        ChecklistItem.objects.filter(mode=mode, name=name).update(instructions="Edited by the desk")
        call_command("remove_examples", "--yes", stdout=io.StringIO())
        for obj in (real_cat, real_cat2, mine):
            self.assertTrue(type(obj).objects.filter(pk=obj.pk).exists(), obj)
        self.assertTrue(ChecklistItem.objects.filter(mode=mode, name=name).exists())
        self.assertFalse(ChecklistItem.objects.filter(instructions__startswith="Example", name__in=[i[2] for i in sb.ITEMS]).exists())

    def test_the_new_remove_flags_of_the_older_seed_commands(self):
        call_command("seed_examples", "--remove", stdout=io.StringIO())
        self.assertFalse(UrlCheck.objects.filter(name="NSE Web Portal").exists())
        call_command("seed_observe", "--remove", stdout=io.StringIO())
        self.assertEqual(examples.summary(only=["observe"])[0]["total"], 0)
        call_command("seed_administration", "--remove", stdout=io.StringIO())
        self.assertFalse(Site.objects.filter(name="Example DR Chennai").exists())
        self.assertEqual(real_snapshot()["realowner"], 1)


class DemoDataCardTests(TestCase):
    def setUp(self):
        call_command("seed_examples", stdout=io.StringIO())
        call_command("seed_tcpcheck", stdout=io.StringIO())
        self.admin = User.objects.create_superuser("root", "r@acme.test", PW)
        self.operator = User.objects.create_user("op", password=PW, is_staff=True)
        self.viewer = User.objects.create_user("vw", password=PW)
        self.site_admin = User.objects.create_user("sa", password=PW)
        site = Site.objects.create(name="Mumbai", code="mum")
        Membership.objects.create(user=self.site_admin, site=site, role="admin")
        self.health = reverse("administration:health")
        self.remove = reverse("administration:demo_remove")

    def test_card_shows_counts_for_an_admin(self):
        self.client.force_login(self.admin)
        r = self.client.get(self.health)
        self.assertContains(r, 'id="demo-data"')
        self.assertContains(r, "Remove all example data")
        self.assertContains(r, "URL checks: 7, domains: 9")
        self.assertContains(r, "TCP checks: 5")
        self.assertContains(r, reverse("urlchecker:index"))             # linked to the area

    def test_card_without_examples_says_clean(self):
        call_command("remove_examples", "--yes", stdout=io.StringIO())
        self.client.force_login(self.admin)
        r = self.client.get(self.health)
        self.assertContains(r, "No example data.")
        self.assertNotContains(r, "Remove all example data")

    def test_only_admins_reach_it(self):
        for user in (self.operator, self.viewer, self.site_admin):
            self.client.force_login(user)
            self.assertEqual(self.client.get(self.health).status_code, 403, user.username)
            self.client.post(self.remove, {"confirm": "remove"})
            self.assertTrue(examples.exists(), user.username)
        self.client.logout()
        self.assertEqual(self.client.post(self.remove, {"confirm": "remove"}).status_code, 302)
        self.assertTrue(examples.exists())

    def test_get_does_not_remove(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(self.remove).status_code, 405)
        self.assertTrue(examples.exists())

    def test_csrf_is_enforced(self):
        c = Client(enforce_csrf_checks=True)
        c.force_login(self.admin)
        self.assertEqual(c.post(self.remove, {"confirm": "remove"}).status_code, 403)
        self.assertTrue(examples.exists())

    def test_confirmation_is_required(self):
        self.client.force_login(self.admin)
        r = self.client.post(self.remove, {}, follow=True)
        self.assertContains(r, "Tick the confirmation box")
        self.client.post(self.remove, {"confirm": "yes please"})
        self.assertTrue(examples.exists())
        self.assertFalse(AuditEvent.objects.filter(object_type="core.exampledata").exists())

    def test_remove_deletes_audits_and_reports(self):
        UrlCheck.objects.create(name="Real shop", url="https://shop.acme.test/")
        self.client.force_login(self.admin)
        r = self.client.post(self.remove, {"confirm": "remove"}, follow=True)
        self.assertContains(r, "Removed 21 example record")
        self.assertFalse(examples.exists())
        self.assertTrue(UrlCheck.objects.filter(name="Real shop").exists())
        ev = AuditEvent.objects.get(object_type="core.exampledata")
        self.assertEqual((ev.actor_name, ev.action, ev.result), ("root", "delete", "ok"))
        self.assertEqual(ev.details["removed"], 21)
        self.assertEqual(self.client.session[examples.SESSION_KEY], 0)


class ExampleBannerTests(TestCase):
    def setUp(self):
        call_command("seed_examples", stdout=io.StringIO())
        self.admin = User.objects.create_superuser("root", "r@acme.test", PW)
        self.operator = User.objects.create_user("op", password=PW, is_staff=True)
        self.viewer = User.objects.create_user("vw", password=PW)

    def test_admin_sees_the_banner_after_signing_in(self):
        self.client.force_login(self.admin)
        r = self.client.get("/")
        self.assertContains(r, "Showing example data")
        self.assertContains(r, reverse("administration:demo_dismiss"))
        self.assertContains(self.client.get(reverse("urlchecker:index")), "Showing example data")

    def test_other_roles_never_see_it(self):
        for user in (self.operator, self.viewer):
            self.client.force_login(user)
            self.assertNotContains(self.client.get("/"), "Showing example data", msg_prefix=user.username)
            self.assertNotIn(examples.SESSION_KEY, self.client.session)

    def test_anonymous_sees_nothing(self):
        self.assertNotContains(Client().get(reverse("b2b_landing")), "Showing example data")

    def test_no_banner_without_example_data(self):
        call_command("remove_examples", "--yes", stdout=io.StringIO())
        self.client.force_login(self.admin)
        self.assertNotContains(self.client.get("/"), "Showing example data")

    def test_dismiss_hides_it_for_this_session_only(self):
        self.client.force_login(self.admin)
        r = self.client.post(reverse("administration:demo_dismiss"), {"next": "/monitoring/url-checker/"})
        self.assertRedirects(r, "/monitoring/url-checker/", fetch_redirect_response=False)
        self.assertNotContains(self.client.get("/"), "Showing example data")
        self.client.logout()
        self.client.force_login(self.admin)             # a new sign-in shows it again
        self.assertContains(self.client.get("/"), "Showing example data")

    def test_dismiss_does_not_redirect_off_site(self):
        self.client.force_login(self.admin)
        r = self.client.post(reverse("administration:demo_dismiss"), {"next": "https://evil.test/"})
        self.assertRedirects(r, reverse("dashboard"), fetch_redirect_response=False)

    def test_dismiss_needs_post_csrf_and_an_admin(self):
        url = reverse("administration:demo_dismiss")
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(url).status_code, 405)
        c = Client(enforce_csrf_checks=True)
        c.force_login(self.admin)
        self.assertEqual(c.post(url).status_code, 403)
        self.client.force_login(self.operator)
        self.client.post(url)
        self.assertNotIn(examples.DISMISS_KEY, self.client.session)

    def test_banner_adds_no_database_query_to_a_page_view(self):
        self.client.force_login(self.admin)
        self.client.get("/")                                                  # warm anything cached
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        with CaptureQueriesContext(connection) as with_banner:
            self.client.get(reverse("tcpcheck:index"))
        session = self.client.session
        session[examples.SESSION_KEY] = 0
        session.save()
        with CaptureQueriesContext(connection) as without:
            self.client.get(reverse("tcpcheck:index"))
        self.assertEqual(len(with_banner), len(without))

    def test_card_visit_refreshes_the_flag(self):
        self.client.force_login(self.admin)
        call_command("remove_examples", "--yes", stdout=io.StringIO())
        self.assertEqual(self.client.session[examples.SESSION_KEY], 1)       # stale until the card (or a new sign-in) looks
        self.client.get(reverse("administration:health"))
        self.assertEqual(self.client.session[examples.SESSION_KEY], 0)
        self.assertNotContains(self.client.get("/"), "Showing example data")


class GoLiveExampleCheckTests(TestCase):
    def run_check(self):
        return [r for r in golive.run_checks(only=["example_data"])]

    def test_pass_when_clean(self):
        (r,) = self.run_check()
        self.assertEqual((r.key, r.status), ("example_data", golive.PASS))

    def test_warns_with_the_fix_when_examples_exist(self):
        call_command("seed_examples", stdout=io.StringIO())
        (r,) = self.run_check()
        self.assertEqual(r.status, golive.WARN)
        self.assertIn("16 example records", r.detail)
        self.assertIn("remove_examples", r.fix)

    def test_a_warning_never_fails_the_report(self):
        call_command("seed_examples", stdout=io.StringIO())
        self.assertIn("example_data", golive.check_keys())
