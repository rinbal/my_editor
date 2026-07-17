# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""RSS 2.0, Atom, and JSON Feed parsing.

Auto-detects format from the input string and returns a normalised
``Feed`` containing ``FeedItem`` records. Field extraction matches the
TypeScript reference at ``nostr-core/src/rss.ts`` so a given feed yields
the same drafts on either side.

The dataclasses are deliberately small. Anything Nostr-specific (d-tag
derivation, NIP-23 mapping, source-link footer) lives in sibling modules.
"""

from __future__ import annotations

import calendar
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, List, Mapping, Optional, Sequence, Tuple

import feedparser


FeedFormat = str  # one of: "rss", "atom", "jsonfeed"


@dataclass(frozen=True)
class FeedItem:
    """One normalised entry from a feed of any supported format."""

    guid: str
    title: str
    link: Optional[str]
    summary: Optional[str]
    content_html: str
    published_at: Optional[int]
    categories: Tuple[str, ...]
    image: Optional[str]
    author: Optional[str]


@dataclass(frozen=True)
class Feed:
    """A parsed feed envelope and its items."""

    format: FeedFormat
    title: str
    link: Optional[str]
    description: Optional[str]
    items: Tuple[FeedItem, ...] = field(default_factory=tuple)


class RssError(Exception):
    """Raised when a feed cannot be parsed or recognised."""

    def __init__(self, message: str, code: str = "RSS_ERROR") -> None:
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- #
# Shared helpers                                                               #
# --------------------------------------------------------------------------- #

_IMG_SRC_RE = re.compile(r"""<img[^>]+src=["']([^"']+)["']""", re.IGNORECASE)


def _first_image_from_html(html: str) -> Optional[str]:
    """Return the ``src`` of the first ``<img>`` tag in ``html``, if any."""
    if not html:
        return None
    match = _IMG_SRC_RE.search(html)
    return match.group(1) if match else None


def _struct_time_to_unix(st: Optional[time.struct_time]) -> Optional[int]:
    """Convert a feedparser ``struct_time`` (always UTC) to unix seconds."""
    if st is None:
        return None
    try:
        return int(calendar.timegm(st))
    except (TypeError, ValueError, OverflowError):
        return None


def _parse_date_string(value: Optional[str]) -> Optional[int]:
    """Best-effort RFC-822 / ISO-8601 to unix seconds. JSON Feed only."""
    if not value:
        return None
    s = value.strip()
    if not s:
        return None
    # ISO 8601 with trailing Z is the JSON Feed canonical form.
    try:
        if s.endswith("Z"):
            dt = datetime.fromisoformat(s[:-1] + "+00:00")
        else:
            dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(s)
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except (TypeError, ValueError):
        return None


def _clean_str(value: Any) -> str:
    """Coerce to a stripped string, never raising."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


# --------------------------------------------------------------------------- #
# RSS 2.0                                                                      #
# --------------------------------------------------------------------------- #

def _rss_image(entry: Mapping[str, Any], content_html: str) -> Optional[str]:
    """Image priority: image enclosure, media:content, media:thumbnail, body."""
    for enc in entry.get("enclosures") or ():
        ctype = (enc.get("type") or "").lower()
        href = enc.get("href") or enc.get("url")
        if href and ctype.startswith("image/"):
            return href

    media_content = entry.get("media_content") or ()
    for mc in media_content:
        url = mc.get("url") if isinstance(mc, Mapping) else None
        if url:
            return url

    media_thumb = entry.get("media_thumbnail") or ()
    for mt in media_thumb:
        url = mt.get("url") if isinstance(mt, Mapping) else None
        if url:
            return url

    return _first_image_from_html(content_html)


def _entry_categories(entry: Mapping[str, Any]) -> Tuple[str, ...]:
    out: List[str] = []
    for tag in entry.get("tags") or ():
        if isinstance(tag, Mapping):
            term = _clean_str(tag.get("term"))
            if term:
                out.append(term)
    return tuple(out)


def _content_html_from_entry(entry: Mapping[str, Any]) -> str:
    """feedparser exposes ``content:encoded`` / ``<content>`` via ``content``."""
    contents = entry.get("content") or ()
    for c in contents:
        if isinstance(c, Mapping):
            value = c.get("value")
            if value:
                return value
    return ""


def _parse_rss(parsed: Any) -> Feed:
    channel = parsed.feed or {}
    items: List[FeedItem] = []

    for entry in parsed.entries or ():
        content_html = _content_html_from_entry(entry)
        summary = _clean_str(entry.get("summary"))
        # ``description`` and ``content:encoded`` coincide when the feed only
        # ships a description. Don't duplicate it into the summary slot.
        if not content_html:
            content_html = summary
            summary_for_item: Optional[str] = None
        else:
            summary_for_item = summary if summary and summary != content_html else None

        link = _clean_str(entry.get("link")) or None
        title = _clean_str(entry.get("title"))
        guid = _clean_str(entry.get("id")) or link or title

        items.append(
            FeedItem(
                guid=guid,
                title=title,
                link=link,
                summary=summary_for_item,
                content_html=content_html,
                published_at=_struct_time_to_unix(entry.get("published_parsed")),
                categories=_entry_categories(entry),
                image=_rss_image(entry, content_html),
                author=_clean_str(entry.get("author")) or None,
            )
        )

    return Feed(
        format="rss",
        title=_clean_str(channel.get("title")),
        link=_clean_str(channel.get("link")) or None,
        description=_clean_str(channel.get("description")) or None,
        items=tuple(items),
    )


# --------------------------------------------------------------------------- #
# Atom                                                                         #
# --------------------------------------------------------------------------- #

def _parse_atom(parsed: Any) -> Feed:
    head = parsed.feed or {}
    items: List[FeedItem] = []

    for entry in parsed.entries or ():
        content_html = _content_html_from_entry(entry)
        summary = _clean_str(entry.get("summary"))
        if not content_html:
            content_html = summary
            summary_for_item: Optional[str] = None
        else:
            summary_for_item = summary if summary and summary != content_html else None

        link = _clean_str(entry.get("link")) or None
        title = _clean_str(entry.get("title"))
        guid = _clean_str(entry.get("id")) or link or title

        published = entry.get("published_parsed") or entry.get("updated_parsed")

        author = _clean_str(entry.get("author")) or None
        if not author:
            authors = entry.get("authors") or ()
            for a in authors:
                if isinstance(a, Mapping):
                    name = _clean_str(a.get("name"))
                    if name:
                        author = name
                        break

        items.append(
            FeedItem(
                guid=guid,
                title=title,
                link=link,
                summary=summary_for_item,
                content_html=content_html,
                published_at=_struct_time_to_unix(published),
                categories=_entry_categories(entry),
                image=_first_image_from_html(content_html),
                author=author,
            )
        )

    return Feed(
        format="atom",
        title=_clean_str(head.get("title")),
        link=_clean_str(head.get("link")) or None,
        description=_clean_str(head.get("subtitle")) or None,
        items=tuple(items),
    )


# --------------------------------------------------------------------------- #
# JSON Feed                                                                    #
# --------------------------------------------------------------------------- #

def _json_item_author(it: Mapping[str, Any]) -> Optional[str]:
    author = it.get("author")
    if isinstance(author, Mapping):
        name = _clean_str(author.get("name"))
        if name:
            return name
    authors = it.get("authors")
    if isinstance(authors, Sequence) and not isinstance(authors, (str, bytes)):
        for a in authors:
            if isinstance(a, Mapping):
                name = _clean_str(a.get("name"))
                if name:
                    return name
    return None


def _parse_json_feed(payload: Any) -> Feed:
    if not isinstance(payload, Mapping):
        raise RssError("Invalid JSON Feed: top-level value is not an object",
                       "INVALID_JSONFEED")
    raw_items = payload.get("items")
    if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes)):
        raise RssError("Invalid JSON Feed: missing items array",
                       "INVALID_JSONFEED")

    items: List[FeedItem] = []
    for it in raw_items:
        if not isinstance(it, Mapping):
            continue
        content_html = (
            _clean_str(it.get("content_html"))
            or _clean_str(it.get("content_text"))
            or _clean_str(it.get("summary"))
        )
        title = _clean_str(it.get("title"))
        link = _clean_str(it.get("url")) or None
        guid = _clean_str(it.get("id")) or link or title

        tags = it.get("tags")
        categories: Tuple[str, ...] = ()
        if isinstance(tags, Sequence) and not isinstance(tags, (str, bytes)):
            categories = tuple(_clean_str(t) for t in tags if _clean_str(t))

        image = (
            _clean_str(it.get("image"))
            or _clean_str(it.get("banner_image"))
            or _first_image_from_html(content_html)
            or None
        )

        items.append(
            FeedItem(
                guid=guid,
                title=title,
                link=link,
                summary=_clean_str(it.get("summary")) or None,
                content_html=content_html,
                published_at=_parse_date_string(it.get("date_published")),
                categories=categories,
                image=image if isinstance(image, str) else None,
                author=_json_item_author(it),
            )
        )

    return Feed(
        format="jsonfeed",
        title=_clean_str(payload.get("title")),
        link=_clean_str(payload.get("home_page_url")) or None,
        description=_clean_str(payload.get("description")) or None,
        items=tuple(items),
    )


# --------------------------------------------------------------------------- #
# Public entry point                                                           #
# --------------------------------------------------------------------------- #

def parse_feed(text: str) -> Feed:
    """Auto-detect and parse an RSS, Atom, or JSON Feed payload.

    Raises ``RssError`` if the input doesn't look like any supported format.
    """
    if not text:
        raise RssError("Empty feed payload", "EMPTY_FEED")

    stripped = text.lstrip()
    if stripped.startswith("{"):
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise RssError(f"Invalid JSON Feed: {exc}", "INVALID_JSONFEED") from exc
        return _parse_json_feed(payload)

    parsed = feedparser.parse(text)
    version = (getattr(parsed, "version", "") or "").lower()
    if version.startswith("atom"):
        return _parse_atom(parsed)
    if version.startswith("rss") or version.startswith("cdf"):
        return _parse_rss(parsed)

    # feedparser's auto-detection failed. If there are entries we can still
    # try treating it as RSS, otherwise give up with a clear message.
    if parsed.entries:
        return _parse_rss(parsed)
    raise RssError("Unrecognised feed format", "UNKNOWN_FORMAT")
