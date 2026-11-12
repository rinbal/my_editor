# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins the Insert menu in every kind of document.

What must hold:

  Image…, Table…, Divider, Code Block, Footnote and Add Link are always in
  the Insert menu, never hidden. They act where a document holds
  Markdown structure (Markdown and HTML documents, new documents) and are
  dimmed in plain text, code files, R Markdown and Markdown opened as its
  text, and where only the preview shows. Each one, where it is offered,
  does what it says.

  The format toolbar's Insert button opens this same menu: the same items
  in the same order, the Nostr ones only while a Nostr account is in use.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests.app_process import run_window_script  # noqa: E402

COMMANDS = ("insert.image", "insert.table", "insert.divider", "insert.code_block",
            "insert.footnote", "insert.link")

SCRIPT = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtGui import QTextCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv[:1])
import main_window, rich_text
from link_popover import LinkPopover
COMMANDS = %r
w = main_window.MainWindow()
w.show()
home = os.environ["HOME"]
r = {"kinds": {}, "acts": {}}

def state():
    return {c: [w.commands.action(c).isEnabled(), w.commands.action(c).isVisible(),
                w.commands.action(c) in w.m_insert.actions()] for c in COMMANDS}

def open_file(name, body):
    path = os.path.join(home, name)
    open(path, "w").write(body)
    w.open_path(path)
    app.processEvents()
    return w.current_editor()

ed = w.new_tab()
app.processEvents()
r["kinds"]["new"] = state()
for name, body in (("doc.md", "# Doc\n\nWords\n"), ("doc.html", "<p>Words</p>"),
                   ("doc.txt", "Words\n"), ("doc.Rmd", "Words\n"), ("doc.py", "x = 1\n"),
                   ("raw.md", "<div>raw</div>\n\nWords\n")):
    open_file(name, body)
    r["kinds"][name] = state()
open_file("view.md", "# View\n\nWords\n")
w.act_view_preview.trigger()
app.processEvents()
r["kinds"]["preview"] = state()
w.act_view_write.trigger()

# Where they act, each does what it says.
for name, body in (("acts.md", "Words\n"), ("acts.html", "<p>Words</p>")):
    ed = open_file(name, body)
    ed.setFocus()
    done = {}
    w.act_divider.trigger()
    done["divider"] = any(rich_text.is_divider(ed.document().findBlockByNumber(i))
                          for i in range(ed.document().blockCount()))
    w.act_footnote.trigger()
    done["footnote"] = "[^1]" in ed.toPlainText()
    cursor = ed.textCursor()
    cursor.movePosition(QTextCursor.MoveOperation.End)
    ed.setTextCursor(cursor)
    w.act_table.trigger()
    app.processEvents()
    w._table_picker.picked.emit(2, 2)
    app.processEvents()
    done["table"] = ed.textCursor().currentTable() is not None
    cursor = ed.textCursor()
    cursor.movePosition(QTextCursor.MoveOperation.End)       # below the table
    ed.setTextCursor(cursor)
    QTest.keyClicks(ed, "code")
    w.act_code_block.trigger()
    app.processEvents()
    w.m_code_language.hide()
    done["code_block"] = rich_text.is_code_block(ed.textCursor().block())
    cursor = ed.textCursor()
    cursor.movePosition(QTextCursor.MoveOperation.Start)
    cursor.movePosition(QTextCursor.MoveOperation.EndOfWord, QTextCursor.MoveMode.KeepAnchor)
    ed.setTextCursor(cursor)
    w.act_link.trigger()
    app.processEvents()
    popovers = [p for p in ed.findChildren(LinkPopover) if p.isVisible()]
    done["link"] = len(popovers) == 1
    for p in popovers:
        p.close()
    app.processEvents()
    r["acts"][name] = done

# The toolbar's Insert button holds the menu itself.
def titles():
    w.m_insert.aboutToShow.emit()
    return [a.objectName() or a.text() for a in w.m_insert.actions()
            if a.isVisible() and not a.isSeparator()]
r["button_menu"] = w.format_toolbar.insert_button.menu() is w.m_insert
r["without"] = titles()
w.nostr_state._active = True
w._update_nostr_commands()
r["with"] = titles()
for i in range(w.tabs.count()):
    e = w._editor_from_widget(w.tabs.widget(i))
    if e is not None:
        e.document().setModified(False)
w.close()
print("RESULT " + json.dumps(r))
""" % (COMMANDS,)


@pytest.fixture(scope="module")
def r(tmp_path_factory):
    return run_window_script(SCRIPT, tmp_path_factory.mktemp("home"))


@pytest.mark.parametrize("kind", ["new", "doc.md", "doc.html"])
def test_insert_acts_where_a_document_holds_structure(r, kind):
    assert r["kinds"][kind] == {c: [True, True, True] for c in COMMANDS}


@pytest.mark.parametrize("kind", ["doc.txt", "doc.Rmd", "doc.py", "raw.md", "preview"])
def test_insert_is_dimmed_never_hidden_elsewhere(r, kind):
    assert r["kinds"][kind] == {c: [False, True, True] for c in COMMANDS}


@pytest.mark.parametrize("kind", ["acts.md", "acts.html"])
def test_each_insert_command_does_what_it_says(r, kind):
    assert r["acts"][kind] == {"divider": True, "footnote": True, "table": True,
                               "code_block": True, "link": True}


def test_the_toolbar_button_opens_the_same_menu(r):
    assert r["button_menu"]
    assert r["without"] == ["insert.link", *COMMANDS[:5]]
    assert r["with"] == ["insert.link", *COMMANDS[:5], "nostr.insert_mention",
                         "nostr.insert_reference", "nostr.insert_image",
                         "nostr.insert_audio_video"]


WORDING_SCRIPT = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv[:1])
import main_window
w = main_window.MainWindow()
w.show()
ed = w.new_tab()
w.nostr_state._active = True
w._update_nostr_commands()
app.processEvents()

def shown(menu):
    return [a.text() for a in menu.actions() if a.isVisible() and not a.isSeparator()]


from PySide6.QtWidgets import QMenu
nostr = next(o for o in w.act_insert_mention.associatedObjects()
             if isinstance(o, QMenu) and o is not w.m_insert)
r = {}
w.m_insert.aboutToShow.emit()
r["insert"] = shown(w.m_insert)
r["link_key"] = w.act_link.shortcut().toString(QKeySequence.SequenceFormat.PortableText)
r["same_actions"] = all(a in w.m_insert.actions() for a in (
    w.act_link, w.act_insert_mention, w.act_nostr_insert_image))
w.m_insert.aboutToHide.emit()
r["after"] = [w.act_link.text(), w.act_insert_mention.text(), w.act_nostr_insert_image.text()]
nostr.aboutToShow.emit()
r["nostr"] = [a.text() for a in nostr.actions() if a.objectName() in (
    "nostr.insert_image", "nostr.insert_mention", "nostr.insert_reference",
    "nostr.insert_audio_video")]
nostr.aboutToHide.emit()
r["slash"] = [item.title or item.action.text() for item in w._slash_items("")
              if item.section == "insert"]
context = QMenu(w)
w._fill_editor_context_menu(context, ed, ed.cursorRect().center())
r["context"] = [a.text() for a in context.actions() if not a.isSeparator()]
ed.document().setModified(False)
w.close()
print("RESULT " + json.dumps(r))
"""


def test_the_insert_menu_says_what_it_inserts_without_insert(tmp_path):
    """Pages' Insert menu says "Table" and "Choose…", never "Insert Table":
    the menu's name says the verb. The Nostr menu keeps the verb, and both
    menus hold the same commands, with one key and one state."""
    r = run_window_script(WORDING_SCRIPT, tmp_path)
    assert r["insert"] == ["Link…", "Image…", "Table…", "Divider", "Code Block", "Footnote",
                           "Mention…", "Note or Article…", "From Media Library…",
                           "Audio or Video…"]
    assert r["link_key"] == "Ctrl+K" and r["same_actions"]
    assert r["after"] == ["Add Link…", "Insert Mention…", "Insert from Media Library…"]
    assert r["nostr"] == ["Insert from Media Library…", "Insert Mention…",
                          "Insert Note or Article…", "Insert Audio or Video…"]
    assert r["slash"] == ["Link…", "Footnote", "Image…", "Photo Group…", "From Media Library…",
                          "Mention…", "Note or Article…", "Audio or Video…"]
    assert not any(title.startswith("Insert") for title in r["context"])
