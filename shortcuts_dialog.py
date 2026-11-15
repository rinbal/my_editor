# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Keyboard-shortcuts cheat sheet - modal dialog with a "mega menu" feel.

Style brief:
  - Category cards laid out in two responsive columns.
  - Each row reads ``[kbd-style key caps]  [action label]`` so the
    shortcut is the first thing the eye lands on.
  - A live search field at the top filters across all categories at
    once; an empty result hides the corresponding card so the layout
    doesn't have awkward gaps.
  - Theme-aware (dark + light pair) and Unicode-only glyphs so the
    dialog renders identically on macOS, Windows, and Linux.

The window's commands and their shortcuts come from the command list
(commands.py, see ``CommandRegistry.shortcut_groups``), so a new or
changed shortcut shows here by itself. Only keys that are not commands
(Tab, Esc, the PDF reader's keys) are listed in ``OTHER_KEYS`` below.

Keys are shown the way the computer in use writes them: on a Mac in
Apple's symbols and order (⌃⌥⇧⌘, as in menus), elsewhere as Ctrl+Shift+S
with the key names of the reader's keyboard (Strg, Umschalt in German).
The list is the one for this platform, since the commands' keys follow
each platform's conventions.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import List, Optional, Tuple

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
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

from i18n import _, pgettext
from pdf_viewer import reader_key_text


# --------------------------------------------------------------------------- #
# Shortcut catalogue - single source of truth for the help dialog             #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Shortcut:
    keys: str       # the rendered key combo, e.g. "Ctrl+Shift+D"
    action: str     # human-readable description


@dataclass(frozen=True)
class ShortcutGroup:
    title: str
    items: Tuple[Shortcut, ...]


# Keys that are not commands of the window: the editor, the find bar
# and the PDF reader handle them themselves. Everything that IS a command
# (a menu item with a shortcut) comes from the command list in
# commands.py, so the two can never disagree. Group names and keys are
# written the command list's way (English); the window translates them
# where it shows them.
OTHER_KEYS: Tuple[Tuple[str, Tuple[Tuple[str, str], ...]], ...] = (
    ("Editing", (
        ("Tab", _("Nest a list item, or start a list")),
        ("Shift+Tab", _("Move a list item out one level")),
        ("Enter", _("New paragraph (on an empty list item: end the list)")),
        ("Shift+Enter", _("Line break within the paragraph")),
        ("Ctrl+Click", _("Open a link")),
    )),
    ("Search", (
        ("Enter", _("Next match")),
        ("Shift+Enter", _("Previous match")),
        ("Esc", _("Close find bar")),
    )),
    ("PDF Reading", (
        ("Ctrl+F", _("Find in PDF")),
        ("Ctrl+C", _("Copy selected text")),
        ("Esc", _("Clear selection")),
        ("Space", _("Next screenful")),
        ("Shift+Space", _("Previous screenful")),
        ("J", _("Scroll down")),
        ("K", _("Scroll up")),
        ("N", _("Next page")),
        ("P", _("Previous page")),
        ("G", _("Go to page")),
        ("Home", _("First page")),
        ("End", _("Last page")),
        ("Ctrl+=", _("Zoom in (also Ctrl+wheel)")),
        ("Ctrl+-", _("Zoom out (also Ctrl+wheel)")),
        # The reader's zoom keys differ on a Mac (pdf_viewer.reader_keys).
        (reader_key_text(_("Fit page width")), _("Fit page width")),
        (reader_key_text(_("Actual size")), _("Actual size")),
        (reader_key_text(_("Fit whole page")), _("Fit whole page")),
        ("F12", _("Toggle table of contents")),
    )),
)

# A group's name on its card. Nostr is a name, the same in every language.
_GROUP_TITLES = {
    "File": pgettext("shortcut group", "File"),
    "Editing": pgettext("shortcut group", "Editing"),
    "Formatting": pgettext("shortcut group", "Formatting"),
    "Insert": pgettext("shortcut group", "Insert"),
    "Search": pgettext("shortcut group", "Search"),
    "View": pgettext("shortcut group", "View"),
    "Window": pgettext("shortcut group", "Window"),
    "Help": pgettext("shortcut group", "Help"),
    "PDF Reading": pgettext("shortcut group", "PDF Reading"),
}

# Keys as the reader's keyboard labels them (Strg and Umschalt on a
# German one). Letters, digits and F-keys read the same everywhere.
_KEY_NAMES = {
    "Ctrl": pgettext("key", "Ctrl"),
    "Shift": pgettext("key", "Shift"),
    "Alt": pgettext("key", "Alt"),
    "Enter": pgettext("key", "Enter"),
    "Return": pgettext("key", "Return"),
    "Space": pgettext("key", "Space"),
    "Tab": pgettext("key", "Tab"),
    "Esc": pgettext("key", "Esc"),
    "Home": pgettext("key", "Home"),
    "End": pgettext("key", "End"),
    "PgUp": pgettext("key", "PgUp"),
    "PgDown": pgettext("key", "PgDown"),
    "Del": pgettext("key", "Del"),
    "Backspace": pgettext("key", "Backspace"),
    "Click": pgettext("key", "Click"),
}

IS_MAC = sys.platform == "darwin"

# Apple's modifier symbols, in the order Apple writes them (HIG, Keyboards).
# Qt names Command "Ctrl" and Control "Meta" on a Mac.
_MAC_MODIFIERS = (("Meta", "\u2303"), ("Alt", "\u2325"), ("Shift", "\u21e7"),
                  ("Ctrl", "\u2318"))
_MAC_KEYS = {
    "Enter": "\u21a9", "Return": "\u21a9", "Tab": "\u21e5", "Esc": "esc",
    "Backspace": "\u232b", "Del": "\u2326", "Home": "\u2196", "End": "\u2198",
    "PgUp": "\u21de", "PgDown": "\u21df", "Up": "\u2191", "Down": "\u2193",
    "Left": "\u2190", "Right": "\u2192",
}


def _parts(keys: str) -> List[str]:
    """"Ctrl+Shift+D" as its keys; "Ctrl++" (the plus key) keeps its "+"."""
    parts = [p.strip() for p in keys.split("+")]
    keys_found = [p for p in parts if p]
    if keys.endswith("++"):
        keys_found.append("+")
    return keys_found


def _key_names(keys: str, *, mac: Optional[bool] = None) -> List[str]:
    """The keys of "Ctrl+Shift+D", each named the way the computer in use
    labels it (``mac`` says which, the one in use by default): on a Mac
    Apple's symbols in Apple's order (⇧⌘D), elsewhere the reader's
    keyboard's names (Strg, Umschalt)."""
    parts = _parts(keys)
    if not (IS_MAC if mac is None else mac):
        return [_KEY_NAMES.get(part, part) for part in parts]
    modifiers = dict(_MAC_MODIFIERS)
    held = [symbol for name, symbol in _MAC_MODIFIERS if name in parts]
    rest = [_MAC_KEYS.get(part, _KEY_NAMES.get(part, part))
            for part in parts if part not in modifiers]
    return held + rest


def groups_from(rows) -> Tuple[ShortcutGroup, ...]:
    """``[(title, [(keys, action), ...]), ...]`` (as
    CommandRegistry.shortcut_groups returns it) as dialog groups."""
    return tuple(ShortcutGroup(title=title,
                               items=tuple(Shortcut(keys, action) for keys, action in items))
                 for title, items in rows)


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
    between - closer to how Apple's Help shortcuts viewer or Sublime's
    cheat sheet display key combos than a single bold string.
    """
    container = QWidget()
    layout = QHBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(3)
    for i, part in enumerate(_key_names(keys)):
        # A Mac writes its keys side by side (⇧⌘D), elsewhere with "+".
        if i and not IS_MAC:
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
        # shown - making it useless for "should this card take a grid
        # slot?" decisions during initial construction.
        self._filtered_out = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 14)
        outer.setSpacing(10)

        header = QLabel(_GROUP_TITLES.get(group.title, group.title))
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
            action.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            row_idx = self._grid.rowCount()
            self._grid.addWidget(keycap, row_idx, 0, Qt.AlignTop | Qt.AlignLeft)
            # No alignment in the cell: the label then takes the column's
            # width, and a long (or translated) action wraps onto a
            # second line instead of being cut off.
            self._grid.addWidget(action, row_idx, 1)
            self._rows.append((keycap, action, shortcut))

    def apply_filter(self, needle: str) -> int:
        """Show only rows that contain ``needle`` (case-insensitive).

        Returns the number of visible rows so the dialog can hide the
        whole card when nothing matches.
        """
        needle = needle.strip().lower()
        visible = 0
        for keycap, action, shortcut in self._rows:
            # The keys as written (Ctrl+S) and as shown (Strg+S, ⌘S) all match.
            shown = "+".join(_key_names(shortcut.keys)) + " " + "".join(
                _key_names(shortcut.keys))
            text = f"{shortcut.keys} {shown} {shortcut.action}".lower()
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

    A reference window styled like the keyboard-shortcuts pages found in
    major desktop applications: category cards laid out in two columns,
    kbd-style key caps, and a live search across all groups. It is not
    modal: it stays open beside the document, so a key can be tried
    while it is up.
    """

    def __init__(self, groups, *, is_dark: bool = True,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._groups = tuple(groups)
        self.setObjectName("shortcuts_dialog")
        self.setWindowTitle(_("Keyboard Shortcuts"))
        self.setModal(False)
        # Wide-and-tall by default so all six categories fit without
        # scrolling on the common laptop resolutions. The dialog is
        # resizable so users on small screens can still see everything.
        self.resize(760, 620)
        self._is_dark = is_dark
        self._cards: List[_ShortcutCard] = []
        self._build_ui()
        self.apply_theme(is_dark)

    # -- construction ------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 18)
        root.setSpacing(12)

        title = QLabel(_("Keyboard Shortcuts"))
        title.setObjectName("shortcuts_title")
        root.addWidget(title)

        subtitle = QLabel(_("Every key MyEditor answers to on this computer. Type to filter."))
        subtitle.setObjectName("shortcuts_subtitle")
        subtitle.setWordWrap(True)
        root.addWidget(subtitle)

        self._search = QLineEdit()
        self._search.setObjectName("shortcuts_search")
        self._search.setPlaceholderText(_("Filter Shortcuts"))
        # A placeholder is not a name; a screen reader needs one of its own.
        self._search.setAccessibleName(_("Filter Shortcuts"))
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
        self._make_cards()

        scroll.setWidget(inner)
        root.addWidget(scroll, 1)
        self._scroll = scroll

        self._no_results = QLabel(_("No shortcuts match your filter."))
        self._no_results.setObjectName("shortcuts_no_results")
        self._no_results.setAlignment(Qt.AlignCenter)
        self._no_results.setVisible(False)
        root.addWidget(self._no_results)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        # Close is a "reject" role by default - wire its click to accept
        # so Enter / Esc both dismiss cleanly.
        close_btn = buttons.button(QDialogButtonBox.Close)
        close_btn.setDefault(True)
        close_btn.clicked.connect(self.accept)
        root.addWidget(buttons)
        # Command-W (Ctrl+W) closes it, as it does any window.
        QShortcut(QKeySequence.StandardKey.Close, self).activated.connect(self.close)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._keep_cards_whole()

    def _keep_cards_whole(self) -> None:
        """Never narrower than the cards need. They do not scroll sideways,
        so a longer translation widens the window instead of being cut off.
        Measured when shown, once the cards have their style and fonts."""
        cards = self._scroll.widget().minimumSizeHint().width()
        self._scroll.setMinimumWidth(cards + self._scroll.verticalScrollBar().sizeHint().width())
        self.setMinimumSize(max(560, self.minimumSizeHint().width()), 420)

    # -- behaviour ---------------------------------------------------------

    def apply_theme(self, is_dark: bool) -> None:
        self._is_dark = is_dark
        self.setStyleSheet(_DARK_CSS if is_dark else _LIGHT_CSS)

    def set_groups(self, groups) -> None:
        """List ``groups`` instead, keeping the filter typed: the window
        stays open while Nostr comes into use or goes out of it, and lists
        only the keys that work."""
        for card in self._cards:
            self._cards_grid.removeWidget(card)
            card.deleteLater()
        self._groups = tuple(groups)
        self._make_cards()
        self._on_filter_changed(self._search.text())

    def _make_cards(self) -> None:
        self._cards = [_ShortcutCard(group) for group in self._groups]
        self._reflow_cards()

    def _on_filter_changed(self, text: str) -> None:
        total_visible = 0
        for card in self._cards:
            total_visible += card.apply_filter(text)
        # Hidden cards left holes in the 2-column grid before - re-pack
        # so the survivors fill the layout sequentially from the top.
        self._reflow_cards()
        self._no_results.setVisible(total_visible == 0)

    def _reflow_cards(self) -> None:
        """Re-pack visible cards into a tight two-column grid.

        Qt's ``QGridLayout`` doesn't collapse empty cells when a child
        is hidden - it preserves grid positions and leaves visible
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
