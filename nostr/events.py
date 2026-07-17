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
    ``id`` populated but no ``sig`` — useful for handing off to a remote
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

    The pubkey on the event is overwritten to match ``sk`` (defensive —
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
