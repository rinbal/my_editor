"""URI parsing for bunker:// — pure-function tests, no network."""

from __future__ import annotations

import pytest

from nostr.bunker import parse_bunker_uri


PK = "a" * 64


def test_parses_full_uri():
    uri = (
        f"bunker://{PK}"
        "?relay=wss%3A%2F%2Frelay.example%2F"
        "&relay=wss%3A%2F%2Frelay2.example%2F"
        "&secret=hunter2"
    )
    parsed = parse_bunker_uri(uri)
    assert parsed.bunker_pubkey == PK
    assert parsed.relays == ["wss://relay.example/", "wss://relay2.example/"]
    assert parsed.secret == "hunter2"


def test_parses_unencoded_relay_urls():
    uri = f"bunker://{PK}?relay=wss://relay.example&secret=abc"
    parsed = parse_bunker_uri(uri)
    assert parsed.relays == ["wss://relay.example"]
    assert parsed.secret == "abc"


def test_secret_is_optional():
    uri = f"bunker://{PK}?relay=wss://relay.example"
    parsed = parse_bunker_uri(uri)
    assert parsed.secret is None


def test_multiple_relays_preserved_in_order():
    uri = f"bunker://{PK}?relay=wss://a&relay=wss://b&relay=wss://c"
    parsed = parse_bunker_uri(uri)
    assert parsed.relays == ["wss://a", "wss://b", "wss://c"]


def test_pubkey_lowercased():
    upper_pk = "A" * 64
    uri = f"bunker://{upper_pk}?relay=wss://relay.example"
    parsed = parse_bunker_uri(uri)
    assert parsed.bunker_pubkey == "a" * 64


@pytest.mark.parametrize("uri", [
    "",
    "not-a-uri",
    "nostr://" + "a" * 64 + "?relay=wss://x",            # wrong scheme
    f"bunker://{PK}",                                      # missing relays
    f"bunker://{PK}?relay=",                               # empty relay
    f"bunker://{PK}?relay=http://insecure.example",       # bad relay scheme
    f"bunker://shortpubkey?relay=wss://relay.example",    # bad pubkey
    f"bunker://{'z' * 64}?relay=wss://relay.example",      # non-hex pubkey
])
def test_rejects_malformed(uri):
    with pytest.raises(ValueError):
        parse_bunker_uri(uri)
