# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sitemap source resolver.

Claims an explicit sitemap URL (``.../sitemap.xml``,
``.../sitemap_index.xml``, ``.../post-sitemap.xml``) or a pasted
sitemap document, and turns its article URLs into importable items
whose bodies are recovered per-article at import time. The RSS resolver
additionally falls back to sitemap *discovery* when a page exposes no
feed, so a user is never dead-ended.

Runs before the RSS catch-all so a sitemap URL is not mistaken for a
feed.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from ...rss.parser import Feed
from ..errors import ERROR_CODES, SourceError
from ..registry import ResolveContext, ResolveInput, ResolveResult, SourceResolver
from ..sources.sitemap import (
    collect_sitemap_entries,
    entries_to_items,
    is_sitemap,
)

_SITEMAP_URL_RE = re.compile(r"sitemap[^/]*\.xml(?:$|[?#])", re.IGNORECASE)


def _hostname(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").removeprefix("www.")
    except ValueError:
        return ""


def _detect(input_: ResolveInput) -> bool:
    if input_.pasted_body and is_sitemap(input_.pasted_body):
        return True
    return bool(input_.url and _SITEMAP_URL_RE.search(input_.url))


def sitemap_feed(entries, *, source_url: str) -> Feed:
    host = _hostname(source_url)
    return Feed(
        format="sitemap",
        title=f"{host} · sitemap" if host else "Sitemap",
        link=source_url or None,
        description=None,
        items=tuple(entries_to_items(entries)),
    )


def _resolve(input_: ResolveInput, ctx: ResolveContext) -> None:
    url = input_.url or ""
    ctx.stage("connecting", url, hostname=_hostname(url))

    def _on_entries(entries) -> None:
        if ctx.is_cancelled():
            return
        if not entries:
            ctx.on_failure(SourceError(
                "That sitemap had no importable article URLs",
                ERROR_CODES.SITEMAP_EMPTY,
            ))
            return
        feed = sitemap_feed(entries, source_url=url)
        ctx.stage("done", url, hostname=_hostname(url),
                  item_count=len(feed.items))
        ctx.on_success(ResolveResult(url=url, feed=feed))

    collect_sitemap_entries(
        fetcher=ctx.fetcher,
        url=url or None,
        text=input_.pasted_body,
        on_done=_on_entries,
        is_cancelled=ctx.is_cancelled,
    )


SITEMAP_RESOLVER = SourceResolver(
    id="sitemap",
    label="Sitemap",
    detect=_detect,
    resolve=_resolve,
)
