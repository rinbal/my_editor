# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Technical facts behind a tooltip, reachable without a mouse.

Screens name things plainly and keep the technical facts for a tooltip
(lesson 26: "hover and keyboard focus, never hover only"). Qt shows a
tooltip on hover alone, so :func:`tip_on_focus` also shows it when the
keyboard moves the focus onto the widget, and reads it with the widget
(its accessible description). :class:`HelpButton` is the round help
button of Apple's guidelines ("?"), whose click shows the same text.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QPoint, Qt
from PySide6.QtWidgets import QToolButton, QToolTip, QWidget

from i18n import _

HELP_SIDE = 22                                  # a round help button's side (macOS)

_KEYBOARD = (Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason,
             Qt.FocusReason.ShortcutFocusReason)


def show_tip(widget: QWidget) -> None:
    """Show ``widget``'s tooltip under it, as a hover would."""
    text = widget.toolTip()
    if text:
        QToolTip.showText(widget.mapToGlobal(QPoint(0, widget.height())), text, widget)


class _FocusTip(QObject):
    def eventFilter(self, watched, event) -> bool:  # noqa: N802, Qt's name
        if event.type() == QEvent.Type.FocusIn and event.reason() in _KEYBOARD:
            show_tip(watched)
        elif event.type() == QEvent.Type.FocusOut:
            QToolTip.hideText()
        return False


def tip_on_focus(widget: QWidget, text: str) -> None:
    """``text`` as ``widget``'s tooltip, also shown when the keyboard
    gives it the focus, and read with it."""
    widget.setToolTip(text)
    widget.setAccessibleDescription(text)
    if not hasattr(widget, "_focus_tip"):
        widget._focus_tip = _FocusTip(widget)
        widget.installEventFilter(widget._focus_tip)


class HelpButton(QToolButton):
    """A round "?" whose tooltip holds the technical facts; a click, or
    Space with the focus on it, shows them too."""

    def __init__(self, facts: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("help_button")
        self.setText("?")
        self.setAccessibleName(_("Help"))
        self.setFixedSize(HELP_SIDE, HELP_SIDE)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        tip_on_focus(self, facts)
        self.clicked.connect(lambda: show_tip(self))


HELP_BUTTON_CSS = """
QToolButton#help_button {{
    border: 1px solid {edge}; border-radius: 11px; background: {field};
    color: {muted}; font-weight: 600; padding: 0px;
}}
QToolButton#help_button:focus {{ border-color: {ring}; }}
"""


def help_button_stylesheet(roles) -> str:
    """The help button's look in the window's roles (appearance.roles)."""
    return HELP_BUTTON_CSS.format(edge=roles.edge, field=roles.field, muted=roles.muted,
                                  ring=roles.focus)


__all__ = ["HELP_SIDE", "HelpButton", "help_button_stylesheet", "show_tip", "tip_on_focus"]
