"""build_article + slugify — pure builder + identifier-derivation tests."""

from __future__ import annotations

import pytest

from nostr import CLIENT_NAME
from nostr.events import verify_event
from nostr.publisher import build_article, slugify


PK = "79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"


# --------------------------------------------------------------------------- #
# slugify                                                                     #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("title,expected", [
    ("Hello World", "hello-world"),
    ("  Hello World  ", "hello-world"),
    ("Hello, World!", "hello-world"),
    ("Already-Hyphenated", "already-hyphenated"),
    ("UPPERCASE", "uppercase"),
    ("café & croissant", "caf-croissant"),    # non-ASCII collapses; no fancy unicode normalisation
    ("multiple   spaces", "multiple-spaces"),
    ("with/slashes/and:colons", "with-slashes-and-colons"),
])
def test_slugify_produces_expected(title, expected):
    assert slugify(title) == expected


def test_slugify_empty_returns_fallback():
    assert slugify("") == "untitled"
    assert slugify("   ") == "untitled"
    assert slugify("!!!") == "untitled"
    assert slugify("", fallback="draft") == "draft"


# --------------------------------------------------------------------------- #
# build_article                                                               #
# --------------------------------------------------------------------------- #

def test_build_article_minimal_has_required_d_and_client_tags():
    event = build_article("body", PK, slug="my-article")
    assert event["kind"] == 30023
    assert event["pubkey"] == PK
    assert event["content"] == "body"
    assert "sig" not in event
    # d-tag is required by NIP-23; client tag is our convention.
    assert ["d", "my-article"] in event["tags"]
    assert ["client", CLIENT_NAME] in event["tags"]


def test_build_article_rejects_empty_slug():
    with pytest.raises(ValueError):
        build_article("body", PK, slug="")
    with pytest.raises(ValueError):
        build_article("body", PK, slug="   ")


def test_build_article_full_metadata():
    event = build_article(
        "## Heading\n\nA paragraph.",
        PK,
        slug="hello-world",
        title="Hello World",
        summary="A friendly hello.",
        image="https://example.com/cover.png",
        published_at=1_700_000_000,
        hashtags=["Intro", "#meta", "  spaced  "],
    )
    tags = {tuple(t) for t in event["tags"]}
    assert ("title", "Hello World") in tags
    assert ("summary", "A friendly hello.") in tags
    assert ("image", "https://example.com/cover.png") in tags
    assert ("published_at", "1700000000") in tags
    # Hashtags are lowercased, stripped of '#', and skipped if blank.
    assert ("t", "intro") in tags
    assert ("t", "meta") in tags
    assert ("t", "spaced") in tags


def test_build_article_omits_empty_optional_tags():
    event = build_article("body", PK, slug="x", title="", summary="   ", image="")
    tag_keys = {t[0] for t in event["tags"]}
    assert "title" not in tag_keys
    assert "summary" not in tag_keys
    assert "image" not in tag_keys


def test_build_article_published_at_zero_is_included():
    # Falsy but explicitly provided — must still be emitted.
    event = build_article("body", PK, slug="x", published_at=0)
    assert ["published_at", "0"] in event["tags"]


def test_build_article_extra_tags_appended():
    event = build_article(
        "body",
        PK,
        slug="x",
        extra_tags=[["e", "abc123"]],
    )
    assert ["e", "abc123"] in event["tags"]


def test_build_article_signs_and_verifies():
    """Round-trip with a known key just like build_note does."""
    from nostr import crypto

    sk = bytes(31) + b"\x01"
    event = build_article("hello", PK, slug="hi", title="Hi", published_at=1_000_000)
    event["sig"] = crypto.sign_schnorr(sk, bytes.fromhex(event["id"])).hex()
    assert verify_event(event)
