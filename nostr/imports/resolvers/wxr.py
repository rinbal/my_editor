# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""WordPress WXR export resolver.

Claims a pasted or uploaded WordPress export (the ``wp:`` namespace is
the unambiguous marker) and turns the published posts + pages into
normalised ``FeedItem``s. Detection is body-based: a WXR is a
downloaded file, not a live URL, and a live ``/feed/`` never carries
the namespace, so this can never intercept an ordinary feed. Runs
before the RSS catch-all.
"""

from __future__ import annotations

from ...rss.parser import Feed
from ..errors import ERROR_CODES, SourceError
from ..registry import ResolveContext, ResolveInput, ResolveResult, SourceResolver
from ..sources.wxr import is_wxr, parse_wxr


def _detect(input_: ResolveInput) -> bool:
    return bool(input_.pasted_body and is_wxr(input_.pasted_body))


def _resolve(input_: ResolveInput, ctx: ResolveContext) -> None:
    url = input_.url or ""
    ctx.stage("parsing", url)

    def _deliver(export) -> None:
        if ctx.is_cancelled():
            return
        if not export.items:
            ctx.on_failure(SourceError(
                "No published posts or pages were found in that WordPress "
                "export",
                ERROR_CODES.WXR_EMPTY,
            ))
            return
        feed = Feed(
            format="wxr",
            title=export.title or "WordPress export",
            link=None,
            description=None,
            items=export.items,
        )
        ctx.stage("done", url, item_count=len(feed.items))
        ctx.on_success(ResolveResult(url=url, feed=feed))

    # Exports run to tens of MB; parse off the UI thread when wired.
    ctx.blocking(
        lambda: parse_wxr(input_.pasted_body or ""),
        _deliver,
        lambda exc: ctx.on_failure(SourceError(
            f"Could not read that WordPress export: {exc}",
            ERROR_CODES.UNKNOWN,
        )),
    )


WXR_RESOLVER = SourceResolver(
    id="wxr",
    label="WordPress export (WXR)",
    detect=_detect,
    resolve=_resolve,
)
