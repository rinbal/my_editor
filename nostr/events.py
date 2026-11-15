# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""NIP-01 events: canonical serialization, id computation, sign & verify.

References:
  - NIP-01: https://github.com/nostr-protocol/nips/blob/master/01.md

The canonical serialization is strict:
  [0, pubkey_hex, created_at, kind, tags, content]
serialized as UTF-8 JSON with NO whitespace and the standard JSON escape
set (\\\", \\\\, \\n, \\r, \\t, \\b, \\f, plus \\uXXXX for other control
characters). Python's ``json.dumps(..., separators=(',', ':'),
ensure_ascii=False)`` produces exactly this byte stream.
"""

from __future__ import annotations

import json
import time
from hashlib import sha256
from typing import Any, Dict, List, Optional

from . import crypto

# A Nostr event is just a dict; we type-alias for readability.
Event = Dict[str, Any]
Tag = List[str]


# --------------------------------------------------------------------------- #
# Canonical serialization & id                                                #
# --------------------------------------------------------------------------- #

def canonical_serialize(
    pubkey_hex: str,
    created_at: int,
    kind: int,
    tags: List[Tag],
    content: str,
) -> bytes:
    """Return the exact byte stream that hashes to the event id (NIP-01)."""
    return json.dumps(
        [0, pubkey_hex, created_at, kind, tags, content],
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def compute_event_id(
    pubkey_hex: str,
    created_at: int,
    kind: int,
    tags: List[Tag],
    content: str,
) -> str:
    """SHA-256 of canonical serialization, lowercase hex."""
    return sha256(canonical_serialize(pubkey_hex, created_at, kind, tags, content)).hexdigest()


# --------------------------------------------------------------------------- #
# Build & sign                                                                #
# --------------------------------------------------------------------------- #

def build_event(
    kind: int,
    content: str,
    tags: Optional[List[Tag]] = None,
    *,
    sk: Optional[bytes] = None,
    pubkey_hex: Optional[str] = None,
    created_at: Optional[int] = None,
) -> Event:
    """Construct an event dict.

    If ``sk`` is given, the event is fully signed (the public key is
    derived from sk and the signature is computed). If ``sk`` is None but
    ``pubkey_hex`` is provided, an *unsigned* event is returned with
    ``id`` populated but no ``sig``, useful for handing off to a remote
    signer (NIP-46) which will fill in the signature.

    ``tags`` defaults to an empty list; ``created_at`` defaults to now.
    """
    if tags is None:
        tags = []
    if created_at is None:
        created_at = int(time.time())

    if sk is not None:
        if len(sk) != 32:
            raise ValueError("secret key must be 32 bytes")
        pk = crypto.get_public_key(sk).hex()
    elif pubkey_hex is not None:
        pk = pubkey_hex.lower()
        if len(pk) != 64:
            raise ValueError("pubkey_hex must be 64 hex chars (32 bytes)")
    else:
        raise ValueError("either sk or pubkey_hex must be provided")

    event_id = compute_event_id(pk, created_at, kind, tags, content)
    event: Event = {
        "id": event_id,
        "pubkey": pk,
        "created_at": created_at,
        "kind": kind,
        "tags": tags,
        "content": content,
    }
    if sk is not None:
        event["sig"] = crypto.sign_schnorr(sk, bytes.fromhex(event_id)).hex()
    return event


def sign_event(unsigned: Event, sk: bytes) -> Event:
    """Sign an existing unsigned event dict in place AND return it.

    The pubkey on the event is overwritten to match ``sk`` (defensive:
    a mismatched pubkey would produce a verifiable event with the wrong
    author). The id is recomputed for the same reason.
    """
    pk = crypto.get_public_key(sk).hex()
    created_at = int(unsigned.get("created_at", time.time()))
    kind = int(unsigned["kind"])
    tags = unsigned.get("tags", [])
    content = unsigned.get("content", "")
    event_id = compute_event_id(pk, created_at, kind, tags, content)
    unsigned["pubkey"] = pk
    unsigned["created_at"] = created_at
    unsigned["id"] = event_id
    unsigned["sig"] = crypto.sign_schnorr(sk, bytes.fromhex(event_id)).hex()
    return unsigned


# --------------------------------------------------------------------------- #
# Verify                                                                      #
# --------------------------------------------------------------------------- #

def fingerprint(event: Event) -> str:
    """The whole event as text, to tell copies apart.

    Two copies differing in any field are different copies, even when
    they claim the same id and signature: a relay can send a forged copy
    of a real event, and checking a copy is checking exactly that text.
    """
    try:
        return json.dumps(event, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return repr(event)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_lower_hex(value: object, length: int) -> bool:
    return (isinstance(value, str) and len(value) == length
            and all(c in "0123456789abcdef" for c in value))


def has_event_shape(event: object) -> bool:
    """True when ``event`` has the shape of a Nostr event at all: the
    fields every later check reads, each of the type it must be (an
    integer that is an integer, not text or true/false). Checked before
    any of them is read, so an event with ``tags: null`` or a tag that is
    a number is refused like any other, not a crash."""
    if not isinstance(event, dict):
        return False
    if not _is_int(event.get("kind")) or not _is_int(event.get("created_at")):
        return False
    if not isinstance(event.get("content"), str):
        return False
    tags = event.get("tags")
    if not isinstance(tags, list):
        return False
    for entry in tags:
        if not isinstance(entry, list) or not entry:
            return False
        if not all(isinstance(item, str) for item in entry):
            return False
    return all(isinstance(event.get(name), str) for name in ("id", "pubkey", "sig"))


def is_verified_event(event: object, *, kind: Optional[int] = None) -> bool:
    """An event exactly as NIP-01 shapes it (:func:`has_event_shape`,
    lowercase hex id, pubkey and signature), of ``kind`` when given,
    whose id and signature verify. :func:`verify_event` alone converts
    with ``int()``, so a kind sent as text would pass it."""
    if not has_event_shape(event):
        return False
    if not (_is_lower_hex(event["id"], 64) and _is_lower_hex(event["pubkey"], 64)
            and _is_lower_hex(event["sig"], 128)):
        return False
    if kind is not None and event["kind"] != kind:
        return False
    return verify_event(event)


def verify_event(event: Event) -> bool:
    """Validate id, pubkey shape, and signature. Returns False on any failure."""
    try:
        pk = event["pubkey"]
        created_at = int(event["created_at"])
        kind = int(event["kind"])
        tags = event["tags"]
        content = event["content"]
        claimed_id = event["id"]
        sig = event["sig"]
    except (KeyError, TypeError, ValueError):
        return False

    if not isinstance(pk, str) or len(pk) != 64:
        return False
    if not isinstance(claimed_id, str) or len(claimed_id) != 64:
        return False
    if not isinstance(sig, str) or len(sig) != 128:
        return False
    if not isinstance(tags, list) or not all(
        isinstance(t, list) and all(isinstance(x, str) for x in t) for t in tags
    ):
        return False
    if not isinstance(content, str):
        return False

    actual_id = compute_event_id(pk, created_at, kind, tags, content)
    if actual_id != claimed_id:
        return False
    try:
        return crypto.verify_schnorr(
            bytes.fromhex(pk), bytes.fromhex(sig), bytes.fromhex(claimed_id)
        )
    except ValueError:
        return False
