# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins how a private key leaves the clipboard again.

What must hold:

  A copied secret is taken off the clipboard after its time, and when
  MyEditor quits, but only while it is still what the clipboard holds:
  nothing the person copied afterwards is ever cleared.

  A secret still waiting for its time is also cleared when Python exits
  without the app quitting first, and the exit stays clean.

  The secret is marked as one, so clipboard managers and clipboard history
  skip it: on a Mac with the real pasteboard type (Qt would rename a custom
  type), on KDE and on Windows with their own hints.
"""

import os
import subprocess
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from nostr.ui import assistant  # noqa: E402
from nostr.ui.assistant import copy_secret  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


@pytest.fixture(autouse=True)
def clean_clipboard(qt_app):
    assistant.clear_pending_secrets()
    qt_app.clipboard().clear()
    yield
    assistant.clear_pending_secrets()
    qt_app.clipboard().clear()


def settle():
    for _ in range(3):
        QApplication.processEvents()


def test_the_secret_leaves_the_clipboard_when_its_time_is_up(qt_app):
    copy_secret("nsec1first", seconds=0)
    assert qt_app.clipboard().text() == "nsec1first"
    settle()
    assert qt_app.clipboard().text() == ""


def test_what_was_copied_afterwards_is_never_cleared(qt_app):
    copy_secret("nsec1first", seconds=0)
    qt_app.clipboard().setText("my own note")
    settle()
    assert qt_app.clipboard().text() == "my own note"


def test_quitting_clears_a_secret_still_waiting_for_its_time(qt_app):
    copy_secret("nsec1first", seconds=3600)
    assert assistant._quit_hooked
    assistant.clear_pending_secrets()          # what aboutToQuit runs
    assert qt_app.clipboard().text() == ""
    assert assistant._pending_clears == []
    qt_app.clipboard().setText("my own note")
    assistant.clear_pending_secrets()
    assert qt_app.clipboard().text() == "my own note"


def test_exiting_without_quitting_clears_the_secret_and_exits_cleanly():
    # A script, a test run or any exit that skips aboutToQuit. Qt tears its
    # clipboard down after Python is gone; data made in Python still on it
    # then crashed the exit after everything had worked.
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = (
        "import sys\n"
        "from PySide6.QtWidgets import QApplication\n"
        "app = QApplication(sys.argv)\n"
        "from nostr.ui.assistant import copy_secret\n"
        "copy_secret('nsec1waiting')\n"
        "print(app.clipboard().text())\n"
    )
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    done = subprocess.run([sys.executable, "-c", script], cwd=root, env=env,
                          capture_output=True, text=True, timeout=60)
    assert done.stdout.strip() == "nsec1waiting"
    assert done.returncode == 0, done.stderr[-500:]

def test_the_secret_is_marked_for_clipboard_managers(qt_app):
    copy_secret("nsec1first", seconds=3600)
    formats = qt_app.clipboard().mimeData().formats()
    assert assistant.CONCEALED_MAC in formats
    assert assistant.CONCEALED_KDE in formats
    assert bytes(qt_app.clipboard().mimeData().data(assistant.CONCEALED_KDE)) == b"secret"
    assert assistant.CONCEALED_WINDOWS in formats


@pytest.mark.skipif(sys.platform != "darwin", reason="the Mac pasteboard")
def test_on_a_mac_the_pasteboard_carries_the_concealed_type_itself():
    # A private pasteboard, never the person's own clipboard.
    board = assistant._MacPasteboard(f"org.myeditor.test-{uuid.uuid4().hex}")
    c = board._c

    def types():
        def read():
            listed = board._msg(board._pasteboard(), b"types")
            count = board._msg(listed, b"count", restype=c.c_ulong)
            return [c.string_at(board._msg(board._msg(listed, b"objectAtIndex:", i,
                                                      argtypes=(c.c_ulong,)),
                                           b"UTF8String")).decode()
                    for i in range(count)]
        return board._in_pool(read)

    def text():
        def read():
            value = board._msg(board._pasteboard(), b"stringForType:",
                               board._string("public.utf8-plain-text"),
                               argtypes=(c.c_void_p,))
            return c.string_at(board._msg(value, b"UTF8String")).decode() if value else None
        return board._in_pool(read)

    try:
        clear_first = board.put_secret("nsec1first")
        assert text() == "nsec1first" and assistant.CONCEALED_MAC in types()
        clear_second = board.put_secret("nsec1second")
        clear_first()                          # something else was copied since
        assert text() == "nsec1second"
        clear_second()
        assert text() is None
    finally:
        board._in_pool(lambda: board._msg(board._pasteboard(), b"releaseGlobally"))
