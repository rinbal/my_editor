# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Optional native chrome/preferences, with an opaque cross-platform fallback.

No frame replacement, style-mask changes, effects or extra dependencies. Rubicon
is already used by the app's macOS spelling and dictation integration.
"""
from __future__ import annotations

import sys
from PySide6.QtCore import QObject
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication


def _read_preferences() -> tuple[bool, bool]:
    app = QApplication.instance()
    if app is None:
        return False, False
    high = False
    hints = app.styleHints()
    if hasattr(hints, "accessibility"):
        high = hints.accessibility().contrastPreference().name == "HighContrast"
    if sys.platform == "darwin" and app.platformName() == "cocoa":
        try:
            from rubicon.objc import ObjCClass
            workspace = ObjCClass("NSWorkspace").sharedWorkspace
            return (high or bool(workspace.accessibilityDisplayShouldIncreaseContrast),
                    bool(workspace.accessibilityDisplayShouldReduceTransparency))
        except (ImportError, AttributeError, RuntimeError):
            pass
    return high, False


def apply_chrome(widget, dark: bool, *, nostr: bool = True) -> bool:
    app = QApplication.instance()
    if sys.platform != "darwin" or app.platformName() != "cocoa":
        return False
    try:
        from ctypes import c_void_p
        from rubicon.objc import ObjCClass, ObjCInstance
        from .appearance import roles
        view = ObjCInstance(c_void_p(int(widget.winId())))
        window = view.window
        if window is None:
            return False
        import theme
        color = QColor(roles(dark).chrome) if nostr else theme._palette(dark).window().color()
        window.backgroundColor = ObjCClass("NSColor").colorWithSRGBRed_green_blue_alpha_(
            color.redF(), color.greenF(), color.blueF(), 1.0)
        window.titlebarAppearsTransparent = nostr
        window.appearance = ObjCClass("NSAppearance").appearanceNamed_(
            "NSAppearanceNameDarkAqua" if dark else "NSAppearanceNameAqua")
        return True
    except (ImportError, AttributeError, RuntimeError, ValueError):
        return False


def preferences() -> tuple[bool, bool]:
    app = QApplication.instance()
    if app is None:
        return False, False
    if not hasattr(app, "_night_violet_preferences"):
        app._night_violet_preferences = _read_preferences()
    return app._night_violet_preferences


class _PreferenceObserver(QObject):
    def refresh(self, *_):
        app = QApplication.instance()
        before = preferences()
        app._night_violet_preferences = _read_preferences()
        if before != app._night_violet_preferences:
            from .appearance import refresh_windows
            import theme
            refresh_windows(theme.is_dark_active())

def observe_preferences() -> None:
    app = QApplication.instance()
    if app is None or hasattr(app, "_night_violet_observer"):
        return
    observer = app._night_violet_observer = _PreferenceObserver(app)
    app.applicationStateChanged.connect(observer.refresh)
    hints = app.styleHints()
    if hasattr(hints, "accessibility"):
        hints.accessibility().contrastPreferenceChanged.connect(observer.refresh)
