#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""URL policies for everything this app fetches, stores or opens.

Three separate policies, because "safe" means three different things
here and collapsing them breaks either security or interoperability:

- MEDIA (:func:`is_safe_media_url`): anything downloaded into the blob
  cache, written into a document, published, or persisted. https only,
  plus http on loopback so a local dev Blossom server still works. A
  server response naming ``file:///etc/passwd`` reads a local file into
  the cache, so this gate runs at every parse point.
- EXTERNAL (:func:`is_safe_external_url`): the gate before handing a URL
  to the desktop's browser. Its job is refusing ``file:``, ``data:``,
  ``javascript:``, ``smb:`` and UNC targets, not enforcing TLS: a PDF or
  a release page may legitimately link plain http.
- MIRROR SOURCE (:func:`is_safe_mirror_source`): third-party image URLs
  handed to a mirror server, and avatar URLs. Plain http is common in
  the wild for both, so the media policy would break real content; what
  matters is refusing non-http schemes, userinfo, and IP literals that
  point back into the local network.

Pure stdlib on purpose: this module is imported by the network layer,
the exporters and the importer alike.
"""

from __future__ import annotations

import ipaddress
from typing import Optional
from urllib.parse import urlsplit


class UnsafeUrlError(ValueError):
    """Raised by :func:`require_safe_media_url` for a refused URL."""


def _split(url: str):
    """Parse ``url``, or return None when it is unusable as a URL."""
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        parts = urlsplit(url.strip())
        parts.port  # an invalid port raises here, not at split time
    except (ValueError, AttributeError):
        return None
    return parts


def host_of(url: str) -> Optional[str]:
    """Lowercased hostname, without IPv6 brackets. None when absent."""
    parts = _split(url)
    if parts is None:
        return None
    host = (parts.hostname or "").lower()
    return host or None


def origin_of(url: str) -> Optional[str]:
    """Return ``scheme://host[:port]`` with the host lowercased.

    An IPv6 host is re-bracketed: ``urlsplit().hostname`` strips the
    brackets, and reassembling without them yields ``http://::1:3000``,
    which no client can parse back.
    """
    parts = _split(url)
    if parts is None:
        return None
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    if not scheme or not host:
        return None
    if ":" in host:
        host = f"[{host}]"
    port = f":{parts.port}" if parts.port else ""
    return f"{scheme}://{host}{port}"


def same_origin(a: str, b: str) -> bool:
    """True when both URLs share scheme, host (case-insensitive) and port."""
    origin_a = origin_of(a)
    origin_b = origin_of(b)
    return origin_a is not None and origin_a == origin_b


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def is_safe_media_url(url: str, *, allowed_origin: Optional[str] = None) -> bool:
    """Media policy: https anywhere, http only on loopback.

    ``allowed_origin`` additionally pins the URL to one server's origin,
    which is how a blob descriptor from server A is stopped from naming
    server B.
    """
    parts = _split(url)
    if parts is None:
        return False
    if "@" in parts.netloc:
        return False
    host = (parts.hostname or "").lower()
    if not host:
        return False
    scheme = (parts.scheme or "").lower()
    if scheme == "http":
        if not _is_loopback(host):
            return False
    elif scheme != "https":
        return False
    if allowed_origin is not None and not same_origin(url, allowed_origin):
        return False
    return True


def require_safe_media_url(url: str, *, allowed_origin: Optional[str] = None) -> str:
    """Return ``url`` when the media policy accepts it, else raise."""
    if not is_safe_media_url(url, allowed_origin=allowed_origin):
        raise UnsafeUrlError(f"URL was not allowed: {url!r}")
    return url


def is_safe_external_url(url: str) -> bool:
    """Gate for handing a URL to the desktop browser."""
    parts = _split(url)
    if parts is None:
        return False
    if "@" in parts.netloc:
        return False
    if (parts.scheme or "").lower() not in ("http", "https"):
        return False
    return bool((parts.hostname or "").strip())


def is_safe_mirror_source(url: str) -> bool:
    """Gate for third-party image URLs (mirror sources, avatars).

    Named hosts pass: they cannot be resolved without a network call and
    the fetching server owns that risk. An IP literal must be global, so
    ``169.254.169.254``, ``10.0.0.1`` and link-local IPv6 are refused.
    """
    parts = _split(url)
    if parts is None:
        return False
    if "@" in parts.netloc:
        return False
    if (parts.scheme or "").lower() not in ("http", "https"):
        return False
    host = (parts.hostname or "").lower()
    if not host:
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return True
