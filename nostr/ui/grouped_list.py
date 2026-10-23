# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Rows in a rounded group, the way System Settings lists what an account has.

    In MyEditor
    ╭──────────────────────────────────────────────────────────╮
    │ [▣] Members’ relay                     ✓ In your relay list │
    │     Readers find your notes and articles at …             │
    │     ─────────────────────────────────────────────────────  │
    │ [▣] Media storage                    ( Open Media Library ) │
    │     1.2 GB of 5 GB used · up to 1 GB per file              │
    │     ━━━━━━━━━━━━━━━━━━━━━                                  │
    ╰──────────────────────────────────────────────────────────╯

A row is a symbol on a tinted tile, a title, one short line under it,
and at most one thing at its trailing end: a status in words, or one
small button. Never both: a row either says it is done or offers the one
step that is left. The status is in the secondary color with a check
mark before its words, so it never rests on the mark (or on a color)
alone. What a click set going, and how it ended when that needs saying,
is a note under the row, where it happened.

The tile and its symbol are drawn by the row, so a screen reader meets
the row's words and its control rather than an unnamed picture, and a
row's button is described by the row's title, so "Add" is read with
what it adds.

``Notice`` is the quiet line above a group that says something is not
available right now, with an information mark and a way to try again.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QHBoxLayout, QProgressBar, QPushButton, QVBoxLayout, QWidget

import format_icons
import theme
from . import appearance
from nostr.ui.assistant import LINK_EDGE, link_button, text_label

TILE = 24               # a row's symbol tile, as System Settings draws one
TILE_RADIUS = 6
SYMBOL = 16             # the symbol on it
INSET = 12              # a row's contents from the group's edges, all round
SPACE = 12              # between the tile, the words and the trailing control
RADIUS = 10             # the group's corners
MARK = 14               # a notice's information mark


def stylesheet(is_dark: bool) -> str:
    """The rows' words and controls, for the window's style sheet."""
    # The secondary words in a row are a shade lighter than the window's
    # muted ones on the dark group, which is lighter than the window: 4.5:1
    # against it, as against the window.
    r = appearance.roles(is_dark)
    muted, fill, edge, hover = r.muted, r.control, r.edge, r.ink
    track, used, ring = r.soft, r.icon, r.focus
    full = theme.attention_color(is_dark)
    return f"""
    QLabel#group_header {{ font-weight: 600; }}
    QLabel#row_title {{ font-weight: 600; }}
    QLabel#row_detail, QLabel#row_status {{ color: {muted}; }}
    QLabel#row_note {{ color: {muted}; font-size: 12px; }}
    QPushButton#row_button {{
        background: {fill}; border: 1px solid {edge}; border-radius: 5px;
        padding: 3px 10px; min-width: 0; font-size: 12px;
    }}
    QPushButton#row_button:hover {{ border-color: {hover}; }}
    QPushButton#row_button:focus {{ border: 2px solid {ring}; padding: 2px 9px; }}
    QPushButton#row_button:disabled {{ background: {fill}; border-color: {edge}; }}
    QProgressBar#usage {{
        background: {track}; border: none; border-radius: 2px;
        min-height: 4px; max-height: 4px; margin-top: 4px;
    }}
    QProgressBar#usage::chunk {{ background: {used}; border-radius: 2px; }}
    QProgressBar#usage[tone="full"]::chunk {{ background: {full}; }}
    """


def _surface(is_dark: bool):
    """The group's fill and the color of its edge and hairlines."""
    if is_dark:
        return QColor(255, 255, 255, 14), QColor(255, 255, 255, 26)
    return QColor(0, 0, 0, 10), QColor(0, 0, 0, 22)


def spoken(text: str) -> str:
    """``text`` as a screen reader should read it: the middle dots that
    part a line ("1.2 GB of 5 GB used · up to 1 GB per file") as pauses,
    not as "middle dot"."""
    return text.replace(" \u00b7 ", ", ")


def group_header(text: str):
    """The title over a group ("In MyEditor")."""
    return text_label(text, "group_header")


def usage_bar(name: str) -> QProgressBar:
    """A thin bar for how much of an allowance is used, in thousandths;
    its "tone" property "full" draws it in the attention color."""
    bar = QProgressBar()
    bar.setObjectName("usage")
    bar.setRange(0, 1000)
    bar.setTextVisible(False)
    bar.setAccessibleName(name)
    return bar


class ListGroup(QWidget):
    """Rows on one rounded surface, with a hairline between each two."""

    def __init__(self, *, is_dark: bool, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._is_dark = is_dark
        self._rows = []
        self._column = QVBoxLayout(self)
        self._column.setContentsMargins(0, 0, 0, 0)
        self._column.setSpacing(0)

    def add_row(self, row: "ListRow") -> "ListRow":
        self._rows.append(row)
        self._column.addWidget(row)
        return row

    def paintEvent(self, _event) -> None:  # noqa: N802, Qt's name
        fill, line = _surface(self._is_dark)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(line, 1))
        painter.setBrush(fill)
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5),
                                RADIUS, RADIUS)
        # Between rows, from where the words start to the trailing inset,
        # as a grouped list draws its separators.
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        shown = [row for row in self._rows if not row.isHidden()]
        for row in shown[1:]:
            y = row.geometry().top()
            painter.drawLine(INSET + TILE + SPACE, y, self.width() - INSET, y)


class ListRow(QWidget):
    """One row: the tile, the words, and one trailing status or button."""

    def __init__(self, title: str, *, symbol: str, tint: str,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._symbol = symbol
        self._tint = QColor(tint)
        column = QVBoxLayout(self)
        column.setContentsMargins(INSET + TILE + SPACE, INSET, INSET, INSET)
        column.setSpacing(4)
        # The tile and the trailing control are centered on the head: the
        # title and its line, not a note that comes and goes under them.
        self._head = QHBoxLayout()
        self._head.setSpacing(SPACE)
        self._words = QVBoxLayout()
        self._words.setSpacing(2)
        self.title_label = text_label(title, "row_title")
        self.detail_label = text_label("", "row_detail")
        self._words.addWidget(self.title_label)
        self._words.addWidget(self.detail_label)
        self._head.addLayout(self._words, 1)
        self.status_label = text_label("", "row_status", wrap=False)
        self.button = QPushButton()
        self.button.setObjectName("row_button")
        # A row's button never takes Return from the window's action.
        self.button.setAutoDefault(False)
        self.button.setAccessibleDescription(title)
        self._head.addWidget(self.status_label, 0, Qt.AlignmentFlag.AlignVCenter)
        self._head.addWidget(self.button, 0, Qt.AlignmentFlag.AlignVCenter)
        column.addLayout(self._head)
        self.note_label = text_label("", "row_note")
        self.note_label.hide()
        column.addWidget(self.note_label)
        self.clear_accessory()

    # -- what it says ---------------------------------------------------------

    def set_detail(self, text: str, *, selectable: bool = False) -> None:
        """The line under the title; ``selectable`` for words a person may
        want to copy (an address)."""
        self.detail_label.setText(text)
        self.detail_label.setAccessibleName(spoken(text))
        self.detail_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse if selectable
            else Qt.TextInteractionFlag.NoTextInteraction)

    def add_detail(self, widget: QWidget) -> None:
        """Something more under the line (a usage bar)."""
        self._words.addWidget(widget)

    def set_note(self, text: str) -> None:
        """What a click set going, or why it did not work; "" hides it."""
        self.note_label.setText(text)
        self.note_label.setVisible(bool(text))

    def show_status(self, words: str) -> None:
        """Say the row is done: a check mark and ``words``, no button."""
        self.status_label.setText(f"✓ {words}")
        # Read as the words alone, not "check mark".
        self.status_label.setAccessibleName(words)
        self.status_label.show()
        self.button.hide()

    def show_button(self, title: str, *, description: str = "") -> None:
        """Offer the one step left: a button titled ``title``, ready to be
        clicked; ``description`` says more to a screen reader than the
        row's title (where the button goes)."""
        self.button.setText(title)
        self.button.setAccessibleDescription(description or self.title_label.text())
        self.button.setEnabled(True)
        self.button.show()
        self.status_label.hide()

    def clear_accessory(self) -> None:
        """Neither a status nor a button: nothing to do here right now."""
        self.status_label.hide()
        self.button.hide()

    # -- drawing --------------------------------------------------------------

    def paintEvent(self, _event) -> None:  # noqa: N802, Qt's name
        head = self._head.geometry()
        tile = QRectF(INSET, head.center().y() + 0.5 - TILE / 2, TILE, TILE)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        if self.window().property("nightVioletMembership"):
            appearance.membership_background(painter, tile, TILE_RADIUS)
        else:
            painter.setBrush(self._tint)
            painter.drawRoundedRect(tile, TILE_RADIUS, TILE_RADIUS)
        margin = (TILE - SYMBOL) / 2
        format_icons.paint(self._symbol, painter, tile.adjusted(margin, margin, -margin, -margin),
                           QColor("#FFFFFF"))


class Notice(QWidget):
    """A quiet line with an information mark: what is not available right
    now, and a link to try again (hidden when there is nothing to retry)."""

    def __init__(self, text: str, action: str, on_action, *, is_dark: bool,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._is_dark = is_dark
        row = QHBoxLayout(self)
        row.setContentsMargins(MARK + 8, 0, 0, 0)
        row.setSpacing(16)
        self.label = text_label(text, "muted")
        # The link's words on the label's first line: the link keeps a
        # transparent edge around them for its focus ring.
        self.label.setContentsMargins(0, LINK_EDGE, 0, 0)
        self.link = link_button(action, on_action)
        row.addWidget(self.label, 1)
        row.addWidget(self.link, 0, Qt.AlignmentFlag.AlignTop)

    def paintEvent(self, _event) -> None:  # noqa: N802, Qt's name
        # Beside the first line of the words, in their color.
        line = self.label.fontMetrics().height()
        top = self.label.geometry().top() + self.label.contentsMargins().top() + (line - MARK) / 2
        painter = QPainter(self)
        format_icons.paint("info", painter, QRectF(0, top, MARK, MARK),
                           QColor(appearance.roles(self._is_dark).muted))
