# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A picture's description (its alt text) is read whole, as CommonMark
reads it, and written so it reads back the same.

Qt's reader inserted the picture once for every piece of text md4c hands
it inside ``![...]``: an escape, an entity, emphasis, code, a line break
or an autolink each start a piece, so ``![a \\<b\\>](x)`` came out as five
pictures (and five pictures went back into the file). The rules checked
here are CommonMark's (spec 0.31.2, "Backslash escapes", "Entity and
numeric character references", "Images": the description is rendered as
plain text).
"""

from __future__ import annotations

import string

import pytest
from PySide6.QtGui import QTextCursor, QTextDocument, QTextFormat
from PySide6.QtWidgets import QApplication

import photo_group
from markdown_writer import (
    _image_as_read, document_to_markdown, holds_faithfully, image_markdown, read_markdown,
)


@pytest.fixture(scope="module", autouse=True)
def app():
    return QApplication.instance() or QApplication([])


def pictures(markdown: str):
    """(name, description) of each picture ``markdown`` reads into."""
    doc = QTextDocument()
    read_markdown(doc, markdown)
    found = []
    block = doc.begin()
    while block.isValid():
        it = block.begin()
        while not it.atEnd():
            fragment = it.fragment()
            fmt = fragment.charFormat()
            if fmt.isImageFormat():
                image = fmt.toImageFormat()
                found += [(image.name(),
                           image.property(QTextFormat.Property.ImageAltText))] * fragment.length()
            it += 1
        block = block.next()
    return found


# "Any ASCII punctuation character may be backslash-escaped" (spec 2.4).
PUNCTUATION = string.punctuation


@pytest.mark.parametrize("char", list(PUNCTUATION), ids=[f"0x{ord(c):02x}" for c in PUNCTUATION])
def test_an_escaped_punctuation_mark_is_one_picture_with_the_mark(char):
    assert pictures(f"![a\\{char}b](x.png)\n") == [("x.png", f"a{char}b")]


def test_a_backslash_before_anything_else_is_a_backslash():
    # Spec example 13: "Backslashes before other characters are treated as
    # literal backslashes."
    assert pictures("![a\\b\\Ä\\ c](x.png)\n") == [("x.png", "a\\b\\Ä\\ c")]


def test_an_escape_inside_code_is_not_one():
    # Spec example 15: backslash escapes do not work in code spans.
    assert pictures("![`a\\*b` c](x.png)\n") == [("x.png", "a\\*b c")]


def test_entities_emphasis_and_line_breaks_are_their_plain_text():
    assert pictures("![a &amp; &copy; &#35; *em* **strong** ~~gone~~](x.png)\n") == [
        ("x.png", "a & © # em strong gone")]
    assert pictures("![two\nlines](x.png)\n") == [("x.png", "two lines")]
    assert pictures("![see https://e.org/x and me@e.org](x.png)\n") == [
        ("x.png", "see https://e.org/x and me@e.org")]


def test_code_and_escaped_pictures_stay_text():
    assert pictures("```\n![a\\*b](x.png)\n```\n") == []
    assert pictures("`![a\\*b](x.png)`\n") == []
    assert pictures("    ![a\\*b](x.png)\n") == []
    assert pictures("\\![a\\*b](x.png)\n") == []


def test_a_reference_picture_and_a_title_keep_their_meaning():
    assert pictures("![a\\*b][ref]\n\n[ref]: x.png\n") == [("x.png", "a*b")]
    assert pictures("![a\\*b](<x y.png>)\n") == [("x y.png", "a*b")]


@pytest.mark.parametrize("alt", [
    "a*b", "a & b", "a &amp; b", "*em* x", "a [b] c", "a]b", "a [b", "a\\b", "a\\*b",
    "a <b> c", "1 < 2", "`code`", "snake_case_name", "_lead", "~~x~~", "see https://e.org/x",
    "Ä ö ü ß", "שלום", "a​b", "!", "\\",
])
def test_a_description_is_written_so_it_reads_back_the_same(alt):
    written = image_markdown(alt, "x.png")
    assert pictures(written + "\n") == [("x.png", alt)], written
    # Written again from what was read, it is the same.
    assert image_markdown(pictures(written + "\n")[0][1], "x.png") == written


def test_plain_descriptions_are_written_as_they_were():
    for alt in ("Floor plan of the bothy", "a*b", "a & b", "snake_case_name", "1 < 2"):
        assert image_markdown(alt, "x.png") == f"![{alt}](x.png)"


def test_the_editor_keeps_a_document_with_an_escaped_description():
    markdown = "Words.\n\n![The jetty \\<at dusk\\>](p.png)\n\n![Bay](q.png)\n"
    assert holds_faithfully(markdown)
    doc = QTextDocument()
    read_markdown(doc, markdown)
    groups = photo_group.groups_in(doc)
    assert len(groups) == 1
    assert [unit.alt for unit in groups[0].units] == ["The jetty <at dusk>", "Bay"]


def test_a_description_is_written_as_the_file_spelled_it_until_it_is_changed():
    """Gate 13: "Fish &amp; Chips" came back as "Fish & Chips" after any
    edit. The spelling is kept with the picture, alone and in a group, and
    goes when the description itself is changed."""
    alone = "![Fish &amp; Chips](a.png)\n\nText.\n"
    grouped = "![Fish &amp; Chips](a.png)\n\n![The jetty \\<at dusk\\>](b.png)\n\nText.\n"
    for markdown in (alone, grouped):
        assert holds_faithfully(markdown)
        doc = QTextDocument()
        read_markdown(doc, markdown)
        cursor = QTextCursor(doc)
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(" Added.")
        assert document_to_markdown(doc, _image_as_read) == markdown.replace("Text.", "Text. Added.")
    # The description changed: written as the writer spells the new one.
    doc = QTextDocument()
    read_markdown(doc, alone)
    cursor = QTextCursor(doc)
    cursor.setPosition(0)
    cursor.setPosition(1, QTextCursor.MoveMode.KeepAnchor)
    fmt = cursor.charFormat()
    fmt.setProperty(QTextFormat.Property.ImageAltText, "Fish & Chips, new")
    cursor.setCharFormat(fmt)
    assert document_to_markdown(doc, _image_as_read) == "![Fish & Chips, new](a.png)\n\nText.\n"


def test_two_descriptions_that_differ_are_not_the_same_document():
    """The description is part of what the editor checks before it holds
    a file: a reading that lost or changed one is not faithful."""
    from markdown_writer import _fingerprint
    one, other = QTextDocument(), QTextDocument()
    read_markdown(one, "![Fish](a.png)\n")
    read_markdown(other, "![Chips](a.png)\n")
    assert _fingerprint(one) != _fingerprint(other)
