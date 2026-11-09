"""The sidebar: grouping, every old URL still reachable, active state, badges, and the menu.js / CSS contract."""
import json
import re
from pathlib import Path

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from core import overview
from core.context_processors import build_nav
from core.models import Membership, Site
from core.nav import NAV, SECTIONS

# the URL names of the flat menu this one replaced (core/nav.py before the sections): none may disappear
OLD_URLS = json.loads("""["dashboard", "agent:chat", "cmdb:assets", "cmdb:software", "cmdb:licenses", "cmdb:lifecycle", "cmdb:topology", "cmdb:items", "cmdb:relationships", "cmdb:heatmap", "telemetry:fleet", "telemetry:infrastructure", "telemetry:discovery", "telemetry:nagios", "telemetry:prometheus", "telemetry:alertmanager", "tcpcheck:index", "tcpcheck:uptime", "telemetry:explorer", "network:switches", "network:routers", "network:firewalls", "network:load_balancers", "network:wireless", "network:physical_servers", "network:virtual_machines", "network:all", "network:fabric", "network:discovery", "architecture:overview", "compute:overview", "compute:servers", "compute:vms", "compute:clusters", "compute:kubernetes", "hypervisor:datastores", "hypervisor:unmatched", "hypervisor:requests", "hypervisor:sources", "k8s:overview", "k8s:clients", "k8s:namespaces", "k8s:nodes", "k8s:workloads", "k8s:services", "k8s:ingresses", "k8s:volumes", "k8s:certificates", "k8s:secrets", "k8s:tools", "k8s:monitoring", "k8s:issues", "k8s:imports", "ipam:overview", "ipam:subnets", "ipam:addresses", "ipam:vlans", "ipam:dns", "dcim:overview", "dcim:racks", "dcim:power", "dcim:cooling", "apm:index", "apm:sources", "hardware:endpoints", "hardware:requests", "domains:overview", "domains:list", "domains:dns", "domains:ssl", "domains:uptime", "domains:performance", "domains:security", "domains:export_reports", "urlchecker:index", "automation:templates", "observe:exporters", "observe:logs", "bodeod:status", "bodeod:keys", "bodeod:history", "bodeod:checklist", "bodeod:holidays", "automation:runbooks", "automation:stackstorm", "automation:workflows", "reports:analytics", "reports:index", "administration:index", "onboarding:index", "onboarding:sd_page", "servicedesk:tickets", "servicedesk:incidents", "servicedesk:problems", "servicedesk:changes", "servicedesk:catalog", "servicedesk:sla"]""")
CORE = Path(__file__).resolve().parent
STATIC = CORE / "static" / "linkedeye"


def leaves():
    for item in NAV:
        if item.get("children"):
            for c in item["children"]:
                yield item, c
        else:
            yield item, item


class NavDataTests(SimpleTestCase):
    def test_every_old_url_name_is_present_exactly_once(self):
        urls = [c["url"] for _g, c in leaves()]
        for name in OLD_URLS:
            self.assertEqual(urls.count(name), 1, name)

    def test_only_the_overview_page_is_new(self):
        urls = {c["url"] for _g, c in leaves()}
        self.assertEqual(urls - set(OLD_URLS), {"telemetry:overview"})

    def test_every_url_name_resolves(self):
        for _g, c in leaves():
            reverse(c["url"])

    def test_every_entry_has_a_known_section_and_sections_are_non_empty(self):
        keys = {k for k, _l in SECTIONS}
        for item in NAV:
            self.assertIn(item.get("section"), keys, item["label"])
        for k in keys:
            self.assertTrue(any(i["section"] == k for i in NAV), k)

    def test_labels_are_unique_within_a_section(self):
        # the menu filter lists a match under its group, but two identical labels in one section still read as a bug
        for key, _label in SECTIONS:
            labels = []
            for item in (i for i in NAV if i["section"] == key):
                labels.append(item["label"])
                labels.extend(c["label"] for c in item.get("children", []))
            dupes = {x for x in labels if labels.count(x) > 1}
            self.assertFalse(dupes, (key, dupes))

    def test_ambiguous_names_were_disambiguated(self):
        labels = [c["label"] for _g, c in leaves()]
        self.assertEqual(labels.count("Discovery"), 0)
        self.assertIn("Kubernetes (live)", labels)
        groups = [i["label"] for i in NAV]
        self.assertEqual(groups.count("Kubernetes"), 1)

    def test_section_order_puts_operate_first_and_platform_last(self):
        keys = [k for k, _l in SECTIONS]
        self.assertEqual(keys[0], "operate")
        self.assertEqual(keys[-1], "platform")
        self.assertEqual([i["section"] for i in NAV], sorted((i["section"] for i in NAV), key=keys.index))


class ActiveStateTests(SimpleTestCase):
    def active(self, path):
        nav, sections = build_nav(path)
        on = [(e["label"], c["label"]) for e in nav for c in (e["children"] or [e]) if c["active"]]
        return nav, sections, on

    def test_dashboard_only_on_the_root_path(self):
        _n, sections, on = self.active("/")
        self.assertEqual(on, [("Dashboard", "Dashboard")])
        self.assertEqual([s["key"] for s in sections if s["active"]], ["operate"])
        self.assertEqual(self.active("/nowhere/at/all/")[2], [])

    def test_a_single_link_page_marks_its_section(self):
        _n, sections, on = self.active("/monitoring/fleet/")
        self.assertEqual(on, [("Fleet", "Fleet")])
        self.assertEqual([s["key"] for s in sections if s["active"]], ["observe"])

    def test_a_child_page_opens_its_group_and_only_that_group(self):
        nav, sections, on = self.active("/apm/sources/")
        self.assertEqual(on, [("APM", "Sources")])
        opened = [e["label"] for e in nav if e["open"]]
        self.assertEqual(opened, ["APM"])
        self.assertEqual([s["key"] for s in sections if s["active"]], ["observe"])

    def test_a_detail_page_keeps_its_list_active(self):
        nav, _s, on = self.active("/monitoring/infrastructure/db-01:9100/metrics/")
        self.assertEqual(on, [("Infrastructure", "Infrastructure")])

    def test_the_most_specific_link_wins(self):
        # /monitoring/infrastructure/ prefixes many pages; a deeper URL name must beat a shallower one
        for path in ("/monitoring/alerts/", "/monitoring/alerts/12/"):
            self.assertEqual(self.active(path)[2], [("Alert Manager", "Alert Manager")], path)

    def test_badges_attach_to_their_entries_and_cap(self):
        nav, _s = build_nav("/", {"incidents": 12, "alerts": 3, "checks": 250})
        got = {e["label"]: (e["badge"], e["badge_text"]) for e in nav if e["badge"]}
        self.assertEqual(got, {"Service Desk": (12, "12"), "Alert Manager": (3, "3"), "Checks": (250, "99+")})
        self.assertEqual(overview.badge_text(0), "")
        self.assertEqual(overview.badge_text(99), "99")
        self.assertEqual(overview.badge_text(100), "99+")


class RenderedSidebarTests(TestCase):
    def setUp(self):
        cache.clear()
        self.mum = Site.objects.create(name="Mumbai", code="mum")
        self.dr = Site.objects.create(name="DR Site", code="dr")
        self.admin = User.objects.create_superuser("root", "r@example.com", "Corr3ct-horse-battery")
        self.viewer = User.objects.create_user("viewer", "v@example.com", "Corr3ct-horse-battery")
        Membership.objects.create(user=self.viewer, site=self.mum, role="viewer")

    def tearDown(self):
        cache.clear()

    def test_markup_has_sections_filter_pins_and_aria(self):
        self.client.force_login(self.admin)
        html = self.client.get(reverse("telemetry:fleet")).content.decode()
        side = html[html.index('<nav class="nav"'):html.index("</nav>")]
        for key, label in SECTIONS:
            self.assertIn(f'data-sec="{key}"', side)
            self.assertIn(f"<span>{label}</span>", side)
        self.assertIn('data-nav-filter', html)
        self.assertIn('aria-current="page"', side)
        self.assertEqual(side.count('aria-current="page"'), 1)
        self.assertIn('aria-expanded="true"', side)           # section buttons start open
        self.assertIn('data-pin="telemetry:fleet"', side)
        self.assertIn('data-pinned-list', side)
        # the active group is open on the server, so the menu works without JavaScript
        apm = self.client.get("/apm/").content.decode()
        self.assertRegex(apm, r'<details class="nav-group on" data-key="apm" data-label="APM" open>')

    def test_site_scoped_badges_count_only_the_users_sites(self):
        from tcpcheck.models import TcpCheck
        from urlchecker.models import UrlCheck
        UrlCheck.objects.create(name="a", url="https://a.example.com/", site="Mumbai", last_status="down")
        UrlCheck.objects.create(name="b", url="https://b.example.com/", site="DR Site", last_status="down")
        TcpCheck.objects.create(name="t", host="h.example.com", port=1, site="DR Site", last_status="down")
        self.assertEqual(overview.nav_badges(self.admin)["checks"], 3)
        cache.clear()
        self.assertEqual(overview.nav_badges(self.viewer)["checks"], 1)

    def test_badges_are_cached_so_a_page_costs_nothing_extra_after_the_first(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        self.client.force_login(self.admin)
        self.client.get(reverse("search"))
        cache.clear()
        with CaptureQueriesContext(connection) as cold:
            self.client.get(reverse("search"))
        with CaptureQueriesContext(connection) as warm:
            self.client.get(reverse("search"))
        # the badge counts are the only extra queries: three COUNTs (incidents, alerts, checks) at most four
        self.assertGreaterEqual(len(cold) - len(warm), 3)
        self.assertLessEqual(len(cold) - len(warm), 5)

    def test_the_sidebar_costs_the_same_queries_with_one_site_or_forty_and_with_badges_on(self):
        """The context processor runs on every page: its query count must not grow with sites, checks or alerts."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from telemetry.models import Alert
        from urlchecker.models import UrlCheck

        def count():
            self.client.force_login(self.admin)
            cache.clear()
            self.client.get(reverse("search"))          # session, content types, template caches
            cache.clear()
            with CaptureQueriesContext(connection) as q:
                self.client.get(reverse("search"))
            return len(q)
        before = count()
        for i in range(40):
            Site.objects.create(name=f"Bulk {i}", code=f"bulk{i}")
            UrlCheck.objects.create(name=f"bulk{i}", url=f"https://b{i}.example.com/", site=f"Bulk {i}", last_status="down")
            Alert.objects.create(fingerprint=f"bulk{i}", alertname=f"A{i}", severity="critical", site=f"Bulk {i}",
                                 starts_at=timezone.now())
        self.assertEqual(count(), before)
        self.assertEqual(overview.nav_badges(self.admin)["alerts"], 40)        # and the badges really counted them

    def test_unresolved_url_names_fall_back_to_the_coming_soon_page(self):
        from core.context_processors import _resolve
        entry = _resolve({"label": "Nothing Yet", "url": "no_such:name"}, "/")
        self.assertEqual(entry["href"], reverse("coming_soon", args=["nothing-yet"]))


class MenuContractTests(SimpleTestCase):
    """menu.js and app.css are plain files: pin down what the templates, the CSS and the browser check rely on."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.js = (STATIC / "menu.js").read_text()
        cls.css = (STATIC / "app.css").read_text()
        cls.base = (CORE / "templates" / "core" / "base.html").read_text()

    def test_js_uses_the_documented_hooks(self):
        for hook in ("[data-nav]", "[data-nav-filter]", "[data-nav-empty]", "[data-nav-sec]", "[data-pinned-list]",
                     "linkedeye-nav-sections", "linkedeye-nav-groups", "linkedeye-nav-pins", "nav-flyout"):
            self.assertIn(hook, self.js, hook)

    def test_js_never_builds_html_from_strings(self):
        for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
            self.assertNotIn(bad, self.js, bad)

    def test_every_local_storage_access_is_guarded(self):
        # a private window throws on any access: the only direct uses are the two helpers, both inside try
        uses = [m.start() for m in re.finditer(r"localStorage\.", self.js)]
        self.assertEqual(len(uses), 2)
        for at in uses:
            self.assertIn("try", self.js[max(0, at - 160):at])

    def test_keyboard_contract_is_implemented(self):
        for key in ("ArrowDown", "ArrowUp", "ArrowRight", "ArrowLeft", "Escape", "Home", "End"):
            self.assertIn(f"'{key}'", self.js, key)
        self.assertIn("e.key !== '/'", self.js)               # "/" focuses the filter

    def test_template_has_no_inline_handlers_and_loads_the_script(self):
        self.assertNotRegex(self.base, r"\son[a-z]+=")
        self.assertIn("linkedeye/menu.js", self.base)
        self.assertIn('data-nav-filter', self.base)
        self.assertIn('aria-controls="nav-sec-{{ sec.key }}"', self.base)

    def test_css_covers_every_section_for_the_remembered_collapsed_state(self):
        # the head script sets data-nav-c before first paint; app.css hides those section bodies
        for key, _l in SECTIONS:
            self.assertIn(f'html[data-nav-c~="{key}"] .nav-sec[data-sec="{key}"]', self.css, key)
        self.assertIn("linkedeye-nav-sections", self.base)

    def test_css_defines_the_rail_flyout_and_focus_styles(self):
        for sel in (".nav-flyout", ".nav-pin", ".nav-badge", ".nav-filter", ".nav-sec-h", "focus-visible"):
            self.assertIn(sel, self.css, sel)
        self.assertIn("prefers-reduced-motion", self.css)

    def test_colours_in_the_sidebar_are_tokens(self):
        block = self.css[self.css.index("Sidebar v4"):self.css.index("Paper is white")]
        # no hard-coded colours outside var(): rgba()/hex would break the other theme
        self.assertNotRegex(re.sub(r"var\([^)]*\)", "", block), r"#[0-9a-fA-F]{3,8}\b")


class DocsTests(SimpleTestCase):
    def test_ui_doc_covers_the_sections_and_how_to_add_an_item(self):
        text = (CORE.parent / "docs" / "UI.md").read_text()
        for _k, label in SECTIONS:
            self.assertIn(label, text)
        for needle in ("core/nav.py", "build_nav", "menu.js", "Before / after", "nav_badges", "dashboard_data", "--fs-title"):
            self.assertIn(needle, text, needle)
        self.assertIn("docs/UI.md", (CORE.parent / "docs" / "MODULES.md").read_text())


class ArtDirectionTests(SimpleTestCase):
    """Things a reviewer saw as amateur: look-alike rail icons, a menu that hides the current page, awkward keys."""

    def test_top_level_icons_are_unique_so_the_icon_rail_is_readable(self):
        icons = [i["icon"] for i in NAV]
        dupes = {x for x in icons if icons.count(x) > 1}
        self.assertFalse(dupes, dupes)

    def test_every_icon_is_drawn(self):
        from core.templatetags.le import ICONS
        for item in NAV:
            self.assertIn(item["icon"], ICONS, item["label"])

    def test_the_menu_scrolls_the_current_page_into_view(self):
        js = (STATIC / "menu.js").read_text()
        self.assertIn("revealCurrent", js)
        self.assertIn('a[aria-current="page"]', js)
        self.assertIn("nav.scrollTop", js)               # the menu scrolls, never the page (no scrollIntoView jump)
        self.assertNotIn("scrollIntoView", js)

    def test_arrows_walk_only_the_matching_links_while_filtering(self):
        js = (STATIC / "menu.js").read_text()
        self.assertIn("is-filtering') ? '.nav-row > a'", js)

    def test_one_typeface_inside_pages_and_a_visible_help_glyph(self):
        css = (STATIC / "app.css").read_text()
        self.assertIn(".page { --serif: var(--font); }", css)
        self.assertIn(".page :is(h1, h2, h3, h4) { font-family: var(--font); }", css)
        self.assertIn(".icon-btn.solid .ic-mark { stroke: var(--panel); }", css)
