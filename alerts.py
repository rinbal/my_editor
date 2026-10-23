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

The role also decides where a button goes, the same on every platform, as
the Human Interface Guidelines lay it out: in a row, the default at the
trailing edge with Cancel beside it, other choices next to Cancel, and a
destructive one at the leading edge, away from the default; in a stack
(when the titles are too long for a row), the default on top and Cancel at
the bottom. A QDialogButtonBox has no stack and no leading destructive
button, so the alert lays them out itself. (Other dialogs' button boxes
follow the same row order through the app's style, theme.AppStyle.) The
keyboard follows the same order, and the default button has the focus,
so Space and Return both choose it; Return on another focused button
follows the system (answer_return_as_the_system_does).

Every color comes from theme.dialog_stylesheet, so no call site can paint
light text on a light background again.

Call sites use the functions at the bottom (ask, ask_with_checkbox, inform,
confirm_destructive). They all build the dialog through the module-level
``Alert`` name, which is the single seam tests replace.
"""

import sys
from dataclasses import dataclass

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QBoxLayout,
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

import theme
from fonts import monospace_font
from i18n import _

DEFAULT = "default"
CANCEL = "cancel"
DESTRUCTIVE = "destructive"
NORMAL = "normal"

# Leading to trailing in a row, after the destructive buttons at the leading
# edge; and top to bottom in a stack.
_ROW_TRAILING = (NORMAL, CANCEL, DEFAULT)
_STACK = (DEFAULT, NORMAL, DESTRUCTIVE, CANCEL)

# Apple's alerts are narrow: long text wraps rather than widening the alert.
_WIDTH = 300
_WIDE = 380          # three buttons, or details to read
_ICON = 64
_BUTTON_SPACING = 8

_EXTRA_CSS = """
QLabel#alert_title { font-size: 14px; font-weight: 600; }
QLabel#alert_message { font-size: 12px; }
QPushButton#alert_details {
    border: 2px solid transparent; border-radius: 3px; background: transparent;
    min-width: 0; padding: 0px;
}
"""


# Whether the keyboard follows the Mac (see answer_return_as_the_system_does).
IS_MAC = sys.platform == "darwin"

@dataclass(frozen=True)
class Button:
    label: str
    value: object
    role: str = NORMAL
    tooltip: str = ""


OK = Button(_("OK"), "ok", DEFAULT)


class Alert(QDialog):
    """The alert itself. Prefer the functions below; use this for tests."""

    def __init__(self, parent, *, title: str, message: str = "", buttons=(OK,),
                 caution: bool = False, details: str = "", checkbox: str = "",
                 is_dark: bool = None, nostr: bool = False):
        super().__init__(parent)
        buttons = tuple(buttons)
        if sum(b.role == DEFAULT for b in buttons) != 1:
            raise ValueError("an alert needs exactly one default button")

        dark = theme.is_dark_active() if is_dark is None else is_dark
        self.setWindowTitle("")
        self.setWindowFlag(Qt.WindowCloseButtonHint, False)
        self._nostr = nostr
        self.apply_theme(dark)
        if nostr:
            from nostr.ui.appearance import bind
            bind(self)

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
        self._details_toggle = None
        if details:
            self._details_toggle = self._details(details)
            layout.addWidget(self._details_toggle, 0, Qt.AlignHCenter)
            layout.addWidget(self.details_view)

        self.checkbox = None
        if checkbox:
            self.checkbox = QCheckBox(checkbox)
            layout.addSpacing(4)
            layout.addWidget(self.checkbox, 0, Qt.AlignHCenter)

        layout.addSpacing(12)
        width = _WIDE if (len(buttons) > 2 or details) else _WIDTH
        self.setFixedWidth(width)
        self.buttons = {}
        made = []
        # Made inside the alert from the start, so they are measured with its
        # stylesheet when the row is chosen.
        self.button_row = QWidget(self)
        for spec in buttons:
            button = QPushButton(spec.label, self.button_row)
            button.setToolTip(spec.tooltip)
            answer_return_as_the_system_does([button])
            button.setDefault(spec.role == DEFAULT)
            if spec.role == DESTRUCTIVE:
                button.setObjectName("destructive")
            button.clicked.connect(lambda _=False, v=spec.value: self._choose(v))
            self.buttons[spec.label] = button
            made.append((spec.role, button))
        self.ordered_buttons = self._lay_out(made, width - 40)
        layout.addWidget(self.button_row)
        self._follow_in_tab_order(
            [w for w in (self._details_toggle, self.details_view, self.checkbox)
             if w is not None] + self.ordered_buttons)
        start_on_default(next(b for role, b in made if role == DEFAULT))

    def apply_theme(self, dark: bool) -> None:
        if self._nostr:
            from nostr.ui import appearance
            appearance.surface(self, dark)
            css = appearance.dialog_stylesheet(dark)
            link, ring = appearance.roles(dark).ink, appearance.roles(dark).focus
        else:
            css = theme.dialog_stylesheet(dark)
            link, ring = theme.dialog_link_color(dark), theme.focus_ring_color(dark)
        self.setStyleSheet(css + _EXTRA_CSS
            + f"QPushButton#alert_details {{ color: {link}; }}"
            + f"QPushButton#alert_details:focus {{ border-color: {ring}; }}")

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
        self.details_view.setAccessibleName(_("Details"))
        self.details_view.setReadOnly(True)
        self.details_view.setFont(monospace_font(10))
        self.details_view.setMaximumHeight(140)
        self.details_view.hide()
        toggle = QPushButton(_("Show Details"))
        toggle.setObjectName("alert_details")
        answer_return_as_the_system_does([toggle])

        def flip():
            shown = not self.details_view.isVisible()
            self.details_view.setVisible(shown)
            toggle.setText(_("Hide Details") if shown else _("Show Details"))
            self.adjustSize()

        toggle.clicked.connect(flip)
        return toggle

    def _lay_out(self, made, available: int) -> list:
        """Put the ``(role, button)`` pairs in a row when they fit, otherwise
        in a stack, full width, as macOS does for long button titles.
        Returns the buttons in the order they appear (and are tabbed to)."""
        buttons = [button for _role, button in made]
        needed = (sum(b.sizeHint().width() for b in buttons)
                  + _BUTTON_SPACING * (len(buttons) - 1))
        if needed <= available:
            row = QHBoxLayout(self.button_row)
            leading = [b for role, b in made if role == DESTRUCTIVE]
            trailing = [b for kind in _ROW_TRAILING for role, b in made if role == kind]
            for button in leading:
                row.addWidget(button)
            row.addStretch(1)
            for button in trailing:
                row.addWidget(button)
            ordered = leading + trailing
        else:
            row = QVBoxLayout(self.button_row)
            ordered = [b for kind in _STACK for role, b in made if role == kind]
            for button in ordered:
                button.setMinimumWidth(available)
                row.addWidget(button)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(_BUTTON_SPACING)
        return ordered

    @property
    def orientation(self) -> Qt.Orientation:
        """Qt.Horizontal for a row of buttons, Qt.Vertical for a stack."""
        layout = self.button_row.layout()
        return (Qt.Vertical if layout.direction() in (QBoxLayout.TopToBottom,
                                                      QBoxLayout.BottomToTop)
                else Qt.Horizontal)

    @staticmethod
    def _follow_in_tab_order(widgets) -> None:
        """Tab moves through ``widgets`` in this order: the order they are seen in."""
        for first, second in zip(widgets, widgets[1:]):
            QWidget.setTabOrder(first, second)

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


def start_on_default(button: QPushButton) -> None:
    """Give a dialog's default button the keyboard focus.

    Space acts on the button with the focus, so it starts on the default,
    the choice Return makes. Where Tab skips buttons (a Mac without
    keyboard navigation) nothing has the focus, as in a native alert, and
    Return still chooses the default."""
    if QGuiApplication.styleHints().tabFocusBehavior() == Qt.TabFocusAllControls:
        button.setFocus()


def answer_return_as_the_system_does(buttons) -> None:
    """Let a dialog's push buttons answer Return the way the system's own
    dialogs do. On a Mac, Return chooses the default button whichever button
    has the focus, and Space the focused one. On Windows and Linux, Return
    on a focused button presses that button (the default moves with the
    focus there, and so does the accent that marks it). From any other
    control, Return chooses the default everywhere.

    For a button box, pass ``box.buttons()`` once it has them, and make no
    button the default by hand: the box makes its accept button the default
    when the dialog is shown. (Done before the box was in its dialog, that
    left Qt to choose the first button in the row, Cancel.)"""
    for button in buttons:
        if isinstance(button, QPushButton):
            button.setAutoDefault(not IS_MAC)


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
        details: str = "", is_dark: bool = None, nostr: bool = False):
    """Show an alert with these buttons; return the chosen button's value."""
    return Alert(parent, title=title, message=message, buttons=buttons,
                 caution=caution, details=details, is_dark=is_dark, nostr=nostr).run()


def ask_with_checkbox(parent, *, title: str, message: str = "", buttons, checkbox: str,
                      is_dark: bool = None, nostr: bool = False):
    """Like ask(), with a checkbox such as "Remember this choice".
    Returns ``(value, checked)``."""
    alert = Alert(parent, title=title, message=message, buttons=buttons,
                  checkbox=checkbox, is_dark=is_dark, nostr=nostr)
    return alert.run(), alert.checked


def inform(parent, *, title: str, message: str = "", caution: bool = False,
           details: str = "", is_dark: bool = None, nostr: bool = False) -> None:
    """Tell the person something that needs no decision: one OK button."""
    ask(parent, title=title, message=message, buttons=(OK,), caution=caution,
        details=details, is_dark=is_dark, nostr=nostr)


def confirm_destructive(parent, *, title: str, message: str = "", action: str,
                        caution: bool = False, details: str = "",
                        is_dark: bool = None, nostr: bool = False) -> bool:
    """Ask before removing something. Cancel is the default, so Return never
    destroys anything. Pass ``caution`` when the loss can't be undone.
    True only when the action button was clicked."""
    return ask(parent, title=title, message=message, caution=caution, details=details,
               is_dark=is_dark, nostr=nostr,
               buttons=(Button(action, True, DESTRUCTIVE),
                        Button(_("Cancel"), False, DEFAULT))) is True
