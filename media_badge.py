# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Where a picture, audio or video of the text is: on this computer only,
on its way to the media servers, failed to get there, or there.

A small badge on the object's top left corner says it, as Photos marks a
picture still to be uploaded to iCloud: a glyph and a few words on a dark
capsule that reads on any picture, in light and dark alike. The view's
owner answers which badge an object wears (``badge_of``, by the name of
its image format); this module only finds the objects in sight and draws.
Only painted, never put into the document, so nothing of it is saved,
printed or published.

The editor calls ``paint`` from its paintEvent, over the text, and
``object_at`` for what is under the pointer (its tooltip, its context
menu).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterator, Optional, Tuple

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen, QTextCharFormat, QTextCursor,
)

LOCAL = "local"
UPLOADING = "uploading"
FAILED = "failed"
UPLOADED = "uploaded"


@dataclass(frozen=True)
class Badge:
    """``place`` one of the four above; ``words`` on the badge; ``hint``
    the object's tooltip, a sentence on what it means."""

    place: str
    words: str
    hint: str = ""


BadgeOf = Callable[[str], Optional[Badge]]

# Objects smaller than this (an icon in a line of text) wear no badge: it
# would cover them. They still have a tooltip.
MIN_WIDTH = 96
MIN_HEIGHT = 48

_INSET = 8          # from the object's corner
_HEIGHT = 20        # the capsule's height, at least
_PAD = 7            # room inside the capsule, left and right
_POINTS = (8.0, 11.0)   # the words' size: the text's, smaller, within these
_GLYPH = 12         # the glyph's box
_GAP = 5            # between glyph and words

_FILL = QColor(0, 0, 0, 150)
_FAILED_FILL = QColor(196, 43, 28, 225)
_INK = QColor(255, 255, 255)


def objects_in_sight(view) -> Iterator[Tuple[str, QRectF]]:
    """Each object of the text (an image format: a picture, an audio or
    video card) in sight of ``view`` (a QTextEdit): its name, and where it
    stands in the viewport."""
    height = view.viewport().height()
    layout = view.document().documentLayout()
    dx, dy = view.horizontalScrollBar().value(), view.verticalScrollBar().value()
    block = view.cursorForPosition(QPoint(0, 0)).block()
    while block.isValid():
        top = layout.blockBoundingRect(block).top()
        if top - dy > height and QTextCursor(block).currentTable() is None:
            # Below the view. Not in a table: its cells' paragraphs come
            # cell after cell, so one below the view in a tall cell may be
            # followed by one in sight in the next cell of the row.
            break
        it = block.begin()
        while not it.atEnd():
            fragment = it.fragment()
            fmt = fragment.charFormat()
            if fragment.isValid() and fmt.isImageFormat():
                box = _object_box(layout, block, fragment.position() - block.position(),
                                  fmt.toImageFormat())
                if box is not None:
                    yield fmt.toImageFormat().name(), box.translated(-dx, -dy)
            it += 1
        block = block.next()


def _object_box(layout, block, offset: int, image) -> Optional[QRectF]:
    """Where the object at ``offset`` in ``block`` stands, in document
    coordinates (a block's own layout is placed within its frame, a table
    cell's within the cell, so the document layout says where it is). It
    sits on its line's baseline, as Qt lays it out: its width the room it
    takes in the line (either way, in right-to-left text too), its height
    its own (or, with none set, the line's ascent, which an object taller
    than the text sets). One aligned otherwise takes its line's height."""
    line = block.layout().lineForTextPosition(offset)
    if not line.isValid():
        return None
    start, end = line.cursorToX(offset)[0], line.cursorToX(offset + 1)[0]
    width = image.width() if image.width() > 0 else abs(end - start)
    origin = layout.blockBoundingRect(block).topLeft()
    if image.verticalAlignment() not in (QTextCharFormat.VerticalAlignment.AlignNormal,
                                          QTextCharFormat.VerticalAlignment.AlignBaseline):
        return QRectF(origin.x() + min(start, end), origin.y() + line.y(), width, line.height())
    height = image.height() if image.height() > 0 else line.ascent()
    baseline = origin.y() + line.y() + line.ascent()
    return QRectF(origin.x() + min(start, end), baseline - height, width, height)


def object_at(view, pos: QPoint) -> Optional[str]:
    """The name of the object under ``pos`` (viewport coordinates)."""
    point = QPointF(pos)
    for name, box in objects_in_sight(view):
        if box.contains(point):
            return name
    return None


def paint(view, painter: QPainter, badge_of: BadgeOf) -> None:
    """A badge on each object in sight of ``view`` that ``badge_of``
    answers one for."""
    font = QFont(view.font())
    font.setPointSizeF(min(max(font.pointSizeF() * 0.72, _POINTS[0]), _POINTS[1]))
    font.setWeight(QFont.Weight.DemiBold)
    metrics = QFontMetricsF(font)
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setFont(font)
    for name, box in objects_in_sight(view):
        if box.width() < MIN_WIDTH or box.height() < MIN_HEIGHT:
            continue
        badge = badge_of(name)
        if badge is not None:
            _draw(painter, box, badge, metrics)
    painter.restore()


def _draw(painter: QPainter, box: QRectF, badge: Badge, metrics: QFontMetricsF) -> None:
    words = badge.words
    width = _PAD * 2 + _GLYPH + (_GAP + metrics.horizontalAdvance(words) if words else 0)
    if width > box.width() - 2 * _INSET:
        words, width = "", _PAD * 2 + _GLYPH          # too narrow for words: the glyph
    height = max(_HEIGHT, metrics.height() + 6)
    capsule = QRectF(box.left() + _INSET, box.top() + _INSET, width, height)
    painter.setPen(Qt.PenStyle.NoPen)
    from nostr.ui.appearance import roles
    fill = (_FAILED_FILL if badge.place == FAILED else QColor(roles(True).button)
            if badge.place in (UPLOADING, UPLOADED) else _FILL)
    painter.setBrush(fill)
    painter.drawRoundedRect(capsule, height / 2, height / 2)
    glyph = QRectF(capsule.left() + _PAD, capsule.center().y() - _GLYPH / 2, _GLYPH, _GLYPH)
    _GLYPHS[badge.place](painter, glyph)
    if words:
        painter.setPen(_INK)
        text = QRectF(glyph.right() + _GAP, capsule.top(), capsule.right() - glyph.right(),
                      capsule.height())
        painter.drawText(text, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, words)


def _pen(width: float = 1.4) -> QPen:
    pen = QPen(_INK, width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return pen


def _computer(painter: QPainter, r: QRectF) -> None:
    """A laptop: on this computer."""
    painter.setPen(_pen())
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRoundedRect(QRectF(r.left() + 1.5, r.top() + 1.5, r.width() - 3,
                                   r.height() * 0.58), 1.5, 1.5)
    y = r.top() + r.height() * 0.58 + 3.5
    painter.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))


def _arrow_up(painter: QPainter, r: QRectF) -> None:
    """An arrow going up: on its way."""
    painter.setPen(_pen(1.6))
    cx = r.center().x()
    painter.drawLine(QPointF(cx, r.bottom() - 1), QPointF(cx, r.top() + 1))
    painter.drawLine(QPointF(cx - 4, r.top() + 5), QPointF(cx, r.top() + 1))
    painter.drawLine(QPointF(cx + 4, r.top() + 5), QPointF(cx, r.top() + 1))


def _exclamation(painter: QPainter, r: QRectF) -> None:
    """An exclamation mark: it could not get there."""
    painter.setPen(_pen(1.8))
    cx = r.center().x()
    painter.drawLine(QPointF(cx, r.top() + 1), QPointF(cx, r.bottom() - 4.5))
    painter.drawPoint(QPointF(cx, r.bottom() - 1))


def _cloud(painter: QPainter, r: QRectF) -> None:
    """A cloud: on the media servers."""
    def circle(cx: float, cy: float, radius: float) -> QPainterPath:
        path = QPainterPath()
        path.addEllipse(QPointF(r.left() + cx, r.top() + cy), radius, radius)
        return path

    base = QPainterPath()
    base.addRect(QRectF(r.left() + 3, r.top() + 7, 6.5, 3))
    cloud = base.united(circle(3, 7.5, 2.5)).united(circle(6.3, 5.2, 3.3)) \
        .united(circle(9.4, 7.3, 2.7))
    painter.setPen(_pen(1.3))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawPath(cloud.simplified())


_GLYPHS = {LOCAL: _computer, UPLOADING: _arrow_up, FAILED: _exclamation, UPLOADED: _cloud}
