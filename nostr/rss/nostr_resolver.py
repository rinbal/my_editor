# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Resolve thin RSS items to NIP-23 long-form events on Nostr.

Some publishers (Habla, Yakihonne, Pareto, self-hosted Nostr-aware blogs)
emit RSS feeds where each item's body is a teaser and the real prose
lives in a kind:30023 event on relays. The feed's ``<link>`` carries
the addressable coordinate, either as a ``nostr:naddr1...`` URI or as
a bech32 ``naddr`` embedded in an HTTP URL path (njump.me, habla.news,
yakihonne.com, etc.).

This module turns that situation into a usable draft:

- :func:`extract_nostr_coord` is a pure parser: it pulls the bech32
  naddr out of any URL or ``nostr:`` URI, decodes it, and returns the
  long-form coordinate. Returns ``None`` when the link is not a
  long-form pointer (no naddr, wrong kind, malformed bech32).
- :class:`LongFormFetcher` is a thin Qt wrapper around the existing
  ``fetch_latest_event`` one-shot query. It hands the resolved event
  (or ``None`` on timeout) back via callback so the importer can
  substitute the prose without changing its pipeline shape.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional, Tuple

from PySide6.QtCore import QObject

from ..bech32 import decode_naddr
from ..queries import fetch_latest_event
from ..relay import RelayPool


# NIP-23 long-form article kind. We deliberately don't accept other
# parameterised-replaceable kinds here — the importer is for long-form,
# and resolving e.g. a notebook (kind 31000) into a long-form draft
# would be lossy.
_LONGFORM_KIND: int = 30023

# Default timeout for one resolution. NIP-65 read-relay sets can include
# slow or unreachable relays; 8 seconds is long enough to wait out a few
# of those while still keeping a multi-item import responsive.
_DEFAULT_TIMEOUT_MS: int = 8_000

# Bech32 strings are case-insensitive but conventionally lowercase. The
# decoder validates the alphabet; this regex is a coarse locator so we
# can find the naddr substring within an arbitrary URL or URI.
_NADDR_RE = re.compile(r"naddr1[a-z0-9]+", re.IGNORECASE)


@dataclass(frozen=True)
class LongFormCoord:
    """Addressable coordinate for a NIP-23 long-form event."""

    pubkey_hex: str
    kind: int
    d_tag: str
    relay_hints: Tuple[str, ...]


# --------------------------------------------------------------------------- #
# Pure: parse a feed-item link into a long-form coordinate                    #
# --------------------------------------------------------------------------- #

def extract_nostr_coord(link: Optional[str]) -> Optional[LongFormCoord]:
    """Return the long-form coordinate referenced by ``link``, if any.

    Accepts:
      - ``nostr:naddr1...`` URI scheme.
      - ``https://njump.me/naddr1...`` and any URL with a bech32 naddr
        substring anywhere in its path or fragment.
      - The bare bech32 string ``naddr1...``.

    A URL may contain *multiple* naddr-shaped substrings — for example
    a humanised slug that truncates the bech32 (``/post-naddr1qqr...``)
    followed by the canonical full address in the next path segment.
    We iterate every regex match and return the first one that decodes
    cleanly to a NIP-23 long-form coordinate.

    Returns ``None`` if:
      - ``link`` is falsy.
      - No matching substring decodes to a valid long-form naddr.
    """
    if not link:
        return None
    haystack = link.strip()
    # Strip the URI scheme prefix so the regex doesn't have to.
    if haystack.lower().startswith("nostr:"):
        haystack = haystack[len("nostr:"):]
    for match in _NADDR_RE.finditer(haystack):
        coord = _decode_to_longform(match.group(0))
        if coord is not None:
            return coord
    return None


def is_nostr_uri_scheme(link: Optional[str]) -> bool:
    """``True`` when the link's scheme is ``nostr:``.

    The importer uses this to decide whether to always resolve (the link
    explicitly points at Nostr) or only resolve as a thin-content
    fallback (the link is an HTTP URL that *happens* to embed a naddr).
    """
    if not link:
        return False
    return link.lstrip().lower().startswith("nostr:")


def _decode_to_longform(candidate: str) -> Optional[LongFormCoord]:
    """Decode ``candidate`` as bech32 and return the coord if it's
    a NIP-23 long-form pointer. ``None`` on any decode rejection."""
    try:
        d_tag, author_hex, kind, relays = decode_naddr(candidate)
    except (ValueError, IndexError, TypeError):
        return None
    if kind != _LONGFORM_KIND:
        return None
    if not author_hex or not d_tag:
        return None
    return LongFormCoord(
        pubkey_hex=author_hex.lower(),
        kind=int(kind),
        d_tag=d_tag,
        relay_hints=tuple(_dedup_relays(relays)),
    )


# --------------------------------------------------------------------------- #
# Async: fetch the event from relays                                          #
# --------------------------------------------------------------------------- #

class LongFormFetcher(QObject):
    """One-shot fetch of a kind:30023 event by addressable coordinate.

    Wraps ``queries.fetch_latest_event`` so the EOSE-or-timeout
    behaviour comes for free. The fetcher itself is stateless across
    calls; allocate one per importer (or reuse it).
    """

    def __init__(
        self,
        relay_pool: RelayPool,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._relay_pool = relay_pool

    def fetch(
        self,
        coord: LongFormCoord,
        *,
        extra_relays: Iterable[str],
        on_success: Callable[[dict], None],
        on_not_found: Callable[[], None],
        timeout_ms: int = _DEFAULT_TIMEOUT_MS,
    ) -> None:
        """Query relays for the event identified by ``coord``.

        ``extra_relays`` (typically the user's NIP-65 read set) is
        merged with ``coord.relay_hints`` and deduplicated. On EOSE or
        timeout, the newest matching event is delivered to
        ``on_success``; if nothing matched, ``on_not_found`` fires.

        Exactly one of the two callbacks is invoked, once.
        """
        relays = _dedup_relays((*extra_relays, *coord.relay_hints))
        if not relays:
            # Nothing to query against — no read relays cached, no naddr
            # hints. Fail fast and let the caller fall back.
            on_not_found()
            return

        filters = [{
            "kinds": [coord.kind],
            "authors": [coord.pubkey_hex],
            "#d": [coord.d_tag],
            "limit": 1,
        }]

        # Honour the "exactly one callback, once" contract even if the
        # underlying subscription raises synchronously (bad filter shape,
        # relay pool already torn down, etc.). The caller treats us as a
        # black box that always settles; if we leak the exception the
        # item gets stuck mid-pipeline.
        try:
            fetch_latest_event(
                self._relay_pool,
                relays,
                filters,
                on_done=lambda event: (
                    on_success(event) if event else on_not_found()
                ),
                timeout_ms=timeout_ms,
                parent=self,
            )
        except Exception:  # noqa: BLE001 — settle the contract, move on
            on_not_found()


# --------------------------------------------------------------------------- #
# Relay-list hygiene                                                          #
# --------------------------------------------------------------------------- #

def _dedup_relays(relays: Iterable[object]) -> List[str]:
    """Order-preserving dedupe.

    Uses the URL trimmed of a trailing slash and case-folded as the dedupe
    key so ``wss://relay.example/`` and ``wss://Relay.Example`` collapse.
    Returns the strings in their original first-seen form so caller
    diagnostics stay readable.
    """
    seen: set[str] = set()
    out: List[str] = []
    for raw in relays:
        if not isinstance(raw, str):
            continue
        cleaned = raw.strip()
        if not cleaned:
            continue
        key = cleaned.rstrip("/").lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
    return out
