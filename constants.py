#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys

from PySide6.QtGui import QColor

# Product identity. These drive the packaging pipeline (PyInstaller spec,
# Windows installer, macOS Info.plist, Linux AppImage) and the in-app title.
# Bump APP_VERSION and tag the release "v<APP_VERSION>" to ship a new build.
APP_VERSION = "3.0"
APP_DISPLAY_NAME = "MyEditor"   # shown in title bars and OS menus
APP_BINARY_NAME = "my-editor"             # executable / file name, no spaces
APP_BUNDLE_ID = "com.rinbal.myeditor"     # reverse-DNS id; change to your own
APP_REPO_SLUG = "rinbal/my_editor"        # canonical GitHub repo (owner/name)
APP_URL = f"https://github.com/{APP_REPO_SLUG}"
APP_RELEASES_URL = f"{APP_URL}/releases/latest"

# UI theme colors
DARK_BG = "#1E1E1E"
DARK_FG = "#D4D4D4"
DARK_SELECTION = "#264F78"
DARK_BORDER = "#3C3C3C"
DARK_MENU_BG = "#252526"
DARK_MENU_FG = "#CCCCCC"
# Secondary text used for placeholders and disabled controls. Keep this
# distinct from DARK_BORDER: borders can be subtle, but text must remain
# readable against both DARK_BG and DARK_MENU_BG.
DARK_MUTED_FG = "#8F8F8F"

LIGHT_BG = "#FFFFFF"
LIGHT_FG = "#333333"
LIGHT_SELECTION = "#0078D4"
LIGHT_BORDER = "#E1E1E1"
LIGHT_MENU_BG = "#F3F3F3"
LIGHT_MENU_FG = "#333333"
# Meets normal-text contrast against both LIGHT_BG and LIGHT_MENU_BG while
# remaining visually subordinate to LIGHT_FG.
LIGHT_MUTED_FG = "#6D6D6D"

# Background viewing aids (patterns, current-line band, paper surface).
# Low-alpha guide colors so they whisper behind text on both themes.
DARK_GUIDE = QColor(255, 255, 255, 22)
LIGHT_GUIDE = QColor(0, 0, 0, 26)
DARK_CURRENT_LINE = QColor(255, 255, 255, 14)
LIGHT_CURRENT_LINE = QColor(0, 0, 0, 13)
# Opaque page surface for Paper Mode (slightly offset from the base bg so the
# centered page reads as a sheet floating on the window backdrop).
DARK_PAPER = QColor(38, 38, 38)     # #262626 on a #1E1E1E backdrop
LIGHT_PAPER = QColor(250, 249, 246) # #FAF9F6 on a #FFFFFF backdrop

# Pick a monospace font that ships with the host OS, otherwise Qt warns
# at startup ("Populating font family aliases took N ms") on every run.
if sys.platform == "darwin":
    MONO_FONT = "Menlo"
elif sys.platform == "win32":
    MONO_FONT = "Consolas"
else:
    MONO_FONT = "Noto Sans Mono"

# Single universal color palette — mid-range saturation (Material Design 600).
# These colors are clearly visible on both dark (#1E1E1E) and light (#FFFFFF) backgrounds,
# so no remapping is needed when switching themes or exporting to PDF/HTML.
TEXT_COLORS = {
    "Red":    QColor(229, 57,  53 ),   # #E53935
    "Green":  QColor(67,  160, 71 ),   # #43A047
    "Orange": QColor(251, 140, 0  ),   # #FB8C00
    "Yellow": QColor(249, 168, 37 ),   # #F9A825  (golden yellow)
    "Blue":   QColor(30,  136, 229),   # #1E88E5
    "Purple": QColor(142, 36,  170),   # #8E24AA
}

COLOR_MAP = TEXT_COLORS
