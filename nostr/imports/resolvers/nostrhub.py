# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""NostrHub NIP source resolver.

NostrHub (https://nostrhub.io) is a client-rendered SPA with no RSS and
no server-rendered content, so the RSS resolver can never see its
material. Its "NIPs" (Nostr Implementation Possibilities) are published
as kind-30817 addressable events on NostrHub's Ditto relays, NOT on the
author's kind-10002 outbox, so this resolver queries those relays
explicitly (hint relays from an nprofile/naddr merge in ahead).

Scope is deliberately narrow: a ``nostrhub.io`` URL carrying an npub /
nprofile / naddr, or a bare naddr that decodes to kind 30817. A bare
npub is ambiguous (articles vs NIPs) and belongs to the generic Nostr
resolver, which is registered after this one.
"""

from __future__ import annotations

import re
from typing import List

from ..constants import NOSTR_MAX_ARTICLES, NOSTRHUB_NIP_KIND, NOSTRHUB_RELAYS
from ..errors import ERROR_CODES, SourceError
from ..registry import ResolveContext, ResolveInput, ResolveResult, SourceResolver
from ...rss.parser import Feed, FeedItem
from ..sources.mdx import derive_summary
from ..sources.nostr import (
    NostrEntity,
    dedup_relays,
    event_tag,
    extract_nostr_entity,
    fetch_author_name,
)
from ...bech32 import encode_naddr

HUB_BASE_URL = "https://nostrhub.io"

_NOSTRHUB_HOST_RE = re.compile(r"(^|//|\.)nostrhub\.io(/|$)", re.IGNORECASE)


def _detect(input_: ResolveInput) -> bool:
    url = input_.url
    if not url:
        return False
    entity = extract_nostr_entity(url)
    if entity is None:
        return False
    if _NOSTRHUB_HOST_RE.search(url):
        return entity.type in ("npub", "nprofile", "naddr")
    # Off-host, only a bare NIP coordinate is unambiguous enough to claim.
    return entity.type == "naddr" and entity.kind == NOSTRHUB_NIP_KIND


def _kind_labels(event: dict) -> List[str]:
    """Hashtag-friendly labels from the NIP's ``k`` (supported-kind) tags."""
    labels = ["nip"]
    for tag in event.get("tags", []) or []:
        if isinstance(tag, list) and len(tag) >= 2 and tag[0] == "k" and tag[1]:
            labels.append(f"kind-{tag[1]}")
    return labels


def nip_event_to_item(event: dict) -> FeedItem:
    """Kind-30817 event -> normalised ``FeedItem`` (body is Markdown)."""
    d_tag = event_tag(event, "d")
    pubkey = str(event.get("pubkey", ""))
    kind = int(event.get("kind", NOSTRHUB_NIP_KIND))
    title = event_tag(event, "title") or d_tag or "Untitled NIP"
    published_raw = event_tag(event, "published_at")
    try:
        published = int(published_raw) if published_raw else None
    except ValueError:
        published = None
    if published is None:
        published = int(event.get("created_at", 0)) or None
    naddr = encode_naddr(d_tag, pubkey, kind)
    body = str(event.get("content") or "")
    return FeedItem(
        guid=f"{kind}:{pubkey}:{d_tag}",  # stable coordinate = stable d-tag
        title=title,
        link=f"{HUB_BASE_URL}/{naddr}",
        summary=derive_summary(body) or None,
        content_html="",
        published_at=published,
        categories=tuple(_kind_labels(event)),
        image=event_tag(event, "image") or None,
        author=None,
        content_markdown=body,
    )


def _canonical_source_url(raw_url: str, bech32: str) -> str:
    """Keep an actual NostrHub link; canonicalise everything else."""
    if _NOSTRHUB_HOST_RE.search(raw_url or ""):
        return raw_url
    return f"{HUB_BASE_URL}/{bech32}"


def _build_feed(events: list, *, author_name: str, source_url: str) -> Feed:
    items = sorted(
        (nip_event_to_item(e) for e in events),
        key=lambda it: it.published_at or 0,
        reverse=True,
    )
    title = f"{author_name} · NostrHub NIPs" if author_name else "NostrHub NIPs"
    return Feed(format="nostr", title=title, link=source_url,
                description=None, items=tuple(items))


def _resolve(input_: ResolveInput, ctx: ResolveContext) -> None:
    url = input_.url or ""
    entity = extract_nostr_entity(url)
    if entity is None:
        ctx.on_failure(SourceError(
            "Not a NostrHub source", ERROR_CODES.NOSTR_INVALID))
        return
    query = getattr(ctx, "nostr_query", None)
    if query is None:
        ctx.on_failure(SourceError(
            "No relay connection is available for Nostr sources",
            ERROR_CODES.NO_RELAY_ACCESS,
        ))
        return

    ctx.stage("connecting", url, hostname="nostrhub.io")
    relays = dedup_relays(entity.relays, NOSTRHUB_RELAYS)

    def _finish(events: list, source_url: str) -> None:
        if ctx.is_cancelled():
            return
        fetch_author_name(
            query, relays, _author_of(entity, events),
            lambda name: _deliver(events, name, source_url),
        )

    def _deliver(events: list, author_name: str, source_url: str) -> None:
        if ctx.is_cancelled():
            return
        feed = _build_feed(events, author_name=author_name,
                           source_url=source_url)
        ctx.stage("done", source_url, hostname="nostrhub.io",
                  item_count=len(feed.items))
        ctx.on_success(ResolveResult(url=source_url, feed=feed))

    ctx.stage("parsing", url, hostname="nostrhub.io")
    if entity.type == "naddr":
        source_url = _canonical_source_url(url, entity.bech32)

        def _on_event(event) -> None:
            if ctx.is_cancelled():
                return
            if not event:
                ctx.on_failure(SourceError(
                    "That NostrHub NIP was not found on its relays",
                    ERROR_CODES.NOSTR_NOT_FOUND,
                ))
                return
            _finish([event], source_url)

        query.latest(relays, [{
            "kinds": [entity.kind],
            "authors": [entity.pubkey],
            "#d": [entity.identifier],
            "limit": 1,
        }], _on_event)
        return

    # Author profile: every NIP that author has published.
    source_url = _canonical_source_url(url, entity.bech32)
    query.addressable(relays, [{
        "kinds": [NOSTRHUB_NIP_KIND],
        "authors": [entity.pubkey],
        "limit": NOSTR_MAX_ARTICLES,
    }], lambda events: _finish(events or [], source_url))


def _author_of(entity: NostrEntity, events: list) -> str:
    if entity.pubkey:
        return entity.pubkey
    for event in events:
        pubkey = str(event.get("pubkey", ""))
        if pubkey:
            return pubkey
    return ""


NOSTRHUB_RESOLVER = SourceResolver(
    id="nostrhub",
    label="NostrHub NIP",
    detect=_detect,
    resolve=_resolve,
)
