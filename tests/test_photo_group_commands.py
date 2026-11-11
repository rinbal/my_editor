# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Format > Photo's commands, as the Markdown saved after each.

Each command runs as the menus, the chips and the keys run it
(HtmlEditor.photos.run) on a selected picture: the test checks the exact
Markdown, and that one Undo brings the file back as it was. Then one test
each for a drag, the keys and Undo selecting what it took back.
"""

from __future__ import annotations

import sys
import types

import pytest
from PySide6.QtCore import QBuffer, QByteArray, QEvent, QIODevice, QPointF, QRect, QRectF, Qt
from PySide6.QtGui import (
    QAction, QColor, QImage, QMouseEvent, QPainter, QTextCursor, QTextImageFormat,
)
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

import announce
import photo_group
import photo_group_actions
import photo_group_editing
import photo_group_overlay
import picture_crop
import theme
from editor import HtmlEditor
from main_window import MainWindow
from markdown_writer import read_markdown
from photo_group_menu import (
    ADD, CAPTION, CROP, DESCRIPTION, DOWN, FIRST, JOIN_ABOVE, JOIN_BELOW, LARGE, LAST, LEFT,
    MOVE_MODE, REMOVE, RIGHT, SPLIT, TAKE_OUT, UP,
)
from tests.photo_corners import image_of
from tests.test_photo_group_editing import (
    FOUR, FOUR_PICTURES, ONE, THREE, TWO, _editor, _group, _markdown, _text, _undone,
    app,  # noqa: F401
)

TWO_GROUPS = _text("Before.", ONE, TWO, "Middle.", THREE, FOUR, "After.")


@pytest.fixture(autouse=True)
def said(monkeypatch):
    words = []
    monkeypatch.setattr(announce, "announce", lambda source, text: words.append(text))
    return words


def _selected(markdown: str = FOUR_PICTURES, number: int = 1, *, group: int = 0,
              width: int = 900):
    ed = _editor(markdown, width)
    ed.photos.select(_group(ed, group).start + number)
    return ed


def _after(name: str, number: int = 1, markdown: str = FOUR_PICTURES, **where) -> str:
    """The Markdown after the command ``name`` on picture ``number`` (from
    0) of a group; one Undo must bring ``markdown`` back."""
    ed = _selected(markdown, number, **where)
    ed.photos.run(name)
    written = _markdown(ed)
    assert _undone(ed, markdown)
    ed.close()
    return written


def test_move_left_swaps_a_picture_with_the_one_before():
    assert _after(LEFT) == _text("Before.", TWO, ONE, THREE, FOUR, "After.")


def test_move_right_swaps_a_picture_with_the_one_after():
    assert _after(RIGHT) == _text("Before.", ONE, THREE, TWO, FOUR, "After.")


def test_move_to_start_puts_a_picture_first():
    assert _after(FIRST, 2) == _text("Before.", THREE, ONE, TWO, FOUR, "After.")


def test_move_to_end_puts_a_picture_last():
    assert _after(LAST) == _text("Before.", ONE, THREE, FOUR, TWO, "After.")


def test_move_up_on_a_wide_page_puts_a_picture_above_the_paragraph_before():
    assert _after(UP) == _text(TWO, "Before.", ONE, THREE, FOUR, "After.")


def test_move_down_in_a_narrow_column_moves_a_picture_down_its_group():
    assert _after(DOWN, width=380) == _text("Before.", ONE, THREE, TWO, FOUR, "After.")


def test_show_large_marks_a_picture_wide():
    assert _after(LARGE, 0) == _text("Before.", "![one](photo.png#wide)", TWO, THREE, FOUR,
                                     "After.")


def test_take_out_of_group_puts_a_picture_alone_under_the_next_paragraph():
    assert _after(TAKE_OUT) == _text("Before.", ONE, THREE, FOUR, "After.", TWO)


def test_join_the_group_above_and_below_make_one_group():
    joined = _text("Before.", ONE, TWO, THREE, FOUR, "Middle.", "After.")
    assert _after(JOIN_ABOVE, 0, TWO_GROUPS, group=1) == joined
    assert _after(JOIN_BELOW, 1, TWO_GROUPS) == joined


def test_remove_takes_a_picture_away_and_says_how_to_undo(said):
    ed = _selected()
    ed.photos.run(REMOVE)
    assert _markdown(ed) == _text("Before.", ONE, THREE, FOUR, "After.")
    assert said[-1].startswith("Photo removed, 3 photos remain. Undo with ")
    assert _undone(ed) and said[-1] == "Undone: Remove Photo"
    ed.close()


def test_split_group_here_opens_a_line_whose_first_words_are_the_same_step():
    ed = _selected()
    ed.photos.run(SPLIT)
    QTest.keyClicks(ed, "Middle.")
    assert _markdown(ed) == _text("Before.", ONE, TWO, "Middle.", THREE, FOUR, "After.")
    assert ed.undo_name() == "Split Group"
    assert _undone(ed)
    ed.close()


def test_a_split_whose_line_stays_empty_is_taken_back(said):
    ed = _selected()
    ed.photos.run(SPLIT)
    QTest.keyClick(ed, Qt.Key.Key_Up)
    assert _markdown(ed) == FOUR_PICTURES and not ed.document().isUndoAvailable()
    assert said[-1] == "The line stayed empty, the photos belong together again"
    ed.close()


def _write_in_field(ed, words: str) -> None:
    field = next((found for found in ed.viewport().findChildren(QWidget, "pictureField")
                  if found.isVisible()), None)
    assert field is not None
    field.line.setText(words)
    QTest.keyClick(field.line, Qt.Key.Key_Return)
    QApplication.processEvents()


def test_caption_is_written_in_place_and_saved_under_the_picture():
    ed = _selected(number=0)
    ed.photos.run(CAPTION)
    _write_in_field(ed, "The jetty")
    assert _markdown(ed) == _text("Before.", "![one](photo.png)\n\n*The jetty*", TWO, THREE,
                                  FOUR, "After.")
    assert ed.undo_name() == "Caption" and ed.hasFocus()
    assert _undone(ed)
    ed.close()


def test_a_caption_keeps_its_bold_words_and_its_link_when_one_word_is_changed():
    rich = "![two](square.png)\n\n*The **old** jetty at [Loch Ossian](https://example.org/l)*"
    markdown = _text("Before.", ONE, rich, THREE, FOUR, "After.")
    ed = _selected(markdown, number=1)
    ed.photos.run(CAPTION)
    _write_in_field(ed, "The old pier at Loch Ossian")
    assert _markdown(ed) == markdown.replace("jetty", "pier")
    ed.photos.run(CAPTION)
    _write_in_field(ed, "The old pier at Loch Ossian")
    assert _markdown(ed) == markdown.replace("jetty", "pier") and ed.undo_name() == "Caption"
    ed.close()


def test_return_in_an_unchanged_field_writes_nothing():
    # Gate RG-PG2E-a 7: a soft line break or a double space made it new.
    markdown = _text("Before.", ONE, "![two  words](square.png)\n\n*Line one\nline two*", THREE,
                     FOUR, "After.")
    ed = _selected(markdown, number=1)
    for kind in (CAPTION, DESCRIPTION):
        ed.photos.run(kind)
        field = next(found for found in ed.viewport().findChildren(QWidget, "pictureField")
                     if found.isVisible())
        QTest.keyClick(field.line, Qt.Key.Key_Return)
        QApplication.processEvents()
    assert _markdown(ed) == markdown and not ed.document().isModified()
    ed.close()


def test_markup_typed_into_a_caption_is_read_back_as_typed():
    typed = "Mit *Stern* und _Strich_ [x](y)"
    ed = _selected(number=0)
    ed.photos.run(CAPTION)
    _write_in_field(ed, typed)
    again = _editor(_markdown(ed))
    unit = photo_group.group_at(again.document(), _group(again).start).units[0]
    assert unit.caption_line == typed and not any(run.href for run in unit.caption)
    ed.close()
    again.close()


def test_description_is_written_on_the_picture_and_saved_as_its_alt_text():
    ed = _selected(number=0)
    ed.photos.run(DESCRIPTION)
    _write_in_field(ed, "A wooden jetty")
    assert _markdown(ed) == _text("Before.", "![A wooden jetty](photo.png)", TWO, THREE, FOUR,
                                  "After.")
    assert _undone(ed)
    ed.close()


def _png(image: QImage) -> bytes:
    out = QByteArray()
    buffer = QBuffer(out)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(out)


def test_crop_asks_for_the_part_and_shows_it_as_a_new_file():
    ed = _selected(number=0)
    asked = []
    ed.photos.crop_requested.connect(asked.append)
    ed.photos.run(CROP)
    assert asked == [_group(ed).start]
    # The part is a file of its own; the picture's file is never written.
    source = QImage(40, 30, QImage.Format.Format_RGB32)
    source.fill(Qt.GlobalColor.darkCyan)
    data = _png(source)
    cut, ext = picture_crop.crop_bytes(data, QRect(5, 5, 20, 10))
    assert ext == "png" and QImage.fromData(cut).size().toTuple() == (20, 10)
    ed.photos.set_address(asked[0], "landscape.png")       # (the new file's name)
    assert _markdown(ed) == _text("Before.", "![one](landscape.png)", TWO, THREE, FOUR,
                                  "After.")
    assert ed.undo_name() == "Crop Photo" and _undone(ed)
    ed.close()


def _jpeg_turned(width: int, height: int, orientation: int) -> bytes:
    """A JPEG ``width`` x ``height`` with an EXIF orientation, as a phone writes it."""
    out = QByteArray()
    buffer = QBuffer(out)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.darkCyan)
    image.save(buffer, "JPEG")
    tiff = (b"II*\x00\x08\x00\x00\x00\x01\x00" + b"\x12\x01\x03\x00\x01\x00\x00\x00"
            + orientation.to_bytes(2, "little") + b"\x00\x00" + b"\x00\x00\x00\x00")
    exif = b"Exif\x00\x00" + tiff
    return bytes(out)[:2] + b"\xff\xe1" + (len(exif) + 2).to_bytes(2, "big") + exif \
        + bytes(out)[2:]


def test_a_crop_stands_the_way_the_camera_said():
    # Gate RG-PG2E-a 5: an iPhone portrait (40x20, orientation 6) came out on its side.
    data = _jpeg_turned(40, 20, 6)
    cut, _ext = picture_crop.crop_bytes(data, QRect(0, 0, 20, 30))
    assert QImage.fromData(cut).size().toTuple() == (20, 30)


def test_the_crop_sheet_dims_crop_until_the_frame_changes_and_shows_its_focus():
    window = QWidget()
    window.resize(700, 600)
    window.show()
    data = _jpeg_turned(40, 20, 6)
    sheet = picture_crop.CropSheet(window, data, picture_crop.decode_upright(data))
    sheet.show()
    QApplication.processEvents()
    assert sheet.view.size().toTuple()[1] > sheet.view.size().toTuple()[0]     # upright
    assert not sheet.crop_button.isEnabled() and not sheet.crop_button.isDefault()
    view = sheet.view
    ring = view.palette().color(view.palette().ColorRole.Highlight)
    if view.hasFocus():
        edge = view.grab().toImage().pixelColor(view.width() // 2, 1)
        assert abs(edge.blue() - ring.blue()) < 40 and abs(edge.red() - ring.red()) < 40
    sheet.shape.setCurrentIndex(2)                                          # Square
    assert sheet.crop_button.isEnabled() and sheet.crop_button.isDefault()
    sheet.reject()
    window.close()


def test_add_photos_puts_the_photos_chosen_at_the_end_of_the_group(said):
    ed = _selected(number=0)
    asked = []
    ed.photos.add_requested.connect(lambda place, paths: asked.append((place, paths)))
    ed.photos.run(ADD)
    (place, paths), = asked
    assert paths is None                        # the window asks for the files
    added = QTextImageFormat()
    added.setName("landscape.png")
    added.setProperty(QTextImageFormat.ImageAltText, "five")
    ed.photos.insert(place, [added], "Add Photo")
    assert _markdown(ed) == _text("Before.", ONE, TWO, THREE, FOUR, "![five](landscape.png)",
                                  "After.")
    assert said[-1] == "1 photo added. The group has 5 photos now"
    assert _undone(ed)
    ed.close()


def test_move_mode_moves_a_picture_with_the_arrows_and_return_puts_it_down():
    ed = _selected(number=0)
    ed.photos.run(MOVE_MODE)
    QTest.keyClick(ed, Qt.Key.Key_Right)
    QTest.keyClick(ed, Qt.Key.Key_Right)
    assert _markdown(ed) == FOUR_PICTURES         # nothing moved before Return
    QTest.keyClick(ed, Qt.Key.Key_Return)
    assert _markdown(ed) == _text("Before.", TWO, THREE, ONE, FOUR, "After.")
    assert _undone(ed)
    ed.close()


# --------------------------------------------------------------------------- #
# The pointer, the keys, Undo                                                  #
# --------------------------------------------------------------------------- #

def _mouse(ed, kind, point: QPointF, buttons) -> None:
    button = Qt.MouseButton.LeftButton if kind != QEvent.Type.MouseMove else Qt.MouseButton.NoButton
    event = QMouseEvent(kind, point, ed.viewport().mapToGlobal(point), button, buttons,
                        Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(ed.viewport(), event)


def test_a_picture_dragged_onto_another_place_of_its_group_goes_there():
    ed = _selected(number=0)
    photos = ed.photos
    start = photos.to_view(photos.box_of(photos.current())).center()
    last = photos.to_view(photos.box_of(photo_group_actions.picture_at(
        ed.document(), _group(ed).start + 3))).center()
    held = Qt.MouseButton.LeftButton
    _mouse(ed, QEvent.Type.MouseButtonPress, start, held)
    _mouse(ed, QEvent.Type.MouseMove, start + QPointF(30, 0), held)
    _mouse(ed, QEvent.Type.MouseMove, last, held)
    QTest.qWait(200)                               # the target waits its moment
    _mouse(ed, QEvent.Type.MouseMove, last + QPointF(1, 0), held)
    assert _markdown(ed) == FOUR_PICTURES          # nothing moved while dragging
    _mouse(ed, QEvent.Type.MouseButtonRelease, last, Qt.MouseButton.NoButton)
    assert _markdown(ed) == _text("Before.", TWO, THREE, FOUR, ONE, "After.")
    assert _undone(ed)
    ed.close()


def test_the_move_keys_move_the_selected_picture_one_place():
    ed = _selected()
    held = (Qt.KeyboardModifier.MetaModifier | Qt.KeyboardModifier.ControlModifier
            if sys.platform == "darwin"
            else Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
    QTest.keyClick(ed, Qt.Key.Key_Right, held)
    assert _markdown(ed) == _text("Before.", ONE, THREE, TWO, FOUR, "After.")
    assert ed.textCursor().selectionStart() == _group(ed).start + 2      # still selected
    ed.close()


def test_undo_selects_the_picture_where_it_was_and_says_what_it_took_back(said):
    ed = _selected()
    ed.photos.run(LAST)
    ed.undo()
    cursor = ed.textCursor()
    assert (cursor.selectionStart(), cursor.selectionEnd()) == (_group(ed).start + 1,
                                                                _group(ed).start + 2)
    assert said[-1] == "Undone: Move Photo"
    ed.redo()
    assert ed.textCursor().selectionStart() == _group(ed).start + 3
    assert said[-1] == "Redone: Move Photo"
    ed.close()


# -- what outlives one event follows the document (gate RG-PG2E-a 1, 2, 6) ----

def test_move_mode_stops_when_undo_changes_the_document_under_it():
    ed = _selected(number=2)
    ed.photos.run(TAKE_OUT)
    ed.photos.select(_group(ed).start)
    ed.photos.run(MOVE_MODE)
    QTest.keyClick(ed, Qt.Key.Key_Right)
    ed.undo()
    QTest.keyClick(ed, Qt.Key.Key_Return)
    assert _markdown(ed) == FOUR_PICTURES
    ed.close()


def test_a_command_run_while_a_field_is_open_writes_the_field_first():
    ed = _selected(number=0)
    ed.photos.run(CAPTION)
    field = next(found for found in ed.viewport().findChildren(QWidget, "pictureField")
                 if found.isVisible())
    field.line.setText("New caption")
    ed.photos.run(RIGHT)
    assert _markdown(ed) == _text("Before.", TWO, "![one](photo.png)\n\n*New caption*", THREE,
                                  FOUR, "After.")
    ed.close()


def test_leaving_an_empty_split_line_selects_the_picture_the_caret_went_to():
    ed = _selected(number=1)
    ed.photos.run(SPLIT)
    QTest.keyClick(ed, Qt.Key.Key_Down)
    assert _markdown(ed) == FOUR_PICTURES
    picture = ed.photos.current()
    cursor = ed.textCursor()
    assert picture.unit.name == "portrait.png"
    assert (cursor.selectionStart(), cursor.selectionEnd()) == (picture.position,
                                                                picture.position + 1)
    QTest.keyClick(ed, Qt.Key.Key_Backspace)
    assert _markdown(ed) == _text("Before.", ONE, TWO, FOUR, "After.")
    ed.close()


# -- a place between paragraphs is what the file will hold (gates a 4, b 3) ----

def test_take_out_then_up_shows_what_the_file_reopens_as():
    ed = _selected(number=1)
    ed.photos.run(TAKE_OUT)
    ed.photos.run(UP)
    written = _markdown(ed)
    assert written == _text("Before.", ONE, THREE, FOUR, TWO, "After.")
    shown = [len(group.units) for group in photo_group.groups_in(ed.document())]
    again = _editor(written)
    assert shown == [len(group.units) for group in photo_group.groups_in(again.document())] == [4]
    ed.close()
    again.close()


def test_a_picture_moved_up_goes_past_an_italic_paragraph_that_would_be_its_caption():
    ed = _selected(_text("Words.", "*An italic aside.*", ONE, THREE, FOUR), number=0)
    ed.photos.run(UP)
    assert _markdown(ed) == _text(ONE, "Words.", "*An italic aside.*", THREE, FOUR)
    ed.close()


def test_take_out_lands_where_the_file_keeps_it_alone():
    ed = _selected(_text("Before.", ONE, TWO, THREE, "After.", "*An aside.*", "End."), number=1)
    ed.photos.run(TAKE_OUT)
    assert _markdown(ed) == _text("Before.", ONE, THREE, "After.", "*An aside.*", TWO, "End.")
    ed.close()
    ed = _selected(TWO_GROUPS, number=1)
    assert ed.photos.state().hints[TAKE_OUT] == "Then it stands alone above the paragraph before"
    ed.photos.run(TAKE_OUT)
    assert _markdown(ed) == _text(TWO, "Before.", ONE, "Middle.", THREE, FOUR, "After.")
    ed.close()


def test_a_click_on_a_picture_in_view_scrolls_nothing():
    # Gate RG-PG2E-b 2: the page jumped 193 to 229 px on a click.
    # In a narrow column, the caret after picture 1 is at picture 2, out of view.
    words = _text(*[f"Paragraph {n} with a few words." for n in range(40)])
    ed = _editor(_text(words, ONE, TWO, THREE, FOUR, words), width=420)
    ed.resize(420, 500)
    QApplication.processEvents()
    photos = ed.photos
    box = photos.box_of(photo_group_actions.picture_at(ed.document(), _group(ed).start))
    bar = ed.verticalScrollBar()
    bar.setValue(int(box.bottom()) - ed.viewport().height() + 20)
    before = bar.value()
    point = photos.to_view(box).center()
    _mouse(ed, QEvent.Type.MouseButtonPress, point, Qt.MouseButton.LeftButton)
    _mouse(ed, QEvent.Type.MouseButtonRelease, point, Qt.MouseButton.NoButton)
    assert photos.current().place == 1 and bar.value() == before
    ed.close()


def test_move_mode_shows_its_keys_beside_the_picture_and_lets_a_click_or_a_key_out(
        said, monkeypatch):
    hints = []
    painted = photo_group_overlay.paint_hint
    monkeypatch.setattr(photo_group_overlay, "paint_hint",
                        lambda painter, box, words, **how: (hints.append((box, words)),
                                                            painted(painter, box, words, **how)))
    ed = _selected(number=1)
    ed.photos.run(MOVE_MODE)
    QTest.keyClick(ed, Qt.Key.Key_Right)
    ed.viewport().repaint()
    (box, words), *_rest = hints
    assert words == "Arrow keys move it, Return puts it down, Esc cancels"
    assert ed.viewport().rect().intersects(box.toAlignedRect())
    QTest.keyClick(ed, Qt.Key.Key_A)
    assert said[-1] == "Cancelled, the photo is back in its place"
    ed.photos.run(MOVE_MODE)
    text = ed.cursorRect(QTextCursor(ed.document())).center()
    _mouse(ed, QEvent.Type.MouseButtonPress, QPointF(text), Qt.MouseButton.LeftButton)
    _mouse(ed, QEvent.Type.MouseButtonRelease, QPointF(text), Qt.MouseButton.NoButton)
    assert said[-1] == "Cancelled, the photo is back in its place"
    assert _markdown(ed) == FOUR_PICTURES and ed.textCursor().block().text() == "Before."
    ed.close()


def test_a_command_says_only_what_it_did(said):
    # Gate RG-PG2E-b 6: "Photo 3 of 3. boats" came first.
    ed = _selected(_text("Before.", ONE, TWO, "![boats](portrait.png)", "After."), number=1)
    said.clear()
    ed.photos.run(DESCRIPTION)
    _write_in_field(ed, "Two boats")
    ed.photos.run(REMOVE)
    assert said == ["Description saved",
                    f"Photo removed, 2 photos remain. Undo with {photo_group_editing._undo_keys()}"]
    ed.close()


def test_undo_names_a_split_with_its_words_and_a_group_made_from_the_slash_menu(said):
    # Gate RG-PG2E-a 8 and RG-PG2E-b 7: both were silent.
    ed = _selected(number=1)
    ed.photos.run(SPLIT)
    QTest.keyClicks(ed, "Words")
    ed.undo()
    assert said[-1] == "Undone: Split Group" and ed.photos.current().place == 2
    ed.close()
    ed = _editor(_text("Before.", "/photo", "After."))
    cursor = ed.textCursor()
    cursor.setPosition(len("Before.\n/photo"))
    ed.setTextCursor(cursor)
    ed._slash_at = len("Before.\n")
    picked = QTextImageFormat()
    picked.setName("photo.png")
    group = QAction("Photo Group …", ed)              # as German spells it: " …"
    group.triggered.connect(lambda: ed.photos.insert(ed.photos.free_place(), [picked],
                                                     "Add Photos"))
    said.clear()
    ed._pick_slash(group)
    assert said == ["1 photo added"] and ed.undo_name() == "Photo Group"
    ed.undo()
    assert said[-1] == "Undone: Photo Group"
    ed.close()


def test_the_large_chip_shows_its_new_state_at_once(monkeypatch):
    # Gate RG-PG2E-b 8: it showed the old state until the pointer moved.
    shown = []
    painted = photo_group_overlay.paint_chips
    monkeypatch.setattr(photo_group_overlay, "paint_chips",
                        lambda painter, chips, **how: (shown.append(how["big_on"]),
                                                       painted(painter, chips, **how)))
    ed = _selected(number=0)
    chip = ed.photos.pointer._chips(ed.photos.current()).boxes[photo_group_overlay.BIG].center()
    _mouse(ed, QEvent.Type.MouseMove, chip, Qt.MouseButton.NoButton)
    _mouse(ed, QEvent.Type.MouseButtonPress, chip, Qt.MouseButton.LeftButton)
    _mouse(ed, QEvent.Type.MouseButtonRelease, chip, Qt.MouseButton.NoButton)
    assert _group(ed).units[0].wide
    shown.clear()
    ed.viewport().repaint()
    assert shown == [True]
    ed.close()


def test_the_line_a_split_opens_says_what_it_is_for(monkeypatch):
    # Gate RG-PG2E-b 9: the empty line had no words (EDITING 2.5).
    written = []
    monkeypatch.setattr(QPainter, "drawText", lambda painter, *where: written.append(where[-1]))
    ed = _selected(number=1)
    ed.photos.run(SPLIT)
    ed.viewport().repaint()
    assert "Write something here, then the group is split" in written
    written.clear()
    QTest.keyClicks(ed, "W")
    ed.viewport().repaint()
    assert "Write something here, then the group is split" not in written
    ed.close()


def test_a_click_beside_a_narrower_picture_of_a_group_selects_the_nearest(said):
    # Gate RG-PG2E-b 10: Qt's caret as tall as the row was left there.
    ed = _editor(_text("Before.", "![one](portrait.png)", "![two](portrait.png)", "After."),
                 width=1400)
    group = _group(ed)
    photos = ed.photos
    box = photos.to_view(photos.box_of(photo_group_actions.picture_at(ed.document(),
                                                                      group.start + 1)))
    room = ed.document().documentLayout().blockBoundingRect(ed.document().findBlock(group.start))
    beside = QPointF(min(box.right() + 30, photos.to_view(room).right() - 4), box.center().y())
    assert photos.picture_hit(beside + QPointF(ed.horizontalScrollBar().value(),
                                               ed.verticalScrollBar().value())) is None
    _mouse(ed, QEvent.Type.MouseButtonPress, beside, Qt.MouseButton.LeftButton)
    _mouse(ed, QEvent.Type.MouseButtonRelease, beside, Qt.MouseButton.NoButton)
    assert photos.current().place == 2 and ed.textCursor().hasSelection()
    ed.close()


def test_a_picture_alone_selected_is_not_tinted(said):
    # Gate RG-PG2E-b 11: Qt's highlight tinted it blue.
    ed = _editor(_text("Before.", "![one](photo.png)", "After."))
    picture = photo_group_actions.picture_at(ed.document(), len("Before.\n"))
    middle = ed.photos.to_view(ed.photos.box_of(picture)).center().toPoint()
    plain = ed.viewport().grab().toImage().pixelColor(middle)
    ed.photos.select(picture.position)
    assert ed.textCursor().hasSelection()
    assert ed.viewport().grab().toImage().pixelColor(middle) == plain
    ed.close()


def test_the_bars_small_text_stands_out_at_4_5_to_1(monkeypatch):
    # Gate RG-PG2E-b 12: "Drag to arrange" was 2.5:1 on the light page.
    pens = []
    monkeypatch.setattr(QPainter, "setPen", lambda painter, pen: pens.append(pen))
    for surface, ink in ((QColor(250, 249, 246), QColor("#333333")),
                         (QColor(38, 38, 38), QColor("#D4D4D4"))):
        for in_accent in (False, True):
            pens.clear()
            image = QImage(600, 40, QImage.Format.Format_ARGB32)
            painter = QPainter(image)
            bar = photo_group_overlay.bar_over(QRectF(0, 40, 600, 300), "Photo")
            photo_group_overlay.paint_bar(painter, bar, "Photo Group · 3 Photos",
                                          "Drag to arrange", "Photo", ink=ink,
                                          accent=QColor(theme.focus_ring_color(
                                              surface.lightness() < 128)),
                                          surface=surface, hint_in_accent=in_accent, hot=False,
                                          solid=False)
            painter.end()
            for pen in pens:
                if isinstance(pen, QColor):
                    alpha = pen.alphaF()
                    seen = QColor.fromRgbF(*(alpha * mine + (1 - alpha) * page for mine, page in
                                             zip(pen.getRgbF()[:3], surface.getRgbF()[:3])))
                    assert theme.contrast_ratio(seen, surface) >= 4.5, pen.name()


def test_crop_is_dimmed_for_a_stand_in_and_says_why_a_drawing_cannot_be_cut(tmp_path):
    # Gate RG-PG2E-a 9: an SVG beside the document opened an empty sheet.
    ed = HtmlEditor()
    ed.set_local_image_resolver(lambda name: None if name.endswith(".svg") else image_of(name))
    read_markdown(ed.document(), _text("Before.", "![a](drawing.svg)", "![b](photo.png)",
                                       "After."))
    ed.show()
    QApplication.processEvents()
    group = _group(ed)
    can = []
    for number in range(2):
        ed.photos.select(group.start + number)
        can.append(ed.photos.state().can[CROP])
    assert can == [False, True]
    drawing = tmp_path / "drawing.svg"
    drawing.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="4" height="4"/>')
    window = types.SimpleNamespace(_look_files=None,
                                   _picture_file=lambda editor, name: str(drawing))
    notices = []
    ed.notice.connect(notices.append)
    MainWindow._crop_photo(window, ed, group.start)
    assert notices == ["This picture is an SVG drawing, which can’t be cropped here. Crop it in "
                       "a drawing app instead."]
    ed.close()
