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
the field goes and the focus returns to where it came from.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QRect, Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import QFrame, QLabel, QLineEdit, QVBoxLayout, QWidget

import photo_caption


class PictureField(QFrame):
    """One line over the text. ``title`` and ``hint`` make it a card (a
    picture's description); without them it is the bare line (a caption)."""

    committed = Signal(str)
    cancelled = Signal()

    def __init__(self, parent: QWidget, *, name: str, text: str, placeholder: str,
                 font: QFont, ink: QColor, surface: QColor, accent: QColor,
                 title: str = "", hint: str = "") -> None:
        super().__init__(parent)
        self._done = False
        self._card = bool(title)
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
        column.addWidget(self.line)
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
            f" border: 2px solid {accent.name()}; border-radius: 5px; padding: 2px 5px;"
            f" selection-background-color: {accent.name()};"
            "}")

    def place(self, box: QRect) -> None:
        """Show the field at ``box`` (the parent's coordinates), moved so
        that all of it is inside the parent, and write in it."""
        room = self.parentWidget().rect()
        width = max(box.width(), 240 if not self._card else 300)
        width = min(width, room.width() - 16)
        self.setFixedWidth(width)
        height = self.sizeHint().height()
        left = max(8, min(box.left(), room.right() - 8 - width))
        top = max(8, min(box.top(), room.bottom() - 8 - height))
        self.setGeometry(left, top, width, height)
        self.show()
        self.raise_()
        self.line.setFocus(Qt.FocusReason.OtherFocusReason)
        self.line.selectAll()

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

    def eventFilter(self, watched, event) -> bool:
        if watched is self.line:
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

