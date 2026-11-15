# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The PDF reader's keys and the window's commands never cancel out.

The PDF reader binds keys of its own (Ctrl+0, Ctrl+1 and Ctrl+2 for
its zoom, Ctrl+C to copy its selection), and the window has commands
on some of the same keys (Heading 1 and 2 are Ctrl+1 and Ctrl+2 on
Windows and Linux, Edit > Copy is Ctrl+C). Two shortcuts on one key are
ambiguous to Qt, and then neither fires. What must hold:

  In a PDF tab, Ctrl+0, Ctrl+1 and Ctrl+2 reach the reader's zoom and
  Ctrl+C copies the reader's selection; no editing command runs (Body is
  Ctrl+0 on Windows and Linux).

  In a text tab, the same keys reach the editing commands.

On a Mac the reader's zoom keys are Option-Command-0, 1 and 2, the
styles' keys there, and Command-0 is Actual Size: the reader's in a PDF
tab, View > Actual Size in a text tab.

Run in a child process with a home folder of its own, as every test
that builds the whole window is (tests/app_process.py), so the real
settings are never touched; the window's keys are those of the platform
the test names (Windows and Linux, or a Mac), whatever runs it.
"""

import pytest

from tests.app_process import run_window_script

SCRIPT = r"""
import json, os, sys, types
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtCore import Qt
from PySide6.QtGui import QTextDocument
from PySide6.QtPdfWidgets import QPdfView
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv[:1])
import commands
# The keys of the platform named: Heading 1 is Ctrl+1 on Windows and
# Linux, Option-Command-1 on a Mac.
commands.sys = types.SimpleNamespace(platform=sys.argv[2])
mac = sys.argv[2] == "darwin"
CTRL = Qt.KeyboardModifier.ControlModifier
STYLE = CTRL | Qt.KeyboardModifier.AltModifier if mac else CTRL
import main_window
from export_pdf import export_pdf

home = os.environ["HOME"]
pdf = os.path.join(home, "sample.pdf")
doc = QTextDocument()
doc.setPlainText("needle in the haystack\n" + "plain body line\n" * 80)
export_pdf(doc, pdf, title="Sample")

w = main_window.MainWindow()
w.resize(1100, 720)
w.show()
w.activateWindow()
QTest.qWaitForWindowExposed(w)
ran = []
for action in (w.commands.action("format.style.h1"), w.commands.action("format.style.h2"),
               w.commands.action("format.style.body"), w.commands.action("view.actual_size")):
    action.triggered.connect(lambda _c=False, n=action.objectName(): ran.append(n))
result = {}

viewer = w.open_path(pdf)
app.processEvents()
view = viewer.view
view.setFocus()
app.processEvents()
result["pdf_focus"] = app.focusWidget() is view
def actual_size():
    return view.zoomMode() == QPdfView.ZoomMode.Custom and abs(view.zoomFactor() - 1.0) < 1e-6
QTest.keyClick(view, Qt.Key.Key_2, STYLE)
result["ctrl2_fit_page"] = view.zoomMode() == QPdfView.ZoomMode.FitInView
QTest.keyClick(view, Qt.Key.Key_1, STYLE)
result["ctrl1_actual_size"] = actual_size()
QTest.keyClick(view, Qt.Key.Key_0, STYLE)
result["ctrl0_fit_width"] = view.zoomMode() == QPdfView.ZoomMode.FitToWidth
QTest.keyClick(view, Qt.Key.Key_0, CTRL)
result["cmd0_actual_size"] = actual_size() if mac else None
view._selections = [(0, viewer.document.getSelectionAtIndex(0, 0, 6))]
app.clipboard().setText("before")
QTest.keyClick(view, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
result["ctrl_c_pdf"] = app.clipboard().text()
result["commands_in_pdf"] = list(ran)

ed = w.new_tab()
app.processEvents()
ed.setFocus()
QTest.keyClicks(ed, "Title")
QTest.keyClick(ed, Qt.Key.Key_1, STYLE)
result["ctrl1_heading"] = ed.textCursor().block().blockFormat().headingLevel()
QTest.keyClick(ed, Qt.Key.Key_0, STYLE)
result["ctrl0_body"] = ed.textCursor().block().blockFormat().headingLevel()
if mac:
    # Bigger text first: Actual Size is off while the text is at it.
    w.act_zoom_in.trigger()
    app.processEvents()
    QTest.keyClick(ed, Qt.Key.Key_0, CTRL)
ed.selectAll()
QTest.keyClick(ed, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
result["ctrl_c_text"] = app.clipboard().text()
result["commands_in_text"] = list(ran)

for i in range(w.tabs.count()):
    e = w._editor_from_widget(w.tabs.widget(i))
    if e is not None:
        e.document().setModified(False)
w.close()
print("RESULT " + json.dumps(result))
"""


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_pdf_keys_reach_the_reader_and_text_keys_the_editor(tmp_path, platform):
    r = run_window_script(SCRIPT, tmp_path, platform)
    mac = platform == "darwin"
    assert r["pdf_focus"]
    assert r["ctrl2_fit_page"] and r["ctrl1_actual_size"] and r["ctrl0_fit_width"]
    assert r["cmd0_actual_size"] is (True if mac else None)
    assert r["ctrl_c_pdf"] == "needle"
    assert r["commands_in_pdf"] == []
    assert r["ctrl1_heading"] == 1 and r["ctrl0_body"] == 0
    assert r["commands_in_text"] == ["format.style.h1", "format.style.body",
                                     *(["view.actual_size"] if mac else [])]
    assert r["ctrl_c_text"] == "Title"
