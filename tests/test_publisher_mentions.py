# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Mention handling in build_note / build_article — pure-function tests."""

from __future__ import annotations

import pytest

from nostr.bech32 import encode_npub, encode_nprofile
from nostr.publisher import (
    build_article,
    build_note,
    extract_inline_mentions,
)


# Author key for the unsigned events
AUTHOR_PK = "79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"

# Three distinct test recipients
ALICE_PK = "01" * 32
BOB_PK   = "02" * 32
CAROL_PK = "03" * 32


def _p_tags(event: dict) -> list[list[str]]:
    return [t for t in event["tags"] if t and t[0] == "p"]


# --------------------------------------------------------------------------- #
# extract_inline_mentions                                                     #
# --------------------------------------------------------------------------- #

def test_extract_finds_npub_uri():
    npub = encode_npub(ALICE_PK)
    found = extract_inline_mentions(f"Hello nostr:{npub} how are you")
    assert found == [(ALICE_PK, "")]


def test_extract_finds_nprofile_uri_with_relay_hint():
    nprofile = encode_nprofile(ALICE_PK, ["wss://r.example"])
    found = extract_inline_mentions(f"cc nostr:{nprofile}")
    assert found == [(ALICE_PK, "wss://r.example")]


def test_extract_preserves_document_order():
    a = encode_npub(ALICE_PK)
    b = encode_npub(BOB_PK)
    found = extract_inline_mentions(f"first nostr:{b} then nostr:{a}")
    assert found == [(BOB_PK, ""), (ALICE_PK, "")]


def test_extract_silently_skips_garbage():
    """A near-miss URI (bad checksum) is left alone, not raised."""
    bad = "nostr:npub1invalidcharacters00000000000000000000000"
    found = extract_inline_mentions(f"see {bad} ok?")
    assert found == []


def test_extract_returns_empty_on_no_uris():
    assert extract_inline_mentions("just plain text, no mentions here") == []


# --------------------------------------------------------------------------- #
# build_note + mentions                                                       #
# --------------------------------------------------------------------------- #

def test_build_note_no_mentions_unchanged_body():
    event = build_note("hello", AUTHOR_PK)
    assert event["content"] == "hello"
    assert _p_tags(event) == []


def test_build_note_appends_chip_mentions_as_uris():
    event = build_note(
        "hello",
        AUTHOR_PK,
        mentions=[(ALICE_PK, "wss://r.example"), (BOB_PK, "")],
    )
    # Body has the original prose, then a blank line, then both URIs
    assert event["content"].startswith("hello\n\nnostr:nprofile1")
    assert "nostr:nprofile1" in event["content"]
    # One p-tag per mention, in order, with relay hint preserved
    assert _p_tags(event) == [
        ["p", ALICE_PK, "wss://r.example"],
        ["p", BOB_PK],
    ]


def test_build_note_dedupes_inline_vs_chip():
    """A chip mention whose pubkey already appears inline must NOT cause a
    second URI to be appended, but the p-tag is still emitted once."""
    nprofile = encode_nprofile(ALICE_PK, ["wss://inline.example"])
    body = f"As nostr:{nprofile} pointed out, this is great."
    event = build_note(
        body,
        AUTHOR_PK,
        mentions=[(ALICE_PK, "wss://chip.example")],
    )
    # Body is unchanged — no duplicate URI appended.
    assert event["content"] == body
    # Exactly one p-tag for Alice; inline relay hint wins (first seen).
    assert _p_tags(event) == [["p", ALICE_PK, "wss://inline.example"]]


def test_build_note_inline_only_still_emits_p_tag():
    """Manual paste of a URI in the body must also produce a p-tag — even
    with no chip mentions at all."""
    npub = encode_npub(BOB_PK)
    event = build_note(f"shout-out to nostr:{npub}", AUTHOR_PK)
    assert _p_tags(event) == [["p", BOB_PK]]


def test_build_note_empty_body_with_chip_mentions():
    """If the user has only mentions and no prose, the body is just the
    URI line (no leading blank line)."""
    event = build_note("", AUTHOR_PK, mentions=[(ALICE_PK, "")])
    assert event["content"].startswith("nostr:nprofile1")
    assert not event["content"].startswith("\n")


def test_build_note_dedupes_duplicate_chip_picks():
    event = build_note(
        "hi",
        AUTHOR_PK,
        mentions=[(ALICE_PK, "wss://a"), (ALICE_PK, "wss://b")],
    )
    # Only one URI appended, only one p-tag — first-seen hint wins.
    nprofile_count = event["content"].count("nostr:nprofile1")
    assert nprofile_count == 1
    assert _p_tags(event) == [["p", ALICE_PK, "wss://a"]]


# --------------------------------------------------------------------------- #
# build_article + mentions                                                    #
# --------------------------------------------------------------------------- #

def test_build_article_p_tags_appear_after_d_tag_before_metadata():
    """Tag order is cosmetic per NIP-01 but matters for readability; verify
    we put p-tags right after the d-tag so the article's identity stays
    obvious at the top of the tag list."""
    event = build_article(
        "body text",
        AUTHOR_PK,
        slug="hello",
        title="Hello",
        mentions=[(ALICE_PK, "")],
    )
    keys = [t[0] for t in event["tags"]]
    # Expect: client, d, p, title  (no other tags in this test)
    assert keys == ["client", "d", "p", "title"]


def test_build_article_dedupes_inline_vs_chip():
    nprofile = encode_nprofile(CAROL_PK, ["wss://inline.example"])
    body = f"As nostr:{nprofile} once wrote, this matters."
    event = build_article(
        body,
        AUTHOR_PK,
        slug="x",
        mentions=[(CAROL_PK, "wss://chip.example")],
    )
    assert event["content"] == body  # no duplicate appended
    assert _p_tags(event) == [["p", CAROL_PK, "wss://inline.example"]]


def test_build_article_no_mentions_no_p_tags():
    event = build_article("body", AUTHOR_PK, slug="x")
    assert _p_tags(event) == []
