# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A notice at the top of a document: something about it the person
should know, said where it happened, without stopping them.

Apple's guidelines ask for information to be given "within the relevant
context" rather than in an alert, and for an alert only when a decision
protects work. So a document's file changing on disk, the file going
away, a recovered copy or a newer draft each show here: one sentence that
names the file, at most two actions named by what they do ("Revert…",
"Save As…") and Close, which keeps everything as it is. An action that
would throw work away asks first (the window's confirm_destructive).

It looks like the system's own: a quiet fill over the document's
background with a hairline under it, regular-weight text, an info or a
caution symbol drawn in the theme's ink, standard small buttons. It is as
tall as its sentence needs: one line in a wide window, more where a
narrow one wraps it, never cut. Screen readers hear the sentence when it
appears; Escape closes it while it has the focus, and the focus goes back
to the document.
"""

from __future__ import annotations

from typing import Iterable, Optional

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QToolButton, QWidget,
)

import announce
from constants import (
    DARK_BG, DARK_BORDER, DARK_FG, DARK_MENU_BG, DARK_MUTED_FG,
    LIGHT_BG, LIGHT_BORDER, LIGHT_FG, LIGHT_MENU_BG, LIGHT_MUTED_FG,
)
from i18n import _

INFO = "info"
CAUTION = "caution"

# The caution symbol's orange, as the system's: readable on either theme.
_CAUTION_INK = {True: "#FF9F0A", False: "#C93400"}


class _Symbol(QWidget):
    """The notice's symbol: an info circle, or a caution triangle."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(QSize(16, 16))
        self.kind = INFO
        self.ink = QColor(DARK_MUTED_FG)

    def paintEvent(self, event) -> None:  # noqa: N802, Qt's name
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(1, 1, 14, 14)
        if self.kind == CAUTION:
            triangle = QPainterPath()
            triangle.moveTo(r.center().x(), r.top())
            triangle.lineTo(r.right(), r.bottom())
            triangle.lineTo(r.left(), r.bottom())
            triangle.closeSubpath()
            painter.fillPath(triangle, self.ink)
            mark = QColor(DARK_BG if self.ink.lightness() > 128 else LIGHT_BG)
            painter.fillRect(QRectF(7.25, 5.5, 1.5, 4.5), mark)
            painter.fillRect(QRectF(7.25, 11.25, 1.5, 1.5), mark)
            return
        pen = QPen(self.ink, 1.3)
        painter.setPen(pen)
        painter.drawEllipse(r.adjusted(0.5, 0.5, -0.5, -0.5))
        painter.fillRect(QRectF(7.3, 7, 1.4, 4.5), self.ink)
        painter.fillRect(QRectF(7.3, 4.4, 1.4, 1.4), self.ink)


class DocumentNotice(QFrame):
    """The notice (see the module docstring). ``present`` shows a sentence
    with its actions; each action's button emits ``acted(key)``, Close
    emits ``closed``. Either hides it."""

    acted = Signal(str)
    closed = Signal()

    def __init__(self, parent: Optional[QWidget] = None, *, is_dark: bool = True, nostr: bool = False) -> None:
        super().__init__(parent)
        self.setObjectName("DocumentNotice")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._dark = is_dark
        self._nostr = nostr
        self._focus_return: Optional[QWidget] = None
        row = QHBoxLayout(self)
        row.setContentsMargins(14, 6, 8, 6)
        row.setSpacing(10)
        self._symbol = _Symbol()
        row.addWidget(self._symbol, 0, Qt.AlignmentFlag.AlignVCenter)
        self._text = QLabel()
        self._text.setObjectName("DocumentNoticeText")
        self._text.setWordWrap(True)
        self._text.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        row.addWidget(self._text, 1)
        self._actions = QHBoxLayout()
        self._actions.setSpacing(6)
        row.addLayout(self._actions)
        self._buttons: list = []
        self._close = QToolButton()
        self._close.setObjectName("DocumentNoticeClose")
        # The multiplication sign: in every font on every system, where the
        # heavier cross of the dingbats is not.
        self._close.setText("\u00d7")
        self._close.setToolTip(_("Close"))
        self._close.setAccessibleName(_("Close"))
        self._close.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self._close.clicked.connect(self._on_close)
        row.addWidget(self._close, 0, Qt.AlignmentFlag.AlignVCenter)
        self.apply_theme(is_dark)
        self.hide()

    def present(self, text: str, *, kind: str = INFO,
                actions: Iterable[tuple] = ()) -> None:
        """Show ``text`` with a symbol of ``kind`` (INFO or CAUTION) and a
        button for each ``(key, label)`` of ``actions``, in that order."""
        self._symbol.kind = kind
        self._text.setText(text)
        for old in self._buttons:
            self._actions.removeWidget(old)
            old.hide()
            old.deleteLater()
        self._buttons = []
        for key, label in actions:
            button = QPushButton(label, self)
            button.setObjectName("DocumentNoticeAction")
            button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            button.clicked.connect(lambda _checked=False, k=key: self._on_action(k))
            self._actions.addWidget(button)
            self._buttons.append(button)
        self.setAccessibleName(text)
        self._paint_symbol()
        self.show()
        if self.window().isVisible():
            announce.announce(self, text)

    def set_focus_return(self, widget: QWidget) -> None:
        """Where the focus goes when the notice closes from the keyboard:
        the document it is about."""
        self._focus_return = widget

    def text(self) -> str:
        return self._text.text()

    def action_labels(self) -> list:
        return [button.text() for button in self._buttons]

    def trigger(self, key: str) -> None:
        """As if the action ``key`` were clicked (menus and tests)."""
        self._on_action(key)

    def apply_theme(self, is_dark: bool) -> None:
        self._dark = is_dark
        if is_dark:
            fill, line, text, button, edge, muted = (
                "rgba(255, 255, 255, 0.05)", DARK_BORDER, DARK_FG, DARK_MENU_BG,
                DARK_MUTED_FG, DARK_MUTED_FG)
        else:
            fill, line, text, button, edge, muted = (
                "rgba(0, 0, 0, 0.035)", LIGHT_BORDER, LIGHT_FG, LIGHT_MENU_BG,
                LIGHT_MUTED_FG, LIGHT_MUTED_FG)
        if self._nostr:
            from nostr.ui.appearance import roles
            r = roles(is_dark)
            fill, line, text, button, edge, muted = r.soft, r.line, r.text, r.control, r.edge, r.ink
        self.setStyleSheet(f"""
            #DocumentNotice {{ background: {fill}; border: none; border-bottom: 1px solid {line}; }}
            #DocumentNoticeText {{ color: {text}; background: transparent; font-size: 13px; }}
            #DocumentNoticeAction {{
                color: {text}; background: {button}; border: 1px solid {edge};
                border-radius: 5px; padding: 2px 10px; font-size: 12px;
            }}
            #DocumentNoticeAction:hover {{ border-color: {text}; }}
            #DocumentNoticeAction:focus {{ border: 2px solid {text}; padding: 1px 9px; }}
            #DocumentNoticeClose {{
                color: {muted}; background: transparent; border: none;
                border-radius: 5px; padding: 0px 5px; font-size: 16px;
            }}
            #DocumentNoticeClose:hover {{ color: {text}; background: {fill}; }}
        """)
        self._paint_symbol()

    def keyPressEvent(self, event) -> None:  # noqa: N802, Qt's name
        if event.key() == Qt.Key.Key_Escape:
            self._on_close()
            return
        super().keyPressEvent(event)

    def _paint_symbol(self) -> None:
        if self._symbol.kind == CAUTION:
            self._symbol.ink = QColor(_CAUTION_INK[self._dark])
        else:
            if self._nostr:
                from nostr.ui.appearance import roles
                self._symbol.ink = QColor(roles(self._dark).icon)
            else:
                self._symbol.ink = QColor(DARK_MUTED_FG if self._dark else LIGHT_MUTED_FG)
        self._symbol.update()

    def _on_action(self, key: str) -> None:
        self._go_away()
        self.acted.emit(key)

    def _on_close(self) -> None:
        self._go_away()
        self.closed.emit()

    def _go_away(self) -> None:
        had_focus = self.isAncestorOf(QApplication.focusWidget())
        self.hide()
        if had_focus and self._focus_return is not None:
            self._focus_return.setFocus()
