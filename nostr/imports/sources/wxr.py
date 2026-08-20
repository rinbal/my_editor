# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""WordPress WXR (eXtended RSS) export parsing.

WXR is the ``.xml`` file WordPress produces at Tools -> Export: RSS 2.0
plus a ``wp:`` namespace carrying every post, page, draft, attachment,
and nav-menu item of a site. It is NOT a live feed (a live ``/feed/``
never carries the ``wp:`` namespace), so detection can never misfire on
an ordinary feed; it arrives as a pasted or uploaded file.

Only real content the user meant to publish is kept: ``wp:post_type``
of ``post`` (and, by default, ``page``) with ``wp:status`` of
``publish``. Attachments, nav menus, drafts, and scheduled posts are
skipped.

Parsed with a CDATA-masking regex scan: ``content:encoded`` bodies are
CDATA-wrapped HTML that routinely contains ``<tag>`` markup and can
even contain a literal ``</item>``, which would tear a naive structural
scan. Masking every CDATA section behind a placeholder first makes the
structure scan safe; fields are unmasked on extraction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable, List, Optional, Tuple

from ...rss.parser import FeedItem


_WP_NS_RE = re.compile(
    r"xmlns:wp\s*=\s*[\"']https?://wordpress\.org/export/", re.IGNORECASE)
_WXR_VERSION_RE = re.compile(r"<wp:wxr_version[\s>]", re.IGNORECASE)

_ITEM_RE = re.compile(r"<item\b[^>]*>([\s\S]*?)</item>", re.IGNORECASE)
_CATEGORY_RE = re.compile(
    r"<category\b[^>]*>([\s\S]*?)</category>", re.IGNORECASE)
_TITLE_RE = re.compile(r"<title\b[^>]*>([\s\S]*?)</title>", re.IGNORECASE)
_CDATA_RE = re.compile(r"<!\[CDATA\[([\s\S]*?)\]\]>")


def is_wxr(text: Optional[str]) -> bool:
    """Cheap, specific "is this a WordPress export?" test."""
    if not isinstance(text, str):
        return False
    head = text[:4000]
    return bool(_WP_NS_RE.search(head) or _WXR_VERSION_RE.search(head))


def _mask_cdata(text: str) -> Tuple[str, Callable[[str], str]]:
    """Stash CDATA sections behind placeholders; returns (masked, unmask)."""
    blocks: List[str] = []

    def _stash(match: re.Match) -> str:
        blocks.append(match.group(1))
        return f"\x00C{len(blocks) - 1}\x00"

    masked = _CDATA_RE.sub(_stash, str(text))

    def _unmask(value: str) -> str:
        return re.sub(
            r"\x00C(\d+)\x00",
            lambda m: blocks[int(m.group(1))]
            if int(m.group(1)) < len(blocks) else "",
            str(value),
        )

    return masked, _unmask


def _field(body: str, name: str, unmask: Callable[[str], str]) -> str:
    match = re.search(
        rf"<{name}\b[^>]*>([\s\S]*?)</{name}>", body, re.IGNORECASE)
    return unmask(match.group(1)).strip() if match else ""


def _to_unix(gmt: str, pub_date: str) -> Optional[int]:
    """``wp:post_date_gmt`` (UTC, ``YYYY-MM-DD HH:MM:SS``) preferred,
    ``pubDate`` (RFC 822) as fallback. WordPress writes
    ``0000-00-00 00:00:00`` for some rows; those are unusable."""
    if gmt and not gmt.startswith("0000"):
        try:
            parsed = datetime.strptime(gmt, "%Y-%m-%d %H:%M:%S")
            return int(parsed.replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            pass
    if pub_date:
        try:
            parsed = parsedate_to_datetime(pub_date)
            if parsed is not None:
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return int(parsed.timestamp())
        except (TypeError, ValueError):
            pass
    return None


@dataclass(frozen=True)
class WxrExport:
    title: str
    items: Tuple[FeedItem, ...]


def parse_wxr(text: str, *, include_pages: bool = True) -> WxrExport:
    """Parse WXR text into published posts (and pages) as ``FeedItem``s.

    The HTML body stays in ``content_html`` so the pipeline's normal
    HTML-to-Markdown pass handles it. Never throws on malformed input;
    unknown shapes just yield fewer items.
    """
    masked, unmask = _mask_cdata(text or "")

    title_match = _TITLE_RE.search(masked)
    export_title = unmask(title_match.group(1)).strip() if title_match else ""

    items: List[FeedItem] = []
    seen: set = set()
    for match in _ITEM_RE.finditer(masked):
        body = match.group(1)
        if _field(body, "wp:status", unmask) != "publish":
            continue
        post_type = _field(body, "wp:post_type", unmask)
        if post_type != "post" and not (include_pages and post_type == "page"):
            continue

        post_id = _field(body, "wp:post_id", unmask)
        post_name = _field(body, "wp:post_name", unmask)
        link = _field(body, "link", unmask)
        item_title = _field(body, "title", unmask)
        guid = f"wp-{post_id}" if post_id else (post_name or link or item_title)
        if not guid or guid in seen:
            continue
        seen.add(guid)

        categories: List[str] = []
        for cat in _CATEGORY_RE.finditer(body):
            name = unmask(cat.group(1)).strip()
            if name and name not in categories:
                categories.append(name)

        items.append(FeedItem(
            guid=guid,
            title=item_title or post_name or "Untitled",
            link=link or None,
            summary=_field(body, "excerpt:encoded", unmask) or None,
            content_html=_field(body, "content:encoded", unmask),
            published_at=_to_unix(
                _field(body, "wp:post_date_gmt", unmask),
                _field(body, "pubDate", unmask),
            ),
            categories=tuple(categories),
            image=None,
            author=_field(body, "dc:creator", unmask) or None,
        ))
    return WxrExport(title=export_title, items=tuple(items))
