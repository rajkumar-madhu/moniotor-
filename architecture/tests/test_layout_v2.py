"""The visual rules of the second pass: bands and node sizes, straight links with evenly spread attachment points,
rack cabinets, leaf above group, the cloud, and what is left out of the drawing."""
import re

from django.test import SimpleTestCase

from architecture import layout as lay

from .test_layout import fabric


def coords(d):
    return [float(v) for v in re.findall(r"-?\d+(?:\.\d+)?", d)]


class StraightLinkTests(SimpleTestCase):
    def setUp(self):
        self.res = lay.layout(*fabric(4, 4))

    def test_links_between_neighbouring_tiers_are_straight_lines(self):
        for p in self.res.paths:
            if p.kind in ("link", "bundle", "sym"):
                self.assertRegex(p.d, r"^M[-\d. ]+L[-\d. ]+$", p.key)
                self.assertNotIn("C", p.d)

    def test_only_a_link_over_a_neighbour_in_the_same_tier_is_curved(self):
        nodes = [lay.LNode(c, "core", c) for c in "abc"]
        res = lay.layout(nodes, [lay.LEdge("1", "a", "b"), lay.LEdge("2", "a", "c")])
        kinds = {p.key: (p.kind, "C" in p.d) for p in res.paths}
        self.assertEqual(kinds, {"1": ("link", False), "2": ("arc", True)})

    def test_a_full_mesh_attaches_every_link_at_its_own_point_along_the_edge(self):
        spines = [n for n in self.res.nodes.values() if n.tier == "spine"]
        for sp in spines:
            xs = sorted(p.x1 for p in self.res.paths if p.kind == "link" and p.a.startswith("s") and p.a == sp.id and p.y1 == sp.y + sp.h)
            self.assertEqual(len(xs), 4)
            self.assertEqual(len(set(xs)), 4)
            steps = {round(b - a, 1) for a, b in zip(xs, xs[1:])}
            self.assertEqual(len(steps), 1, "evenly spaced")
            self.assertGreater(steps.pop(), 20)                       # a lattice, not a pile on the centre
            self.assertGreaterEqual(min(xs), sp.x)
            self.assertLessEqual(max(xs), sp.x + sp.w)

    def test_attachment_order_follows_the_other_end_so_nothing_crosses_at_a_node(self):
        leaf_x = {n.id: n.x for n in self.res.nodes.values() if n.tier == "leaf"}
        for sp_id in ("s0", "s1", "s2", "s3"):
            mine = [p for p in self.res.paths if p.kind == "link" and p.a == sp_id and p.b in leaf_x]
            mine.sort(key=lambda p: p.x1)
            self.assertEqual([leaf_x[p.b] for p in mine], sorted(leaf_x[p.b] for p in mine))

    def test_links_enter_the_top_edge_and_leave_below_the_label(self):
        for p in self.res.paths:
            if p.kind != "link":
                continue
            a, b = self.res.nodes[p.a], self.res.nodes[p.b]
            up, down = (a, b) if a.y < b.y else (b, a)
            if abs(a.y - b.y) > 1:
                self.assertAlmostEqual(p.y1, up.y + up.h, 1)
                self.assertAlmostEqual(p.y2, down.y, 1)


class NodeSizeTests(SimpleTestCase):
    def test_four_wide_tiers_use_big_photo_first_faces(self):
        res = lay.layout(*fabric(4, 4))
        for tier in ("firewall", "core", "spine", "leaf"):
            for n in (p for p in res.nodes.values() if p.tier == tier):
                self.assertEqual((n.mode, n.w, n.fh), ("full", 208.0, 76.0), tier)
                self.assertGreater(n.h, n.fh)                         # the label sits under the face
        self.assertGreaterEqual(lay.SIZES["full"][0], 170)

    def test_routers_are_a_little_smaller_than_the_switches(self):
        res = lay.layout(*fabric(4, 4))
        wan = [p for p in res.nodes.values() if p.tier == "wan"][0]
        spine = [p for p in res.nodes.values() if p.tier == "spine"][0]
        self.assertLess(wan.w, spine.w)
        self.assertGreater(wan.w, spine.w * 0.7)

    def test_nodes_shrink_then_wrap_then_become_chips(self):
        def mode(n):
            nodes = [lay.LNode(f"n{i}", "spine", f"n{i:02d}") for i in range(n)]
            return {p.mode for p in lay.layout(nodes, []).nodes.values()}
        self.assertEqual(mode(4), {"full"})
        self.assertEqual(mode(6), {"medium"})
        self.assertEqual(mode(9), {"small"})
        self.assertEqual(mode(30), {"chip"})

    def test_the_cloud_sits_above_the_wan_row_and_is_wired_to_every_router(self):
        res = lay.layout(*fabric(4, 4))
        cx, cy, cw, ch = res.cloud
        wan = [p for p in res.nodes.values() if p.tier == "wan"]
        self.assertTrue(all(cy + ch < p.y for p in wan))
        sym = [p for p in res.paths if p.kind == "sym"]
        self.assertEqual(len(sym), len(wan))
        self.assertEqual(len({round(p.x1, 1) for p in sym}), len(sym))      # their own points on the cloud


class BandTests(SimpleTestCase):
    def test_bands_are_generously_padded_and_separated(self):
        res = lay.layout(*fabric(4, 4))
        by = {b.tier: b for b in res.bands}
        for tier in ("firewall", "core", "spine", "leaf"):
            b = by[tier]
            nodes = [p for p in res.nodes.values() if p.tier == tier]
            self.assertGreaterEqual(min(n.y for n in nodes) - b.y, lay.HEADER - 1)       # room for the label pill
            self.assertGreaterEqual(b.y + b.h - max(n.y + n.h for n in nodes), lay.BAND_BOTTOM - 1)
            self.assertGreaterEqual(min(n.x for n in nodes) - b.x, lay.BAND_PAD)
        gaps = [b2.y - (b1.y + b1.h) for b1, b2 in zip(res.bands[1:], res.bands[2:])]
        self.assertTrue(all(g >= 80 for g in gaps), gaps)               # room for a lattice of lines


class CabinetTests(SimpleTestCase):
    def test_a_group_is_one_rack_cabinet_with_its_servers_as_faces(self):
        res = lay.layout(*fabric(4, 4))
        for card in res.cards:
            self.assertEqual(len(card.cabs), 1)
            cab = card.cabs[0]
            self.assertEqual(len(card.tiles), 5)
            for (_id, x, y, w, h, mode) in card.tiles:
                self.assertEqual((w, mode), (lay.UNIT_W, "face1"))
                self.assertGreaterEqual(x, cab.x)
                self.assertLessEqual(x + w, cab.x + cab.w)
                self.assertGreaterEqual(y, cab.body_y)
                self.assertLessEqual(y + h, cab.body_y + cab.body_h)
            self.assertLess(cab.y + cab.h, card.pill_y)               # the name pill sits under the cabinet
            self.assertLess(card.pill_y, card.count_y)
            self.assertLessEqual(card.count_y, card.foot_y)
            self.assertLessEqual(card.foot_y, card.y + card.h)

    def test_two_u_servers_take_two_slots(self):
        nodes = [lay.LNode("a", "server", "a", group="g", unit=1), lay.LNode("b", "server", "b", group="g", unit=2)]
        card = lay.layout(nodes, [], [lay.LGroup("g", "G")]).cards[0]
        (ia, xa, ya, wa, ha, ma), (ib, xb, yb, wb, hb, mb) = card.tiles
        self.assertEqual((ma, mb), ("face1", "face2"))
        self.assertAlmostEqual(hb / ha, 2.0, 1)
        self.assertAlmostEqual(yb - ya, lay.SLOT_H[1], 1)

    def test_a_closed_group_is_capped_with_a_more_strip_and_a_count(self):
        nodes = [lay.LNode(f"v{i}", "server", f"v{i:02d}", group="g") for i in range(40)]
        card = lay.layout(nodes, [], [lay.LGroup("g", "G")]).cards[0]
        self.assertEqual(card.shown + card.hidden, 40)
        self.assertLess(card.shown, 16)
        self.assertEqual(len(card.cabs), 1)
        x, y, w, h = card.more
        last = max(t[2] + t[4] for t in card.tiles)
        self.assertGreaterEqual(y, last)                                  # the strip follows the last face

    def test_an_opened_group_uses_several_cabinets_side_by_side(self):
        nodes = [lay.LNode(f"v{i}", "server", f"v{i:03d}", group="g") for i in range(100)]
        card = lay.layout(nodes, [], [lay.LGroup("g", "G")], lay.Options(expand=frozenset({"g"}))).cards[0]
        self.assertEqual((card.shown, card.hidden, card.more), (100, 0, None))
        self.assertGreater(len(card.cabs), 3)
        self.assertGreater(len({c.x for c in card.cabs}), 1)

    def test_the_racks_of_one_row_stand_level(self):
        nodes, edges, groups = fabric(4, 4, per_group=5)
        nodes = [n for n in nodes if n.id != "v0_4"] + [lay.LNode("x", "server", "x", group="g0"), lay.LNode("y", "server", "y", group="g0")]
        res = lay.layout(nodes, edges, groups)
        self.assertEqual(len({(c.cabs[0].y, c.cabs[0].h) for c in res.cards}), 1)

    def test_leaf_i_sits_above_group_i_and_its_bundle_is_vertical(self):
        res = lay.layout(*fabric(4, 4))
        leaves = sorted((p for p in res.nodes.values() if p.tier == "leaf"), key=lambda p: p.x)
        cards = sorted(res.cards, key=lambda c: c.x)
        self.assertEqual(len(leaves), len(cards))
        for leaf, card in zip(leaves, cards):
            self.assertAlmostEqual(leaf.x + leaf.w / 2, card.x + card.w / 2, 1)
            strokes = [p for p in res.paths if p.kind == "bundle" and p.a == leaf.id]
            self.assertEqual(len(strokes), 5)
            for p in strokes:
                self.assertAlmostEqual(p.x1, p.x2, 1, f"{p.key} should run straight down")
                self.assertGreaterEqual(p.x2, card.cabs[0].x)
                self.assertLessEqual(p.x2, card.cabs[0].x + card.cabs[0].w)
            self.assertEqual(len({round(p.x1, 1) for p in strokes}), 5)          # parallel, evenly spaced
            xs = sorted(p.x1 for p in strokes)
            self.assertEqual(len({round(b - a, 1) for a, b in zip(xs, xs[1:])}), 1)

    def test_with_other_counts_the_groups_are_spread_evenly_under_the_leaves(self):
        nodes, edges, groups = fabric(4, 3, groups=4)
        res = lay.layout(nodes, edges, groups)
        centres = sorted(c.x + c.w / 2 for c in res.cards)
        gaps = {round(b - a, 0) for a, b in zip(centres, centres[1:])}
        self.assertEqual(len(gaps), 1)

    def test_bundle_strokes_end_on_the_cabinet_top(self):
        res = lay.layout(*fabric(4, 4))
        for p in (p for p in res.paths if p.kind == "bundle"):
            card = next(c for c in res.cards if c.key == p.group)
            self.assertAlmostEqual(p.y2, card.cab_top + 3, 1)
            self.assertAlmostEqual(card.cab_top, card.cabs[0].y, 1)


class OneLeafTests(SimpleTestCase):
    def test_one_spine_one_leaf_and_no_firewalls_still_lay_out(self):
        nodes = [lay.LNode("s", "spine", "s"), lay.LNode("l", "leaf", "l"), lay.LNode("v", "server", "v", group="g")]
        res = lay.layout(nodes, [lay.LEdge("1", "s", "l"), lay.LEdge("2", "l", "v")], [lay.LGroup("g", "G")])
        self.assertIsNone(res.cloud)
        self.assertEqual([b.tier for b in res.bands], ["spine", "leaf"])
        s, l = res.nodes["s"], res.nodes["l"]
        self.assertAlmostEqual(s.x + s.w / 2, l.x + l.w / 2, 1)
        link = [p for p in res.paths if p.key == "1"][0]
        self.assertAlmostEqual(link.x1, link.x2, 1)                      # a single link is vertical


class RibbonTests(SimpleTestCase):
    def test_the_ribbon_runs_between_the_tiers_not_over_a_wrapped_tier(self):
        res = lay.layout(*fabric(16, 48))
        rib = res.aggregates[0]
        spine = [b for b in res.bands if b.tier == "spine"][0]
        self.assertGreaterEqual(min(coords(rib.d)[1::2]), spine.y + spine.h - lay.BAND_BOTTOM - 1)


class RibbonGapTests(SimpleTestCase):
    def test_the_spine_leaf_ribbon_fills_the_gap_between_the_two_bands_only(self):
        res = lay.layout(*fabric(16, 48))
        rib = [a for a in res.aggregates if a.b_tier == "leaf"][0]
        spine = [b for b in res.bands if b.tier == "spine"][0]
        leaf = [b for b in res.bands if b.tier == "leaf"][0]
        ys = coords(rib.d)[1::2]
        self.assertAlmostEqual(min(ys), spine.y + spine.h, 1)
        self.assertAlmostEqual(max(ys), leaf.y, 1)

    def _crowded(self, per_group=30):
        nodes = [lay.LNode(f"s{i}", "spine", f"spine{i}") for i in range(2)]
        nodes += [lay.LNode(f"l{i}", "leaf", f"leaf{i:02d}") for i in range(8)]
        gs = [lay.LGroup(f"g{g}", f"Group {g}", c, 2) for g, c in enumerate(("orange", "green", "blue"))]
        edges, k = [], 0
        for g in range(3):
            for j in range(per_group):
                nodes.append(lay.LNode(f"v{g}_{j}", "server", f"srv{g}{j:02d}", group=f"g{g}"))
                k += 1
                edges.append(lay.LEdge(f"e{k}", f"l{j % 8}", f"v{g}_{j}"))
        for s in range(2):
            for l in range(8):
                k += 1
                edges.append(lay.LEdge(f"e{k}", f"s{s}", f"l{l}"))
        return nodes, edges, gs

    def test_a_crowded_leaf_row_gets_one_coloured_ribbon_per_group_and_lazy_strokes(self):
        res = lay.layout(*self._crowded())
        rib = [a for a in res.aggregates if a.b_tier == "server"]
        self.assertEqual(sorted(a.colour for a in rib), ["blue", "green", "orange"])
        bundles = [p for p in res.paths if p.kind == "bundle"]
        self.assertTrue(bundles)
        self.assertTrue(all(p.lazy for p in bundles), "every stroke is still there, shown on hover")
        self.assertEqual(sum(a.count for a in rib), len(bundles))
        leaf = [b for b in res.bands if b.tier == "leaf"][0]
        for a in rib:
            self.assertGreaterEqual(min(coords(a.d)[1::2]), leaf.y + leaf.h - 1)

    def test_a_calm_leaf_row_keeps_its_strokes(self):
        res = lay.layout(*self._crowded(per_group=4))
        self.assertFalse([a for a in res.aggregates if a.b_tier == "server"])
        self.assertFalse([p for p in res.paths if p.kind == "bundle" and p.lazy])
