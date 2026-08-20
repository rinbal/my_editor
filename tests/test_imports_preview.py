# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for ``nostr.imports.preview`` (pure helpers)."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nostr.imports.constants import MAX_LIMIT
from nostr.imports.preview import (
    SCOPE_PRESETS,
    count_words,
    default_scope_key,
    filter_items,
    read_minutes,
)
from tests.imports_fakes import make_item


class TestFilterItems(unittest.TestCase):
    def test_sorts_newest_first(self):
        items = [
            make_item("Old", published_at=100),
            make_item("New", published_at=300),
            make_item("Mid", published_at=200),
        ]
        out = filter_items(items, limit=10)
        self.assertEqual([i.title for i in out], ["New", "Mid", "Old"])

    def test_undated_items_keep_feed_order_and_sort_last(self):
        items = [
            make_item("A"),
            make_item("Dated", published_at=100),
            make_item("B"),
        ]
        out = filter_items(items, limit=10)
        self.assertEqual([i.title for i in out], ["Dated", "A", "B"])

    def test_limit_caps_after_sorting(self):
        items = [make_item(f"P{i}", published_at=i) for i in range(10)]
        out = filter_items(items, limit=3)
        self.assertEqual([i.title for i in out], ["P9", "P8", "P7"])

    def test_limit_clamped_to_bounds(self):
        items = [make_item(f"P{i}") for i in range(5)]
        self.assertEqual(len(filter_items(items, limit=0)), 1)
        self.assertEqual(len(filter_items(items, limit=10**9)), 5)

    def test_since_drops_older_items(self):
        items = [
            make_item("Old", published_at=100),
            make_item("New", published_at=300),
        ]
        out = filter_items(items, since=200, limit=10)
        self.assertEqual([i.title for i in out], ["New"])

    def test_since_drops_undated_items(self):
        # Documented: a since-scope means "new since", and an undated
        # item cannot prove it is new.
        items = [make_item("Undated"), make_item("New", published_at=300)]
        out = filter_items(items, since=200, limit=10)
        self.assertEqual([i.title for i in out], ["New"])

    def test_empty_input(self):
        self.assertEqual(filter_items([], limit=10), [])


class TestScopePresets(unittest.TestCase):
    def test_exactly_one_recommended(self):
        recommended = [p for p in SCOPE_PRESETS if p.recommended]
        self.assertEqual(len(recommended), 1)
        self.assertEqual(recommended[0].key, "newest25")

    def test_all_preset_caps_at_max_limit(self):
        by_key = {p.key: p for p in SCOPE_PRESETS}
        self.assertEqual(by_key["all"].limit, MAX_LIMIT)

    def test_default_scope_shrinks_for_small_feeds(self):
        self.assertEqual(default_scope_key(3), "newest10")
        self.assertEqual(default_scope_key(10), "newest10")
        self.assertEqual(default_scope_key(11), "newest25")
        self.assertEqual(default_scope_key(500), "newest25")


class TestStats(unittest.TestCase):
    def test_count_words_strips_tags(self):
        self.assertEqual(count_words("<p>one two <b>three</b></p>"), 3)

    def test_count_words_empty(self):
        self.assertEqual(count_words(""), 0)
        self.assertEqual(count_words(None), 0)

    def test_read_minutes_floors_at_one(self):
        self.assertEqual(read_minutes("<p>short body</p>"), 1)

    def test_read_minutes_zero_for_empty(self):
        self.assertEqual(read_minutes(""), 0)

    def test_read_minutes_scales_with_length(self):
        html = "<p>" + ("word " * 900) + "</p>"
        self.assertEqual(read_minutes(html), 4)


if __name__ == "__main__":
    unittest.main()
