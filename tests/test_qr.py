# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""nostr/qr.py draws what its docstring says (P1): black on white by
default, with a quiet zone of four modules, whatever the theme; every
module whole pixels of the screen, never fewer logical pixels than
asked; and the code reads back as its text."""

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


# -- the pairing code (RG-M2r2b 7) ----------------------------------------------

def _pairing_link():
    from nostr.bunker import NIP46_RELAYS, build_nostrconnect_uri
    return build_nostrconnect_uri("f" * 64, list(NIP46_RELAYS), "f" * 16)


@pytest.mark.parametrize("ratio", [1.0, 1.25, 1.5, 2.0, 3.0])
def test_a_module_is_whole_screen_pixels_and_never_less_than_asked(ratio):
    import math
    from nostr.qr import qr_matrix
    text = _pairing_link()
    total = len(qr_matrix(text, "l")) + 8                    # the quiet zone, 4 a side
    pixmap = make_qr_pixmap(text, size=280, error="l", min_module=3, device_pixel_ratio=ratio)
    assert pixmap.devicePixelRatio() == ratio
    assert pixmap.width() == pixmap.height() and pixmap.width() % total == 0
    module = pixmap.width() // total
    assert module >= math.ceil(3 * ratio)                   # 3 logical pixels at least
    if ratio == int(ratio):                                 # whole ratios fit the room
        assert pixmap.deviceIndependentSize().width() <= 280


def test_a_code_too_large_for_its_room_is_larger_not_finer():
    from nostr.qr import qr_matrix
    text = "x" * 1_500
    total = len(qr_matrix(text, "l")) + 8
    pixmap = make_qr_pixmap(text, size=100, error="l", min_module=3)
    assert pixmap.width() == 3 * total > 100


def test_the_error_correction_is_the_one_asked_for():
    import segno
    from nostr.qr import qr_matrix
    text = _pairing_link()
    assert qr_matrix(text, "l") == [list(row) for row in segno.make(text, error="l").matrix]
    assert len(qr_matrix(text, "l")) < len(qr_matrix(text, "m"))   # L is the smaller


def _modules_of(image, total):
    """The modules as the image shows them: the colour at each one's middle."""
    side = image.width() // total
    return [[1 if image.pixelColor(x * side + side // 2, y * side + side // 2).lightness() < 128
             else 0 for x in range(4, total - 4)] for y in range(4, total - 4)]


@pytest.mark.parametrize("ratio", [1.0, 2.0])
def test_the_drawn_code_reads_back_as_its_text(tmp_path, ratio):
    import shutil
    import subprocess
    from nostr.qr import qr_matrix
    text = _pairing_link()
    matrix = qr_matrix(text, "l")
    pixmap = make_qr_pixmap(text, size=280, error="l", min_module=3, device_pixel_ratio=ratio)
    image = pixmap.toImage()
    # The module matrix drawn is segno's, module for module.
    assert _modules_of(image, len(matrix) + 8) == matrix
    # And a decoder reads the text back from the picture, offline.
    zbarimg = shutil.which("zbarimg")
    if zbarimg is None:
        pytest.skip("no offline QR decoder (zbarimg) here; the module matrix was compared")
    path = tmp_path / "code.png"
    assert image.save(str(path))
    read = subprocess.run([zbarimg, "--raw", "-q", str(path)], capture_output=True, text=True,
                          check=False, timeout=30)
    assert read.stdout.strip() == text
