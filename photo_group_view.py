# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Photo groups as a view shows them: sized, drawn and found by the layout.

A group is one paragraph of picture characters (photo_group). Here a
handler for the document's pictures (install, the drawing object of
photo_group_drawing) gives each picture of a group the box that makes
Qt's own line breaking lay the paragraph out as the rows STANDUP's engine
arranges (photo_group_geometry): a row fills its line, so the next picture
starts a new one, and Qt breaks a page between two rows, never through a
picture. The handler draws each picture in its box, its caption under it.

Nothing here changes the document: the rows follow the width of the text
and the height of the screen at every layout, and a picture that arrives
late (picture_arrived) only has the paragraphs that show it laid out
again. So no width and no late picture leaves a step in Undo or marks the
document as changed.

The same boxes say where each picture is drawn (picture_boxes, picture_at)
and which picture the caret stands at (picture_at_cursor), for Quick Look,
the ring around it and what a screen reader says.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

from PySide6.QtCore import QRectF, Qt, QUrl
from PySide6.QtGui import QImage, QPixmap, QTextCursor, QTextDocument, QTextFormat

import photo_group
import photo_layout
from i18n import _
from photo_group_drawing import Arranged, CaptionMark, Handler, SizeLookup, is_placeholder

IMAGE_OBJECT = int(QTextFormat.ObjectTypes.ImageObject)


# --------------------------------------------------------------------------- #
# The sizes a surface has                                                      #
# --------------------------------------------------------------------------- #

def picture_size(image) -> Optional[Tuple[int, int]]:
    """The pixel size of a picture at hand (a QImage or QPixmap), or None
    for a stand-in or nothing. The engine decides which sizes it takes."""
    if isinstance(image, QPixmap):
        image = image.toImage()
    if not isinstance(image, QImage) or image.isNull() or is_placeholder(image):
        return None
    return photo_layout.picture_size(image.width(), image.height())


def resource_sizes(doc: QTextDocument) -> SizeLookup:
    """Sizes from the pictures ``doc`` holds, asked for as it asks to draw
    them. Only for a document a view shows: the view loads its pictures
    (by its own rules for local files and addresses); a document of its
    own would read a local name from the disk itself."""
    def size(name: str) -> Optional[Tuple[int, int]]:
        return picture_size(doc.resource(QTextDocument.ResourceType.ImageResource.value,
                                         QUrl(name)))
    return size


def sizes_from(found: Dict[str, object]) -> SizeLookup:
    """Sizes from pictures at hand by name (QImage, QSize or (w, h))."""
    def size(name: str) -> Optional[Tuple[int, int]]:
        value = found.get(name)
        if isinstance(value, (QImage, QPixmap)):
            return picture_size(value)
        if hasattr(value, "width") and hasattr(value, "height"):
            return photo_layout.picture_size(value.width(), value.height())
        if isinstance(value, tuple) and len(value) == 2:
            return photo_layout.picture_size(*value)
        return None
    return size


# Set on a document once install gave its layout a handler: a document
# that never had one (read for an export, a check or a copy) is not given
# a layout by asking, which would lay out every change made to it after.
_INSTALLED = "photoGroupHandler"


def _handler(doc) -> Optional[Handler]:
    if not doc.property(_INSTALLED):
        return None
    found = doc.documentLayout().handlerForObject(IMAGE_OBJECT)
    return found if isinstance(found, Handler) else None


def install(doc: QTextDocument, sizes: Optional[SizeLookup] = None) -> None:
    """Have ``doc``'s layout size the pictures of its groups (its other
    pictures as before), with the sizes ``sizes`` gives (without it, every
    picture is taken for 3:2 until it is known). Again for a new layout."""
    layout = doc.documentLayout()
    found = _handler(doc)
    if found is not None:
        found.sizes = sizes if sizes is not None else found.sizes
        found.forget()
        return
    original = layout.handlerForObject(IMAGE_OBJECT)
    handler = Handler(layout, original, sizes)
    layout.registerHandler(IMAGE_OBJECT, handler)
    doc.setProperty(_INSTALLED, True)


def set_marks(doc: QTextDocument, marks: Sequence[CaptionMark]) -> None:
    """Paint ``marks`` over the captions of ``doc``'s groups, in place of
    the ones before (the caller repaints)."""
    found = _handler(doc)
    if found is not None:
        found.marks = tuple(marks)


def set_screen_height(doc: QTextDocument, height: Optional[float]) -> None:
    """The height ``doc`` is read on (a view's, a page's), in its layout's
    pixels: a row takes at most its share of it, as on a reader's screen.
    The groups are laid out again when it changed."""
    found = _handler(doc)
    if found is None or found.screen_height == height:
        return
    before = found.metrics(doc)
    found.screen_height = height
    if found.metrics(doc) == before:
        return                          # a screen high enough before and after: the same rows
    found.forget()
    # From the first group to the last, in one go: each call lays out
    # what it names and places everything after it again.
    first = last = None
    block = doc.begin()
    while block.isValid():
        if photo_group.is_group_block(block):
            first = block if first is None else first
            last = block
        block = block.next()
    if first is not None:
        doc.markContentsDirty(first.position(),
                              last.position() + last.length() - first.position())


def picture_arrived(doc: QTextDocument, name: str) -> int:
    """The picture ``name`` is at hand now (or another one is): each
    paragraph that shows it is laid out again, once, and nothing else,
    with nothing in the document changed; a group only when it was laid
    out with another size. How many paragraphs that is."""
    found = _handler(doc)
    if found is None:
        return 0
    size = found.sizes(name) if found.sizes else None
    count = 0
    for position in found.showing(doc, name):
        block = doc.findBlock(position)
        units = [unit for unit in photo_group.block_units(block) if unit.name == name]
        stale = [unit for unit in units if found._sized.get(unit.number, False) != size]
        if units and not stale:
            continue
        for unit in stale:
            found._sized[unit.number] = size
        found.forget(block)
        doc.markContentsDirty(block.position(), block.length())
        count += 1
    return count


# --------------------------------------------------------------------------- #
# Where the pictures are                                                       #
# --------------------------------------------------------------------------- #

def _boxes_of(doc, found: Handler, block) -> List[Tuple[int, photo_group.Unit, QRectF]]:
    """Where each picture of the group in ``block`` is drawn."""
    text_layout = block.layout()
    if text_layout is None or not text_layout.lineCount():
        return []
    rect = doc.documentLayout().blockBoundingRect(block)
    out = []
    for start, _units in photo_group.unit_stretches(block):
        at = found._at(doc, start)
        if at is None:
            continue
        laid = at[0]
        for member, (head, dx, dy, tile) in enumerate(laid.drawn):
            offset = laid.positions[head] - block.position()
            line = text_layout.lineForTextPosition(offset)
            if not line.isValid():
                continue
            left = rect.left() + line.cursorToX(offset)[0]
            top = rect.top() + line.y() + line.ascent() - laid.sizes[head][1]
            out.append((laid.positions[member], laid.units[member],
                        QRectF(left + dx, top + dy, tile.width, tile.height)))
    return out


def picture_boxes(doc: QTextDocument) -> List[Tuple[int, str, QRectF]]:
    """Where each picture of the groups of ``doc`` is drawn, in the
    document's coordinates: (position, name, box), in the author's order.
    (For the checks that a picture shows whole.)"""
    found = _handler(doc)
    if found is None:
        return []
    out = []
    block = doc.begin()
    while block.isValid():
        if photo_group.is_group_block(block):
            out.extend((position, unit.name, box)
                       for position, unit, box in _boxes_of(doc, found, block))
        block = block.next()
    return out


def group_boxes(doc: QTextDocument, position: int) -> List[Tuple[int, QRectF]]:
    """Where each picture of the group whose paragraph holds ``position``
    is drawn: (its position, its box), in the author's order."""
    found = _handler(doc)
    block = doc.findBlock(position)
    if found is None or not block.isValid() or not photo_group.is_group_block(block):
        return []
    return [(at, box) for at, _unit, box in _boxes_of(doc, found, block)]


def caption_box(doc: QTextDocument, position: int) -> Optional[QRectF]:
    """Where the caption of the group picture at ``position`` is drawn,
    under it; for a picture without one, the line where one would start."""
    found = _handler(doc)
    box = picture_box(doc, position)
    at = found._at(doc, position) if found is not None and box is not None else None
    if at is None:
        return None
    laid, index = at
    tile = laid.drawn[index][3]
    gap = found._caption_gap(doc)
    height = max(tile.caption_height - gap, found._fonts(doc)[2])
    return QRectF(box.left() - (tile.x - tile.figure_x), box.bottom() + gap,
                  tile.figure_width, height)


def picture_box(doc: QTextDocument, position: int) -> Optional[QRectF]:
    """Where the group picture at ``position`` is drawn, or None."""
    found = _handler(doc)
    block = doc.findBlock(position)
    if found is None or not block.isValid() or not photo_group.is_group_block(block):
        return None
    return next((box for at, _unit, box in _boxes_of(doc, found, block) if at == position), None)


def picture_at(doc: QTextDocument, point, *,
               nearest: bool = False) -> Optional[Tuple[int, str]]:
    """The group picture drawn at ``point`` (the document's coordinates):
    its position and name, or None. ``nearest``: on no picture but in a
    group's paragraph (the margin beside a narrower picture, a gap), the
    picture nearest to it (EDITING 2.3)."""
    found = _handler(doc)
    if found is None:
        return None
    position = doc.documentLayout().hitTest(point, Qt.HitTestAccuracy.FuzzyHit)
    block = doc.findBlock(max(0, position))
    for near in (block, block.previous(), block.next()):
        if not near.isValid() or not photo_group.is_group_block(near):
            continue
        boxes = _boxes_of(doc, found, near)
        for at, unit, box in boxes:
            if box.contains(point):
                return at, unit.name
        if nearest and boxes and doc.documentLayout().blockBoundingRect(near).contains(point):
            at, unit, _box = min(boxes, key=lambda item: math.hypot(
                max(item[2].left() - point.x(), 0.0, point.x() - item[2].right()),
                max(item[2].top() - point.y(), 0.0, point.y() - item[2].bottom())))
            return at, unit.name
    return None


def picture_at_cursor(cursor: QTextCursor) -> Optional[Tuple[int, int, int, photo_group.Unit]]:
    """The group picture ``cursor`` stands at: the one it selects alone,
    or, with nothing selected, the one after the caret (at the end of the
    group, the last). (position, its place from 1, how many, the unit); None
    away from a group or with more selected."""
    if cursor.hasSelection() and cursor.selectionEnd() - cursor.selectionStart() != 1:
        return None
    position = cursor.selectionStart()
    # The cursor's own paragraph (a cursor is never asked for its
    # document: PySide would hand out a second owner of it). One selected
    # character that is a picture stands in the paragraph of both ends.
    block = cursor.block()
    if not photo_group.is_group_block(block) or not (
            block.position() <= position < block.position() + block.length()):
        return None
    places = [(start + index, unit) for start, units in photo_group.unit_stretches(block)
              for index, unit in enumerate(units)]
    if not places:
        return None
    if cursor.hasSelection():
        k = next((k for k, (at, _unit) in enumerate(places) if at == position), None)
        if k is None:
            return None
    else:
        k = next((k for k, (at, _unit) in enumerate(places) if at >= position), len(places) - 1)
    return places[k][0], k + 1, len(places), places[k][1]


def set_room_above(doc: QTextDocument, pixels: float) -> None:
    """Keep ``pixels`` free above every group of ``doc`` (the layout's
    pixels): where a view shows something of its own over a group."""
    found = _handler(doc)
    if found is not None and found.room_above != pixels:
        found.room_above = pixels
        found.forget()


def arrange(doc: QTextDocument, units) -> Optional[Arranged]:
    """Where ``units`` would stand as one group in ``doc``, at its width
    (nothing in the document changes); None for a document no view shows."""
    found = _handler(doc)
    return found.arrange(doc, list(units)) if found is not None else None


def draw_unit(painter, doc: QTextDocument, unit, box: QRectF,
              figure: Tuple[float, float] = (0.0, 0.0)) -> None:
    """Draw ``unit``'s picture in ``box`` (and its caption under it, in
    the figure given), as its group draws it."""
    found = _handler(doc)
    if found is not None:
        found.draw(painter, doc, unit, box, figure)


def is_narrow(doc: QTextDocument) -> bool:
    """Whether ``doc``'s groups show as a narrow column's runs (one
    picture under another, mostly), not as the mosaic."""
    found = _handler(doc)
    return found is not None and found.metrics(doc).narrow


def own_row(doc: QTextDocument, unit) -> str:
    """Why ``unit``'s picture has a row of its own in any group, without
    being asked to (#wide): "panorama" for one as wide as the engine's
    panoramas, "tall" for a very tall one, "" for any other picture (or
    one whose size is not known yet)."""
    found = _handler(doc)
    size = found.sizes(unit.name) if found is not None and found.sizes and unit.name else None
    if size is None:
        return ""
    ratio = photo_layout.ratio_of({"w": size[0], "h": size[1]})
    if ratio >= photo_layout.CONSTANTS["panorama"]:
        return "panorama"
    return "tall" if ratio <= photo_layout.CONSTANTS["tall"] else ""


def picture_words(found: Optional[Tuple[int, int, int, photo_group.Unit]]) -> Optional[str]:
    """What a screen reader says for a group picture (picture_at_cursor):
    its place, its description and its caption, "Photo 2 of 5. Loch. The
    loch at dusk", as the exported page's viewer says it. (The text holds
    a picture as one character, which a screen reader cannot read, and a
    caption is drawn with its picture.)"""
    if found is None:
        return None
    _position, k, n, unit = found
    words = _("Photo {k} of {n}").format(k=k, n=n)
    return ". ".join(part for part in (words, unit.description, unit.caption_text.strip())
                     if part)
