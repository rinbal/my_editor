"""Deterministic NIP-23 d-tag derivation for feed items.

Matches the reference implementation in ``nostr-core/src/rss.ts``:

    sha256(guid || link || title)[:16]

The first non-empty of (guid, link, title) is hashed. Falling back through
``link`` and ``title`` makes the identifier stable across CMSs that
regenerate ``guid`` on every render.
"""

from __future__ import annotations

from hashlib import sha256
from typing import Optional


class NoIdentifierError(ValueError):
    """Raised when a feed item has no guid, link, or title to hash."""


def derive_identifier(
    *,
    guid: Optional[str] = None,
    link: Optional[str] = None,
    title: Optional[str] = None,
    prefix: Optional[str] = None,
) -> str:
    """Return a 16-char hex identifier for the item.

    The seed is the first non-empty value among ``guid``, ``link``,
    ``title``. ``prefix`` is prepended verbatim to the hash when given.
    """
    seed = (guid or "") or (link or "") or (title or "")
    if not seed:
        raise NoIdentifierError(
            "Cannot derive identifier: item has no guid, link, or title"
        )
    digest = sha256(seed.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}{digest}" if prefix else digest
