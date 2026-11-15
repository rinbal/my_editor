# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins how the Keyboard Shortcuts window writes keys.

What must hold:

  On a Mac, keys are Apple's symbols in Apple's order (⌃⌥⇧⌘), side by
  side as in menus; elsewhere they are joined by "+" and named as the
  reader's keyboard names them.

  The editing keys that are not commands (Tab and Shift+Tab in lists,
  Return, Shift+Return, Command-click on a link) are listed.

  Filtering finds a shortcut by its name and by its keys, either way of
  writing them.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

import shortcuts_dialog  # noqa: E402
from shortcuts_dialog import (  # noqa: E402
    OTHER_KEYS, Shortcut, ShortcutGroup, ShortcutsDialog, _key_names, groups_from,
)


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("keys, mac, shown", [
    ("Ctrl+Alt+Shift+V", True, ["⌥", "⇧", "⌘", "V"]),
    ("Meta+Ctrl+F", True, ["⌃", "⌘", "F"]),
    ("Shift+Tab", True, ["⇧", "⇥"]),
    ("Ctrl++", True, ["⌘", "+"]),
    ("Ctrl+Click", True, ["⌘", "Click"]),
    ("Ctrl+Alt+Shift+V", False, ["Ctrl", "Alt", "Shift", "V"]),
    ("Esc", False, ["Esc"]),
])
def test_keys_are_written_the_platform_way(keys, mac, shown):
    assert _key_names(keys, mac=mac) == shown


@pytest.mark.parametrize("mac", [True, False])
def test_a_mac_writes_keys_side_by_side(monkeypatch, mac):
    monkeypatch.setattr(shortcuts_dialog, "IS_MAC", mac)
    row = shortcuts_dialog._make_keycap_row("Ctrl+B")
    texts = [label.text() for label in row.findChildren(QLabel)]
    assert ("+" in texts) is (not mac)


def test_the_list_and_link_keys_are_listed():
    editing = dict(OTHER_KEYS)["Editing"]
    keys = [k for k, _words in editing]
    assert {"Tab", "Shift+Tab", "Enter", "Shift+Enter", "Ctrl+Click"} <= set(keys)


def test_the_pdf_zoom_keys_listed_are_this_computers():
    # On a Mac Option-Command-0 and 2, and Command-0 is Actual Size, as in
    # Preview; elsewhere SumatraPDF's Ctrl+0, 1 and 2.
    listed = {words: keys for keys, words in dict(OTHER_KEYS)["PDF Reading"]}
    mac = sys.platform == "darwin"
    assert [listed["Fit page width"], listed["Actual size"], listed["Fit whole page"]] == (
        ["Ctrl+Alt+0", "Ctrl+0", "Ctrl+Alt+2"] if mac else ["Ctrl+0", "Ctrl+1", "Ctrl+2"])


def test_filtering_finds_keys_written_either_way(monkeypatch):
    monkeypatch.setattr(shortcuts_dialog, "IS_MAC", True)
    group = ShortcutGroup(title="File", items=(Shortcut("Ctrl+S", "Save"),
                                               Shortcut("Ctrl+O", "Open file")))
    card = shortcuts_dialog._ShortcutCard(group)
    assert card.apply_filter("⌘S") == 1
    assert card.apply_filter("ctrl+s") == 1
    assert card.apply_filter("open") == 1
    assert card.apply_filter("nothing like it") == 0


def test_the_window_is_a_reference_beside_the_document_not_modal(qt_app):
    """Help > Keyboard Shortcuts used to block the document while open; a
    reference stays open beside it so a key can be tried, and choosing the
    command again brings the same window to the front."""
    from types import SimpleNamespace
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from test_commands import StandIn
    from main_window import MainWindow
    win = StandIn()
    MainWindow._build_actions(win)
    win.nostr_state = SimpleNamespace(active=False)
    win.is_dark_theme = False
    win._shortcuts_window = None
    win._shortcut_groups = lambda: MainWindow._shortcut_groups(win)
    MainWindow._show_shortcuts(win)
    first = win._shortcuts_window
    assert first is not None and first.isVisible() and not first.isModal()
    MainWindow._show_shortcuts(win)
    assert win._shortcuts_window is first
    first.close()


def test_an_open_window_follows_an_account_coming_and_going(qt_app):
    # Review L4: the window kept listing the Nostr keys after Sign Out
    # (and lacked them when opened before signing in).
    from types import SimpleNamespace
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from test_commands import StandIn
    from main_window import MainWindow
    win = StandIn()
    MainWindow._build_actions(win)
    win.nostr_state = SimpleNamespace(active=True)
    win.is_dark_theme = False
    win._settings_window = None
    win._shortcuts_window = None
    win._shortcut_groups = lambda: MainWindow._shortcut_groups(win)

    def listed():
        return {item.action for group in win._shortcuts_window._groups
                for item in group.items}

    MainWindow._show_shortcuts(win)
    window = win._shortcuts_window
    window._search.setText("e")
    with_nostr = listed()
    win.nostr_state.active = False
    MainWindow._on_nostr_in_use_changed(win, False)
    without_nostr = listed()
    assert without_nostr < with_nostr
    assert without_nostr == {item.action for group in MainWindow._shortcut_groups(win)
                             for item in group.items}
    assert window._search.text() == "e"            # what was typed stays
    win.nostr_state.active = True
    MainWindow._on_nostr_in_use_changed(win, True)
    assert listed() == with_nostr
    window.close()



def test_command_w_closes_it_as_any_window(qt_app):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    dialog = ShortcutsDialog(groups_from([("Editing", [("Ctrl+B", "Bold")])]), is_dark=False)
    dialog.show()
    qt_app.processEvents()
    QTest.keyClick(dialog, Qt.Key_W, Qt.ControlModifier)
    assert not dialog.isVisible()
