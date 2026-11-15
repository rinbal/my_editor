# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The delivery of a submission, in motion (plan 10.3; the owner's choice
"Einwurf": the article goes into the publication's inbox below it).

            ┌───────────────────────────────┐
            │ [▒▒] Ein Jahr mit Lightning … │     the article
            │      Artikel · Fassung 1 · …  │
            └───────────────────────────────┘
                           ·
                           ·                      three steps
                           ·
            ┌ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┐
              [21] Einundzwanzig Magazin          the publication's inbox
                   Eingang der Redaktion
            └ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┘

                 Wird an … gesendet …             the words, centred

One centred column: the article with its facts (kind, version, pictures),
a short way of three dots, the inbox, and under it the words of the
state. The stage moves only as far as the protocol got
(:class:`~nostr.standup.submitting.Progress`): a dot turns to the accent
colour when its step is done (encrypted, uploaded, sent), the upload
shows as a bar along the article's lower edge, and the dot of the step
that waits for the signer app breathes. While the request is on its way
and while no answer has come, the article hovers right above the inbox,
never on it. Only when the phase is DONE, which only a matching receipt
reaches (submitting.py), does it drop in: the article and the way close
up and the inbox grows into the receipt, which holds the publication
with a check and the article. Nothing was sent: the article is back in
its place, the dots are clear, the inbox waits again.

With Reduce Motion (motion.py) nothing travels or scales: the states
cross-fade in 120 ms. The words are the same.

:class:`HandoffView` is what a sheet shows in the place of its form and
its heading (the headline under the column is the title of this
moment): the stage, the words and the buttons each state has, together
at the trailing end with the default one last (Abbrechen while it can
still be taken back, dimmed once the request is on its way, and Im
Hintergrund fortsetzen; Abbrechen and Erneut versuchen after a failure,
Schließen alone when trying again would not help; Schließen while it is
being checked; Einreichung ansehen and Fertig once it is in). It only
shows the state and forwards the person's choice. After a refusal for a
server's limit, Erneut versuchen (and Erneut senden for the first
message) stays dimmed until the limit's wait is over, and the words say
when that is. In a low window the column is drawn smaller, and what is
still left over scrolls (:meth:`HandoffView.scrolling`).

A failure is said once (lessons 36): its title, what did not happen,
and one sentence, the likely cause and what to do. What an upload's
servers answered is technical: only behind Details.

The painted stage and the view that shows it, with the words of each
state, stay together, so the module is longer than most.
"""

from __future__ import annotations

import math
import time
from typing import Dict, Optional

from PySide6.QtCore import (
    Property, QDateTime, QEasingCurve, QEvent, QLocale, QPointF, QPropertyAnimation, QRectF,
    Qt, Signal,
)
from PySide6.QtGui import (
    QColor, QFont, QFontInfo, QFontMetricsF, QPainter, QPainterPath, QPen, QPixmap,
)
from PySide6.QtWidgets import (
    QDialogButtonBox, QGridLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea, QToolButton,
    QToolTip, QVBoxLayout, QWidget,
)

import announce
import i18n
from i18n import N_, _, ngettext, pgettext

from ..failures import spoken
from ..standup import words
from ..standup.submitting import HELD_WORDS, MessageStatus, Phase, Progress, Step
from . import appearance, motion
from .publish_sheet import cut_after_a_word, scroll_area
from .standup_waits import WaitTimer, wait_words
from .submission_cover import cover_pixmap
from .submission_thread import day_words

# The column, in points (the owner's prototype, variant "Einwurf").
_COLUMN = 344
_ROW = 70               # the article, and the inbox while it waits
_WAY = 30               # between them: three dots of 4, 5 apart
_RECEIPT = 118          # the inbox once the work is in
_RADIUS = 12
_HOVER = 22             # how far down the article hovers while it is sent
_DROP = 100             # and drops, into the inbox
# Room around the column for the cards' soft shadow (0 6 18): the words
# start right under it, 22 below the column.
_PAD_TOP, _PAD_SIDE, _PAD_BOTTOM = 12, 18, 22
# In a low window the column is drawn smaller, down to this.
_LEAST_ZOOM = 0.6
# The words: never wider, and the room three lines take is kept, so the
# column does not jump from state to state.
_WORDS_WIDTH = 460
_SMALL, _FACTS, _HEADLINE = 11.5 / 13, 12 / 13, 15 / 13     # of the app's font
# Dropping in, from the receipt on (milliseconds): the drop, the article
# fading, the article and the way closing up while the inbox grows, the
# receipt's two rows.
_SETTLE_MS = 1150
_GLIDE_MS = 750


def _bezier(x1: float, y1: float, x2: float, y2: float) -> QEasingCurve:
    curve = QEasingCurve(QEasingCurve.Type.BezierSpline)
    curve.addCubicBezierSegment(QPointF(x1, y1), QPointF(x2, y2), QPointF(1.0, 1.0))
    return curve


_GLIDE = _bezier(0.45, 0.0, 0.15, 1.0)          # what travels and what grows
_EASE = _bezier(0.25, 0.1, 0.25, 1.0)           # what fades


def _shadow_layers(blur: float = 18.0, step: float = 1.5) -> tuple:
    """A soft shadow as rounded rectangles laid over each other: how far
    each reaches beyond the card, and its share of the shadow's colour
    (the fall-off of a blur of ``blur``)."""
    def cover(distance: float) -> float:
        return 0.5 * (1 - math.erf(distance / (blur / 2 * math.sqrt(2))))

    reach = [index * step for index in range(int(blur / step) + 1)]
    return tuple((grow, cover(grow) - (cover(reach[index + 1]) if index + 1 < len(reach) else 0))
                 for index, grow in enumerate(reach))


_SHADOW = _shadow_layers()


def _elide(painter: QPainter, text: str, width: float) -> str:
    """One line of a card, cut as the sheet's card cuts its words."""
    return cut_after_a_word(painter.fontMetrics(), text, int(width), 1)


def _blend(a: QColor, b: QColor, share: float) -> QColor:
    share = max(0.0, min(1.0, share))
    return QColor.fromRgbF(a.redF() + (b.redF() - a.redF()) * share,
                           a.greenF() + (b.greenF() - a.greenF()) * share,
                           a.blueF() + (b.blueF() - a.blueF()) * share,
                           a.alphaF() + (b.alphaF() - a.alphaF()) * share)


def _animated(name: str) -> Property:
    return Property(float, lambda self: self._values[name],
                    lambda self, value: self._set(name, value))


class HandoffStage(QWidget):
    """The painted column: the article, the way, the inbox (see the module
    docstring). Animated, each 0 to 1 unless said: ``hover`` (the article
    right above the inbox), ``settle`` (dropping in, the whole of it),
    ``accent`` (the inbox's border in the accent colour), ``steps`` (how
    many dots are done, 0 to 3), ``bar`` (the upload), ``breath`` (the
    waiting dot) and ``mix`` (Reduce Motion's cross-fade)."""

    # Its height changed (dropping in closes the column up).
    resized = Signal()

    def __init__(self, is_dark: bool, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._is_dark = is_dark
        self._values = {"hover": 0.0, "settle": 0.0, "accent": 0.0, "steps": 0.0, "bar": 0.0,
                        "breath": 0.0, "mix": 1.0}
        self._animations: Dict[str, QPropertyAnimation] = {}
        self._bar_shown = False
        self._waiting = False
        self._before = QPixmap()                # the state before, while it cross-fades
        self._thumbs: dict = {}
        # What the pointer finds: a text that was cut, whole (tip_at).
        self._cut_title = self._cut_name = False
        self._badge = QRectF()
        # The work is in: the inbox is the receipt.
        self.received = False
        self.title = self.facts = self.publication = ""
        self.kind = "article"
        self.cover = QPixmap()
        self.logo = QPixmap()
        self.setFixedHeight(self.full_height())
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)

    # -- the animated values -------------------------------------------------

    def _set(self, name: str, value: float) -> None:
        self._values[name] = float(value)
        if name == "settle":
            self.resized.emit()
        self.update()

    hover = _animated("hover")
    settle = _animated("settle")
    accent = _animated("accent")
    steps = _animated("steps")
    bar = _animated("bar")
    breath = _animated("breath")
    mix = _animated("mix")

    def value(self, name: str) -> float:
        return self._values[name]

    def target(self, name: str) -> float:
        """Where ``name`` is going (where it is, when it is not moving)."""
        if self.animating(name):
            return float(self._animations[name].endValue())
        return self._values[name]

    def animating(self, name: str) -> bool:
        animation = self._animations.get(name)
        return animation is not None and animation.state() == QPropertyAnimation.State.Running

    def animate(self, name: str, end: float, ms: int, *, cross_fade: bool = False,
                curve: Optional[QEasingCurve] = None) -> None:
        """``name`` to ``end`` in ``ms`` (motion.duration: none at all, or
        the 120 ms cross-fade, when motion is reduced)."""
        running = self._animations.pop(name, None)
        if running is not None:
            running.stop()
        duration = motion.duration(ms, cross_fade=cross_fade)
        if duration <= 0 or self._values[name] == end:
            self._set(name, end)
            return
        animation = QPropertyAnimation(self, name.encode("ascii"), self)
        animation.setDuration(duration)
        animation.setStartValue(self._values[name])
        animation.setEndValue(float(end))
        animation.setEasingCurve(curve if curve is not None
                                 else QEasingCurve(QEasingCurve.Type.Linear))
        self._animations[name] = animation
        animation.start()

    def _breathe(self, on: bool) -> None:
        """The waiting dot breathing (never with Reduce Motion)."""
        running = self._animations.pop("breath", None)
        if running is not None:
            running.stop()
        self._set("breath", 0.0)
        if not on or motion.reduce_motion():
            return
        animation = QPropertyAnimation(self, b"breath", self)
        animation.setDuration(1600)
        animation.setKeyValueAt(0.0, 0.0)
        animation.setKeyValueAt(0.5, 1.0)
        animation.setKeyValueAt(1.0, 0.0)
        animation.setEasingCurve(QEasingCurve(QEasingCurve.Type.InOutSine))
        animation.setLoopCount(-1)
        self._animations["breath"] = animation
        animation.start()

    # -- what it shows -------------------------------------------------------

    def show_state(self, *, steps: int, waiting: bool, near: bool, received: bool,
                   moving: bool = True) -> None:
        """The stage for a state: ``steps`` dots done, the next one
        ``waiting`` (for the signer app), the article ``near`` the inbox
        (the request is on its way), or ``received``: in. ``moving``:
        False puts it there at once (a sheet opened on a run)."""
        if moving and motion.reduce_motion() and self.isVisible():
            self._cross_fade()
        ms = 1 if moving else 0
        self._waiting = waiting
        if received:
            if not self.received:
                self.received = True
                self.animate("settle", 1.0, _SETTLE_MS * ms)
        else:
            self.received = False
            self.animate("settle", 0.0, 0)
            self.animate("hover", 1.0 if near and not motion.reduce_motion() else 0.0,
                         _GLIDE_MS * ms, curve=_GLIDE)
        self.animate("accent", 1.0 if near or received else 0.0, 300 * ms, curve=_EASE)
        self.animate("steps", float(steps), 300 * ms, curve=_EASE)
        self._breathe(waiting)
        self.update()

    def _cross_fade(self) -> None:
        """Reduce Motion: what shows now fades into what follows."""
        self._before = self._picture()
        running = self._animations.pop("mix", None)
        if running is not None:
            running.stop()
        self._values["mix"] = 0.0
        animation = QPropertyAnimation(self, b"mix", self)
        animation.setDuration(motion.CROSS_FADE_MS)
        animation.setStartValue(0.0)
        animation.setEndValue(1.0)
        self._animations["mix"] = animation
        animation.start()

    def set_bar(self, fraction: Optional[float]) -> None:
        """The upload's fraction along the article's lower edge; None: no bar."""
        shown = fraction is not None
        if not shown or not self._bar_shown:
            self.animate("bar", fraction or 0.0, 0)
        else:
            self.animate("bar", max(0.0, min(1.0, fraction)), 250)
        self._bar_shown = shown
        self.update()

    def set_dark(self, is_dark: bool) -> None:
        self._is_dark = is_dark
        self.update()

    # -- where things are ----------------------------------------------------

    def _part(self, start: int, length: int, curve: QEasingCurve = _EASE) -> float:
        """How far one part of dropping in is (0 to 1): it starts ``start``
        milliseconds after the receipt and takes ``length``."""
        at = self._values["settle"] * _SETTLE_MS
        return curve.valueForProgress(max(0.0, min(1.0, (at - start) / length)))

    def _closed(self) -> float:
        """How far the article and the way have closed up (0 to 1)."""
        return self._part(600, 450, _GLIDE)

    def _full(self) -> float:
        return (_PAD_TOP + _ROW + (_ROW + _WAY) * (1 - self._closed())
                + (_RECEIPT - _ROW) * self._closed() + _PAD_BOTTOM)

    def full_height(self) -> int:
        """The stage's height, whole, as it stands now."""
        return round(self._full())

    def zoom(self) -> float:
        """1, or less in a window too low for the column whole."""
        full = self._full()
        return 1.0 if self.height() >= full - 1 else max(0.05, self.height() / full)

    def _places(self) -> tuple:
        """The article in its place and the inbox, before the zoom."""
        width = self.width() / self.zoom()
        column = max(0.0, min(float(_COLUMN), width - 2 * _PAD_SIDE))
        left = (width - column) / 2
        closed = self._closed()
        top = _PAD_TOP + (_ROW + _WAY) * (1 - closed)
        return (QRectF(left, _PAD_TOP, column, _ROW),
                QRectF(left, top, column, _ROW + (_RECEIPT - _ROW) * closed))

    def _pose(self) -> tuple:
        """How far down the article is, and its scale."""
        hover, drop = self._values["hover"], self._part(0, _GLIDE_MS, _GLIDE)
        down, scale = _HOVER * hover, 1 - 0.03 * hover
        return down + (_DROP - down) * drop, scale + (0.9 - scale) * drop

    def _zoomed(self, rect: QRectF) -> QRectF:
        zoom = self.zoom()
        return QRectF(rect.left() * zoom, rect.top() * zoom, rect.width() * zoom,
                      rect.height() * zoom)

    def row_rect(self) -> QRectF:
        """The article as it is drawn now: moved, scaled."""
        row, _inbox = self._places()
        down, scale = self._pose()
        width, height = row.width() * scale, row.height() * scale
        centre = row.center() + QPointF(0, down)
        return self._zoomed(QRectF(centre.x() - width / 2, centre.y() - height / 2, width, height))

    def inbox_rect(self) -> QRectF:
        return self._zoomed(self._places()[1])

    def tip_at(self, position) -> str:
        """What the pointer at ``position`` is told: a title or a name that
        was cut, whole; on the check, what it stands for."""
        zoom = self.zoom()
        point = QPointF(position.x() / zoom, position.y() / zoom)
        inbox = self._places()[1]
        if self.received:
            if self._badge.contains(point):
                return _(words.SUBMITTED_DETAIL)
            if inbox.contains(point):
                upper = point.y() < inbox.top() + inbox.height() / 2
                return (self.publication if upper and self._cut_name
                        else self._title() if not upper and self._cut_title else "")
            return ""
        if self.row_rect().contains(QPointF(position)) and self._cut_title:
            return self._title()
        return self.publication if inbox.contains(point) and self._cut_name else ""

    def event(self, event) -> bool:  # noqa: A003, Qt's name
        if event.type() == QEvent.Type.ToolTip:
            text = self.tip_at(event.pos())
            if text:
                QToolTip.showText(event.globalPos(), text, self)
            else:
                QToolTip.hideText()
                event.ignore()
            return True
        return super().event(event)

    # -- painting ------------------------------------------------------------

    def _title(self) -> str:
        return self.title or _("Untitled")

    def _font(self, size: float = 1.0, *, strong: bool = False) -> QFont:
        font = QFont(self.font())
        if size != 1.0:
            if font.pointSizeF() > 0:
                font.setPointSizeF(font.pointSizeF() * size)
            else:
                font.setPixelSize(max(1, round(font.pixelSize() * size)))
        if strong:
            font.setWeight(QFont.Weight.DemiBold)
        return font

    def _line(self, font: QFont) -> float:
        """A line's height in ``font``: a little airier than the font's own
        (1.35 of its size), as the column's rows are set."""
        size = QFontInfo(font).pointSizeF() * self.logicalDpiY() / 72.0
        return max(QFontMetricsF(font).height(), 1.35 * size)

    def _thumb(self, size: int, radius: float) -> QPixmap:
        """The cover as a rounded square of ``size`` points, with two
        pixels a point, made once for a cover; without one, the app's
        stand-in tile of the kind."""
        key = (self.cover.cacheKey(), size, self._is_dark)
        made = self._thumbs.get(key)
        if made is not None:
            return made
        if self.cover.isNull():
            r = appearance.roles(self._is_dark)
            made = cover_pixmap(None, self.kind, size, QColor(r.soft), QColor(r.icon))
        else:
            side = size * 2
            image = self.cover.toImage()
            image.setDevicePixelRatio(1.0)
            image = image.scaled(side, side, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                 Qt.TransformationMode.SmoothTransformation)
            made = QPixmap(side, side)
            made.setDevicePixelRatio(2.0)
            made.fill(Qt.GlobalColor.transparent)
            painter = QPainter(made)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            path = QPainterPath()
            path.addRoundedRect(QRectF(0, 0, size, size), radius, radius)
            painter.setClipPath(path)
            painter.drawImage(QRectF(0, 0, size, size), image,
                              QRectF((image.width() - side) / 2, (image.height() - side) / 2,
                                     side, side))
            painter.end()
        if len(self._thumbs) > 8:
            self._thumbs.clear()
        self._thumbs[key] = made
        return made

    def _picture(self) -> QPixmap:
        """The stage as it shows now, as a picture."""
        ratio = self.devicePixelRatioF()
        picture = QPixmap(max(1, round(self.width() * ratio)), max(1, round(self.height() * ratio)))
        picture.setDevicePixelRatio(ratio)
        picture.fill(Qt.GlobalColor.transparent)
        painter = QPainter(picture)
        self._paint(painter)
        painter.end()
        return picture

    def paintEvent(self, _event) -> None:  # noqa: N802, Qt's name
        painter = QPainter(self)
        mix = self._values["mix"]
        if mix < 1.0 and not self._before.isNull():
            painter.setOpacity(1.0 - mix)
            before = self._before.deviceIndependentSize()
            painter.drawPixmap(QPointF((self.width() - before.width()) / 2, 0), self._before)
            painter.setOpacity(mix)
        self._paint(painter)

    def _paint(self, painter: QPainter) -> None:
        r = appearance.roles(self._is_dark)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing
                               | QPainter.RenderHint.TextAntialiasing
                               | QPainter.RenderHint.SmoothPixmapTransform)
        base = painter.opacity()
        painter.save()
        zoom = self.zoom()
        painter.scale(zoom, zoom)
        row, inbox = self._places()
        self._paint_way(painter, r, row, base)
        painter.setOpacity(base)
        self._paint_inbox(painter, r, inbox, base)
        shown = 1.0 - self._part(400, 300)
        if shown > 0:
            down, scale = self._pose()
            centre = row.center() + QPointF(0, down)
            painter.setOpacity(base * shown)
            painter.translate(centre)
            painter.scale(scale, scale)
            painter.translate(-row.center())
            self._paint_row(painter, r, row)
        painter.restore()

    def _paint_shadow(self, painter: QPainter, r, rect: QRectF, strength: float = 1.0) -> None:
        """A card's soft shadow, a little below it (none where the system
        asks for less transparency or more contrast)."""
        if strength <= 0 or not r.glow_alpha:
            return
        painter.setPen(Qt.PenStyle.NoPen)
        for grow, share in _SHADOW:
            ink = QColor(r.shadow)
            ink.setAlphaF(min(1.0, r.shadow_alpha / 255 * share * strength))
            painter.setBrush(ink)
            painter.drawRoundedRect(rect.adjusted(-grow, 6 - grow, grow, 6 + grow),
                                    _RADIUS + grow, _RADIUS + grow)

    def _paint_lines(self, painter: QPainter, r, room: QRectF, first: str, second: str) -> bool:
        """A name over a small line, centred in ``room``; whether the name
        was cut."""
        strong, small = self._font(strong=True), self._font(_SMALL)
        tall, low = self._line(strong), self._line(small)
        top = room.center().y() - (tall + low) / 2
        align = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        painter.setFont(strong)
        painter.setPen(QColor(r.text))
        cut = painter.fontMetrics().horizontalAdvance(first) > int(room.width())
        painter.drawText(QRectF(room.left(), top, room.width(), tall), align,
                         _elide(painter, first, room.width()))
        painter.setFont(small)
        painter.setPen(QColor(r.muted))
        painter.drawText(QRectF(room.left(), top + tall, room.width(), low), align,
                         _elide(painter, second, room.width()))
        return cut

    def _paint_row(self, painter: QPainter, r, rect: QRectF) -> None:
        self._paint_shadow(painter, r, rect)
        path = QPainterPath()
        path.addRoundedRect(rect, _RADIUS, _RADIUS)
        painter.setPen(QPen(QColor(r.line), 1))
        painter.setBrush(QColor(r.field))
        painter.drawPath(path)
        thumb = QRectF(rect.left() + 13, rect.center().y() - 22, 44, 44)
        picture = self._thumb(44, 8)
        painter.drawPixmap(thumb, picture, QRectF(picture.rect()))
        left = thumb.right() + 11
        self._cut_title = self._paint_lines(
            painter, r, QRectF(left, rect.top(), max(0.0, rect.right() - 13 - left),
                               rect.height()), self._title(), self.facts)
        if self._bar_shown:
            painter.save()
            painter.setClipPath(path)
            painter.fillRect(QRectF(rect.left(), rect.bottom() - 3,
                                    rect.width() * self._values["bar"], 3), QColor(r.button))
            painter.restore()

    def _paint_way(self, painter: QPainter, r, row: QRectF, base: float) -> None:
        shown = 1.0 - self._part(0, 200)
        if shown <= 0:
            return
        idle, done = QColor(r.edge), QColor(r.button)
        idle.setAlphaF(0.7)
        steps = self._values["steps"]
        next_step = int(round(self.target("steps")))
        painter.setPen(Qt.PenStyle.NoPen)
        for index in range(3):
            ink, alpha = _blend(idle, done, steps - index), 1.0
            if self._waiting and index == next_step and self.animating("breath"):
                ink, alpha = done, 1.0 - 0.65 * self._values["breath"]
            painter.setOpacity(base * shown * alpha)
            painter.setBrush(ink)
            painter.drawEllipse(QRectF(row.center().x() - 2, row.bottom() + 4 + 9 * index, 4, 4))

    def _paint_inbox(self, painter: QPainter, r, rect: QRectF, base: float) -> None:
        accent = QColor(r.button)
        box = rect.adjusted(0.75, 0.75, -0.75, -0.75)
        if self.received:
            taken = self._part(0, 300)
            self._paint_shadow(painter, r, rect, taken)
            fill = QColor(r.field)
            fill.setAlphaF(taken)
            painter.setBrush(fill)
            painter.setPen(QPen(accent, 1.5))
        else:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            pen = QPen(_blend(QColor(r.edge), accent, self._values["accent"]), 1.5)
            pen.setDashPattern([2.0, 2.0])
            pen.setCapStyle(Qt.PenCapStyle.FlatCap)
            painter.setPen(pen)
        painter.drawRoundedRect(box, _RADIUS, _RADIUS)
        waits = 1.0 - self._part(0, 250)
        if waits > 0:
            painter.setOpacity(base * waits)
            place = QRectF(rect.left(), rect.top(), rect.width(), _ROW)
            logo = QRectF(place.left() + 13, place.center().y() - 20, 40, 40)
            if not self.logo.isNull():
                painter.drawPixmap(logo, self.logo, QRectF(self.logo.rect()))
            left = logo.right() + 11
            self._cut_name = self._paint_lines(
                painter, r, QRectF(left, place.top(), max(0.0, place.right() - 13 - left),
                                   place.height()), self.publication, _("Editorial inbox"))
        held = self._part(850, 300)
        if held > 0:
            painter.setOpacity(base * held)
            self._paint_receipt(painter, r, rect)
        painter.setOpacity(base)

    def _paint_receipt(self, painter: QPainter, r, rect: QRectF) -> None:
        """The receipt's two rows: the publication with the check, a
        hairline, the article."""
        middle = rect.top() + 58.5
        logo = QRectF(rect.left() + 15, rect.top() + 14.5, 30, 30)
        if not self.logo.isNull():
            painter.drawPixmap(logo, self.logo, QRectF(self.logo.rect()))
        # The check and "In the inbox", at the trailing end.
        strong = self._font(_FACTS, strong=True)
        said = _("In the inbox")
        wide = QFontMetricsF(strong).horizontalAdvance(said)
        badge = QRectF(rect.right() - 15 - wide - 17, rect.top() + 1.5, wide + 17, 57)
        self._badge = badge
        ok = QColor(r.success)
        tick = QPen(ok, 1.8)
        tick.setCapStyle(Qt.PenCapStyle.RoundCap)
        tick.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(tick)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        x, y = badge.left(), badge.center().y() - 6.5
        mark = QPainterPath(QPointF(x + 2.6, y + 6.8))
        mark.lineTo(x + 5.1, y + 9.3)
        mark.lineTo(x + 10.4, y + 3.7)
        painter.drawPath(mark)
        painter.setFont(strong)
        painter.drawText(QRectF(badge.left() + 17, badge.top(), wide + 1, badge.height()),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, said)
        left = logo.right() + 10
        self._cut_name = self._paint_lines(
            painter, r, QRectF(left, rect.top() + 1.5, max(0.0, badge.left() - 10 - left), 57),
            self.publication, _("Editorial inbox"))
        painter.setPen(QPen(QColor(r.line), 1))
        painter.drawLine(QPointF(rect.left() + 1.5, middle), QPointF(rect.right() - 1.5, middle))
        thumb = QRectF(rect.left() + 15, middle + 11.5, 34, 34)
        picture = self._thumb(34, 7)
        painter.drawPixmap(thumb, picture, QRectF(picture.rect()))
        left = thumb.right() + 10
        self._cut_title = self._paint_lines(
            painter, r, QRectF(left, middle + 0.5, max(0.0, rect.right() - 15 - left), 57),
            self._title(), self.facts)


# A later version, in: the headline, as the submission's history says it.
HANDED_IN = N_("Version {number} handed in")
# When the server took it, among the receipt's facts: "Today, 03:21".
RECEIVED_AT = N_("{day}, {time}")


def work_facts(number: int, pictures: int = 0) -> str:
    """The article's facts in one line (translated): its kind, the version
    that goes, and how many pictures it shows (left out for none)."""
    facts = [_("Article"), _("Version {number}").format(number=number)]
    if pictures > 0:
        facts.append(ngettext("{count} picture", "{count} pictures",
                              pictures).format(count=pictures))
    return " · ".join(facts)


def receipt_facts(progress: Progress, now: Optional[float] = None) -> str:
    """The receipt's facts in one line (translated): when the server took
    the work (its receipt's time; left out when the run holds none), that
    it is end-to-end encrypted, and that the first message went with it
    (only once that is sent)."""
    facts = []
    at = getattr(progress.receipt, "received_at", None)
    if isinstance(at, int) and at > 0:
        today = QDateTime.fromSecsSinceEpoch(int(_now() if now is None else now)).date()
        locale = i18n.display_locale()
        clock = locale.toString(QDateTime.fromSecsSinceEpoch(at).time(),
                                locale.timeFormat(QLocale.FormatType.ShortFormat))
        facts.append(_(RECEIVED_AT).format(day=day_words(at, today.toPython()), time=clock))
    facts.append(_("End-to-end encrypted"))
    if progress.message is MessageStatus.SENT:
        facts.append(_(words.WITH_YOUR_MESSAGE))
    return " · ".join(facts)


def phase_words(progress: Progress, publication: str, now: Optional[float] = None, *,
                number: int = 1, signer_app: bool = True) -> tuple:
    """The two lines a state says (translated), the plan's words.
    ``signer_app``: the account signs in a signer app; one whose key is
    kept on this computer has none to wait for, so while the request is
    signed it says what happens, that the work is being sent (it said
    "Waiting for your signer app. Confirm the request there." to a
    person who has no such app). While
    the request is on its way and while it is checked, the words are
    what the server holds (HELD_WORDS, lesson 32): a run whose receipt
    came never says "Not confirmed yet" (RG-M2r2a 3). A failure: its
    title, from what the server holds, and its one sentence, with when
    trying again helps while a limit's wait is not over (lessons 36)."""
    phase = progress.phase
    if phase is Phase.FAILED and progress.failure is not None:
        failure = progress.failure
        sentence = failure.said or wait_words(
            failure.retry_at, _now() if now is None else now, words.LIMIT_AT,
            words.LIMIT_TOMORROW_AT, words.LIMIT_OVER, publication=publication) or _(
            failure.sentence).format(publication=publication)
        return _(failure.title).format(publication=publication), sentence
    held = HELD_WORDS[progress.held]
    if progress.step is Step.RELOADING:
        lines = (words.SUBMITTED_ELSEWHERE, words.READING_LIST)
    else:
        lines = {
            Phase.ENCRYPT: (words.ENCRYPTING, ""),
            Phase.UPLOAD: (words.UPLOADING_PICTURE if progress.pictures == 1
                           else words.UPLOADING_PICTURES, words.UPLOADING_DETAIL),
            Phase.SIGNER: ((words.WAITING_FOR_SIGNER, words.WAITING_DETAIL) if signer_app
                           else (held.sending, "")),
            Phase.SEND: (held.sending, ""),
            Phase.UNCONFIRMED: (held.checking, words.UNCONFIRMED_DETAIL),
            Phase.DONE: (words.SUBMITTED_HEADLINE if number < 2 else HANDED_IN,
                         words.SUBMITTED_NEXT),
        }.get(phase, ("", ""))
    return (_(lines[0]).format(publication=publication, number=number) if lines[0] else "",
            _(lines[1]) if lines[1] else "")


def _now() -> float:
    return time.time()


_MESSAGE_WORDS = {MessageStatus.SIGNING: words.MESSAGE_SIGNING,
                  MessageStatus.SENDING: words.MESSAGE_SENDING,
                  MessageStatus.SENT: words.MESSAGE_SENT}


def message_words(progress: Progress, now: float, *, signer_app: bool = True) -> tuple:
    """The first message's line (translated), once the work is in: how it
    goes, or, when it did not go, its title and its one sentence, with
    when sending again helps while a limit's wait is not over (lessons
    36). ``("", "")`` when there is nothing to say. Without a
    ``signer_app`` (a key kept here) its signing is part of its sending."""
    if progress.phase is not Phase.DONE:
        return "", ""
    failure = progress.message_failure
    if progress.message is MessageStatus.FAILED and failure is not None:
        sentence = wait_words(failure.retry_at, now, words.MESSAGE_LIMIT_AT,
                              words.MESSAGE_LIMIT_TOMORROW_AT,
                              words.MESSAGE_LIMIT_OVER) or _(failure.sentence)
        return _(failure.title), sentence
    status = progress.message
    if status is MessageStatus.SIGNING and not signer_app:
        status = MessageStatus.SENDING
    shown = _MESSAGE_WORDS.get(status)
    return (_(shown), "") if shown else ("", "")


class HandoffView(QWidget):
    """The stage, the words and the buttons (see the module docstring)."""

    cancel_requested = Signal()
    retry_requested = Signal()
    retry_message_requested = Signal()
    close_requested = Signal()
    # The sheet closes and the submission goes on (RG-M2b 10).
    background_requested = Signal()
    # Einreichung ansehen, once it is in: its detail opens.
    view_requested = Signal()

    def __init__(self, *, is_dark: bool, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.number = 1                         # the version handed in
        self.pictures = 0                       # how many pictures the article shows
        self.signer_app = True                  # the account signs in a signer app
        self._is_dark = is_dark
        self._phase: Optional[Phase] = None
        self._said = ""
        self._progress: Optional[Progress] = None
        self._error = False                 # the headline in the error colour
        self._problem = False               # the first message did not go
        self._scroll: Optional[QScrollArea] = None      # see scrolling()
        self._beside: Optional[bool] = None # Send Again right after its line
        self._words_height = 0              # what the words take, beside the stage
        # Shows the state again once a limit's wait is over (RG-M2r 7).
        self._wait = WaitTimer(self._wait_over, self)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        # The column and its words stand in the middle of the room.
        column.addStretch(1)
        self.stage = HandoffStage(is_dark)
        self.stage.resized.connect(self._size_stage)
        column.addWidget(self.stage)
        self.headline = QLabel()
        self.headline.setObjectName("handoff_headline")
        self.detail = QLabel()
        self.detail.setObjectName("muted")
        # Once it is in: when, that it is encrypted, and the message with it.
        self.facts = QLabel()
        self.facts.setObjectName("muted")
        self.message = QLabel()
        # What an upload's servers answered: behind Details (lessons 36).
        self.details_toggle = QToolButton()
        self.details_toggle.setObjectName("disclosure")     # as the sheets' others
        self.details_toggle.setText(_("Details"))
        self.details_toggle.setCheckable(True)
        self.details_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.details_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.details_toggle.toggled.connect(self._show_details)
        self.details_toggle.hide()
        self.details = QLabel()
        self.details.setObjectName("muted")
        self.details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.details.hide()
        # The first message's one action, in its line: Send Again.
        self.send_again = QPushButton(_("Send Again"))
        self.send_again.setAutoDefault(False)
        self.send_again.setCursor(Qt.CursorShape.PointingHandCursor)
        self.send_again.clicked.connect(self.retry_message_requested)
        self._lines = QWidget()
        lines = QVBoxLayout(self._lines)
        lines.setContentsMargins(0, 0, 0, 0)
        lines.setSpacing(6)
        self.facts.setContentsMargins(0, 2, 0, 0)
        for label in (self.headline, self.detail, self.facts, self.message, self.details):
            label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            label.setWordWrap(True)
            label.setTextFormat(Qt.TextFormat.PlainText)
        lines.addWidget(self.headline)
        lines.addWidget(self.detail)
        lines.addWidget(self.facts)
        self._message_row = QGridLayout()
        self._message_row.setContentsMargins(0, 4, 0, 0)
        self._message_row.setHorizontalSpacing(8)
        self._message_row.setVerticalSpacing(2)
        lines.addLayout(self._message_row)
        lines.addWidget(self.details_toggle, 0, Qt.AlignmentFlag.AlignHCenter)
        lines.addWidget(self.details)
        lines.addStretch(1)
        self._lines.setMaximumWidth(_WORDS_WIDTH)
        middle = QHBoxLayout()
        middle.addStretch(1)
        middle.addWidget(self._lines, 1000)
        middle.addStretch(1)
        column.addLayout(middle)
        # As much under the words as the stage keeps free above the column.
        column.addSpacing(_PAD_TOP)
        column.addStretch(1)
        self.buttons = QDialogButtonBox()
        self.buttons.layout().setSpacing(10)
        # Together at the trailing end, the default one last. The box puts
        # a Yes after everything else and a second accepting button before
        # Cancel, so the two that close the screen when it went well are a
        # Yes each: Continue in Background after Cancel, Done after View
        # Submission. Continue in Background is not the default: while it
        # goes on, Return never closes the sheet by a key pressed once
        # more than meant.
        self.background_button = self.buttons.addButton(
            _("Continue in Background"), QDialogButtonBox.ButtonRole.YesRole)
        self.background_button.setAutoDefault(False)
        self.cancel_button = self.buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        self.close_button = self.buttons.addButton(_("Close"),
                                                   QDialogButtonBox.ButtonRole.RejectRole)
        self.retry_button = self.buttons.addButton(_("Try Again"),
                                            QDialogButtonBox.ButtonRole.AcceptRole)
        self.view_button = self.buttons.addButton(_("View Submission"),
                                                  QDialogButtonBox.ButtonRole.RejectRole)
        self.view_button.setAutoDefault(False)
        self.view_button.clicked.connect(self.view_requested)
        self.done_button = self.buttons.addButton(_("Done"),
                                                  QDialogButtonBox.ButtonRole.YesRole)
        self.background_button.clicked.connect(self.background_requested)
        self.cancel_button.clicked.connect(self.cancel_requested)
        self.close_button.clicked.connect(self.close_requested)
        self.retry_button.clicked.connect(self.retry_requested)
        self.done_button.clicked.connect(self.close_requested)
        self._fonts()
        self._arrange_message()
        self.set_dark(is_dark)

    def _fonts(self) -> None:
        """The headline larger and semibold, the facts a little smaller,
        from the app's font, and the room three lines take kept."""
        headline, facts = self.stage._font(_HEADLINE, strong=True), self.stage._font(_FACTS)
        self.headline.setFont(headline)
        self.facts.setFont(facts)
        self._lines.setMinimumHeight(math.ceil(
            QFontMetricsF(headline).height() + QFontMetricsF(self.font()).height()
            + QFontMetricsF(facts).height()) + 14)

    def scrolling(self) -> QScrollArea:
        """The view for a sheet, in a part that scrolls (as a sheet's form
        does): in a window too low for it, its words were painted over
        the stage. The column is drawn as tall as the room beside the
        words allows, whole or smaller, and only what is still left over
        scrolls; the buttons stay outside, always whole."""
        if self._scroll is None:
            self._scroll = scroll_area(self)
            self._scroll.viewport().installEventFilter(self)
        return self._scroll

    def eventFilter(self, watched, event) -> bool:  # noqa: N802, Qt's name
        if event.type() == QEvent.Type.Resize and self._scroll is not None \
                and watched is self._scroll.viewport():
            self._fit_stage()
        return False

    def resizeEvent(self, event) -> None:  # noqa: N802, Qt's name
        super().resizeEvent(event)
        self._arrange_message()

    def _fit_stage(self) -> None:
        """The stage's height for the room the sheet has (scrolling),
        measured anew: other words, another width."""
        if self._scroll is not None:
            self.layout().activate()
            width = self._scroll.viewport().width()
            self._words_height = (self.layout().totalHeightForWidth(width)
                                  - self.stage.maximumHeight())
        self._size_stage()

    def _size_stage(self) -> None:
        full = self.stage.full_height()
        if self._scroll is not None:
            room = self._scroll.viewport().height() - self._words_height
            full = max(round(full * _LEAST_ZOOM), min(full, room))
        if full != self.stage.maximumHeight():
            self.stage.setFixedHeight(full)

    def set_work(self, *, title: str, cover: QPixmap, publication: str, logo: QPixmap,
                 number: int = 1, pictures: int = 0, signer_app: bool = True) -> None:
        """``number``: the version handed in (1 for a first submission).
        ``pictures``: how many pictures the article shows (0: not said).
        ``signer_app``: whether the account signs in a signer app (False
        for a key kept on this computer: phase_words)."""
        self.number = number
        self.pictures = pictures
        self.signer_app = signer_app
        stage = self.stage
        stage.title, stage.facts = title, work_facts(number, pictures)
        stage.cover, stage.publication, stage.logo = cover, publication, logo
        self._describe()
        stage.update()

    def set_cover(self, cover: QPixmap) -> None:
        """The cover, come after the work was shown."""
        self.stage.cover = cover
        self.stage.update()

    def _describe(self) -> None:
        """What the painted stage shows, for a screen reader."""
        stage = self.stage
        self.stage.setAccessibleName(spoken(
            stage.title or _("Untitled"), stage.facts, stage.publication, _("Editorial inbox"),
            _("In the inbox") if stage.received else ""))

    def set_dark(self, is_dark: bool) -> None:
        self._is_dark = is_dark
        self.stage.set_dark(is_dark)
        r = appearance.roles(is_dark)
        self.headline.setStyleSheet(f"color: {r.error if self._error else r.text};")
        self.message.setStyleSheet(f"color: {r.text if self._problem else r.muted};")
        # An action in a line of words: as a link, with the app's focus ring.
        self.send_again.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {r.ink}; min-width: 0;
                border: 1px solid transparent; border-radius: 4px; padding: 0 3px;
                text-decoration: underline; }}
            QPushButton:hover, QPushButton:pressed {{ background: transparent;
                border-color: transparent; }}
            QPushButton:pressed {{ color: {r.pressed if not is_dark else r.icon}; }}
            QPushButton:focus {{ border: 1px solid {r.focus}; padding: 0 3px; }}
            QPushButton:disabled {{ background: transparent; color: {r.disabled};
                border-color: transparent; text-decoration: none; }}""")

    def show_progress(self, progress: Progress, *, quietly: bool = False) -> None:
        """Show ``progress``; what it says is announced when it changed,
        unless ``quietly`` (a wait that is over: only its words change)."""
        self._progress = progress
        phase = progress.phase
        now = _now()
        first, second = phase_words(progress, self.stage.publication, now, number=self.number,
                                    signer_app=self.signer_app)
        message_title, message_sentence = message_words(progress, now,
                                                        signer_app=self.signer_app)
        retry_at, message_at = self._waits(progress)
        self.send_again.setEnabled(not message_at or message_at <= now)
        self._wait.follow(retry_at, message_at, now=now)
        self.headline.setText(first)
        self.detail.setText(second)
        self.detail.setVisible(bool(second))
        done = phase is Phase.DONE
        self.facts.setText(receipt_facts(progress, now) if done else "")
        self.facts.setVisible(done)
        # A message that is sent is one of the facts; one on its way, or
        # one that did not go, has a line of its own, the latter with its
        # one action.
        problem = done and progress.message is MessageStatus.FAILED
        shown = "" if progress.message is MessageStatus.SENT else spoken(message_title,
                                                                         message_sentence)
        self.message.setText(shown)
        self.message.setVisible(bool(shown))
        self.send_again.setVisible(problem)
        failure = progress.failure if phase is Phase.FAILED else None
        error = failure is not None and failure.error
        details = failure.details if failure is not None else ""
        self.details.setText(details)
        self.details_toggle.setVisible(bool(details))
        if not details:
            self.details_toggle.setChecked(False)
        self.details.setVisible(bool(details) and self.details_toggle.isChecked())
        moved = phase is not self._phase
        if moved or error is not self._error or problem is not self._problem:
            first_time, self._phase = self._phase is None, phase
            self._error, self._problem = error, problem
            self.set_dark(self._is_dark)
            if moved:
                self._move(phase, moving=not first_time)
        self.stage.set_bar(progress.uploaded if phase is Phase.UPLOAD and progress.pictures
                           else None)
        self._show_buttons(progress)
        self._arrange_message()
        self._fit_stage()                           # other words, other room
        # The check says that the server confirmed it: heard, not written.
        said = spoken(first, _(words.SUBMITTED_DETAIL) if done else "", second, message_title,
                      message_sentence)
        if said != self._said:
            self._said = said
            if not quietly:
                announce.announce(self, said)

    def _arrange_message(self) -> None:
        """The first message's line with Send Again right after it, or
        under it where one line has no room for both."""
        row, label, button = self._message_row, self.message, self.send_again
        room = min(_WORDS_WIDTH, self.width())
        beside = (not button.isHidden() and label.fontMetrics().horizontalAdvance(label.text())
                  + row.horizontalSpacing() + button.sizeHint().width() <= room)
        if beside is self._beside:
            return
        self._beside = beside
        for widget in (label, button):
            row.removeWidget(widget)
        for index in range(4):
            row.setColumnStretch(index, 1 if beside and index in (0, 3) else 0)
        label.setWordWrap(not beside)
        if beside:
            row.addWidget(label, 0, 1)
            row.addWidget(button, 0, 2)
        else:
            row.addWidget(label, 0, 0, 1, 4)
            row.addWidget(button, 1, 0, 1, 4, Qt.AlignmentFlag.AlignHCenter)

    @staticmethod
    def _waits(progress: Progress) -> tuple:
        """The limits' waits this state has: Try Again's, Send Again's."""
        failure = progress.failure if progress.phase is Phase.FAILED else None
        message_failed = progress.phase is Phase.DONE and progress.message is MessageStatus.FAILED
        message = progress.message_failure if message_failed else None
        return (failure.retry_at if failure is not None else None,
                message.retry_at if message is not None else None)

    def _wait_over(self) -> None:
        if self._progress is not None:
            self.show_progress(self._progress, quietly=True)

    def _show_details(self, shown: bool) -> None:
        self.details_toggle.setArrowType(Qt.ArrowType.DownArrow if shown
                                         else Qt.ArrowType.RightArrow)
        self.details.setVisible(shown and bool(self.details.text()))
        self._fit_stage()

    def _move(self, phase: Phase, *, moving: bool = True) -> None:
        """The stage for ``phase``: a dot for each step done (encrypted,
        uploaded, sent), the article right above the inbox while the
        request is out, and in it only for DONE, which only a matching
        receipt reaches."""
        steps = {Phase.UPLOAD: 1, Phase.SIGNER: 2, Phase.SEND: 2, Phase.UNCONFIRMED: 3,
                 Phase.DONE: 3}.get(phase, 0)
        self.stage.show_state(steps=steps, waiting=phase is Phase.SIGNER and self.signer_app,
                              near=phase in (Phase.SEND, Phase.UNCONFIRMED),
                              received=phase is Phase.DONE, moving=moving)
        self._describe()

    def _show_buttons(self, progress: Progress) -> None:
        phase = progress.phase
        retry = phase is Phase.FAILED and progress.failure is not None \
            and progress.failure.retry is not None
        going = phase not in (Phase.DONE, Phase.FAILED, Phase.UNCONFIRMED)
        self.background_button.setVisible(going)
        self.cancel_button.setVisible(going)
        self.cancel_button.setEnabled(progress.cancellable)
        self.close_button.setVisible(phase in (Phase.FAILED, Phase.UNCONFIRMED))
        # Beside Try Again after a failure, Cancel (lessons 36); alone, or
        # after an end that is no error, Close.
        self.close_button.setText(_("Cancel") if retry and progress.failure.error
                                  else _("Close"))
        self.retry_button.setVisible(retry)
        # Another device stored this round: the list is read again (the
        # contract's words: "Erneut laden", so a context of its own).
        # A stored revision that is not the writer's: the next one goes
        # ("Erneut senden", 12.4 at bfa8844).
        again = progress.failure.retry if retry else None
        self.retry_button.setText(pgettext("submissions list", "Reload")
                                  if again is Step.RELOADING
                                  else _("Send Again") if again is Step.REVISING
                                  else _("Try Again"))
        retry_at = progress.failure.retry_at if retry else None
        waiting = retry_at is not None and retry_at > _now()
        self.retry_button.setEnabled(not waiting)
        self.done_button.setVisible(phase is Phase.DONE)
        self.view_button.setVisible(phase is Phase.DONE)
        # The one thing to do answers Return: Done, Try Again once it may
        # be tried, and Close where it stands alone.
        default = (self.done_button if phase is Phase.DONE
                   else self.retry_button if retry and not waiting
                   else self.close_button if phase in (Phase.FAILED, Phase.UNCONFIRMED)
                   and not retry else None)
        for button in (self.cancel_button, self.close_button, self.retry_button,
                       self.done_button):
            button.setDefault(button is default)


__all__ = ["HandoffStage", "HandoffView", "message_words", "phase_words", "receipt_facts",
           "wait_words", "work_facts"]
