# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bech32 (BIP-173) + NIP-19 helpers.

NIP-19 uses plain bech32 (constant 1), NOT bech32m (constant 0x2bc830a3).

This module implements just the pieces we need: encode/decode of the raw
bech32 stream, plus npub/nsec helpers (simple 32-byte payloads). The TLV
forms (naddr, nevent, nprofile) live next to the publisher that builds
them, not here.

Reference implementation:
  https://github.com/sipa/bech32/blob/master/ref/python/segwit_addr.py
"""

from __future__ import annotations

from typing import List, Tuple

CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"

# NIP-19 bech32 length limit: practical maximum for plain pubkey/nsec strings.
# (The original BIP-173 limit of 90 is too tight for naddr/nevent so NIP-19
# explicitly lifts it. We pick a comfortable ceiling here.)
NIP19_MAX_LEN = 5000


# --------------------------------------------------------------------------- #
# Bech32 core (lifted directly from the BIP-173 reference, then trimmed)      #
# --------------------------------------------------------------------------- #

def _polymod(values: List[int]) -> int:
    GEN = (0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3)
    chk = 1
    for v in values:
        top = chk >> 25
        chk = (chk & 0x1ffffff) << 5 ^ v
        for i in range(5):
            chk ^= GEN[i] if ((top >> i) & 1) else 0
    return chk


def _hrp_expand(hrp: str) -> List[int]:
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]


def _verify_checksum(hrp: str, data: List[int]) -> bool:
    # Plain bech32 checksum constant is 1 (bech32m would be 0x2bc830a3).
    return _polymod(_hrp_expand(hrp) + data) == 1


def _create_checksum(hrp: str, data: List[int]) -> List[int]:
    values = _hrp_expand(hrp) + data
    poly = _polymod(values + [0, 0, 0, 0, 0, 0]) ^ 1
    return [(poly >> 5 * (5 - i)) & 31 for i in range(6)]


def bech32_encode(hrp: str, data: List[int]) -> str:
    """Encode a bech32 string from human-readable part and 5-bit data."""
    combined = data + _create_checksum(hrp, data)
    return hrp + "1" + "".join(CHARSET[d] for d in combined)


def bech32_decode(bech: str) -> Tuple[str, List[int]]:
    """Decode a bech32 string. Raises ValueError on any malformedness.

    Returns (hrp, 5-bit-data WITHOUT the 6-byte checksum).
    """
    if len(bech) > NIP19_MAX_LEN:
        raise ValueError("bech32 string too long")
    if any(ord(x) < 33 or ord(x) > 126 for x in bech):
        raise ValueError("bech32 contains characters outside printable ASCII")
    lower = bech.lower()
    upper = bech.upper()
    if bech != lower and bech != upper:
        raise ValueError("bech32 mixes upper and lower case")
    bech = lower
    pos = bech.rfind("1")
    if pos < 1 or pos + 7 > len(bech):
        raise ValueError("invalid bech32 separator position")
    hrp = bech[:pos]
    if any(ord(c) < 33 or ord(c) > 126 for c in hrp):
        raise ValueError("invalid character in HRP")
    data: List[int] = []
    for c in bech[pos + 1 :]:
        if c not in CHARSET:
            raise ValueError(f"invalid bech32 data character: {c!r}")
        data.append(CHARSET.index(c))
    if not _verify_checksum(hrp, data):
        raise ValueError("bech32 checksum mismatch")
    return hrp, data[:-6]


def convertbits(data: List[int], frombits: int, tobits: int, pad: bool = True) -> List[int]:
    """Repack a stream of integers from frombits-per-element to tobits-per-element."""
    acc = 0
    bits = 0
    ret: List[int] = []
    maxv = (1 << tobits) - 1
    max_acc = (1 << (frombits + tobits - 1)) - 1
    for value in data:
        if value < 0 or (value >> frombits):
            raise ValueError("convertbits: value out of range")
        acc = ((acc << frombits) | value) & max_acc
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad:
        if bits:
            ret.append((acc << (tobits - bits)) & maxv)
    elif bits >= frombits or ((acc << (tobits - bits)) & maxv):
        raise ValueError("convertbits: invalid padding")
    return ret


# --------------------------------------------------------------------------- #
# NIP-19 — simple types (npub, nsec, note)                                    #
# --------------------------------------------------------------------------- #

def _encode_raw32(hrp: str, raw_32: bytes) -> str:
    if len(raw_32) != 32:
        raise ValueError(f"{hrp} payload must be 32 bytes, got {len(raw_32)}")
    return bech32_encode(hrp, convertbits(list(raw_32), 8, 5, pad=True))


def _decode_raw32(expected_hrp: str, encoded: str) -> bytes:
    hrp, data = bech32_decode(encoded)
    if hrp != expected_hrp:
        raise ValueError(f"expected hrp {expected_hrp!r}, got {hrp!r}")
    raw = bytes(convertbits(data, 5, 8, pad=False))
    if len(raw) != 32:
        raise ValueError(f"{expected_hrp} payload must decode to 32 bytes, got {len(raw)}")
    return raw


def encode_npub(pubkey_hex: str) -> str:
    """Hex 32-byte pubkey -> npub1…."""
    return _encode_raw32("npub", bytes.fromhex(pubkey_hex))


def decode_npub(npub: str) -> str:
    """npub1… -> hex 32-byte pubkey."""
    return _decode_raw32("npub", npub).hex()


def encode_nsec(sk_hex: str) -> str:
    """Hex 32-byte secret key -> nsec1…."""
    return _encode_raw32("nsec", bytes.fromhex(sk_hex))


def decode_nsec(nsec: str) -> str:
    """nsec1… -> hex 32-byte secret key."""
    return _decode_raw32("nsec", nsec).hex()


def encode_note(event_id_hex: str) -> str:
    """Hex 32-byte event id -> note1…."""
    return _encode_raw32("note", bytes.fromhex(event_id_hex))


def decode_note(note: str) -> str:
    """note1… -> hex 32-byte event id."""
    return _decode_raw32("note", note).hex()


# --------------------------------------------------------------------------- #
# NIP-19 — TLV types (naddr)                                                  #
# --------------------------------------------------------------------------- #
#
# Each TLV entry is [type(1 byte) | length(1 byte) | value(length bytes)].
# Per NIP-19:
#   type 0 (special)   identifier — d-tag value, UTF-8, may be empty
#   type 1 (relay)     optional, repeatable, ASCII relay URL
#   type 2 (author)    author pubkey, 32 raw bytes
#   type 3 (kind)      event kind, big-endian uint32 (4 bytes)
# Length is a single byte (cap 255), enforced explicitly below.

_TLV_SPECIAL: int = 0
_TLV_RELAY: int = 1
_TLV_AUTHOR: int = 2
_TLV_KIND: int = 3
_TLV_MAX_VALUE_LEN: int = 255


def _tlv(t: int, value: bytes) -> bytes:
    if len(value) > _TLV_MAX_VALUE_LEN:
        raise ValueError(f"TLV value of type {t} is {len(value)} bytes (max {_TLV_MAX_VALUE_LEN})")
    return bytes([t, len(value)]) + value


def encode_nprofile(
    pubkey_hex: str,
    relays: list[str] | tuple[str, ...] = (),
) -> str:
    """Encode a pubkey + relay hints as ``nprofile1…`` (NIP-19 TLV).

    TLV layout:
      type 0 — pubkey (32 raw bytes)
      type 1 — relay  (ASCII URL, repeatable)
    """
    pk = bytes.fromhex(pubkey_hex)
    if len(pk) != 32:
        raise ValueError("pubkey must be 32 bytes")
    payload = _tlv(_TLV_SPECIAL, pk)
    for relay in relays:
        payload += _tlv(_TLV_RELAY, relay.encode("ascii"))
    return bech32_encode("nprofile", convertbits(list(payload), 8, 5, pad=True))


def decode_nprofile(nprofile: str) -> tuple[str, list[str]]:
    """Inverse of ``encode_nprofile``. Returns (pubkey_hex, relays)."""
    hrp, data = bech32_decode(nprofile)
    if hrp != "nprofile":
        raise ValueError(f"expected hrp 'nprofile', got {hrp!r}")
    raw = bytes(convertbits(data, 5, 8, pad=False))

    pubkey_hex: str | None = None
    relays: list[str] = []
    i = 0
    while i < len(raw):
        if i + 2 > len(raw):
            raise ValueError("truncated TLV header in nprofile")
        t = raw[i]
        ln = raw[i + 1]
        i += 2
        if i + ln > len(raw):
            raise ValueError(f"TLV value of type {t} runs past payload")
        value = raw[i : i + ln]
        i += ln
        if t == _TLV_SPECIAL:
            if len(value) != 32:
                raise ValueError(f"pubkey TLV must be 32 bytes, got {len(value)}")
            pubkey_hex = value.hex()
        elif t == _TLV_RELAY:
            relays.append(value.decode("ascii"))
        # Unknown types: ignore for forward compatibility.

    if pubkey_hex is None:
        raise ValueError("nprofile missing required pubkey TLV")
    return pubkey_hex, relays


def encode_naddr(
    identifier: str,
    author_pubkey_hex: str,
    kind: int,
    relays: list[str] | tuple[str, ...] = (),
) -> str:
    """Encode a parameterized-replaceable-event address as ``naddr1…``.

    Used to share long-form articles (kind 30023). ``identifier`` is the
    ``d``-tag value of the event; ``relays`` is the optional hint set
    where readers can find it.
    """
    if not (0 <= kind <= 0xFFFFFFFF):
        raise ValueError("kind must fit in u32")
    author = bytes.fromhex(author_pubkey_hex)
    if len(author) != 32:
        raise ValueError("author pubkey must be 32 bytes")

    payload = b""
    payload += _tlv(_TLV_SPECIAL, identifier.encode("utf-8"))
    for relay in relays:
        payload += _tlv(_TLV_RELAY, relay.encode("ascii"))
    payload += _tlv(_TLV_AUTHOR, author)
    payload += _tlv(_TLV_KIND, kind.to_bytes(4, "big"))

    return bech32_encode("naddr", convertbits(list(payload), 8, 5, pad=True))


def decode_naddr(naddr: str) -> tuple[str, str, int, list[str]]:
    """Inverse of ``encode_naddr``. Returns (identifier, author_hex, kind, relays).

    Tolerant of TLV ordering and ignores unknown TLV types so newer naddrs
    don't break us.
    """
    hrp, data = bech32_decode(naddr)
    if hrp != "naddr":
        raise ValueError(f"expected hrp 'naddr', got {hrp!r}")
    raw = bytes(convertbits(data, 5, 8, pad=False))

    identifier: str | None = None
    author_hex: str | None = None
    kind: int | None = None
    relays: list[str] = []

    i = 0
    while i < len(raw):
        if i + 2 > len(raw):
            raise ValueError("truncated TLV header in naddr")
        t = raw[i]
        ln = raw[i + 1]
        i += 2
        if i + ln > len(raw):
            raise ValueError(f"TLV value of type {t} runs past payload")
        value = raw[i : i + ln]
        i += ln
        if t == _TLV_SPECIAL:
            identifier = value.decode("utf-8")
        elif t == _TLV_RELAY:
            relays.append(value.decode("ascii"))
        elif t == _TLV_AUTHOR:
            if len(value) != 32:
                raise ValueError(f"author TLV must be 32 bytes, got {len(value)}")
            author_hex = value.hex()
        elif t == _TLV_KIND:
            if len(value) != 4:
                raise ValueError(f"kind TLV must be 4 bytes, got {len(value)}")
            kind = int.from_bytes(value, "big")
        # Unknown types: ignore silently for forward compatibility.

    if identifier is None or author_hex is None or kind is None:
        raise ValueError("naddr missing required TLV(s)")
    return identifier, author_hex, kind, relays
