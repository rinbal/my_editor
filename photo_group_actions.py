# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What an author does to the pictures of a document, as changes of it.

A picture stands in a photo group (photo_group) or alone in a paragraph of
its own, its caption under it. Every action here takes such a picture
(picture_at, picture_of) and writes the document the Markdown means after
it: whole units change places, a unit goes away or comes in, a picture's
address gains or loses #wide, a caption or a description changes. Nothing
else is ever written (EDITING.md 2.1 of the photo grid design).

- **move** puts a picture at another place of its group;
- **relocate** takes it to a Place: a slot of a group, beside a picture
  that stands alone (the two are a group then), or a paragraph of its own
  before or after another paragraph. **beyond** finds the place past the
  words above or below (out of the group, or into the next one);
- **insert** puts new pictures at a Place, **remove** takes one away;
- **split_after** opens an empty paragraph after a picture of a group, and
  **join** makes two groups with only words between them one;
- **set_wide**, **set_caption**, **set_description** and **set_address**
  change one picture.

Each is called inside HtmlEditor.undo_step, so it is one named step of
Undo, and photo_group_edit.settle (run when that step's edit block ends)
lets pictures the action made neighbours be one group, as they are when
the file is opened again. Nothing here knows a view.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable, List, Optional, Sequence

from PySide6.QtGui import (
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QTextFormat,
    QTextImageFormat,
)

import photo_group
import photo_group_find
from doc_walk import OBJECT_REPLACEMENT
from photo_group import Unit

_KEEP = QTextCursor.MoveMode.KeepAnchor


@dataclass
class Picture:
    """A picture an action is about: where its character is, its place
    among the pictures it stands with (from 1; alone: 1 of 1), its unit,
    and its run: the paragraphs the unit and its companions take up."""

    position: int
    place: int
    count: int
    unit: Unit
    first: int
    end: int
    units: List[Unit]
    format: QTextBlockFormat
    grouped: bool

    @property
    def index(self) -> int:
        return self.place - 1


def _top(doc: QTextDocument, block) -> bool:
    """Whether ``block`` is at the top of the document (not in a table)."""
    return doc.frameAt(block.position()).parentFrame() is None


def _lone(doc: QTextDocument, block) -> Optional[Picture]:
    """The picture that stands alone in ``block``, with the caption under
    it (on its line, or the italic paragraph that follows)."""
    if not block.isValid() or photo_group.is_group_block(block) or not _top(doc, block):
        return None
    units = photo_group_find.picture_units(block)
    if units is None or len(units) != 1:
        return None
    unit = units[0]
    end = block.position() + block.length() - 1
    below = block.next()
    if not unit.caption and below.isValid() and _top(doc, below):
        caption = photo_group_find.caption_paragraph(below)
        if caption is not None:
            unit.caption = caption
            end = below.position() + below.length() - 1
    return Picture(block.position(), 1, 1, unit, block.position(), end, [unit],
                   block.blockFormat(), False)


def picture_at(doc: QTextDocument, position: int) -> Optional[Picture]:
    """The picture whose character is at ``position``: one of a group, or
    one alone at the start of its paragraph; None for anything else (a
    picture in a line of words, in a list or in a table)."""
    block = doc.findBlock(position)
    if not block.isValid():
        return None
    if not photo_group.is_group_block(block):
        return _lone(doc, block) if position == block.position() else None
    group = photo_group.group_at(doc, position)
    index = position - block.position()
    if group is None or not 0 <= index < len(group.units):
        return None
    return Picture(position, index + 1, len(group.units), group.units[index], group.start,
                   group.end, group.units, group.format, True)


def picture_numbered(doc: QTextDocument, number: int) -> Optional[Picture]:
    """The group picture whose unit has ``number`` (photo_group.Unit.number),
    wherever it stands now; None when no group holds it (a picture alone
    has no number)."""
    if number > 0:
        for group in photo_group.groups_in(doc):
            for index, unit in enumerate(group.units):
                if unit.number == number:
                    return picture_at(doc, group.start + index)
    return None


def standing(doc: QTextDocument, picture: Picture) -> Optional[Picture]:
    """``picture`` as the document holds it now: a group's by its number,
    wherever it went; one alone at its place. None when it is gone."""
    found = (picture_numbered(doc, picture.unit.number) if picture.grouped
             else picture_at(doc, picture.position))
    return found if found is not None and found.unit.name == picture.unit.name else None


def picture_of(doc: QTextDocument, cursor: QTextCursor) -> Optional[Picture]:
    """The picture ``cursor`` means: the one it selects alone, or, with
    nothing selected, the picture of a group after the caret (at the
    group's end, the last) or the picture alone in the caret's paragraph."""
    start, end = cursor.selectionStart(), cursor.selectionEnd()
    if end - start > 1:
        return None
    block = doc.findBlock(start)
    if not block.isValid():
        return None
    if photo_group.is_group_block(block):
        count = len(photo_group.block_units(block))
        if not count:
            return None
        return picture_at(doc, block.position() + min(start - block.position(), count - 1))
    if end - start == 1 and start != block.position():
        return None
    return _lone(doc, block)


# --------------------------------------------------------------------------- #
# Places                                                                       #
# --------------------------------------------------------------------------- #

SLOT, BESIDE, GAP = "slot", "beside", "gap"


@dataclass(frozen=True)
class Place:
    """Where a picture can go.

    - SLOT: among the pictures of the group that starts at ``at``, as its
      picture number ``index`` (from 0; counted without the picture that
      moves, when it is one of that group's);
    - BESIDE: before or after the picture that stands alone at ``at``:
      the two are a group then;
    - GAP: a paragraph of its own before or after the paragraph that
      starts at ``at``."""

    kind: str
    at: int
    index: int = 0
    before: bool = True


def _tidy(units: Sequence[Unit]) -> List[Unit]:
    """``units`` as a group or a paragraph can hold them: a picture written
    on the line of the one before it ("![a](a.png) ![b](b.png)") starts a
    paragraph of its own when it is first now, or when the one before it
    has a caption under it."""
    out: List[Unit] = []
    for unit in units:
        if unit.join is not None and (not out or out[-1].has_caption):
            unit = replace(unit, join=None)
        out.append(unit)
    return out


def _remove_paragraphs(cursor: QTextCursor, doc: QTextDocument, first: int, end: int) -> None:
    """Take the paragraphs from ``first`` to ``end`` away whole, the ones
    around them as they were. (A paragraph's format is held by the break
    before it: taking that break keeps the next paragraph's format.)"""
    after = doc.findBlock(end).next()
    if doc.findBlock(first).previous().isValid():
        cursor.setPosition(first - 1)
        cursor.setPosition(end, _KEEP)
        cursor.removeSelectedText()
    elif after.isValid():
        block_format, char_format = after.blockFormat(), after.charFormat()
        cursor.setPosition(first)
        cursor.setPosition(end + 1, _KEEP)
        cursor.removeSelectedText()
        cursor.setBlockFormat(block_format)
        cursor.setBlockCharFormat(char_format)
    else:
        cursor.setPosition(first)
        cursor.setPosition(end, _KEEP)
        cursor.removeSelectedText()
        cursor.setBlockFormat(QTextBlockFormat())


def _take(cursor: QTextCursor, doc: QTextDocument, picture: Picture) -> Unit:
    """Take ``picture`` out of where it stands; its unit, free of the
    line it shared."""
    if picture.grouped:
        rest = [unit for unit in picture.units if unit is not picture.unit]
        photo_group.replace_paragraphs(cursor, picture.first, picture.end, picture.format,
                                       _tidy(rest))
    else:
        _remove_paragraphs(cursor, doc, picture.first, picture.end)
    return replace(picture.unit, join=None)


def _put(cursor: QTextCursor, doc: QTextDocument, place: Place, units: Sequence[Unit],
         own: QTextBlockFormat) -> int:
    """Write ``units`` at ``place``; where the first of them is then."""
    if place.kind == SLOT:
        group = photo_group.group_at(doc, place.at)
        together = list(group.units)
        together[place.index:place.index] = units
        photo_group.replace_paragraphs(cursor, group.start, group.end, group.format,
                                       _tidy(together))
        return group.start + place.index
    if place.kind == BESIDE:
        lone = picture_at(doc, place.at)
        together = [*units, lone.unit] if place.before else [lone.unit, *units]
        photo_group.replace_paragraphs(cursor, lone.first, lone.end, lone.format,
                                       _tidy(together))
        return lone.first + (0 if place.before else 1)
    block = doc.findBlock(place.at)
    if place.before:
        cursor.setPosition(block.position())
        cursor.insertBlock(block.blockFormat(), block.charFormat())
        cursor.movePosition(QTextCursor.MoveOperation.PreviousBlock)
        cursor.setBlockCharFormat(QTextCharFormat())
    else:
        cursor.setPosition(block.position() + block.length() - 1)
        cursor.insertBlock(own, QTextCharFormat())
    at = cursor.position()
    if len(units) >= 2:
        photo_group.write_group(cursor, _tidy(units), own)
    else:
        photo_group.write_paragraphs(cursor, list(units), own)
    return at


def _plain(own: QTextBlockFormat) -> QTextBlockFormat:
    """The format of a picture's paragraph of its own: ``own`` when that
    is a plain paragraph's, else a plain one."""
    return own if not own.headingLevel() and own.objectIndex() < 0 else QTextBlockFormat()


# --------------------------------------------------------------------------- #
# Moving                                                                       #
# --------------------------------------------------------------------------- #

def move(cursor: QTextCursor, picture: Picture, to: int) -> int:
    """Put ``picture`` at place ``to`` (from 0) of its group, the others
    in their order; where it is then."""
    units = [unit for unit in picture.units if unit is not picture.unit]
    units.insert(to, replace(picture.unit, join=None))
    photo_group.replace_paragraphs(cursor, picture.first, picture.end, picture.format,
                                   _tidy(units))
    return picture.first + to


def relocate(cursor: QTextCursor, doc: QTextDocument, picture: Picture, place: Place) -> int:
    """Take ``picture`` to ``place``; where it is then (before settle has
    joined it to pictures it stands next to now)."""
    if place.kind == SLOT and picture.grouped and place.at == picture.first:
        return move(cursor, picture, place.index)
    # The place by a cursor, which moves with what is taken away before it.
    marker = QTextCursor(doc)
    marker.setPosition(place.at)
    unit = _take(cursor, doc, picture)
    return _put(cursor, doc, replace(place, at=marker.position()), [unit], _plain(picture.format))


def _has_pictures(block) -> bool:
    return OBJECT_REPLACEMENT in block.text()


def beyond(doc: QTextDocument, picture: Picture, down: bool) -> Optional[Place]:
    """The place past the words under ``picture``'s run (``down``) or
    above it: a paragraph of its own behind the first paragraph with
    words in it, or, when pictures come first (with only empty lines
    between, which Markdown does not keep), a place among them. A table
    counts as a paragraph. None when nothing stands there."""
    step = (lambda block: block.next()) if down else (lambda block: block.previous())
    block = step(doc.findBlock(picture.end if down else picture.first))
    while block.isValid() and _top(doc, block) and not block.text().strip():
        block = step(block)
    if not block.isValid():
        return None
    if not _top(doc, block):
        # A table: behind it, which is before (after) the paragraph there.
        while block.isValid() and not _top(doc, block):
            block = step(block)
        return Place(GAP, block.position(), before=down) if block.isValid() else None
    if photo_group.is_group_block(block):
        return Place(SLOT, block.position(), 0 if down else len(photo_group.block_units(block)))
    if not down and photo_group_find.caption_paragraph(block) is not None:
        above = _lone(doc, block.previous())
        if above is not None and above.end >= block.position():
            return Place(BESIDE, above.position, before=False)
    lone = _lone(doc, block)
    if lone is not None:
        return Place(BESIDE, lone.position, before=down)
    return Place(GAP, block.position(), before=not down)


def neighbour(doc: QTextDocument, picture: Picture, down: bool) -> Optional[Picture]:
    """The pictures nearest to ``picture``'s run with only plain
    paragraphs of words between: a group (its first picture) or a picture
    alone. None when a heading, a list, a table or nothing comes first."""
    step = (lambda block: block.next()) if down else (lambda block: block.previous())
    block = step(doc.findBlock(picture.end if down else picture.first))
    while block.isValid() and _top(doc, block):
        if photo_group.is_group_block(block):
            return picture_at(doc, block.position())
        lone = _lone(doc, block)
        if lone is not None:
            return lone
        if not down and photo_group_find.caption_paragraph(block) is not None:
            above = _lone(doc, block.previous())
            if above is not None and above.end >= block.position():
                return above
        if _has_pictures(block) or not photo_group.is_plain_paragraph(block):
            return None
        block = step(block)
    return None


def join(cursor: QTextCursor, doc: QTextDocument, picture: Picture, down: bool) -> Optional[int]:
    """Make ``picture``'s run and the pictures under it (``down``) or
    above it one group, the words between them under it; where ``picture``
    is then. None when no pictures stand there (neighbour)."""
    other = neighbour(doc, picture, down)
    if other is None:
        return None
    upper, lower = (picture, other) if down else (other, picture)
    _remove_paragraphs(cursor, doc, lower.first, lower.end)
    photo_group.replace_paragraphs(cursor, upper.first, upper.end, upper.format,
                                   _tidy([*upper.units, *lower.units]))
    return upper.first + picture.index + (0 if down else upper.count)


def split_after(cursor: QTextCursor, picture: Picture) -> int:
    """Open an empty paragraph after ``picture`` in its group: the
    pictures before it and those after it stand apart once words are
    written there. Where that paragraph starts."""
    own = picture.format
    parts = (picture.units[:picture.place], picture.units[picture.place:])
    cursor.setPosition(picture.first)
    cursor.setPosition(picture.end, _KEEP)
    cursor.removeSelectedText()
    _write(cursor, parts[0], own)
    cursor.insertBlock(own, QTextCharFormat())
    at = cursor.position()
    cursor.insertBlock(own, QTextCharFormat())
    _write(cursor, parts[1], own)
    return at


def _write(cursor: QTextCursor, units: Sequence[Unit], own: QTextBlockFormat) -> None:
    if len(units) >= 2:
        photo_group.write_group(cursor, _tidy(units), own)
    else:
        photo_group.write_paragraphs(cursor, _tidy(units), own)


def close_gap(cursor: QTextCursor, doc: QTextDocument, position: int) -> None:
    """Take the empty paragraph at ``position`` away (a split that got no
    words): the pictures around it are neighbours again."""
    block = doc.findBlock(position)
    _remove_paragraphs(cursor, doc, block.position(), block.position() + block.length() - 1)


# --------------------------------------------------------------------------- #
# Adding and removing                                                          #
# --------------------------------------------------------------------------- #

def insert(cursor: QTextCursor, doc: QTextDocument, place: Place,
           pictures: Sequence[QTextCharFormat]) -> int:
    """Put new ``pictures`` (image formats) at ``place``, in their order,
    without captions; where the first of them is then."""
    units = [photo_group.unit_of_picture(QTextCharFormat(fmt)) for fmt in pictures]
    return _put(cursor, doc, place, units, QTextBlockFormat())


def remove(cursor: QTextCursor, doc: QTextDocument, picture: Picture) -> Optional[int]:
    """Take ``picture`` away, its caption with it. Where the picture that
    takes its place in the group is (the one before it, for the last);
    None for a picture that stood alone."""
    _take(cursor, doc, picture)
    if not picture.grouped:
        return None
    return picture.first + min(picture.index, max(0, picture.count - 2))


# --------------------------------------------------------------------------- #
# One picture                                                                  #
# --------------------------------------------------------------------------- #

def _set_image(cursor: QTextCursor, picture: Picture,
               change: Callable[[QTextImageFormat], None]) -> None:
    image = picture.unit.image.toImageFormat()
    change(image)
    unit = picture.unit
    cursor.setPosition(picture.position)
    cursor.setPosition(picture.position + 1, _KEEP)
    if picture.grouped:
        cursor.setCharFormat(Unit(image, image.name(), unit.caption, unit.caption_join,
                                  unit.join, unit.number).unit_format())
    else:
        cursor.setCharFormat(image)


def set_wide(cursor: QTextCursor, picture: Picture, on: bool) -> None:
    """Give ``picture``'s address its #wide, or take it away."""
    name = photo_group.without_wide(picture.unit.name)
    _set_image(cursor, picture,
               lambda image: image.setName(name + photo_group.WIDE_SUFFIX if on else name))


def set_description(cursor: QTextCursor, picture: Picture, words: str) -> None:
    """``words`` as the picture's description (its alt text)."""
    _set_image(cursor, picture,
               lambda image: image.setProperty(QTextFormat.Property.ImageAltText, words))


def set_address(cursor: QTextCursor, picture: Picture, name: str) -> None:
    """Show another file in ``picture``'s place (a crop of it): ``name``,
    with the #wide the picture had; a size the old file was fitted to is
    forgotten."""
    def change(image: QTextImageFormat) -> None:
        image.setName(name + photo_group.WIDE_SUFFIX if picture.unit.wide else name)
        image.clearProperty(QTextFormat.Property.ImageWidth)
        image.clearProperty(QTextFormat.Property.ImageHeight)
    _set_image(cursor, picture, change)


def set_caption(cursor: QTextCursor, picture: Picture, words: str) -> None:
    """``words`` as the picture's caption; none takes the caption away."""
    unit = picture.unit.captioned(words)
    if picture.grouped:
        cursor.setPosition(picture.position)
        cursor.setPosition(picture.position + 1, _KEEP)
        cursor.setCharFormat(unit.unit_format())
    else:
        photo_group.replace_paragraphs(cursor, picture.first, picture.end, picture.format, [unit])
