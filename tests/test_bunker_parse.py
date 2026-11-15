# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""URI parsing for bunker://: pure-function tests, no network."""

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
    "bunker://shortpubkey?relay=wss://relay.example",     # bad pubkey
    f"bunker://{'z' * 64}?relay=wss://relay.example",      # non-hex pubkey
])
def test_rejects_malformed(uri):
    with pytest.raises(ValueError):
        parse_bunker_uri(uri)


@pytest.mark.parametrize("key", ["cd" * 32, "00" * 32, "ff" * 32,
                                 "fffffffffffffffffffffffffffffffffffffffffffffffffffffffefffffc2f"])
def test_rejects_a_key_that_is_no_public_key(key):
    # RG-M2r2b 2: 64 hex digits, but no point of the curve: no channel can
    # be opened to it, so the address is refused where it is read.
    with pytest.raises(ValueError):
        parse_bunker_uri(f"bunker://{key}?relay=wss://relay.example")


def test_about_half_of_all_keys_are_no_public_key_and_a_real_one_passes():
    import os
    from nostr import crypto
    real = crypto.get_public_key(crypto.generate_secret_key()).hex()
    assert parse_bunker_uri(f"bunker://{real}?relay=wss://r.example").bunker_pubkey == real
    refused = sum(not crypto.is_public_key(os.urandom(32)) for _ in range(400))
    assert 120 < refused < 280
    assert not crypto.is_public_key(b"\x02" * 31) and not crypto.is_public_key(b"")


@pytest.mark.parametrize("reason, words", [
    ("timed out waiting for signer", "Your signer app didn’t answer: open it and try again."),
    ("could not deliver request to any relay (x)",
     "Your signer app didn’t answer: open it and try again."),
    ("user rejected", "Your signer app declined the request: allow it there and try again."),
    ("Unknown client", "Your signer app declined the request:"),
    ("not allowed", "Your signer app declined the request:"),
    ("unauthorized", "Your signer app declined the request:"),
    ("secret did not match (possible spoof attempt)",
     "The answer was meant for another connection: try again."),
    ("", "The connection didn’t work: try again, or connect another way."),
    ("something else", "The connection didn’t work:"),
])
def test_a_pairing_that_did_not_work_is_one_plain_sentence(reason, words):
    # Lessons 36: one sentence, its likely cause and what to do.
    from nostr.bunker import pairing_failure_words
    shown = pairing_failure_words(reason)
    assert shown.startswith(words) and shown.count(".") == 1 and shown.endswith(".")
    assert reason == "" or reason.lower() not in shown.lower()


@pytest.mark.parametrize("reason, words", [
    ("timed out waiting for signer", "Your signer app didn’t answer: open it and try again."),
    ("could not deliver request to any relay (wss://a: blocked)",
     "Your signer app didn’t answer: open it and try again."),
    ("not connected", "Your signer app didn’t answer: open it and try again."),
    ("dropped from session pool", "Your signer app didn’t answer: open it and try again."),
    ("user rejected", "Your signer app declined the request: allow it there and try again."),
    ("Permission denied by user", "Your signer app declined the request:"),
    ("invalid event: 42", "Your signer app ran into a problem: open it and try again."),
    ("", "Your signer app ran into a problem:"),
])
def test_a_request_the_signer_did_not_do_is_one_plain_sentence(reason, words):
    # Lessons 36 and 26: the cause and what to do, never the reason itself.
    from nostr.bunker import signer_failure_words
    shown = signer_failure_words(reason)
    assert shown.startswith(words) and shown.count(".") == 1 and shown.endswith(".")
    assert reason == "" or reason.lower() not in shown.lower()
