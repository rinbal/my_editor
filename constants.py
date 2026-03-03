#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from PySide6.QtGui import QColor

# UI theme colors
DARK_BG = "#1E1E1E"
DARK_FG = "#D4D4D4"
DARK_SELECTION = "#264F78"
DARK_BORDER = "#3C3C3C"
DARK_MENU_BG = "#252526"
DARK_MENU_FG = "#CCCCCC"

LIGHT_BG = "#FFFFFF"
LIGHT_FG = "#333333"
LIGHT_SELECTION = "#0078D4"
LIGHT_BORDER = "#E1E1E1"
LIGHT_MENU_BG = "#F3F3F3"
LIGHT_MENU_FG = "#333333"

MONO_FONT = "Fira Code, JetBrains Mono, Consolas, 'Courier New', monospace"

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
