# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Event id, signing, and verification — NIP-01 conformance.

The NIP-44 v2 suite already deeply validates the crypto primitives this
module depends on. These tests focus on the event-level wiring:
canonical serialization, id computation, roundtrip signing, and the
guarantees of verify_event().
"""

from __future__ import annotations

import copy

import pytest

from nostr import crypto, events


# A standard BIP-340 test secret key (value 1). Its x-only pubkey is the
# secp256k1 generator's x-coordinate, which is well known.
SK_ONE = bytes(31) + b"\x01"
PK_ONE_HEX = "79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"


# --------------------------------------------------------------------------- #
# Canonical serialization                                                     #
# --------------------------------------------------------------------------- #

def test_pubkey_derivation_matches_known_value():
    assert crypto.get_public_key(SK_ONE).hex() == PK_ONE_HEX


def test_canonical_serialize_is_compact_json():
    """No whitespace, in NIP-01 order."""
    raw = events.canonical_serialize(PK_ONE_HEX, 1700000000, 1, [], "hello")
    expected = (
        b'[0,"79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"'
        b',1700000000,1,[],"hello"]'
    )
    assert raw == expected


def test_canonical_serialize_handles_utf8_and_escapes():
    """Non-ASCII passes through verbatim; control chars get JSON-escaped."""
    raw = events.canonical_serialize(PK_ONE_HEX, 0, 1, [], 'GM ☀️ "line1"\nline2\t')
    # ensure_ascii=False keeps the emoji and sun as raw UTF-8 bytes
    assert "☀️".encode("utf-8") in raw
    # standard JSON escapes
    assert b'\\"line1\\"' in raw
    assert b"\\n" in raw
    assert b"\\t" in raw


def test_compute_event_id_is_deterministic():
    a = events.compute_event_id(PK_ONE_HEX, 1700000000, 1, [["t", "test"]], "hi")
    b = events.compute_event_id(PK_ONE_HEX, 1700000000, 1, [["t", "test"]], "hi")
    assert a == b
    assert len(a) == 64
    # Changing any field changes the id
    assert events.compute_event_id(PK_ONE_HEX, 1700000000, 1, [["t", "test"]], "hi!") != a
    assert events.compute_event_id(PK_ONE_HEX, 1700000001, 1, [["t", "test"]], "hi") != a
    assert events.compute_event_id(PK_ONE_HEX, 1700000000, 30023, [["t", "test"]], "hi") != a


# --------------------------------------------------------------------------- #
# build_event / sign_event / verify_event                                     #
# --------------------------------------------------------------------------- #

def test_build_event_signs_and_verifies():
    event = events.build_event(
        kind=1,
        content="hello from a fixed key",
        tags=[["t", "test"]],
        sk=SK_ONE,
        created_at=1700000000,
    )
    assert event["pubkey"] == PK_ONE_HEX
    assert event["kind"] == 1
    assert event["created_at"] == 1700000000
    assert event["tags"] == [["t", "test"]]
    assert len(event["id"]) == 64
    assert len(event["sig"]) == 128
    assert events.verify_event(event)


def test_build_unsigned_event_omits_sig():
    """For NIP-46: build the event with the user's pubkey, no signature, and
    hand to the remote signer to add ``sig``."""
    event = events.build_event(
        kind=1,
        content="will be signed by remote",
        pubkey_hex=PK_ONE_HEX,
        created_at=1700000000,
    )
    assert "sig" not in event
    assert event["pubkey"] == PK_ONE_HEX
    assert len(event["id"]) == 64

    # After signing externally, verify works
    signed = copy.deepcopy(event)
    signed["sig"] = crypto.sign_schnorr(SK_ONE, bytes.fromhex(signed["id"])).hex()
    assert events.verify_event(signed)


def test_sign_event_overwrites_mismatched_pubkey():
    """If a caller hands sign_event a dict with a stale pubkey, the resulting
    event uses the pubkey derived from sk — never publish under the wrong
    author."""
    other_sk = crypto.generate_secret_key()
    unsigned = {
        "pubkey": crypto.get_public_key(other_sk).hex(),
        "created_at": 1700000000,
        "kind": 1,
        "tags": [],
        "content": "x",
    }
    signed = events.sign_event(unsigned, SK_ONE)
    assert signed["pubkey"] == PK_ONE_HEX
    assert events.verify_event(signed)


def test_verify_event_rejects_tampered_content():
    event = events.build_event(kind=1, content="original", sk=SK_ONE, created_at=1700000000)
    event["content"] = "tampered"
    assert not events.verify_event(event)


def test_verify_event_rejects_tampered_signature():
    event = events.build_event(kind=1, content="x", sk=SK_ONE, created_at=1700000000)
    # Flip one bit in the signature
    bad = bytearray.fromhex(event["sig"])
    bad[0] ^= 0x01
    event["sig"] = bad.hex()
    assert not events.verify_event(event)


def test_verify_event_rejects_wrong_pubkey():
    event = events.build_event(kind=1, content="x", sk=SK_ONE, created_at=1700000000)
    other_pk = crypto.get_public_key(crypto.generate_secret_key()).hex()
    event["pubkey"] = other_pk
    assert not events.verify_event(event)


@pytest.mark.parametrize(
    "missing_field", ["id", "pubkey", "created_at", "kind", "tags", "content", "sig"]
)
def test_verify_event_rejects_missing_fields(missing_field):
    event = events.build_event(kind=1, content="x", sk=SK_ONE)
    del event[missing_field]
    assert not events.verify_event(event)


def test_verify_event_rejects_malformed_tags():
    event = events.build_event(kind=1, content="x", sk=SK_ONE)
    event["tags"] = "not a list"  # type: ignore[assignment]
    assert not events.verify_event(event)


def test_verify_event_rejects_non_string_tag_elements():
    event = events.build_event(kind=1, content="x", sk=SK_ONE)
    event["tags"] = [["t", 42]]  # type: ignore[list-item]
    assert not events.verify_event(event)
