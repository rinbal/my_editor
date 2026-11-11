# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pictures edited with the pointer, and moved step by step with the keys.

What the pointer is over shows what can be done there (EDITING.md 2.2 and
2.3 of the photo grid design): a picture shows its chips (a handle, More,
ALT, Large), a group shows its bar above it with "+ Photo". A click on a
chip does what it says; a click on a picture selects it, a click on a
group picture's caption writes it in place.

A picture pressed and moved past the system's drag distance is lifted:
the document is not touched while it moves, the group it would land in
is drawn in its new order (photo_group_drag), and letting go is one step
of Undo; Escape, or letting go where it came from, leaves nothing behind.
Files dragged in from another app show the slot they would take the same
way. The handle clicked, or Format > Photo > Move With Arrow Keys, picks
a group's picture up for the arrow keys instead (move mode): Return puts
it down, Escape cancels.

Everything here is painted over the text, never put into the document
(photo_group_overlay). The commands themselves are photo_group_editing's.
"""

from __future__ import annotations

import time
from typing import List, Optional, Tuple

from PySide6.QtCore import QObject, QPoint, QPointF, QRectF, Qt, QTimer, QUrl
from PySide6.QtGui import (
    QAbstractTextDocumentLayout, QColor, QImage, QPainter, QPixmap, QTextDocument,
)
from PySide6.QtWidgets import QApplication

import accessibility_display
import file_paths
import image_safety
import photo_group
import photo_group_actions as actions
import photo_group_drag as drag
import photo_group_overlay as overlay
import photo_group_view
import theme
from i18n import _, ngettext
from photo_group_actions import Picture
from photo_group_menu import CAPTION, DESCRIPTION, LARGE

# The frame time of what moves on screen, and how far from the top or the
# bottom of the view a dragged picture scrolls the page (up to MAX_SCROLL
# pixels a frame; EDITING.md 2.3).
_FRAME_MS = 16
_EDGE = 28.0
_MAX_SCROLL = 18.0
_FLASH_S = 0.6
# The largest side of the copy that follows the pointer.
_GHOST = 160.0


_MODIFIERS = (Qt.Key.Key_Shift, Qt.Key.Key_Control, Qt.Key.Key_Alt, Qt.Key.Key_Meta,
              Qt.Key.Key_AltGr, Qt.Key.Key_CapsLock)


def _now() -> float:
    return time.monotonic()


class PhotoPointer(QObject):
    """The pointer's part of one editor's photo editing."""

    def __init__(self, editor, photos) -> None:
        super().__init__(editor)
        self._editor = editor
        self._photos = photos
        # The picture the pointer is over (or whose bar it is over), and
        # the chip under it.
        self._hover: Optional[Picture] = None
        self._hot: Optional[str] = None
        # A press on a picture, not yet a drag: (where, the picture, on its handle).
        self._pressed: Optional[Tuple[QPoint, Picture, bool]] = None
        # A picture or files being moved, and how: "drag", "keys", "files".
        self._session: Optional[drag.Session] = None
        self._how = ""
        self._point = QPointF()
        self._ghost: Optional[QPixmap] = None
        self._scroll = 0.0
        self._flash: Optional[Tuple[int, float]] = None
        self._ring = None
        self._frames = QTimer(self)
        self._frames.setInterval(_FRAME_MS)
        self._frames.timeout.connect(self._frame)
        editor.viewport().setMouseTracking(True)

    # ------------------------------------------------------------------ #
    # Where things are                                                    #
    # ------------------------------------------------------------------ #

    def _doc_point(self, point) -> QPointF:
        return QPointF(point) + QPointF(self._editor.horizontalScrollBar().value(),
                                        self._editor.verticalScrollBar().value())

    def _editable(self) -> bool:
        ed = self._editor
        return not ed.isReadOnly() and ed._holds_structure()

    def _chips(self, picture: Picture) -> Optional[overlay.Chips]:
        box = self._photos.box_of(picture)
        if box is None:
            return None
        missing = not picture.unit.description
        return overlay.chips_on(self._photos.to_view(box), _("More"),
                                _("No ALT") if missing else _("ALT"),
                                _("Large") if picture.grouped else _("Wide"))

    def _bar(self, picture: Picture) -> Optional[overlay.Bar]:
        if not picture.grouped:
            return None
        room = drag.room_of(self._editor.document(), picture.first)
        if room is None:
            return None
        origin, (width, height) = room
        return overlay.bar_over(self._photos.to_view(QRectF(origin.x(), origin.y(), width, height)),
                                _("Photo"))

    def _shown(self) -> Optional[Picture]:
        """The picture whose chips show: the one the pointer is over, else
        the one selected while the editor has the focus."""
        if self._session is not None:
            return None
        if self._hover is not None:
            return self._hover
        return self._photos.current() if self._editor.hasFocus() else None

    # ------------------------------------------------------------------ #
    # Hover, press, release                                               #
    # ------------------------------------------------------------------ #

    def mouse_move(self, e) -> bool:
        point = e.position()
        if self._session is not None and self._how == "drag":
            self._follow(point)
            return True
        if self._pressed is not None and e.buttons() & Qt.MouseButton.LeftButton:
            start, picture, _on_handle = self._pressed
            if (point.toPoint() - start).manhattanLength() >= QApplication.startDragDistance():
                self._pressed = None
                self._lift(picture, point)
            return True
        if e.buttons() != Qt.MouseButton.NoButton:
            return False
        self._hover_at(point)
        return False

    def _hover_at(self, point: QPointF) -> None:
        hover, hot = None, None
        if self._editable():
            kept = self._hover or self._photos.current()
            bar = self._bar(kept) if kept is not None else None
            if bar is not None and bar.box.contains(point):
                hover, hot = kept, bar.at(point)
            else:
                hover = self._photos.picture_hit(self._doc_point(point))
                chips = self._chips(hover) if hover is not None else None
                hot = chips.at(point) if chips is not None else None
        before = (self._hover.position if self._hover is not None else None, self._hot)
        self._hover, self._hot = hover, hot
        if before == (hover.position if hover is not None else None, hot):
            return
        self._editor.viewport().update()
        # The pointer's shape changes only over pictures (the text's own,
        # over a link too, stays the text's).
        viewport = self._editor.viewport()
        if hover is not None:
            viewport.setCursor(Qt.CursorShape.OpenHandCursor if hot == overlay.HANDLE
                               else Qt.CursorShape.ArrowCursor)
        elif before[0] is not None:
            viewport.setCursor(Qt.CursorShape.IBeamCursor)

    def leave(self) -> None:
        if self._hover is not None and self._session is None:
            self._hover, self._hot = None, None
            self._editor.viewport().update()

    def mouse_press(self, e) -> bool:
        if (e.button() != Qt.MouseButton.LeftButton or e.modifiers() != Qt.KeyboardModifier.NoModifier
                or not self._editable()):
            return False
        if self._session is not None:
            if self._how != "keys":
                return True
            self.stop(cancelled=True)       # move mode: a click elsewhere puts it back
        point = e.position()
        self._hover_at(point)
        picture, hot = self._hover, self._hot
        if picture is None:
            return False
        self._editor.setFocus(Qt.FocusReason.MouseFocusReason)
        photos = self._photos
        if hot == overlay.ADD:
            photos.add_requested.emit(photos.adding_place(picture), None)
            return True
        if hot in (overlay.MORE, overlay.ALT, overlay.BIG):
            photos.select(picture.position)
            chips = self._chips(photos.current())
            if hot == overlay.MORE:
                box = chips.boxes[overlay.MORE] if chips is not None else QRectF(point, point)
                photos.keys.open_menu(box.bottomLeft() + QPointF(0, 4))
            else:
                photos.run(DESCRIPTION if hot == overlay.ALT else LARGE)
            return True
        if picture.grouped and hot is None:
            caption = photo_group_view.caption_box(self._editor.document(), picture.position)
            if (picture.unit.has_caption and caption is not None
                    and caption.contains(self._doc_point(point))):
                photos.fields.open(picture, CAPTION)
                return True
        self._pressed = (point.toPoint(), picture, hot == overlay.HANDLE)
        return True

    def mouse_release(self, e) -> bool:
        if self._session is not None and self._how == "drag":
            self._drop()
            return True
        pressed, self._pressed = self._pressed, None
        if pressed is None:
            return False
        _start, picture, on_handle = pressed
        self._photos.select(picture.position, quiet=False)
        if on_handle and picture.grouped:
            self.pick_up(self._photos.current())
        self._editor.viewport().update()
        return True

    def tooltip(self, point: QPoint) -> Optional[str]:
        """What a chip or the bar's button under ``point`` does."""
        self._hover_at(QPointF(point))
        picture, hot = self._hover, self._hot
        if picture is None or hot is None:
            return None
        if hot == overlay.HANDLE:
            return _("Drag to move it, or click to move it with the arrow keys")
        if hot == overlay.MORE:
            return _("All commands for {photo}").format(photo=self._photos.place_words(picture))
        if hot == overlay.ALT:
            alt = picture.unit.description
            return (_("Description: {words}").format(words=alt) if alt
                    else _("Description missing: for people who can’t see the photo"))
        if hot == overlay.BIG:
            if not picture.grouped:
                return (_("Show as wide as the text") if picture.unit.wide
                        else _("Show wider than the text"))
            if photo_group_view.own_row(self._editor.document(), picture.unit):
                return _("Already the whole width")
            return (_("Show normal again") if picture.unit.wide
                    else _("Show large: a row of its own across the whole width"))
        return _("Add photos to this group. Or drag them onto it.")

    # ------------------------------------------------------------------ #
    # Moving a picture                                                     #
    # ------------------------------------------------------------------ #

    def _start(self, picture: Optional[Picture], how: str) -> drag.Session:
        session = drag.Session(self._editor.document(), picture, _("Show alone here"),
                               _("Together with this photo"))
        self._session, self._how = session, how
        self._hover, self._hot = None, None
        if picture is not None and picture.grouped:
            session.steady.take(session.slot(picture.first, picture.index), self._point)
            session.show(_now(), animate=False)
        return session

    def _lift(self, picture: Picture, point: QPointF) -> None:
        """The pressed picture follows the pointer from here."""
        self._point = QPointF(point)
        self._photos.select(picture.position)
        # Selecting it can take an empty split line back, which moves it.
        picture = self._photos.current() or picture
        self._start(picture, "drag")
        self._ghost = self._ghost_of(picture)
        self._editor.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
        self._photos.say(_("{photo} picked up").format(photo=self._photos.place_words(picture)))
        self._frames.start()
        self._editor.viewport().update()

    def _ghost_of(self, picture: Picture) -> Optional[QPixmap]:
        name = photo_group.without_wide(picture.unit.name)
        found = self._editor.document().resource(
            QTextDocument.ResourceType.ImageResource.value, QUrl(name))
        if isinstance(found, QImage) and not found.isNull():
            found = QPixmap.fromImage(found)
        if not isinstance(found, QPixmap) or found.isNull():
            return None
        return found.scaled(int(_GHOST), int(_GHOST), Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.SmoothTransformation)

    def pick_up(self, picture: Optional[Picture]) -> None:
        """Move mode: ``picture`` (of a group) moves with the arrow keys."""
        if picture is None or not picture.grouped or picture.count < 2:
            return
        self.stop()
        self._start(picture, "keys")
        self._photos.say(_("{photo} picked up. Move it with the arrow keys, Return puts it down, "
                           "Escape cancels.").format(photo=self._photos.place_words(picture)))
        self._editor.viewport().update()

    def key_press(self, e) -> bool:
        """Escape while something is moved; the arrows, Return and Escape
        in move mode. True when the key was answered."""
        session = self._session
        if session is None:
            return False
        key = e.key()
        if key == Qt.Key.Key_Escape:
            self.stop(cancelled=True)
            return True
        if self._how != "keys":
            return True
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._drop()
            return True
        steps = {Qt.Key.Key_Left: -1, Qt.Key.Key_Up: -1, Qt.Key.Key_Right: 1, Qt.Key.Key_Down: 1}
        if key in _MODIFIERS:
            return True
        if key not in steps:
            # Any other key ends move mode, said, and goes on as it would.
            self.stop(cancelled=True)
            return False
        target = session.target
        to = target.index + steps[key]
        if not 0 <= to < len(target.units):
            words = _("{photo}, already first") if to < 0 else _("{photo}, already last")
            self._photos.say(words.format(photo=_("Photo {k} of {n}").format(
                k=target.index + 1, n=len(target.units))))
            return True
        session.steady.take(session.slot(target.place.at, to), self._point)
        session.show(_now(), animate=not accessibility_display.reduce_motion())
        self._photos.say(_("Photo {k} of {n}").format(k=to + 1, n=len(target.units)))
        self._frames.start()
        self._editor.viewport().update()
        return True

    def _follow(self, point: QPointF) -> None:
        """The pointer moved to ``point`` (the viewport's) while dragging."""
        self._point = QPointF(point)
        height = self._editor.viewport().height()
        edge = min(point.y() - _EDGE, height - _EDGE - point.y())
        self._scroll = 0.0
        if edge < 0:
            self._scroll = max(-_MAX_SCROLL, min(_MAX_SCROLL, -edge * _MAX_SCROLL / _EDGE))
            if point.y() < height / 2:
                self._scroll = -self._scroll
            return          # targets pause while the page scrolls
        self._aim()

    def _aim(self) -> None:
        session = self._session
        doc = self._editor.document()
        at = self._doc_point(self._point)
        block = doc.findBlock(max(0, doc.documentLayout().hitTest(
            at, Qt.HitTestAccuracy.FuzzyHit)))
        candidate = session.candidate(at, block)
        if candidate is None and session.picture is not None and session.picture.grouped:
            candidate = session.slot(session.picture.first, session.picture.index)
        if session.steady.settle(candidate, at, _now()):
            session.show(_now(), animate=not accessibility_display.reduce_motion())

    def _frame(self) -> None:
        if self._session is None and self._flash is None:
            self._frames.stop()
            return
        if self._session is not None and self._how in ("drag", "files"):
            if self._scroll:
                bar = self._editor.verticalScrollBar()
                bar.setValue(bar.value() + round(self._scroll))
            else:
                self._aim()         # a target waits its moment even when the pointer rests
        if self._flash is not None and _now() - self._flash[1] > _FLASH_S:
            self._flash = None
        self._editor.viewport().update()

    def _drop(self) -> None:
        """Let go: the picture goes where the target shown says, as one
        step; at home, nothing happens."""
        session = self._session
        target = session.target if session is not None else None
        picture = session.picture if session is not None else None
        self.stop()
        # Refused when its group is not the one it was picked up from.
        if (picture is None or target is None or target.home
                or actions.standing(self._editor.document(), picture) != picture):
            self._photos.say(_("Cancelled, the photo is back in its place"))
            if picture is not None:
                self._photos.select(picture.position)
            return
        self._photos.relocate(picture, target.place)

    def stop(self, *, cancelled: bool = False) -> None:
        """Nothing is moved any more (Escape, the focus gone, a command)."""
        self._pressed = None
        if self._session is None:
            return
        picture = self._session.picture
        self._session, self._how, self._ghost, self._scroll = None, "", None, 0.0
        if self._flash is None:
            self._frames.stop()
        self._editor.viewport().setCursor(Qt.CursorShape.IBeamCursor)
        if cancelled and picture is not None:
            self._photos.say(_("Cancelled, the photo is back in its place"))
        self._editor.viewport().update()

    # ------------------------------------------------------------------ #
    # Files from another app                                               #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _picture_files(mime) -> List[str]:
        if not mime.hasUrls():
            return []
        paths = [file_paths.normalize(url.toLocalFile()) for url in mime.urls()
                 if url.isLocalFile()]
        if not paths or len(paths) != len(mime.urls()):
            return []
        return paths if all(image_safety.is_image_file(path) for path in paths) else []

    def drag_files(self, event) -> bool:
        """Picture files dragged over the text: the slot they would take
        is shown. True when there is one (the drop is accepted)."""
        if not self._editable() or not self._picture_files(event.mimeData()):
            self.stop()
            return False
        if self._session is None or self._how != "files":
            self.stop()
            self._start(None, "files")
            self._frames.start()
        self._follow(event.position())
        if self._session.target is None:
            return False
        event.acceptProposedAction()
        self._editor.viewport().update()
        return True

    def drop_files(self, event) -> bool:
        """Picture files let go of over the slot shown: they go there."""
        session = self._session if self._how == "files" else None
        paths = self._picture_files(event.mimeData())
        target = session.target if session is not None else None
        self.stop()
        if target is None or not paths:
            return False
        self._photos.add_requested.emit(target.place, paths)
        event.acceptProposedAction()
        return True

    # ------------------------------------------------------------------ #
    # Painting                                                             #
    # ------------------------------------------------------------------ #

    def flash(self, position: int) -> None:
        """Light up the picture at ``position`` for a moment (what Undo
        brought back)."""
        self._flash = (position, _now())
        self._frames.start()

    def caret_moved(self) -> None:
        """The ring follows the picture at the caret."""
        picture = self._photos.current() if self._editor.hasFocus() else None
        ring = picture.position if picture is not None else None
        if ring != self._ring:
            self._ring = ring
            self._editor.viewport().update()

    def paint_text(self, event) -> bool:
        """The text drawn while a picture of a group is selected: as Qt
        draws it, without its highlight, which would fill the group's
        whole line around the pictures (the ring shows what is selected,
        and no caret blinks, as in Pages). False otherwise: Qt draws it."""
        ed = self._editor
        cursor = ed.textCursor()
        if (cursor.selectionEnd() - cursor.selectionStart() != 1
                or not photo_group.is_group_block(cursor.block())):
            return False
        dx, dy = ed.horizontalScrollBar().value(), ed.verticalScrollBar().value()
        clip = QRectF(event.rect().translated(dx, dy))
        context = QAbstractTextDocumentLayout.PaintContext()
        context.palette = ed.palette()
        context.clip = clip
        selections = []
        for extra in ed.extraSelections():          # spelling, found words
            selection = QAbstractTextDocumentLayout.Selection()
            selection.cursor, selection.format = extra.cursor, extra.format
            selections.append(selection)
        context.selections = selections
        painter = QPainter(ed.viewport())
        painter.translate(-dx, -dy)
        painter.setClipRect(clip)
        ed.document().documentLayout().draw(painter, context)
        painter.end()
        return True

    def colors(self) -> Tuple[QColor, QColor, QColor]:
        """The accent (the focus ring's blue, which stands out on the page
        in either theme; the selection colour does not in the dark), the
        ink and the page's colour."""
        surface = self._editor.surface_color()
        accent = QColor(theme.focus_ring_color(surface.lightness() < 128))
        return accent, self._editor.palette().color(self._editor.palette().ColorRole.Text), surface

    def paint(self, painter) -> None:
        accent, ink, surface = self.colors()
        solid = accessibility_display.increase_contrast()
        photos = self._photos
        if self._session is not None:
            self._paint_moving(painter, accent, ink, surface, solid)
            return
        current = photos.current() if self._editor.hasFocus() else None
        if current is not None:
            box = photos.box_of(current)
            if box is not None:
                strength = 1.0
                if self._flash is not None and self._flash[0] == current.position:
                    strength = 2.0 - (_now() - self._flash[1]) / _FLASH_S
                overlay.paint_ring(painter, photos.to_view(box), accent, max(1.0, strength))
        shown = self._shown()
        if shown is None:
            return
        bar = self._bar(shown)
        if bar is not None:
            count = shown.count
            overlay.paint_bar(painter, bar, ngettext("Photo Group · {n} Photo",
                                                     "Photo Group · {n} Photos",
                                                     count).format(n=count),
                              _("Drag to arrange"), _("Photo"), ink=ink, accent=accent,
                              hint_in_accent=False, hot=self._hot == overlay.ADD, solid=solid)
        chips = self._chips(shown)
        if chips is not None:
            doc = self._editor.document()
            big_on = shown.unit.wide or bool(shown.grouped
                                             and photo_group_view.own_row(doc, shown.unit))
            overlay.paint_chips(painter, chips, missing=not shown.unit.description, big_on=big_on,
                                hot=self._hot, accent=accent, solid=solid)

    def _paint_moving(self, painter, accent: QColor, ink: QColor, surface: QColor,
                      solid: bool) -> None:
        session = self._session
        target = session.target
        photos = self._photos
        now = _now()
        offset = QPointF(-self._editor.horizontalScrollBar().value(),
                         -self._editor.verticalScrollBar().value())
        picture = session.picture
        own_shown = (session.preview is not None and picture is not None and picture.grouped
                     and session.preview.start == picture.first)
        if picture is not None and not own_shown:
            box = photos.box_of(picture)
            if box is not None:
                veil = QColor(surface)
                veil.setAlphaF(0.62)
                painter.fillRect(photos.to_view(box), veil)      # lifted from its place
        painter.save()
        painter.translate(offset)
        if session.preview is not None:
            session.preview.paint(painter, now, accent=accent, surface=surface,
                                  words=_("New Photo"), lifted=self._how != "keys")
        elif target is not None and target.line is not None:
            left, right, y = target.line
            overlay.paint_line(painter, left, right, y, target.words, accent=accent, solid=solid)
        painter.restore()
        label = ""
        if target is not None and target.units is not None:
            label = _("Photo {k} of {n}").format(k=target.index + 1, n=len(target.units))
            area = session.preview.area().translated(offset) if session.preview else None
            if area is not None:
                hint = (_("Arrow keys move it, Return puts it down, Esc cancels")
                        if self._how == "keys" else
                        _("Release: here as photo {k} of {n} · Esc cancels").format(
                            k=target.index + 1, n=len(target.units)))
                count = len(target.units)
                moving = session.preview.moved_box(now)
                if self._how == "keys" and moving is not None:
                    overlay.paint_hint(painter, moving.translated(offset), hint, accent=accent,
                                       room=QRectF(self._editor.viewport().rect()))
                overlay.paint_bar(painter, overlay.bar_over(area, _("Photo")),
                                  ngettext("Photo Group · {n} Photo", "Photo Group · {n} Photos",
                                           count).format(n=count), hint, _("Photo"), ink=ink,
                                  accent=accent, hint_in_accent=True, hot=False, solid=solid)
        elif target is not None:
            label = target.words
        if self._ghost is not None and self._how == "drag":
            size = self._ghost.size()
            box = QRectF(self._point.x() - size.width() / 2, self._point.y() - size.height() / 2,
                         size.width(), size.height())
            overlay.paint_ghost(painter, self._ghost, box, label, accent=accent)
        if session.preview is not None and session.preview.moving(now):
            self._frames.start()
