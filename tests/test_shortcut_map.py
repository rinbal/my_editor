# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins MyEditor's keyboard map: MyEditor 3.3's keys, on every system.

What must hold:

  Every key MyEditor 3.3 had still does what it did, or what took its
  command's place (Reset Format is Clear Formatting, Insert image… is
  Insert from Media Library…), on a Mac, on Windows and on Linux.
  The one exception is logged in OPS map and the user guide: Shift-Command-P
  is Page Setup on a Mac (Apple's guidelines), so Publish as Note is
  Option-Command-P there.

  The PDF reader and the Media Library keep their 3.3 keys too.
"""

import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent, QKeySequence  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import commands  # noqa: E402

PLATFORMS = ("darwin", "win32", "linux")
STD = QKeySequence.StandardKey

# MyEditor 3.3's keys (main_window.py, _build_actions and _build_menu), by
# the command that has them now.
V33 = {
    "file.new": STD.New, "file.open": STD.Open, "file.save": STD.Save,
    "file.save_as": "Ctrl+Shift+S", "file.print": STD.Print,
    "file.knit_html": "Ctrl+Shift+K", "file.close_tab": "Ctrl+W", "file.quit": "Ctrl+Q",
    "format.bold": "Ctrl+B", "format.italic": "Ctrl+I", "format.underline": "Ctrl+U",
    "format.reset": "Ctrl+D",                          # was Reset Format
    "search.find": "Ctrl+F", "search.next": "F3", "search.previous": "Shift+F3",
    "view.appearance.switch": "Ctrl+Shift+T",          # was Toggle Dark/Light Theme
    "view.line_numbers": "Ctrl+Shift+L", "view.syntax_highlighting": "Ctrl+Shift+H",
    "view.fullscreen": STD.FullScreen,
    "nostr.publish_note": "Ctrl+Shift+P", "nostr.publish_article": "Ctrl+Shift+A",
    "nostr.media_library": "Ctrl+Shift+M",
    "nostr.insert_image": "Ctrl+Shift+I",              # was Insert image…
    "nostr.drafts": "Ctrl+Shift+D",
}
# Where a system's own convention has the key, and what the command took.
MOVED = {("darwin", "nostr.publish_note"): "Ctrl+Alt+P"}


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    return QApplication.instance() or QApplication([])


def window_on(platform, monkeypatch):
    from main_window import MainWindow
    from tests.test_commands import StandIn
    monkeypatch.setattr(commands, "sys", types.SimpleNamespace(platform=platform))
    # The File menu offers Print Preview by the system the app starts on
    # (a Mac's print panel has its own preview), so that is set for
    # ``platform`` too.
    import printing
    monkeypatch.setattr(printing, "OFFERS_PRINT_PREVIEW", platform != "darwin")
    win = StandIn()
    MainWindow._build_actions(win)          # refuses two commands on one key
    return win


def bound(action):
    return {k.toString(QKeySequence.SequenceFormat.PortableText) for k in action.shortcuts()}


@pytest.mark.parametrize("platform", PLATFORMS)
def test_every_key_of_3_3_still_does_its_job(monkeypatch, platform):
    win = window_on(platform, monkeypatch)
    for command_id, keys in V33.items():
        keys = MOVED.get((platform, command_id), keys)
        expected = set(commands.all_key_texts(keys))
        assert expected <= bound(win.commands.action(command_id)), (platform, command_id)


@pytest.mark.parametrize("platform", PLATFORMS)
def test_the_key_3_3_gave_up_on_a_mac_is_page_setup_there(monkeypatch, platform):
    win = window_on(platform, monkeypatch)
    page_setup = bound(win.commands.action("file.page_setup"))
    assert ("Ctrl+Shift+P" in page_setup) is (platform == "darwin")


def test_the_pdf_reader_keeps_its_3_3_keys():
    from pdf_viewer import reader_keys
    keys = {k for _s, _w, _a, tab, view in reader_keys() for k in tab + view}
    assert {"Ctrl+=", "Ctrl+0", "F12", "Space", "Shift+Space", "Home", "End", "J", "K",
            "N", "P", "+", "-", "G", "Esc"} <= keys
    assert set(commands.all_key_texts(STD.ZoomIn)) <= keys
    assert set(commands.all_key_texts(STD.ZoomOut)) <= keys
    assert set(commands.all_key_texts(STD.Copy)) <= keys


def test_the_media_library_keeps_its_3_3_keys():
    from nostr.ui.media_grid import MediaGrid
    grid = MediaGrid(is_dark=False)
    heard = []
    for name in ("copy_requested", "save_requested", "paste_requested", "delete_requested"):
        getattr(grid, name).connect(lambda name=name: heard.append(name))

    def press(keys):
        combo = QKeySequence(keys)[0]
        grid.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, combo.key(),
                                     combo.keyboardModifiers()))

    for keys in (QKeySequence(STD.Copy), QKeySequence(STD.Save), QKeySequence(STD.Paste),
                 QKeySequence(Qt.Key.Key_Delete), QKeySequence(Qt.Key.Key_Backspace)):
        press(keys)
    assert heard == ["copy_requested", "save_requested", "paste_requested",
                     "delete_requested", "delete_requested"]
    grid.deleteLater()


# -- every menu command has a key, or a reason not to -------------------------------

def menu_commands(win):
    from tests.test_commands import menu_actions
    return [a for top in win.menuBar().actions() for a in menu_actions(top.menu())]


@pytest.mark.parametrize("platform", PLATFORMS)
def test_every_menu_command_has_a_key_or_a_reason_not_to(monkeypatch, platform):
    from main_window import MainWindow
    win = window_on(platform, monkeypatch)
    MainWindow._build_menu(win)
    in_menus = [a for a in menu_commands(win)
                if platform == "darwin" or not a.objectName().startswith("window.")]
    unbound = sorted(a.objectName() for a in in_menus if not a.shortcuts())
    unreasoned = [i for i in unbound if not commands.no_shortcut_reason(i, platform=platform)]
    assert unreasoned == [], platform
    # And no reason stays behind for a command that has a key now.
    for name, where, _reason in commands.NO_SHORTCUT:
        if where == "mac" and platform != "darwin" or where == "other" and platform == "darwin":
            continue
        matching = [a for a in in_menus if a.objectName() == name
                    or name.endswith(".") and a.objectName().startswith(name)]
        assert not any(a.shortcuts() for a in matching), (platform, name)


# The keys given where 3.3 had none, by the convention each follows (see
# the comment at each command in main_window.py).
EXTENDED = {
    "darwin": {"format.code": "Ctrl+Shift+E", "format.list.tick": "Ctrl+Shift+U",
               "format.quote": "Ctrl+'", "insert.image": "Ctrl+Shift+V",
               "insert.table": "Ctrl+F12", "insert.divider": "Ctrl+Shift+-",
               "insert.code_block": "Ctrl+Alt+Shift+C", "insert.footnote": "Ctrl+Shift+F",
               "edit.check_spelling": "Ctrl+Alt+;", "view.write": "Meta+Ctrl+1",
               "view.preview": "Meta+Ctrl+2", "view.split": "Meta+Ctrl+3",
               "nostr.article_details": "Ctrl+Alt+I", "nostr.insert_reference": "Ctrl+Shift+R",
               "nostr.insert_audio_video": "Ctrl+Shift+J", "nostr.imports": "Ctrl+Shift+N",
               "nostr.published": "Ctrl+Shift+B", "help.shortcuts": "Ctrl+/",
               "app.settings": "Ctrl+,"},
    "other": {"format.code": "Ctrl+E", "format.list.tick": "Ctrl+Return",
              "format.quote": "Ctrl+'", "insert.image": "Ctrl+Shift+E",
              "insert.table": "Ctrl+F12", "insert.divider": "Ctrl+Shift+-",
              "insert.code_block": "Ctrl+Shift+C", "insert.footnote": "Ctrl+Shift+F",
              "edit.check_spelling": "Shift+F7", "view.write": "Ctrl+Shift+1",
              "view.preview": "Ctrl+Shift+2", "view.split": "Ctrl+Shift+3",
              "nostr.article_details": "Ctrl+F5", "nostr.insert_reference": "Ctrl+Shift+R",
              "nostr.insert_audio_video": "Ctrl+Shift+J", "nostr.imports": "Ctrl+Shift+N",
              "nostr.published": "Ctrl+Shift+B", "help.shortcuts": "Ctrl+/",
              "app.settings": "Ctrl+,", "file.print_preview": "Ctrl+Shift+O"},
}


@pytest.mark.parametrize("platform", PLATFORMS)
def test_the_commands_people_use_have_keys(monkeypatch, platform):
    win = window_on(platform, monkeypatch)
    expected = EXTENDED["darwin" if platform == "darwin" else "other"]
    for command_id, keys in expected.items():
        assert keys in bound(win.commands.action(command_id)), (platform, command_id)


def test_a_standard_key_leaves_a_desktops_extra_combination_to_a_named_command(qt_app):
    # KDE's Full Screen is Ctrl+Shift+F as well as F11; Footnote names
    # Ctrl+Shift+F, so Full Screen keeps F11 there instead of refusing to
    # start.
    from PySide6.QtWidgets import QMainWindow
    registry = commands.CommandRegistry(QMainWindow())
    taken = commands.all_key_texts(STD.FindNext)[-1]
    registry.add(commands.Command("insert.x", "X", commands.INSERT, taken))
    action = registry.add(commands.Command("search.next", "Find Next", commands.SEARCH,
                                           STD.FindNext))
    assert taken not in bound(action) and bound(action)
    with pytest.raises(ValueError):          # a named key is never shared
        registry.add(commands.Command("insert.y", "Y", commands.INSERT, taken))


class _Panel:
    DRAFTS_VIEW = "drafts"

    def __init__(self):
        self.visible, self.view = False, "drafts"

    def isVisible(self):  # noqa: N802, Qt's name
        return self.visible

    def current_view(self):
        return self.view

    def show_view(self, key):
        self.view = key

    def views(self):
        return ["drafts", "published", "submissions"]


def test_published_and_drafts_each_show_their_view_or_put_the_panel_away():
    from main_window import MainWindow
    from nostr.ui.published_view import PANEL_VIEW as PUBLISHED
    from tests.test_commands import StandIn
    win = StandIn()
    win._drafts_panel = panel = _Panel()
    win._show_drafts_panel = lambda: setattr(panel, "visible", True)
    win._hide_drafts_panel = lambda: setattr(panel, "visible", False)
    win._show_or_hide_panel_view = types.MethodType(MainWindow._show_or_hide_panel_view, win)
    MainWindow._on_show_published(win)
    assert panel.visible and panel.view == PUBLISHED
    MainWindow._on_toggle_drafts_panel(win)          # the other view: shown, not hidden
    assert panel.visible and panel.view == "drafts"
    MainWindow._on_toggle_drafts_panel(win)
    assert not panel.visible
    MainWindow._on_show_published(win)
    MainWindow._on_show_published(win)
    assert not panel.visible
    # ME-7: Einreichungen has its command too, beside the two others.
    MainWindow._on_show_submissions(win)
    assert panel.visible and panel.view == "submissions"
    MainWindow._on_show_published(win)               # another view: shown, not hidden
    assert panel.visible and panel.view == PUBLISHED
    MainWindow._on_show_submissions(win)
    MainWindow._on_show_submissions(win)
    assert not panel.visible


@pytest.mark.parametrize("platform", PLATFORMS)
def test_submissions_has_a_command_with_a_key_of_its_own_on_every_system(monkeypatch, platform):
    # ME-7: nothing but the small dot at the tab led to Einreichungen.
    win = window_on(platform, monkeypatch)               # refuses two commands on one key
    assert bound(win.commands.action("nostr.submissions")) == {"Ctrl+Shift+Y"}
