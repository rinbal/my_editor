# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""An update restart, end to end, with real windows.

One window opens a file, edits it without saving, opens an untitled tab
linked to a Nostr draft, selects some text, and closes for an update. A
second window starts the way the relaunched app does. What must hold:

  Every tab comes back in its order, with its unsaved content still
  marked unsaved, its file, its draft link, its selection and its scroll
  position, and the tab that was active is active again. PDF tabs come
  back at their page. A Welcome tab saved as a file comes back as that
  file.

  A file that changed while MyEditor was closed comes back as a copy that
  cannot overwrite it; one that was deleted comes back with its unsaved
  work; a saved file that was deleted is simply not reopened.

  Only documents whose backup can't be written are asked about, by name.

  Crash recovery does not open the same work a second time, and skips
  nothing else: a crash backup of the file named on the command line, or
  any leftover the restart did not take over, still comes back.

  A finished update says so ("You're now using MyEditor ..."); an update
  that did not install says that instead, and the tabs still come back.

  Quitting normally afterwards leaves no record and no backups behind.

Each run happens in a child process with its own HOME, because the app
keeps its settings, session and backups under the home folder, and a test
must never read or touch the real ones.
"""

import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SCRIPT = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from types import SimpleNamespace
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv)
import main_window
from constants import APP_VERSION

informed = []
main_window.inform = lambda parent, **kw: informed.append(kw.get("title", ""))

home = os.environ["HOME"]
doc = os.path.join(home, "notes.txt")
with open(doc, "w") as f:
    f.write("line one\nline two\n")

w = main_window.MainWindow()
w.open_path(doc)
notes = w.current_editor()
notes.insertPlainText("UNSAVED ")
w.new_tab()
draft = w.current_editor()
draft.insertPlainText("draft body")
draft._draft_binding = main_window.DraftBinding(
    identifier="abc", inner_kind=30023, title="My Draft", profile_pubkey="f" * 64)
w.tabs.setCurrentIndex(1)
cursor = notes.textCursor()
cursor.setPosition(8)
cursor.setPosition(12, cursor.MoveMode.KeepAnchor)
notes.setTextCursor(cursor)

target = APP_VERSION if sys.argv[2] == "same" else "999.0"
w._pending_release = SimpleNamespace(
    version=target, notes="# MyEditor\n\n- New thing",
    page_url="https://github.com/rinbal/my_editor/releases/tag/v" + target)
prepared = w._prepare_workspace_for_update()
w._closing_for_update = True
w.close()

w2 = main_window.MainWindow()
app.processEvents()
tabs = []
for i in range(w2.tabs.count()):
    e = w2._editor_from_widget(w2.tabs.widget(i))
    binding = getattr(e, "_draft_binding", None)
    tabs.append({
        "title": w2.tabs.tabText(i),
        "text": e.toPlainText(),
        "modified": e.document().isModified(),
        "path": getattr(e, "_file_path", None),
        "draft": binding.identifier if binding else None,
        "selection": [e.textCursor().anchor(), e.textCursor().position()],
    })
result = {
    "prepared": prepared,
    "tabs": tabs,
    "active": w2.tabs.currentIndex(),
    "bar": "" if w2.update_bar.isHidden() else w2.update_bar._text.text(),
    "informed": informed,
    "record_left": os.path.exists(os.path.join(home, ".cache", "my_editor", "workspace.json")),
    "version": APP_VERSION,
    "doc": doc,
}
for i in range(w2.tabs.count()):
    w2._editor_from_widget(w2.tabs.widget(i)).document().setModified(False)
w2.close()
backups = os.path.join(home, ".cache", "my_editor", "backups")
result["backups_after_quit"] = os.listdir(backups) if os.path.isdir(backups) else []
result["record_after_quit"] = os.path.exists(
    os.path.join(home, ".cache", "my_editor", "workspace.json"))
print("RESULT " + json.dumps(result))
"""


def run(tmp_path, script: str, *args: str) -> dict:
    env = dict(os.environ, HOME=str(tmp_path), QT_QPA_PLATFORM="offscreen")
    proc = subprocess.run(
        [sys.executable, "-c", script, REPO, *args],
        env=env, capture_output=True, text=True, timeout=120,
    )
    line = next((l for l in proc.stdout.splitlines() if l.startswith("RESULT ")), None)
    assert line, f"child failed:\n{proc.stdout}\n{proc.stderr}"
    return json.loads(line[len("RESULT "):])


def restart(tmp_path, target: str) -> dict:
    return run(tmp_path, SCRIPT, target)


# Shared by the scenarios below: a QApplication, alerts and questions that
# never block (each is recorded, and a question gets the answer a scenario
# sets), and helpers to describe the tabs and to close for an update.
PRELUDE = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from types import SimpleNamespace
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv[:1])
import main_window, recovery
from constants import APP_VERSION
from editor import HtmlEditor

home = os.environ["HOME"]
result = {}
informed, asked = [], []
answers = []          # what each question is answered, in order
main_window.inform = lambda parent, **kw: informed.append(kw.get("title", ""))

def ask(parent, title="", **kw):
    asked.append(title)
    return answers.pop(0) if answers else "cancel"

main_window.ask = ask

def write(name, text):
    path = os.path.join(home, name)
    with open(path, "w") as f:
        f.write(text)
    return path

def tabs_of(w):
    tabs = []
    for i in range(w.tabs.count()):
        page = w.tabs.widget(i)
        e = w._editor_from_widget(page)
        viewer = w._pdf_viewer_from_widget(page)
        tabs.append({
            "title": w.tabs.tabText(i),
            "text": e.toPlainText() if e is not None else None,
            "modified": e.document().isModified() if e is not None else None,
            "path": getattr(e if e is not None else viewer, "_file_path", None),
        })
    return tabs

def close_for_update(w, version=None):
    w._pending_release = SimpleNamespace(
        version=version or APP_VERSION, notes="- New",
        page_url="https://github.com/rinbal/my_editor/releases")
    prepared = w._prepare_workspace_for_update()
    if prepared:
        w._closing_for_update = True
        w.close()
    return prepared

def wait_until(check, ms=5000):
    # Lets the event loop run until check() holds, or the time is up.
    from PySide6.QtTest import QTest
    waited = 0
    while not check() and waited < ms:
        QTest.qWait(20)
        waited += 20
    return check()

def finish():
    result["informed"] = informed
    result["asked"] = asked
    print("RESULT " + json.dumps(result))
"""


def scenario(tmp_path, body: str) -> dict:
    return run(tmp_path, PRELUDE + body + "\nfinish()\n")


def titles(r) -> list:
    return [t["title"] for t in r["tabs"]]


def test_a_crash_backup_of_the_file_named_on_the_command_line_comes_back(tmp_path):
    # A file tab's backup is named after its path. Starting with that file
    # as the argument must not make the crash sweep take its backup for the
    # clean tab's own, skip it, and let the clean tab overwrite it later.
    r = scenario(tmp_path, r"""
doc = write("notes.txt", "line one\n")
crashed = HtmlEditor()
crashed.setPlainText("UNSAVED line one\n")
assert recovery.EditorBackup(crashed, doc).write_now()
w = main_window.MainWindow(doc)
app.processEvents()
result["tabs"] = tabs_of(w)
""")
    assert titles(r) == ["notes.txt", "notes.txt (recovered)*"]
    assert r["tabs"][1]["text"].startswith("UNSAVED line one")
    assert r["tabs"][1]["modified"] is True


def test_a_welcome_tab_saved_as_a_file_is_that_file_from_then_on(tmp_path):
    r = scenario(tmp_path, r"""
w = main_window.MainWindow()
assert w.tabs.tabText(0) == "Welcome"
saved = os.path.join(home, "welcome.html")
main_window.QFileDialog = SimpleNamespace(
    getSaveFileName=lambda *a, **k: (saved, ".html (*.html)"))
assert w.save_as()
result["title_after_save"] = w.tabs.tabText(0)
assert close_for_update(w)
w2 = main_window.MainWindow()
app.processEvents()
result["tabs"] = tabs_of(w2)
result["saved"] = saved
""")
    assert r["title_after_save"] == "welcome.html"
    assert titles(r) == ["welcome.html"]
    assert r["tabs"][0]["path"] == r["saved"]
    assert "Welcome to MyEditor" in r["tabs"][0]["text"]


def test_only_documents_that_cannot_be_kept_are_asked_about_by_name(tmp_path):
    # Three tabs whose backup can't be written (a full disk, say) among tabs
    # whose backup can. Only those three are asked about, one at a time.
    r = scenario(tmp_path, r"""
def unprotectable(ed):
    ed._backup.write_now = lambda: False

w = main_window.MainWindow()                      # 0: Welcome
w.new_tab()                                       # 1: kept in its backup
w.current_editor().insertPlainText("kept")
notes = write("notes.txt", "on disk\n")
w.open_path(notes)                                # 2: not saved: back as on disk
w.current_editor().insertPlainText("lost ")
unprotectable(w.current_editor())
w.new_tab()                                       # 3: untitled, not saved: left out
w.current_editor().insertPlainText("gone")
unprotectable(w.current_editor())
w.new_tab()                                       # 4: saved when asked
w.current_editor().insertPlainText("saved when asked")
unprotectable(w.current_editor())
w.tabs.setCurrentIndex(3)

saved = os.path.join(home, "saved.txt")
main_window.QFileDialog = SimpleNamespace(
    getSaveFileName=lambda *a, **k: (saved, ".txt (*.txt)"))
answers[:] = ["discard", "discard", "save"]
assert close_for_update(w)
asked_before_restart = list(asked)
w2 = main_window.MainWindow()
app.processEvents()
result["tabs"] = tabs_of(w2)
result["active"] = w2.tabs.currentIndex()
result["asked"] = asked_before_restart
""")
    assert r["asked"] == [
        "Do you want to save the changes you made to “notes.txt” before updating?",
        "Do you want to save the changes you made to “Untitled” before updating?",
        "Do you want to save the changes you made to “Untitled” before updating?",
    ]
    assert titles(r) == ["Welcome", "Untitled*", "notes.txt", "saved.txt"]
    assert r["tabs"][1]["text"] == "kept"
    assert r["tabs"][2]["text"] == "on disk\n" and r["tabs"][2]["modified"] is False
    assert r["tabs"][3]["text"] == "saved when asked"
    assert r["active"] == 2   # the active tab was left out: the one before it


def test_cancelling_the_question_keeps_everything_open_and_writes_nothing_down(tmp_path):
    r = scenario(tmp_path, r"""
w = main_window.MainWindow()
w.new_tab()
w.current_editor().insertPlainText("no room for this")
w.current_editor()._backup.write_now = lambda: False
answers[:] = ["cancel"]
result["prepared"] = close_for_update(w)
result["record"] = os.path.exists(os.path.join(home, ".cache", "my_editor", "workspace.json"))
result["tabs"] = tabs_of(w)
""")
    assert r["prepared"] is False and r["record"] is False
    assert len(r["asked"]) == 1
    assert titles(r) == ["Welcome", "Untitled*"]


def test_every_tab_comes_back_at_its_scroll_position_not_only_the_active_one(tmp_path):
    r = scenario(tmp_path, r"""
from PySide6.QtTest import QTest
w = main_window.MainWindow()
w.resize(900, 700)
w.show()
for name in ("one.txt", "two.txt"):
    w.open_path(write(name, "\n".join(f"{name} line {i}" for i in range(600))))
before = {}
for i in (1, 2):
    w.tabs.setCurrentIndex(i)
    QTest.qWait(30)
    bar = w._editor_from_widget(w.tabs.widget(i)).verticalScrollBar()
    bar.setValue(bar.maximum() // (i + 1))
    before[w.tabs.tabText(i)] = bar.value()
w.tabs.setCurrentIndex(1)                         # two.txt is a tab in the background
assert close_for_update(w)

w2 = main_window.MainWindow()
w2.resize(900, 700)
w2.show()
after = {}
for i in (1, 2):
    w2.tabs.setCurrentIndex(i)
    bar = w2._editor_from_widget(w2.tabs.widget(i)).verticalScrollBar()
    wait_until(lambda: bar.value() == before[w2.tabs.tabText(i)], ms=1500)
    after[w2.tabs.tabText(i)] = bar.value()
result["before"] = before
result["after"] = after
""")
    assert set(r["before"]) == {"one.txt", "two.txt"}
    assert all(value > 0 for value in r["before"].values())
    assert r["after"] == r["before"]


CHANGED_WHILE_CLOSED = r"""
notes = write("notes.txt", "on disk\n")
w = main_window.MainWindow()
w.open_path(notes)
w.current_editor().insertPlainText("UNSAVED ")
assert close_for_update(w)
{change}
w2 = main_window.MainWindow()
app.processEvents()
result["tabs"] = tabs_of(w2)
result["on_disk"] = open(notes).read() if os.path.exists(notes) else None
result["notes"] = notes
"""


def test_a_file_changed_while_closed_comes_back_as_a_copy_that_cannot_overwrite_it(tmp_path):
    r = scenario(tmp_path, CHANGED_WHILE_CLOSED.format(change=r"""
with open(notes, "w") as f:
    f.write("newer work from elsewhere\n")
later = os.stat(notes).st_mtime + 60
os.utime(notes, (later, later))
"""))
    assert titles(r) == ["Welcome", "notes.txt (recovered copy)*"]
    copy = r["tabs"][1]
    assert copy["text"].startswith("UNSAVED on disk")
    assert copy["path"] is None          # Save goes through Save As
    assert r["on_disk"] == "newer work from elsewhere\n"


def test_a_file_deleted_while_closed_comes_back_with_its_unsaved_work(tmp_path):
    r = scenario(tmp_path, CHANGED_WHILE_CLOSED.format(change="os.remove(notes)"))
    assert titles(r) == ["Welcome", "notes.txt (recovered)*"]
    assert r["tabs"][1]["text"].startswith("UNSAVED on disk")
    assert r["tabs"][1]["path"] == r["notes"]   # Save puts the file back
    assert r["on_disk"] is None


def test_a_saved_file_deleted_while_closed_is_simply_not_reopened(tmp_path):
    r = scenario(tmp_path, r"""
gone = write("gone.txt", "saved\n")
kept = write("kept.txt", "saved too\n")
w = main_window.MainWindow()
w.open_path(gone)
w.open_path(kept)
assert close_for_update(w)
os.remove(gone)
w2 = main_window.MainWindow()
app.processEvents()
result["tabs"] = tabs_of(w2)
""")
    assert titles(r) == ["Welcome", "kept.txt"]
    assert r["informed"] == []   # no error about the missing file


def test_a_pdf_tab_comes_back_active_at_its_page(tmp_path):
    r = scenario(tmp_path, r"""
from PySide6.QtCore import QMarginsF
from PySide6.QtGui import QPageLayout, QPageSize, QPainter, QPdfWriter
from PySide6.QtTest import QTest
pdf = os.path.join(home, "paper.pdf")
writer = QPdfWriter(pdf)
writer.setPageLayout(QPageLayout(QPageSize(QPageSize.PageSizeId.A5),
                                 QPageLayout.Orientation.Portrait, QMarginsF()))
painter = QPainter(writer)
for page in range(4):
    painter.drawText(100, 100, f"page {page + 1}")
    if page < 3:
        writer.newPage()
painter.end()

w = main_window.MainWindow()
w.resize(900, 700)
w.show()
w.open_path(write("notes.txt", "text\n"))
viewer = w.open_path(pdf)
QTest.qWait(100)
viewer.jump_to_page(2)
wait_until(lambda: viewer.current_page() == 2)
result["page_before"] = viewer.current_page()
assert close_for_update(w)

w2 = main_window.MainWindow()
w2.resize(900, 700)
w2.show()
current = w2._pdf_viewer_from_widget(w2.tabs.currentWidget())
if current is not None:
    wait_until(lambda: current.current_page() == 2)
result["tabs"] = tabs_of(w2)
result["active"] = w2.tabs.currentIndex()
result["page_after"] = current.current_page() if current is not None else None
result["pdf"] = pdf
""")
    assert titles(r) == ["Welcome", "notes.txt", "paper.pdf"]
    assert r["tabs"][2]["path"] == r["pdf"]
    assert r["active"] == 2
    assert r["page_before"] == 2
    assert r["page_after"] == 2


def test_after_an_update_the_command_line_file_and_crash_leftovers_still_open(tmp_path):
    # The update restore skips only what it took over: a crash backup it did
    # not take over is still restored, alongside the file on the command line.
    r = scenario(tmp_path, r"""
w = main_window.MainWindow()
w.new_tab()
w.current_editor().insertPlainText("kept across the update")
assert close_for_update(w)

other = write("other.txt", "other on disk\n")
stray = HtmlEditor()
stray.setPlainText("a crash leftover")
assert recovery.EditorBackup(stray, None).write_now()

w2 = main_window.MainWindow(other)
app.processEvents()
result["tabs"] = tabs_of(w2)
""")
    assert titles(r) == ["Welcome", "Untitled*", "other.txt", "Untitled (recovered)*"]
    assert r["tabs"][1]["text"] == "kept across the update"
    assert r["tabs"][3]["text"] == "a crash leftover"


def test_every_tab_comes_back_after_an_update(tmp_path):
    r = restart(tmp_path, "same")
    assert r["prepared"] is True
    assert [t["title"] for t in r["tabs"]] == ["Welcome", "notes.txt*", "⚿ My Draft*"]

    notes = r["tabs"][1]
    assert notes["text"].startswith("UNSAVED line one")
    assert notes["modified"] is True
    assert notes["path"] == r["doc"]
    assert notes["selection"] == [8, 12]

    draft = r["tabs"][2]
    assert draft["text"] == "draft body"
    assert draft["draft"] == "abc"
    assert draft["path"] is None

    assert r["active"] == 1
    assert not any("recovered" in t["title"] for t in r["tabs"])
    assert r["bar"] == f"You’re now using MyEditor {r['version']}."
    assert r["informed"] == []
    assert r["record_left"] is False


def test_an_update_that_did_not_install_says_so_and_keeps_the_tabs(tmp_path):
    r = restart(tmp_path, "newer")
    assert r["informed"] == ["The update wasn’t installed"]
    assert r["bar"] == ""
    assert [t["title"] for t in r["tabs"]] == ["Welcome", "notes.txt*", "⚿ My Draft*"]


def test_a_normal_quit_afterwards_leaves_nothing_behind(tmp_path):
    r = restart(tmp_path, "same")
    assert r["backups_after_quit"] == []
    assert r["record_after_quit"] is False
