# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a submission is encrypted with (editorial contract 3, 4, 6, 7).

- The content key ``K`` comes from the submission key ``s_sub`` by
  HKDF-SHA256 (3).
- The revision travels as one AES-256-GCM envelope under ``K``, its
  submission and revision bound in as associated data (6).
- Images and audio are SMF1 files: fixed-size chunks under a random
  per-file key, each chunk's number and "last" flag bound in, so a file
  that was cut short, extended or reordered does not decrypt (7). The
  :class:`MediaEncryptor` and :class:`_MediaDecryptor` take the data in
  pieces, so a 90 MB episode is never held twice.
- A key wrap carries a secret to one recipient without a signer call
  (fresh ephemeral key, NIP-44), and its plaintext names its purpose and
  what it belongs to, which the recipient checks (4).

What opens anything (an envelope, a wrap, a media file, a submission key
in a wrap) is private to this module and nostr/standup/opening.py, which
calls it only for a revision it verified (12.4): nothing else may open on
the strength of what a server serves (RG-M1 12).

AES-GCM is the ``cryptography`` package's, as in nostr/media/filecrypto.py.
Pure: no Qt, no network. Errors are :class:`StandupCryptoError` (a
ValueError) and never carry a key or a plaintext.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Callable, Dict, List, Optional, Sequence

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .. import crypto as nostr_crypto
from . import formats
from .formats import FormatError

_NONCE = 12
_TAG = 16
_MAGIC = b"SMF1"
_HEADER = 8


class StandupCryptoError(ValueError):
    """Something did not decrypt or does not belong where it was found."""


class WrapInvalid(StandupCryptoError):
    """A key wrap that does not open, or opens to a key for something else."""


# --------------------------------------------------------------------------- #
# Keys (3)                                                                     #
# --------------------------------------------------------------------------- #

def new_secret_key() -> bytes:
    """32 random bytes that are a valid secp256k1 secret (a submission key)."""
    return nostr_crypto.generate_secret_key()


def pubkey_of(secret: bytes) -> str:
    return nostr_crypto.get_public_key(bytes(secret)).hex()


def new_status_key() -> str:
    """32 random bytes, hex (2): the writer's app keeps it to follow a
    submission without signing."""
    return os.urandom(32).hex()


def content_key(s_sub: bytes) -> bytes:
    """``K = HKDF-SHA256(ikm = s_sub, salt = "standup-editorial-v1",
    info = "content", L = 32)``."""
    if not isinstance(s_sub, (bytes, bytearray)) or len(s_sub) != 32:
        raise StandupCryptoError("a submission key is 32 bytes")
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=formats.CONTENT_KEY_SALT,
                info=formats.CONTENT_KEY_INFO).derive(bytes(s_sub))


def secret_from_hex(value: Any) -> bytes:
    """A secret key given as hex, checked to be a valid scalar."""
    if not formats.is_hex64(value):
        raise StandupCryptoError("a secret key is 64 hex digits")
    secret = bytes.fromhex(value)
    try:
        pubkey_of(secret)
    except ValueError:
        raise StandupCryptoError("not a valid secret key") from None
    return secret


# --------------------------------------------------------------------------- #
# The envelope (6)                                                             #
# --------------------------------------------------------------------------- #

def encrypt_envelope(key: bytes, plaintext: bytes, submission_id: str, revision: int,
                     *, nonce: Optional[bytes] = None) -> bytes:
    """``nonce || AES-256-GCM(K, nonce, plaintext, aad)``. ``nonce`` is
    for test vectors only."""
    aad = formats.envelope_aad(submission_id, revision)
    nonce = nonce if nonce is not None else os.urandom(_NONCE)
    if len(nonce) != _NONCE:
        raise StandupCryptoError("an envelope nonce is 12 bytes")
    sealed = nonce + AESGCM(bytes(key)).encrypt(nonce, bytes(plaintext), aad)
    if len(sealed) > formats.MAX_ENVELOPE_BYTES:
        raise StandupCryptoError("the envelope is larger than 4 MiB")
    return sealed


def _decrypt_envelope(key: bytes, envelope: bytes, submission_id: str, revision: int) -> bytes:
    if not isinstance(envelope, (bytes, bytearray)):
        raise StandupCryptoError("an envelope is bytes")
    if len(envelope) > formats.MAX_ENVELOPE_BYTES:
        raise StandupCryptoError("the envelope is larger than 4 MiB")
    if len(envelope) < _NONCE + _TAG:
        raise StandupCryptoError("not an envelope")
    try:
        aad = formats.envelope_aad(submission_id, revision)
        return AESGCM(bytes(key)).decrypt(bytes(envelope[:_NONCE]), bytes(envelope[_NONCE:]), aad)
    except (InvalidTag, FormatError):
        raise StandupCryptoError("the envelope does not open for this submission") from None


@dataclass(frozen=True)
class SealedRevision:
    """A revision, encrypted, with what the submit body names it by."""

    plaintext: bytes                # canonical JSON: what revision_hash covers
    revision_hash: str
    envelope: bytes
    envelope_b64: str
    envelope_sha256: str


def seal_revision(revision: dict, s_sub: bytes, *, nonce: Optional[bytes] = None) -> SealedRevision:
    """Check ``revision`` (secret keys included), encrypt it under the
    submission's content key and name it."""
    formats.check_revision(revision)
    formats.refuse_secret_keys(formats.revision_texts(revision))
    plaintext = formats.canonical_bytes(revision)
    envelope = encrypt_envelope(content_key(s_sub), plaintext, revision["submission_id"],
                                revision["revision"], nonce=nonce)
    return SealedRevision(plaintext=plaintext, revision_hash=formats.sha256_hex(plaintext),
                          envelope=envelope, envelope_b64=formats.b64encode(envelope),
                          envelope_sha256=formats.sha256_hex(envelope))


def _open_revision(envelope_b64: str, s_sub: bytes, *, submission_id: str, revision: int,
                  revision_hash: Optional[str] = None,
                  envelope_sha256: Optional[str] = None) -> dict:
    """The revision in an envelope, with every check: the envelope's hash
    when given, the AES-GCM tag with its submission and revision, the
    revision's hash when given, and its shape."""
    envelope = formats.b64decode(envelope_b64, limit=formats.MAX_ENVELOPE_BYTES)
    if envelope_sha256 is not None and formats.sha256_hex(envelope) != envelope_sha256:
        raise StandupCryptoError("the envelope is not the one named")
    plaintext = _decrypt_envelope(content_key(s_sub), envelope, submission_id, revision)
    if revision_hash is not None and formats.sha256_hex(plaintext) != revision_hash:
        raise StandupCryptoError("the revision is not the one named")
    try:
        return formats.read_revision(plaintext, submission_id_=submission_id,
                                     revision_number=revision)
    except FormatError as exc:
        raise StandupCryptoError(str(exc)) from None


# --------------------------------------------------------------------------- #
# Media files: SMF1 (7)                                                        #
# --------------------------------------------------------------------------- #

def _media_header(chunk_size: int) -> bytes:
    return _MAGIC + int(chunk_size).to_bytes(4, "big")


def _record_aad(header: bytes, index: int, final: bool) -> bytes:
    return header + int(index).to_bytes(8, "big") + (b"\x01" if final else b"\x00")


def media_cipher_size(size: int, chunk_size: int = formats.MEDIA_CHUNK_SIZE) -> int:
    """The size of the SMF1 file for ``size`` bytes of plaintext."""
    records = max(1, -(-size // chunk_size))     # an empty file is one empty record
    return _HEADER + records * (_NONCE + _TAG) + size


class MediaEncryptor:
    """An SMF1 file written piece by piece: ``update`` takes plaintext and
    returns the bytes of the file that are ready, ``finish`` the rest
    (the last record, flagged as the last). Hashes and sizes are kept as
    it goes. ``nonces`` is for test vectors only."""

    def __init__(self, key: bytes, *, chunk_size: int = formats.MEDIA_CHUNK_SIZE,
                 nonces: Optional[Sequence[bytes]] = None) -> None:
        if len(key) != 32:
            raise StandupCryptoError("a media key is 32 bytes")
        if not formats.MEDIA_CHUNK_MIN <= chunk_size <= formats.MEDIA_CHUNK_MAX:
            raise StandupCryptoError("chunk size out of range")
        self._aes = AESGCM(bytes(key))
        self._chunk = chunk_size
        self.chunk_size = chunk_size
        self._header = _media_header(chunk_size)
        self._pending = bytearray()
        self._index = 0
        self._nonces = list(nonces) if nonces is not None else None
        self._plain = sha256()
        self._cipher = sha256()
        self._cipher.update(self._header)
        self.size = 0
        self.cipher_size = len(self._header)
        self._started = False
        self._finished = False

    def _record(self, chunk: bytes, final: bool) -> bytes:
        nonce = self._nonces.pop(0) if self._nonces else os.urandom(_NONCE)
        record = nonce + self._aes.encrypt(nonce, bytes(chunk),
                                           _record_aad(self._header, self._index, final))
        self._index += 1
        self._cipher.update(record)
        self.cipher_size += len(record)
        return record

    def update(self, data: bytes) -> bytes:
        if self._finished:
            raise StandupCryptoError("the file is finished")
        self._plain.update(data)
        self.size += len(data)
        self._pending += data
        out = bytearray()
        if not self._started:
            out += self._header
            self._started = True
        # A full chunk is written only once more data follows it: the
        # last chunk, full or not, is the one flagged as last.
        while len(self._pending) > self._chunk:
            chunk = bytes(self._pending[:self._chunk])
            del self._pending[:self._chunk]
            out += self._record(chunk, final=False)
        return bytes(out)

    def finish(self) -> bytes:
        if self._finished:
            raise StandupCryptoError("the file is finished")
        out = bytearray()
        if not self._started:
            out += self._header
            self._started = True
        out += self._record(bytes(self._pending), final=True)
        self._pending.clear()
        self._finished = True
        return bytes(out)

    @property
    def plain_sha256(self) -> str:
        return self._plain.hexdigest()

    @property
    def cipher_sha256(self) -> str:
        return self._cipher.hexdigest()


class _MediaDecryptor:
    """An SMF1 file read piece by piece: ``update`` takes file bytes and
    returns the plaintext of every record known not to be the last,
    ``finish`` the last one. Refuses a wrong header, a damaged, reordered
    or missing record, a file that ends before its last record and one
    that goes on after it."""

    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise StandupCryptoError("a media key is 32 bytes")
        self._aes = AESGCM(bytes(key))
        self._buffer = bytearray()
        self._header: Optional[bytes] = None
        self._record_len = 0
        self._index = 0
        self._finished = False
        self._plain = sha256()
        self._cipher = sha256()
        self.size = 0

    def _open(self, record: bytes, final: bool) -> bytes:
        if len(record) < _NONCE + _TAG:
            raise StandupCryptoError("the media file is cut short")
        try:
            chunk = self._aes.decrypt(record[:_NONCE], record[_NONCE:],
                                      _record_aad(self._header, self._index, final))
        except InvalidTag:
            raise StandupCryptoError("the media file is damaged or not this key's") from None
        if not final and len(chunk) != self._record_len - _NONCE - _TAG:
            raise StandupCryptoError("the media file is damaged")
        self._index += 1
        self._plain.update(chunk)
        self.size += len(chunk)
        return chunk

    def update(self, data: bytes) -> bytes:
        if self._finished:
            raise StandupCryptoError("the file is finished")
        self._cipher.update(data)
        self._buffer += data
        if self._header is None:
            if len(self._buffer) < _HEADER:
                return b""
            header = bytes(self._buffer[:_HEADER])
            if header[:4] != _MAGIC:
                raise StandupCryptoError("not an SMF1 media file")
            chunk_size = int.from_bytes(header[4:8], "big")
            if not formats.MEDIA_CHUNK_MIN <= chunk_size <= formats.MEDIA_CHUNK_MAX:
                raise StandupCryptoError("the media file's chunk size is out of range")
            self._header = header
            self._record_len = _NONCE + chunk_size + _TAG
            del self._buffer[:_HEADER]
        out = bytearray()
        while len(self._buffer) > self._record_len:
            record = bytes(self._buffer[:self._record_len])
            del self._buffer[:self._record_len]
            out += self._open(record, final=False)
        return bytes(out)

    def finish(self) -> bytes:
        if self._finished:
            raise StandupCryptoError("the file is finished")
        if self._header is None:
            raise StandupCryptoError("not an SMF1 media file")
        self._finished = True
        chunk = self._open(bytes(self._buffer), final=True)
        self._buffer.clear()
        return chunk

    @property
    def plain_sha256(self) -> str:
        return self._plain.hexdigest()

    @property
    def cipher_sha256(self) -> str:
        return self._cipher.hexdigest()


def encrypt_media(plaintext: bytes, key: bytes, *, chunk_size: int = formats.MEDIA_CHUNK_SIZE,
                  nonces: Optional[Sequence[bytes]] = None) -> bytes:
    """A whole SMF1 file for ``plaintext``."""
    encryptor = MediaEncryptor(key, chunk_size=chunk_size, nonces=nonces)
    return encryptor.update(plaintext) + encryptor.finish()


def _decrypt_media(data: bytes, key: bytes, *, cipher_sha256: Optional[str] = None,
                  plain_sha256: Optional[str] = None) -> bytes:
    """The plaintext of a whole SMF1 file, with the hashes the revision
    names when given."""
    if cipher_sha256 is not None and formats.sha256_hex(data) != cipher_sha256:
        raise StandupCryptoError("the media file is not the one named")
    decryptor = _MediaDecryptor(key)
    plaintext = decryptor.update(data) + decryptor.finish()
    if plain_sha256 is not None and decryptor.plain_sha256 != plain_sha256:
        raise StandupCryptoError("the media file decrypted to something else")
    return plaintext


def media_entry(*, key: bytes, encryptor: MediaEncryptor, name: str, mime: str,
                alt: str = "", servers: Sequence[str] = ()) -> dict:
    """The revision's media entry (5) for a file ``encryptor`` wrote
    (finished), with the sizes and hashes it kept."""
    cipher = encryptor.cipher_sha256
    return {
        "ref": formats.media_ref(cipher), "name": name, "mime": mime, "alt": alt,
        "size": encryptor.size, "plain_sha256": encryptor.plain_sha256,
        "cipher_sha256": cipher, "cipher_size": encryptor.cipher_size,
        "key": formats.b64encode(key), "chunk_size": encryptor.chunk_size,
        "servers": list(servers),
    }


# --------------------------------------------------------------------------- #
# Key wraps (4)                                                                #
# --------------------------------------------------------------------------- #

def make_wrap(plaintext: dict, recipient_pubkey: str, *,
              eph_sk: Optional[bytes] = None) -> dict:
    """``{"eph", "ct"}``: ``plaintext`` (canonical JSON) NIP-44 encrypted
    from a fresh ephemeral key to ``recipient_pubkey``. ``eph_sk`` is for
    tests only."""
    if not formats.is_pubkey(recipient_pubkey):
        raise WrapInvalid("the recipient is not a valid key")
    eph = eph_sk if eph_sk is not None else new_secret_key()
    ct = nostr_crypto.encrypt(formats.canonical_json(plaintext),
                              nostr_crypto.conversation_key(eph, bytes.fromhex(recipient_pubkey)))
    return {"eph": pubkey_of(eph), "ct": ct}


def submission_key_plaintext(submission_id: str, s_sub: bytes) -> dict:
    return {"v": 1, "type": "submission-key", "submission_id": submission_id,
            "sk": bytes(s_sub).hex()}


def redaktion_key_plaintext(publication: str, epoch: int, s_epoch: bytes) -> dict:
    return {"v": 1, "type": "redaktion-key", "publication": publication, "epoch": epoch,
            "sk": bytes(s_epoch).hex()}


def wrap_submission_key(submission_id: str, s_sub: bytes, recipients: Dict[str, str]) -> Dict[str, dict]:
    """One wrap of the submission key per recipient: ``{name: pubkey}``
    to ``{name: wrap}`` (``redaktion``, ``writer``, each contributor)."""
    plaintext = submission_key_plaintext(submission_id, s_sub)
    return {name: make_wrap(plaintext, pubkey) for name, pubkey in recipients.items()}


def read_wrap_plaintext(text: str) -> dict:
    try:
        value = json.loads(text)
    except (TypeError, ValueError, RecursionError):
        raise WrapInvalid("the wrap does not hold JSON") from None
    if not isinstance(value, dict) or value.get("v") != 1:
        raise WrapInvalid("the wrap holds something else")
    return value


def _open_wrap_local(wrap: Any, recipient_sk: bytes) -> dict:
    """A wrap's plaintext, opened with a secret held here (no signer)."""
    if not formats.is_wrap(wrap):
        raise WrapInvalid("not a key wrap")
    try:
        text = nostr_crypto.decrypt(wrap["ct"], nostr_crypto.conversation_key(
            bytes(recipient_sk), bytes.fromhex(wrap["eph"])))
    except ValueError:
        raise WrapInvalid("the wrap does not open with this key") from None
    return read_wrap_plaintext(text)


def _open_wrap_with_signer(client, wrap: Any, on_success: Callable[[dict], None],
                          on_failure: Callable[[str], None], *,
                          on_invalid: Optional[Callable[[WrapInvalid], None]] = None) -> None:
    """A wrap's plaintext, opened by the account's signer
    (``nip44_decrypt(eph, ct)``). ``on_failure`` gets the signer's short
    reason; ``on_invalid`` a :class:`WrapInvalid` for a wrap that is not
    one or holds something else (without it, ``on_failure`` its words)."""
    invalid = on_invalid or (lambda exc: on_failure(str(exc)))
    if not formats.is_wrap(wrap):
        invalid(WrapInvalid("not a key wrap"))
        return

    def opened(text: str) -> None:
        try:
            value = read_wrap_plaintext(text)
        except WrapInvalid as exc:
            invalid(exc)
            return
        on_success(value)

    client.nip44_decrypt(wrap["eph"], wrap["ct"], on_success=opened, on_failure=on_failure)


def _check_submission_key(plaintext: dict, *, submission_id: str, p_sub: str) -> bytes:
    """The submission key in a wrap's plaintext, when it is for this
    submission and matches its ``p_sub`` (4)."""
    if plaintext.get("type") != "submission-key":
        raise WrapInvalid("not a submission key")
    if plaintext.get("submission_id") != submission_id:
        raise WrapInvalid("a key for another submission")
    secret = _secret_of(plaintext.get("sk"))
    if pubkey_of(secret) != p_sub:
        raise WrapInvalid("the key does not match the submission")
    return secret


def check_redaktion_key(plaintext: dict, *, publication: str, epoch: int,
                        epoch_pubkey: str) -> bytes:
    """The Redaktion key of ``epoch`` in a wrap's plaintext, when it is for
    this publication and epoch and matches the verified key event's
    ``pubkey`` (4)."""
    if plaintext.get("type") != "redaktion-key":
        raise WrapInvalid("not a Redaktion key")
    if plaintext.get("publication") != publication or plaintext.get("epoch") != epoch:
        raise WrapInvalid("a key for another publication or epoch")
    secret = _secret_of(plaintext.get("sk"))
    if pubkey_of(secret) != epoch_pubkey:
        raise WrapInvalid("the key does not match the key event")
    return secret


def check_history_chain(plaintext: dict, *, publication: str, member: str, epoch: int,
                        epoch_pubkeys: Dict[int, str]) -> Dict[int, bytes]:
    """The older epochs' keys in a history chain, each matching the
    verified key event of its epoch (4). A chain is addressed to one
    member, never to an epoch key: ``member`` is the account that opened
    it, and the chain must name it, so a chain copied to someone else's
    account (or to a "since joining" member) is refused. ``epoch`` is the
    epoch of the invitation; the keys are of earlier epochs only.
    ``epoch_pubkeys`` maps an epoch to the ``pubkey`` of its verified key
    event."""
    if plaintext.get("type") != "redaktion-history":
        raise WrapInvalid("not a history chain")
    if plaintext.get("publication") != publication or plaintext.get("epoch") != epoch:
        raise WrapInvalid("a chain for another publication or epoch")
    if plaintext.get("member") != member:
        raise WrapInvalid("a chain for another member")
    keys = plaintext.get("keys")
    if not isinstance(keys, list):
        raise WrapInvalid("a history chain without keys")
    found: Dict[int, bytes] = {}
    for item in keys:
        if not isinstance(item, dict) or not formats.is_int(item.get("epoch"), low=1):
            raise WrapInvalid("a history entry without an epoch")
        if item["epoch"] >= epoch or item["epoch"] in found:
            raise WrapInvalid("a history entry for an epoch it cannot hold")
        expected = epoch_pubkeys.get(item["epoch"])
        secret = _secret_of(item.get("sk"))
        if expected is None or pubkey_of(secret) != expected:
            raise WrapInvalid("a history key that does not match its key event")
        found[item["epoch"]] = secret
    return found


def _secret_of(value: Any) -> bytes:
    try:
        return secret_from_hex(value)
    except StandupCryptoError:
        raise WrapInvalid("the wrap holds no valid key") from None


def wraps_sha256(wraps: Dict[str, dict]) -> Dict[str, str]:
    """``{name: sha256(canonical_json(wrap))}`` as the receipt names them."""
    return {name: formats.wrap_sha256(wrap) for name, wrap in wraps.items()}


__all__: List[str] = [
    "MediaEncryptor", "SealedRevision", "StandupCryptoError", "WrapInvalid",
    "check_history_chain", "check_redaktion_key", "content_key", "encrypt_envelope",
    "encrypt_media", "make_wrap", "media_cipher_size", "media_entry", "new_secret_key",
    "new_status_key", "pubkey_of", "redaktion_key_plaintext", "seal_revision",
    "secret_from_hex", "submission_key_plaintext", "wrap_submission_key", "wraps_sha256",
]
