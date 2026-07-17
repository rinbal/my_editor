# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""NIP-44 v2 conformance — runs the full official vector suite.

Source: https://github.com/paulmillr/nip44/blob/main/nip44.vectors.json
Frozen copy: tests/nip44_vectors.json

This is the gold-standard interop test for NIP-44 v2. Any deviation
between our implementation and these vectors is a protocol bug and
ciphertext will not round-trip with other clients (Amber, nostr-tools,
noble, etc).
"""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest

from nostr import crypto


VECTORS = json.loads(
    (Path(__file__).parent / "nip44_vectors.json").read_text(encoding="utf-8")
)["v2"]


# --------------------------------------------------------------------------- #
# valid.get_conversation_key                                                  #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("vector", VECTORS["valid"]["get_conversation_key"])
def test_conversation_key_vectors(vector):
    sk = bytes.fromhex(vector["sec1"])
    pk = bytes.fromhex(vector["pub2"])
    expected = bytes.fromhex(vector["conversation_key"])
    assert crypto.conversation_key(sk, pk) == expected


# --------------------------------------------------------------------------- #
# valid.get_message_keys                                                      #
# --------------------------------------------------------------------------- #

def test_message_keys_vectors():
    bundle = VECTORS["valid"]["get_message_keys"]
    conv_key = bytes.fromhex(bundle["conversation_key"])
    for entry in bundle["keys"]:
        nonce = bytes.fromhex(entry["nonce"])
        chacha_key, chacha_nonce, hmac_key = crypto.message_keys(conv_key, nonce)
        assert chacha_key.hex() == entry["chacha_key"], f"chacha_key mismatch for nonce {entry['nonce']}"
        assert chacha_nonce.hex() == entry["chacha_nonce"], f"chacha_nonce mismatch for nonce {entry['nonce']}"
        assert hmac_key.hex() == entry["hmac_key"], f"hmac_key mismatch for nonce {entry['nonce']}"


# --------------------------------------------------------------------------- #
# valid.calc_padded_len                                                       #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("vector", VECTORS["valid"]["calc_padded_len"])
def test_calc_padded_len_vectors(vector):
    plain_len, expected = vector
    assert crypto.calc_padded_len(plain_len) == expected


# --------------------------------------------------------------------------- #
# valid.encrypt_decrypt                                                       #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("vector", VECTORS["valid"]["encrypt_decrypt"])
def test_encrypt_decrypt_vectors(vector):
    sk1 = bytes.fromhex(vector["sec1"])
    sk2 = bytes.fromhex(vector["sec2"])
    pk2 = crypto.get_public_key(sk2)
    pk1 = crypto.get_public_key(sk1)
    expected_conv = bytes.fromhex(vector["conversation_key"])
    nonce = bytes.fromhex(vector["nonce"])
    plaintext = vector["plaintext"]
    expected_payload = vector["payload"]

    # Conversation key matches from both sides
    conv_a = crypto.conversation_key(sk1, pk2)
    conv_b = crypto.conversation_key(sk2, pk1)
    assert conv_a == conv_b == expected_conv

    # Deterministic encrypt with the given nonce reproduces the vector payload
    actual_payload = crypto.encrypt(plaintext, conv_a, nonce=nonce)
    assert actual_payload == expected_payload

    # Roundtrip decryption from both directions
    assert crypto.decrypt(expected_payload, conv_a) == plaintext
    assert crypto.decrypt(expected_payload, conv_b) == plaintext


# --------------------------------------------------------------------------- #
# valid.encrypt_decrypt_long_msg                                              #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("vector", VECTORS["valid"]["encrypt_decrypt_long_msg"])
def test_encrypt_decrypt_long_msg_vectors(vector):
    conv_key = bytes.fromhex(vector["conversation_key"])
    nonce = bytes.fromhex(vector["nonce"])
    plaintext = vector["pattern"] * vector["repeat"]

    assert sha256(plaintext.encode("utf-8")).hexdigest() == vector["plaintext_sha256"]

    payload = crypto.encrypt(plaintext, conv_key, nonce=nonce)
    assert sha256(payload.encode("ascii")).hexdigest() == vector["payload_sha256"]

    decrypted = crypto.decrypt(payload, conv_key)
    assert decrypted == plaintext


# --------------------------------------------------------------------------- #
# invalid.get_conversation_key — should raise                                 #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("vector", VECTORS["invalid"]["get_conversation_key"])
def test_invalid_conversation_key_vectors(vector):
    sk_hex = vector["sec1"]
    pk_hex = vector["pub2"]
    try:
        sk = bytes.fromhex(sk_hex)
        pk = bytes.fromhex(pk_hex)
    except ValueError:
        return  # malformed hex is already a rejection
    with pytest.raises(Exception):
        crypto.conversation_key(sk, pk)


# --------------------------------------------------------------------------- #
# invalid.decrypt — should raise                                              #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("vector", VECTORS["invalid"]["decrypt"])
def test_invalid_decrypt_vectors(vector):
    conv_key = bytes.fromhex(vector["conversation_key"])
    payload = vector["payload"]
    with pytest.raises(Exception):
        crypto.decrypt(payload, conv_key)


# --------------------------------------------------------------------------- #
# invalid.encrypt_msg_lengths — should raise                                  #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("length", VECTORS["invalid"]["encrypt_msg_lengths"])
def test_invalid_encrypt_msg_lengths(length):
    """Lengths 0, 65536, 100000, 10000000 must all be rejected."""
    conv_key = b"\x00" * 32
    plaintext = "a" * length
    with pytest.raises(Exception):
        crypto.encrypt(plaintext, conv_key)
