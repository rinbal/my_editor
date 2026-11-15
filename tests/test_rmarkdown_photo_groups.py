# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Photo groups in R Markdown, and on a page knitted from it.

A group is written as a pandoc fenced div around one paragraph: its
pictures and captions on lines joined by hard breaks. A knitted PDF or
Word document shows them one under another, as other apps do: one
paragraph, so no figure floats away and no description becomes a
caption. A page knitted to HTML here has each group laid out as the HTML
export lays it out, with the same markup, stylesheet and scripts.

The knitted pages below are written in the shape pandoc's HTML writer
gives that div (Writers/HTML.hs: a Div is <div class>, a LineBreak
"<br />" and a new line, an Image <img src alt> with its classes, Emph
<em>; with self_contained the src a data: URI), html4 as rmarkdown's
html_document asks for, and again wrapped where pandoc wraps long lines.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtGui import QTextDocument
from PySide6.QtWidgets import QApplication

import export_html
import photo_page
from markdown_writer import read_markdown
from rmarkdown import KnitRunner, document_to_rmd, lay_out_knitted_groups
from tests.photo_corners import PHOTOS
from tests.test_export_photo_groups import (
    CHROME, DOC, NAMES, _check_whole, _look, needs_chrome,
)


@pytest.fixture(scope="module", autouse=True)
def app():
    return QApplication.instance() or QApplication([])


def _doc(markdown: str = DOC) -> QTextDocument:
    doc = QTextDocument()
    read_markdown(doc, markdown)
    return doc


def _uri(name: str) -> str:
    return export_html.image_data_uri((PHOTOS / name).read_bytes())


GROUP_RMD = """::: {.photo-group}
![Steg](story_media/portrait.png)\\
*The jetty at dusk*\\
![Loch](story_media/landscape.png)\\
![Bay](story_media/photo.png)\\
![Shore](story_media/photo.png)\\
![Pier \\$& jetty](story_media/landscape.png)\\
![Hut](story_media/square.png)\\
![Panorama](story_media/panorama.png)\\
![Plan](story_media/plan.png){.wide}\\
*Floor plan of the bothy*
:::"""


def test_a_group_is_a_div_of_one_paragraph_and_the_rest_reads_as_before():
    rmd = document_to_rmd(_doc(), "Bothy", copy_image=lambda name: "story_media/" + name)
    body = rmd.split("---\n", 2)[2]
    assert body == ("\n# Bothy weekend\n\nWe walked in from the station.\n\n" + GROUP_RMD
                    + "\n\nThe next morning it rained.\n")
    # A picture on its own is written as it was.
    lone = document_to_rmd(_doc("Text\n\n![Hut](square.png)\n\nMore\n"), "x",
                           copy_image=lambda name: "m/" + name)
    assert "photo-group" not in lone and "![](m/square.png)" in lone


def test_a_picture_that_cannot_be_had_keeps_its_place_in_the_group():
    rmd = document_to_rmd(_doc("![A](a.png)\n\n![B](b.png)\n"), "x",
                          copy_image=lambda name: None if name == "a.png" else "m/" + name)
    assert "::: {.photo-group}\n*[image unavailable]*\\\n![B](m/b.png)\n:::" in rmd


def _knitted(wrapped: bool = False, group: str = None) -> str:
    """The page pandoc writes for the document's R Markdown."""
    br = "<br />\n"
    if group is None:
        lines = [f'<img src="{_uri("portrait.png")}" alt="Steg" />',
                 "<em>The jetty at dusk</em>",
                 f'<img src="{_uri("landscape.png")}" alt="Loch" />',
                 f'<img src="{_uri("photo.png")}" alt="Bay" />',
                 f'<img src="{_uri("photo.png")}" alt="Shore" />',
                 (f'<img src="{_uri("landscape.png")}"\nalt="Pier $&amp; jetty" />' if wrapped
                  else f'<img src="{_uri("landscape.png")}" alt="Pier $&amp; jetty" />'),
                 f'<img src="{_uri("square.png")}" alt="Hut" />',
                 f'<img src="{_uri("panorama.png")}" alt="Panorama" />',
                 f'<img src="{_uri("plan.png")}" class="wide" alt="Plan" />',
                 "<em>Floor plan of\nthe bothy</em>" if wrapped else "<em>Floor plan of the bothy</em>"]
        group = '<div class="photo-group">\n<p>' + br.join(lines) + "</p>\n</div>"
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8" />
<title>Bothy</title>
<script>var tag = "</head>";</script>
<style>body {{ font-family: serif; margin: 0 auto; max-width: 940px; }}</style>
</head>
<body>
<div class="container-fluid main-container">
<div id="header"><h1 class="title toc-ignore">Bothy</h1></div>
<div id="bothy-weekend" class="section level1">
<h1>Bothy weekend</h1>
<p>We walked in from the station.</p>
{group}
<p>The next morning it rained.</p>
</div>
</div>
<script>var end = "</body>";</script>
</body>
</html>
"""


def _groups(page: str) -> list:
    return re.findall(r'<div class="article-content" lang="en"><div class="e21-imagegroup.*?'
                      r'</div></div></div>', page, re.S)


@pytest.mark.parametrize("wrapped", [False, True], ids=["one-line", "wrapped"])
def test_a_knitted_group_has_the_markup_of_the_export(wrapped):
    exported = export_html.document_to_html(_doc(), "Bothy", image_roots=[str(PHOTOS)])
    knitted = lay_out_knitted_groups(_knitted(wrapped))
    assert len(_groups(exported)) == 1
    assert _groups(knitted) == _groups(exported)
    assert 'class="photo-group"' not in knitted
    # The words around it are pandoc's, untouched.
    assert "<p>We walked in from the station.</p>" in knitted
    assert "<p>The next morning it rained.</p>" in knitted


def test_the_stylesheet_and_scripts_come_once_in_the_pages_light_colours():
    knitted = lay_out_knitted_groups(_knitted())
    head = knitted[:knitted.index("<body")]
    assert head.count(photo_page.group_head(dark=False)) == 1
    assert head.index(photo_page.group_head(dark=False)) > head.index('var tag = "</head>"')
    assert "prefers-color-scheme: dark" not in photo_page.group_head(dark=False)
    assert "prefers-color-scheme: dark" in photo_page.group_head()
    tail = knitted[knitted.rindex(photo_page.group_scripts()):]
    assert knitted.count(photo_page.group_scripts()) == 1
    assert tail.startswith(photo_page.group_scripts() + "</body>\n</html>")


@pytest.mark.parametrize("group", [
    # Not what MyEditor wrote: left as pandoc wrote it.
    '<div class="photo-group">\n<p>Words first<br />\n<img src="a.png" alt="A" /><br />\n'
    '<img src="b.png" alt="B" /></p>\n</div>',
    '<div class="photo-group">\n<p><img src="a.png" alt="A" /></p>\n</div>',
    '<div class="photo-group">\n<p><img src="a.png" alt="A" /> <img src="b.png" alt="B" /></p>\n</div>',
    '<div class="photo-group">\n<p><img src="a.png" alt="A" /></p>\n<p><img src="b.png" alt="B" /></p>\n</div>',
], ids=["words-first", "one-picture", "two-on-a-line", "two-paragraphs"])
def test_a_group_in_another_shape_stays_as_pandoc_wrote_it(group):
    page = _knitted(group=group)
    assert lay_out_knitted_groups(page) == page


def test_a_picture_that_could_not_be_had_is_the_exports_stand_in():
    group = ('<div class="photo-group">\n<p><em>[image unavailable]</em><br />\n'
             f'<img src="{_uri("photo.png")}" alt="Bay" /></p>\n</div>')
    knitted = lay_out_knitted_groups(_knitted(group=group))
    assert len(_groups(knitted)) == 1
    assert "<figure" in knitted and "<em>[image unavailable]</em></figure>" in knitted


class _Done:
    def readAllStandardOutput(self):
        return b""


@pytest.mark.parametrize("fmt", ["html", "pdf"])
def test_a_knit_to_html_lays_out_the_groups_and_one_to_pdf_is_left_alone(tmp_path, fmt):
    out = tmp_path / ("story.html" if fmt == "html" else "story.pdf")
    out.write_text(_knitted(), encoding="utf-8")
    runner = KnitRunner()
    done = []
    runner.finished.connect(done.append)
    runner._rmd_path, runner._fmt = str(tmp_path / "story.Rmd"), fmt
    runner._output, runner._proc = f"Output created: {out.name}\n", _Done()
    runner._on_finished(0, None)
    assert done == [str(out)]
    assert (len(_groups(out.read_text(encoding="utf-8"))) == 1) == (fmt == "html")


@needs_chrome
def test_in_a_browser_the_knitted_group_shows_every_picture_whole(tmp_path):
    probe, image = _look(lay_out_knitted_groups(_knitted()), 1440, tmp_path)
    seen = set()
    _check_whole(probe, image, 1440, seen)
    assert seen == set(range(len(NAMES)))
    assert probe["scrollWidth"] == 1440


def test_the_help_says_how_a_group_knits():
    usage = (Path(__file__).resolve().parent.parent / "docs" / "usage.md").read_text(encoding="utf-8")
    knitting = usage[usage.index("## R Markdown knitting"):]
    assert "one under another" in knitting.split("\n## ")[0]
