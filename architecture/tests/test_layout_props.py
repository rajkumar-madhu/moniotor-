"""Property-style tests for architecture/layout.py: random graphs from fixed seeds must never crash or hang, must come
out byte-identical on a second run (and whatever order the input arrives in), and must never put two boxes on top of
each other. Plus crossing reduction, odd names and degenerate shapes."""
import itertools
import math
import random
import time

from django.test import SimpleTestCase

from architecture import layout as lay

NAMES = ["spine-01", "Leaf_7", "ノード-東京", "سويتش-الرئيسي", "a" * 80, "x", "😀-sw", "é" * 30, "", "  ", "A<b>&\"'", "z-10", "z-9"]
TIERS = list(lay.TIER_ORDER)


def random_graph(seed, max_nodes=90, bad_tiers=False):
    r = random.Random(seed)
    n = r.randint(0, max_nodes)
    group_keys = [f"g{i}" for i in range(r.randint(1, 7))]
    nodes = []
    for i in range(n):
        tier = r.choice(TIERS + (["bogus", ""] if bad_tiers else []))
        name = r.choice(NAMES) if r.random() < 0.5 else f"dev-{r.randint(0, 40)}"
        nodes.append(lay.LNode(f"d{i}", tier, name, group=r.choice(group_keys) if tier == "server" else "",
                               order=r.choice((0, 0, 0, 1, 5))))
    groups = [lay.LGroup(k, k.upper() * r.randint(1, 12), r.choice(("orange", "blue", "stone")), r.randint(0, 6))
              for k in group_keys if r.random() < 0.8]
    edges = []
    for j in range(r.randint(0, n * 3) if n else 0):
        a, b = r.randrange(n), r.randrange(n)
        if r.random() < 0.03:
            b = n + 50                                  # unknown end
        edges.append(lay.LEdge(f"e{j}", f"d{a}", f"d{b}"))
    return nodes, edges, groups


def snapshot(res):
    return repr((res.width, res.height, [vars(b) for b in res.bands], {k: vars(v) for k, v in sorted(res.nodes.items())},
                 [vars(c) for c in res.cards], [vars(p) for p in res.paths], [vars(a) for a in res.aggregates],
                 res.cloud, res.notes, res.ignored))


def boxes_overlap(a, b, eps=0.5):
    return a[0] < b[0] + b[2] - eps and b[0] < a[0] + a[2] - eps and a[1] < b[1] + b[3] - eps and b[1] < a[1] + a[3] - eps


def check_geometry(test, res, label=""):
    W, H = res.width, res.height
    for v in (W, H):
        test.assertTrue(math.isfinite(v), label)
    # nodes: finite, inside the canvas, no two drawn nodes overlap
    drawn = [p for p in res.nodes.values() if p.mode != "hidden"]
    for p in drawn:
        test.assertTrue(all(math.isfinite(v) for v in (p.x, p.y, p.w, p.h)), label)
        test.assertGreaterEqual(p.x, -0.01, label)
        test.assertLessEqual(p.x + p.w, W + 0.01, f"{label} {p.id} sticks out on the right")
        test.assertLessEqual(p.y + p.h, H + 0.01, f"{label} {p.id} sticks out at the bottom")
    boxes = sorted(((p.x, p.y, p.w, p.h, p.id) for p in drawn), key=lambda t: (t[1], t[0]))
    for i, a in enumerate(boxes):
        for b in boxes[i + 1:]:
            if b[1] >= a[1] + a[3]:
                break
            test.assertFalse(boxes_overlap(a, b), f"{label}: {a[4]} overlaps {b[4]}")
    # cards do not overlap each other, tiles sit inside their card and do not overlap the footer or the "+N more" tile
    cards = [(c.x, c.y, c.w, c.h) for c in res.cards]
    for a, b in itertools.combinations(cards, 2):
        test.assertFalse(boxes_overlap(a, b), f"{label}: cards overlap")
    for c in res.cards:
        test.assertLessEqual(c.x + c.w, W + 0.01, label)
        test.assertGreaterEqual(c.x, -0.01, label)
        for (_id, x, y, w, h, _m) in c.tiles + ([("more",) + tuple(c.more) + ("",)] if c.more else []):
            test.assertGreaterEqual(x, c.x - 0.01, f"{label}: tile left of its card")
            test.assertLessEqual(x + w, c.x + c.w + 0.01, f"{label}: tile right of its card ({c.key})")
            test.assertGreaterEqual(y, c.y, label)
            test.assertLessEqual(y + h, c.foot_y + 0.01 if c.foot_y else c.y + c.h, f"{label}: tile runs into the footer")
        test.assertEqual(c.shown + c.hidden, len(c.members), label)
    # bands: stacked, never overlapping, every node of a tier inside its band; cards below the bands above them
    for a, b in zip(res.bands, res.bands[1:]):
        test.assertLessEqual(a.y + a.h, b.y + 0.01, label)
    by_tier = {b.tier: b for b in res.bands}
    for p in drawn:
        if p.tier != "server":
            band = by_tier[p.tier]
            test.assertGreaterEqual(p.y, band.y - 0.01, label)
            test.assertLessEqual(p.y + p.h, band.y + band.h + 0.01, label)
    if res.cloud:
        cl = res.cloud
        test.assertGreaterEqual(cl[0], 0, label)
        test.assertLessEqual(cl[0] + cl[2], W + 0.01, label)
        for p in drawn:
            test.assertFalse(boxes_overlap(cl, (p.x, p.y, p.w, p.h)), f"{label}: cloud covers {p.id}")
    for path in res.paths:
        for v in (path.x1, path.y1, path.x2, path.y2):
            test.assertTrue(math.isfinite(v), label)
        test.assertNotIn("nan", path.d.lower(), label)
        test.assertNotIn("inf", path.d.lower(), label)


def crossings(res):
    """Crossings among the links that run between the same two rows (straight chords through the attachment points)."""
    segs = {}
    for p in res.paths:
        if p.kind in ("link", "bundle"):
            segs.setdefault((p.y1, p.y2), []).append((p.x1, p.x2))
    total = 0
    for s in segs.values():
        for (a1, b1), (a2, b2) in itertools.combinations(s, 2):
            if (a1 - a2) * (b1 - b2) < -1e-6:
                total += 1
    return total


class RandomGraphTests(SimpleTestCase):
    SEEDS = range(60)

    def test_random_graphs_never_crash_and_never_overlap(self):
        for seed in self.SEEDS:
            nodes, edges, groups = random_graph(seed, bad_tiers=seed % 3 == 0)
            for width in (1200.0, 760.0, 420.0):
                res = lay.layout(nodes, edges, groups, lay.Options(width=width))
                check_geometry(self, res, f"seed {seed} width {width}")

    def test_random_graphs_with_everything_expanded(self):
        expand = frozenset(lay.TIER_ORDER) | {f"g{i}" for i in range(8)}
        for seed in self.SEEDS:
            nodes, edges, groups = random_graph(seed, max_nodes=60)
            res = lay.layout(nodes, edges, groups, lay.Options(expand=expand))
            check_geometry(self, res, f"expanded seed {seed}")
            self.assertEqual(sum(c.hidden for c in res.cards), 0)

    def test_same_input_is_byte_identical(self):
        for seed in self.SEEDS:
            nodes, edges, groups = random_graph(seed)
            a = snapshot(lay.layout(nodes, edges, groups))
            b = snapshot(lay.layout(nodes, edges, groups))
            self.assertEqual(a, b, f"seed {seed}")

    def test_input_order_does_not_change_the_drawing(self):
        # the database may hand the rows over in any order: the drawing must not depend on it
        for seed in self.SEEDS:
            nodes, edges, groups = random_graph(seed)
            base = snapshot(lay.layout(nodes, edges, groups))
            r = random.Random(seed)
            n2, e2, g2 = list(nodes), list(edges), list(groups)
            r.shuffle(n2)
            r.shuffle(e2)
            r.shuffle(g2)
            self.assertEqual(base, snapshot(lay.layout(n2, e2, g2)), f"seed {seed}")

    def test_every_valid_link_gets_exactly_one_path(self):
        for seed in self.SEEDS:
            nodes, edges, groups = random_graph(seed, max_nodes=50)
            res = lay.layout(nodes, edges, groups, lay.Options(max_bundle=10 ** 6))
            tier = {n.id: (n.tier if n.tier in lay.TIER_ORDER else "other") for n in nodes}
            valid = [e for e in edges if e.a in tier and e.b in tier and e.a != e.b and
                     not (tier[e.a] == "server" and tier[e.b] == "server")]
            drawn = sorted(p.key for p in res.paths if p.kind != "sym")
            self.assertEqual(drawn, sorted(e.key for e in valid), f"seed {seed}")
            self.assertEqual(res.ignored, len([e for e in edges if e.a in tier and e.b in tier and e.a != e.b and
                                               tier[e.a] == tier[e.b] == "server"]), f"seed {seed}")

    def test_bundle_strokes_equal_the_link_count_up_to_the_cap(self):
        nodes = [lay.LNode("l1", "leaf", "l1")] + [lay.LNode(f"v{i}", "server", f"v{i:02d}", group="g") for i in range(40)]
        edges = [lay.LEdge(f"e{i}", "l1", f"v{i}") for i in range(40)] + [lay.LEdge("dup", "l1", "v3")]
        for cap in (3, 16, 41, 100):
            res = lay.layout(nodes, edges, [lay.LGroup("g", "G")], lay.Options(max_bundle=cap))
            strokes = [p for p in res.paths if p.kind == "bundle"]
            self.assertEqual(len(strokes), min(cap, 41))
            self.assertEqual(len({p.key for p in strokes}), len(strokes))
            if cap < 41:
                self.assertTrue(any("beyond" in n["text"] for n in res.notes), "the cap must be announced")
        # strokes of one bundle are parallel: distinct ends on both sides
        self.assertEqual(len({round(p.x1, 1) for p in strokes}), 41)


class CrossingTests(SimpleTestCase):
    @staticmethod
    def pods(npods, spp, lpp, seed):
        r = random.Random(seed)
        order = list(range(npods * (spp + lpp)))
        r.shuffle(order)
        it = iter(order)
        spines, leaves, edges = [], [], []
        for p in range(npods):
            spines += [(p, lay.LNode(f"s{p}_{i}", "spine", f"n{next(it):04d}")) for i in range(spp)]
            leaves += [(p, lay.LNode(f"l{p}_{i}", "leaf", f"n{next(it):04d}")) for i in range(lpp)]
        for (ps, s), (pl, l) in itertools.product(spines, leaves):
            if ps == pl:
                edges.append(lay.LEdge(f"{s.id}-{l.id}", s.id, l.id))
        return [n for _p, n in spines + leaves], edges

    def test_scrambled_pods_are_untangled(self):
        # K(2,4) pods cannot be drawn below 6 crossings each; a good order gets there, a name-sorted one does not
        for seed in range(5):
            nodes, edges = self.pods(3, 2, 4, seed)
            before = crossings(lay.layout(nodes, edges, [], lay.Options(sweeps=0)))
            after = crossings(lay.layout(nodes, edges, []))
            self.assertLess(after, before, f"seed {seed}")
            self.assertEqual(after, 3 * 6, f"seed {seed}: {after} crossings, the optimum is 18")

    def test_bigger_pods_improve_a_lot(self):
        for seed in range(3):
            nodes, edges = self.pods(6, 2, 8, seed)
            before = crossings(lay.layout(nodes, edges, [], lay.Options(sweeps=0)))
            after = crossings(lay.layout(nodes, edges, []))
            self.assertLess(after, before * 0.5, f"seed {seed}: {before} -> {after}")

    def test_scrambled_4x4_mesh_with_servers_is_no_worse_than_unsorted(self):
        from .test_layout import fabric
        nodes, edges, groups = fabric(4, 4)
        r = random.Random(3)
        names = list(range(len(nodes)))
        r.shuffle(names)
        for n, i in zip(nodes, names):
            n.name = f"n{i:03d}"
        before = crossings(lay.layout(nodes, edges, groups, lay.Options(sweeps=0)))
        after = crossings(lay.layout(nodes, edges, groups))
        self.assertLessEqual(after, before)

    def test_ordering_is_stable_for_a_symmetric_full_mesh(self):
        # nothing to gain in a full mesh: the barycentres tie, so the name order must survive untouched
        from .test_layout import fabric
        res = lay.layout(*fabric(4, 4))
        xs = [res.nodes[f"l{i}"].x for i in range(4)]
        self.assertEqual(xs, sorted(xs))


class OddShapeTests(SimpleTestCase):
    def test_leaf_to_leaf_spine_to_spine_and_cycles(self):
        nodes = [lay.LNode(f"s{i}", "spine", f"s{i}") for i in range(6)] + [lay.LNode(f"l{i}", "leaf", f"l{i}") for i in range(6)]
        edges = [lay.LEdge(f"ss{i}{j}", f"s{i}", f"s{j}") for i in range(6) for j in range(i + 1, 6)]
        edges += [lay.LEdge(f"ll{i}{j}", f"l{i}", f"l{j}") for i in range(6) for j in range(i + 1, 6)]
        edges += [lay.LEdge(f"sl{i}{j}", f"s{i}", f"l{j}") for i in range(6) for j in range(6)]
        res = lay.layout(nodes, edges, [])
        check_geometry(self, res)
        self.assertEqual(len([p for p in res.paths if p.kind != "sym"]), len(edges))
        self.assertTrue(all(p.kind in ("link", "arc") for p in res.paths))

    def test_a_cycle_through_four_tiers(self):
        tiers = ("core", "spine", "leaf", "firewall")
        nodes = [lay.LNode(t, t, t) for t in tiers]
        edges = [lay.LEdge(f"{a}-{b}", a, b) for a, b in zip(tiers, tiers[1:] + tiers[:1])]
        check_geometry(self, lay.layout(nodes, edges, []))

    def test_orphans_and_single_device_tiers(self):
        nodes = [lay.LNode("w", "wan", "w"), lay.LNode("s", "spine", "s"), lay.LNode("o", "other", "o"),
                 lay.LNode("v", "server", "v", group="g")]
        res = lay.layout(nodes, [], [lay.LGroup("g", "G")])
        check_geometry(self, res)
        self.assertEqual([b.tier for b in res.bands], ["wan", "spine", "other"])
        self.assertEqual(res.paths and [p.kind for p in res.paths], ["sym"])      # only the cloud line

    def test_every_tier_alone(self):
        for t in lay.TIER_ORDER:
            nodes = [lay.LNode("a", t, "a", group="g" if t == "server" else ""), lay.LNode("b", t, "b", group="g" if t == "server" else "")]
            res = lay.layout(nodes, [lay.LEdge("1", "a", "b")], [lay.LGroup("g", "G")])
            check_geometry(self, res, t)

    def test_an_unknown_tier_is_drawn_as_unclassified_and_linked_without_error(self):
        nodes = [lay.LNode("a", "bogus", "a"), lay.LNode("b", "alien", "b"), lay.LNode("c", "bogus", "c")]
        edges = [lay.LEdge("1", "a", "b"), lay.LEdge("2", "a", "c"), lay.LEdge("3", "b", "c")]
        res = lay.layout(nodes, edges, [])
        check_geometry(self, res)
        self.assertEqual([b.tier for b in res.bands], ["other"])
        self.assertEqual(len([p for p in res.paths if p.kind != "sym"]), 3)

    def test_unknown_tier_in_a_big_mesh_does_not_break_the_ribbons(self):
        nodes = [lay.LNode(f"a{i}", "bogus", f"a{i}") for i in range(3)] + [lay.LNode(f"b{i}", "leaf", f"b{i}") for i in range(3)]
        edges = [lay.LEdge(f"{i}-{j}", f"a{i}", f"b{j}") for i in range(3) for j in range(3)]
        res = lay.layout(nodes, edges, [], lay.Options(agg_limit=2))
        self.assertEqual(res.aggregates[0].count, 9)

    def test_unicode_rtl_and_hostile_names_do_not_move_anything(self):
        nodes = [lay.LNode(f"n{i}", "leaf", name) for i, name in enumerate(NAMES)]
        res = lay.layout(nodes, [], [])
        check_geometry(self, res)
        self.assertEqual(len(res.nodes), len(NAMES))

    def test_the_same_names_in_two_orders_give_the_same_drawing(self):
        a = [lay.LNode("1", "leaf", "Éa"), lay.LNode("2", "leaf", "éb"), lay.LNode("3", "leaf", "Ea")]
        self.assertEqual(snapshot(lay.layout(a, [])), snapshot(lay.layout(a[::-1], [])))

    def test_equal_names_break_ties_by_id_not_by_input_order(self):
        a = [lay.LNode(str(i), "leaf", "same") for i in range(10)]
        self.assertEqual(snapshot(lay.layout(a, [])), snapshot(lay.layout(a[::-1], [])))

    def test_four_hundred_servers_in_one_group(self):
        nodes = [lay.LNode("l1", "leaf", "leaf-1"), lay.LNode("l2", "leaf", "leaf-2")]
        nodes += [lay.LNode(f"v{i}", "server", f"srv-{i:03d}", group="g") for i in range(400)]
        edges = [lay.LEdge(f"e{i}", "l1" if i % 2 else "l2", f"v{i}") for i in range(400)]
        t = time.perf_counter()
        res = lay.layout(nodes, edges, [lay.LGroup("g", "Big", "blue", 4)])
        self.assertLess(time.perf_counter() - t, 1.0)
        check_geometry(self, res)
        card = res.cards[0]
        self.assertEqual((card.shown + card.hidden, len(card.members)), (400, 400))
        self.assertTrue(card.hidden)
        self.assertTrue(any(n.get("group") == "g" for n in res.notes))
        # the drill-down draws all of them, and says nothing was left out
        res = lay.layout(nodes, edges, [lay.LGroup("g", "Big", "blue", 4)], lay.Options(expand=frozenset({"g"})))
        check_geometry(self, res)
        self.assertEqual(res.cards[0].hidden, 0)
        self.assertEqual(len([n for n in res.nodes.values() if n.mode != "hidden"]), 402)
        card = res.cards[0]
        self.assertGreater(card.w, 800)                 # an opened big group takes the whole row, not a 3-column strip
        self.assertLess(card.h, 1500)

    def test_an_opened_big_group_gets_its_own_row_and_the_others_keep_theirs(self):
        nodes = [lay.LNode("l", "leaf", "l")]
        nodes += [lay.LNode(f"a{i}", "server", f"a{i:03d}", group="big") for i in range(120)]
        nodes += [lay.LNode(f"{g}{i}", "server", f"{g}{i}", group=g) for g in ("x", "y", "z") for i in range(3)]
        edges = [lay.LEdge(f"e{n.id}", "l", n.id) for n in nodes[1:]]
        groups = [lay.LGroup(k, k.upper()) for k in ("big", "x", "y", "z")]
        res = lay.layout(nodes, edges, groups, lay.Options(expand=frozenset({"big"})))
        check_geometry(self, res)
        cards = {c.key: c for c in res.cards}
        self.assertEqual(len({cards[k].y for k in "xyz"}), 1)
        self.assertGreater(cards["big"].y, cards["x"].y)
        self.assertEqual(cards["big"].hidden, 0)
        only = lay.layout(nodes[:121], edges[:120], groups[:1], lay.Options(expand=frozenset({"server"})))
        check_geometry(self, only)

    def test_hidden_servers_stay_in_the_node_map_so_a_link_can_find_them(self):
        nodes = [lay.LNode(f"v{i}", "server", f"v{i:03d}", group="g") for i in range(100)] + [lay.LNode("l", "leaf", "l")]
        res = lay.layout(nodes, [lay.LEdge("e", "l", "v99")], [lay.LGroup("g", "G")])
        self.assertEqual(res.nodes["v99"].mode, "hidden")
        self.assertEqual([p.kind for p in res.paths], ["bundle"])

    def test_large_synthetic_fabrics_are_fast_and_bounded(self):
        from .test_layout import fabric
        for spines, leaves, limit in ((16, 48, 1.0), (32, 96, 2.5)):
            data = fabric(spines, leaves, groups=6, per_group=40)
            t = time.perf_counter()
            res = lay.layout(*data)
            took = time.perf_counter() - t
            self.assertLess(took, limit, f"{spines}x{leaves}: {took:.2f}s")
            check_geometry(self, res, f"{spines}x{leaves}")
            # the drawing stays small enough for a browser
            self.assertLess(len(res.paths), 20000)
            self.assertLess(res.height, 6000)

    def test_a_dense_tier_with_many_same_tier_links_is_not_quadratic_in_edges_times_nodes(self):
        nodes = [lay.LNode(f"c{i}", "core", f"c{i:04d}") for i in range(400)]
        edges = [lay.LEdge(f"e{i}-{j}", f"c{i}", f"c{j}") for i in range(400) for j in range(i + 1, min(i + 26, 400))]
        t = time.perf_counter()
        res = lay.layout(nodes, edges, [])
        self.assertLess(time.perf_counter() - t, 3.0)
        self.assertEqual(len(res.paths), len(edges))


class LabelTests(SimpleTestCase):
    def test_labels_are_cut_by_width_not_by_length(self):
        from architecture.services import _clip, _tail, _cells_len
        self.assertEqual(_clip("abcdef", 10), "abcdef")
        self.assertEqual(_clip("abcdefghij", 6), "abcde…")
        wide = _clip("東京東京東京東京", 7)
        self.assertEqual(wide, "東京東…")
        self.assertLessEqual(_cells_len(wide), 7)
        self.assertEqual(_clip("a" * 4 + "😀" * 5, 8), "aaaa😀…")
        self.assertEqual(_tail("dc1-leaf-23", 6), "…eaf-23"[:1] + "af-23")
        self.assertEqual(_cells_len("e\u0301" * 10), 10)

    def test_a_cut_never_leaves_a_dangling_joiner_or_combining_mark(self):
        from architecture.services import _clip, _tail
        self.assertFalse(_clip("ab\u200dcdefgh", 4).replace("…", "").endswith("\u200d"))
        cut = _tail("abcde\u0301fghij", 6)
        self.assertFalse(cut[1:2] == "\u0301")
        for n in range(1, 12):
            for s in ("e\u0301" * 8, "👨\u200d👩\u200d👧" * 3, "سويتش-الرئيسي", "x" * 30):
                self.assertTrue(_clip(s, n).endswith("…") or _clip(s, n) == s)

    def test_bidi_overrides_and_control_characters_are_removed(self):
        from architecture.services import _clip, _tail
        self.assertEqual(_clip("a\u202eb\u202cc\x00d\x1be", 20), "abcde")
        self.assertEqual(_tail("a\u2066b\u2069", 20), "ab")
        self.assertEqual(_clip("می\u200cخواهم", 20), "می\u200cخواهم")      # ZWNJ is part of Persian words
        self.assertEqual(_clip(None, 5), "")


class BundleTests(SimpleTestCase):
    def test_bundles_into_one_card_do_not_run_into_each_other(self):
        nodes = [lay.LNode(f"l{i}", "leaf", f"leaf{i}") for i in range(4)]
        nodes += [lay.LNode(f"v{i}", "server", f"v{i:02d}", group="g") for i in range(20)]
        edges = [lay.LEdge(f"e{l}-{i}", f"l{l}", f"v{i}") for l in range(4) for i in range(20)]
        res = lay.layout(nodes, edges, [lay.LGroup("g", "G")], lay.Options(max_bundle=16))
        by_leaf = {}
        for p in res.paths:
            if p.kind == "bundle":
                by_leaf.setdefault(p.a, []).append(p.x2)
        spans = sorted((min(v), max(v)) for v in by_leaf.values())
        self.assertEqual(len(spans), 4)
        for (a0, a1), (b0, b1) in zip(spans, spans[1:]):
            self.assertLess(a1, b0, "two bundles overlap where they enter the card")
        card = res.cards[0]
        self.assertGreaterEqual(spans[0][0], card.x)
        self.assertLessEqual(spans[-1][1], card.x + card.w)
