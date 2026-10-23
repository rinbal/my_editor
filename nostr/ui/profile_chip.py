# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Round profile-avatar chip with chevron, sits in the editor header.

The capsule uses the shared Nostr roles; the account menu keeps the
editor's existing neutral presentation. Avatar + chevron rendering lives in
``avatar.py`` and is shared with the in-dialog profile switcher.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import QMenu, QToolButton

from . import appearance
from i18n import _
from nostr.profiles import short_npub

from .avatar import (
    AVATAR_SIZE,
    CHIP_TOTAL_WIDTH,
    compose_chip_icon,
    make_disconnected_pixmap,
    pixmap_for_profile,
)


# --------------------------------------------------------------------------- #
# Editor palette, kept literally identical to widgets.py / editor.py          #
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

# --------------------------------------------------------------------------- #
# ProfileChip widget                                                          #
# --------------------------------------------------------------------------- #

class ProfileChip(QToolButton):
    """Header avatar chip, click opens a styled QMenu of profiles + actions."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._is_dark = True
        self._disconnected = True
        self.setPopupMode(QToolButton.InstantPopup)
        self.setToolButtonStyle(Qt.ToolButtonIconOnly)
        # Icon-only, so the accessible name is the only thing VoiceOver
        # and Full Keyboard Access have to announce this control by; the
        # description says whose account it is.
        self.setAccessibleName(_("Account"))
        self.setCursor(Qt.PointingHandCursor)
        self.setIconSize(QSize(CHIP_TOTAL_WIDTH, AVATAR_SIZE))
        self.setFixedHeight(32)

        self.setMenu(QMenu(self))
        self._apply_chip_style()
        self._apply_menu_style()
        self.set_disconnected()

    # -- state setters ------------------------------------------------------

    def set_disconnected(self) -> None:
        self._disconnected = True
        avatar = make_disconnected_pixmap(is_dark=self._is_dark)
        self._profile_avatar = avatar
        self.setIcon(compose_chip_icon(avatar, self._chevron_color()))
        self.setToolTip(_("Account"))
        self.setAccessibleDescription("")

    def set_profile(
        self,
        display_name: str,
        user_pubkey_hex: str,
        avatar_pixmap: Optional[QPixmap] = None,
    ) -> None:
        self._disconnected = False
        avatar = pixmap_for_profile(display_name, user_pubkey_hex, avatar_pixmap)
        self._profile_avatar = avatar
        self.setIcon(compose_chip_icon(avatar, self._chevron_color()))

        # The name people know the account by; the key only for one without.
        label = display_name or short_npub(user_pubkey_hex)
        self.setToolTip(label)
        self.setAccessibleDescription(label)

    # -- theme --------------------------------------------------------------

    def set_dark_theme(self, is_dark: bool) -> None:
        self._is_dark = is_dark
        self._apply_chip_style()
        self._apply_menu_style()
        appearance.bind(self, "set_dark_theme")
        if self._disconnected:
            self.set_disconnected()
        elif hasattr(self, "_profile_avatar"):
            self.setIcon(compose_chip_icon(self._profile_avatar, self._chevron_color()))

    # -- internals ----------------------------------------------------------

    def _chevron_color(self) -> QColor:
        return QColor(appearance.roles(self._is_dark).ink)

    def _apply_chip_style(self) -> None:
        r = appearance.roles(self._is_dark)
        self.setStyleSheet(f"""
            QToolButton {{ background: {r.soft}; color: {r.ink}; border: 1px solid {r.edge};
                border-radius: 13px; padding: 3px 8px; }}
            QToolButton:hover {{ background: {r.hover}; }}
            QToolButton:pressed {{ background: {r.tab}; }}
            QToolButton:focus {{ border: 2px solid {r.focus}; padding: 2px 7px; }}
            QToolButton::menu-indicator {{ image: none; width: 0; }}
        """)

    def _apply_menu_style(self) -> None:
        self.menu().setStyleSheet(_DARK_MENU_CSS if self._is_dark else _LIGHT_MENU_CSS)
