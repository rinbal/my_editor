# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared Nostr import primitives.

The building blocks every Nostr-facing resolver needs, in one place:
NIP-19 entity extraction, NIP-05 address resolution, the NIP-65 outbox
relay lookup, event-to-FeedItem normalisation, and the Qt adapter that
gives resolvers a tiny relay-query surface.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional, Tuple

from PySide6.QtCore import QObject

from ...bech32 import (
    decode_naddr,
    decode_nevent,
    decode_nprofile,
    decode_npub,
    decode_note,
    encode_naddr,
    encode_nevent,
)
from ...outbox import parse_relay_list
from ...queries import fetch_addressable_events, fetch_latest_event
from ...relay import RelayPool
from ..constants import (
    NOSTR_INDEXER_RELAYS,
    NOSTR_LONGFORM_RELAYS,
    NOSTR_WIKI_KIND,
)
from ..fetch import SourceFetcher
from ...rss.parser import FeedItem
from .mdx import derive_summary
from .wiki import normalize_wiki_content


NJUMP = "https://njump.me"

# Tolerant of an optional ``nostr:`` URI prefix (NIP-21) and of the
# entity being embedded anywhere in a host's URL (njump.me/..., habla
# .news/u/..., primal.net/p/...).
_ENTITY_RE = re.compile(
    r"(?:nostr:)?((?:npub|nprofile|nevent|note|naddr)1[a-z0-9]+)",
    re.IGNORECASE,
)

# A NIP-05 identifier is an email-shaped ``name@domain``. Anchored so it
# only matches a *bare* address, never a URL that merely contains ``@``.
_NIP05_RE = re.compile(
    r"^(?:nostr:)?([a-z0-9._%+-]+)@([a-z0-9.-]+\.[a-z]{2,})$",
    re.IGNORECASE,
)

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# NIP-19 entity extraction (pure)                                             #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class NostrEntity:
    """One decoded NIP-19 entity, shape-normalised across types."""

    type: str            # "npub" | "nprofile" | "nevent" | "note" | "naddr"
    bech32: str
    pubkey: Optional[str] = None      # author (npub/nprofile/naddr/nevent)
    event_id: Optional[str] = None    # nevent / note
    kind: Optional[int] = None        # naddr always; nevent when present
    identifier: Optional[str] = None  # naddr d-tag
    relays: Tuple[str, ...] = ()


def extract_nostr_entity(value: Optional[str]) -> Optional[NostrEntity]:
    """Find and decode the first NIP-19 entity in ``value``, else None."""
    if not value or not isinstance(value, str):
        return None
    match = _ENTITY_RE.search(value)
    if not match:
        return None
    bech = match.group(1)
    prefix = bech[: bech.index("1")].lower()
    try:
        if prefix == "npub":
            return NostrEntity("npub", bech, pubkey=decode_npub(bech))
        if prefix == "nprofile":
            pubkey, relays = decode_nprofile(bech)
            return NostrEntity(
                "nprofile", bech, pubkey=pubkey, relays=tuple(relays))
        if prefix == "note":
            return NostrEntity("note", bech, event_id=decode_note(bech))
        if prefix == "nevent":
            event_id, relays, author, kind = decode_nevent(bech)
            return NostrEntity(
                "nevent", bech, pubkey=author, event_id=event_id,
                kind=kind, relays=tuple(relays))
        if prefix == "naddr":
            d_tag, author, kind, relays = decode_naddr(bech)
            return NostrEntity(
                "naddr", bech, pubkey=author, kind=int(kind),
                identifier=d_tag, relays=tuple(relays))
    except (ValueError, IndexError, TypeError):
        return None
    return None


# --------------------------------------------------------------------------- #
# NIP-05                                                                      #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Nip05Address:
    name: str
    domain: str

    @property
    def address(self) -> str:
        return f"{self.name}@{self.domain}"


def extract_nip05(value: Optional[str]) -> Optional[Nip05Address]:
    """``name@domain`` for a bare NIP-05 identifier, else None."""
    match = _NIP05_RE.match(str(value or "").strip())
    if not match:
        return None
    return Nip05Address(match.group(1).lower(), match.group(2).lower())


def resolve_nip05(
    fetcher: SourceFetcher,
    address: Nip05Address,
    on_done: Callable[[Optional[Tuple[str, List[str]]]], None],
) -> None:
    """Resolve via ``/.well-known/nostr.json``; best-effort.

    ``on_done`` receives ``(pubkey_hex, relay_hints)`` or ``None`` on
    any network / shape / not-found failure so the caller can surface a
    friendly message.
    """
    url = (
        f"https://{address.domain}/.well-known/nostr.json"
        f"?name={address.name}"
    )

    def _on_body(body: str) -> None:
        try:
            payload = json.loads(body)
        except (ValueError, TypeError):
            on_done(None)
            return
        names = payload.get("names") if isinstance(payload, dict) else None
        pubkey = names.get(address.name) if isinstance(names, dict) else None
        if not isinstance(pubkey, str) or not _HEX64_RE.match(pubkey):
            on_done(None)
            return
        pubkey = pubkey.lower()
        relay_map = payload.get("relays")
        hints = (
            relay_map.get(pubkey)
            if isinstance(relay_map, dict) else None
        )
        relays = [r for r in hints if isinstance(r, str) and r] \
            if isinstance(hints, list) else []
        on_done((pubkey, relays))

    fetcher.fetch(url, on_success=_on_body, on_failure=lambda _err: on_done(None))


# --------------------------------------------------------------------------- #
# Event helpers (pure)                                                        #
# --------------------------------------------------------------------------- #

def event_tag(event: dict, name: str) -> str:
    """First value of tag ``name`` on ``event``, or ``''``."""
    for tag in (event or {}).get("tags", []) or []:
        if isinstance(tag, list) and len(tag) >= 2 and tag[0] == name:
            return str(tag[1] or "")
    return ""


def _title_from_content(markdown: str, max_len: int = 90) -> str:
    """A title for events that carry none (e.g. a kind-1 note)."""
    for line in str(markdown or "").split("\n"):
        line = re.sub(r"^#+\s*", "", line)
        line = re.sub(r"[*_`>]", "", line).strip()
        if line:
            if len(line) > max_len:
                return line[: max_len - 1].rstrip() + "…"
            return line
    return ""


def nostr_event_to_item(event: dict) -> FeedItem:
    """Normalise a Nostr event into the importer's ``FeedItem`` shape.

    Addressable events (30000-39999, incl. kind-30023 articles) are
    keyed and linked by their naddr coordinate; everything else by its
    nevent. Bodies are Markdown already, carried as
    ``content_markdown`` so the pipeline takes them verbatim.
    """
    kind = int(event.get("kind", 0))
    pubkey = str(event.get("pubkey", ""))
    d_tag = event_tag(event, "d")
    addressable = 30000 <= kind < 40000
    is_wiki = kind == NOSTR_WIKI_KIND

    if addressable:
        bech = encode_naddr(d_tag, pubkey, kind)
    else:
        bech = encode_nevent(str(event.get("id", "")), (), pubkey or None, kind)

    published_raw = event_tag(event, "published_at")
    try:
        published = int(published_raw) if published_raw else None
    except ValueError:
        published = None
    if published is None:
        published = int(event.get("created_at", 0)) or None

    body = normalize_wiki_content(event.get("content"))\
        if is_wiki else str(event.get("content") or "")
    title = (
        event_tag(event, "title")
        or _title_from_content(body)
        or f"Nostr note {str(event.get('id', ''))[:8]}"
    )
    topics = [
        str(t[1]) for t in event.get("tags", []) or []
        if isinstance(t, list) and len(t) >= 2 and t[0] == "t" and t[1]
    ]
    return FeedItem(
        guid=f"{kind}:{pubkey}:{d_tag}" if addressable else str(event.get("id", "")),
        title=title,
        link=f"{NJUMP}/{bech}",
        summary=event_tag(event, "summary") or derive_summary(body) or None,
        content_html="",
        published_at=published,
        categories=tuple(["wiki", *topics] if is_wiki else topics),
        image=event_tag(event, "image") or None,
        author=None,
        content_markdown=body,
    )


def dedup_relays(*groups: Iterable[str]) -> List[str]:
    """Order-preserving dedupe across relay groups; drops falsy values."""
    seen: set = set()
    out: List[str] = []
    for group in groups:
        for relay in group or ():
            if not isinstance(relay, str):
                continue
            cleaned = relay.strip()
            key = cleaned.rstrip("/").lower()
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(cleaned)
    return out


# --------------------------------------------------------------------------- #
# Relay-query adapter + higher-level lookups                                  #
# --------------------------------------------------------------------------- #

class RelayQueryAdapter(QObject):
    """The tiny relay-query surface resolvers depend on.

    Wraps the app's one-shot query helpers; injectable fakes implement
    the same two methods so resolver tests never touch relays.
    """

    def __init__(
        self,
        relay_pool: RelayPool,
        parent: Optional[QObject] = None,
        *,
        timeout_ms: int = 8_000,
    ) -> None:
        super().__init__(parent)
        self._pool = relay_pool
        self._timeout_ms = timeout_ms

    def latest(self, relays, filters, on_done) -> None:
        """Newest single matching event, or None."""
        try:
            fetch_latest_event(
                self._pool, list(relays), list(filters), on_done,
                timeout_ms=self._timeout_ms, parent=self,
            )
        except Exception:  # noqa: BLE001, settle the callback contract
            on_done(None)

    def addressable(self, relays, filters, on_done) -> None:
        """Newest-per-(kind,pubkey,d) events, newest first; may be []."""
        try:
            fetch_addressable_events(
                self._pool, list(relays), list(filters), on_done,
                timeout_ms=self._timeout_ms, parent=self,
            )
        except Exception:  # noqa: BLE001
            on_done([])


def author_article_relays(
    nostr_query,
    pubkey: str,
    hints: Iterable[str],
    on_done: Callable[[List[str]], None],
) -> None:
    """Best relays to read an author's articles from.

    Their advertised write relays (kind 10002, per the outbox model),
    plus any hint relays, plus the long-form fallback set. Never fails:
    a missed lookup just yields hints + fallback.
    """
    hint_list = list(hints or ())

    def _on_relay_list(event: Optional[dict]) -> None:
        outbox: List[str] = []
        if event:
            try:
                outbox = list(parse_relay_list(event).write)
            except Exception:  # noqa: BLE001, malformed event = no outbox
                outbox = []
        on_done(dedup_relays(hint_list, outbox, NOSTR_LONGFORM_RELAYS))

    nostr_query.latest(
        dedup_relays(hint_list, NOSTR_INDEXER_RELAYS),
        [{"kinds": [10002], "authors": [pubkey], "limit": 1}],
        _on_relay_list,
    )


def fetch_author_name(
    nostr_query,
    relays: Iterable[str],
    pubkey: str,
    on_done: Callable[[str], None],
) -> None:
    """Best-effort display name from the author's kind-0 metadata."""

    def _on_meta(event: Optional[dict]) -> None:
        if not event:
            on_done("")
            return
        try:
            meta = json.loads(event.get("content") or "{}")
            on_done(str(meta.get("display_name") or meta.get("name") or ""))
        except (ValueError, TypeError, AttributeError):
            on_done("")

    nostr_query.latest(
        list(relays),
        [{"kinds": [0], "authors": [pubkey], "limit": 1}],
        _on_meta,
    )
