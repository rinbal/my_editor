#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The app's one alert, laid out the way Apple's Human Interface Guidelines
describe alerts, and rendered the same on macOS, Windows and Linux.

Anatomy, top to bottom: the app icon (with a caution badge when something
could be lost), a bold title that states the situation or asks the
question, an optional short message, optional details behind Show Details,
an optional checkbox, and the buttons. The title lives inside the alert,
never in the window's title bar, which stays empty: Linux window managers
would print it there as a second headline.

Buttons carry a role instead of per-call styling:

    DEFAULT      the likely choice; accent colored, answers Return
    CANCEL       backs out; answers Escape
    DESTRUCTIVE  removes or discards something; red text, never the default
    NORMAL       any other choice

A QDialogButtonBox places them in each platform's own order (Cancel left of
the default on macOS and Linux, and so on), and every color comes from
theme.dialog_stylesheet, so no call site can paint light text on a light
background again.

Call sites use the functions at the bottom (ask, ask_with_checkbox, inform,
confirm_destructive). They all build the dialog through the module-level
``Alert`` name, which is the single seam tests replace.
"""

from dataclasses import dataclass

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

import theme
from constants import MONO_FONT

DEFAULT = "default"
CANCEL = "cancel"
DESTRUCTIVE = "destructive"
NORMAL = "normal"

_BOX_ROLES = {
    DEFAULT: QDialogButtonBox.AcceptRole,
    CANCEL: QDialogButtonBox.RejectRole,
    DESTRUCTIVE: QDialogButtonBox.DestructiveRole,
    NORMAL: QDialogButtonBox.ActionRole,
}

# Apple's alerts are narrow: long text wraps rather than widening the alert.
_WIDTH = 300
_WIDE = 380          # three buttons, or details to read
_ICON = 64

_EXTRA_CSS = """
QLabel#alert_title { font-size: 14px; font-weight: 600; }
QLabel#alert_message { font-size: 12px; }
QPushButton#alert_details { border: none; background: transparent; min-width: 0; padding: 2px; }
"""


@dataclass(frozen=True)
class Button:
    label: str
    value: object
    role: str = NORMAL
    tooltip: str = ""


OK = Button("OK", "ok", DEFAULT)


class Alert(QDialog):
    """The alert itself. Prefer the functions below; use this for tests."""

    def __init__(self, parent, *, title: str, message: str = "", buttons=(OK,),
                 caution: bool = False, details: str = "", checkbox: str = "",
                 is_dark: bool = None):
        super().__init__(parent)
        buttons = tuple(buttons)
        if sum(b.role == DEFAULT for b in buttons) != 1:
            raise ValueError("an alert needs exactly one default button")

        dark = theme.is_dark_active() if is_dark is None else is_dark
        self.setWindowTitle("")
        self.setWindowFlag(Qt.WindowCloseButtonHint, False)
        self.setStyleSheet(
            theme.dialog_stylesheet(dark) + _EXTRA_CSS
            + f"QPushButton#alert_details {{ color: {theme.dialog_link_color(dark)}; }}")

        cancel = next((b for b in buttons if b.role == CANCEL), None)
        default = next(b for b in buttons if b.role == DEFAULT)
        self._escape_value = (cancel or default).value
        self._value = self._escape_value

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.setSpacing(8)

        icon = QLabel()
        icon.setPixmap(_icon_pixmap(caution))
        icon.setAlignment(Qt.AlignCenter)
        layout.addWidget(icon)
        layout.addSpacing(4)

        self.title_label = self._text_label(title, "alert_title")
        layout.addWidget(self.title_label)
        self.message_label = self._text_label(message, "alert_message")
        self.message_label.setVisible(bool(message))
        layout.addWidget(self.message_label)

        self.details_view = None
        if details:
            layout.addWidget(self._details(details), 0, Qt.AlignHCenter)
            layout.addWidget(self.details_view)

        self.checkbox = None
        if checkbox:
            self.checkbox = QCheckBox(checkbox)
            layout.addSpacing(4)
            layout.addWidget(self.checkbox, 0, Qt.AlignHCenter)

        layout.addSpacing(12)
        self.button_box = QDialogButtonBox()
        self.buttons = {}
        for spec in buttons:
            button = self.button_box.addButton(spec.label, _BOX_ROLES[spec.role])
            button.setToolTip(spec.tooltip)
            button.setAutoDefault(False)
            button.setDefault(spec.role == DEFAULT)
            if spec.role == DESTRUCTIVE:
                button.setObjectName("destructive")
            button.clicked.connect(lambda _=False, v=spec.value: self._choose(v))
            self.buttons[spec.label] = button
        layout.addWidget(self.button_box)

        width = _WIDE if (len(buttons) > 2 or details) else _WIDTH
        self.setFixedWidth(width)
        self._fit_buttons(width - 40)

    # -- building ------------------------------------------------------------
    @staticmethod
    def _text_label(text: str, name: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName(name)
        label.setAlignment(Qt.AlignCenter)
        label.setWordWrap(True)
        label.setTextFormat(Qt.PlainText)   # names and errors are data, not markup
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        return label

    def _details(self, details: str) -> QPushButton:
        self.details_view = QPlainTextEdit(details)
        self.details_view.setReadOnly(True)
        self.details_view.setFont(QFont(MONO_FONT, 10))
        self.details_view.setMaximumHeight(140)
        self.details_view.hide()
        toggle = QPushButton("Show Details")
        toggle.setObjectName("alert_details")
        toggle.setAutoDefault(False)

        def flip():
            shown = not self.details_view.isVisible()
            self.details_view.setVisible(shown)
            toggle.setText("Hide Details" if shown else "Show Details")
            self.adjustSize()

        toggle.clicked.connect(flip)
        return toggle

    def _fit_buttons(self, available: int) -> None:
        """Side by side when they fit, otherwise stacked full width, as
        macOS does for long button titles."""
        buttons = self.button_box.buttons()
        needed = sum(b.sizeHint().width() for b in buttons) + 8 * (len(buttons) - 1)
        if needed > available:
            self.button_box.setOrientation(Qt.Vertical)
            self.button_box.setCenterButtons(True)
            for button in buttons:
                button.setMinimumWidth(available)

    # -- answering -----------------------------------------------------------
    def _choose(self, value) -> None:
        self._value = value
        self.accept()

    def reject(self) -> None:
        self._value = self._escape_value
        super().reject()

    @property
    def value(self):
        """The chosen button's value (Escape and closing count as Cancel)."""
        return self._value

    @property
    def checked(self) -> bool:
        return bool(self.checkbox and self.checkbox.isChecked())

    def run(self):
        self.exec()
        return self._value


def _icon_pixmap(caution: bool) -> QPixmap:
    """The app icon; with a caution badge when something could be lost, the
    way macOS marks a critical alert."""
    pixmap = QApplication.windowIcon().pixmap(_ICON, _ICON)
    if pixmap.isNull() or not caution:
        return pixmap
    pixmap = QPixmap(pixmap)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    size, x, y = 30.0, _ICON - 30.0, _ICON - 28.0
    triangle = QPainterPath(QPointF(x + size / 2, y))
    triangle.lineTo(x + size, y + size * 0.9)
    triangle.lineTo(x, y + size * 0.9)
    triangle.closeSubpath()
    painter.setPen(QPen(QColor("#FFFFFF"), 2))
    painter.setBrush(QColor("#F5B400"))
    painter.drawPath(triangle)
    painter.setPen(QPen(QColor("#3A2A00"), 3, Qt.SolidLine, Qt.RoundCap))
    painter.drawLine(QPointF(x + size / 2, y + 9), QPointF(x + size / 2, y + 17))
    painter.drawPoint(QPointF(x + size / 2, y + 22))
    painter.end()
    return pixmap


# -- what call sites use -------------------------------------------------------

def ask(parent, *, title: str, message: str = "", buttons, caution: bool = False,
        details: str = "", is_dark: bool = None):
    """Show an alert with these buttons; return the chosen button's value."""
    return Alert(parent, title=title, message=message, buttons=buttons,
                 caution=caution, details=details, is_dark=is_dark).run()


def ask_with_checkbox(parent, *, title: str, message: str = "", buttons, checkbox: str,
                      is_dark: bool = None):
    """Like ask(), with a checkbox such as "Remember this choice".
    Returns ``(value, checked)``."""
    alert = Alert(parent, title=title, message=message, buttons=buttons,
                  checkbox=checkbox, is_dark=is_dark)
    return alert.run(), alert.checked


def inform(parent, *, title: str, message: str = "", caution: bool = False,
           details: str = "", is_dark: bool = None) -> None:
    """Tell the person something that needs no decision: one OK button."""
    ask(parent, title=title, message=message, buttons=(OK,), caution=caution,
        details=details, is_dark=is_dark)


def confirm_destructive(parent, *, title: str, message: str = "", action: str,
                        caution: bool = False, details: str = "",
                        is_dark: bool = None) -> bool:
    """Ask before removing something. Cancel is the default, so Return never
    destroys anything. Pass ``caution`` when the loss can't be undone.
    True only when the action button was clicked."""
    return ask(parent, title=title, message=message, caution=caution, details=details,
               is_dark=is_dark,
               buttons=(Button(action, True, DESTRUCTIVE),
                        Button("Cancel", False, DEFAULT))) is True
