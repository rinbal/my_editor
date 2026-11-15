# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Where each picture of a photo group stands at a width: numbers only.

STANDUP's engine (photo_layout.py, vendored unchanged) arranges a group:
which pictures share a row, which stand in a stack, which get a row of
their own. This module turns an arrangement into places at one width, by
the readers' CSS (SPEC 3.4 and 5 of the photo grid design), so a group in
this app is laid out as STANDUP's pages lay it out:

- **wide**, a text 600 pixels wide or wider: the mosaic, rows of pictures
  at one height that fill the width (mosaic_places);
- **narrow**, below it: runs of pictures of a similar shape. A run that
  still shows every picture at 5/8 of its size alone stands side by side,
  every other picture on a line of its own (a reader's swipe strip,
  unrolled: nothing here swipes) (narrow_rows, narrow_places).

group_rows gives a whole group at a width, and object_boxes the box each
picture's character takes in its paragraph so that Qt's own line breaking
makes exactly those rows (photo_group_view). Nothing here knows a
document or draws: the sizes come in as the engine's items, a caption's
height as a function.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import photo_layout

# Below this width (CSS pixels) a group shows as runs (SPEC 2).
NARROW_BELOW = 600

# The share of the screen's height a mosaic row may take: STANDUP's
# --pg-cap, min(560px, 72svh) (backend/htmlgen/article.css). The wide and
# narrow caps are the engine's own (wideCap, phone capVh).
REGULAR_CAP_VH = 0.72

# How many of the engine's answers are kept (arrangement, _row_numbers).
_ARRANGEMENTS_KEPT = 4096

# How much of the text's width a row leaves free, in pixels: Qt adds the
# widths up in 64ths of a pixel, and a row a hair wider than its line would
# break in two.
ROW_SLACK = 0.5


def item(size: Optional[Tuple[int, int]], caption: str, wide: bool) -> dict:
    """One picture as the engine takes it: its pixel size (0 when not
    known), its caption's length in code points, and #wide."""
    w, h = size if size else (0, 0)
    return {"w": w, "h": h, "caption": len(caption), "wide": bool(wide)}


@dataclass(frozen=True)
class Metrics:
    """The sizes a group is laid out with, in the layout's pixels."""

    width: float            # the room for a row
    gap: float
    cap: float              # tallest row (a #wide row: wide_cap)
    wide_cap: float
    line: float             # one caption line under a picture, with its gap
    caption_min: float      # a narrow captioned picture's least width
    narrow: bool = False    # runs instead of the mosaic


def metrics_at(room: float, scale: float, screen_height: Optional[float],
               line: float) -> Metrics:
    """The sizes for a text ``room`` pixels wide on a device of ``scale``
    pixels per CSS pixel, read on a screen (or page) ``screen_height``
    high (None: no share of a height limits a row); ``line`` is a caption
    line with its gap."""
    constants = photo_layout.CONSTANTS

    def cap(px: float, share: float) -> float:
        if screen_height is None or screen_height <= 0:
            return px * scale
        return min(px * scale, share * screen_height)

    wide_cap = cap(constants["wideCap"]["px"], constants["wideCap"]["vh"])
    width = max(1.0, room - ROW_SLACK)
    phone = constants["phone"]
    if room < NARROW_BELOW * scale:
        return Metrics(width, phone["gap"] * scale, cap(phone["capPx"], phone["capVh"]),
                       wide_cap, line, phone["captionMin"] * scale, True)
    regular = constants["regular"]
    return Metrics(width, regular["gap"] * scale, cap(regular["cap"], REGULAR_CAP_VH),
                   wide_cap, line, phone["captionMin"] * scale, False)


@dataclass(frozen=True)
class Place:
    """Where a picture of a row goes: its figure's box (width, height), and
    the picture's own box inside it (offset, width, height)."""

    width: float
    height: float
    x: float = 0.0
    picture_width: float = 0.0
    picture_height: float = 0.0


def _number(properties: dict, key: str) -> float:
    return float(properties.get(key, "0"))


# The engine's numbers for a row (--rs, --rg, --rl, --rc and each cell's
# --r, --k, --c), as the CSS reads them from the markup: rounded to four
# places. By the row's shapes, so a row laid out at another width, or the
# same row in another group, is not asked for again.
_numbers_of_rows: Dict[tuple, tuple] = {}


def _row_numbers(cells: List[List[int]], items: List[dict]) -> tuple:
    key = tuple(tuple((items[i]["w"], items[i]["h"], bool(items[i].get("caption")))
                      for i in cell) for cell in cells)
    found = _numbers_of_rows.get(key)
    if found is None:
        engine_cells = [cell[0] if len(cell) == 1 else list(cell) for cell in cells]
        props = photo_layout.cell_properties({"cells": engine_cells}, 0, items)
        found = (tuple(_number(props[0], name) for name in ("--rs", "--rg", "--rl", "--rc")),
                 tuple(tuple(_number(prop, name) for name in ("--r", "--k", "--c"))
                       for prop in props))
        if len(_numbers_of_rows) >= _ARRANGEMENTS_KEPT:
            _numbers_of_rows.clear()
        _numbers_of_rows[key] = found
    return found


def mosaic_places(cells: List[List[int]], items: List[dict], metrics: Metrics) -> Tuple[
        List[List[Place]], float]:
    """The places of one mosaic row's pictures (cell by cell, a stack's
    from the top) and the row's width, by the readers' CSS (SPEC 3.4): the
    row is ``hr = min(cap + rc*line, (100% - rg*gap + rl*line)/rs)`` tall
    and a cell ``r*(hr - k*gap - c*line)`` wide, from the engine's cell
    properties. A #wide picture's cell is the whole width, the picture in
    its middle at most wide_cap tall."""
    W, g, line = metrics.width, metrics.gap, metrics.line
    places: List[List[Place]] = []
    if len(cells) == 1 and len(cells[0]) == 1 and items[cells[0][0]].get("wide") is True:
        ratio = photo_layout.ratio_of(items[cells[0][0]])
        pw = min(W, metrics.wide_cap * ratio)
        return [[Place(W, pw / ratio, (W - pw) / 2, pw, pw / ratio)]], W
    (rs, rg, rl, rc), of_cells = _row_numbers(cells, items)
    hr = min(metrics.cap + rc * line, (W - rg * g + rl * line) / rs) if rs > 0 else 0.0
    for cell, (r, k, c) in zip(cells, of_cells):
        cw = max(1.0, r * (hr - k * g - c * line))
        out = []
        for index in cell:
            h = cw / photo_layout.ratio_of(items[index])
            out.append(Place(cw, h, 0.0, cw, h))
        places.append(out)
    return places, rs * hr + rg * g - rl * line


def narrow_places(row: List[List[int]], mode: str, items: List[dict], metrics: Metrics) -> Tuple[
        List[List[Place]], float]:
    """The places of one narrow row (SPEC 5, the narrow block of the CSS):
    a run that reads as a row at one height, side by side; a picture alone
    as wide as the column and never taller than the cap (a #wide one: the
    wide cap), its figure at least caption_min wide when it has a caption."""
    W, g = metrics.width, metrics.gap
    indices = [cell[0] for cell in row]
    if mode == "row" and len(indices) > 1:
        H = _row_height(indices, items, metrics)
        places = [[Place(photo_layout.ratio_of(items[i]) * H, H, 0.0,
                         photo_layout.ratio_of(items[i]) * H, H)] for i in indices]
        return places, sum(p[0].width for p in places) + (len(indices) - 1) * g
    index = indices[0]
    ratio = photo_layout.ratio_of(items[index])
    wide = items[index].get("wide") is True
    cap = metrics.wide_cap if wide else metrics.cap
    pw = min(W, cap * ratio)
    figure = W if wide else (max(pw, min(metrics.caption_min, W)) if items[index].get("caption")
                             else pw)
    return [[Place(figure, pw / ratio, (figure - pw) / 2, pw, pw / ratio)]], figure


def _row_height(indices: Sequence[int], items: List[dict], metrics: Metrics) -> float:
    sum_r = 0.0
    for index in indices:
        sum_r += photo_layout.ratio_of(items[index])
    return min(metrics.cap, (metrics.width - (len(indices) - 1) * metrics.gap) / sum_r)


def _reads_as_row(indices: Sequence[int], items: List[dict], metrics: Metrics) -> bool:
    """Whether a run still reads side by side in this column: every picture
    at least 5/8 of its size alone, a captioned one wide enough for its
    caption. The engine asks this for its reference phone (350 pixels); a
    column of another width is asked again here, by the same rule (D7)."""
    spread = photo_layout.CONSTANTS["phone"]["spread"]
    H = _row_height(indices, items, metrics)
    for index in indices:
        ratio = photo_layout.ratio_of(items[index])
        if ratio * H * spread < min(metrics.width, metrics.cap * ratio):
            return False
        if items[index].get("caption") and ratio * H < metrics.caption_min:
            return False
    return True


# The engine's answers by the items asked about: it is deterministic, and a
# document lays its groups out again at every width.
_arrangements: Dict[tuple, Tuple[list, list]] = {}


def arrangement(items: List[dict]) -> Tuple[list, list]:
    """The engine's arrangement of ``items``, which no width changes: the
    rows of the mosaic and the runs of a narrow column."""
    key = tuple((i["w"], i["h"], i["caption"], i["wide"]) for i in items)
    found = _arrangements.get(key)
    if found is None:
        rows = photo_layout.layout(items)
        found = (rows, photo_layout.phone_segments(rows, items))
        if len(_arrangements) >= _ARRANGEMENTS_KEPT:
            _arrangements.clear()
        _arrangements[key] = found
    return found


def mosaic_rows(items: List[dict]) -> List[List[List[int]]]:
    """The rows of the mosaic: each a list of cells, a cell the indices of
    its pictures (two or more: a stack, from the top)."""
    return [[[cell] if isinstance(cell, int) else list(cell) for cell in row["cells"]]
            for row in arrangement(items)[0]]


def narrow_rows(items: List[dict], metrics: Optional[Metrics] = None) -> List[
        Tuple[List[List[int]], str]]:
    """The rows of a narrow column, strips unrolled, each with its mode: a
    run that reads as a row is one row ("row"); every other picture stands
    on a line of its own ("single" or "strip"). With ``metrics``, a row is
    one only while it reads as a row at that width too."""
    rows: List[Tuple[List[List[int]], str]] = []
    for segment in arrangement(items)[1]:
        figures = segment["figures"]
        if segment["mode"] == "row" and (metrics is None
                                         or _reads_as_row(figures, items, metrics)):
            rows.append(([[i] for i in figures], "row"))
        else:
            mode = "strip" if segment["mode"] == "row" else segment["mode"]
            rows.extend(([[i]], mode) for i in figures)
    return rows


@dataclass(frozen=True)
class Tile:
    """One picture in its row: the picture's box, its figure's (where its
    caption runs) and the room its caption takes under it, gap included."""

    index: int
    x: float
    y: float
    width: float
    height: float
    figure_x: float
    figure_width: float
    caption_height: float = 0.0


@dataclass(frozen=True)
class Row:
    """One row of a group at a width: its height (captions included), its
    cells as picture indices, and a tile for each picture."""

    height: float
    cells: Tuple[Tuple[int, ...], ...]
    tiles: Tuple[Tile, ...]


# The height a picture's caption takes in a figure of the given width, its
# gap included; 0 for a picture without one. Asked with (index, width).
CaptionHeight = Callable[[int, float], float]


def group_rows(items: List[dict], metrics: Metrics, caption_height: CaptionHeight) -> List[Row]:
    """A whole group at ``metrics``: its rows from the top, each centred in
    the width, every picture with its place in its row."""
    if metrics.narrow:
        found = [(cells, narrow_places(cells, mode, items, metrics))
                 for cells, mode in narrow_rows(items, metrics)]
    else:
        found = [(cells, mosaic_places(cells, items, metrics)) for cells in mosaic_rows(items)]
    return [_row(cells, places, width, metrics, caption_height)
            for cells, (places, width) in found]


def _row(cells: List[List[int]], places: List[List[Place]], width: float, metrics: Metrics,
         caption_height: CaptionHeight) -> Row:
    tiles: List[Tile] = []
    height = 0.0
    x = (metrics.width - width) / 2
    for cell, cell_places in zip(cells, places):
        y = 0.0
        for index, place in zip(cell, cell_places):
            caption = caption_height(index, place.width)
            tiles.append(Tile(index, x + place.x, y, place.picture_width, place.picture_height,
                              x, place.width, caption))
            y += place.picture_height + caption + metrics.gap
        height = max(height, y - metrics.gap)
        x += cell_places[0].width + metrics.gap
    return Row(height, tuple(tuple(cell) for cell in cells), tuple(tiles))


@dataclass(frozen=True)
class ObjectBox:
    """The box one picture's character takes in its group's paragraph, and
    the tiles drawn in it, each with its offset from the box's left edge."""

    width: float
    height: float
    tiles: Tuple[Tuple[Tile, float], ...] = ()


def object_boxes(rows: Sequence[Row], count: int, metrics: Metrics) -> List[ObjectBox]:
    """A box for each of a group's ``count`` pictures, in the author's
    order, such that a paragraph holding them one after another breaks
    into exactly ``rows``: the first picture of each cell takes the cell's
    room (from its left edge to the next cell's, the first from the line's
    start, the last to its end), so a row fills its line and the next
    picture starts a new one. A stack's first picture draws the whole
    stack; its other pictures take no room. Every row but the last is the
    gap taller than its pictures."""
    boxes = [ObjectBox(0.0, 0.0)] * count
    last = len(rows) - 1
    for number, row in enumerate(rows):
        by_index = {tile.index: tile for tile in row.tiles}
        height = row.height + (metrics.gap if number < last else 0.0)
        starts = [by_index[cell[0]].figure_x for cell in row.cells]
        for k, cell in enumerate(row.cells):
            left = 0.0 if k == 0 else starts[k]
            right = metrics.width if k == len(row.cells) - 1 else starts[k + 1]
            boxes[cell[0]] = ObjectBox(right - left, height,
                                       tuple((by_index[i], left) for i in cell))
    return boxes
