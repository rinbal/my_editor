# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Editing the pictures of a document where they stand: the commands.

Every picture of a photo group, and every picture that stands alone in a
paragraph of its own, can be moved, taken out of its group or put into
one, shown large, given a caption and a description, cropped and removed
(EDITING.md 2 and 4 of the photo grid design). Each command is here once,
by its name (run), and the Format > Photo menu, the context menu, the
chips on a picture, the keys and a drag all come to it:

- **current** is the picture a command acts on: the one selected, or in a
  group the one at the caret;
- **state** says what each command would do to it now (whether it can,
  and what it is called: "Move Up" in a narrow column is "Into the Group
  Above" on a picture that stands alone), for the menus;
- **run** does one, as one named step of Undo (photo_group_actions),
  selects the picture where it stands then and says so to a screen
  reader; Undo and Redo select it again where they put it (came_back).

The pointer's part (chips, the bar over a group, dragging) is
photo_group_pointer's, the keys' photo_group_keys', and what the menus
show photo_group_menu's; this module is what each of them asks for.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Callable, Iterator, Optional, Sequence

from PySide6.QtCore import QObject, QPoint, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QKeySequence, QTextCursor, QTextDocument
from PySide6.QtWidgets import QToolTip

import accessibility_display
import announce
import media_badge
import photo_caption
import photo_group
import photo_group_actions as actions
import photo_group_menu
import photo_group_view
from doc_walk import OBJECT_REPLACEMENT
from i18n import N_, _, ngettext
from photo_group_actions import BESIDE, GAP, SLOT, Picture, Place
from photo_group_fields import PhotoFields
from photo_group_keys import PhotoKeys
from photo_group_menu import (
    ADD, CAPTION, COMMANDS, CROP, DESCRIPTION, DOWN, FIRST, JOIN_ABOVE, JOIN_BELOW, LARGE, LAST,
    LEFT, MOVE_MODE, REMOVE, RIGHT, SPLIT, TAKE_OUT, UP, PhotoState,
)
from photo_group_pointer import PhotoPointer
from photo_group_split import SplitLine
from photo_group_steps import PhotoSteps

# What each step of Undo is called (Edit > Undo "Move Photo").
MOVE_STEP = N_("Move Photo")
_STEPS = {LARGE: (N_("Show Large"), N_("Show Normal")), "wide": (N_("Wide"), N_("Text Width")),
          JOIN_ABOVE: N_("Join Groups"), REMOVE: N_("Remove Photo"),
          CAPTION: N_("Caption"), DESCRIPTION: N_("Photo Description"), CROP: N_("Crop Photo")}

# How long a note beside a chip stays: long enough to read (EDITING.md 9).
_NOTE_MS_PER_CHARACTER = 55
_NOTE_MS_LEAST = 2800


def _undo_keys() -> str:
    return QKeySequence(QKeySequence.StandardKey.Undo).toString(
        QKeySequence.SequenceFormat.NativeText)


def _quoted(words: str) -> str:
    """A paragraph's words to say, without its pictures; empty for none."""
    words = " ".join(words.replace(OBJECT_REPLACEMENT, " ").split())
    return words if len(words) <= 32 else words[:30].rstrip() + "…"


class PhotoEditing(QObject):
    """The photo commands of one editor (HtmlEditor.photos)."""

    # What the commands can do changed (another picture, or none).
    changed = Signal()
    # Photos to add at a Place: the files (None: ask for them).
    add_requested = Signal(object, object)
    # "Crop…" on the picture at a position.
    crop_requested = Signal(int)

    def __init__(self, editor) -> None:
        super().__init__(editor)
        self._editor = editor
        # While a command moves the caret: no "Photo 2 of 4" spoken for it
        # (the command says what happened, once).
        self.quiet = False
        self.steps = PhotoSteps(editor)
        self._context = None
        # While above 0: the document changes by a photo command (writing).
        self._own = 0
        self.pointer = PhotoPointer(editor, self)
        self.keys = PhotoKeys(self)
        self.fields = PhotoFields(self)
        self.split = SplitLine(self)
        editor.cursorPositionChanged.connect(self._caret_moved)
        editor.selectionChanged.connect(self._caret_moved)
        editor.document().contentsChange.connect(self._document_changed)

    # ------------------------------------------------------------------ #
    # The picture a command acts on                                       #
    # ------------------------------------------------------------------ #

    def current(self) -> Optional[Picture]:
        """The picture selected, or a group's at the caret; None for
        anything else (a picture alone counts only while it is selected)."""
        cursor = self._editor.textCursor()
        grouped = photo_group.is_group_block(cursor.block())
        if not grouped and cursor.selectionEnd() - cursor.selectionStart() != 1:
            return None
        if not self._editor._holds_structure():
            return None
        picture = actions.picture_of(self._editor.document(), cursor)
        if picture is not None and not picture.grouped and not cursor.hasSelection():
            return None
        return picture

    def picture_hit(self, point: QPointF, *, nearest: bool = False) -> Optional[Picture]:
        """The picture drawn at ``point`` (the document's coordinates);
        ``nearest``: in a group, the nearest (photo_group_view.picture_at)."""
        doc = self._editor.document()
        found = photo_group_view.picture_at(doc, point, nearest=nearest)
        if found is not None:
            return actions.picture_at(doc, found[0])
        block = doc.findBlock(max(0, doc.documentLayout().hitTest(
            point, Qt.HitTestAccuracy.FuzzyHit)))
        for near in (block, block.previous()):
            lone = actions.picture_at(doc, near.position()) if near.isValid() else None
            box = self.box_of(lone) if lone is not None else None
            if box is not None and box.contains(point):
                return lone
        return None

    def box_of(self, picture: Picture) -> Optional[QRectF]:
        """Where ``picture`` is drawn, in the document's coordinates."""
        doc = self._editor.document()
        if picture.grouped:
            return photo_group_view.picture_box(doc, picture.position)
        block = doc.findBlock(picture.position)
        image = block.begin().fragment().charFormat().toImageFormat()
        return media_badge.object_box(doc.documentLayout(), block, 0, image)

    def to_view(self, box: QRectF) -> QRectF:
        """``box`` in the viewport's coordinates."""
        return box.translated(-self._editor.horizontalScrollBar().value(),
                              -self._editor.verticalScrollBar().value())

    @contextmanager
    def quietly(self) -> Iterator[None]:
        """The caret moved inside without "Photo 2 of 4" spoken for it:
        what was done says so itself, once (a command, Undo)."""
        self.quiet = True
        try:
            yield
        finally:
            self.quiet = False

    def select(self, position: int, *, quiet: bool = True) -> None:
        """Select the picture at ``position``, the view scrolled to it only
        when it is not in view (a click on it moves nothing)."""
        cursor = self._editor.textCursor()
        cursor.setPosition(position)
        cursor.setPosition(position + 1, QTextCursor.MoveMode.KeepAnchor)
        # Qt scrolls to the caret, after the picture (in the next row).
        bar = self._editor.verticalScrollBar()
        kept = bar.value()
        if quiet:
            with self.quietly():
                self._editor.setTextCursor(cursor)
            self._caret_moved()
        else:
            self._editor.setTextCursor(cursor)
        bar.setValue(kept)
        picture = actions.picture_at(self._editor.document(), position)
        self._editor.show_document_rect(self.box_of(picture) if picture is not None else None)

    @property
    def editor(self):
        return self._editor

    def document(self) -> QTextDocument:
        return self._editor.document()

    def place_words(self, picture: Picture) -> str:
        return _("Photo {k} of {n}").format(k=picture.place, n=picture.count)

    # ------------------------------------------------------------------ #
    # What each command would do                                          #
    # ------------------------------------------------------------------ #

    def narrow(self) -> bool:
        return photo_group_view.is_narrow(self._editor.document())

    def hop(self, picture: Picture, down: bool) -> Optional[Place]:
        """Where Up (Down) takes ``picture``: a place among its group's
        pictures in a narrow column, else past the words around it."""
        if picture.grouped and self.narrow():
            to = picture.index + (1 if down else -1)
            if 0 <= to < picture.count:
                return Place(SLOT, picture.first, to)
        return actions.beyond(self._editor.document(), picture, down)

    def take_out_place(self, picture: Picture) -> Optional[Place]:
        """Where Take Out of Group puts ``picture``: alone under the next
        paragraph, or above the one before when none comes after."""
        doc = self._editor.document()
        for down in (True, False):
            place = actions.beyond(doc, picture, down)
            if place is not None and place.kind == GAP:
                return place
        return None

    def state(self) -> PhotoState:
        """What each command would do now (photo_group_menu.state_of)."""
        return photo_group_menu.state_of(self)

    # ------------------------------------------------------------------ #
    # Running a command                                                    #
    # ------------------------------------------------------------------ #

    def run(self, name: str) -> None:
        """Do the command ``name`` (one of COMMANDS) to the current picture
        (Add Photos… with none: a new group at the caret)."""
        # A field open meanwhile (a menu-bar click leaves the focus in it)
        # writes what it holds first.
        self.fields.commit()
        picture = self.current()
        if picture is None and name == ADD and self.free_place() is not None:
            self.add_requested.emit(self.free_place(), None)
            return
        if picture is None or name not in COMMANDS:
            return
        self.pointer.stop()
        if name in (LEFT, RIGHT, FIRST, LAST):
            self._move_in_group(picture, name)
        elif name in (UP, DOWN):
            self._move_by_hop(picture, name == DOWN)
        elif name == TAKE_OUT:
            place = self.take_out_place(picture)
            if place is not None:
                self.relocate(picture, place)
        elif name == LARGE:
            self._toggle_large(picture)
        elif name == SPLIT:
            self.split.open(picture)
        elif name in (JOIN_ABOVE, JOIN_BELOW):
            self._join(picture, name == JOIN_BELOW)
        elif name == REMOVE:
            self._remove(picture)
        elif name in (CAPTION, DESCRIPTION):
            self.fields.open(picture, name)
        elif name == CROP:
            self.crop_requested.emit(picture.position)
        elif name == ADD:
            self.add_requested.emit(self.adding_place(picture), None)
        elif name == MOVE_MODE:
            self.pointer.pick_up(picture)

    def _act(self, step: str, start: int, change: Callable[[QTextCursor], Optional[int]]
             ) -> Optional[Picture]:
        """``change`` (given a cursor to write with; it returns where the
        picture is then, or None) as one step called ``step``; the
        picture selected there. ``start`` is where the picture was (where
        Undo selects it again). The picture as it stands then."""
        ed = self._editor
        doc = ed.document()
        before = doc.availableUndoSteps()
        # Quietly: the caret moves with the change; what was done says so.
        with self.quietly(), self.writing(), ed.undo_step(step):
            at = change(QTextCursor(doc))
            if at is not None:
                # Inside the step: the group written anew around it when
                # the step ends keeps the caret before this picture.
                caret = ed.textCursor()
                caret.setPosition(at)
                ed.setTextCursor(caret)
        if doc.availableUndoSteps() == before:
            return None
        found = None
        if at is not None:
            position = ed.textCursor().position()
            self.select(position)
            found = actions.picture_at(doc, position)
        self.steps.keep(before, step, start, found.position if found is not None else -1)
        self.changed.emit()
        return found

    @contextmanager
    def writing(self) -> Iterator[None]:
        """What changes in the document meanwhile, a photo command changes."""
        self._own += 1
        try:
            yield
        finally:
            self._own -= 1

    def _document_changed(self, position: int, _removed: int, _added: int) -> None:
        """A change no photo command made (typing, Undo, another command):
        what outlives one event stops, since it would act on a document it
        no longer matches (move mode, a drag, a field); the split's line
        waits on only for words written in it."""
        if self._own:
            return
        self.pointer.stop(cancelled=True)
        self.fields.close()
        self.split.outside_change(position)

    def _move_in_group(self, picture: Picture, name: str) -> None:
        to = {LEFT: picture.index - 1, RIGHT: picture.index + 1, FIRST: 0,
              LAST: picture.count - 1}[name]
        if not picture.grouped or not 0 <= to < picture.count or to == picture.index:
            words = (_("{photo}, already first") if to < picture.index or name in (LEFT, FIRST)
                     else _("{photo}, already last"))
            self.say(words.format(photo=self.place_words(picture)))
            return
        moved = self._act(MOVE_STEP, picture.position, lambda cursor: actions.move(cursor, picture, to))
        if moved is not None:
            self.say(self.moved_words(moved, name))

    def moved_words(self, moved: Picture, how: str) -> str:
        """What a screen reader hears after a move inside a group."""
        narrow = self.narrow()
        words = {LEFT: _("{photo}, moved up") if narrow else _("{photo}, moved left"),
                 RIGHT: _("{photo}, moved down") if narrow else _("{photo}, moved right"),
                 UP: _("{photo}, moved up"), DOWN: _("{photo}, moved down"),
                 FIRST: _("{photo}, moved to the start"), LAST: _("{photo}, moved to the end")}
        return words.get(how, _("{photo}, moved")).format(photo=self.place_words(moved))

    def _move_by_hop(self, picture: Picture, down: bool) -> None:
        place = self.hop(picture, down)
        if place is None:
            self.say(_("No paragraph below it any more") if down
                     else _("No paragraph above it any more"))
        elif place.kind == SLOT and picture.grouped and place.at == picture.first:
            moved = self._act(MOVE_STEP, picture.position,
                              lambda cursor: actions.move(cursor, picture, place.index))
            if moved is not None:
                self.say(self.moved_words(moved, DOWN if down else UP))
        else:
            self.relocate(picture, place)

    def relocate(self, picture: Picture, place: Place) -> Optional[Picture]:
        """Take ``picture`` to ``place`` (out of its group, into another,
        beside a picture alone) as one step, and say where it went."""
        doc = self._editor.document()
        if place.kind == SLOT and picture.grouped and place.at == picture.first:
            moved = self._act(MOVE_STEP, picture.position,
                              lambda cursor: actions.move(cursor, picture, place.index))
            if moved is not None:
                how = RIGHT if place.index > picture.index else LEFT
                self.say(self.moved_words(moved, how))
            return moved
        moved = self._act(MOVE_STEP, picture.position,
                          lambda cursor: actions.relocate(cursor, doc, picture, place))
        if moved is None:
            return None
        if moved.grouped:
            self.say(_("Put into the group, {photo}").format(photo=self.place_words(moved)))
        else:
            # Quoted: the nearest paragraph with words in it.
            above = doc.findBlock(moved.first).previous()
            while above.isValid() and not _quoted(above.text()):
                above = above.previous()
            below = doc.findBlock(moved.end).next()
            while below.isValid() and not _quoted(below.text()):
                below = below.next()
            if above.isValid():
                self.say(_("The photo stands alone now, under “{words}”").format(
                    words=_quoted(above.text())))
            elif below.isValid():
                self.say(_("The photo stands alone now, above “{words}”").format(
                    words=_quoted(below.text())))
        return moved

    def _toggle_large(self, picture: Picture) -> None:
        doc = self._editor.document()
        if not picture.grouped:
            on = not picture.unit.wide
            step = _STEPS["wide"][0 if on else 1]
            if self._act(step, picture.position, lambda cursor: (
                    actions.set_wide(cursor, picture, on), picture.position)[1]) is not None:
                self.say(_("Photo wider than the text") if on else _("Photo as wide as the text"))
            return
        if not picture.unit.wide and photo_group_view.own_row(doc, picture.unit):
            self.note(picture, _("Already the whole width, it can’t get larger here. Readers "
                                 "can open any photo to see it large."))
            return
        on = not picture.unit.wide
        narrow = self.narrow()
        changed = self._act(_STEPS[LARGE][0 if on else 1], picture.position, lambda cursor: (
            actions.set_wide(cursor, picture, on), picture.position)[1])
        if changed is None:
            return
        photo = self.place_words(changed)
        if narrow:
            words = (_("Shown large: across the whole width on large screens. Here it stands "
                       "alone already.") if on
                     else _("Normal again: beside the others on large screens."))
            self.note(changed, words)
        else:
            self.say(_("{photo} is shown large now, across the whole width").format(photo=photo)
                     if on else _("{photo} shown normal again").format(photo=photo))

    def _join(self, picture: Picture, down: bool) -> None:
        doc = self._editor.document()
        joined = self._act(_STEPS[JOIN_ABOVE], picture.position,
                           lambda cursor: actions.join(cursor, doc, picture, down))
        if joined is not None:
            self.say(_("The photos are one group now, {count} photos").format(
                count=joined.count))

    def _remove(self, picture: Picture) -> None:
        doc = self._editor.document()
        ed = self._editor
        before = doc.availableUndoSteps()
        with self.quietly(), self.writing(), ed.undo_step(_STEPS[REMOVE]):
            at = actions.remove(QTextCursor(doc), doc, picture)
        if doc.availableUndoSteps() == before:
            return
        self.steps.keep(before, _STEPS[REMOVE], picture.position, -1)
        left = picture.count - 1 if picture.grouped else 0
        if at is not None and left >= 2:
            self.select(at)
        elif at is not None:
            caret = ed.textCursor()
            caret.setPosition(min(at, doc.characterCount() - 1))
            with self.quietly():
                ed.setTextCursor(caret)
            self._caret_moved()
        self.changed.emit()
        keys = _undo_keys()
        if left:
            self.say(ngettext("Photo removed, {count} photo remains. Undo with {keys}",
                              "Photo removed, {count} photos remain. Undo with {keys}",
                              left).format(count=left, keys=keys))
        else:
            self.say(_("Photo removed. Undo with {keys}").format(keys=keys))

    # ------------------------------------------------------------------ #
    # Adding, a new file, a caption, a description                        #
    # ------------------------------------------------------------------ #

    def adding_place(self, picture: Picture) -> Place:
        """Where photos added to ``picture``'s group go: at its end (after
        a picture alone, the two are a group then)."""
        if picture.grouped:
            return Place(SLOT, picture.first, picture.count)
        return Place(BESIDE, picture.position, before=False)

    def free_place(self) -> Optional[Place]:
        """Where photos added from the words go: a paragraph of their own
        before the empty paragraph at the caret (writing goes on under
        them), else after the caret's paragraph; None in a list, a quote,
        a table or code, or with words selected."""
        ed = self._editor
        cursor = ed.textCursor()
        block = cursor.block()
        doc = ed.document()
        if (cursor.hasSelection() or not ed._holds_structure()
                or doc.frameAt(block.position()).parentFrame() is not None
                or not (photo_group.is_plain_paragraph(block)
                        or block.blockFormat().headingLevel())):
            return None
        return actions.settle(doc, Place(GAP, block.position(), before=block.length() == 1), True)

    def insert(self, place: Place, pictures: Sequence, step: str) -> None:
        """Put new ``pictures`` (image formats) at ``place`` as one step
        called ``step``, the first of them selected."""
        doc = self._editor.document()
        if not pictures:
            return
        added = self._act(step, -1, lambda cursor: actions.insert(cursor, doc, place, pictures))
        if added is None:
            return
        count = len(pictures)
        words = ngettext("{count} photo added", "{count} photos added", count).format(count=count)
        if added.grouped:
            words += ". " + ngettext("The group has {n} photo now", "The group has {n} photos now",
                                     added.count).format(n=added.count)
        self.say(words)

    def set_address(self, position: int, name: str) -> None:
        """Show the file ``name`` (a crop) in place of the picture at
        ``position``, as one step."""
        picture = actions.picture_at(self._editor.document(), position)
        if picture is None:
            return
        if self._act(_STEPS[CROP], picture.position, lambda cursor: (
                actions.set_address(cursor, picture, name), picture.position)[1]) is not None:
            self.say(_("Photo cropped, as a new file"))

    def set_words(self, kind: str, position: int, words: str) -> None:
        """``words`` as the caption (``kind`` CAPTION) or the description
        of the picture at ``position``, as one step, when they are new."""
        picture = actions.picture_at(self._editor.document(), position)
        if picture is None:
            return
        old = (picture.unit.caption_line if kind == CAPTION
               else photo_caption.one_line(picture.unit.alt))
        if words == old:                # as the field shows it: nothing new
            return
        write = actions.set_caption if kind == CAPTION else actions.set_description
        if self._act(_STEPS[kind], position,
                     lambda cursor: (write(cursor, picture, words), position)[1]) is not None:
            self.say(_("Caption saved") if kind == CAPTION else _("Description saved"))

    # ------------------------------------------------------------------ #
    # Words for the person                                                 #
    # ------------------------------------------------------------------ #

    def say(self, words: str) -> None:
        announce.announce(self._editor, words)

    def note(self, picture: Picture, words: str) -> None:
        """``words`` beside ``picture``, long enough to read, and said."""
        box = self.box_of(picture)
        if box is not None:
            view = self.to_view(box)
            QToolTip.showText(self._editor.viewport().mapToGlobal(
                view.bottomRight().toPoint() - QPoint(24, 40)), words, self._editor.viewport(),
                view.toAlignedRect(), max(_NOTE_MS_LEAST, _NOTE_MS_PER_CHARACTER * len(words)))
        self.say(words)

    # ------------------------------------------------------------------ #
    # The editor's hooks                                                   #
    # ------------------------------------------------------------------ #

    def context_at(self, point: QPoint) -> None:
        """A right-click at ``point`` (the viewport's): the picture there
        selected, its commands in the menu."""
        at = QPointF(point) + QPointF(self._editor.horizontalScrollBar().value(),
                                      self._editor.verticalScrollBar().value())
        picture = self.picture_hit(at)
        if picture is not None:
            self.select(picture.position)

    def came_back(self, count: int, redo: bool) -> None:
        """Undo (``redo`` False) or Redo was done at Qt's count ``count``:
        a photo step it took back or brought again is selected where its
        picture stands, briefly lit, and named."""
        found = self.steps.came_back(count, redo)
        if found is None:
            self._caret_moved()
            return
        at, name = found
        doc = self._editor.document()
        if 0 <= at < doc.characterCount() and actions.picture_at(doc, at) is not None:
            self.select(at)
            if not accessibility_display.reduce_motion():
                self.pointer.flash(at)
        self.changed.emit()
        self.say((_("Redone: {action}") if redo else _("Undone: {action}")).format(
            action=_(name)))

    def focus_lost(self) -> None:
        self.pointer.stop(cancelled=True)

    def _caret_moved(self) -> None:
        if self.quiet:
            return
        self.pointer.caret_moved()
        self.split.caret_moved()
        cursor = self._editor.textCursor()
        context = None
        if photo_group.is_group_block(cursor.block()) or (
                cursor.selectionEnd() - cursor.selectionStart() == 1):
            picture = self.current()
            if picture is not None:
                context = (picture.position, picture.count, self._editor.document().revision())
        if context != self._context:
            self._context = context
            self.changed.emit()

    def paint(self, painter) -> None:
        self.split.paint(painter)
        self.pointer.paint(painter)
