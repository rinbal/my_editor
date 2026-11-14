# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Apple's segmented control, drawn alike on every system.

    ╭───────────────────────────────╮
    │ ╭─────────────╮               │   a quiet track; the chosen
    │ │   Drafts    │   Published   │   segment on a raised, neutral
    │ ╰─────────────╯               │   thumb; the others as text
    ╰───────────────────────────────╯

One control for every switch between views: Write · Preview · Split in
the toolbar and Drafts · Published in the panel beside it, so the two
read as one family. Apple's guidelines for segmented controls ask for
segments of equal width ("When all segments have equal width, a
segmented control feels balanced"), nouns as labels and a tooltip on
each segment. The chosen segment is a raised thumb in a neutral color,
as in macOS, not the accent fill a default button or a list selection
uses: a view switch says where you are, it does not ask for a click.

Each segment is a checkable button, so a screen reader names it and
says whether it is the chosen one. Tab reaches the control on the
chosen segment, as on a group of radio buttons, and the arrow keys move
the choice (passing over a dimmed segment). A click does not draw the
focus ring, the keyboard does.

The control knows nothing about what it switches. ``chosen(key)`` says
which segment was picked; :class:`ActionSegments` binds segments to
QActions instead (the toolbar's View commands).
"""

from __future__ import annotations

from typing import List, NamedTuple, Optional, Sequence, Tuple

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QAction, QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QAbstractButton, QHBoxLayout, QSizePolicy, QWidget

import theme

# Room inside the track around the segments, and inside a segment around
# its label.
TRACK_INSET = 2
LABEL_PAD = 12
TRACK_RADIUS = 7.0
THUMB_RADIUS = 5.0
MIN_HEIGHT = 26
# The dot of a marked segment (something new waits there), at the upper
# end of its label, inside the label's padding.
MARK_SIZE = 6.0


class Colors(NamedTuple):
    """One theme's colors. ``track`` is translucent, drawn over the band
    the control sits on; everything else is opaque."""

    track: QColor
    track_line: QColor
    divider: QColor
    thumb: QColor
    thumb_line: QColor
    thumb_shadow: QColor
    text: QColor           # the chosen segment's label
    muted: QColor          # the other labels
    focus: QColor
    mark: QColor = QColor("#007AFF")    # a marked segment's dot: the system's blue


def colors(dark: bool) -> Colors:
    """The control's colors in the dark or the light theme.

    Measured against the toolbar band (the menu background): the chosen
    label is 8.0:1 on its thumb in dark and 17.4:1 in light, the other
    labels 5.4:1 and 5.3:1 on the track.
    """
    if dark:
        return Colors(
            track=QColor(255, 255, 255, 18), track_line=QColor(255, 255, 255, 22),
            divider=QColor(255, 255, 255, 34), thumb=QColor("#505054"),
            thumb_line=QColor(255, 255, 255, 30), thumb_shadow=QColor(0, 0, 0, 90),
            text=QColor("#FFFFFF"), muted=QColor("#ABABAB"),
            focus=QColor(theme.focus_ring_color(True)), mark=QColor("#0A84FF"))
    return Colors(
        track=QColor(0, 0, 0, 15), track_line=QColor(0, 0, 0, 14),
        divider=QColor(0, 0, 0, 38), thumb=QColor("#FFFFFF"),
        thumb_line=QColor(0, 0, 0, 22), thumb_shadow=QColor(0, 0, 0, 40),
        text=QColor("#1A1A1A"), muted=QColor("#5C5C5C"),
        focus=QColor(theme.focus_ring_color(False)))


class Segment(QAbstractButton):
    """One segment: its label, and its thumb while it is the chosen one.

    The label is shortened at paint time from the segment's own width, so
    a squeezed segment ends in an ellipsis instead of losing both ends of
    its word; ``text()`` stays the whole label for the layout, the
    tooltip and screen readers."""

    def __init__(self, key: str, title: str, tooltip: str, control: "SegmentedControl") -> None:
        super().__init__(control)
        self._control = control
        self.key = key
        self._action: Optional[QAction] = None
        self._keyboard_focus = False
        self._marked = False
        self.setText(title)
        self.setCheckable(True)
        self.setAccessibleName(title)
        self.setToolTip(tooltip)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def defaultAction(self) -> Optional[QAction]:  # noqa: N802, as QToolButton names it
        """The command this segment stands for, when it is bound to one."""
        return self._action

    def bind(self, action: QAction) -> None:
        self._action = action

    def set_marked(self, marked: bool, spoken: str = "") -> None:
        """A dot at the label: something new waits in what the segment
        shows. ``spoken`` is what a screen reader hears after the title
        then (the dot is nothing to it)."""
        self._marked = bool(marked)
        self.setAccessibleName(f"{self.text()}, {spoken}" if marked and spoken
                               else self.text())
        self.update()

    def is_marked(self) -> bool:
        return self._marked

    def label_width(self) -> int:
        return self.fontMetrics().horizontalAdvance(self.text()) + 2 * LABEL_PAD

    def sizeHint(self) -> QSize:
        return QSize(self.label_width(), self._control.segment_height())

    def minimumSizeHint(self) -> QSize:
        return QSize(2 * LABEL_PAD, self._control.segment_height())

    def painted_text(self) -> str:
        """The label as it is drawn at the segment's current width."""
        room = max(0, self.width() - 2 * LABEL_PAD)
        # Whole wherever it measures to fit: elidedText rounds differently
        # from horizontalAdvance and would cut a label that just fits.
        if self.fontMetrics().horizontalAdvance(self.text()) <= room:
            return self.text()
        return self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight, room)

    def focusInEvent(self, event) -> None:
        # The ring answers the keyboard (Tab, Shift-Tab, a shortcut); a
        # click, or the window handing focus out as it opens, draws none.
        self._keyboard_focus = event.reason() in (Qt.FocusReason.TabFocusReason,
                                                  Qt.FocusReason.BacktabFocusReason,
                                                  Qt.FocusReason.ShortcutFocusReason)
        super().focusInEvent(event)

    def focusOutEvent(self, event) -> None:
        self._keyboard_focus = False
        super().focusOutEvent(event)

    def keyPressEvent(self, event) -> None:
        step = {Qt.Key.Key_Right: 1, Qt.Key.Key_Down: 1,
                Qt.Key.Key_Left: -1, Qt.Key.Key_Up: -1}.get(event.key())
        if step is None or event.modifiers() not in (Qt.KeyboardModifier.NoModifier,
                                                      Qt.KeyboardModifier.KeypadModifier):
            super().keyPressEvent(event)
            return
        if event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right) and self.isRightToLeft():
            step = -step
        self._control.step_from(self, step)
        event.accept()

    def paintEvent(self, event) -> None:
        palette = self._control.palette_colors()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        box = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if self.isChecked():
            shadow = QPainterPath()
            shadow.addRoundedRect(box.translated(0, 0.75), THUMB_RADIUS, THUMB_RADIUS)
            painter.fillPath(shadow, palette.thumb_shadow)
            thumb = QPainterPath()
            thumb.addRoundedRect(box, THUMB_RADIUS, THUMB_RADIUS)
            painter.fillPath(thumb, palette.thumb)
            painter.setPen(QPen(palette.thumb_line, 1))
            painter.drawPath(thumb)
        if self.hasFocus() and self._keyboard_focus:
            painter.setPen(QPen(palette.focus, 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(box.adjusted(0.5, 0.5, -0.5, -0.5),
                                    THUMB_RADIUS, THUMB_RADIUS)
        label = QColor(palette.text if self.isChecked() else palette.muted)
        if not self.isEnabled():
            label.setAlphaF(label.alphaF() * 0.45)
        painter.setPen(label)
        painter.setFont(self.font())
        text = self.painted_text()
        painter.drawText(self.rect(), int(Qt.AlignmentFlag.AlignCenter), text)
        if self._marked:
            metrics = self.fontMetrics()
            after = (self.width() + metrics.horizontalAdvance(text)) / 2.0 + 2.0
            x = min(after, self.width() - MARK_SIZE - 1.0)
            y = self.height() / 2.0 - metrics.capHeight() / 2.0 - MARK_SIZE / 2.0
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(palette.mark)
            painter.drawEllipse(QRectF(x, max(1.0, y), MARK_SIZE, MARK_SIZE))


class SegmentedControl(QWidget):
    """Equal segments on a track; ``chosen(key)`` when one is picked.

    Public:
      set_segments(items)        ``(key, title, hint)`` each, in order
      set_current(key) / current()
      set_segment_enabled(key, enabled)
      set_segment_marked(key, marked, spoken)   a dot: something new waits there
      set_dark(dark)
      segments                   the Segment buttons, in order
    """

    chosen = Signal(str)

    def __init__(self, *, dark: bool = False, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._dark = bool(dark)
        self._current = ""
        self.segments: List[Segment] = []
        self._row = QHBoxLayout(self)
        self._row.setContentsMargins(TRACK_INSET, TRACK_INSET, TRACK_INSET, TRACK_INSET)
        self._row.setSpacing(0)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    # -- what it shows -------------------------------------------------------

    def set_segments(self, items: Sequence[Tuple[str, str, str]]) -> None:
        """One segment per ``(key, title, hint)``; the tooltip reads
        "Title: hint", the title alone without a hint."""
        for segment in self.segments:
            self._row.removeWidget(segment)
            segment.hide()
            segment.deleteLater()
        self.segments = []
        for key, title, hint in items:
            segment = Segment(key, title, f"{title}: {hint}" if hint else title, self)
            segment.clicked.connect(self._on_clicked)
            self._row.addWidget(segment, 1)
            self.segments.append(segment)
        self._sync()
        self.updateGeometry()

    def set_current(self, key: str) -> None:
        self._current = key
        self._sync()

    def current(self) -> str:
        return self._current

    def set_segment_enabled(self, key: str, enabled: bool) -> None:
        for segment in self.segments:
            if segment.key == key:
                segment.setEnabled(enabled)

    def set_segment_marked(self, key: str, marked: bool, spoken: str = "") -> None:
        """A dot at the segment ``key`` (``Segment.set_marked``)."""
        for segment in self.segments:
            if segment.key == key:
                segment.set_marked(marked, spoken)

    def set_dark(self, dark: bool) -> None:
        self._dark = bool(dark)
        self.update()
        for segment in self.segments:
            segment.update()

    def palette_colors(self) -> Colors:
        return colors(self._dark)

    # -- size ----------------------------------------------------------------

    def segment_height(self) -> int:
        return max(MIN_HEIGHT, self.fontMetrics().height() + 10) - 2 * TRACK_INSET

    def sizeHint(self) -> QSize:
        widest = max((segment.label_width() for segment in self.segments), default=0)
        return QSize(widest * len(self.segments) + 2 * TRACK_INSET,
                     self.segment_height() + 2 * TRACK_INSET)

    def minimumSizeHint(self) -> QSize:
        return QSize(2 * LABEL_PAD * max(1, len(self.segments)) + 2 * TRACK_INSET,
                     self.sizeHint().height())

    # -- choosing ------------------------------------------------------------

    def _on_clicked(self) -> None:
        segment = self.sender()
        if isinstance(segment, Segment):
            self._choose(segment)

    def _choose(self, segment: Segment) -> None:
        self._current = segment.key
        self._sync()
        self.chosen.emit(segment.key)

    def step_from(self, segment: Segment, step: int) -> None:
        """The arrow keys: the next segment that can be chosen, focused."""
        index = self.segments.index(segment)
        count = len(self.segments)
        for offset in range(1, count):
            candidate = self.segments[(index + step * offset) % count]
            if candidate.isEnabled():
                self._choose(candidate)
                candidate.setFocus(Qt.FocusReason.TabFocusReason)
                return

    def _sync(self) -> None:
        for segment in self.segments:
            chosen = segment.key == self._current
            segment.setChecked(chosen)
            # Tab reaches the control on the chosen segment, as it reaches
            # a group of radio buttons on the one chosen.
            segment.setFocusPolicy(Qt.FocusPolicy.StrongFocus if chosen
                                   else Qt.FocusPolicy.ClickFocus)
        self.update()

    # -- drawing -------------------------------------------------------------

    def paintEvent(self, event) -> None:
        palette = self.palette_colors()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        track = QPainterPath()
        track.addRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5),
                             TRACK_RADIUS, TRACK_RADIUS)
        painter.fillPath(track, palette.track)
        painter.setPen(QPen(palette.track_line, 1))
        painter.drawPath(track)
        # A faint line between two segments neither of which is chosen.
        painter.setPen(QPen(palette.divider, 1))
        for left, right in zip(self.segments, self.segments[1:]):
            if left.isChecked() or right.isChecked():
                continue
            x = right.geometry().left() + 0.5
            inset = self.height() * 0.3
            painter.drawLine(QPointF(x, inset), QPointF(x, self.height() - inset))


class ActionSegments(SegmentedControl):
    """Segments that stand for checkable QActions (the window's View
    commands): a click triggers the action, and the action's checked and
    enabled states are what the segments show, so the menu and the
    control can never disagree."""

    def __init__(self, actions: Sequence[QAction], *, dark: bool = False,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(dark=dark, parent=parent)
        self._actions = list(actions)
        self.set_segments([(str(index), action.text().replace("&", ""), "")
                           for index, action in enumerate(self._actions)])
        for segment, action in zip(self.segments, self._actions):
            segment.bind(action)
            segment.setToolTip(action.toolTip() or segment.text())
            action.changed.connect(self._follow_actions)
        self.chosen.connect(self._trigger)
        self._follow_actions()

    def _trigger(self, key: str) -> None:
        action = self._actions[int(key)]
        if not action.isChecked():
            action.trigger()
        self._follow_actions()

    def _follow_actions(self) -> None:
        checked = next((str(i) for i, a in enumerate(self._actions) if a.isChecked()), "")
        for segment, action in zip(self.segments, self._actions):
            segment.setEnabled(action.isEnabled())
        if checked != self.current():
            self.set_current(checked)
