# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Split Group Here: the empty line it opens between two pictures of a
group, while it waits for words (EDITING 2.5).

The first words written there join the split's step, so one Undo takes
both back. When the caret leaves the line with nothing written in it,
the split is taken back as if it had never been, and the caret stays
with what it went to: a picture by its number (photo_group.Unit.number),
since taking the split back moves the pictures below it; anything else
as it stands. A change of the document off the line (Undo, another
command) ends the wait: the line is then the writer's.
"""

from __future__ import annotations

from typing import Optional, Tuple

from PySide6.QtGui import QTextCursor, QTextDocument

import photo_group_actions as actions
from i18n import N_, _
from photo_group_actions import Picture

SPLIT_STEP = N_("Split Group")


class SplitLine:
    """The line a split of one editor's group opened, if it waits."""

    def __init__(self, photos) -> None:
        self._photos = photos
        # The cursor at the line, and Qt's count after the split.
        self._line: Optional[Tuple[QTextCursor, int]] = None

    def open(self, picture: Picture) -> None:
        """Split ``picture``'s group after it, the caret on a new line."""
        photos = self._photos
        if not picture.grouped or picture.index == picture.count - 1:
            photos.say(_("The group already ends after the last photo"))
            return
        ed = photos.editor
        doc = ed.document()
        before = doc.availableUndoSteps()
        with photos.quietly(), photos.writing(), ed.undo_step(SPLIT_STEP):
            at = actions.split_after(QTextCursor(doc), picture)
        caret = ed.textCursor()
        caret.setPosition(at)
        ed.setTextCursor(caret)
        # The first words written there are part of the same step.
        ed.type_into_last_step()
        self._line = (QTextCursor(caret), doc.availableUndoSteps())
        photos.steps.keep(before, SPLIT_STEP, picture.position, at)
        photos.changed.emit()
        photos.say(_("Group split. Write the text between the photos; if the line stays "
                     "empty, they belong together again."))

    def caret_moved(self) -> None:
        if self._line is not None and (self._line[0].block()
                                       != self._photos.editor.textCursor().block()):
            self._left()

    def outside_change(self, position: int) -> None:
        """The document changed at ``position``, not by a photo command."""
        if self._line is not None and (self._photos.editor.document().findBlock(position)
                                       != self._line[0].block()):
            self._line = None

    def _left(self) -> None:
        """The caret left the line: with nothing written in it, the split
        is taken back."""
        line, after = self._line
        self._line = None
        photos = self._photos
        ed = photos.editor
        doc = ed.document()
        if doc.availableUndoSteps() != after or line.block().length() > 1:
            return
        went_to = photos.current()
        number = went_to.unit.number if went_to is not None and went_to.grouped else 0
        caret = QTextCursor(ed.textCursor())
        with photos.writing():
            doc.undo()
            doc.clearUndoRedoStacks(QTextDocument.Stacks.RedoStack)
        photos.steps.drop(after)
        found = actions.picture_numbered(doc, number)
        if found is not None:
            photos.select(found.position)
        else:
            with photos.quietly():
                ed.setTextCursor(caret)
        photos.say(_("The line stayed empty, the photos belong together again"))
