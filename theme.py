#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later

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
    DARK_BG, DARK_BORDER, DARK_FG, DARK_MENU_BG, DARK_MUTED_FG, DARK_SELECTION,
    LIGHT_BG, LIGHT_BORDER, LIGHT_FG, LIGHT_MENU_BG, LIGHT_MUTED_FG, LIGHT_SELECTION,
)

# Dialog buttons, per theme: the default action in the accent color and a
# destructive action in red text, as Apple's alerts do. Every color here is
# chosen against its own theme's surfaces, so no button can end up as light
# text on a light background.
_DIALOG_ACCENT = {True: ("#007ACC", "#1177C7"), False: (LIGHT_SELECTION, "#106EBE")}
_DIALOG_DESTRUCTIVE = {True: "#FF6B5E", False: "#C0392B"}
_DIALOG_LINK = {True: "#4FA3F7", False: LIGHT_SELECTION}


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


def is_dark_active() -> bool:
    """Whether the dark palette is the one currently applied.

    Read back from the application palette that apply_app_theme() sets, so
    dialogs can follow the theme without it being passed to every call.
    """
    app = QApplication.instance()
    return app is not None and app.palette().color(QPalette.Window).lightness() < 128


def dialog_stylesheet(is_dark: bool) -> str:
    """Base stylesheet for the app's own dialogs (alerts.py, update_dialog.py).

    Covers the surface, text, and the three kinds of push button: normal,
    default (``setDefault(True)``), and destructive (objectName
    ``destructive``). Dialogs append their own rules after it.
    """
    if is_dark:
        bg, fg, muted, border, field = DARK_BG, DARK_FG, DARK_MUTED_FG, DARK_BORDER, DARK_MENU_BG
    else:
        bg, fg, muted, border, field = LIGHT_BG, LIGHT_FG, LIGHT_MUTED_FG, LIGHT_BORDER, LIGHT_MENU_BG
    accent, accent_hover = _DIALOG_ACCENT[is_dark]
    destructive = _DIALOG_DESTRUCTIVE[is_dark]
    return f"""
    QDialog {{ background: {bg}; }}
    QLabel {{ color: {fg}; font-size: 13px; }}
    QPushButton {{
        background: {field}; color: {fg}; border: 1px solid {border};
        padding: 6px 14px; border-radius: 5px; min-width: 72px;
    }}
    QPushButton:hover {{ border-color: {muted}; }}
    QPushButton:default {{ background: {accent}; color: #FFFFFF; border-color: {accent}; }}
    QPushButton:default:hover {{ background: {accent_hover}; border-color: {accent_hover}; }}
    QPushButton#destructive {{ color: {destructive}; }}
    QPushButton#destructive:hover {{ border-color: {destructive}; }}
    """


def dialog_link_color(is_dark: bool) -> str:
    """Link color for rich-text labels in the app's dialogs.

    The palette's Link role doubles as the selection color, which is too
    dark to read as a link on the dark theme's background.
    """
    return _DIALOG_LINK[is_dark]


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
