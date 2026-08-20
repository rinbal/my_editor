# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""RSS / Atom / JSON Feed resolver, the default (catch-all).

Behaviour, in order:

  1. ``pasted_body``: parse directly (files and pastes never hit the
     network).
  2. Otherwise fetch the URL, and if the body parses as a feed, done.
  3. If the body is an HTML page instead, run the discovery dance as a
     breadth-first search over candidates:
       - every ``<link rel="alternate">`` hint from the page's
         ``<head>``, in document order;
       - then the common-feed-path palette (``/feed/``, ``/rss.xml``,
         ...) probed under three bases: the pasted path, its first
         segment, and the bare origin.
     Candidates are tried once each (a ``tried`` set makes discovery
     idempotent) and palette probes never spawn further palette probes,
     so a server that answers soft-404 HTML for every path cannot send
     the search into ever-deeper URLs.
  4. Exhausted candidates fall back to the site's *sitemap* (robots.txt
     ``Sitemap:`` lines, then the conventional paths): its article URLs
     become importable items whose bodies (and real titles) the import
     pipeline recovers per-article via full-text extraction.
  5. Only then a terminal error, the most useful one available: the
     transport failure when nothing was ever fetched, ``NO_FEED_FOUND``
     when we only ever saw HTML, ``NOT_A_FEED`` for non-HTML garbage.

This resolver stays the *last* entry in the registry: its ``detect`` is
the broad "looks like a fetchable URL, or a pasted body" test, so more
specific resolvers get first refusal on any given input.
"""

from __future__ import annotations

from typing import List, Optional

from ...rss.discovery import (
    candidate_feed_urls,
    extract_feeds_from_html,
    is_likely_feed_url,
    looks_like_html,
    normalize_user_url,
)
from ...rss.parser import RssError, parse_feed
from ..errors import ERROR_CODES, SourceError
from ..registry import ResolveContext, ResolveInput, ResolveResult, SourceResolver
from ..sources.podcast import enrich_feed_with_podcast


def _parse_feed_enriched(body: str):
    """Parse a feed body and recover the Podcasting 2.0 layer (audio
    enclosures + podcast/itunes namespaces) the shared parser drops.
    A no-op for feeds without audio enclosures."""
    return enrich_feed_with_podcast(parse_feed(body), body)


def _detect(input_: ResolveInput) -> bool:
    if input_.pasted_body and input_.pasted_body.strip():
        return True
    return bool(input_.url) and is_likely_feed_url(input_.url)


def _resolve(input_: ResolveInput, ctx: ResolveContext) -> None:
    body = (input_.pasted_body or "").strip()
    if body:
        url = input_.url or ""
        ctx.stage("parsing", url)

        def _parsed(feed) -> None:
            if ctx.is_cancelled():
                return
            ctx.stage("done", url, item_count=len(feed.items))
            ctx.on_success(ResolveResult(url=url, feed=feed))

        def _parse_failed(exc: BaseException) -> None:
            if ctx.is_cancelled():
                return
            if isinstance(exc, RssError):
                ctx.on_failure(SourceError(str(exc), ERROR_CODES.NOT_A_FEED))
            else:
                ctx.on_failure(SourceError(
                    f"Unexpected parse error: {exc}", ERROR_CODES.UNKNOWN))

        ctx.blocking(lambda: _parse_feed_enriched(body), _parsed, _parse_failed)
        return

    if not input_.url:
        ctx.on_failure(SourceError(
            "Either a URL or a pasted body is required",
            ERROR_CODES.UNSUPPORTED_SOURCE,
        ))
        return
    _Discovery(normalize_user_url(input_.url), ctx).run()


class _Discovery:
    """Breadth-first feed discovery over a candidate queue.

    Plain object (no Qt) driven entirely by fetcher callbacks; with a
    synchronous fake fetcher the whole search runs synchronously, which
    is what makes it testable without an event loop.
    """

    def __init__(self, start_url: str, ctx: ResolveContext) -> None:
        self._ctx = ctx
        self._start_url = start_url
        self._candidates: List[str] = [start_url]
        self._tried: set[str] = set()
        # Palette-synthesised candidates. They must not spawn *more*
        # palette probes: a server returning soft-404 HTML for every
        # path would otherwise generate ever-deeper URLs forever.
        self._fallback_probes: set[str] = set()
        self._hints: tuple = ()
        self._first_fetch_error: Optional[SourceError] = None
        self._saw_html = False
        self._saw_garbage = False

    def run(self) -> None:
        self._try_next()

    # -- the search loop ---------------------------------------------------

    def _try_next(self) -> None:
        if self._ctx.is_cancelled():
            return
        while self._candidates:
            url = self._candidates.pop(0)
            if url in self._tried:
                continue
            self._tried.add(url)
            self._ctx.stage("connecting", url)
            self._ctx.fetcher.fetch(
                url,
                on_success=lambda body, u=url: self._on_body(u, body),
                on_failure=self._on_fetch_failed,
            )
            return
        self._give_up()

    def _on_fetch_failed(self, error: SourceError) -> None:
        if self._first_fetch_error is None:
            self._first_fetch_error = error
        self._try_next()

    def _on_parsed(self, url: str, feed) -> None:
        if self._ctx.is_cancelled():
            return
        self._ctx.stage("done", url, item_count=len(feed.items))
        self._ctx.on_success(ResolveResult(url=url, feed=feed, hints=self._hints))

    def _on_parse_failed(self, exc: BaseException) -> None:
        if self._ctx.is_cancelled():
            return
        if isinstance(exc, RssError):
            # Neither HTML nor a feed; remember and keep searching.
            self._saw_garbage = True
            self._try_next()
            return
        self._ctx.on_failure(SourceError(
            f"Unexpected parse error: {exc}", ERROR_CODES.UNKNOWN))

    def _on_body(self, url: str, body: str) -> None:
        if self._ctx.is_cancelled():
            return
        is_html = looks_like_html(body)

        if not is_html:
            self._ctx.stage("parsing", url)
            # Parsing can be CPU-heavy (a 16 MiB feed); the context's
            # executor keeps it off the UI thread when one is wired.
            self._ctx.blocking(
                lambda: _parse_feed_enriched(body),
                lambda feed: self._on_parsed(url, feed),
                self._on_parse_failed,
            )
            return

        # An HTML page. Queue its autodiscovery hints, else the palette.
        self._saw_html = True
        self._ctx.stage("discovering", url)
        hints = extract_feeds_from_html(body, base_url=url)
        if hints:
            self._hints = tuple(hints)
            self._candidates.extend(h.url for h in hints)
        elif url not in self._fallback_probes:
            for fallback in candidate_feed_urls(url):
                if fallback not in self._tried and fallback not in self._candidates:
                    self._candidates.append(fallback)
                    self._fallback_probes.add(fallback)
        self._try_next()

    # -- sitemap fallback + terminal failure -------------------------------

    def _give_up(self) -> None:
        if self._saw_html:
            # The site is real but advertises no feed anywhere. Last
            # resort: its sitemap, whose article URLs import via the
            # pipeline's per-article full-text recovery.
            self._try_sitemap_fallback()
            return
        if self._saw_garbage:
            self._ctx.on_failure(SourceError(
                "Response was neither a feed nor an HTML page",
                ERROR_CODES.NOT_A_FEED,
            ))
            return
        # Nothing was ever fetched: surface the transport reason.
        self._ctx.on_failure(
            self._first_fetch_error
            or SourceError("No URL could be fetched", ERROR_CODES.FETCH_ERROR)
        )

    def _try_sitemap_fallback(self) -> None:
        from ..sources.sitemap import discover_sitemap
        from .sitemap import sitemap_feed

        self._ctx.stage("discovering", self._start_url)

        def _on_sitemap(found) -> None:
            if self._ctx.is_cancelled():
                return
            if found:
                _sitemap_url, entries = found
                feed = sitemap_feed(entries, source_url=self._start_url)
                self._ctx.stage("done", self._start_url,
                                item_count=len(feed.items))
                self._ctx.on_success(ResolveResult(
                    url=self._start_url, feed=feed, hints=self._hints))
                return
            self._ctx.on_failure(SourceError(
                "No feed found at this site or any well-known path",
                ERROR_CODES.NO_FEED_FOUND,
            ))

        discover_sitemap(
            self._start_url,
            fetcher=self._ctx.fetcher,
            on_done=_on_sitemap,
            is_cancelled=self._ctx.is_cancelled,
        )


RSS_RESOLVER = SourceResolver(
    id="rss",
    label="RSS / Atom / JSON Feed",
    detect=_detect,
    resolve=_resolve,
)
