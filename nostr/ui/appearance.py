# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Night Violet presentation. Ownership is explicit; the application stays neutral.

Roles feed palettes, stylesheets and painters. ``surface`` decorates only the
chosen container; content children keep their own neutral palette/style. Register
an existing theme method with ``bind`` to include a detached window in appearance
changes. No account state, timers, document formats or operations live here.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from functools import lru_cache

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QLinearGradient, QPainter, QPalette, QPixmap, QRadialGradient
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication, QComboBox, QWidget
from format_toolbar import Disclosure

import theme


@dataclass(frozen=True)
class Roles:
    chrome: str
    side: str
    list: str
    panel: str
    text: str
    muted: str
    field: str
    control: str
    line: str
    edge: str
    popover: str
    hover: str
    tab: str
    icon: str
    ink: str
    selection: str
    button: str
    highlight: str
    pressed: str
    soft: str
    focus: str
    on_accent: str
    disabled: str
    glow_alpha: int
    orange: str = "#F7931A"
    dusk: str = "#16131C"
    amount: str = "#8A4800"
    error: str = "#A52A1D"
    success: str = "#26783D"
    payment_pressed: str = "#DD7A0A"
    glow: str = "#8C5CFF"


_LIGHT = Roles(
    chrome="#F3EEFB", side="#EEE7F9", list="#FBF9FE", panel="#F5F0FC",
    text="#1F1A2B", muted="#625971", field="#FFFFFF", control="#FFFFFF",
    line="#DBD0ED", edge="#8A7A9F", popover="#FFFFFF", hover="#E9E0F7",
    tab="#EEE6FA", icon="#7136DD", ink="#5B28BE", selection="#7136DD",
    button="#7136DD", highlight="#8050DC", pressed="#5925B7", soft="#E7DCF8",
    focus="#7136DD", on_accent="#FFFFFF", disabled="#83788F", glow_alpha=51)
_DARK = Roles(
    chrome="#1E1828", side="#201A2B", list="#1B1623", panel="#1E1828",
    text="#F0EBF9", muted="#B7ACC9", field="#2A2436", control="#2E273A",
    line="#40334F", edge="#827392", popover="#272033", hover="#362B47",
    tab="#261E33", icon="#B89CFF", ink="#CDB9FF", selection="#6A43D8",
    button="#6E40E2", highlight="#8050DC", pressed="#5830BB", soft="#3D2E57",
    focus="#B89CFF", on_accent="#FFFFFF", disabled="#85798F", glow_alpha=66,
    amount="#FFB561", error="#FF8C82", success="#58C777")


def roles(dark: bool, *, increased_contrast: bool | None = None,
          reduced_effects: bool | None = None) -> Roles:
    from .native_appearance import preferences
    high, reduced = preferences()
    high = high if increased_contrast is None else increased_contrast
    reduced = reduced if reduced_effects is None else reduced_effects
    r = _DARK if dark else _LIGHT
    if high:
        r = replace(r, muted=r.text, edge=r.ink, line=r.ink, disabled=r.muted,
                    focus=r.ink, glow_alpha=0)
    return replace(r, glow_alpha=0) if reduced else r


def palette(dark: bool, *, surface: str = "panel") -> QPalette:
    r = roles(dark)
    p = theme._palette(dark)
    pairs = {QPalette.Window: getattr(r, surface), QPalette.WindowText: r.text,
             QPalette.Base: r.field, QPalette.AlternateBase: r.side,
             QPalette.Text: r.text, QPalette.PlaceholderText: r.muted,
             QPalette.Button: r.control, QPalette.ButtonText: r.text,
             QPalette.Mid: r.line, QPalette.Dark: r.edge,
             QPalette.ToolTipBase: r.popover, QPalette.ToolTipText: r.text,
             QPalette.Highlight: r.selection, QPalette.HighlightedText: "#FFFFFF",
             QPalette.Link: r.ink, QPalette.LinkVisited: r.ink, QPalette.Accent: r.icon}
    for role, color in pairs.items():
        p.setColor(role, QColor(color))
    p.setColor(QPalette.Inactive, QPalette.Highlight, QColor(r.soft))
    p.setColor(QPalette.Inactive, QPalette.HighlightedText, QColor(r.text))
    for role in (QPalette.Text, QPalette.WindowText, QPalette.ButtonText):
        p.setColor(QPalette.Disabled, role, QColor(r.disabled))
    return p


def gradient(dark: bool, rect: QRectF) -> QLinearGradient:
    r = roles(dark)
    result = QLinearGradient(rect.topLeft(), rect.bottomLeft())
    result.setColorAt(0, QColor(r.highlight))
    result.setColorAt(1, QColor(r.selection))
    return result


def accent_button_css(selector: str, dark: bool, *, payment: bool = False) -> str:
    r = roles(dark)
    fill = (r.orange if payment else
            f"qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 {r.highlight},stop:1 {r.button})")
    ink = r.dusk if payment else "#FFFFFF"
    return f"""
    {selector} {{ background: {fill}; color: {ink}; border-color: {r.button if not payment else r.orange}; }}
    {selector}:hover {{ background: {r.orange if payment else r.highlight}; }}
    {selector}:pressed {{ background: {r.payment_pressed if payment else r.pressed}; }}
    {selector}:focus {{ border: 2px solid {r.on_accent if not payment else r.dusk}; }}
    {selector}:disabled {{ background: {r.control}; color: {r.disabled}; border-color: {r.line}; }}
    """


def controls(dark: bool) -> str:
    r = roles(dark)
    error = r.error
    return f"""
    QLabel {{ color: {r.text}; background: transparent; }}
    QLabel:disabled {{ color: {r.disabled}; }}
    QPushButton {{ background: {r.control}; color: {r.text}; border: 1px solid {r.edge};
        padding: 6px 14px; border-radius: 7px; min-width: 72px; }}
    QPushButton:hover {{ background: {r.hover}; border-color: {r.ink}; }}
    QPushButton:pressed {{ background: {r.soft}; }}
    QPushButton:focus {{ border: 2px solid {r.focus}; padding: 5px 13px; }}
    QPushButton:disabled {{ color: {r.disabled}; border-color: {r.line}; background: {r.control}; }}
    QLineEdit, QPlainTextEdit, QComboBox, QSpinBox, QDateTimeEdit {{
        color: {r.text}; background: {r.field}; border: 1px solid {r.edge};
        border-radius: 7px; padding: 4px 6px; selection-background-color: {r.selection};
        selection-color: #FFFFFF; placeholder-text-color: {r.muted}; }}
    QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QSpinBox:focus {{ border-color: {r.focus}; }}
    QLineEdit:disabled, QPlainTextEdit:disabled, QComboBox:disabled {{ color: {r.disabled}; }}
    QComboBox {{ padding-right: 24px; }}
    QComboBox::drop-down {{ border: none; width: 20px; background: transparent; }}
    QComboBox::down-arrow {{ image: none; }}
    QComboBox QAbstractItemView {{ background: {r.popover}; color: {r.text};
        border: 1px solid {r.edge}; selection-background-color: {r.soft}; selection-color: {r.text}; }}
    QListWidget {{ background: {r.list}; color: {r.text}; border: 1px solid {r.edge};
        selection-background-color: {r.selection}; selection-color: #FFFFFF; }}
    QListWidget::item:selected {{ background: {r.selection}; color: #FFFFFF; }}
    QListWidget::item:selected:!active {{ background: {r.soft}; color: {r.text}; }}
    QListWidget::item:focus {{ border: 1px solid {r.focus}; }}
    QToolButton {{ color: {r.ink}; border: 1px solid transparent; border-radius: 5px; padding: 3px; }}
    QToolButton:hover {{ background: {r.hover}; }}
    QToolButton:pressed, QToolButton:checked {{ background: {r.soft}; }}
    QToolButton:focus {{ border-color: {r.focus}; }}
    QToolButton:disabled {{ color: {r.disabled}; }}
    QProgressBar {{ background: {r.soft}; border: none; border-radius: 3px; }}
    QProgressBar::chunk {{ background: {r.icon}; border-radius: 3px; }}
    QSlider::groove:horizontal {{ height: 4px; background: {r.soft}; border: 1px solid {r.edge}; border-radius: 2px; }}
    QSlider::sub-page:horizontal {{ background: {r.icon}; border-radius: 2px; }}
    QSlider::handle:horizontal {{ background: {r.control}; border: 1px solid {r.ink};
        width: 12px; margin: -5px 0; border-radius: 5px; }}
    QSlider::handle:horizontal:focus {{ border: 2px solid {r.focus}; }}
    QSlider::sub-page:horizontal:disabled {{ background: {r.disabled}; }}
    QSlider::handle:horizontal:disabled {{ border-color: {r.disabled}; }}
    QScrollArea {{ border: none; background: transparent; }}
    QToolTip {{ color: {r.text}; background: {r.popover}; border: 1px solid {r.edge}; }}
    {accent_button_css('QPushButton:default', dark)}
    QPushButton#destructive, QPushButton#destructive:default {{
        color: {error}; background: {r.control}; border-color: {r.edge}; }}
    QPushButton#destructive:focus {{ border-color: {error}; }}
    """


def dialog_stylesheet(dark: bool) -> str:
    return f"QDialog {{ background: {roles(dark).panel}; }}\n" + controls(dark)


def neutral(widget: QWidget, dark: bool) -> None:
    """Reset palette inheritance at a document, image or profile boundary.

    The content's own stylesheet remains authoritative. Callers also retain the
    existing neutral rules for fields and labels within that content.
    """
    widget.setPalette(theme._palette(dark))


class _Glow(QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        self.dark = False
        self.membership = False
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.NoFocus)
        parent.installEventFilter(self)
        self.setGeometry(parent.rect())
        self.lower()
        self.show()

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Resize:
            self.setGeometry(obj.rect())
        elif event.type() == QEvent.Show:
            if obj.property("nightVioletSurface"):
                _decorate_controls(obj, self.dark)
            if obj.isWindow():
                from .native_appearance import apply_chrome
                apply_chrome(obj, self.dark, nostr=bool(obj.property("nightVioletSurface")))
        return False

    def paintEvent(self, event):
        r = roles(self.dark)
        if not r.glow_alpha:
            return
        painter = QPainter(self)
        # Static light, behind child widgets. Never overlays images or text.
        glow = QRadialGradient(QPointF(35, 20), 380)
        color = QColor(r.glow)
        color.setAlpha(r.glow_alpha)
        glow.setColorAt(0, color)
        color.setAlpha(0)
        glow.setColorAt(1, color)
        painter.fillRect(self.rect(), glow)
        if self.membership:
            warm = QRadialGradient(QPointF(self.width(), self.height()), 380)
            color = QColor(r.orange)
            color.setAlpha(r.glow_alpha)
            warm.setColorAt(0, color)
            color.setAlpha(0)
            warm.setColorAt(1, color)
            painter.fillRect(self.rect(), warm)


def surface(widget: QWidget, dark: bool, *, kind: str = "panel", glow: bool = True,
            membership: bool = False) -> None:
    widget.setPalette(palette(dark, surface=kind))
    widget.setProperty("nightVioletSurface", kind)
    widget.setAutoFillBackground(True)
    _decorate_controls(widget, dark)
    if glow:
        decoration = getattr(widget, "_night_violet_glow", None)
        if decoration is None:
            decoration = widget._night_violet_glow = _Glow(widget)
        decoration.show()
        decoration.dark = dark
        decoration.membership = membership
        decoration.update()
    if widget.isVisible() and widget.isWindow():
        from .native_appearance import apply_chrome
        apply_chrome(widget, dark)


def bind(widget: QWidget, method: str = "apply_theme") -> None:
    """An explicit presentation owner; Qt owns its lifetime, no signal registry."""
    widget.setProperty("nightVioletThemeMethod", method)
    from .native_appearance import observe_preferences
    observe_preferences()


def refresh_windows(dark: bool) -> None:
    for widget in QApplication.allWidgets():
        method = widget.property("nightVioletThemeMethod")
        if method:
            getattr(widget, method)(dark)


def relay_icon(dark: bool, size: int = 18, *, color: str | None = None) -> QIcon:
    # The immutable reference's relay mark; rasterized at the current DPR.
    return _relay_icon(color or roles(dark).icon, size)


def transport_icon(icon: QIcon, dark: bool, size: int = 18) -> QIcon:
    """Retain Qt's transport shapes while making every mode readable."""
    r = roles(dark)
    result = QIcon()
    for mode, ink in ((QIcon.Normal, r.icon), (QIcon.Active, r.ink),
                      (QIcon.Disabled, r.disabled)):
        for scale in (1, 2, 3):
            pixmap = icon.pixmap(size * scale, size * scale, QIcon.Normal)
            painter = QPainter(pixmap)
            painter.setCompositionMode(QPainter.CompositionMode_SourceIn)
            painter.fillRect(pixmap.rect(), QColor(ink))
            painter.end()
            pixmap.setDevicePixelRatio(scale)
            result.addPixmap(pixmap, mode)
    return result


@lru_cache(maxsize=48)
def _relay_icon(ink: str, size: int) -> QIcon:
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20">
    <g fill="none" stroke="{ink}" stroke-width="1.7" stroke-linecap="round">
    <path d="M6.3 6.4a5.1 5.1 0 0 0 0 7.2 M13.7 6.4a5.1 5.1 0 0 1 0 7.2
    M3.6 3.8a8.8 8.8 0 0 0 0 12.4 M16.4 3.8a8.8 8.8 0 0 1 0 12.4"/></g>
    <circle cx="10" cy="10" r="2.3" fill="{ink}"/></svg>'''
    result = QIcon()
    for scale in (1, 2, 3):
        pixmap = QPixmap(size * scale, size * scale)
        pixmap.setDevicePixelRatio(scale)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        QSvgRenderer(svg.encode()).render(painter, QRectF(0, 0, size, size))
        painter.end()
        result.addPixmap(pixmap)
    return result


def membership_background(painter: QPainter, rect: QRectF, radius: float) -> None:
    """Dusk card/tiles with static violet and orange corner light."""
    r = roles(True)
    painter.setBrush(QColor(r.dusk))
    painter.drawRoundedRect(rect, radius, radius)
    if not r.glow_alpha:
        return
    for corner, color in ((rect.bottomLeft(), r.glow), (rect.topRight(), r.orange)):
        glow = QRadialGradient(corner, rect.width() * .7)
        ink = QColor(color)
        ink.setAlpha(70)
        glow.setColorAt(0, ink)
        ink.setAlpha(0)
        glow.setColorAt(1, ink)
        painter.setBrush(glow)
        painter.drawRoundedRect(rect, radius, radius)


def popover(widget: QWidget, dark: bool) -> None:
    r = roles(dark)
    surface(widget, dark, kind="popover", glow=False)
    widget.setStyleSheet(controls(dark) + f"""
        QFrame#{widget.objectName()} {{ background: {r.popover}; border: 1px solid {r.edge}; border-radius: 8px; }}
        QLabel#imports_hint, QLabel#imports_job_counts, QLabel#imports_job_stage {{ color: {r.muted}; }}
        QLabel#imports_job_problem {{ color: {r.error}; }}
    """)


class _Disclosure(Disclosure):
    def ink(self) -> QColor:
        r = roles(self._dark)
        return QColor(r.icon if self.parentWidget().isEnabled() else r.disabled)


def _decorate_controls(widget: QWidget, dark: bool) -> None:
    # Reuse the app's vector disclosure; Qt's stylesheet arrow is a dot on
    # Cocoa and a beveled block on Fusion. Clicks still reach the real combo.
    for combo in widget.findChildren(QComboBox):
        arrow = getattr(combo, "_night_violet_arrow", None)
        if arrow is None:
            arrow = combo._night_violet_arrow = _Disclosure(combo, end=7, dark=dark)
        arrow.set_dark(dark)
        arrow.raise_()
