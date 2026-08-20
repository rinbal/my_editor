# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Encrypt a file so a media host stores bytes it cannot read.

This is deliberately not our own scheme. It is the one Lotus writes, so
a private file uploaded there opens here and the other way round. Any
"improvement" to the construction below silently breaks that, which is
the whole reason the format is pinned by a vector generated from the
same WebCrypto calls Lotus makes.

The envelope::

    version(1) = 0x02 || nonce(32) || AES-256-GCM ciphertext

and the two keys come from one HKDF pass over a NIP-44 conversation key::

    derived = HKDF-SHA256(ikm=conversation_key, salt=nonce,
                          info="nip44-v2", length=44)
    aes_key = derived[0:32]
    iv      = derived[32:44]

Two things about that look wrong and are not. The info string says
nip44-v2 while the cipher is AES-GCM rather than ChaCha20, because the
browser this format came from has no ChaCha20 in WebCrypto and kept
NIP-44's shape around a cipher it did have. And the conversation key is
derived from a fresh per-file secret key against *its own* public key,
so the file key alone decrypts the file and nothing about the user's
identity is involved. That is what lets a key be handed to someone else
without handing over anything of the account.

The per-file key is the secret. This module never persists it; the
caller decides where it lives.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .. import crypto


ENVELOPE_VERSION: Final[int] = 2
_INFO: Final[bytes] = b"nip44-v2"
_NONCE_LEN: Final[int] = 32
_TAG_LEN: Final[int] = 16
_HEADER_LEN: Final[int] = 1 + _NONCE_LEN

# The smallest possible envelope: header plus an empty ciphertext, which
# is still a GCM tag.
MIN_ENVELOPE_LEN: Final[int] = _HEADER_LEN + _TAG_LEN

# AES-GCM here works on whole buffers, so encrypting costs roughly twice
# the file in memory. The association's own per-file ceiling is 1 GiB,
# and doubling that on a laptop is not something to attempt silently, so
# a file past this is refused with a real message instead of an
# out-of-memory kill.
MAX_PLAINTEXT_BYTES: Final[int] = 256 * 1024 * 1024


class FileCryptoError(Exception):
    """The bytes could not be encrypted or decrypted as asked."""


@dataclass(frozen=True)
class EncryptedFile:
    """Ciphertext plus the only key that opens it."""

    envelope: bytes
    key_hex: str


def _derive(conversation_key: bytes, nonce: bytes) -> tuple[bytes, bytes]:
    """Split one HKDF output into the AES key and the GCM nonce.

    WebCrypto's deriveBits is extract-then-expand, so this is too. Doing
    only the expand half would produce different keys and a file neither
    side could read.
    """
    prk = crypto._hkdf_extract(nonce, conversation_key)
    derived = crypto._hkdf_expand(prk, _INFO, 44)
    return derived[:32], derived[32:44]


def conversation_key_for(key_hex: str) -> bytes:
    """The NIP-44 conversation key a file key stands for.

    Self-directed on purpose: the file key is both halves of the
    exchange, so possession of it is the entire authorisation.
    """
    try:
        secret = bytes.fromhex(key_hex)
    except ValueError as exc:
        raise FileCryptoError("file key is not hex") from exc
    if len(secret) != 32:
        raise FileCryptoError("file key must be 32 bytes")
    return crypto.conversation_key(secret, crypto.get_public_key(secret))


def encrypt_file(
    plaintext: bytes,
    *,
    key_hex: str | None = None,
    nonce: bytes | None = None,
) -> EncryptedFile:
    """Encrypt ``plaintext`` under a fresh per-file key.

    ``key_hex`` and ``nonce`` exist so a test can pin a known vector.
    Production callers pass neither: a reused nonce under a reused key
    is the one mistake GCM does not survive.
    """
    if not isinstance(plaintext, (bytes, bytearray)):
        raise FileCryptoError("plaintext must be bytes")
    if len(plaintext) > MAX_PLAINTEXT_BYTES:
        raise FileCryptoError(
            f"file is larger than the {MAX_PLAINTEXT_BYTES // 1024 ** 2} MB "
            f"encryption limit"
        )
    secret = bytes.fromhex(key_hex) if key_hex else crypto.generate_secret_key()
    if len(secret) != 32:
        raise FileCryptoError("file key must be 32 bytes")
    nonce = nonce if nonce is not None else os.urandom(_NONCE_LEN)
    if len(nonce) != _NONCE_LEN:
        raise FileCryptoError("nonce must be 32 bytes")

    conversation_key = crypto.conversation_key(secret, crypto.get_public_key(secret))
    aes_key, iv = _derive(conversation_key, nonce)
    ciphertext = AESGCM(aes_key).encrypt(iv, bytes(plaintext), None)
    envelope = bytes([ENVELOPE_VERSION]) + nonce + ciphertext
    return EncryptedFile(envelope=envelope, key_hex=secret.hex())


def decrypt_file(envelope: bytes, key_hex: str) -> bytes:
    """Recover the plaintext, or raise.

    A wrong key and a tampered file are the same error on purpose. GCM
    authenticates, so there is no partial answer to hand back and no way
    to tell the two apart without leaking which one it was.
    """
    if not isinstance(envelope, (bytes, bytearray)):
        raise FileCryptoError("envelope must be bytes")
    if len(envelope) < MIN_ENVELOPE_LEN:
        raise FileCryptoError("not an encrypted file")
    if envelope[0] != ENVELOPE_VERSION:
        raise FileCryptoError(
            f"unsupported envelope version {envelope[0]}"
        )
    nonce = bytes(envelope[1:_HEADER_LEN])
    ciphertext = bytes(envelope[_HEADER_LEN:])
    aes_key, iv = _derive(conversation_key_for(key_hex), nonce)
    try:
        return AESGCM(aes_key).decrypt(iv, ciphertext, None)
    except InvalidTag as exc:
        raise FileCryptoError(
            "the file could not be decrypted with this key"
        ) from exc


def looks_encrypted(data: bytes) -> bool:
    """Whether ``data`` is shaped like one of these envelopes.

    A weak test, and knowingly so: it reads one version byte and a
    length. Real images start with their own magic numbers and none of
    them is 0x02, so this is enough to tell a private blob from a
    picture, which is all the library needs. It is not enough to prove a
    blob is ours, so it must never gate anything but presentation.
    """
    return (
        isinstance(data, (bytes, bytearray))
        and len(data) >= MIN_ENVELOPE_LEN
        and data[0] == ENVELOPE_VERSION
    )
