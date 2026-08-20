# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Universal source importer.

The extensible half of the import feature. ``nostr/rss/`` stays a pure
feed-format library (parsing, d-tag derivation, HTML-to-Markdown
normalisation, URL discovery helpers); this package owns everything
around it:

- ``errors``     stable error codes + friendly user-facing copy
- ``fetch``      the shared Qt network fetcher (size-capped, charset
                 sniffing, bounded redirects)
- ``registry``   the source-resolver registry: ``detect`` + ``resolve``
                 per source kind, everything downstream consumes one
                 normalised ``Feed`` shape
- ``resolvers``  one module per source kind, RSS as the catch-all

The central contract: input -> detect() -> resolver -> normalised Feed
-> preview -> filtering -> import pipeline -> article creation. Adding
a new source kind is one resolver module + one registry entry + tests;
the pipeline and UI never change for it.
"""

from .errors import ERROR_CODES, SourceError, friendly_message
from .registry import (
    ResolveInput,
    ResolveResult,
    can_resolve_source,
    detect_resolver,
    resolve_source,
)

__all__ = [
    "ERROR_CODES",
    "SourceError",
    "friendly_message",
    "ResolveInput",
    "ResolveResult",
    "can_resolve_source",
    "detect_resolver",
    "resolve_source",
]
