# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Comparing two versions of a text word by word (plan 8.1; the M-3
brief's bounds). Pure: no Qt.

The texts are a writer's, or an editor's correction: someone else
chooses them, and ``difflib`` takes time that grows with the square of
what it compares (lesson 13). So :func:`compare_words` bounds the work,
not only the answer:

- the paragraphs are matched first, so only paragraphs that changed are
  compared word by word, each on its own;
- a text of more than :data:`MAX_WORDS` words or :data:`MAX_PARAGRAPHS`
  paragraphs, or a changed stretch whose word-by-word table would have
  more than :data:`MAX_CELLS` cells, is not marked;
- the clock is read between the stretches, and past :data:`MAX_SECONDS`
  the marking stops.

Whatever stops it, the answer is ``too_many``: the screen then says "Zu
viele Änderungen zum Markieren" and still shows both versions. The
caller runs it off the main thread, once per pair of versions
(``nostr/ui/standup_compare.py``).

:func:`change_summary` is the one line of a version card ("1 Absatz neu,
1 ergänzt"), from the paragraphs alone.
"""

from __future__ import annotations

import difflib
import re
import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Tuple

from i18n import _, ngettext

MAX_WORDS = 40_000
MAX_PARAGRAPHS = 4_000
MAX_CELLS = 4_000_000
MAX_SECONDS = 2.0

SAME, ADDED, REMOVED = "same", "added", "removed"

_PARAGRAPH_BREAK = re.compile(r"\n[ \t]*\n+")
_TOKENS = re.compile(r"\s+|[^\s]+")
# A changed paragraph that keeps this much of what it had was added to.
_EXTENDED_KEEPS = 0.8


@dataclass(frozen=True)
class Part:
    """A stretch of text: the same in both, only in the new one
    (``added``) or only in the old one (``removed``)."""

    kind: str
    text: str


@dataclass(frozen=True)
class Comparison:
    """Two versions compared: ``parts`` in reading order, or
    ``too_many`` when the bounds stopped the marking (``parts`` is then
    empty; ``old`` and ``new`` are always there to show)."""

    old: str
    new: str
    parts: Tuple[Part, ...] = ()
    too_many: bool = False

    @property
    def changed(self) -> bool:
        return self.too_many or any(part.kind != SAME for part in self.parts)


def paragraphs(text: str) -> List[str]:
    return [p for p in _PARAGRAPH_BREAK.split(text.strip("\n")) if p.strip()]


def _matcher(a: Sequence[str], b: Sequence[str]) -> difflib.SequenceMatcher:
    return difflib.SequenceMatcher(None, a, b, autojunk=False)


class _Bound(Exception):
    """A bound was met: nothing is marked."""


def _words_apart(old: str, new: str, deadline: Callable[[], bool]) -> List[Part]:
    a, b = _TOKENS.findall(old), _TOKENS.findall(new)
    if len(a) * len(b) > MAX_CELLS or deadline():
        raise _Bound()
    parts: List[Part] = []
    for tag, a1, a2, b1, b2 in _matcher(a, b).get_opcodes():
        if tag == "equal":
            parts.append(Part(SAME, "".join(a[a1:a2])))
            continue
        if a2 > a1:
            parts.append(Part(REMOVED, "".join(a[a1:a2])))
        if b2 > b1:
            parts.append(Part(ADDED, "".join(b[b1:b2])))
    return parts


def compare_words(old: str, new: str, *, clock: Callable[[], float] = time.monotonic,
                  max_seconds: float = MAX_SECONDS) -> Comparison:
    """``old`` and ``new`` compared word by word within the bounds (see
    the module docstring). ``clock`` is a seam."""
    started = clock()

    def deadline() -> bool:
        return clock() - started > max_seconds

    a, b = paragraphs(old), paragraphs(new)
    try:
        if max(len(a), len(b)) > MAX_PARAGRAPHS \
                or len(old.split()) + len(new.split()) > MAX_WORDS:
            raise _Bound()
        parts: List[Part] = []
        for tag, a1, a2, b1, b2 in _matcher(a, b).get_opcodes():
            if parts:
                parts.append(Part(SAME, "\n\n"))
            if tag == "equal":
                parts.append(Part(SAME, "\n\n".join(a[a1:a2])))
            elif tag == "delete":
                parts.append(Part(REMOVED, "\n\n".join(a[a1:a2])))
            elif tag == "insert":
                parts.append(Part(ADDED, "\n\n".join(b[b1:b2])))
            else:
                parts += _words_apart("\n\n".join(a[a1:a2]), "\n\n".join(b[b1:b2]), deadline)
    except _Bound:
        return Comparison(old, new, too_many=True)
    return Comparison(old, new, parts=_joined(parts))


def _joined(parts: Sequence[Part]) -> Tuple[Part, ...]:
    """Neighbours of one kind as one part."""
    out: List[Part] = []
    for part in parts:
        if not part.text:
            continue
        if out and out[-1].kind == part.kind:
            out[-1] = Part(part.kind, out[-1].text + part.text)
        else:
            out.append(part)
    return tuple(out)


def _extended(old: str, new: str) -> bool:
    """Whether a changed paragraph kept most of its words and grew."""
    a, b = old.split(), new.split()
    if len(b) <= len(a) or len(a) * len(b) > MAX_CELLS:
        return False
    kept = sum(block.size for block in _matcher(a, b).get_matching_blocks())
    return kept >= _EXTENDED_KEEPS * len(a)


def change_counts(old: str, new: str) -> Optional[Tuple[int, int, int, int]]:
    """How many paragraphs are new, were added to, changed otherwise and
    were removed between ``old`` and ``new``; None past the bounds."""
    a, b = paragraphs(old), paragraphs(new)
    if max(len(a), len(b)) > MAX_PARAGRAPHS:
        return None
    new_count = extended = changed = removed = 0
    for tag, a1, a2, b1, b2 in _matcher(a, b).get_opcodes():
        if tag == "insert":
            new_count += b2 - b1
        elif tag == "delete":
            removed += a2 - a1
        elif tag == "replace":
            pairs = min(a2 - a1, b2 - b1)
            for offset in range(pairs):
                if _extended(a[a1 + offset], b[b1 + offset]):
                    extended += 1
                else:
                    changed += 1
            new_count += (b2 - b1) - pairs
            removed += (a2 - a1) - pairs
    return new_count, extended, changed, removed


def change_summary(old: str, new: str) -> str:
    """The one line of a version card (translated): "1 Absatz neu, 1
    ergänzt"; "Keine Änderungen am Text" when the text is the same, and
    a plain "Geändert" past the bounds."""
    counts = change_counts(old, new)
    if counts is None:
        return _("Changed")
    new_count, extended, changed, removed = counts
    said = []
    if new_count:
        said.append(ngettext("{count} paragraph new", "{count} paragraphs new",
                             new_count).format(count=new_count))
    if extended:
        said.append(ngettext("{count} added to", "{count} added to",
                             extended).format(count=extended))
    if changed:
        said.append(ngettext("{count} changed", "{count} changed",
                             changed).format(count=changed))
    if removed:
        said.append(ngettext("{count} removed", "{count} removed",
                             removed).format(count=removed))
    return ", ".join(said) if said else _("No changes to the text")


__all__ = ["ADDED", "Comparison", "MAX_CELLS", "MAX_PARAGRAPHS", "MAX_SECONDS", "MAX_WORDS",
           "Part", "REMOVED", "SAME", "change_counts", "change_summary", "compare_words",
           "paragraphs"]
