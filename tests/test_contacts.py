"""Pure-function tests for the contact-list parser + kind 0 parser."""

from __future__ import annotations

import json

from nostr.contacts import parse_contact_list, parse_metadata_event


PK = "79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"
A = "01" * 32
B = "02" * 32
C = "03" * 32


# --------------------------------------------------------------------------- #
# parse_contact_list                                                          #
# --------------------------------------------------------------------------- #

def test_parse_contact_list_basic():
    event = {"tags": [
        ["p", A],
        ["p", B, "wss://r.example"],
        ["p", C, "wss://r.example", "carol-the-cat"],
    ]}
    people = parse_contact_list(event)
    assert [(p.pubkey, p.display_name, p.relay_hint) for p in people] == [
        (A, "", ""),
        (B, "", "wss://r.example"),
        (C, "carol-the-cat", "wss://r.example"),
    ]
    # All entries marked as coming from the contact list.
    assert all(p.source == "contact" for p in people)


def test_parse_contact_list_dedupes_repeated_pubkeys():
    event = {"tags": [
        ["p", A, "wss://r1"],
        ["p", A, "wss://r2"],  # duplicate — first wins
    ]}
    assert len(parse_contact_list(event)) == 1
    assert parse_contact_list(event)[0].relay_hint == "wss://r1"


def test_parse_contact_list_ignores_non_p_tags():
    event = {"tags": [
        ["client", "Acme"],
        ["e", "abc"],
        ["p", A, "", "alice"],
    ]}
    people = parse_contact_list(event)
    assert len(people) == 1
    assert people[0].display_name == "alice"


def test_parse_contact_list_skips_malformed():
    event = {"tags": [
        ["p"],                       # missing pubkey
        ["p", ""],                   # blank
        ["p", "short"],              # not 64 hex
        ["p", A, 5],                 # relay-hint isn't a string — treated as missing
        ["p", B],
    ]}
    people = parse_contact_list(event)
    assert [p.pubkey for p in people] == [A, B]
    # The malformed relay-hint integer turns into empty string, not a crash
    assert people[0].relay_hint == ""


# --------------------------------------------------------------------------- #
# parse_metadata_event                                                        #
# --------------------------------------------------------------------------- #

def test_parse_metadata_event_full():
    event = {
        "pubkey": PK,
        "created_at": 1_700_000_000,
        "content": json.dumps({
            "name": "alice",
            "display_name": "Alice Wonderland",
            "picture": "https://example.com/a.png",
            "nip05": "alice@nostr.band",
        }),
    }
    person = parse_metadata_event(event)
    assert person is not None
    assert person.pubkey == PK
    assert person.display_name == "Alice Wonderland"
    assert person.picture == "https://example.com/a.png"
    assert person.nip05 == "alice@nostr.band"
    assert person.updated_at == 1_700_000_000


def test_parse_metadata_prefers_display_name_over_name():
    event = {
        "pubkey": PK,
        "created_at": 1,
        "content": json.dumps({"name": "the-fallback", "display_name": "The Preferred"}),
    }
    assert parse_metadata_event(event).display_name == "The Preferred"


def test_parse_metadata_falls_back_to_name_when_display_missing():
    event = {
        "pubkey": PK,
        "created_at": 1,
        "content": json.dumps({"name": "only-name"}),
    }
    assert parse_metadata_event(event).display_name == "only-name"


def test_parse_metadata_returns_none_on_missing_pubkey():
    assert parse_metadata_event({"content": "{}"}) is None


def test_parse_metadata_returns_none_on_short_pubkey():
    assert parse_metadata_event({"pubkey": "ab", "content": "{}"}) is None


def test_parse_metadata_returns_none_on_non_json_content():
    event = {"pubkey": PK, "content": "not json at all", "created_at": 0}
    assert parse_metadata_event(event) is None


def test_parse_metadata_returns_none_on_non_object_content():
    event = {"pubkey": PK, "content": "[1, 2, 3]", "created_at": 0}
    assert parse_metadata_event(event) is None


def test_parse_metadata_tolerates_empty_content():
    event = {"pubkey": PK, "content": "", "created_at": 5}
    person = parse_metadata_event(event)
    assert person is not None
    assert person.pubkey == PK
    assert person.display_name == ""
