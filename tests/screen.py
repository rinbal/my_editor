# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Test windows taller or wider than the screen.

On Windows' own platform (the CI runs the tests there, on a desktop of
1024 x 768), Windows keeps a framed top-level window within the screen:
a test asking for 1200 x 3000 gets about 1000 x 733, the pictures below
the bottom edge are not drawn, and a click meant for one lands on
another. A frameless window keeps the size it asks for. On the offscreen
platform, and on a Mac or Linux desktop, nothing changes.
"""

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication


def unclamped(widget):
    """Let ``widget``, a top-level window not shown yet, keep the size a
    test gives it, also beyond the screen. Returns ``widget``."""
    if QGuiApplication.platformName() == "windows":
        widget.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
    return widget


__all__ = ["unclamped"]
