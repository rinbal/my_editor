# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Outbox: NIP-65 parsing + publish-set selection (pure-function tests)."""

from __future__ import annotations

import pytest

from nostr.outbox import RELAY_CAP, parse_relay_list, select_publish_relays


# --------------------------------------------------------------------------- #
# parse_relay_list                                                            #
# --------------------------------------------------------------------------- #

def test_parse_empty_event_returns_empty_lists():
    rl = parse_relay_list({"tags": []})
    assert rl.write == [] and rl.read == []
    assert rl.is_empty


def test_parse_omitted_marker_means_both():
    event = {"tags": [["r", "wss://relay.example"]]}
    rl = parse_relay_list(event)
    assert rl.write == ["wss://relay.example"]
    assert rl.read == ["wss://relay.example"]


def test_parse_respects_read_and_write_markers():
    event = {
        "tags": [
            ["r", "wss://both.example"],
            ["r", "wss://read.example", "read"],
            ["r", "wss://write.example", "write"],
        ]
    }
    rl = parse_relay_list(event)
    assert rl.write == ["wss://both.example", "wss://write.example"]
    assert rl.read == ["wss://both.example", "wss://read.example"]


def test_parse_unknown_marker_falls_back_to_both():
    # Defensive: a future or typo'd marker shouldn't drop the relay entirely.
    event = {"tags": [["r", "wss://x.example", "bogus"]]}
    rl = parse_relay_list(event)
    assert rl.write == ["wss://x.example"]
    assert rl.read == ["wss://x.example"]


def test_parse_ignores_non_r_tags():
    event = {
        "tags": [
            ["p", "deadbeef"],
            ["d", "myslug"],
            ["r", "wss://only.example"],
        ]
    }
    rl = parse_relay_list(event)
    assert rl.write == ["wss://only.example"]


def test_parse_skips_malformed_r_tags():
    event = {
        "tags": [
            ["r"],                       # no url
            ["r", ""],                    # blank url
            ["r", "wss://valid.example"],
        ]
    }
    rl = parse_relay_list(event)
    assert rl.write == ["wss://valid.example"]


# --------------------------------------------------------------------------- #
# select_publish_relays                                                       #
# --------------------------------------------------------------------------- #

BASE = ("wss://b1.example", "wss://b2.example", "wss://b3.example")


def test_select_with_no_user_relays_returns_base():
    out = select_publish_relays([], base=BASE, cap=10)
    assert out == list(BASE)


def test_select_appends_user_writes_after_base():
    user = ["wss://u1.example", "wss://u2.example"]
    out = select_publish_relays(user, base=BASE, cap=10)
    assert out == list(BASE) + user


def test_select_dedupes_overlap_case_insensitive():
    user = ["WSS://B1.EXAMPLE/", "wss://b2.example", "wss://unique.example"]
    out = select_publish_relays(user, base=BASE, cap=10)
    # The base entry wins (kept in its original casing/order); duplicates dropped.
    assert out == [
        "wss://b1.example",
        "wss://b2.example",
        "wss://b3.example",
        "wss://unique.example",
    ]


def test_select_strips_trailing_slash_in_output():
    out = select_publish_relays(["wss://x.example/"], base=("wss://b.example",), cap=10)
    assert out == ["wss://b.example", "wss://x.example"]


def test_select_respects_cap():
    user = [f"wss://u{i}.example" for i in range(20)]
    out = select_publish_relays(user, base=BASE, cap=5)
    assert len(out) == 5
    assert out == list(BASE) + user[:2]


def test_select_default_cap_is_module_constant():
    user = [f"wss://u{i}.example" for i in range(50)]
    out = select_publish_relays(user, base=BASE)
    assert len(out) == RELAY_CAP


def test_select_filters_empty_entries():
    user = ["", "   ", "wss://valid.example"]
    out = select_publish_relays(user, base=BASE, cap=10)
    assert out == list(BASE) + ["wss://valid.example"]
