# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Skip, with Undo (the owner's decision Q-12).

Skip sets posts aside on this computer: the button, or Delete or
Backspace in the list. A message says how many, with Undo; Edit > Undo
(Command-Z or Ctrl+Z) undoes it too, unless the text being typed has
something to undo. In the Skipped list the button brings posts back.
The app's menu bar acts on the Imports window while it is in front.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtTest import QTest

from commands import undo_words
from main_window import MainWindow
from nostr.imports.inbox_store import SKIPPED_POSTS
from nostr.ui.imports_sidebar import FILE, LIST
from nostr.ui.imports_window import ImportsWindow
from tests.outbox_fakes import settle
from tests.test_imports_create import Jobs, make_controller
from tests.widget_lifetime import delete_new_windows


@pytest.fixture(autouse=True)
def _windows_deleted():
    """Every window and panel a test makes is deleted after it: left to
    the cycle collector, one without a parent can crash it."""
    yield from delete_new_windows()


@pytest.fixture
def window(tmp_path):
    controller = make_controller(tmp_path, Jobs())
    win = ImportsWindow(controller)
    win.resize(1180, 760)
    win.show()
    settle()
    yield win
    win.close()
    controller.account_changed(None)
    settle()


def titles(win):
    model = win.posts.model_
    return [model.post(r).title for r in range(model.rowCount())]


def undo_action(win):
    undo = QKeySequence(QKeySequence.StandardKey.Undo)
    return next(a for a in win.actions() if a.shortcut() == undo)


class TestSkip:
    def test_the_button_skips_the_open_post_and_says_so(self, window):
        first = titles(window)[0]
        assert window.action_bar.skip.text() == "Skip"
        window.action_bar.skip.click()
        assert first not in titles(window)
        assert window._controller.counts().skipped == 1
        assert window.banner.label.text() == "Skipped 1 post."
        assert window.banner.action.text() == "Undo"

    def test_undo_in_the_message(self, window):
        first = titles(window)[0]
        window.action_bar.skip.click()
        window.banner.action.click()
        assert first in titles(window)
        assert window._controller.counts().skipped == 0
        assert not window.banner.isVisibleTo(window)

    def test_delete_skips_the_checked_posts(self, window):
        model = window.posts.model_
        model.set_checked(0, True)
        model.set_checked(1, True)
        window.posts.setFocus()
        QTest.keyClick(window.posts, Qt.Key.Key_Delete)
        assert window._controller.counts().skipped == 2
        assert window.banner.label.text() == "Skipped 2 posts."
        assert model.checked() == []

    def test_edit_undo_undoes_the_skip(self, window):
        before = titles(window)
        window.posts.setFocus()
        QTest.keyClick(window.posts, Qt.Key.Key_Backspace)
        # Named where the platform names it (commands.undo_words).
        assert window.can_undo() and window.undo_text() == undo_words("Skip")
        undo_action(window).trigger()
        assert titles(window) == before
        assert not window.can_undo()

    def test_undo_while_typing_undoes_the_typing(self, window):
        window.posts.setFocus()
        QTest.keyClick(window.posts, Qt.Key.Key_Delete)
        window.search.setFocus()
        QTest.keyClicks(window.search, "ab")
        assert window.undo_text() == "Undo"
        undo_action(window).trigger()
        assert window.search.text() != "ab"
        # The skip is still there to undo from the list.
        assert window._controller.counts().skipped == 1

    def test_restore_in_the_skipped_list(self, window):
        window.action_bar.skip.click()
        window.sidebar.select(LIST, SKIPPED_POSTS)
        assert window.action_bar.skip.text() == "Restore"
        window.action_bar.skip.click()
        assert window._controller.counts().skipped == 0
        assert window.banner.label.text() == "Restored 1 post."

    def test_the_keys_restore_nothing_in_skipped(self, window):
        """CP2 review L-b: Delete or Backspace restored posts in Skipped,
        the opposite of what they mean, with no Undo; and Undo Skip then
        stood for an older skip."""
        window.action_bar.skip.click()
        assert window.can_undo()
        window.sidebar.select(LIST, SKIPPED_POSTS)
        window.posts.setFocus()
        window.posts.setCurrentIndex(window.posts.model_.index(0))
        QTest.keyClick(window.posts, Qt.Key.Key_Delete)
        QTest.keyClick(window.posts, Qt.Key.Key_Backspace)
        assert window._controller.counts().skipped == 1
        window.action_bar.skip.click()          # Restore, the button
        assert window._controller.counts().skipped == 0
        assert not window.can_undo()

    def test_files_are_not_skipped(self, window):
        from tests.imports_fakes import make_item
        collection = window._controller.open_posts(kind="file", label="blog.xml",
                                                   items=[make_item("One", guid="g1")])
        window.show_view(FILE, collection.id)
        assert window.action_bar.skip.isHidden()

    def test_a_window_that_only_shows_skips_nothing(self, window):
        window._controller.read_only = True
        window.skip_or_restore()
        assert window._controller.counts().skipped == 0


class TestTheMenuBar:
    """The app's menu bar acts on the Imports window while it is in front."""

    def stand_in(self, window):
        return SimpleNamespace(
            _imports_in_front=lambda: window, _front_window=lambda: window,
            current_editor=lambda: None, current_pdf_viewer=lambda: None)

    def test_edit_undo(self, window):
        window.posts.setFocus()
        QTest.keyClick(window.posts, Qt.Key.Key_Delete)
        MainWindow._undo(self.stand_in(window))
        assert window._controller.counts().skipped == 0

    def test_edit_select_all_and_delete(self, window, monkeypatch):
        import main_window
        monkeypatch.setattr(main_window.QApplication, "focusWidget",
                            staticmethod(lambda: window.posts))
        MainWindow._edit_focused(self.stand_in(window), "select_all")
        assert len(window.posts.model_.checked()) == window.posts.model_.rowCount()
        MainWindow._edit_focused(self.stand_in(window), "delete")
        assert window._controller.counts().inbox == 0

    def test_find_and_close(self, window):
        closed = []
        window.close = lambda: closed.append(True)
        stand_in = self.stand_in(window)
        MainWindow._show_find(stand_in)
        assert window.focusWidget() is window.search
        MainWindow._close_current_tab(stand_in)
        assert closed == [True]

    def test_the_undo_item_says_what_it_undoes(self, window):
        from PySide6.QtGui import QAction
        stand_in = self.stand_in(window)
        stand_in.act_undo, stand_in.act_redo = QAction("Undo"), QAction("Redo")
        window.posts.setFocus()
        QTest.keyClick(window.posts, Qt.Key.Key_Delete)
        MainWindow._update_undo_redo_buttons(stand_in)
        assert stand_in.act_undo.text() == undo_words("Skip")
        assert stand_in.act_undo.isEnabled()
        window.undo_skip()
        MainWindow._update_undo_redo_buttons(stand_in)
        assert stand_in.act_undo.text() == "Undo"
        assert not stand_in.act_undo.isEnabled()


def test_the_documents_commands_are_dimmed_while_imports_is_in_front(window, monkeypatch):
    """Review M9: the menu bar is the app's while the Imports window is
    in front; what it cannot do there is dimmed, and View > Show Sidebar
    works for it."""
    from PySide6.QtWidgets import QMainWindow, QMenu
    from commands import EDIT, FILE, FORMAT, VIEW, Command, CommandRegistry
    host_window = QMainWindow()
    registry = CommandRegistry(host_window)
    save = registry.add(Command("file.save", "Save", FILE, "Ctrl+S"))
    bold = registry.add(Command("format.bold", "Bold", FORMAT, "Ctrl+B"))
    copy = registry.add(Command("edit.copy", "Copy", EDIT))
    sidebar = registry.add(Command("view.sidebar", "Show Sidebar", VIEW), enabled=False)
    front = {"window": window}
    from PySide6.QtGui import QAction
    fullscreen = QAction("Full Screen")
    host = SimpleNamespace(
        commands=registry, act_window_sidebar=sidebar, _m_background=QMenu(),
        act_fullscreen=fullscreen, isFullScreen=lambda: False, act_close_tab=QAction("Close"),
        _imports_in_front=lambda: front["window"], _update_knit_actions=lambda: None,
        _update_format_buttons=lambda: None,
        _DOCUMENT_COMMANDS=MainWindow._DOCUMENT_COMMANDS,
        _DOCUMENT_PREFIXES=MainWindow._DOCUMENT_PREFIXES)
    host._is_document_command = lambda cid: MainWindow._is_document_command(host, cid)
    host._update_window_commands = lambda: MainWindow._update_window_commands(host)
    host._name_full_screen = lambda: MainWindow._name_full_screen(host)
    bold.setEnabled(False)          # as the document left it
    MainWindow._update_window_commands(host)
    assert fullscreen.text() == "Enter Full Screen"     # named for the window in front
    assert not save.isEnabled() and not bold.isEnabled()
    assert copy.isEnabled()          # acts on the window in front
    assert sidebar.isEnabled() and sidebar.text() == "Hide Sidebar"
    assert host.act_close_tab.isEnabled()       # File > Close closes it
    assert host.act_close_tab.text() == "Close Window"      # S5: it has no tabs
    import main_window
    window.follow_website()
    sheet = window.sheet()
    monkeypatch.setattr(main_window.QApplication, "activeWindow", staticmethod(lambda: sheet))
    MainWindow._update_window_commands(host)
    assert not host.act_close_tab.isEnabled()   # but not under its sheet (CP3 review M2)
    sheet.reject()
    monkeypatch.setattr(main_window.QApplication, "activeWindow", staticmethod(lambda: None))
    MainWindow._update_window_commands(host)
    assert host.act_close_tab.isEnabled()
    MainWindow._toggle_window_sidebar(host)
    assert not window.act_sidebar.isChecked()
    assert sidebar.text() == "Show Sidebar"
    window.showFullScreen()
    MainWindow._update_window_commands(host)
    assert fullscreen.text() == "Exit Full Screen"
    window.showNormal()
    front["window"] = None           # the editor's window in front again
    MainWindow._update_window_commands(host)
    assert fullscreen.text() == "Enter Full Screen"
    assert save.isEnabled() and not bold.isEnabled()
    assert sidebar.isEnabled()           # the folder sidebar's, in this window
    assert host.act_close_tab.text() == "Close Tab"


def test_a_sheet_of_imports_in_front_counts_as_imports_in_front(window, monkeypatch):
    """CP2 review L-a: with a sheet of the Imports window up, the window
    itself is not the active one, so the document's commands came back
    and Edit > Undo undid text in the document behind."""
    import main_window
    window.follow_website()
    sheet = window.sheet()
    host = SimpleNamespace(_imports=SimpleNamespace(window=lambda: window))
    monkeypatch.setattr(main_window.QApplication, "activeWindow", staticmethod(lambda: sheet))
    assert MainWindow._imports_in_front(host) is window
    monkeypatch.setattr(main_window.QApplication, "activeWindow", staticmethod(lambda: None))
    assert MainWindow._imports_in_front(host) is None
    sheet.reject()


def test_a_sheet_takes_the_edit_commands_and_keeps_its_window(window, monkeypatch):
    """CP3 review M2: with Source Settings up, Edit > Undo undid the skip
    behind the sheet instead of the typing in it, Edit > Delete skipped a
    post behind it, and File > Close closed the window under it. As in
    Mail, a sheet takes the Edit commands for its text field, does nothing
    else with them, and its window is not closed under it."""
    import main_window
    from PySide6.QtGui import QAction
    window.posts.setFocus()
    QTest.keyClick(window.posts, Qt.Key.Key_Delete)
    controller = window._controller
    assert controller.counts().skipped == 1
    window.source_settings(controller.sources()[0].key)
    sheet = window.sheet()
    monkeypatch.setattr(main_window.QApplication, "activeWindow", staticmethod(lambda: sheet))
    sheet.name.setFocus()
    monkeypatch.setattr(main_window.QApplication, "focusWidget",
                        staticmethod(lambda: sheet.name))
    QTest.keyClicks(sheet.name, " renamed")
    typed = sheet.name.text()
    closed = []
    window.close = lambda: closed.append(True)
    host = SimpleNamespace(
        _imports_in_front=lambda: window, _front_window=lambda: sheet,
        current_editor=lambda: None, current_pdf_viewer=lambda: None,
        act_undo=QAction("Undo"), act_redo=QAction("Redo"))
    MainWindow._update_undo_redo_buttons(host)
    assert host.act_undo.text() == "Undo" and host.act_undo.isEnabled()
    MainWindow._undo(host)
    assert sheet.name.text() != typed                 # the typing is undone
    assert controller.counts().skipped == 1           # the skip behind stays
    monkeypatch.setattr(main_window.QApplication, "focusWidget",
                        staticmethod(lambda: sheet.copy_images))
    inbox = controller.counts().inbox
    for name in ("delete", "select_all"):
        MainWindow._edit_focused(host, name)
    assert controller.counts().inbox == inbox and not window.posts.model_.checked()
    MainWindow._update_undo_redo_buttons(host)
    assert not host.act_undo.isEnabled()              # nothing of the window behind
    MainWindow._undo(host)
    assert controller.counts().skipped == 1
    MainWindow._close_current_tab(host)
    assert closed == [] and sheet.isVisible()
    sheet.reject()


def test_the_edit_menu_follows_the_focused_field(window, monkeypatch):
    """N2, S4 and T2 of the CP3 fixes reviews: with a sheet up, the Edit menu
    kept the state it had when the sheet opened (Undo dimmed after typing
    in it), with a check box focused Cut, Copy and Delete followed the
    document in the other window, and a selection in the article did not
    enable Copy, and Paste stayed enabled where nothing takes a paste. As
    in Mail, they follow the field with the focus as it
    changes, through the one place that follows focused fields."""
    import main_window
    from PySide6.QtCore import QObject
    from PySide6.QtGui import QAction
    from PySide6.QtWidgets import QWidget
    from editor import HtmlEditor
    document = HtmlEditor()
    document.setPlainText("Text in the document")
    document.selectAll()

    class Host(QObject):
        """The menu bar's part of MainWindow, with its own updaters."""
        _follow_field = MainWindow._follow_field
        _note_side_in_use = MainWindow._note_side_in_use
        _on_field_changed = MainWindow._on_field_changed
        _update_clipboard_commands = MainWindow._update_clipboard_commands
        _update_undo_redo_buttons = MainWindow._update_undo_redo_buttons
        _update_editor_commands = MainWindow._update_editor_commands

    host = Host()
    host.tabs, host.current_editor = QWidget(), lambda: document
    host._imports_in_front, host.current_pdf_viewer = lambda: window, lambda: None
    host.act_undo, host.act_redo = QAction("Undo"), QAction("Redo")
    host.act_cut, host.act_copy = QAction("Cut"), QAction("Copy")
    host.act_delete, host.act_paste = QAction("Delete"), QAction("Paste")
    host.act_paste_plain = QAction("Paste and Match Style")
    focus = {"widget": None}
    monkeypatch.setattr(main_window.QApplication, "focusWidget",
                        staticmethod(lambda: focus["widget"]))

    def focus_on(widget):
        focus["widget"] = widget
        MainWindow._on_focus_changed(host, None, widget)

    from PySide6.QtWidgets import QApplication
    QApplication.clipboard().setText("PASTED")
    focus_on(window.posts)                              # T2: nothing here takes a paste
    assert not (host.act_paste.isEnabled() or host.act_paste_plain.isEnabled())
    window.source_settings(window._controller.sources()[0].key)
    sheet = window.sheet()
    monkeypatch.setattr(main_window.QApplication, "activeWindow", staticmethod(lambda: sheet))
    focus_on(sheet.name)
    assert host.act_paste.isEnabled() and host.act_paste_plain.isEnabled()
    sheet.name.end(False)
    QTest.keyClicks(sheet.name, "!")                    # the menu hears it at once
    assert host.act_undo.isEnabled()
    assert not (host.act_cut.isEnabled() or host.act_copy.isEnabled()
                or host.act_delete.isEnabled())         # nothing selected in Name
    sheet.name.selectAll()
    assert host.act_cut.isEnabled() and host.act_copy.isEnabled()
    focus_on(sheet.copy_images)                         # a check box of the sheet
    assert not (host.act_undo.isEnabled() or host.act_cut.isEnabled()
                or host.act_copy.isEnabled() or host.act_delete.isEnabled()
                or host.act_paste.isEnabled())
    sheet.reject()
    monkeypatch.setattr(main_window.QApplication, "activeWindow", staticmethod(lambda: window))
    pane = window.article._preview
    focus_on(pane)
    assert not host.act_copy.isEnabled()
    pane.selectAll()                                    # text selected in the article
    assert host.act_copy.isEnabled() and not host.act_cut.isEnabled()
    assert not host.act_paste.isEnabled()               # it is read only
    focus_on(None)
    document.deleteLater()


def test_edit_delete_skips_only_where_the_key_does(window, monkeypatch):
    """S2 of the CP3 fixes review: Edit > Delete restored posts in Skipped,
    with no Undo, and skipped the open post with the sidebar focused. It
    does what the Delete key does in the post list, and nothing else."""
    import main_window
    from PySide6.QtGui import QAction
    focus = {"widget": window.posts}
    monkeypatch.setattr(main_window.QApplication, "focusWidget",
                        staticmethod(lambda: focus["widget"]))
    host = SimpleNamespace(
        _imports_in_front=lambda: window, current_editor=lambda: None,
        current_pdf_viewer=lambda: None, act_cut=QAction("Cut"), act_copy=QAction("Copy"),
        act_delete=QAction("Delete"), act_paste=QAction("Paste"),
        act_paste_plain=QAction("Paste and Match Style"))
    controller = window._controller
    MainWindow._update_clipboard_commands(host)
    assert host.act_delete.isEnabled()                  # the post list: Skip
    focus["widget"] = window.sidebar
    MainWindow._update_clipboard_commands(host)
    assert not host.act_delete.isEnabled()
    MainWindow._edit_focused(host, "delete")
    assert controller.counts().skipped == 0
    focus["widget"] = window.posts
    MainWindow._edit_focused(host, "delete")
    assert controller.counts().skipped == 1
    window.sidebar.select(LIST, SKIPPED_POSTS)
    window.posts.setCurrentIndex(window.posts.model_.index(0))
    MainWindow._update_clipboard_commands(host)
    assert not host.act_delete.isEnabled()              # Restore is the button
    MainWindow._edit_focused(host, "delete")
    assert controller.counts().skipped == 1


def test_copy_copies_the_link_of_every_post_an_action_applies_to(window):
    """S3 of the CP3 fixes review: after Select All, Copy put only the open
    post's link on the clipboard. As Mail copies every selected message,
    it copies the link of each post, one per line."""
    from PySide6.QtWidgets import QApplication
    from tests.imports_fakes import make_item
    controller = window._controller
    key = controller.sources()[0].key
    controller.inbox.ingest(key, [
        make_item("Linked one", guid="l1", link="https://journal.example/1"),
        make_item("Linked two", guid="l2", link="https://journal.example/2")])
    controller.posts_changed.emit()                     # the window shows them
    settle()
    model = window.posts.model_
    linked = {model.post(r).link: r for r in range(model.rowCount()) if model.post(r).link}
    assert sorted(linked) == ["https://journal.example/1", "https://journal.example/2"]
    window.posts.setCurrentIndex(model.index(linked["https://journal.example/1"]))
    assert window.can_edit("copy")
    window.edit("copy")
    assert QApplication.clipboard().text() == "https://journal.example/1"
    window.edit("select_all")
    window.edit("copy")
    assert sorted(QApplication.clipboard().text().split("\n")) == sorted(linked)


def test_full_screen_acts_on_the_window_in_front(window):
    """CP2 review L-a: View > Full Screen made the editor window behind
    full screen while the Imports window was in front."""
    host = SimpleNamespace(_imports_in_front=lambda: window, _name_full_screen=lambda: None)
    MainWindow._toggle_fullscreen(host)
    assert window.isFullScreen()
    MainWindow._toggle_fullscreen(host)
    assert not window.isFullScreen()



def test_command_w_closes_the_window(window):
    # Every key the system binds to Close, Ctrl+W included (a shortcut set
    # from the standard key alone got only Ctrl+F4 on Windows).
    QTest.keyClick(window, Qt.Key_W, Qt.ControlModifier)
    assert not window.isVisible()


@pytest.mark.parametrize("command_id", [
    "nostr.insert_mention", "edit.footnote_jump", "view.write", "view.preview", "view.split"])
def test_the_commands_that_act_on_the_document_rest_while_imports_is_in_front(command_id):
    # Review of the merge: with Imports in front, Insert Mention… stayed
    # enabled and acted on the document behind, as did the footnote jump
    # and Write, Preview and Split; Publish and Insert Image were dimmed.
    assert MainWindow._is_document_command(MainWindow, command_id)
