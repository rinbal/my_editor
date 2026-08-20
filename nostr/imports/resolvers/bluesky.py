# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bluesky thread source resolver.

Claims a ``bsky.app`` post link (or an ``at://...app.bsky.feed.post/``
URI) and stitches the author's whole self-thread into one long-form
draft. Runs before the RSS catch-all so a Bluesky link is never
mis-fetched as a web page.

We are a long-form tool: a single post is not an article; only a
multi-post self-thread stitches into one, so a lone post is rejected
with a clear explanation rather than imported as a one-liner.
"""

from __future__ import annotations

from ...rss.parser import Feed
from ..errors import ERROR_CODES, SourceError
from ..registry import ResolveContext, ResolveInput, ResolveResult, SourceResolver
from ..sources.bluesky import parse_bluesky_url, resolve_bluesky_thread


def _detect(input_: ResolveInput) -> bool:
    return bool(input_.url and parse_bluesky_url(input_.url))


def _resolve(input_: ResolveInput, ctx: ResolveContext) -> None:
    url = input_.url or ""
    ctx.stage("connecting", url, hostname="bsky.app")

    def _on_thread(result: dict) -> None:
        if ctx.is_cancelled():
            return
        item = result["item"]
        if result["count"] < 2:
            ctx.on_failure(SourceError(
                "That is a single Bluesky post, not a thread",
                ERROR_CODES.BLUESKY_NOT_THREAD,
            ))
            return
        if not (item.content_markdown or "").strip():
            ctx.on_failure(SourceError(
                "That Bluesky thread had no text to import",
                ERROR_CODES.BLUESKY_NOT_FOUND,
            ))
            return
        who = result["display_name"] or f"@{result['handle']}"
        feed = Feed(
            format="bluesky",
            title=f"Bluesky thread by {who} ({result['count']} posts)",
            link=item.link,
            description=None,
            items=(item,),
        )
        ctx.stage("done", item.link or url, hostname="bsky.app", item_count=1)
        ctx.on_success(ResolveResult(url=item.link or url, feed=feed))

    def _on_error(message: str, code: str) -> None:
        if ctx.is_cancelled():
            return
        ctx.on_failure(SourceError(
            message, getattr(ERROR_CODES, code, ERROR_CODES.UNKNOWN)))

    resolve_bluesky_thread(
        url, fetcher=ctx.fetcher, on_done=_on_thread, on_error=_on_error)


BLUESKY_RESOLVER = SourceResolver(
    id="bluesky",
    label="Bluesky thread",
    detect=_detect,
    resolve=_resolve,
)
