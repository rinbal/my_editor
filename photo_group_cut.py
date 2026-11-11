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
  which deletes it and breaks no line there (STANDUP's rule).
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from PySide6.QtGui import QTextCursor, QTextDocument

import photo_group_actions as actions

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
