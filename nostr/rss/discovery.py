# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
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

import re
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


# Hard cap on anything we treat as a feed URL. Also the backstop that
# stops a stray full-XML paste (kilobytes long) from ever being handled
# as a "URL". Mirrors the reference importer's frontend/backend guard.
MAX_FEED_URL_LENGTH: int = 2048

# Angle brackets and internal whitespace never appear in a real URL but
# are the signature of an XML/HTML paste, the decisive cheap check.
_URL_REJECT_RE = re.compile(r"[<>\s]")


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
    if host_segment.startswith("["):
        # Bracketed IPv6 literal, e.g. ``[::1]:8080``. The host is the
        # bracket contents; splitting on ``:`` would slice mid-address.
        end = host_segment.find("]")
        host = host_segment[1:end].lower() if end > 0 else ""
    else:
        host = host_segment.split(":", 1)[0].lower()
    scheme = "http" if host in _LOCAL_HOSTS else "https"
    return f"{scheme}://{cleaned}"


def is_likely_feed_url(value: str) -> bool:
    """Is this string something we can sensibly fetch as a feed source?

    The single validation authority for "does this look like a URL at
    all", used by the panel before an import may start. It is what stops
    a pasted XML document (or any multi-line blob) from being treated as
    a URL. Deliberately strict: a false negative just asks the user for
    a cleaner URL, while a false positive would hand the fetcher a
    kilobyte-long "URL".
    """
    raw = (value or "").strip()
    if not raw or len(raw) > MAX_FEED_URL_LENGTH:
        return False
    if _URL_REJECT_RE.search(raw):
        return False
    try:
        parsed = urlparse(normalize_user_url(raw))
        host = (parsed.hostname or "").lower()
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    if not host:
        return False
    # ``urlparse`` strips the brackets from an IPv6 literal, so a colon
    # in the hostname can only mean IPv6, fetchable as-is. Otherwise
    # require a dotted host (domain or IPv4) or a known loopback name;
    # a bare word like ``feed`` is not fetchable on its own.
    if ":" in host:
        return True
    return "." in host or host in _LOCAL_HOSTS


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
    except Exception:  # noqa: BLE001, html.parser raises on truly broken input
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

def candidate_feed_urls(page_url: str) -> List[str]:
    """Ordered best-guess feed URLs when a page has no autodiscovery tag.

    Probes the :data:`COMMON_FEED_PATHS` palette under three bases, most
    specific first, so the feed closest to what the user pasted wins:

      1. the pasted path itself:  ``/blog``         -> ``/blog/feed/`` ...
      2. its first path segment:  ``/blog/article`` -> ``/blog/feed/`` ...
      3. the bare origin:         ``/``             -> ``/feed/`` ...

    Bases are de-duplicated (a bare ``/blog`` collapses 1 and 2), every
    candidate is absolute and appears once. Returns ``[]`` when
    ``page_url`` can't be parsed. Callers iterate lazily and stop at the
    first candidate that parses, so the list length costs nothing up
    front.
    """
    try:
        parsed = urlparse(page_url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return []
    if not parsed.scheme or not hostname:
        return []
    origin = f"{parsed.scheme}://{hostname}{f':{port}' if port else ''}"

    segments = [s for s in parsed.path.split("/") if s]
    base_paths: List[str] = []
    if segments:
        base_paths.append("/" + "/".join(segments))
    if len(segments) > 1:
        base_paths.append("/" + segments[0])
    base_paths.append("")  # the origin

    seen_bases: set[str] = set()
    seen_urls: set[str] = set()
    urls: List[str] = []
    for base in base_paths:
        if base in seen_bases:
            continue
        seen_bases.add(base)
        for leaf in COMMON_FEED_PATHS:
            url = f"{origin}{base}{leaf}"
            if url in seen_urls:
                continue
            seen_urls.add(url)
            urls.append(url)
    return urls


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
