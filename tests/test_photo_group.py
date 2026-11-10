# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""photo_group: pictures that follow each other, shown as one group.

Folding builds a group's tables from the paragraphs Markdown gave, the way
STANDUP finds groups; unfolding gives the very same paragraphs back. The
handler sizes the pictures by the readers' CSS from the engine's numbers,
so every picture shows whole (the coloured corners of the fixtures), the
mosaic fills the text's width at any width, and a row is never taller
than its cap. A read-only surface narrower than 600 pixels shows the
runs, strips unrolled: one picture a line.
"""

from __future__ import annotations

import time

import pytest
from PySide6.QtCore import QUrl
from PySide6.QtGui import QImage, QTextDocument, QTextTable
from PySide6.QtWidgets import QApplication, QTextEdit

import photo_group
import photo_layout
from markdown_writer import _image_as_read, document_to_markdown
from tests.photo_corners import SHAPES, corner_colours, image_of, shows_whole

IMAGE = QTextDocument.ResourceType.ImageResource.value


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _sizes(name):
    return SHAPES.get(photo_group.without_wide(name))


def _read(markdown: str, *, sizes=_sizes, narrow: bool = False) -> QTextDocument:
    """Markdown as Qt reads it, with the groups folded."""
    doc = QTextDocument()
    doc.setMarkdown(markdown)
    photo_group.fold(doc, sizes, narrow=narrow)
    return doc


def _markdown(*names, captions=None) -> str:
    captions = captions or {}
    parts = []
    for name in names:
        parts.append(f"![{name}]({name})")
        if name in captions:
            parts.append(f"*{captions[name]}*")
    return "\n\n".join(parts) + "\n"


def _group_tables(doc):
    return [frame for frame in doc.rootFrame().childFrames() if photo_group.is_group_table(frame)]


def _shown(markdown: str, width: int, *, narrow: bool = False):
    """A view of ``markdown`` ``width`` wide, its pictures the fixtures."""
    view = QTextEdit()
    view.resize(width, 3000)
    doc = view.document()
    photo_group.install(doc, _sizes)
    doc.setMarkdown(markdown)
    photo_group.fold(doc, narrow=narrow)
    for name in SHAPES:
        image = image_of(name)
        doc.addResource(IMAGE, QUrl(name), image)
        doc.addResource(IMAGE, QUrl(name + "#wide"), image)
    view.show()
    QApplication.processEvents()
    view.verticalScrollBar().setValue(0)
    QApplication.processEvents()
    return view


# --------------------------------------------------------------------------- #
# #wide and stand-ins                                                          #
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


def test_a_stand_in_has_no_size(app):
    stand_in = photo_group.mark_placeholder(QImage(160, 100, QImage.Format.Format_RGB32))
    assert photo_group.picture_size(stand_in) is None
    assert photo_group.picture_size(QImage(160, 100, QImage.Format.Format_RGB32)) == (160, 100)
    assert photo_group.picture_size(None) is None
    assert photo_group.picture_size(QImage()) is None


@pytest.mark.parametrize("size", [(0, 0), (1, 100000), (100000, 1), (-5, 10), (1, 40)])
def test_hostile_sizes_count_as_unknown(size):
    """A size the engine does not take is the provisional 3:2."""
    found = photo_group.table_sizes({"a.png": size})("a.png")
    assert found is None
    entry = photo_group.item(found, "", False)
    assert photo_layout.ratio_of(entry) == 1.5


# --------------------------------------------------------------------------- #
# Finding groups as STANDUP does                                               #
# --------------------------------------------------------------------------- #

def _found(markdown: str):
    doc = QTextDocument()
    doc.setMarkdown(markdown)
    return [[(unit.name, unit.caption_text) for unit in group.units]
            for group in photo_group.find_groups(doc)]


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


# --------------------------------------------------------------------------- #
# Fold and unfold                                                              #
# --------------------------------------------------------------------------- #

def test_fold_builds_one_table_a_row_with_the_engines_rows(app):
    names = ("portrait.png", "landscape.png", "square.png", "panorama.png")
    doc = _read(_markdown(*names))
    items = [photo_group.item(SHAPES[n], "", False) for n in names]
    rows = photo_layout.layout(items)
    tables = _group_tables(doc)
    assert len(tables) == len(rows)
    for table, row in zip(tables, rows):
        # A spacer each side, then a cell for each of the row's cells.
        assert table.columns() == len(row["cells"]) + 2
        assert table.format().property(photo_group.ARRANGEMENT) == photo_group.MOSAIC
    stacks = [cell for row in rows for cell in row["cells"] if not isinstance(cell, int)]
    nested = [frame for table in tables for frame in table.childFrames()
              if isinstance(frame, QTextTable) and frame.format().boolProperty(photo_group.STACK)]
    assert len(nested) == len(stacks)


def test_every_picture_has_its_own_number(app):
    doc = _read(_markdown("photo.png", "photo.png", "square.png"))
    numbers = [unit.number for group in photo_group.groups_in(doc) for unit in group.units]
    assert len(numbers) == 3 and len(set(numbers)) == 3


@pytest.mark.parametrize("markdown", [
    _markdown("photo.png", "square.png"),
    "Intro\n\n![a](photo.png)\n*Under*\n\n![b](square.png)\n\n*Own paragraph*\n\nEnd\n",
    "![a](photo.png) ![b](square.png)\n![c](portrait.png)\n\n![d](plan.png#wide)\n",
    "# T\n\n![a](photo.png)\n\n![b](square.png)\n\nText\n\n![c](portrait.png)\n\n![d](panorama.png)\n",
])
def test_unfold_gives_back_the_paragraphs_as_they_were(app, markdown):
    plain = QTextDocument()
    plain.setMarkdown(markdown)
    want = document_to_markdown(plain, _image_as_read)
    doc = _read(markdown)
    assert photo_group.has_groups(doc)
    assert photo_group.unfold(doc) >= 1
    assert not photo_group.has_groups(doc)
    assert document_to_markdown(doc, _image_as_read) == want
    assert doc.toPlainText() == plain.toPlainText()


def test_unfolded_leaves_the_document_as_it_is(app):
    doc = _read(_markdown("photo.png", "square.png"))
    copy = photo_group.unfolded(doc)
    assert copy is not doc and photo_group.has_groups(doc) and not photo_group.has_groups(copy)
    plain = QTextDocument()
    assert photo_group.unfolded(plain) is plain


def test_refold_arranges_a_document_again(app):
    doc = _read(_markdown("landscape.png", "landscape.png", "landscape.png"))
    assert photo_group.arrangement_of(doc) == photo_group.MOSAIC
    photo_group.refold(doc, _sizes, narrow=True)
    assert photo_group.arrangement_of(doc) == photo_group.NARROW


# --------------------------------------------------------------------------- #
# Geometry                                                                     #
# --------------------------------------------------------------------------- #

def _vectors():
    import json
    from pathlib import Path
    path = Path(__file__).resolve().parent / "fixtures" / "photo_layout_vectors.json"
    return json.loads(path.read_text(encoding="utf-8"))["cases"]


def test_the_mosaic_rows_have_the_engines_geometry():
    """Without captions, the CSS formulas the handler uses give the
    engine's own row geometry (rowGeometry), at the reference width."""
    regular = photo_layout.CONSTANTS["regular"]
    metrics = photo_group.Metrics(regular["width"], regular["gap"], regular["cap"],
                                  photo_layout.CONSTANTS["wideCap"]["px"], 0.0, 136)
    checked = 0
    for case in _vectors():
        items = case["items"]
        if any(item.get("caption") for item in items):
            continue
        for row, want in zip(photo_layout.layout(items), case["geometry"]["rows"]):
            cells = [[c] if isinstance(c, int) else list(c) for c in row["cells"]]
            places, width = photo_group.mosaic_places(cells, items, metrics)
            tiles = [tile for cell in places for tile in cell]
            if row["name"] == "wide":
                assert tiles[0].picture_height == pytest.approx(want["height"], abs=0.2)
                continue
            assert width == pytest.approx(want["width"], abs=0.2)
            for got, tile in zip(tiles, want["tiles"]):
                assert got.picture_width == pytest.approx(tile[3], abs=0.2)
                assert got.picture_height == pytest.approx(tile[4], abs=0.2)
            checked += 1
    assert checked > 100


def test_a_row_is_never_taller_than_its_cap():
    """A picture is at most the cap tall; in a row with a stack whose upper
    pictures have captions, a cell's pictures and those captions together
    at most the cap and that stack's caption lines (the readers' --rc and
    --c: the caption under a cell's last picture is below the row), as in
    STANDUP."""
    # The engine writes its numbers to four places (fixed4), as the CSS
    # reads them: a tenth of a pixel is that rounding.
    line, gap, rounding = 20.0, 12, 0.1
    metrics = photo_group.Metrics(2000, gap, 560, 900, line, 136)
    for case in _vectors():
        items = case["items"]
        for row in photo_layout.layout(items):
            cells = [[c] if isinstance(c, int) else list(c) for c in row["cells"]]
            places, width = photo_group.mosaic_places(cells, items, metrics)
            cap = 900 if row["name"] == "wide" else 560
            captions = [sum(1 for i in cell[:-1] if items[i].get("caption")) for cell in cells]
            for cell, places_of_cell, own in zip(cells, places, captions):
                tall = sum(place.picture_height for place in places_of_cell)
                assert tall + own * line + (len(cell) - 1) * gap <= cap + max(captions) * line + rounding
                if not any(captions):
                    assert tall + (len(cell) - 1) * gap <= cap + rounding
            assert width <= 2000 + rounding


def test_narrow_rows_unroll_a_strip_one_picture_a_line():
    """Below 600 pixels a strip is unrolled: each picture on a line of its
    own; a run that still reads side by side is one row (SPEC 18, 4)."""
    shapes = [SHAPES["landscape.png"], SHAPES["photo.png"], SHAPES["photo.png"],
              SHAPES["landscape.png"], SHAPES["portrait.png"], SHAPES["portrait.png"]]
    items = [photo_group.item(size, "", False) for size in shapes]
    segments = photo_layout.phone_segments(photo_layout.layout(items), items)
    assert {segment["mode"] for segment in segments} >= {"strip", "row"}
    rows = photo_group.narrow_rows(items)
    expected = []
    for segment in segments:
        if segment["mode"] == "row":
            expected.append([[i] for i in segment["figures"]])
        else:
            expected.extend([[i]] for i in segment["figures"])
    assert [row.cells for row in rows] == expected


def test_a_narrow_picture_alone_is_at_most_as_wide_as_the_column_and_the_cap():
    metrics = photo_group.Metrics(390, 8, 520, 900, 0.0, 136)
    tall = photo_group.item(SHAPES["portrait.png"], "", False)
    places, width = photo_group.narrow_places([[0]], "single", [tall], metrics)
    place = places[0][0]
    assert place.picture_height == pytest.approx(520)
    assert place.picture_width == pytest.approx(520 * 809 / 1800)
    captioned = photo_group.item(SHAPES["portrait.png"], "A caption", False)
    places, _ = photo_group.narrow_places([[0]], "single", [captioned], metrics)
    # The figure is at least 8em wide for its caption, the picture centred.
    assert places[0][0].width == pytest.approx(max(520 * 809 / 1800, 136))
    wide = photo_group.item(SHAPES["plan.png"], "", True)
    places, width = photo_group.narrow_places([[0]], "single", [wide], metrics)
    assert places[0][0].width == 390 and places[0][0].picture_width == 390


# --------------------------------------------------------------------------- #
# Shown: every picture whole, at every width                                   #
# --------------------------------------------------------------------------- #

ALL = ("portrait.png", "landscape.png", "square.png", "panorama.png", "photo.png",
       "plan.png#wide", "landscape.png")


@pytest.mark.parametrize("width", [1200, 900, 640, 420])
def test_every_picture_shows_whole_at_every_width(app, width):
    view = _shown(_markdown(*ALL, captions={"square.png": "A caption under the square"}), width)
    shot = view.viewport().grab().toImage()
    boxes = photo_group.picture_boxes(view.document())
    assert [name for _p, name, _b in boxes] == list(ALL)
    room = view.viewport().width()
    for _position, name, box in boxes:
        assert shows_whole(shot, box), (name, box, [c.name() for c in corner_colours(shot, box)])
        w, h = SHAPES[photo_group.without_wide(name)]
        assert box.width() / box.height() == pytest.approx(w / h, rel=0.01)
        assert box.right() <= room
    view.close()


def test_a_cropped_picture_fails_the_check(app):
    """The negative control: what object-fit: cover would show."""
    view = _shown(_markdown("photo.png", "square.png"), 900)
    doc = view.document()
    whole = image_of("square.png")
    cropped = whole.copy(whole.width() // 8, whole.height() // 8, whole.width() * 3 // 4,
                         whole.height() * 3 // 4)
    doc.addResource(IMAGE, QUrl("square.png"), cropped)
    doc.markContentsDirty(0, doc.characterCount())
    QApplication.processEvents()
    view.viewport().update()
    QApplication.processEvents()
    shot = view.viewport().grab().toImage()
    boxes = {name: box for _p, name, box in photo_group.picture_boxes(doc)}
    assert shows_whole(shot, boxes["photo.png"])
    assert not shows_whole(shot, boxes["square.png"])
    view.close()


def test_the_layout_follows_the_width_without_a_change_to_the_document(app):
    view = _shown(_markdown("photo.png", "square.png", "landscape.png"), 1000)
    doc = view.document()
    revision = doc.revision()
    before = [box.width() for _p, _n, box in photo_group.picture_boxes(doc)]
    view.resize(500, 3000)
    QApplication.processEvents()
    after = [box.width() for _p, _n, box in photo_group.picture_boxes(doc)]
    assert all(b < a for a, b in zip(before, after))
    assert doc.revision() == revision
    view.close()


def test_rows_fill_the_width_and_stay_below_the_cap(app):
    view = _shown(_markdown("photo.png", "photo.png", "square.png", "landscape.png"), 1100)
    room = view.viewport().width()
    for _p, _n, box in photo_group.picture_boxes(view.document()):
        assert box.height() <= 560 + 0.5
    rows = {}
    for _p, _n, box in photo_group.picture_boxes(view.document()):
        rows.setdefault(round(box.top()), []).append(box)
    for boxes in rows.values():
        if len(boxes) > 1:
            left = min(b.left() for b in boxes)
            right = max(b.right() for b in boxes)
            assert right - left <= room
    view.close()


def test_a_row_takes_at_most_its_share_of_the_screens_height(app):
    """As on a reader's screen: min(560px, 72svh), #wide min(900px, 85svh)."""
    view = _shown(_markdown("portrait.png", "portrait.png", "plan.png#wide"), 1400)
    doc = view.document()
    tall = {name: box.height() for _p, name, box in photo_group.picture_boxes(doc)}
    # A whole number of pixels wide: a portrait is up to 1/ratio pixels lower.
    assert tall["portrait.png"] == pytest.approx(560, abs=1800 / 809)
    photo_group.set_screen_height(doc, 500)
    QApplication.processEvents()
    tall = {name: box.height() for _p, name, box in photo_group.picture_boxes(doc)}
    assert tall["portrait.png"] <= 0.72 * 500 + 0.5
    assert tall["plan.png#wide"] <= 0.85 * 500 + 0.5
    view.close()


def test_below_600_pixels_a_read_only_view_unrolls_a_strip(app):
    strip = ("landscape.png", "photo.png", "photo.png", "landscape.png")
    view = _shown(_markdown(*strip), 400, narrow=True)
    boxes = [box for _p, _n, box in photo_group.picture_boxes(view.document())]
    tops = [round(box.top()) for box in boxes]
    assert len(set(tops)) == len(boxes)          # one picture a line
    shot = view.viewport().grab().toImage()
    for box in boxes:
        assert shows_whole(shot, box)
        assert box.width() <= view.viewport().width()
    view.close()


# --------------------------------------------------------------------------- #
# Red team: big groups, many groups                                            #
# --------------------------------------------------------------------------- #

def _cpu(work) -> float:
    start = time.process_time()
    work()
    return time.process_time() - start


def test_a_group_of_500_pictures_is_not_searched(app):
    """Past the engine's limit every picture gets a row of its own."""
    names = [list(SHAPES)[n % 5] for n in range(500)]
    doc = _read(_markdown(*names))
    assert len(_group_tables(doc)) == 500
    assert document_to_markdown(photo_group.unfolded(doc), _image_as_read).count("![") == 500


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


def test_the_engine_lays_a_group_of_twelve_out_in_under_2_ms_of_cpu():
    items = [photo_group.item(list(SHAPES.values())[n % 6], "", False) for n in range(12)]
    rounds = 50
    spent = _cpu(lambda: [photo_layout.layout(items) for _ in range(rounds)])
    assert spent / rounds < 0.002
