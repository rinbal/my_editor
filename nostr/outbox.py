# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""NIP-65 outbox: per-pubkey relay-list cache + publish-set selection.

Spec: https://github.com/nostr-protocol/nips/blob/master/65.md

A user's NIP-65 event (``kind:10002``) is a list of ``["r", url, marker?]``
tags. Marker is ``"read"``, ``"write"``, or omitted (meaning both). We
extract the write set for publishing.

For publishing our own kind 1 notes, we use:

    dedup(DEFAULT_RELAYS ∪ user_write_relays)[:RELAY_CAP]

DEFAULT_RELAYS guarantees a known-good base set even when the user has
no published list yet; the union ensures the note also reaches the
relays the user has chosen to advertise, so other clients querying their
pubkey via the outbox model find it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from PySide6.QtCore import QObject

from . import DEFAULT_RELAYS
from .queries import fetch_latest_event
from .relay import RelayPool


# Per the NIP-65 spec, lists should stay small (2-4 per category), so clamping
# the union at 10 keeps publishing fast even if a user has a sprawling list.
RELAY_CAP: int = 10

# Cache TTLs. Empty results retry sooner so a user who just published their
# kind:10002 sees their preferences honoured on the next publish.
_TTL_HIT_S: int = 30 * 60
_TTL_EMPTY_S: int = 3 * 60


# --------------------------------------------------------------------------- #
# Pure parsing & selection                                                    #
# --------------------------------------------------------------------------- #

@dataclass
class RelayList:
    """Parsed NIP-65 relay list for one user."""
    write: List[str] = field(default_factory=list)
    read: List[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.write and not self.read


def parse_relay_list(event: dict) -> RelayList:
    """Extract write/read relay URLs from a kind:10002 event.

    Tag shape: ``["r", url, marker?]`` where marker is ``read`` / ``write``
    / absent (meaning both). Unknown markers are treated as "both" so we
    don't lose data to typos.
    """
    write: List[str] = []
    read: List[str] = []
    for tag in event.get("tags", []):
        if not isinstance(tag, list) or len(tag) < 2 or tag[0] != "r":
            continue
        url = str(tag[1]).strip()
        if not url:
            continue
        marker = tag[2].lower() if len(tag) >= 3 and isinstance(tag[2], str) else ""
        if marker == "read":
            read.append(url)
        elif marker == "write":
            write.append(url)
        else:
            write.append(url)
            read.append(url)
    return RelayList(write=write, read=read)


def relay_list_tags_adding(
    existing_event: Optional[dict],
    url: str,
    *,
    marker: str = "write",
) -> Optional[List[List[str]]]:
    """Tags for a kind 10002 that adds ``url`` and changes nothing else.

    Returns None when the addition must not be published, which is the
    important half of this function. A kind 10002 is replaceable, so
    publishing one built from an unread list does not add a relay, it
    replaces the user's entire list with whatever we happened to know.
    Losing an author's relay list scatters their readers, and no undo
    exists once relays have taken the replacement. So a caller with no
    confirmed current event gets None and must refuse.

    None is also returned when there is nothing to do, so a caller never
    asks the signer to approve a no-op. That covers a relay already
    listed under any marker, including read-only: silently promoting a
    read entry to a write one would be rewriting a choice the user made,
    not adding to it.
    """
    if not isinstance(existing_event, dict):
        return None
    normalized = _normalize_for_dedup(url)
    if not normalized:
        return None

    tags: List[List[str]] = []
    for tag in existing_event.get("tags", []):
        if not isinstance(tag, list):
            continue
        tags.append([str(part) for part in tag])
        if len(tag) >= 2 and tag[0] == "r" and _normalize_for_dedup(str(tag[1])) == normalized:
            return None  # already listed, under whatever marker they chose

    tags.append(["r", url.strip().rstrip("/"), marker])
    return tags


def select_draft_publish_relays(
    relay_list: "RelayList",
    *,
    bunker_relays: Iterable[str] = (),
    base: Iterable[str] = DEFAULT_RELAYS,
    entitled: Iterable[str] = (),
    cap: int = RELAY_CAP,
) -> List[str]:
    """Choose where to *publish* a NIP-37 private draft.

    Drafts must land somewhere the user's other devices will read back.
    The reader path (``draft_sync._select_read_relays``) consults
    ``read`` → ``write`` → bunker, so we mirror that by publishing to
    the union ``write`` ∪ ``read`` ∪ bunker, with the curated base set
    as a backstop for brand-new profiles. Deduped and capped at ``cap``.

    Distinct from ``select_publish_relays`` (used for regular notes &
    articles), which only blends write + base. Drafts need the
    extra read-set inclusion specifically because asymmetric read /
    write sets are common (paid read relays + free write relays, etc.)
    and we cannot afford drafts written on device A to be invisible
    on device B.
    """
    seen: set[str] = set()
    out: List[str] = []
    # The user's own relays lead, since those are where their other devices
    # look. An entitled relay comes next, ahead of the generic backstop, so
    # a member's draft reaches it before the cap runs out.
    for url in (
        list(relay_list.write)
        + list(relay_list.read)
        + list(bunker_relays)
        + list(entitled)
        + list(base)
    ):
        normalized = _normalize_for_dedup(url)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        out.append(url)
        if len(out) >= cap:
            break
    return out


def select_publish_relays(
    user_write_relays: Iterable[str],
    *,
    base: Iterable[str] = DEFAULT_RELAYS,
    entitled: Iterable[str] = (),
    cap: int = RELAY_CAP,
) -> List[str]:
    """Combine the curated base set with the user's write relays.

    Order: entitled relays first, then base relays (most trusted by us),
    then the user's choices. Case-folded host comparison so trivial URL
    variations don't double-publish.

    ``entitled`` is for a relay this account has specific standing on, such
    as one that comes with an association membership. It leads because a
    relay that accepts writes only from its own members is worth more to
    that member than any general-purpose relay, and because being dropped
    by ``cap`` would quietly cost them the benefit. This module stays
    unaware of what confers the entitlement; the caller resolves that.
    """
    seen: set[str] = set()
    out: List[str] = []
    for relay in list(entitled) + list(base) + list(user_write_relays):
        normalized = _normalize_for_dedup(relay)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        out.append(relay.strip().rstrip("/"))
        if len(out) >= cap:
            break
    return out


def _normalize_for_dedup(url: str) -> str:
    """Lowercase + strip trailing slash, just for set membership. The original
    URL is preserved in the output so we don't accidentally rewrite a path."""
    s = url.strip().rstrip("/").lower()
    return s


# --------------------------------------------------------------------------- #
# Cached fetcher                                                              #
# --------------------------------------------------------------------------- #

@dataclass
class _CacheEntry:
    relay_list: RelayList
    fetched_at: float
    empty: bool


class RelayListCache(QObject):
    """Fetches NIP-65 events on demand and caches them per pubkey.

    Callers pass in the relays to consult (typically ``DEFAULT_RELAYS`` plus
    the bunker relays for the profile). The fetch is asynchronous; callers
    receive the parsed ``RelayList`` via callback. Cached hits resolve
    synchronously on the next event-loop tick.
    """

    def __init__(self, pool: RelayPool, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._pool = pool
        self._cache: Dict[str, _CacheEntry] = {}
        self._inflight: Dict[str, List[Callable[[RelayList], None]]] = {}

    def clear(self) -> None:
        self._cache.clear()

    def invalidate(self, pubkey_hex: str) -> None:
        self._cache.pop(pubkey_hex, None)

    def get_cached(self, pubkey_hex: str) -> Optional[RelayList]:
        """Return the cached list if still fresh, else ``None``."""
        entry = self._cache.get(pubkey_hex)
        if entry is None:
            return None
        ttl = _TTL_EMPTY_S if entry.empty else _TTL_HIT_S
        if time.time() - entry.fetched_at > ttl:
            return None
        return entry.relay_list

    def fetch(
        self,
        pubkey_hex: str,
        relays: List[str],
        on_done: Callable[[RelayList], None],
        *,
        timeout_ms: int = 6_000,
    ) -> None:
        """Resolve the user's NIP-65 list (cached or fresh) and call ``on_done``."""
        cached = self.get_cached(pubkey_hex)
        if cached is not None:
            on_done(cached)
            return

        # Coalesce concurrent fetches so a flood of publishes doesn't
        # spam the same relays with identical REQs.
        waiters = self._inflight.get(pubkey_hex)
        if waiters is not None:
            waiters.append(on_done)
            return
        self._inflight[pubkey_hex] = [on_done]

        def _on_event(event: Optional[dict]) -> None:
            relay_list = parse_relay_list(event) if event else RelayList()
            self._cache[pubkey_hex] = _CacheEntry(
                relay_list=relay_list,
                fetched_at=time.time(),
                empty=relay_list.is_empty,
            )
            callbacks = self._inflight.pop(pubkey_hex, [])
            for cb in callbacks:
                try:
                    cb(relay_list)
                except Exception:  # noqa: BLE001, best-effort, don't swallow others
                    pass

        fetch_latest_event(
            self._pool,
            relays,
            filters=[{"kinds": [10002], "authors": [pubkey_hex], "limit": 1}],
            on_done=_on_event,
            timeout_ms=timeout_ms,
            parent=self,
        )
