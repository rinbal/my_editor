"""NIP-37 pure builders & parsers — see ``nostr/drafts.py``.

These are the on-wire-shape guarantees we make to other Nostr clients:
the d/k/expiration/client tag set, the inner-event JSON whitelist, and
the tombstone semantics. Any change here that breaks an existing test
is a protocol-level break and probably needs a coordinated rollout.
"""

from __future__ import annotations

import json

import pytest

from nostr.drafts import (
    DEFAULT_EXPIRATION_SECONDS,
    DRAFT_WRAP_KIND,
    INNER_KIND_LONG_FORM,
    INNER_KIND_SHORT_NOTE,
    MAX_INNER_PAYLOAD_BYTES,
    SUPPORTED_INNER_KINDS,
    DraftWrapMeta,
    build_draft_wrap,
    build_inner_event,
    build_tombstone_wrap,
    derive_preview_snippet,
    extract_article_metadata,
    new_note_identifier,
    parse_inner_event,
    parse_wrap_event,
    serialize_inner_event,
)


PK = "a" * 64
OTHER_PK = "b" * 64


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

def test_protocol_constants():
    # Locking these into a test means a typo / accidental change becomes
    # a CI failure, not a silent on-wire incompatibility.
    assert DRAFT_WRAP_KIND == 31234
    assert INNER_KIND_SHORT_NOTE == 1
    assert INNER_KIND_LONG_FORM == 30023
    assert SUPPORTED_INNER_KINDS == (1, 30023)
    assert DEFAULT_EXPIRATION_SECONDS == 90 * 24 * 60 * 60
    assert MAX_INNER_PAYLOAD_BYTES == 65535


# --------------------------------------------------------------------------- #
# build_inner_event                                                           #
# --------------------------------------------------------------------------- #

def test_build_inner_event_shape():
    e = build_inner_event(kind=1, content="hello", pubkey_hex=PK, tags=[["t", "x"]])
    # No id / sig — inner is unsigned by NIP-37 contract.
    assert "id" not in e and "sig" not in e
    assert e["kind"] == 1
    assert e["content"] == "hello"
    assert e["tags"] == [["t", "x"]]
    assert e["pubkey"] == PK
    assert isinstance(e["created_at"], int)


def test_build_inner_event_lowercases_pubkey():
    e = build_inner_event(kind=1, content="x", pubkey_hex=PK.upper())
    assert e["pubkey"] == PK


def test_build_inner_event_rejects_short_pubkey():
    with pytest.raises(ValueError, match="64 hex"):
        build_inner_event(kind=1, content="x", pubkey_hex="abc")


def test_build_inner_event_rejects_unsupported_kind():
    with pytest.raises(ValueError, match="unsupported inner kind"):
        build_inner_event(kind=9999, content="x", pubkey_hex=PK)


def test_build_inner_event_defaults_tags_to_empty():
    e = build_inner_event(kind=1, content="x", pubkey_hex=PK)
    assert e["tags"] == []


def test_build_inner_event_respects_explicit_created_at():
    e = build_inner_event(kind=1, content="x", pubkey_hex=PK, created_at=1234567890)
    assert e["created_at"] == 1234567890


# --------------------------------------------------------------------------- #
# serialize / parse round-trip                                                #
# --------------------------------------------------------------------------- #

def test_serialize_uses_compact_json():
    e = build_inner_event(kind=1, content="x", pubkey_hex=PK)
    s = serialize_inner_event(e)
    # Compact separators — no whitespace between keys.
    assert ", " not in s and ": " not in s


def test_serialize_preserves_unicode():
    # NIP-44 plaintext is UTF-8; we must not escape non-ASCII or the
    # payload grows for no good reason.
    e = build_inner_event(kind=1, content="café — 🎩", pubkey_hex=PK)
    s = serialize_inner_event(e)
    assert "café" in s and "🎩" in s


def test_serialize_strips_id_and_sig():
    # If a caller accidentally hands us a signed event dict, the
    # whitelist in serialize_inner_event must drop id/sig — otherwise
    # the encrypted payload bloats and leaks past activity.
    poisoned = build_inner_event(kind=1, content="x", pubkey_hex=PK)
    poisoned["id"] = "f" * 64
    poisoned["sig"] = "0" * 128
    s = serialize_inner_event(poisoned)
    assert '"id"' not in s
    assert '"sig"' not in s


def test_parse_round_trips():
    e = build_inner_event(
        kind=30023, content="body", pubkey_hex=PK,
        tags=[["title", "Hello"], ["t", "blog"]],
        created_at=100,
    )
    back = parse_inner_event(serialize_inner_event(e))
    assert back == {
        "kind": 30023,
        "content": "body",
        "tags": [["title", "Hello"], ["t", "blog"]],
        "created_at": 100,
        "pubkey": PK,
    }


@pytest.mark.parametrize("payload", [
    "",                                # empty
    "not json",                        # garbage
    "[1, 2, 3]",                       # array, not object
    '{"kind": "abc"}',                 # kind not int
    '{"kind": 1, "tags": "nope"}',     # tags not list
    '{"kind": 1, "tags": [[1]]}',      # tag entry not list-of-str
])
def test_parse_rejects_malformed(payload):
    with pytest.raises(ValueError):
        parse_inner_event(payload)


def test_parse_tolerates_unknown_kind():
    # A future client could stash kind 9999 drafts; we must surface them
    # rather than silently fail. The store branch is what decides how to
    # render — parse just hands the data through.
    s = json.dumps({"kind": 9999, "content": "x", "tags": [], "created_at": 1, "pubkey": PK})
    parsed = parse_inner_event(s)
    assert parsed["kind"] == 9999


# --------------------------------------------------------------------------- #
# build_draft_wrap — outer 31234                                              #
# --------------------------------------------------------------------------- #

def test_build_draft_wrap_tag_set():
    wrap = build_draft_wrap(
        identifier="my-slug", inner_kind=30023,
        encrypted_content="b64payload==", pubkey_hex=PK, client_name="My-Editor",
        created_at=100, expiration_seconds=86400,
    )
    assert wrap["kind"] == DRAFT_WRAP_KIND
    assert wrap["content"] == "b64payload=="
    assert wrap["pubkey"] == PK
    # Tag order matters for human-readable inspection but not protocol;
    # we still pin it so the canonical-serialized id is reproducible.
    assert wrap["tags"] == [
        ["d", "my-slug"],
        ["k", "30023"],
        ["expiration", "86500"],  # created_at(100) + 86400
        ["client", "My-Editor"],
    ]


def test_build_draft_wrap_empty_identifier_rejected():
    with pytest.raises(ValueError, match="identifier"):
        build_draft_wrap(
            identifier="", inner_kind=1, encrypted_content="x",
            pubkey_hex=PK, client_name="X",
        )


def test_build_draft_wrap_unsupported_inner_kind_rejected():
    with pytest.raises(ValueError, match="unsupported inner kind"):
        build_draft_wrap(
            identifier="x", inner_kind=9999, encrypted_content="x",
            pubkey_hex=PK, client_name="X",
        )


def test_build_draft_wrap_extra_tags_appended():
    wrap = build_draft_wrap(
        identifier="x", inner_kind=1, encrypted_content="ct",
        pubkey_hex=PK, client_name="X",
        extra_tags=[["source", "rss", "https://example.com/feed.xml"]],
    )
    # Extras come after the spec tags so spec-aware readers see d/k/etc first.
    assert wrap["tags"][-1] == ["source", "rss", "https://example.com/feed.xml"]


def test_build_tombstone_is_empty_content():
    t = build_tombstone_wrap(identifier="x", inner_kind=1, pubkey_hex=PK, client_name="X")
    assert t["content"] == ""
    # Same d + k so addressable replacement targets the right event.
    assert ["d", "x"] in t["tags"]
    assert ["k", "1"] in t["tags"]


# --------------------------------------------------------------------------- #
# parse_wrap_event                                                            #
# --------------------------------------------------------------------------- #

def test_parse_wrap_extracts_meta():
    wrap = build_draft_wrap(
        identifier="x", inner_kind=1, encrypted_content="ct",
        pubkey_hex=PK, client_name="X", created_at=42,
    )
    wrap["id"] = "f" * 64  # a relay would have filled this in
    meta = parse_wrap_event(wrap)
    assert isinstance(meta, DraftWrapMeta)
    assert meta.identifier == "x"
    assert meta.inner_kind == 1
    assert meta.event_id == "f" * 64
    assert meta.created_at == 42
    assert meta.ciphertext == "ct"
    assert meta.is_tombstone is False


def test_parse_wrap_recognises_tombstone():
    t = build_tombstone_wrap(identifier="x", inner_kind=1, pubkey_hex=PK, client_name="X")
    meta = parse_wrap_event(t)
    assert meta is not None
    assert meta.is_tombstone is True


@pytest.mark.parametrize("event", [
    None,
    {},
    {"kind": 1, "tags": []},                              # wrong kind
    {"kind": 31234, "tags": "not-a-list"},                # tags not list
    {"kind": 31234, "tags": []},                          # no d-tag → reject
    {"kind": 31234, "tags": [["d", ""]]},                 # empty d-tag → reject
    {"kind": 31234, "tags": [["k", "1"]]},                # no d-tag → reject
])
def test_parse_wrap_rejects_garbage(event):
    assert parse_wrap_event(event) is None


def test_parse_wrap_unknown_k_tag_falls_back_to_zero():
    # When a tombstone or sloppy client omits / mangles k, we still want
    # to honour d-based addressable semantics for tombstone matching.
    wrap = {
        "kind": 31234,
        "tags": [["d", "x"], ["k", "not-a-number"]],
        "id": "f" * 64,
        "pubkey": PK,
        "created_at": 1,
        "content": "",
    }
    meta = parse_wrap_event(wrap)
    assert meta is not None
    assert meta.inner_kind == 0
    assert meta.is_tombstone is True


def test_parse_wrap_lowercases_pubkey():
    wrap = build_draft_wrap(
        identifier="x", inner_kind=1, encrypted_content="ct",
        pubkey_hex=PK.upper(), client_name="X",
    )
    wrap["id"] = "0" * 64
    meta = parse_wrap_event(wrap)
    assert meta is not None
    assert meta.pubkey == PK  # lowercased


# --------------------------------------------------------------------------- #
# Identifier helpers                                                          #
# --------------------------------------------------------------------------- #

def test_new_note_identifier_is_unique_and_prefixed():
    a = new_note_identifier()
    b = new_note_identifier()
    assert a != b
    assert a.startswith("note-") and len(a) > len("note-")


# --------------------------------------------------------------------------- #
# Article-metadata extraction                                                 #
# --------------------------------------------------------------------------- #

def test_extract_article_metadata_pulls_known_keys():
    inner = build_inner_event(
        kind=30023, content="body", pubkey_hex=PK,
        tags=[
            ["title", "My Post"],
            ["summary", "A summary"],
            ["image", "https://example.com/i.png"],
            ["published_at", "1700000000"],
            ["t", "ignored-non-meta"],
        ],
    )
    meta = extract_article_metadata(inner)
    assert meta == {
        "title": "My Post",
        "summary": "A summary",
        "image": "https://example.com/i.png",
        "published_at": "1700000000",
    }


def test_extract_article_metadata_first_value_wins():
    # Spec doesn't say which to keep when a tag repeats; first-write-wins
    # is the safest default — it matches what most other clients do.
    inner = {
        "tags": [["title", "First"], ["title", "Second"]],
    }
    assert extract_article_metadata(inner)["title"] == "First"


def test_extract_article_metadata_defaults_to_empty():
    inner = {"tags": []}
    meta = extract_article_metadata(inner)
    assert meta == {"title": "", "summary": "", "image": "", "published_at": ""}


# --------------------------------------------------------------------------- #
# derive_preview_snippet                                                      #
# --------------------------------------------------------------------------- #

def test_snippet_empty_returns_empty():
    assert derive_preview_snippet("") == ""
    assert derive_preview_snippet("   \n\n  \t  ") == ""


def test_snippet_strips_leading_level1_heading():
    # Articles duplicate their title in the body; dropping the leading
    # "# heading" gives the panel a useful summary rather than echoing
    # the title twice.
    assert derive_preview_snippet("# Heading\n\nBody text.") == "Body text."


def test_snippet_keeps_non_level1_headings():
    # "##" and "#word" are NOT level-1 headings; keep them as content.
    assert "##" in derive_preview_snippet("## sub\n\nbody")
    assert "#tag" in derive_preview_snippet("#tag at start\nbody")


def test_snippet_collapses_whitespace():
    assert derive_preview_snippet("a\n\n\n   b\t c") == "a b c"


def test_snippet_truncates_with_ellipsis():
    long = "x" * 200
    snippet = derive_preview_snippet(long, max_chars=20)
    assert snippet.endswith("…")
    assert len(snippet) == 20
