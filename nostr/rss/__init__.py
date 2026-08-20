# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure feed-format primitives: parsing, normalisation, discovery.

This package is format-level only: RSS/Atom/JSON parsing (``parser``),
HTML-to-Markdown + article templating (``normalize``), deterministic
d-tag derivation (``dtag``), URL/discovery helpers (``discovery``), and
NIP-23 long-form coordinate resolution (``nostr_resolver``).

Everything orchestration-shaped (the resolver registry, fetching, the
import pipeline, subscriptions, the UI) lives in ``nostr.imports``.
"""

from __future__ import annotations

from .dtag import NoIdentifierError, derive_identifier

__all__ = [
    "NoIdentifierError",
    "derive_identifier",
]
