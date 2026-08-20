# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Generic Nostr source resolver.

Turns any Nostr reference into importable long-form content, covering
the whole ecosystem behind one resolver instead of per-host cases:

- a bare or ``nostr:``-prefixed **npub / nprofile**: that author's
  kind-30023 long-form articles, read via their NIP-65 outbox relays;
- a **NIP-05** address (``name@domain``): resolved to a pubkey, then
  the same author-articles path;
- a **naddr / nevent / note**: that single event as one draft, with
  NIP-54 wiki bodies (kind 30818) normalised to Markdown;
- the same identifiers embedded in any host URL (njump.me, habla.news,
  yakihonne.com, primal.net, ...), since those are just SPAs around the
  same events.

Bodies are Markdown already, so items carry ``content_markdown`` and
the pipeline takes them verbatim. Registered *after* the NostrHub
resolver so ``nostrhub.io`` links keep their kind-30817 handling; this
resolver owns everything else Nostr.
"""

from __future__ import annotations

from typing import List, Optional

from ..constants import NOSTR_LONGFORM_RELAYS, NOSTR_MAX_ARTICLES
from ..errors import ERROR_CODES, SourceError
from ..registry import ResolveContext, ResolveInput, ResolveResult, SourceResolver
from ...rss.parser import Feed
from ..sources.nostr import (
    NJUMP,
    Nip05Address,
    NostrEntity,
    author_article_relays,
    dedup_relays,
    extract_nip05,
    extract_nostr_entity,
    fetch_author_name,
    nostr_event_to_item,
    resolve_nip05,
)


def _detect(input_: ResolveInput) -> bool:
    if not input_.url:
        return False
    return bool(
        extract_nostr_entity(input_.url) or extract_nip05(input_.url))


def _require_query(ctx: ResolveContext):
    query = getattr(ctx, "nostr_query", None)
    if query is None:
        ctx.on_failure(SourceError(
            "No relay connection is available for Nostr sources",
            ERROR_CODES.NO_RELAY_ACCESS,
        ))
    return query


def _canonical_source_url(
    raw_url: str,
    entity: Optional[NostrEntity],
    nip05: Optional[Nip05Address],
) -> str:
    """A re-resolvable URL to key + store the source under."""
    if nip05 is not None:
        return nip05.address
    lowered = (raw_url or "").lower()
    if lowered.startswith("http://") or lowered.startswith("https://"):
        return raw_url  # a real host URL: keep it
    return f"{NJUMP}/{entity.bech32}" if entity else raw_url


def _resolve(input_: ResolveInput, ctx: ResolveContext) -> None:
    url = input_.url or ""
    entity = extract_nostr_entity(url)
    nip05 = None if entity else extract_nip05(url)
    if entity is None and nip05 is None:
        ctx.on_failure(SourceError(
            "That is not a Nostr address we recognise",
            ERROR_CODES.NOSTR_INVALID,
        ))
        return
    query = _require_query(ctx)
    if query is None:
        return

    if entity is not None and entity.type in ("naddr", "nevent", "note"):
        _resolve_single_event(url, entity, ctx, query)
        return

    # An author (npub / nprofile / NIP-05): their long-form articles.
    if entity is not None:
        _resolve_author(url, entity, None, entity.pubkey,
                        list(entity.relays), ctx, query)
        return

    ctx.stage("connecting", url, hostname=nip05.domain)
    resolve_nip05(
        ctx.fetcher,
        nip05,
        lambda result: (
            _resolve_author(url, None, nip05, result[0], result[1], ctx, query)
            if result is not None
            else ctx.on_failure(SourceError(
                f"No Nostr profile is published at {nip05.address}",
                ERROR_CODES.NIP05_NOT_FOUND,
            ))
        ),
    )


def _resolve_single_event(
    url: str,
    entity: NostrEntity,
    ctx: ResolveContext,
    query,
) -> None:
    ctx.stage("connecting", url, hostname="nostr")
    if entity.type == "naddr":
        filters = [{
            "kinds": [entity.kind],
            "authors": [entity.pubkey],
            "#d": [entity.identifier],
            "limit": 1,
        }]
    else:
        filters = [{"ids": [entity.event_id], "limit": 1}]
    relays = dedup_relays(entity.relays, NOSTR_LONGFORM_RELAYS)
    ctx.stage("parsing", url, hostname="nostr")

    def _on_event(event) -> None:
        if ctx.is_cancelled():
            return
        if not event:
            ctx.on_failure(SourceError(
                "That Nostr event was not found on the relays",
                ERROR_CODES.NOSTR_NOT_FOUND,
            ))
            return
        item = nostr_event_to_item(event)
        feed = Feed(
            format="nostr", title=item.title, link=item.link,
            description=None, items=(item,),
        )
        ctx.stage("done", item.link or url, hostname="nostr", item_count=1)
        ctx.on_success(ResolveResult(url=item.link or url, feed=feed))

    query.latest(relays, filters, _on_event)


def _resolve_author(
    url: str,
    entity: Optional[NostrEntity],
    nip05: Optional[Nip05Address],
    pubkey: str,
    hints: List[str],
    ctx: ResolveContext,
    query,
) -> None:
    host = nip05.domain if nip05 is not None else "nostr"
    ctx.stage("parsing", url, hostname=host)

    def _on_relays(relays: List[str]) -> None:
        if ctx.is_cancelled():
            return
        query.addressable(
            relays,
            [{"kinds": [30023], "authors": [pubkey],
              "limit": NOSTR_MAX_ARTICLES}],
            lambda events: _on_articles(relays, events or []),
        )

    def _on_articles(relays: List[str], events: list) -> None:
        if ctx.is_cancelled():
            return
        fetch_author_name(
            query, relays, pubkey,
            lambda name: _finish(events, name),
        )

    def _finish(events: list, author_name: str) -> None:
        if ctx.is_cancelled():
            return
        items = tuple(nostr_event_to_item(e) for e in events)
        source_url = _canonical_source_url(url, entity, nip05)
        title = (
            f"{author_name} · Articles" if author_name
            else (nip05.address if nip05 else "Nostr articles")
        )
        # An author with no articles yields an empty feed (the UI shows
        # a calm "nothing to import" state), not an error.
        ctx.stage("done", source_url, hostname=host, item_count=len(items))
        ctx.on_success(ResolveResult(
            url=source_url,
            feed=Feed(format="nostr", title=title, link=source_url,
                      description=None, items=items),
        ))

    author_article_relays(query, pubkey, hints, _on_relays)


NOSTR_RESOLVER = SourceResolver(
    id="nostr",
    label="Nostr profile or event",
    detect=_detect,
    resolve=_resolve,
)
