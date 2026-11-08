"""The page of the second visual pass: header strip, presentation mode, faces on the page, the slim unclassified strip,
state shown only on the LED and the label chip, and everything hostile staying escaped."""
import re

from django.urls import reverse

from architecture import services
from network.models import Device

from .fixtures import Base, link, make_fabric


class VisualPageTests(Base):
    def setUp(self):
        super().setUp()
        Device.objects.filter(pk=self.spine.pk).update(vendor="Cisco", model="Nexus 9336C-FX2", status="up")
        Device.objects.filter(pk=self.leaf.pk).update(vendor="Cisco", model="Nexus 93180YC-FX", status="down")
        Device.objects.filter(pk=self.srv.pk).update(vendor="Dell", model="PowerEdge R750", role="Web server")
        link(self.spine, self.leaf, "Eth1/1", "Eth1/49", speed_mbps=100000)
        link(self.leaf, self.srv, "Eth1/1", "eth0", speed_mbps=25000)

    def page(self, **params):
        self.login(self.viewer)
        return self.client.get(reverse("architecture:overview"), params)

    def test_header_strip_has_the_title_the_site_and_the_live_summary(self):
        r = self.page()
        html = r.content.decode()
        strip = html.split('class="ar-strip"')[1].split("</header>")[0]
        self.assertIn("Data Center Architecture", strip)
        self.assertIn("All sites", strip)
        for key in ("devices_up", "devices_down", "devices_unknown", "links_up", "links_down"):
            self.assertIn(f'data-kpi="{key}"', strip)
        self.assertIn('id="ar-present"', strip)
        self.assertIn("Presentation mode", strip)
        self.assertEqual(r.context["kpis"]["devices_up"], 1)

    def test_band_labels_are_drawn_after_the_links_so_no_link_runs_through_a_pill(self):
        html = self.page().content.decode()
        self.assertLess(html.index('class="ar-edges"'), html.index('class="ar-tags"'))
        self.assertLess(html.index('class="ar-tags"'), html.index('class="ar-nodes"'))
        tags = html.split('class="ar-tags"')[1].split('class="ar-nodes"')[0]
        self.assertIn("band-tag", tags)
        self.assertNotIn("band-tag", html.split('class="ar-tags"')[0])

    def test_the_site_name_shows_in_the_strip_when_a_site_is_chosen(self):
        html = self.page(site="Mumbai").content.decode()
        self.assertRegex(html.split('class="ar-strip"')[1].split("</header>")[0], r'class="ar-site">Mumbai<')

    def test_presentation_mode_is_a_get_parameter_and_hides_the_chrome_by_class(self):
        off = self.page().content.decode()
        on = self.page(present="1").content.decode()
        self.assertNotIn("ar-present\" id=\"ar-page\"", off)
        self.assertRegex(on, r'class="pg-architecture ar-present" id="ar-page"')
        self.assertIn('aria-pressed="true"', on.split('id="ar-present"')[1][:200])
        self.assertIn("Exit presentation", on)
        # the button links to the toggled state, so it works without script
        self.assertIn('href="?present=1"', off)
        self.assertNotIn('href="?present=1"', on.split('id="ar-present"')[0][-60:])
        # the same chrome is in the page in both modes (CSS hides it), and the empty state never presents
        self.assertIn('class="kpis ar-kpis"', on)

    def test_presentation_css_and_script_exist(self):
        from pathlib import Path
        base = Path(__file__).resolve().parent.parent / "static" / "architecture"
        css, js = (base / "page.css").read_text(), (base / "architecture.js").read_text()
        self.assertIn(".pg-architecture.ar-present", css)
        self.assertIn("100vh", css)
        self.assertIn("setPresent", js)
        self.assertIn("'Escape'", js)

    def test_faces_are_symbols_used_once_per_device(self):
        html = self.page().content.decode()
        defs = html.split("<defs>")[1].split("</defs>")[0]
        syms = re.findall(r'<symbol id="([^"]+)"', defs)
        self.assertGreaterEqual(len(syms), 3)
        uses = re.findall(r'<use class="nd-face" href="#([^"]+)"', html)
        self.assertTrue(uses and set(uses) <= set(syms))
        self.assertEqual(len(syms), len(set(syms)))
        # no raster, no external reference
        self.assertNotIn("<image", html.split("<svg id=\"ar-svg\"")[1].split("</svg>")[0])

    def test_state_colours_only_the_led_and_the_label_chip(self):
        html = self.page().content.decode()
        down = re.search(r'<g class="nd nd-full st-down"[^>]*>(.*?)</g></a>', html, re.S).group(1)
        self.assertIn('class="nd-tag"', down)
        self.assertIn('class="nd-dot"', down)
        self.assertNotIn("fill=", down)                                  # the face itself carries no state colour
        self.assertIn("dc1-leaf-01", down)

    def test_links_are_straight_in_the_page_too(self):
        html = self.page().content.decode()
        d = re.findall(r'<path class="lk-line" d="([^"]+)"', html)
        self.assertTrue(d)
        self.assertTrue(all("C" not in x for x in d))

    def test_unclassified_devices_go_to_a_slim_strip_not_a_band(self):
        Device.objects.create(name="odd-appliance", device_type="load_balancer", site="Mumbai", mgmt_ip="192.0.2.99")
        # an unplaced device of a kind that classifies nowhere
        from architecture.models import Placement
        Placement.objects.create(device=Device.objects.get(name="odd-appliance"), tier="other")
        r = self.page()
        html = r.content.decode()
        self.assertEqual(r.context["unclassified_total"], 1)
        self.assertIn('class="ar-unclass"', html)
        self.assertIn("1</b> unclassified device: set a placement", html)
        svg = html.split('<svg id="ar-svg"')[1].split("</svg>")[0]
        self.assertNotIn("odd-appliance", svg)
        self.assertNotIn('data-tier="other"', svg)
        self.assertIn("odd-appliance", html.split('class="ar-unclass"')[1])

    def test_no_unclassified_strip_when_every_device_has_a_tier(self):
        self.assertNotIn('class="ar-unclass"', self.page().content.decode())

    def test_a_viewer_gets_no_placement_link_in_the_strip(self):
        from architecture.models import Placement
        d = Device.objects.create(name="odd-appliance", device_type="load_balancer", site="Mumbai", mgmt_ip="192.0.2.99")
        Placement.objects.create(device=d, tier="other")
        self.assertNotIn("Set a placement</a>", self.page().content.decode())
        self.login(self.operator)
        html = self.client.get(reverse("architecture:overview")).content.decode()
        self.assertIn(reverse("architecture:placement_edit", args=[d.pk]), html)

    def test_hostile_vendor_and_model_are_escaped_inside_the_faces(self):
        evil = '"><script>alert(7)</script>'
        Device.objects.filter(pk=self.spine.pk).update(vendor=evil, model=evil)
        Device.objects.filter(pk=self.srv.pk).update(vendor=evil, model=evil)
        html = self.page().content.decode()
        self.assertNotIn("<script>alert(7)", html)
        self.assertIn("&lt;script&gt;", html.split("<defs>")[1].split("</defs>")[0])

    def test_the_page_is_byte_stable_for_the_same_data_apart_from_the_clock(self):
        def stable(html):
            return re.sub(r"<time data-updated>[^<]*</time>|name=\"csrfmiddlewaretoken\" value=\"[^\"]+\"|\"generated_at\": ?\"[^\"]+\"", "", html)
        a = stable(self.page().content.decode())
        b = stable(self.page().content.decode())
        self.assertEqual(a.split('<svg id="ar-svg"')[1].split("</svg>")[0], b.split('<svg id="ar-svg"')[1].split("</svg>")[0])

    def test_many_faces_switch_to_the_simple_detail(self):
        from architecture import layout as lay
        small = lay.layout([lay.LNode(f"n{i}", "spine", f"n{i}") for i in range(4)], [])
        self.assertEqual(services.face_detail(small), "full")
        many = lay.layout([lay.LNode(f"n{i}", "spine", f"n{i:02d}") for i in range(24)] +
                          [lay.LNode(f"l{i}", "leaf", f"l{i:02d}") for i in range(24)] +
                          [lay.LNode(f"c{i}", "core", f"c{i:02d}") for i in range(16)], [], [])
        self.assertEqual(services.face_detail(many), "simple")

    def test_live_summary_carries_the_counts_the_strip_shows(self):
        self.login(self.viewer)
        j = self.client.get(reverse("architecture:live")).json()["summary"]
        self.assertEqual((j["devices_up"], j["devices_down"]), (1, 1))
        self.assertEqual(j["devices_up"] + j["devices_down"] + j["devices_unknown"], j["devices"])

    def test_the_dense_class_is_set_for_a_big_mesh_only(self):
        self.assertNotIn("ar-dense", self.page().content.decode())
        make_fabric(8, 16)
        self.assertIn("ar-dense", self.page().content.decode())

