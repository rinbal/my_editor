# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Round profile-avatar chip with chevron — sits in the editor header.

The chip's hover state and its dropdown menu reuse the editor's existing
CSS verbatim (the same blocks in ``widgets.py`` and ``editor.py``) so it
reads as native to the app. Avatar + chevron rendering lives in
``avatar.py`` and is shared with the in-dialog profile switcher.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import QMenu, QToolButton

from .avatar import (
    AVATAR_SIZE,
    CHIP_TOTAL_WIDTH,
    compose_chip_icon,
    make_disconnected_pixmap,
    pixmap_for_profile,
)


# --------------------------------------------------------------------------- #
# Editor palette — kept literally identical to widgets.py / editor.py         #
# --------------------------------------------------------------------------- #

_DARK_MENU_CSS = """
QMenu {
    background: #252526;
    color: #CCCCCC;
    border: 1px solid #3C3C3C;
    padding: 4px;
}
QMenu::item { padding: 4px 20px 4px 30px; }
QMenu::item:selected { background: #1E1E1E; color: #FFFFFF; }
QMenu::item:disabled { color: #6A6A6A; }
QMenu::separator { height: 1px; background: #3C3C3C; margin: 4px 0px; }
"""

_LIGHT_MENU_CSS = """
QMenu {
    background: #F8F8F8;
    color: #333333;
    border: 1px solid #E1E1E1;
    padding: 4px;
}
QMenu::item { padding: 4px 20px 4px 30px; }
QMenu::item:selected { background: #F3F3F3; color: #000000; }
QMenu::item:disabled { color: #999999; }
QMenu::separator { height: 1px; background: #E1E1E1; margin: 4px 0px; }
"""

_DARK_CHIP_CSS = """
QToolButton {
    background: transparent;
    border: none;
    border-radius: 4px;
    padding: 3px 6px;
}
QToolButton:hover { background: #3C3C3C; }
QToolButton:pressed { background: #1E1E1E; }
QToolButton::menu-indicator { image: none; width: 0; }
"""

_LIGHT_CHIP_CSS = """
QToolButton {
    background: transparent;
    border: none;
    border-radius: 4px;
    padding: 3px 6px;
}
QToolButton:hover { background: #E1E1E1; }
QToolButton:pressed { background: #D0D0D0; }
QToolButton::menu-indicator { image: none; width: 0; }
"""


# --------------------------------------------------------------------------- #
# ProfileChip widget                                                          #
# --------------------------------------------------------------------------- #

class ProfileChip(QToolButton):
    """Header avatar chip — click opens a styled QMenu of profiles + actions."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._is_dark = True
        self.setPopupMode(QToolButton.InstantPopup)
        self.setToolButtonStyle(Qt.ToolButtonIconOnly)
        self.setCursor(Qt.PointingHandCursor)
        self.setIconSize(QSize(CHIP_TOTAL_WIDTH, AVATAR_SIZE))
        self.setFixedHeight(32)

        self.setMenu(QMenu(self))
        self._apply_chip_style()
        self._apply_menu_style()
        self.set_disconnected()

    # -- state setters ------------------------------------------------------

    def set_disconnected(self) -> None:
        avatar = make_disconnected_pixmap(is_dark=self._is_dark)
        self.setIcon(compose_chip_icon(avatar, self._chevron_color()))
        self.setToolTip("Connect Nostr")

    def set_profile(
        self,
        display_name: str,
        user_pubkey_hex: str,
        avatar_pixmap: Optional[QPixmap] = None,
    ) -> None:
        avatar = pixmap_for_profile(display_name, user_pubkey_hex, avatar_pixmap)
        self.setIcon(compose_chip_icon(avatar, self._chevron_color()))

        short = f"{user_pubkey_hex[:8]}…{user_pubkey_hex[-4:]}"
        self.setToolTip(f"{display_name} · {short}" if display_name else short)

    # -- theme --------------------------------------------------------------

    def set_dark_theme(self, is_dark: bool) -> None:
        if is_dark == self._is_dark:
            return
        self._is_dark = is_dark
        self._apply_chip_style()
        self._apply_menu_style()
        if self.toolTip() == "Connect Nostr":
            self.set_disconnected()

    # -- internals ----------------------------------------------------------

    def _chevron_color(self) -> QColor:
        return QColor("#CCCCCC") if self._is_dark else QColor("#555555")

    def _apply_chip_style(self) -> None:
        self.setStyleSheet(_DARK_CHIP_CSS if self._is_dark else _LIGHT_CHIP_CSS)

    def _apply_menu_style(self) -> None:
        self.menu().setStyleSheet(_DARK_MENU_CSS if self._is_dark else _LIGHT_MENU_CSS)
