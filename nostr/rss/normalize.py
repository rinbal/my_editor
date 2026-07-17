# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Normalise a ``FeedItem`` into kwargs for ``nostr.publisher.build_article``.

Two layers:

- :func:`html_to_markdown` runs ``markdownify`` with TurndownService-aligned
  options so the output matches the TypeScript reference byte-for-byte
  whenever the input HTML round-trips cleanly.
- :func:`item_to_article` produces a small :class:`ArticleTemplate` value
  the importer feeds to ``build_article`` (which then becomes the inner
  event for a NIP-37 draft wrap).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Tuple

from markdownify import ATX, markdownify as _markdownify

from .dtag import derive_identifier
from .parser import FeedItem


_SOURCE_FOOTER_SEPARATOR = "\n\n---\n\n"


@dataclass(frozen=True)
class ArticleTemplate:
    """Inputs for ``build_article``. The signing pubkey is supplied by
    the caller; everything else comes from the feed item."""

    slug: str
    content: str
    title: str
    summary: str
    image: str
    published_at: Optional[int]
    hashtags: Tuple[str, ...]


def html_to_markdown(html: str) -> str:
    """HTML to Markdown with TS-reference-aligned options."""
    if not html:
        return ""
    return _markdownify(
        html,
        heading_style=ATX,
        bullets="-",
        strong_em_symbol="_",
    )


def _merge_hashtags(
    item_categories: Iterable[str],
    extra: Iterable[str],
) -> Tuple[str, ...]:
    """Lowercase, strip, dedupe (preserving first-seen order)."""
    seen: list[str] = []
    for raw in (*item_categories, *extra):
        if not raw:
            continue
        tag = raw.strip().lstrip("#").lower()
        if tag and tag not in seen:
            seen.append(tag)
    return tuple(seen)


def source_link_footer(link: str) -> str:
    """Footer block appended to a feed item's body when ``link`` is set.

    Public so other callers (notably the long-form resolver) can compute
    its length to decide whether the pre-footer body is "thin", without
    re-implementing the format.
    """
    return f"{_SOURCE_FOOTER_SEPARATOR}*Originally published at [{link}]({link})*"


def item_to_article(
    item: FeedItem,
    *,
    hashtags: Iterable[str] = (),
    identifier_prefix: Optional[str] = None,
    append_source_link: bool = True,
) -> ArticleTemplate:
    """Convert a :class:`FeedItem` to an :class:`ArticleTemplate`.

    The d-tag identifier is derived deterministically from
    ``(guid, link, title)`` so re-importing the same feed produces the
    same draft slot on the relay (idempotent replacement).
    """
    markdown = html_to_markdown(item.content_html).strip()
    if append_source_link and item.link:
        markdown = (markdown + source_link_footer(item.link)).strip()

    slug = derive_identifier(
        guid=item.guid,
        link=item.link,
        title=item.title,
        prefix=identifier_prefix,
    )

    return ArticleTemplate(
        slug=slug,
        content=markdown,
        title=item.title or "",
        summary=item.summary or "",
        image=item.image or "",
        published_at=item.published_at,
        hashtags=_merge_hashtags(item.categories, hashtags),
    )
