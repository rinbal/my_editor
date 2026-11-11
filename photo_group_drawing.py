# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The drawing object of photo groups: the handler a document's layout
asks to size and draw its pictures (photo_group_view.install puts it in
place, and asks it where each picture is).

It gives each picture of a group the box that makes Qt's own line
breaking lay the paragraph out as the rows STANDUP's engine arranges
(photo_group_geometry): a row fills its line, so the next picture starts
a new one, and Qt breaks a page between two rows, never through a
picture. It draws each picture in its box, its caption under it, and
hands every other picture to Qt's own handler.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple

from PySide6.QtCore import QPointF, QRectF, QSizeF, QUrl
from PySide6.QtGui import (
    QFont,
    QFontMetricsF,
    QImage,
    QPixmap,
    QPyTextObject,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QTextLayout,
    QTextOption,
)

import photo_group
import photo_group_geometry as geometry
from doc_walk import OBJECT_REPLACEMENT, doc_length


class CaptionMark(NamedTuple):
    """Words of a group picture's caption painted over, as a highlight is
    over the text (a match of Find, a misspelling): the picture by a
    cursor at it (the document moves it with each edit), the words by
    their UTF-16 offsets in the caption."""

    cursor: QTextCursor
    start: int
    end: int
    format: QTextCharFormat

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
# Stand-ins and sizes                                                          #
# --------------------------------------------------------------------------- #

def mark_placeholder(image: QImage) -> QImage:
    """``image``, marked as a stand-in: its shape is not the picture's."""
    image.setText(PLACEHOLDER_KEY, "1")
    return image


def is_placeholder(image) -> bool:
    return isinstance(image, QImage) and image.text(PLACEHOLDER_KEY) == "1"


# The size of a picture by its name in the document (its address, #wide
# included), or None while it is not known.
SizeLookup = Callable[[str], Optional[Tuple[int, int]]]


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


class Handler(QPyTextObject):
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
        # The words painted over in captions (set_marks).
        self.marks: Sequence[CaptionMark] = ()
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
                                             target.bottom() + self._caption_gap(doc)),
                            self._marked(laid.positions[member]))

    def _marked(self, position: int) -> List[QTextLayout.FormatRange]:
        """The marks on the caption of the picture at ``position``."""
        out = []
        for mark in self.marks:
            if mark.cursor.position() == position:
                item = QTextLayout.FormatRange()
                item.start, item.length, item.format = mark.start, mark.end - mark.start, mark.format
                out.append(item)
        return out

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
