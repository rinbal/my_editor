# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Side-docked drafts panel, the editor's primary surface for NIP-37 drafts.

Layout, top to bottom:

  ┌──────────────────────────────────────────┐
  │  ( Drafts | Published )          ⋯    ×  │  band 1: the toolbar band's
  ├──────────────────────────────────────────┤  section over this pane
  │  [ Search Drafts                      ]  │  band 2: level with the
  ├──────────────────────────────────────────┤  document tabs
  │  Refreshing drafts…                      │  the banner, only with
  ├──────────────────────────────────────────┤  something to say
  │  [▢] Imports                    14 new › │  the Imports row
  │  Article title                      2h   │  list rows
  │  blog.example.com · first words of body  │
  │                                          │
  │   (empty state placeholder)              │
  └──────────────────────────────────────────┘

The panel is a pane of the window, not a thing beside it: the window's
divider is its left edge, and the window keeps its two bands as tall as
the format toolbar and the tab row beside it (``align_bands``,
window_grid.py), so the lines under them run straight across.

Everything starts at the same 12 px gutter: the view switch, the search
field, the banner, the Imports row, the empty state and both row lines.

Band 1 names the panel's view. With one view (Drafts) it is a title;
when another view is added (``add_view``), a segmented switch takes its
place, and where the segments do not fit beside the action menu and
the close button it becomes a pop-up button naming the current view, as
Apple's guidelines ask for tight spaces.

The action menu (⋯) holds what concerns the panel as a whole, where
panel-wide actions go: Refresh (the view shown reads again), Draft
Relays… (where the account keeps its drafts, also in the Nostr menu),
and in the Drafts view Select All and Delete for the drafts selected.

Words are never cut to fit: the band first changes control, then moves
the two buttons to a second line. Up to the platform's own text size
its narrowest width never depends on how long a label is, so wide
Windows and Linux fonts and German words do not widen the panel. Only
where even the pop-up button on a line of its own cannot name a view
whole (German at 200 percent on the narrowest panel) does the panel ask
for that room, up to MAX_PANEL_WIDTH.

The Imports row (the owner's decision D-2: one place for imports, the
Imports window) says how many new posts wait and opens the window; while
an import has posts to do, it says how far it is, with Pause or Resume.

Band 2 is the search field of the view shown: each view filters itself
(``add_view(search=...)``) and keeps what was typed into it.

Each row is two stacked lines with exactly one inline item, the age.
``typography.md`` warns that in a horizontally constrained context
inline glyphs and timestamps "can crowd text and cause truncation", and
recommends "a stacked layout where text appears above secondary items".
At ``MIN_PANEL_WIDTH`` there is room for one such item and no more, so
the lock glyph and the kind pill that used to share line 1 are gone and
their 80 px went to the title.

Decryption state is carried by three channels that survive greyscale and
colour blindness: the title's slant, the words on line 2, and the row's
accessible text. It used to be carried by the hue of one lock glyph.

The account the drafts belong to is shown by the account's picture at
the end of the tab row beside the panel (the ProfileChip, ``widgets.py``),
the one control that switches accounts. The panel carries no second chip
and no line naming the account: at rest it says nothing, and its banner
speaks only while there is something to know.

There is no kind filter. The wrap's ``k`` tag is optional, so an
undecrypted, loading or failed draft has ``inner_kind == 0``, and
filtering on it hid drafts instead of sorting them. Search is the only
filter, so clearing the field always shows every draft in the store.

The rows are painted by post_list.py, which the Published view uses
too, so both lists read alike: grouped by date as Notes groups notes,
two lines each (the title; what it is, when, and the words after the
title), selected as a Mac list selects. A draft's row is an item of
Qt's own list with its words in roles, so selection, the keyboard and
the scrollbar are the platform's.

Cross-platform discipline:
  - Glyphs are drawn (format_icons.py) or Unicode (⋯), so the panel
    renders identically on macOS / Windows / Linux without bundling fonts.
  - No platform-specific code paths, `is_dark` is the only branching
    axis, matching the rest of the editor; the selection takes the
    system's selection color (theme.list_selection_color).

The panel is **dormant** when no profile is bound: ``set_active_profile(None)``
puts it in a quiet "not connected" state. It is the host (MainWindow)'s
job not to *show* the panel at all when there is no profile, but if
it does, the panel won't crash, it'll just show the disconnected state.
"""

from __future__ import annotations

import time
from typing import Callable, List, Optional, Tuple

from PySide6.QtCore import QEvent, QPointF, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QFont,
    QFontMetrics,
    QKeySequence,
    QPainter,
    QPalette,
    QPen,
)
from PySide6.QtWidgets import (
    QApplication,
    QBoxLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QSizePolicy,
    QStackedLayout,
    QStackedWidget,
    QStyle,
    QStyleOptionButton,
    QStyleOptionToolButton,
    QStylePainter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import appearance
from i18n import _, ngettext
from segmented_control import Segment, SegmentedControl

from ..draft_store import DraftRecord, DraftState, DraftStore
from ..imports.constants import IDENTIFIER_PREFIX
from ..profiles import Profile
# Colour, type and record-to-words live in ``drafts_common`` because the
# preview popover needs the same three things and cannot import this
# module back: the panel is what builds the preview's controller. The
# aliases keep the panel's own body, and its tests, reading as before.
from .drafts_common import (
    LIST_GUTTER as GUTTER,
    MIN_CONTROL_PX as _MIN_CONTROL_PX,
    THEME_TOKENS as _THEME_TOKENS,
    format_absolute_date,
    format_absolute_time as _format_absolute_time,
    secondary_font as _secondary_font,
    source_host as _source_host,
)
from .drafts_preview import (
    PreviewAction,
    PreviewController,
    preview_announcement,
    preview_is_eligible,
)
from .eliding_label import ElidingLabel
from .post_list import (
    EMPTY_SYMBOL_SIZE, FAILED, LOADING, READY, ROW_ROLE, PostRow, PostRowDelegate, list_date,
    section_height, section_starts, symbol_pixmap, words_after_title,
)
from .imports_glyphs import glyph


# Qt's largest widget size, which lets a band take its own height again.
QWIDGETSIZE_MAX = (1 << 24) - 1

# Width hints. The host can resize through a QSplitter; these are the
# sensible defaults so the panel doesn't crowd the editor.
DEFAULT_PANEL_WIDTH: int = 320
MIN_PANEL_WIDTH: int = 260
MAX_PANEL_WIDTH: int = 520

# The panel's single gutter (GUTTER) comes from drafts_common, and the
# rows are drawn by post_list.py, which both of the panel's lists share,
# so its views read alike. The segmented
# control, the search field, the banner, the empty state and both
# row lines all start at the gutter. ``layout.md``: "Align components
# with one another to make them easier to scan and to communicate
# organization and hierarchy."

# How long a one-off sync narration holds the banner once the store
# is no longer loading. Matches the main window's own transient status
# messages, and lets the line fall back to naming the account.
_SYNC_MESSAGE_TTL_MS: int = 6_000


def _panel_css(is_dark: bool) -> str:
    """One QSS template for both themes, filled from the token table.

    No ``font-size`` declaration appears here on purpose. Qt treats both
    ``px`` and ``pt`` in a stylesheet as absolute and discards ``em``
    outright, so any size set here would freeze while the rest of the
    app scaled around it. QSS carries colour and weight; the point sizes
    are set in Python, from ``QApplication.font()``.
    """
    t = _THEME_TOKENS[bool(is_dark)]
    return f"""
/* No edge of its own: the window's divider is the panel's left edge. */
QFrame#drafts_panel {{ background: {t["panel_bg"]}; }}
/* Band 1 is the toolbar band's section over this pane: its background and
   its line. Band 2 is the tab row's: the list's ground and the line under
   the document tabs. */
QFrame#drafts_panel_top_band {{
    background: {t["chrome_bg"]};
    border: none;
    border-bottom: 1px solid {t["border"]};
}}
QFrame#drafts_panel_search_band {{
    background: {t["panel_bg"]};
    border: none;
    border-bottom: 1px solid {t["border"]};
}}
QFrame#drafts_panel_imports {{ background: {t["panel_bg"]}; border: none; }}
QFrame#drafts_panel_banner {{
    background: {t["panel_bg"]};
    border: none;
    border-bottom: 1px solid {t["border"]};
}}
QLabel#drafts_panel_locked {{ color: {t["muted"]}; }}
/* Try Again under the line, as a link: a quiet way to ask now, where a
   bordered button would make the line read as a problem. The transparent
   border leaves room for the focus ring without moving the words. */
QPushButton#drafts_panel_banner_action {{
    background: transparent;
    color: {t["link"]};
    border: 1px solid transparent;
    border-radius: 3px;
    padding: 0px;
    text-align: left;
}}
QPushButton#drafts_panel_banner_action:pressed {{ color: {t["muted"]}; }}
QPushButton#drafts_panel_banner_action:focus {{ border-color: {t["accent"]}; }}
QLabel#drafts_panel_title {{ color: {t["chrome_fg"]}; font-weight: 600; }}
QLabel#drafts_panel_imports_activity {{ color: {t["muted"]}; }}
QPushButton#drafts_panel_imports_open {{
    background: transparent;
    color: {t["row_fg"]};
    border: 1px solid transparent;
    border-radius: 6px;
}}
QPushButton#drafts_panel_imports_open:hover {{ background: {t["hover_bg"]}; }}
QPushButton#drafts_panel_imports_action {{
    background: transparent;
    color: {t["row_fg"]};
    border: 1px solid {t["border"]};
    border-radius: 4px;
    padding: 2px 10px;
}}
QPushButton#drafts_panel_imports_action:hover {{ background: {t["hover_bg"]}; }}
QToolButton#drafts_panel_view_popup {{
    background: transparent;
    color: {t["row_fg"]};
    border: 1px solid {t["border"]};
    border-radius: 4px;
    padding: 2px {_POPUP_CHEVRON_ROOM}px 2px 8px;
}}
/* The pop-up button draws its own chevrons in that room (_ViewPopup);
   the style's arrow sat in the corner, on the view's name. */
QToolButton#drafts_panel_view_popup::menu-indicator {{ image: none; width: 0px; }}
QToolButton#drafts_panel_view_popup:hover {{ background: {t["hover_bg"]}; }}
QLabel#drafts_panel_status {{ color: {t["muted"]}; }}
QLabel#drafts_panel_status[error="true"] {{ color: {t["error_fg"]}; }}

QToolButton#drafts_panel_icon_btn {{
    background: transparent;
    color: {t["chrome_fg"]};
    /* Transparent rather than none so the focus ring below can appear
       without nudging the glyph by a pixel. */
    border: 1px solid transparent;
    border-radius: 4px;
    padding: 2px 6px;
}}
QToolButton#drafts_panel_icon_btn:hover {{ background: {t["control_hover_bg"]}; }}
QToolButton#drafts_panel_icon_btn:pressed {{ background: {t["control_pressed_bg"]}; }}
QToolButton#drafts_panel_icon_btn:disabled {{ color: {t["disabled"]}; }}
QToolButton#drafts_panel_icon_btn::menu-indicator {{ image: none; width: 0px; }}

/* Focus indicators. Measured before this change: rendering each control
   focused and unfocused differed by zero pixels, because setting a
   background and a border in QSS makes Qt paint the whole control from
   the stylesheet box model and skip the native ring. accessibility.md:
   "Let people use the keyboard alone to navigate and interact with your
   app... evaluate your app to ensure it works well with Full Keyboard
   Access." The view switch draws its own ring (segmented_control.py). */
QToolButton#drafts_panel_icon_btn:focus,
QToolButton#drafts_panel_view_popup:focus,
QPushButton#drafts_panel_imports_open:focus,
QPushButton#drafts_panel_imports_action:focus,
QPushButton#drafts_panel_empty_action:focus {{ border-color: {t["accent"]}; }}

QLineEdit#drafts_panel_search {{
    background: {t["panel_bg"]};
    color: {t["field_fg"]};
    border: 1px solid {t["border"]};
    border-radius: 4px;
    padding: 3px 8px;
    selection-background-color: {t["text_selection_bg"]};
}}
QLineEdit#drafts_panel_search:focus {{ border-color: {t["accent"]}; }}

QListWidget#drafts_panel_list {{
    background: {t["panel_bg"]};
    border: none;
}}
QListWidget#drafts_panel_list::item {{ border: none; background: transparent; }}

QLabel#drafts_panel_empty_title {{ color: {t["chrome_fg"]}; font-weight: 600; }}
QLabel#drafts_panel_empty_body {{ color: {t["muted"]}; }}
QPushButton#drafts_panel_empty_action {{
    background: transparent;
    color: {t["row_fg"]};
    border: 1px solid {t["border"]};
    border-radius: 4px;
    padding: 4px 12px;
}}
QPushButton#drafts_panel_empty_action:hover {{ background: {t["hover_bg"]}; }}

"""


def _menu_css(is_dark: bool) -> str:
    """Context-menu QSS, from the same token table as the panel."""
    t = _THEME_TOKENS[bool(is_dark)]
    return f"""
QMenu {{
    background: {t["chrome_bg"]};
    color: {t["chrome_fg"]};
    border: 1px solid {t["border"]};
    padding: 4px;
}}
QMenu::item {{ padding: 4px 20px 4px 14px; }}
QMenu::item:selected {{
    background: {t["selected_bg"]};
    color: {t["selected_fg"]};
}}
QMenu::item:disabled {{ color: {t["disabled"]}; }}
QMenu::separator {{ height: 1px; background: {t["border"]}; margin: 4px 0px; }}
"""


# --------------------------------------------------------------------------- #
# Type scale and derived geometry                                             #
# --------------------------------------------------------------------------- #

def _control_height() -> int:
    """Minimum height for the panel's chrome controls.

    Expressed from font metrics rather than as a QSS ``padding`` literal
    so it still holds when the application font grows. The segments
    measured 25 px and the icon buttons about 24 before this; the macOS
    default control size is 28.
    """
    return max(_MIN_CONTROL_PX, QFontMetrics(QApplication.font()).height() + 8)


def _save_shortcut_text() -> str:
    """The stash shortcut in the host platform's own notation.

    A literal "Ctrl+Shift+S" tells a Mac user to press a combination
    that does nothing there; the same sequence renders as ⇧⌘S.
    ``writing.md``: "Write for how people use each device."
    """
    return QKeySequence("Ctrl+Shift+S").toString(QKeySequence.NativeText)


# --------------------------------------------------------------------------- #
# Record to text                                                              #
# --------------------------------------------------------------------------- #

def _display_title(record: DraftRecord) -> str:
    """Line 1 of a row. Shared so the row and its announcement agree."""
    if record.state is DraftState.LOADING:
        return _("Decrypting…")
    return record.title or _("(no title)")


def _display_meta(record: DraftRecord) -> str:
    """Line 2 of a row after its date: one composed string, one elision.

    The words after the title, never the title again: a note's title is
    its first line, and its preview used to start with that same line.
    Elide-right naturally preserves the host, because the host sits at
    the head of the line.
    """
    if record.state is DraftState.LOADING:
        return _("Decrypting")
    if record.state is DraftState.FAILED:
        reason = record.failure_reason or _("Could not decrypt")
        # With the ciphertext still in hand a retry is one double-click
        # away, so the row says so rather than looking permanently broken.
        if record.ciphertext:
            return _("{reason}. Double-click to retry.").format(reason=reason)
        return reason
    host = _source_host(record)
    # One of the person's own drafts that still carries an end date (saved
    # before drafts stopped expiring) says so where it is seen, with the
    # way to keep it (engine review M6). An imported draft keeps its
    # preview: its 90 days are said once, where drafts are created (Q-11),
    # not on every row (CP2 review N6); its tooltip and its spoken text
    # say it.
    preview = words_after_title(record.title, record.snippet or "")
    if not is_imported(record):
        preview = expiry_note(record.expiration) or preview
    if host and preview:
        return f"{host} · {preview}"
    return host or preview


def is_imported(record: DraftRecord) -> bool:
    """Whether a draft came from Imports (its identifier, or the source it
    names inside)."""
    return record.identifier.startswith(IDENTIFIER_PREFIX) or bool(_source_host(record))


def expiry_note(expiration: Optional[int]) -> str:
    """"Removed on 04 January 2027 unless you save it again", or ''."""
    if not expiration:
        return ""
    return _("Removed on {date} unless you save it again").format(
        date=format_absolute_date(int(expiration)))


def _accessible_row_text(record: DraftRecord, *, now: Optional[int] = None) -> str:
    """What a screen reader announces for one row.

    The visible row is painted (post_list.py), so the item itself has no
    display text, and a screen reader heard a list of blank rows. This
    goes on the item, in reading order, in words rather than glyphs.
    ``accessibility.md``: "Describe your app's interface and content for
    VoiceOver... VoiceOver is a screen reader that lets people
    experience your app's interface without needing to see the screen."

    The reading time, the hashtags and an imminent expiry are appended
    after the save time, so everything the hover preview shows is
    reachable without a pointer and without opening anything. That is
    what keeps the preview a visual convenience rather than an
    information channel, which is the only way a surface that dismisses
    itself on a timer can satisfy ``accessibility.md``'s "Prefer
    dismissing views with an explicit action".
    """
    sentences = [_display_title(record).rstrip("…")]
    if record.state is DraftState.LOADING:
        sentences.append(_("Decrypting"))
    elif record.state is DraftState.FAILED:
        sentences.append(record.failure_reason or _("Could not decrypt"))
        if record.ciphertext:
            sentences.append(_("Press Return to retry"))
    else:
        host = _source_host(record)
        if host:
            sentences.append(_("Imported from {host}").format(host=host))
    saved = _format_absolute_time(record.created_at)
    if saved:
        sentences.append(_("Saved {time}").format(time=saved))
    if record.state is DraftState.READY and expiry_note(record.expiration):
        sentences.append(expiry_note(record.expiration))
    extra = preview_announcement(
        record, now=now if now is not None else int(time.time()),
    )
    if extra:
        sentences.append(extra)
    return ". ".join(s.rstrip(". ") for s in sentences if s.strip()) + "."


# What a draft's inner kind is, as the row's symbol says it.
_KIND_NAMES = {1: "note", 30023: "article"}

_ROW_STATES = {DraftState.READY: READY, DraftState.LOADING: LOADING,
               DraftState.FAILED: FAILED}


def _draft_row(record: DraftRecord, *, now: int) -> PostRow:
    """One draft as its row draws it (post_list.py): the title, then what
    it is, when it was saved and the words after the title. A draft still
    decrypting knows none of that yet; one that could not be decrypted
    says why, with an alert at the end of its title."""
    when = list_date(record.created_at, now)
    state = _ROW_STATES.get(record.state, READY)
    if record.state is DraftState.LOADING:
        return PostRow(_display_title(record), when=when, state=state)
    detail = _display_meta(record)
    if record.state is DraftState.FAILED:
        return PostRow(_display_title(record), when=when, detail=detail, state=state,
                       mark="alert")
    expiring = not is_imported(record) and bool(expiry_note(record.expiration))
    return PostRow(_display_title(record), kind=_KIND_NAMES.get(record.inner_kind, ""),
                   when=when, detail=detail or _("No additional text"), state=state,
                   mark="clock" if expiring else "")


# --------------------------------------------------------------------------- #
# Chrome widgets                                                              #
# --------------------------------------------------------------------------- #

class _ElidingLabel(ElidingLabel):
    """Single-line label that elides at paint time, from its own width
    (eliding_label.py, shared with the Imports window).

    Elision must not run through ``QLabel.setText``: that changes the
    label's sizeHint, which re-runs the layout and moves the width the
    elision was computed against, so the string overflows the final
    label and QLabel hard-clips the ellipsis that was added. That is why
    a row once read ``![One Class, One`` with no trailing dots at every
    panel width. ``QSizePolicy.Ignored`` plus a zero minimum width keeps
    a very long title from forcing the panel wider than the user dragged
    it, which matters most at ``MIN_PANEL_WIDTH``.

    The stylesheet gives these labels colour and weight only. The
    tooltip is owned by the row, so the whole row is readable from any
    point on it rather than one line at a time.
    """

    def __init__(self, parent: Optional[QWidget] = None,
                 mode: Qt.TextElideMode = Qt.ElideRight) -> None:
        super().__init__("", parent, mode=mode, tooltip=False)


# Room on the right of the view pop-up for its chevrons, in pixels.
_POPUP_CHEVRON_ROOM = 20


class _ViewPopup(QToolButton):
    """The view switch where its segments do not fit: a pop-up button
    naming the current view. ``buttons.md``: a pop-up button "displays a
    menu of mutually exclusive options" and suits tight spaces where a
    segmented control does not fit.

    It claims no minimum width of its own: the band only shows it where
    it fits whole, so the panel never grows for a long view name. Past
    every size that can happen (text far beyond 200 percent on a 260 px
    panel), it elides as a last resort, and its tooltip keeps the name.
    """

    def __init__(self, control_h: int, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("drafts_panel_view_popup")
        self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.setMinimumHeight(control_h)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.setMenu(QMenu(self))

    def minimumSizeHint(self) -> QSize:
        hint = super().minimumSizeHint()
        hint.setWidth(0)
        return hint

    def painted_text(self) -> str:
        """The string ``paintEvent`` draws at the current width: the whole
        name wherever the button has the room it asks for."""
        if self.width() >= self.sizeHint().width():
            return self.text()
        inner = self.width() - (self.sizeHint().width()
                                - self.fontMetrics().horizontalAdvance(self.text()))
        return self.fontMetrics().elidedText(self.text(), Qt.ElideRight, max(0, inner))

    def paintEvent(self, event) -> None:
        option = QStyleOptionToolButton()
        self.initStyleOption(option)
        option.text = self.painted_text()
        painter = QStylePainter(self)
        painter.drawComplexControl(QStyle.CC_ToolButton, option)
        # A pop-up button's chevrons, up and down (one of several views),
        # in the room the style sheet keeps for them on the right.
        unit = max(1.0, self.fontMetrics().height() / 16.0)
        x = self.width() - _POPUP_CHEVRON_ROOM / 2.0 - 1.0
        y = self.height() / 2.0
        half, rise, gap = 3.0 * unit, 2.5 * unit, 1.5 * unit
        pen = QPen(self.palette().color(self.foregroundRole()), 1.4)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(pen)
        painter.drawPolyline([QPointF(x - half, y - gap), QPointF(x, y - gap - rise),
                              QPointF(x + half, y - gap)])
        painter.drawPolyline([QPointF(x - half, y + gap), QPointF(x, y + gap + rise),
                              QPointF(x + half, y + gap)])


class _ViewSwitch(QWidget):
    """Segments for the panel's views (the shared segmented control, as
    the toolbar's Write · Preview · Split), or a pop-up button where they
    do not fit (see ``_TopBand``).

    Signals:
      view_chosen(str)   a view was picked (its key)
    """

    view_chosen = Signal(str)

    def __init__(self, control_h: int, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("drafts_panel_view_switch")
        self._control_h = control_h
        self._views: List[Tuple[str, str, str]] = []
        self._current = ""
        self._segments = SegmentedControl()
        self._segments.chosen.connect(self.view_chosen)
        self.popup = _ViewPopup(control_h)
        self.popup.setAccessibleName(_("View"))
        self.popup.hide()
        # One slot for the menu, the view's key on each action: no closure
        # over this switch, which would keep it and the panel alive.
        self.popup.menu().triggered.connect(self._on_menu_action)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        row.addWidget(self._segments)
        row.addWidget(self.popup)
        # The band shows the segments only where they fit, and the pop-up
        # button claims no width of its own: the switch never widens the
        # panel. (By default a layout would hold its widget at least as
        # wide as everything in it.)
        row.setSizeConstraint(QBoxLayout.SetNoConstraint)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

    def minimumSizeHint(self) -> QSize:
        return QSize(0, self._control_h)

    def sizeHint(self) -> QSize:
        shown = self.popup if self.is_popup() else self._segments
        return QSize(shown.sizeHint().width(), self._control_h)

    @property
    def segments(self) -> List[Segment]:
        return self._segments.segments

    def set_dark(self, dark: bool) -> None:
        self._segments.set_dark(dark)

    def has_choice(self) -> bool:
        return len(self._views) > 1

    def is_popup(self) -> bool:
        return not self.popup.isHidden()

    def segments_width(self) -> int:
        return self._segments.sizeHint().width()

    def popup_width(self) -> int:
        """The pop-up button's width for the longest view name, so the
        band keeps its shape whichever view is up. Each name is measured
        the way the button sizes its own text (QToolButton.sizeHint), not
        by its advance, which can be a pixel short."""
        metrics = self.popup.fontMetrics()

        def text_width(text: str) -> int:
            return metrics.size(Qt.TextFlag.TextShowMnemonic, text).width()

        chrome = self.popup.sizeHint().width() - text_width(self.popup.text())
        return chrome + max((text_width(title) for _key, title, _hint in self._views),
                            default=0)

    def use_popup(self, popup: bool) -> None:
        if popup == self.is_popup():
            return
        self.popup.setVisible(popup)
        self._segments.setVisible(not popup)
        self.updateGeometry()

    def set_views(self, views, current: str) -> None:
        self._views = list(views)
        self._segments.set_segments(self._views)
        menu = self.popup.menu()
        menu.clear()
        for key, title, hint in self._views:
            action = menu.addAction(title)
            action.setCheckable(True)
            action.setToolTip(hint)
            action.setData(key)
        self.set_current(current)

    def _on_menu_action(self, action: QAction) -> None:
        key = action.data()
        if isinstance(key, str) and key:
            self.view_chosen.emit(key)

    def set_current(self, key: str) -> None:
        self._current = key
        self._segments.set_current(key)
        for index, (view_key, title, _hint) in enumerate(self._views):
            chosen = view_key == key
            actions = self.popup.menu().actions()
            if index < len(actions):
                actions[index].setChecked(chosen)
            if chosen:
                self.popup.setText(title)
                self.popup.setToolTip(title)
                # The control is named for what it switches, and says
                # which view is up, as a pop-up button reads.
                self.popup.setAccessibleName(_("View: {title}").format(title=title))
        self.updateGeometry()


class _TopBand(QFrame):
    """Band 1: the view's title or the switch, then refresh and close.

    With one view the title leads, and it elides rather than wrap, so
    band 1 is always one line. With more, the band picks the first of
    these that fits, so a word is never cut to fit (``typography.md``:
    "Consider adjusting your layout at large font sizes"):

    1. the segments, beside the two buttons;
    2. the pop-up button, beside the two buttons;
    3. the pop-up button on its own line, the two buttons on the next.

    The band's narrowest width never depends on a label: the title
    elides and the switch claims no width of its own, so wide fonts and
    long words do not widen the panel.

    Reflowing is one ``setDirection`` call and two visibility flips,
    with no reparenting, so focus chain and tab order are untouched. It
    cannot oscillate: the choice depends on the band's width and on the
    size hints of the candidates, which the choice does not change.
    """

    def __init__(self, title: QWidget, switch: "_ViewSwitch", icons: QWidget,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("drafts_panel_top_band")
        self._title = title
        self._switch = switch
        self._icons = icons
        self._box = QBoxLayout(QBoxLayout.LeftToRight, self)
        self._box.setContentsMargins(GUTTER, 5, 8, 5)
        self._box.setSpacing(6)
        self._box.addWidget(title, 1)
        self._box.addWidget(switch)
        # Collapses to nothing in the vertical direction, because the
        # band is only ever given its sizeHint height.
        self._box.addStretch(0)
        self._box.addWidget(icons)
        switch.hide()

    def is_stacked(self) -> bool:
        """True when the icon buttons have moved to their own line."""
        return self._box.direction() == QBoxLayout.TopToBottom

    def popup_room(self) -> int:
        """The band's width that shows the pop-up button whole on a line
        of its own, for the longest view name (0 with one view)."""
        if not self._switch.has_choice():
            return 0
        margins = self._box.contentsMargins()
        return self._switch.popup_width() + margins.left() + margins.right()

    def reflow(self) -> None:
        margins = self._box.contentsMargins()
        available = self.width() - margins.left() - margins.right()
        beside = self._icons.sizeHint().width() + self._box.spacing()
        stacked = False
        if not self._switch.has_choice():
            self._switch.hide()
            self._title.show()
        else:
            self._title.hide()
            self._switch.show()
            if self._switch.segments_width() + beside <= available:
                self._switch.use_popup(False)
            else:
                self._switch.use_popup(True)
                stacked = self._switch.popup_width() + beside > available
        wanted = QBoxLayout.TopToBottom if stacked else QBoxLayout.LeftToRight
        if self._box.direction() != wanted:
            self._box.setDirection(wanted)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.reflow()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.FontChange:
            self.reflow()


class _LauncherButton(QPushButton):
    """The Imports row's button: a tray, "Imports", how many new posts
    wait, and a chevron that says it opens something.

    Painted rather than laid out, so its narrowest width never depends on
    its words. Where the count does not fit beside the title (large text
    on a narrow panel), it moves under the title and the button grows a
    line, so both stay whole; only past every ordinary size does the
    title elide, and the full words stay in the accessible name and the
    tooltip.
    """

    _PAD, _GAP, _COUNT_GAP = 6, 8, 12

    def __init__(self, control_h: int, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("drafts_panel_imports_open")
        self.setCursor(Qt.PointingHandCursor)
        self._control_h = control_h
        self._count_text = ""
        self.setText(_("Imports"))
        self.setToolTip(_("Open the Imports window"))
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Minimum)
        self.set_count(0)

    def set_count(self, new_posts: int) -> None:
        self._count_text = (ngettext("{count} new", "{count} new", new_posts).format(
            count=new_posts) if new_posts else "")
        self.setAccessibleName(
            ngettext("Imports, {count} new post", "Imports, {count} new posts",
                     new_posts).format(count=new_posts) if new_posts else _("Imports"))
        self._fit_height()
        self.updateGeometry()
        self.update()

    def count_text(self) -> str:
        return self._count_text

    def _count_font(self) -> QFont:
        font = QFont(self.font())
        font.setBold(True)
        return font

    def _sizes(self):
        """Icon, chevron, title and count widths, and the line height."""
        metrics = self.fontMetrics()
        count = (QFontMetrics(self._count_font()).horizontalAdvance(self._count_text)
                 if self._count_text else 0)
        return (metrics.height(), metrics.horizontalAdvance("›"),
                metrics.horizontalAdvance(self.text()), count, metrics.height())

    def _one_line_width(self) -> int:
        icon, chevron, title, count, _line = self._sizes()
        width = self._PAD + icon + self._GAP + title + self._GAP + chevron + self._PAD
        return width + (self._COUNT_GAP + count if count else 0)

    def is_stacked(self) -> bool:
        """True when the count sits under the title."""
        return bool(self._count_text) and self.width() < self._one_line_width()

    def _height(self, lines: int) -> int:
        line = self._sizes()[4]
        return max(self._control_h, line * lines + 10 + (2 if lines > 1 else 0))

    def sizeHint(self) -> QSize:
        return QSize(self._one_line_width(), self._height(2 if self.is_stacked() else 1))

    def minimumSizeHint(self) -> QSize:
        icon, chevron, _title, _count, _line = self._sizes()
        return QSize(self._PAD + icon + self._GAP + chevron + self._PAD, self._height(1))

    def heightForWidth(self, width: int) -> int:
        stacked = bool(self._count_text) and width < self._one_line_width()
        return self._height(2 if stacked else 1)

    def _fit_height(self) -> None:
        """A line taller while the count sits under the title. Only the
        height follows the width, so this settles at once."""
        wanted = self.heightForWidth(self.width())
        if self.minimumHeight() != wanted:
            self.setMinimumHeight(wanted)
            self.updateGeometry()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit_height()

    def painted_title(self) -> str:
        icon, chevron, _title, count, _line = self._sizes()
        used = self._PAD + icon + self._GAP + self._GAP + chevron + self._PAD
        if self._count_text and not self.is_stacked():
            used += self._COUNT_GAP + count
        return self.fontMetrics().elidedText(self.text(), Qt.ElideRight,
                                             max(0, self.width() - used))

    def paintEvent(self, event) -> None:
        option = QStyleOptionButton()
        self.initStyleOption(option)
        option.text = ""
        painter = QStylePainter(self)
        painter.drawControl(QStyle.CE_PushButton, option)
        icon, chevron, _title, count, line = self._sizes()
        rect = self.rect()
        stacked = self.is_stacked()
        first = QRect(rect.left(), rect.top() + 5, rect.width(), line) if stacked else rect
        colour = self.palette().color(QPalette.ButtonText)
        left = self._PAD
        painter.drawPixmap(left, first.center().y() - icon // 2 + 1,
                           glyph("inbox", icon, colour, self.devicePixelRatioF()))
        left += icon + self._GAP
        painter.setPen(colour)
        right = rect.right() - self._PAD
        painter.drawText(QRect(right - chevron + 1, first.top(), chevron, first.height()),
                         int(Qt.AlignRight | Qt.AlignVCenter), "›")
        right -= chevron + self._GAP
        if self._count_text:
            painter.setFont(self._count_font())
            if stacked:
                painter.drawText(QRect(left, first.bottom() + 3, max(0, right - left + 1), line),
                                 int(Qt.AlignLeft | Qt.AlignVCenter), self._count_text)
            else:
                painter.drawText(QRect(right - count + 1, rect.top(), count, rect.height()),
                                 int(Qt.AlignRight | Qt.AlignVCenter), self._count_text)
                right -= count + self._COUNT_GAP
            painter.setFont(self.font())
        painter.drawText(QRect(left, first.top(), max(0, right - left + 1), first.height()),
                         int(Qt.AlignLeft | Qt.AlignVCenter), self.painted_title())


class _ImportsRow(QFrame):
    """The Drafts panel's one row about imports (D-2).

    Signals:
      open_requested()     the row: open the Imports window
      pause_requested()    Pause
      resume_requested()   Resume, or Try Again
    """

    open_requested = Signal()
    pause_requested = Signal()
    resume_requested = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("drafts_panel_imports")
        control_h = _control_height()
        column = QVBoxLayout(self)
        column.setContentsMargins(GUTTER - 6, 4, GUTTER - 6, 4)
        column.setSpacing(2)
        self.button = _LauncherButton(control_h)
        self.button.clicked.connect(self.open_requested)
        column.addWidget(self.button)
        line = QHBoxLayout()
        line.setContentsMargins(6, 0, 0, 2)
        line.setSpacing(8)
        self.activity = QLabel()
        self.activity.setObjectName("drafts_panel_imports_activity")
        self.activity.setFont(_secondary_font())
        self.activity.setTextFormat(Qt.PlainText)
        self.activity.setWordWrap(True)
        self.activity.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        line.addWidget(self.activity, 1)
        self.action = QPushButton()
        self.action.setObjectName("drafts_panel_imports_action")
        self.action.setText(_("Pause"))
        self.action.clicked.connect(self._act)
        line.addWidget(self.action, 0, Qt.AlignTop)
        self._activity_line = QWidget()
        self._activity_line.setLayout(line)
        column.addWidget(self._activity_line)
        self._action = ""
        self._activity_line.hide()
        self.hide()

    def minimumSizeHint(self) -> QSize:
        hint = super().minimumSizeHint()
        hint.setWidth(min(hint.width(), self.button.minimumSizeHint().width() + 2 * GUTTER))
        return hint

    def show_state(self, *, available: bool, new_posts: int, activity: str,
                   action: str) -> None:
        self.setVisible(available)
        self.button.set_count(new_posts)
        self._action = action
        self.activity.setText(activity)
        words = {"pause": _("Pause"), "resume": _("Resume"), "retry": _("Try Again")}
        self.action.setText(words.get(action, _("Pause")))
        self.action.setVisible(bool(action))
        self._activity_line.setVisible(bool(activity))

    def _act(self) -> None:
        if self._action == "pause":
            self.pause_requested.emit()
        elif self._action in ("resume", "retry"):
            self.resume_requested.emit()


# --------------------------------------------------------------------------- #
# Drafts panel                                                                #
# --------------------------------------------------------------------------- #

# What typing a draft's first letters finds it by: its title (or its
# first words), kept on the row, which has no text of its own to show.
TITLE_ROLE = Qt.UserRole + 1


class _DraftList(QListWidget):
    """The drafts, with type-to-select by title as in every list on the
    system: the rows are widgets, so Qt's own search finds no text."""

    def __init__(self) -> None:
        super().__init__()
        self._typed, self._typed_at = "", 0.0

    def keyboardSearch(self, search: str) -> None:  # noqa: N802, Qt's name
        now = time.monotonic()
        if now - self._typed_at > QApplication.keyboardInputInterval() / 1000:
            self._typed = ""
        self._typed_at, self._typed = now, self._typed + search.lower()
        count = self.count()
        # One letter again moves on to the next match; more letters refine.
        start = self.currentRow() + (1 if len(self._typed) == 1 else 0)
        for offset in range(count):
            item = self.item((start + offset) % count)
            if str(item.data(TITLE_ROLE) or "").lower().startswith(self._typed):
                self.setCurrentItem(item)
                return


class DraftsPanel(QFrame):
    """Side-docked drafts surface.

    Public signals (the host wires these into MainWindow handlers):
      open_draft(str)            , identifier of a draft to open in a new tab
      publish_draft(str)         , identifier to promote draft → real publish
      delete_drafts(list)        , identifiers to tombstone, one or many
      copy_event_id(str)         , copy outer wrap event id to clipboard
      refresh_requested()        , Refresh in the action menu (the Drafts
                                   view; a view added with a ``refresh``
                                   of its own reads again itself)
      draft_relays_requested()   , Draft Relays… in the action menu
      hide_requested()           , Hide Drafts in the action menu (the
                                   toolbar's toggle and the Nostr menu
                                   show and hide it too)
      view_changed(str)          , another view was chosen (its key)
      open_imports()             , the Imports row: open the Imports window
      pause_import()             , the Imports row's Pause
      resume_import()            , the Imports row's Resume or Try Again

    Switching profiles is not one of these: the ProfileChip at the end
    of the tab row owns that menu and names the account.

    Public methods:
      bind_store(store)          , connect to a DraftStore instance
      set_active_profile(p)      , the account the drafts belong to
      set_status(text)           , narrate a sync step in the banner
      align_bands(toolbar, search), the window's grid (window_grid.py)
      set_signer_unsupported(bool), show the "signer lacks NIP-44" state
      set_signer_unreachable(bool), drafts wait for the signer app: one
                                   quiet line with Try Again (retry_signer)
      apply_theme(is_dark)       , switch dark/light
      set_preview_image_loader(l), hand the hover preview a ThumbnailLoader
      add_view(key, title, hint, widget, refresh=, refresh_name=),
                                   another view beside Drafts
      show_view(key) / current_view()
      set_imports_state(...)     , what the Imports row says

    Hovering a row opens a preview of that draft after half a second.
    That surface lives in ``drafts_preview.py``; the panel owns one
    controller and answers four questions for it, below under "preview
    host". It hands over a record, never a widget, so a search keystroke
    can destroy every row without leaving the controller holding a dead
    pointer.
    """

    open_draft = Signal(str)
    publish_draft = Signal(str)
    # One signal for one draft and for twenty. The host asks the same
    # question either way, and a separate single-draft path was a second
    # confirmation and a second delete job to keep in step with this one.
    delete_drafts = Signal(list)
    retry_decrypt = Signal(str)
    retry_signer = Signal()
    delete_drafts = Signal(list)
    copy_event_id = Signal(str)
    refresh_requested = Signal()
    draft_relays_requested = Signal()
    hide_requested = Signal()
    view_changed = Signal(str)
    open_imports = Signal()
    pause_import = Signal()
    resume_import = Signal()

    DRAFTS_VIEW = "drafts"

    def __init__(self, *, is_dark: bool = True, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("drafts_panel")
        self.setFrameShape(QFrame.NoFrame)
        self.setMinimumWidth(MIN_PANEL_WIDTH)
        self.setMaximumWidth(MAX_PANEL_WIDTH)
        self.resize(DEFAULT_PANEL_WIDTH, self.height())

        self._is_dark = is_dark
        self._store: Optional[DraftStore] = None
        self._active_profile: Optional[Profile] = None
        self._search_text: str = ""
        self._signer_unsupported: bool = False
        self._signer_unreachable: bool = False
        self._loading: bool = False
        self._sync_message: str = ""
        # Maps draft identifier → QListWidgetItem so signal updates can
        # find their row without scanning.
        self._items: dict[str, QListWidgetItem] = {}
        # The panel's views: (key, title, hint) each, Drafts first.
        self._views: List[Tuple[str, str, str]] = [
            (self.DRAFTS_VIEW, _("Drafts"), _("Your saved private drafts"))]
        self._view_pages: dict = {}
        # How each view reads again, and what the refresh button is
        # called while it is shown. Drafts: refresh_requested, to the host.
        self._view_refresh: dict = {}
        self._refresh_names: dict = {self.DRAFTS_VIEW: _("Refresh Drafts")}
        # Each view's search: what the field is called while it is shown,
        # how the view filters, and what was typed into it, kept per view.
        self._search_names: dict = {self.DRAFTS_VIEW: _("Search Drafts")}
        self._view_search: dict = {}
        self._view_search_text: dict = {}

        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.timeout.connect(self._on_status_expired)

        # Built before the UI, because ``_insert_row`` hands it every row
        # it creates and a rebuild can happen during ``_build_ui``.
        self._preview = PreviewController(self)

        self._build_ui()
        self._preview.attach_list(self._list)
        self.apply_theme(is_dark)
        self._refresh_empty_state()
        self._render_status()

    # -- construction ------------------------------------------------------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        outer.addWidget(self._build_top_band())
        outer.addWidget(self._build_search_band())
        outer.addWidget(self._build_banner())
        outer.addWidget(self._build_mode_stack(), 1)
        # Return and Enter open the action menu (``eventFilter``), once the
        # list it filters for too is there.
        self._actions_btn.installEventFilter(self)

    def _build_mode_stack(self) -> QWidget:
        """The views, one page each: Drafts, and whatever ``add_view``
        adds. The Imports row belongs to the drafts list, so it lives on
        its page, at the top of the list."""
        self._mode_stack = QStackedWidget()
        self._mode_stack.setObjectName("drafts_panel_mode_stack")

        drafts_mode = QWidget()
        drafts_mode_layout = QVBoxLayout(drafts_mode)
        drafts_mode_layout.setContentsMargins(0, 0, 0, 0)
        drafts_mode_layout.setSpacing(0)
        self._imports_row = _ImportsRow()
        self._imports_row.open_requested.connect(self.open_imports)
        self._imports_row.pause_requested.connect(self.pause_import)
        self._imports_row.resume_requested.connect(self.resume_import)
        drafts_mode_layout.addWidget(self._imports_row)
        drafts_mode_layout.addWidget(self._build_body(), 1)
        self._mode_stack.addWidget(drafts_mode)
        self._view_pages[self.DRAFTS_VIEW] = drafts_mode
        return self._mode_stack

    def _build_top_band(self) -> QWidget:
        """Band 1: the view's title (or the switch between views) on the
        leading edge, the panel's action menu on the trailing one. No
        close button: a Mac pane is shown and hidden from the toolbar (its
        toggle), the menu bar and a key, and Hide Drafts is in the action
        menu.

        ``_TopBand`` chooses between the title, the segments and the
        pop-up button, and takes the two buttons to a second line only
        when even the pop-up button does not fit beside them.
        """
        control_h = _control_height()
        self._title = _ElidingLabel()
        self._title.setObjectName("drafts_panel_title")
        self._title.setMinimumHeight(control_h)
        self._title.set_full_text(self._views[0][1])

        self._view_switch = _ViewSwitch(control_h)
        self._view_switch.view_chosen.connect(self.show_view)

        # Icon-only, so the accessible name is the only string that
        # announces it; the glyph alone reads as "⋯".
        # accessibility.md: "To ensure a smooth experience, label
        # interface elements appropriately."
        icons = QWidget()
        icons_layout = QHBoxLayout(icons)
        icons_layout.setContentsMargins(0, 0, 0, 0)
        icons_layout.setSpacing(6)
        # Keeps the pair on the trailing edge once the band stacks and
        # this holder spans the whole width. It costs nothing on one
        # line, where the holder is only ever as wide as its buttons.
        icons_layout.addStretch(1)
        icons_layout.addWidget(self._build_actions_button(control_h))

        self._top_band = _TopBand(self._title, self._view_switch, icons)
        return self._top_band

    def _make_icon_button(
        self, glyph: str, name: str, slot, control_h: int,
    ) -> QToolButton:
        button = QToolButton()
        button.setObjectName("drafts_panel_icon_btn")
        button.setText(glyph)
        button.setToolTip(name)
        # One string for both, so the label a screen reader reads and the
        # label a sighted user hovers cannot drift apart.
        button.setAccessibleName(name)
        button.setMinimumSize(QSize(control_h, control_h))
        if slot is not None:
            button.clicked.connect(slot)
        return button

    def refresh_name(self) -> str:
        """What Refresh is called for the view shown ("Refresh Drafts")."""
        return self._refresh_names.get(self.current_view(), _("Refresh"))

    def refresh_shown_view(self) -> None:
        """The view shown reads again: Drafts through the host, another
        view by its own ``refresh`` (the action menu, and the host's menu
        bar command)."""
        refresh = self._view_refresh.get(self.current_view())
        if refresh is None:
            self.refresh_requested.emit()
        else:
            refresh()

    def _build_actions_button(self, control_h: int) -> QToolButton:
        """The panel's action menu: a pull-down button, as Apple's
        guidelines have for a menu of actions on a view, where panel-wide
        actions go (the trailing edge of band 1), not beside the search
        field, where it read as a search option. It opens on a click,
        Space, Return or Enter.

        Refresh (the view shown reads again), Draft Relays… (also in the
        Nostr menu), in the Drafts view Select All and Delete for the
        drafts selected, and Hide Drafts. ``pull-down-buttons.md``: "listing a minimum
        of three items can help the interaction feel worthwhile".
        """
        self._actions_btn = self._make_icon_button("⋯", _("Actions"), None, control_h)
        self._actions_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self._actions_btn)
        self._actions_menu = menu
        # Bound methods, no closures: the menu then keeps nothing alive.
        menu.aboutToShow.connect(self._update_actions_menu)
        self._refresh_action = menu.addAction(self._refresh_names[self.DRAFTS_VIEW])
        self._refresh_action.triggered.connect(self.refresh_shown_view)
        self._draft_relays_action = menu.addAction(_("Draft Relays…"))
        self._draft_relays_action.setEnabled(False)       # until there is an account
        self._draft_relays_action.triggered.connect(self._ask_for_draft_relays)
        self._bulk_separator = menu.addSeparator()
        self._select_all_action = menu.addAction(_("Select All Drafts"))
        self._select_all_action.triggered.connect(self._select_all_drafts)
        self._delete_selected_action = menu.addAction(_("Delete Draft"))
        self._delete_selected_action.triggered.connect(self._delete_selected)
        menu.addSeparator()
        hide = menu.addAction(_("Hide Drafts"))
        hide.triggered.connect(self.hide_requested)
        self._actions_btn.setMenu(menu)
        return self._actions_btn

    def _update_actions_menu(self) -> None:
        """Bring the action menu up to date for the view shown and the
        drafts selected (before it opens, and when the view changes)."""
        view = self.current_view()
        self._refresh_action.setText(self.refresh_name())
        drafts = view == self.DRAFTS_VIEW
        for action in (self._bulk_separator, self._select_all_action,
                       self._delete_selected_action):
            action.setVisible(drafts)
        listed = hasattr(self, "_list") and self._list.count() > 0
        self._select_all_action.setEnabled(drafts and listed)
        selected = self.selected_identifiers() if listed else []
        self._delete_selected_action.setText(
            ngettext("Delete {n} Draft", "Delete {n} Drafts", len(selected)).format(
                n=len(selected)) if len(selected) > 1 else _("Delete Draft"))
        self._delete_selected_action.setEnabled(drafts and bool(selected))

    def _select_all_drafts(self) -> None:
        self._list.selectAll()
        self._list.setFocus()

    def _delete_selected(self) -> None:
        selected = self.selected_identifiers()
        if selected:
            self._request_delete(selected)

    def _ask_for_draft_relays(self) -> None:
        self.draft_relays_requested.emit()

    def _build_search_band(self) -> QWidget:
        """Band 2: the search field of the view shown, level with the
        document tabs beside the panel (the window sets its height,
        ``align_bands``), so the line under it meets the line under the
        tabs.

        ``search-fields.md``: "Include search at the top of the sidebar
        when filtering content or navigation there." Search is each view's
        only filter: it reads the whole text of a draft, not only its
        title, and clearing it shows everything again.

        There used to be an All / Notes / Articles chip row under it. It
        filtered on ``record.inner_kind``, which comes from the outer
        wrap's optional ``k`` tag, so every draft that had not been
        decrypted yet, and every draft from a client that omits the tag,
        was kind 0 and matched neither Notes nor Articles. The chips did
        not mis-sort those drafts, they hid them.
        """
        frame = QFrame()
        frame.setObjectName("drafts_panel_search_band")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(GUTTER, 6, GUTTER, 7)
        layout.setSpacing(0)
        self._search_band = frame

        self._search_edit = QLineEdit()
        self._search_edit.setObjectName("drafts_panel_search")
        self._search_edit.setPlaceholderText(self._search_names[self.DRAFTS_VIEW])
        self._search_edit.setAccessibleName(self._search_names[self.DRAFTS_VIEW])
        self._search_edit.setClearButtonEnabled(True)
        self._search_edit.setMinimumHeight(_control_height())
        self._search_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._search_edit.textChanged.connect(self._on_search_changed)
        layout.addWidget(self._search_edit, 1, Qt.AlignVCenter)
        return frame

    def _build_banner(self) -> QWidget:
        """What the panel has to say, over the list, only while there is
        something to know: a refresh under way, drafts that could not be
        decrypted, drafts that wait for the signer app (with Try Again).
        At rest it says nothing; the account's picture at the end of the
        tab row beside the panel names whose drafts these are.

        ``feedback.md``: "When status feedback is available near the
        items it describes, people get important information without
        having to take action or leave their current context."
        ``sidebars.md``: "Avoid putting critical information or actions at
        the bottom of a sidebar", so it sits at the top of the list.
        """
        frame = QFrame()
        frame.setObjectName("drafts_panel_banner")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(GUTTER, 6, GUTTER, 7)
        layout.setSpacing(6)
        # The sentence has the whole width, so it is said whole on the
        # narrowest panel; eliding only past that, with the whole sentence
        # as its tooltip, so it never widens the panel.
        self._status_label = _ElidingLabel()
        self._status_label.setObjectName("drafts_panel_status")
        self._status_label.setFont(_secondary_font())
        self._status_label.setProperty("error", "false")
        layout.addWidget(self._status_label)
        layout.addWidget(self._build_locked_line())
        self._banner = frame
        frame.hide()
        return frame

    def _build_locked_line(self) -> QWidget:
        """Drafts that wait for the signer app, said once and quietly: a
        small lock, the sentence in secondary text, and Try Again as a
        link under it. Not an error, so not red: nothing is wrong with
        the drafts, and the remedy is the person opening an app."""
        line = QWidget()
        row = QHBoxLayout(line)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        # Decoration, so a screen reader passes over it; the sentence says it.
        self._locked_symbol = QLabel()
        self._locked_symbol.setAccessibleName("")
        self._locked_symbol.setAlignment(Qt.AlignCenter)
        row.addWidget(self._locked_symbol, 0, Qt.AlignTop)
        words = QVBoxLayout()
        words.setContentsMargins(0, 0, 0, 0)
        words.setSpacing(1)
        # Wrapped, not elided: said whole however narrow the panel.
        self._locked_label = QLabel()
        self._locked_label.setObjectName("drafts_panel_locked")
        self._locked_label.setFont(_secondary_font())
        self._locked_label.setWordWrap(True)
        words.addWidget(self._locked_label)
        # Its action under it, leading, as an inline message has it.
        self._banner_action = QPushButton(_("Try Again"))
        self._banner_action.setObjectName("drafts_panel_banner_action")
        self._banner_action.setFont(_secondary_font())
        self._banner_action.setFlat(True)
        self._banner_action.setCursor(Qt.PointingHandCursor)
        self._banner_action.clicked.connect(self.retry_signer.emit)
        words.addWidget(self._banner_action, 0, Qt.AlignLeft)
        row.addLayout(words, 1)
        self._locked_line = line
        line.hide()
        return line

    def _paint_locked_symbol(self) -> None:
        """The lock, in the line's own grey, as tall as a line of it."""
        metrics = QFontMetrics(_secondary_font())
        size = max(10, round(metrics.height() * 0.8))
        self._locked_symbol.setFixedSize(size, metrics.height())
        self._locked_symbol.setPixmap(symbol_pixmap(
            "lock", size, QColor(_THEME_TOKENS[bool(self._is_dark)]["muted"]),
            self.devicePixelRatioF() or 2.0))

    # -- the window's grid ----------------------------------------------------

    def align_bands(self, toolbar_height: int, search_height: int) -> None:
        """Line the panel's two bands up with the window beside it: band 1
        as tall as the format toolbar (``toolbar_height``), band 2 ending
        on the line under the document tabs (``search_height``). 0 gives a
        band its own height (no toolbar shown, or the find bar between the
        toolbar and the tabs). A band whose own content is taller (very
        large text) keeps its height rather than cutting its controls."""
        self._top_band.setMinimumHeight(max(0, int(toolbar_height)))
        if search_height > 0:
            self._search_band.setFixedHeight(int(search_height))
        else:
            self._search_band.setMinimumHeight(0)
            self._search_band.setMaximumHeight(QWIDGETSIZE_MAX)

    def _show_search_of(self, key: str) -> None:
        """The search field as the view ``key`` has it: its name, what was
        typed into it there, or no field for a view that has no search."""
        searchable = key == self.DRAFTS_VIEW or key in self._view_search
        self._search_edit.setVisible(searchable)
        if not searchable:
            return
        name = self._search_names.get(key, _("Search"))
        self._search_edit.setPlaceholderText(name)
        self._search_edit.setAccessibleName(name)
        wanted = self._view_search_text.get(key, "")
        if self._search_edit.text() != wanted:
            self._search_edit.setText(wanted)

    def _build_body(self) -> QWidget:
        # Stacks the actual list and an empty-state widget on the same
        # area; ``_refresh_empty_state`` flips which is visible.
        container = QWidget()
        self._body_stack = QStackedLayout(container)
        self._body_stack.setContentsMargins(0, 0, 0, 0)

        self._list = _DraftList()
        self._list.setObjectName("drafts_panel_list")
        self._list.setAccessibleName(_("Drafts"))
        self._list.setSpacing(0)
        # A section's first row is taller by the section's title.
        self._list.setUniformItemSizes(False)
        self._list.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._row_delegate = PostRowDelegate(self._list, is_dark=self._is_dark)
        self._list.setItemDelegate(self._row_delegate)
        # Extended, so a shift-drag or a cmd-click selects a range the way
        # it does in every list on this platform, and Select All means
        # what it says. The preview and the open/publish commands still
        # follow the current row, because those act on one draft and
        # there is exactly one current row whatever else is selected.
        self._list.setSelectionMode(QListWidget.ExtendedSelection)
        self._list.itemActivated.connect(self._on_item_activated)
        self._list.setContextMenuPolicy(Qt.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._on_context_menu)
        # Delete / Backspace on the list, see ``eventFilter``.
        self._list.installEventFilter(self)
        self._body_stack.addWidget(self._list)

        # Empty / disconnected / unsupported / no-results state.
        self._empty_widget = QWidget()
        empty_layout = QVBoxLayout(self._empty_widget)
        empty_layout.setContentsMargins(GUTTER, 24, GUTTER, 24)
        empty_layout.setSpacing(8)
        empty_layout.addStretch(1)
        # A quiet symbol over the title, as Apple's empty states have one;
        # decoration, so a screen reader passes over it.
        self._empty_symbol = QLabel()
        self._empty_symbol.setAlignment(Qt.AlignCenter)
        self._empty_symbol.setAccessibleName("")
        self._empty_symbol_name = ""
        empty_layout.addWidget(self._empty_symbol)
        self._empty_title = QLabel("")
        self._empty_title.setObjectName("drafts_panel_empty_title")
        self._empty_title.setAlignment(Qt.AlignCenter)
        self._empty_title.setWordWrap(True)
        empty_layout.addWidget(self._empty_title)
        self._empty_body = QLabel("")
        self._empty_body.setObjectName("drafts_panel_empty_body")
        self._empty_body.setFont(_secondary_font())
        self._empty_body.setAlignment(Qt.AlignCenter)
        self._empty_body.setWordWrap(True)
        empty_layout.addWidget(self._empty_body)
        # A branch gets a button when it has a remedy to offer. writing.md:
        # "guide people on actions they can take, and give them a button
        # or link to do so if possible." The label and the handler both
        # belong to the branch, so the button dispatches through
        # ``_empty_action_handler`` rather than being wired to one of them
        # for the life of the panel. It joins the panel only while a branch
        # offers one, so no unnamed button waits hidden for screen readers.
        self._empty_action = QPushButton()
        self._empty_action.setObjectName("drafts_panel_empty_action")
        self._empty_action.setMinimumHeight(_control_height())
        self._empty_action_handler: Optional[Callable[[], None]] = None
        self._empty_action.clicked.connect(self._on_empty_action)
        self._empty_action.hide()
        self._empty_layout = empty_layout
        empty_layout.addStretch(2)
        self._body_stack.addWidget(self._empty_widget)
        return container

    # -- public API: views --------------------------------------------------

    def add_view(self, key: str, title: str, hint: str, widget: QWidget, *,
                 refresh: Optional[Callable[[], None]] = None,
                 refresh_name: str = "",
                 search: Optional[Callable[[str], None]] = None,
                 search_name: str = "") -> None:
        """Add a view beside Drafts (another page of the panel). From the
        second view on, band 1 offers the switch between them.

        ``refresh`` is how the view reads again when Refresh in the action
        menu is chosen while it is shown, and ``refresh_name`` what that
        command is called then (title case, as menus write it).
        ``search`` filters the view by what is typed in the search field
        while it is shown, and ``search_name`` names the field then; a
        view without one leaves the field out."""
        if key in self._view_pages:
            raise ValueError(f"the panel already has a view {key!r}")
        self._views.append((key, title, hint))
        self._view_pages[key] = widget
        if refresh is not None:
            self._view_refresh[key] = refresh
        if refresh_name:
            self._refresh_names[key] = refresh_name
        if search is not None:
            self._view_search[key] = search
            self._search_names[key] = search_name or _("Search")
        self._mode_stack.addWidget(widget)
        self._view_switch.set_views(self._views, self.current_view())
        self._make_room_for_view_names()
        self._top_band.reflow()

    def views(self) -> List[str]:
        return [key for key, _title, _hint in self._views]

    def current_view(self) -> str:
        page = self._mode_stack.currentWidget()
        return next((key for key, widget in self._view_pages.items() if widget is page),
                    self.DRAFTS_VIEW)

    def show_view(self, key: str) -> None:
        page = self._view_pages.get(key)
        if page is None or key == self.current_view():
            self._view_switch.set_current(self.current_view())
            return
        self._mode_stack.setCurrentWidget(page)
        self._view_switch.set_current(key)
        self._show_search_of(key)
        self._render_status()
        self._update_actions_menu()
        self._top_band.reflow()
        # Another view is other data and other rows, so the drafts
        # preview has nothing to describe there.
        self._preview.close(disarm=True)
        self.view_changed.emit(key)

    # -- public API: the Imports row ------------------------------------------

    def set_imports_state(self, *, available: bool, new_posts: int = 0,
                          activity: str = "", action: str = "") -> None:
        """What the Imports row says: shown while imports are ``available``
        (an account is in use), with ``new_posts`` waiting, an import's
        ``activity`` line, and its ``action`` ("pause", "resume", "retry"
        or "")."""
        self._imports_row.show_state(available=available, new_posts=new_posts,
                                     activity=activity, action=action)

    # -- public API: theming ----------------------------------------------

    def apply_theme(self, is_dark: bool) -> None:
        self._is_dark = is_dark
        appearance.bind(self)
        self.setStyleSheet(_panel_css(is_dark))
        appearance.surface(self, is_dark)
        self._view_switch.set_dark(is_dark)
        self._actions_menu.setStyleSheet(_menu_css(is_dark))
        self._apply_placeholder_colour(is_dark)
        # Re-styled in place rather than closed: a theme switch is not a
        # reason to take a surface away from someone reading it.
        self._preview.apply_theme(is_dark)
        if hasattr(self, "_imports_row"):
            self._imports_row.update()
        if hasattr(self, "_empty_symbol"):
            self._paint_empty_symbol()
        if hasattr(self, "_locked_symbol"):
            self._paint_locked_symbol()
        if getattr(self, "_row_delegate", None) is not None:
            self._row_delegate.set_dark(is_dark)

    def _apply_placeholder_colour(self, is_dark: bool) -> None:
        """Hold the search placeholder above the 4.5:1 minimum.

        Qt derives ``QPalette::PlaceholderText`` from the stylesheet's
        ``color`` at half alpha, so the field's own text token
        composites to 3.83:1 dark and 2.85:1 light against the field
        fill. ``accessibility.md``: "Text size / Text weight / Minimum
        contrast ratio / Up to 17 pts / All / 4.5:1", and "If your app
        supports Dark Mode, make sure to check the minimum contrast in
        both light and dark appearances." The placeholder is the only
        visible label the search field has, and search is the panel's
        only filter, so it is held at the muted token at full alpha
        instead: 5.15:1 dark, 5.17:1 light.

        Re-applied after every ``setStyleSheet`` because polishing is
        what installs the derived colour, so a theme switch would
        otherwise put the half-alpha value straight back.
        """
        edit = getattr(self, "_search_edit", None)
        if edit is None:
            return
        edit.ensurePolished()
        palette = edit.palette()
        palette.setColor(
            QPalette.PlaceholderText,
            QColor(_THEME_TOKENS[bool(is_dark)]["muted"]),
        )
        edit.setPalette(palette)

    # -- public API: data binding -----------------------------------------

    def bind_store(self, store: DraftStore) -> None:
        if self._store is store:
            return
        if self._store is not None:
            # Disconnect previous bindings, Qt allows this idiom by
            # disconnecting the exact slot-callable pair.
            try:
                self._store.record_added.disconnect(self._on_record_added)
                self._store.record_changed.disconnect(self._on_record_changed)
                self._store.record_removed.disconnect(self._on_record_removed)
                self._store.cleared.disconnect(self._on_store_cleared)
                self._store.loading_state_changed.disconnect(self._on_loading_changed)
            except (TypeError, RuntimeError):
                pass
        self._store = store
        if store is not None:
            store.record_added.connect(self._on_record_added)
            store.record_changed.connect(self._on_record_changed)
            store.record_removed.connect(self._on_record_removed)
            store.cleared.connect(self._on_store_cleared)
            store.loading_state_changed.connect(self._on_loading_changed)
        self._rebuild_list()

    def set_active_profile(self, profile: Optional[Profile]) -> None:
        self._active_profile = profile
        # Where drafts are kept is the account's: nothing to set without one.
        self._draft_relays_action.setEnabled(profile is not None)
        self._refresh_empty_state()
        self._render_status()

    def set_status(self, text: str) -> None:
        """Narrate one sync step. Never writes the label directly.

        ``set_status`` and the store-progress refresh used to write the
        same label, and the refresh ran on every ``record_added``, so a
        sync message was overwritten within milliseconds of being set.
        Everything now mutates state and renders through one writer.
        """
        self._sync_message = text or ""
        self._restart_status_ttl()
        self._render_status()

    def set_signer_unsupported(self, unsupported: bool) -> None:
        self._signer_unsupported = unsupported
        self._render_status()
        self._refresh_empty_state()

    def set_signer_unreachable(self, unreachable: bool) -> None:
        """Drafts wait for the signer app, or no longer do. While they
        wait, the ones not open yet are one quiet line over the list
        (``_locked_drafts``), not a row each; the ones already open stay."""
        if unreachable == self._signer_unreachable:
            return
        self._signer_unreachable = unreachable
        if unreachable:
            # A narration line left from before would otherwise come back
            # the moment the drafts no longer wait.
            self._sync_message = ""
        # Only the drafts not open yet leave the list or come back to it:
        # the rows someone may be reading keep their place and selection.
        for record in list(self._store or []):
            if record.state is DraftState.LOADING:
                self._on_record_changed(record.identifier)
        self._refresh_empty_state()
        self._render_status()

    def _locked_drafts(self) -> int:
        """How many drafts wait for the signer app: those not open yet
        while it does not answer (none while it does)."""
        if not self._signer_unreachable or self._signer_unsupported:
            return 0
        return sum(1 for record in self._store or []
                   if record.state is DraftState.LOADING)

    # -- public API: the hover preview ------------------------------------

    def set_preview_image_loader(self, loader) -> None:
        """Hand the preview the app's one ``ThumbnailLoader``, or ``None``.

        ``main_window`` injects the loader it already owns, so there is
        one cache, one URL policy and one place where an image request
        can be made. The default is ``None``, and with ``None`` the
        preview renders with no hero image at all and constructs no
        request, which is what keeps a ``DraftsPanel()`` built in a test
        free of a network by construction rather than by discipline.
        """
        self._preview.set_image_loader(loader)

    def set_preview_pictures(self, lookup, arrived=None) -> None:
        """Where the full preview finds the pictures in a draft's text, and
        the signal naming one that arrived (PreviewController.set_pictures);
        None, the default, shows frames and fetches nothing."""
        self._preview.set_pictures(lookup, arrived)

    # -- preview host: the four questions the controller asks --------------

    def preview_record(self, identifier: str) -> Optional[DraftRecord]:
        """The record behind one row, or ``None`` if it has gone."""
        if self._store is None or not identifier:
            return None
        return self._store.get(identifier)

    def preview_current_row(self) -> Tuple[str, QRect]:
        """The focused row's identifier and its rect in global coordinates.

        The rect rather than the widget, because ``_rebuild_list``
        destroys every row widget and the controller must never hold one
        past the call it was handed in.
        """
        return self._preview_anchor(self._list.currentItem())

    def preview_row_at(self, pos) -> Tuple[str, QRect]:
        """The draft under ``pos`` (in the list's viewport) and its row's
        rect in global coordinates; nothing over a section's title, which
        is no draft's."""
        item = self._list.itemAt(pos)
        identifier, anchor = self._preview_anchor(item)
        if not identifier:
            return "", QRect()
        local = self._list.visualItemRect(item)
        row = item.data(ROW_ROLE)
        if isinstance(row, PostRow) and row.section and pos.y() < local.top() + section_height():
            return "", QRect()
        return identifier, anchor

    def _preview_anchor(self, item: Optional[QListWidgetItem]) -> Tuple[str, QRect]:
        if item is None:
            return "", QRect()
        identifier = item.data(Qt.UserRole)
        if not isinstance(identifier, str) or not identifier:
            return "", QRect()
        rect = self._list.visualItemRect(item)
        row = item.data(ROW_ROLE)
        if isinstance(row, PostRow) and row.section:
            # The row itself, under its section's title.
            rect = rect.adjusted(0, section_height(), 0, 0)
        return identifier, QRect(
            self._list.viewport().mapToGlobal(rect.topLeft()), rect.size(),
        )

    def _show_preview_for(self, item: QListWidgetItem) -> None:
        """Open the full preview for the row a context menu was raised on.

        Resolved from the item rather than from the current row: a
        right-click does not move the selection, so the two are often
        different rows and the menu has to describe the one under the
        pointer.
        """
        identifier, anchor = self._preview_anchor(item)
        if identifier:
            self._preview.open_for(identifier, anchor, full=True)

    def preview_anchor_for(self, identifier: str) -> QRect:
        """Where a draft's row is now (global), or an empty rect when it is
        not in sight: a full preview moves with its row as the list
        scrolls."""
        item = self._items.get(identifier)
        if item is None:
            return QRect()
        local = self._list.visualItemRect(item)
        if not self._list.viewport().rect().intersects(local):
            return QRect()
        return self._preview_anchor(item)[1]

    def preview_actions(self, identifier: str) -> List[PreviewAction]:
        """The full preview's commands for a draft: Continue Editing (the
        next step, also Return and a double click), Publish…, and in its ⋯
        menu Copy Event ID and Delete Draft."""
        record = self.preview_record(identifier)
        actions = [PreviewAction("publish", _("Publish…")),
                   PreviewAction("open", _("Continue Editing"), primary=True)]
        if record is not None and record.event_id:
            actions.append(PreviewAction("copy_id", _("Copy Event ID"), more=True))
        actions.append(PreviewAction("delete", _("Delete Draft"), more=True, destructive=True))
        return actions

    def preview_act(self, identifier: str, key: str) -> None:
        """Do what the full preview's bar asked for one draft."""
        record = self.preview_record(identifier)
        if record is None:
            return
        if key == "open":
            self.open_draft.emit(identifier)
        elif key == "publish":
            self.publish_draft.emit(identifier)
        elif key == "copy_id":
            self._copy_event_id(record)
        elif key == "delete":
            self._request_delete([identifier])

    def preview_is_available(self) -> bool:
        """Whether a preview may open at all right now.

        A floating window over another application's window, describing
        a draft in a window the user is not looking at, is a bug, so the
        panel has to be on screen, in drafts mode, and inside the active
        window.
        """
        if not self.isVisible() or self.current_view() != self.DRAFTS_VIEW:
            return False
        window = self.window()
        return window is not None and window.isActiveWindow()

    def preview_now(self) -> int:
        """The clock the preview reads, in one place so tests can move it."""
        return int(time.time())

    # -- the banner: one writer ---------------------------------------------

    def _render_status(self) -> None:
        """The single writer for the banner."""
        text, is_error = self._status_line()
        # Drafts waiting for the signer app get a line of their own (the
        # status line's sentence then): wrapped rather than elided, with
        # the lock and Try Again.
        locked = self._locked_drafts() > 0
        self._locked_label.setText(text if locked else "")
        self._locked_line.setVisible(locked)
        text = "" if locked else text
        self._status_label.set_full_text(text)
        self._status_label.setToolTip(text)
        self._status_label.setVisible(bool(text))
        # Only the drafts view speaks here, and only with something to say:
        # an empty banner would be a strip of chrome over the list.
        self._banner.setVisible((bool(text) or locked)
                                and self.current_view() == self.DRAFTS_VIEW)
        self._status_label.setProperty("error", "true" if is_error else "false")
        self._status_label.style().unpolish(self._status_label)
        self._status_label.style().polish(self._status_label)

    def _status_line(self) -> Tuple[str, bool]:
        """``(text, is_error)`` in priority order, first match wins; at
        rest ``("", False)``.

        At rest it used to name the account ("Drafts for Alice"), a line
        of chrome that said the same thing all day; the account's picture
        beside the panel says it. Before that it read "{ready}/{total}
        decrypted", which said "54/54" for the whole time the user was
        actually reading the list.
        """
        if self._signer_unsupported:
            return _("Your signer app can’t decrypt drafts"), True
        # Outranks both the failure count and the decrypting count: it is
        # the one thing standing between the person and those drafts, and
        # its remedy is somewhere other than this app. Said as a state, not
        # a problem: a signer app on a phone that is asleep is ordinary.
        locked = self._locked_drafts()
        if locked:
            text = ngettext(
                "{n} draft is locked until your signer app answers.",
                "{n} drafts are locked until your signer app answers.",
                locked,
            )
            return text.format(n=locked), False

        failed = ready = total = 0
        for record in self._store or []:
            total += 1
            if record.state is DraftState.FAILED:
                failed += 1
            elif record.state is DraftState.READY:
                ready += 1

        if failed:
            text = ngettext(
                "{n} draft could not be decrypted",
                "{n} drafts could not be decrypted",
                failed,
            )
            return text.format(n=failed), True
        if self._loading or self._sync_message:
            return self._sync_message or _("Refreshing drafts"), False
        if total and ready < total:
            text = _("Decrypting {ready} of {total}")
            return text.format(ready=ready, total=total), False
        return "", False

    def _restart_status_ttl(self) -> None:
        self._status_timer.stop()
        # While a refresh is in flight the loading branch holds the line
        # on its own. Once it is not, a narration line is a report of a
        # finished event, so it expires and the banner goes away.
        if self._sync_message and not self._loading:
            self._status_timer.start(_SYNC_MESSAGE_TTL_MS)

    def _on_status_expired(self) -> None:
        self._sync_message = ""
        self._render_status()

    # -- empty states ------------------------------------------------------

    def _refresh_empty_state(self) -> None:
        """Pick the body card: the list, or one of four placeholders.

        Branches on the number of *visible* rows, not on the store size.
        Filtering to zero results used to leave the stack on the list, so
        a search that matched nothing showed a blank rectangle. With the
        kind chips gone and search the only filter, that is the panel's
        most likely dead end.
        """
        if self._active_profile is None:
            # The host closes the panel when no account is in use; should it
            # show anyway, it says where an account comes from.
            self._show_placeholder(
                _("No Nostr account in use"),
                _("Create an account, connect your signer app or restore an "
                  "account from its backup, all in the Nostr menu."),
            )
            return
        if self._signer_unsupported:
            self._show_placeholder(
                _("Your signer app can’t decrypt drafts"),
                _("Drafts are encrypted in a way this signer app can’t open. To use "
                  "drafts, connect a signer app that can, such as Amber or nsec.app."),
                symbol="alert",
            )
            return
        if self._store is None or len(self._store) == 0:
            if self._loading:
                # A first refresh in flight is not an empty library.
                # loading.md: "Show something as soon as possible. If you
                # make people wait for loading to complete before
                # displaying anything, they can interpret the lack of
                # content as a problem with your app." Rows arrive into
                # the list and the banner narrates the wait.
                self._body_stack.setCurrentIndex(0)
                return
            body = _("Press {shortcut} in any tab to save it as an encrypted draft. "
                     "Your drafts appear here, on every computer you use with "
                     "this account.")
            self._show_placeholder(
                _("No Drafts"),
                body.format(shortcut=_save_shortcut_text()),
                symbol="pages",
            )
            return
        # Drafts that all wait for the signer app leave the list empty
        # without a search: the line over it says it, once.
        if self._list.count() == 0 and self._search_text:
            # Apple's own words for a search that found nothing.
            self._show_placeholder(
                _("No Results for “{query}”").format(query=self._search_text.strip()),
                _("Check the spelling or try a new search."),
                action=_("Clear Search"),
                on_action=self._search_edit.clear,
                symbol="search",
            )
            return
        self._body_stack.setCurrentIndex(0)

    def _show_placeholder(
        self,
        title: str,
        body: str,
        *,
        action: str = "",
        on_action: Optional[Callable[[], None]] = None,
        symbol: str = "",
    ) -> None:
        self._empty_symbol_name = symbol
        self._paint_empty_symbol()
        self._empty_title.setText(title)
        self._empty_body.setText(body)
        self._empty_action_handler = on_action if action else None
        button = self._empty_action
        if action:
            button.setText(action)
            if button.parentWidget() is None:
                # After the symbol, the title and the body, before the
                # space below.
                self._empty_layout.insertWidget(4, button, 0, Qt.AlignHCenter)
            button.show()
        elif button.parentWidget() is not None:
            self._empty_layout.removeWidget(button)
            button.hide()
            button.setParent(None)
        self._body_stack.setCurrentIndex(1)

    def _paint_empty_symbol(self) -> None:
        name = self._empty_symbol_name
        self._empty_symbol.setVisible(bool(name))
        if name:
            self._empty_symbol.setPixmap(symbol_pixmap(
                name, EMPTY_SYMBOL_SIZE, QColor(_THEME_TOKENS[bool(self._is_dark)]["muted"]),
                self.devicePixelRatioF() or 2.0))

    def _on_empty_action(self) -> None:
        handler = self._empty_action_handler
        if handler is not None:
            handler()

    # -- list rebuild + filter --------------------------------------------

    def _clear_list(self) -> None:
        # Block signals across the clear: currentItemChanged fires while
        # the items are being destroyed, and the handler would reach into
        # a row widget that no longer exists.
        blocked = self._list.blockSignals(True)
        self._list.clear()
        self._list.blockSignals(blocked)
        self._items.clear()

    def _rebuild_list(self) -> None:
        self._clear_list()
        if self._store is None:
            self._refresh_empty_state()
            self._render_status()
            return
        for record in self._store.all():
            if self._passes_filter(record):
                self._insert_row(record)
        self._refresh_sections()
        self._ensure_current_row()
        self._refresh_empty_state()
        self._render_status()

    def _ensure_current_row(self) -> None:
        """Give the list a current row so arrow keys work on arrival.

        Without it, tabbing into a freshly populated list leaves
        ``currentRow() == -1``: the arrow keys have no anchor and the
        Menu key resolves no row at all.
        """
        if self._list.count() and self._list.currentRow() < 0:
            self._list.setCurrentRow(0)

    def _passes_filter(self, record: DraftRecord) -> bool:
        """Search is the only filter, so an empty field shows everything.

        This is the invariant the kind chips broke: no draft may be
        unreachable, whatever its ``inner_kind`` or decryption state. The one
        exception is drafts waiting for the signer app: none of them can be
        opened yet, and the line over the list stands for all of them
        (``_locked_drafts``).
        """
        if self._signer_unreachable and record.state is DraftState.LOADING:
            return False
        if not self._search_text:
            return True
        needle = self._search_text.lower()
        # ``content`` is the full decrypted body and is already in
        # memory. Searching only the title and the 140-character snippet
        # made a field labelled "Search drafts…" miss words that are
        # plainly in the draft. searching.md: "Clearly display the
        # current scope of a search."
        haystack = f"{record.title} {record.snippet} {record.content}".lower()
        return needle in haystack

    def _insert_row(self, record: DraftRecord, *, at_index: Optional[int] = None) -> None:
        item = QListWidgetItem()
        item.setData(Qt.UserRole, record.identifier)
        self._decorate_item(item, record)
        if at_index is None:
            self._list.addItem(item)
        else:
            self._list.insertItem(at_index, item)
        self._items[record.identifier] = item

    def _decorate_item(self, item: QListWidgetItem, record: DraftRecord) -> None:
        """What one row draws and what a screen reader says for it.

        Called from both ``_insert_row`` and ``_update_row`` so what a
        screen reader hears cannot drift from what the row shows. The row
        keeps the section title it has (``_refresh_sections`` owns that);
        the tooltip is the painter's, shown only where a title is cut.
        """
        old = item.data(ROW_ROLE)
        row = _draft_row(record, now=self.preview_now())
        if isinstance(old, PostRow) and old.section:
            row = row._replace(section=old.section)
        item.setData(ROW_ROLE, row)
        item.setData(Qt.AccessibleTextRole, _accessible_row_text(record))
        item.setData(TITLE_ROLE, (record.title or record.snippet or "").strip())

    def _refresh_sections(self) -> None:
        """Give each row that starts a date section that section's title
        (Today, Yesterday, Previous 7 Days...), and take it from the
        others. Run after the rows change, and as the panel is shown, so
        a day that turned over moves its drafts along."""
        now = self.preview_now()
        items = [self._list.item(row) for row in range(self._list.count())]
        times = []
        for item in items:
            record = self.preview_record(item.data(Qt.UserRole))
            times.append(record.created_at if record is not None else 0)
        for item, section in zip(items, section_starts(times, now)):
            row = item.data(ROW_ROLE)
            if isinstance(row, PostRow) and row.section != section:
                item.setData(ROW_ROLE, row._replace(section=section))

    def _remove_row(self, identifier: str) -> None:
        item = self._items.pop(identifier, None)
        if item is None:
            return
        row = self._list.row(item)
        if row >= 0:
            self._list.takeItem(row)
        self._refresh_sections()

    def _update_row(self, identifier: str) -> None:
        item = self._items.get(identifier)
        if item is None or self._store is None:
            return
        record = self._store.get(identifier)
        if record is None:
            return
        self._decorate_item(item, record)
        self._refresh_sections()

    # -- store-signal handlers --------------------------------------------

    def _on_record_added(self, identifier: str) -> None:
        if self._store is None:
            return
        record = self._store.get(identifier)
        if record is None or not self._passes_filter(record):
            # Not a row (outside a search, or waiting for the signer app),
            # but counted in the line over the list all the same.
            self._refresh_empty_state()
            self._render_status()
            return
        # Insert in the natural store order (newest-first). Easiest is
        # to re-look-up the position from store.all(), N is small.
        ordered = self._store.all()
        idx = next((i for i, r in enumerate(ordered) if r.identifier == identifier), -1)
        if idx < 0:
            return
        # Walk the visible list, count rows whose underlying record is
        # newer-or-equal in store order, and insert there.
        visible_idx = 0
        for i, r in enumerate(ordered):
            if r.identifier == identifier:
                break
            if r.identifier in self._items:
                visible_idx += 1
        self._insert_row(record, at_index=visible_idx)
        self._refresh_sections()
        self._ensure_current_row()
        self._refresh_empty_state()
        self._render_status()

    def _on_record_changed(self, identifier: str) -> None:
        # The change may have made the row newly-pass or newly-fail the
        # current filter, recompute and adjust if needed.
        if self._store is None:
            return
        record = self._store.get(identifier)
        if record is None:
            return
        present = identifier in self._items
        passes = self._passes_filter(record)
        if present and not passes:
            self._preview.on_record_removed(identifier)
            self._remove_row(identifier)
            self._refresh_empty_state()
        elif not present and passes:
            self._on_record_added(identifier)  # treat as fresh insertion
        else:
            self._update_row(identifier)
            # A row that has just finished decrypting under the pointer
            # becomes previewable without the user moving off and back.
            self._preview.on_record_changed(identifier)
        self._render_status()

    def _on_record_removed(self, identifier: str) -> None:
        self._preview.on_record_removed(identifier)
        self._remove_row(identifier)
        self._refresh_empty_state()
        self._render_status()

    def _on_store_cleared(self) -> None:
        # A profile switch, so every cached digest and every remembered
        # broken image belonged to someone else's library.
        self._preview.on_store_cleared()
        self._clear_list()
        self._refresh_empty_state()
        self._render_status()

    def _on_loading_changed(self, loading: bool) -> None:
        self._loading = bool(loading)
        # A message set just before the flag drops (the "Loaded N drafts"
        # line) still gets its time on screen; one set during a refresh
        # is held by the loading branch instead.
        if self._loading:
            self._status_timer.stop()
        else:
            self._restart_status_ttl()
        self._render_status()
        self._refresh_empty_state()

    # -- search handler ----------------------------------------------------

    def _on_search_changed(self, text: str) -> None:
        view = self.current_view()
        self._view_search_text[view] = text
        if view != self.DRAFTS_VIEW:
            search = self._view_search.get(view)
            if search is not None:
                search(text)
            return
        # Closed and disarmed before the rebuild, not after: the rebuild
        # destroys every row widget, and typing must never leave a
        # preview anchored to one of them or open one for whichever row
        # slid under a stationary pointer.
        self._preview.on_search_changed()
        self._search_text = text
        self._rebuild_list()

    # -- list interaction --------------------------------------------------

    def _on_item_activated(self, item: QListWidgetItem) -> None:
        """Double-click / Enter on a row.

        Behaviour depends on the row's state:
          - READY   → open the decrypted draft in a new tab.
          - FAILED  → retry decryption (most common case: the signer
                      timed out and the user wants to approve now).
          - LOADING → no-op; the row is mid-decryption.
        """
        identifier = item.data(Qt.UserRole)
        if not isinstance(identifier, str) or not identifier:
            return
        record = self._store.get(identifier) if self._store is not None else None
        if record is None:
            return
        # The draft itself opens; its preview makes way.
        self._preview.close()
        if record.state is DraftState.READY:
            self.open_draft.emit(identifier)
        elif record.state is DraftState.FAILED:
            self.retry_decrypt.emit(identifier)
        # LOADING, intentionally no-op

    def _item_for_context(self, pos) -> Optional[QListWidgetItem]:
        """The row a context-menu request is about.

        Falling back to the current item is what makes the Menu key
        work: it delivers a position inside the viewport that may hit no
        row at all, and resolving by position alone meant the key did
        nothing whenever it missed.
        """
        return self._list.itemAt(pos) or self._list.currentItem()

    def selected_identifiers(self) -> List[str]:
        """Every selected draft, in the order the list shows them.

        List order rather than click order, because the count in the
        confirmation and the order things are deleted in should match
        what the user is looking at.
        """
        if self._store is None:
            return []
        out: List[str] = []
        for row in range(self._list.count()):
            item = self._list.item(row)
            if item is None or not item.isSelected():
                continue
            identifier = item.data(Qt.UserRole)
            if isinstance(identifier, str) and identifier:
                out.append(identifier)
        return out

    def _request_delete(self, identifiers: List[str]) -> None:
        if identifiers:
            # Closed first: the confirmation is about to open over this
            # panel, and a popover must not outlive the row it describes.
            self._preview.close(disarm=True)
            self.delete_drafts.emit(identifiers)

    def eventFilter(self, obj, event) -> bool:
        """Delete removes the selected drafts.

        Filtered on the list rather than handled on the panel, because
        the key belongs to the list and only the list. Reaching it here
        means the list had focus, which is the condition that matters and
        the one a ``hasFocus`` check in the panel could only approximate:
        the search field is in this panel too, and Delete there is an
        edit, not a deletion.

        Backspace as well. On this platform it is the key people actually
        reach for in a list, and having only one of the two work reads as
        the panel being half-wired.
        """
        if (
            obj is self._actions_btn
            and event.type() == QEvent.KeyPress
            and event.key() in (Qt.Key_Return, Qt.Key_Enter)
        ):
            # Space opens it by itself; on Windows and Linux people press
            # Enter on a focused button.
            self._actions_btn.showMenu()
            return True
        if (
            obj is self._list
            and event.type() == QEvent.KeyPress
            and event.key() in (Qt.Key_Delete, Qt.Key_Backspace)
        ):
            selected = self.selected_identifiers()
            if selected:
                self._request_delete(selected)
                return True
        return super().eventFilter(obj, event)

    def _on_context_menu(self, pos) -> None:
        # First statement, before anything is built. ``popovers.md``:
        # "Don't show another view over a popover. Make sure nothing
        # displays on top of a popover, except for an alert."
        self._preview.close(disarm=True)
        menu = self._build_context_menu(self._item_for_context(pos))
        if menu is not None:
            menu.exec(self._list.mapToGlobal(pos))
            # One menu per click: kept, each one stayed on the list.
            menu.deleteLater()

    def _build_context_menu(self, item: Optional[QListWidgetItem]) -> Optional[QMenu]:
        """The row's menu, built but not shown.

        Split from ``_on_context_menu`` so the commands and their enabled
        states can be asserted without a modal event loop, which is the
        only way to test them at all.
        """
        if item is None:
            return None
        identifier = item.data(Qt.UserRole)
        if not isinstance(identifier, str) or self._store is None:
            return None
        record = self._store.get(identifier)
        if record is None:
            return None
        menu = QMenu(self._list)
        menu.setStyleSheet(_menu_css(self._is_dark))

        # menus.md: "To be consistent with platform experiences, use
        # title-style capitalization." The ellipsis stays on the one
        # command that opens a dialog.

        # First, because it is how the Space key is discovered at all.
        # Hover is not allowed to be the only route to the preview:
        # an audit of this panel already flagged hover-only affordances,
        # and the Menu key reaches this menu without a pointer.
        # Each command is carried on its action and run by one slot of
        # this panel (``_on_row_command``): a closure over the panel on
        # one of its children keeps the panel alive.
        menu.triggered.connect(self._on_row_command)
        act_preview = QAction(_("Show Preview"), menu)
        act_preview.setShortcut(QKeySequence(Qt.Key_Space))
        act_preview.setData(("preview", identifier))
        act_preview.setEnabled(
            preview_is_eligible(record, now=self.preview_now())
        )
        menu.addAction(act_preview)
        menu.addSeparator()

        if record.state is DraftState.FAILED:
            # For a failed row the *only* useful primary action is to
            # retry decryption. Promote it to the top of the menu so
            # right-click → Enter is the recovery path.
            act_retry = QAction(_("Retry Decryption"), menu)
            act_retry.setData(("retry", identifier))
            act_retry.setEnabled(bool(record.ciphertext))
            menu.addAction(act_retry)
            menu.addSeparator()

        # What Return and a double click do, and the preview's next step.
        act_open = QAction(_("Continue Editing"), menu)
        act_open.setShortcut(QKeySequence(Qt.Key_Return))
        act_open.setData(("open", identifier))
        act_open.setEnabled(record.state is DraftState.READY)
        menu.addAction(act_open)

        act_publish = QAction(_("Publish…"), menu)
        act_publish.setData(("publish", identifier))
        act_publish.setEnabled(record.state is DraftState.READY)
        menu.addAction(act_publish)

        menu.addSeparator()
        act_copy_id = QAction(_("Copy Event ID"), menu)
        act_copy_id.setData(("copy_id", identifier))
        act_copy_id.setEnabled(bool(record.event_id))
        menu.addAction(act_copy_id)

        menu.addSeparator()
        # Right-clicking inside the selection acts on the selection;
        # right-clicking a row outside it acts on that row, which is what
        # every list on this platform does and what stops a stray click
        # from deleting twelve drafts the user had selected earlier.
        targets = (
            self.selected_identifiers()
            if item.isSelected()
            else [identifier]
        )
        if len(targets) > 1:
            label = ngettext("Delete {n} Draft", "Delete {n} Drafts", len(targets))
            act_delete = QAction(label.format(n=len(targets)), menu)
        else:
            act_delete = QAction(_("Delete Draft"), menu)
        act_delete.setData(("delete", list(targets)))
        menu.addAction(act_delete)
        return menu

    def _on_row_command(self, action: QAction) -> None:
        """A command of a row's menu, by what its action carries. The draft
        is looked up again here: the list may have been rebuilt while the
        menu was open."""
        command = action.data()
        if not isinstance(command, tuple) or len(command) != 2:
            return
        name, target = command
        if name == "delete":
            self._request_delete([i for i in target if isinstance(i, str)])
        elif name == "preview":
            item = self._item_of(target)
            if item is not None:
                self._show_preview_for(item)
        elif name == "retry":
            self.retry_decrypt.emit(target)
        elif name == "open":
            self.open_draft.emit(target)
        elif name == "publish":
            self.publish_draft.emit(target)
        elif name == "copy_id":
            record = self._store.get(target) if self._store is not None else None
            if record is not None:
                self._copy_event_id(record)

    def _item_of(self, identifier: str) -> Optional[QListWidgetItem]:
        for row in range(self._list.count()):
            item = self._list.item(row)
            if item is not None and item.data(Qt.UserRole) == identifier:
                return item
        return None

    # -- lifetime ----------------------------------------------------------

    def showEvent(self, event) -> None:
        # Dates are relative to today: a panel shown on another day says
        # "Yesterday" where it said "Today".
        for row in range(self._list.count()):
            item = self._list.item(row)
            record = self.preview_record(item.data(Qt.UserRole))
            if record is not None:
                self._decorate_item(item, record)
        self._refresh_sections()
        super().showEvent(event)

    def hideEvent(self, event) -> None:
        """A hidden panel must not leave a floating window describing it."""
        self._preview.close(disarm=True)
        super().hideEvent(event)

    def changeEvent(self, event) -> None:
        if event.type() == QEvent.FontChange:
            # Every dimension of the preview is derived from font
            # metrics, and recomposing one under the pointer is not
            # worth the code. The next hover builds it at the new size.
            self._preview.close(disarm=True)
            self._make_room_for_view_names()
        super().changeEvent(event)

    def _make_room_for_view_names(self) -> None:
        """Never so narrow that the pop-up button, on a line of its own,
        cuts a view's name: at large text the panel asks for that room,
        up to MAX_PANEL_WIDTH (past it the name elides, and stays whole
        in the button's tooltip and accessible name). Below that, the
        panel's narrowest width stays MIN_PANEL_WIDTH whatever the names."""
        wanted = max(MIN_PANEL_WIDTH, min(MAX_PANEL_WIDTH, self._top_band.popup_room()))
        if wanted != self.minimumWidth():
            self.setMinimumWidth(wanted)

    def closeEvent(self, event) -> None:
        self._preview.shutdown()
        super().closeEvent(event)

    def _copy_event_id(self, record: DraftRecord) -> None:
        clip = QApplication.clipboard()
        if clip is not None and record.event_id:
            clip.setText(record.event_id)
        # Notify the host so it can confirm in the status bar.
        self.copy_event_id.emit(record.event_id)
