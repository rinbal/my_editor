# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""photo_group: pictures that follow each other, held as one group.

Groups are found the way STANDUP finds them. Folding makes each group one
paragraph holding a picture character for each picture, its caption on
it; unfolding gives the very same paragraphs back, and a writer writes
them without unfolding. Nothing about how a group is arranged is in the
document.
"""

from __future__ import annotations

import time

import pytest
from PySide6.QtCore import QUrl
from PySide6.QtGui import QTextCursor, QTextDocument, QTextDocumentFragment
from PySide6.QtWidgets import QApplication

import photo_group
import photo_group_find
from markdown_writer import _image_as_read, document_to_markdown, read_markdown
from tests.photo_corners import SHAPES


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _read(markdown: str) -> QTextDocument:
    """Markdown as Qt reads it, with the groups folded."""
    doc = QTextDocument()
    doc.setMarkdown(markdown)
    photo_group_find.fold(doc)
    return doc


def _markdown(*names, captions=None) -> str:
    captions = captions or {}
    parts = []
    for name in names:
        parts.append(f"![{name}]({name})")
        if name in captions:
            parts.append(f"*{captions[name]}*")
    return "\n\n".join(parts) + "\n"


def _paragraphs(doc):
    """Each paragraph as "G<pictures>" for a group, else its text."""
    out = []
    block = doc.begin()
    while block.isValid():
        out.append(f"G{len(photo_group.block_units(block))}"
                   if photo_group.is_group_block(block) else block.text())
        block = block.next()
    return out


# --------------------------------------------------------------------------- #
# #wide                                                                        #
# --------------------------------------------------------------------------- #

def test_wide_is_read_from_the_end_of_the_address_and_loaded_without():
    assert photo_group.is_wide("plan.png#wide")
    assert photo_group.is_wide("https://e.org/plan.png#WIDE")
    assert not photo_group.is_wide("plan.png")
    assert not photo_group.is_wide("plan.png#wider")
    assert photo_group.without_wide("plan.png#wide") == "plan.png"
    assert photo_group.without_wide("plan.png") == "plan.png"
    url = photo_group.without_wide_url(QUrl("https://e.org/p.png#wide"))
    assert url.toString() == "https://e.org/p.png"
    assert photo_group.without_wide_url(QUrl("p.png#top")).toString() == "p.png#top"


# --------------------------------------------------------------------------- #
# Finding groups as STANDUP does                                               #
# --------------------------------------------------------------------------- #

def _found(markdown: str):
    doc = QTextDocument()
    doc.setMarkdown(markdown)
    return [[(unit.name, unit.caption_text) for unit in group.units]
            for group in photo_group_find.find_groups(doc)]


def test_two_pictures_with_empty_lines_between_are_a_group(app):
    assert _found("![a](a.png)\n\n![b](b.png)\n") == [[("a.png", ""), ("b.png", "")]]


def test_one_picture_is_no_group(app):
    assert _found("Text\n\n![a](a.png)\n\nMore\n") == []


def test_words_between_pictures_end_a_group(app):
    assert _found("![a](a.png)\n\nWords\n\n![b](b.png)\n") == []


def test_a_caption_under_its_picture_or_in_the_next_paragraph(app):
    assert _found("![a](a.png)\n*Under it*\n\n![b](b.png)\n\n*Next paragraph*\n") == [
        [("a.png", "Under it"), ("b.png", "Next paragraph")]]


def test_pictures_in_one_paragraph_are_pictures_of_the_group(app):
    assert _found("![a](a.png) ![b](b.png)\n![c](c.png)\n") == [
        [("a.png", ""), ("b.png", ""), ("c.png", "")]]


def test_what_is_not_one_emphasis_is_no_caption(app):
    # Two emphases, bold, or words after the picture: a paragraph, which
    # ends the group (STANDUP's _caption_only and _figure_parts).
    assert _found("![a](a.png)\n\n*one* *two*\n\n![b](b.png)\n\n![c](c.png)\n") == [
        [("b.png", ""), ("c.png", "")]]
    assert _found("![a](a.png)\n\n**bold**\n\n![b](b.png)\n") == []
    assert _found("![a](a.png) and words\n\n![b](b.png)\n") == []


def test_a_picture_in_a_quote_or_list_is_not_in_a_group(app):
    assert _found("> ![a](a.png)\n\n![b](b.png)\n") == []
    assert _found("- ![a](a.png)\n\n![b](b.png)\n") == []


def test_audio_and_video_are_not_pictures(app):
    assert _found("![a](a.mp4)\n\n![b](b.png)\n") == []


def test_a_picture_directly_below_a_group_belongs_to_it(app):
    assert len(_found(_markdown("photo.png", "square.png", "landscape.png"))[0]) == 3


def test_wide_stays_in_the_group(app):
    groups = _found(_markdown("photo.png", "plan.png#wide", "square.png"))
    assert [name for name, _ in groups[0]] == ["photo.png", "plan.png#wide", "square.png"]


def test_pictures_each_side_of_a_table_are_no_group(app):
    assert _found("![a](a.png)\n\n| t |\n|---|\n| c |\n\n![b](b.png)\n") == []


def test_a_picture_in_a_table_is_not_in_a_group(app):
    assert _found("| ![a](a.png) |\n|---|\n| c |\n\n![b](b.png)\n") == []


def test_groups_are_found_where_letters_take_two_places(app):
    """A character beyond U+FFFF takes two positions in a document."""
    doc = _read("An emoji \U0001F600 here\n\n![a](a.png)\n\n![b](b.png)\n\nEnd \U0001F600\n")
    assert _paragraphs(doc) == ["An emoji \U0001F600 here", "G2", "End \U0001F600"]


# --------------------------------------------------------------------------- #
# Fold and unfold                                                              #
# --------------------------------------------------------------------------- #

def test_a_group_is_one_paragraph_with_a_character_for_each_picture(app):
    doc = _read("Before\n\n" + _markdown("portrait.png", "landscape.png", "square.png",
                                         captions={"landscape.png": "The caption"}) + "\nAfter\n")
    assert _paragraphs(doc) == ["Before", "G3", "After"]
    group = photo_group.groups_in(doc)[0]
    assert doc.findBlock(group.start).text() == "\ufffc" * 3
    assert [(unit.name, unit.caption_text) for unit in group.units] == [
        ("portrait.png", ""), ("landscape.png", "The caption"), ("square.png", "")]
    assert photo_group.group_at(doc, group.start + 1).units[1].name == "landscape.png"
    assert photo_group.group_at(doc, 0) is None


def test_the_captions_of_a_documents_groups_can_be_read_for_counting_its_words(app):
    doc = _read(_markdown("photo.png", "square.png", captions={"photo.png": "Two words"})
                + "\nText\n\n" + _markdown("photo.png", "square.png",
                                           captions={"square.png": "Three more words"}))
    assert photo_group.captions(doc) == ["Two words", "Three more words"]
    assert "Two words" not in doc.toPlainText()


def test_every_picture_has_its_own_number(app):
    doc = _read(_markdown("photo.png", "photo.png", "square.png"))
    numbers = [unit.number for group in photo_group.groups_in(doc) for unit in group.units]
    assert len(numbers) == 3 and len(set(numbers)) == 3


UNFOLDED = [
    _markdown("photo.png", "square.png"),
    "Intro\n\n![a](photo.png)\n*Under*\n\n![b](square.png)\n\n*Own paragraph*\n\nEnd\n",
    "![a](photo.png) ![b](square.png)\n![c](portrait.png)\n\n![d](plan.png#wide)\n",
    "# T\n\n![a](photo.png)\n\n![b](square.png)\n\nText\n\n![c](portrait.png)\n\n![d](panorama.png)\n",
    "![a](photo.png)\n\n*With **bold**, ~~struck~~ and a [link](https://e.org) in it*\n\n"
    "![b](square.png)\n",
]


def test_unfold_gives_back_the_paragraphs_as_they_were(app):
    for markdown in UNFOLDED:
        plain = QTextDocument()
        read_markdown_plain(plain, markdown)
        want = document_to_markdown(plain, _image_as_read)
        doc = QTextDocument()
        read_markdown(doc, markdown)
        assert photo_group.has_groups(doc)
        # Written from the group as it is, then from its paragraphs again.
        assert document_to_markdown(doc, _image_as_read) == want
        assert photo_group.unfold(doc) >= 1
        assert not photo_group.has_groups(doc)
        assert document_to_markdown(doc, _image_as_read) == want
        assert doc.toPlainText() == plain.toPlainText()
        formats = [(b.blockFormat().topMargin(), b.blockFormat().bottomMargin())
                   for b in _blocks(doc)]
        assert formats == [(b.blockFormat().topMargin(), b.blockFormat().bottomMargin())
                           for b in _blocks(plain)]


def read_markdown_plain(doc, markdown: str) -> None:
    """Markdown as the editor reads it, its pictures left as paragraphs."""
    from markdown_writer import _read_counting
    _read_counting(doc, markdown, groups=False)


def _blocks(doc):
    block = doc.begin()
    while block.isValid():
        yield block
        block = block.next()


def test_unfolded_leaves_the_document_as_it_is(app):
    doc = _read(_markdown("photo.png", "square.png"))
    copy = photo_group.unfolded(doc)
    assert copy is not doc and photo_group.has_groups(doc) and not photo_group.has_groups(copy)
    plain = QTextDocument()
    assert photo_group.unfolded(plain) is plain


def test_a_selection_that_starts_inside_a_group_is_copied_as_its_paragraphs(app):
    """A fragment keeps the characters of its first paragraph, not its
    format: the pictures it starts with are unfolded all the same."""
    doc = _read("Before\n\n" + _markdown("photo.png", "square.png", "portrait.png",
                                         captions={"square.png": "Two"}) + "\nAfter\n")
    group = photo_group.groups_in(doc)[0]
    cursor = QTextCursor(doc)
    cursor.setPosition(group.start + 1)
    cursor.setPosition(group.end + 3, QTextCursor.MoveMode.KeepAnchor)
    copy = QTextDocument()
    QTextCursor(copy).insertFragment(photo_group.unfolded_fragment(cursor.selection()))
    assert not photo_group.has_groups(copy)
    assert document_to_markdown(copy, _image_as_read) == (
        "![square.png](square.png)\n\n*Two*\n\n![portrait.png](portrait.png)\n\nAf\n")
    whole = QTextDocumentFragment(doc)
    assert photo_group.unfolded_fragment(QTextDocumentFragment(QTextDocument())).isEmpty()
    copy = QTextDocument()
    QTextCursor(copy).insertFragment(photo_group.unfolded_fragment(whole))
    assert document_to_markdown(copy, _image_as_read) == document_to_markdown(doc, _image_as_read)


def test_what_an_edit_left_beside_a_groups_pictures_is_still_written(app):
    """Text that got between the pictures of a group (before the editor
    put it right) is written as a paragraph of its own, and every picture
    with its caption: a writer loses nothing."""
    doc = _read(_markdown("photo.png", "square.png", captions={"square.png": "Two"}))
    cursor = QTextCursor(doc)
    cursor.setPosition(photo_group.groups_in(doc)[0].start + 1)
    cursor.insertText("typed")
    assert document_to_markdown(doc, _image_as_read) == (
        "![photo.png](photo.png)\n\ntyped\n\n![square.png](square.png)\n\n*Two*\n")


# --------------------------------------------------------------------------- #
# Red team: big groups, many groups                                            #
# --------------------------------------------------------------------------- #

def _cpu(work) -> float:
    start = time.process_time()
    work()
    return time.process_time() - start


def test_a_group_of_500_pictures_is_one_paragraph_and_written_whole(app):
    names = [list(SHAPES)[n % 5] for n in range(500)]
    doc = _read(_markdown(*names))
    assert _paragraphs(doc) == ["G500"]
    assert document_to_markdown(doc, _image_as_read).count("![") == 500


def test_folding_grows_with_the_number_of_groups_not_faster():
    """10,000 groups cost about 10,000 times one: the time grows in step
    with the count (CPU time of 40 and of 320 groups)."""
    def document(count: int) -> str:
        return "\n\nText between.\n\n".join(_markdown("photo.png", "square.png").strip()
                                            for _ in range(count)) + "\n"

    def fold(count: int) -> float:
        markdown = document(count)
        return min(_cpu(lambda: _read(markdown)) for _ in range(2))

    small, large = fold(40), fold(320)
    assert large < small * 8 * 2.5
