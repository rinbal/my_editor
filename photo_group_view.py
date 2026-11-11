# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Photo groups as a view shows them: sized, drawn and found by the layout.

A group is one paragraph of picture characters (photo_group). Here a
handler for the document's pictures (install) gives each picture of a
group the box that makes Qt's own line breaking lay the paragraph out as
the rows STANDUP's engine arranges (photo_group_geometry): a row fills its
line, so the next picture starts a new one, and Qt breaks a page between
two rows, never through a picture. The handler draws each picture in its
box, its caption under it.

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

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from PySide6.QtCore import QPointF, QRectF, QSizeF, Qt, QUrl
from PySide6.QtGui import (
    QFont,
    QFontMetricsF,
    QImage,
    QPixmap,
    QPyTextObject,
    QTextCursor,
    QTextDocument,
    QTextFormat,
    QTextLayout,
    QTextOption,
)

import photo_group
import photo_group_geometry as geometry
import photo_layout
from doc_walk import OBJECT_REPLACEMENT, doc_length
from i18n import _

IMAGE_OBJECT = int(QTextFormat.ObjectTypes.ImageObject)

# A caption sits this share of the text's font size under its picture (the
# readers: 0.6em of a caption at 0.833em).
CAPTION_GAP_EM = 0.5
# Room above and below a group, in the text's font size: a group stands
# apart from the paragraphs around it, as a figure does (the readers give
# it 2em of their larger type).
GROUP_MARGIN_EM = 1.0

# The text key a stand-in picture carries (a grey box drawn while the
# picture is not at hand): its shape is not the picture's.
PLACEHOLDER_KEY = "myeditor-placeholder"


# --------------------------------------------------------------------------- #
# The sizes a surface has                                                      #
# --------------------------------------------------------------------------- #

def mark_placeholder(image: QImage) -> QImage:
    """``image``, marked as a stand-in: its shape is not the picture's."""
    image.setText(PLACEHOLDER_KEY, "1")
    return image


def is_placeholder(image) -> bool:
    return isinstance(image, QImage) and image.text(PLACEHOLDER_KEY) == "1"


def picture_size(image) -> Optional[Tuple[int, int]]:
    """The pixel size of a picture at hand (a QImage or QPixmap), or None
    for a stand-in or nothing. The engine decides which sizes it takes."""
    if isinstance(image, QPixmap):
        image = image.toImage()
    if not isinstance(image, QImage) or image.isNull() or is_placeholder(image):
        return None
    return photo_layout.picture_size(image.width(), image.height())


# The size of a picture by its name in the document (its address, #wide
# included), or None while it is not known.
SizeLookup = Callable[[str], Optional[Tuple[int, int]]]


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


# --------------------------------------------------------------------------- #
# A group laid out                                                             #
# --------------------------------------------------------------------------- #

@dataclass
class Arranged:
    """A list of pictures as one group at a width, before it is one (a
    picture being dragged to another place): each picture's box and its
    figure's left edge and width, from the group's top left corner, and
    the group's width and height."""

    boxes: List[QRectF]
    figures: List[Tuple[float, float]]
    width: float
    height: float


@dataclass
class _Laid:
    """One group at the width it was laid out for: the place of each of
    its pictures' characters, the units, the box of each character, and
    for each picture the character that draws it and where in that box."""

    positions: List[int]
    units: List[photo_group.Unit]
    sizes: List[Tuple[float, float]]
    drawn: List[Tuple[int, float, float, geometry.Tile]]


def _font_pixels(font: QFont, scale: float) -> float:
    if font.pixelSize() > 0:
        return float(font.pixelSize())
    return font.pointSizeF() * 96.0 / 72.0 * scale


def _fitted(target: QRectF, image) -> QRectF:
    """``target`` narrowed to its picture's own shape, centred in it. The
    engine lays a picture out at 3:2 when it does not take its size (a
    shape past its limits, photo_layout.picture_size): it is drawn whole
    inside that box, never stretched to it."""
    if (image is None or is_placeholder(image)
            or target.width() <= 0 or target.height() <= 0):
        return target
    ratio = image.width() / image.height()
    room = target.width() / target.height()
    if abs(ratio - room) <= 0.01 * ratio:
        return target
    if ratio > room:
        height = target.width() / ratio
        return QRectF(target.x(), target.y() + (target.height() - height) / 2,
                      target.width(), height)
    width = target.height() * ratio
    return QRectF(target.x() + (target.width() - width) / 2, target.y(), width, target.height())


class _Handler(QPyTextObject):
    """Sizes and draws the pictures of a group; every other picture goes
    to Qt's own handler, as before."""

    def __init__(self, layout, original, sizes: Optional[SizeLookup]) -> None:
        super().__init__(layout)
        self._original = original
        self.sizes = sizes
        # The height of the screen (or page) the document is read on, in
        # the layout's pixels: no row is taller than its share of it.
        self.screen_height: Optional[float] = None
        # The groups laid out, by a picture's place; for one state of the
        # document, one width, one height and one font of the text (_valid).
        self._valid: Optional[tuple] = None
        self._laid: Dict[int, Tuple[_Laid, int]] = {}
        # What every group is laid out with in that state: the metrics,
        # the device's scale, a caption's gap and a group's margin.
        self._measures: tuple = ()
        # The size each picture was last laid out with, by its unit's
        # number: what picture_arrived compares a new size with.
        self._sized: Dict[int, Optional[Tuple[int, int]]] = {}
        # Where the groups showing each picture are, for one state of the
        # document (picture_arrived).
        self._showing: Optional[Tuple[int, Dict[str, List[int]]]] = None
        # A caption laid out, by its runs and its width.
        self._captions: Dict[tuple, Tuple[QTextLayout, float]] = {}
        self._caption_font: Optional[tuple] = None
        # Room a view keeps free above every group, in the layout's pixels
        # (the editor's bar over a group floats there): at least this much.
        self.room_above = 0.0

    # Qt asks for each object's size whenever it lays its line out (also
    # after the width changed), and has it drawn into the box it gave.

    def intrinsicSize(self, doc, position, fmt):
        if fmt.hasProperty(photo_group.UNIT):
            found = self._at(doc, position)
            if found is not None:
                laid, index = found
                return QSizeF(*laid.sizes[index])
        if self._original is not None:
            return self._original.intrinsicSize(doc, position, fmt)
        return QSizeF(0, 0)

    def drawObject(self, painter, rect, doc, position, fmt):
        found = self._at(doc, position) if fmt.hasProperty(photo_group.UNIT) else None
        if found is None:
            if self._original is not None:
                self._original.drawObject(painter, rect, doc, position, fmt)
            return
        laid, index = found
        for member, (head, dx, dy, tile) in enumerate(laid.drawn):
            if head != index:
                continue
            unit = laid.units[member]
            target = QRectF(rect.x() + dx, rect.y() + dy, tile.width, tile.height)
            self._draw_picture(painter, target, doc, unit)
            if unit.has_caption:
                layout, _height = self._caption(doc, unit, tile.figure_width)
                layout.draw(painter, QPointF(rect.x() + dx - (tile.x - tile.figure_x),
                                             target.bottom() + self._caption_gap(doc)))

    def _draw_picture(self, painter, target: QRectF, doc, unit) -> None:
        if not unit.name:
            return
        image = doc.resource(QTextDocument.ResourceType.ImageResource.value, QUrl(unit.name))
        if isinstance(image, QPixmap):
            painter.drawPixmap(_fitted(target, image), image, QRectF(image.rect()))
        elif isinstance(image, QImage) and not image.isNull():
            painter.drawImage(_fitted(target, image), image)

    # ---- the room and the fonts ----

    def _scale(self, doc) -> float:
        device = doc.documentLayout().paintDevice()
        return device.logicalDpiY() / 96.0 if device is not None else 1.0

    def room(self, doc) -> float:
        """The width a row has: the text's, inside the document's margins."""
        width = doc.textWidth()
        if width <= 0:
            width = doc.pageSize().width()
        # The root frame's margins hold the document's margin.
        root = doc.rootFrame().frameFormat()
        return width - (root.leftMargin() + root.rightMargin()
                        + 2 * (root.padding() + root.border())) * self._scale(doc)

    def _fonts(self, doc) -> Tuple[QFont, QFont, float]:
        """The text's font, a caption's (the text's, in italic) and the
        height of one caption line, on the device the document is laid
        out for."""
        font = doc.defaultFont()
        scale = self._scale(doc)
        found = self._caption_font
        if found is None or found[0] != font or found[3] != scale:
            caption = QFont(font)
            caption.setItalic(True)
            device = doc.documentLayout().paintDevice()
            metrics = (QFontMetricsF(caption, device) if device is not None
                       else QFontMetricsF(caption))
            found = self._caption_font = (QFont(font), caption, metrics.lineSpacing(), scale)
            self._captions.clear()
        return found[:3]

    def _caption_gap(self, doc) -> float:
        return round(CAPTION_GAP_EM * _font_pixels(doc.defaultFont(), self._scale(doc)), 2)

    def metrics(self, doc) -> geometry.Metrics:
        line = self._caption_gap(doc) + self._fonts(doc)[2]
        return geometry.metrics_at(self.room(doc), self._scale(doc), self.screen_height, line)

    def _caption(self, doc, unit, width: float) -> Tuple[QTextLayout, float]:
        """``unit``'s caption laid out in a figure ``width`` wide, and its
        height."""
        _font, caption_font, _line = self._fonts(doc)
        key = (unit.caption, round(width))
        found = self._captions.get(key)
        if found is not None:
            return found
        device = doc.documentLayout().paintDevice()
        layout = (QTextLayout(unit.caption_text, caption_font, device) if device is not None
                  else QTextLayout(unit.caption_text, caption_font))
        option = QTextOption()
        option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        layout.setTextOption(option)
        ranges = []
        start = 0
        for run in unit.caption:
            item = QTextLayout.FormatRange()
            item.start = start
            item.length = doc_length(run.text)
            item.format = run.format()
            ranges.append(item)
            start += item.length
        layout.setFormats(ranges)
        height = 0.0
        layout.beginLayout()
        while True:
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(max(1.0, width))
            line.setPosition(QPointF(0, height))
            height += line.height()
        layout.endLayout()
        if len(self._captions) >= 2048:
            self._captions.clear()
        self._captions[key] = (layout, height)
        return layout, height

    # ---- the groups laid out ----

    def _measure(self, doc) -> None:
        """What the groups are laid out with, for the document as it is."""
        valid = (doc.revision(), doc.textWidth(), doc.pageSize().width(), self.screen_height,
                 doc.defaultFont(), self.room_above)
        if valid != self._valid:
            self._valid = valid
            self._laid = {}
            scale = self._scale(doc)
            self._measures = (self.metrics(doc), scale, self._caption_gap(doc),
                              GROUP_MARGIN_EM * _font_pixels(doc.defaultFont(), scale))

    def _at(self, doc, position: int) -> Optional[Tuple[_Laid, int]]:
        """The group laid out around the picture at ``position``, and the
        picture's number in it."""
        self._measure(doc)
        found = self._laid.get(position)
        if found is None:
            block = doc.findBlock(position)
            for start, units in photo_group.unit_stretches(block):
                laid = self._lay_out(doc, block, start, units)
                for index, at in enumerate(laid.positions):
                    self._laid[at] = (laid, index)
            found = self._laid.get(position)
        return found

    def _rows(self, doc, units, *, note: bool) -> List[geometry.Row]:
        """The rows ``units`` stand in at the document's width; with
        ``note``, the size each was laid out with is kept (picture_arrived)."""
        metrics, _scale, gap, _margin = self._measures
        items = []
        for unit in units:
            size = self.sizes(unit.name) if self.sizes and unit.name else None
            if note:
                self._sized[unit.number] = size
            items.append(geometry.item(size, unit.caption_text, unit.wide))

        def caption_height(index: int, width: float) -> float:
            unit = units[index]
            return self._caption(doc, unit, width)[1] + gap if unit.has_caption else 0.0

        return geometry.group_rows(items, metrics, caption_height)

    def arrange(self, doc, units) -> "Arranged":
        """Where the pictures of ``units`` would stand as one group at
        the document's width, from the group's top left corner."""
        self._measure(doc)
        metrics = self._measures[0]
        rows = self._rows(doc, units, note=False)
        boxes: List[QRectF] = [QRectF()] * len(units)
        figures: List[Tuple[float, float]] = [(0.0, 0.0)] * len(units)
        top = 0.0
        for number, row in enumerate(rows):
            for tile in row.tiles:
                boxes[tile.index] = QRectF(tile.x, top + tile.y, tile.width, tile.height)
                figures[tile.index] = (tile.figure_x, tile.figure_width)
            top += row.height + (metrics.gap if number < len(rows) - 1 else 0.0)
        return Arranged(boxes, figures, metrics.width, top)

    def draw(self, painter, doc, unit, box: QRectF, figure: Tuple[float, float]) -> None:
        """``unit``'s picture in ``box``, its caption under it in a figure
        from ``figure[0]`` and ``figure[1]`` wide."""
        self._draw_picture(painter, box, doc, unit)
        if unit.has_caption and figure[1] > 0:
            layout, _height = self._caption(doc, unit, figure[1])
            layout.draw(painter, QPointF(figure[0], box.bottom() + self._caption_gap(doc)))

    def _lay_out(self, doc, block, start: int, units) -> _Laid:
        metrics, scale, gap, margin = self._measures
        rows = self._rows(doc, units, note=True)
        boxes = geometry.object_boxes(rows, len(units), metrics)
        # The room above and below it that its paragraph's own margins do
        # not give already.
        fmt = block.blockFormat()
        above = max(0.0, max(margin, self.room_above) - fmt.topMargin() * scale)
        below = max(0.0, margin - fmt.bottomMargin() * scale)
        first = {cell[0] for cell in rows[0].cells} if rows else set()
        last = {cell[0] for cell in rows[-1].cells} if rows else set()
        sizes = []
        drawn: List[Optional[Tuple[int, float, float, geometry.Tile]]] = [None] * len(units)
        for index, box in enumerate(boxes):
            top = above if index in first else 0.0
            height = box.height + top + (below if index in last else 0.0)
            sizes.append((box.width, height if box.tiles else 0.0))
            for tile, left in box.tiles:
                drawn[tile.index] = (index, tile.x - left, top + tile.y, tile)
        return _Laid(list(range(start, start + len(units))), list(units), sizes, drawn)

    def forget(self, block=None) -> None:
        """The pictures' sizes changed (those of the group in ``block``,
        else any): measure again at the next layout."""
        if block is None:
            self._valid = None
            self._laid = {}
            return
        for position in range(block.position(), block.position() + block.length()):
            self._laid.pop(position, None)

    def showing(self, doc, name: str) -> List[int]:
        """Where the paragraphs that show the picture ``name`` start (a
        group's, or one a picture stands in by itself). Read once for a
        state of the document: pictures arrive one after another."""
        found = self._showing
        if found is None or found[0] != doc.revision():
            index: Dict[str, List[int]] = {}
            block = doc.begin()
            while block.isValid():
                if OBJECT_REPLACEMENT in block.text():
                    it = block.begin()
                    while not it.atEnd():
                        fmt = it.fragment().charFormat()
                        it += 1
                        if fmt.isImageFormat():
                            places = index.setdefault(fmt.toImageFormat().name(), [])
                            if not places or places[-1] != block.position():
                                places.append(block.position())
                block = block.next()
            found = self._showing = (doc.revision(), index)
        return found[1].get(name, [])


# Set on a document once install gave its layout a handler: a document
# that never had one (read for an export, a check or a copy) is not given
# a layout by asking, which would lay out every change made to it after.
_INSTALLED = "photoGroupHandler"


def _handler(doc) -> Optional[_Handler]:
    if not doc.property(_INSTALLED):
        return None
    found = doc.documentLayout().handlerForObject(IMAGE_OBJECT)
    return found if isinstance(found, _Handler) else None


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
    handler = _Handler(layout, original, sizes)
    layout.registerHandler(IMAGE_OBJECT, handler)
    doc.setProperty(_INSTALLED, True)


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

def _boxes_of(doc, found: _Handler, block) -> List[Tuple[int, photo_group.Unit, QRectF]]:
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


def picture_at(doc: QTextDocument, point) -> Optional[Tuple[int, str]]:
    """The group picture drawn at ``point`` (the document's coordinates):
    its position and name, or None."""
    found = _handler(doc)
    if found is None:
        return None
    position = doc.documentLayout().hitTest(point, Qt.HitTestAccuracy.FuzzyHit)
    block = doc.findBlock(max(0, position))
    for near in (block, block.previous(), block.next()):
        if not near.isValid() or not photo_group.is_group_block(near):
            continue
        for at, unit, box in _boxes_of(doc, found, near):
            if box.contains(point):
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
    return ". ".join(part for part in (words, unit.alt, unit.caption_text.strip()) if part)
