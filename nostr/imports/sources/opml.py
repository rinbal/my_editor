# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""OPML subscription-list parsing (bulk feed import).

OPML is the universal "list of feeds" format every RSS reader imports
and exports. Unlike a single feed it yields *subscriptions*, not
articles, so it drives the bulk-subscribe flow rather than the resolver
pipeline.

A feed is any ``<outline>`` element carrying an ``xmlUrl`` (the feed
URL) at any nesting depth; category outlines omit it and only group
children. The display name is ``title`` or ``text``.

Parsed with a tolerant regex scan on purpose: a hand-edited or
vendor-quirky export must never throw. Unknown shapes just yield fewer
feeds, never an exception.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple


_OUTLINE_RE = re.compile(r"<outline\b([^>]*)>", re.IGNORECASE | re.DOTALL)
_ATTR_RE = re.compile(
    r"([A-Za-z_:][\w:.-]*)\s*=\s*\"([^\"]*)\"|([A-Za-z_:][\w:.-]*)\s*=\s*'([^']*)'"
)
_TITLE_RE = re.compile(r"<title\b[^>]*>([^<]*)</title>", re.IGNORECASE)


def _decode_entities(value: str) -> str:
    return (
        str(value)
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#039;", "'")
        .replace("&apos;", "'")
        .replace("&amp;", "&")  # last, so "&amp;lt;" is not double-decoded
    )


@dataclass(frozen=True)
class OpmlFeed:
    xml_url: str
    title: str
    html_url: Optional[str] = None


@dataclass(frozen=True)
class OpmlDocument:
    title: str
    feeds: Tuple[OpmlFeed, ...]


def is_opml(text: Optional[str]) -> bool:
    """Tolerant: the ``<opml>`` root or any xmlUrl-carrying outline."""
    if not isinstance(text, str):
        return False
    head = text[:8000]
    return "<opml" in head.lower() or bool(
        re.search(r"<outline\b[^>]*\bxmlurl\s*=", head, re.IGNORECASE))


def _attrs(tag_body: str) -> dict:
    out = {}
    for match in _ATTR_RE.finditer(tag_body):
        name = (match.group(1) or match.group(3) or "").lower()
        value = match.group(2) if match.group(2) is not None else match.group(4)
        out[name] = _decode_entities(value or "")
    return out


def parse_opml(text: str) -> OpmlDocument:
    """Flattens nested categories, de-duplicates by feed URL, never throws."""
    src = str(text or "")
    feeds: List[OpmlFeed] = []
    seen: set = set()
    for match in _OUTLINE_RE.finditer(src):
        attrs = _attrs(match.group(1))
        xml_url = attrs.get("xmlurl", "").strip()
        if not xml_url:
            continue  # a category / grouping node
        key = xml_url.lower()
        if key in seen:
            continue
        seen.add(key)
        feeds.append(OpmlFeed(
            xml_url=xml_url,
            title=(attrs.get("title") or attrs.get("text") or xml_url).strip(),
            html_url=attrs.get("htmlurl", "").strip() or None,
        ))
    title_match = _TITLE_RE.search(src)
    return OpmlDocument(
        title=_decode_entities(title_match.group(1)).strip()
        if title_match else "",
        feeds=tuple(feeds),
    )
