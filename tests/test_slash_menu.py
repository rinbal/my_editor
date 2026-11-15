# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins the slash menu (slash_menu.py, HtmlEditor's slash handling and
CommandRegistry.rank).

What must hold:

  "/" opens the menu only at the start of a word, in a document that
  holds Markdown structure, outside code blocks and tables. It lists the
  window's commands by group (Text, Lists, Blocks, Insert), enabled ones
  only, Nostr ones only while Nostr is in use.

  The words after the slash narrow it, a word that starts with them
  first ("/he" puts the headings first). Up and Down move (the pointer
  too), Return or Tab picks: "/words" goes and the command runs there,
  as one step to undo. Escape closes it and keeps the text; so do a
  space right after the slash (a slash in a sentence), the caret
  leaving the words, three letters that match nothing, and a space
  after words that match nothing. Return after a bare slash ends the
  paragraph, unless Up or Down chose an item.

  The text keeps the keyboard focus, and screen readers hear the menu
  open, each choice with its place in the list, and the pick.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent, QTextCursor  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMainWindow  # noqa: E402

import commands  # noqa: E402
import rich_text  # noqa: E402
import slash_menu  # noqa: E402
from commands import Command, CommandRegistry  # noqa: E402
from editor import HtmlEditor  # noqa: E402
from markdown_writer import document_to_markdown, read_markdown  # noqa: E402
from slash_menu import SlashItem  # noqa: E402
from tests.accessibility import unnamed_controls  # noqa: E402
from tests.rich_text_helpers import press, type_text  # noqa: E402

from tests.app_process import run_window_script  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    return QApplication.instance() or QApplication([])


class Setup:
    """An editor with a small registry behind its slash menu."""

    def __init__(self, markdown: str = ""):
        self.window = QMainWindow()
        self.editor = HtmlEditor()
        self.window.setCentralWidget(self.editor)
        if markdown:
            read_markdown(self.editor.document(), markdown)
            self.editor.moveCursor(QTextCursor.MoveOperation.End)
        self.nostr = False
        self.registry = CommandRegistry(self.window)
        add = self.registry.add
        ed = self.editor
        for level, title in ((0, "Body"), (1, "Heading 1"), (2, "Heading 2"), (3, "Heading 3")):
            add(Command(f"format.style.{'body' if not level else f'h{level}'}", title,
                        commands.FORMAT, keywords=("h%d" % level,),
                        slash_section=slash_menu.TEXT),
                triggered=lambda n=level: ed.set_heading(n))
        add(Command("format.list.bullet", "Bulleted List", commands.FORMAT, "Ctrl+Shift+8",
                    keywords=("bullets", "list"), slash_section=slash_menu.LISTS),
            triggered=lambda: ed.toggle_list(rich_text.BULLET))
        add(Command("format.list.number", "Numbered List", commands.FORMAT,
                    keywords=("numbers", "list"), slash_section=slash_menu.LISTS),
            triggered=lambda: ed.toggle_list(rich_text.NUMBER))
        add(Command("format.quote", "Quote", commands.FORMAT, slash_section=slash_menu.BLOCKS),
            triggered=lambda: ed.toggle_quote())
        add(Command("insert.divider", "Divider", commands.INSERT, keywords=("rule",),
                    slash_section=slash_menu.BLOCKS),
            triggered=lambda: ed.insert_divider())
        add(Command("nostr.mention", "Mention", commands.NOSTR, nostr=True,
                    slash_section=slash_menu.INSERT), triggered=lambda: None)
        add(Command("file.save", "Save", commands.FILE), triggered=lambda: None)
        ed.set_slash_items(lambda query: [
            SlashItem(c.slash_section, self.registry.action(c.id))
            for c in self.registry.rank(query, nostr=self.nostr)])
        self.window.resize(600, 400)
        self.window.show()
        ed.setFocus()
        QApplication.processEvents()

    def titles(self):
        menu = self.editor.slash_menu()
        if menu is None:
            return None
        return [menu.list.item(i).text() for i in range(menu.list.count())]


def test_the_menu_opens_only_at_the_start_of_a_word():
    s = Setup()
    type_text(s.editor, "and/or")
    assert s.editor.slash_menu() is None
    type_text(s.editor, " /")
    assert s.editor.slash_menu() is not None
    s2 = Setup()
    type_text(s2.editor, "/")
    titles = s2.titles()
    assert titles[:2] == ["Text", "Body"]                   # by group, under its name
    assert "Save" not in titles                             # not a slash command
    assert unnamed_controls(s2.editor.slash_menu()) == []
    assert s2.editor.hasFocus()                             # the text keeps the keys


def test_the_words_after_the_slash_narrow_it_best_first():
    s = Setup()
    type_text(s.editor, "/he")
    assert s.titles()[:3] == ["Heading 1", "Heading 2", "Heading 3"]
    assert s.editor.accessibleDescription() == "Heading 1"
    type_text(s.editor, "/")                                # "/he/" matches nothing
    assert s.editor.slash_menu() is None


def test_return_runs_the_chosen_command_where_the_slash_was_as_one_undo_step():
    s = Setup("Intro\n\n")
    press(s.editor, Qt.Key.Key_Return)
    type_text(s.editor, "/he")
    press(s.editor, Qt.Key.Key_Down)
    assert s.editor.accessibleDescription() == "Heading 2"
    press(s.editor, Qt.Key.Key_Return)
    assert s.editor.slash_menu() is None
    assert s.editor.textCursor().block().blockFormat().headingLevel() == 2
    assert s.editor.toPlainText() == "Intro\n"
    s.editor.document().undo()                              # the pick, all of it
    assert s.editor.toPlainText() == "Intro\n/he"
    assert not s.editor.document().lastBlock().blockFormat().headingLevel()
    s.editor.document().redo()
    s.editor.moveCursor(QTextCursor.MoveOperation.End)
    type_text(s.editor, "Part")
    assert document_to_markdown(s.editor.document()) == "Intro\n\n## Part\n"


def test_tab_picks_too_and_divider_and_lists_work():
    s = Setup()
    type_text(s.editor, "/rule")
    press(s.editor, Qt.Key.Key_Tab)
    type_text(s.editor, "/list")
    press(s.editor, Qt.Key.Key_Return)
    type_text(s.editor, "item")
    assert document_to_markdown(s.editor.document()) == "---\n\n- item\n"


def test_escape_closes_and_keeps_the_text():
    s = Setup()
    type_text(s.editor, "/he")
    press(s.editor, Qt.Key.Key_Escape)
    assert s.editor.slash_menu() is None
    assert s.editor.toPlainText() == "/he"
    assert s.editor.accessibleDescription() == ""


@pytest.mark.parametrize("typed", ["/zzz", "/zz "])
def test_words_that_match_nothing_close_it(typed):
    s = Setup()
    type_text(s.editor, typed)
    assert s.editor.slash_menu() is None
    type_text(s.editor, "x")
    assert s.editor.slash_menu() is None                    # it stays closed
    assert s.editor.toPlainText() == typed + "x"


def test_the_caret_leaving_the_words_closes_it():
    s = Setup()
    type_text(s.editor, "/he")
    press(s.editor, Qt.Key.Key_Left)
    assert s.editor.slash_menu() is not None                # still inside "/he"
    s.editor.moveCursor(QTextCursor.MoveOperation.StartOfBlock)
    assert s.editor.slash_menu() is None


def test_nostr_commands_show_only_while_nostr_is_in_use():
    s = Setup()
    type_text(s.editor, "/men")
    assert s.editor.slash_menu() is None
    press(s.editor, Qt.Key.Key_Escape)
    s.nostr = True
    type_text(s.editor, " /men")
    assert s.titles()[0] == "Mention"


def test_disabled_commands_are_not_offered():
    s = Setup()
    s.registry.action("format.quote").setEnabled(False)
    type_text(s.editor, "/")
    assert "Quote" not in s.titles()


@pytest.mark.parametrize("markdown", ["```\ncode\n```\n", "| a | b |\n| --- | --- |\n| 1 | 2 |\n"])
def test_no_menu_in_code_or_a_table(markdown):
    s = Setup(markdown)
    cursor = s.editor.textCursor()
    cursor.setPosition(s.editor.toPlainText().index("code" if "code" in markdown else "1") + 1)
    s.editor.setTextCursor(cursor)
    type_text(s.editor, " /")
    assert s.editor.slash_menu() is None


def test_no_menu_in_a_plain_text_file():
    s = Setup()
    s.editor.set_structure_check(lambda: False)
    type_text(s.editor, "/")
    assert s.editor.slash_menu() is None


def test_a_click_on_a_row_picks_it():
    s = Setup()
    type_text(s.editor, "/he")
    menu = s.editor.slash_menu()
    row = menu.list.item(1)                                 # Heading 2
    rect = menu.list.visualItemRect(row)
    QTest.mouseClick(menu.list.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
    assert s.editor.textCursor().block().blockFormat().headingLevel() == 2
    assert s.editor.toPlainText() == ""


def test_the_ranking_prefers_a_word_that_starts_with_the_query():
    s = Setup()
    found = [c.title for c in s.registry.rank("li", nostr=False)]
    assert found[:2] == ["Bulleted List", "Numbered List"]
    assert [c.title for c in s.registry.rank("", nostr=False)][:4] == [
        "Body", "Heading 1", "Heading 2", "Heading 3"]
    assert s.registry.rank("zzz", nostr=False) == []



# -- a slash in a sentence (review H3) -------------------------------------------------

@pytest.mark.parametrize("typed, kept", [
    ("either / or", "either / or"),
    ("a / b", "a / b"),
    ("km / h", "km / h"),
    ("The root directory is /", "The root directory is /"),
])
def test_a_slash_in_a_sentence_keeps_the_text_and_return_ends_the_paragraph(typed, kept):
    s = Setup()
    type_text(s.editor, typed)
    press(s.editor, Qt.Key.Key_Return)
    type_text(s.editor, "next")
    assert s.editor.toPlainText() == kept + "\nnext"
    first = s.editor.document().begin()
    assert not first.blockFormat().headingLevel() and first.textList() is None


def test_a_space_right_after_the_slash_closes_the_menu():
    s = Setup()
    type_text(s.editor, "/")
    assert s.editor.slash_menu() is not None
    type_text(s.editor, " ")
    assert s.editor.slash_menu() is None
    type_text(s.editor, "or")
    assert s.editor.slash_menu() is None


def test_a_bare_slash_picks_what_up_or_down_chose():
    s = Setup()
    type_text(s.editor, "/")
    press(s.editor, Qt.Key.Key_Down)                        # Heading 1
    press(s.editor, Qt.Key.Key_Return)
    assert s.editor.textCursor().block().blockFormat().headingLevel() == 1
    assert s.editor.toPlainText() == ""


# -- what a screen reader hears (review M3) ---------------------------------------------

def test_screen_readers_hear_the_menu_the_choice_and_the_pick(monkeypatch):
    import announce
    heard = []
    monkeypatch.setattr(announce, "announce", lambda source, text: heard.append(text))
    s = Setup()
    type_text(s.editor, "/")
    count = len([t for t in s.titles() if t not in ("Text", "Lists", "Blocks", "Insert")])
    assert heard == [f"Insert menu. Body, 1 of {count}"]
    press(s.editor, Qt.Key.Key_Down)
    assert heard[-1] == f"Heading 1, 2 of {count}"
    type_text(s.editor, "he")                               # the list changes under it
    assert heard[-1] == "Heading 1, 1 of 3"
    press(s.editor, Qt.Key.Key_Return)
    assert heard[-1] == "Heading 1 applied"


def test_the_pointer_chooses_the_row_it_is_over():
    s = Setup()
    type_text(s.editor, "/he")
    menu = s.editor.slash_menu()
    rect = menu.list.visualItemRect(menu.list.item(2))      # Heading 3
    viewport = menu.list.viewport()
    QApplication.sendEvent(viewport, QMouseEvent(
        QEvent.Type.MouseMove, QPointF(rect.center()), QPointF(viewport.mapToGlobal(
            rect.center())), Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier))
    assert menu.chosen_title() == "Heading 3"


# -- in the window -------------------------------------------------------------------------

SCRIPT = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv[:1])
import main_window
from markdown_writer import document_to_markdown
from tests.rich_text_helpers import press, type_text

w = main_window.MainWindow()
w.show()
ed = w.new_tab()
ed.setFocus()
app.processEvents()
type_text(ed, "/")
menu = ed.slash_menu()
r = {"groups": [menu.list.item(i).text() for i in range(menu.list.count())
                if not menu.list.item(i).flags()]}
press(ed, Qt.Key.Key_Escape)
press(ed, Qt.Key.Key_Backspace)
type_text(ed, "/heading 2")
press(ed, Qt.Key.Key_Return)
type_text(ed, "Title")
r["markdown"] = document_to_markdown(ed.document())
txt = os.path.join(os.environ["HOME"], "plain.txt")
with open(txt, "w", encoding="utf-8") as f:
    f.write("plain\n")
plain = w.open_path(txt)
plain.moveCursor(plain.textCursor().MoveOperation.End)
type_text(plain, "/")
r["plain_menu"] = plain.slash_menu() is not None
w.tabs.setCurrentIndex(w._editor_widget_index(ed))
ed.setFocus()
type_text(ed, "\n/link")
press(ed, Qt.Key.Key_Return)
app.processEvents()
from link_popover import LinkPopover
popovers = [p for p in ed.findChildren(LinkPopover) if p.isVisible()]
r["link_popover"] = bool(popovers) and popovers[0].address_edit.hasFocus()
for p in popovers:
    p.close()
for e in (ed, plain):
    e.document().setModified(False)
w.close()
print("RESULT " + json.dumps(r))
"""


def test_the_window_offers_its_own_commands(tmp_path):
    r = run_window_script(SCRIPT, tmp_path)
    assert r["groups"] == ["Text", "Lists", "Blocks", "Insert"]
    assert r["markdown"] == "## Title\n"
    assert r["plain_menu"] is False
    assert r["link_popover"]                    # Add Link's panel keeps the focus it took


def test_photo_and_foto_offer_the_photo_group_first(tmp_path, monkeypatch):
    from tests.window_fixture import close_window, make_window
    window = make_window(tmp_path, monkeypatch)
    window.new_tab()
    for typed in ("photo", "foto"):
        found = window.commands.rank(typed, nostr=False)
        assert found[0].insert_title == "Photo Group…", typed
    close_window(window)
