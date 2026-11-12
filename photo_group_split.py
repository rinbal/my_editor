# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Split Group Here: the empty line it opens between two pictures of a
group, while it waits for words (EDITING 2.5), with a placeholder that
says what it is for.

The split is a step of Undo of its own and the words written there are
typing of their own, as in STANDUP's editor (PG1C session 4d): Undo takes
the words back first, and the line waits again, then the split. When the
caret leaves the line with nothing in it while the split is the last
step, the split is taken back as if it had never been, and the caret
stays with what it went to: a picture by its number
(photo_group.Unit.number), since taking the split back moves the
pictures below it; anything else as it stands. A change of the document
off the line (another edit, another command) ends the wait: the line is
then the writer's.
"""

from __future__ import annotations

from typing import Optional, Tuple

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QPalette, QTextCursor, QTextDocument

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
        self._line = (QTextCursor(caret), doc.availableUndoSteps())
        photos.steps.keep(before, SPLIT_STEP, picture.position, at)
        photos.changed.emit()
        photos.say(_("Group split. Write the text between the photos; if the line stays "
                     "empty, they belong together again."))

    def caret_moved(self) -> None:
        if self._line is not None and (self._line[0].block()
                                       != self._photos.editor.textCursor().block()):
            self._left()

    def paint(self, painter) -> None:
        """The placeholder in the line while it is empty, after the caret."""
        if self._line is None or self._line[0].block().length() > 1:
            return
        ed = self._photos.editor
        caret = ed.cursorRect(QTextCursor(self._line[0].block()))
        painter.save()
        painter.setFont(ed.font())
        painter.setPen(ed.palette().color(QPalette.ColorRole.PlaceholderText))
        painter.drawText(QRectF(caret.right() + 2, caret.top(),
                                ed.viewport().width() - caret.right() - 2, caret.height()),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         _("Write something here, then the group is split"))
        painter.restore()

    def came_back(self) -> None:
        """Undo or Redo of a step that was no photo command's: one that took
        the words of the waiting line back leaves the caret there, and the
        line waits again (STANDUP's editor does the same)."""
        if self._line is None or self._line[0].block().length() > 1:
            return
        ed = self._photos.editor
        caret = ed.textCursor()
        caret.setPosition(self._line[0].block().position())
        with self._photos.quietly():
            ed.setTextCursor(caret)

    def outside_change(self, position: int) -> None:
        """The document changed at ``position``, not by a photo command."""
        if self._line is not None and (self._photos.editor.document().findBlock(position)
                                       != self._line[0].block()):
            self._line = None

    def _left(self) -> None:
        """The caret left the line: with nothing in it while the split is
        the last step, the split is taken back. A line with words goes on
        waiting, since an Undo of them empties it again."""
        line, after = self._line
        if line.block().length() > 1:
            return
        self._line = None
        photos = self._photos
        ed = photos.editor
        doc = ed.document()
        if doc.availableUndoSteps() != after:
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
