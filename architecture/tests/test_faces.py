"""architecture/faces.py: generated device faces. Pure Python, so these are SimpleTestCases."""
import re

from django.test import SimpleTestCase

from architecture import faces


class PortGuessTests(SimpleTestCase):
    def test_known_models(self):
        table = {
            "Nexus 9336C-FX2": (0, 0, 36),            # 36 QSFP
            "Nexus 93180YC-FX": (0, 48, 6),           # 48 SFP + 6 QSFP
            "Catalyst 9500-48Y4C": (0, 48, 4),        # 48 x 25G + 4 x 100G
            "Aruba CX 6300M 48G": (48, 4, 0),         # 48 RJ45 + 4 SFP
            "Nexus 9364C": (0, 0, 64),
            "": (24, 4, 0),                           # the default
            "something unknown": (24, 4, 0),
            None: (24, 4, 0),
        }
        for model, ports in table.items():
            self.assertEqual(faces.guess_ports(model), ports, model)

    def test_small_switches(self):
        self.assertEqual(faces.guess_ports("SG-8"), (8, 0, 0))
        rj, sfp, q = faces.guess_ports("Switch 16")
        self.assertEqual((rj, q), (16, 0))

    def test_rack_units(self):
        self.assertEqual(faces.rack_units("storage", "anything"), 2)
        self.assertEqual(faces.rack_units("server", "Dell PowerEdge R650"), 1)
        self.assertEqual(faces.rack_units("server", "Dell PowerEdge R750"), 2)
        self.assertEqual(faces.rack_units("server", "PowerEdge R7525"), 2)
        self.assertEqual(faces.rack_units("server", "HPE ProLiant DL380 Gen10"), 2)
        self.assertEqual(faces.rack_units("server", "Cisco UCS C240"), 2)
        self.assertEqual(faces.rack_units("server", "Generic 2U box"), 2)
        self.assertEqual(faces.rack_units("server", ""), 1)

    def test_face_kind_follows_the_art_kind_first_then_the_device_type(self):
        self.assertEqual(faces.face_kind("physical_server", "storage"), "storage")
        self.assertEqual(faces.face_kind("switch"), "switch")
        self.assertEqual(faces.face_kind("wireless"), "access_point")
        self.assertEqual(faces.face_kind("virtual_machine"), "server")
        self.assertEqual(faces.face_kind("load_balancer"), "load_balancer")
        self.assertEqual(faces.face_kind("nonsense"), "generic")


class FaceSetTests(SimpleTestCase):
    def test_every_kind_renders_a_symbol_with_its_view_box(self):
        fs = faces.FaceSet()
        ids = [fs.use(k, "Cisco", "Nexus 9336C-FX2") for k in faces.KINDS]
        out = fs.defs()
        self.assertEqual(len(set(ids)), len(faces.KINDS))
        for sid in ids:
            self.assertIn(f'<symbol id="{sid}" viewBox="0 0 170 62">', out)
        self.assertTrue(out.startswith("<defs>") and out.endswith("</defs>"))

    def test_rack_units_have_their_own_sheet(self):
        fs = faces.FaceSet()
        one, two, st, vm = fs.use("server", unit=1), fs.use("server", unit=2), fs.use("storage", unit=2), fs.use("vm", unit=1)
        out = fs.defs()
        self.assertIn(f'<symbol id="{one}" viewBox="0 0 120 11">', out)
        self.assertIn(f'<symbol id="{two}" viewBox="0 0 120 22">', out)
        self.assertIn(f'<symbol id="{st}" viewBox="0 0 120 22">', out)
        self.assertIn("fc-virt", out)

    def test_same_model_is_drawn_once_however_many_devices_use_it(self):
        fs = faces.FaceSet()
        ids = {fs.use("switch", "Cisco", "Nexus 93180YC-FX") for _ in range(60)}
        self.assertEqual(len(ids), 1)
        self.assertEqual(len(fs), 1)
        fs.use("switch", "Cisco", "Nexus 9336C-FX2")
        self.assertEqual(len(fs), 2)

    def test_output_is_deterministic_and_sorted(self):
        def build(order):
            fs = faces.FaceSet()
            for kind, vendor, model in order:
                fs.use(kind, vendor, model)
            return fs.defs()
        specs = [("switch", "Cisco", "9336C"), ("firewall", "Fortinet", "601F"), ("router", "Cisco", "ASR"),
                 ("server", "Dell", "R650"), ("storage", "Dell", "ME5024")]
        self.assertEqual(build(specs), build(specs))
        self.assertEqual(build(specs), build(specs[::-1]))
        ids = re.findall(r'<symbol id="([^"]+)"', build(specs))
        self.assertEqual(ids, sorted(ids))

    def test_hostile_vendor_and_model_text_is_escaped_and_clipped(self):
        evil = '"><script>alert(1)</script><img src=x onerror=alert(2)>'
        fs = faces.FaceSet()
        for kind in faces.KINDS:
            fs.use(kind, evil, evil)
        fs.use("server", evil, evil, unit=1)
        out = fs.defs()
        self.assertNotIn("<script", out)
        self.assertNotIn("<img", out)
        self.assertNotIn("onerror=alert(2)>", out)
        self.assertIn("&lt;script&gt;", out)
        # nothing that could load or run anything
        for bad in ("<image", "href=\"http", "data:", "javascript:", "<foreignObject", "<style"):
            self.assertNotIn(bad, out)

    def test_control_characters_and_bidi_overrides_never_reach_the_markup(self):
        fs = faces.FaceSet()
        fs.use("switch", "Ci\x00sco‮", "9336\x1bC⁦-FX2\n\t")
        out = fs.defs()
        for ch in ("\x00", "\x1b", "‮", "⁦"):
            self.assertNotIn(ch, out)
        self.assertIn("CISCO", out)

    def test_markup_is_well_formed_xml(self):
        from xml.dom import minidom
        fs = faces.FaceSet()
        for kind in faces.KINDS:
            fs.use(kind, "A & B <Co>", "M\"1'")
        fs.use("storage", unit=2)
        minidom.parseString(f'<svg xmlns="http://www.w3.org/2000/svg">{fs.defs()}</svg>')

    def test_a_big_switch_stays_small_because_ports_are_paths_not_elements(self):
        fs = faces.FaceSet()
        fs.use("switch", "Cisco", "Nexus 93180YC-FX")
        self.assertLess(len(fs.defs()), 6000)
        self.assertLess(fs.defs().count("<"), 60)

    def test_simple_detail_drops_text_and_ports(self):
        full, simple = faces.FaceSet("full"), faces.FaceSet("simple")
        full.use("switch", "Cisco", "Nexus 93180YC-FX")
        simple.use("switch", "Cisco", "Nexus 93180YC-FX")
        self.assertIn("<text", full.defs())
        self.assertNotIn("<text", simple.defs())
        self.assertLess(len(simple.defs()), len(full.defs()))
        # simple faces share one symbol per kind, whatever the model
        simple.use("switch", "Juniper", "QFX5120")
        self.assertEqual(len(simple), 1)

    def test_unknown_kind_and_detail_fall_back(self):
        fs = faces.FaceSet("bogus")
        self.assertEqual(fs.detail, "full")
        self.assertIn("fc-f-generic", fs.use("???", "x", "y"))
        self.assertEqual(faces.FaceSet().defs(), "")

    def test_switch_port_grid_reflects_the_model(self):
        def ports(model):
            fs = faces.FaceSet()
            fs.use("switch", "X", model)
            d = re.search(r'class="fc-port" d="([^"]+)"', fs.defs())
            return len(re.findall(r"M", d.group(1))) if d else 0
        self.assertEqual(ports("6300M 48G"), 48 + 2 * 0 + 0)           # 48 RJ45 in the main grid
        self.assertEqual(ports("Switch 24"), 24)
        self.assertEqual(ports("Nexus 9336C"), 0)                       # QSFP cages, no RJ45 sockets
