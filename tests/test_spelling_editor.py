# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins the editor's side of spell checking (HtmlEditor.set_spelling and
the window's Edit > Spelling, context menu and per-tab wiring).

What must hold:

  Misspelled words on screen are underlined the platform's way
  (SpellCheckUnderline), as extra selections, only for the blocks on
  screen; the word being typed is left alone until it is finished. The
  service always knows where the caret is and what is on screen.

  Edit > Spelling > Check Spelling While Typing is on at first and
  remembered; it is dimmed where the system has no checker. Prose is
  checked (documents with structure, Markdown and plain text), code is
  not. On Windows and Linux, Edit > Spelling > Language chooses the
  language (the system's at first), remembered; macOS tells languages
  apart by itself and has no such menu.

  Right-clicking a misspelled word offers what it might be (a choice
  replaces it as one undo step), Ignore Spelling and Learn Spelling
  (Ignore All and Add to Dictionary on Windows and Linux).
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QTextCharFormat, QTextCursor  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from editor import HtmlEditor  # noqa: E402
from markdown_writer import read_markdown  # noqa: E402
from spelling.service import DocumentSpelling, SpellChecker  # noqa: E402
from tests.app_process import run_window_script  # noqa: E402
from tests.rich_text_helpers import type_text  # noqa: E402
from tests.spelling_fakes import FakeBackend, drain  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    return QApplication.instance() or QApplication([])


def checked_editor(markdown: str = ""):
    ed = HtmlEditor()
    ed.resize(500, 300)
    ed.show()
    if markdown:
        read_markdown(ed.document(), markdown)
    checker = SpellChecker(FakeBackend())
    spelling = DocumentSpelling(checker, ed.document())
    ed.set_spelling(spelling)
    QApplication.processEvents()
    drain(spelling)
    return ed, spelling, checker


def underlined(ed) -> list:
    out = []
    for mark in ed.highlights("spelling"):
        assert mark.format.underlineStyle() == QTextCharFormat.UnderlineStyle.SpellCheckUnderline
        out.append(mark.cursor.selectedText())
    return out


def test_misspelled_words_on_screen_are_underlined():
    ed, spelling, _checker = checked_editor("This text has one mistaek and one wrod.\n")
    assert underlined(ed) == ["has", "mistaek", "wrod"]
    ed.hide()


def test_misspelled_words_in_a_photo_caption_are_underlined():
    ed, _spelling, _checker = checked_editor(
        "![a](a.png)\n\n*The mistaek in a **wrod***\n\n![b](b.png)\n")
    marks = ed.caption_highlights("spelling")
    caption = "The mistaek in a wrod"
    assert [caption[m.start:m.end] for m in marks] == ["mistaek", "wrod"]
    assert all(m.cursor.position() == 0 for m in marks)       # under the first picture
    ed.hide()


def test_the_word_being_typed_waits_until_it_is_finished(monkeypatch):
    # The pause that also finishes a word cannot run out while this test
    # looks: no real short timer races the keys typed here.
    monkeypatch.setattr(DocumentSpelling, "TYPING_PAUSE_SECONDS", 3600)
    ed, spelling, _checker = checked_editor()
    ed.setFocus()
    type_text(ed, "the wrod")
    drain(spelling)
    assert underlined(ed) == []                      # still being typed
    type_text(ed, " ")
    drain(spelling)
    assert underlined(ed) == ["wrod"]
    ed.hide()


def test_only_the_blocks_on_screen_get_underlines():
    many = "".join(f"line {n} with a mistaek\n\n" for n in range(200))
    ed, spelling, _checker = checked_editor(many)
    spelling.check_all()
    ed._draw_spelling()
    first, last = ed.visible_blocks()
    shown = {ed.document().findBlock(m.cursor.position()).blockNumber()
             for m in ed.highlights("spelling")}
    assert shown and all(first <= n <= last for n in shown)
    assert len(ed.highlights("spelling")) < 100
    ed.hide()


def test_stopping_takes_the_underlines_away():
    ed, spelling, _checker = checked_editor("one mistaek\n")
    assert underlined(ed) == ["mistaek"]
    spelling.close()
    ed.set_spelling(None)
    assert ed.highlights("spelling") == [] and ed.spelling() is None
    ed.hide()


def test_a_suggestion_replaces_the_word_as_one_step():
    ed, spelling, _checker = checked_editor("A **wrod** here.\n")
    position = ed.toPlainText().index("wrod") + 1
    word = spelling.misspelling_at(position)
    block = ed.document().findBlock(position)
    ed.replace_misspelling(word, block.position(), "word")
    assert ed.toPlainText() == "A word here."
    fmt = QTextCursor(ed.document())
    fmt.setPosition(3)
    assert fmt.charFormat().fontWeight() > 500                  # still bold
    ed.document().undo()
    assert ed.toPlainText() == "A wrod here."
    ed.hide()


def test_language_names_are_written_as_their_speakers_write_them():
    from main_window import spelling_language_names
    named = spelling_language_names(["en-US", "de-DE", "fr"])
    names = dict(named)
    assert names["de-DE"] == "Deutsch (Deutschland)" and names["fr"] == "Français"
    assert names["en-US"].endswith("(United States)")
    assert [name for _tag, name in named] == sorted(names.values(), key=str.casefold)


SCRIPT = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtWidgets import QApplication, QMenu
app = QApplication(sys.argv[:1])
import spelling.service as service
from tests.spelling_fakes import FakeBackend, drain
backend = FakeBackend()
service.create_backend = lambda: backend
import main_window, settings
from tests.rich_text_helpers import type_text

w = main_window.MainWindow()
w.show()
ed = w.new_tab()
ed.setFocus()
app.processEvents()
r = {"on": w.act_check_spelling.isChecked(), "checked": ed.spelling() is not None,
     "language_menu": hasattr(w, "m_spelling_language")}
type_text(ed, "one mistaek here ")
drain(ed.spelling())
r["underlined"] = [m.cursor.selectedText() for m in ed.highlights("spelling")]
spot = ed.cursorRect(ed.textCursor())
cursor = ed.textCursor()
cursor.setPosition(ed.toPlainText().index("mistaek") + 2)
menu = QMenu(ed)
w._fill_editor_context_menu(menu, ed, ed.cursorRect(cursor).center())
r["menu"] = [a.text() for a in menu.actions() if a.text()][:3]
learn = [a for a in menu.actions() if a.text() in ("Learn Spelling", "Add to Dictionary")]
learn[0].trigger()
drain(ed.spelling())
r["learned"] = list(backend.learned)
r["after_learn"] = [m.cursor.selectedText() for m in ed.highlights("spelling")]
menu.deleteLater()
app.processEvents()
menu = QMenu(ed)
w._fill_editor_context_menu(menu, ed, ed.cursorRect(cursor).center())
unlearn = [a for a in menu.actions()
           if a.text() in ("Unlearn Spelling", "Remove from Dictionary")]
r["unlearn_offered"] = [a.text() for a in unlearn]
unlearn[0].trigger()
drain(ed.spelling())
r["after_unlearn"] = [list(backend.learned),
                      [m.cursor.selectedText() for m in ed.highlights("spelling")]]
code = os.path.join(os.environ["HOME"], "tool.py")
with open(code, "w", encoding="utf-8") as f:
    f.write("def mistaek():\n    pass\n")
py = w.open_path(code)
r["code_checked"] = py.spelling() is not None
r["welcome_checked"] = w.show_welcome_tab().spelling() is not None
w.tabs.setCurrentIndex(w._editor_widget_index(ed))
w.act_check_spelling.trigger()
r["off"] = [ed.spelling() is None, ed.highlights("spelling") == [],
            settings.load_settings().get("check_spelling")]
missing = FakeBackend()
missing.absent = "no checker here"            # a system without a checker
service.create_backend = lambda: missing
w._checker = None
w._update_spelling_menu()
r["dimmed"] = not w.act_check_spelling.isEnabled()
if not hasattr(w, "m_spelling_language"):
    w.m_spelling_language = w.m_spelling.addMenu("Language")    # as on Windows and Linux
service.create_backend = lambda: backend
w._checker = None
w._update_spelling_menu()                    # the checker works again
w.act_check_spelling.trigger()
w._update_spelling_menu()
r["languages"] = [a.text() for a in w.m_spelling_language.actions() if a.text()]
german = [a for a in w.m_spelling_language.actions() if a.text().startswith("Deutsch")]
german[0].trigger()
r["language"] = [ed.spelling().language(), settings.load_settings().get("spelling_language")]
# Review L5 (CP3): a remembered language without a dictionary falls back.
w.spelling_language = "fr-FR"
w._update_spelling_menu()
r["missing_language"] = [a.text() for a in w.m_spelling_language.actions()
                         if a.isCheckable() and a.isChecked()]
r["missing_used"] = w._usable_spelling_language()
for e in (ed, py):
    e.document().setModified(False)
w.close()
print("RESULT " + json.dumps(r))
"""


def test_spelling_in_the_window(tmp_path):
    r = run_window_script(SCRIPT, tmp_path)
    assert r["on"] and r["checked"]
    assert r["language_menu"] == (sys.platform != "darwin")
    assert r["underlined"] == ["mistaek"]
    mac = sys.platform == "darwin"
    assert r["menu"] == (["mistake", "Ignore Spelling", "Learn Spelling"] if mac
                         else ["mistake", "Ignore All", "Add to Dictionary"])
    assert r["learned"] == ["mistaek"] and r["after_learn"] == []
    # Review M9 (CP3): a learned word can be taken back.
    assert r["unlearn_offered"] == (["Unlearn Spelling"] if mac
                                    else ["Remove from Dictionary"])
    assert r["after_unlearn"] == [[], ["mistaek"]]
    assert r["code_checked"] is False and r["welcome_checked"] is False
    assert r["off"] == [True, True, False]
    assert r["dimmed"]
    assert r["languages"][0] == "System Language" and "Deutsch (Deutschland)" in r["languages"]
    assert r["language"] == ["de-DE", "de-DE"]
    assert r["missing_language"] == ["System Language"] and r["missing_used"] is None
