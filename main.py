#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# MyEditor built by rinbal


import os
import sys
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QIcon
from PySide6.QtNetwork import QLocalSocket
from main_window import MainWindow
import constants

_IPC_SERVER_NAME = "minimal-texteditor-ipc"


class EditorApplication(QApplication):
    """QApplication subclass that handles macOS Finder file-open (odoc) events.

    When a user double-clicks a document in Finder, macOS does not pass the
    path as a command-line argument. Qt receives it as a QFileOpenEvent
    instead. A cold launch can deliver this event before the main window
    exists, so paths are buffered here until register_main_window() is
    called."""

    def __init__(self, argv):
        super().__init__(argv)
        self._main_window = None
        self._pending_paths = []

    def event(self, event):
        if event.type() == QEvent.Type.FileOpen:
            path = event.file()
            if not path:
                return True
            if self._main_window is not None:
                self._main_window.open_path(path)
                self._main_window.raise_()
                self._main_window.activateWindow()
            else:
                self._pending_paths.append(path)
            return True
        return super().event(event)

    def register_main_window(self, win):
        self._main_window = win
        for path in self._pending_paths:
            win.open_path(path)
        self._pending_paths.clear()


def resource_path(rel_path: str) -> str:
    """Resolve a bundled resource, working both from source and when frozen.

    PyInstaller unpacks bundled data under sys._MEIPASS; from source we resolve
    relative to this file (the repo root). The spec bundles assets preserving
    their repo-relative path, so the same rel_path works in both cases."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel_path)


def _forward_to_running_instance(path: str) -> bool:
    """Send a file path to an already-running instance via a local socket.
    Returns True if a running instance was found and the path was forwarded."""
    socket = QLocalSocket()
    socket.connectToServer(_IPC_SERVER_NAME)
    if not socket.waitForConnected(300):
        return False
    socket.write((path + "\n").encode("utf-8"))
    socket.waitForBytesWritten(300)
    socket.disconnectFromServer()
    return True


def main():
    app = EditorApplication(sys.argv)
    app.setApplicationName(constants.APP_DISPLAY_NAME)
    app.setApplicationDisplayName(constants.APP_DISPLAY_NAME)
    app.setApplicationVersion(constants.APP_VERSION)
    # Link the window to the installed .desktop launcher so Linux desktops show
    # the app icon in the dock / task switcher instead of a generic one. No-op
    # on Windows and macOS.
    app.setDesktopFileName("my-editor")
    app.setWindowIcon(QIcon(resource_path("packaging/icons/icon-256.png")))
    # Fusion honors the QPalette consistently on every OS, so un-styled
    # widgets (message boxes, input dialogs, the tab scroller) follow the
    # in-app light/dark theme instead of the native platform look. The
    # authoritative palette is set from MainWindow._apply_theme during
    # startup and on every toggle; see theme.apply_app_theme.
    app.setStyle("Fusion")

    initial_path = sys.argv[1] if len(sys.argv) > 1 else None

    if initial_path and _forward_to_running_instance(initial_path):
        sys.exit(0)

    win = MainWindow(initial_path)
    win.show()
    app.register_main_window(win)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
