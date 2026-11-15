# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins what the status bar says about files added in the Media Library
that did not upload after its window was closed.

What must hold:

  Each failure is counted: one names the file, several say how many,
  and every reason is in the tooltip. It stays, as a backup problem
  does, until the person opens the Media Library (where each is shown
  with Retry), however many other messages come and go; Open Media
  Library beside it does that. A failure while the library is open is
  said there, not here. It shortens in a narrow window instead of
  widening it, and gives way to another message there: shortened, or
  aside where not even a few letters of it fit, it leaves the message
  its room, and takes it back when the message goes. The word count and
  the caret's line step aside before it does.
"""

from tests.app_process import run_window_script  # noqa: E402

SCRIPT = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv[:1])
import main_window
from tests.accessibility import unnamed_controls

w = main_window.MainWindow()
# The status bar's own width is measured: the folder sidebar, whose
# narrowest width the window keeps, is put away.
w._library_window.set_pane_shown("sidebar", False)
w.resize(1100, 700)
w.show()
app.processEvents()
store = w._media_store
store.can_retry = lambda name: True
opened = []
w._on_nostr_media_library = lambda: opened.append(True) or w._clear_library_failures()


def shown():
    return [w._library_problem.isVisible(), w._library_problem.full_text(),
            w._library_open.isVisible()]


r = {"quiet": shown()}
store.upload_failed.emit("Holiday.png", "Your media server is busy: try again later.")
app.processEvents()
r["one"] = shown()
for name in ("Talk.mp4", "Scan.pdf"):
    store.upload_failed.emit(name, "Couldn’t reach your media server: check your internet "
                                   "connection and try again.")
app.processEvents()
r["three"] = shown()
r["tooltip"] = w._library_problem.toolTip()
w.status.showMessage("Saved: /tmp/x.txt", 0)
app.processEvents()
r["after_other_message"] = shown()
r["names"] = unnamed_controls(w.statusBar())
r["min_width"] = w.minimumSizeHint().width()
# A narrow window: the failure gives way to a message, shortened, and
# takes its room back when the message goes.
message = "Saved: /Users/me/Documents/Essay.md"
w.resize(600, 600)
w.status.showMessage(message, 0)
app.processEvents()
r["narrow_room"] = [w._library_problem.geometry().left(),
                    w.status.fontMetrics().horizontalAdvance(message)]
r["narrow_text"] = w._library_problem.text()
w.status.clearMessage()
app.processEvents()
r["narrow_quiet_text"] = w._library_problem.text()
w.resize(1100, 700)
app.processEvents()
# Narrower still, with a document whose word count shows: the word count
# gives way first, and comes back when the message goes.
editor = w.new_tab()
editor.insertPlainText("A few words in a document, enough to be counted here. " * 40)
app.processEvents()
w._update_word_count()
app.processEvents()
r["counted"] = w._words_label.text()
r["widths"] = {}
for width in (1, 480, 560, 640):                  # 1: as narrow as the window goes
    w.status.clearMessage()
    app.processEvents()
    w.resize(width, 600)
    w.status.showMessage(message, 0)
    app.processEvents()
    app.processEvents()
    # Up to the first permanent widget shown, even one without width.
    left = min(x.geometry().left() for x in (w._library_problem, w._words_label,
                                              w._library_open, w._line_label)
               if x.isVisible())
    r["widths"][w.width()] = [left, w.status.fontMetrics().horizontalAdvance(message)]
w.status.clearMessage()
app.processEvents()
app.processEvents()
r["words_back"] = w._words_label.width() > 0
w.resize(1, 600)
w.status.showMessage(message, 0)
app.processEvents()
w.status.clearMessage()
app.processEvents()
app.processEvents()
r["failure_back"] = [w._library_problem.width() > 0, w._library_open.width() > 0]
editor.document().setModified(False)
w.resize(1100, 700)
app.processEvents()
w._library_open.click()
app.processEvents()
r["opened"] = opened
r["after_open"] = shown()
w.close()
print("RESULT " + json.dumps(r))
"""


def test_failures_after_the_library_closed_are_said_and_stay_until_it_opens(tmp_path):
    r = run_window_script(SCRIPT, tmp_path)
    assert r["quiet"] == [False, "", False]
    assert r["one"] == [True, "“Holiday.png” wasn’t added to your Media Library.", True]
    # Lessons 36: never how many; each file and its sentence is the tooltip.
    assert r["three"] == [True, "Some files weren’t added to your Media Library.", True]
    assert r["tooltip"].splitlines() == [
        "“Holiday.png” wasn’t added to your Media Library. Your media server is busy: "
        "try again later.",
        "“Talk.mp4” wasn’t added to your Media Library. Couldn’t reach your media server: "
        "check your internet connection and try again.",
        "“Scan.pdf” wasn’t added to your Media Library. Couldn’t reach your media server: "
        "check your internet connection and try again.",
    ]
    assert r["after_other_message"] == r["three"]
    assert r["names"] == []
    assert r["min_width"] < 420
    room, needed = r["narrow_room"]
    # QStatusBar draws the message from 6 points in to 2 before the
    # first permanent widget.
    assert room >= needed + 8
    assert r["narrow_text"].endswith("…")
    assert len(r["narrow_quiet_text"]) > len(r["narrow_text"])
    assert r["counted"]
    for width, (room, needed) in r["widths"].items():
        assert room >= needed + 8, f"the message is cut at {width} points"
    assert r["words_back"]
    assert r["failure_back"] == [True, True]
    assert r["opened"] == [True]
    assert r["after_open"] == [False, "", False]
