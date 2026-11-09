# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""nostr/qr.py draws what its docstring says (P1): black on white by
default, with a quiet zone of four modules, whatever the theme."""

from __future__ import annotations

import sys

import pytest
from PySide6.QtWidgets import QApplication

from nostr.qr import make_qr_pixmap


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    yield QApplication.instance() or QApplication(sys.argv)


def test_a_code_is_black_on_white_with_its_quiet_zone_by_default():
    image = make_qr_pixmap("nostrconnect://example", size=200).toImage()
    assert image.width() == image.height() <= 200
    corner = image.pixelColor(1, 1)                      # in the quiet zone
    assert (corner.red(), corner.green(), corner.blue()) == (255, 255, 255)
    darkest = min(image.pixelColor(x, y).lightness()
                  for x in range(image.width()) for y in range(image.height()))
    assert darkest == 0


def test_an_empty_text_is_refused():
    with pytest.raises(ValueError):
        make_qr_pixmap("")
