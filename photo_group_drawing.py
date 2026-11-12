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

from PySide6.QtCore import QPointF, QRectF, QSizeF, Qt, QUrl
from PySide6.QtGui import (
    QFont,
    QFontMetricsF,
    QGuiApplication,
    QImage,
    QPagedPaintDevice,
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
# The height a #wide picture's "Enlarge" place holds beside its caption on
# a reader's page, in CSS pixels: article.css's .e21-pg-zoom, at least
# 44 pixels high, 4 of them over the caption's line.
ZOOM_HEIGHT = 40.0
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


def _font_pixels(font: QFont, dpi: float) -> float:
    """The font's size in the pixels of a device of ``dpi`` logical dots
    per inch (a point is 1/72 inch)."""
    if font.pixelSize() > 0:
        return float(font.pixelSize())
    return font.pointSizeF() * dpi / 72.0


def _text_dpi() -> float:
    """The dots per inch Qt's text layout reads a document's own lengths
    (a margin) in before it turns them into a device's pixels: the
    screen's logical ones (qt_defaultDpiY), 72 on a Mac and 96 on other
    systems, or 96 when the app asks for that."""
    app = QGuiApplication.instance()
    if app is not None and app.testAttribute(Qt.ApplicationAttribute.AA_Use96Dpi):
        return 96.0
    screen = QGuiApplication.primaryScreen()
    return float(round(screen.logicalDotsPerInchY())) if screen is not None else 100.0


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
        # The width a #wide picture's "Enlarge" place takes at the end of
        # its caption line on a reader's page, the gap before it included,
        # in CSS pixels: a preview holds it, so the rows below stand as on
        # the page. 0 where nothing holds it (the editor shows no such
        # control; it has its own chips).
        self.zoom_place = 0.0

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

    # Three measures, kept apart: a Mac lays text out at 72 logical dots
    # per inch and other systems at 96, so a rule that takes 96 for every
    # screen made the rows on a Mac a quarter too small, and 2 pixels too
    # wide for their line, where Qt put the last picture on a row of its own.

    def _dpi(self, doc) -> float:
        """The logical dots per inch of the device the document is laid
        out for (the editor's view, a page)."""
        device = doc.documentLayout().paintDevice()
        return float(device.logicalDpiY()) if device is not None else _text_dpi()

    def _length_scale(self, doc) -> float:
        """The layout's pixels per pixel of the document's own lengths (its
        margins), as Qt's text layout turns them: 1 on a screen."""
        return self._dpi(doc) / _text_dpi()

    def _scale(self, doc) -> float:
        """The layout's pixels per CSS pixel. On a screen a CSS pixel is a
        device-independent pixel, as in a browser, which is what the
        editor lays out in on a Mac (72 logical dots per inch) and on
        other systems (96) alike; on paper it is 1/96 inch."""
        device = doc.documentLayout().paintDevice()
        if isinstance(device, QPagedPaintDevice):
            return device.logicalDpiY() / 96.0
        return self._length_scale(doc)

    def room(self, doc) -> float:
        """The width a row has: the text's, inside the document's margins."""
        width = doc.textWidth()
        if width <= 0:
            width = doc.pageSize().width()
        # The root frame's margins hold the document's margin.
        root = doc.rootFrame().frameFormat()
        return width - (root.leftMargin() + root.rightMargin()
                        + 2 * (root.padding() + root.border())) * self._length_scale(doc)

    def _fonts(self, doc) -> Tuple[QFont, QFont, float]:
        """The text's font, a caption's (the text's, in italic) and the
        height of one caption line, on the device the document is laid
        out for."""
        font = doc.defaultFont()
        dpi = self._dpi(doc)
        found = self._caption_font
        if found is None or found[0] != font or found[3] != dpi:
            caption = QFont(font)
            caption.setItalic(True)
            device = doc.documentLayout().paintDevice()
            metrics = (QFontMetricsF(caption, device) if device is not None
                       else QFontMetricsF(caption))
            found = self._caption_font = (QFont(font), caption, metrics.lineSpacing(), dpi)
            self._captions.clear()
        return found[:3]

    def _caption_gap(self, doc) -> float:
        return round(CAPTION_GAP_EM * _font_pixels(doc.defaultFont(), self._dpi(doc)), 2)

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
                 doc.defaultFont(), self.room_above, self.zoom_place)
        if valid != self._valid:
            self._valid = valid
            self._laid = {}
            self._measures = (self.metrics(doc), self._length_scale(doc), self._caption_gap(doc),
                              GROUP_MARGIN_EM * _font_pixels(doc.defaultFont(), self._dpi(doc)))

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
        metrics, _lengths, gap, _margin = self._measures
        items = []
        for unit in units:
            size = self.sizes(unit.name) if self.sizes and unit.name else None
            if note:
                self._sized[unit.number] = size
            items.append(geometry.item(size, unit.caption_text, unit.wide))

        def caption_height(index: int, width: float) -> float:
            unit = units[index]
            place = self._zoom_place(doc, unit)
            words = (self._caption(doc, unit, width - place)[1] + gap if unit.has_caption
                     else 0.0)
            return max(words, ZOOM_HEIGHT * self._scale(doc)) if place else words

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

    def _zoom_place(self, doc, unit) -> float:
        """The width ``unit``'s "Enlarge" place takes beside its caption."""
        return self.zoom_place * self._scale(doc) if unit.wide else 0.0

    def draw(self, painter, doc, unit, box: QRectF, figure: Tuple[float, float]) -> None:
        """``unit``'s picture in ``box``, its caption under it in a figure
        from ``figure[0]`` and ``figure[1]`` wide (beside the "Enlarge"
        place, which is held, not drawn)."""
        self._draw_picture(painter, box, doc, unit)
        if unit.has_caption and figure[1] > 0:
            layout, _height = self._caption(doc, unit, figure[1] - self._zoom_place(doc, unit))
            layout.draw(painter, QPointF(figure[0], box.bottom() + self._caption_gap(doc)))

    def _lay_out(self, doc, block, start: int, units) -> _Laid:
        metrics, lengths, gap, margin = self._measures
        rows = self._rows(doc, units, note=True)
        boxes = geometry.object_boxes(rows, len(units), metrics)
        # The room above and below it that its paragraph's own margins do
        # not give already.
        fmt = block.blockFormat()
        above = max(0.0, max(margin, self.room_above) - fmt.topMargin() * lengths)
        below = max(0.0, margin - fmt.bottomMargin() * lengths)
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
