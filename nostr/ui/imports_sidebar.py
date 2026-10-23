# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Imports window's sidebar: the lists, then the sources.

Two levels at most, as Apple's guidelines ask of a sidebar:

    Inbox            14        the posts waiting to be looked at
    Older Posts      32        already on a source when it was followed
    Imported         48        posts that became drafts
    Skipped           3        set aside on this computer

    Sources
      A  Field notes   5       new posts waiting from this source
      N  Night Shift   •       its last check failed (the tooltip says why)

    Files
      wordpress.xml   48       a file or a link opened to import once

A count shows only when there is something to count; the Inbox's is
bold. A source shows its initial in a calm tint (its site icon comes
later), its new posts, a dot when its last check failed, or a pause
glyph when its automatic checks are turned off. Section
headers cannot be selected, so the arrow keys pass over them.

:class:`SidebarModel` holds the rows; the window fills it from the
controller and reacts to the selection (``entry_selected``) and to a
context menu request (``menu_requested``). Nothing here knows about the
inbox or the network.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from PySide6.QtCore import (
    QAbstractListModel,
    QModelIndex,
    QPoint,
    QRect,
    QRectF,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPalette
from PySide6.QtWidgets import QAbstractItemView, QListView, QStyle, QStyledItemDelegate

import theme
from . import appearance
from i18n import _

from .imports_glyphs import glyph, is_dark, source_avatar

LIST, HEADER, SOURCE, FILE = "list", "header", "source", "file"
ROW_HEIGHT = 30
HEADER_HEIGHT = 30
ICON = 16


@dataclass(frozen=True)
class Entry:
    """One row of the sidebar."""

    kind: str                  # LIST | HEADER | SOURCE | FILE
    key: str                   # a list's name, a source's key, a file's id
    title: str
    glyph: str = ""            # for a LIST and a FILE
    count: int = 0
    trailing: str = ""         # instead of the count: "31/48"
    emphasized: bool = False   # the Inbox count
    failed: bool = False       # a source whose last check failed
    paused: bool = False       # a source whose automatic checks are off
    tooltip: str = ""
    url: str = ""              # a source's address
    automatic: bool = True

    @property
    def selectable(self) -> bool:
        return self.kind != HEADER


EntryRole = Qt.ItemDataRole.UserRole + 1


class SidebarModel(QAbstractListModel):
    """The sidebar's rows. Updating with the same rows in the same order
    keeps the selection; anything else rebuilds."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._entries: List[Entry] = []

    def set_entries(self, entries: List[Entry]) -> None:
        same_shape = [(e.kind, e.key) for e in entries] == [
            (e.kind, e.key) for e in self._entries]
        if same_shape:
            changed = [i for i, (old, new) in enumerate(zip(self._entries, entries))
                       if old != new]
            self._entries = list(entries)
            for row in changed:
                index = self.index(row)
                self.dataChanged.emit(index, index)
            return
        self.beginResetModel()
        self._entries = list(entries)
        self.endResetModel()

    def entries(self) -> List[Entry]:
        return list(self._entries)

    def entry(self, row: int) -> Optional[Entry]:
        return self._entries[row] if 0 <= row < len(self._entries) else None

    def row_of(self, kind: str, key: str) -> int:
        for row, entry in enumerate(self._entries):
            if entry.kind == kind and entry.key == key:
                return row
        return -1

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._entries)

    def flags(self, index):
        entry = self.entry(index.row())
        if entry is None or not entry.selectable:
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        entry = self.entry(index.row())
        if entry is None:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return entry.title
        if role == EntryRole:
            return entry
        if role == Qt.ItemDataRole.ToolTipRole:
            return entry.tooltip or None
        if role == Qt.ItemDataRole.AccessibleTextRole:
            parts = [entry.title]
            if entry.trailing:
                parts.append(entry.trailing)
            elif entry.count:
                parts.append(str(entry.count))
            if entry.failed:
                parts.append(_("Couldn't check this source"))
            elif entry.paused:
                parts.append(_("Not checked automatically"))
            return ", ".join(parts)
        return None


class SidebarDelegate(QStyledItemDelegate):
    """Paints the rows: a glyph or an initial, the title, a count or a dot."""

    def __init__(self, view: "Sidebar") -> None:
        super().__init__(view)
        self._view = view

    def sizeHint(self, option, index) -> QSize:
        entry = index.data(EntryRole)
        height = HEADER_HEIGHT if entry is not None and entry.kind == HEADER else ROW_HEIGHT
        return QSize(option.rect.width(), height)

    def paint(self, painter: QPainter, option, index) -> None:
        entry: Entry = index.data(EntryRole)
        if entry is None:
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        palette = option.palette
        rect = option.rect
        if entry.kind == HEADER:
            font = QFont(option.font)
            font.setBold(True)
            font.setPointSizeF(max(8.0, option.font.pointSizeF() - 1.5))
            painter.setFont(font)
            painter.setPen(palette.color(QPalette.ColorRole.PlaceholderText))
            painter.drawText(rect.adjusted(14, 8, -8, 0),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                             entry.title)
            painter.restore()
            return

        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        focused = self._view.hasFocus()
        text_color = palette.color(QPalette.ColorRole.Text)
        muted = palette.color(QPalette.ColorRole.PlaceholderText)
        if selected:
            highlight = QColor(appearance.roles(is_dark(palette)).soft)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(highlight)
            painter.drawRoundedRect(QRectF(rect.adjusted(6, 1, -6, -1)), 6, 6)
            text_color = QColor(appearance.roles(is_dark(palette)).ink)
            muted = text_color

        dpr = self._view.devicePixelRatioF()
        icon_rect = QRect(rect.left() + 14, rect.center().y() - ICON // 2 + 1, ICON, ICON)
        if entry.kind in (LIST, FILE):
            painter.drawPixmap(icon_rect, glyph(entry.glyph, ICON, muted if not (
                selected and focused) else text_color, dpr))
        else:
            icon_for = self._view.icon_for
            image = icon_for(entry.key, entry.url) if icon_for is not None else None
            painter.drawPixmap(icon_rect, source_avatar(entry.title, entry.url or entry.key,
                                                        ICON, dark=is_dark(palette), dpr=dpr,
                                                        image=image))

        trailing = ""
        trailing_width = 0
        font = QFont(option.font)
        paused = entry.paused and not entry.failed and not (entry.trailing or entry.count)
        if entry.failed or paused:
            trailing_width = 14
        elif entry.trailing or entry.count:
            trailing = entry.trailing or str(entry.count)
            count_font = QFont(font)
            count_font.setBold(entry.emphasized)
            trailing_width = QFontMetrics(count_font).horizontalAdvance(trailing) + 4

        text_left = icon_rect.right() + 9
        text_rect = QRect(text_left, rect.top(), rect.right() - 14 - trailing_width - 6
                          - text_left, rect.height())
        painter.setFont(font)
        painter.setPen(text_color)
        title = QFontMetrics(font).elidedText(entry.title, Qt.TextElideMode.ElideRight,
                                              text_rect.width())
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         title)

        if entry.failed:
            dot = 7
            center = QRect(rect.right() - 14 - dot, rect.center().y() - dot // 2 + 1, dot, dot)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(_attention(palette))
            painter.drawEllipse(center)
        elif paused:
            # Checks turned off: a quiet pause glyph, where a count would be
            # (the count wins: what waits matters more than how it came).
            size = 12
            painter.drawPixmap(QRect(rect.right() - 14 - size, rect.center().y() - size // 2 + 1,
                                     size, size), glyph("pause", size, muted, dpr))
        elif trailing:
            count_font = QFont(font)
            count_font.setBold(entry.emphasized)
            painter.setFont(count_font)
            painter.setPen(text_color if entry.emphasized else muted)
            painter.drawText(QRect(rect.right() - 14 - trailing_width, rect.top(),
                                   trailing_width, rect.height()),
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                             trailing)
        painter.restore()


def _attention(palette: QPalette) -> QColor:
    """The red of a problem, readable on the theme's background."""
    return QColor(theme.attention_color(is_dark(palette)))


class Sidebar(QListView):
    """The sidebar list.

    Signals:
      entry_selected(object)           an Entry was chosen
      menu_requested(object, QPoint)   a context menu for an Entry, at a
                                       global position
    """

    entry_selected = Signal(object)
    menu_requested = Signal(object, QPoint)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("imports_sidebar")
        self.setAccessibleName(_("Lists and sources"))
        self.model_ = SidebarModel(self)
        self.setModel(self.model_)
        self.setItemDelegate(SidebarDelegate(self))
        # (source key, address) -> its site icon or None (C12); the window
        # sets it.
        self.icon_for = None
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setFrameShape(QListView.Shape.NoFrame)
        self.setUniformItemSizes(False)
        self.setMouseTracking(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu)
        self.selectionModel().currentChanged.connect(self._on_current)
        self.model_.modelReset.connect(self._restore_selection)
        self._chosen = (LIST, "inbox")

    def set_entries(self, entries: List[Entry]) -> None:
        self.model_.set_entries(entries)
        self._restore_selection()

    def select(self, kind: str, key: str, *, announce: bool = True) -> bool:
        """Choose the row ``(kind, key)``; False when there is none.
        ``entry_selected`` is said only when the choice changed (and
        ``announce`` is on): the same choice found again after the rows
        changed is not a new one."""
        row = self.model_.row_of(kind, key)
        if row < 0:
            return False
        changed = (kind, key) != self._chosen
        self._chosen = (kind, key)
        index = self.model_.index(row)
        if self.currentIndex() != index:
            blocked = self.selectionModel().blockSignals(True)
            self.setCurrentIndex(index)
            self.selectionModel().blockSignals(blocked)
            self.viewport().update()
        if changed and announce:
            self.entry_selected.emit(self.model_.entry(row))
        return True

    def chosen(self) -> Optional[Entry]:
        row = self.model_.row_of(*self._chosen)
        return self.model_.entry(row)

    def _restore_selection(self) -> None:
        """After the rows changed: the same choice, quietly (the window keeps
        its search, its checks and the open post), or the Inbox when what
        was chosen is gone."""
        if self.select(*self._chosen, announce=False):
            return
        self.select(LIST, "inbox")

    def _on_current(self, current: QModelIndex, _previous: QModelIndex) -> None:
        entry = self.model_.entry(current.row())
        if entry is None or not entry.selectable or (entry.kind, entry.key) == self._chosen:
            return
        self._chosen = (entry.kind, entry.key)
        self.entry_selected.emit(entry)

    def _on_context_menu(self, pos: QPoint) -> None:
        entry = self.model_.entry(self.indexAt(pos).row())
        if entry is not None and entry.kind in (SOURCE, FILE):
            self.menu_requested.emit(entry, self.viewport().mapToGlobal(pos))

    def focusInEvent(self, event) -> None:
        super().focusInEvent(event)
        self.viewport().update()

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        self.viewport().update()
