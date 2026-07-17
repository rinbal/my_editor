# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""RSS, Atom, and JSON Feed import for the drafts panel.

Mirrors the TypeScript reference at ``nostr-core/src/rss.ts`` (branch
``rss_feed``), adapted to feed the editor's existing NIP-37 draft pipeline
instead of building a parallel one.

Public surface is re-exported below. Import submodules directly if you
need lower-level access (parsers, normalisers, etc.).
"""

from __future__ import annotations

from .dtag import NoIdentifierError, derive_identifier

__all__ = [
    "NoIdentifierError",
    "derive_identifier",
]
