# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The rows of the panel's lists, drawn alike for drafts and published posts.

    Today                                        a section's title, over
  ╭──────────────────────────────────────────╮   its first row
  │ Why I write in plain text                │   the title
  │ ▤ 14:05  Every tool I loved is gone. My… │   kind, date, the words
  ╰──────────────────────────────────────────╯   after the title
    Yesterday
    Thinking about relays again
    ◌ 21:40  Who keeps my drafts, and for ho…
    ─────────────────────────────────────────    a hairline between rows

A writer's list, as Notes draws one. Rows are grouped under Today,
Yesterday, Previous 7 Days, Previous 30 Days, then the months of this
year, then years (:func:`date_section`), so a glance finds a piece by
when it was written. Each row is two lines: the title, then a symbol for
what it is (an article, a note, a curation), a short date (the time
today and yesterday, the weekday this week, the date before that,
:func:`list_date`) and the words that follow the title, never the title
again (:func:`words_after_title`). A selected row is a rounded shape
inset from the list's edges, in the system's selection color while the
list has the keyboard focus and a quiet grey otherwise, as on a Mac;
thin lines separate the rows of a section.

A list hands each row to :class:`PostRowDelegate` as a :class:`PostRow`
in the ``ROW_ROLE`` of its index; the list decides the words, this
module how they look. A section's title is drawn as part of its first
row (the row is taller by the title's height), so sections cost the
list no rows of their own: the arrow keys, the selection and type to
select see only posts. A row whose title is cut short shows the whole
title as its tooltip, and only then.
"""

from __future__ import annotations

import re
from typing import List, NamedTuple, Optional, Sequence

from PySide6.QtCore import (
    QDate, QDateTime, QLocale, QModelIndex, QPointF, QRect, QRectF, QSize, Qt,
)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QAbstractItemView, QStyle, QStyledItemDelegate, QToolTip

import format_icons
from . import appearance
from i18n import _

from .drafts_common import (
    LIST_GUTTER, ROW_LINE_GAP, ROW_PAD_V, THEME_TOKENS, display_locale, scaled,
    secondary_font, title_font,
)

# The index role a list keeps its PostRow in.
ROW_ROLE = Qt.ItemDataRole.UserRole + 40

# The selection shape's distance from the list's edges, and its corners.
# The text starts at LIST_GUTTER, the panel's one gutter.
SELECTION_INSET = 6
SELECTION_RADIUS = 6.0
# Room for a section's title over its row.
SECTION_PAD_TOP = 10
SECTION_PAD_BOTTOM = 3

READY, LOADING, FAILED, DELETING = "ready", "loading", "failed", "deleting"


class PostRow(NamedTuple):
    """What one row says, already in words."""

    title: str
    kind: str = ""          # "article", "note", "curation", or "" while not known
    when: str = ""          # the short date of line 2 (list_date)
    detail: str = ""        # the words after the title, or what the state says
    state: str = READY      # READY, LOADING, FAILED or DELETING
    mark: str = ""          # a glyph at the end of line 1: "alert", "clock" or ""
    section: str = ""       # on a section's first row: the section's title


# --------------------------------------------------------------------------- #
# Words                                                                       #
# --------------------------------------------------------------------------- #

def _squeeze(text: str) -> str:
    return " ".join(str(text or "").split())


def words_after_title(title: str, text: str) -> str:
    """``text`` without the ``title`` it starts with: a note's title is its
    first line, and the row showed that line twice, as the title and as
    the start of its preview. Text that does not start with the title (an
    article's summary, an imported post's body) is kept whole."""
    title, text = _squeeze(title), _squeeze(text)
    if title and text.casefold().startswith(title.casefold()):
        rest = text[len(title):]
        return re.sub(r"^[\s.:;,!?…\-]+", "", rest)
    return text


def _local_day(ts: int) -> Optional[QDate]:
    if ts <= 0:
        return None
    moment = QDateTime.fromSecsSinceEpoch(int(ts))
    return moment.date() if moment.isValid() else None


def list_date(ts: int, now: int, locale: Optional[QLocale] = None) -> str:
    """The short date of a row, as Notes and Mail write it: the time on
    the day itself and the day before (the section already says which),
    the weekday within a week, the date in digits before that, each in the
    language's own way ("14:05", "Montag", "02.10.26")."""
    day, today = _local_day(ts), _local_day(now)
    if day is None or today is None:
        return ""
    locale = locale or display_locale()
    age = day.daysTo(today)
    if age <= 1:
        return locale.toString(QDateTime.fromSecsSinceEpoch(int(ts)).time(),
                               QLocale.FormatType.ShortFormat)
    if age < 7:
        return locale.dayName(day.dayOfWeek(), QLocale.FormatType.LongFormat)
    return locale.toString(day, QLocale.FormatType.ShortFormat)


def date_section(ts: int, now: int, locale: Optional[QLocale] = None) -> str:
    """The section a row belongs to, as Notes groups its notes: Today,
    Yesterday, Previous 7 Days, Previous 30 Days, then the month (this
    year), then the year. A time after ``now`` (a clock running ahead)
    counts as today."""
    day, today = _local_day(ts), _local_day(now)
    if day is None or today is None:
        return ""
    age = day.daysTo(today)
    if age <= 0:
        return _("Today")
    if age == 1:
        return _("Yesterday")
    if age < 7:
        return _("Previous 7 Days")
    if age < 30:
        return _("Previous 30 Days")
    if day.year() == today.year():
        return (locale or display_locale()).standaloneMonthName(
            day.month(), QLocale.FormatType.LongFormat)
    return str(day.year())


def section_starts(times: Sequence[int], now: int,
                   locale: Optional[QLocale] = None) -> List[str]:
    """For rows in list order, the section title on each row that starts a
    section and "" on the others."""
    out: List[str] = []
    previous = None
    for ts in times:
        section = date_section(ts, now, locale)
        out.append(section if section != previous else "")
        previous = section
    return out


# --------------------------------------------------------------------------- #
# Drawing                                                                     #
# --------------------------------------------------------------------------- #

def section_font() -> QFont:
    font = secondary_font()
    font.setWeight(QFont.Weight.DemiBold)
    return font


def row_height() -> int:
    """Two lines and their padding, from the fonts in use."""
    return (ROW_PAD_V + QFontMetrics(title_font()).height() + ROW_LINE_GAP
            + QFontMetrics(secondary_font()).height() + ROW_PAD_V)


def section_height() -> int:
    return (scaled(SECTION_PAD_TOP) + QFontMetrics(section_font()).height()
            + scaled(SECTION_PAD_BOTTOM))


def symbol_pixmap(name: str, size: int, color: QColor, dpr: float = 2.0) -> QPixmap:
    """A glyph (format_icons.paint) as a ``size`` point pixmap, for the
    symbol over an empty list's title, as Apple's empty states have one."""
    pixmap = QPixmap(round(size * dpr), round(size * dpr))
    pixmap.setDevicePixelRatio(dpr)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    format_icons.paint(name, painter, QRectF(0, 0, size, size), color)
    painter.end()
    return pixmap


EMPTY_SYMBOL_SIZE = 34


class PostRowDelegate(QStyledItemDelegate):
    """Paints a list's rows from their PostRow (see the module). An index
    without one (Published's Load More) goes to :meth:`paint_other`."""

    def __init__(self, view: QAbstractItemView, *, is_dark: bool = True) -> None:
        super().__init__(view)
        self._view = view
        # A value, not a callback into the list's owner: a child holding
        # its owner would keep the owner alive.
        self._dark = bool(is_dark)

    def set_dark(self, is_dark: bool) -> None:
        self._dark = bool(is_dark)
        self._view.viewport().update()

    # -- size ---------------------------------------------------------------

    def sizeHint(self, option, index) -> QSize:
        row = index.data(ROW_ROLE)
        height = row_height()
        if isinstance(row, PostRow) and row.section:
            height += section_height()
        return QSize(0, height)

    @staticmethod
    def row_rect(option_rect: QRect, row: PostRow) -> QRect:
        """The row's own part of its index's rect, under any section title."""
        top = section_height() if row.section else 0
        return option_rect.adjusted(0, top, 0, 0)

    def title_rect(self, option_rect: QRect, row: PostRow) -> QRect:
        rect = self.row_rect(option_rect, row)
        inner = rect.adjusted(LIST_GUTTER, ROW_PAD_V, -LIST_GUTTER, -ROW_PAD_V)
        mark = self._mark_size() + 6 if row.mark else 0
        return QRect(inner.left(), inner.top(), max(0, inner.width() - mark),
                     QFontMetrics(title_font()).height())

    def title_is_cut(self, option_rect: QRect, row: PostRow) -> bool:
        width = self.title_rect(option_rect, row).width()
        return QFontMetrics(self._title_font(row)).horizontalAdvance(row.title) > width

    @staticmethod
    def _mark_size() -> int:
        return max(10, round(QFontMetrics(secondary_font()).height() * 0.8))

    @staticmethod
    def _title_font(row: PostRow) -> QFont:
        font = title_font()
        if row.state in (LOADING, FAILED):
            # Progress and failure recede and lean, a channel that
            # survives greyscale (accessibility.md).
            font.setItalic(True)
            font.setWeight(QFont.Weight.Normal)
        return font

    # -- painting -----------------------------------------------------------

    def paint(self, painter: QPainter, option, index: QModelIndex) -> None:
        row = index.data(ROW_ROLE)
        if not isinstance(row, PostRow):
            self.paint_other(painter, option, index)
            return
        tokens = THEME_TOKENS[self._dark]
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if row.section:
            self._paint_section(painter, option.rect, row.section, tokens)
        rect = self.row_rect(option.rect, row)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        focused = self._view.hasFocus()
        if selected:
            shape = QPainterPath()
            shape.addRoundedRect(QRectF(rect).adjusted(SELECTION_INSET, 0.5,
                                                       -SELECTION_INSET, -0.5),
                                 SELECTION_RADIUS, SELECTION_RADIUS)
            fill = (appearance.gradient(self._dark, QRectF(rect)) if focused
                    else QColor(appearance.roles(self._dark).soft))
            painter.fillPath(shape, fill)
        elif self._separator_above(index, row):
            painter.setPen(QPen(QColor(tokens["border"]), 1))
            y = rect.top() + 0.5
            painter.drawLine(QPointF(rect.left() + LIST_GUTTER, y),
                             QPointF(rect.right() - LIST_GUTTER + 1, y))
        on_accent = selected and focused
        strong = QColor("#FFFFFF") if on_accent else QColor(tokens["row_fg"])
        muted = QColor("#FFFFFF") if on_accent else QColor(tokens["muted"])
        title_colour = muted if row.state in (LOADING, FAILED, DELETING) else strong
        self._paint_lines(painter, option, rect, row, title_colour, strong, muted, tokens,
                          on_accent)
        if (option.state & QStyle.StateFlag.State_HasFocus) and not selected:
            # The keyboard's place while nothing is selected.
            painter.setPen(QPen(QColor(tokens["accent"]), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(QRectF(rect).adjusted(SELECTION_INSET + 0.5, 1.0,
                                                          -SELECTION_INSET - 0.5, -1.0),
                                    SELECTION_RADIUS, SELECTION_RADIUS)
        painter.restore()

    def paint_other(self, painter: QPainter, option, index: QModelIndex) -> None:
        """A row that is not a post (a list's own action row)."""

    def _paint_section(self, painter: QPainter, rect: QRect, title: str, tokens) -> None:
        font = section_font()
        metrics = QFontMetrics(font)
        box = QRect(rect.left() + LIST_GUTTER, rect.top() + scaled(SECTION_PAD_TOP),
                    max(0, rect.width() - 2 * LIST_GUTTER), metrics.height())
        painter.setFont(font)
        painter.setPen(QColor(tokens["muted"]))
        painter.drawText(box, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                         metrics.elidedText(title, Qt.TextElideMode.ElideRight, box.width()))

    def _separator_above(self, index: QModelIndex, row: PostRow) -> bool:
        """A line between two rows of one section, neither of them selected."""
        if row.section or index.row() == 0:
            return False
        above = index.siblingAtRow(index.row() - 1)
        if not isinstance(above.data(ROW_ROLE), PostRow):
            return False
        selection = self._view.selectionModel()
        return selection is None or not selection.isSelected(above)

    def _paint_lines(self, painter, option, rect: QRect, row: PostRow, title_colour: QColor,
                     strong: QColor, muted: QColor, tokens, on_accent: bool) -> None:
        line_1 = self.title_rect(option.rect, row)
        font = self._title_font(row)
        painter.setFont(font)
        painter.setPen(title_colour)
        left = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        painter.drawText(line_1, left, QFontMetrics(font).elidedText(
            row.title, Qt.TextElideMode.ElideRight, line_1.width()))
        mark_size = self._mark_size()
        if row.mark:
            ink = muted if on_accent else QColor(
                tokens["error_fg"] if row.mark == "alert" else tokens["muted"])
            box = QRectF(line_1.right() + 7, line_1.center().y() - mark_size / 2 + 0.5,
                         mark_size, mark_size)
            format_icons.paint(row.mark, painter, box, ink)

        secondary = secondary_font()
        metrics = QFontMetrics(secondary)
        inner_left = rect.left() + LIST_GUTTER
        line_2 = QRect(inner_left, line_1.bottom() + 1 + ROW_LINE_GAP,
                       max(0, rect.width() - 2 * LIST_GUTTER), metrics.height())
        x = line_2.left()
        if row.kind:
            glyph = QRectF(x, line_2.center().y() - mark_size / 2 + 0.5, mark_size, mark_size)
            format_icons.paint(row.kind, painter, glyph, muted)
            x += mark_size + 5
        painter.setFont(secondary)
        if row.when:
            when_width = metrics.horizontalAdvance(row.when)
            # The date a step above the words after it, as Notes sets it.
            date_ink = QColor(strong)
            date_ink.setAlphaF(1.0 if on_accent else 0.85)
            painter.setPen(date_ink)
            painter.drawText(QRect(x, line_2.top(), when_width + 1, line_2.height()), left,
                             row.when)
            x += when_width + 6
        if row.detail:
            room = max(0, line_2.right() + 1 - x)
            painter.setPen(muted)
            painter.drawText(QRect(x, line_2.top(), room, line_2.height()), left,
                             metrics.elidedText(row.detail, Qt.TextElideMode.ElideRight, room))

    # -- tooltips -----------------------------------------------------------

    def helpEvent(self, event, view, option, index) -> bool:  # noqa: N802, Qt's name
        """The whole title, where the row cuts it short, and nothing where
        the row shows it whole: a tooltip repeating the row covered the
        row below it."""
        row = index.data(ROW_ROLE)
        if not isinstance(row, PostRow):
            return super().helpEvent(event, view, option, index)
        if self.title_is_cut(option.rect, row):
            QToolTip.showText(event.globalPos(), row.title, view)
        else:
            QToolTip.hideText()
        return True
