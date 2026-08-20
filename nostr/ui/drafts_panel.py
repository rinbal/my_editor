# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Side-docked drafts panel, the editor's primary surface for NIP-37 drafts.

Layout, top to bottom:

  ┌──────────────────────────────────────────┐
  │  [ Drafts | Feeds ]              ⟲    ×  │  chrome band 1
  │  [ Search drafts…                      ] │  chrome band 2
  │  Drafts for Alice                        │  status line
  ├──────────────────────────────────────────┤
  │  Article title                      2h   │  list rows
  │  blog.example.com · first words of body  │
  │  Another draft                      1d   │
  │  first words of body                     │
  │                                          │
  │   (empty state placeholder)              │
  └──────────────────────────────────────────┘

Everything starts at the same 12 px gutter: the segmented control, the
search field, the status line, the empty state and both row lines.

Band 1 is one line only for as long as its two halves fit on one. Once
the application font has grown enough to crowd them, the refresh and
close pair drops to a second line and the mode switch keeps the first,
so its labels stay whole instead of being clipped to fit.

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

The account the drafts belong to is named in the status line, and the
one control that switches accounts is the editor header's ProfileChip
(``widgets.py``). The panel deliberately carries no second chip: two
identical avatar-plus-chevron controls a few hundred pixels apart read
as one duplicated control, and only one of them owned the menu.

There is no kind filter. The wrap's ``k`` tag is optional, so an
undecrypted, loading or failed draft has ``inner_kind == 0``, and
filtering on it hid drafts instead of sorting them. Search is the only
filter, so clearing the field always shows every draft in the store.

Cross-platform discipline:
  - All glyphs are Unicode codepoints (⟲, ×) so the panel renders
    identically on macOS / Windows / Linux without bundling fonts.
  - Uses Qt's built-in widgets exclusively (QListWidget + setItemWidget
    pattern) so the panel honours each platform's native scrollbar,
    selection highlight, and high-DPI behaviour for free.
  - No platform-specific code paths, `is_dark` is the only branching
    axis, matching the rest of the editor.

The panel is **dormant** when no profile is bound: ``set_active_profile(None)``
puts it in a quiet "not connected" state. It is the host (MainWindow)'s
job not to *show* the panel at all when there is no profile, but if
it does, the panel won't crash, it'll just show the disconnected state.
"""

from __future__ import annotations

import time
from typing import Callable, List, NamedTuple, Optional, Tuple

from PySide6.QtCore import QEvent, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QFont,
    QFontMetrics,
    QKeySequence,
    QPainter,
    QPalette,
)
from PySide6.QtWidgets import (
    QApplication,
    QBoxLayout,
    QButtonGroup,
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
    QStylePainter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..draft_store import DraftRecord, DraftState, DraftStore
from ..profiles import Profile
# Colour, type and record-to-words live in ``drafts_common`` because the
# preview popover needs the same three things and cannot import this
# module back: the panel is what builds the preview's controller. The
# aliases keep the panel's own body, and its tests, reading as before.
from .drafts_common import (
    MIN_CONTROL_PX as _MIN_CONTROL_PX,
    THEME_TOKENS as _THEME_TOKENS,
    format_absolute_time as _format_absolute_time,
    secondary_font as _secondary_font,
    source_host as _source_host,
    title_font as _title_font,
)
from .drafts_preview import (
    PreviewController,
    preview_announcement,
    preview_is_eligible,
)
from .feeds_panel import FeedsPanel


# Width hints. The host can resize through a QSplitter; these are the
# sensible defaults so the panel doesn't crowd the editor.
DEFAULT_PANEL_WIDTH: int = 320
MIN_PANEL_WIDTH: int = 260
MAX_PANEL_WIDTH: int = 520

# The panel's single gutter. The segmented control, the search field,
# the status line, the empty state and both row lines all start here.
# ``layout.md``: "Align components with one another to make them easier
# to scan and to communicate organization and hierarchy."
GUTTER: int = 12

# Row box model, all vertical values feed the derived row height below.
_ROW_PAD_V: int = 7
_ROW_LINE_GAP: int = 2
_ROW_INLINE_GAP: int = 8
# Selected rows get a leading bar as well as a fill. ``accessibility.md``:
# "Offer visual indicators, like distinct shapes or icons, in addition to
# color to help people perceive differences in function and changes in
# state."
_SELECTION_BAR_W: int = 3

# How long a one-off sync narration holds the status line once the store
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
QFrame#drafts_panel {{
    background: {t["panel_bg"]};
    border-left: 1px solid {t["border"]};
}}
QFrame#drafts_panel_top_band,
QFrame#drafts_panel_search_band {{
    background: {t["chrome_bg"]};
    border: none;
}}
/* One hairline, where the chrome meets the list. The two bands read as
   a single surface so nothing divides them. layout.md: "Group related
   items to help people find the information they want... use negative
   space, background shapes, colors, materials, or separator lines to
   show when elements are related". */
QFrame#drafts_panel_search_band {{ border-bottom: 1px solid {t["border"]}; }}

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

QPushButton#drafts_panel_segment {{
    background: transparent;
    color: {t["muted"]};
    border: 1px solid {t["border"]};
    padding: 2px {_segment_padding_px()}px;
}}
QPushButton#drafts_panel_segment[seg="first"] {{
    border-top-left-radius: 4px;
    border-bottom-left-radius: 4px;
    border-right: none;
}}
QPushButton#drafts_panel_segment[seg="last"] {{
    border-top-right-radius: 4px;
    border-bottom-right-radius: 4px;
}}
QPushButton#drafts_panel_segment:hover {{ color: {t["chrome_fg"]}; }}
QPushButton#drafts_panel_segment:checked {{
    background: {t["panel_bg"]};
    color: {t["row_fg"]};
}}

/* Focus indicators. Measured before this change: rendering each control
   focused and unfocused differed by zero pixels, because setting a
   background and a border in QSS makes Qt paint the whole control from
   the stylesheet box model and skip the native ring. accessibility.md:
   "Let people use the keyboard alone to navigate and interact with your
   app... evaluate your app to ensure it works well with Full Keyboard
   Access." Focus is carried by the border because the segments already
   spend their background on ``checked``, so the two states have to be
   distinguishable from one another as well as from rest. */
QPushButton#drafts_panel_segment:focus,
QToolButton#drafts_panel_icon_btn:focus,
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
QListWidget#drafts_panel_list::item {{ border: none; }}
QListWidget#drafts_panel_list::item:selected {{ background: {t["selected_bg"]}; }}
/* No ::item:hover rule: the row widget covers the item rect exactly and
   is not mouse transparent, so the viewport never sees the pointer and
   such a rule could not fire. The row paints its own hover fill.
   No ``outline: 0`` either, so the keyboard's current row keeps
   whatever indicator the platform draws. */

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

QLabel#drafts_row_title {{ color: {t["row_fg"]}; font-weight: 600; }}
QLabel#drafts_row_age,
QLabel#drafts_row_meta {{ color: {t["muted"]}; }}
/* Progress and failure must not share a channel. feedback.md: "it often
   works well to display status information in a passive way so that
   people can view it when they need it. In contrast, a warning about
   possible data loss needs to interrupt people." Both recede in colour,
   only failure leans. */
QLabel#drafts_row_title[state="loading"],
QLabel#drafts_row_title[state="failed"] {{ color: {t["muted"]}; }}
QLabel#drafts_row_title[sel="true"] {{ color: {t["selected_fg"]}; }}
QLabel#drafts_row_age[sel="true"],
QLabel#drafts_row_meta[sel="true"] {{ color: {t["selected_muted"]}; }}
/* Last, and colour-free, so a failed row keeps its slant whether or not
   it is also the selected one. */
QLabel#drafts_row_title[state="failed"] {{ font-style: italic; }}
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

# The age column is sized from the widest thing it can ever hold, so it
# never clips and never changes width from row to row. "just now" is
# rendered "now" for the same reason: it was the one string wide enough
# to give a freshly saved draft a different column width from every
# other row.
_AGE_SAMPLES: Tuple[str, ...] = ("now", "59m", "23h", "29d", "11mo", "99y")


class _RowMetrics(NamedTuple):
    title_font: QFont
    secondary_font: QFont
    height: int
    age_width: int


def _row_metrics() -> _RowMetrics:
    """Row fonts and geometry, derived rather than pinned.

    Recomputed on demand rather than cached at import: the row widget
    and its list item's size hint have to agree, and they only agree if
    both read this one function against the current application font. At
    the macOS 13 pt default this returns a 54 px row, which is exactly
    the height that used to be hardcoded, so density does not change on
    a normal machine, it merely scales now.
    """
    title_font = _title_font()
    secondary_font = _secondary_font()
    fm_title = QFontMetrics(title_font)
    fm_secondary = QFontMetrics(secondary_font)
    height = (
        _ROW_PAD_V
        + fm_title.height()
        + _ROW_LINE_GAP
        + fm_secondary.height()
        + _ROW_PAD_V
    )
    age_width = max(fm_secondary.horizontalAdvance(s) for s in _AGE_SAMPLES)
    return _RowMetrics(title_font, secondary_font, height, age_width)


# The inset a segment carries at the size the panel was drawn for, and
# the least it may shrink to before the control stops reading as a
# button rather than as bare text.
_SEGMENT_PADDING_PX = 14
_MIN_SEGMENT_PADDING_PX = 8

# The two labels the switch has to fit. Kept here rather than read off
# the widgets because the stylesheet is built before they exist.
_SEGMENT_LABELS = ("Drafts", "Feeds")


def _segment_padding_px() -> int:
    """Horizontal inset for one segment of the mode switch.

    Both segments share one line at the panel's narrowest width, and at
    200 percent type the two labels plus a 14 px inset each need more
    room than that line has. Something has to give, and the inset is the
    right thing: a generous margin around a word that has been cut in
    half helps nobody, while a tighter one around the whole word still
    reads as a button.

    Derived from the constraint rather than from a scaling curve, so it
    stays correct if the labels, the gutter or the minimum width change.
    The floor means a truly enormous font still elides, which the
    segment already does legibly, rather than collapsing the control.
    """
    metrics = QFontMetrics(QApplication.font())
    widest = max(metrics.horizontalAdvance(label) for label in _SEGMENT_LABELS)
    line = MIN_PANEL_WIDTH - 2 * GUTTER
    # Two segments, each one label, two insets and a one pixel border.
    room = (line - 2 * (widest + 2)) // 4
    return max(_MIN_SEGMENT_PADDING_PX, min(_SEGMENT_PADDING_PX, room))


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

def _format_relative_time(ts: int, *, now: Optional[int] = None) -> str:
    """Return a compact human-readable age, e.g. ``'2h'``, ``'3d'``.

    Every result fits the age column, which is sized from
    ``_AGE_SAMPLES``. Full timestamps belong in the row tooltip and in
    the row's accessible text, not on the row itself.
    """
    if ts <= 0:
        return ""
    now = now if now is not None else int(time.time())
    delta = max(0, now - int(ts))
    if delta < 60:
        return "now"
    if delta < 3600:
        return f"{delta // 60}m"
    if delta < 86_400:
        return f"{delta // 3600}h"
    if delta < 86_400 * 30:
        return f"{delta // 86_400}d"
    if delta < 86_400 * 365:
        return f"{delta // (86_400 * 30)}mo"
    return f"{delta // (86_400 * 365)}y"


# Row state as a stylesheet property. Set from one mapping rather than a
# boolean OR: LOADING and FAILED used to share a single ``failed`` flag,
# so a draft mid-decryption rendered identically to one that could not be
# decrypted and a cold load of a whole library looked broken.
_ROW_STATE = {
    DraftState.READY: "ready",
    DraftState.LOADING: "loading",
    DraftState.FAILED: "failed",
}


def _display_title(record: DraftRecord) -> str:
    """Line 1 of a row. Shared so the row and its announcement agree."""
    if record.state is DraftState.LOADING:
        return "Decrypting…"
    return record.title or "(no title)"


def _display_meta(record: DraftRecord) -> str:
    """Line 2 of a row: one composed string, one elision.

    Elide-right naturally preserves the host, because the host sits at
    the head of the line.
    """
    if record.state is DraftState.LOADING:
        return "Decrypting"
    if record.state is DraftState.FAILED:
        reason = record.failure_reason or "Could not decrypt"
        # With the ciphertext still in hand a retry is one double-click
        # away, so the row says so rather than looking permanently broken.
        if record.ciphertext:
            return f"{reason}. Double-click to retry."
        return reason
    host = _source_host(record)
    preview = record.snippet or ""
    if host and preview:
        return f"{host} · {preview}"
    return host or preview


def _accessible_row_text(record: DraftRecord, *, now: Optional[int] = None) -> str:
    """What a screen reader announces for one row.

    The visible row lives in a ``setItemWidget`` widget that the
    accessibility tree does not descend into, and the item itself had no
    display text, so a screen reader heard a list of blank rows. This
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
        sentences.append("Decrypting")
    elif record.state is DraftState.FAILED:
        sentences.append(record.failure_reason or "Could not decrypt")
        if record.ciphertext:
            sentences.append("Press Return to retry")
    else:
        host = _source_host(record)
        if host:
            sentences.append(f"Imported from {host}")
    saved = _format_absolute_time(record.created_at)
    if saved:
        sentences.append(f"Saved {saved}")
    extra = preview_announcement(
        record, now=now if now is not None else int(time.time()),
    )
    if extra:
        sentences.append(extra)
    return ". ".join(s.rstrip(". ") for s in sentences if s.strip()) + "."


def _row_tooltip(record: DraftRecord) -> str:
    """The whole row, unelided, plus the exact save time."""
    lines = [_display_title(record)]
    meta = _display_meta(record)
    if meta:
        lines.append(meta)
    saved = _format_absolute_time(record.created_at)
    if saved:
        lines.append(f"Saved {saved}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Row widget                                                                  #
# --------------------------------------------------------------------------- #

class _ElidingLabel(QLabel):
    """Single-line label that elides at paint time, from its own width.

    Elision must not run through ``setText``: setText changes the
    label's sizeHint, which re-runs the layout and moves the width the
    elision was computed against, so the string overflows the final
    label and QLabel hard-clips the ellipsis that was added. That is
    why a row read ``![One Class, One`` with no trailing dots at every
    panel width. Painting from ``contentsRect()`` has no such feedback
    loop, and re-elides on splitter drags and font changes for free.

    ``QSizePolicy.Ignored`` plus a zero minimum width is what keeps a
    very long title from forcing the panel wider than the user dragged
    it, which matters most at ``MIN_PANEL_WIDTH``.

    It paints text and nothing else. The stylesheet gives these labels
    colour and weight only; anyone adding a background or a border to
    them has to draw the style primitive here first, or set
    ``Qt.WA_StyledBackground``. The tooltip is owned by the row, so the
    whole row is readable from any point on it rather than one line at
    a time.
    """

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        mode: Qt.TextElideMode = Qt.ElideRight,
    ) -> None:
        super().__init__(parent)
        self._full = ""
        self._mode = mode
        self.setTextInteractionFlags(Qt.NoTextInteraction)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

    def full_text(self) -> str:
        """The unelided string. The label never mutates it."""
        return self._full

    def set_full_text(self, text: str) -> None:
        self._full = text or ""
        # The painted string is elided and ``text()`` is empty, so the
        # accessible name is where the whole string stays reachable.
        # Going through ``setText`` instead is what caused the feedback
        # loop described above.
        self.setAccessibleName(self._full)
        self.update()

    def minimumSizeHint(self) -> QSize:
        hint = super().minimumSizeHint()
        hint.setWidth(0)
        return hint

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        rect = self.contentsRect()
        elided = self.fontMetrics().elidedText(self._full, self._mode, rect.width())
        # drawItemText paints through the palette, which is where the
        # stylesheet's ``color`` (including the [state] and [sel]
        # variants) lands after a polish.
        self.style().drawItemText(
            painter,
            rect,
            int(self.alignment()),
            self.palette(),
            self.isEnabled(),
            elided,
            self.foregroundRole(),
        )


class _SegmentButton(QPushButton):
    """Mode-switch segment that elides its own label at paint time.

    QPushButton does not elide. When the layout hands it less than its
    sizeHint it clips the centred string from both ends, so at 200
    percent type on a narrow panel "Drafts" painted as "raf" with no
    ellipsis to say anything had been cut. That is the opposite of what
    ``accessibility.md`` asks for: "Support larger text sizes. Make sure
    people can adjust the size of your text or icons to make them more
    legible, visible, and comfortable to read. Ideally, give people the
    option to enlarge text by at least 200 percent."

    Elision happens at paint time for the same reason it does in
    ``_ElidingLabel``: eliding through ``setText`` would shrink the
    button's sizeHint, which re-runs the layout, which hands the button
    a different width, which changes the elision. ``text()`` stays the
    full label, so the layout keeps asking for the room the whole word
    needs and only the painted glyphs are ever cut.

    The recovery is the tooltip, which always carries the untruncated
    label. ``text-fields.md``: "Consider using an expansion tooltip to
    show the full version of clipped or truncated text."
    """

    def __init__(
        self, label: str, hint: str, parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(label, parent)
        self.setObjectName("drafts_panel_segment")
        self.setCheckable(True)
        self.setAccessibleName(label)
        # The label leads, so the word survives even when the segment is
        # too narrow to paint it. ``buttons.md`` notes that "buttons that
        # contain text don't need to display a tooltip because the
        # button's descriptive label communicates what it does", which
        # holds only for as long as the label is fully visible.
        self.setToolTip(f"{label}: {hint}")

    def painted_text(self) -> str:
        """The string ``paintEvent`` will draw at the current width."""
        option = QStyleOptionButton()
        self.initStyleOption(option)
        # Ask the style rather than subtracting a padding literal: the
        # padding lives in the panel's QSS, so QStyleSheetStyle is the
        # only thing that knows the real inner width. ``contentsRect()``
        # does not, it reports the full width and is what made this
        # clipping invisible to measurement.
        inner = self.style().subElementRect(
            QStyle.SE_PushButtonContents, option, self,
        )
        return self.fontMetrics().elidedText(
            self.text(), Qt.ElideRight, inner.width(),
        )

    def paintEvent(self, event) -> None:
        option = QStyleOptionButton()
        self.initStyleOption(option)
        option.text = self.painted_text()
        # Drawn through the widget's own style, so the stylesheet keeps
        # ownership of the fill, the border, the corner rounding and the
        # focus ring. Only the string differs from what QPushButton
        # would have painted for itself.
        QStylePainter(self).drawControl(QStyle.CE_PushButton, option)


class _TopBand(QFrame):
    """Band 1, which drops to two lines when the type outgrows one.

    The mode switch and the two icon buttons share a single line only
    for as long as they fit on one. At 200 percent type on a 260 px
    panel they need 356 px of a 240 px line, and the layout pays for
    that by squeezing the segments to 65 px each, which is less than
    the word "Drafts" occupies.

    ``typography.md``: "Consider adjusting your layout at large font
    sizes. When font size increases in a horizontally constrained
    context, inline items (like glyphs and timestamps) and container
    boundaries can crowd text and cause truncation or overlapping." The
    row widget already answers that with a stacked layout; this is the
    same answer for the band, and it hands the segments the whole line,
    which is enough for the label to survive intact at every size the
    panel is likely to meet.

    Reflowing is one ``setDirection`` call, with no reparenting, so the
    button group, the focus chain and the tab order are untouched by it.
    It cannot oscillate either: neither the band's width nor the two
    holders' size hints change when the direction does, so the test that
    chose vertical keeps choosing vertical.
    """

    def __init__(
        self,
        segments: QWidget,
        icons: QWidget,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("drafts_panel_top_band")
        self._segments = segments
        self._icons = icons
        self._box = QBoxLayout(QBoxLayout.LeftToRight, self)
        self._box.setContentsMargins(GUTTER, 5, 8, 5)
        self._box.setSpacing(6)
        self._box.addWidget(segments)
        # Collapses to nothing in the vertical direction, because the
        # band is only ever given its sizeHint height.
        self._box.addStretch(1)
        self._box.addWidget(icons)

    def is_stacked(self) -> bool:
        """True when the icon buttons have moved to their own line."""
        return self._box.direction() == QBoxLayout.TopToBottom

    def _reflow(self) -> None:
        margins = self._box.contentsMargins()
        available = self.width() - margins.left() - margins.right()
        needed = (
            self._segments.sizeHint().width()
            + self._icons.sizeHint().width()
            + self._box.spacing()
        )
        wanted = (
            QBoxLayout.LeftToRight if needed <= available
            else QBoxLayout.TopToBottom
        )
        if self._box.direction() != wanted:
            self._box.setDirection(wanted)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._reflow()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.FontChange:
            self._reflow()


class _DraftRowWidget(QWidget):
    """One row: title and age on line 1, host and preview on line 2.

    Two stacked lines with exactly one inline item, per ``typography.md``:
    "When font size increases in a horizontally constrained context,
    inline items (like glyphs and timestamps) and container boundaries
    can crowd text and cause truncation or overlapping. To improve
    readability, consider using a stacked layout where text appears
    above secondary items."

    The lock glyph and the kind pill that used to sit on line 1 are
    gone. ``layout.md``: "Make essential information easy to find by
    giving it sufficient space. People want to view the most important
    information right away, so don't obscure it by crowding it with
    nonessential details." Neither discriminated anything: the lock was
    an identical ⚿ on every row and the pill read "Imported" on 54 of 54
    rows in a real library.
    """

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        *,
        is_dark: bool = True,
    ) -> None:
        super().__init__(parent)
        self._is_dark = bool(is_dark)
        self._selected = False
        self._hovered = False

        metrics = _row_metrics()
        self._row_height = metrics.height
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(GUTTER, _ROW_PAD_V, GUTTER, _ROW_PAD_V)
        layout.setSpacing(_ROW_LINE_GAP)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(_ROW_INLINE_GAP)

        self._title = _ElidingLabel()
        self._title.setObjectName("drafts_row_title")
        self._title.setFont(metrics.title_font)
        self._title.setProperty("state", "ready")
        self._title.setProperty("sel", "false")
        top.addWidget(self._title, 1)

        # The only fixed width on line 1, and it is derived, so it holds
        # at 200% font instead of clipping "just now" inside 36 px.
        self._age = QLabel("")
        self._age.setObjectName("drafts_row_age")
        self._age.setFont(metrics.secondary_font)
        self._age.setFixedWidth(metrics.age_width)
        self._age.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._age.setProperty("sel", "false")
        top.addWidget(self._age, 0)
        layout.addLayout(top)

        self._meta = _ElidingLabel()
        self._meta.setObjectName("drafts_row_meta")
        self._meta.setFont(metrics.secondary_font)
        self._meta.setProperty("sel", "false")
        layout.addWidget(self._meta)

        self._styled_labels = (self._title, self._age, self._meta)
        self.setFixedHeight(metrics.height)

    # -- geometry ----------------------------------------------------------

    def row_height(self) -> int:
        """The height this row asked for, so the item can hint the same.

        Both the widget and its ``QListWidgetItem`` must read one value
        or the list allocates its own height whatever the row asks for.
        """
        return self._row_height

    # -- content -----------------------------------------------------------

    def set_record(self, record: DraftRecord) -> None:
        self._title.set_full_text(_display_title(record))
        self._meta.set_full_text(_display_meta(record))
        self._age.setText(_format_relative_time(record.created_at))
        self.setToolTip(_row_tooltip(record))
        self._title.setProperty("state", _ROW_STATE.get(record.state, "ready"))
        # The draft this row is showing, stored on the widget so the
        # preview controller's event filter can resolve a row without
        # walking the list, and so it never has to hold a widget
        # reference of its own past the event it is handling.
        self.setProperty("draft_identifier", record.identifier)
        self.refresh_style()

    # -- state -------------------------------------------------------------

    def set_selected(self, selected: bool) -> None:
        if bool(selected) == self._selected:
            return
        self._selected = bool(selected)
        for label in self._styled_labels:
            label.setProperty("sel", "true" if self._selected else "false")
        self.refresh_style()

    def apply_theme(self, is_dark: bool) -> None:
        self._is_dark = bool(is_dark)
        self.update()

    def enterEvent(self, event) -> None:
        # Hover changes the fill only, never a text colour, so it costs a
        # repaint and not a restyle.
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def refresh_style(self) -> None:
        for label in self._styled_labels:
            label.style().unpolish(label)
            label.style().polish(label)
        self.update()

    # -- painting ----------------------------------------------------------

    def paintEvent(self, event) -> None:
        tokens = _THEME_TOKENS[self._is_dark]
        painter = QPainter(self)
        if self._hovered and not self._selected:
            # The list's ::item:hover rule cannot fire, this widget covers
            # the item rect exactly and is not mouse transparent. Making
            # it transparent would revive the rule and lose the row's
            # tooltip, which is the only place the full title and the
            # exact save time are readable, so the fill is painted here.
            painter.fillRect(self.rect(), QColor(tokens["hover_bg"]))
        if self._selected:
            # The shape cue that goes with the selection fill. A bar
            # survives greyscale and colour blindness in a way a
            # background tint does not.
            painter.fillRect(
                QRect(0, 0, _SELECTION_BAR_W, self.height()),
                QColor(tokens["selection_bar"]),
            )


# --------------------------------------------------------------------------- #
# Drafts panel                                                                #
# --------------------------------------------------------------------------- #

class DraftsPanel(QFrame):
    """Side-docked drafts surface.

    Public signals (the host wires these into MainWindow handlers):
      open_draft(str)            , identifier of a draft to open in a new tab
      publish_draft(str)         , identifier to promote draft → real publish
      delete_drafts(list)        , identifiers to tombstone, one or many
      copy_event_id(str)         , copy outer wrap event id to clipboard
      refresh_requested()        , manual refresh tap on the header
      close_requested()          , × on the header

    Switching profiles is not one of these: the editor header's
    ProfileChip owns that menu, and the panel names the bound account
    in its status line rather than duplicating the control.

    Public methods:
      bind_store(store)          , connect to a DraftStore instance
      set_active_profile(p)      , the account the drafts belong to
      set_status(text)           , narrate a sync step on the status line
      set_signer_unsupported(bool), show the "signer lacks NIP-44" state
      apply_theme(is_dark)       , switch dark/light
      set_preview_image_loader(l), hand the hover preview a ThumbnailLoader

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
    close_requested = Signal()

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
        outer.addWidget(self._build_mode_stack(), 1)

    def _build_mode_stack(self) -> QWidget:
        """Top-level page switcher: ``Drafts`` vs ``Feeds``.

        Each segment button maps to one index in this stack. The search
        band only applies to the drafts list so it lives inside the
        drafts-mode container, not at the outer level.
        """
        self._mode_stack = QStackedWidget()
        self._mode_stack.setObjectName("drafts_panel_mode_stack")

        # Drafts mode: search band + body.
        drafts_mode = QWidget()
        drafts_mode_layout = QVBoxLayout(drafts_mode)
        drafts_mode_layout.setContentsMargins(0, 0, 0, 0)
        drafts_mode_layout.setSpacing(0)
        drafts_mode_layout.addWidget(self._build_search_band())
        drafts_mode_layout.addWidget(self._build_body(), 1)
        self._mode_stack.addWidget(drafts_mode)

        # Feeds mode: RSS / Atom / JSON Feed importer.
        self._feeds_panel = FeedsPanel(is_dark=self._is_dark, parent=self)
        self._mode_stack.addWidget(self._feeds_panel)

        return self._mode_stack

    def _build_top_band(self) -> QWidget:
        """Band 1: the mode switch on the leading edge, refresh and close.

        This was two stacked bands, a 44 px header holding nothing but
        the trailing buttons and a 40 px row holding a centred segmented
        control. Together with the old chip row that put 146 px of chrome
        above the first draft; it is now 76 px, and the freed space is
        deliberately not backfilled.

        ``_TopBand`` takes the two halves back to two lines, but only
        when the application font has grown enough that they no longer
        share one.
        """
        control_h = _control_height()

        # A button group with the cosmetic ``checked`` look, flat buttons
        # reading as a segmented control. The corner rounding is a
        # ``seg`` property rather than a per-button stylesheet so that
        # the panel sheet stays the single owner of the segments' look
        # and its :focus rule is not shadowed.
        # Both segments elide their own label rather than clipping it,
        # so the band survives its own type scale at any panel width.
        self._seg_drafts = _SegmentButton("Drafts", "Your saved private drafts")
        self._seg_drafts.setProperty("seg", "first")
        self._seg_drafts.setChecked(True)
        self._seg_drafts.setMinimumHeight(control_h)

        self._seg_feeds = _SegmentButton(
            "Feeds", "Import RSS, Atom, or JSON feeds as private drafts",
        )
        self._seg_feeds.setProperty("seg", "last")
        self._seg_feeds.setMinimumHeight(control_h)

        segments = QWidget()
        segments_layout = QHBoxLayout(segments)
        segments_layout.setContentsMargins(0, 0, 0, 0)
        segments_layout.setSpacing(0)
        segments_layout.addWidget(self._seg_drafts)
        segments_layout.addWidget(self._seg_feeds)

        group = QButtonGroup(segments)
        group.setExclusive(True)
        group.addButton(self._seg_drafts, 0)
        group.addButton(self._seg_feeds, 1)
        group.idToggled.connect(self._on_segment_changed)

        # Icon-only, so the accessible name is the only string that
        # announces them; the glyph alone reads as "⟲" and "×".
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
        self._refresh_btn = self._make_icon_button(
            "⟲", "Refresh drafts", self.refresh_requested.emit, control_h,
        )
        icons_layout.addWidget(self._refresh_btn)
        self._close_btn = self._make_icon_button(
            "×", "Close drafts panel", self.close_requested.emit, control_h,
        )
        icons_layout.addWidget(self._close_btn)

        self._top_band = _TopBand(segments, icons)
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
        button.clicked.connect(slot)
        return button

    def _build_search_band(self) -> QWidget:
        """Band 2: the search field, which is the panel's only filter.

        ``searching.md``: "If search is important, give it a primary
        position in your app or view." It is the only filter left, so it
        gets the whole band.

        There used to be an All / Notes / Articles chip row under it. It
        filtered on ``record.inner_kind``, which comes from the outer
        wrap's optional ``k`` tag, so every draft that had not been
        decrypted yet, and every draft from a client that omits the tag,
        was kind 0 and matched neither Notes nor Articles. The chips did
        not mis-sort those drafts, they hid them.

        The status line sits under the field rather than in a footer.
        ``sidebars.md``: "Avoid putting critical information or actions
        at the bottom of a sidebar. People often relocate a window in a
        way that hides its bottom edge." ``feedback.md``: "When status
        feedback is available near the items it describes, people get
        important information without having to take action or leave
        their current context."
        """
        frame = QFrame()
        frame.setObjectName("drafts_panel_search_band")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(GUTTER, 0, GUTTER, 6)
        layout.setSpacing(3)

        self._search_edit = QLineEdit()
        self._search_edit.setObjectName("drafts_panel_search")
        self._search_edit.setPlaceholderText("Search drafts…")
        self._search_edit.setAccessibleName("Search drafts")
        self._search_edit.setClearButtonEnabled(True)
        self._search_edit.setMinimumHeight(_control_height())
        self._search_edit.textChanged.connect(self._on_search_changed)
        layout.addWidget(self._search_edit)

        # Eliding, for the same reason the row title is: a long display
        # name would otherwise be clipped from the right with no sign it
        # had been cut, and would push the band's sizeHint past the
        # width the user dragged the panel to.
        self._status_label = _ElidingLabel()
        self._status_label.setObjectName("drafts_panel_status")
        self._status_label.setFont(_secondary_font())
        self._status_label.setProperty("error", "false")
        layout.addWidget(self._status_label)
        return frame

    def _build_body(self) -> QWidget:
        # Stacks the actual list and an empty-state widget on the same
        # area; ``_refresh_empty_state`` flips which is visible.
        container = QWidget()
        self._body_stack = QStackedLayout(container)
        self._body_stack.setContentsMargins(0, 0, 0, 0)

        self._list = QListWidget()
        self._list.setObjectName("drafts_panel_list")
        self._list.setSpacing(0)
        self._list.setUniformItemSizes(True)
        self._list.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        # Extended, so a shift-drag or a cmd-click selects a range the way
        # it does in every list on this platform, and Select All means
        # what it says. The preview and the open/publish commands still
        # follow the current row, because those act on one draft and
        # there is exactly one current row whatever else is selected.
        self._list.setSelectionMode(QListWidget.ExtendedSelection)
        self._list.itemActivated.connect(self._on_item_activated)
        self._list.currentItemChanged.connect(self._on_current_item_changed)
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
        # for the life of the panel.
        self._empty_action = QPushButton()
        self._empty_action.setObjectName("drafts_panel_empty_action")
        self._empty_action.setMinimumHeight(_control_height())
        self._empty_action_handler: Optional[Callable[[], None]] = None
        self._empty_action.clicked.connect(self._on_empty_action)
        self._empty_action.hide()
        empty_layout.addWidget(self._empty_action, 0, Qt.AlignHCenter)
        empty_layout.addStretch(2)
        self._body_stack.addWidget(self._empty_widget)
        return container

    # -- public API: feeds page wiring ------------------------------------

    @property
    def feeds(self) -> FeedsPanel:
        """The RSS importer page. Host wires its runtime via this handle."""
        return self._feeds_panel

    def _on_segment_changed(self, button_id: int, checked: bool) -> None:
        """Switch the top-level mode stack when a segment toggles on.

        Guarded with ``hasattr`` because the button group's ``idToggled``
        signal is connected during ``_build_top_band``, which runs
        before ``_build_mode_stack``. Any future reordering of the build
        sequence shouldn't crash on the early signal path.
        """
        if not checked or not hasattr(self, "_mode_stack"):
            return
        if 0 <= button_id < self._mode_stack.count():
            self._mode_stack.setCurrentIndex(button_id)
        # Feeds is different data and different rows, so the drafts
        # preview has nothing to describe there.
        self._preview.close(disarm=True)

    # -- public API: theming ----------------------------------------------

    def apply_theme(self, is_dark: bool) -> None:
        self._is_dark = is_dark
        self.setStyleSheet(_panel_css(is_dark))
        self._apply_placeholder_colour(is_dark)
        # Re-styled in place rather than closed: a theme switch is not a
        # reason to take a surface away from someone reading it.
        self._preview.apply_theme(is_dark)
        if hasattr(self, "_feeds_panel") and self._feeds_panel is not None:
            self._feeds_panel.apply_theme(is_dark)
        if not hasattr(self, "_list") or self._list is None:
            return
        self._list.viewport().update()
        # Rows paint their own hover fill and selection bar, so they each
        # need the new tokens, and their property-driven labels need a
        # repolish to pick up the new colours.
        for i in range(self._list.count()):
            widget = self._list.itemWidget(self._list.item(i))
            if isinstance(widget, _DraftRowWidget):
                widget.apply_theme(is_dark)
                widget.refresh_style()

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
        self._refresh_empty_state()
        # The status line names the account, so it has to be re-rendered
        # whenever the binding changes.
        self._render_status()
        if hasattr(self, "_feeds_panel") and self._feeds_panel is not None:
            self._feeds_panel.set_active_profile(profile)

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
        """Show (or clear) the state where the signer is not answering."""
        if unreachable == self._signer_unreachable:
            return
        self._signer_unreachable = unreachable
        if unreachable:
            # A stale narration line would otherwise outrank the new
            # state for its remaining TTL, which is exactly the window
            # where the user is looking for an explanation.
            self._sync_message = ""
        self._render_status()
        self._refresh_empty_state()

    def _has_readable_draft(self) -> bool:
        return any(
            record.state is DraftState.READY for record in self._store or []
        )

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

    def _preview_anchor(self, item: Optional[QListWidgetItem]) -> Tuple[str, QRect]:
        if item is None:
            return "", QRect()
        identifier = item.data(Qt.UserRole)
        if not isinstance(identifier, str) or not identifier:
            return "", QRect()
        rect = self._list.visualItemRect(item)
        return identifier, QRect(
            self._list.viewport().mapToGlobal(rect.topLeft()), rect.size(),
        )

    def _show_preview_for(self, item: QListWidgetItem) -> None:
        """Open the preview for the row a context menu was raised on.

        Resolved from the item rather than from the current row: a
        right-click does not move the selection, so the two are often
        different rows and the menu has to describe the one under the
        pointer. Opened as a keyboard preview, because it was reached by
        an explicit command and so should take focus and be dismissible
        with Escape rather than by moving the pointer away.
        """
        identifier, anchor = self._preview_anchor(item)
        if identifier:
            self._preview.open_for(identifier, anchor, keyboard=True)

    def preview_is_available(self) -> bool:
        """Whether a preview may open at all right now.

        A floating window over another application's window, describing
        a draft in a window the user is not looking at, is a bug, so the
        panel has to be on screen, in drafts mode, and inside the active
        window.
        """
        if not self.isVisible() or self._mode_stack.currentIndex() != 0:
            return False
        window = self.window()
        return window is not None and window.isActiveWindow()

    def preview_now(self) -> int:
        """The clock the preview reads, in one place so tests can move it."""
        return int(time.time())

    # -- status line: one writer ------------------------------------------

    def _render_status(self) -> None:
        """The single writer for the status line."""
        text, is_error = self._status_line()
        self._status_label.set_full_text(text)
        self._status_label.setToolTip(text)
        # An empty line collapses rather than leaving a blank strip of
        # chrome above the list.
        self._status_label.setVisible(bool(text))
        self._status_label.setProperty("error", "true" if is_error else "false")
        self._status_label.style().unpolish(self._status_label)
        self._status_label.style().polish(self._status_label)

    def _status_line(self) -> Tuple[str, bool]:
        """``(text, is_error)`` in priority order, first match wins.

        The resting text names the account, which is what replaces the
        deleted profile chip. It used to read "{ready}/{total}
        decrypted", which said "54/54" for the whole time the user was
        actually reading the list and so said nothing there.
        """
        if self._signer_unsupported:
            return "Signer cannot decrypt drafts (no NIP-44)", True
        # Outranks both the failure count and the decrypting count. Those
        # describe drafts; this describes the one thing standing between
        # the user and all of them, and it is the only line here whose
        # remedy is somewhere other than this app.
        if self._signer_unreachable:
            return "Your signer is not responding", True

        failed = ready = total = 0
        for record in self._store or []:
            total += 1
            if record.state is DraftState.FAILED:
                failed += 1
            elif record.state is DraftState.READY:
                ready += 1

        if failed:
            noun = "draft" if failed == 1 else "drafts"
            return f"{failed} {noun} could not be decrypted", True
        if self._loading or self._sync_message:
            return self._sync_message or "Refreshing drafts", False
        if total and ready < total:
            return f"Decrypting {ready} of {total}", False
        profile = self._active_profile
        if profile is None:
            return "", False
        return f"Drafts for {profile.display_name or profile.npub_short()}", False

    def _restart_status_ttl(self) -> None:
        self._status_timer.stop()
        # While a refresh is in flight the loading branch holds the line
        # on its own. Once it is not, a narration line is a report of a
        # finished event, so it expires and the line goes back to naming
        # the account.
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
            self._show_placeholder(
                "Connect a Nostr profile",
                "Drafts are end-to-end encrypted to your Nostr key. Connect a "
                "signer from Nostr → Connect Signer… to view, search, and "
                "create drafts.",
            )
            return
        if self._signer_unsupported:
            self._show_placeholder(
                "Signer does not support NIP-44",
                "This profile's signer cannot decrypt drafts. Connect a "
                "NIP-44-capable signer (Amber, nsec.app) to use drafts.",
            )
            return
        # Only when there is nothing readable to show. A signer that went
        # quiet halfway through leaves drafts already decrypted on
        # screen, and replacing those with an explanation would take away
        # more than it gives; there the status line and the per-row retry
        # carry the message.
        if self._signer_unreachable and not self._has_readable_draft():
            self._show_placeholder(
                "Your signer is not responding",
                "Drafts stay encrypted until your signer unlocks them. Open "
                "your signer app, make sure it is running, then try again.",
                action="Try again",
                on_action=self.retry_signer.emit,
            )
            return
        if self._store is None or len(self._store) == 0:
            if self._loading:
                # A first refresh in flight is not an empty library.
                # loading.md: "Show something as soon as possible. If you
                # make people wait for loading to complete before
                # displaying anything, they can interpret the lack of
                # content as a problem with your app." Rows arrive into
                # the list and the status line narrates the wait.
                self._body_stack.setCurrentIndex(0)
                return
            self._show_placeholder(
                "No private drafts yet",
                f"Press {_save_shortcut_text()} in any tab to save its contents "
                "as an encrypted draft on Nostr. Drafts sync to your other "
                "devices signed in with the same profile.",
            )
            return
        if self._list.count() == 0:
            self._show_placeholder(
                "No matching drafts",
                f'No draft matches "{self._search_text}".',
                action="Clear search",
                on_action=self._search_edit.clear,
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
    ) -> None:
        self._empty_title.setText(title)
        self._empty_body.setText(body)
        self._empty_action_handler = on_action if action else None
        if action:
            self._empty_action.setText(action)
        self._empty_action.setVisible(bool(action))
        self._body_stack.setCurrentIndex(1)

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
        unreachable, whatever its ``inner_kind`` or decryption state.
        """
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
        widget = _DraftRowWidget(is_dark=self._is_dark)
        widget.set_record(record)
        # One value, read from the row itself, so the list cannot
        # allocate a different height from the one the row asked for.
        item.setSizeHint(QSize(0, widget.row_height()))
        self._decorate_item(item, record)
        if at_index is None:
            self._list.addItem(item)
        else:
            self._list.insertItem(at_index, item)
        self._list.setItemWidget(item, widget)
        # Mouse tracking plus the controller's event filter. It cannot go
        # on the list's viewport: the row widget covers the item rect
        # exactly and is not mouse transparent, so the viewport never
        # sees the pointer.
        self._preview.attach_row(widget)
        self._items[record.identifier] = item

    def _decorate_item(self, item: QListWidgetItem, record: DraftRecord) -> None:
        """Announcement and tooltip for one row.

        Called from both ``_insert_row`` and ``_update_row`` so what a
        screen reader hears cannot drift from what the row shows.
        """
        item.setData(Qt.AccessibleTextRole, _accessible_row_text(record))
        item.setData(Qt.ToolTipRole, _row_tooltip(record))

    def _remove_row(self, identifier: str) -> None:
        item = self._items.pop(identifier, None)
        if item is None:
            return
        row = self._list.row(item)
        if row >= 0:
            self._list.takeItem(row)

    def _update_row(self, identifier: str) -> None:
        item = self._items.get(identifier)
        if item is None or self._store is None:
            return
        record = self._store.get(identifier)
        if record is None:
            return
        self._decorate_item(item, record)
        widget = self._list.itemWidget(item)
        if isinstance(widget, _DraftRowWidget):
            widget.set_record(record)

    # -- store-signal handlers --------------------------------------------

    def _on_record_added(self, identifier: str) -> None:
        if self._store is None:
            return
        record = self._store.get(identifier)
        if record is None or not self._passes_filter(record):
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
        # Closed and disarmed before the rebuild, not after: the rebuild
        # destroys every row widget, and typing must never leave a
        # preview anchored to one of them or open one for whichever row
        # slid under a stationary pointer.
        self._preview.on_search_changed()
        self._search_text = text
        self._rebuild_list()

    # -- list interaction --------------------------------------------------

    def _on_current_item_changed(
        self,
        current: Optional[QListWidgetItem],
        previous: Optional[QListWidgetItem],
    ) -> None:
        """Drive the row widgets' selected look from the list's current row.

        The list paints the selection fill through ``::item:selected``,
        but the row widget on top of it owns the text colours and the
        leading accent bar, so it has to be told.
        """
        for item, selected in ((previous, False), (current, True)):
            if item is None:
                continue
            try:
                widget = self._list.itemWidget(item)
            except RuntimeError:
                continue
            if isinstance(widget, _DraftRowWidget):
                widget.set_selected(selected)

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
        act_preview = QAction("Show Preview", menu)
        act_preview.setShortcut(QKeySequence(Qt.Key_Space))
        act_preview.triggered.connect(lambda: self._show_preview_for(item))
        act_preview.setEnabled(
            preview_is_eligible(record, now=self.preview_now())
        )
        menu.addAction(act_preview)
        menu.addSeparator()

        if record.state is DraftState.FAILED:
            # For a failed row the *only* useful primary action is to
            # retry decryption. Promote it to the top of the menu so
            # right-click → Enter is the recovery path.
            act_retry = QAction("Retry Decryption", menu)
            act_retry.triggered.connect(
                lambda: self.retry_decrypt.emit(identifier)
            )
            act_retry.setEnabled(bool(record.ciphertext))
            menu.addAction(act_retry)
            menu.addSeparator()

        act_open = QAction("Open in New Tab", menu)
        act_open.triggered.connect(lambda: self.open_draft.emit(identifier))
        act_open.setEnabled(record.state is DraftState.READY)
        menu.addAction(act_open)

        act_publish = QAction("Publish…", menu)
        act_publish.triggered.connect(lambda: self.publish_draft.emit(identifier))
        act_publish.setEnabled(record.state is DraftState.READY)
        menu.addAction(act_publish)

        menu.addSeparator()
        act_copy_id = QAction("Copy Event ID", menu)
        act_copy_id.triggered.connect(lambda: self._copy_event_id(record))
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
            act_delete = QAction(f"Delete {len(targets)} Drafts", menu)
            act_delete.triggered.connect(
                lambda checked=False, ids=list(targets): self._request_delete(ids)
            )
        else:
            act_delete = QAction("Delete Draft", menu)
            act_delete.triggered.connect(
                lambda checked=False, ids=[identifier]: self._request_delete(ids)
            )
        menu.addAction(act_delete)
        return menu

    # -- lifetime ----------------------------------------------------------

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
        super().changeEvent(event)

    def closeEvent(self, event) -> None:
        self._preview.shutdown()
        super().closeEvent(event)

    def _copy_event_id(self, record: DraftRecord) -> None:
        clip = QApplication.clipboard()
        if clip is not None and record.event_id:
            clip.setText(record.event_id)
        # Notify the host so it can confirm in the status bar.
        self.copy_event_id.emit(record.event_id)
