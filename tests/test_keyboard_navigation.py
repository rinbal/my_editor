# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins moving around MyEditor by key: tabs and panes.

What must hold:

  The next and the previous tab: Control-Tab and Shift-Command-] on a
  Mac (Safari), Ctrl+Tab and Ctrl+PgDown elsewhere (Firefox, GNOME); from
  the last tab the next is the first. Tab 1 to 8 and the last tab:
  Command-1 to 9 on a Mac, Alt+1 to 9 elsewhere (Ctrl+1 is Heading 1
  there). The tab's document takes the keys.

  F6 and Shift+F6 move the keyboard focus to the next and the previous
  pane shown: the document, Article Details, the Drafts panel.

  The PDF reader's own keys never take a command's key, but those of the
  editing commands, which are off in a PDF tab: on a Mac, Command-1 and
  2 go to tabs there too, and Actual Size and Fit Page are
  Option-Command-1 and 2.
"""

import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QKeySequence  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QLineEdit, QListWidget, QSplitter, QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

import commands  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    return QApplication.instance() or QApplication([])


def keys_of(win, command_id):
    return [k.toString(QKeySequence.SequenceFormat.PortableText)
            for k in win.commands.action(command_id).shortcuts()]


@pytest.mark.parametrize("platform, expected", [
    ("darwin", {"nav.next_tab": ["Meta+Tab", "Ctrl+}"],
                "nav.previous_tab": ["Meta+Shift+Tab", "Ctrl+{"],
                "nav.tab_1": ["Ctrl+1"], "nav.tab_8": ["Ctrl+8"], "nav.last_tab": ["Ctrl+9"],
                "nav.next_pane": ["F6"], "nav.previous_pane": ["Shift+F6"]}),
    ("win32", {"nav.next_tab": ["Ctrl+Tab", "Ctrl+PgDown"],
               "nav.previous_tab": ["Ctrl+Shift+Tab", "Ctrl+PgUp"],
               "nav.tab_1": ["Alt+1"], "nav.last_tab": ["Alt+9"],
               "nav.next_pane": ["F6"], "nav.previous_pane": ["Shift+F6"]}),
    ("linux", {"nav.next_tab": ["Ctrl+Tab", "Ctrl+PgDown"], "nav.tab_3": ["Alt+3"]}),
])
def test_tabs_and_panes_have_each_platforms_keys(monkeypatch, platform, expected):
    from main_window import MainWindow
    from tests.test_commands import StandIn
    monkeypatch.setattr(commands, "sys", types.SimpleNamespace(platform=platform))
    win = StandIn()
    MainWindow._build_actions(win)          # the PDF reader's keys are checked here too
    for command_id, keys in expected.items():
        assert keys_of(win, command_id) == keys, (platform, command_id)


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_the_pdf_reader_never_takes_a_tab_key(monkeypatch, platform):
    from pdf_viewer import reader_keys
    monkeypatch.setattr(commands, "sys", types.SimpleNamespace(platform=platform))
    zoom = {words: tab for _s, words, _a, tab, _v in reader_keys()}
    mac = platform == "darwin"
    # Command-0 is Actual Size on a Mac, as in Preview and View > Actual Size.
    assert zoom["Actual size"] == (("Ctrl+0", "Ctrl+Alt+1") if mac else ("Ctrl+1",))
    assert zoom["Fit whole page"] == (("Ctrl+Alt+2",) if mac else ("Ctrl+2",))
    assert zoom["Fit page width"] == (("Ctrl+Alt+0",) if mac else ("Ctrl+0",))


def test_a_reader_key_on_a_command_that_stays_on_is_refused():
    from PySide6.QtWidgets import QMainWindow
    registry = commands.CommandRegistry(QMainWindow())
    registry.add(commands.Command("nav.tab_1", "Go to Tab 1", commands.NAVIGATION, "Ctrl+1"))
    registry.add(commands.Command("format.style.h1", "Heading 1", commands.FORMAT, "Ctrl+2"))
    with pytest.raises(ValueError, match="nav.tab_1"):
        registry.refuse_clashes("the PDF reader", ["Ctrl+1"])
    registry.refuse_clashes("the PDF reader", ["Ctrl+2"], yielding={"format.style.h1"})
    with pytest.raises(ValueError, match="twice"):
        registry.refuse_clashes("the PDF reader", ["J", "J"])


class Window(QWidget):
    """The parts of MainWindow that moving by key uses: tabs of editors,
    and the panes beside them."""

    def __init__(self):
        super().__init__()
        self.tabs = QTabWidget()
        self.editors = [QTextEdit() for _ in range(3)]
        for number, editor in enumerate(self.editors, 1):
            self.tabs.addTab(editor, f"Tab {number}")
        document = QWidget()
        QVBoxLayout(document).addWidget(self.tabs)
        self._editor_side = document
        self._article_details_panel = QWidget()
        self.title = QLineEdit(self._article_details_panel)
        self._drafts_panel = QWidget()
        self.drafts = QListWidget(self._drafts_panel)
        self._central_splitter = QSplitter()
        for pane in (document, self._article_details_panel, self._drafts_panel):
            self._central_splitter.addWidget(pane)
        QVBoxLayout(self).addWidget(self._central_splitter)

    def current_editor(self):
        return self.tabs.currentWidget()

    def current_pdf_viewer(self):
        return None

    def current_reader(self):
        return None

    def _sheet_from_widget(self, widget):
        return None


@pytest.fixture
def window(qt_app):
    from main_window import MainWindow
    win = Window()
    for name in ("_show_tab", "_panes", "_move_to_pane", "_pane_focus_target"):
        setattr(win, name, types.MethodType(getattr(MainWindow, name), win))
    win.show()
    win.activateWindow()
    qt_app.processEvents()
    yield win
    win.close()
    win.deleteLater()


def test_tabs_go_round_and_their_document_takes_the_keys(window, qt_app):
    window._show_tab(window.tabs.currentIndex() + 1)
    assert window.tabs.currentIndex() == 1
    assert qt_app.focusWidget() is window.editors[1]
    window._show_tab(window.tabs.currentIndex() - 2)        # from the first, the last
    assert window.tabs.currentIndex() == 2
    window._show_tab(3)                                     # past the last: the first
    assert window.tabs.currentIndex() == 0
    window._show_tab(7, wrap=False)                         # tab 8 of 3: nothing
    assert window.tabs.currentIndex() == 0


def test_f6_visits_the_panes_shown(window, qt_app):
    window.editors[0].setFocus()
    window._move_to_pane(+1)
    assert qt_app.focusWidget() is window.title             # Article Details
    window._move_to_pane(+1)
    assert qt_app.focusWidget() is window.drafts            # the Drafts panel's list
    window._move_to_pane(+1)
    assert qt_app.focusWidget() is window.editors[0]        # round to the document
    window._move_to_pane(-1)
    assert qt_app.focusWidget() is window.drafts
    window._drafts_panel.hide()
    window.title.setFocus()
    window._move_to_pane(+1)
    assert qt_app.focusWidget() is window.editors[0]        # a hidden pane is skipped


SCRIPT = r"""
import json, os, sys, types
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv[:1])
import commands
commands.sys = types.SimpleNamespace(platform=sys.argv[2])
import main_window

w = main_window.MainWindow()
w.resize(1100, 720)
w.show()
w.activateWindow()
QTest.qWaitForWindowExposed(w)
while w.tabs.count() < 3:
    w.new_tab()
w.tabs.setCurrentIndex(0)
w.current_editor().setFocus()
app.processEvents()
C, S, A, M = (Qt.KeyboardModifier.ControlModifier, Qt.KeyboardModifier.ShiftModifier,
              Qt.KeyboardModifier.AltModifier, Qt.KeyboardModifier.MetaModifier)
result = {}
texts = lambda: [w._editor_from_widget(w.tabs.widget(i)).toPlainText()
                 for i in range(w.tabs.count())]
before = texts()

def press(key, mods=Qt.KeyboardModifier.NoModifier):
    QTest.keyClick(app.focusWidget(), key, mods)
    app.processEvents()
    return w.tabs.currentIndex()

if sys.argv[2] == "darwin":
    result["next"] = press(Qt.Key.Key_Tab, M)
    result["previous"] = press(Qt.Key.Key_Backtab, M | S)
    result["number"] = press(Qt.Key.Key_3, C)
    # Shift-Command-]: a Mac's key mapper hands Qt "}" with Command (the
    # Shift spent on the brace), which the offscreen one does not do.
    result["bracket"] = press(Qt.Key.Key_BraceRight, C)
else:
    result["next"] = press(Qt.Key.Key_Tab, C)
    result["previous"] = press(Qt.Key.Key_Backtab, C | S)
    result["number"] = press(Qt.Key.Key_3, A)
    result["bracket"] = press(Qt.Key.Key_PageDown, C)
result["typed_nothing"] = texts() == before
for i in range(w.tabs.count()):
    e = w._editor_from_widget(w.tabs.widget(i))
    if e is not None:
        e.document().setModified(False)
w.close()
print("RESULT " + json.dumps(result))
"""


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_the_tab_keys_reach_the_window_from_the_document(tmp_path, platform):
    from tests.app_process import run_window_script
    r = run_window_script(SCRIPT, tmp_path, platform)
    assert r == {"next": 1, "previous": 0, "number": 2, "bracket": 0, "typed_nothing": True}
