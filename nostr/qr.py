"""Render text (typically a ``nostrconnect://`` URI) to a QPixmap.

We use ``segno`` to compute the QR matrix (pure Python, already in
requirements.txt) and paint it ourselves with QPainter so there's no
PNG decode round-trip and no Pillow dependency.

The QR spec requires a 4-module quiet zone around the matrix — without
it many scanners refuse to lock onto the code. We add it explicitly
since segno's matrix iterator stops at the data modules.
"""

from __future__ import annotations

import segno
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPixmap


# Per QR Code spec, at least 4 modules of quiet zone is required.
_QUIET_MODULES: int = 4


def make_qr_pixmap(
    text: str,
    size: int = 280,
    dark: QColor | str = "#FFFFFF",
    light: QColor | str = "#1E1E1E",
) -> QPixmap:
    """Encode ``text`` as a QR code and return a ``size``×``size`` QPixmap.

    ``dark`` / ``light`` follow QR convention (the dark modules are the
    "data" pixels; light is the background). For our dark dialog theme
    we want a light-on-dark code, which inverts the everyday
    black-on-white convention — pass ``dark="#FFFFFF"`` and
    ``light="#1E1E1E"``.

    Error-correction is set to **M** (medium, ~15%) which is the segno
    default for typical payloads and gives readers room to recover from
    a glare or a finger over the screen.
    """
    if not text:
        raise ValueError("QR text must not be empty")

    qr = segno.make(text, error="m")
    modules = qr.matrix  # list[bytearray], 1 = dark, 0 = light

    matrix_size = len(modules)
    if matrix_size == 0:
        raise ValueError("segno returned an empty matrix")
    total_modules = matrix_size + 2 * _QUIET_MODULES

    # Pick the largest integer module size that fits in ``size`` so all
    # modules are pixel-aligned — fractional modules look smeary.
    module_px = max(1, size // total_modules)
    pixmap_px = module_px * total_modules

    dark_color = QColor(dark) if isinstance(dark, str) else dark
    light_color = QColor(light) if isinstance(light, str) else light

    pix = QPixmap(pixmap_px, pixmap_px)
    pix.fill(light_color)
    painter = QPainter(pix)
    painter.setPen(Qt.NoPen)
    painter.setBrush(dark_color)
    for y, row in enumerate(modules):
        for x, cell in enumerate(row):
            if not cell:
                continue
            painter.drawRect(
                (x + _QUIET_MODULES) * module_px,
                (y + _QUIET_MODULES) * module_px,
                module_px,
                module_px,
            )
    painter.end()
    return pix
