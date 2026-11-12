# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins Find and Replace (find_replace.py and the find bar).

What must hold:

  Find looks for the text with or without matching case, and as whole
  words only when asked.

  A replacement takes the style of the text it replaces: a bold word
  stays bold, a link stays a link to the same address.

  Replace All replaces every match as one step on the undo stack, and
  says how many it replaced.

  In the window, Find and Replace shows the Replace row, Replace replaces
  the selected match and selects the next one, and the matches are found
  again after the document changes.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QTextDocument  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from find_replace import FindOptions, find_all, replace_all, replace_match  # noqa: E402
from markdown_writer import READ_FEATURES, document_to_markdown  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    return QApplication.instance() or QApplication([])


def doc_of(markdown: str) -> QTextDocument:
    doc = QTextDocument()
    doc.setMarkdown(markdown, READ_FEATURES)
    return doc


def test_find_ignores_case_unless_asked():
    doc = doc_of("Cat cat CAT category\n")
    assert len(find_all(doc, "cat")) == 4
    assert find_all(doc, "cat", FindOptions(match_case=True)) == [(4, 7), (12, 15)]


def test_whole_words_skip_parts_of_words():
    doc = doc_of("cat category cat\n")
    assert find_all(doc, "cat", FindOptions(whole_words=True)) == [(0, 3), (13, 16)]


def qt_find_all(doc, needle, options):
    """What QTextDocument.find finds, one call per match: the reference."""
    from PySide6.QtGui import QTextCursor
    flags = QTextDocument.FindFlag(0)
    if options.match_case:
        flags |= QTextDocument.FindFlag.FindCaseSensitively
    if options.whole_words:
        flags |= QTextDocument.FindFlag.FindWholeWords
    found, cursor = [], QTextCursor(doc)
    while True:
        cursor = doc.find(needle, cursor, flags)
        if cursor.isNull():
            return found
        found.append((cursor.selectionStart(), cursor.selectionEnd()))


STRUCTURED = ("# The Cat\n\nA cat, a CAT and a category.\n\n- cat one\n    - cat two\n\n"
              "| cat | dog |\n| --- | --- |\n| a cat | cats |\n\n> cat\n\n```\ncat = 1\n```\n\n"
              "Last cat\u00a0here, my_cat and cat.\n")


@pytest.mark.parametrize("needle", ["cat", "Cat", "a c", "t", "cat one"])
@pytest.mark.parametrize("options", [FindOptions(), FindOptions(match_case=True),
                                     FindOptions(whole_words=True),
                                     FindOptions(match_case=True, whole_words=True)])
def test_find_agrees_with_qt_in_every_structure(needle, options):
    doc = doc_of(STRUCTURED)
    assert find_all(doc, needle, options) == qt_find_all(doc, needle, options)


def test_accented_letters_are_found_whichever_way_they_are_stored():
    # Review L2: "e" with a combining accent, as macOS file names store it.
    doc = QTextDocument()
    doc.setPlainText("Cafe\u0301 au lait, Cafe noir, Mu\u0308nchen, Ko\u0308ln")
    assert find_all(doc, "Cafe", FindOptions(whole_words=True)) == [(15, 19)]
    assert find_all(doc, "Cafe") == [(15, 19)]                 # the accent is the last letter's
    assert find_all(doc, "München") == [(26, 34)]               # typed as one character each
    assert find_all(doc, "Ko\u0308ln") == [(36, 41)]
    assert replace_all(doc, "Cafe", "Bar", FindOptions(whole_words=True)) == 1
    assert doc.toPlainText().startswith("Cafe\u0301 au lait, Bar noir")
    precomposed = QTextDocument()
    precomposed.setPlainText("Köln")
    assert find_all(precomposed, "Ko\u0308ln") == [(0, 4)]      # typed with a combining accent


def test_find_is_quick_in_a_long_chapter():
    import time
    doc = QTextDocument()
    doc.setPlainText("Der schnelle braune Fuchs springt über den faulen Hund. " * 9000)
    started = time.perf_counter()
    found = find_all(doc, "e")
    assert len(found) == 63000
    assert time.perf_counter() - started < 0.5


def test_nothing_to_find_finds_nothing():
    assert find_all(doc_of("text\n"), "") == []


def test_a_replacement_keeps_the_style_it_replaces():
    doc = doc_of("a **bold** word and [a link](https://x.example)\n")
    start = doc.toPlainText().index("bold")
    replace_match(doc, (start, start + 4), "strong")
    start = doc.toPlainText().index("a link")
    replace_match(doc, (start, start + 6), "the site")
    assert document_to_markdown(doc) == (
        "a **strong** word and [the site](https://x.example)\n")


def test_replace_all_is_one_undo_step_and_counts():
    doc = doc_of("one fish, two fish, red fish\n")
    assert replace_all(doc, "fish", "cat") == 3
    assert document_to_markdown(doc) == "one cat, two cat, red cat\n"
    doc.undo()
    assert document_to_markdown(doc) == "one fish, two fish, red fish\n"


def test_replace_all_with_text_that_holds_the_needle():
    doc = doc_of("a a a\n")
    assert replace_all(doc, "a", "aa") == 3
    assert doc.toPlainText() == "aa aa aa"


def test_replace_all_respects_the_options():
    doc = doc_of("Cat cat category\n")
    assert replace_all(doc, "cat", "dog", FindOptions(match_case=True, whole_words=True)) == 1
    assert doc.toPlainText() == "Cat dog category"


# -- in the window ---------------------------------------------------------------------

def test_the_find_bar_offers_replace_and_options():
    from widgets import FindBar
    bar = FindBar(lambda: None, lambda: None, lambda: None, replace=True, options=True)
    assert bar.replace_edit.isHidden()
    bar.show_replace(True)
    assert not bar.replace_edit.isHidden() and bar.replace_shown()
    bar.match_case.setChecked(True)
    assert bar.options() == FindOptions(match_case=True)
    assert bar.options_button.text() == "Options (1)"      # short: the bar keeps its width
    assert bar.options_button.toolTip() == "Match Case"
    assert bar.options_button.accessibleDescription() == "Match Case"
    asked = []
    bar.replace_requested.connect(lambda: asked.append("one"))
    bar.replace_all_requested.connect(lambda: asked.append("all"))
    bar.replace_btn.click()
    bar.replace_all_btn.click()
    assert asked == ["one", "all"]
    assert bar.edit.nextInFocusChain() is bar.replace_edit     # Tab: find, then replace
    from tests.accessibility import unnamed_controls
    assert unnamed_controls(bar) == []                          # a placeholder is not a name


WINDOW_SCRIPT = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv[:1])
import main_window
from markdown_writer import document_to_markdown

w = main_window.MainWindow()
w.show()
ed = w.new_tab()
ed.insertPlainText("one fish, two fish, red fish")
r = {}
w._show_find(replace=True)
r["replace_row"] = w.findbar.replace_shown()
w.findbar.edit.setText("fish")
r["count"] = w.findbar.match_info.text()
w._find_next()
r["first"] = [ed.textCursor().selectionStart(), ed.textCursor().selectionEnd()]
w.findbar.replace_edit.setText("cat")
w._replace_current()
r["after_one"] = ed.toPlainText()
r["selected"] = ed.textCursor().selectedText()
r["position"] = w.findbar.match_info.text()
w._replace_all()
r["after_all"] = ed.toPlainText()
r["replaced"] = w.findbar.match_info.text()
ed.document().undo()
r["undone"] = ed.toPlainText()
# Typing changes the document; the count follows.
ed.moveCursor(ed.textCursor().MoveOperation.End)
ed.insertPlainText(" fish")
QTest.qWait(400)
r["refreshed"] = w.findbar.match_info.text()
w._show_find(replace=False)
r["replace_row_kept"] = w.findbar.replace_shown()
QTest.keyClick(w.findbar.edit, Qt.Key.Key_Escape)
r["closed"] = w.findbar.isHidden()
ed.document().setModified(False)
w.close()
print("RESULT " + json.dumps(r))
"""


def test_find_and_replace_in_the_window(tmp_path):
    from tests.app_process import run_window_script
    r = run_window_script(WINDOW_SCRIPT, tmp_path)
    assert r["replace_row"]
    assert r["count"] == "3 matches"
    assert r["first"] == [4, 8]
    assert r["after_one"] == "one cat, two fish, red fish"
    assert r["selected"] == "fish" and r["position"] == "1 of 2"
    assert r["after_all"] == "one cat, two cat, red cat"
    assert r["replaced"] == "2 replaced"
    assert r["undone"] == "one cat, two fish, red fish"
    assert r["refreshed"] == "3 matches"           # found again; no match selected
    assert r["replace_row_kept"]          # Find while the row is open keeps it
    assert r["closed"]


MANY_SCRIPT = r"""
import json, os, sys, time
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv[:1])
import main_window

w = main_window.MainWindow()
w.resize(1000, 700)
w.show()
ed = w.new_tab()
ed.setPlainText("One fish, two fish, and some words around them.\n" * 10000)
app.processEvents()
w._show_find(replace=True)
w.findbar.edit.setText("fish")
app.processEvents()
r = {"matches": len(w._search_matches), "painted": len(ed.highlights("find"))}
w.findbar.replace_edit.setText("cat")
started = time.perf_counter()
w._replace_all()
r["replace_all"] = time.perf_counter() - started
r["left"] = ed.toPlainText().count("fish")
started = time.perf_counter()
ed.document().undo()
r["undo"] = time.perf_counter() - started
r["back"] = ed.toPlainText().count("fish")
from PySide6.QtTest import QTest
QTest.qWait(400)                      # the matches are found again after an edit
ed.verticalScrollBar().setValue(ed.verticalScrollBar().maximum())
app.processEvents()
first, last = ed.visible_range()
r["painted_after_scroll"] = [c.cursor.selectionStart() >= first - 60
                             for c in ed.highlights("find")]
ed.document().setModified(False)
w.close()
print("RESULT " + json.dumps(r))
"""


def test_replace_all_stays_quick_with_thousands_of_matches(tmp_path):
    # Review H2: one highlight cursor per match made Replace All of
    # 54,000 matches take 84 seconds. Only what is on screen is painted.
    from tests.app_process import run_window_script
    r = run_window_script(MANY_SCRIPT, tmp_path)
    assert r["matches"] == 20000
    assert 0 < r["painted"] < 400                      # what is on screen, not 20,000
    assert r["left"] == 0 and r["back"] == 20000
    assert r["replace_all"] < 1.0, r
    assert r["undo"] < 1.0, r
    assert 0 < len(r["painted_after_scroll"]) < 400       # scrolled: what is on screen now
    assert all(r["painted_after_scroll"])



CARET_SCRIPT = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtGui import QTextCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv[:1])
import main_window

w = main_window.MainWindow()
w.show()
ed = w.new_tab()
ed.insertPlainText("fish A, fish B, fish C, fish D")
w._show_find(replace=True)
w.findbar.edit.setText("fish")


def caret_after(text):
    cursor = ed.textCursor()
    cursor.setPosition(ed.toPlainText().index(text) + len(text))
    ed.setTextCursor(cursor)


def selected():
    return ed.textCursor().selectedText() + "|" + ed.toPlainText()[
        ed.textCursor().selectionEnd():ed.textCursor().selectionEnd() + 2]


r = {}
caret_after("fish B")
w._find_next()
r["next"] = selected()
caret_after("fish B")
ed.insertPlainText("!")
QTest.qWait(400)                         # the matches are found again
w._find_next()
r["after_edit"] = selected()
caret_after("fish C")
w._find_prev()
r["previous"] = selected()
caret_after("fish C")
w.findbar.replace_edit.setText("cat")
w._replace_current()
r["replace_selects"] = selected()
r["unchanged"] = ed.toPlainText()
w._replace_current()
r["replaced"] = ed.toPlainText()
ed.document().setModified(False)
w.close()
print("RESULT " + json.dumps(r))
"""


def test_find_next_and_replace_go_on_from_the_caret(tmp_path):
    # Review M1: they went back to the first match after any edit.
    from tests.app_process import run_window_script
    r = run_window_script(CARET_SCRIPT, tmp_path)
    assert r["next"] == "fish| C"                       # the one after "fish B": fish C
    assert r["after_edit"] == "fish| C"
    assert r["previous"] == "fish| C"                   # back from after "fish C": fish C
    assert r["replace_selects"] == "fish| D"            # Replace first finds the next one
    assert r["unchanged"] == "fish A, fish B!, fish C, fish D"
    assert r["replaced"] == "fish A, fish B!, fish C, cat D"


FIND_SCRIPT = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtCore import qInstallMessageHandler
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv[:1])
import main_window
warnings = []
qInstallMessageHandler(lambda kind, context, message: warnings.append(message))
w = main_window.MainWindow()
w.resize(1000, 700)
w.show()
short = w.new_tab()
short.setPlainText("one word")
long = w.new_tab()
long.setPlainText("word " * 400)
w._show_find(replace=False)
w.findbar.edit.setText("word")
app.processEvents()
w.tabs.setCurrentIndex(w._editor_widget_index(short))   # its view is laid out anew
app.processEvents()
r = {"warnings": [m for m in warnings if "out of range" in m],
     "short_highlights": len(short.highlights("find"))}
w.tabs.setCurrentIndex(w._editor_widget_index(long))
app.processEvents()
long.document().setModified(False)
w.close_tab(w.tabs.currentIndex())                     # the tab with the matches
app.processEvents()
r["after_close"] = [m for m in warnings if "out of range" in m]
for i in range(w.tabs.count()):
    e = w._editor_from_widget(w.tabs.widget(i))
    if e is not None:
        e.document().setModified(False)
w.close()
print("RESULT " + json.dumps(r))
"""


def test_find_matches_of_one_tab_never_land_on_another(tmp_path):
    """Switching tabs laid the new tab's view out before the old tab's
    matches were forgotten, and they were painted on it: "Position out of
    range" for a shorter text, wrong words for a longer one."""
    from tests.app_process import run_window_script
    r = run_window_script(FIND_SCRIPT, tmp_path)
    assert r["warnings"] == [] and r["after_close"] == []
    assert r["short_highlights"] == 1


STALE_SCRIPT = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtCore import Qt, qInstallMessageHandler
from PySide6.QtGui import QTextCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv[:1])
import main_window
warnings = []
qInstallMessageHandler(lambda kind, context, message: warnings.append(message))
w = main_window.MainWindow()
w.resize(900, 500)
w.show()
ed = w.new_tab()
ed.setFocus()
ed.setPlainText("".join("line %d with the word find\n" % i for i in range(60)))
w._show_find(replace=False)
w.findbar.edit.setText("find")
app.processEvents()
found = len(w._search_matches)
cursor = ed.textCursor()
cursor.setPosition(24)                            # in the first match
cursor.movePosition(QTextCursor.MoveOperation.End, QTextCursor.MoveMode.KeepAnchor)
ed.setTextCursor(cursor)
QTest.keyClick(ed, Qt.Key.Key_Backspace)          # the text shrinks
ed.verticalScrollBar().setValue(0)
w._on_visible_area_changed(ed)                    # before Find looks again
QTest.qWait(600)                                  # then it does
r = {"found": found, "warnings": [m for m in warnings if "out of range" in m],
     "after": len(w._search_matches)}
ed.document().setModified(False)
w.close()
print("RESULT " + json.dumps(r))
"""


def test_matches_from_before_an_edit_are_not_painted(tmp_path):
    """Review 2: the matches found before an edit were painted at their
    old places when the view scrolled before Find looked again, past the
    end of a shorter text ("Position out of range")."""
    from tests.app_process import run_window_script
    r = run_window_script(STALE_SCRIPT, tmp_path)
    assert r["found"] == 60 and r["warnings"] == [] and r["after"] == 0


CAPTION_SCRIPT = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv[:1])
import main_window
from markdown_writer import document_to_markdown

path = os.path.join(sys.argv[2], "trip.md")
with open(path, "w", encoding="utf-8") as out:
    out.write("Before the jetty.\n\n![a](a.png)\n\n*The **old** jetty, the jetty*\n\n"
              "![b](b.png)\n")
w = main_window.MainWindow()
w.show()
ed = w.open_path(path)
w._show_find(replace=True)
w.findbar.edit.setText("jetty")
r = {"matches": w._search_matches}
w._find_next()
w._find_next()
cursor = ed.textCursor()
r["selected"] = [cursor.selectionStart(), cursor.selectionEnd()]
r["painted"] = [[m.start, m.end, m.format.foreground().color().name()]
                for m in ed.caption_highlights("find")]
w.findbar.replace_edit.setText("pier")
w._replace_current()
r["after_replace"] = document_to_markdown(ed.document())
r["next"] = w._search_matches[w._current_match_index]
w._replace_all()
r["after_all"] = document_to_markdown(ed.document())
ed.document().setModified(False)
w.close()
print("RESULT " + json.dumps(r))
"""


def test_find_and_replace_reach_a_photo_caption(tmp_path):
    from tests.app_process import run_window_script
    r = run_window_script(CAPTION_SCRIPT, tmp_path, str(tmp_path))
    # The body's match, then the caption's two, after their picture (18).
    assert r["matches"] == [[11, 16], [18, 19, 8, 13], [18, 19, 19, 24]]
    assert r["selected"] == [18, 19]                  # the first picture, its caption's match
    assert sorted(color for _s, _e, color in r["painted"]) == ["#000000", "#ffffff"]
    # (Written without the pictures: no image target.)
    assert r["after_replace"] == "Before the jetty.\n\n*The **old** pier, the jetty*\n"
    assert r["next"] == [18, 19, 18, 23]
    assert r["after_all"] == "Before the pier.\n\n*The **old** pier, the pier*\n"
