# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Editing a document with photo groups in it, with the keys an author uses.

A group is one paragraph of picture characters: the caret has one stop at
each picture, Backspace and Delete take one picture away, and one Undo
takes back one edit, whatever the editor did to keep the group whole in
the same step (photo_group_edit.settle). Text never lands between the
pictures of a group. Each test checks the exact Markdown.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QMimeData, QPoint, Qt
from PySide6.QtGui import QInputMethodEvent, QTextCursor, QTextImageFormat
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import photo_group
import photo_group_edit
import photo_group_view
from editor import HtmlEditor
from markdown_writer import _image_as_read, document_to_markdown, read_markdown
from tests.photo_corners import image_of


@pytest.fixture(scope="module", autouse=True)
def app():
    return QApplication.instance() or QApplication([])


ONE, TWO, THREE, FOUR = ("![one](photo.png)", "![two](square.png)\n\n*Cap two*",
                         "![three](portrait.png)", "![four](landscape.png)")


def _text(*parts) -> str:
    return "\n\n".join(parts) + "\n"


FOUR_PICTURES = _text("Before.", ONE, TWO, THREE, FOUR, "After.")


def _editor(markdown: str = FOUR_PICTURES, width: int = 900) -> HtmlEditor:
    ed = HtmlEditor()
    ed.set_local_image_resolver(image_of)
    ed.resize(width, 1600)
    read_markdown(ed.document(), markdown)
    ed.document().clearUndoRedoStacks()
    ed.document().setModified(False)
    ed.show()
    ed.setFocus()
    QApplication.processEvents()
    return ed


def _markdown(ed) -> str:
    return document_to_markdown(ed.document(), _image_as_read)


def _paragraphs(ed):
    """Each paragraph as "G<pictures>" for a group, else its text."""
    out = []
    block = ed.document().begin()
    while block.isValid():
        out.append(f"G{len(photo_group.block_units(block))}"
                   if photo_group.is_group_block(block) else block.text())
        block = block.next()
    return out


def _group(ed, number: int = 0) -> photo_group.Group:
    return photo_group.groups_in(ed.document())[number]


def _caret(ed, position: int) -> None:
    cursor = ed.textCursor()
    cursor.setPosition(position)
    ed.setTextCursor(cursor)


def _undone(ed, markdown: str = FOUR_PICTURES) -> bool:
    """One Undo brings ``markdown`` back, and nothing is left to undo."""
    ed.undo()
    return _markdown(ed) == markdown and not ed.document().isUndoAvailable()


# --------------------------------------------------------------------------- #
# The caret                                                                    #
# --------------------------------------------------------------------------- #

def test_the_arrows_select_each_picture_in_turn_and_go_on_into_the_words():
    """From the words before a group, Right selects its pictures one after
    another, then the caret goes into the words after it (EDITING.md 2.3)."""
    ed = _editor()
    group = _group(ed)
    _caret(ed, group.start - 1)                    # the end of "Before."
    stops = []
    for _ in range(5):
        QTest.keyClick(ed, Qt.Key.Key_Right)
        cursor = ed.textCursor()
        stops.append((cursor.selectionStart(), cursor.selectionEnd()))
    assert stops == [(group.start + k, group.start + k + 1) for k in range(4)] + [
        (group.end + 1, group.end + 1)]
    QTest.keyClick(ed, Qt.Key.Key_Left)            # back: the last picture
    assert ed.textCursor().selectionStart() == group.start + 3
    ed.close()


def test_a_ring_shows_which_picture_the_caret_is_at_also_in_a_stack():
    """A stack's lower picture takes no room of its own in the line: the
    ring is what shows the caret there."""
    ed = _editor(_text("Text.", "![a](portrait.png)", "![b](landscape.png)", "![c](landscape.png)"))
    group = _group(ed)
    boxes = {position: box for position, _n, box in photo_group_view.picture_boxes(ed.document())}

    def ring():
        picture = ed.photos.current()
        return ed.photos.box_of(picture) if picture is not None else None

    rings = []
    for position in range(group.start, group.end + 1):
        _caret(ed, position)
        rings.append(ring())
    assert [box.center().toPoint() for box in rings[:3]] == [
        boxes[position].center().toPoint() for position in range(group.start, group.end)]
    assert rings[3] == rings[2]                    # after the last picture: the last one
    _caret(ed, 0)
    assert ring() is None
    # Painted in the accent colour (the focus ring's), on the picture's edge.
    _caret(ed, group.start + 2)
    QApplication.processEvents()
    shot = ed.viewport().grab().toImage()
    box = ed.photos.to_view(rings[2])
    edge = QPoint(int(box.left()) + 40, int(box.top()) + 1)
    ratio = shot.devicePixelRatio() or 1.0
    accent = ed.photos.pointer.colors()[0]
    drawn = shot.pixelColor(int(edge.x() * ratio), int(edge.y() * ratio))
    assert abs(drawn.red() - accent.red()) + abs(drawn.green() - accent.green()) \
        + abs(drawn.blue() - accent.blue()) < 60
    ed.close()


def test_a_click_on_a_picture_selects_it_and_backspace_takes_it_away():
    ed = _editor()
    box = next(box for _p, name, box in photo_group_view.picture_boxes(ed.document())
               if name == "portrait.png")
    QTest.mouseClick(ed.viewport(), Qt.MouseButton.LeftButton, pos=box.center().toPoint())
    assert photo_group_view.picture_at_cursor(ed.textCursor())[3].name == "portrait.png"
    assert ed.textCursor().selectedText() == "￼"
    QTest.keyClick(ed, Qt.Key.Key_Backspace)
    assert _markdown(ed) == _text("Before.", ONE, TWO, FOUR, "After.")
    assert _undone(ed)
    ed.close()


# --------------------------------------------------------------------------- #
# Backspace and Delete                                                         #
# --------------------------------------------------------------------------- #

def test_backspace_takes_one_picture_away_with_its_caption_and_undo_brings_it_back():
    ed = _editor()
    _caret(ed, _group(ed).start + 2)               # after "two"
    QTest.keyClick(ed, Qt.Key.Key_Backspace)
    assert _markdown(ed) == _text("Before.", ONE, THREE, FOUR, "After.")
    assert _paragraphs(ed) == ["Before.", "G3", "After."]
    assert _undone(ed)
    ed.redo()
    assert _markdown(ed) == _text("Before.", ONE, THREE, FOUR, "After.")
    ed.close()


def test_delete_takes_the_picture_after_the_caret_away():
    ed = _editor()
    _caret(ed, _group(ed).start)
    QTest.keyClick(ed, Qt.Key.Key_Delete)
    assert _markdown(ed) == _text("Before.", TWO, THREE, FOUR, "After.")
    assert _undone(ed)
    ed.close()


def test_deleting_a_word_among_pictures_takes_one_picture_too():
    """Option-Delete (Ctrl+Backspace elsewhere) deletes a word: among
    pictures that would be all of them."""
    import sys
    ed = _editor()
    _caret(ed, _group(ed).end)
    word = (Qt.KeyboardModifier.AltModifier if sys.platform == "darwin"
            else Qt.KeyboardModifier.ControlModifier)
    QTest.keyClick(ed, Qt.Key.Key_Backspace, word)
    assert _markdown(ed) == _text("Before.", ONE, TWO, THREE, "After.")
    ed.close()


def test_a_group_left_with_one_picture_is_that_pictures_paragraphs_again():
    ed = _editor(_text("Before.", ONE, TWO, "After."))
    _caret(ed, _group(ed).start)
    QTest.keyClick(ed, Qt.Key.Key_Delete)
    assert _paragraphs(ed) == ["Before.", "￼", "Cap two", "After."]
    assert _markdown(ed) == _text("Before.", TWO, "After.")
    assert ed.textCursor().position() == ed.document().findBlock(8).position()   # before it
    assert _undone(ed, _text("Before.", ONE, TWO, "After."))
    assert _paragraphs(ed) == ["Before.", "G2", "After."]
    ed.close()


def test_backspace_below_a_group_goes_to_its_end_and_takes_nothing():
    """From words under a group the first Backspace only goes up to the
    group; the next one takes the last picture."""
    ed = _editor()
    group = _group(ed)
    _caret(ed, group.end + 1)                      # the start of "After."
    QTest.keyClick(ed, Qt.Key.Key_Backspace)
    assert ed.textCursor().position() == group.end
    assert _markdown(ed) == FOUR_PICTURES and not ed.document().isUndoAvailable()
    QTest.keyClick(ed, Qt.Key.Key_Backspace)
    assert _markdown(ed) == _text("Before.", ONE, TWO, THREE, "After.")
    ed.close()


def test_delete_above_a_group_goes_to_its_start_and_takes_nothing():
    ed = _editor()
    group = _group(ed)
    _caret(ed, group.start - 1)                    # the end of "Before."
    QTest.keyClick(ed, Qt.Key.Key_Delete)
    assert ed.textCursor().position() == group.start
    assert _markdown(ed) == FOUR_PICTURES and not ed.document().isUndoAvailable()
    ed.close()


def test_deleting_the_words_between_two_groups_makes_them_one():
    """EDITING 2.1, join: with the paragraph between them gone, the
    pictures are one group, as they are when the file is opened again."""
    source = _text(ONE, TWO, "x", THREE, FOUR)
    ed = _editor(source)
    assert _paragraphs(ed) == ["G2", "x", "G2"]
    _caret(ed, _group(ed, 1).start - 1)            # after the "x"
    QTest.keyClick(ed, Qt.Key.Key_Backspace)
    assert _paragraphs(ed) == ["G2", "", "G2"]     # an empty line keeps them apart
    QTest.keyClick(ed, Qt.Key.Key_Backspace)
    assert _paragraphs(ed) == ["G4"]
    assert ed.textCursor().position() == _group(ed).start + 2
    assert _markdown(ed) == _text(ONE, TWO, THREE, FOUR)
    reopened = _editor(_markdown(ed))
    assert _paragraphs(reopened) == ["G4"]
    assert _undone(ed, source)                     # the run of Backspace: one step
    assert _paragraphs(ed) == ["G2", "x", "G2"]
    ed.close()
    reopened.close()


# --------------------------------------------------------------------------- #
# Typing                                                                       #
# --------------------------------------------------------------------------- #

def test_what_is_typed_among_the_pictures_goes_under_the_group():
    ed = _editor()
    _caret(ed, _group(ed).start + 2)
    QTest.keyClicks(ed, "Words")
    assert _paragraphs(ed) == ["Before.", "G4", "Words", "After."]
    assert _markdown(ed) == _text("Before.", ONE, TWO, THREE, FOUR, "Words", "After.")
    assert _undone(ed)                             # the paragraph and its words: one step
    ed.close()


def test_what_is_typed_before_the_first_picture_goes_above_the_group():
    ed = _editor()
    _caret(ed, _group(ed).start)
    QTest.keyClicks(ed, "Up")
    assert _paragraphs(ed) == ["Before.", "Up", "G4", "After."]
    assert _undone(ed)
    ed.close()


def test_return_among_the_pictures_opens_a_paragraph_under_the_group():
    ed = _editor()
    _caret(ed, _group(ed).start + 1)
    QTest.keyClick(ed, Qt.Key.Key_Return)
    assert _paragraphs(ed) == ["Before.", "G4", "", "After."]
    assert ed.textCursor().block().text() == ""
    assert _markdown(ed) == FOUR_PICTURES          # an empty line is saved as nothing
    ed.close()


def test_tab_among_the_pictures_does_nothing():
    ed = _editor()
    _caret(ed, _group(ed).start)
    QTest.keyClick(ed, Qt.Key.Key_Tab)
    assert _markdown(ed) == FOUR_PICTURES and not ed.document().isUndoAvailable()
    ed.close()


def test_composed_text_among_the_pictures_goes_under_the_group():
    """An input method (a dead key, Chinese, Japanese or Korean) commits
    its text where typed letters go."""
    ed = _editor()
    _caret(ed, _group(ed).start + 1)
    event = QInputMethodEvent()
    event.setCommitString("é")
    QApplication.sendEvent(ed, event)
    assert _paragraphs(ed) == ["Before.", "G4", "é", "After."]
    ed.close()


def test_typing_over_a_selection_that_ends_in_a_group_keeps_the_rest_a_group():
    ed = _editor()
    group = _group(ed)
    cursor = ed.textCursor()
    cursor.setPosition(3)                          # "Bef|ore."
    cursor.setPosition(group.start + 2, QTextCursor.MoveMode.KeepAnchor)
    ed.setTextCursor(cursor)
    QTest.keyClicks(ed, "x")
    assert _paragraphs(ed) == ["Befx", "G2", "After."]
    assert _markdown(ed) == _text("Befx", THREE, FOUR, "After.")
    assert _undone(ed)
    ed.close()


def _select(ed, start: str, end: str) -> None:
    """Select from where the words ``start`` begin to where ``end`` begin."""
    cursor = ed.textCursor()
    cursor.setPosition(ed.document().find(start).selectionStart())
    cursor.setPosition(ed.document().find(end).selectionStart(), QTextCursor.MoveMode.KeepAnchor)
    ed.setTextCursor(cursor)


def test_a_selection_from_words_into_a_caption_keeps_its_picture():
    """STANDUP's editor keeps it (PG1C 4e): Backspace, Delete, a letter or
    Return over words and the start of a caption take what is selected,
    the picture stays with the rest of its caption, one Undo brings all
    back. Return leaves the caret at the caption."""
    source = _text("Before words.", ONE, "*Cap one words*", "After.")
    for key, letter, saved, at in ((Qt.Key.Key_Backspace, "", "Before", "Before "),
                                   (Qt.Key.Key_Delete, "", "Before", "Before "),
                                   (None, "Z", "Before Z", "Before Z"),
                                   (Qt.Key.Key_Return, "", "Before", "")):
        ed = _editor(source)
        _select(ed, "words.", "one words")
        if key is None:
            QTest.keyClicks(ed, letter)
        else:
            QTest.keyClick(ed, key)
        assert _markdown(ed) == _text(saved, ONE, "*one words*", "After."), key
        caret = ed.textCursor()
        assert (caret.block().text(), caret.positionInBlock()) == (
            (at, len(at)) if at else ("one words", 0))
        assert _undone(ed, source)
        ed.close()


def test_words_typed_under_a_lone_picture_between_pictures_stay_words():
    """Typing never makes a group: an italic line being written under a
    picture is not taken for its caption while it is typed (it is one when
    the file is opened again)."""
    ed = _editor(_text(ONE, "x", THREE))
    assert _paragraphs(ed) == ["￼", "x", "￼"]
    words = ed.document().findBlockByNumber(1)
    cursor = ed.textCursor()
    cursor.setPosition(words.position())
    cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
    ed.setTextCursor(cursor)
    ed.toggle_italic()
    assert _paragraphs(ed) == ["\ufffc", "x", "\ufffc"]
    _caret(ed, words.position() + 1)
    QTest.keyClicks(ed, "yz")
    QTest.keyClick(ed, Qt.Key.Key_Backspace)
    assert _paragraphs(ed) == ["\ufffc", "xy", "\ufffc"]
    reopened = _editor(_markdown(ed))
    assert _paragraphs(reopened) == ["G2"]
    reopened.close()
    ed.close()


# --------------------------------------------------------------------------- #
# Paste, cut, and a command                                                    #
# --------------------------------------------------------------------------- #

def test_a_picture_pasted_under_a_group_joins_it():
    """SPEC D9: a picture directly below a group belongs to it."""
    ed = _editor(_text(ONE, TWO, "After."))
    other = _editor(_text("Words.", THREE))
    cursor = other.textCursor()
    cursor.setPosition(other.document().lastBlock().position())
    cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
    other.setTextCursor(cursor)
    mime = other.createMimeDataFromSelection()
    _caret(ed, _group(ed).end)
    QTest.keyClick(ed, Qt.Key.Key_Return)          # an empty paragraph under the group
    ed.insertFromMimeData(mime)
    assert _paragraphs(ed) == ["G3", "After."]
    assert _markdown(ed) == _text(ONE, TWO, THREE, "After.")
    ed.undo()                                      # the paste: one step
    assert _paragraphs(ed) == ["G2", "", "After."]
    ed.close()
    other.close()


def test_cutting_pictures_out_of_a_group_and_pasting_them_keeps_their_captions():
    ed = _editor()
    group = _group(ed)
    cursor = ed.textCursor()
    cursor.setPosition(group.start + 1)
    cursor.setPosition(group.start + 3, QTextCursor.MoveMode.KeepAnchor)   # two and three
    ed.setTextCursor(cursor)
    ed.cut()
    assert _markdown(ed) == _text("Before.", ONE, FOUR, "After.")
    _caret(ed, ed.document().characterCount() - 1)
    QTest.keyClick(ed, Qt.Key.Key_Return)
    ed.paste_from_clipboard()
    assert _markdown(ed) == _text("Before.", ONE, FOUR, "After.", TWO, THREE)
    assert _paragraphs(ed) == ["Before.", "G2", "After.", "G2"]
    ed.close()


def test_a_cut_or_a_paste_from_words_into_a_caption_keeps_its_picture():
    """As the keys do (PG1C 4f): Cut puts the selection on the clipboard
    as it is written and takes what is selected, the picture stays; a
    paste over it lands where the selection started. One Undo each."""
    source = _text("Before words.", ONE, "*Cap one words*", "After.")
    ed = _editor(source)
    _select(ed, "words.", "one words")
    ed.cut()
    assert _markdown(ed) == _text("Before", ONE, "*one words*", "After.")
    clipboard = QApplication.clipboard().mimeData()
    assert clipboard.text().startswith("words.") and clipboard.text().endswith("Cap ")
    assert _undone(ed, source)
    _select(ed, "words.", "one words")
    mime = QMimeData()
    mime.setText("New")
    QApplication.clipboard().setMimeData(mime)
    ed.paste_from_clipboard()
    assert _markdown(ed) == _text("Before New", ONE, "*one words*", "After.")
    assert _undone(ed, source)
    ed.close()


def test_a_command_moves_a_picture_as_one_named_step():
    """What the commands of Format > Photo build on: the group's units in
    another order, written in one step that Undo names."""
    ed = _editor()
    group = _group(ed)
    units = list(group.units)
    units.insert(3, units.pop(0))                  # "one" to the end
    with ed.undo_step("Move Photo"):
        photo_group_edit.set_units(ed.textCursor(), group, units)
    assert _markdown(ed) == _text("Before.", TWO, THREE, FOUR, ONE, "After.")
    assert [unit.number for unit in _group(ed).units] == [unit.number for unit in units]
    assert ed.undo_name() == "Move Photo"
    assert _undone(ed)
    ed.close()


def test_a_command_takes_a_picture_out_of_its_group_as_one_step():
    """Removing one of two leaves the other as its own paragraphs; a unit
    written as paragraphs elsewhere stands alone."""
    ed = _editor(_text("Before.", ONE, TWO, "After."))
    group = _group(ed)
    with ed.undo_step("Remove Photo"):
        photo_group_edit.set_units(ed.textCursor(), group, group.units[1:])
    assert _paragraphs(ed) == ["Before.", "￼", "Cap two", "After."]
    assert _markdown(ed) == _text("Before.", TWO, "After.")
    assert _undone(ed, _text("Before.", ONE, TWO, "After."))
    ed.close()


def test_a_command_gives_a_picture_another_caption_as_one_step():
    ed = _editor()
    group = _group(ed)
    with ed.undo_step("Caption"):
        photo_group_edit.set_unit(ed.textCursor(), group.start,
                                  group.units[0].captioned("Loch Ossian · Foto: Anna Berg"))
    assert _markdown(ed) == _text("Before.", ONE, "*Loch Ossian · Foto: Anna Berg*", TWO, THREE,
                                  FOUR, "After.")
    assert _undone(ed)
    ed.close()


def test_a_picture_inserted_among_the_pictures_is_one_of_the_group():
    """A picture a command puts where the caret is (Insert > Picture, a
    dropped file): among a group's pictures it takes that place."""
    ed = _editor(_text(ONE, TWO, "After."))
    _caret(ed, _group(ed).start + 1)
    picture = QTextImageFormat()
    picture.setName("portrait.png")
    picture.setProperty(QTextImageFormat.Property.ImageAltText, "three")
    with ed.undo_step("Insert Picture"):
        ed.textCursor().insertImage(picture)
    assert _paragraphs(ed) == ["G3", "After."]
    assert _markdown(ed) == _text(ONE, THREE, TWO, "After.")
    assert _undone(ed, _text(ONE, TWO, "After."))
    ed.close()


def test_a_badge_stands_on_a_group_picture_where_it_is_drawn():
    """The badges of media_badge (a picture still uploading) go by where
    an object stands: a group's picture stands where its layout draws it,
    a stack's lower picture included."""
    import media_badge
    ed = _editor(_text("Text.", "![a](portrait.png)", "![b](landscape.png)", "![c](square.png)"))
    drawn = [box for _p, _n, box in photo_group_view.picture_boxes(ed.document())]
    seen = dict(media_badge.objects_in_sight(ed))
    assert [seen[name].center().toPoint() for name in ("portrait.png", "landscape.png", "square.png")] \
        == [box.center().toPoint() for box in drawn]
    ed.close()
