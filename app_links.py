# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Handing a link of an app's own scheme (``nostrconnect://`` for a signer
app) to the app this computer opens such links with, and saying honestly
whether one took it: :data:`OPENED`, :data:`NO_APP`, or :data:`UNKNOWN`
when the system cannot tell.

Not through QDesktopServices: QUrl refuses a nostrconnect link, whose key
is a 64-character host label where a label has at most 63
(tests/test_app_links.py pins it), so every system would be told nothing.

Nothing waits on the GUI thread. The answer comes on a later turn:

  macOS    ``open <link>``, run as a child process: LaunchServices answers
           at once, 0 when an app took the link.
  Windows  the registry first: a scheme an app registered is
           ``HKCR\\<scheme>`` with ``URL Protocol`` and a
           ``shell\\open\\command``. Without one, Windows would show its
           own "find an app" prompt and still report success, so the
           answer is no app. With one, the shell opens it (os.startfile)
           on a worker thread.
  Linux    ``xdg-mime query default x-scheme-handler/<scheme>`` first: no
           handler is a clear no (xdg-open would hand the link to a web
           browser and report success). With one, ``xdg-open`` is started
           detached and never waited for: on a desktop it does not
           recognise, it runs the handler in the foreground. When xdg-mime
           cannot answer, the link is still handed over, and the answer is
           that it cannot tell.

The link carries the pairing secret: neither it nor an error that quotes
it is ever logged, only its scheme. It is handed to the opening program
as an argument, as every one of them takes a link; an app set up for
such links is started with it the same way, so that is the system's way
of passing it, not this module's. Windows is not verified on a device.
"""

from __future__ import annotations

import importlib
import logging
import os
import sys
import threading
from typing import Callable, List, Optional

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, Qt, QTimer, Signal, Slot

log = logging.getLogger(__name__)

OPENED = "opened"
NO_APP = "no_app"
UNKNOWN = "unknown"

# The longest wait for ``open`` or ``xdg-mime`` to answer; after it, the
# answer is that the system could not tell.
ANSWER_WAIT_MS = 10_000

_MAC, _WINDOWS, _LINUX = "mac", "windows", "linux"


def _platform() -> str:
    if sys.platform == "darwin":
        return _MAC
    if sys.platform == "win32":
        return _WINDOWS
    return _LINUX


def is_app_link(uri: str) -> bool:
    """Whether ``uri`` is a link of an app's own scheme, and nothing else:
    printable ASCII, no spaces, ``<letters>://``."""
    return (isinstance(uri, str) and uri.isascii() and uri.isprintable() and " " not in uri
            and "://" in uri and uri.split("://", 1)[0].isalpha())


def _scheme(uri: str) -> str:
    return uri.split("://", 1)[0].lower()


# -- running a program of the system, never waited for on the GUI thread ----

_running: List[QObject] = []            # kept alive until they answer


def _environment() -> QProcessEnvironment:
    import rmd_toolchain
    environment = QProcessEnvironment()
    for name, value in rmd_toolchain.program_environment().items():
        environment.insert(name, value)
    return environment


def _run(program: str, arguments: List[str], on_exit: Callable[[Optional[int], str], None],
         *, timeout_ms: Optional[int] = None) -> None:
    """Run ``program`` as a child, and ``on_exit(code, output)`` once it
    ends: ``code`` None when it could not start or ran past
    ``timeout_ms`` (ANSWER_WAIT_MS; it is then killed). Never logs its
    arguments."""
    timeout_ms = ANSWER_WAIT_MS if timeout_ms is None else timeout_ms
    process = QProcess()
    process.setProcessEnvironment(_environment())
    process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
    process.setStandardInputFile(QProcess.nullDevice())
    timer = QTimer(process)
    timer.setSingleShot(True)
    answered = []

    def finish(code: Optional[int]) -> None:
        if answered:
            return
        answered.append(code)
        timer.stop()
        output = ""
        if code is not None:
            output = bytes(process.readAllStandardOutput()).decode("utf-8", "replace")
        if process in _running:
            _running.remove(process)
        process.deleteLater()
        on_exit(code, output)

    def ran_out() -> None:
        log.info("%s did not answer within %d ms", program, timeout_ms)
        process.kill()
        finish(None)

    def failed(error) -> None:
        if error == QProcess.ProcessError.FailedToStart:
            log.info("%s could not be started", program)
            QTimer.singleShot(0, lambda: finish(None))

    process.finished.connect(
        lambda code, status: finish(code if status == QProcess.ExitStatus.NormalExit else None))
    process.errorOccurred.connect(failed)
    timer.timeout.connect(ran_out)
    _running.append(process)
    timer.start(timeout_ms)
    process.start(program, arguments)


def _start_detached(program: str, arguments: List[str]) -> bool:
    """Start ``program`` on its own, never waited for; whether it started."""
    process = QProcess()
    process.setProgram(program)
    process.setArguments(arguments)
    process.setProcessEnvironment(_environment())
    process.setStandardInputFile(QProcess.nullDevice())
    for channel in (process.setStandardOutputFile, process.setStandardErrorFile):
        channel(QProcess.nullDevice())
    started = process.startDetached()           # (bool, pid) in some PySide6 versions
    return bool(started[0] if isinstance(started, tuple) else started)


class _Answer(QObject):
    """Brings a worker thread's answer to the GUI thread: made there, so
    its slot runs there (a queued connection)."""
    given = Signal(str)

    def __init__(self, done: Callable[[str], None]) -> None:
        super().__init__()
        self._done = done
        self.given.connect(self._deliver, Qt.ConnectionType.QueuedConnection)

    @Slot(str)
    def _deliver(self, answer: str) -> None:
        if self in _running:
            _running.remove(self)
        self.deleteLater()
        self._done(answer)


def _in_worker(work: Callable[[], str], done: Callable[[str], None]) -> None:
    """``work()`` on a thread of its own; ``done(answer)`` on the GUI thread,
    :data:`UNKNOWN` when ``work`` raised (its error goes on, to the
    thread's excepthook: a programming error is never swallowed) or no
    thread could be started."""
    bridge = _Answer(done)
    _running.append(bridge)

    def run() -> None:
        answer = UNKNOWN
        try:
            answer = work()
        finally:
            bridge.given.emit(answer)

    try:
        threading.Thread(target=run, daemon=True).start()
    except RuntimeError:                                    # "can't start new thread"
        log.info("no thread could be started to hand a link over")
        bridge.given.emit(UNKNOWN)


def _later(done: Callable[[str], None], answer: str) -> None:
    QTimer.singleShot(0, lambda: done(answer))


# -- the platforms --------------------------------------------------------------

def windows_registered(scheme: str, registry=None) -> bool:
    """Whether an app registered ``scheme`` with Windows: ``HKCR\\<scheme>``
    names itself a ``URL Protocol`` and says how to open it
    (``shell\\open\\command``). ``registry`` is ``winreg`` (a seam for
    tests)."""
    winreg = registry if registry is not None else importlib.import_module("winreg")
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, scheme) as key:
            winreg.QueryValueEx(key, "URL Protocol")
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, scheme + r"\shell\open\command") as key:
            command, _kind = winreg.QueryValueEx(key, "")
    except OSError:
        return False
    return bool(str(command or "").strip())


def _shell_open(uri: str) -> str:
    """The Windows shell opens ``uri`` with its registered app (worker
    thread). The error is named by its kind only: its text quotes the link."""
    try:
        os.startfile(uri)                                   # noqa: S606, the registered app
    except OSError as error:
        log.info("the shell opened no app for a %s link (%s)", _scheme(uri),
                 type(error).__name__)
        return NO_APP
    return OPENED


def _open_on_windows(uri: str, done: Callable[[str], None], registry=None) -> None:
    scheme = _scheme(uri)
    try:
        registered = windows_registered(scheme, registry)
    except ImportError:                                     # a Python without winreg
        log.info("the registry could not be read for %s links", scheme)
        registered = None
    if registered is False:
        log.info("no app is registered for %s links", scheme)
        _later(done, NO_APP)
        return
    _in_worker(lambda: _shell_open(uri), done if registered else
               (lambda answer: done(UNKNOWN if answer == OPENED else answer)))


def _open_on_mac(uri: str, done: Callable[[str], None]) -> None:
    def exited(code: Optional[int], _output: str) -> None:
        if code is None:
            done(UNKNOWN)
        elif code == 0:
            done(OPENED)
        else:
            log.info("no app took a %s link", _scheme(uri))
            done(NO_APP)
    _run("open", [uri], exited)


def _open_on_linux(uri: str, done: Callable[[str], None]) -> None:
    scheme = _scheme(uri)

    def hand_over(answer_if_started: str) -> None:
        if _start_detached("xdg-open", [uri]):
            done(answer_if_started)
        else:
            log.info("xdg-open could not be started for a %s link", scheme)
            done(NO_APP)

    def queried(code: Optional[int], output: str) -> None:
        if code == 0 and not output.strip():
            log.info("no app is set up for %s links", scheme)
            done(NO_APP)
        elif code == 0:
            hand_over(OPENED)
        else:
            # xdg-mime is missing or could not answer: handed over all the
            # same, and said as it is.
            hand_over(UNKNOWN)

    _run("xdg-mime", ["query", "default", f"x-scheme-handler/{scheme}"], queried)


def open_link(uri: str, done: Callable[[str], None], *, registry=None) -> None:
    """Hand ``uri`` to the app this computer opens its links with, and
    ``done(answer)`` on a later turn: :data:`OPENED`, :data:`NO_APP`
    (also for anything that is not such a link), or :data:`UNKNOWN`.
    ``registry`` is Windows' ``winreg`` (a seam for tests)."""
    if not is_app_link(uri):
        _later(done, NO_APP)
        return
    platform = _platform()
    if platform == _MAC:
        _open_on_mac(uri, done)
    elif platform == _WINDOWS:
        _open_on_windows(uri, done, registry)
    else:
        _open_on_linux(uri, done)


__all__ = ["ANSWER_WAIT_MS", "NO_APP", "OPENED", "UNKNOWN", "is_app_link", "open_link",
           "windows_registered"]
