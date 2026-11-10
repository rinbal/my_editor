# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The photo fixtures with coloured corners, and the check that a picture
is shown whole (SPEC 17 of the photo grid work).

Each fixture has a magenta border and a square of colour in each corner:
red top left, green top right, blue bottom left, yellow bottom right.
A picture shown whole shows all four, each in its own corner; a cropped
one (object-fit: cover, a cut edge) loses at least one. The same PNGs are
STANDUP's tests/fixtures/photos.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Tuple

from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QImage

PHOTOS = Path(__file__).resolve().parent / "fixtures" / "photos"

# name -> (width, height) in pixels
SHAPES: Dict[str, Tuple[int, int]] = {
    "portrait.png": (809, 1800),
    "landscape.png": (1800, 809),
    "square.png": (1400, 1400),
    "panorama.png": (3600, 900),
    "photo.png": (1800, 1200),
    "plan.png": (1800, 1273),
}

# Top left, top right, bottom left, bottom right.
CORNERS = (QColor("#E5484D"), QColor("#30A46C"), QColor("#0090FF"), QColor("#FFB224"))


def path_of(name: str) -> Path:
    return PHOTOS / name


def image_of(name: str) -> QImage:
    image = QImage(str(path_of(name)))
    assert not image.isNull(), name
    return image


def corner_colours(shot: QImage, box: QRectF) -> List[QColor]:
    """The colours 3 % of the box's short side inside each of its corners,
    and one pixel more: a picture is drawn on whole pixels, so its edge
    can be a pixel off its box, which in a picture 100 pixels wide is a
    third of the way from 3 % to the white edge of a corner's square
    (its colour runs from 2.3 % to 10.2 % of the short side)."""
    inset = min(box.width(), box.height()) * 0.03 + 1.0
    left, top = box.left() + inset, box.top() + inset
    right, bottom = box.right() - inset, box.bottom() - inset
    ratio = shot.devicePixelRatio() or 1.0
    return [QColor(shot.pixel(int(x * ratio), int(y * ratio)))
            for x, y in ((left, top), (right, top), (left, bottom), (right, bottom))]


def _close(a: QColor, b: QColor, tolerance: int = 48) -> bool:
    return (abs(a.red() - b.red()) <= tolerance and abs(a.green() - b.green()) <= tolerance
            and abs(a.blue() - b.blue()) <= tolerance)


def shows_whole(shot: QImage, box: QRectF) -> bool:
    """Whether the picture in ``box`` of ``shot`` shows all four corners."""
    return all(_close(found, wanted) for found, wanted in zip(corner_colours(shot, box), CORNERS))
