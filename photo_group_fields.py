# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Writing a picture's caption or its description where the picture is.

Caption… (or Return on a selected picture, or a click on a caption of a
group) opens a line over the caption, in its italic, as wide as the
picture; Description… (or the ALT chip) a small card on the picture with
one sentence on what a description is for (EDITING.md 2.4 of the photo
grid design). photo_field.PictureField is the field; what is written is
one step of Undo (PhotoEditing.set_words).
"""

from __future__ import annotations

from typing import Optional

import shiboken6
from PySide6.QtCore import QRectF, Qt

import photo_group_view
from i18n import _
from photo_field import PictureField
import photo_group_actions as actions
from photo_group_actions import Picture
from photo_group_menu import CAPTION


class PhotoFields:
    """The field open on one editor's picture, if any."""

    def __init__(self, photos) -> None:
        self._photos = photos
        self._field: Optional[PictureField] = None

    def open(self, picture: Picture, kind: str) -> None:
        """The field to write ``picture``'s caption (``kind`` CAPTION) or
        its description in, where it is drawn."""
        photos = self._photos
        ed = photos.editor
        doc = ed.document()
        photos.select(picture.position)
        box = photos.box_of(picture)
        if box is None:
            return
        accent, ink, surface = photos.pointer.colors()
        colors = dict(ink=ink, surface=surface, accent=accent)
        font = doc.defaultFont()
        if kind == CAPTION:
            found = (photo_group_view.caption_box(doc, picture.position) if picture.grouped
                     else None)
            spot = found if found is not None else QRectF(box.left(), box.bottom() + 4,
                                                          box.width(), 24)
            font.setItalic(True)
            field = PictureField(
                ed.viewport(), name=_("Caption of {photo}").format(
                    photo=photos.place_words(picture)), text=picture.unit.caption_line,
                placeholder=_("Caption, e.g. “The jetty · Photo: Anna Berg”"), font=font,
                **colors)
        else:
            spot = QRectF(box.left() + 8, box.top() + 8, box.width(), 0)
            field = PictureField(
                ed.viewport(), name=_("Description"), text=picture.unit.alt, placeholder="",
                font=font, title=_("Description"),
                hint=_("Describe in one sentence what the photo shows. Whoever can’t see it "
                       "hears this sentence read out."), **colors)
        self.close()
        self._field = field
        field.committed.connect(lambda words, f=field: self._done(f, kind, picture, words))
        field.cancelled.connect(lambda f=field: self._done(f, kind, picture, None))
        field.place(photos.to_view(spot).toAlignedRect())

    def _done(self, field: PictureField, kind: str, picture: Picture,
              words: Optional[str]) -> None:
        if not shiboken6.isValid(self._photos):
            return          # the editor closed with the field open: nothing to write into
        back = field.line.hasFocus()
        if self._field is field:
            self._field = None
        field.hide()
        field.deleteLater()
        # The picture by its number: a change of the document closes the
        # field, but a command run meanwhile writes it first.
        found = actions.standing(self._photos.editor.document(), picture)
        if words is not None and found is not None:
            self._photos.set_words(kind, found.position, words)
        if back:
            # Return or Escape: back to the text, the picture selected.
            self._photos.editor.setFocus(Qt.FocusReason.OtherFocusReason)

    def close(self) -> None:
        """The field open (if any) goes, with nothing written."""
        if self._field is not None:
            self._field.cancel()

    def commit(self) -> None:
        """The field open (if any) goes, what it holds written."""
        if self._field is not None:
            self._field.commit()
