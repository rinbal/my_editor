# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins the editor's rich-text model (rich_text.py).

What must hold:

  Every structure the editor makes is one the Markdown writer writes,
  and Markdown read back gives the same document again: the round trip
  is the test of every structure.

  What Qt's Markdown reader leaves behind is tidied: no checklist mark
  outside a list, so a paragraph after a checklist never turns into a
  checked item.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import (  # noqa: E402
    QTextBlockFormat, QTextCursor, QTextDocument, QTextFormat, QTextListFormat,
)
from PySide6.QtWidgets import QApplication  # noqa: E402

import rich_text  # noqa: E402
from markdown_writer import READ_FEATURES, document_to_markdown  # noqa: E402
from tests.rich_text_helpers import (  # noqa: E402
    assert_round_trip, block_named, from_markdown, press, select, type_text,
)


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    return QApplication.instance() or QApplication([])


# -- reading Markdown ----------------------------------------------------------------

def test_no_checklist_mark_is_left_after_a_checklist():
    doc = QTextDocument()
    doc.setMarkdown("- [x] done\n- [ ] open\n\nAfter\n\n> quote\n", READ_FEATURES)
    rich_text.normalize_after_markdown_load(doc)
    marker = QTextFormat.Property.BlockMarker
    assert not block_named(doc, "After").blockFormat().hasProperty(marker)
    assert not block_named(doc, "quote").blockFormat().hasProperty(marker)
    assert block_named(doc, "done").blockFormat().marker() == QTextBlockFormat.MarkerType.Checked
    assert block_named(doc, "open").blockFormat().marker() == QTextBlockFormat.MarkerType.Unchecked


def test_a_list_started_after_a_checklist_is_a_plain_list():
    doc = from_markdown("- [x] done\n\nAfter\n")
    QTextCursor(block_named(doc, "After")).createList(QTextListFormat.Style.ListDisc)
    # (A list of its own, by its other marker.)
    assert document_to_markdown(doc) == "- [x] done\n* After\n"


def test_a_checklist_comes_back_the_same():
    assert_round_trip(from_markdown("- [x] done\n- [ ] open\n\nAfter\n"))




# -- inline styles --------------------------------------------------------------------

def test_a_partly_struck_selection_becomes_all_struck():
    doc = from_markdown("keep ~~these~~ words\n")
    cursor = select(doc, "keep these")
    assert rich_text.toggle_style(cursor, rich_text.STRIKE) is True
    assert assert_round_trip(doc) == "~~keep these~~ words\n"


def test_a_wholly_struck_selection_is_unstruck():
    doc = from_markdown("~~gone~~ text\n")
    assert rich_text.toggle_style(select(doc, "gone"), rich_text.STRIKE) is False
    assert document_to_markdown(doc) == "gone text\n"


def test_inline_code_is_a_code_span_and_comes_back_off():
    doc = from_markdown("call print now\n")
    rich_text.toggle_style(select(doc, "print"), rich_text.CODE)
    assert assert_round_trip(doc) == "call `print` now\n"
    rich_text.toggle_style(select(doc, "print"), rich_text.CODE)
    assert document_to_markdown(doc) == "call print now\n"
    fmt = block_named(doc, "call print now").begin().fragment().charFormat()
    assert not fmt.hasProperty(QTextFormat.Property.FontFamilies)


def test_code_with_a_backtick_gets_a_longer_fence():
    doc = from_markdown("use a`b here\n")
    rich_text.toggle_style(select(doc, "a`b"), rich_text.CODE)
    assert assert_round_trip(doc) == "use ``a`b`` here\n"


def test_a_style_change_is_one_undo_step():
    doc = from_markdown("one two three\n")
    rich_text.toggle_style(select(doc, "one two three"), rich_text.STRIKE)
    doc.undo()
    assert document_to_markdown(doc) == "one two three\n"


def test_clear_formatting_keeps_links_and_headings():
    doc = from_markdown("# Title\n\n**bold** ~~s~~ `c` [site](https://x.example)\n")
    cursor = QTextCursor(doc)
    cursor.select(QTextCursor.SelectionType.Document)
    rich_text.clear_formatting(cursor)
    assert assert_round_trip(doc) == "# Title\n\nbold s c [site](https://x.example)\n"
    link = select(doc, "site")
    link.setPosition(link.selectionStart() + 1)
    assert link.charFormat().isAnchor()
    assert link.charFormat().hasProperty(QTextFormat.Property.ForegroundBrush)   # still looks like one


def test_the_editor_toggles_for_what_is_typed_next():
    from editor import HtmlEditor
    ed = HtmlEditor()
    assert ed.toggle_strike() is True
    ed.insertPlainText("gone")
    assert ed.toggle_strike() is False
    ed.insertPlainText(" kept")
    assert document_to_markdown(ed.document()) == "~~gone~~ kept\n"
    ed.insertPlainText(" ")
    ed.toggle_code()
    ed.insertPlainText("x")
    ed.toggle_code()
    ed.insertPlainText(" y")
    assert document_to_markdown(ed.document()) == "~~gone~~ kept `x` y\n"



def test_inline_code_shows_as_a_chip_that_is_not_in_the_document():
    from highlighter import RichTextLook
    from markdown_writer import has_local_only_formatting
    doc = from_markdown("call `print` now\n")
    look = RichTextLook(doc, is_dark=False)
    look.rehighlight()
    chips = [r for r in doc.begin().layout().formats() if r.format.hasProperty(
        QTextFormat.Property.BackgroundBrush)]
    assert [(r.start, r.length) for r in chips] == [(5, 5)]
    # Only the screen shows it: nothing is saved or counted as formatting.
    assert not has_local_only_formatting(doc)
    assert document_to_markdown(doc) == "call `print` now\n"



# -- paragraph styles -----------------------------------------------------------------

@pytest.mark.parametrize("level, marks", [(1, "#"), (2, "##"), (3, "###")])
def test_a_paragraph_becomes_a_heading_and_back(level, marks):
    doc = from_markdown("Intro\n\nA title\n")
    rich_text.set_heading(QTextCursor(block_named(doc, "A title")), level)
    assert assert_round_trip(doc) == f"Intro\n\n{marks} A title\n"
    block = block_named(doc, "A title")
    fmt = block.begin().fragment().charFormat()
    assert fmt.property(QTextFormat.Property.FontSizeAdjustment) == rich_text.HEADING_SIZE[level]
    # The same style again is Body again.
    rich_text.set_heading(QTextCursor(block), level)
    assert document_to_markdown(doc) == "Intro\n\nA title\n"
    assert not block.begin().fragment().charFormat().hasProperty(
        QTextFormat.Property.FontSizeAdjustment)


def test_bold_words_stay_bold_through_a_heading_and_back():
    # Review F8 (early range): Body, Heading, Body wiped the bold.
    doc = from_markdown("This is **important** text\n")
    cursor = QTextCursor(doc.begin())
    rich_text.set_heading(cursor, 1)
    assert document_to_markdown(doc) == "# This is important text\n"
    rich_text.set_heading(cursor, 2)                      # another heading on the way
    rich_text.set_heading(cursor, 0)
    assert assert_round_trip(doc) == "This is **important** text\n"


def test_return_after_a_heading_that_was_bold_types_plain_words():
    ed = _editor_with("This is **important**\n")
    ed.set_heading(1)
    ed.moveCursor(QTextCursor.MoveOperation.End)
    type_text(ed, "\nnext")
    assert document_to_markdown(ed.document()) == "# This is important\n\nnext\n"


@pytest.mark.parametrize("first", ["heading", "quote"])
def test_a_heading_is_never_quoted(first):
    # Review F5 (early range): "> # Title" is a quote readers drop, and the
    # file opened as source.
    from markdown_writer import holds_faithfully
    doc = from_markdown("Title\n\nBody\n")
    cursor = QTextCursor(doc.begin())
    if first == "heading":
        rich_text.set_heading(cursor, 1)
        rich_text.toggle_quote(cursor)
        expected = "> Title\n\nBody\n"
    else:
        rich_text.toggle_quote(cursor)
        rich_text.set_heading(cursor, 1)
        expected = "# Title\n\nBody\n"
    markdown = assert_round_trip(doc)
    assert markdown == expected and holds_faithfully(markdown)


def test_a_list_item_made_a_heading_leaves_the_list():
    # Review F7 (early range): it showed as a heading and was saved bold.
    doc = from_markdown("- Item one\n- Item two\n")
    rich_text.set_heading(QTextCursor(block_named(doc, "Item one")), 2)
    assert assert_round_trip(doc) == "## Item one\n\n- Item two\n"


def test_a_typed_heading_looks_like_one_read_from_markdown():
    typed = from_markdown("Title\n")
    rich_text.set_heading(QTextCursor(typed), 2)
    read = from_markdown("## Title\n")
    one = typed.begin().begin().fragment().charFormat()
    other = read.begin().begin().fragment().charFormat()
    for prop in (QTextFormat.Property.FontSizeAdjustment, QTextFormat.Property.FontWeight):
        assert one.property(prop) == other.property(prop)


def test_mixed_paragraphs_have_no_single_style():
    doc = from_markdown("# One\n\nTwo\n")
    cursor = QTextCursor(doc)
    cursor.select(QTextCursor.SelectionType.Document)
    assert rich_text.heading_level(cursor) == -1
    rich_text.set_heading(cursor, 2)
    assert document_to_markdown(doc) == "## One\n\n## Two\n"


def _editor_with(markdown: str):
    from editor import HtmlEditor
    ed = HtmlEditor()
    ed.document().setMarkdown(markdown, READ_FEATURES)
    return ed


def test_return_after_a_heading_types_body_text():
    ed = _editor_with("## Title\n")
    ed.moveCursor(QTextCursor.MoveOperation.End)
    type_text(ed, "\nPlain words")
    assert document_to_markdown(ed.document()) == "## Title\n\nPlain words\n"
    fmt = block_named(ed.document(), "Plain words").begin().fragment().charFormat()
    assert fmt.fontWeight() == 400
    assert not fmt.hasProperty(QTextFormat.Property.FontSizeAdjustment)


def test_return_inside_a_heading_splits_it_into_two():
    ed = _editor_with("## Title words\n")
    cursor = ed.textCursor()
    cursor.setPosition(6)
    ed.setTextCursor(cursor)
    type_text(ed, "\n")
    assert document_to_markdown(ed.document()) == "## Title\n\n## words\n"


def test_backspace_at_the_start_of_a_heading_makes_it_body():
    from PySide6.QtCore import Qt
    ed = _editor_with("Intro\n\n## Title\n")
    ed.setTextCursor(QTextCursor(block_named(ed.document(), "Title")))
    press(ed, Qt.Key.Key_Backspace)
    assert document_to_markdown(ed.document()) == "Intro\n\nTitle\n"
    press(ed, Qt.Key.Key_Backspace)                  # now an ordinary Backspace
    assert document_to_markdown(ed.document()) == "IntroTitle\n"


def test_setting_a_heading_with_the_caret_styles_what_is_typed():
    from editor import HtmlEditor
    ed = HtmlEditor()
    ed.set_heading(1)
    type_text(ed, "Big\nsmall")
    assert document_to_markdown(ed.document()) == "# Big\n\nsmall\n"



def test_a_null_cursor_has_no_style():
    # An editor's cursor is null while setHtml replaces its document, and
    # the window asks for the style at the caret right then.
    assert rich_text.heading_level(QTextCursor()) == -1


# -- what the menus show about a selection, in bounded time ---------------------------

def _bold_pieces(count: int) -> QTextDocument:
    """A document of ``count`` bold pieces that differ (bold, bold italic),
    so no answer about bold is known before the end."""
    doc = QTextDocument()
    cursor = QTextCursor(doc)
    for index in range(count):
        fmt = rich_text.style_format(rich_text.BOLD, True)
        fmt.setFontItalic(index % 2 == 1)
        cursor.insertText("word ", fmt)
    return doc


def test_the_state_of_a_selection_is_read_in_one_pass():
    doc = from_markdown("**all bold** and plain\n")
    state = rich_text.selection_state(select(doc, "all bold"))
    assert state[rich_text.BOLD] is True and state[rich_text.ITALIC] is False
    assert rich_text.selection_state(select(doc, "bold and"))[rich_text.BOLD] is False


def test_reading_the_state_stops_at_the_budget(monkeypatch):
    calls = []
    real = rich_text.has_style
    monkeypatch.setattr(rich_text, "has_style",
                        lambda fmt, style: calls.append(style) or real(fmt, style))
    doc = _bold_pieces(3 * 40)
    cursor = QTextCursor(doc)
    cursor.select(QTextCursor.SelectionType.Document)
    state = rich_text.selection_state(cursor, budget=40)
    # Past the budget, bold could not be checked throughout: shown off.
    assert state[rich_text.BOLD] is False
    assert len(calls) <= 40 * len(rich_text.INLINE)
    calls.clear()
    assert rich_text.selection_state(cursor, budget=1000)[rich_text.BOLD] is True


def test_a_style_button_that_shows_off_turns_its_style_on():
    # Review L1: past the budget Bold showed off, and the click took bold
    # away everywhere. The command decides from the same reading.
    doc = _bold_pieces(3 * 40)
    cursor = QTextCursor(doc)
    cursor.select(QTextCursor.SelectionType.Document)
    assert rich_text.selection_state(cursor, budget=40)[rich_text.BOLD] is False   # shown off
    assert rich_text.toggle_style(cursor, rich_text.BOLD, budget=40) is True       # so: on
    assert rich_text.selection_has(cursor, rich_text.BOLD) is True


def test_the_window_reads_selections_within_the_budget():
    assert rich_text.STATE_BUDGET <= 2000


def test_the_state_of_paragraphs_stops_at_the_budget():
    doc = from_markdown("\n\n".join(f"> quoted {i}" for i in range(30)) + "\n")
    cursor = QTextCursor(doc)
    cursor.select(QTextCursor.SelectionType.Document)
    assert rich_text.paragraph_state(cursor).quoted is True
    assert rich_text.paragraph_state(cursor, budget=10).quoted is False
    mixed = from_markdown("# Title\n\n- item\n\nplain\n")
    cursor = QTextCursor(mixed)
    cursor.select(QTextCursor.SelectionType.Document)
    state = rich_text.paragraph_state(cursor)
    assert (state.heading, state.list_kind, state.quoted) == (-1, "mixed", False)



def test_nested_bullets_read_from_markdown_have_the_shape_of_their_depth():
    # Review L5 (early range): a nested item made with Tab is a circle; read
    # back from Markdown it was a disc.
    from PySide6.QtGui import QTextListFormat
    doc = from_markdown("- one\n    - two\n        - three\n")
    shapes = [block_named(doc, text).textList().format().style()
              for text in ("one", "two", "three")]
    assert shapes == [QTextListFormat.Style.ListDisc, QTextListFormat.Style.ListCircle,
                      QTextListFormat.Style.ListSquare]
    assert assert_round_trip(doc) == "- one\n    - two\n        - three\n"
