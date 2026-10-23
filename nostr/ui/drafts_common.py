# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Colour, type and record-to-words, shared by the drafts surfaces.

The drafts panel and its hover preview have to agree on three things, or
the two of them contradict each other on screen: the colour tokens, the
type scale, and how a record's origin and save time read as words. They
cannot reach into each other for those, because the panel builds the
preview's controller and the preview would then have to import the panel
back. This module is what both import instead.

Everything here is pure and widget-free. ``QFont`` is the one Qt type it
touches, because the type scale is expressed in Python rather than in a
stylesheet: Qt treats both ``px`` and ``pt`` in QSS as absolute and
discards ``em``, so any size set in a sheet would freeze while the rest
of the app scaled around it.
"""

from __future__ import annotations

from urllib.parse import urlparse

from PySide6.QtCore import QDateTime, QLocale
from PySide6.QtGui import QFont, QFontInfo
from PySide6.QtWidgets import QApplication

import i18n

from ..draft_store import DraftRecord


# --------------------------------------------------------------------------- #
# Theme tokens                                                                #
# --------------------------------------------------------------------------- #

# Compatibility mapping for the existing panel/preview templates. All appearance
# values resolve through the shared semantic roles, including accessibility.
def theme_tokens(dark: bool) -> dict:
    from .appearance import roles
    r = roles(dark)
    return dict(panel_bg=r.panel, chrome_bg=r.chrome, border=r.line,
                chrome_fg=r.text, field_fg=r.text, muted=r.muted,
                disabled=r.disabled, row_fg=r.text, hover_bg=r.hover,
                control_hover_bg=r.hover, control_pressed_bg=r.soft,
                selected_bg=r.selection, selected_fg="#FFFFFF", accent=r.focus,
                text_selection_bg=r.selection, error_fg=r.error,
                link=r.ink)


class _Tokens:
    def __getitem__(self, dark):
        return theme_tokens(dark)


THEME_TOKENS = _Tokens()


# --------------------------------------------------------------------------- #
# Type scale                                                                  #
# --------------------------------------------------------------------------- #

# ``accessibility.md`` platform tables: type is "macOS 13 pt 10 pt"
# (default, minimum) and controls are "macOS 28x28 pt 20x20 pt". The
# 44x44 figure in ``buttons.md`` is the cross-platform fingertip rule;
# for a mouse-driven desktop side panel the macOS row governs.
MIN_TEXT_PT: float = 10.0
MIN_CONTROL_PX: int = 28

# The size the macOS text-style table in ``typography.md`` is written
# against, so every derived dimension can be expressed as a ratio of the
# application font and land on its documented value on a stock machine.
BASE_POINT_SIZE: float = 13.0


def app_point_size() -> float:
    """The application font size in points, whatever unit it was set in."""
    font = QApplication.font()
    size = font.pointSizeF()
    if size <= 0:
        # Set in pixels. QFontInfo reports what the platform actually
        # resolved, in points.
        size = float(QFontInfo(font).pointSizeF())
    return size if size > 0 else BASE_POINT_SIZE


def title_font() -> QFont:
    """The row title: application size, heavier weight.

    Weight only. A QSS ``font-weight: 600`` survives a Python-set font,
    but a QSS ``font-size`` would pin the row while the rest of the app
    scaled, so the size is inherited from the application font here.
    """
    font = QFont(QApplication.font())
    font.setWeight(QFont.DemiBold)
    return font


def secondary_font() -> QFont:
    """The age and meta line: one point below the title, clamped.

    Clamped at both ends. The floor honours the macOS 10 pt minimum. The
    ceiling stops the secondary text overtaking the title on a machine
    whose base font is already at or below 10 pt, where the hierarchy
    then rests on weight and colour, which is enough.
    """
    font = QFont(QApplication.font())
    app_pt = app_point_size()
    font.setPointSizeF(min(max(MIN_TEXT_PT, app_pt - 1.0), app_pt))
    return font


# The rows of the panel's lists (post_list.py): two stacked lines from one
# gutter; a selected row is a rounded shape inset from the list's edges,
# a shape as well as a fill (``accessibility.md``: "Offer visual
# indicators, like distinct shapes or icons, in addition to color"). The
# vertical values add up to the row height.
LIST_GUTTER: int = 12
ROW_PAD_V: int = 7
ROW_LINE_GAP: int = 2


def scaled(base: int) -> int:
    """``base`` px at the macOS default point size, scaled to this app's.

    Every gap, padding and box in the drafts surfaces goes through here
    so the whole composition grows together rather than a stack of text
    growing inside a frame that did not.
    """
    return max(1, round(base * app_point_size() / BASE_POINT_SIZE))


# --------------------------------------------------------------------------- #
# Record to words                                                             #
# --------------------------------------------------------------------------- #

# A source URL reduced to its host still has to fit beside a preview on
# a 260 px line, so a pathological one is capped rather than elided into
# eating the whole line.
MAX_SOURCE_CHARS: int = 28


def first_tag(record: DraftRecord, name: str) -> str:
    """The first value of the ``name`` tag on the inner event, or ''."""
    for tag in record.inner_tags or []:
        if isinstance(tag, list) and len(tag) >= 2 and tag[0] == name:
            return str(tag[1])
    return ""


def imported_source(record: DraftRecord) -> str:
    """The origin feed/file of an imported draft, or '' for authored ones.

    The importer writes a ``source`` tag on the inner event; it only
    becomes visible here after decryption.
    """
    return first_tag(record, "source")


def source_host(record: DraftRecord) -> str:
    """The origin of an imported draft, reduced to something row-sized.

    Replaces the fixed-width "Imported" pill, which read the same on 54
    of 54 rows in a real library and so discriminated nothing while
    costing 61 px of the title column. The host does discriminate, and
    it costs line 1 nothing.
    """
    raw = imported_source(record)
    if not raw:
        return ""
    parsed = urlparse(raw)
    if parsed.netloc:
        host = parsed.netloc.rsplit("@", 1)[-1].split(":")[0]
        if host.startswith("www."):
            host = host[4:]
    else:
        # Not a URL. A file import writes the file name here, and a
        # Windows path must not be mistaken for a scheme plus a host.
        host = raw.replace("\\", "/").rsplit("/", 1)[-1] or raw
    if len(host) <= MAX_SOURCE_CHARS:
        return host
    # Elide in the middle, not at the end. A host is identified by its
    # tail, so cutting "some-very-long-name.example.org" to
    # "some-very-long-name.example." throws away the informative half.
    keep = MAX_SOURCE_CHARS - 1
    head = keep // 2
    return f"{host[:head]}…{host[head - keep:]}"


def display_locale() -> QLocale:
    """The locale dates and numbers are written in: the language MyEditor
    speaks (i18n.display_locale, the one place that decides it)."""
    return i18n.display_locale()


def format_count(n: int) -> str:
    """A number with the language's thousands separator: 1,240 or 1.240."""
    return display_locale().toString(int(n))


def _moment(ts: int):
    """``ts`` as a local QDateTime, or None when it is no time."""
    if ts <= 0:
        return None
    try:
        moment = QDateTime.fromSecsSinceEpoch(int(ts))
    except (ValueError, OverflowError):
        return None
    return moment if moment.isValid() else None


def format_absolute_time(ts: int) -> str:
    """The exact save time, in words, for tooltips and screen readers:
    the language's own way of writing a day and a time ("January 6, 2027
    at 2:05 PM", "6. Januar 2027 um 14:05")."""
    moment = _moment(ts)
    return i18n.format_moment(moment) if moment is not None else ""


def format_absolute_date(ts: int) -> str:
    """The day only, for a publication date that has no useful clock time.

    Same day as :func:`format_absolute_time` writes, so the two dates on
    the preview's origin line cannot be formatted differently from one
    another.
    """
    moment = _moment(ts)
    return i18n.format_day(moment) if moment is not None else ""


def format_short_date(ts: int) -> str:
    """The day with a short month, for a list row with little room
    ("Jan 6, 2027", "6. Jan. 2027")."""
    moment = _moment(ts)
    return i18n.format_day(moment, short_month=True) if moment is not None else ""
