# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Preview-side pure helpers: scope filtering + per-item display stats.

Everything here is pure so the panel can call it per selection change
without side effects, and tests need no Qt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Sequence

from ..rss.parser import FeedItem
from .constants import DEFAULT_LIMIT, MAX_LIMIT


# --------------------------------------------------------------------------- #
# Scope presets                                                               #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ScopePreset:
    """One chip above the preview list."""

    key: str
    label: str
    limit: int
    recommended: bool = False
    # Reserved for the subscriptions phase: scope to items newer than
    # the feed's lastFetchedAt instead of a fixed count.
    since_last_visit: bool = False


SCOPE_PRESETS: tuple = (
    ScopePreset("newest10", "Newest 10", 10),
    ScopePreset("newest25", "Newest 25", 25, recommended=True),
    ScopePreset("newest50", "Newest 50", 50),
    ScopePreset("all", f"All (up to {MAX_LIMIT})", MAX_LIMIT),
    # Only offered for subscribed sources with a recorded last import;
    # the panel hides the chip otherwise.
    ScopePreset("sinceVisit", "New since last import", MAX_LIMIT,
                since_last_visit=True),
)


def default_scope_key(total_items: int) -> str:
    """The chip pre-selected when a preview opens. Small feeds get the
    small chip so the UI never promises more than exists."""
    if total_items <= 10:
        return "newest10"
    return "newest25"


def filter_items(
    items: Sequence[FeedItem],
    *,
    since: Optional[int] = None,
    limit: int = DEFAULT_LIMIT,
) -> List[FeedItem]:
    """Filter by ``since`` (unix seconds), sort newest first, cap.

    The sort is stable and undated items key as 0, so they keep their
    feed order and land after every dated item; a limit-only scope never
    drops them outright. ``limit`` is clamped to [1, MAX_LIMIT].
    """
    cap = max(1, min(int(limit), MAX_LIMIT))
    kept = [
        it for it in items
        if since is None or (it.published_at or 0) >= int(since)
    ]
    kept.sort(key=lambda it: it.published_at or 0, reverse=True)
    return kept[:cap]


# --------------------------------------------------------------------------- #
# Item display stats                                                          #
# --------------------------------------------------------------------------- #

_TAG_RE = re.compile(r"<[^>]+>")
_WORD_RE = re.compile(r"\S+")
_AVG_WPM = 225  # standard prose reading speed


def count_words(html: str) -> int:
    """Ballpark word count of an HTML fragment (tags stripped). Crude on
    purpose; this feeds an "N min read" badge, not typography."""
    if not html:
        return 0
    text = _TAG_RE.sub(" ", str(html))
    return len(_WORD_RE.findall(text))


def read_minutes(html: str) -> int:
    """Estimated read time in minutes; floor 1 for any non-empty body."""
    words = count_words(html)
    if words == 0:
        return 0
    return max(1, round(words / _AVG_WPM))
