# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""photo_group_view: a group laid out and drawn by the document's layout.

Every picture shows whole (the coloured corners of the fixtures), where
the geometry puts it, at every width: the mosaic from 600 pixels, the
runs below it. The document itself never changes for it: no width, no
height of the screen and no picture arriving late leaves a step in Undo,
marks the document as changed or changes a character.
"""

from __future__ import annotations

import itertools

import pytest
from PySide6.QtCore import QPointF, QUrl
from PySide6.QtGui import QColor, QImage, QTextCursor, QTextDocument
from PySide6.QtWidgets import QApplication, QTextEdit

import photo_group
import photo_group_drawing
import photo_group_geometry as geometry
import photo_group_view
import photo_layout
from markdown_writer import _image_as_read, document_to_markdown, read_markdown
from tests.photo_corners import SHAPES, corner_colours, image_of, shows_whole

IMAGE = QTextDocument.ResourceType.ImageResource.value


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _sizes(name):
    return SHAPES.get(photo_group.without_wide(name))


def _markdown(*names, captions=None) -> str:
    captions = captions or {}
    parts = []
    for name in names:
        parts.append(f"![{name}]({name})")
        if name in captions:
            parts.append(f"*{captions[name]}*")
    return "\n\n".join(parts) + "\n"


def _shown(markdown: str, width: int, *, sizes=_sizes, pictures=SHAPES):
    """A view of ``markdown`` ``width`` wide, its pictures the fixtures."""
    view = QTextEdit()
    view.resize(width, 3000)
    doc = view.document()
    photo_group_view.install(doc, sizes)
    read_markdown(doc, markdown)
    for name in pictures:
        image = image_of(name)
        doc.addResource(IMAGE, QUrl(name), image)
        doc.addResource(IMAGE, QUrl(name + "#wide"), image)
    doc.clearUndoRedoStacks()
    doc.setModified(False)
    view.show()
    QApplication.processEvents()
    view.verticalScrollBar().setValue(0)
    QApplication.processEvents()
    return view


def _boxes(doc):
    return photo_group_view.picture_boxes(doc)


# --------------------------------------------------------------------------- #
# The sizes a surface has                                                      #
# --------------------------------------------------------------------------- #

def test_a_stand_in_has_no_size(app):
    stand_in = photo_group_drawing.mark_placeholder(QImage(160, 100, QImage.Format.Format_RGB32))
    assert photo_group_view.picture_size(stand_in) is None
    assert photo_group_view.picture_size(QImage(160, 100, QImage.Format.Format_RGB32)) == (160, 100)
    assert photo_group_view.picture_size(None) is None
    assert photo_group_view.picture_size(QImage()) is None


def test_a_hostile_size_at_hand_counts_as_unknown():
    assert photo_group_view.sizes_from({"a.png": (1, 100000)})("a.png") is None
    assert photo_group_view.sizes_from({"a.png": (1800, 1200)})("a.png") == (1800, 1200)


# --------------------------------------------------------------------------- #
# Shown: every picture whole, at every width                                   #
# --------------------------------------------------------------------------- #

ALL = ("portrait.png", "landscape.png", "square.png", "panorama.png", "photo.png",
       "plan.png#wide", "landscape.png")


def test_every_picture_shows_whole_where_the_geometry_puts_it(app):
    """The mosaic from 600 pixels, the runs below (Paper Mode's 533, the
    window's least width): drawn whole, in the author's order, within a
    tenth of a pixel of the engine's geometry for that width."""
    for width in (1200, 640, 533, 300):
        view = _shown(_markdown(*ALL, captions={"square.png": "A caption under the square"}), width)
        doc = view.document()
        shot = view.viewport().grab().toImage()
        boxes = _boxes(doc)
        assert [name for _p, name, _b in boxes] == list(ALL)
        room = view.viewport().width()
        for _position, name, box in boxes:
            assert shows_whole(shot, box), (name, box, [c.name() for c in corner_colours(shot, box)])
            w, h = SHAPES[photo_group.without_wide(name)]
            assert box.width() / box.height() == pytest.approx(w / h, rel=0.01)
            assert box.right() <= room
        # Against the numbers: each picture's size, and its place in its row.
        handler = photo_group_view._handler(doc)
        metrics = handler.metrics(doc)
        assert metrics.narrow == (handler.room(doc) < 600)
        units = photo_group.groups_in(doc)[0].units
        items = [geometry.item(_sizes(unit.name), unit.caption_text, unit.wide) for unit in units]
        rows = geometry.group_rows(items, metrics, lambda _i, _w: 0.0)
        left = doc.documentLayout().blockBoundingRect(doc.begin()).left()
        for row in rows:
            for tile in row.tiles:
                box = boxes[tile.index][2]
                assert box.width() == pytest.approx(tile.width, abs=0.1)
                assert box.height() == pytest.approx(tile.height, abs=0.1)
                assert box.left() - left == pytest.approx(tile.x, abs=0.1)
            tops = {round(boxes[cell[0]][2].top(), 1) for cell in row.cells}
            assert len(tops) == 1                       # the row's pictures start level
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
    boxes = {name: box for _p, name, box in _boxes(doc)}
    assert shows_whole(shot, boxes["photo.png"])
    assert not shows_whole(shot, boxes["square.png"])
    view.close()


def test_in_paper_mode_a_portrait_is_never_tiny(app):
    """Gate 3: at 533 pixels a portrait beside two landscapes showed at
    27 % of its size alone. Below 600 pixels a group is laid out as runs:
    every picture at least 5/8 of its size alone (D7)."""
    view = _shown(_markdown("portrait.png", "landscape.png", "landscape.png"), 533)
    doc = view.document()
    metrics = photo_group_view._handler(doc).metrics(doc)
    assert metrics.narrow
    for _p, name, box in _boxes(doc):
        w, h = SHAPES[name]
        alone = min(metrics.width, metrics.cap * w / h)
        assert box.width() >= 0.625 * alone - 0.1, (name, box.width(), alone)
    view.close()


def test_rows_fill_the_width_and_stay_below_the_cap(app):
    view = _shown(_markdown("photo.png", "photo.png", "square.png", "landscape.png"), 1100)
    doc = view.document()
    room = photo_group_view._handler(doc).room(doc)
    rows = {}
    for _p, _n, box in _boxes(doc):
        assert box.height() <= 560 + 0.5
        rows.setdefault(round(box.top()), []).append(box)
    assert len(rows) >= 2
    for boxes in rows.values():
        width = max(b.right() for b in boxes) - min(b.left() for b in boxes)
        assert width <= room
        if all(b.height() < 559 for b in boxes):
            assert width == pytest.approx(room, abs=1.0)        # not capped: the whole width
    view.close()


def test_a_row_takes_at_most_its_share_of_the_screens_height(app):
    """As on a reader's screen: min(560px, 72svh), #wide min(900px, 85svh)."""
    view = _shown(_markdown("portrait.png", "portrait.png", "plan.png#wide"), 1400)
    doc = view.document()
    tall = {name: box.height() for _p, name, box in _boxes(doc)}
    assert tall["portrait.png"] == pytest.approx(560, abs=0.5)
    photo_group_view.set_screen_height(doc, 500)
    QApplication.processEvents()
    tall = {name: box.height() for _p, name, box in _boxes(doc)}
    assert tall["portrait.png"] <= 0.72 * 500 + 0.5
    assert tall["plan.png#wide"] <= 0.85 * 500 + 0.5
    view.close()


def test_below_600_pixels_a_strip_is_unrolled_one_picture_a_line(app):
    strip = ("landscape.png", "photo.png", "photo.png", "landscape.png")
    view = _shown(_markdown(*strip), 400)
    boxes = [box for _p, _n, box in _boxes(view.document())]
    tops = [round(box.top()) for box in boxes]
    assert len(set(tops)) == len(boxes)
    shot = view.viewport().grab().toImage()
    for box in boxes:
        assert shows_whole(shot, box)
        assert box.width() <= view.viewport().width()
    view.close()


# --------------------------------------------------------------------------- #
# The document never changes for its layout                                    #
# --------------------------------------------------------------------------- #

def _state(doc):
    return (doc.toRawText(), doc.revision(), doc.isModified(), doc.isUndoAvailable(),
            doc.isRedoAvailable(), document_to_markdown(doc, _image_as_read))


def test_no_width_and_no_screen_height_changes_the_document(app):
    """Gate 2 and 3: the rows follow the window with nothing recorded: no
    step in Undo, not modified, the same revision, the same characters."""
    view = _shown("Text.\n\n" + _markdown("photo.png", "square.png", "landscape.png"), 1000)
    doc = view.document()
    # One real edit and its Undo, so both stacks hold something to lose.
    QTextCursor(doc).insertText("x")
    doc.undo()
    doc.setModified(False)
    before = _state(doc)
    wide = [box.width() for _p, _n, box in _boxes(doc)]
    for width in (640, 560, 533, 300, 1000, 500):
        view.resize(width, 3000)
        QApplication.processEvents()
        assert _state(doc) == before, width
    photo_group_view.set_screen_height(doc, 400)
    QApplication.processEvents()
    assert _state(doc) == before
    narrow = [box.width() for _p, _n, box in _boxes(doc)]
    assert all(abs(a - b) > 1 for a, b in zip(wide, narrow))    # laid out anew all the same
    view.close()


def test_a_picture_arriving_late_lays_out_its_group_once_and_changes_nothing(app, monkeypatch):
    """Gate 2: rows come from the sizes the view has. A web picture is a
    stand-in (3:2) until it arrives; then the groups that show it, and
    only they, are laid out again, once; the same size again, not at all."""
    known = dict(SHAPES)
    late = "https://example.org/late.png"
    view = _shown("![a](photo.png)\n\n![b](" + late + ")\n\nBetween.\n\n"
                  "![c](square.png)\n\n![d](landscape.png)\n", 1000, sizes=known.get)
    doc = view.document()
    QTextCursor(doc).insertText("x")
    doc.undo()
    doc.setModified(False)
    before = _state(doc)
    provisional = {name: box for _p, name, box in _boxes(doc)}
    assert provisional[late].width() / provisional[late].height() == pytest.approx(1.5, rel=0.01)

    laid_out = []
    lay_out = photo_group_drawing.Handler._lay_out
    monkeypatch.setattr(photo_group_drawing.Handler, "_lay_out",
                        lambda self, doc, block, start, units: (
                            laid_out.append(block.position()),
                            lay_out(self, doc, block, start, units))[1])
    known[late] = (2400, 1000)
    doc.addResource(IMAGE, QUrl(late), image_of("panorama.png"))
    assert photo_group_view.picture_arrived(doc, late) == 1
    QApplication.processEvents()
    first = photo_group.groups_in(doc)[0].start
    assert laid_out and set(laid_out) == {first}        # its group, not the other
    arrived = {name: box for _p, name, box in _boxes(doc)}
    assert arrived[late].width() / arrived[late].height() == pytest.approx(2.4, rel=0.01)
    assert arrived["square.png"].size() == provisional["square.png"].size()
    assert _state(doc) == before
    # The same size again: nothing to lay out.
    assert photo_group_view.picture_arrived(doc, late) == 0
    assert photo_group_view.picture_arrived(doc, "photo.png") == 0
    view.close()


def test_a_lone_picture_arriving_lays_out_its_own_paragraph_only(app):
    view = _shown("![a](photo.png)\n\nBetween.\n\n![c](square.png)\n\n![d](landscape.png)\n", 900)
    doc = view.document()
    before = _state(doc)
    assert photo_group_view.picture_arrived(doc, "photo.png") == 1
    assert photo_group_view.picture_arrived(doc, "nowhere.png") == 0
    assert _state(doc) == before
    view.close()


# --------------------------------------------------------------------------- #
# Captions and odd pictures                                                    #
# --------------------------------------------------------------------------- #

def _ink(shot, rect) -> int:
    """How many pixels in ``rect`` of ``shot`` stand out from the page. The
    page is read at the view's corner, inside the document's margin: white,
    or dark once a window has given the app a dark look (it follows the
    system's appearance, and the look stays after the window closes)."""
    ratio = shot.devicePixelRatio() or 1.0
    page = QColor(shot.pixel(1, 1)).lightness()
    count = 0
    for y in range(int(rect.top() * ratio), int(rect.bottom() * ratio)):
        for x in range(int(rect.left() * ratio), int(rect.right() * ratio)):
            if abs(QColor(shot.pixel(x, y)).lightness() - page) > 55:
                count += 1
    return count


def test_a_caption_is_drawn_under_its_picture_and_makes_room(app):
    plain = _shown(_markdown("photo.png", "square.png") + "\nAfter.\n", 900)
    view = _shown(_markdown("photo.png", "square.png", captions={"square.png": "Under the square"})
                  + "\nAfter.\n", 900)
    doc = view.document()
    boxes = {name: box for _p, name, box in _boxes(doc)}
    square = boxes["square.png"]
    after = doc.documentLayout().blockBoundingRect(doc.lastBlock())
    lower = doc.documentLayout().blockBoundingRect(plain.document().lastBlock())
    assert after.top() > lower.top() + 10            # the caption's line pushed the text down
    shot = view.viewport().grab().toImage()
    from PySide6.QtCore import QRectF
    under_square = QRectF(square.left(), square.bottom() + 2, square.width(), after.top() - square.bottom() - 4)
    under_photo = QRectF(boxes["photo.png"].left(), square.bottom() + 2,
                         boxes["photo.png"].width(), after.top() - square.bottom() - 4)
    assert _ink(shot, under_square) > 20
    assert _ink(shot, under_photo) == 0
    plain.close()
    view.close()


def test_a_long_caption_stays_under_its_picture(app):
    caption = "Unbreakable" * 40
    view = _shown(f"![a](photo.png)\n\n*{caption}*\n\n![b](square.png)\n\n![c](portrait.png)\n",
                  760)
    doc = view.document()
    boxes = [box for _p, _n, box in _boxes(doc)]
    assert len(boxes) == 3
    for one, other in itertools.combinations(boxes, 2):
        assert not one.intersects(other)
    width = view.viewport().width()
    assert all(box.right() <= width + 0.5 for box in boxes)
    shot = view.viewport().grab().toImage()
    assert all(shows_whole(shot, box) for box in boxes)
    # Nothing of it is drawn beside its picture's column: the pictures
    # next to it start level with it and are whole (checked above), and
    # the strip between the columns stays empty.
    first, second = boxes[0], min((b for b in boxes[1:] if b.left() > boxes[0].left()),
                                  key=lambda b: b.left())
    from PySide6.QtCore import QRectF
    gap = QRectF(first.right() + 1, first.top(), second.left() - first.right() - 2,
                 first.height() + 200)
    assert _ink(shot, gap) == 0
    view.close()


def test_a_picture_past_the_engines_limits_is_drawn_whole_not_stretched(app):
    shape = (3300, 100)
    """The engine lays a picture of a shape past its limits out at 3:2
    (photo_layout.picture_size); it is drawn inside at its own shape."""
    blue = QColor("#0090FF")
    odd = QImage(*shape, QImage.Format.Format_RGB32)
    odd.fill(blue)
    view = QTextEdit()
    view.resize(760, 3000)
    doc = view.document()
    photo_group_view.install(doc, photo_group_view.resource_sizes(doc))
    read_markdown(doc, "![a](odd.png)\n\n![b](photo.png)\n")
    doc.addResource(IMAGE, QUrl("odd.png"), odd)
    doc.addResource(IMAGE, QUrl("photo.png"), image_of("photo.png"))
    view.show()
    QApplication.processEvents()
    shot = view.viewport().grab().toImage()
    box = next(box for _p, name, box in _boxes(doc) if name == "odd.png")
    assert box.width() / box.height() == pytest.approx(1.5, rel=0.02)
    ratio = shot.devicePixelRatio() or 1.0
    found = [(x, y) for y in range(int(box.top()), int(box.bottom()) + 1)
             for x in range(int(box.left()), int(box.right()) + 1)
             if QColor(shot.pixel(int(x * ratio), int(y * ratio))) == blue]
    xs = [x for x, _y in found]
    ys = [y for _x, y in found]
    drawn = (max(xs) - min(xs) + 1) / (max(ys) - min(ys) + 1)
    assert drawn == pytest.approx(shape[0] / shape[1], rel=0.15)
    view.close()


def test_the_engine_lays_a_group_of_twelve_out_in_under_2_ms_of_cpu():
    import time
    items = [geometry.item(list(SHAPES.values())[n % 6], "", False) for n in range(12)]
    rounds = 50
    start = time.process_time()
    for _ in range(rounds):
        photo_layout.layout(items)
    assert (time.process_time() - start) / rounds < 0.002


# --------------------------------------------------------------------------- #
# Finding a picture                                                            #
# --------------------------------------------------------------------------- #

def test_the_picture_at_a_point_is_the_one_drawn_there_also_in_a_stack(app):
    """A stack is drawn by its first picture's character: a point on its
    lower picture finds the lower picture."""
    names = ("portrait.png", "landscape.png", "landscape.png")
    view = _shown("Text.\n\n" + _markdown(*names), 900)
    doc = view.document()
    boxes = _boxes(doc)
    assert len({round(box.left()) for _p, _n, box in boxes}) == 2      # a lead and a stack
    for position, name, box in boxes:
        assert photo_group_view.picture_at(doc, box.center()) == (position, name)
        assert photo_group_view.picture_box(doc, position) == box
    assert photo_group_view.picture_at(doc, QPointF(5, 5)) is None
    view.close()


def test_a_larger_text_gives_a_caption_more_room_without_a_change_to_the_document(app):
    """The app's text size (View > Zoom, the previews): a caption is drawn
    in the text's font, so its group is laid out again for the new one."""
    view = _shown(_markdown("photo.png", "square.png", captions={"square.png": "Under the square"})
                  + "\nAfter.\n", 900)
    doc = view.document()
    before = _state(doc)
    layout = doc.documentLayout()
    gap = layout.blockBoundingRect(doc.lastBlock()).top() - _boxes(doc)[1][2].bottom()
    font = doc.defaultFont()
    font.setPointSizeF(font.pointSizeF() * 2)
    doc.setDefaultFont(font)
    QApplication.processEvents()
    larger = layout.blockBoundingRect(doc.lastBlock()).top() - _boxes(doc)[1][2].bottom()
    assert larger > gap * 1.5
    assert _state(doc) == before
    view.close()
