# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A line to write a picture's caption or its description in, in place.

A caption of a photo group is drawn with its picture, not text of the
document, so it is written in a field that lies over where it is drawn:
in the caption's own font, as wide as the picture's figure. A picture's
description (its alt text) is not shown in the document at all: its field
is a small card on the picture, with one sentence on what it is for.

Return takes what was written, and so does a click elsewhere, as renaming
a file in the Finder does; Escape leaves everything as it was. Either way
the field goes and the focus returns to where it came from. Tab takes
what was written too and asks for the same field of the next picture
(Shift-Tab: of the one before), as Tab goes from cell to cell of a table:
a group's captions or descriptions are written one after another without
the pointer.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QRect, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF
from PySide6.QtWidgets import QFrame, QLabel, QLineEdit, QVBoxLayout, QWidget

import photo_caption

# The line's frame and the room inside it (its style sheet below), and the
# margin a line edit keeps around its words by itself: together, how far
# inside the field its words start.
_BORDER = 2
_PAD_X, _PAD_Y = 5, 2
_LINE_MARGIN_X, _LINE_MARGIN_Y = 2, 1
_INSET_X = _BORDER + _PAD_X + _LINE_MARGIN_X
_INSET_Y = _BORDER + _PAD_Y + _LINE_MARGIN_Y


class PictureField(QFrame):
    """One line over the text. ``title`` and ``hint`` make it a card (a
    picture's description); without them it is the bare line (a caption).
    ``short_placeholder`` stands in an empty line too narrow for
    ``placeholder``, which would be cut off there."""

    committed = Signal(str)
    cancelled = Signal()
    # Tab (1) or Shift-Tab (-1), with what is written: taken, and on to
    # the next picture's field.
    stepped = Signal(str, int)

    def __init__(self, parent: QWidget, *, name: str, text: str, placeholder: str,
                 font: QFont, ink: QColor, surface: QColor, accent: QColor,
                 title: str = "", hint: str = "", short_placeholder: str = "") -> None:
        super().__init__(parent)
        self._done = False
        self._card = bool(title)
        self._placeholders = (placeholder, short_placeholder)
        self.setObjectName("pictureField")
        column = QVBoxLayout(self)
        column.setContentsMargins(*((12, 10, 12, 10) if self._card else (0, 0, 0, 0)))
        column.setSpacing(6)
        faint = QColor(ink)
        faint.setAlphaF(0.62)
        if title:
            heading = QLabel(title)
            heading.setStyleSheet(f"color: {ink.name()}; font-weight: 600;")
            column.addWidget(heading)
        self.line = QLineEdit(text)
        self.line.setObjectName("pictureFieldLine")
        self.line.setFont(font)
        self.line.setPlaceholderText(placeholder)
        self.line.setAccessibleName(name)
        self.line.setAccessibleDescription(hint)
        self.line.installEventFilter(self)
        column.addWidget(self.line, 0, Qt.AlignmentFlag.AlignTop)
        if hint:
            note = QLabel(hint)
            note.setWordWrap(True)
            note.setStyleSheet(f"color: {faint.name(QColor.NameFormat.HexArgb)};")
            column.addWidget(note)
        edge = QColor(ink)
        edge.setAlphaF(0.22)
        self.setStyleSheet(
            "#pictureField {"
            f" background: {surface.name()};"
            + (f" border: 1px solid {edge.name(QColor.NameFormat.HexArgb)}; border-radius: 10px;"
               if self._card else " border: none;")
            + "}"
            "#pictureFieldLine {"
            f" background: {surface.name()}; color: {ink.name()};"
            f" border: {_BORDER}px solid {accent.name()}; border-radius: 5px;"
            f" padding: {_PAD_Y}px {_PAD_X}px;"
            f" selection-background-color: {accent.name()};"
            "}")

    def place(self, box: QRect, *, cover: bool = False) -> None:
        """Show the field at ``box`` (the parent's coordinates), moved so
        that all of it is inside the parent, and write in it. The bare
        line's words start where ``box`` does, so a caption clicked stays
        where it was drawn while it is written. With ``cover`` the field
        hides all of ``box`` under its one line: a caption that is text
        of the document and runs over more lines than the field has."""
        room = self.parentWidget().rect()
        if self._card:
            # A card wider than its picture stands over the picture's
            # middle, not off its right edge.
            width = max(box.width(), 300)
            left, top = box.center().x() - width // 2, box.top()
        else:
            left, top = box.left() - _INSET_X, box.top() - _INSET_Y
            width = max(box.width() + 2 * _INSET_X, 240)
        width = min(width, room.width() - 16)
        self.setFixedWidth(width)
        long, short = self._placeholders
        if short and QFontMetricsF(self.line.font()).horizontalAdvance(long) > width - 2 * _INSET_X:
            self.line.setPlaceholderText(short)
        height = self.sizeHint().height()
        if cover and not self._card:
            height = max(height, box.height() + _INSET_Y)       # to the box's last line, no further
        left = max(8, min(left, room.right() - 8 - width))
        top = max(8, min(top, room.bottom() - 8 - height))
        self.setGeometry(left, top, width, height)
        self.show()
        self.raise_()
        self.line.setFocus(Qt.FocusReason.OtherFocusReason)
        # All of it selected, from its end back to its start: words longer
        # than the line show their beginning, not their end.
        written = len(self.line.text())
        self.line.setSelection(written, -written)

    def text(self) -> str:
        """What is written, on one line, without spaces around it."""
        return photo_caption.one_line(self.line.text())

    def commit(self) -> None:
        if not self._done:
            self._done = True
            self.committed.emit(self.text())

    def cancel(self) -> None:
        if not self._done:
            self._done = True
            self.cancelled.emit()

    def step(self, by: int) -> None:
        if not self._done:
            self._done = True
            self.stepped.emit(self.text(), by)

    def eventFilter(self, watched, event) -> bool:
        if watched is self.line:
            if event.type() == QEvent.Type.KeyPress and event.key() in (
                    Qt.Key.Key_Tab, Qt.Key.Key_Backtab) and not event.modifiers() & (
                    Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier
                    | Qt.KeyboardModifier.MetaModifier):
                # Taken here: the text under the field would get the key.
                back = (event.key() == Qt.Key.Key_Backtab
                        or bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier))
                self.step(-1 if back else 1)
                return True
            if event.type() == QEvent.Type.KeyPress and event.key() in (
                    Qt.Key.Key_Escape, Qt.Key.Key_Return, Qt.Key.Key_Enter):
                # Taken here: a line edit passes Return on to the text
                # under it, which would open the field again.
                if event.key() == Qt.Key.Key_Escape:
                    self.cancel()
                else:
                    self.commit()
                return True
            if event.type() == QEvent.Type.FocusOut and not self._done \
                    and event.reason() != Qt.FocusReason.PopupFocusReason:
                # A click elsewhere keeps what was written (not the
                # field's own context menu opening).
                self.commit()
        return super().eventFilter(watched, event)

