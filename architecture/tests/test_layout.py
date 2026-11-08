import time

from django.test import SimpleTestCase

from architecture import layout as lay


def fabric(spines, leaves, groups=4, per_group=5):
    nodes, edges, k = [], [], 0
    nodes += [lay.LNode(f"r{i}", "wan", f"rtr{i}") for i in range(2)]
    nodes += [lay.LNode(f"f{i}", "firewall", f"fw{i}") for i in range(2)]
    nodes += [lay.LNode(f"c{i}", "core", f"core{i}") for i in range(2)]
    nodes += [lay.LNode(f"s{i}", "spine", f"spine{i:02d}") for i in range(spines)]
    nodes += [lay.LNode(f"l{i}", "leaf", f"leaf{i:02d}") for i in range(leaves)]
    gs = [lay.LGroup(f"g{g}", f"Group {g}", "blue", 2) for g in range(groups)]
    for g in range(groups):
        nodes += [lay.LNode(f"v{g}_{j}", "server", f"srv{g}{j}", group=f"g{g}") for j in range(per_group)]

    def E(a, b):
        nonlocal k
        k += 1
        edges.append(lay.LEdge(f"e{k}", a, b))
    for i in range(2):
        E(f"r{i}", f"f{i}")
    for f in range(2):
        for c in range(2):
            E(f"f{f}", f"c{c}")
    for c in range(2):
        for s in range(spines):
            E(f"c{c}", f"s{s}")
    for s in range(spines):
        for l in range(leaves):
            E(f"s{s}", f"l{l}")
    for g in range(groups):
        for j in range(per_group):
            E(f"l{g % leaves}", f"v{g}_{j}")
    return nodes, edges, gs


class LayoutTests(SimpleTestCase):
    def test_empty(self):
        r = lay.layout([], [])
        self.assertEqual((r.bands, r.paths, r.cards, r.cloud), ([], [], [], None))

    def test_tiers_stack_top_to_bottom_and_nodes_stay_inside(self):
        r = lay.layout(*fabric(4, 4))
        order = [b.tier for b in r.bands]
        self.assertEqual(order, ["wan", "firewall", "core", "spine", "leaf"])
        ys = [b.y for b in r.bands]
        self.assertEqual(ys, sorted(ys))
        for p in r.nodes.values():
            self.assertGreaterEqual(p.x, 0)
            self.assertLessEqual(p.x + p.w, r.width)
            self.assertLessEqual(p.y + p.h, r.height)
        self.assertEqual(len(r.cards), 4)
        self.assertGreater(r.cards[0].y, [b for b in r.bands if b.tier == "leaf"][0].y)

    def test_uncabled_group_goes_last_and_cabled_groups_share_the_first_row(self):
        nodes, edges, groups = fabric(4, 4)
        nodes.append(lay.LNode("x1", "server", "lonely", group="aaa"))   # sorts first by name, has no link
        groups.append(lay.LGroup("aaa", "Aaa unlinked", "stone", 2))
        r = lay.layout(nodes, edges, groups)
        cards = {c.key: c for c in r.cards}
        self.assertEqual(len(cards), 5)
        top = {c.y for c in r.cards if c.key != "aaa"}
        self.assertEqual(len(top), 1)                       # the four cabled groups sit in one row
        self.assertGreater(cards["aaa"].y, max(top))        # the uncabled one wraps below, nothing crosses it

    def test_one_path_per_link_and_bundle_strokes_match_link_count(self):
        nodes, edges, groups = fabric(4, 4)
        r = lay.layout(nodes, edges, groups)
        keys = [p.key for p in r.paths if p.kind != "sym"]
        self.assertEqual(sorted(keys), sorted(e.key for e in edges))
        bundles = [p for p in r.paths if p.kind == "bundle"]
        self.assertEqual(len(bundles), 4 * 5)
        self.assertEqual(len({p.x2 for p in bundles if p.group == "g0"}), 5)   # parallel, not stacked
        self.assertEqual(len({p.key for p in r.paths}), len(r.paths))

    def test_deterministic(self):
        a = lay.layout(*fabric(4, 4))
        b = lay.layout(*fabric(4, 4))
        self.assertEqual([(p.key, p.d) for p in a.paths], [(p.key, p.d) for p in b.paths])
        self.assertEqual({k: (v.x, v.y) for k, v in a.nodes.items()}, {k: (v.x, v.y) for k, v in b.nodes.items()})

    def test_barycentre_untangles_crossed_leaves(self):
        nodes = [lay.LNode("a", "spine", "a"), lay.LNode("b", "spine", "b"),
                 lay.LNode("x", "leaf", "x"), lay.LNode("y", "leaf", "y")]
        # name order would put x under a and y under b, but x links to b and y to a: the sweep must swap them
        edges = [lay.LEdge("1", "a", "y"), lay.LEdge("2", "b", "x")]
        r = lay.layout(nodes, edges)
        self.assertLess(r.nodes["y"].x, r.nodes["x"].x)

    def test_sort_order_breaks_ties(self):
        nodes = [lay.LNode("a", "leaf", "a", order=5), lay.LNode("b", "leaf", "b", order=1)]
        r = lay.layout(nodes, [])
        self.assertLess(r.nodes["b"].x, r.nodes["a"].x)

    def test_large_tier_collapses_to_chips_and_says_so(self):
        r = lay.layout(*fabric(8, 32))
        leaf = [b for b in r.bands if b.tier == "leaf"][0]
        self.assertTrue(leaf.collapsed)
        self.assertEqual(leaf.count, 32)
        self.assertEqual(len([n for n in r.nodes.values() if n.tier == "leaf"]), 32)    # none dropped
        self.assertTrue(any(n.get("tier") == "leaf" for n in r.notes))
        self.assertTrue(all(n.mode == "chip" for n in r.nodes.values() if n.tier == "leaf"))

    def test_expand_draws_the_tier_at_full_size(self):
        r = lay.layout(*fabric(8, 32), options=lay.Options(expand=frozenset({"leaf"})))
        leaf = [b for b in r.bands if b.tier == "leaf"][0]
        self.assertFalse(leaf.collapsed)
        self.assertTrue(all(n.mode == "small" for n in r.nodes.values() if n.tier == "leaf"))

    def test_big_mesh_is_aggregated_but_every_link_has_a_path(self):
        nodes, edges, groups = fabric(16, 48)
        r = lay.layout(nodes, edges, groups)
        self.assertEqual(len(r.aggregates), 1)
        self.assertEqual(r.aggregates[0].count, 16 * 48)
        lazy = [p for p in r.paths if p.lazy]
        self.assertEqual(len(lazy), 16 * 48)
        self.assertEqual(len([p for p in r.paths if p.kind != "sym"]), len(edges))

    def test_same_tier_links_are_arcs_or_straight(self):
        nodes = [lay.LNode("a", "core", "a"), lay.LNode("b", "core", "b"), lay.LNode("c", "core", "c")]
        r = lay.layout(nodes, [lay.LEdge("1", "a", "b"), lay.LEdge("2", "a", "c")])
        kinds = {p.key: p.kind for p in r.paths}
        self.assertEqual(kinds, {"1": "link", "2": "arc"})

    def test_server_to_server_links_are_counted_not_drawn(self):
        nodes = [lay.LNode("a", "server", "a", group="g"), lay.LNode("b", "server", "b", group="g")]
        r = lay.layout(nodes, [lay.LEdge("1", "a", "b")], [lay.LGroup("g", "G")])
        self.assertEqual((r.ignored, len(r.paths)), (1, 0))

    def test_unknown_edge_ends_are_ignored(self):
        r = lay.layout([lay.LNode("a", "leaf", "a")], [lay.LEdge("1", "a", "zzz"), lay.LEdge("2", "a", "a")])
        self.assertEqual(r.paths, [])

    def test_many_servers_in_a_group_are_not_silently_dropped(self):
        nodes = [lay.LNode(f"v{i}", "server", f"v{i:02d}", group="g") for i in range(40)]
        r = lay.layout(nodes, [], [lay.LGroup("g", "G")])
        card = r.cards[0]
        self.assertEqual(len(card.members), 40)
        self.assertEqual(card.shown + card.hidden, 40)
        self.assertTrue(card.hidden > 0 and card.more)
        self.assertTrue(any(n.get("group") == "g" for n in r.notes))
        r2 = lay.layout(nodes, [], [lay.LGroup("g", "G")], lay.Options(expand=frozenset({"g"})))
        self.assertEqual(r2.cards[0].hidden, 0)

    def test_unclassified_band_comes_last(self):
        nodes = [lay.LNode("l", "leaf", "l"), lay.LNode("v", "server", "v", group="g"), lay.LNode("o", "other", "o")]
        r = lay.layout(nodes, [], [lay.LGroup("g", "G")])
        self.assertEqual([b.tier for b in r.bands], ["leaf", "other"])
        self.assertGreater(r.nodes["o"].y, r.cards[0].y)

    def test_timing_8x32_and_16x48(self):
        for spines, leaves, limit in ((8, 32, 0.2), (16, 48, 1.0)):
            data = fabric(spines, leaves)
            best = min(self._time(data) for _ in range(3))
            self.assertLess(best, limit, f"{spines}x{leaves} took {best:.3f}s")

    @staticmethod
    def _time(data):
        t = time.perf_counter()
        lay.layout(*data)
        return time.perf_counter() - t
