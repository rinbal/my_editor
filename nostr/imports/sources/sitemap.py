# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sitemap (sitemaps.org) parsing + discovery.

A sitemap is a site's own list of URLs: the natural fallback when a
site exposes no RSS/Atom/JSON feed. Its article URLs become importable
items whose body is recovered per-article at import time (the existing
Readability full-text path), so the user is never dead-ended.

Shapes handled:
- ``<urlset>`` of ``<url><loc>..</loc><lastmod>..</lastmod></url>``
- a sitemap *index* ``<sitemapindex>`` pointing at sub-sitemaps
- referenced from ``robots.txt`` via ``Sitemap: <url>`` lines

Parsed with tolerant regex: a malformed sitemap yields fewer entries,
never an exception. Gzipped ``.xml.gz`` sitemaps are out of scope (rare
for blogs; the plain ``/sitemap.xml`` a homepage serves is what the
fallback fetches).

To keep imports sane, entries are filtered to article-like URLs (media,
feeds, and taxonomy/pagination paths dropped), sorted newest-first by
``lastmod``, and capped, so a 10k-URL sitemap never floods the preview.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, List, Optional, Tuple
from urllib.parse import unquote, urlparse

from ...rss.parser import FeedItem


SITEMAP_MAX_URLS = 200
SITEMAP_MAX_SUBMAPS = 5

_SITEMAP_ROOT_RE = re.compile(r"<(?:urlset|sitemapindex)\b", re.IGNORECASE)
_URL_BLOCK_RE = re.compile(r"<url\b[^>]*>([\s\S]*?)</url>", re.IGNORECASE)
_SUB_BLOCK_RE = re.compile(r"<sitemap\b[^>]*>([\s\S]*?)</sitemap>", re.IGNORECASE)
_ROBOTS_SITEMAP_RE = re.compile(r"^\s*sitemap:\s*(\S+)", re.IGNORECASE | re.MULTILINE)

# Non-article URLs: assets, feeds, and taxonomy/pagination/system paths.
_SKIP_EXT_RE = re.compile(
    r"\.(jpe?g|png|gif|webp|svg|ico|bmp|pdf|zip|gz|mp[34]|m4a|mov|avi|css|js"
    r"|json|xml)(?:$|[?#])",
    re.IGNORECASE,
)
_SKIP_PATH_RE = re.compile(
    r"/(?:tags?|categor(?:y|ies)|authors?|pages?|feed|amp|wp-content|wp-json"
    r"|wp-admin|search|comments?)/",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SitemapEntry:
    loc: str
    lastmod: str = ""


def is_sitemap(text: Optional[str]) -> bool:
    return isinstance(text, str) and bool(
        _SITEMAP_ROOT_RE.search(text[:4000]))


def _decode(value: str) -> str:
    return (
        str(value)
        .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
        .replace("&#039;", "'").replace("&apos;", "'").replace("&amp;", "&")
    )


def _tag(block: str, name: str) -> str:
    match = re.search(
        rf"<{name}\b[^>]*>([\s\S]*?)</{name}>", block, re.IGNORECASE)
    return _decode(match.group(1).strip()) if match else ""


def parse_sitemap(text: str) -> Tuple[List[SitemapEntry], List[SitemapEntry]]:
    """Parse into ``(urls, sub_sitemaps)``; both may be empty."""
    src = str(text or "")
    urls = []
    subs = []
    for match in _URL_BLOCK_RE.finditer(src):
        loc = _tag(match.group(1), "loc")
        if loc:
            urls.append(SitemapEntry(loc, _tag(match.group(1), "lastmod")))
    for match in _SUB_BLOCK_RE.finditer(src):
        loc = _tag(match.group(1), "loc")
        if loc:
            subs.append(SitemapEntry(loc, _tag(match.group(1), "lastmod")))
    return urls, subs


def _lastmod_seconds(value: str) -> int:
    if not value:
        return 0
    raw = value.strip()
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    except ValueError:
        return 0


def is_article_url(loc: str) -> bool:
    try:
        parsed = urlparse(loc)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    if parsed.path in ("", "/"):
        return False  # homepage
    if _SKIP_EXT_RE.search(parsed.path):
        return False
    if _SKIP_PATH_RE.search(parsed.path):
        return False
    return True


def _sub_score(loc: str) -> int:
    """Sub-sitemaps that look like posts/articles fetch first.

    Scored on the path only, never the host, else a domain like
    ``blog.example`` would make every sub-sitemap look like a "blog"
    one and defeat the ranking.
    """
    path = loc
    try:
        path = urlparse(loc).path
    except ValueError:
        pass
    path = path.lower()
    if re.search(r"post|article|blog|entry|news", path):
        return 3
    if re.search(r"category|tag|author|attachment", path):
        return -1
    if "page" in path:
        return 1
    return 2


def title_from_url(loc: str) -> str:
    """Slug-derived placeholder title, upgraded at import time."""
    try:
        segments = [s for s in urlparse(loc).path.split("/") if s]
        last = unquote(segments[-1]) if segments else ""
        last = re.sub(r"\.(?:html?|php|aspx?)$", "", last, flags=re.IGNORECASE)
        last = re.sub(r"[-_]+", " ", last).strip()
        if not last:
            return "Untitled"
        return last[0].upper() + last[1:]
    except ValueError:
        return "Untitled"


def filter_entries(entries: List[SitemapEntry]) -> List[SitemapEntry]:
    """Article-like only, newest first by lastmod, capped."""
    kept = [e for e in entries if is_article_url(e.loc)]
    kept.sort(key=lambda e: _lastmod_seconds(e.lastmod), reverse=True)
    return kept[:SITEMAP_MAX_URLS]


def entries_to_items(entries: List[SitemapEntry]) -> List[FeedItem]:
    """Sitemap entries -> normalised ``FeedItem``s.

    Bodies are empty on purpose: every item is "thin", so the pipeline's
    full-text recovery reads the actual page at import time and also
    upgrades the slug placeholder title (``title_from_url=True``).
    """
    return [
        FeedItem(
            guid=e.loc,
            title=title_from_url(e.loc),
            link=e.loc,
            summary=None,
            content_html="",
            published_at=_lastmod_seconds(e.lastmod) or None,
            categories=(),
            image=None,
            author=None,
            title_from_url=True,
        )
        for e in entries
    ]


# --------------------------------------------------------------------------- #
# Async collection + discovery (callback style over the shared fetcher)       #
# --------------------------------------------------------------------------- #

def collect_sitemap_entries(
    *,
    fetcher,
    url: Optional[str] = None,
    text: Optional[str] = None,
    on_done: Callable[[List[SitemapEntry]], None],
    is_cancelled: Callable[[], bool] = lambda: False,
) -> None:
    """Resolve a sitemap (index or urlset) into filtered entries.

    An index is followed into its highest-ranked sub-sitemaps (up to
    ``SITEMAP_MAX_SUBMAPS``, sequentially). Every failure path delivers
    ``[]`` rather than an error; the caller decides what that means.
    """

    def _with_body(body: Optional[str]) -> None:
        if not is_sitemap(body):
            on_done([])
            return
        urls, subs = parse_sitemap(body)
        if urls or not subs:
            on_done(filter_entries(urls))
            return

        ranked = sorted(subs, key=lambda e: _sub_score(e.loc), reverse=True)
        ranked = ranked[:SITEMAP_MAX_SUBMAPS]
        collected: List[SitemapEntry] = []

        def _next_sub(index: int) -> None:
            if (
                index >= len(ranked)
                or is_cancelled()
                or len(collected) >= SITEMAP_MAX_URLS * 3
            ):
                on_done(filter_entries(collected))
                return
            fetcher.fetch(
                ranked[index].loc,
                on_success=lambda sub_body, i=index: _sub_done(i, sub_body),
                on_failure=lambda _err, i=index: _next_sub(i + 1),
            )

        def _sub_done(index: int, sub_body: str) -> None:
            if is_sitemap(sub_body):
                collected.extend(parse_sitemap(sub_body)[0])
            _next_sub(index + 1)

        _next_sub(0)

    if text is not None:
        _with_body(text)
        return
    if not url:
        on_done([])
        return
    fetcher.fetch(
        url,
        on_success=_with_body,
        on_failure=lambda _err: on_done([]),
    )


def discover_sitemap(
    page_url: str,
    *,
    fetcher,
    on_done: Callable[[Optional[Tuple[str, List[SitemapEntry]]]], None],
    is_cancelled: Callable[[], bool] = lambda: False,
) -> None:
    """Find a site's sitemap from a page/origin.

    ``robots.txt``'s ``Sitemap:`` lines are authoritative and tried
    first, then the conventional ``/sitemap.xml`` and
    ``/sitemap_index.xml``. Delivers ``(url, entries)`` for the first
    candidate that yields article URLs, else ``None``.
    """
    try:
        parsed = urlparse(page_url)
        origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else ""
    except ValueError:
        origin = ""
    if not origin:
        on_done(None)
        return

    def _try_candidates(candidates: List[str], index: int) -> None:
        if index >= len(candidates) or is_cancelled():
            on_done(None)
            return
        collect_sitemap_entries(
            fetcher=fetcher,
            url=candidates[index],
            on_done=lambda entries: (
                on_done((candidates[index], entries))
                if entries
                else _try_candidates(candidates, index + 1)
            ),
            is_cancelled=is_cancelled,
        )

    def _with_robots(body: Optional[str]) -> None:
        candidates = [
            m.group(1).strip()
            for m in _ROBOTS_SITEMAP_RE.finditer(str(body or ""))
        ]
        if not candidates:
            candidates = [f"{origin}/sitemap.xml", f"{origin}/sitemap_index.xml"]
        _try_candidates(candidates, 0)

    fetcher.fetch(
        f"{origin}/robots.txt",
        on_success=_with_robots,
        on_failure=lambda _err: _with_robots(None),
    )
