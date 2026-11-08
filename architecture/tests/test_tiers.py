from django.test import SimpleTestCase

from architecture import tiers as tr
from architecture.tiers import Facts, Graph, classify, infer


def f(name="x", device_type="switch", **kw):
    return Facts(id=kw.pop("id", 1), name=name, device_type=device_type, **kw)


class InferTests(SimpleTestCase):
    def test_device_types(self):
        self.assertEqual(infer(f(device_type="router")).tier, "wan")
        self.assertEqual(infer(f(device_type="firewall")).tier, "firewall")
        self.assertEqual(infer(f(device_type="wireless")).tier, "other")
        self.assertEqual(infer(f(device_type="whatever")).tier, "other")
        for t in ("physical_server", "virtual_machine", "load_balancer"):
            self.assertEqual(infer(f(device_type=t)).tier, "server")

    def test_router_name_hint_does_not_override_type(self):
        self.assertEqual(infer(f("spine-rtr", "router")).tier, "wan")

    def test_switch_keywords(self):
        cases = {"dc1-spine-01": "spine", "SPINE2": "spine", "leaf01": "leaf", "dc1-tor-a": "leaf", "r12-ToR": "leaf",
                 "core-sw-01": "core", "border1": "core", "edge-sw": "core", "agg-02": "core", "dc-aggregation-1": "core"}
        for name, tier in cases.items():
            p = infer(f(name))
            self.assertEqual((name, p.tier, p.source), (name, tier, "name"))

    def test_keyword_is_a_word_not_a_substring(self):
        for name in ("motor-sw", "encore", "storefront", "leafy-greens"):
            self.assertNotEqual(infer(f(name)).source, "name", name)

    def test_camel_case_names_match_and_tor_still_matches(self):
        cases = {"SpineA": "spine", "DcSpine01": "spine", "LeafB2": "leaf", "BorderLeaf": "leaf", "CoreSwitch": "core",
                 "Core-SpineA": "spine", "EdgeSw": "core", "r12-ToR": "leaf", "ToR12": "leaf"}
        for name, tier in cases.items():
            p = infer(f(name))
            self.assertEqual((name, p.tier, p.source), (name, tier, "name"))
        for name in ("MotorSw", "Encore", "StoreFront", "LeafyGreens", "Spineless"):
            self.assertNotEqual(infer(f(name)).source, "name", name)
        self.assertEqual(infer(f("DBServer", "physical_server")).group, "Database Servers")
        self.assertEqual(infer(f("WebFarm1", "physical_server")).group, "Web Servers")

    def test_conflicting_hints_across_sources_are_settled_by_source_order(self):
        # name says spine, role says leaf: the role is the more deliberate field and wins, and the reason names it
        p = infer(f("dc1-spine-01", role="leaf"))
        self.assertEqual((p.tier, p.source), ("leaf", "name"))
        self.assertIn("role", p.reason)
        p = infer(f("dc1-spine-01", role="leaf", tags=("core-site",)))
        self.assertEqual(p.tier, "core")
        self.assertIn("tag", p.reason)
        # an empty or irrelevant role does not hide the name
        self.assertEqual(infer(f("dc1-spine-01", role="Data Center switch")).tier, "spine")

    def test_every_reason_is_explainable_text(self):
        cases = [f("x", "router"), f("x", "firewall"), f("sw"), f("sw", rack="R"), f("spine-1"), f("web-1", "physical_server"),
                 f("n", "physical_server", rack="R"), f("n", "physical_server")]
        for c in cases:
            p = infer(c, Graph(2, {"switch": 2}))
            self.assertTrue(p.reason and p.reason.endswith("."), (c, p.reason))
        self.assertIn("switches", infer(f("sw"), Graph(3, {"switch": 3})).reason)
        self.assertIn("firewall", infer(f("sw"), Graph(1, {"firewall": 1})).reason)

    def test_unicode_and_hostile_text_never_raise(self):
        for text in ("ノード-東京", "سويتش-leaf", "\u202espine\u202c", "é" * 500, "\x00leaf\x00", "spine" * 2000, ""):
            for t in ("switch", "physical_server", "router", "x"):
                p = infer(f(text, t, role=text, tags=(text, text)), Graph(1, {"switch": 1}))
                self.assertIn(p.tier, tr.TIERS)
        self.assertEqual(infer(f("سويتش-leaf-01")).tier, "leaf")

    def test_source_order_tags_then_role_then_name(self):
        self.assertEqual(infer(f("leaf-01", role="spine", tags=("core",))).tier, "core")
        self.assertEqual(infer(f("leaf-01", role="spine")).tier, "spine")
        self.assertEqual(infer(f("leaf-01")).tier, "leaf")

    def test_conflict_inside_one_source_spine_beats_leaf_beats_core(self):
        self.assertEqual(infer(f("spine-leaf-01")).tier, "spine")
        self.assertEqual(infer(f("border-leaf-01")).tier, "leaf")
        self.assertEqual(infer(f("core-spine")).tier, "spine")

    def test_override_wins_over_everything(self):
        p = infer(f("dc1-spine-01", tier_override="leaf"))
        self.assertEqual((p.tier, p.source, p.inferred), ("leaf", "override", False))
        self.assertEqual(infer(f("fw", "firewall", tier_override="core")).tier, "core")

    def test_graph_fallback_only_without_keywords(self):
        fw = Graph(degree=2, types={"firewall": 2})
        self.assertEqual(infer(f("sw-a"), fw).tier, "core")
        self.assertEqual(infer(f("sw-a"), fw).source, "graph")
        self.assertEqual(infer(f("sw-a"), Graph(1, {"router": 1})).tier, "core")
        self.assertEqual(infer(f("leaf-9"), fw).tier, "leaf")       # a keyword is stronger than the graph
        spine = Graph(degree=4, types={"switch": 4})
        p = infer(f("sw-b"), spine)
        self.assertEqual((p.tier, p.source), ("spine", "graph"))
        self.assertIn("4 switches", p.reason)
        self.assertTrue(p.weak)

    def test_graph_spine_needs_no_servers_and_no_rack(self):
        self.assertEqual(infer(f("sw"), Graph(5, {"switch": 4, "physical_server": 1})).tier, "leaf")
        self.assertEqual(infer(f("sw", rack="R1"), Graph(4, {"switch": 4})).tier, "leaf")
        self.assertEqual(infer(f("sw"), Graph(1, {"switch": 1})).tier, "leaf")     # one neighbour is not a spine

    def test_plain_switch_default(self):
        self.assertEqual(infer(f("sw", rack="R1")).tier, "leaf")
        self.assertEqual(infer(f("sw"), Graph(1, {"physical_server": 1})).tier, "leaf")
        p = infer(f("sw"))
        self.assertEqual((p.tier, p.source), ("other", "default"))

    def test_server_groups_from_keywords(self):
        cases = {"web-01": "Web Servers", "www2": "Web Servers", "app-03": "Application Servers",
                 "payments-application": "Application Servers", "db-1": "Database Servers", "sql-prod": "Database Servers",
                 "san-01": "Storage (SAN)", "nas2": "Storage (SAN)", "dc1-storage-3": "Storage (SAN)"}
        for name, group in cases.items():
            self.assertEqual(infer(f(name, "physical_server")).group, group, name)

    def test_group_colours_match_the_drawing(self):
        colours = {n: infer(f(n, "physical_server")).colour for n in ("web-1", "app-1", "db-1", "san-1", "misc-1")}
        self.assertEqual(colours, {"web-1": "orange", "app-1": "green", "db-1": "blue", "san-1": "red", "misc-1": "stone"})

    def test_group_keyword_precedence_storage_db_app_web(self):
        self.assertEqual(infer(f("web-db-01", "physical_server")).group, "Database Servers")
        self.assertEqual(infer(f("app-web-01", "physical_server")).group, "Application Servers")
        self.assertEqual(infer(f("db-san-01", "physical_server")).group, "Storage (SAN)")
        self.assertEqual(infer(f("x", "physical_server", role="web", tags=("db",))).group, "Database Servers")   # tags first

    def test_group_falls_back_to_rack_then_other_servers(self):
        self.assertEqual(infer(f("node-1", "physical_server", rack="Rack A01")).group, "Rack A01")
        self.assertEqual(infer(f("node-1", "physical_server")).group, "Other servers")
        self.assertEqual(infer(f("node-1", "physical_server", storage=True)).group, "Storage (SAN)")

    def test_group_and_colour_overrides(self):
        p = infer(f("web-1", "physical_server", group_override="  Edge   Cache ", colour_override="teal"))
        self.assertEqual((p.group, p.colour), ("Edge Cache", "teal"))
        self.assertEqual(infer(f("a", "physical_server", group_override="web servers")).colour, "orange")
        self.assertEqual(infer(f("a", "physical_server", colour_override="pink")).colour, "stone")   # not on the allow-list
        self.assertEqual(infer(f("a", "switch", group_override="Web")).group, "")                      # servers only

    def test_group_key_is_case_insensitive(self):
        self.assertEqual(tr.group_key(" Web SERVERS "), "web servers")


class ClassifyTests(SimpleTestCase):
    def test_graph_is_built_from_links_among_known_devices_only(self):
        devs = [f("a", id=1), f("b", "firewall", id=2), f("c", id=3)]
        out = classify(devs, [(1, 2), (3, 99)])
        self.assertEqual((out[1].tier, out[1].source), ("core", "graph"))
        self.assertEqual(out[3].tier, "other")      # its only neighbour is not in the list

    def test_one_colour_per_group(self):
        devs = [f("web-a", "physical_server", id=1), f("web-b", "physical_server", id=2, colour_override="purple")]
        out = classify(devs, [])
        self.assertEqual({out[1].colour, out[2].colour}, {"purple"})
