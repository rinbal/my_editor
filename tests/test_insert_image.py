# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins Insert > Image… (MainWindow._insert_image).

What must hold:

  Insert > Image… opens the system's file dialog and puts the chosen
  picture in at the caret exactly as dropping that file there does: the
  same path (MainWindow._handle_dropped_images), one step of Undo (called
  "Image"), and, while a Nostr account is in use, kept on this computer
  without a question, as a drop is. Several files chosen at once are a
  photo group of their own (one step, "Add Photos"). Cancelling the
  dialog changes nothing.

  It is offered where a document can keep a picture as Markdown or HTML
  does (Markdown and HTML documents, new documents), not in plain text,
  code files, R Markdown or Markdown opened as its text, nor in a code
  block, and it is dimmed there, never hidden.

  The Insert menu never shows two items for a picture: the Nostr one,
  which inserts a file from the media library (already on the media
  servers), says so ("From Media Library…").
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests.app_process import run_window_script  # noqa: E402

SCRIPT = r"""
import json, os, sys, types
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtGui import QColor, QImage, QTextCursor, QTextFormat
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog
app = QApplication(sys.argv[:1])
import main_window
import photo_group
asked, dialogs = [], []
main_window.ask = lambda parent, **kw: (asked.append(kw.get("title")), False)[1]
home = os.environ["HOME"]
picture = os.path.join(home, "holiday.png")
image = QImage(4, 4, QImage.Format.Format_RGB32)
image.fill(QColor("red"))
image.save(picture)
answer = [[picture]]
def chosen(parent, caption, directory, filters):
    dialogs.append([caption, filters])
    return answer[0], ""
QFileDialog.getOpenFileNames = staticmethod(chosen)
w = main_window.MainWindow()
w.show()
r = {}

def titles():
    w.m_insert.aboutToShow.emit()
    shown = [a.text() for a in w.m_insert.actions() if a.isVisible() and not a.isSeparator()]
    w.m_insert.aboutToHide.emit()
    return shown

r["menu"] = titles()
ed = w.new_tab()
ed.setFocus()
QTest.keyClicks(ed, "Look: ")
app.processEvents()
r["enabled_new"] = w.act_insert_image.isEnabled()
w.act_insert_image.trigger()
app.processEvents()
r["inserted"] = ["￼" in ed.toPlainText(), ed.undo_name(), asked[:], dialogs[-1][0]]
it = ed.document().findBlock(len("Look: ")).begin()
while not it.fragment().charFormat().isImageFormat():
    it += 1
r["alt"] = it.fragment().charFormat().stringProperty(QTextFormat.Property.ImageAltText)
# Described by the name its asset keeps, as an earlier version did: none.
named = it.fragment().charFormat().toImageFormat()
named.setProperty(QTextFormat.Property.ImageAltText, "holiday")
unit = photo_group.unit_of_picture(named)
r["described"] = [ed.photos.stored_name(unit.name), ed.photos.described(unit)]
r["filters"] = dialogs[-1][1]
ed.undo()
r["undone"] = ed.toPlainText()
answer[0] = []
w.act_insert_image.trigger()
r["cancelled"] = [ed.toPlainText(), ed.undo_name()]

# With an account in use: no question, as a drop asks none.
answer[0] = [picture]
w.nostr_state._active = True
w._update_nostr_commands()
w._profile_store.default = lambda: types.SimpleNamespace(
    user_pubkey="ab" * 32, display_name="Me", npub_short=lambda: "npub1me")
w.act_insert_image.trigger()
r["account"] = asked[:]
r["account_inserted"] = ed.toPlainText().count("\ufffc")
r["menu_account"] = titles()

# Several at once: a photo group of their own under the words.
second = os.path.join(home, "harbour.png")
image.save(second)
answer[0] = [picture, second]
ed = w.new_tab()
ed.setFocus()
QTest.keyClicks(ed, "Look:")
QTest.keyClick(ed, Qt.Key.Key_Return)
w.act_insert_image.trigger()
app.processEvents()
group = photo_group.group_at(ed.document(), ed.document().findBlockByNumber(1).position())
r["several"] = [len(group.units) if group is not None else 0, ed.undo_name()]

# Saved and opened again: the pictures and their group, not the file's
# Markdown text (gate RG-PG2E-c 1: "![](...)" read as lossy).
holiday = os.path.join(home, "Holiday.md")
w._save_to(holiday)
saved = open(holiday, encoding="utf-8").read()
w.close_tab(w.tabs.currentIndex())
again = w.open_path(holiday)
app.processEvents()
group = photo_group.group_at(again.document(), again.document().findBlockByNumber(1).position())
r["reopened"] = [saved.count("![]("), again._markdown_source,
                 len(group.units) if group is not None else 0]

# Where a picture cannot be kept, it is dimmed.
for name, body in (("doc.md", "# Doc\n"), ("doc.html", "<p>doc</p>"), ("doc.txt", "doc\n"),
                   ("doc.Rmd", "doc\n"), ("doc.py", "x = 1\n")):
    path = os.path.join(home, name)
    open(path, "w").write(body)
    w.open_path(path)
    app.processEvents()
    r[name] = w.act_insert_image.isEnabled()
ed = w.new_tab()
ed.make_code_block("python")
app.processEvents()
w._update_format_buttons()
r["code_block"] = w.act_insert_image.isEnabled()
for i in range(w.tabs.count()):
    e = w._editor_from_widget(w.tabs.widget(i))
    if e is not None:
        e.document().setModified(False)
w.close()
print("RESULT " + json.dumps(r))
"""


@pytest.fixture(scope="module")
def r(tmp_path_factory):
    return run_window_script(SCRIPT, tmp_path_factory.mktemp("home"))


def test_insert_image_is_a_drop_of_the_chosen_file(r):
    assert r["enabled_new"]
    assert r["inserted"] == [True, "Image", [], "Insert Image"]
    # Gate RG-PG2E-b 5: no description, never the file's name.
    assert r["alt"] == ""
    assert r["described"] == ["holiday", ""]
    assert "*.png" in r["filters"] and "*.jpg" in r["filters"]
    assert r["undone"] == "Look: "
    assert r["cancelled"] == ["Look: ", "Typing"]
    assert r["account"] == [] and r["account_inserted"] == 1
    # Gate RG-PG2E-b 14: one file is one picture, several are a group.
    assert r["several"] == [2, "Add Photos"]
    assert r["reopened"] == [2, False, 2]


def test_one_image_item_in_the_insert_menu(r):
    assert r["menu"].count("Image…") == 1
    assert not any("Image" in title and title != "Image…" for title in r["menu"])
    assert r["menu_account"].count("Image…") == 1
    assert "From Media Library…" in r["menu_account"]
    assert "Insert Image…" not in r["menu_account"]


def test_insert_image_is_dimmed_where_a_picture_cannot_be_kept(r):
    assert r["doc.md"] and r["doc.html"]
    assert not r["doc.txt"] and not r["doc.Rmd"] and not r["doc.py"]
    assert not r["code_block"]


PLAIN_SCRIPT = r"""
import json, os, sys, types
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtCore import QMimeData, QUrl
from PySide6.QtGui import QColor, QImage, QTextCursor
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv[:1])
import main_window
asked = []
main_window.ask = lambda parent, **kw: (asked.append(kw.get("title")), False)[1]
main_window.ask_with_checkbox = lambda parent, **kw: (
    asked.append(kw.get("title")), (False, False))[1]
home = os.environ["HOME"]
picture = os.path.join(home, "holiday.png")
image = QImage(4, 4, QImage.Format.Format_RGB32)
image.fill(QColor("red"))
image.save(picture)
w = main_window.MainWindow()
w.show()
w.nostr_state._active = True
w._update_nostr_commands()
w._profile_store.default = lambda: types.SimpleNamespace(
    user_pubkey="ab" * 32, display_name="Me", npub_short=lambda: "npub1me")
r = {}
for name, body in (("doc.txt", "Words\n"), ("code.py", "x = 1\n"),
                   ("doc.md", "Words\n"), ("doc.Rmd", "Words\n"),
                   ("raw.md", "<div>raw</div>\n")):
    path = os.path.join(home, name)
    open(path, "w").write(body)
    w.open_path(path)
    app.processEvents()
    ed = w.current_editor()
    cursor = ed.textCursor()
    cursor.movePosition(QTextCursor.MoveOperation.End)
    ed.setTextCursor(cursor)
    asked.clear()
    ed.urls_dropped.emit([QUrl.fromLocalFile(picture)])
    dropped = [ed.toPlainText(), ed.undo_name(), asked[:]]
    ed.undo()
    data = QMimeData()
    data.setUrls([QUrl.fromLocalFile(picture)])
    ed.insertFromMimeData(data)
    pasted = [ed.toPlainText(), ed.undo_name()]
    ed.undo()
    bitmap = QMimeData()
    bitmap.setImageData(image)
    can = ed.canInsertFromMimeData(bitmap)
    ed.insertFromMimeData(bitmap)
    r[name] = {"drop": dropped, "paste": pasted, "bitmap": [can, "￼" in ed.toPlainText()],
               "picture": picture}
    ed.document().setModified(False)
w.close()
print("RESULT " + json.dumps(r))
"""


@pytest.fixture(scope="module")
def plain(tmp_path_factory):
    return run_window_script(PLAIN_SCRIPT, tmp_path_factory.mktemp("plain"))


@pytest.mark.parametrize("name", ["doc.txt", "code.py"])
def test_a_picture_in_plain_text_is_its_path_as_text(plain, name):
    """A plain-text or code file cannot keep a picture: dropped or pasted,
    a picture file goes in as its path, as in TextEdit's plain text, and a
    bitmap on the clipboard is not taken. Nothing is asked about uploading."""
    r = plain[name]
    text = r["picture"]
    assert r["drop"][0].endswith(text) and "￼" not in r["drop"][0]
    assert r["drop"][1:] == ["Drag", []]
    assert r["paste"][0].endswith(text) and r["paste"][1] == "Paste"
    assert r["bitmap"] == [False, False]


@pytest.mark.parametrize("name", ["doc.md", "doc.Rmd", "raw.md"])
def test_where_a_picture_is_kept_it_goes_in_as_one(plain, name):
    r = plain[name]
    assert "￼" in r["drop"][0] and r["drop"][1] == "Drag"
    assert r["bitmap"][0] is True


FILES_SCRIPT = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl
from PySide6.QtGui import QColor, QDropEvent, QImage, QTextCursor
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv[:1])
import main_window
home = os.environ["HOME"]
picture = os.path.join(home, "holiday.png")
image = QImage(4, 4, QImage.Format.Format_RGB32)
image.fill(QColor("red"))
image.save(picture)
pdf = os.path.join(home, "paper.pdf")
open(pdf, "wb").write(b"%PDF-1.4\n")
w = main_window.MainWindow()
w.show()
r = {}
for name, body in (("doc.txt", "Words\n"), ("code.py", "x = 1\n")):
    path = os.path.join(home, name)
    open(path, "w").write(body)
    w.open_path(path)
    app.processEvents()
    ed = w.current_editor()

    def at_end():
        cursor = ed.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        ed.setTextCursor(cursor)
        return ed.toPlainText()

    before = at_end()
    files = QMimeData()
    files.setUrls([QUrl.fromLocalFile(picture), QUrl.fromLocalFile(picture)])
    QApplication.clipboard().setMimeData(files)
    ed.paste_normalized()
    matched = ed.toPlainText()[len(before):]
    ed.undo()
    before = at_end()
    document = QMimeData()
    document.setUrls([QUrl.fromLocalFile(pdf)])
    ed.insertFromMimeData(document)
    pasted_pdf = ed.toPlainText()[len(before):]
    ed.undo()
    before = at_end()
    web = QMimeData()
    web.setImageData(image)
    web.setHtml("<img src='https://example.org/a.png'>")
    web.setUrls([QUrl("https://example.org/a.png")])
    at = ed.cursorRect().center()
    ed.dropEvent(QDropEvent(QPointF(at), Qt.DropAction.CopyAction, web,
                            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
    dropped_web = [ed.toPlainText()[len(before):], ed.undo_name()]
    r[name] = {"matched": matched, "pdf": pasted_pdf, "web": dropped_web,
               "paths": [picture, pdf]}
    ed.document().setModified(False)
w.close()
print("RESULT " + json.dumps(r))
"""


@pytest.mark.parametrize("name", ["doc.txt", "code.py"])
def test_files_and_web_pictures_in_plain_text_are_their_paths_and_addresses(tmp_path, name):
    """Review L5: Paste and Match Style of picture files and a pasted PDF
    went in as file:// addresses, and a picture dragged from the web
    brought nothing. In plain text a file is its path, as Paste puts it,
    and a picture from the web its address, as in TextEdit's plain text."""
    r = run_window_script(FILES_SCRIPT, tmp_path)[name]
    picture, pdf = r["paths"]
    assert r["matched"] == picture + "\n" + picture
    assert r["pdf"] == pdf
    assert r["web"] == ["https://example.org/a.png", "Drag"]
