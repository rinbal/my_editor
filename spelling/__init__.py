# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Spell checking with each system's own spell checker.

- ``SpellChecker`` (one per app) reaches the system checker: suggestions,
  Learn Spelling, Ignore Spelling, availability for the Spelling menu.
- ``DocumentSpelling`` (one per document) knows the misspelled words of
  every block and says when they change, without blocking typing.
- ``backends`` holds the system checkers (macOS, Windows, Enchant on
  Linux, or none) behind one interface that never raises; ``words``
  finds the words of Markdown text that are checked.

See docs/develop/spelling.md for how the editor uses them.
"""

from .backends import (
    AUTOMATIC,
    NullBackend,
    SpellBackend,
    TextCheck,
    best_language,
    create_backend,
    normalize_language,
)
from .service import DocumentSpelling, Found, Misspelling, SpellChecker

__all__ = [
    "AUTOMATIC",
    "DocumentSpelling",
    "Found",
    "Misspelling",
    "NullBackend",
    "SpellBackend",
    "SpellChecker",
    "TextCheck",
    "best_language",
    "create_backend",
    "normalize_language",
]
