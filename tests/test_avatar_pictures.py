# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A person's picture is sharp on a Retina screen.

What must hold: every face, monogram and the toolbar's account chip is
made with two pixels a point (as the publications' logos are), and keeps
the size asked for in points, so a label or a button lays it out as
before. Made at one pixel a point, they were blurred on every Retina
screen.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QSize, QSizeF
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import QApplication

from nostr.ui import avatar


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_a_face_has_two_pixels_a_point_and_its_size_in_points(qapp):
    photo = QPixmap(300, 200)
    photo.fill(QColor("#336699"))
    made = [
        avatar.make_initials_pixmap("AK", QColor("#7136DD"), 18),
        avatar.make_avatar_pixmap_from_image(photo, 18),
        avatar.pixmap_for_profile("Anna Keller", "a" * 64, None, size=18),
        avatar.pixmap_for_profile("Anna Keller", "a" * 64, photo, size=18),
        avatar.make_disconnected_pixmap(False, 18),
    ]
    for picture in made:
        assert picture.devicePixelRatio() == 2.0
        assert picture.size() == QSize(36, 36)
        assert picture.deviceIndependentSize() == QSizeF(18, 18)
    # The photo fills its circle: the middle is the photo's colour, a corner is clear.
    image = made[1].toImage()
    assert image.pixelColor(18, 18) == QColor("#336699")
    assert image.pixelColor(0, 0).alpha() == 0
    # The toolbar's chip: the face, then its arrow, at the chip's size in points.
    chip = avatar.compose_chip_icon(made[0], QColor("#000000")).pixmap(
        QSize(avatar.CHIP_TOTAL_WIDTH, avatar.AVATAR_SIZE), 2.0)
    assert chip.deviceIndependentSize() == QSizeF(avatar.CHIP_TOTAL_WIDTH, avatar.AVATAR_SIZE)
