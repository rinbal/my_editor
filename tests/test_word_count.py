# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins how words are counted and reading time estimated (word_count.py).

What must hold:

  A word holds a letter or a digit; a web address or a Nostr link is one
  word; Markdown marks standing alone and footnote marks are none.

  Reading time is 225 words a minute, rounded up, at least a minute for
  anything there is to read, nothing for nothing.

  The status bar, the preview and the publish sheet use this one rule.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from word_count import count_words, reading_minutes  # noqa: E402


@pytest.mark.parametrize("text, words", [
    ("", 0),
    ("   \n\t ", 0),
    ("one two  three\nfour", 4),
    ("Über die Brücke, schnell!", 4),
    ("see https://example.com/a?b=c and nostr:npub1abc", 4),
    ("- item\n- other\n> quote\n# Title", 4),
    ("A claim[^1].\n\n[^1]: The source.", 4),
    ("**bold** and *italic* -- 42", 4),
])
def test_what_counts_as_a_word(text, words):
    assert count_words(text) == words


@pytest.mark.parametrize("words, minutes", [(0, 0), (1, 1), (225, 1), (226, 2), (1000, 5)])
def test_reading_time(words, minutes):
    assert reading_minutes(words) == minutes


# -- the status bar --------------------------------------------------------------------

@pytest.fixture
def status(qt_app_for_status):
    from PySide6.QtWidgets import QLabel
    from main_window import MainWindow
    from tests.test_commands import StandIn
    win = StandIn()
    win._words_label = QLabel()
    win._document_words = 0
    win._show_word_count = lambda ed: MainWindow._show_word_count(win, ed)
    win.update = lambda: MainWindow._update_word_count(win)
    return win


@pytest.fixture(scope="module")
def qt_app_for_status():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _editor(text):
    from editor import HtmlEditor
    ed = HtmlEditor()
    ed.setPlainText(text)
    return ed


def test_the_status_bar_says_words_and_reading_time(status):
    ed = _editor("one two three four")
    status.current_editor = lambda: ed
    status.update()
    assert status._words_label.text() == "4 words · 1 min read"


def test_long_documents_get_grouped_numbers(status):
    ed = _editor("word " * 1234)
    status.current_editor = lambda: ed
    status.update()
    assert status._words_label.text() == "1,234 words · 6 min read"


def test_a_selection_says_how_many_of_the_words(status):
    from PySide6.QtGui import QTextCursor
    ed = _editor("one two three four")
    status.current_editor = lambda: ed
    status.update()
    cursor = ed.textCursor()
    cursor.setPosition(0)
    cursor.setPosition(7, QTextCursor.MoveMode.KeepAnchor)
    ed.setTextCursor(cursor)
    status._show_word_count(ed)
    assert status._words_label.text() == "2 of 4 words"


def test_a_selected_picture_leaves_the_documents_count(status):
    """A selection without a word in it (a picture is one character that
    is no word) said "0 of 4 words"."""
    from PySide6.QtGui import QTextCursor, QTextImageFormat
    ed = _editor("one two three four")
    cursor = ed.textCursor()
    cursor.movePosition(QTextCursor.MoveOperation.End)
    picture = QTextImageFormat()
    picture.setName("photo.png")
    cursor.insertImage(picture)
    status.current_editor = lambda: ed
    status.update()
    cursor.movePosition(QTextCursor.MoveOperation.Left, QTextCursor.MoveMode.KeepAnchor)
    ed.setTextCursor(cursor)
    status._show_word_count(ed)
    assert status._words_label.text() == "4 words · 1 min read"


def test_an_empty_document_or_a_pdf_says_nothing(status):
    ed = _editor("")
    status.current_editor = lambda: ed
    status.update()
    assert status._words_label.text() == ""
    status.current_editor = lambda: None
    status.update()
    assert status._words_label.text() == ""


def test_the_preview_and_the_publish_dialog_count_the_same_way():
    from nostr import preview
    text = "see https://example.com/a?b=c and [link](https://x.example)\n" * 120
    words = count_words(text)
    assert preview.reading_minutes(text) == reading_minutes(words)



MIXED = ("# Eine Liste\n\n1. Eins\n2. Zwei\n3. Drei\n\n- [x] Erledigt\n- [ ] Offen\n\n"
         "> Ein Zitat\n\n```python\nprint(\"hallo welt\")\n```\n\n"
         "![Ein Bild von einer Katze](https://example.com/cat.png)\n\n"
         "| A | B |\n| --- | --- |\n| eins | zwei |\n\nSiehe [die Seite](https://example.com).\n")


def test_the_status_bar_and_the_publish_dialog_agree():
    # Review M6: list numbers, checkboxes, the code fence and a picture's
    # description counted in the dialog and the preview, not on screen.
    from PySide6.QtGui import QTextDocument
    from PySide6.QtWidgets import QApplication
    from markdown_writer import read_markdown
    from word_count import count_markdown_words
    QApplication.instance() or QApplication([])
    doc = QTextDocument()
    read_markdown(doc, MIXED)
    assert count_markdown_words(MIXED) == count_words(doc.toPlainText())
