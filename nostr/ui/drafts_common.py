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

import time
from urllib.parse import urlparse

from PySide6.QtGui import QFont, QFontInfo
from PySide6.QtWidgets import QApplication

from constants import (
    DARK_BG,
    DARK_BORDER,
    DARK_FG,
    DARK_MENU_BG,
    DARK_MENU_FG,
    DARK_MUTED_FG,
    DARK_SELECTION,
    LIGHT_BG,
    LIGHT_BORDER,
    LIGHT_FG,
    LIGHT_MUTED_FG,
    LIGHT_SELECTION,
)

from ..draft_store import DraftRecord


# --------------------------------------------------------------------------- #
# Theme tokens                                                                #
# --------------------------------------------------------------------------- #

# One table per theme, and every QSS template in the drafts surfaces is
# filled from it. The panel used to carry two hand-maintained stylesheets
# with about sixty literal hexes between them, which is exactly how the
# two themes drifted apart: five text pairs failed the 4.5:1 minimum in
# light and four in dark, and fixing one meant hunting for its twin in
# the other sheet. Now a contrast fix is a single edit here, and the
# preview popover inherits it without a table of its own.
#
# App-wide colours come from ``constants.py``, the same single source of
# truth ``theme.py`` uses. Only roles with no app-wide token stay
# literal, and each literal appears exactly once.
#
# ``accessibility.md``: "Text size / Text weight / Minimum contrast ratio
# / Up to 17 pts / All / 4.5:1", and "If your app supports Dark Mode,
# make sure to check the minimum contrast in both light and dark
# appearances." Every text pair below clears 4.5:1 on the list
# background, on the hover fill and on the selection fill, in both
# themes. Disabled text sits deliberately below it so it still reads as
# unavailable (``labels.md``: "Tertiary label: Text that describes an
# unavailable item or behavior") but is legible rather than invisible.
THEME_TOKENS = {
    True: {
        "panel_bg": DARK_BG,               # list surface and field fill
        "chrome_bg": DARK_MENU_BG,          # the two bands above the list
        "border": DARK_BORDER,
        "chrome_fg": DARK_MENU_FG,
        "field_fg": DARK_FG,
        "muted": DARK_MUTED_FG,             # 5.15 list / 4.68 hover / 4.74 chrome
        "disabled": "#7A7A7A",
        "row_fg": "#FFFFFF",
        # Was #2A2D2E, which put the repo's muted grey at a marginal
        # 4.29:1. The fill is ours to choose and the token is not, so
        # the fill moved and the token stayed.
        "hover_bg": "#262626",
        "control_hover_bg": DARK_BORDER,
        "control_pressed_bg": DARK_BG,
        "selected_bg": "#094771",
        "selected_fg": "#FFFFFF",
        "selected_muted": "#C9D9E5",        # 6.76 on the selection fill
        "accent": "#007ACC",                # focus ring
        # The selection bar sits on top of the selection fill, so it is
        # not the accent: #007ACC on #094771 is 2.16:1 and the shape cue
        # disappears exactly where it has to be legible. 3.82:1 here.
        "selection_bar": "#4DA6FF",
        "text_selection_bg": DARK_SELECTION,
        "error_fg": "#F48771",
    },
    False: {
        "panel_bg": LIGHT_BG,
        "chrome_bg": "#F8F8F8",
        "border": LIGHT_BORDER,
        "chrome_fg": LIGHT_FG,
        "field_fg": LIGHT_FG,
        "muted": LIGHT_MUTED_FG,            # 5.17 list / 4.66 hover / 4.87 chrome
        "disabled": "#8A8A8A",
        "row_fg": "#1A1A1A",
        "hover_bg": "#F3F3F3",
        "control_hover_bg": LIGHT_BORDER,
        "control_pressed_bg": "#D0D0D0",
        "selected_bg": "#DCEEFA",
        "selected_fg": "#1A1A1A",
        "selected_muted": "#565656",        # 6.17 on the selection fill
        "accent": "#0078D4",
        "selection_bar": "#0F5FA8",         # 5.48 on the selection fill
        "text_selection_bg": LIGHT_SELECTION,
        "error_fg": "#C8412A",
    },
}


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


def format_absolute_time(ts: int) -> str:
    """The exact save time, in words, for tooltips and screen readers.

    It was previously unavailable to every user by any means.
    """
    if ts <= 0:
        return ""
    try:
        return time.strftime("%d %B %Y at %H:%M", time.localtime(int(ts)))
    except (ValueError, OverflowError, OSError):
        return ""


def format_absolute_date(ts: int) -> str:
    """The day only, for a publication date that has no useful clock time.

    Same ``strftime`` family as :func:`format_absolute_time` so the two
    dates on the preview's origin line cannot be formatted differently
    from one another.
    """
    if ts <= 0:
        return ""
    try:
        return time.strftime("%d %B %Y", time.localtime(int(ts)))
    except (ValueError, OverflowError, OSError):
        return ""
