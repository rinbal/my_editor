#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Application-level color palette.

The window and its custom widgets are themed with hand-written Qt
stylesheets, but standard pop-ups (QMessageBox, QInputDialog), the
QTabBar overflow scroller and any other control nobody explicitly
skinned fall back to the platform style. With the Fusion style forced
in main.py, Qt honors the QPalette below consistently on every OS, so
those un-styled widgets follow the light/dark toggle automatically
instead of rendering in the native theme.

This is the single source of truth for the palette; colors come from
constants.py so there is no second list to keep in sync. Call
apply_app_theme() once at startup and again on every theme change.
"""

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from constants import (
    DARK_BG, DARK_FG, DARK_MENU_BG, DARK_MUTED_FG, DARK_SELECTION,
    LIGHT_BG, LIGHT_FG, LIGHT_MENU_BG, LIGHT_MUTED_FG, LIGHT_SELECTION,
)


def _palette(is_dark: bool) -> QPalette:
    if is_dark:
        bg, fg, menu_bg, muted_fg, sel = (
            DARK_BG, DARK_FG, DARK_MENU_BG, DARK_MUTED_FG, DARK_SELECTION
        )
    else:
        bg, fg, menu_bg, muted_fg, sel = (
            LIGHT_BG, LIGHT_FG, LIGHT_MENU_BG, LIGHT_MUTED_FG, LIGHT_SELECTION
        )

    role = QPalette.ColorRole
    group = QPalette.ColorGroup
    p = QPalette()

    # Window / dialog surfaces and their text.
    p.setColor(role.Window, QColor(bg))
    p.setColor(role.WindowText, QColor(fg))
    # Text-entry surfaces (QLineEdit in the rename dialog, etc.).
    p.setColor(role.Base, QColor(bg))
    p.setColor(role.AlternateBase, QColor(menu_bg))
    p.setColor(role.Text, QColor(fg))
    p.setColor(role.PlaceholderText, QColor(muted_fg))
    # Push buttons (message-box Yes / No / Cancel).
    p.setColor(role.Button, QColor(menu_bg))
    p.setColor(role.ButtonText, QColor(fg))
    # Tooltips (previously a hard-coded app stylesheet in main.py).
    p.setColor(role.ToolTipBase, QColor(menu_bg))
    p.setColor(role.ToolTipText, QColor(fg))
    # Selection highlight; white reads cleanly on both selection colors.
    p.setColor(role.Highlight, QColor(sel))
    p.setColor(role.HighlightedText, QColor("#FFFFFF"))
    p.setColor(role.Link, QColor(sel))
    p.setColor(role.LinkVisited, QColor(sel))
    p.setColor(role.Accent, QColor(sel))

    # Greyed-out text (disabled buttons, disabled menu entries) stays
    # legible but clearly muted on both themes.
    for r in (role.WindowText, role.Text, role.ButtonText):
        p.setColor(group.Disabled, r, QColor(muted_fg))

    return p


def apply_app_theme(is_dark: bool) -> None:
    """Push the light/dark palette onto the running QApplication.

    Fetches the application instance itself so callers do not need to
    import QApplication or thread it through. A no-op if no application
    exists yet (e.g. during import in tests).
    """
    app = QApplication.instance()
    if app is None:
        return
    app.setPalette(_palette(is_dark))
