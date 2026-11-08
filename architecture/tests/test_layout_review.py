"""Review pass on the layout and tier modules: random graphs from fixed seeds must not crash, must come out
byte-identical, keep every stroke on the canvas, draw parallel links apart and each link key once; plus hostile
widths, collapsed tiers and tier inference that does not depend on the order the rows arrive in."""
import math
import random

from django.test import SimpleTestCase

from architecture import layout as lay
from architecture import tiers as tr

from .test_layout import fabric
from .test_layout_props import check_geometry, random_graph, snapshot


def drawn_paths(res):
    return [p for p in res.paths if p.kind != "sym"]


class ParallelLinkTests(SimpleTestCase):
    def test_parallel_links_inside_one_tier_are_drawn_apart(self):
        nodes = [lay.LNode(f"a{i}", "leaf", f"a{i}") for i in range(4)]
        edges = [lay.LEdge(f"n{i}", "a0", "a1") for i in range(3)] + [lay.LEdge(f"f{i}", "a0", "a3") for i in range(3)]
        res = lay.layout(nodes, edges)
        by_pair = {}
        for p in res.paths:
            by_pair.setdefault((p.a, p.b), []).append(p.d)
        for pair, ds in by_pair.items():
            self.assertEqual(len(ds), 3, pair)
            self.assertEqual(len(set(ds)), 3, f"{pair}: parallel links lie on top of each other")
        # the lanes stay on the node face
        face = res.nodes["a0"]
        for p in res.paths:
            if p.kind == "link":
                self.assertGreaterEqual(p.y1, face.y)
                self.assertLessEqual(p.y1, face.y + face.fh)

    def test_parallel_links_between_two_tiers_take_their_own_slots(self):
        nodes = [lay.LNode("s", "spine", "s"), lay.LNode("l", "leaf", "l")]
        edges = [lay.LEdge(f"e{i}", "s", "l") for i in range(4)]
        res = lay.layout(nodes, edges)
        self.assertEqual(len({p.d for p in res.paths}), 4)

    def test_a_link_key_is_drawn_once_even_when_two_rows_share_it(self):
        nodes = [lay.LNode(f"a{i}", "leaf", f"a{i}") for i in range(3)]
        edges = [lay.LEdge("same", "a0", "a1"), lay.LEdge("same", "a1", "a2"), lay.LEdge("other", "a0", "a2")]
        res = lay.layout(nodes, edges)
        self.assertEqual(sorted(p.key for p in drawn_paths(res)), ["other", "same"])
        again = lay.layout(nodes, list(reversed(edges)))
        self.assertEqual(snapshot(res), snapshot(again))


class WidthTests(SimpleTestCase):
    def test_hostile_widths_never_break_the_cards(self):
        nodes = [lay.LNode("l", "leaf", "l")] + [lay.LNode(f"v{i}", "server", f"v{i}", group="g") for i in range(30)]
        edges = [lay.LEdge(f"e{i}", "l", f"v{i}") for i in range(30)]
        for width in (0, -5, 1, 100.0, 200, 359.9, float("nan"), float("inf"), None, "wide"):
            res = lay.layout(nodes, edges, [lay.LGroup("g", "G")], lay.Options(width=width))
            check_geometry(self, res, f"width {width!r}")
            self.assertGreaterEqual(res.width, lay.MIN_WIDTH)
            self.assertTrue(math.isfinite(res.width))
            card = res.cards[0]
            self.assertGreaterEqual(card.w, card.cabs[0].w, f"width {width!r}: the cabinet is wider than its card")


class RandomGraphReviewTests(SimpleTestCase):
    SEEDS = range(80)

    def test_every_stroke_stays_on_the_canvas(self):
        for seed in self.SEEDS:
            nodes, edges, groups = random_graph(seed, max_nodes=70, bad_tiers=seed % 4 == 0)
            for width in (1200.0, 500.0):
                res = lay.layout(nodes, edges, groups, lay.Options(width=width))
                for p in res.paths:
                    for x, y in ((p.x1, p.y1), (p.x2, p.y2)):
                        self.assertTrue(-1 <= x <= res.width + 1 and -1 <= y <= res.height + 1,
                                        f"seed {seed} width {width}: {p.key} ends at {x},{y} on a {res.width}x{res.height} canvas")

    def test_bundle_strokes_equal_the_link_count_for_every_pair(self):
        for seed in self.SEEDS:
            nodes, edges, groups = random_graph(seed, max_nodes=60)
            res = lay.layout(nodes, edges, groups, lay.Options(max_bundle=10 ** 6))
            tier = {n.id: (n.tier if n.tier in lay.TIER_ORDER else "other") for n in nodes}
            grp = {n.id: n.group for n in nodes}
            want = {}
            seen = set()
            for e in sorted(edges, key=lambda e: e.key):
                if e.a not in tier or e.b not in tier or e.a == e.b or e.key in seen:
                    continue
                seen.add(e.key)
                sa, sb = tier[e.a] == "server", tier[e.b] == "server"
                if sa != sb:
                    sw, sv = (e.b, e.a) if sa else (e.a, e.b)
                    want[(sw, grp[sv])] = want.get((sw, grp[sv]), 0) + 1
            got = {}
            for p in res.paths:
                if p.kind == "bundle":
                    got[(p.a, p.group)] = got.get((p.a, p.group), 0) + 1
            self.assertEqual(got, want, f"seed {seed}")

    def test_the_drawing_does_not_depend_on_width_type_or_repeat_calls(self):
        for seed in self.SEEDS:
            nodes, edges, groups = random_graph(seed, max_nodes=40)
            a = snapshot(lay.layout(nodes, edges, groups, lay.Options(width=900.0)))
            b = snapshot(lay.layout(nodes, edges, groups, lay.Options(width=900)))
            self.assertEqual(a, b, f"seed {seed}")

    def test_nodes_do_not_overflow_their_labels(self):
        for seed in self.SEEDS:
            nodes, edges, groups = random_graph(seed, max_nodes=70)
            res = lay.layout(nodes, edges, groups)
            for p in res.nodes.values():
                if p.mode == "hidden":
                    continue
                size = 10 if p.mode in ("chip", "face1", "face2") else 11
                self.assertLessEqual(p.chars * size * 0.56, p.w, f"seed {seed}: {p.id} label wider than its node")


class CollapseTests(SimpleTestCase):
    def test_a_big_tier_is_chips_with_a_visible_message_and_the_drill_down_draws_them_all(self):
        data = fabric(2, 40)
        res = lay.layout(*data)
        leaf = [b for b in res.bands if b.tier == "leaf"][0]
        self.assertTrue(leaf.collapsed)
        self.assertEqual(sum(1 for p in res.nodes.values() if p.tier == "leaf"), 40)       # nothing dropped
        self.assertTrue(any(n.get("tier") == "leaf" and "40" in n["text"] for n in res.notes))
        res = lay.layout(*data, lay.Options(expand=frozenset({"leaf"})))
        leaf = [b for b in res.bands if b.tier == "leaf"][0]
        self.assertFalse(leaf.collapsed)
        self.assertTrue(leaf.expanded)
        self.assertFalse(any(n.get("tier") == "leaf" for n in res.notes))
        check_geometry(self, res)

    def test_exactly_at_the_limit_is_not_collapsed(self):
        res = lay.layout(*fabric(2, 24))
        self.assertFalse([b for b in res.bands if b.tier == "leaf"][0].collapsed)
        res = lay.layout(*fabric(2, 25))
        self.assertTrue([b for b in res.bands if b.tier == "leaf"][0].collapsed)

    def test_single_device_tiers_and_empty_groups_list(self):
        nodes = [lay.LNode("s", "spine", "s"), lay.LNode("l", "leaf", "l"), lay.LNode("v", "server", "v", group="")]
        res = lay.layout(nodes, [lay.LEdge("1", "s", "l"), lay.LEdge("2", "l", "v")], [])
        check_geometry(self, res)
        self.assertEqual(len(drawn_paths(res)), 2)
        self.assertEqual(res.cards[0].label, "Servers")


class TierReviewTests(SimpleTestCase):
    @staticmethod
    def facts(seed, n=60):
        r = random.Random(seed)
        words = ["spine", "leaf", "core", "tor", "web", "db", "app", "san", "", "x", "ノード", "سويتش", "SpineA", "BorderLeaf", "a" * 90]
        types = ["switch"] * 5 + ["router", "firewall", "physical_server", "virtual_machine", "load_balancer", "wireless", "weird"]
        out = []
        for i in range(n):
            out.append(tr.Facts(id=i, name=f"{r.choice(words)}-{r.randint(0, 9)}", device_type=r.choice(types),
                                role=r.choice(words), tags=tuple(r.choice(words) for _ in range(r.randint(0, 2))),
                                rack=r.choice(["", "", "R1", "R2"]), storage=r.random() < 0.1,
                                tier_override=r.choice(["", "", "", "leaf", "bogus"]),
                                group_override=r.choice(["", "", "Edge"]), colour_override=r.choice(["", "teal", "pink"])))
        pairs = [(r.randrange(n + 3), r.randrange(n + 3)) for _ in range(n * 2)]
        return out, pairs

    def test_classification_is_total_deterministic_and_independent_of_input_order(self):
        for seed in range(60):
            facts, pairs = self.facts(seed)
            base = tr.classify(facts, pairs)
            self.assertEqual(set(base), {f.id for f in facts})
            for p in base.values():
                self.assertIn(p.tier, tr.TIERS)
                self.assertTrue(p.reason, "every placement must be explainable")
                if p.tier == tr.SERVER:
                    self.assertIn(p.colour, tr.COLOURS)
                    self.assertTrue(p.group)
                else:
                    self.assertEqual((p.group, p.colour), ("", ""))
            r = random.Random(seed)
            f2, p2 = list(facts), [(b, a) for a, b in pairs]
            r.shuffle(f2)
            r.shuffle(p2)
            self.assertEqual(base, tr.classify(f2, p2), f"seed {seed}")

    def test_a_graph_placement_always_says_why(self):
        for seed in range(30):
            facts, pairs = self.facts(seed)
            for p in tr.classify(facts, pairs).values():
                if p.source == "graph":
                    self.assertTrue(p.weak)
                    self.assertRegex(p.reason, r"linked to")

    def test_links_to_devices_that_are_filtered_out_or_to_itself_do_not_count(self):
        sw = tr.Facts(1, "sw", "switch")
        out = tr.classify([sw], [(1, 1), (1, 99), (99, 1)])
        self.assertEqual((out[1].tier, out[1].source), ("other", "default"))
        fw = tr.Facts(2, "fw", "firewall")
        self.assertEqual(tr.classify([sw, fw], [(1, 2), (2, 1)])[1].tier, "core")

    def test_a_spine_to_spine_or_leaf_to_leaf_mesh_does_not_hang(self):
        facts = [tr.Facts(i, f"sw{i}", "switch") for i in range(120)]
        pairs = [(i, j) for i in range(120) for j in range(i + 1, 120)]
        out = tr.classify(facts, pairs)
        self.assertEqual({p.tier for p in out.values()}, {"spine"})
