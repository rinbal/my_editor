# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deleting a selection that reaches a picture standing alone.

A picture alone is one unit with its caption (on its line, or the italic
paragraph under it). Qt deletes a selection as it is: one from words
into a caption takes the caption's picture with it, and the caption's
other words join the words above; one that ends at the start of a
picture's line joins the picture into the words before it. STANDUP's
editor keeps such a picture (deleteKeepingPictures, PG1C sessions 4e and
4f), and so does this one: a selection deletes a picture alone only when
it holds its whole unit, and a line that still holds a picture keeps it.

- **cuts** says what of a selection to delete instead: the part before
  the picture's paragraph and the caption's selected words;
- **delete** deletes a selection that way, inside the step its caller
  has open (Backspace, Delete, Return, typing, Cut and Paste all come
  here), and says where the rest of the selection's last line starts;
- **in_caption** tells Return over a selection that starts in a caption,
  which deletes it and breaks no line there (STANDUP's rule);
- **clipped** is such a selection as copied, cut or dragged (PG1C 4g):
  the selected text and the pictures wholly in it; of a picture whose
  caption it starts or ends in, only the selected caption words.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from PySide6.QtGui import QTextCharFormat, QTextCursor, QTextDocument, QTextDocumentFragment

import photo_group_actions as actions
from doc_walk import OBJECT_REPLACEMENT

# Ranges to delete, the last one first.
Cuts = List[Tuple[int, int]]


def _alone_around(doc: QTextDocument, position: int) -> Optional[Tuple[actions.Picture, int]]:
    """The picture alone whose unit ``position`` is in (its paragraph, or
    the caption paragraph under it), with where its caption's words start
    (its unit's end when it has none)."""
    block = doc.findBlock(position)
    for candidate in (block, block.previous()):
        picture = actions.alone(doc, candidate) if candidate.isValid() else None
        if picture is None or not picture.position <= position <= picture.end:
            continue
        own_end = candidate.position() + candidate.length() - 1
        if picture.end > own_end:                       # the caption is the paragraph below
            return picture, own_end + 1
        text = candidate.text()
        lead = 1
        while lead < len(text) and text[lead].isspace():
            lead += 1
        return picture, candidate.position() + lead
    return None


def _held_in_part(doc: QTextDocument, end: int) -> Optional[Tuple[int, int]]:
    """The picture alone whose unit a selection ending at ``end`` reaches
    into without holding all of it (also one that ends at the start of
    the picture's line): the picture's position and where its caption's
    words start."""
    found = _alone_around(doc, end)
    if found is None or not found[0].position <= end < found[0].end:
        return None
    return found[0].position, found[1]


def in_caption(doc: QTextDocument, position: int) -> bool:
    """Whether ``position`` is in the caption of a picture alone."""
    found = _alone_around(doc, position)
    return found is not None and bool(found[0].unit.caption) and found[1] <= position


def cuts(doc: QTextDocument, start: int, end: int) -> Optional[Cuts]:
    """What to delete of the selection from ``start`` to ``end`` so that a
    picture alone it holds only in part stays, with the rest of its
    caption: the words up to the picture's paragraph (which keeps its own
    line) and the caption's selected words. None when deleting the
    selection as it is keeps every picture."""
    held = _held_in_part(doc, end) if start < end else None
    if held is None or start > held[0]:
        return None
    picture, words = held
    out: Cuts = []
    if end > words:
        out.append((words, end))
    if start < picture - 1:
        out.append((start, picture - 1))
    return out


def delete(cursor: QTextCursor) -> Optional[int]:
    """Delete ``cursor``'s selection as cuts says, the cursor left where
    the selection started; where the rest of the selection's last line
    (the kept caption, the picture's line) starts then. None, and nothing
    done, when the selection deleted as it is keeps every picture."""
    start, end = cursor.selectionStart(), cursor.selectionEnd()
    found = cuts(cursor.document(), start, end)
    if found is None:
        return None
    for first, last in found:
        cursor.setPosition(first)
        cursor.setPosition(last, QTextCursor.MoveMode.KeepAnchor)
        cursor.removeSelectedText()
    cursor.setPosition(start)
    return end - sum(last - first for first, last in found)


def _caption_end(doc: QTextDocument, position: int) -> int:
    """Where the caption ``position`` is in ends (in_caption)."""
    return _alone_around(doc, position)[0].end


def clipped(cursor: QTextCursor) -> Optional[QTextDocumentFragment]:
    """``cursor``'s selection as the clipboard holds it when it starts or
    ends in the caption of a picture alone: what is selected, a picture
    alone only with its whole unit; of the picture whose caption it ends
    in, only the selected words, on a line of their own. Caption words
    lose the caption's italic, which is the caption's, not theirs (as
    STANDUP's caption node gives them). None when nothing is clipped."""
    doc = cursor.document()
    start, end = cursor.selectionStart(), cursor.selectionEnd()
    held = _held_in_part(doc, end) if start < end else None
    ends_in = held is not None and start <= held[0] < end
    starts_in = start < end and in_caption(doc, start)
    if not ends_in and not starts_in:
        return None
    copy = QTextDocument()
    QTextCursor(copy).insertFragment(cursor.selection())
    plain = QTextCharFormat()
    plain.setFontItalic(False)
    pieces = QTextCursor(copy)
    if ends_in:
        picture, words = held
        if copy.characterAt(picture - start) != OBJECT_REPLACEMENT:
            return None                     # a selection the copy does not keep in place
        if end > words:
            pieces.setPosition(words - start)
            pieces.setPosition(end - start, QTextCursor.MoveMode.KeepAnchor)
            pieces.mergeCharFormat(plain)
        pieces.setPosition(picture - start)
        pieces.setPosition(min(words, end) - start, QTextCursor.MoveMode.KeepAnchor)
        pieces.removeSelectedText()
    if starts_in:
        pieces.setPosition(0)
        pieces.setPosition(min(end, _caption_end(doc, start)) - start,
                           QTextCursor.MoveMode.KeepAnchor)
        pieces.mergeCharFormat(plain)
    return QTextDocumentFragment(copy)
