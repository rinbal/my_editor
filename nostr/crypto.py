"""Cryptographic primitives for Nostr (BIP-340 schnorr + NIP-44 v2).

All public functions in this module are pure: they take bytes in and return
bytes (or str for base64 payloads). State lives nowhere — the caller owns
every key.

References:
  - BIP-340: https://github.com/bitcoin/bips/blob/master/bip-0340.mediawiki
  - NIP-01:  https://github.com/nostr-protocol/nips/blob/master/01.md
  - NIP-44:  https://github.com/nostr-protocol/nips/blob/master/44.md

Verified against the official NIP-44 v2 vectors at
https://github.com/paulmillr/nip44/blob/main/nip44.vectors.json — see
tests/test_nip44.py.
"""

from __future__ import annotations

import base64
import hmac
import os
from hashlib import sha256
from typing import Tuple

from coincurve import PrivateKey, PublicKey, PublicKeyXOnly
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms

# --------------------------------------------------------------------------- #
# NIP-44 v2 protocol constants                                                #
# --------------------------------------------------------------------------- #

NIP44_VERSION: int = 2
NIP44_SALT: bytes = b"nip44-v2"
NIP44_MIN_PLAINTEXT_LEN: int = 1
NIP44_MAX_PLAINTEXT_LEN: int = 65535


# --------------------------------------------------------------------------- #
# Keys & signing (BIP-340 schnorr)                                            #
# --------------------------------------------------------------------------- #

def generate_secret_key() -> bytes:
    """Return 32 random bytes valid as a secp256k1 private key.

    Rejects keys >= curve order n; the probability of needing a retry is
    cryptographically negligible but the check is still required for
    correctness.
    """
    while True:
        candidate = os.urandom(32)
        try:
            PrivateKey(candidate)
            return candidate
        except ValueError:
            continue


def get_public_key(sk: bytes) -> bytes:
    """Return the 32-byte x-only public key for ``sk`` (BIP-340)."""
    if len(sk) != 32:
        raise ValueError("secret key must be 32 bytes")
    return PrivateKey(sk).public_key_xonly.format()


def sign_schnorr(sk: bytes, message_32: bytes, aux_random: bytes | None = None) -> bytes:
    """Return a 64-byte BIP-340 schnorr signature over ``message_32``.

    ``aux_random`` defaults to fresh OS randomness, matching the
    recommendation in BIP-340 §3.3.
    """
    if len(sk) != 32:
        raise ValueError("secret key must be 32 bytes")
    if len(message_32) != 32:
        raise ValueError("message must be 32 bytes")
    if aux_random is None:
        aux_random = os.urandom(32)
    return PrivateKey(sk).sign_schnorr(message_32, aux_random)


def verify_schnorr(pubkey_xonly: bytes, sig: bytes, message_32: bytes) -> bool:
    """Verify a BIP-340 schnorr signature. Returns False on any failure."""
    if len(pubkey_xonly) != 32 or len(sig) != 64 or len(message_32) != 32:
        return False
    try:
        return PublicKeyXOnly(pubkey_xonly).verify(sig, message_32)
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# ECDH — raw x-coordinate (NOT the libsecp256k1 default SHA256-hashed point)  #
# --------------------------------------------------------------------------- #

def ecdh_x(sk: bytes, pubkey_xonly: bytes) -> bytes:
    """Return the 32-byte x-coordinate of ``sk * pubkey``.

    NIP-44 (and Nostr generally) uses the raw x-coordinate, not the
    libsecp256k1 default which would SHA256-hash the compressed shared
    point. We multiply the point manually and slice off the x bytes.

    The BIP-340 convention is that x-only pubkeys have even y; we prepend
    0x02 to recover the full compressed encoding.
    """
    if len(sk) != 32:
        raise ValueError("secret key must be 32 bytes")
    if len(pubkey_xonly) != 32:
        raise ValueError("pubkey must be 32 bytes (x-only)")
    pk = PublicKey(b"\x02" + pubkey_xonly)
    shared = pk.multiply(sk)
    # format(compressed=False) returns 0x04 || x(32) || y(32)
    return shared.format(compressed=False)[1:33]


# --------------------------------------------------------------------------- #
# NIP-44 v2 — key derivation                                                  #
# --------------------------------------------------------------------------- #

def _hkdf_extract(salt: bytes, ikm: bytes) -> bytes:
    """HKDF-Extract (RFC 5869) with SHA-256 — single HMAC call."""
    return hmac.new(salt, ikm, sha256).digest()


def _hkdf_expand(prk: bytes, info: bytes, length: int) -> bytes:
    """HKDF-Expand (RFC 5869) with SHA-256.

    NIP-44 v2 needs exactly 76 bytes (one iteration) so this loop will
    almost always run once, but the general form is cheap and correct.
    """
    if length > 255 * 32:
        raise ValueError("HKDF-Expand length exceeds 255 * HashLen")
    out = b""
    block = b""
    counter = 1
    while len(out) < length:
        block = hmac.new(prk, block + info + bytes([counter]), sha256).digest()
        out += block
        counter += 1
    return out[:length]


def conversation_key(sk: bytes, pubkey_xonly: bytes) -> bytes:
    """Derive the 32-byte NIP-44 v2 conversation key for a (sk, pk) pair.

    conversation_key = HKDF-Extract(salt = "nip44-v2", IKM = ecdh_x(sk, pk))
    """
    return _hkdf_extract(NIP44_SALT, ecdh_x(sk, pubkey_xonly))


def message_keys(conv_key: bytes, nonce: bytes) -> Tuple[bytes, bytes, bytes]:
    """Derive (chacha_key, chacha_nonce, hmac_key) from conv_key and nonce.

    Per NIP-44 v2:
      keys = HKDF-Expand(prk = conv_key, info = nonce, L = 76)
      chacha_key   = keys[0:32]
      chacha_nonce = keys[32:44]   # 12 bytes
      hmac_key     = keys[44:76]
    """
    if len(conv_key) != 32:
        raise ValueError("conversation key must be 32 bytes")
    if len(nonce) != 32:
        raise ValueError("nonce must be 32 bytes")
    keys = _hkdf_expand(conv_key, nonce, 76)
    return keys[0:32], keys[32:44], keys[44:76]


# --------------------------------------------------------------------------- #
# NIP-44 v2 — padding                                                         #
# --------------------------------------------------------------------------- #

def calc_padded_len(plaintext_len: int) -> int:
    """Return the padded plaintext length per NIP-44 v2 §3.3.

    The output is the size of the buffer *before* the 2-byte length
    prefix is prepended.
    """
    if plaintext_len < NIP44_MIN_PLAINTEXT_LEN:
        raise ValueError("plaintext too short")
    # The upper plaintext bound (65535 bytes) is enforced by encrypt(); the
    # math here is exercised for boundary values like 65536 in the spec
    # vectors, so calc_padded_len itself stays pure.
    if plaintext_len <= 32:
        return 32
    # next power of two >= plaintext_len
    next_power = 1 << ((plaintext_len - 1).bit_length())
    chunk = 32 if next_power <= 256 else next_power // 8
    return chunk * ((plaintext_len - 1) // chunk + 1)


def _pad_plaintext(plaintext_bytes: bytes) -> bytes:
    """u16_be(len) || plaintext || zeros — total length 2 + calc_padded_len(len)."""
    plen = len(plaintext_bytes)
    padded = calc_padded_len(plen)
    return plen.to_bytes(2, "big") + plaintext_bytes + b"\x00" * (padded - plen)


def _unpad_plaintext(padded: bytes) -> bytes:
    """Inverse of _pad_plaintext, with strict length validation."""
    if len(padded) < 2:
        raise ValueError("padded plaintext too short for length prefix")
    plen = int.from_bytes(padded[0:2], "big")
    if plen < NIP44_MIN_PLAINTEXT_LEN or plen > NIP44_MAX_PLAINTEXT_LEN:
        raise ValueError("declared plaintext length out of bounds")
    plaintext = padded[2 : 2 + plen]
    if len(plaintext) != plen:
        raise ValueError("declared plaintext length exceeds buffer")
    expected_total = 2 + calc_padded_len(plen)
    if len(padded) != expected_total:
        raise ValueError("padded buffer length does not match spec")
    return plaintext


# --------------------------------------------------------------------------- #
# NIP-44 v2 — ChaCha20 + HMAC                                                 #
# --------------------------------------------------------------------------- #

def _chacha20(key: bytes, nonce_12: bytes, data: bytes) -> bytes:
    """RFC 7539 ChaCha20 with counter = 0.

    ``cryptography``'s ChaCha20 takes a 16-byte "nonce" which is internally
    treated as ``counter(4 LE) || nonce(12)``. Starting counter is zero per
    NIP-44, so we prepend four zero bytes.
    """
    if len(key) != 32:
        raise ValueError("chacha key must be 32 bytes")
    if len(nonce_12) != 12:
        raise ValueError("chacha nonce must be 12 bytes")
    cipher = Cipher(algorithms.ChaCha20(key, b"\x00\x00\x00\x00" + nonce_12), mode=None)
    encryptor = cipher.encryptor()
    return encryptor.update(data) + encryptor.finalize()


def _hmac_aad(hmac_key: bytes, nonce_32: bytes, ciphertext: bytes) -> bytes:
    """HMAC-SHA256 keyed by hmac_key over nonce || ciphertext (no version byte)."""
    return hmac.new(hmac_key, nonce_32 + ciphertext, sha256).digest()


# --------------------------------------------------------------------------- #
# NIP-44 v2 — public API                                                      #
# --------------------------------------------------------------------------- #

def encrypt(plaintext: str, conv_key: bytes, nonce: bytes | None = None) -> str:
    """Encrypt ``plaintext`` (UTF-8) under ``conv_key``. Returns base64 payload.

    ``nonce`` is for deterministic test vectors only. In production always
    let it default to fresh OS randomness — nonce reuse with the same
    conversation key is catastrophic.
    """
    pt_bytes = plaintext.encode("utf-8")
    if len(pt_bytes) < NIP44_MIN_PLAINTEXT_LEN:
        raise ValueError("plaintext must not be empty")
    if len(pt_bytes) > NIP44_MAX_PLAINTEXT_LEN:
        raise ValueError("plaintext exceeds 65535 bytes")
    if nonce is None:
        nonce = os.urandom(32)
    elif len(nonce) != 32:
        raise ValueError("nonce must be 32 bytes")

    chacha_key, chacha_nonce, hmac_key = message_keys(conv_key, nonce)
    padded = _pad_plaintext(pt_bytes)
    ciphertext = _chacha20(chacha_key, chacha_nonce, padded)
    mac = _hmac_aad(hmac_key, nonce, ciphertext)
    payload = bytes([NIP44_VERSION]) + nonce + ciphertext + mac
    return base64.b64encode(payload).decode("ascii")


def decrypt(payload_b64: str, conv_key: bytes) -> str:
    """Decrypt a NIP-44 v2 payload. Raises ValueError on any malformedness.

    Validation order matters: MAC is checked with ``hmac.compare_digest``
    before any decryption work so a wrong key fails fast without leaking
    timing.
    """
    if not payload_b64:
        raise ValueError("empty payload")
    if payload_b64.startswith("#"):
        # Future-versioned payloads start with '#'. Treat as unsupported
        # rather than try to b64-decode and produce a misleading error.
        raise ValueError("unsupported NIP-44 version (future format)")
    try:
        payload = base64.b64decode(payload_b64, validate=True)
    except Exception as exc:
        raise ValueError("invalid base64") from exc

    # Smallest valid payload: version(1) + nonce(32) + min_ct(2 + 32) + mac(32) = 99
    # Largest valid payload: version(1) + nonce(32) + (2 + 65536) + mac(32) = 65603
    if len(payload) < 99 or len(payload) > 65603:
        raise ValueError("payload length out of valid range")
    if payload[0] != NIP44_VERSION:
        raise ValueError(f"unsupported version byte 0x{payload[0]:02x}")

    nonce = payload[1:33]
    mac = payload[-32:]
    ciphertext = payload[33:-32]

    chacha_key, chacha_nonce, hmac_key = message_keys(conv_key, nonce)
    expected_mac = _hmac_aad(hmac_key, nonce, ciphertext)
    if not hmac.compare_digest(expected_mac, mac):
        raise ValueError("invalid MAC")

    padded = _chacha20(chacha_key, chacha_nonce, ciphertext)
    return _unpad_plaintext(padded).decode("utf-8")


# --------------------------------------------------------------------------- #
# Convenience: encrypt/decrypt directly between two keypairs                  #
# --------------------------------------------------------------------------- #

def encrypt_to(plaintext: str, sk: bytes, pubkey_xonly: bytes) -> str:
    """Derive the conversation key on the fly and encrypt."""
    return encrypt(plaintext, conversation_key(sk, pubkey_xonly))


def decrypt_from(payload_b64: str, sk: bytes, pubkey_xonly: bytes) -> str:
    """Derive the conversation key on the fly and decrypt."""
    return decrypt(payload_b64, conversation_key(sk, pubkey_xonly))
