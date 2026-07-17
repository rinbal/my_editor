# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bech32 + NIP-19 encoding tests.

Validates against:
  - Known npub/pubkey pair (jb55) that is independently verifiable on any
    Nostr client — proves interop with the wider ecosystem.
  - Standard roundtrip and tamper-rejection tests.
"""

from __future__ import annotations

import pytest

from nostr import bech32


# Well-known reference pair (independently verifiable on damus, snort, etc.)
JB55_NPUB = "npub1xtscya34g58tk0z605fvr788k263gsu6cy9x0mhnm87echrgufzsevkk5s"
JB55_HEX = "32e1827635450ebb3c5a7d12c1f8e7b2b514439ac10a67eef3d9fd9c5c68e245"


def test_npub_decode_matches_known_reference():
    assert bech32.decode_npub(JB55_NPUB) == JB55_HEX


def test_npub_encode_matches_known_reference():
    assert bech32.encode_npub(JB55_HEX) == JB55_NPUB


def test_npub_roundtrip_random():
    import os
    for _ in range(20):
        pk_hex = os.urandom(32).hex()
        assert bech32.decode_npub(bech32.encode_npub(pk_hex)) == pk_hex


def test_nsec_roundtrip_random():
    import os
    for _ in range(20):
        sk_hex = os.urandom(32).hex()
        assert bech32.decode_nsec(bech32.encode_nsec(sk_hex)) == sk_hex


def test_note_roundtrip_random():
    import os
    for _ in range(20):
        id_hex = os.urandom(32).hex()
        assert bech32.decode_note(bech32.encode_note(id_hex)) == id_hex


def test_decode_rejects_wrong_hrp():
    """Decoding with a mismatched hrp must fail (npub decoded as nsec etc)."""
    npub = bech32.encode_npub(JB55_HEX)
    with pytest.raises(ValueError):
        bech32.decode_nsec(npub)


def test_decode_rejects_tampered_checksum():
    npub = bech32.encode_npub(JB55_HEX)
    # Flip the last data character (still in the bech32 charset) to break checksum
    tampered = npub[:-1] + ("q" if npub[-1] != "q" else "p")
    with pytest.raises(ValueError):
        bech32.decode_npub(tampered)


def test_decode_rejects_mixed_case():
    npub = bech32.encode_npub(JB55_HEX)
    # All-lower or all-upper is allowed; mixed must fail.
    mixed = npub[:4] + npub[4:].upper()
    with pytest.raises(ValueError):
        bech32.decode_npub(mixed)


def test_decode_rejects_missing_separator():
    with pytest.raises(ValueError):
        bech32.bech32_decode("npubmissingseparator")


def test_decode_rejects_bad_character():
    with pytest.raises(ValueError):
        bech32.decode_npub("npub1" + "B" + JB55_NPUB[6:])  # 'b' is allowed but 'B' would be uppercase mix


# --------------------------------------------------------------------------- #
# NIP-19 naddr (TLV)                                                          #
# --------------------------------------------------------------------------- #

def test_naddr_roundtrip_minimal():
    naddr = bech32.encode_naddr(
        identifier="my-article",
        author_pubkey_hex=JB55_HEX,
        kind=30023,
    )
    assert naddr.startswith("naddr1")
    ident, author, kind, relays = bech32.decode_naddr(naddr)
    assert ident == "my-article"
    assert author == JB55_HEX
    assert kind == 30023
    assert relays == []


def test_naddr_roundtrip_with_relays():
    relays = ["wss://relay.primal.net", "wss://nos.lol"]
    naddr = bech32.encode_naddr(
        identifier="hello-world",
        author_pubkey_hex=JB55_HEX,
        kind=30023,
        relays=relays,
    )
    ident, author, kind, decoded_relays = bech32.decode_naddr(naddr)
    assert ident == "hello-world"
    assert author == JB55_HEX
    assert kind == 30023
    assert decoded_relays == relays


def test_naddr_empty_identifier_is_legal():
    """NIP-19 explicitly allows an empty d-tag (normal replaceable events)."""
    naddr = bech32.encode_naddr(
        identifier="",
        author_pubkey_hex=JB55_HEX,
        kind=10002,
    )
    ident, _, kind, _ = bech32.decode_naddr(naddr)
    assert ident == ""
    assert kind == 10002


def test_naddr_utf8_identifier_survives_roundtrip():
    naddr = bech32.encode_naddr(
        identifier="café-☕",
        author_pubkey_hex=JB55_HEX,
        kind=30023,
    )
    ident, _, _, _ = bech32.decode_naddr(naddr)
    assert ident == "café-☕"


def test_naddr_rejects_oversized_value():
    with pytest.raises(ValueError):
        bech32.encode_naddr("a" * 256, JB55_HEX, 30023)


def test_naddr_rejects_bad_author_size():
    with pytest.raises(ValueError):
        bech32.encode_naddr("x", "ab" * 16, 30023)  # 32 hex chars = 16 bytes


def test_naddr_rejects_oversized_kind():
    with pytest.raises(ValueError):
        bech32.encode_naddr("x", JB55_HEX, 0xFFFFFFFF + 1)


def test_decode_naddr_rejects_wrong_hrp():
    npub = bech32.encode_npub(JB55_HEX)
    with pytest.raises(ValueError):
        bech32.decode_naddr(npub)


# --------------------------------------------------------------------------- #
# NIP-19 nprofile (TLV)                                                       #
# --------------------------------------------------------------------------- #

def test_nprofile_roundtrip_no_relays():
    nprofile = bech32.encode_nprofile(JB55_HEX)
    assert nprofile.startswith("nprofile1")
    pk, relays = bech32.decode_nprofile(nprofile)
    assert pk == JB55_HEX
    assert relays == []


def test_nprofile_roundtrip_with_relays():
    relays = ["wss://relay.primal.net", "wss://nos.lol"]
    nprofile = bech32.encode_nprofile(JB55_HEX, relays=relays)
    pk, decoded_relays = bech32.decode_nprofile(nprofile)
    assert pk == JB55_HEX
    assert decoded_relays == relays


def test_nprofile_rejects_bad_pubkey():
    with pytest.raises(ValueError):
        bech32.encode_nprofile("ab" * 16)  # 16 bytes, not 32


def test_decode_nprofile_rejects_wrong_hrp():
    npub = bech32.encode_npub(JB55_HEX)
    with pytest.raises(ValueError):
        bech32.decode_nprofile(npub)
