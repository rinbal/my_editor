# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""NIP-49: a private key protected by a password (``ncryptsec1...``).

Spec: https://github.com/nostr-protocol/nips/blob/master/49.md

Used for the backup file Create Account offers and for signing in with a
protected key. The format is shared by most Nostr apps, so a backup made
here can be imported elsewhere and the other way round.

    password  -> NFKC normalized, UTF-8
    key       = scrypt(password, salt[16], N = 2^log_n, r = 8, p = 1, 32 bytes)
    payload   = 0x02 | log_n | salt[16] | nonce[24] | key_security[1]
                | XChaCha20-Poly1305(key, nonce, secret[32], aad = key_security)
    result    = bech32("ncryptsec", payload)

XChaCha20-Poly1305 is built from HChaCha20 and the IETF ChaCha20-Poly1305
the cryptography package provides, as the XChaCha draft defines it: the
first 16 nonce bytes derive a subkey, the last 8 (behind four zero bytes)
are the IETF nonce.

``key_security`` records how the key was handled before it was encrypted:
0x00 known to have been handled insecurely, 0x01 known not to have been,
0x02 unknown. MyEditor does not follow a key's history (it can be copied
to the clipboard, or have come from a paste), so it always writes 0x02.

``log_n`` sets how much work opening the key takes. New files use 2^16;
files from other apps open with anything up to 2^20 (1 GiB of memory),
including weaker settings, because refusing them would lock a person out
of their own key. Above 2^20 Python's scrypt can't allocate the memory, so
such a file is explained instead of failing with a library message.
"""

from __future__ import annotations

import hashlib
import os
import struct
import unicodedata

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from nostr import bech32

HRP = "ncryptsec"
VERSION = 0x02
DEFAULT_LOG_N = 16          # 64 MiB of memory, about a second to open: the spec's suggestion

KEY_INSECURE = 0x00
KEY_SECURE = 0x01
KEY_UNKNOWN = 0x02

_MIN_LOG_N = 16             # the weakest setting MyEditor writes
_MAX_LOG_N = 20             # 1 GiB of scrypt memory; hashlib.scrypt can't go higher


class Nip49Error(ValueError):
    """Why an ncryptsec could not be opened. ``wrong_password`` tells the
    two cases a person can act on apart from a damaged file."""

    def __init__(self, message: str, *, wrong_password: bool = False):
        super().__init__(message)
        self.wrong_password = wrong_password


def encrypt(secret_key: bytes, password: str, *, log_n: int = DEFAULT_LOG_N,
            key_security: int = KEY_UNKNOWN, salt: bytes = None, nonce: bytes = None) -> str:
    """``ncryptsec1...`` for ``secret_key`` (32 bytes) under ``password``.
    ``salt`` and ``nonce`` exist for test vectors; leave them out."""
    if len(secret_key) != 32:
        raise ValueError("a secret key is 32 bytes")
    if key_security not in (KEY_INSECURE, KEY_SECURE, KEY_UNKNOWN):
        raise ValueError("unknown key security byte")
    if not _MIN_LOG_N <= log_n <= _MAX_LOG_N:
        raise ValueError(f"log_n must be between {_MIN_LOG_N} and {_MAX_LOG_N}")
    salt = salt if salt is not None else os.urandom(16)
    nonce = nonce if nonce is not None else os.urandom(24)
    key = _derive(password, salt, log_n)
    aad = bytes([key_security])
    sealed = _xchacha_seal(key, nonce, secret_key, aad)
    payload = bytes([VERSION, log_n]) + salt + nonce + aad + sealed
    return bech32.bech32_encode(HRP, bech32.convertbits(list(payload), 8, 5))


def decrypt(ncryptsec: str, password: str) -> bytes:
    """The 32-byte secret key, or Nip49Error."""
    try:
        hrp, data = bech32.bech32_decode(ncryptsec.strip().lower())
    except Exception as exc:  # noqa: BLE001, any decoding failure is a damaged key
        raise Nip49Error("This isn’t a complete protected key.") from exc
    if hrp != HRP:
        raise Nip49Error("This isn’t a protected key.")
    try:
        payload = bytes(bech32.convertbits(data, 5, 8, False))
    except ValueError as exc:
        raise Nip49Error("This isn’t a complete protected key.") from exc
    if len(payload) != 1 + 1 + 16 + 24 + 1 + 48 or payload[0] != VERSION:
        raise Nip49Error("This protected key uses a format MyEditor doesn’t know.")
    log_n = payload[1]
    if log_n == 0:
        raise Nip49Error("This protected key is damaged.")
    if log_n > _MAX_LOG_N:
        raise Nip49Error("This protected key was saved with settings that need more "
                         "memory to open than MyEditor can use.")
    salt, nonce = payload[2:18], payload[18:42]
    aad, sealed = payload[42:43], payload[43:]
    try:
        key = _derive(password, salt, log_n)
    except (ValueError, MemoryError, OverflowError) as exc:
        raise Nip49Error("This computer couldn’t open this protected key. It may "
                         "need more memory than is free right now.") from exc
    try:
        secret = _xchacha_open(key, nonce, sealed, aad)
    except InvalidTag as exc:
        raise Nip49Error("The password is wrong.", wrong_password=True) from exc
    if len(secret) != 32:
        raise Nip49Error("This protected key is damaged.")
    return secret


# -- primitives ---------------------------------------------------------------------

def _derive(password: str, salt: bytes, log_n: int) -> bytes:
    normalized = unicodedata.normalize("NFKC", password or "").encode("utf-8")
    n = 1 << log_n
    # scrypt needs 128 * r * (N + 2) bytes; give it that plus headroom.
    # At log_n 20 this stays under the 2 GiB hashlib.scrypt accepts.
    return hashlib.scrypt(normalized, salt=salt, n=n, r=8, p=1, dklen=32,
                          maxmem=128 * 8 * n + 1024 * 1024)


def _rotl(v: int, c: int) -> int:
    return ((v << c) & 0xFFFFFFFF) | (v >> (32 - c))


def _quarter(s, a, b, c, d) -> None:
    s[a] = (s[a] + s[b]) & 0xFFFFFFFF; s[d] = _rotl(s[d] ^ s[a], 16)  # noqa: E702
    s[c] = (s[c] + s[d]) & 0xFFFFFFFF; s[b] = _rotl(s[b] ^ s[c], 12)  # noqa: E702
    s[a] = (s[a] + s[b]) & 0xFFFFFFFF; s[d] = _rotl(s[d] ^ s[a], 8)   # noqa: E702
    s[c] = (s[c] + s[d]) & 0xFFFFFFFF; s[b] = _rotl(s[b] ^ s[c], 7)   # noqa: E702


def hchacha20(key: bytes, nonce16: bytes) -> bytes:
    """The HChaCha20 subkey (draft-irtf-cfrg-xchacha, section 2.2)."""
    state = [0x61707865, 0x3320646E, 0x79622D32, 0x6B206574]
    state += list(struct.unpack("<8I", key)) + list(struct.unpack("<4I", nonce16))
    for _ in range(10):
        _quarter(state, 0, 4, 8, 12)
        _quarter(state, 1, 5, 9, 13)
        _quarter(state, 2, 6, 10, 14)
        _quarter(state, 3, 7, 11, 15)
        _quarter(state, 0, 5, 10, 15)
        _quarter(state, 1, 6, 11, 12)
        _quarter(state, 2, 7, 8, 13)
        _quarter(state, 3, 4, 9, 14)
    return struct.pack("<8I", *(state[0:4] + state[12:16]))


def _xchacha_seal(key: bytes, nonce24: bytes, plaintext: bytes, aad: bytes) -> bytes:
    subkey = hchacha20(key, nonce24[:16])
    return ChaCha20Poly1305(subkey).encrypt(b"\x00" * 4 + nonce24[16:], plaintext, aad)


def _xchacha_open(key: bytes, nonce24: bytes, sealed: bytes, aad: bytes) -> bytes:
    subkey = hchacha20(key, nonce24[:16])
    return ChaCha20Poly1305(subkey).decrypt(b"\x00" * 4 + nonce24[16:], sealed, aad)
