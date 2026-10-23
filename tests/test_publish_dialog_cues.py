# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins that Publish as Note and Publish as Article show what the keyboard
will do, as every other dialog does.

What must hold, light and dark: Publish, the default, is drawn in the
accent, and the button with the keyboard focus has the ring, Cancel
included; on Windows and Linux, where the default follows the focus, a
focused Cancel takes the accent instead. (Review R3: their own stylesheets drew both buttons alike and
no focus at all, in the two dialogs whose action cannot be taken back.)
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QColor  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import alerts  # noqa: E402
from nostr.ui.appearance import roles  # noqa: E402
from tests.test_publish_job import _dialog  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def edge(button):
    # The ordinary border is one pixel; the keyboard ring is two pixels.
    image = button.grab().toImage()
    scale = image.devicePixelRatio()
    return image.pixelColor(round(.5 * scale), round(button.height() * scale / 2))


def middle(button):
    image = button.grab().toImage()
    scale = image.devicePixelRatio()
    return image.pixelColor(round(4 * scale), round(button.height() * scale / 2))


@pytest.mark.parametrize("dark", [True, False])
@pytest.mark.parametrize("kind", ["note", "article"])
@pytest.mark.parametrize("mac", [True, False])
def test_the_default_and_the_focus_can_be_seen(tmp_path, monkeypatch, mac, kind, dark):
    # On a Mac the default stays on Publish and a focused Cancel gets the
    # ring; on Windows and Linux the default moves with the focus, so a
    # focused Cancel takes the accent and the ring that shows on it, and
    # Publish turns plain: what Return will press is what looks pressed.
    monkeypatch.setattr(alerts, "IS_MAC", mac)
    dialog = _dialog(kind, tmp_path, None, is_dark=dark)
    if kind == "article":
        dialog._title_edit.setText("My title")     # Publish needs a title
    dialog.show()
    QApplication.processEvents()
    try:
        publish, cancel = dialog._publish_btn, dialog._cancel_btn
        r = roles(dark)
        accent = middle(publish)
        top, bottom = QColor(r.highlight), QColor(r.button)
        for channel in ("red", "green", "blue"):
            lo, hi = sorted((getattr(top, channel)(), getattr(bottom, channel)()))
            assert lo <= getattr(accent, channel)() <= hi
        assert publish.isDefault()
        assert middle(cancel) != middle(publish)
        unfocused = edge(cancel)
        cancel.setFocus(Qt.FocusReason.TabFocusReason)
        QApplication.processEvents()
        if mac:
            assert edge(cancel) == QColor(r.focus)
        else:
            assert cancel.isDefault() and middle(cancel) == accent
            assert edge(cancel) == QColor(r.on_accent)
            assert middle(publish) != accent
        assert edge(cancel) != unfocused
    finally:
        dialog.close()
