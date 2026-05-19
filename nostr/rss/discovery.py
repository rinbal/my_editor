"""Make feed URLs forgiving.

Most people paste the URL of an article, a homepage, or a half-typed
domain. Three small helpers turn that into something the importer can
actually fetch:

- :func:`normalize_user_url` cleans whitespace and adds a sensible
  scheme so ``example.com`` becomes ``https://example.com``.
- :func:`looks_like_html` cheaply decides whether a response body is
  HTML rather than a feed, so the importer can switch into discovery
  mode without parsing the whole document twice.
- :func:`extract_feeds_from_html` reads ``<link rel="alternate">`` tags
  from the page's ``<head>`` (the canonical RSS / Atom / JSON Feed
  pointer that every modern CMS emits) and resolves relative URLs
  against the page URL.
- :func:`candidate_root_feed` returns the single most likely "I have no
  link-rel hints, but I'm probably a WordPress site" fallback URL.
  Trying *one* well-known path is a clear UX win; probing eight in
  sequence is hostile to the server and slow for the user, so we keep
  the full list in :data:`COMMON_FEED_PATHS` for callers that want to
  do more.

All functions in this module are pure. Network I/O is the importer's
responsibility.
"""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from typing import List, Optional
from urllib.parse import urljoin, urlparse


# Feed-shaped MIME types we accept on ``<link rel="alternate" type="...">``.
# Anything else (image/*, text/css, etc.) is ignored even if rel=alternate.
_FEED_MIME_TYPES = frozenset({
    "application/rss+xml",
    "application/atom+xml",
    "application/feed+json",
    "application/json",     # some JSON Feed sources mis-label as plain JSON
    "application/xml",
    "text/xml",
})


# Common feed paths to probe when no ``<link rel="alternate">`` is found,
# ordered by how often each one is the answer in the wild. The importer
# only tries the first by default. Phase-2 work that wants to probe all
# eight can iterate this constant.
COMMON_FEED_PATHS = (
    "/feed/",       # WordPress, default install
    "/feed",        # WordPress, no-trailing-slash variant
    "/rss",         # generic
    "/rss.xml",     # static site generators
    "/atom.xml",    # Jekyll, Hugo Atom theme
    "/feed.xml",    # Hugo, Eleventy
    "/index.xml",   # Hugo root feed
    "/feed.json",   # JSON Feed
)


# Hostnames that should default to ``http`` when the user omits the
# scheme. Anywhere else, ``https`` is the safer default in 2026.
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})


@dataclass(frozen=True)
class FeedHint:
    """One feed URL discovered on an HTML page."""

    url: str
    title: Optional[str] = None
    mime_type: Optional[str] = None


# --------------------------------------------------------------------------- #
# URL hygiene                                                                 #
# --------------------------------------------------------------------------- #

def normalize_user_url(value: str) -> str:
    """Make a user-pasted URL more likely to parse.

    Trims whitespace and adds a scheme if missing. ``localhost`` and
    loopback hosts default to ``http`` so dev-mode pastes work; every
    other host defaults to ``https``.
    """
    cleaned = (value or "").strip()
    if not cleaned:
        return ""
    if "://" in cleaned[:10].lower():
        return cleaned
    # Strip a leading ``//`` (protocol-relative paste) before adding scheme.
    if cleaned.startswith("//"):
        cleaned = cleaned[2:]
    host_segment = cleaned.split("/", 1)[0]
    host = host_segment.split(":", 1)[0].lower()
    scheme = "http" if host in _LOCAL_HOSTS else "https"
    return f"{scheme}://{cleaned}"


# --------------------------------------------------------------------------- #
# HTML detection                                                              #
# --------------------------------------------------------------------------- #

def looks_like_html(body: str) -> bool:
    """Cheap heuristic for "I got a webpage, not a feed".

    Only inspects the first 2 KiB. Returns ``True`` on the obvious HTML
    markers; deliberately lenient so the importer can decide to *try*
    discovery and falls back to the friendly error if discovery turns
    up nothing.
    """
    if not body:
        return False
    head = body.lstrip()[:2048].lower()
    return (
        "<!doctype html" in head
        or "<html" in head
        or "<head" in head
        or "<body" in head
    )


# --------------------------------------------------------------------------- #
# <link rel="alternate"> extraction                                           #
# --------------------------------------------------------------------------- #

class _LinkRelExtractor(HTMLParser):
    """Pulls feed hints from ``<link rel="alternate">`` tags in ``<head>``.

    Conservative on purpose:
      - Only looks at tags before ``</head>`` (or before ``<body>``).
      - Only accepts ``rel`` values that contain the literal token
        ``alternate``.
      - Requires an explicit feed-shaped ``type`` (rss+xml, atom+xml,
        feed+json, or xml). Alternates without an explicit type are
        almost always ``hreflang`` i18n pointers; the importer's
        ``/feed/`` fallback handles truly typeless feeds.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hints: List[FeedHint] = []
        self._in_head: bool = False
        self._head_closed: bool = False

    def handle_starttag(self, tag: str, attrs) -> None:
        if self._head_closed:
            return
        tag_lower = tag.lower()
        if tag_lower == "head":
            self._in_head = True
            return
        if tag_lower == "body":
            # No </head> yet, but we're already inside the body. Stop.
            self._head_closed = True
            return
        if tag_lower != "link" or not self._in_head:
            return

        attr_map = {k.lower(): (v or "") for k, v in attrs}
        rel_tokens = attr_map.get("rel", "").lower().split()
        if "alternate" not in rel_tokens:
            return

        href = attr_map.get("href", "").strip()
        if not href:
            return

        # Feed autodiscovery (per the RSS / Atom convention) requires an
        # explicit feed-shaped MIME on ``type``. Untyped alternates are
        # almost always ``hreflang`` i18n pointers, not feeds.
        mime = attr_map.get("type", "").lower().strip()
        if mime not in _FEED_MIME_TYPES:
            return

        title = attr_map.get("title") or None
        self.hints.append(FeedHint(
            url=href,
            title=title,
            mime_type=mime,
        ))

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "head":
            self._head_closed = True


def extract_feeds_from_html(
    html: str,
    *,
    base_url: str,
) -> List[FeedHint]:
    """Return feed hints declared in an HTML page's ``<head>``.

    Relative ``href`` values are resolved against ``base_url``. Order is
    preserved so callers can pick the first hint as a sensible default.
    """
    if not html:
        return []
    parser = _LinkRelExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 — html.parser raises on truly broken input
        # Return whatever we managed to collect before the failure.
        pass
    return [
        FeedHint(
            url=urljoin(base_url, hint.url),
            title=hint.title,
            mime_type=hint.mime_type,
        )
        for hint in parser.hints
    ]


# --------------------------------------------------------------------------- #
# Common-path fallback                                                        #
# --------------------------------------------------------------------------- #

def candidate_root_feed(page_url: str) -> Optional[str]:
    """Most likely feed URL when no ``<link rel="alternate">`` is found.

    Returns the origin (scheme + host + port) joined with ``/feed/``,
    which covers the vast majority of WordPress, Substack, and Ghost
    deployments. Returns ``None`` if ``page_url`` can't be parsed.
    """
    parsed = urlparse(page_url)
    if not parsed.scheme or not parsed.hostname:
        return None
    port = f":{parsed.port}" if parsed.port else ""
    origin = f"{parsed.scheme}://{parsed.hostname}{port}"
    return origin + COMMON_FEED_PATHS[0]
