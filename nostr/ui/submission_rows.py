# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The rows of the Einreichungen list, drawn (plan 10.1; they were part
of :mod:`.submissions_view`).

:class:`Item` is one line already in words: a section's title, the
Erledigt disclosure, or a submission (its cover, its title on up to two
lines, one status word with what follows it, its age at the trailing
edge, a dot when something is unread). :class:`Rows` is the list's
model; a section's title cannot be selected, the disclosure and the
submissions can. :class:`RowDelegate` draws them as the panel's other
lists draw theirs (post_list.py): the same gutter, the same rounded
selection, the keyboard's place while nothing is selected. Whose turn
it is shows in the word's weight as well as its place; unread in a dot
and in the row's spoken text.
"""

from __future__ import annotations

from typing import List, NamedTuple, Optional

from PySide6.QtCore import QAbstractListModel, QModelIndex, QPointF, QRect, QRectF, QSize, Qt
from PySide6.QtGui import (
    QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen, QTextLayout, QTextOption,
)
from PySide6.QtWidgets import QAbstractItemView, QStyle, QStyledItemDelegate

from i18n import _

from . import appearance
from .drafts_common import (
    LIST_GUTTER, ROW_LINE_GAP, ROW_PAD_V, THEME_TOKENS, secondary_font, title_font,
)
from .post_list import SELECTION_INSET, SELECTION_RADIUS, section_font, section_height
from .submission_cover import cover_pixmap

ITEM_ROLE = Qt.ItemDataRole.UserRole + 41
_COVER = 44
_DOT = 8
# The system's blue, as Mail's unread dot: on either theme.
_UNREAD = {True: "#0A84FF", False: "#007AFF"}


class Item(NamedTuple):
    """One line of the list: a section's title, the Erledigt disclosure,
    or a submission, already in words."""

    kind: str                   # "header", "done" or "row"
    title: str
    sid: str = ""
    word: str = ""
    rest: str = ""
    age: str = ""
    unread: bool = False
    turn: bool = False
    work: str = "article"
    cover: Optional[bytes] = None
    open: bool = False          # the disclosure: whether Erledigt shows its rows
    quiet: bool = False         # not sent, not confirmed: the title leans
    # ``rest`` without the sender's name, for a row too narrow for both
    # (RG-M3-b 12): the message itself is what is new.
    short: str = ""

    @property
    def spoken(self) -> str:
        if self.kind != "row":
            return self.title
        return ", ".join(part for part in (self.title, self.word, self.rest, self.age,
                                           _("New") if self.unread else "") if part)


class Rows(QAbstractListModel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.items: List[Item] = []

    def show(self, items: List[Item]) -> None:
        self.beginResetModel()
        self.items = items
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802, Qt's name
        return 0 if parent.isValid() else len(self.items)

    def flags(self, index):
        item = self.items[index.row()]
        if item.kind == "header":
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        item = self.items[index.row()]
        if role == ITEM_ROLE:
            return item
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.AccessibleTextRole):
            return item.spoken
        return None

    def row_of(self, sid: str) -> int:
        return next((i for i, item in enumerate(self.items) if item.sid == sid), -1)


def _title_lines(text: str, font: QFont, width: int) -> List[str]:
    """``text`` on at most two lines of ``width``, the second cut with an
    ellipsis."""
    layout = QTextLayout(text, font)
    option = QTextOption()
    option.setWrapMode(QTextOption.WrapMode.WordWrap)
    layout.setTextOption(option)
    layout.beginLayout()
    lines = []
    while len(lines) < 2:
        line = layout.createLine()
        if not line.isValid():
            break
        line.setLineWidth(max(1, width))
        lines.append((line.textStart(), line.textLength()))
    layout.endLayout()
    if not lines:
        return [""]
    first = text[lines[0][0]:lines[0][0] + lines[0][1]].rstrip()
    if len(lines) == 1:
        return [first]
    rest = text[lines[1][0]:].strip()
    return [first, QFontMetrics(font).elidedText(rest, Qt.TextElideMode.ElideRight, width)]


class RowDelegate(QStyledItemDelegate):
    """Draws the list (see the module's sketch)."""

    def __init__(self, view: QAbstractItemView, *, is_dark: bool) -> None:
        super().__init__(view)
        self._view = view
        self._dark = bool(is_dark)

    def set_dark(self, is_dark: bool) -> None:
        self._dark = bool(is_dark)
        self._view.viewport().update()

    @staticmethod
    def row_height() -> int:
        text = (2 * QFontMetrics(title_font()).height() + ROW_LINE_GAP
                + QFontMetrics(secondary_font()).height())
        return 2 * ROW_PAD_V + max(_COVER, text)

    def sizeHint(self, option, index) -> QSize:  # noqa: N802, Qt's name
        item = index.data(ITEM_ROLE)
        if isinstance(item, Item) and item.kind != "row":
            return QSize(0, section_height() + (6 if item.kind == "done" else 0))
        return QSize(0, self.row_height())

    def paint(self, painter: QPainter, option, index: QModelIndex) -> None:
        item = index.data(ITEM_ROLE)
        if not isinstance(item, Item):
            return
        tokens = THEME_TOKENS[self._dark]
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = option.rect
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        focused = self._view.hasFocus()
        if selected and item.kind != "header":
            shape = QPainterPath()
            shape.addRoundedRect(QRectF(rect).adjusted(SELECTION_INSET, 0.5, -SELECTION_INSET,
                                                       -0.5), SELECTION_RADIUS, SELECTION_RADIUS)
            painter.fillPath(shape, appearance.gradient(self._dark, QRectF(rect)) if focused
                             else QColor(appearance.roles(self._dark).soft))
        on_accent = selected and focused and item.kind != "header"
        strong = QColor("#FFFFFF") if on_accent else QColor(tokens["row_fg"])
        muted = QColor("#FFFFFF") if on_accent else QColor(tokens["muted"])
        if item.kind == "row":
            self._paint_row(painter, rect, item, strong, muted, on_accent)
        else:
            self._paint_section(painter, rect, item, muted)
        if (option.state & QStyle.StateFlag.State_HasFocus) and not selected \
                and item.kind != "header":
            painter.setPen(QPen(QColor(tokens["accent"]), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(QRectF(rect).adjusted(SELECTION_INSET + 0.5, 1.0,
                                                          -SELECTION_INSET - 0.5, -1.0),
                                    SELECTION_RADIUS, SELECTION_RADIUS)
        painter.restore()

    def _paint_section(self, painter, rect: QRect, item: Item, muted: QColor) -> None:
        font = section_font()
        metrics = QFontMetrics(font)
        painter.setFont(font)
        painter.setPen(muted)
        text = item.title
        if item.kind == "done":
            text = ("⌄ " if item.open else "› ") + text
        box = QRect(rect.left() + LIST_GUTTER, rect.bottom() - metrics.height() - 2,
                    max(0, rect.width() - 2 * LIST_GUTTER), metrics.height())
        painter.drawText(box, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                         metrics.elidedText(text, Qt.TextElideMode.ElideRight, box.width()))

    def _paint_row(self, painter, rect: QRect, item: Item, strong: QColor, muted: QColor,
                   on_accent: bool) -> None:
        r = appearance.roles(self._dark)
        cover = cover_pixmap(item.cover, item.work, _COVER, QColor(r.soft), QColor(r.icon))
        painter.drawPixmap(QPointF(rect.left() + LIST_GUTTER,
                                   rect.center().y() - _COVER / 2 + 0.5), cover)
        secondary = secondary_font()
        small = QFontMetrics(secondary)
        left = rect.left() + LIST_GUTTER + _COVER + 10
        right = rect.right() - LIST_GUTTER
        age_width = small.horizontalAdvance(item.age) if item.age else 0
        title = title_font()
        if item.quiet:
            title.setItalic(True)
            title.setWeight(QFont.Weight.Normal)
        metrics = QFontMetrics(title)
        lines = _title_lines(item.title, title, right - left - age_width - 8)
        block = len(lines) * metrics.height() + ROW_LINE_GAP + small.height()
        y = rect.center().y() - block // 2
        flags = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        painter.setFont(title)
        painter.setPen(muted if item.quiet else strong)
        for line in lines:
            painter.drawText(QRect(left, y, right - left - age_width - 8, metrics.height()),
                             flags, line)
            y += metrics.height()
        painter.setFont(secondary)
        painter.setPen(muted)
        if item.age:
            painter.drawText(QRect(right - age_width, y - len(lines) * metrics.height(),
                                   age_width + 1, metrics.height()),
                             int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                             item.age)
        y += ROW_LINE_GAP
        x = left
        room = right - left - (_DOT + 6 if item.unread else 0)
        if item.word:
            word_font = QFont(secondary)
            if item.turn:
                word_font.setWeight(QFont.Weight.DemiBold)
            painter.setFont(word_font)
            painter.setPen(strong if item.turn else muted)
            word = QFontMetrics(word_font).elidedText(item.word, Qt.TextElideMode.ElideRight,
                                                      room)
            painter.drawText(QRect(x, y, room, small.height()), flags, word)
            x += QFontMetrics(word_font).horizontalAdvance(word)
        if item.rest and x < left + room:
            painter.setFont(secondary)
            painter.setPen(muted)
            lead = " · " if item.word else ""
            rest = lead + item.rest
            if item.short and small.horizontalAdvance(rest) > left + room - x:
                rest = lead + item.short
            painter.drawText(QRect(x, y, left + room - x, small.height()), flags,
                             small.elidedText(rest, Qt.TextElideMode.ElideRight,
                                              left + room - x))
        if item.unread:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#FFFFFF") if on_accent else QColor(_UNREAD[self._dark]))
            painter.drawEllipse(QRectF(right - _DOT, y + (small.height() - _DOT) / 2,
                                       _DOT, _DOT))


__all__ = ["ITEM_ROLE", "Item", "RowDelegate", "Rows"]
