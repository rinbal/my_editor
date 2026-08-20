# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ghost export resolver.

A Ghost export is a single JSON file (not a ZIP), so it fits the
body-detected resolver pattern like WXR: recognise the ``db``/``posts``
shape in a pasted or uploaded body and turn the published posts into
normalised ``FeedItem``s. Ordinary JSON Feeds have a different shape,
so this never intercepts them. Runs before the RSS catch-all.
"""

from __future__ import annotations

from ...rss.parser import Feed
from ..errors import ERROR_CODES, SourceError
from ..registry import ResolveContext, ResolveInput, ResolveResult, SourceResolver
from ..sources.ghost import is_ghost_export, parse_ghost_export


def _detect(input_: ResolveInput) -> bool:
    return bool(input_.pasted_body and is_ghost_export(input_.pasted_body))


def _resolve(input_: ResolveInput, ctx: ResolveContext) -> None:
    url = input_.url or ""
    ctx.stage("parsing", url)

    def _deliver(export) -> None:
        if ctx.is_cancelled():
            return
        if not export.items:
            ctx.on_failure(SourceError(
                "No published posts were found in that Ghost export",
                ERROR_CODES.GHOST_EMPTY,
            ))
            return
        feed = Feed(
            format="ghost",
            title=export.title or "Ghost export",
            link=None,
            description=None,
            items=export.items,
        )
        ctx.stage("done", url, item_count=len(feed.items))
        ctx.on_success(ResolveResult(url=url, feed=feed))

    ctx.blocking(
        lambda: parse_ghost_export(input_.pasted_body or ""),
        _deliver,
        lambda exc: ctx.on_failure(SourceError(
            f"Could not read that Ghost export: {exc}",
            ERROR_CODES.UNKNOWN,
        )),
    )


GHOST_RESOLVER = SourceResolver(
    id="ghost",
    label="Ghost export (JSON)",
    detect=_detect,
    resolve=_resolve,
)
