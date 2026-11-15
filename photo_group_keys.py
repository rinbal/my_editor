# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The keys of the pictures of a document (EDITING.md 2.3 of the photo
grid design).

- The arrows go from picture to picture of a group, each one selected in
  turn, and from its first or last one into the words around it; from
  the words they go into a group with its nearest picture selected.
- Control-Command-arrows on a Mac, Ctrl+Shift+arrows elsewhere, move the
  picture at the caret one place (the keys of Format > Photo's moves; a
  text field takes Ctrl+Shift+arrows for selecting words before any
  menu's shortcut, so they are answered here first).
- Return on a selected picture writes its caption, Backspace and Delete
  remove it, and the Menu key or Shift+F10 opens its menu. A letter typed
  on a selected picture of a group changes nothing.

Every other key goes past at once: nothing here costs time while typing.
"""

from __future__ import annotations

import sys
from typing import Optional

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QContextMenuEvent, QTextCursor

import photo_group
from photo_group_actions import Picture
from photo_group_menu import CAPTION, DOWN, LEFT, REMOVE, RIGHT, UP

_MOVE_HELD = (Qt.KeyboardModifier.MetaModifier | Qt.KeyboardModifier.ControlModifier
              if sys.platform == "darwin"
              else Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
_ARROWS = {Qt.Key.Key_Left: LEFT, Qt.Key.Key_Right: RIGHT, Qt.Key.Key_Up: UP,
           Qt.Key.Key_Down: DOWN}
# The keys looked at; every other one goes past at once.
_KEYS = frozenset((*_ARROWS, Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Backspace,
                   Qt.Key.Key_Delete, Qt.Key.Key_Escape, Qt.Key.Key_Menu, Qt.Key.Key_F10))
_COMMAND_HELD = Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier


class PhotoKeys:
    """The keys of one editor's pictures (HtmlEditor.photos.keys)."""

    def __init__(self, photos) -> None:
        self._photos = photos
        self._editor = photos.editor

    def key_press(self, e) -> bool:
        """A key the pictures answer (True), before the editor's own."""
        photos = self._photos
        if photos.pointer.key_press(e):
            return True
        key = e.key()
        if key not in _KEYS:
            # A letter on a selected picture of a group: nothing (Return
            # writes its caption, Space shows it).
            return (key != Qt.Key.Key_Space and e.text().isprintable() and bool(e.text())
                    and not e.modifiers() & _COMMAND_HELD and self._group_picture_selected())
        held = e.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
        if key in _ARROWS and held == _MOVE_HELD:
            if photos.current() is None:
                return False
            photos.run(_ARROWS[key])
            return True
        if key == Qt.Key.Key_Menu or (key == Qt.Key.Key_F10
                                      and held == Qt.KeyboardModifier.ShiftModifier):
            return self.open_menu()
        if held != Qt.KeyboardModifier.NoModifier:
            return False
        cursor = self._editor.textCursor()
        selected = cursor.selectionEnd() - cursor.selectionStart() == 1
        picture = photos.current() if selected else None
        if key in _ARROWS:
            return self._arrow(cursor, picture, key)
        if picture is None:
            return False
        if key in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete):
            photos.run(REMOVE)
            return True
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            photos.run(CAPTION)
            return True
        return False

    def _group_picture_selected(self) -> bool:
        cursor = self._editor.textCursor()
        return (cursor.selectionEnd() - cursor.selectionStart() == 1
                and photo_group.is_group_block(cursor.block()))

    def _arrow(self, cursor: QTextCursor, picture: Optional[Picture], key) -> bool:
        """Plain arrows from picture to picture of a group, from its edges
        into the words, and from the words into a group."""
        photos = self._photos
        doc = self._editor.document()
        backward = key in (Qt.Key.Key_Left, Qt.Key.Key_Up)
        if picture is not None and picture.grouped:
            to = picture.index + (-1 if backward else 1)
            if 0 <= to < picture.count:
                photos.select(picture.first + to, quiet=False)
                return True
            block = doc.findBlock(picture.first)
            out = block.previous() if backward else block.next()
            if out.isValid():
                cursor.setPosition(out.position() + (out.length() - 1 if backward else 0))
                self._editor.setTextCursor(cursor)
            return True
        if cursor.hasSelection():
            return False
        block = cursor.block()
        near = block.previous() if backward else block.next()
        if (photo_group.is_group_block(block) or not near.isValid()
                or not photo_group.is_group_block(near) or not self._at_edge(cursor, key)):
            return False
        count = len(photo_group.block_units(near))
        if not count:
            return False
        photos.select(near.position() + (count - 1 if backward else 0), quiet=False)
        return True

    @staticmethod
    def _at_edge(cursor: QTextCursor, key) -> bool:
        """Whether ``key`` leaves the caret's paragraph: Left at its
        start, Right at its end, Up on its first line, Down on its last."""
        if key == Qt.Key.Key_Left:
            return cursor.atBlockStart()
        if key == Qt.Key.Key_Right:
            return cursor.atBlockEnd()
        layout = cursor.block().layout()
        line = layout.lineForTextPosition(cursor.positionInBlock()) if layout else None
        if line is None or not line.isValid():
            return False
        return line.lineNumber() == (0 if key == Qt.Key.Key_Up else layout.lineCount() - 1)

    def open_menu(self, at: Optional[QPointF] = None) -> bool:
        """The picture's menu at ``at`` (the viewport's), by default on the
        picture; False without a picture."""
        photos = self._photos
        picture = photos.current()
        box = photos.box_of(picture) if picture is not None else None
        if box is None:
            return False
        point = (at if at is not None else photos.to_view(box).center()).toPoint()
        ed = self._editor
        ed.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Reason.Keyboard, point,
                                              ed.viewport().mapToGlobal(point)))
        return True
