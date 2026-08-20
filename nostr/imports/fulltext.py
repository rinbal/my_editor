# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Full-text recovery for summary-only feeds.

Many CMS feeds (Webflow, Squarespace, some Ghost / WordPress configs)
export only the post *summary*: there is no ``content:encoded`` to read,
the body simply isn't in the feed. The only way to recover the article
text is to fetch the post's own page and lift the main content out of
the surrounding nav / footer / ad chrome.

This module is the *pure* half of that: HTML in, clean article HTML out,
via ``readability-lxml`` (the same Readability family Firefox Reader
View uses). The network fetch and the adopt-or-keep decision stay in the
pipeline.

Defensive by design: any malformed input or extraction failure returns
``None`` so the caller quietly keeps the feed summary.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

from readability import Document

_log = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class ReadableContent:
    """Main-article extraction result."""

    html: str
    text_length: int
    title: str


def extract_readable_content(
    html: str,
    *,
    url: Optional[str] = None,
) -> Optional[ReadableContent]:
    """Extract the main article content from a full HTML page.

    ``url`` is the page's own address; passing it lets the extractor
    absolutise relative ``src``/``href`` values so images and links
    survive the import instead of 404ing against nothing.
    """
    if not html or not isinstance(html, str):
        return None
    try:
        doc = Document(html, url=url) if url else Document(html)
        summary_html = (doc.summary(html_partial=True) or "").strip()
        title = (doc.short_title() or "").strip()
    except Exception as exc:  # noqa: BLE001, any parse failure means "no article"
        _log.debug("readability extraction failed: %s", exc)
        return None
    if not summary_html:
        return None
    text = _TAG_RE.sub(" ", summary_html)
    text_length = len(" ".join(text.split()))
    if text_length == 0:
        return None
    return ReadableContent(
        html=summary_html,
        text_length=text_length,
        title=title,
    )
