"""Keyboard-shortcuts cheat sheet — modal dialog with a "mega menu" feel.

Style brief:
  - Category cards laid out in two responsive columns.
  - Each row reads ``[kbd-style key caps]  [action label]`` so the
    shortcut is the first thing the eye lands on.
  - A live search field at the top filters across all categories at
    once; an empty result hides the corresponding card so the layout
    doesn't have awkward gaps.
  - Theme-aware (dark + light pair) and Unicode-only glyphs so the
    dialog renders identically on macOS, Windows, and Linux.

The dialog is a static, declarative description of the editor's
shortcut surface. Editing the shortcuts in code does not need to know
about this file; we only update ``SHORTCUT_GROUPS`` below when a new
binding lands.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)


# --------------------------------------------------------------------------- #
# Shortcut catalogue — single source of truth for the help dialog             #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Shortcut:
    keys: str       # the rendered key combo, e.g. "Ctrl+Shift+D"
    action: str     # human-readable description


@dataclass(frozen=True)
class ShortcutGroup:
    title: str
    items: Tuple[Shortcut, ...]


SHORTCUT_GROUPS: Tuple[ShortcutGroup, ...] = (
    ShortcutGroup(
        title="File",
        items=(
            Shortcut("Ctrl+N",          "New tab"),
            Shortcut("Ctrl+O",          "Open file"),
            Shortcut("Ctrl+S",          "Save"),
            Shortcut("Ctrl+Shift+S",    "Save As"),
            Shortcut("Ctrl+W",          "Close tab"),
            Shortcut("Ctrl+Q",          "Quit"),
        ),
    ),
    ShortcutGroup(
        title="Editing",
        items=(
            Shortcut("Ctrl+Z",          "Undo"),
            Shortcut("Ctrl+Y",          "Redo"),
            Shortcut("Ctrl+Shift+Z",    "Redo"),
            Shortcut("Tab",             "Indent"),
            Shortcut("Shift+Tab",       "Outdent"),
            Shortcut("Enter",           "New line"),
        ),
    ),
    ShortcutGroup(
        title="Formatting",
        items=(
            Shortcut("Ctrl+B",          "Bold"),
            Shortcut("Ctrl+I",          "Italic"),
            Shortcut("Ctrl+U",          "Underline"),
            Shortcut("Ctrl+D",          "Reset to default format"),
        ),
    ),
    ShortcutGroup(
        title="Search",
        items=(
            Shortcut("Ctrl+F",          "Find"),
            Shortcut("F3",              "Find next"),
            Shortcut("Shift+F3",        "Find previous"),
            Shortcut("Enter",           "Next match"),
            Shortcut("Shift+Enter",     "Previous match"),
            Shortcut("Esc",             "Close find bar"),
        ),
    ),
    ShortcutGroup(
        title="View",
        items=(
            Shortcut("Ctrl+Shift+T",    "Toggle theme"),
            Shortcut("Ctrl+Shift+L",    "Toggle line numbers"),
            Shortcut("Ctrl+Shift+H",    "Toggle syntax highlighting"),
        ),
    ),
    ShortcutGroup(
        title="Nostr",
        items=(
            Shortcut("Ctrl+Shift+P",    "Publish as note"),
            Shortcut("Ctrl+Shift+A",    "Publish as article"),
            Shortcut("Ctrl+Shift+D",    "Toggle Drafts panel"),
            Shortcut("Ctrl+Shift+S",    "Save As (local or draft)"),
        ),
    ),
)


# --------------------------------------------------------------------------- #
# Stylesheets                                                                 #
# --------------------------------------------------------------------------- #

_DARK_CSS = """
QDialog#shortcuts_dialog { background: #1E1E1E; }
QScrollArea#shortcuts_scroll { background: transparent; border: none; }
QScrollArea#shortcuts_scroll > QWidget > QWidget { background: transparent; }

QLabel#shortcuts_title { color: #FFFFFF; font-size: 16px; font-weight: 600; }
QLabel#shortcuts_subtitle { color: #858585; font-size: 12px; }
QLabel#shortcuts_no_results {
    color: #858585; font-size: 12px; font-style: italic;
    padding: 24px 0;
}

QLineEdit#shortcuts_search {
    background: #252526;
    color: #D4D4D4;
    border: 1px solid #3C3C3C;
    border-radius: 4px;
    padding: 6px 10px;
    selection-background-color: #264F78;
    font-size: 12px;
}
QLineEdit#shortcuts_search:focus { border-color: #007ACC; }

QFrame#shortcut_card {
    background: #252526;
    border: 1px solid #3C3C3C;
    border-radius: 6px;
}
QLabel#shortcut_card_title {
    color: #FFFFFF;
    font-size: 12px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.7px;
}
QLabel#shortcut_action {
    color: #D4D4D4;
    font-size: 12px;
    padding: 2px 0;
}
QLabel#shortcut_keycap {
    background: #1E1E1E;
    color: #DCDCDC;
    border: 1px solid #3C3C3C;
    border-bottom: 2px solid #2D2D30;
    border-radius: 4px;
    padding: 2px 6px;
    font-family: "Noto Sans Mono", "Menlo", "Consolas", monospace;
    font-size: 11px;
}

QPushButton {
    background: #2D2D30;
    color: #D4D4D4;
    border: 1px solid #3C3C3C;
    padding: 6px 14px;
    border-radius: 4px;
    min-width: 80px;
}
QPushButton:hover { background: #3C3C3C; }
QPushButton:pressed { background: #1E1E1E; }
QPushButton:default { background: #007ACC; color: #FFFFFF; border-color: #1177C7; }
QPushButton:default:hover { background: #1177C7; }
"""

_LIGHT_CSS = """
QDialog#shortcuts_dialog { background: #FFFFFF; }
QScrollArea#shortcuts_scroll { background: transparent; border: none; }
QScrollArea#shortcuts_scroll > QWidget > QWidget { background: transparent; }

QLabel#shortcuts_title { color: #1A1A1A; font-size: 16px; font-weight: 600; }
QLabel#shortcuts_subtitle { color: #777777; font-size: 12px; }
QLabel#shortcuts_no_results {
    color: #777777; font-size: 12px; font-style: italic;
    padding: 24px 0;
}

QLineEdit#shortcuts_search {
    background: #FFFFFF;
    color: #333333;
    border: 1px solid #CCCCCC;
    border-radius: 4px;
    padding: 6px 10px;
    selection-background-color: #0078D4;
    font-size: 12px;
}
QLineEdit#shortcuts_search:focus { border-color: #0078D4; }

QFrame#shortcut_card {
    background: #F8F8F8;
    border: 1px solid #E1E1E1;
    border-radius: 6px;
}
QLabel#shortcut_card_title {
    color: #1A1A1A;
    font-size: 12px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.7px;
}
QLabel#shortcut_action {
    color: #333333;
    font-size: 12px;
    padding: 2px 0;
}
QLabel#shortcut_keycap {
    background: #FFFFFF;
    color: #1A1A1A;
    border: 1px solid #CCCCCC;
    border-bottom: 2px solid #BBBBBB;
    border-radius: 4px;
    padding: 2px 6px;
    font-family: "Noto Sans Mono", "Menlo", "Consolas", monospace;
    font-size: 11px;
}

QPushButton {
    background: #ECECEC;
    color: #333333;
    border: 1px solid #CCCCCC;
    padding: 6px 14px;
    border-radius: 4px;
    min-width: 80px;
}
QPushButton:hover { background: #E1E1E1; }
QPushButton:pressed { background: #D0D0D0; }
QPushButton:default { background: #0078D4; color: #FFFFFF; border-color: #1066B4; }
QPushButton:default:hover { background: #1066B4; }
"""


# --------------------------------------------------------------------------- #
# Internal widgets                                                            #
# --------------------------------------------------------------------------- #

def _make_keycap_row(keys: str) -> QWidget:
    """Render a key combo as a row of pill-shaped key caps.

    "Ctrl+Shift+D" becomes three caps with thin "+" separators in
    between — closer to how Apple's Help shortcuts viewer or Sublime's
    cheat sheet display key combos than a single bold string.
    """
    container = QWidget()
    layout = QHBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(3)
    parts = [p.strip() for p in keys.split("+") if p.strip()]
    for i, part in enumerate(parts):
        if i:
            plus = QLabel("+")
            plus.setObjectName("shortcut_action")
            plus.setAlignment(Qt.AlignCenter)
            layout.addWidget(plus)
        cap = QLabel(part)
        cap.setObjectName("shortcut_keycap")
        cap.setAlignment(Qt.AlignCenter)
        layout.addWidget(cap)
    layout.addStretch(1)
    return container


class _ShortcutCard(QFrame):
    """One titled group of shortcuts. Hides itself when its row count
    drops to zero (used by the search filter)."""

    def __init__(self, group: ShortcutGroup, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("shortcut_card")
        self.setFrameShape(QFrame.NoFrame)
        self._group = group
        # Tracked separately from Qt's ``isHidden()`` because that
        # property reports True for any widget that hasn't yet been
        # shown — making it useless for "should this card take a grid
        # slot?" decisions during initial construction.
        self._filtered_out = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 14)
        outer.setSpacing(10)

        header = QLabel(group.title)
        header.setObjectName("shortcut_card_title")
        outer.addWidget(header)

        # Two-column grid: keys on the left, action on the right. The
        # action column stretches, the keys column hugs its content.
        # Vertical spacing is generous so rows feel scannable rather
        # than stacked tight on top of each other.
        self._grid = QGridLayout()
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(14)
        self._grid.setVerticalSpacing(9)
        self._grid.setColumnStretch(0, 0)
        self._grid.setColumnStretch(1, 1)
        outer.addLayout(self._grid)

        self._rows: List[Tuple[QWidget, QLabel, Shortcut]] = []
        for shortcut in group.items:
            keycap = _make_keycap_row(shortcut.keys)
            action = QLabel(shortcut.action)
            action.setObjectName("shortcut_action")
            action.setWordWrap(True)
            row_idx = self._grid.rowCount()
            self._grid.addWidget(keycap, row_idx, 0, Qt.AlignTop | Qt.AlignLeft)
            self._grid.addWidget(action, row_idx, 1, Qt.AlignTop | Qt.AlignLeft)
            self._rows.append((keycap, action, shortcut))

    def apply_filter(self, needle: str) -> int:
        """Show only rows that contain ``needle`` (case-insensitive).

        Returns the number of visible rows so the dialog can hide the
        whole card when nothing matches.
        """
        needle = needle.strip().lower()
        visible = 0
        for keycap, action, shortcut in self._rows:
            text = f"{shortcut.keys} {shortcut.action}".lower()
            match = (not needle) or (needle in text)
            keycap.setVisible(match)
            action.setVisible(match)
            if match:
                visible += 1
        # Track whether this card should occupy a grid slot. The dialog
        # reflows after every filter change; cards with ``_filtered_out``
        # set are excluded from the layout so visible cards close ranks.
        self._filtered_out = visible == 0
        self.setVisible(not self._filtered_out)
        return visible


# --------------------------------------------------------------------------- #
# Dialog                                                                      #
# --------------------------------------------------------------------------- #

class ShortcutsDialog(QDialog):
    """Help → Keyboard Shortcuts.

    A modal cheat sheet styled like the keyboard-shortcuts pages found
    in major desktop applications: category cards laid out in two
    columns, kbd-style key caps, and a live search across all groups.
    """

    def __init__(self, *, is_dark: bool = True, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("shortcuts_dialog")
        self.setWindowTitle("Keyboard Shortcuts")
        self.setModal(True)
        # Wide-and-tall by default so all six categories fit without
        # scrolling on the common laptop resolutions. The dialog is
        # resizable so users on small screens can still see everything.
        self.resize(760, 620)
        self.setMinimumSize(560, 420)
        self._is_dark = is_dark
        self._cards: List[_ShortcutCard] = []
        self._build_ui()
        self.apply_theme(is_dark)

    # -- construction ------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 18)
        root.setSpacing(12)

        title = QLabel("Keyboard Shortcuts")
        title.setObjectName("shortcuts_title")
        root.addWidget(title)

        subtitle = QLabel(
            "Every binding the editor responds to. Type to filter; "
            "Ctrl maps to Command on macOS automatically."
        )
        subtitle.setObjectName("shortcuts_subtitle")
        subtitle.setWordWrap(True)
        root.addWidget(subtitle)

        self._search = QLineEdit()
        self._search.setObjectName("shortcuts_search")
        self._search.setPlaceholderText("Filter shortcuts… (e.g. 'draft', 'save', 'Ctrl+S')")
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._on_filter_changed)
        root.addWidget(self._search)

        # Scrollable area so a long category list never breaks the
        # dialog on small displays.
        scroll = QScrollArea()
        scroll.setObjectName("shortcuts_scroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        inner = QWidget()
        self._cards_grid = QGridLayout(inner)
        self._cards_grid.setContentsMargins(0, 0, 0, 0)
        self._cards_grid.setHorizontalSpacing(12)
        self._cards_grid.setVerticalSpacing(12)
        self._cards_grid.setColumnStretch(0, 1)
        self._cards_grid.setColumnStretch(1, 1)

        # Build every card up front; ``_reflow_cards`` decides where
        # each one sits in the two-column grid. Reflow runs on every
        # filter change so hidden cards don't leave holes in the layout.
        for group in SHORTCUT_GROUPS:
            self._cards.append(_ShortcutCard(group))
        self._reflow_cards()

        scroll.setWidget(inner)
        root.addWidget(scroll, 1)

        self._no_results = QLabel("No shortcuts match your filter.")
        self._no_results.setObjectName("shortcuts_no_results")
        self._no_results.setAlignment(Qt.AlignCenter)
        self._no_results.setVisible(False)
        root.addWidget(self._no_results)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        # Close is a "reject" role by default — wire its click to accept
        # so Enter / Esc both dismiss cleanly.
        close_btn = buttons.button(QDialogButtonBox.Close)
        close_btn.setDefault(True)
        close_btn.clicked.connect(self.accept)
        root.addWidget(buttons)

    # -- behaviour ---------------------------------------------------------

    def apply_theme(self, is_dark: bool) -> None:
        self._is_dark = is_dark
        self.setStyleSheet(_DARK_CSS if is_dark else _LIGHT_CSS)

    def _on_filter_changed(self, text: str) -> None:
        total_visible = 0
        for card in self._cards:
            total_visible += card.apply_filter(text)
        # Hidden cards left holes in the 2-column grid before — re-pack
        # so the survivors fill the layout sequentially from the top.
        self._reflow_cards()
        self._no_results.setVisible(total_visible == 0)

    def _reflow_cards(self) -> None:
        """Re-pack visible cards into a tight two-column grid.

        Qt's ``QGridLayout`` doesn't collapse empty cells when a child
        is hidden — it preserves grid positions and leaves visible
        gaps. Whenever the filter changes (or on first render) we
        remove every card from the grid and re-add only the ones that
        survived the filter, in sequential row-major order.
        """
        # Remove every card from its current cell. The widget objects
        # stay alive; we just clear the layout's record of their
        # positions so the next ``addWidget`` can place them fresh.
        for card in self._cards:
            self._cards_grid.removeWidget(card)
        # Drop any row stretches left over from a previous pack so a
        # smaller surviving set anchors at the top of the scroll area.
        for row in range(self._cards_grid.rowCount()):
            self._cards_grid.setRowStretch(row, 0)

        visible_cards = [c for c in self._cards if not c._filtered_out]
        for i, card in enumerate(visible_cards):
            row = i // 2
            col = i % 2
            self._cards_grid.addWidget(card, row, col, Qt.AlignTop)
        # Stretch the row just below the visible cards so survivors
        # anchor at the top of the scroll area instead of expanding to
        # fill it. Use ``rowCount()`` after re-adds for the right index.
        trailing_row = max(self._cards_grid.rowCount(), 1)
        self._cards_grid.setRowStretch(trailing_row, 1)
