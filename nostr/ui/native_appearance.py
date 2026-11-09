# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Optional native chrome/preferences, with an opaque cross-platform fallback.

No frame replacement, style-mask changes, effects or extra dependencies. Rubicon
is already used by the app's macOS spelling and dictation integration.

The preferences read here: increased contrast and reduced transparency (for
Night Violet), and Reduce Motion (for nostr/ui/motion.py), on macOS, Windows
and desktops that follow GTK's settings. Each is read once, kept on the
application, and read again after the app comes back to the front, which is
when someone who changed it in the system's settings returns.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from PySide6.QtCore import QObject
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication

_log = logging.getLogger(__name__)


def _workspace():
    """NSWorkspace's shared instance, where the macOS accessibility switches
    are read; None off macOS, under another Qt platform (offscreen tests)
    or without Rubicon."""
    app = QApplication.instance()
    if sys.platform != "darwin" or app is None or app.platformName() != "cocoa":
        return None
    try:
        from rubicon.objc import ObjCClass
        return ObjCClass("NSWorkspace").sharedWorkspace
    except (ImportError, AttributeError, RuntimeError):
        return None


def _read_preferences() -> tuple[bool, bool]:
    app = QApplication.instance()
    if app is None:
        return False, False
    high = False
    hints = app.styleHints()
    if hasattr(hints, "accessibility"):
        high = hints.accessibility().contrastPreference().name == "HighContrast"
    workspace = _workspace()
    if workspace is not None:
        try:
            return (high or bool(workspace.accessibilityDisplayShouldIncreaseContrast),
                    bool(workspace.accessibilityDisplayShouldReduceTransparency))
        except (AttributeError, RuntimeError):
            pass
    return high, False


# -- Reduce Motion ------------------------------------------------------------
#
# Qt has no motion preference to ask (QAccessibilityHints in Qt 6.11 offers
# only the contrast), so each system is asked the way its own toolkit does.

# SystemParametersInfoW: "Determines whether animations are enabled or
# disabled. The pvParam parameter must point to a BOOL variable that receives
# TRUE if animations are enabled" (Microsoft's documentation). Chromium
# takes prefers-reduced-motion from it alone (animation_win.cc), and that
# follows the Animation effects switch in Windows' accessibility settings.
SPI_GETCLIENTAREAANIMATION = 0x1042

# GTK's switch. GNOME's Settings and KDE's kde-gtk-config both set the
# GSettings key, which is where GTK itself reads it on Wayland
# (gdkscreen-wayland.c maps org.gnome.desktop.interface enable-animations
# to gtk-enable-animations); kde-gtk-config also writes the key into the
# settings.ini files, which are GTK's own fallback without a settings
# daemon. Chromium and Firefox take gtk-enable-animations off for
# prefers-reduced-motion on Linux.
GTK_SCHEMA = "org.gnome.desktop.interface"
GTK_KEY = "enable-animations"
GTK_SETTING = "gtk-enable-animations"
# gsettings answers from dconf in a few milliseconds; one that hangs (no
# session bus, a broken dconf) must not hold up the animation that asked.
GSETTINGS_TIMEOUT_S = 1.0


def _windows_animations_off() -> bool:
    """True when Windows' "Animation effects" is off. A call that fails
    counts as on, as in Chromium: never take motion from someone who did
    not ask for it to go."""
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32")
        ask = user32.SystemParametersInfoW
        ask.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT]
        ask.restype = wintypes.BOOL
        enabled = wintypes.BOOL(True)
        if not ask(SPI_GETCLIENTAREAANIMATION, 0, ctypes.byref(enabled), 0):
            return False
        return not enabled.value
    except (AttributeError, OSError, ValueError):
        return False


def _gsettings_animations() -> bool | None:
    """GNOME's switch as gsettings reads it: True, False, or None when there
    is no answer (no gsettings, no such schema, too slow)."""
    program = shutil.which("gsettings")
    if program is None:
        return None
    import rmd_toolchain
    try:
        done = subprocess.run([program, "get", GTK_SCHEMA, GTK_KEY], capture_output=True,
                              text=True, timeout=GSETTINGS_TIMEOUT_S, check=False,
                              stdin=subprocess.DEVNULL,
                              env=rmd_toolchain.program_environment())
    except (OSError, subprocess.SubprocessError) as exc:
        _log.debug("gsettings gave no answer: %s", exc)
        return None
    answer = done.stdout.strip() if done.returncode == 0 else ""
    return {"true": True, "false": False}.get(answer)


def _gtk_settings_files() -> list[str]:
    """GTK's settings.ini files, the person's own first: GTK 4 and GTK 3
    under $XDG_CONFIG_HOME, then under each of $XDG_CONFIG_DIRS, then
    /etc (the places GTK's documentation names)."""
    home = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    system = [d for d in (os.environ.get("XDG_CONFIG_DIRS") or "/etc/xdg").split(":") if d]
    return [os.path.join(base, version, "settings.ini")
            for base in (home, *system, "/etc") for version in ("gtk-4.0", "gtk-3.0")]


def _settings_ini_animations() -> bool | None:
    """``gtk-enable-animations`` from the first settings.ini that sets it,
    read as GLib reads a boolean (true or 1, false or 0); None when none
    does."""
    from default_apps import read_key_file
    for path in _gtk_settings_files():
        value = read_key_file(path).get("Settings", {}).get(GTK_SETTING)
        answer = {"true": True, "1": True, "false": False, "0": False}.get((value or "").strip())
        if answer is not None:
            return answer
    return None


def _gtk_animations_off() -> bool:
    """True when the desktop turned GTK's animations off: GSettings first
    (what GNOME and KDE set), then the settings.ini files (what GTK reads
    where nothing else answers). The Settings portal's reduced-motion
    (xdg-desktop-portal 1.21) needs D-Bus and is not asked yet."""
    enabled = _gsettings_animations()
    if enabled is None:
        enabled = _settings_ini_animations()
    return enabled is False


def _read_reduce_motion(platform: str = "") -> bool:
    """Ask the system (``platform`` defaults to this one's ``sys.platform``)."""
    platform = platform or sys.platform
    if platform == "darwin":
        workspace = _workspace()
        try:
            return bool(workspace is not None
                        and workspace.accessibilityDisplayShouldReduceMotion)
        except (AttributeError, RuntimeError):
            return False
    if platform == "win32":
        return _windows_animations_off()
    return _gtk_animations_off()


def reduce_motion_preference() -> bool:
    """Whether the system asks for less motion, read once and kept until
    the app comes back to the front (see ``observe_preferences``)."""
    app = QApplication.instance()
    if app is None:
        return False
    observe_preferences()
    if not hasattr(app, "_reduce_motion_preference"):
        app._reduce_motion_preference = _read_reduce_motion()
    return app._reduce_motion_preference


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
        if hasattr(app, "_reduce_motion_preference"):
            # Read again when next asked, not here: on Linux the answer
            # costs a gsettings run, and most returns start no animation.
            del app._reduce_motion_preference
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
