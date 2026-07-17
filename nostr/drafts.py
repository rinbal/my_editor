# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""NIP-37 private encrypted drafts — pure builders.

Spec: https://github.com/nostr-protocol/nips/blob/master/37.md

A draft is a kind ``31234`` *Draft Wrap*. It is addressable on
``(31234, pubkey, d-tag)`` so re-stashing the same logical document
replaces the previous version. The ``content`` field is a NIP-44
encrypted JSON-serialized *inner* unsigned event (kind 1 short note,
kind 30023 long-form article, etc.).

This module is intentionally Qt-free and network-free: it deals only
with the on-wire shape of drafts and the JSON of the inner payload.
Encryption itself happens elsewhere (via the bunker / NIP-46 channel)
because the editor never holds the user's private key.

Lifecycle:
  1. Build an inner unsigned event with ``build_inner_event``.
  2. Hand its serialized JSON to the bunker to NIP-44-encrypt under the
     user's own pubkey (self-encryption).
  3. Wrap the ciphertext in a 31234 event with ``build_draft_wrap``.
  4. Sign and publish via the existing PublishJob pipeline.

To delete a draft, publish a tombstone (same ``d`` + ``k``, empty
content) — see ``build_tombstone_wrap``.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


# --------------------------------------------------------------------------- #
# Protocol constants                                                          #
# --------------------------------------------------------------------------- #

# Outer wrap kind — addressable / parameterized-replaceable.
DRAFT_WRAP_KIND: int = 31234

# Inner-event kinds we currently support. Notes and long-form articles
# match the two publish flows the editor already implements.
INNER_KIND_SHORT_NOTE: int = 1
INNER_KIND_LONG_FORM: int = 30023

SUPPORTED_INNER_KINDS: Tuple[int, ...] = (
    INNER_KIND_SHORT_NOTE,
    INNER_KIND_LONG_FORM,
)

# Default expiration window per the NIP-37 recommendation. Relays SHOULD
# honour NIP-40 and reap drafts after this falls in the past; users
# expect "stale drafts age out" semantics.
DEFAULT_EXPIRATION_SECONDS: int = 90 * 24 * 60 * 60  # 90 days

# NIP-44 v2 caps the *plaintext* (pre-padding) at 65,535 bytes. The wrap
# encrypts ``serialize_inner_event(inner)`` so this is the effective
# ceiling on a draft's encoded inner-event size. Long-form articles can
# approach this; we expose the constant so the publisher can pre-flight
# rather than failing inside the bunker round-trip.
MAX_INNER_PAYLOAD_BYTES: int = 65535


# --------------------------------------------------------------------------- #
# Identifier helpers                                                          #
# --------------------------------------------------------------------------- #

def new_note_identifier() -> str:
    """Return a stable ``d`` tag for a fresh short-note draft.

    Short notes have no natural slug (no title field), so we mint a
    UUID4. Re-stashing the same tab reuses this identifier so the
    addressable wrap replaces cleanly.
    """
    return f"note-{uuid.uuid4().hex}"


# --------------------------------------------------------------------------- #
# Inner unsigned event                                                        #
# --------------------------------------------------------------------------- #

def build_inner_event(
    *,
    kind: int,
    content: str,
    pubkey_hex: str,
    tags: Optional[List[List[str]]] = None,
    created_at: Optional[int] = None,
) -> Dict[str, Any]:
    """Construct the unsigned inner event that will be encrypted.

    Per NIP-37 the inner event is unsigned — no ``id``, no ``sig``. We
    include ``pubkey`` and ``created_at`` so callers can later promote
    the draft to a real publish without rebuilding from scratch.

    The pubkey MUST be the author's own pubkey: the wrap is encrypted
    to that same key, and a mismatch would silently produce drafts the
    user can decrypt but not publish under their identity.
    """
    if kind not in SUPPORTED_INNER_KINDS:
        raise ValueError(
            f"unsupported inner kind {kind!r}; expected one of {SUPPORTED_INNER_KINDS}"
        )
    if len(pubkey_hex) != 64:
        raise ValueError("pubkey_hex must be 64 hex chars")
    if created_at is None:
        created_at = int(time.time())
    return {
        "kind": int(kind),
        "content": content,
        "tags": list(tags or []),
        "created_at": int(created_at),
        "pubkey": pubkey_hex.lower(),
    }


def serialize_inner_event(inner: Dict[str, Any]) -> str:
    """Return the canonical JSON form of an inner event for encryption.

    Uses compact separators and ``ensure_ascii=False`` so the encrypted
    payload is as small as possible — NIP-44 has a 65535-byte plaintext
    cap and long-form articles can approach it.
    """
    # Explicit key whitelist keeps stray fields (e.g. an accidentally
    # populated id/sig from a copy) out of the encrypted payload.
    payload = {
        "kind": int(inner["kind"]),
        "content": str(inner.get("content", "")),
        "tags": list(inner.get("tags", [])),
        "created_at": int(inner["created_at"]),
        "pubkey": str(inner["pubkey"]).lower(),
    }
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def parse_inner_event(plaintext: str) -> Dict[str, Any]:
    """Parse a decrypted draft payload back into an inner event dict.

    Returns a dict with the same shape as ``build_inner_event``'s output.
    Raises ``ValueError`` on any malformedness so callers can mark a
    draft row as failed without taking down the whole list refresh.
    """
    try:
        data = json.loads(plaintext)
    except json.JSONDecodeError as exc:
        raise ValueError(f"draft payload is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("draft payload must be a JSON object")
    try:
        kind = int(data["kind"])
        content = str(data.get("content", ""))
        tags = list(data.get("tags", []))
        created_at = int(data.get("created_at", 0))
        pubkey = str(data.get("pubkey", "")).lower()
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"draft payload missing required fields: {exc}") from exc
    # Unknown inner kinds are intentionally tolerated: a future client
    # could stash other kinds and we'd rather display "unknown draft
    # type" than silently drop them. ``DraftStore`` decides how to
    # render — see the kind branch in ``set_decrypted``.
    if not all(isinstance(t, list) and all(isinstance(x, str) for x in t) for t in tags):
        raise ValueError("draft payload tags must be list[list[str]]")
    return {
        "kind": kind,
        "content": content,
        "tags": tags,
        "created_at": created_at,
        "pubkey": pubkey,
    }


# --------------------------------------------------------------------------- #
# Outer wrap (kind 31234)                                                     #
# --------------------------------------------------------------------------- #

def build_draft_wrap(
    *,
    identifier: str,
    inner_kind: int,
    encrypted_content: str,
    pubkey_hex: str,
    client_name: str,
    expiration_seconds: int = DEFAULT_EXPIRATION_SECONDS,
    extra_tags: Optional[List[List[str]]] = None,
    created_at: Optional[int] = None,
) -> Dict[str, Any]:
    """Build an unsigned kind-31234 wrap ready for the signer.

    Tags emitted, in order:
      ``["d", identifier]``        — addressable id (NIP-01).
      ``["k", str(inner_kind)]``   — required by NIP-37; lets clients
                                      filter "drafts of articles" vs.
                                      "drafts of notes" without
                                      decryption.
      ``["expiration", ...]``      — NIP-40, recommended by NIP-37.
      ``["client", client_name]``  — NIP-89 attribution.
      ``*extra_tags``              — opaque pass-through (reserved for
                                      future use, e.g. RSS-source tags).

    ``encrypted_content`` is the base64 NIP-44 payload produced by the
    bunker. This function does not perform any encryption itself.
    """
    if not identifier:
        raise ValueError("draft identifier (d-tag) must not be empty")
    if inner_kind not in SUPPORTED_INNER_KINDS:
        raise ValueError(
            f"unsupported inner kind {inner_kind!r}; expected one of {SUPPORTED_INNER_KINDS}"
        )
    if len(pubkey_hex) != 64:
        raise ValueError("pubkey_hex must be 64 hex chars")
    if created_at is None:
        created_at = int(time.time())
    expiration_unix = int(created_at) + max(0, int(expiration_seconds))

    tags: List[List[str]] = [
        ["d", identifier],
        ["k", str(int(inner_kind))],
        ["expiration", str(expiration_unix)],
        ["client", client_name],
    ]
    if extra_tags:
        tags.extend(list(t) for t in extra_tags)

    # The signer fills in ``id`` and ``sig``; we hand it the dict the
    # NIP-46 ``sign_event`` method expects.
    return {
        "kind": DRAFT_WRAP_KIND,
        "content": encrypted_content,
        "tags": tags,
        "created_at": int(created_at),
        "pubkey": pubkey_hex.lower(),
    }


def build_tombstone_wrap(
    *,
    identifier: str,
    inner_kind: int,
    pubkey_hex: str,
    client_name: str,
    created_at: Optional[int] = None,
) -> Dict[str, Any]:
    """Build a deletion-marker wrap for an existing draft.

    Per NIP-37: "A blanked .content field signals that the draft has
    been deleted." Same ``d`` + ``k`` (so the addressable replacement
    targets the right event), empty content, no encryption needed.

    The expiration tag is intentionally short here — a tombstone only
    needs to live long enough for other clients to observe the empty
    content; we still set 90 days because some relays drop events with
    expiration in the near past.
    """
    return build_draft_wrap(
        identifier=identifier,
        inner_kind=inner_kind,
        encrypted_content="",
        pubkey_hex=pubkey_hex,
        client_name=client_name,
        created_at=created_at,
    )


# --------------------------------------------------------------------------- #
# Wrap inspection (no decryption required)                                    #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class DraftWrapMeta:
    """Lightweight view of an outer 31234 event prior to decryption."""

    identifier: str          # d-tag value
    inner_kind: int          # value of the k-tag, parsed as int
    event_id: str            # outer event id (hex)
    pubkey: str              # author pubkey (hex, lowercase)
    created_at: int          # outer event created_at
    expiration: Optional[int]  # NIP-40 expiration, if present
    ciphertext: str          # base64 NIP-44 payload — empty == tombstone

    @property
    def is_tombstone(self) -> bool:
        return self.ciphertext == ""


def parse_wrap_event(event: Dict[str, Any]) -> Optional[DraftWrapMeta]:
    """Extract metadata from a relay-delivered 31234 event.

    Returns ``None`` if the event is not a recognisable draft wrap —
    specifically: not a dict, wrong kind, missing ``d`` tag, or whose
    tag list isn't a list of lists. Empty / non-integer ``k`` tag maps
    to ``inner_kind = 0`` (treat as "unknown" downstream); a missing
    ``d`` is a hard reject because addressable replacement requires it.

    The caller (the draft-sync orchestrator) uses this to dedupe by
    ``(pubkey, d)`` before spending bunker round-trips on decryption.
    """
    if not isinstance(event, dict):
        return None
    if int(event.get("kind", -1)) != DRAFT_WRAP_KIND:
        return None
    raw_tags = event.get("tags", [])
    if not isinstance(raw_tags, list):
        return None

    identifier = ""
    inner_kind_str = ""
    expiration: Optional[int] = None
    for tag in raw_tags:
        if not isinstance(tag, list) or len(tag) < 2:
            continue
        name, value = tag[0], tag[1]
        if name == "d" and not identifier:
            identifier = str(value)
        elif name == "k" and not inner_kind_str:
            inner_kind_str = str(value)
        elif name == "expiration" and expiration is None:
            try:
                expiration = int(value)
            except (TypeError, ValueError):
                pass

    if not identifier:
        # No d-tag → not addressable → not a NIP-37 draft we can manage.
        return None
    try:
        inner_kind = int(inner_kind_str) if inner_kind_str else 0
    except ValueError:
        inner_kind = 0

    event_id = str(event.get("id", ""))
    pubkey = str(event.get("pubkey", "")).lower()
    try:
        created_at = int(event.get("created_at", 0))
    except (TypeError, ValueError):
        created_at = 0
    ciphertext = str(event.get("content", ""))

    return DraftWrapMeta(
        identifier=identifier,
        inner_kind=inner_kind,
        event_id=event_id,
        pubkey=pubkey,
        created_at=created_at,
        expiration=expiration,
        ciphertext=ciphertext,
    )


# --------------------------------------------------------------------------- #
# Inner-event content shaping                                                 #
# --------------------------------------------------------------------------- #

def extract_article_metadata(inner: Dict[str, Any]) -> Dict[str, str]:
    """Pull common NIP-23 metadata tags off an inner article event.

    Returns a dict with keys ``title``, ``summary``, ``image``,
    ``published_at`` — missing tags map to empty strings. The drafts
    panel uses these to render article rows without re-parsing tag lists
    at every paint.
    """
    out = {"title": "", "summary": "", "image": "", "published_at": ""}
    for tag in inner.get("tags", []):
        if not isinstance(tag, list) or len(tag) < 2:
            continue
        name = tag[0]
        if name in out and not out[name]:
            out[name] = str(tag[1])
    return out


def derive_preview_snippet(content: str, *, max_chars: int = 140) -> str:
    """Return a single-line preview for a list row.

    Strips leading Markdown heading hashes, collapses whitespace,
    truncates with an ellipsis. Empty or whitespace-only content yields
    the empty string (the panel can then fall back to a placeholder).
    """
    if not content:
        return ""
    # Drop a leading "# heading" line — for articles it duplicates the
    # title-tag row; for notes it's still useful to keep, so we only
    # strip when it looks like a level-1 heading.
    stripped = content.lstrip()
    if stripped.startswith("# "):
        first_newline = stripped.find("\n")
        if first_newline != -1:
            stripped = stripped[first_newline + 1 :]
    # Collapse all runs of whitespace into single spaces.
    flat = " ".join(stripped.split())
    if len(flat) <= max_chars:
        return flat
    return flat[: max_chars - 1].rstrip() + "…"
