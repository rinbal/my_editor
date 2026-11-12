# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Photo groups in a document that is being edited.

A group is one paragraph of picture characters (photo_group), so most of
editing it is Qt's own: the caret stops before each picture and after the
last, Backspace and Delete take one picture away, a selection can start
or end at any picture. What Qt does not know is what a group is. After an
edit, **settle** puts that right, inside the edit's own step of Undo:

- text that landed between a group's pictures gets a paragraph of its own
  (the group is in two parts then), and a group left with one picture is
  that picture's paragraph again, its caption under it;
- pictures that an edit made neighbours (a picture pasted or dropped next
  to a group, the paragraph between two groups deleted) become one group,
  as they are one group when the file is opened again.

**set_units** replaces a group's pictures by another list of them (the
same in another order, one fewer, one more): with settle, all a command
needs to move, add, remove, split or join pictures as one step.
"""

from __future__ import annotations

from typing import Optional, Sequence, Tuple

from PySide6.QtGui import QTextBlockFormat, QTextCharFormat, QTextCursor, QTextDocument

import photo_group
import photo_group_find
from doc_walk import OBJECT_REPLACEMENT

# What an edit changed, as QTextDocument.contentsChange says it: (position,
# characters removed, characters added).
Change = Tuple[int, int, int]


def _has_units_or_mark(block) -> bool:
    return OBJECT_REPLACEMENT in block.text() or photo_group.is_group_block(block)


def _span(doc, changes: Sequence[Change]) -> Tuple[int, int]:
    """From where to where ``changes`` reach in the document as it is now.
    Several changes: each was reported where it was at its time, so the
    reach allows for what the later ones added."""
    start = min(position for position, _removed, _added in changes)
    end = max(position + added for position, _removed, added in changes)
    if len(changes) > 1:
        end += sum(added for _position, _removed, added in changes)
    last = max(0, doc.characterCount() - 1)
    return min(start, last), min(end, last)


class _Step:
    """The step of Undo that the edit before made, joined once there is
    something to write: an edit that leaves every group whole adds
    nothing to it."""

    def __init__(self, doc: QTextDocument) -> None:
        self.cursor = QTextCursor(doc)
        self.open = False

    def write(self) -> QTextCursor:
        """The cursor to write with, the step joined."""
        if not self.open:
            self.cursor.joinPreviousEditBlock()
            self.open = True
        return self.cursor

    def end(self) -> bool:
        if self.open:
            self.cursor.endEditBlock()
        return self.open


def settle(doc: QTextDocument, changes: Sequence[Change],
           caret: Optional[QTextCursor] = None, *, join: bool = True) -> bool:
    """Make the paragraphs an edit touched hold their pictures as groups
    are held (see the module's words). Called once the edit's own edit
    block has ended, with the ``changes`` the document reported for it;
    what is written here joins that block, so one Undo takes back both.
    ``join`` lets pictures that became neighbours form one group; a run
    of typing passes False, so that words typed under a picture are never
    taken for its caption while they are typed. ``caret`` (the cursor the
    view goes on with) keeps its place among the pictures of a group
    written anew. True when anything was written."""
    if not changes:
        return False
    start, end = _span(doc, changes)
    first = doc.findBlock(start)
    last = doc.findBlock(end)
    if first.previous().isValid():
        first = first.previous()
    if last.next().isValid():
        last = last.next()
    block = first
    while not _has_units_or_mark(block):
        if block.position() >= last.position():
            return False
        block = block.next()
    step = _Step(doc)
    # Places that move with what is written before them: the end of what
    # is looked at, and the two ends of what the edit changed.
    stop, changed_from, changed_to = QTextCursor(doc), QTextCursor(doc), QTextCursor(doc)
    stop.setPosition(last.position() + last.length() - 1)
    changed_from.setPosition(start)
    changed_to.setPosition(end)
    _separate(doc, step, first.position(), stop)
    low = first.position()
    if join:
        low, high = _reach(doc, low, stop.position())
        stop.setPosition(high)
        # One change: only the pictures it touched join their neighbours
        # (a caption being written between two pictures stays words).
        touched = ((changed_from.position(), changed_to.position()) if len(changes) == 1
                   else (low, high))
        for run in reversed(photo_group_find.find_runs(doc, low, stop.position())):
            if (len(run.units) >= 2 and not run.is_group
                    and any(a <= touched[1] and b >= touched[0] for a, b in run.pictures)):
                _rewrite(doc, step.write(), run, caret)
    # A group left with one picture is that picture's paragraphs again.
    for run in reversed(photo_group_find.find_runs(doc, low, stop.position(), lone=False)):
        if len(run.units) < 2:
            _rewrite(doc, step.write(), run, caret)
    return step.end()


def _separate(doc, step: _Step, first: int, stop: QTextCursor) -> None:
    """From the paragraph at ``first`` to ``stop``: every stretch of units
    alone in a paragraph marked as a group's, and no other paragraph
    marked so; text typed after a picture (which took the picture's
    format with it) in a plain format."""
    block = doc.findBlock(first)
    while block.isValid() and block.position() <= stop.position():
        position = block.position()
        segments = photo_group.block_segments(block)
        marked = photo_group.is_group_block(block)
        if segments is None:
            if marked:
                step.write().setPosition(position)
                step.cursor.setBlockFormat(photo_group.own_format(block))
            block = block.next()
            continue
        top = _may_hold_group(doc, block)
        if len(segments) == 1 and top and marked:
            block = block.next()            # a group's paragraph as it should be
            continue
        cursor = step.write()
        own = photo_group.own_format(block)
        group = QTextBlockFormat(own)
        group.setProperty(photo_group.GROUP, True)
        for _start, segment in segments:
            if not isinstance(segment[0], photo_group.Unit):
                _plain_format(cursor, segment)
        for start, end, units in photo_group.isolate(cursor, block):
            cursor.setPosition(start)
            if top:
                cursor.setBlockFormat(group)
            else:
                # In a list, a quote or a table: no group there.
                cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
                cursor.removeSelectedText()
                photo_group.write_paragraphs(cursor, units, own)
        if marked and not (top and isinstance(segments[0][1][0], photo_group.Unit)):
            # What stands before its first stretch of units is no group's.
            cursor.setPosition(position)
            cursor.setBlockFormat(own)
        block = doc.findBlock(position).next()


def _may_hold_group(doc, block) -> bool:
    """Whether a group's paragraph may stand where ``block`` does: at the
    top of the document, outside lists, quotes, headings and code."""
    if doc.frameAt(block.position()).parentFrame() is not None:
        return False
    return photo_group.is_plain_paragraph(block)


def _plain_format(cursor: QTextCursor, pieces) -> None:
    """Text typed after a picture carries the picture's format, its unit
    included (QTextCursor::insertText keeps all of a format but the object
    type): it gets a plain one."""
    for position, text, fmt in pieces:
        if fmt.hasProperty(photo_group.UNIT):
            cursor.setPosition(position)
            cursor.setPosition(position + len(text), QTextCursor.MoveMode.KeepAnchor)
            cursor.setCharFormat(QTextCharFormat())


def _reach(doc, first: int, last: int) -> Tuple[int, int]:
    """``first`` and ``last`` moved out over the picture paragraphs (and
    the captions between them) that stand right before and after: the
    whole of every run of pictures that starts or ends between them."""
    def pictures(block) -> bool:
        return block.isValid() and block.text().startswith(OBJECT_REPLACEMENT)

    block = doc.findBlock(first)
    while True:
        before = block.previous()
        if pictures(before):
            block = before
        elif (before.isValid() and pictures(before.previous())
              and photo_group_find.caption_paragraph(before) is not None):
            block = before.previous()
        else:
            break
    low = block.position()
    block = doc.findBlock(last)
    while True:
        after = block.next()
        if pictures(after) or (after.isValid() and pictures(block)
                               and photo_group_find.caption_paragraph(after) is not None):
            block = after
        else:
            break
    return low, block.position() + block.length() - 1


def _rewrite(doc, cursor: QTextCursor, run: photo_group_find.Found,
             caret: Optional[QTextCursor]) -> None:
    """Write ``run`` anew: a group when it holds two pictures or more,
    else its picture's paragraphs. A ``caret`` in it stays before the
    picture it stood before (after the last, at its end)."""
    place = None
    if caret is not None and run.first <= caret.position() <= run.end:
        place = sum(1 for position in range(run.first, caret.position())
                    if doc.characterAt(position) == OBJECT_REPLACEMENT)
    photo_group.replace_paragraphs(cursor, run.first, run.end, run.format, run.units)
    if place is None:
        return
    if len(run.units) >= 2:
        caret.setPosition(run.first + place)
        return
    # One picture's paragraphs: before it, or at their end.
    block = doc.findBlock(run.first)
    end = block.position() + block.length() - 1
    if place and run.units and run.units[0].has_caption and run.units[0].caption_join is None:
        end = block.next().position() + block.next().length() - 1
    caret.setPosition(end if place else run.first)


def set_unit(cursor: QTextCursor, position: int, unit: photo_group.Unit) -> None:
    """Put ``unit`` in place of the group picture at ``position`` (the
    same picture with another caption, say): one change of the document."""
    cursor.setPosition(position)
    cursor.setPosition(position + 1, QTextCursor.MoveMode.KeepAnchor)
    cursor.setCharFormat(unit.unit_format())


def set_units(cursor: QTextCursor, group: photo_group.Group,
              units: Sequence[photo_group.Unit]) -> None:
    """Write ``units`` in place of ``group``'s pictures: two or more as
    the group, one as its own paragraphs, none as an empty paragraph. One
    change of the document; inside HtmlEditor.undo_step, one named step."""
    photo_group.replace_paragraphs(cursor, group.start, group.end, group.format, list(units))
