"""Side-docked drafts panel — the editor's primary surface for NIP-37 drafts.

Layout, top to bottom:

  ┌──────────────────────────────────────────┐
  │  ▸ profile chip       Drafts    ↻  ×    │   header
  ├──────────────────────────────────────────┤
  │  [ My Drafts ] [ Feeds (soon) ]          │   segmented control
  ├──────────────────────────────────────────┤
  │  [ search… ]                  [ ⇅ ]      │   search + sort
  │  [All] [Notes] [Articles]                │   kind filter chips
  ├──────────────────────────────────────────┤
  │                                          │
  │   ⚿  Article title           A   2h     │   list rows
  │     Summary line…                       │
  │   ⚿  Another draft           N   1d     │
  │     First chars of body…                │
  │                                          │
  │   (empty state placeholder, or          │
  │    skeleton rows during initial load)   │
  │                                          │
  ├──────────────────────────────────────────┤
  │  4/5 relays · 7 decrypted               │   footer status
  └──────────────────────────────────────────┘

Cross-platform discipline:
  - All glyphs are Unicode codepoints (⚿, ⟲, ×, ⇅, A, N) so the panel
    renders identically on macOS / Windows / Linux without bundling
    fonts.
  - Uses Qt's built-in widgets exclusively (QListWidget + setItemWidget
    pattern) so the panel honours each platform's native scrollbar,
    selection highlight, and high-DPI behaviour for free.
  - No platform-specific code paths — `is_dark` is the only branching
    axis, matching the rest of the editor.

The panel is **dormant** when no profile is bound: ``set_active_profile(None)``
puts it in a quiet "not connected" state. It is the host (MainWindow)'s
job not to *show* the panel at all when there is no profile — but if
it does, the panel won't crash, it'll just show the disconnected state.
"""

from __future__ import annotations

import time
from typing import List, Optional

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QAction, QColor, QFontMetrics, QPalette
from PySide6.QtWidgets import (
    QApplication,
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
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..avatar_store import AvatarStore
from ..draft_store import DraftRecord, DraftState, DraftStore
from ..drafts import INNER_KIND_LONG_FORM, INNER_KIND_SHORT_NOTE
from ..profiles import Profile
from .avatar import (
    AVATAR_SIZE,
    CHIP_TOTAL_WIDTH,
    compose_chip_icon,
    pixmap_for_profile,
)


# Width hints. The host can resize through a QSplitter; these are the
# sensible defaults so the panel doesn't crowd the editor.
DEFAULT_PANEL_WIDTH: int = 320
MIN_PANEL_WIDTH: int = 260
MAX_PANEL_WIDTH: int = 520


# --------------------------------------------------------------------------- #
# Stylesheets                                                                 #
# --------------------------------------------------------------------------- #

_DARK_CSS = """
QFrame#drafts_panel {
    background: #1E1E1E;
    border-left: 1px solid #2D2D30;
}
QFrame#drafts_panel_header,
QFrame#drafts_panel_segment_row,
QFrame#drafts_panel_filter_row,
QFrame#drafts_panel_footer {
    background: #252526;
    border: none;
}
QFrame#drafts_panel_header { border-bottom: 1px solid #3C3C3C; }
QFrame#drafts_panel_footer { border-top: 1px solid #3C3C3C; }

QLabel#drafts_panel_heading { color: #CCCCCC; font-size: 12px; font-weight: 500; }
QLabel#drafts_panel_footer_text { color: #858585; font-size: 11px; }

QToolButton#drafts_panel_icon_btn {
    background: transparent;
    color: #CCCCCC;
    border: none;
    padding: 4px 8px;
    border-radius: 4px;
    font-size: 14px;
}
QToolButton#drafts_panel_icon_btn:hover { background: #3C3C3C; }
QToolButton#drafts_panel_icon_btn:pressed { background: #1E1E1E; }
QToolButton#drafts_panel_icon_btn:disabled { color: #555555; }

QPushButton#drafts_panel_segment {
    background: transparent;
    color: #858585;
    border: 1px solid #3C3C3C;
    padding: 4px 14px;
    font-size: 12px;
}
QPushButton#drafts_panel_segment:checked {
    background: #1E1E1E;
    color: #FFFFFF;
}
QPushButton#drafts_panel_segment:hover:!disabled {
    color: #D4D4D4;
}
QPushButton#drafts_panel_segment:disabled { color: #555555; }

QLineEdit#drafts_panel_search {
    background: #1E1E1E;
    color: #D4D4D4;
    border: 1px solid #3C3C3C;
    border-radius: 4px;
    padding: 5px 8px;
    selection-background-color: #264F78;
    font-size: 12px;
}
QLineEdit#drafts_panel_search:focus { border-color: #007ACC; }

QPushButton#drafts_panel_chip {
    background: transparent;
    color: #858585;
    border: 1px solid #3C3C3C;
    padding: 3px 10px;
    border-radius: 11px;
    font-size: 11px;
}
QPushButton#drafts_panel_chip:checked {
    background: #094771;
    color: #FFFFFF;
    border-color: #007ACC;
}
QPushButton#drafts_panel_chip:hover:!checked { color: #D4D4D4; }

QListWidget#drafts_panel_list {
    background: #1E1E1E;
    border: none;
    outline: 0;
}
QListWidget#drafts_panel_list::item { border: none; }
QListWidget#drafts_panel_list::item:selected { background: #094771; }
QListWidget#drafts_panel_list::item:hover { background: #2A2D2E; }

QLabel#drafts_panel_empty_title { color: #CCCCCC; font-size: 13px; font-weight: 500; }
QLabel#drafts_panel_empty_body { color: #858585; font-size: 11px; }

QLabel#drafts_row_title { color: #FFFFFF; font-size: 13px; font-weight: 500; }
QLabel#drafts_row_title[failed="true"] { color: #B5B5B5; font-style: italic; }
QLabel#drafts_row_snippet { color: #858585; font-size: 11px; }
QLabel#drafts_row_meta { color: #858585; font-size: 11px; }
QLabel#drafts_row_kind {
    color: #CCCCCC;
    background: #3C3C3C;
    border-radius: 8px;
    padding: 0 6px;
    font-size: 10px;
    font-weight: 600;
}
QLabel#drafts_row_lock { color: #6F9F4F; font-size: 12px; }
QLabel#drafts_row_lock[failed="true"] { color: #C09060; }
"""

_LIGHT_CSS = """
QFrame#drafts_panel {
    background: #FFFFFF;
    border-left: 1px solid #E1E1E1;
}
QFrame#drafts_panel_header,
QFrame#drafts_panel_segment_row,
QFrame#drafts_panel_filter_row,
QFrame#drafts_panel_footer {
    background: #F8F8F8;
    border: none;
}
QFrame#drafts_panel_header { border-bottom: 1px solid #E1E1E1; }
QFrame#drafts_panel_footer { border-top: 1px solid #E1E1E1; }

QLabel#drafts_panel_heading { color: #333333; font-size: 12px; font-weight: 500; }
QLabel#drafts_panel_footer_text { color: #777777; font-size: 11px; }

QToolButton#drafts_panel_icon_btn {
    background: transparent;
    color: #555555;
    border: none;
    padding: 4px 8px;
    border-radius: 4px;
    font-size: 14px;
}
QToolButton#drafts_panel_icon_btn:hover { background: #E1E1E1; }
QToolButton#drafts_panel_icon_btn:pressed { background: #D0D0D0; }
QToolButton#drafts_panel_icon_btn:disabled { color: #BBBBBB; }

QPushButton#drafts_panel_segment {
    background: transparent;
    color: #777777;
    border: 1px solid #CCCCCC;
    padding: 4px 14px;
    font-size: 12px;
}
QPushButton#drafts_panel_segment:checked {
    background: #FFFFFF;
    color: #1A1A1A;
}
QPushButton#drafts_panel_segment:hover:!disabled { color: #333333; }
QPushButton#drafts_panel_segment:disabled { color: #BBBBBB; }

QLineEdit#drafts_panel_search {
    background: #FFFFFF;
    color: #333333;
    border: 1px solid #CCCCCC;
    border-radius: 4px;
    padding: 5px 8px;
    selection-background-color: #0078D4;
    font-size: 12px;
}
QLineEdit#drafts_panel_search:focus { border-color: #0078D4; }

QPushButton#drafts_panel_chip {
    background: transparent;
    color: #555555;
    border: 1px solid #CCCCCC;
    padding: 3px 10px;
    border-radius: 11px;
    font-size: 11px;
}
QPushButton#drafts_panel_chip:checked {
    background: #DCEEFA;
    color: #084D80;
    border-color: #0078D4;
}
QPushButton#drafts_panel_chip:hover:!checked { color: #333333; }

QListWidget#drafts_panel_list {
    background: #FFFFFF;
    border: none;
    outline: 0;
}
QListWidget#drafts_panel_list::item { border: none; }
QListWidget#drafts_panel_list::item:selected { background: #DCEEFA; }
QListWidget#drafts_panel_list::item:hover { background: #F3F3F3; }

QLabel#drafts_panel_empty_title { color: #333333; font-size: 13px; font-weight: 500; }
QLabel#drafts_panel_empty_body { color: #777777; font-size: 11px; }

QLabel#drafts_row_title { color: #1A1A1A; font-size: 13px; font-weight: 500; }
QLabel#drafts_row_title[failed="true"] { color: #777777; font-style: italic; }
QLabel#drafts_row_snippet { color: #777777; font-size: 11px; }
QLabel#drafts_row_meta { color: #999999; font-size: 11px; }
QLabel#drafts_row_kind {
    color: #555555;
    background: #E1E1E1;
    border-radius: 8px;
    padding: 0 6px;
    font-size: 10px;
    font-weight: 600;
}
QLabel#drafts_row_lock { color: #4A8A2C; font-size: 12px; }
QLabel#drafts_row_lock[failed="true"] { color: #A05000; }
"""

_DARK_MENU_CSS = """
QMenu { background: #252526; color: #CCCCCC; border: 1px solid #3C3C3C; padding: 4px; }
QMenu::item { padding: 4px 20px 4px 14px; }
QMenu::item:selected { background: #1E1E1E; color: #FFFFFF; }
QMenu::separator { height: 1px; background: #3C3C3C; margin: 4px 0px; }
"""

_LIGHT_MENU_CSS = """
QMenu { background: #F8F8F8; color: #333333; border: 1px solid #E1E1E1; padding: 4px; }
QMenu::item { padding: 4px 20px 4px 14px; }
QMenu::item:selected { background: #F3F3F3; color: #000000; }
QMenu::separator { height: 1px; background: #E1E1E1; margin: 4px 0px; }
"""


# --------------------------------------------------------------------------- #
# Filter / sort enums                                                         #
# --------------------------------------------------------------------------- #

# Kind filter values match the inner-kind ints plus a sentinel for "all".
_FILTER_ALL = -1


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #

def _format_relative_time(ts: int, *, now: Optional[int] = None) -> str:
    """Return a compact human-readable age, e.g. ``'2h'``, ``'3d'``.

    Stays small enough for a list-row badge — full timestamps belong in
    a tooltip, not the row. Falls through to ``'just now'`` for entries
    less than a minute old.
    """
    if ts <= 0:
        return ""
    now = now if now is not None else int(time.time())
    delta = max(0, now - int(ts))
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{delta // 60}m"
    if delta < 86_400:
        return f"{delta // 3600}h"
    if delta < 86_400 * 30:
        return f"{delta // 86_400}d"
    if delta < 86_400 * 365:
        return f"{delta // (86_400 * 30)}mo"
    return f"{delta // (86_400 * 365)}y"


def _kind_label(kind: int) -> str:
    if kind == INNER_KIND_SHORT_NOTE:
        return "Note"
    if kind == INNER_KIND_LONG_FORM:
        return "Article"
    return "?"


# --------------------------------------------------------------------------- #
# Row widget                                                                  #
# --------------------------------------------------------------------------- #

class _DraftRowWidget(QWidget):
    """One row in the drafts list.

    Composed as ``[lock] title  [kind][time]\n      snippet``. Failed
    decryption rows tint the lock orange and italicise the title.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(2)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(6)
        self._lock = QLabel("⚿")
        self._lock.setObjectName("drafts_row_lock")
        self._lock.setProperty("failed", "false")
        self._lock.setFixedWidth(14)
        top.addWidget(self._lock, 0, Qt.AlignVCenter)

        self._title = QLabel("")
        self._title.setObjectName("drafts_row_title")
        self._title.setProperty("failed", "false")
        # Single-line, elided in the middle so both the start and end
        # of long titles stay readable. We do the elision manually in
        # ``set_record`` since QLabel doesn't expose it directly.
        self._title.setTextInteractionFlags(Qt.NoTextInteraction)
        top.addWidget(self._title, 1, Qt.AlignVCenter)

        self._kind = QLabel("")
        self._kind.setObjectName("drafts_row_kind")
        self._kind.setAlignment(Qt.AlignCenter)
        self._kind.setFixedHeight(16)
        top.addWidget(self._kind, 0, Qt.AlignVCenter)

        self._time = QLabel("")
        self._time.setObjectName("drafts_row_meta")
        self._time.setMinimumWidth(36)
        self._time.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        top.addWidget(self._time, 0, Qt.AlignVCenter)

        layout.addLayout(top)

        self._snippet = QLabel("")
        self._snippet.setObjectName("drafts_row_snippet")
        self._snippet.setTextInteractionFlags(Qt.NoTextInteraction)
        self._snippet.setFixedHeight(16)
        layout.addWidget(self._snippet)

        self.setFixedHeight(54)

    def set_record(self, record: DraftRecord) -> None:
        # Title: fall back to a sensible placeholder for not-yet-decrypted
        # rows; failed rows are styled in italic + muted.
        failed = record.state is DraftState.FAILED
        loading = record.state is DraftState.LOADING

        if loading:
            display_title = "Decrypting…"
        elif record.title:
            display_title = record.title
        else:
            display_title = "(no title)"

        # Elide the title to the available width so the row never wraps.
        fm = QFontMetrics(self._title.font())
        elided = fm.elidedText(display_title, Qt.ElideRight, self._title.width() or 180)
        self._title.setText(elided)
        self._title.setToolTip(display_title)

        self._title.setProperty("failed", "true" if (failed or loading) else "false")
        self._lock.setProperty("failed", "true" if failed else "false")
        # Re-evaluate stylesheet on the labels affected by the property change.
        for w in (self._title, self._lock):
            w.style().unpolish(w)
            w.style().polish(w)

        if loading:
            self._snippet.setText("…")
        elif failed:
            self._snippet.setText(record.failure_reason or "Could not decrypt")
        else:
            self._snippet.setText(record.snippet or "")
        self._snippet.setToolTip(self._snippet.text())

        self._kind.setText(_kind_label(record.inner_kind))
        self._time.setText(_format_relative_time(record.created_at))


# --------------------------------------------------------------------------- #
# Drafts panel                                                                #
# --------------------------------------------------------------------------- #

class DraftsPanel(QFrame):
    """Side-docked drafts surface.

    Public signals (the host wires these into MainWindow handlers):
      open_draft(str)             — identifier of a draft to open in a new tab
      publish_draft(str)          — identifier to promote draft → real publish
      delete_draft(str, int)      — (identifier, inner_kind) to tombstone
      copy_event_id(str)          — copy outer wrap event id to clipboard
      switch_profile_requested()  — clicked the profile chip
      refresh_requested()         — manual refresh tap on the header
      close_requested()           — × on the header

    Public methods:
      bind_store(store)           — connect to a DraftStore instance
      set_active_profile(p)       — refresh the chip header
      set_avatar_store(s)         — wire avatars
      set_status(text)            — update the footer line
      set_signer_unsupported(bool)— show the "signer lacks NIP-44" footer
      apply_theme(is_dark)        — switch dark/light
    """

    open_draft = Signal(str)
    publish_draft = Signal(str)
    delete_draft = Signal(str, int)
    copy_event_id = Signal(str)
    switch_profile_requested = Signal()
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
        self._avatar_store: Optional[AvatarStore] = None
        self._search_text: str = ""
        self._kind_filter: int = _FILTER_ALL
        self._signer_unsupported: bool = False
        # Maps draft identifier → QListWidgetItem so signal updates can
        # find their row without scanning.
        self._items: dict[str, QListWidgetItem] = {}

        self._build_ui()
        self.apply_theme(is_dark)
        self._refresh_empty_state()
        self._refresh_footer()

    # -- construction ------------------------------------------------------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        outer.addWidget(self._build_header())
        outer.addWidget(self._build_segment_row())
        outer.addWidget(self._build_filter_row())
        outer.addWidget(self._build_body(), 1)
        outer.addWidget(self._build_footer())

    def _build_header(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("drafts_panel_header")
        frame.setFixedHeight(44)
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(8, 4, 4, 4)
        layout.setSpacing(6)

        self._profile_chip = QToolButton()
        self._profile_chip.setObjectName("drafts_panel_icon_btn")
        self._profile_chip.setCursor(Qt.PointingHandCursor)
        self._profile_chip.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self._profile_chip.setIconSize(QSize(CHIP_TOTAL_WIDTH, AVATAR_SIZE))
        self._profile_chip.setText("  Not connected")
        self._profile_chip.setToolTip("Switch Nostr profile")
        self._profile_chip.clicked.connect(self.switch_profile_requested.emit)
        layout.addWidget(self._profile_chip, 1, Qt.AlignVCenter)

        layout.addStretch(1)

        self._refresh_btn = QToolButton()
        self._refresh_btn.setObjectName("drafts_panel_icon_btn")
        self._refresh_btn.setText("⟲")
        self._refresh_btn.setToolTip("Refresh drafts")
        self._refresh_btn.setCursor(Qt.PointingHandCursor)
        self._refresh_btn.clicked.connect(self.refresh_requested.emit)
        layout.addWidget(self._refresh_btn)

        self._close_btn = QToolButton()
        self._close_btn.setObjectName("drafts_panel_icon_btn")
        self._close_btn.setText("×")
        self._close_btn.setToolTip("Close drafts panel")
        self._close_btn.setCursor(Qt.PointingHandCursor)
        self._close_btn.clicked.connect(self.close_requested.emit)
        layout.addWidget(self._close_btn)

        return frame

    def _build_segment_row(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("drafts_panel_segment_row")
        frame.setFixedHeight(40)
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(0)

        # Use a button group with the cosmetic ``checked`` look — flat
        # buttons reading as a segmented control. ``flat="true"``
        # property + matching QSS gives us a native-feeling cluster.
        self._seg_drafts = QPushButton("My Drafts")
        self._seg_drafts.setObjectName("drafts_panel_segment")
        self._seg_drafts.setCheckable(True)
        self._seg_drafts.setChecked(True)
        self._seg_drafts.setCursor(Qt.PointingHandCursor)
        # Round the outer corners only on the segment ends.
        self._seg_drafts.setStyleSheet(
            "QPushButton#drafts_panel_segment {"
            "  border-top-left-radius: 4px;"
            "  border-bottom-left-radius: 4px;"
            "  border-right: none;"
            "}"
        )
        self._seg_feeds = QPushButton("Feeds")
        self._seg_feeds.setObjectName("drafts_panel_segment")
        self._seg_feeds.setCheckable(True)
        self._seg_feeds.setEnabled(False)
        self._seg_feeds.setToolTip("Coming soon — RSS feeds imported as private drafts")
        self._seg_feeds.setStyleSheet(
            "QPushButton#drafts_panel_segment {"
            "  border-top-right-radius: 4px;"
            "  border-bottom-right-radius: 4px;"
            "}"
        )

        group = QButtonGroup(frame)
        group.setExclusive(True)
        group.addButton(self._seg_drafts, 0)
        group.addButton(self._seg_feeds, 1)

        layout.addStretch(1)
        layout.addWidget(self._seg_drafts)
        layout.addWidget(self._seg_feeds)
        layout.addStretch(1)
        return frame

    def _build_filter_row(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("drafts_panel_filter_row")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(8, 4, 8, 6)
        layout.setSpacing(6)

        self._search_edit = QLineEdit()
        self._search_edit.setObjectName("drafts_panel_search")
        self._search_edit.setPlaceholderText("Search drafts…")
        self._search_edit.setClearButtonEnabled(True)
        self._search_edit.textChanged.connect(self._on_search_changed)
        layout.addWidget(self._search_edit)

        chip_row = QHBoxLayout()
        chip_row.setContentsMargins(0, 0, 0, 0)
        chip_row.setSpacing(6)
        self._chip_all = self._make_chip("All", _FILTER_ALL, checked=True)
        self._chip_notes = self._make_chip("Notes", INNER_KIND_SHORT_NOTE)
        self._chip_articles = self._make_chip("Articles", INNER_KIND_LONG_FORM)

        chip_group = QButtonGroup(frame)
        chip_group.setExclusive(True)
        for chip in (self._chip_all, self._chip_notes, self._chip_articles):
            chip_group.addButton(chip)
            chip_row.addWidget(chip)
        chip_row.addStretch(1)
        layout.addLayout(chip_row)
        return frame

    def _make_chip(self, label: str, value: int, *, checked: bool = False) -> QPushButton:
        chip = QPushButton(label)
        chip.setObjectName("drafts_panel_chip")
        chip.setCheckable(True)
        chip.setChecked(checked)
        chip.setCursor(Qt.PointingHandCursor)
        chip.setFlat(True)
        chip.toggled.connect(lambda on, v=value: on and self._set_kind_filter(v))
        return chip

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
        self._list.setSelectionMode(QListWidget.SingleSelection)
        self._list.itemActivated.connect(self._on_item_activated)
        self._list.setContextMenuPolicy(Qt.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._on_context_menu)
        self._body_stack.addWidget(self._list)

        # Empty / disconnected / unsupported state
        self._empty_widget = QWidget()
        empty_layout = QVBoxLayout(self._empty_widget)
        empty_layout.setContentsMargins(24, 32, 24, 32)
        empty_layout.setSpacing(8)
        empty_layout.addStretch(1)
        self._empty_title = QLabel("")
        self._empty_title.setObjectName("drafts_panel_empty_title")
        self._empty_title.setAlignment(Qt.AlignCenter)
        self._empty_title.setWordWrap(True)
        empty_layout.addWidget(self._empty_title)
        self._empty_body = QLabel("")
        self._empty_body.setObjectName("drafts_panel_empty_body")
        self._empty_body.setAlignment(Qt.AlignCenter)
        self._empty_body.setWordWrap(True)
        empty_layout.addWidget(self._empty_body)
        empty_layout.addStretch(2)
        self._body_stack.addWidget(self._empty_widget)
        return container

    def _build_footer(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("drafts_panel_footer")
        frame.setFixedHeight(24)
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(10, 0, 10, 0)
        layout.setSpacing(6)
        self._footer_text = QLabel("")
        self._footer_text.setObjectName("drafts_panel_footer_text")
        layout.addWidget(self._footer_text, 1)
        return frame

    # -- public API: theming ----------------------------------------------

    def apply_theme(self, is_dark: bool) -> None:
        self._is_dark = is_dark
        self.setStyleSheet(_DARK_CSS if is_dark else _LIGHT_CSS)
        if hasattr(self, "_list") and self._list is not None:
            self._list.viewport().update()
        # Re-poll any property-driven labels on existing rows so their
        # state-dependent styles repaint correctly after a theme switch.
        for i in range(self._list.count() if hasattr(self, "_list") else 0):
            widget = self._list.itemWidget(self._list.item(i))
            if isinstance(widget, _DraftRowWidget):
                for child in widget.findChildren(QLabel):
                    child.style().unpolish(child)
                    child.style().polish(child)

    # -- public API: data binding -----------------------------------------

    def bind_store(self, store: DraftStore) -> None:
        if self._store is store:
            return
        if self._store is not None:
            # Disconnect previous bindings — Qt allows this idiom by
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
        self._refresh_profile_chip()
        self._refresh_empty_state()

    def set_avatar_store(self, avatar_store: AvatarStore) -> None:
        if self._avatar_store is avatar_store:
            return
        if self._avatar_store is not None:
            try:
                self._avatar_store.avatar_added.disconnect(self._on_avatar_added)
            except (TypeError, RuntimeError):
                pass
        self._avatar_store = avatar_store
        if avatar_store is not None:
            avatar_store.avatar_added.connect(self._on_avatar_added)
        self._refresh_profile_chip()

    def set_status(self, text: str) -> None:
        # The footer is the primary status surface for sync state.
        if not self._signer_unsupported:
            self._footer_text.setText(text)

    def set_signer_unsupported(self, unsupported: bool) -> None:
        self._signer_unsupported = unsupported
        self._refresh_footer()
        self._refresh_empty_state()

    # -- chip + footer refresh --------------------------------------------

    def _refresh_profile_chip(self) -> None:
        profile = self._active_profile
        if profile is None:
            self._profile_chip.setIcon(compose_chip_icon(
                pixmap_for_profile("", "", None, size=AVATAR_SIZE),
                QColor("#CCCCCC") if self._is_dark else QColor("#555555"),
            ))
            self._profile_chip.setText("  Not connected")
            self._profile_chip.setToolTip("No Nostr profile connected")
            return
        pix = None
        if self._avatar_store is not None:
            pix = self._avatar_store.get(profile.user_pubkey)
        avatar = pixmap_for_profile(
            profile.display_name, profile.user_pubkey, pix, size=AVATAR_SIZE,
        )
        chevron = QColor("#CCCCCC") if self._is_dark else QColor("#555555")
        self._profile_chip.setIcon(compose_chip_icon(avatar, chevron))
        label = profile.display_name or profile.npub_short()
        # Truncate so the chip never elbows the action buttons out.
        if len(label) > 22:
            label = label[:21].rstrip() + "…"
        self._profile_chip.setText(f"  {label}")
        self._profile_chip.setToolTip(
            f"{profile.display_name or 'Profile'} · {profile.npub_short()}"
        )

    def _refresh_footer(self) -> None:
        if self._signer_unsupported:
            self._footer_text.setText("Signer can't decrypt drafts (no NIP-44)")
            return
        if self._store is None or len(self._store) == 0:
            self._footer_text.setText("")
            return
        ready = sum(1 for r in self._store if r.state is DraftState.READY)
        total = len(self._store)
        self._footer_text.setText(f"{ready}/{total} decrypted")

    def _refresh_empty_state(self) -> None:
        # Decide which body card to show: the real list, or the
        # placeholder for "no profile / no drafts / no NIP-44 signer".
        if self._active_profile is None:
            self._empty_title.setText("Connect a Nostr profile")
            self._empty_body.setText(
                "Drafts are end-to-end encrypted to your Nostr key. "
                "Connect a profile from the avatar chip to view, search, "
                "and create drafts."
            )
            self._body_stack.setCurrentIndex(1)
            return
        if self._signer_unsupported:
            self._empty_title.setText("Signer doesn't support NIP-44")
            self._empty_body.setText(
                "This profile's signer can't decrypt drafts. Connect a "
                "NIP-44-capable signer (Amber, nsec.app) to use drafts."
            )
            self._body_stack.setCurrentIndex(1)
            return
        if self._store is None or len(self._store) == 0:
            self._empty_title.setText("No private drafts yet")
            self._empty_body.setText(
                "Press Ctrl+Shift+S in any tab to save its contents as an "
                "encrypted draft on Nostr. Drafts sync to your other devices "
                "signed in with the same profile."
            )
            self._body_stack.setCurrentIndex(1)
            return
        self._body_stack.setCurrentIndex(0)

    # -- list rebuild + filter --------------------------------------------

    def _rebuild_list(self) -> None:
        self._list.clear()
        self._items.clear()
        if self._store is None:
            self._refresh_empty_state()
            self._refresh_footer()
            return
        for record in self._store.all():
            if self._passes_filter(record):
                self._insert_row(record)
        self._refresh_empty_state()
        self._refresh_footer()

    def _passes_filter(self, record: DraftRecord) -> bool:
        if self._kind_filter != _FILTER_ALL and record.inner_kind != self._kind_filter:
            return False
        if self._search_text:
            needle = self._search_text.lower()
            haystack = (record.title + " " + record.snippet).lower()
            if needle not in haystack:
                return False
        return True

    def _insert_row(self, record: DraftRecord, *, at_index: Optional[int] = None) -> None:
        item = QListWidgetItem()
        item.setData(Qt.UserRole, record.identifier)
        item.setSizeHint(QSize(0, 54))
        widget = _DraftRowWidget()
        widget.set_record(record)
        if at_index is None:
            self._list.addItem(item)
        else:
            self._list.insertItem(at_index, item)
        self._list.setItemWidget(item, widget)
        self._items[record.identifier] = item

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
        # to re-look-up the position from store.all() — N is small.
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
        self._refresh_empty_state()
        self._refresh_footer()

    def _on_record_changed(self, identifier: str) -> None:
        # The change may have made the row newly-pass or newly-fail the
        # current filter — recompute and adjust if needed.
        if self._store is None:
            return
        record = self._store.get(identifier)
        if record is None:
            return
        present = identifier in self._items
        passes = self._passes_filter(record)
        if present and not passes:
            self._remove_row(identifier)
        elif not present and passes:
            self._on_record_added(identifier)  # treat as fresh insertion
        else:
            self._update_row(identifier)
        self._refresh_footer()

    def _on_record_removed(self, identifier: str) -> None:
        self._remove_row(identifier)
        self._refresh_empty_state()
        self._refresh_footer()

    def _on_store_cleared(self) -> None:
        self._list.clear()
        self._items.clear()
        self._refresh_empty_state()
        self._refresh_footer()

    def _on_loading_changed(self, loading: bool) -> None:
        # The footer carries loading state from DraftSync — but if the
        # caller forgot to wire status_changed we still want some hint.
        if loading and self._signer_unsupported is False and not self._footer_text.text():
            self._footer_text.setText("Loading…")

    def _on_avatar_added(self, pubkey_hex: str, _pixmap) -> None:
        if self._active_profile and pubkey_hex == self._active_profile.user_pubkey:
            self._refresh_profile_chip()

    # -- search / filter handlers -----------------------------------------

    def _on_search_changed(self, text: str) -> None:
        self._search_text = text
        self._rebuild_list()

    def _set_kind_filter(self, value: int) -> None:
        if value == self._kind_filter:
            return
        self._kind_filter = value
        self._rebuild_list()

    # -- list interaction --------------------------------------------------

    def _on_item_activated(self, item: QListWidgetItem) -> None:
        identifier = item.data(Qt.UserRole)
        if isinstance(identifier, str) and identifier:
            self.open_draft.emit(identifier)

    def _on_context_menu(self, pos) -> None:
        item = self._list.itemAt(pos)
        if item is None:
            return
        identifier = item.data(Qt.UserRole)
        if not isinstance(identifier, str) or self._store is None:
            return
        record = self._store.get(identifier)
        if record is None:
            return
        menu = QMenu(self._list)
        menu.setStyleSheet(_DARK_MENU_CSS if self._is_dark else _LIGHT_MENU_CSS)

        act_open = QAction("Open in new tab", menu)
        act_open.triggered.connect(lambda: self.open_draft.emit(identifier))
        act_open.setEnabled(record.state is DraftState.READY)
        menu.addAction(act_open)

        act_publish = QAction("Publish…", menu)
        act_publish.triggered.connect(lambda: self.publish_draft.emit(identifier))
        act_publish.setEnabled(record.state is DraftState.READY)
        menu.addAction(act_publish)

        menu.addSeparator()
        act_copy_id = QAction("Copy event id", menu)
        act_copy_id.triggered.connect(lambda: self._copy_event_id(record))
        act_copy_id.setEnabled(bool(record.event_id))
        menu.addAction(act_copy_id)

        menu.addSeparator()
        act_delete = QAction("Delete draft", menu)
        act_delete.triggered.connect(
            lambda: self.delete_draft.emit(identifier, record.inner_kind)
        )
        menu.addAction(act_delete)

        menu.exec(self._list.mapToGlobal(pos))

    def _copy_event_id(self, record: DraftRecord) -> None:
        clip = QApplication.clipboard()
        if clip is not None and record.event_id:
            clip.setText(record.event_id)
        # Notify the host so it can confirm in the status bar.
        self.copy_event_id.emit(record.event_id)
