# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ghost export parsing.

Unlike Medium/Substack (ZIPs), a Ghost export is a single JSON file
downloaded from Settings -> Import/Export. Structure (Ghost developer
docs + real exports):

    { "db": [ { "meta": {...}, "data": { "posts": [ {
        "title", "slug", "html", "status", "published_at",
        "feature_image", "custom_excerpt", ... } ] } } ] }

``status`` is ``published`` / ``draft`` / ``scheduled``; ``published_at``
is an ISO string (older exports use epoch ms). The rendered body lives
in ``html``; posts carrying only ``mobiledoc``/``lexical`` and no
``html`` are skipped (converting those formats is a separate, heavy
concern). Only published posts are kept. Never throws.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from ...rss.parser import FeedItem


def is_ghost_export(text: Optional[str]) -> bool:
    """Cheap "is this a Ghost export JSON?" test."""
    if not isinstance(text, str):
        return False
    return bool(
        re.search(r'"db"\s*:\s*\[', text[:4000])
        and re.search(r'"posts"\s*:', text[:200000])
    )


def _to_unix(value) -> Optional[int]:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        # Older exports use epoch milliseconds.
        seconds = float(value) / 1000 if value > 10**12 else float(value)
        return int(seconds)
    raw = str(value).strip()
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    except ValueError:
        return None


@dataclass(frozen=True)
class GhostExport:
    title: str
    items: Tuple[FeedItem, ...]


def parse_ghost_export(text: str) -> GhostExport:
    """Published, html-bodied posts as ``FeedItem``s. Never throws."""
    try:
        payload = json.loads(text or "")
    except (ValueError, TypeError):
        return GhostExport(title="", items=())

    db = payload.get("db") if isinstance(payload, dict) else None
    root = db[0] if isinstance(db, list) and db else payload
    data = root.get("data") if isinstance(root, dict) else None
    posts = data.get("posts") if isinstance(data, dict) else None
    if not isinstance(posts, list):
        return GhostExport(title="", items=())

    items: List[FeedItem] = []
    seen: set = set()
    for post in posts:
        if not isinstance(post, dict):
            continue
        if post.get("status") and post.get("status") != "published":
            continue
        content_html = str(post.get("html") or "").strip()
        if not content_html:
            continue  # mobiledoc/lexical-only posts need a converter we don't have
        guid = "ghost-" + str(
            post.get("id") or post.get("uuid") or post.get("slug")
            or post.get("title") or len(items)
        )
        if guid in seen:
            continue
        seen.add(guid)
        items.append(FeedItem(
            guid=guid,
            title=str(post.get("title") or "Untitled").strip(),
            link=None,
            summary=str(post.get("custom_excerpt") or "").strip() or None,
            content_html=content_html,
            published_at=_to_unix(post.get("published_at")),
            categories=(),
            image=str(post.get("feature_image") or "") or None,
            author=None,
        ))
    return GhostExport(title="Ghost export", items=tuple(items))
