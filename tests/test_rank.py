"""Reciprocal rank fusion.

One implementation, two callers: across tiers a cosine meets a recency position
meets a decayed hop score; within persistent a cosine meets a ts_rank_cd. Ranks
are commensurable where those scores are not.
"""

import unittest

from arteries import rank


def _fuse(*channels):
    return [(name, item["id"]) for name, item in
            rank.fuse(channels, key=lambda row: row["id"])]


class FuseTests(unittest.TestCase):
    def test_a_single_channel_keeps_its_order(self):
        rows = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        self.assertEqual([i for _n, i in _fuse(("one", rows, 1.0))],
                         ["a", "b", "c"])

    def test_a_row_in_both_channels_accumulates_both_terms(self):
        """The property that matters: agreement between channels outranks being
        top of one."""
        dense = [{"id": "top"}, {"id": "both"}]
        lexical = [{"id": "both"}, {"id": "other"}]
        self.assertEqual([i for _n, i in _fuse(("d", dense, 1.0), ("l", lexical, 1.0))][0],
                         "both")

    def test_a_row_in_only_one_channel_still_places(self):
        """An exact identifier match no embedding could find should not need a
        cosine to earn its place."""
        ids = [i for _n, i in _fuse(("d", [{"id": "a"}], 1.0),
                                    ("l", [{"id": "z"}], 1.0))]
        self.assertIn("z", ids)

    def test_weighting_orders_channels(self):
        heavy = [{"id": "heavy"}]
        light = [{"id": "light"}]
        self.assertEqual([i for _n, i in _fuse(("h", heavy, 1.0), ("l", light, 0.1))],
                         ["heavy", "light"])

    def test_the_first_channel_owns_the_label(self):
        """Channels are passed in priority order, so which copy survives is a
        decision rather than an accident of iteration."""
        rows = [{"id": "same"}]
        self.assertEqual(_fuse(("first", rows, 1.0), ("second", rows, 1.0))[0][0],
                         "first")

    def test_ties_resolve_the_same_way_every_run(self):
        a = [{"id": f"a{i}"} for i in range(5)]
        b = [{"id": f"b{i}"} for i in range(5)]
        first = _fuse(("a", a, 1.0), ("b", b, 1.0))
        for _ in range(5):
            self.assertEqual(_fuse(("a", a, 1.0), ("b", b, 1.0)), first)

    def test_an_empty_channel_changes_nothing(self):
        rows = [{"id": "a"}, {"id": "b"}]
        self.assertEqual(_fuse(("d", rows, 1.0), ("l", [], 1.0)),
                         _fuse(("d", rows, 1.0)))

    def test_no_channels_is_an_empty_result(self):
        self.assertEqual(rank.fuse([], key=lambda r: r), [])
