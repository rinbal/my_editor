# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""photo_group_geometry: where each picture of a group stands at a width.

Numbers only: the readers' CSS formulas over the engine's arrangement
give the engine's own geometry (the shared vectors), a row is never
taller than its cap, a narrow column shows runs with strips unrolled and
never a picture below 5/8 of its size alone, and the boxes the pictures'
characters take add up to the rows.
"""

from __future__ import annotations

import pytest

import photo_group_geometry as geometry
import photo_layout
from tests.photo_corners import SHAPES


@pytest.mark.parametrize("size", [(0, 0), (1, 100000), (100000, 1), (-5, 10), (1, 40)])
def test_hostile_sizes_count_as_unknown(size):
    """A size the engine does not take is the provisional 3:2."""
    entry = geometry.item(photo_layout.picture_size(*size), "", False)
    assert photo_layout.ratio_of(entry) == 1.5


def _vectors():
    import json
    from pathlib import Path
    path = Path(__file__).resolve().parent / "fixtures" / "photo_layout_vectors.json"
    return json.loads(path.read_text(encoding="utf-8"))["cases"]


def test_the_mosaic_rows_have_the_engines_geometry():
    """Without captions, the CSS formulas the handler uses give the
    engine's own row geometry (rowGeometry), at the reference width."""
    regular = photo_layout.CONSTANTS["regular"]
    metrics = geometry.Metrics(regular["width"], regular["gap"], regular["cap"],
                                  photo_layout.CONSTANTS["wideCap"]["px"], 0.0, 136)
    checked = 0
    for case in _vectors():
        items = case["items"]
        if any(item.get("caption") for item in items):
            continue
        for row, want in zip(photo_layout.layout(items), case["geometry"]["rows"]):
            cells = [[c] if isinstance(c, int) else list(c) for c in row["cells"]]
            places, width = geometry.mosaic_places(cells, items, metrics)
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
    metrics = geometry.Metrics(2000, gap, 560, 900, line, 136)
    for case in _vectors():
        items = case["items"]
        for row in photo_layout.layout(items):
            cells = [[c] if isinstance(c, int) else list(c) for c in row["cells"]]
            places, width = geometry.mosaic_places(cells, items, metrics)
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
    items = [geometry.item(size, "", False) for size in shapes]
    segments = photo_layout.phone_segments(photo_layout.layout(items), items)
    assert {segment["mode"] for segment in segments} >= {"strip", "row"}
    expected = []
    for segment in segments:
        if segment["mode"] == "row":
            expected.append([[i] for i in segment["figures"]])
        else:
            expected.extend([[i]] for i in segment["figures"])
    assert [cells for cells, _mode in geometry.narrow_rows(items)] == expected


def test_a_narrow_picture_alone_is_at_most_as_wide_as_the_column_and_the_cap():
    metrics = geometry.Metrics(390, 8, 520, 900, 0.0, 136)
    tall = geometry.item(SHAPES["portrait.png"], "", False)
    places, width = geometry.narrow_places([[0]], "single", [tall], metrics)
    place = places[0][0]
    assert place.picture_height == pytest.approx(520)
    assert place.picture_width == pytest.approx(520 * 809 / 1800)
    captioned = geometry.item(SHAPES["portrait.png"], "A caption", False)
    places, _ = geometry.narrow_places([[0]], "single", [captioned], metrics)
    # The figure is at least 8em wide for its caption, the picture centred.
    assert places[0][0].width == pytest.approx(max(520 * 809 / 1800, 136))
    wide = geometry.item(SHAPES["plan.png"], "", True)
    places, width = geometry.narrow_places([[0]], "single", [wide], metrics)
    assert places[0][0].width == 390 and places[0][0].picture_width == 390


def _no_captions(_index, _width):
    return 0.0


GROUPS = {
    "pair": ["photo.png", "square.png"],
    "lead and stack": ["portrait.png", "landscape.png", "landscape.png"],
    "grid": ["photo.png", "photo.png", "photo.png", "photo.png"],
    "panorama": ["photo.png", "panorama.png", "square.png"],
    "five": ["photo.png", "square.png", "portrait.png", "landscape.png", "photo.png"],
    "wide": ["photo.png", "plan.png#wide", "square.png"],
    "eight": ["photo.png", "square.png", "portrait.png", "landscape.png"] * 2,
}

# The widths a text can have: the desktop, the first narrow width, Paper
# Mode (533 wide, 505 for its text), Paper Mode, the Write view and the
# split view at the window's least width, each with the room of its text.
WIDTHS = [1100, 700, 600, 599, 533, 505, 367, 339, 299, 271, 184, 156]


def _items(names):
    return [geometry.item(SHAPES[name.split("#")[0]], "", name.endswith("#wide"))
            for name in names]


@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("names", GROUPS.values(), ids=GROUPS.keys())
def test_no_picture_overlaps_another_or_leaves_the_column(names, width):
    items = _items(names)
    metrics = geometry.metrics_at(width, 1.0, None, 20.0)
    assert metrics.narrow == (width < 600)
    rows = geometry.group_rows(items, metrics, _no_captions)
    assert sorted(tile.index for row in rows for tile in row.tiles) == list(range(len(names)))
    for row in rows:
        for tile in row.tiles:
            assert tile.x >= -0.01 and tile.x + tile.width <= metrics.width + 0.01
            ratio = photo_layout.ratio_of(items[tile.index])
            assert tile.width / tile.height == pytest.approx(ratio, rel=0.001)
        for one in row.tiles:
            for other in row.tiles:
                if one.index < other.index:
                    apart = (one.x + one.width <= other.x + 0.01
                             or other.x + other.width <= one.x + 0.01
                             or one.y + one.height <= other.y + 0.01
                             or other.y + other.height <= one.y + 0.01)
                    assert apart, (one, other)


@pytest.mark.parametrize("width", [width for width in WIDTHS if width < 600])
@pytest.mark.parametrize("names", GROUPS.values(), ids=GROUPS.keys())
def test_below_600_every_picture_shows_at_least_five_eighths_of_its_size_alone(names, width):
    """D7: nothing tiny. A picture's size alone is as wide as the column,
    never taller than the cap (SPEC 5.1)."""
    items = _items(names)
    metrics = geometry.metrics_at(width, 1.0, None, 20.0)
    for row in geometry.group_rows(items, metrics, _no_captions):
        for tile in row.tiles:
            ratio = photo_layout.ratio_of(items[tile.index])
            cap = metrics.wide_cap if items[tile.index]["wide"] else metrics.cap
            alone = min(metrics.width, cap * ratio)
            assert tile.width >= 0.625 * alone - 0.01, (tile, alone)


def test_a_row_that_no_longer_reads_side_by_side_is_unrolled():
    """Two portraits stand side by side in a column of 350 pixels (the
    engine's phone); in one of 150 each would show at less than 5/8 of its
    size alone, so they stand one under the other. The engine asks for
    its phone only: the column's own width is asked here."""
    items = _items(["portrait.png", "portrait.png"])
    segments = photo_layout.phone_segments(photo_layout.layout(items), items)
    assert [segment["mode"] for segment in segments] == ["row"]
    phone = geometry.metrics_at(350, 1.0, None, 20.0)
    assert [mode for _cells, mode in geometry.narrow_rows(items, phone)] == ["row"]
    narrow = geometry.metrics_at(150, 1.0, None, 20.0)
    assert [mode for _cells, mode in geometry.narrow_rows(items, narrow)] == ["strip", "strip"]


@pytest.mark.parametrize("width", [1100, 600, 505, 271])
@pytest.mark.parametrize("names", GROUPS.values(), ids=GROUPS.keys())
def test_the_boxes_of_a_row_fill_its_line_and_a_stack_takes_one_box(names, width):
    """What makes Qt's line breaking give the rows: the boxes of a row add
    up to the text's width (less the slack), so the next box starts a
    line; only the first picture of a stack has a box."""
    items = _items(names)
    metrics = geometry.metrics_at(width, 1.0, None, 20.0)
    rows = geometry.group_rows(items, metrics, _no_captions)
    boxes = geometry.object_boxes(rows, len(names), metrics)
    for number, row in enumerate(rows):
        heads = [cell[0] for cell in row.cells]
        assert sum(boxes[i].width for i in heads) == pytest.approx(metrics.width, abs=0.001)
        gap = metrics.gap if number < len(rows) - 1 else 0.0
        assert {boxes[i].height for i in heads} == {row.height + gap}
        for cell in row.cells:
            assert [tile.index for tile, _left in boxes[cell[0]].tiles] == list(cell)
            for member in cell[1:]:
                assert boxes[member].width == 0 and boxes[member].height == 0


def test_a_caption_makes_its_row_taller_and_hangs_under_its_picture():
    items = _items(["photo.png", "square.png"])
    metrics = geometry.metrics_at(900, 1.0, None, 20.0)
    plain = geometry.group_rows(items, metrics, _no_captions)[0]
    rows = geometry.group_rows(items, metrics, lambda index, _w: 30.0 if index == 1 else 0.0)
    assert rows[0].height == pytest.approx(plain.height + 30.0)
    assert [tile.caption_height for tile in rows[0].tiles] == [0.0, 30.0]
    assert [(tile.width, tile.height) for tile in rows[0].tiles] == [
        (tile.width, tile.height) for tile in plain.tiles]
