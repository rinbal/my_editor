# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the editor shows over a picture to edit it where it stands.

On the picture the pointer is over, or the one selected, four small chips
appear (EDITING.md 2.2 of the photo grid design): a handle to drag it by
(top left), "More" with every command (top right), "ALT" for its
description (bottom left) and "Large" (bottom right; "Wide" on a picture
that stands alone). Over a group, in the room kept free above it, a bar
names it and offers to add photos. While a picture is dragged, the group
it would land in is drawn in its new order, a line shows where it would
stand alone, and a translucent copy follows the pointer.

Like media_badge, all of it is only painted, never put into the document:
nothing of it is saved, printed or published, and showing it moves
nothing. This module knows where each thing goes (chips_on, bar_over) and
draws it; photo_group_editing decides what is shown and what a click
does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen, QPixmap,
)
from PySide6.QtWidgets import QApplication

HANDLE, MORE, ALT, BIG, ADD = "handle", "more", "alt", "big", "add"

# The room kept free above a group for its bar, and the bar's own height.
BAR_ROOM = 34.0
BAR_HEIGHT = 24.0
# A chip's height, its distance from the picture's corner, and how far
# around it a click still counts (a target of 30 points, as the system's
# own small buttons have).
CHIP = 24.0
INSET = 8.0
REACH = 3.0
_PAD = 8.0
_ICON = 12.0
_GAP = 4.0
# A picture lower than this shows only the handle and More (a panorama in
# a narrow window): four chips would cover it.
SHORT = 120.0
# The ring around the selected picture, and the frame around a group a
# dragged picture would land in.
RING = 3.0

_FILL = QColor(22, 22, 26, 178)
_FILL_HOT = QColor(22, 22, 26, 225)
_FILL_SOLID = QColor(0, 0, 0)
_MISSING = QColor(168, 78, 0, 235)
_INK = QColor(255, 255, 255)


def chip_font() -> QFont:
    font = QFont(QApplication.font())
    font.setPixelSize(11)
    font.setWeight(QFont.Weight.DemiBold)
    return font


def bar_font() -> QFont:
    font = QFont(QApplication.font())
    font.setPixelSize(12)
    font.setWeight(QFont.Weight.DemiBold)
    return font


@dataclass
class Chips:
    """The chips on one picture: each one's box, and its words (none for
    a chip that shows its glyph only)."""

    boxes: Dict[str, QRectF] = field(default_factory=dict)
    words: Dict[str, str] = field(default_factory=dict)

    def at(self, point: QPointF) -> Optional[str]:
        for name, box in self.boxes.items():
            if box.adjusted(-REACH, -REACH, REACH, REACH).contains(point):
                return name
        return None


def chips_on(box: QRectF, more: str, alt: str, big: str) -> Chips:
    """Where the chips go on a picture drawn in ``box``, with their words
    (``big`` empty: no such chip). A narrow picture shows glyphs without
    words, a low one only the handle and More, a tiny one only More."""
    metrics = QFontMetricsF(chip_font())
    chips = Chips()

    def width(words: str) -> float:
        return _PAD * 2 + _ICON + (_GAP + metrics.horizontalAdvance(words) if words else 0.0)

    if box.width() < CHIP + 2 * INSET or box.height() < CHIP + 2 * INSET:
        return chips
    top, bottom = box.top() + INSET, box.bottom() - INSET - CHIP
    room = box.width() - 2 * INSET
    if room >= 2 * CHIP + _GAP:
        chips.boxes[HANDLE] = QRectF(box.left() + INSET, top, CHIP, CHIP)
    words = more if room >= CHIP + _GAP + width(more) + 24 else ""
    wide = width(words) if words else CHIP
    chips.boxes[MORE] = QRectF(box.right() - INSET - wide, top, wide, CHIP)
    chips.words[MORE] = words
    if box.height() < SHORT or HANDLE not in chips.boxes:
        return chips
    alt_width = width(alt)
    if alt_width > room:
        return chips
    chips.boxes[ALT] = QRectF(box.left() + INSET, bottom, alt_width, CHIP)
    chips.words[ALT] = alt
    if big:
        words = big if room >= alt_width + _GAP + width(big) else ""
        wide = width(words) if words else CHIP
        if room >= alt_width + _GAP + wide:
            chips.boxes[BIG] = QRectF(box.right() - INSET - wide, bottom, wide, CHIP)
            chips.words[BIG] = words
    return chips


# --------------------------------------------------------------------------- #
# Glyphs                                                                       #
# --------------------------------------------------------------------------- #

def _pen(width: float = 1.5, color: QColor = _INK) -> QPen:
    pen = QPen(color, width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return pen


def _grip(painter: QPainter, r: QRectF) -> None:
    """Six dots: something to take hold of."""
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(_INK)
    for column in (-2.5, 2.5):
        for row in (-4.0, 0.0, 4.0):
            painter.drawEllipse(QPointF(r.center().x() + column, r.center().y() + row), 1.25, 1.25)


def _dots(painter: QPainter, r: QRectF) -> None:
    """Three dots: more."""
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(_INK)
    for column in (-4.5, 0.0, 4.5):
        painter.drawEllipse(QPointF(r.center().x() + column, r.center().y()), 1.4, 1.4)


def _check(painter: QPainter, r: QRectF) -> None:
    painter.setPen(_pen(1.8))
    c = r.center()
    painter.drawPolyline([QPointF(c.x() - 4, c.y()), QPointF(c.x() - 1, c.y() + 3),
                          QPointF(c.x() + 4.5, c.y() - 3.5)])


def _expand(painter: QPainter, r: QRectF) -> None:
    """Two arrows pulling apart: larger."""
    painter.setPen(_pen(1.5))
    c = r.center()
    for sign in (1, -1):
        tip = QPointF(c.x() + sign * 4.5, c.y() - sign * 4.5)
        painter.drawLine(QPointF(c.x() + sign * 1.2, c.y() - sign * 1.2), tip)
        painter.drawPolyline([QPointF(tip.x() - sign * 3.6, tip.y()), tip,
                              QPointF(tip.x(), tip.y() + sign * 3.6)])


def _plus(painter: QPainter, r: QRectF, color: QColor) -> None:
    painter.setPen(_pen(1.6, color))
    c = r.center()
    painter.drawLine(QPointF(c.x() - 4, c.y()), QPointF(c.x() + 4, c.y()))
    painter.drawLine(QPointF(c.x(), c.y() - 4), QPointF(c.x(), c.y() + 4))


def _pictures(painter: QPainter, r: QRectF, color: QColor) -> None:
    """Two pictures, one behind the other: a group of them."""
    painter.setPen(_pen(1.3, color))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRoundedRect(QRectF(r.left() + 3.5, r.top() + 1, r.width() - 4.5, r.height() - 4.5),
                            1.6, 1.6)
    path = QPainterPath()
    path.moveTo(r.left() + 1, r.top() + 3.5)
    path.lineTo(r.left() + 1, r.bottom() - 1)
    path.lineTo(r.right() - 3.5, r.bottom() - 1)
    painter.drawPath(path)


_GLYPHS = {HANDLE: _grip, MORE: _dots, BIG: _expand}


# --------------------------------------------------------------------------- #
# Painting                                                                     #
# --------------------------------------------------------------------------- #

def paint_chips(painter: QPainter, chips: Chips, *, missing: bool, big_on: bool,
                hot: Optional[str], accent: QColor, solid: bool) -> None:
    """The chips of one picture: ``missing`` for a picture without a
    description, ``big_on`` for one shown large, ``hot`` the chip under
    the pointer; ``solid`` when more contrast was asked for."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setFont(chip_font())
    for name, box in chips.boxes.items():
        fill = _FILL_SOLID if solid else _FILL_HOT if name == hot else _FILL
        if name == BIG and big_on:
            fill = QColor(accent)
        elif name == ALT and missing:
            fill = _MISSING
        painter.setPen(QPen(_INK, 1) if solid else Qt.PenStyle.NoPen)
        painter.setBrush(fill)
        painter.drawRoundedRect(box, CHIP / 2, CHIP / 2)
        words = chips.words.get(name, "")
        glyph_box = QRectF(box.left() + (_PAD if words else (box.width() - _ICON) / 2),
                           box.center().y() - _ICON / 2, _ICON, _ICON)
        text_left = glyph_box.right() + _GAP
        if name == ALT and missing:
            text_left = box.left() + _PAD + (_ICON + _GAP) / 2      # words alone, centred
        elif name == ALT:
            _check(painter, glyph_box)
        else:
            _GLYPHS[name](painter, glyph_box)
        if words:
            painter.setPen(_INK)
            painter.drawText(QRectF(text_left, box.top(), box.right() - text_left, box.height()),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, words)
    painter.restore()


@dataclass
class Bar:
    """The bar over a group: where it is, and its button to add photos."""

    box: QRectF
    add: QRectF

    def at(self, point: QPointF) -> Optional[str]:
        return ADD if self.add.adjusted(-REACH, -REACH, REACH, REACH).contains(point) else None


def bar_over(group: QRectF, add_words: str) -> Bar:
    """The bar in the room above a group drawn in ``group``."""
    box = QRectF(group.left(), group.top() - BAR_ROOM + 4, group.width(), BAR_HEIGHT)
    wide = _PAD * 2 + _ICON + _GAP + QFontMetricsF(bar_font()).horizontalAdvance(add_words)
    return Bar(box, QRectF(box.right() - wide, box.top() + 1, wide, BAR_HEIGHT - 2))


def paint_bar(painter: QPainter, bar: Bar, label: str, hint: str, add_words: str, *,
              ink: QColor, accent: QColor, hint_in_accent: bool, hot: bool,
              solid: bool) -> None:
    """A group's bar: what it is, a hint (in the accent colour while it
    says what letting go will do) and the button to add photos."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    font = bar_font()
    painter.setFont(font)
    metrics = QFontMetricsF(font)
    muted = QColor(ink)
    muted.setAlphaF(1.0 if solid else 0.62)
    box = bar.box
    glyph = QRectF(box.left(), box.center().y() - 7, 14, 14)
    _pictures(painter, glyph, muted)
    left = glyph.right() + 6
    flags = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
    room = bar.add.left() - 10 - left
    label = metrics.elidedText(label, Qt.TextElideMode.ElideRight, room)
    painter.setPen(muted)
    painter.drawText(QRectF(left, box.top(), room, box.height()), flags, label)
    left += metrics.horizontalAdvance(label) + 10
    if hint and bar.add.left() - 10 - left > 40:
        if hint_in_accent:
            painter.setPen(accent)
        else:
            font.setWeight(QFont.Weight.Normal)
            painter.setFont(font)
            faint = QColor(ink)
            faint.setAlphaF(0.8 if solid else 0.45)
            painter.setPen(faint)
        room = bar.add.left() - 10 - left
        painter.drawText(QRectF(left, box.top(), room, box.height()), flags,
                         QFontMetricsF(painter.font()).elidedText(
                             hint, Qt.TextElideMode.ElideRight, room))
    font.setWeight(QFont.Weight.DemiBold)
    painter.setFont(font)
    line = QColor(ink)
    line.setAlphaF(1.0 if solid else 0.28)
    fill = QColor(ink)
    fill.setAlphaF(0.10 if hot else 0.0)
    painter.setPen(QPen(line, 1))
    painter.setBrush(fill)
    add = bar.add.adjusted(0.5, 0.5, -0.5, -0.5)
    painter.drawRoundedRect(add, add.height() / 2, add.height() / 2)
    strong = QColor(ink)
    strong.setAlphaF(1.0 if solid else 0.85)
    _plus(painter, QRectF(add.left() + _PAD, add.center().y() - _ICON / 2, _ICON, _ICON), strong)
    painter.setPen(strong)
    painter.drawText(QRectF(add.left() + _PAD + _ICON + _GAP, add.top(), add.width(), add.height()),
                     flags, add_words)
    painter.restore()


def paint_ring(painter: QPainter, box: QRectF, accent: QColor, strength: float = 1.0) -> None:
    """The ring around the selected picture; ``strength`` above 1 while
    it flashes (what Undo brought back)."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    width = RING * max(1.0, strength)
    half = width / 2
    painter.setPen(QPen(accent, width))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRoundedRect(box.adjusted(half, half, -half, -half), 3, 3)
    if strength > 1.0:
        veil = QColor(accent)
        veil.setAlphaF(min(0.35, 0.35 * (strength - 1.0)))
        painter.fillRect(box, veil)
    painter.restore()


def paint_line(painter: QPainter, left: float, right: float, y: float, words: str, *,
               accent: QColor, solid: bool) -> None:
    """The line where a dragged picture would stand, with what that means
    in words on a capsule at its start."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(_pen(3.0 if solid else 2.0, accent))
    painter.drawLine(QPointF(left, y), QPointF(right, y))
    if words:
        painter.setFont(chip_font())
        wide = QFontMetricsF(chip_font()).horizontalAdvance(words) + 2 * _PAD
        capsule = QRectF(left, y - 10, wide, 20)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(accent)
        painter.drawRoundedRect(capsule, 10, 10)
        painter.setPen(_INK)
        painter.drawText(capsule, Qt.AlignmentFlag.AlignCenter, words)
    painter.restore()


def paint_placeholder(painter: QPainter, box: QRectF, words: str, *, accent: QColor) -> None:
    """The slot a dragged picture would take in a group: tinted and
    outlined in the accent colour, with ``words`` in it for a picture
    that is not in the document yet."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    tint = QColor(accent)
    tint.setAlphaF(0.16)
    painter.setPen(QPen(accent, 2))
    painter.setBrush(tint)
    painter.drawRoundedRect(box.adjusted(1, 1, -1, -1), 6, 6)
    if words:
        painter.setFont(bar_font())
        painter.setPen(accent)
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, words)
    painter.restore()


def paint_number(painter: QPainter, box: QRectF, number: int, *, accent: QColor) -> None:
    """A picture's place in the order shown while one is being moved."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    disc = QRectF(box.left() + INSET, box.top() + INSET, 20, 20)
    if disc.right() <= box.right() and disc.bottom() <= box.bottom():
        painter.setPen(QPen(_INK, 1.5))
        painter.setBrush(accent)
        painter.drawEllipse(disc)
        font = chip_font()
        font.setWeight(QFont.Weight.Bold)
        painter.setFont(font)
        painter.setPen(_INK)
        painter.drawText(disc, Qt.AlignmentFlag.AlignCenter, str(number))
    painter.restore()


def paint_frame(painter: QPainter, box: QRectF, accent: QColor) -> None:
    """The frame around the group a dragged picture would land in."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    frame = QColor(accent)
    frame.setAlphaF(0.55)
    painter.setPen(QPen(frame, 2))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRoundedRect(box, 10, 10)
    painter.restore()


def paint_ghost(painter: QPainter, pixmap: QPixmap, box: QRectF, words: str, *,
                accent: QColor) -> None:
    """The copy of a dragged picture that follows the pointer, see-through
    so the place under it shows, with where it would land in words."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    shadow = QColor(0, 0, 0, 40)
    for spread in (6.0, 3.0):
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(shadow)
        painter.drawRoundedRect(box.adjusted(-spread, -spread + 4, spread, spread + 4), 8, 8)
    clip = QPainterPath()
    clip.addRoundedRect(box, 6, 6)
    painter.setClipPath(clip)
    painter.setOpacity(0.78)
    painter.drawPixmap(box, pixmap, QRectF(pixmap.rect()))
    painter.setOpacity(1.0)
    painter.setClipping(False)
    if words:
        painter.setFont(chip_font())
        wide = QFontMetricsF(chip_font()).horizontalAdvance(words) + 2 * _PAD
        capsule = QRectF(box.center().x() - wide / 2, box.bottom() + 6, wide, 20)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(accent)
        painter.drawRoundedRect(capsule, 10, 10)
        painter.setPen(_INK)
        painter.drawText(capsule, Qt.AlignmentFlag.AlignCenter, words)
    painter.restore()


def blend(start: Sequence[QRectF], end: Sequence[QRectF], share: float) -> List[QRectF]:
    """Each box ``share`` of the way from ``start`` to ``end`` (a layout
    moving into another one)."""
    def between(a: float, b: float) -> float:
        return a + (b - a) * share
    return [QRectF(between(a.x(), b.x()), between(a.y(), b.y()),
                   between(a.width(), b.width()), between(a.height(), b.height()))
            for a, b in zip(start, end)]


def fitted(width: float, height: float, room: Tuple[float, float]) -> float:
    """The scale at which something ``width`` by ``height`` fits into
    ``room`` (never larger than it is)."""
    if width <= 0 or height <= 0:
        return 1.0
    return min(1.0, room[0] / width, room[1] / height)
