# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins the one Markdown writer behind .md files, articles and drafts.

What must hold:

  What the editor shows is what is written: bold, italic, strikethrough,
  inline code, links, headings, lists (Qt's and the editor's typed
  bullets), quotes, code blocks, rules, tables and task lists.

  A .md file read and written again comes back the same.

  Lines typed one under the other stay separate, as paragraphs: the one
  break every Nostr reader shows alike.

  Typed text is Markdown and is written as typed (footnote marks
  included); only a web address that holds a character a reader would
  take for emphasis is written as <address>, so it survives being read
  back.

  A short note is plain text: no markup, list markers and link addresses
  kept.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import (  # noqa: E402
    QFont, QTextCharFormat, QTextCursor, QTextDocument, QTextImageFormat,
)
from PySide6.QtWidgets import QApplication  # noqa: E402

from doc_walk import iter_block_runs  # noqa: E402
from markdown_writer import (  # noqa: E402
    READ_FEATURES, document_to, document_to_markdown, document_to_note_text, holds_faithfully,
)


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    return QApplication.instance() or QApplication([])


def from_markdown(text: str) -> QTextDocument:
    doc = QTextDocument()
    doc.setMarkdown(text, READ_FEATURES)
    return doc


def typed(*pieces) -> QTextDocument:
    """A document typed in the editor: ``(text, {format})`` pieces, with
    "\\n" starting a new block as Enter does."""
    doc = QTextDocument()
    cursor = QTextCursor(doc)
    for piece in pieces:
        text, fmt = (piece, {}) if isinstance(piece, str) else piece
        char = QTextCharFormat()
        if fmt.get("bold"):
            char.setFontWeight(QFont.Weight.Bold)
        if fmt.get("italic"):
            char.setFontItalic(True)
        if fmt.get("strike"):
            char.setFontStrikeOut(True)
        if fmt.get("underline"):
            char.setFontUnderline(True)
        if fmt.get("code"):
            char.setFontFixedPitch(True)
        if fmt.get("href"):
            char.setAnchor(True)
            char.setAnchorHref(fmt["href"])
        lines = text.split("\n")
        for index, line in enumerate(lines):
            if index:
                cursor.insertBlock()
            cursor.insertText(line, char)
    return doc


# -- a .md file comes back the same --------------------------------------------------

ROUND_TRIPS = [
    "# Title\n\nSome text.\n",
    "Some **bold** and *italic* and ~~strike~~ and `code`.\n",
    "A [link](https://example.com/a_b) in text.\n",
    "## Second\n\n### Third\n",
    "- one\n- two\n    - nested\n- three\n",
    "1. first\n2. second\n3. third\n",
    "- [ ] todo\n- [x] done\n",
    "> quoted text\n",
    "```python\nprint(1)\n\nprint(2)\n```\n",
    "Before\n\n---\n\nAfter\n",
    "| a | b |\n| --- | --- |\n| 1 | 2 |\n",
    "Para one\n\nPara two\n",
    "Some *italic* via underscores reads as italic.\n",
    "> - quoted item\n> - another\n",
    "See <https://github.com/psf/requests/blob/main/src/requests/__init__.py> now.\n",
    "A [link](<https://x.example/a (b).png>) with parentheses.\n",
]


@pytest.mark.parametrize("source", ROUND_TRIPS)
def test_a_markdown_file_comes_back_the_same(source):
    once = document_to_markdown(from_markdown(source))
    assert once == source
    # And it is stable: writing what was written changes nothing.
    assert document_to_markdown(from_markdown(once)) == once


def test_a_quote_with_two_paragraphs_stays_one_quote():
    out = document_to_markdown(from_markdown("> one\n>\n> two\n"))
    assert out == "> one\n>\n> two\n"


# -- what is typed in the editor ------------------------------------------------------

def test_typed_formatting_reaches_markdown():
    doc = typed("Plain ", ("bold", {"bold": True}), " and ", ("it", {"italic": True}),
                " and ", ("gone", {"strike": True}), " and ", ("x = 1", {"code": True}), ".")
    assert document_to_markdown(doc) == (
        "Plain **bold** and *it* and ~~gone~~ and `x = 1`.\n")


def test_spaces_stay_outside_the_markers():
    doc = typed("a", (" bold ", {"bold": True}), "b")
    assert document_to_markdown(doc) == "a **bold** b\n"


def test_underline_is_for_local_files_only():
    doc = typed("an ", ("underlined", {"underline": True}), " word")
    assert document_to_markdown(doc) == "an underlined word\n"


def test_a_link_keeps_its_words_and_address():
    doc = typed("see ", ("the site", {"href": "https://example.com"}), " now")
    assert document_to_markdown(doc) == "see [the site](https://example.com) now\n"


def test_lines_typed_one_under_the_other_stay_separate():
    doc = typed("Roses are red\nViolets are blue\n\nNew stanza")
    assert document_to_markdown(doc) == (
        "Roses are red\n\nViolets are blue\n\nNew stanza\n")


def test_several_empty_lines_are_one_paragraph_break():
    doc = typed("one\n\n\n\ntwo")
    assert document_to_markdown(doc) == "one\n\ntwo\n"


def test_typed_bullets_become_a_markdown_list():
    doc = typed("Shopping:\n    • milk\n    • bread\n        • rye\n\nDone")
    assert document_to_markdown(doc) == (
        "Shopping:\n\n- milk\n- bread\n    - rye\n\nDone\n")


def test_shift_enter_is_a_hard_line_break():
    doc = typed("first second")
    assert document_to_markdown(doc) == "first  \nsecond\n"


# -- typed Markdown -------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "## A heading typed by hand",
    "See [the docs](https://example.com) and **this**.",
    "> quoted by hand",
    "2 * 3 * 4 and snake_case",
    "#nostr #bitcoin",
    "- a list it looks like",
])
def test_typed_markdown_is_written_as_typed(text):
    assert document_to_markdown(typed(text)) == text + "\n"


@pytest.mark.parametrize("address", [
    "https://github.com/psf/requests/blob/main/src/requests/__init__.py",
    "https://example.com/_foo_/bar",
    "wss://relay.example/x*y",
])
def test_an_address_with_markup_characters_is_kept_whole(address):
    written = document_to_markdown(typed(f"see {address} now"))
    assert written == f"see <{address}> now\n"
    # Read back and written again, it is the same address.
    assert document_to_markdown(from_markdown(written)) == written


def test_footnote_marks_are_written_as_typed():
    # A reference and its definition, typed by hand, reach the reader as
    # Markdown footnotes: nothing in them is escaped.
    doc = typed("A claim[^1] and another[^note].\n\n[^1]: The source.\n[^note]: More.")
    assert document_to_markdown(doc) == (
        "A claim[^1] and another[^note].\n\n[^1]: The source.\n\n[^note]: More.\n")
    assert document_to_note_text(doc) == (
        "A claim[^1] and another[^note].\n\n[^1]: The source.\n[^note]: More.")


FOOTNOTES = ("A claim[^1] and another[^2].\n\n[^1]: https://example.com/source\n\n"
             "[^2]: Ibid.\n")


def test_footnotes_read_back_as_the_text_they_are():
    # Review H3: Qt's reader took "[^1]: Ibid." for a link definition,
    # dropped it and made the mark a link.
    from markdown_writer import read_markdown
    doc = QTextDocument()
    read_markdown(doc, FOOTNOTES)
    assert doc.toPlainText() == ("A claim[^1] and another[^2].\n"
                                 "[^1]: https://example.com/source\n[^2]: Ibid.")
    assert document_to_markdown(doc) == FOOTNOTES


@pytest.mark.parametrize("markdown, kept", [
    ("[^1]: Ibid.\n", "\\[^1]: Ibid.\n"),
    ("> [^q]: quoted\n", "> \\[^q]: quoted\n"),
    ("```\n[^1]: code\n```\n", "```\n[^1]: code\n```\n"),
    ("~~~~\n[^1]: code\n~~~\n[^2]: still code\n~~~~\n[^3]: text\n",
     "~~~~\n[^1]: code\n~~~\n[^2]: still code\n~~~~\n\\[^3]: text\n"),
    ("A claim[^1].\n", "A claim[^1].\n"),
])
def test_only_definitions_outside_code_are_marked_literal(markdown, kept):
    from markdown_writer import literal_footnotes
    assert literal_footnotes(markdown) == kept


def test_a_footnote_mark_in_bold_text_stays_one():
    doc = typed(("a claim[^1]", {"bold": True}))
    assert document_to_markdown(doc) == "**a claim[^1]**\n"


@pytest.mark.parametrize("address", ["https://example.com/plain", "nostr:npub1abcdef"])
def test_a_plain_address_stays_bare(address):
    assert document_to_markdown(typed(f"see {address}")) == f"see {address}\n"


def test_underscores_read_back_as_italic_not_underline():
    doc = from_markdown("an _emphasized_ word\n")
    assert document_to_markdown(doc) == "an *emphasized* word\n"


def test_indented_code_stays_code():
    doc = from_markdown("Intro\n\n    x = a*b*c\n    if x_y_z: pass\n")
    assert document_to_markdown(doc) == "Intro\n\n```\nx = a*b*c\nif x_y_z: pass\n```\n"


def test_a_link_that_shows_its_address_is_an_autolink():
    doc = typed(("https://x.example/a_b", {"href": "https://x.example/a_b"}))
    assert document_to_markdown(doc) == "<https://x.example/a_b>\n"


@pytest.mark.parametrize("words, href, written", [
    ("https://example.com/photo.jpg", "https://example.com/photo.jpg",
     "https://example.com/photo.jpg"),
    ("www.example.com", "http://www.example.com", "www.example.com"),
    ("ada@example.com", "mailto:ada@example.com", "ada@example.com"),
])
def test_a_link_that_shows_its_own_address_is_written_bare(words, href, written):
    doc = typed(("see ", {}), (words, {"href": href}), (". Next", {}))
    out = document_to_markdown(doc)
    assert out == f"see {written}. Next\n"
    # Read back, it is the same link, written the same way.
    assert document_to_markdown(from_markdown(out)) == out


# Addresses whose bare form Qt's reader does not take back whole, in every
# Qt version this app is built with: written <address>, whatever the version.
NEVER_BARE = [
    "https://de.wikipedia.org/wiki/M%C3%BCnchen",          # a percent sign
    "https://example.com:8080/x",                          # a port
    "https://mastodon.social/@user",                       # an @
    "https://example.com/a,b",                             # a comma
    "https://example.com/wow!",                            # an exclamation mark
    "https://example.com/page_(info)",                     # parentheses
    "https://example.com/cdn-cgi/image/width=80,quality=75/a.jpg",
]
# Addresses whose bare form some Qt versions take back whole and others do
# not (6.11.2 reads "c++" back whole, 6.11.1 cuts it): the writer asks the
# Qt it runs on.
DEPENDS_ON_QT = [
    "https://example.com/c++",                             # a plus
]


def _own_address_round_trip(address: str) -> str:
    """The Markdown of an own-address link, after checking what must hold
    whatever the Qt version: it reads back as the same whole link, writing
    it again gives the same Markdown, and a file holding it opens
    formatted."""
    from markdown_writer import holds_faithfully
    doc = typed(("see ", {}), (address, {"href": address}), (" now", {}))
    out = document_to_markdown(doc)
    back = from_markdown(out)
    links = [(text, fmt.anchorHref()) for block in [back.begin()]
             for text, fmt in __import__("doc_walk").iter_block_runs(block) if fmt.isAnchor()]
    assert links == [(address, address)]
    assert document_to_markdown(back) == out
    assert holds_faithfully(out)             # an older file with it opens formatted
    return out


@pytest.mark.parametrize("address", NEVER_BARE)
def test_an_own_address_qt_would_not_read_back_whole_is_kept_whole(address):
    # Review F2: written bare, these came back as no link, or cut short.
    assert _own_address_round_trip(address) == f"see <{address}> now\n"


@pytest.mark.parametrize("address", DEPENDS_ON_QT + ["https://example.com/plain"])
def test_an_own_address_is_bare_exactly_when_qt_reads_it_back_whole(address):
    from markdown_writer import _reads_back_whole
    bare = _reads_back_whole(address, address)
    expected = f"see {address} now\n" if bare else f"see <{address}> now\n"
    assert _own_address_round_trip(address) == expected


def test_a_media_address_alone_on_its_line_stays_bare():
    url = "https://cdn.example/clip.mp4"
    doc = typed("Watch this:\n", (url, {"href": url}), "\nThanks")
    assert document_to_markdown(doc) == f"Watch this:\n\n{url}\n\nThanks\n"


@pytest.mark.parametrize("after", ["word", "-x", "/more"])
def test_an_own_address_followed_by_text_is_kept_whole(after):
    url = "https://example.com"
    out = document_to_markdown(typed((url, {"href": url}), after))
    assert out == f"<{url}>{after}\n"


def test_an_own_address_after_a_word_is_kept_whole():
    url = "https://example.com"
    assert document_to_markdown(typed("see:", (url, {"href": url}))) == f"see:<{url}>\n"


NPUB = "npub1" + "q" * 58
NEVENT = "nevent1" + "q" * 60


def test_a_nostr_link_is_written_bare_whatever_its_words():
    # Review M4 (W2): "[Alice](nostr:npub1...)" is dropped by njump and
    # read differently elsewhere; the bare reference is shown the same
    # everywhere (as the person's name, or a card).
    doc = typed(("Thanks to ", {}), ("Alice", {"href": "nostr:" + NPUB}),
                (" for ", {}), ("this note", {"href": "nostr:" + NEVENT}), (".", {}))
    assert document_to_markdown(doc) == f"Thanks to nostr:{NPUB} for nostr:{NEVENT}.\n"
    assert document_to_note_text(doc) == f"Thanks to nostr:{NPUB} for nostr:{NEVENT}."


def test_a_bare_nostr_reference_keeps_a_space_from_the_words_around_it():
    doc = typed(("see", {}), ("Alice", {"href": "nostr:" + NPUB}), ("today", {}))
    assert document_to_markdown(doc) == f"see nostr:{NPUB} today\n"


def test_a_link_label_with_a_bracket_is_escaped():
    # Both brackets, and a "<" (read as HTML): an unescaped "[" did not
    # read back as the same link, so such a file opened as its text.
    doc = typed(("see [1] <live>", {"href": "https://x.example"}))
    written = document_to_markdown(doc)
    assert written == "[see \\[1\\] \\<live>](https://x.example)\n"
    assert holds_faithfully(written)


def test_backticks_inside_code_get_a_longer_fence():
    doc = typed(("a `b` c", {"code": True}))
    assert document_to_markdown(doc) == "``a `b` c``\n"


# -- images ---------------------------------------------------------------------------

def image_doc():
    doc = QTextDocument()
    cursor = QTextCursor(doc)
    cursor.insertText("before ")
    image = QTextImageFormat()
    image.setName("myeditor-asset://abc")
    cursor.insertImage(image)
    cursor.insertText(" after")
    return doc


def test_images_are_written_where_the_caller_says():
    out = document_to_markdown(image_doc(), lambda fmt: f"![pic](https://cdn/{fmt.name()[-3:]})")
    assert out == "before ![pic](https://cdn/abc) after\n"


def test_an_image_without_a_destination_is_left_out():
    assert document_to_markdown(image_doc(), lambda fmt: None) == "before  after\n"
    assert document_to_markdown(image_doc()) == "before  after\n"


# -- a short note ------------------------------------------------------------------------

def test_a_note_carries_no_markup():
    doc = typed(("Bold", {"bold": True}), " and ", ("code", {"code": True}), " and 2 * 3")
    assert document_to_note_text(doc) == "Bold and code and 2 * 3"


def test_a_note_keeps_list_markers_and_link_addresses():
    doc = from_markdown("# Heading\n\n- one\n- two\n\n1. first\n\nA [site](https://x.example).\n")
    assert document_to_note_text(doc) == (
        "Heading\n\n• one\n• two\n\n1. first\n\nA site (https://x.example).")


def test_a_note_keeps_typed_bullets_exactly():
    doc = typed("List:\n    • a\n        • b")
    assert document_to_note_text(doc) == "List:\n    • a\n        • b"


def test_a_link_that_is_its_own_address_is_written_once():
    doc = typed(("https://x.example", {"href": "https://x.example"}))
    assert document_to_note_text(doc) == "https://x.example"


def test_a_note_keeps_typed_lines_and_empty_lines_exactly():
    doc = typed("one\ntwo\n\n\nthree")
    assert document_to_note_text(doc) == "one\ntwo\n\n\nthree"


def test_flavors_by_name():
    doc = typed(("b", {"bold": True}))
    assert document_to(doc, "markdown") == "**b**\n"
    assert document_to(doc, "note") == "b"


# -- drafts open the way they were written ------------------------------------------------

def draft(kind, content):
    from types import SimpleNamespace
    return SimpleNamespace(inner_kind=kind, content=content)


def test_an_article_draft_opens_formatted_and_saves_back_unescaped():
    from editor import HtmlEditor
    from main_window import MainWindow
    from nostr.drafts import INNER_KIND_LONG_FORM

    ed = HtmlEditor()
    source = "# Imported\n\nSome **bold** and a [link](https://x.example).\n\n- one\n- two\n"
    MainWindow._load_draft_content(ed, draft(INNER_KIND_LONG_FORM, source))
    assert "**" not in ed.toPlainText()          # shown as formatting, not markup
    assert document_to_markdown(ed.document()) == source


def test_a_note_draft_opens_as_typed():
    from editor import HtmlEditor
    from main_window import MainWindow
    from nostr.drafts import INNER_KIND_SHORT_NOTE

    ed = HtmlEditor()
    MainWindow._load_draft_content(ed, draft(INNER_KIND_SHORT_NOTE, "line one\nline *two*"))
    assert ed.toPlainText() == "line one\nline *two*"
    assert document_to_note_text(ed.document()) == "line one\nline *two*"


# -- what the editor can hold ------------------------------------------------------------

from markdown_writer import holds_faithfully  # noqa: E402


@pytest.mark.parametrize("source", ROUND_TRIPS + [
    "# Imported\n\nSome **bold** and a [link](https://x.example).\n\n- one\n- two\n",
    "* star bullets\n* are fine\n",
    "Setext heading\n==============\n\nText.\n",
    # Footnotes, kept as their text (D5).
    "Footnote[^1] and[^source].\n\n[^1]: The note.\n\n[^source]: https://x.example\n",
    # Pictures with alt text: an article or draft with one opens formatted,
    # the picture in its place, not as its Markdown text.
    "Before\n\n![Sunset](https://blossom.example/" + "a" * 64 + ".png)\n\nAfter\n",
    "A picture ![Map](pictures/map.png) in a line.\n",
    # A picture without a description, as the app writes one added in it.
    "An image ![](https://x.example/a.png) without alt text.\n",
    # A reference definition without a title, and words that only look
    # like one in a list item.
    "[a][r] und mehr.\n\n[r]: /u\n",
    "- [Note]: some text here\n",
])
def test_markdown_the_editor_can_hold(source):
    assert holds_faithfully(source)


@pytest.mark.parametrize("source", [
    "Footnote[^1].\n\n[^1]: The note,\n\n    and its second paragraph.\n",
    "<div>raw</div>\n",
    'A [link](https://x.example "with a title").\n',
    "[![badge](https://x.example/b.png)](https://x.example)\n",
    "> # a heading in a quote\n",
    "- item\n\n  second paragraph of the item\n- two\n",
    # Soft line breaks the editor cannot hold as lines: in an indented
    # paragraph, after a backslash break, where a line may be a table's.
    "  Indented line one\n  line two\n",
    "Line one\\\nline two\n",
    "a | b\nc | d\n",
    # Review M6 (CP3): what the editor's own reader changes.
    "```make\nall:\n\tcc main.c\n```\n",                      # a tab in code
    "| a | b |\n| :--- | ---: |\n| 1 | 2 |\n",                    # alignment
    "| **a** | b |\n| --- | --- |\n| 1 | 2 |\n",                 # emphasis in a header
    "1. Run this:\n\n   ```sh\n   make\n   ```\n",            # code in a list item
    "Escaped \\[^1\\] stays literal.\n\n[^1]: A note.\n",    # an escaped mark (L3)
    # Gate RG-PG2E-d 4: a reference definition in a quote or a list item
    # (the quote goes, the items become one with the paragraph after
    # them), or with a title (it goes).
    "> [r]: /u\n\nSee [r] and \\*a\\*.\n",
    "- [r]: /u\\*v\n- [s]: /w\\_x\n\n[r] [s] \\*a\\*\n",
    "1. [r]:\n   /u\n\n[r]\n",
    '[a][r] und \\*b\\*.\n\n[r]: /u "t"\n',
    "[a][r]\n\n[r]: /u\n  'Titel'\n",
])
def test_markdown_the_editor_would_lose(source):
    assert not holds_faithfully(source)


def test_a_draft_the_editor_cannot_hold_opens_as_its_text_and_comes_back_unchanged():
    from editor import HtmlEditor
    from main_window import MainWindow
    from nostr.drafts import INNER_KIND_LONG_FORM

    source = 'Footnote[^1] and a [link](https://x.example "with a title")\n\n[^1]: The note.\n'
    ed = HtmlEditor()
    MainWindow._load_draft_content(ed, draft(INNER_KIND_LONG_FORM, source))
    assert ed._markdown_source
    assert ed.toPlainText() == source
    stub = type("Stub", (), {"_asset_manager": None})()
    content, _media = MainWindow._publish_payload(stub, ed, "markdown")
    assert content == source.rstrip("\n")


def test_undo_right_after_opening_a_draft_keeps_it():
    from editor import HtmlEditor
    from main_window import MainWindow
    from nostr.drafts import INNER_KIND_LONG_FORM

    ed = HtmlEditor()
    MainWindow._load_draft_content(ed, draft(INNER_KIND_LONG_FORM, "# Title\n\nBody\n"))
    ed.document().undo()
    assert "Body" in ed.toPlainText()


# -- soft line breaks: lines of one paragraph, kept as lines ------------------------------

from markdown_writer import SOFT_BREAK_PROPERTY, _image_as_read, read_markdown  # noqa: E402

SOFT_WRAPPED = [
    "Line one\nLine two of the same paragraph\n",
    "# Plan for October\n\nFinish the chapter on harbours and send it to Anna.\n"
    "![Beach](Pictures/Beach.png)\n",
    "Hard-wrapped **bold\nwords** and a [link that\nwraps](https://x.example).\n\nNext.\n",
    "One\ntwo\nthree\n\nAnd a second paragraph\nof two lines.\n",
]


@pytest.mark.parametrize("source", SOFT_WRAPPED)
def test_a_soft_wrapped_paragraph_opens_formatted_and_keeps_its_lines(source):
    assert holds_faithfully(source)
    doc = QTextDocument()
    read_markdown(doc, source)
    assert document_to_markdown(doc, _image_as_read) == source


def test_a_soft_break_shows_as_a_new_line_of_the_paragraph():
    doc = QTextDocument()
    read_markdown(doc, "Line one\nline two\n")
    assert doc.blockCount() == 1
    assert doc.begin().text() == "Line one\u2028line two"


def test_a_line_break_typed_after_a_soft_one_is_a_hard_break():
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from editor import HtmlEditor
    ed = HtmlEditor()
    ed.show()
    read_markdown(ed.document(), "first line\nsecond line\n")
    cursor = ed.textCursor()
    cursor.setPosition(len("first line") + 1)        # just after the soft break
    ed.setTextCursor(cursor)
    QTest.keyClick(ed, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
    ed.insertPlainText("new")
    breaks = [fmt.boolProperty(SOFT_BREAK_PROPERTY) for text, fmt in _runs(ed.document())
              for char in text if char == "\u2028"]
    assert breaks == [True, False]
    assert document_to_markdown(ed.document()) == "first line\n  \nnewsecond line\n"
    ed.close()


def _runs(doc):
    from doc_walk import iter_block_runs, iter_blocks
    return [run for block in iter_blocks(doc) for run in iter_block_runs(block)]


def test_a_preview_reads_a_soft_break_as_readers_do():
    from nostr.preview import MARKDOWN_FEATURES
    doc = QTextDocument()
    read_markdown(doc, "Line one\nline two\n", MARKDOWN_FEATURES)
    assert "\u2028" not in doc.toPlainText() and "\n" not in doc.toPlainText()


# -- photo groups --------------------------------------------------------------------------

import photo_group  # noqa: E402
import photo_group_find  # noqa: E402
import photo_group_edit  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from editor import HtmlEditor  # noqa: E402

GROUP_CORPUS = [
    "![a](a.png)\n\n![b](b.png)\n",
    "Intro\n\n![a](a.png)\n\n*Cap one*\n\n![b](b.png)\n\n*Cap two*\n\n![c](c.png)\n\nOutro text.\n",
    "![a](a.png)\n*Under a*\n\n![b](b.png)\n*Under b*\n",
    "![a](a.png) *beside a*\n\n![b](b.png)\n",
    "![a](a.png) ![b](b.png)\n",
    "![a](a.png)\n![b](b.png)\n![c](c.png)\n![d](d.png)\n![e](e.png)\n",
    # #wide in a group: at the start, in the middle, at the end, two in a row.
    "![a](plan.png#wide)\n\n![b](b.png)\n\n![c](c.png)\n",
    "![a](a.png)\n\n![b](plan.png#wide)\n\n![c](c.png)\n",
    "![a](a.png)\n\n![b](b.png)\n\n![c](plan.png#wide)\n\n*The plan*\n",
    "![a](a.png)\n\n![b](one.png#wide)\n\n![c](two.png#wide)\n\n![d](d.png)\n",
    "# Title\n\n- list\n- items\n\n![a](a.png)\n\n![b](b.png)\n\n> quote\n\n"
    "![c](c.png)\n\n![d](d.png)\n\n![e](e.png)\n",
    "![Sunset over the bay](https://blossom.example/" + "a" * 64 + ".png)\n\n"
    "*Evening, Kiel*\n\n![Harbour](https://blossom.example/" + "b" * 64 + ".jpg)\n",
    # Not groups: one picture, words between, a picture in a quote.
    "![only](one.png)\n\nText\n",
    "![a](a.png)\n\nWords\n\n![b](b.png)\n",
    "> ![a](a.png)\n\n![b](b.png)\n",
    # Past the engine's limit: every picture a row of its own.
    "\n\n".join(f"![p{n}](p{n}.png)" for n in range(61)) + "\n",
]


# Short ids: a test's name must fit where Windows keeps it (test_portability).
GROUP_IDS = [f"document {n}" for n in range(len(GROUP_CORPUS))]


def _read_folded(source: str) -> QTextDocument:
    doc = QTextDocument()
    read_markdown(doc, source)
    return doc


def test_a_picture_with_its_caption_on_the_next_line_is_written_once():
    """The soft break after a picture is not a second copy of it."""
    source = "![a](a.png)\n*The caption*\n\nText\n"
    doc = _read_folded(source)
    assert document_to_markdown(doc, _image_as_read) == source


def test_pictures_that_follow_each_other_open_as_a_group():
    assert photo_group.has_groups(_read_folded("![a](a.png)\n\n![b](b.png)\n"))
    assert not photo_group.has_groups(_read_folded("![a](a.png)\n\nWords\n\n![b](b.png)\n"))


@pytest.mark.parametrize("source", GROUP_CORPUS, ids=GROUP_IDS)
def test_a_document_with_groups_comes_back_byte_for_byte(source):
    assert holds_faithfully(source)
    assert document_to_markdown(_read_folded(source), _image_as_read) == source


@pytest.mark.parametrize("source", GROUP_CORPUS, ids=GROUP_IDS)
def test_groups_are_written_as_if_they_were_not_there(source, monkeypatch):
    """Markdown and a note's text alike: what the paragraphs give."""
    folded = _read_folded(source)
    monkeypatch.setattr(photo_group_find, "fold", lambda *a, **k: 0)
    plain = _read_folded(source)
    assert not photo_group.has_groups(plain)
    assert document_to_markdown(folded, _image_as_read) == document_to_markdown(plain, _image_as_read)
    assert document_to_note_text(folded, _image_as_read) == document_to_note_text(plain, _image_as_read)


def test_captions_stay_with_their_pictures_through_fold_and_unfold():
    source = GROUP_CORPUS[1]
    doc = _read_folded(source)
    pairs = [(unit.name, unit.caption_text) for group in photo_group.groups_in(doc)
             for unit in group.units]
    assert pairs == [("a.png", "Cap one"), ("b.png", "Cap two"), ("c.png", "")]
    photo_group.unfold(doc)
    assert document_to_markdown(doc, _image_as_read) == source


def test_a_caption_changed_in_a_group_is_written_in_its_place():
    doc = _read_folded("![a](a.png)\n\n*Old words*\n\n![b](b.png)\n")
    group = photo_group.groups_in(doc)[0]
    photo_group_edit.set_unit(QTextCursor(doc), group.start,
                              group.units[0].captioned("New words"))
    assert document_to_markdown(doc, _image_as_read) == "![a](a.png)\n\n*New words*\n\n![b](b.png)\n"


def test_a_caption_in_bold_and_with_a_link_is_written_as_one_italic_stretch():
    """Written "*The* ***old*** *jetty*", the spaces between would be upright
    and the line no caption, for STANDUP and when read again."""
    source = "![a](a.png)\n\n*The **old** jetty at [Loch Ossian](https://e.org/loch)*\n"
    assert document_to_markdown(_read_folded(source), _image_as_read) == source


def test_a_break_tag_between_two_pictures_is_read_as_a_break():
    """Gate 17: pictures pasted from a page come as "![a](a.png)<br>![b]
    (b.png)". Read as a break, each picture keeps its description, and
    the two are a group; a tag elsewhere, or in code, stays what it was."""
    from markdown_writer import PASTE_FEATURES
    for features in (PASTE_FEATURES, READ_FEATURES):
        doc = QTextDocument()
        read_markdown(doc, "![a](a.png)<br>![b](b.png) <BR />\n![c](c.png)\n", features)
        assert document_to_markdown(doc, _image_as_read) == (
            "![a](a.png)\n\n![b](b.png)\n\n![c](c.png)\n")
        assert [unit.alt for unit in photo_group.groups_in(doc)[0].units] == ["a", "b", "c"]
    for source in ("In HTML, <br> breaks a line.\n", "`![a](a.png)<br>![b](b.png)`\n"):
        doc = QTextDocument()
        read_markdown(doc, source, PASTE_FEATURES)
        assert document_to_markdown(doc, _image_as_read) == source


def test_what_a_file_escaped_is_saved_escaped():
    # Qt's reader read "\*Stern\*" as the text "*Stern*", saved as emphasis.
    source = ("Ein \\*Stern\\* hier.\n\n![a](a.png)\n\n*Ein \\*Stern\\* hier*\n\n"
              "![b](b.png)\n")
    doc = QTextDocument()
    read_markdown(doc, source)
    assert doc.firstBlock().text() == "Ein *Stern* hier."
    assert [unit.caption_line for unit in photo_group.groups_in(doc)[0].units] == [
        "Ein *Stern* hier", ""]
    assert document_to_markdown(doc, _image_as_read) == source
    assert holds_faithfully(source)


def test_words_typed_over_escaped_text_are_saved_as_typed():
    # Gate RG-PG2E-d 5: typed over a selection, the words took the escaped
    # star's mark and were saved "x \\*neu\\* y". Undo brings back what was.
    ed = HtmlEditor()
    ed.show()
    read_markdown(ed.document(), "Ein \\*Stern\\* hier.\n")
    cursor = ed.textCursor()
    cursor.setPosition(len("Ein "))
    cursor.setPosition(len("Ein *Stern*"), QTextCursor.MoveMode.KeepAnchor)
    ed.setTextCursor(cursor)
    QTest.keyClicks(ed, "x *neu* y")
    assert document_to_markdown(ed.document(), _image_as_read) == "Ein x *neu* y hier.\n"
    ed.document().undo()
    assert document_to_markdown(ed.document(), _image_as_read) == "Ein \\*Stern\\* hier.\n"
    ed.close()


def test_an_escape_saved_bare_would_change_the_text_so_it_is_kept():
    # Saved bare, "1\." and "1\)" at a line's start made a list, "\!"
    # before a link a picture, "\|" in a table cell a column. Text the file
    # did not escape stays bare.
    source = ("1\\. Platz geht an Anna.\n\n1\\) Punkt eins\n\nWow\\![Link](https://ex.com/)\n\n"
              "| a | b |\n| --- | --- |\n| x \\| y | z |\n\n"
              "Wow! Version 2.0 und a|b bleiben, wie sie sind.\n")
    doc = QTextDocument()
    read_markdown(doc, source)
    assert document_to_markdown(doc, _image_as_read) == source


def test_a_file_that_holds_the_escape_mark_is_read_as_it_is():
    # U+2E2C stood before each escape while Qt read a file: a file that held
    # it lost its escapes on save, and "&#x2E2C;" was taken for a mark.
    source = "Vier Punkte ⸬ und ein \\*Stern\\*.\n"
    doc = QTextDocument()
    read_markdown(doc, source)
    assert document_to_markdown(doc, _image_as_read) == source
    doc = QTextDocument()
    read_markdown(doc, "Vier Punkte &#x2E2C; und ein \\*Stern\\*.\n")
    assert doc.firstBlock().text() == "Vier Punkte ⸬ und ein *Stern*."
    assert document_to_markdown(doc, _image_as_read).endswith("\\*Stern\\*.\n")


def test_emphasis_that_ends_a_wrapped_line_is_closed_on_it():
    # The soft break took the italic of the word before it, and the star
    # closing it went to the next line: "*Wort\n*weiter", no emphasis.
    for source in ("Ein *Wort*\nweiter.\n", "*eine lange\nZeile*\n"):
        doc = QTextDocument()
        read_markdown(doc, source)
        assert document_to_markdown(doc, _image_as_read) == source


@pytest.mark.parametrize("source, saved", [
    ("…unter https://example.org/docs\nund im Handbuch.\n", None),
    ("Die Anleitung liegt auf www.example.org\nund ist kurz.\n", None),
    ("Fragen gern an hallo@example.org\noder im Forum.\n", None),
    ("Siehe www.example.org.\nweiter\n", None),
    ("A claim[^1] and another[^2].\n[^1]: https://example.com/source\n[^2]: Ibid.\n", None),
    ("*a*\n*b*\n", None),
    ("Ein *[Link](u)*\nweiter.\n", "Ein [*Link*](u)\nweiter.\n"),
    ("Ein *`code`*\nweiter.\n", "Ein `code`\nweiter.\n"),     # as on one line
])
def test_what_ends_a_wrapped_line_keeps_its_format_to_itself(source, saved):
    # Gate RG-PG2E-d 2: the break took the format of the word before it, a
    # link, code or emphasis, and joined it to the next line: the file
    # opened as text, or saved "[*Link*\n](u)weiter" and "*a\nb*".
    assert holds_faithfully(source)
    doc = QTextDocument()
    read_markdown(doc, source)
    assert document_to_markdown(doc, _image_as_read) == (saved or source)


def test_a_soft_wrapped_line_ends_as_a_line_end_does():
    # A word joiner marked a soft break while Qt read the file: no space,
    # so "*(Wort)*" ending a wrapped line showed its stars, and a word
    # joiner the file held was deleted.
    source = "Ein *(Wort)*\nweiter mit Wort\u2060joiner.\n"
    doc = QTextDocument()
    read_markdown(doc, source)
    assert doc.firstBlock().text() == "Ein (Wort)\u2028weiter mit Wort\u2060joiner."
    assert [text for text, fmt in iter_block_runs(doc.firstBlock())
            if fmt.fontItalic() and text.strip()] == ["(Wort)"]
    assert document_to_markdown(doc, _image_as_read) == source


def test_the_escape_mark_stays_out_of_addresses_and_file_names():
    # Gate RG-PG2E-c 2: a definition in a quote or a list got the mark in
    # its address ("notes/a⸬*b.md", "plan⸬_1.png"), and with it the save,
    # the copy, the export and the publish payload. One the scan does not
    # know (its label on two lines) is read without marks.
    cases = [
        ("> Siehe [Notiz][r] und \\*Stern\\*.\n>\n> [r]: notes/a\\*b.md\n", "notes/a*b.md"),
        ("- ![Plan][p]\n- [p]: plan\\_1.png\n", "plan_1.png"),
        ("Siehe [Notiz][r x].\n\n[r\nx]: notes/a\\*b.md\n\nUnd \\*Stern\\*.\n", "notes/a*b.md"),
    ]
    for source, address in cases:
        doc = _read_folded(source)
        held = [fmt.anchorHref() or fmt.toImageFormat().name()
                for text, fmt in _runs(doc) if fmt.isAnchor() or fmt.isImageFormat()]
        assert held == [address]
        assert "⸬" not in document_to_markdown(doc, _image_as_read) + doc.toHtml()
    assert "\\*Stern\\*" in document_to_markdown(_read_folded(cases[0][0]), _image_as_read)
    # Read without marks, the stars would be saved as emphasis.
    assert not holds_faithfully(cases[2][0])


def test_an_escape_in_a_web_address_is_saved_escaped():
    # A web address that holds an escape is text to Qt's reader, so its
    # escapes are the text's (the scan stepped over it, and "\*" in it
    # was saved bare).
    source = "Siehe www.x.com/\\*y\\* und https://x.com/a\\_b\\_c.\n"
    assert document_to_markdown(_read_folded(source), _image_as_read) == source
    assert holds_faithfully(source)


class _Counted(str):
    """A text that counts how often a scan reads a character of it."""
    reads = 0

    def __getitem__(self, key):
        _Counted.reads += 1
        return super().__getitem__(key)


@pytest.mark.parametrize("run", ["](a (b ", "![a ", "](x", "![a](x"])
def test_the_escape_scan_reads_a_text_once_however_many_openers_it_holds(run):
    # Gate RG-PG2E-c 5 and RG-PG2E-d 3: each opener nothing closed was
    # followed to the end of the text, or of its address: twice the text,
    # four times the reading.
    import markdown_scan
    reads = []
    for count in (1000, 2000):
        _Counted.reads = 0
        text = _Counted("\\* " + run * count)
        markdown_scan.escapes(text)
        markdown_scan.pictures(text)
        reads.append(_Counted.reads)
    assert reads[1] < 2.5 * reads[0]
