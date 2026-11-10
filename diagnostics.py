# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A log of what went wrong, for bug reports.

Once installed, the app runs without a terminal, so warnings and the
traces of unexpected errors would otherwise be lost. ``install`` sends
them to a small file instead: the newest megabyte, plus the one before
(``myeditor.log`` and ``myeditor.log.1``), and a crash of Qt or Python
itself to ``crash.log``. Help > Show Log Files opens the folder, so a
person can attach the files to a report.

Only what the app's own code logs goes in: warnings and errors, which
name relays, files and servers, never keys or private text, plus
Qt's own warnings. Everything still goes to the terminal as before
when there is one.
"""

from __future__ import annotations

import faulthandler
import logging
import logging.handlers
import os
import platform
import sys
import threading
from typing import Optional, TextIO

import user_dirs

LOG_NAME = "myeditor.log"
CRASH_NAME = "crash.log"
MAX_BYTES = 1024 * 1024
KEPT = 1                        # older files kept next to the current one

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

_handler: Optional[logging.Handler] = None
_crash_file: Optional[TextIO] = None
_reports_crashes = False        # whether crash traces go to crash.log


def install(version: str, *, folder: Optional[str] = None, level: int = logging.WARNING) -> bool:
    """Start writing the log. Returns False when the folder cannot be
    written; the app then runs as before, without a log."""
    global _handler, _crash_file, _reports_crashes
    if _handler is not None:
        return True
    folder = folder or log_folder()
    try:
        os.makedirs(folder, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            os.path.join(folder, LOG_NAME), maxBytes=MAX_BYTES, backupCount=KEPT,
            encoding="utf-8")
        crash_file = open(os.path.join(folder, CRASH_NAME), "a", encoding="utf-8")
    except OSError:
        return False
    handler.setFormatter(logging.Formatter(_FORMAT))
    handler.setLevel(level)
    root = logging.getLogger()
    root.addHandler(handler)
    if root.level > level or root.level == logging.NOTSET:
        root.setLevel(level)
    _handler, _crash_file = handler, crash_file
    # A crash of the interpreter (a segfault in Qt, say) leaves no Python
    # trace behind; faulthandler writes the stacks of every thread. Traces
    # that already go somewhere (python -X faulthandler, a test run) stay
    # there: faulthandler cannot say where, so they could not be given back.
    _reports_crashes = not faulthandler.is_enabled()
    if _reports_crashes:
        faulthandler.enable(file=crash_file, all_threads=True)
    _hook_uncaught()
    _hook_qt_messages()
    # One line per start that says what ran, so a report shows the
    # version without asking. Handed to the file directly, because the
    # file keeps only warnings and errors otherwise.
    handler.handle(logging.makeLogRecord({
        "name": "myeditor", "levelno": logging.INFO, "levelname": "INFO",
        "msg": "MyEditor %s started (Python %s, %s %s)",
        "args": (version, platform.python_version(), platform.system(), platform.release()),
    }))
    return True


def log_folder() -> str:
    """The logs in the person's cache folder, found each time (user_dirs)."""
    return str(user_dirs.cache_dir() / "logs")


def uninstall() -> None:
    """Stops writing the log (for tests)."""
    global _handler, _crash_file, _reports_crashes
    if _handler is not None:
        logging.getLogger().removeHandler(_handler)
        _handler.close()
        _handler = None
    if _reports_crashes:
        faulthandler.disable()
        _reports_crashes = False
    if _crash_file is not None:
        _crash_file.close()
        _crash_file = None
    sys.excepthook = sys.__excepthook__
    threading.excepthook = threading.__excepthook__
    try:
        from PySide6.QtCore import qInstallMessageHandler
    except ImportError:
        return
    qInstallMessageHandler(None)


# -- unexpected errors ---------------------------------------------------------

_log = logging.getLogger("myeditor")


def _hook_uncaught() -> None:
    # An error inside a Qt slot reaches sys.excepthook too; the app goes on
    # running, as it did before, but the trace is kept.
    shown = sys.excepthook

    def excepthook(kind, value, trace) -> None:
        if not issubclass(kind, KeyboardInterrupt):
            _log.error("Unexpected error", exc_info=(kind, value, trace))
        shown(kind, value, trace)

    shown_in_thread = threading.excepthook

    def thread_excepthook(args) -> None:
        if not issubclass(args.exc_type, SystemExit):
            _log.error("Unexpected error in thread %s",
                       getattr(args.thread, "name", "?"),
                       exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
        shown_in_thread(args)

    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook


def _hook_qt_messages() -> None:
    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler
    except ImportError:
        return
    levels = {
        QtMsgType.QtWarningMsg: logging.WARNING,
        QtMsgType.QtCriticalMsg: logging.ERROR,
        QtMsgType.QtFatalMsg: logging.CRITICAL,
    }
    qt_log = logging.getLogger("qt")

    def handler(kind, context, message: str) -> None:
        level = levels.get(kind)
        if level is not None:
            category = getattr(context, "category", None) or "default"
            qt_log.log(level, "%s: %s", category, message)
        # As before: Qt's messages also go to the terminal.
        _to_terminal(message + "\n")

    qInstallMessageHandler(handler)


def _to_terminal(text: str) -> None:
    """Writes to the terminal the app was started from, if there is one.

    A Windows build has none (sys.stderr is None), and a terminal closed
    since refuses the write. Neither may raise: PySide6 raises an error of
    the message handler again in the code that made Qt warn."""
    terminal = sys.stderr
    if terminal is None:
        return
    try:
        terminal.write(text)
    except (OSError, ValueError):       # closed, or no longer writable
        pass
