# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins the preview: a note or an article the way Nostr apps show it.

What must hold:

  An article renders its Markdown (headings, emphasis, lists, quotes,
  code, links) under a header of title, summary and byline with the
  reading time, and its tags after the body.

  A note is plain text: ``**`` stays literal, line breaks stay, links,
  hashtags and mentions are colored, and images the note ends with show
  below the text instead of as an address.

  A reference to a person reads "@Name" (or a shortened key), to a note
  or an article as a labelled link; references inside code stay as
  written. Raw HTML in an article shows as text, as in the apps.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QFont, QTextDocument  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from nostr import bech32  # noqa: E402
from nostr import preview as pv  # noqa: E402

PUBKEY = "ab" * 32
NPUB = bech32.encode_npub(PUBKEY)


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    return QApplication.instance() or QApplication([])


def names(pubkey):
    return "Alice" if pubkey == PUBKEY else None


def blocks(doc: QTextDocument):
    block = doc.begin()
    while block.isValid():
        yield block
        block = block.next()


# -- references -----------------------------------------------------------------------

def test_a_person_reads_as_their_name_or_a_short_key():
    assert pv.reference_label(f"nostr:{NPUB}", names) == "@Alice"
    other = bech32.encode_npub("cd" * 32)
    label = pv.reference_label(f"nostr:{other}", names)
    assert label.startswith("@npub1") and "…" in label


def test_notes_and_articles_read_as_labelled_links():
    nevent = bech32.encode_note("ef" * 32)
    assert pv.reference_label(f"nostr:{nevent}", names) == "Quoted note"


def test_references_in_code_stay_as_written():
    md = f"See nostr:{NPUB}\n\n```\nnostr:{NPUB}\n```\n\nand `nostr:{NPUB}`"
    out = pv.markdown_for_preview(md, names)
    assert out.startswith(f"See [@Alice](nostr:{NPUB})")
    assert f"```\nnostr:{NPUB}\n```" in out
    assert f"`nostr:{NPUB}`" in out


# -- articles ----------------------------------------------------------------------------

ARTICLE = pv.Article(title="Why self-custody matters", summary="Keys, not promises.",
                     tags=("bitcoin", "selfcustody"), author="Alice")


def test_an_article_has_its_header_body_and_tags():
    md = "## Start small\n\nSome **bold** words.\n\n- one\n- two\n\n> quoted\n"
    doc = pv.article_document(md, ARTICLE, names=names)
    text = doc.toPlainText()
    assert text.index("Why self-custody matters") < text.index("Keys, not promises.")
    assert "Alice · " in text and "1 min read" in text
    assert text.index("Start small") < text.index("#bitcoin   #selfcustody")
    heading = next(b for b in blocks(doc) if b.text() == "Start small")
    assert heading.blockFormat().headingLevel() == 2
    bold = [f for b in blocks(doc) for f in _fragments(b) if f.text() == "bold"]
    assert bold and bold[0].charFormat().fontWeight() >= QFont.Weight.Bold


def _fragments(block):
    it = block.begin()
    while not it.atEnd():
        yield it.fragment()
        it += 1


def test_a_table_after_code_keeps_its_header():
    md = "```\ncode\n```\n\n| Step | Time |\n| --- | --- |\n| Backup | 10 min |\n"
    doc = pv.article_document(md, pv.Article(title="T"))
    step = next(b for b in blocks(doc) if b.text() == "Step")
    assert step.previous().text() != ""                  # no stray empty line
    assert next(_fragments(step)).charFormat().fontWeight() >= QFont.Weight.Bold


def test_list_markers_are_drawn_at_the_size_of_the_words():
    # Review L7: the markers used the document's default font, small and
    # raised next to the 13.5 pt serif of the words.
    doc = pv.body_document("- Bread\n- Coffee\n")
    assert doc.defaultFont().pointSizeF() == 13.5
    assert doc.defaultFont().families()[0] == pv.SERIF[0]

def test_reading_time_follows_the_words():
    assert pv.reading_minutes("word " * 225) == 1
    assert pv.reading_minutes("word " * 226) == 2


def test_underscores_are_italic_as_in_every_reader():
    doc = pv.article_document("an _emphasized_ word\n", pv.Article(title="T"))
    word = [f for b in blocks(doc) for f in _fragments(b) if f.text() == "emphasized"]
    assert word and word[0].charFormat().fontItalic()
    assert not word[0].charFormat().fontUnderline()


def test_raw_html_shows_as_text_in_an_article():
    doc = pv.article_document("<b>not bold</b> text\n", pv.Article(title="T"))
    assert "<b>not bold</b>" in doc.toPlainText()


def _pieces(doc):
    """(text, format) of every piece of ``doc``, copied."""
    from PySide6.QtGui import QTextCharFormat
    out = []
    block = doc.begin()
    while block.isValid():
        it = block.begin()
        while not it.atEnd():
            fragment = it.fragment()
            if fragment.isValid():
                out.append((fragment.text(), QTextCharFormat(fragment.charFormat())))
            it += 1
        block = block.next()
    return out


FOOTNOTES = ("A claim[^b] and more[^a], again[^b]. Not[^9] a note, nor `[^a]`.\n\n"
             "[^a]: Ibid.\n\n[^b]: [The source](https://example.com/source)\n\n"
             "[^z]: Never marked.\n")


def test_footnotes_show_as_the_readers_draw_them():
    # The early review's H3 had the preview drop the notes; now they are
    # drawn the way the long-form readers do.
    doc = pv.article_document(FOOTNOTES, pv.Article(title="T"))
    text = doc.toPlainText()
    assert "A claim1 and more2, again1. Not[^9] a note, nor [^a]." in text
    assert "Never marked" not in text and "[^b]:" not in text
    blocks = [b for b in text.split("\n") if b.strip()]
    assert blocks[-2:] == ["The source \u21a9", "Ibid. \u21a9"]     # by first use
    pieces = _pieces(doc)
    marks = [(t, f.anchorHref(), f.anchorNames()) for t, f in pieces
             if f.verticalAlignment() == f.VerticalAlignment.AlignSuperScript]
    assert marks == [("1", "#fn-1", ["fnref-1"]), ("2", "#fn-2", ["fnref-2"]),
                     ("1", "#fn-1", [])]
    backs = [f.anchorHref() for t, f in pieces if t == "\u21a9"]
    assert backs == ["#fnref-1", "#fnref-2"]
    places = [f.anchorNames() for t, f in pieces if f.anchorNames() and f.anchorNames()[0]
              .startswith("fn-")]
    assert places == [["fn-1"], ["fn-2"]]
    linked = "".join(t for t, f in pieces if f.anchorHref() == "https://example.com/source")
    assert linked == "The source"                                  # the note keeps its link


def test_an_article_without_notes_shows_its_marks_as_typed():
    doc = pv.article_document("A claim[^1].\n", pv.Article(title="T"))
    assert "A claim[^1]." in doc.toPlainText()


def test_an_untitled_article_says_so():
    doc = pv.article_document("Body\n", pv.Article())
    assert doc.toPlainText().startswith("Untitled")


# -- notes ----------------------------------------------------------------------------

def test_a_note_is_plain_text_with_its_line_breaks():
    doc = pv.note_document("Hello **world**\nsecond line", author="Alice")
    text = doc.toPlainText()
    assert "Hello **world**\nsecond line" in text
    assert text.startswith("Alice")


def test_images_that_end_a_note_show_below_it():
    text, media = pv.split_media("Look at this https://x.example/a.jpg")
    assert text == "Look at this" and media == ["https://x.example/a.jpg"]
    html = pv.note_html("Look https://x.example/a.jpg")
    assert '<img src="https://x.example/a.jpg"' in html


def test_an_image_in_the_middle_of_a_note_keeps_its_place_too():
    text, media = pv.split_media("A https://x.example/a.png B")
    assert text == "A https://x.example/a.png B" and media == ["https://x.example/a.png"]


def test_links_hashtags_and_mentions_are_colored_and_text_is_escaped():
    html = pv.note_html(f"<i>hi</i> #nostr https://x.example nostr:{NPUB}", names=names)
    assert "&lt;i&gt;hi&lt;/i&gt;" in html
    assert '<a href="https://x.example"' in html
    assert ">#nostr</span>" in html and ">@Alice</span>" in html


def test_many_empty_lines_show_as_one():
    html = pv.note_html("a\n\n\n\nb")
    assert "a\n\nb" in html


# -- the widget ----------------------------------------------------------------------------

def test_the_widget_uses_only_the_images_it_is_given():
    seen = []
    view = pv.NostrPreview(images=lambda url: seen.append(url) or None)
    view.show_note("Pic https://x.example/a.png")
    view.resize(900, 600)
    view.show()
    QApplication.processEvents()
    assert seen == ["https://x.example/a.png"]
    view.close()


# -- photo groups --------------------------------------------------------------------------
# The preview shows a group as readers do: the mosaic in a wide column,
# below 600 pixels the runs, each strip one picture a line (nothing here
# swipes); every picture whole.

import photo_group_view  # noqa: E402
from tests.photo_corners import SHAPES, image_of, shows_whole  # noqa: E402

STRIPPED = ("![a](landscape.png)\n\n![b](photo.png)\n\n![c](photo.png)\n\n![d](landscape.png)\n\n"
            "![e](portrait.png)\n\n*A caption under the portrait*\n\n![f](plan.png#wide)\n")


def _group_preview(width):
    from PySide6.QtWidgets import QApplication as App
    view = pv.NostrPreview(images=lambda name: image_of(name) if name in SHAPES else None)
    view.resize(width, 3200)
    view.show_markdown(STRIPPED)
    view.show()
    App.processEvents()
    return view


@pytest.mark.parametrize("width", [1000, 420])
def test_a_group_in_the_preview_shows_every_picture_whole(width):
    view = _group_preview(width)
    shot = view.viewport().grab().toImage()
    boxes = photo_group_view.picture_boxes(view.document())
    assert len(boxes) == 6
    for _p, name, box in boxes:
        assert shows_whole(shot, box), name
        assert box.right() <= view.viewport().width()
    view.close()


def test_a_narrow_preview_shows_the_runs_strips_one_picture_a_line():
    def tops(view):
        return [round(box.top()) for _p, _n, box in
                photo_group_view.picture_boxes(view.document())]

    wide = _group_preview(1000)
    assert len(set(tops(wide))) < len(tops(wide))            # the mosaic: rows of pictures
    narrow = _group_preview(420)
    assert len(set(tops(narrow))) == len(tops(narrow))
    revision = narrow.document().revision()
    narrow.resize(1000, 3200)
    from PySide6.QtWidgets import QApplication as App
    App.processEvents()
    assert tops(narrow) == tops(wide)                        # the mosaic again
    assert narrow.document().revision() == revision          # by its layout alone
    wide.close()
    narrow.close()
