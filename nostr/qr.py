# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Render text (typically a ``nostrconnect://`` link) to a QPixmap.

We use ``segno`` to compute the QR matrix (pure Python, already in
requirements.txt) and paint it ourselves with QPainter so there's no
PNG decode round-trip and no Pillow dependency.

The QR spec requires a 4-module quiet zone around the matrix: without
it many scanners refuse to lock onto the code. We add it explicitly
since segno's matrix iterator stops at the data modules.
"""

from __future__ import annotations

import math

import segno
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPixmap


# Per QR Code spec, at least 4 modules of quiet zone is required.
_QUIET_MODULES: int = 4


def qr_matrix(text: str, error: str = "m") -> list:
    """The modules of ``text``'s code (rows of 1 dark, 0 light), without
    the quiet zone, as :func:`make_qr_pixmap` draws them."""
    if not text:
        raise ValueError("QR text must not be empty")
    modules = segno.make(text, error=error).matrix
    if not modules:
        raise ValueError("segno returned an empty matrix")
    return [list(row) for row in modules]


def make_qr_pixmap(
    text: str,
    size: int = 280,
    dark: QColor | str = "#000000",
    light: QColor | str = "#FFFFFF",
    *,
    error: str = "m",
    device_pixel_ratio: float = 1.0,
    min_module: int = 1,
) -> QPixmap:
    """Encode ``text`` as a QR code: a square QPixmap of the code and its
    quiet zone, every module a whole number of the screen's own pixels
    (fractional modules look smeary), as large as fits in ``size`` logical
    pixels, but never less than ``min_module`` logical pixels a module: a
    code that needs more is larger than ``size``. Drawn at
    ``device_pixel_ratio`` (the window's), so a Retina screen gets twice
    the pixels, not a scaled-up code.

    ``dark`` / ``light`` follow QR convention (the dark modules are the
    "data" pixels; light is the background, the quiet zone included).
    The default is black on white, the way scanners read codes best,
    whatever the window's theme: every window that shows a code draws
    it so (Connect Signer App, the membership invoice, the key backup).

    ``error`` is the error correction: **M** (about 15%) by default, room
    for a glare or a finger; **L** (about 7%) keeps a long link small
    enough to scan from a screen (Connect Signer App).
    """
    modules = qr_matrix(text, error)
    total_modules = len(modules) + 2 * _QUIET_MODULES

    ratio = max(1.0, float(device_pixel_ratio or 1.0))
    module_px = max(1, math.ceil(min_module * ratio), int(size * ratio) // total_modules)
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
    # Drawn in the screen's pixels; laid out in logical ones.
    pix.setDevicePixelRatio(ratio)
    return pix
