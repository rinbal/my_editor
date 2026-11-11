# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Find and replace in a document, apart from any window.

The find bar (widgets.FindBar) asks; this module answers, over a plain
QTextDocument: where the text occurs, with or without matching case and
as whole words only, and what replacing it does. A replacement takes the
style of the text it replaces (bold stays bold, a link stays a link to
the same address), and Replace All is one step on the undo stack.

The text is searched as one string with a regular expression rather
than with QTextDocument.find, which goes back into Python for every
match (1.6 seconds for 55,000 matches in a long chapter, on every pause
in typing while the find bar is open; a few milliseconds this way).

Letters with accents can be stored two ways: as one character (é) or as
a letter followed by a combining accent (e and ´, common in text from
macOS file names and some PDFs). Both ways are found, whichever way the
search is typed, and "Cafe" does not find the "Cafe" inside "Café"
written the second way: the accent belongs to the last letter.

The captions of photo groups are words a reader sees that the text does
not hold (photo_group): they are searched too, after their picture, and
replaced in their runs, so a caption's bold words and links stay.
"""

from __future__ import annotations

import itertools
import re
import unicodedata
from bisect import bisect_left
from dataclasses import dataclass
from typing import List, Tuple

from PySide6.QtGui import QTextCharFormat, QTextCursor, QTextDocument

import photo_caption
import photo_group
import photo_group_edit
from doc_walk import OBJECT_REPLACEMENT, doc_length, doc_offset

# (start, end) positions in the document; in a group picture's caption
# (picture, picture + 1, start, end), start and end in UTF-16 units of
# the caption's words. Either sorts in document order.
Match = Tuple[int, ...]


@dataclass(frozen=True)
class FindOptions:
    """How the text is looked for."""

    match_case: bool = False
    whole_words: bool = False



def _in_word(char: str) -> bool:
    """Letters and numbers make words, as Qt finds whole words: a mark
    and a character beyond U+FFFF (an emoji, a rare CJK letter, which Qt
    sees as two halves) end one. (An accent on a match's last letter is
    looked at on its own: find_all.)"""
    return char.isalnum() and ord(char) <= 0xFFFF


def _pattern(needle: str, options: FindOptions) -> re.Pattern:
    """Every place ``needle`` starts, written either way (one character
    per accented letter, or letter and accent)."""
    forms = sorted({needle, unicodedata.normalize("NFC", needle),
                    unicodedata.normalize("NFD", needle)}, key=len, reverse=True)
    flags = 0 if options.match_case else re.IGNORECASE
    # A lookahead finds overlapping places too, so a place that is not a
    # whole word cannot hide one that starts inside it.
    return re.compile("(?=(" + "|".join(re.escape(form) for form in forms) + "))", flags)


def find_all(doc: QTextDocument, needle: str, options: FindOptions = FindOptions()
             ) -> List[Match]:
    """Every occurrence of ``needle``, in document order, none
    overlapping the one before."""
    if not needle:
        return []
    # The document's characters (paragraph ends and table edges included):
    # one per position, but for a character beyond U+FFFF (an emoji), which
    # takes two positions and is one character here.
    text = doc.toRawText()
    to_document = (lambda index: index) if doc_length(text) == len(text) else _Positions(text)
    pattern = _pattern(needle, options)
    matches: List[Match] = [(to_document(start), to_document(end))
                            for start, end in _found(text, pattern, options)]
    captions = _caption_matches(doc, text, pattern, options)
    return sorted(matches + captions) if captions else matches


def _found(text: str, pattern: re.Pattern, options: FindOptions) -> List[Tuple[int, int]]:
    """Where ``pattern`` occurs in ``text``, by index, none overlapping."""
    found_at: List[Tuple[int, int]] = []
    length = len(text)
    taken_until = 0
    for found in pattern.finditer(text):
        start, end = found.start(1), found.end(1)
        if start < taken_until or start == end:
            continue
        if end < length and unicodedata.combining(text[end]):
            continue                          # the last letter has an accent the search has not
        if options.whole_words and ((start > 0 and _in_word(text[start - 1]))
                                    or (end < length and _in_word(text[end]))):
            continue
        found_at.append((start, end))
        taken_until = end
    return found_at


def _caption_matches(doc: QTextDocument, text: str, pattern: re.Pattern,
                     options: FindOptions) -> List[Match]:
    """The occurrences in the captions of ``doc``'s photo groups (``text``
    being its raw text), each after its picture."""
    if OBJECT_REPLACEMENT not in text:
        return []
    to_document = (lambda index: index) if doc_length(text) == len(text) else _Positions(text)
    matches: List[Match] = []
    seen = set()
    for found in re.finditer(OBJECT_REPLACEMENT, text):
        block = doc.findBlock(to_document(found.start()))
        if block.blockNumber() in seen or not photo_group.is_group_block(block):
            continue
        seen.add(block.blockNumber())
        for start, units in photo_group.unit_stretches(block):
            for offset, unit in enumerate(units):
                words = unit.caption_text
                matches.extend((start + offset, start + offset + 1, doc_length(words[:first]),
                                doc_length(words[:last]))
                               for first, last in _found(words, pattern, options))
    return matches


class _Positions:
    """Document positions of indexes into ``text``, asked in increasing
    order (each found after the one before), so a long document is walked
    once."""

    def __init__(self, text: str) -> None:
        self._text = text
        self._index = 0
        self._position = 0

    def __call__(self, index: int) -> int:
        if index < self._index:
            return doc_offset(self._text, index)
        self._position += doc_length(self._text[self._index:index])
        self._index = index
        return self._position


def visible_matches(matches: List[Match], first: int, last: int) -> List[Tuple[int, Match]]:
    """The matches that touch ``first`` to ``last`` (what is on screen),
    each with its index: only those are painted, however many there are."""
    index = bisect_left(matches, first, key=lambda match: match[0])
    if index > 0 and matches[index - 1][1] > first:
        index -= 1
    shown = []
    while index < len(matches) and matches[index][0] <= last:
        shown.append((index, matches[index]))
        index += 1
    return shown


def _style_of(doc: QTextDocument, position: int) -> QTextCharFormat:
    """The style of the character at ``position``."""
    probe = QTextCursor(doc)
    probe.setPosition(position + 1)
    return QTextCharFormat(probe.charFormat())


def replace_match(doc: QTextDocument, match: Match, replacement: str) -> Match:
    """Replace one match, in the style of its first character. Returns
    where the replacement now is. One step on the undo stack."""
    if len(match) > 2:
        cursor = QTextCursor(doc)
        cursor.beginEditBlock()
        _replace_in_caption(cursor, match[0], [match], replacement)
        cursor.endEditBlock()
        return match[:3] + (match[2] + doc_length(replacement),)
    start, end = match
    cursor = QTextCursor(doc)
    cursor.setPosition(start)
    cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
    style = _style_of(doc, start)
    cursor.beginEditBlock()
    cursor.insertText(replacement, style)
    cursor.endEditBlock()
    return start, start + doc_length(replacement)


def replace_all(doc: QTextDocument, needle: str, replacement: str,
                options: FindOptions = FindOptions()) -> int:
    """Replace every occurrence; returns how many. One step on the undo
    stack, so Undo brings every one of them back."""
    matches = find_all(doc, needle, options)
    if not matches:
        return 0
    cursor = QTextCursor(doc)
    cursor.beginEditBlock()
    # A caption changes its picture's format, not the text: first, in
    # place, picture by picture.
    in_captions = [match for match in matches if len(match) > 2]
    for position, ones in itertools.groupby(in_captions, key=lambda match: match[0]):
        _replace_in_caption(cursor, position, list(ones), replacement)
    # From the end backwards: what is not replaced yet keeps its place.
    for start, end in reversed([match for match in matches if len(match) == 2]):
        style = _style_of(doc, start)
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        cursor.insertText(replacement, style)
    cursor.endEditBlock()
    return len(matches)


def _replace_in_caption(cursor: QTextCursor, position: int, matches: List[Match],
                        replacement: str) -> None:
    """Replace ``matches`` in the caption of the group picture at
    ``position``, each in the style of the run it starts in."""
    unit = photo_group.unit_of_picture(_style_of(cursor.document(), position))
    runs = unit.caption
    for _picture, _after, start, end in reversed(matches):
        runs = photo_caption.replaced(runs, start, end, replacement)
    photo_group_edit.set_unit(cursor, position, unit.with_caption(runs))
