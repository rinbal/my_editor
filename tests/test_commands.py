# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins the one command list behind the menus and the shortcut list.

What must hold:

  A command becomes one action of the window, with its title, shortcut
  and handler, and works without the menu bar.

  No two commands share an id or a shortcut: the second is refused when
  it is added, so a clash fails here and not in a user's hands.

  Every action in the menus is a registered command, and every
  registered command is in a menu.

  The Keyboard Shortcuts window lists exactly the commands' shortcuts
  (plus keys that are not commands), so it cannot drift from the menus.

  Nostr commands are marked, so surfaces that follow the Nostr state
  can leave them out.
"""

import ast
import inspect
import os
import sys
import textwrap

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QAction, QKeySequence  # noqa: E402
from PySide6.QtWidgets import QApplication, QMainWindow, QMenu  # noqa: E402

import commands  # noqa: E402
from commands import Command, CommandRegistry, key_text  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window():
    return QMainWindow()


# -- the registry ---------------------------------------------------------------------

def test_a_command_becomes_an_action_of_the_window(window):
    ran = []
    registry = CommandRegistry(window)
    action = registry.add(Command("file.save", "Save", commands.FILE, "Ctrl+S"),
                          triggered=lambda: ran.append(True))
    assert action.text() == "Save"
    assert action.shortcut() == QKeySequence("Ctrl+S")
    assert action in window.actions()           # works without the menu bar
    assert registry.action("file.save") is action
    action.trigger()
    assert ran == [True]


def test_a_checkable_command_reports_its_state(window):
    states = []
    registry = CommandRegistry(window)
    action = registry.add(Command("view.paper", "Paper Mode", commands.VIEW, checkable=True),
                          toggled=states.append, checked=False)
    action.trigger()
    assert action.isChecked() and states == [True]


def test_two_commands_never_share_an_id(window):
    registry = CommandRegistry(window)
    registry.add(Command("a", "A", commands.FILE))
    with pytest.raises(ValueError):
        registry.add(Command("a", "Another A", commands.FILE))


def test_two_commands_never_share_a_shortcut(window):
    registry = CommandRegistry(window)
    registry.add(Command("one", "One", commands.FILE, "Ctrl+Shift+S"))
    with pytest.raises(ValueError, match="Ctrl\\+Shift\\+S"):
        registry.add(Command("two", "Two", commands.NOSTR, QKeySequence("Ctrl+Shift+S")))


def test_a_command_needs_a_known_group(window):
    with pytest.raises(ValueError):
        CommandRegistry(window).add(Command("x", "X", "Somewhere"))


def test_finding_commands_by_words(window):
    registry = CommandRegistry(window)
    registry.add(Command("nostr.publish_note", "Publish as Note…", commands.NOSTR, nostr=True,
                         keywords=("post",)), triggered=lambda: None)
    registry.add(Command("file.print", "Print…", commands.FILE), triggered=lambda: None)
    assert [c.id for c in registry.find("post", nostr=True)] == ["nostr.publish_note"]
    assert registry.find("post", nostr=False) == []            # Nostr not in use
    assert [c.id for c in registry.find("PRI", nostr=False)] == ["file.print"]
    registry.action("file.print").setEnabled(False)
    assert registry.find("print", nostr=False) == []           # not now


def test_the_shortcut_list_comes_from_the_commands(window):
    registry = CommandRegistry(window)
    registry.add(Command("file.save", "Save", commands.FILE, "Ctrl+S"))
    registry.add(Command("file.save_as", "Save As…", commands.FILE, "Ctrl+Shift+S"))
    registry.add(Command("view.theme", "Toggle Dark/Light Theme", commands.VIEW, "Ctrl+Shift+T",
                         listed_as="Toggle theme"))
    registry.add(Command("file.page_setup", "Page Setup…", commands.FILE))    # no shortcut
    registry.add(Command("nostr.drafts", "Drafts…", commands.NOSTR, "Ctrl+Shift+D",
                         nostr=True))
    groups = registry.shortcut_groups([("Editing", [("Tab", "Indent")])])
    assert groups == [
        ("File", [("Ctrl+S", "Save"), ("Ctrl+Shift+S", "Save As")]),
        ("Editing", [("Tab", "Indent")]),
        ("View", [("Ctrl+Shift+T", "Toggle theme")]),
        ("Nostr", [("Ctrl+Shift+D", "Drafts")]),
    ]
    assert ("Nostr", [("Ctrl+Shift+D", "Drafts")]) not in registry.shortcut_groups(nostr=False)


def test_a_standard_key_answers_to_every_combination_of_the_platform(window):
    registry = CommandRegistry(window)
    action = registry.add(Command("search.next", "Find Next", commands.SEARCH,
                                  QKeySequence.StandardKey.FindNext))
    bound = {s.toString(QKeySequence.SequenceFormat.PortableText) for s in action.shortcuts()}
    expected = set(commands.all_key_texts(QKeySequence.StandardKey.FindNext))
    assert expected and bound == expected
    # Every one of them is taken, not only the first.
    for keys in expected:
        with pytest.raises(ValueError):
            registry.add(Command(f"other.{keys}", "Other", commands.FILE, keys))


def test_platform_keys_follow_each_platform_convention():
    assert commands.platform_keys("Ctrl+Alt+1", "Ctrl+1", platform="darwin") == "Ctrl+Alt+1"
    assert commands.platform_keys("Ctrl+Alt+1", "Ctrl+1", platform="win32") == "Ctrl+1"
    assert commands.platform_keys("Ctrl+Alt+1", "Ctrl+1", platform="linux") == "Ctrl+1"


def test_standard_keys_are_written_out():
    assert key_text(QKeySequence.StandardKey.Save) == "Ctrl+S"
    assert key_text(None) == ""


# -- the window's own commands -----------------------------------------------------------

class StandIn(QMainWindow):
    """Just enough of MainWindow to build its actions and menus."""

    paper_mode = False
    editor_background = "none"
    highlight_current_line = False
    format_toolbar = None          # built with the rest of the window, not here
    _multilingual = None            # authoring service is not mounted by this menu host

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return lambda *args, **kwargs: None


@pytest.fixture(scope="module")
def main_window_commands(qt_app):
    from main_window import MainWindow
    stand_in = StandIn()
    MainWindow._build_actions(stand_in)
    MainWindow._build_menu(stand_in)
    return stand_in


def menu_actions(menu: QMenu, skip=("Recent Files",)):
    for action in menu.actions():
        if action.menu() is not None:
            if action.text() not in skip:
                yield from menu_actions(action.menu(), skip)
        elif not action.isSeparator():
            yield action


def test_every_menu_item_is_a_registered_command(main_window_commands):
    win = main_window_commands
    in_menus = [a for top in win.menuBar().actions() for a in menu_actions(top.menu())]
    assert in_menus
    for action in in_menus:
        assert action.objectName() in win.commands, f"{action.text()} is not a command"
    registered = {c.id for c in win.commands.commands()}
    shown = {a.objectName() for a in in_menus}
    # Print Preview is offered only where the platform has one; moving
    # between tabs and panes is done by key (the Keyboard Shortcuts window
    # lists them; a Mac's Window menu has Show Next and Previous Tab).
    keyboard_only = {c.id for c in win.commands.commands(commands.NAVIGATION)}
    assert registered - shown <= {"file.print_preview"} | keyboard_only


def test_the_menus_build_their_actions_through_the_registry():
    from main_window import MainWindow
    for method in (MainWindow._build_actions, MainWindow._build_menu):
        tree = ast.parse(textwrap.dedent(inspect.getsource(method)))
        made = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                and getattr(n.func, "id", "") == "QAction"]
        assert made == [], f"{method.__name__} makes a QAction outside the command list"


def test_the_shortcut_window_lists_every_command_shortcut(main_window_commands):
    from shortcuts_dialog import OTHER_KEYS
    win = main_window_commands
    listed = {(keys, words) for _group, items in win.commands.shortcut_groups(OTHER_KEYS)
              for keys, words in items}
    for command in win.commands.commands():
        if command.shortcut is not None:
            assert any(keys == key_text(command.shortcut) for keys, _w in listed), command.id


def test_the_shortcut_window_says_what_the_photo_keys_move(main_window_commands):
    """Format > Photo > Move Left stood in the Keyboard Shortcuts window as
    "Move Left", among the keys for text, without a word on what it moves."""
    listed = [words for _group, rows in main_window_commands.commands.shortcut_groups()
              for _keys, words in rows]
    assert sorted(words for words in listed if words.startswith("Move photo")) == [
        "Move photo down", "Move photo left", "Move photo right", "Move photo up"]
    assert not {"Move Left", "Move Right", "Move Up", "Move Down"} & set(listed)


def test_the_nostr_commands_that_need_an_account_are_marked(main_window_commands):
    win = main_window_commands
    marked = {c.id for c in win.commands.commands() if c.nostr}
    assert {"nostr.publish_note", "nostr.publish_article", "nostr.drafts",
            "nostr.media_library", "nostr.insert_image", "nostr.sign_out",
            "nostr.backup_account", "nostr.imports"} <= marked
    # How a person starts using Nostr is never hidden behind Nostr.
    assert not marked & {"nostr.connect", "nostr.create_account", "nostr.restore_account",
                         "nostr.membership"}


def test_menu_titles_follow_title_case_and_no_em_dashes(main_window_commands):
    # Apple's title case: articles, conjunctions and short prepositions small.
    small = {"a", "an", "and", "as", "for", "from", "in", "of", "on", "or", "the", "to"}
    for command in main_window_commands.commands.commands():
        assert "\u2014" not in command.title
        words = command.title.rstrip("…").replace("/", " ").split()
        for index, word in enumerate(words):
            if word.lower() in small and index:
                continue
            assert word[0].isupper() or not word[0].isalpha(), command.title


def test_only_about_settings_and_quit_move_to_the_macos_application_menu(
        main_window_commands):
    # Qt guesses an action's place from its words unless told; a German
    # title starting with "Einstellungen" or "Optionen" would otherwise be
    # moved out of its menu on a Mac. Settings is told, and goes there.
    roles = {c.id: c.role for c in main_window_commands.commands.commands()}
    assert roles.pop("help.about") == QAction.MenuRole.AboutRole
    assert roles.pop("file.quit") == QAction.MenuRole.QuitRole
    assert roles.pop("app.settings") == QAction.MenuRole.PreferencesRole
    assert set(roles.values()) == {QAction.MenuRole.NoRole}
    by_id = {c.id: c for c in main_window_commands.commands.commands()}
    for command_id in ("help.about", "file.quit", "app.settings", "file.save"):
        action = main_window_commands.commands.action(command_id)
        assert action.menuRole() == by_id[command_id].role


# -- the Edit menu, and commands that follow the tab ---------------------------------------

@pytest.fixture
def fresh_window(qt_app):
    """A stand-in built for one test. (QAction.menu() hands its menu to
    Python in PySide6, and walking the menus with it can delete them, so
    tests that read the menus' own objects get a window of their own.)"""
    from main_window import MainWindow
    stand_in = StandIn()
    MainWindow._build_actions(stand_in)
    MainWindow._build_menu(stand_in)
    return stand_in


def test_the_edit_menu_follows_apple_order(fresh_window):
    win = fresh_window
    titles = [a.text() for a in win.m_edit.actions() if not a.isSeparator()]
    assert titles[:8] == ["Undo", "Redo", "Cut", "Copy", "Paste", "Paste and Match Style",
                          "Delete", "Select All"]
    find = [a.objectName() for a in win.m_find.actions()]
    assert find[:5] == ["search.find", "search.replace", "search.next", "search.previous",
                        "search.use_selection"]
    # Find lives in Edit now; there is no Search menu of its own.
    tops = [a.text().replace("&", "") for a in win.menuBar().actions()]
    assert tops[:4] == ["File", "Edit", "Insert", "Format"] and "Search" not in tops


def test_find_next_answers_to_every_key_of_the_platform(fresh_window):
    action = fresh_window.commands.action("search.next")
    bound = {s.toString(QKeySequence.SequenceFormat.PortableText) for s in action.shortcuts()}
    assert bound == set(commands.all_key_texts(QKeySequence.StandardKey.FindNext))


def test_the_colors_are_in_the_format_menu(fresh_window):
    win = fresh_window
    ids = [a.objectName() for a in win.m_color.actions() if not a.isSeparator()]
    assert ids[0] == "format.color.red" and ids[-1] == "format.color.none"
    assert win.m_color.menuAction() in win.m_format.actions()


class _Editor:
    def __init__(self, path=None, **flags):
        self._file_path = path
        for name, value in flags.items():
            setattr(self, name, value)


@pytest.mark.parametrize("editor, kind", [
    (None, ""),
    (_Editor(), "rich"),
    (_Editor("/notes/a.md"), "rich"),
    (_Editor("/notes/a.html"), "rich"),
    (_Editor("/notes/a.txt"), "source"),
    (_Editor("/notes/a.py"), "source"),
    (_Editor("/notes/a.md", _markdown_source=True), "source"),
    (_Editor("/notes/a.Rmd", _loaded_as_rmd_source=True), "source"),
    (_Editor(_language="python"), "source"),
])
def test_what_a_tab_can_hold(editor, kind):
    from main_window import MainWindow
    assert MainWindow._editor_kind(None, editor) == kind


def test_syntax_highlighting_is_dimmed_in_documents(fresh_window):
    # Review L2 (CP3): a document's code blocks are always colored, so
    # View > Syntax Highlighting does nothing there.
    from main_window import MainWindow
    win = fresh_window
    win._editor_kind = lambda ed: MainWindow._editor_kind(win, ed)
    win.current_editor = lambda: _Editor("/notes/a.md")
    MainWindow._update_editor_commands(win)
    assert not win.act_toggle_syntax_hl.isEnabled()
    win.current_editor = lambda: _Editor("/code/tool.py")
    MainWindow._update_editor_commands(win)
    assert win.act_toggle_syntax_hl.isEnabled()


def test_editing_commands_follow_the_tab(fresh_window):
    from main_window import MainWindow
    win = fresh_window
    win._editor_kind = lambda ed: MainWindow._editor_kind(win, ed)
    rich_only = win.commands.action("format.bold")   # stands in for a structure command
    win._rich_actions.append(rich_only)

    win.current_editor = lambda: None                  # a PDF tab
    win.current_pdf_viewer = lambda: object()
    MainWindow._update_editor_commands(win)
    assert not win.act_paste.isEnabled() and not win.act_bold.isEnabled()
    assert win.act_copy.isEnabled()                    # copies from the PDF
    assert win.act_find.isEnabled()                    # finds in the PDF
    assert not win.act_replace.isEnabled()             # but replaces nothing there
    win.current_pdf_viewer = lambda: None              # a picture: nothing to find
    MainWindow._update_editor_commands(win)
    assert not win.act_find.isEnabled()

    win.current_editor = lambda: _Editor("/notes/a.txt")
    MainWindow._update_editor_commands(win)
    assert win.act_paste.isEnabled() and not rich_only.isEnabled()

    win.current_editor = lambda: _Editor("/notes/a.md")
    MainWindow._update_editor_commands(win)
    assert rich_only.isEnabled()


def test_the_pdf_reader_keeps_its_copy_key(qt_app):
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    from pdf_viewer import _ReaderView
    view = _ReaderView()
    copy = QKeySequence(QKeySequence.StandardKey.Copy)[0]
    event = QKeyEvent(QEvent.Type.ShortcutOverride, copy.key(), copy.keyboardModifiers())
    event.ignore()
    view.event(event)
    assert event.isAccepted()
    other = QKeyEvent(QEvent.Type.ShortcutOverride, Qt.Key.Key_B, Qt.KeyboardModifier.ControlModifier)
    other.ignore()
    view.event(other)
    assert not other.isAccepted()



def test_paragraph_styles_use_each_platform_keys(fresh_window):
    expected = "Ctrl+Alt+1" if sys.platform == "darwin" else "Ctrl+1"
    action = fresh_window.commands.action("format.style.h1")
    assert action.shortcut().toString(QKeySequence.SequenceFormat.PortableText) == expected
    assert [a.objectName() for a in fresh_window.m_style.actions()] == [
        "format.style.body", "format.style.h1", "format.style.h2", "format.style.h3"]
    # Structure commands are off where there is no Markdown to hold them.
    assert action in fresh_window._rich_actions



@pytest.mark.parametrize("platform, keys", [
    ("darwin", {"format.strike": "Ctrl+Shift+X", "format.list.bullet": "Ctrl+Shift+8",
                "format.list.number": "Ctrl+Shift+7", "format.quote": "Ctrl+'",
                "format.style.h2": "Ctrl+Alt+2", "format.style.body": "Ctrl+Alt+0",
                "format.style.h1": "Ctrl+Alt+1", "format.style.h3": "Ctrl+Alt+3",
                "edit.paste_plain": "Ctrl+Alt+Shift+V", "file.page_setup": "Ctrl+Shift+P",
                "format.list.task": "Ctrl+Shift+9",
                "search.use_selection": "Ctrl+E", "format.reset": "Ctrl+D",
                "search.replace": "Ctrl+Alt+F",
                "insert.link": "Ctrl+K"}),
    ("win32", {"format.strike": "Alt+Shift+5", "format.list.bullet": "Ctrl+Shift+8",
               "format.list.number": "Ctrl+Shift+7", "format.quote": "Ctrl+'",
               "format.style.h2": "Ctrl+2", "edit.paste_plain": "Ctrl+Shift+V",
               "search.use_selection": "", "format.reset": "Ctrl+D",
               "search.replace": "Ctrl+H",
               "insert.link": "Ctrl+K"}),
    ("linux", {"format.list.bullet": "Ctrl+Shift+8", "format.list.number": "Ctrl+Shift+7",
               "format.list.task": "Ctrl+Shift+9", "file.page_setup": "",
               "format.quote": "Ctrl+'", "format.strike": "Alt+Shift+5", "format.reset": "Ctrl+D",
               "format.style.body": "Ctrl+0"}),
])
def test_each_platform_gets_its_own_conventions(qt_app, monkeypatch, platform, keys):
    import types
    from main_window import MainWindow
    monkeypatch.setattr(commands, "sys", types.SimpleNamespace(platform=platform))
    win = StandIn()
    MainWindow._build_actions(win)        # no two commands share a key here either
    for command_id, expected in keys.items():
        shortcut = win.commands.action(command_id).shortcut()
        assert shortcut.toString(QKeySequence.SequenceFormat.PortableText) == expected, \
            command_id



@pytest.mark.parametrize("path, keeps", [
    ("/notes/page.html", True), ("/notes/page.HTM", True), ("/notes/a.md", False),
    ("/notes/a.txt", False), (None, False),
])
def test_underline_is_offered_only_where_the_file_keeps_it(fresh_window, path, keeps):
    from main_window import MainWindow
    win = fresh_window
    win._editor_kind = lambda ed: MainWindow._editor_kind(win, ed)
    win._keeps_underline = MainWindow._keeps_underline
    win.current_editor = lambda: _Editor(path)
    MainWindow._update_editor_commands(win)
    assert win.act_underline.isEnabled() is keeps
    assert "underline" not in __import__("format_toolbar").LAYOUT



def test_the_welcome_page_names_the_keys_the_platform_way(fresh_window):
    # Review L7: it said "Ctrl+N" on a Mac too.
    from main_window import MainWindow
    from welcome import welcome_html
    keys = MainWindow._welcome_keys(fresh_window)
    native = fresh_window.act_new.shortcut().toString(QKeySequence.SequenceFormat.NativeText)
    assert keys["file.new"] == native
    page = welcome_html(keys)
    assert f"<b>{native}</b>" in page
    assert "<b>⌘N</b>" in welcome_html({"file.new": "⌘N"})


def test_the_welcome_page_says_where_publishing_lives_and_sells_no_local_colors():
    """The first page names the writing keys and, in one line of the same
    list, where publishing starts (the Nostr menu, the one way in), with
    nothing of Nostr to click and none of its words (review, decision 2:
    a 50-word paragraph about signer apps, Drafts and Imports was the most
    Nostr anyone saw before choosing it). Colors stay in local files and
    are not advertised."""
    from welcome import welcome_html
    page = welcome_html()
    assert "View &gt; Appearance" in page or "View > Appearance" in page
    nostr = [line for line in page.splitlines() if "Nostr" in line]
    assert nostr == ["        <li>Nostr menu - publish your writing, when you want to</li>"]
    assert "signer" not in page and "Drafts" not in page and "Imports" not in page
    assert "<a " not in page                       # nothing to click: the menu is the door
    assert "Colors" not in page and "color:" not in page


def test_close_tab_answers_every_key_the_system_binds_to_close(main_window_commands):
    # On Windows, apps with tabs close one with Ctrl+F4 as well as Ctrl+W;
    # Close Tab takes the system's own list, as the other windows' Close does.
    # Its main key, the one menus and Keyboard Shortcuts show, stays Ctrl+W
    # (Command-W) on every system, as in Notepad and Word (review S2: Qt's
    # Windows list starts with Ctrl+F4, which the test platform uses).
    action = main_window_commands.act_close_tab
    keys = {k.toString() for k in action.shortcuts()}
    expected = {k.toString() for k in QKeySequence.keyBindings(QKeySequence.StandardKey.Close)}
    assert keys == expected and "Ctrl+W" in keys
    assert action.shortcut() == QKeySequence("Ctrl+W")
    listed = dict((words, keys) for _group, rows in main_window_commands.commands.shortcut_groups()
                  for keys, words in rows)
    assert listed["Close tab"] == "Ctrl+W"
