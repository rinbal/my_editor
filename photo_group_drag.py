# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Where a picture being dragged would land, and how that is shown.

While a picture is dragged (or files are, from another app, or a picture
is moved with the arrow keys) the document is never touched: what the
drop would do is only drawn (EDITING.md 2.3 and 4 of the photo grid
design), and the drop itself is one step of Undo. So a drag given up
leaves nothing behind.

- **Target** is one place the picture can go, with what shows it: the
  group it would be part of in its new order (Preview), or a line between
  two paragraphs.
- **Session.candidate** finds the target under the pointer: on a picture
  of a group, that picture's place; inside a group's room, the nearest
  picture's; on a picture that stands alone, beside it; else between the
  two paragraphs nearest to the pointer. Pictures next to each other are
  a group in Markdown, so a gap right beside pictures is a place among
  them.
- **Steady** keeps the target calm: a new one must be pointed at for a
  moment, and only after the pointer moved on from where the last change
  happened (further to go back to the target before), so a group that
  lays itself out under the pointer does not flicker.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QPainter, QTextCharFormat

import photo_group
import photo_group_actions as actions
import photo_group_overlay as overlay
import photo_group_view
from photo_group import Unit
from photo_group_actions import BESIDE, GAP, SLOT, Picture, Place

# How long a new target must be pointed at, how far the pointer must have
# moved since the last change of target, and how far to go back to the
# target shown before (seconds, pixels).
DWELL_S = 0.110
MOVE = 12.0
BACK = 28.0
# How long a group takes to lay itself out in a new order.
SETTLE_S = 0.16
# How far around a group or a picture the pointer still counts as on it.
_NEAR = 12.0

# The key of the picture being moved among the boxes of a preview (the
# others go by their unit's number).
_MOVED = -1


@dataclass
class Target:
    """One place a dragged picture can go. ``key`` tells two targets
    apart; ``home`` is the place the picture is at already. For a place
    in a group: the group's pictures as they would stand (``units``, the
    moved one at ``index``). For any other place: the line that shows it
    (left, right, y in the document's coordinates) and its words."""

    place: Place
    key: tuple
    home: bool = False
    units: Optional[List[Unit]] = None
    index: int = 0
    line: Optional[Tuple[float, float, float]] = None
    words: str = ""


class Steady:
    """The target shown, changed only by the rules above."""

    def __init__(self) -> None:
        self.shown: Optional[Target] = None
        self._before: Optional[tuple] = None
        self._changed_at: Optional[QPointF] = None
        self._candidate: Optional[tuple] = None
        self._since = 0.0

    def take(self, target: Optional[Target], point: QPointF) -> None:
        """Show ``target`` at once (the place a drag starts from)."""
        self.shown, self._changed_at, self._candidate = target, QPointF(point), None

    def settle(self, candidate: Optional[Target], point: QPointF, now: float) -> bool:
        """``candidate`` is under the pointer at ``point``: show it when
        the rules allow. True when what is shown changed."""
        key = candidate.key if candidate is not None else None
        shown = self.shown.key if self.shown is not None else None
        if key == shown:
            self._candidate = None
            return False
        if key != self._candidate:
            self._candidate, self._since = key, now
            return False
        if now - self._since < DWELL_S:
            return False
        if self._changed_at is not None:
            moved = math.hypot(point.x() - self._changed_at.x(), point.y() - self._changed_at.y())
            if moved < (BACK if key == self._before else MOVE):
                return False
        self._before, self.shown = shown, candidate
        self._changed_at, self._candidate = QPointF(point), None
        return True


def _key_of(unit: Unit, moved: Unit) -> int:
    return _MOVED if unit is moved else unit.number


class Preview:
    """A group drawn in another order where it stands: each picture's box
    in the document's coordinates, moving from the order shown before to
    the new one unless less motion was asked for. Taller than the group's
    room, it is drawn smaller, so nothing around it is covered."""

    def __init__(self, doc, start: int, origin: QPointF, room: Tuple[float, float],
                 moved: Unit) -> None:
        self.doc = doc
        self.start = start              # where the group's paragraph starts
        self.origin = origin
        self.room = room
        self.moved = moved
        self.units: List[Unit] = []
        self.index = 0
        self.scale = 1.0
        self._figures: List[Tuple[float, float]] = []
        self._to: Dict[int, QRectF] = {}
        self._from: Dict[int, QRectF] = {}
        self._started = 0.0

    def area(self) -> QRectF:
        return QRectF(self.origin.x(), self.origin.y(), self.room[0], self.room[1])

    def show(self, units: List[Unit], index: int, now: float, *, animate: bool) -> None:
        """Lay ``units`` out (the moved one at ``index``), from where each
        is drawn now."""
        arranged = photo_group_view.arrange(self.doc, units)
        if arranged is None:
            return
        current = self.boxes(now)
        self.units, self.index = list(units), index
        self.scale = overlay.fitted(arranged.width, arranged.height, self.room)
        left = self.origin.x() + (self.room[0] - arranged.width * self.scale) / 2
        self._figures = arranged.figures
        self._to = {}
        for unit, box in zip(units, arranged.boxes):
            self._to[_key_of(unit, self.moved)] = QRectF(
                left + box.x() * self.scale, self.origin.y() + box.y() * self.scale,
                box.width() * self.scale, box.height() * self.scale)
        self._from = {key: current.get(key, box) for key, box in self._to.items()}
        self._started = now if animate else now - SETTLE_S

    def moving(self, now: float) -> bool:
        return now - self._started < SETTLE_S

    def boxes(self, now: float) -> Dict[int, QRectF]:
        """Where each picture is drawn at ``now``."""
        if not self.moving(now):
            return dict(self._to)
        share = (now - self._started) / SETTLE_S
        share = 1 - (1 - share) ** 3            # eases out, as the system's own moves do
        keys = list(self._to)
        return dict(zip(keys, overlay.blend([self._from[k] for k in keys],
                                            [self._to[k] for k in keys], share)))

    def nearest(self, point: QPointF) -> int:
        """The picture under ``point``, else the nearest one, as its
        number among the pictures shown."""
        best, best_distance = 0, float("inf")
        for number, unit in enumerate(self.units):
            box = self._to[_key_of(unit, self.moved)]
            if box.contains(point):
                return number
            distance = math.hypot(point.x() - box.center().x(), point.y() - box.center().y())
            if distance < best_distance:
                best, best_distance = number, distance
        return best

    def paint(self, painter: QPainter, now: float, *, accent: QColor, surface: QColor,
              words: str, lifted: bool = True) -> None:
        """The group in the order shown, over where the document draws it:
        the picture that moves faint where it would land while it is
        ``lifted`` (it follows the pointer), else drawn there with a ring
        (moved with the keys)."""
        painter.fillRect(self.area().adjusted(-2, -2, 2, 2), surface)
        boxes = self.boxes(now)
        settled = self.scale == 1.0 and not self.moving(now)
        for number, (unit, figure) in enumerate(zip(self.units, self._figures)):
            box = boxes[_key_of(unit, self.moved)]
            if unit is self.moved and lifted:
                painter.setOpacity(0.35)
                photo_group_view.draw_unit(painter, self.doc, unit, box)
                painter.setOpacity(1.0)
                overlay.paint_placeholder(painter, box, "" if unit.name else words, accent=accent)
            else:
                at = ((self.origin.x() + figure[0], figure[1]) if settled else (0.0, 0.0))
                photo_group_view.draw_unit(painter, self.doc, unit, box, at)
                if unit is self.moved:
                    overlay.paint_ring(painter, box, accent)
            overlay.paint_number(painter, box, number + 1, accent=accent)
        overlay.paint_frame(painter, self.area().adjusted(-8, -8, 8, 8), accent)


def room_of(doc, start: int) -> Optional[Tuple[QPointF, Tuple[float, float]]]:
    """Where the group that starts at ``start`` is drawn: its top left
    corner, and its width and height (captions included)."""
    group = photo_group.group_at(doc, start)
    drawn = photo_group_view.picture_box(doc, start)
    arranged = photo_group_view.arrange(doc, group.units) if group is not None else None
    if drawn is None or arranged is None:
        return None
    first = arranged.boxes[0]
    return (QPointF(drawn.x() - first.x(), drawn.y() - first.y()),
            (arranged.width, arranged.height))


class Session:
    """One drag: of ``picture`` (None for files from another app, shown
    as one new photo), from the place it is at."""

    def __init__(self, doc, picture: Optional[Picture], words_alone: str,
                 words_beside: str) -> None:
        self.doc = doc
        self.picture = picture
        self.unit = picture.unit if picture is not None else Unit(QTextCharFormat(), "")
        self.steady = Steady()
        self.preview: Optional[Preview] = None
        self._words_alone = words_alone
        self._words_beside = words_beside

    @property
    def target(self) -> Optional[Target]:
        return self.steady.shown

    # ---- finding the target ----

    def _own_group(self, start: int) -> bool:
        return (self.picture is not None and self.picture.grouped
                and self.picture.first == start)

    def slot(self, start: int, index: int) -> Optional[Target]:
        """The place ``index`` among the pictures of the group at
        ``start`` (counted without the picture that moves)."""
        group = photo_group.group_at(self.doc, start)
        if group is None:
            return None
        units = list(group.units)
        own = self._own_group(start)
        if own:
            self.unit = units.pop(self.picture.index)
        index = max(0, min(index, len(units)))
        units.insert(index, self.unit)
        return Target(Place(SLOT, start, index), (SLOT, start, index),
                      home=own and index == self.picture.index, units=units, index=index)

    def _beside(self, lone: Picture, before: bool) -> Target:
        if self.picture is not None and not self.picture.grouped \
                and lone.position == self.picture.position:
            return Target(Place(BESIDE, lone.position), ("home",), home=True)
        layout = self.doc.documentLayout()
        first = layout.blockBoundingRect(self.doc.findBlock(lone.first))
        last = layout.blockBoundingRect(self.doc.findBlock(lone.end))
        return Target(Place(BESIDE, lone.position, before=before),
                      (BESIDE, lone.position, before),
                      line=(first.left(), first.right(), first.top() if before else last.bottom()),
                      words=self._words_beside)

    def candidate(self, point: QPointF, block) -> Optional[Target]:
        """The target at ``point`` (the document's coordinates), ``block``
        being the paragraph nearest to it."""
        doc = self.doc
        if self.preview is not None and self.preview.area().adjusted(
                -_NEAR, -overlay.BAR_ROOM, _NEAR, _NEAR).contains(point):
            number = self.preview.nearest(point)
            if number == self.preview.index:
                return self.target
            return self.slot(self.preview.start, number)
        for near in (block, block.previous(), block.next()):
            if not near.isValid() or not photo_group.is_group_block(near):
                continue
            boxes = photo_group_view.group_boxes(doc, near.position())
            found = self._in_group(near.position(), boxes, point)
            if found is not None:
                return found
        if not block.isValid() or doc.frameAt(block.position()).parentFrame() is not None:
            return None
        return self._between(point, block)

    def _in_group(self, start: int, boxes, point: QPointF) -> Optional[Target]:
        if not boxes:
            return None
        area = QRectF()
        for _at, box in boxes:
            area = area.united(box)
        if not area.adjusted(-_NEAR, -overlay.BAR_ROOM, _NEAR, _NEAR).contains(point):
            return None
        number, box = min(
            enumerate(box for _at, box in boxes),
            key=lambda item: (not item[1].contains(point),
                              math.hypot(point.x() - item[1].center().x(),
                                         point.y() - item[1].center().y())))
        # In reading order: the upper left half of a picture is before it.
        before = ((point.x() - box.left()) / max(1.0, box.width())
                  + (point.y() - box.top()) / max(1.0, box.height())) < 1
        if self._own_group(start):
            own = self.picture.index
            if number == own:
                return self.slot(start, own)
            number -= number > own
        return self.slot(start, number + (0 if before else 1))

    def _run_at(self, block, *, upward: bool) -> Optional[Target]:
        """The place that joins the pictures in ``block`` from the gap
        under them (``upward``) or above them."""
        if not block.isValid():
            return None
        if photo_group.is_group_block(block):
            count = len(photo_group.block_units(block)) - self._own_group(block.position())
            return self.slot(block.position(), count if upward else 0)
        lone = actions.picture_at(self.doc, block.position())
        if lone is None and upward and block.previous().isValid():
            above = actions.picture_at(self.doc, block.previous().position())
            if above is not None and above.end >= block.position():
                lone = above                  # ``block`` is its caption
        return self._beside(lone, before=not upward) if lone is not None else None

    def _between(self, point: QPointF, block) -> Optional[Target]:
        """The gap nearest to ``point``: above or under ``block``."""
        layout = self.doc.documentLayout()
        rect = layout.blockBoundingRect(block)
        before = point.y() < rect.center().y()
        upper, lower = (block.previous(), block) if before else (block, block.next())
        joined = self._run_at(upper, upward=True) or self._run_at(lower, upward=False)
        if joined is not None:
            return joined
        # A paragraph of its own: only between plain paragraphs and headings.
        if block.textList() is not None or not (
                photo_group.is_plain_paragraph(block) or block.blockFormat().headingLevel()):
            return None
        return Target(Place(GAP, block.position(), before=before),
                      (GAP, block.position(), before),
                      line=(rect.left(), rect.right(), rect.top() if before else rect.bottom()),
                      words=self._words_alone)

    # ---- showing it ----

    def show(self, now: float, *, animate: bool) -> None:
        """Have the preview follow the target shown."""
        target = self.target
        if target is None or target.units is None:
            self.preview = None
            return
        if self.preview is None or self.preview.start != target.place.at:
            found = room_of(self.doc, target.place.at)
            if found is None:
                self.preview = None
                return
            self.preview = Preview(self.doc, target.place.at, found[0], found[1],
                                   target.units[target.index])
            animate = False
        self.preview.moved = target.units[target.index]
        self.preview.show(target.units, target.index, now, animate=animate)
