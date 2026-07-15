"""Application palette behavior and contrast guarantees."""

from __future__ import annotations

import pytest
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

import theme
from constants import (
    DARK_BG,
    DARK_FG,
    DARK_MENU_BG,
    DARK_MUTED_FG,
    DARK_SELECTION,
    LIGHT_BG,
    LIGHT_FG,
    LIGHT_MENU_BG,
    LIGHT_MUTED_FG,
    LIGHT_SELECTION,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    original_palette = app.palette()
    yield app
    app.setPalette(original_palette)


@pytest.mark.parametrize(
    ("is_dark", "bg", "fg", "menu_bg", "muted_fg", "selection"),
    [
        (True, DARK_BG, DARK_FG, DARK_MENU_BG, DARK_MUTED_FG, DARK_SELECTION),
        (False, LIGHT_BG, LIGHT_FG, LIGHT_MENU_BG, LIGHT_MUTED_FG, LIGHT_SELECTION),
    ],
)
def test_palette_maps_semantic_colors(
    is_dark, bg, fg, menu_bg, muted_fg, selection
):
    palette = theme._palette(is_dark)
    role = QPalette.ColorRole
    group = QPalette.ColorGroup

    expected_roles = {
        role.Window: bg,
        role.WindowText: fg,
        role.Base: bg,
        role.AlternateBase: menu_bg,
        role.Text: fg,
        role.PlaceholderText: muted_fg,
        role.Button: menu_bg,
        role.ButtonText: fg,
        role.ToolTipBase: menu_bg,
        role.ToolTipText: fg,
        role.Highlight: selection,
        role.HighlightedText: "#FFFFFF",
        role.Link: selection,
        role.LinkVisited: selection,
        role.Accent: selection,
    }
    for color_role, expected in expected_roles.items():
        assert palette.color(group.Active, color_role) == QColor(expected)

    for color_role in (role.WindowText, role.Text, role.ButtonText):
        assert palette.color(group.Disabled, color_role) == QColor(muted_fg)


@pytest.mark.parametrize(
    ("foreground", "background"),
    [
        (DARK_MUTED_FG, DARK_BG),
        (DARK_MUTED_FG, DARK_MENU_BG),
        (LIGHT_MUTED_FG, LIGHT_BG),
        (LIGHT_MUTED_FG, LIGHT_MENU_BG),
    ],
)
def test_muted_text_meets_normal_text_contrast(foreground, background):
    assert _contrast_ratio(foreground, background) >= 4.5


@pytest.mark.parametrize("is_dark", [True, False])
def test_apply_app_theme_updates_running_application(qapp, is_dark):
    theme.apply_app_theme(is_dark)
    assert qapp.palette() == theme._palette(is_dark)


def _contrast_ratio(foreground: str, background: str) -> float:
    lighter, darker = sorted(
        (_relative_luminance(foreground), _relative_luminance(background)),
        reverse=True,
    )
    return (lighter + 0.05) / (darker + 0.05)


def _relative_luminance(hex_color: str) -> float:
    color = QColor(hex_color)
    channels = (color.redF(), color.greenF(), color.blueF())
    linear = [
        value / 12.92
        if value <= 0.04045
        else ((value + 0.055) / 1.055) ** 2.4
        for value in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]
