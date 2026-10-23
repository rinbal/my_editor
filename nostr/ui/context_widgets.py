# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Two mixed-context controls: ownership comes from callers/domain state."""
from __future__ import annotations

import weakref

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QStatusBar, QStyle, QStyleOptionTab, QStylePainter, QTabBar, QWidget, QLabel

import theme
from . import appearance


def owns_editor(editor) -> bool:
    """Existing routing/restoration state, independent of the active account."""
    if editor is None:
        return False
    from .save_destination_dialog import SaveDestination
    return (getattr(editor, "_draft_binding", None) is not None
            or getattr(editor, "_save_destination", None) is SaveDestination.NOSTR_DRAFT
            or (getattr(editor, "_published_article", None) is not None
                and not getattr(editor, "_file_path", None)))


class ContextTabBar(QTabBar):
    def __init__(self, owner):
        super().__init__()
        self._owner = weakref.ref(owner)

    def is_nostr(self, index: int) -> bool:
        owner = self._owner()
        return bool(owner and owns_editor(owner._editor_from_widget(owner.tabs.widget(index))))

    def tabSizeHint(self, index):
        size = super().tabSizeHint(index)
        if self.is_nostr(index):
            size.setWidth(size.width() + 20)
        return size

    def paintEvent(self, event):
        painter = QStylePainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        r = appearance.roles(theme.is_dark_active())
        # Let Qt draw all the usual local tabs, focus, scrolling and movement.
        # Only a tab whose document already has Nostr ownership gets this fill.
        order = [i for i in range(self.count()) if i != self.currentIndex()]
        if self.currentIndex() >= 0:
            order.append(self.currentIndex())
        for index in order:
            option = QStyleOptionTab()
            self.initStyleOption(option, index)
            if not self.is_nostr(index):
                painter.drawControl(QStyle.CE_TabBarTab, option)
                continue
            rect = QRectF(option.rect).adjusted(1, 1, -2, 0)
            painter.setPen(QPen(QColor(r.edge), 1))
            painter.setBrush(QColor(r.soft if index == self.currentIndex() else r.tab))
            painter.drawRoundedRect(rect, 7, 7)
            text_rect = self.style().subElementRect(QStyle.SE_TabBarTabText, option, self)
            mark = 14
            appearance.relay_icon(theme.is_dark_active(), mark).paint(
                painter, text_rect.left(), text_rect.center().y() - mark // 2, mark, mark)
            text_rect.adjust(mark + 6, 0, 0, 0)
            painter.setPen(QColor(r.ink))
            painter.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft,
                             self.fontMetrics().elidedText(self.tabText(index), Qt.ElideRight,
                                                           max(0, text_rect.width())))
            if option.state & QStyle.State_HasFocus:
                painter.setPen(QPen(QColor(r.focus), 2))
                painter.setBrush(Qt.NoBrush)
                painter.drawRoundedRect(rect.adjusted(2, 2, -2, -2), 5, 5)


class ContextStatusBar(QStatusBar):
    """The temporary message has an origin; permanent local counters do not."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self._nostr_message = False
        self._message_error = False
        self.messageChanged.connect(self._message_changed)

    def showMessage(self, message: str, timeout: int = 0, *, nostr: bool = False,
                    error: bool = False) -> None:
        self._nostr_message = nostr and bool(message)
        self._message_error = error
        super().showMessage(message, timeout)
        self.update()

    def _message_changed(self, message):
        if not message:
            self._nostr_message = False
            self._message_error = False
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self._nostr_message or not self.currentMessage():
            return
        # Qt still owns message expiry, visibility of ordinary items and layout.
        # Paint only its temporary-message area, before the permanent controls.
        right = min((w.x() for w in self.findChildren(QWidget, options=Qt.FindDirectChildrenOnly)
                     if w.isVisible() and w.x() > 0), default=self.width())
        rect = self.rect().adjusted(4, 2, -(self.width() - right + 4), -2)
        painter = QPainter(self)
        painter.fillRect(rect, self.palette().window())
        dark = theme.is_dark_active()
        ink = theme.error_text_color(dark) if self._message_error else appearance.roles(dark).ink
        appearance.relay_icon(dark, 14, color=ink).paint(painter, rect.left(), rect.center().y() - 7, 14, 14)
        rect.adjust(20, 0, 0, 0)
        painter.setPen(QColor(ink))
        painter.drawText(rect, Qt.AlignVCenter | Qt.AlignLeft,
                         self.fontMetrics().elidedText(self.currentMessage(), Qt.ElideRight,
                                                       max(0, rect.width())))


class NostrLabel(QLabel):
    """An existing mixed-page label with a decorative relay mark.

    Text, buddy, accessible name and visibility stay with the original field.
    """
    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self.setContentsMargins(21, 0, 0, 0)

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        appearance.relay_icon(theme.is_dark_active(), 15).paint(
            painter, 0, (self.height() - 15) // 2, 15, 15)
