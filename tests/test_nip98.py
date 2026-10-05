# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins the NIP-98 credential the membership API verifies.

Every rule here is one the association's server checks and answers
with the same undifferentiated 401 when it fails, so a regression would
surface as "EINUNDZWANZIG could not confirm it is you" with nothing to
debug from. The tests name the rule instead.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nostr import events
from nostr.nip98 import (
    JSON_CONTENT_TYPE,
    NIP98_KIND,
    authorization_header_value,
    build_unsigned_auth_event,
    has_body,
    sha256_hex,
    signed_event_problem,
)


# A throwaway key: these events are signed for real so they can be verified.
SECRET_KEY = bytes.fromhex("3f" * 32)

URL = "https://verein.einundzwanzig.space/api/v1/membership/applications"
BODY = b'{"statutes_accepted":true,"email":"satoshi@example.org"}'


def tag(event, name):
    for entry in event.get("tags") or []:
        if len(entry) >= 2 and entry[0] == name:
            return entry[1]
    return None


def signed(unsigned):
    return events.sign_event(json.loads(json.dumps(unsigned)), SECRET_KEY)


# --------------------------------------------------------------------- #
# Event shape                                                           #
# --------------------------------------------------------------------- #

def test_the_event_is_kind_27235_with_empty_content():
    event = build_unsigned_auth_event(URL, "POST", BODY, now=1_000)
    assert event["kind"] == NIP98_KIND == 27235
    assert event["content"] == ""
    assert event["created_at"] == 1_000


def test_the_url_is_carried_byte_for_byte():
    # A trailing slash or a different query string is a different URL to
    # the server, so nothing may be normalised on the way.
    odd = "https://verein.einundzwanzig.space/api/v1/membership/me/?a=1&b=%20"
    event = build_unsigned_auth_event(odd, "GET", None, now=1)
    assert tag(event, "u") == odd


def test_the_method_is_uppercased():
    event = build_unsigned_auth_event(URL, "post", BODY, now=1)
    assert tag(event, "method") == "POST"


def test_a_body_is_bound_by_its_lowercase_sha256():
    event = build_unsigned_auth_event(URL, "POST", BODY, now=1)
    assert tag(event, "payload") == hashlib.sha256(BODY).hexdigest()
    assert tag(event, "payload") == tag(event, "payload").lower()
    assert sha256_hex(BODY) == hashlib.sha256(BODY).hexdigest()


def test_the_payload_hash_is_over_the_exact_bytes():
    # A re-encoding of the same JSON is different bytes and a different
    # hash: the server hashes what it received, not what it parsed.
    spaced = b'{"statutes_accepted": true, "email": "satoshi@example.org"}'
    a = build_unsigned_auth_event(URL, "POST", BODY, now=1)
    b = build_unsigned_auth_event(URL, "POST", spaced, now=1)
    assert tag(a, "payload") != tag(b, "payload")


@pytest.mark.parametrize("body", [None, b""])
def test_no_body_means_no_payload_tag(body):
    # An empty JSON body such as [] IS a body to the server; nothing at
    # all is not, and must not demand a payload hash.
    event = build_unsigned_auth_event(URL.replace("applications", "me"), "GET", body, now=1)
    assert tag(event, "payload") is None
    assert not has_body(body)


def test_an_empty_json_array_is_a_body():
    event = build_unsigned_auth_event(URL, "POST", b"[]", now=1)
    assert tag(event, "payload") == hashlib.sha256(b"[]").hexdigest()


def test_created_at_is_now_not_backdated():
    # The window is +/-60 s. Backdating, as Blossom tokens do, would
    # only spend half of it before the request even leaves.
    assert build_unsigned_auth_event(URL, "GET", None, now=1_785_062_400)["created_at"] == 1_785_062_400


def test_every_event_gets_its_own_nonce():
    # Two requests for the same URL in the same second would otherwise
    # share an id, and the server accepts each id exactly once.
    a = build_unsigned_auth_event(URL, "POST", BODY, now=5)
    b = build_unsigned_auth_event(URL, "POST", BODY, now=5)
    assert tag(a, "nonce") and tag(b, "nonce")
    assert tag(a, "nonce") != tag(b, "nonce")
    assert signed(a)["id"] != signed(b)["id"]


def test_a_given_nonce_is_used():
    event = build_unsigned_auth_event(URL, "GET", None, now=5, nonce="abc")
    assert tag(event, "nonce") == "abc"


def test_only_documented_tags_and_the_nonce_are_sent():
    event = build_unsigned_auth_event(URL, "POST", BODY, now=5)
    assert sorted(t[0] for t in event["tags"]) == ["method", "nonce", "payload", "u"]


@pytest.mark.parametrize("url", ["/api/v1/membership/me", "verein.einundzwanzig.space/me",
                                 "ftp://x.example/a", "", None])
def test_a_relative_or_odd_url_is_a_programming_error(url):
    with pytest.raises(ValueError):
        build_unsigned_auth_event(url, "GET", None, now=1)


def test_an_empty_method_is_a_programming_error():
    with pytest.raises(ValueError):
        build_unsigned_auth_event(URL, " ", None, now=1)


# --------------------------------------------------------------------- #
# The header                                                            #
# --------------------------------------------------------------------- #

def test_the_header_is_nostr_plus_standard_base64_of_compact_json():
    event = signed(build_unsigned_auth_event(URL, "POST", BODY, now=1))
    value = authorization_header_value(event)
    assert isinstance(value, bytes)
    assert value.startswith(b"Nostr ")
    raw = base64.b64decode(value[len(b"Nostr "):], validate=True)
    assert json.loads(raw) == event
    assert raw == json.dumps(event, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def test_the_header_is_standard_base64_not_base64url():
    # Blossom uses base64url without padding; this server decodes the
    # standard alphabet. Content chosen so the two encodings differ.
    event = {"content": "ÿþý?>>", "kind": 27235, "tags": []}
    value = authorization_header_value(event)[len(b"Nostr "):]
    assert value == base64.b64encode(
        json.dumps(event, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    )
    assert b"+" in value or b"/" in value or value.endswith(b"=")


def test_a_signed_event_round_trips_and_verifies():
    event = signed(build_unsigned_auth_event(URL, "POST", BODY, now=1))
    decoded = json.loads(base64.b64decode(authorization_header_value(event)[6:]))
    assert events.verify_event(decoded)


def test_json_bodies_use_exactly_application_json():
    assert JSON_CONTENT_TYPE == "application/json"


# --------------------------------------------------------------------- #
# Checking the signer's answer                                          #
# --------------------------------------------------------------------- #

def test_a_faithful_signature_has_no_problem():
    unsigned = build_unsigned_auth_event(URL, "POST", BODY, now=1)
    assert signed_event_problem(signed(unsigned), unsigned) is None


@pytest.mark.parametrize("edit", [
    lambda e: {**e, "kind": 1},
    lambda e: {**e, "tags": e["tags"][:-1]},
    lambda e: {**e, "content": "hello"},
    lambda e: {**e, "created_at": "1"},
    lambda e: {**e, "id": e["id"].upper()},
    lambda e: {**e, "pubkey": e["pubkey"].upper()},
    lambda e: {**e, "sig": e["sig"][:-2]},
    lambda e: {k: v for k, v in e.items() if k != "sig"},
])
def test_a_tampered_or_malformed_answer_is_caught(edit):
    unsigned = build_unsigned_auth_event(URL, "POST", BODY, now=1)
    assert signed_event_problem(edit(signed(unsigned)), unsigned)


def test_a_non_event_answer_is_caught():
    unsigned = build_unsigned_auth_event(URL, "GET", None, now=1)
    assert signed_event_problem("not an event", unsigned)
    assert signed_event_problem(None, unsigned)
