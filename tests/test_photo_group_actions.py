# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What an author does to a picture, as the Markdown saved after it.

Each action of photo_group_actions is run as the editor runs a command
(inside one HtmlEditor.undo_step): the test checks the exact Markdown,
and that one Undo brings the file back as it was.
"""

from __future__ import annotations

from PySide6.QtGui import QTextCursor

import photo_group
import photo_group_actions as actions
from tests.test_photo_group_editing import (
    FOUR, FOUR_PICTURES, ONE, THREE, TWO, _editor, _markdown, _text, app,  # noqa: F401
)

TWO_GROUPS = _text(ONE, TWO, "Between.", THREE, FOUR, "After.")
ALONE = _text("Before.", ONE, TWO, "Middle.", THREE, "After.")


def _after(action, markdown: str = FOUR_PICTURES) -> str:
    """The Markdown after ``action(doc, cursor)`` as one step; one Undo
    must bring ``markdown`` back and leave nothing to undo."""
    ed = _editor(markdown)
    doc = ed.document()
    with ed.undo_step("Step"):
        action(doc, QTextCursor(doc))
    written = _markdown(ed)
    ed.undo()
    assert _markdown(ed) == markdown and not doc.isUndoAvailable()
    ed.close()
    return written


def _picture(doc, number: int = 0) -> actions.Picture:
    return actions.picture_at(doc, photo_group.groups_in(doc)[0].start + number)


def _alone(doc) -> actions.Picture:
    return actions.picture_at(doc, doc.findBlockByNumber(3).position())


def test_a_picture_moves_to_another_place_with_its_caption():
    assert _after(lambda doc, c: actions.move(c, _picture(doc, 1), 3)) == _text(
        "Before.", ONE, THREE, FOUR, TWO, "After.")


def test_a_picture_taken_out_stands_alone_under_the_next_paragraph():
    def out(doc, cursor):
        picture = _picture(doc, 1)
        actions.relocate(cursor, doc, picture, actions.beyond(doc, picture, True))
    assert _after(out) == _text("Before.", ONE, THREE, FOUR, "After.", TWO)


def test_a_picture_taken_out_upwards_stands_above_the_paragraph_before():
    def out(doc, cursor):
        picture = _picture(doc, 1)
        actions.relocate(cursor, doc, picture, actions.beyond(doc, picture, False))
    assert _after(out) == _text(TWO, "Before.", ONE, THREE, FOUR, "After.")


def test_a_picture_alone_goes_into_the_group_above_and_can_be_removed():
    def into(doc, cursor):
        actions.relocate(cursor, doc, _alone(doc), actions.beyond(doc, _alone(doc), False))
    assert _after(into, ALONE) == _text("Before.", ONE, TWO, THREE, "Middle.", "After.")
    assert _after(lambda doc, c: actions.remove(c, doc, _alone(doc)), ALONE) == _text(
        "Before.", ONE, TWO, "Middle.", "After.")


def test_a_picture_removed_takes_its_caption_with_it():
    assert _after(lambda doc, c: actions.remove(c, doc, _picture(doc, 1))) == _text(
        "Before.", ONE, THREE, FOUR, "After.")


def test_large_writes_wide_on_the_address_and_nothing_else():
    assert _after(lambda doc, c: actions.set_wide(c, _picture(doc, 2), True)) == _text(
        "Before.", ONE, TWO, "![three](portrait.png#wide)", FOUR, "After.")


def test_a_split_left_without_words_saves_the_file_as_it_was():
    assert _after(lambda doc, c: actions.split_after(c, _picture(doc, 1))) == FOUR_PICTURES


def test_two_groups_joined_are_one_with_the_words_under_them():
    assert _after(lambda doc, c: actions.join(c, doc, _picture(doc), True), TWO_GROUPS) == _text(
        ONE, TWO, THREE, FOUR, "Between.", "After.")


def test_a_caption_and_a_description_are_written_on_their_picture():
    assert _after(lambda doc, c: actions.set_caption(c, _picture(doc), "Der Steg")) == _text(
        "Before.", ONE, "*Der Steg*", TWO, THREE, FOUR, "After.")
    assert _after(lambda doc, c: actions.set_description(c, _picture(doc), "Ein Steg")) == _text(
        "Before.", "![Ein Steg](photo.png)", TWO, THREE, FOUR, "After.")
