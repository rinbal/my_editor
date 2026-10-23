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

from typing import Optional

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPalette, QPen
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QProxyStyle, QStyle, QWidget

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
# The ring around the control that has the keyboard focus: the link blue
# around a plain button (3:1 or more against the button and the window),
# and around the accent-filled default button a color that stands out
# from the accent itself.
_FOCUS_RING = {True: "#4FA3F7", False: LIGHT_SELECTION}
_FOCUS_RING_ON_ACCENT = {True: "#FFFFFF", False: "#0A2540"}


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
    ``destructive``), each with a ring that shows when it has the keyboard
    focus (a thicker border, with the padding reduced to match, so the
    button keeps its size). Dialogs append their own rules after it.
    """
    if is_dark:
        bg, fg, muted, border, field = DARK_BG, DARK_FG, DARK_MUTED_FG, DARK_BORDER, DARK_MENU_BG
    else:
        bg, fg, muted, border, field = LIGHT_BG, LIGHT_FG, LIGHT_MUTED_FG, LIGHT_BORDER, LIGHT_MENU_BG
    destructive = _DIALOG_DESTRUCTIVE[is_dark]
    return f"""
    QDialog {{ background: {bg}; }}
    QLabel {{ color: {fg}; font-size: 13px; }}
    QLabel:disabled {{ color: {muted}; }}
    QPushButton {{
        background: {field}; color: {fg}; border: 1px solid {border};
        padding: 6px 14px; border-radius: 5px; min-width: 72px;
    }}
    QPushButton:hover {{ border-color: {muted}; }}
    QPushButton#destructive {{ color: {destructive}; }}
    QPushButton#destructive:hover {{ border-color: {destructive}; }}
    QPushButton:disabled {{ background: {field}; color: {muted}; border-color: {border}; }}
    {default_and_focus_css(is_dark)}
    """


def default_and_focus_css(is_dark: bool) -> str:
    """Style rules for a dialog's push buttons that show what the keyboard
    will do: the ring around the button with the focus (2 px, the padding
    giving up the extra pixel so the button keeps its size; 6 px by 14 px
    padding and a 1 px border are what it is drawn for) and the default
    button in the accent. Every dialog with its own button style appends
    them after it, so the default and the focus show there too.

    A destructive button is never the default by design, but on Windows and
    Linux the default moves to the button with the focus; it then keeps the
    plain fill and its red label (red on the accent cannot be read) and the
    ring that marks the focus."""
    ring = _FOCUS_RING[is_dark]
    field = DARK_MENU_BG if is_dark else LIGHT_MENU_BG
    destructive = _DIALOG_DESTRUCTIVE[is_dark]
    return (f"QPushButton:focus, QPushButton#destructive:focus "
            f"{{ border: 2px solid {ring}; padding: 5px 13px; }}\n"
            + accent_button_css("QPushButton:default", is_dark)
            + f"QPushButton#destructive:default {{ background: {field}; color: {destructive}; "
              f"border: 2px solid {ring}; padding: 5px 13px; }}\n")


def accent_button_css(selector: str, is_dark: bool) -> str:
    """Style rules that draw the buttons ``selector`` matches as the
    accent-filled button (a dialog's default action): the fill, its hover,
    with the keyboard focus a ring in the color that stands out from the
    fill (the plain ring would be blue on blue), and while it cannot be
    chosen the plain button's dimmed look, as a Mac dims a default button.
    The rules for the plain button must come first or be less specific."""
    accent, accent_hover = _DIALOG_ACCENT[is_dark]
    ring = _FOCUS_RING_ON_ACCENT[is_dark]
    if is_dark:
        field, muted, border = DARK_MENU_BG, DARK_MUTED_FG, DARK_BORDER
    else:
        field, muted, border = LIGHT_MENU_BG, LIGHT_MUTED_FG, LIGHT_BORDER
    return (f"{selector} {{ background: {accent}; color: #FFFFFF; border-color: {accent}; }}\n"
            f"{selector}:hover {{ background: {accent_hover}; border-color: {accent_hover}; }}\n"
            f"{selector}:focus {{ border: 2px solid {ring}; }}\n"
            f"{selector}:disabled {{ background: {field}; color: {muted}; "
            f"border-color: {border}; }}\n")


def focus_ring_color(is_dark: bool) -> str:
    """The color of the ring around a control with the keyboard focus, for
    dialogs that draw their own controls (link-like buttons)."""
    return _FOCUS_RING[is_dark]


def focus_ring_on_accent_color(is_dark: bool) -> str:
    """The ring's color on a control filled with the accent or its tint (a
    checked toolbar button), where the plain ring would not show."""
    return _FOCUS_RING_ON_ACCENT[is_dark]


def error_text_color(is_dark: bool) -> str:
    """Color of a short error said next to a field (why an address cannot
    be a link), the destructive red of the theme."""
    return _DIALOG_DESTRUCTIVE[is_dark]


def dialog_link_color(is_dark: bool) -> str:
    """Link color for rich-text labels in the app's dialogs.

    The palette's Link role doubles as the selection color, which is too
    dark to read as a link on the dark theme's background.
    """
    return _DIALOG_LINK[is_dark]


def attention_color(is_dark: bool) -> str:
    """The red that marks a problem (a destructive button, a failed check),
    readable on its theme's background."""
    return _DIALOG_DESTRUCTIVE[is_dark]


def check_box_border(palette: QPalette) -> QColor:
    """A check box's frame that people can see on either theme: the text
    colour over the field, about half and half, which keeps the 3:1 that
    controls need against the field and the window (Fusion's own frame
    is the window colour darkened, 1.07:1 on the dark theme)."""
    text = palette.color(QPalette.ColorRole.Text)
    base = palette.color(QPalette.ColorRole.Base)
    share = 0.55
    return QColor(round(text.red() * share + base.red() * (1 - share)),
                  round(text.green() * share + base.green() * (1 - share)),
                  round(text.blue() * share + base.blue() * (1 - share)))


class AppStyle(QProxyStyle):
    """Fusion, with check boxes and radio buttons whose frame can be seen
    on the dark theme too (review M5; Fusion's radio ring was as faint as
    its check box frame, so an unchecked choice in Create Account and in
    Settings showed no circle at all), and with every dialog's buttons in
    the order the HIG gives on every system: Cancel, then the default
    action at the trailing end, as MyEditor's alerts have them (a button
    box would otherwise put the default first on Windows and KDE, so one
    flow showed both orders). Everything else is Fusion's."""

    _BOXES = (QStyle.PrimitiveElement.PE_IndicatorCheckBox,
              QStyle.PrimitiveElement.PE_IndicatorItemViewItemCheck)
    _RADIO = QStyle.PrimitiveElement.PE_IndicatorRadioButton

    def styleHint(self, hint, option=None, widget=None, returnData=None):  # noqa: N802
        if hint == QStyle.StyleHint.SH_DialogButtonLayout:
            return QDialogButtonBox.ButtonLayout.MacLayout.value
        return super().styleHint(hint, option, widget, returnData)

    def drawPrimitive(self, element, option, painter, widget=None):  # noqa: N802
        super().drawPrimitive(element, option, painter, widget)
        if option is None or (element not in self._BOXES and element != self._RADIO):
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(check_box_border(option.palette), 1))
        frame = QRectF(option.rect).adjusted(0.5, 0.5, -0.5, -0.5)
        if element == self._RADIO:
            painter.drawEllipse(frame)
        else:
            painter.drawRoundedRect(frame, 2, 2)
        painter.restore()


# The system's own selection color (the accent color people choose in
# System Settings on a Mac, the Windows accent), read once before the
# app's palette replaces it; None until then.
_SYSTEM_HIGHLIGHT: Optional[QColor] = None


def apply_app_theme(is_dark: bool) -> None:
    """Push the light/dark palette onto the running QApplication.

    Fetches the application instance itself so callers do not need to
    import QApplication or thread it through. A no-op if no application
    exists yet (e.g. during import in tests).
    """
    global _SYSTEM_HIGHLIGHT
    app = QApplication.instance()
    if app is None:
        return
    if _SYSTEM_HIGHLIGHT is None:
        _SYSTEM_HIGHLIGHT = QColor(app.palette().color(QPalette.ColorRole.Highlight))
    app.setPalette(_palette(is_dark))
    # Nostr owners opt in; local application roles above stay unchanged.
    import sys
    appearance = sys.modules.get("nostr.ui.appearance")
    if appearance is not None:
        appearance.refresh_windows(is_dark)


def _luminance(colour: QColor) -> float:
    def channel(value: float) -> float:
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4
    return (0.2126 * channel(colour.redF()) + 0.7152 * channel(colour.greenF())
            + 0.0722 * channel(colour.blueF()))


def list_selection_color(is_dark: bool) -> QColor:
    """The fill of a selected row in a list that has the keyboard focus:
    the system's selection color, so it follows the accent color people
    chose, as every list on a Mac does; the theme's own blue where the
    system's would not carry white text at 4.5:1 (or is not known)."""
    system = _SYSTEM_HIGHLIGHT
    if system is not None and system.isValid():
        if (1.05) / (_luminance(system) + 0.05) >= 4.5:
            return QColor(system)
    return QColor(DARK_SELECTION if is_dark else LIGHT_SELECTION)


# Apple's system blue for each theme: the colour the Finder draws its
# sidebar glyphs and folders in, bright enough on its pane to be seen.
_ACCENT_ICON = {True: "#0A84FF", False: "#007AFF"}
# Non-text contrast the guidelines ask of a glyph that tells something.
ICON_CONTRAST = 3.0


def contrast_ratio(first: QColor, second: QColor) -> float:
    """The WCAG contrast ratio of two colours (1 to 21)."""
    lighter, darker = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def accent_icon_color(is_dark: bool) -> QColor:
    """The colour of a glyph in the accent colour on a pane (a sidebar's
    row icons, a folder in a list): the system's accent colour where it
    stands out at 3:1 from the panes of this theme, else Apple's system
    blue for the theme. Not the selection fill, which is darker in the
    dark theme so white text reads on it, and too dark for a glyph."""
    panes = (DARK_BG, DARK_MENU_BG) if is_dark else (LIGHT_BG, LIGHT_MENU_BG)
    system = _SYSTEM_HIGHLIGHT
    if system is not None and system.isValid() and all(
            contrast_ratio(system, QColor(p)) >= ICON_CONTRAST for p in panes):
        return QColor(system)
    return QColor(_ACCENT_ICON[is_dark])


def inactive_selection_color(is_dark: bool) -> QColor:
    """A selected row's fill while its list does not have the focus: a
    quiet grey, as a Mac list shows it, so only one place on screen wears
    the accent."""
    return QColor(255, 255, 255, 26) if is_dark else QColor(0, 0, 0, 20)


# Qt keeps a widget's parsed style sheet under the widget's address as soon
# as anything in it is measured, and forgets it when the widget goes only if
# the widget was polished. A window given a style sheet and dropped without
# ever being shown (a dialog built and then not needed) left its style sheet
# behind under its address, and the next window allocated at that address
# drew with it (review M1: Publish in the Media Library's grey, not the
# accent). So every widget is polished as soon as it is given a style sheet.
#
# The wrapper below covers every call made from Python, which is every call
# in this app. It cannot see a call Qt makes in C++ (a .ui file loaded with
# QUiLoader, a style sheet set from C++ code), nor a bound method taken
# before it was installed; polish_style_sheets covers those, and
# QApplication.setStyleSheet is not wrapped at all. The app loads no .ui
# files and sets no application style sheet, and a test keeps it so
# (tests/test_theme.py).

# Qt's own method, also after this module is reloaded (then the method on
# QWidget is the wrapper already, which keeps the original).
_set_style_sheet = getattr(QWidget.setStyleSheet, "_qt_set_style_sheet",
                           QWidget.setStyleSheet)


def _set_style_sheet_and_polish(self: QWidget, styleSheet: str) -> None:  # noqa: N803
    """QWidget.setStyleSheet, then the widget polished (see above)."""
    _set_style_sheet(self, styleSheet)
    if styleSheet and not self.testAttribute(Qt.WidgetAttribute.WA_WState_Polished):
        self.ensurePolished()


_set_style_sheet_and_polish._qt_set_style_sheet = _set_style_sheet


def polish_style_sheets(root: Optional[QWidget] = None) -> None:
    """Polish every widget with a style sheet that is not polished yet: in
    ``root`` and under it, or everywhere. For style sheets set past the
    wrapper (before it was installed, or by Qt itself)."""
    widgets = ([root, *root.findChildren(QWidget)] if root is not None
               else QApplication.allWidgets())
    for widget in widgets:
        if (widget.styleSheet()
                and not widget.testAttribute(Qt.WidgetAttribute.WA_WState_Polished)):
            widget.ensurePolished()


def style_sheet_guard_installed() -> bool:
    """Whether every style sheet set from Python is polished at once."""
    return getattr(QWidget.setStyleSheet, "_qt_set_style_sheet", None) is not None


def install_style_sheet_guard(app: QApplication) -> None:
    """Polish every widget a style sheet is set on from Python, and those
    given one before (polish_style_sheets). ``app``: the application, which
    must exist."""
    if QWidget.setStyleSheet is not _set_style_sheet_and_polish:
        QWidget.setStyleSheet = _set_style_sheet_and_polish
    polish_style_sheets()
