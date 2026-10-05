# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Outbox: NIP-65 parsing + publish-set selection (pure-function tests)."""

from __future__ import annotations

import pytest

from nostr.outbox import defaults
from nostr.outbox.policy import LookupState, RelayList, parse_relay_list, plan_publish


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
# Where a public event goes (plan_publish)                                    #
# --------------------------------------------------------------------------- #
#
# The selector this replaced put the built-in relays first and the
# author's own write relays after them, where the cap cut them off. The
# author's outbox is where their readers look, so it leads.

def _list(write=(), read=()):
    return RelayList(write=list(write), read=list(read), state=LookupState.FOUND)


def test_the_authors_write_relays_come_first_in_their_own_order():
    user = ["wss://u1.example", "wss://u2.example", "wss://u3.example"]
    assert list(plan_publish(_list(write=user)).author) == user


def test_read_only_relays_are_not_where_the_author_publishes():
    plan = plan_publish(_list(write=["wss://w1.example", "wss://w2.example"],
                              read=["wss://inbox.example"]))
    assert "wss://inbox.example" not in plan.targets


def test_no_known_list_publishes_to_the_fallback_relays():
    assert list(plan_publish(RelayList()).author) == list(defaults.FALLBACK_RELAYS[:3])


def test_a_single_write_relay_is_topped_up_behind_it():
    plan = plan_publish(_list(write=["wss://only.example"]))
    assert plan.author[0] == "wss://only.example"
    assert len(plan.author) >= defaults.MIN_WRITE_TARGETS


def test_duplicates_collapse_ignoring_case_and_trailing_slash():
    plan = plan_publish(_list(write=["WSS://A.EXAMPLE/", "wss://a.example",
                                     "wss://b.example"]))
    assert list(plan.author) == ["wss://a.example", "wss://b.example"]


def test_a_sprawling_list_is_capped_without_losing_its_head():
    user = [f"wss://u{i}.example" for i in range(20)]
    plan = plan_publish(_list(write=user))
    assert list(plan.author) == user[:defaults.WRITE_CAP]


def test_junk_entries_are_dropped():
    plan = plan_publish(_list(write=["", "   ", "https://not-a-relay.example",
                                     "wss://valid.example", "wss://two.example"]))
    assert list(plan.author) == ["wss://valid.example", "wss://two.example"]


def test_mentioned_people_are_reached_at_their_read_relays_after_the_author():
    alice = "a" * 64
    plan = plan_publish(_list(write=["wss://me.example", "wss://me2.example"]),
                        mentioned={alice: _list(read=["wss://alice.example"])})
    assert plan.targets == ["wss://me.example", "wss://me2.example",
                            "wss://alice.example"]


# --------------------------------------------------------------------------- #
# Adding a relay to a published list, without destroying it
# --------------------------------------------------------------------------- #

from nostr.outbox import relay_list_tags_adding

E21 = "wss://nostr.einundzwanzig.space"


def _event(*tags):
    return {"kind": 10002, "tags": [list(t) for t in tags]}


def test_an_unread_list_refuses_rather_than_replacing_it():
    # A kind 10002 is replaceable. Publishing one built from nothing does
    # not add a relay, it wipes the user's list and scatters their readers.
    assert relay_list_tags_adding(None, E21) is None
    assert relay_list_tags_adding("not an event", E21) is None


def test_the_relay_is_appended_as_a_write_relay():
    tags = relay_list_tags_adding(_event(["r", "wss://a.example"]), E21)
    assert tags == [["r", "wss://a.example"], ["r", E21, "write"]]


def test_every_existing_entry_survives_with_its_marker():
    existing = _event(
        ["r", "wss://a.example", "read"],
        ["r", "wss://b.example", "write"],
        ["r", "wss://c.example"],
    )
    tags = relay_list_tags_adding(existing, E21)
    assert tags[:3] == existing["tags"]


def test_unrelated_tags_are_carried_through_untouched():
    # A relay list may carry tags this app has never heard of, and
    # dropping them would be editing the user's event.
    existing = _event(["r", "wss://a.example"], ["alt", "my relays"], ["client", "x"])
    tags = relay_list_tags_adding(existing, E21)
    assert ["alt", "my relays"] in tags and ["client", "x"] in tags


def test_an_already_listed_relay_is_a_no_op():
    # Nothing to do means never asking the signer to approve nothing.
    assert relay_list_tags_adding(_event(["r", E21]), E21) is None
    assert relay_list_tags_adding(_event(["r", E21 + "/"]), E21) is None
    assert relay_list_tags_adding(_event(["r", E21.upper()]), E21) is None


def test_a_read_only_entry_is_left_alone_rather_than_promoted():
    # Promoting read to write would rewrite a choice the user made.
    assert relay_list_tags_adding(_event(["r", E21, "read"]), E21) is None


def test_an_empty_but_real_list_is_still_safe_to_add_to():
    # An author who published an empty list has still published one, so
    # there is nothing to lose by appending.
    assert relay_list_tags_adding(_event(), E21) == [["r", E21, "write"]]


def test_a_junk_url_is_refused():
    assert relay_list_tags_adding(_event(["r", "wss://a.example"]), "") is None
    assert relay_list_tags_adding(_event(["r", "wss://a.example"]), "   ") is None
