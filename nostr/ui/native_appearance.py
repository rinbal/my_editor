# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Optional native chrome/preferences, with an opaque cross-platform fallback.

No frame replacement, style-mask changes, effects or extra dependencies. Rubicon
is already used by the app's macOS spelling and dictation integration.

The preferences read here: increased contrast and reduced transparency (for
Night Violet), and Reduce Motion (for nostr/ui/motion.py), on macOS, Windows
and desktops that follow GTK's settings. Each is read once, kept on the
application, and read again after the app comes back to the front, which is
when someone who changed it in the system's settings returns. GTK's GSettings
answer takes a program run, which happens in the background: until it
answers, the last answer counts, so nothing on screen waits for it.

GUI thread only: what is kept lives on the QApplication, as QObjects that are
its children (they go when it goes).
"""
from __future__ import annotations

import logging
import os
import shutil
import sys
from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer
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
# session bus, a broken dconf) is stopped after this, and the last answer
# stays.
GSETTINGS_TIMEOUT_MS = 1_000


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


def _gsettings_answer(output: str) -> bool | None:
    """What ``gsettings get`` printed for a boolean key: True, False, or
    None for anything else."""
    return {"true": True, "false": False}.get(output.strip())


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


def _gtk_reduce_motion(gsettings: bool | None) -> bool:
    """True when the desktop turned GTK's animations off: GSettings when
    it answered (what GNOME and KDE set), else the settings.ini files
    (what GTK reads where nothing else answers). The Settings portal's
    reduced-motion (xdg-desktop-portal 1.21) needs D-Bus and is not
    asked yet."""
    enabled = gsettings if gsettings is not None else _settings_ini_animations()
    return enabled is False


def _read_reduce_motion(platform: str = "") -> bool:
    """What the system says at once (``platform`` defaults to this one's
    ``sys.platform``): macOS and Windows all of it; on GTK desktops the
    settings.ini files, GSettings coming later (:class:`_MotionPreference`)."""
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
    return _gtk_reduce_motion(None)


class _MotionPreference(QObject):
    """Reduce Motion as the system last said it: one per application, its
    child (``reduce_motion_preference``). GUI thread only.

    Asked when an animation needs it, and again after the app came back
    to the front (:meth:`stale`). On a GTK desktop the GSettings answer
    comes from a ``gsettings`` run in a QProcess; until it arrives the last
    answer counts (at first the settings.ini files'), one that hangs is
    stopped after ``GSETTINGS_TIMEOUT_MS`` and changes nothing."""

    def __init__(self, parent: QObject, platform: str) -> None:
        super().__init__(parent)
        self._platform = platform
        self._value: bool | None = None
        self._stale = True
        self._process: QProcess | None = None

    @property
    def kept(self) -> bool:
        return bool(self._value)

    def value(self) -> bool:
        if self._stale:
            self._stale = False
            self._read()
        return self.kept

    def stale(self) -> None:
        """Read again when next asked."""
        self._stale = True

    def _read(self) -> None:
        if self._platform in ("darwin", "win32"):
            self._value = _read_reduce_motion(self._platform)
            return
        if self._value is None:
            self._value = _read_reduce_motion(self._platform)
        self._ask_gsettings()

    def _ask_gsettings(self) -> None:
        if self._process is not None:
            return                                  # one is on its way
        program = shutil.which("gsettings")
        if program is None:
            self._value = _gtk_reduce_motion(None)
            return
        import rmd_toolchain
        environment = QProcessEnvironment()
        for name, value in rmd_toolchain.program_environment().items():
            environment.insert(name, value)
        process = self._process = QProcess(self)
        process.setProcessEnvironment(environment)
        process.setStandardInputFile(QProcess.nullDevice())
        process.finished.connect(lambda code, status: self._finished(process, code, status))
        process.errorOccurred.connect(lambda error: self._error(process, error))
        QTimer.singleShot(GSETTINGS_TIMEOUT_MS, process, lambda: self._too_slow(process))
        process.start(program, ["get", GTK_SCHEMA, GTK_KEY])

    def _finished(self, process: QProcess, code: int, status) -> None:
        if process is not self._process:
            return
        answer = None
        if status == QProcess.ExitStatus.NormalExit and code == 0:
            answer = _gsettings_answer(
                process.readAllStandardOutput().data().decode("utf-8", "replace"))
        self._value = _gtk_reduce_motion(answer)
        self._done(process)

    def _error(self, process: QProcess, error) -> None:
        # A run that never started gets no ``finished``; a crash gets one.
        if process is self._process and error == QProcess.ProcessError.FailedToStart:
            _log.debug("gsettings did not start: %s", process.errorString())
            self._value = _gtk_reduce_motion(None)
            self._done(process)

    def _too_slow(self, process: QProcess) -> None:
        if process is self._process:
            _log.debug("gsettings gave no answer in %d ms", GSETTINGS_TIMEOUT_MS)
            self._done(process)
            process.kill()

    def _done(self, process: QProcess) -> None:
        self._process = None
        process.deleteLater()


def reduce_motion_preference() -> bool:
    """Whether the system asks for less motion, read once and kept until
    the app comes back to the front (see ``observe_preferences``). GUI
    thread only; asked from another thread, it says what is kept and
    starts nothing."""
    app = QApplication.instance()
    if app is None:
        return False
    kept = getattr(app, "_motion_preference", None)
    if not app.thread().isCurrentThread():
        return kept is not None and kept.kept
    observe_preferences()
    if kept is None:
        kept = app._motion_preference = _MotionPreference(app, sys.platform)
    return kept.value()


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
        motion = getattr(app, "_motion_preference", None)
        if motion is not None:
            # Read again when next asked, not here: on Linux the answer
            # costs a gsettings run, and most returns start no animation.
            motion.stale()
        before = preferences()
        app._night_violet_preferences = _read_preferences()
        if before != app._night_violet_preferences:
            from .appearance import refresh_windows
            import theme
            refresh_windows(theme.is_dark_active())

def observe_preferences() -> None:
    """Read the preferences again whenever the application's state or the
    contrast preference changes. GUI thread only. Once per application:
    the observer is the application's child and goes with it."""
    app = QApplication.instance()
    if app is None or hasattr(app, "_night_violet_observer"):
        return
    observer = app._night_violet_observer = _PreferenceObserver(app)
    app.applicationStateChanged.connect(observer.refresh)
    hints = app.styleHints()
    if hasattr(hints, "accessibility"):
        hints.accessibility().contrastPreferenceChanged.connect(observer.refresh)
