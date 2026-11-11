# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins audio and video in the text (Insert > Audio or Video, a dropped or
pasted file: media_files.py, the asset manager's adopt_media, the editor's
media paragraphs, the window's card, writers, gate and close questions).

What must hold (and the checkpoint 4 review's H1, M1 to M3, L3 to L6):

  The file is an object of the text, as a picture is: an asset named by
  the hash of its bytes, in a paragraph of its own, shown as a card that
  says how its upload is going ("Uploading… 42 %"). Its upload goes to the
  media servers through the asset manager's queue; finishing it changes
  nothing in the document and no step of Undo, so Undo takes back the
  person's own last change, and Undo or Redo of the insertion brings
  back the file itself, never an orphan placeholder. The same file twice
  is one upload.

  It stays a paragraph of its own: typing in its paragraph goes into a new
  one after it; Return after it starts a clean paragraph; Backspace from
  the paragraph below takes it away whole, never joining text to it.

  Until it is on a media server it is written nowhere (Markdown, a note,
  plain text, an HTML file, the preview) and nothing is published or saved
  as a draft; asking to publish uploads it again after a failure. Once it
  is, it is written as its address (with its extension), on a line of its
  own, and described by imeta from what this app knows of it. A crash
  record keeps the object, never words standing for it.

  Closing a document or quitting while an upload runs asks first, with
  Cancel the default. A pasted audio or video file goes in as a dropped
  one, while an account is in use. Types come from one table, whatever
  the computer's says.
"""

import hashlib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QMimeData, QUrl  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import media_files  # noqa: E402
from doc_walk import iter_blocks  # noqa: E402
from editor import HtmlEditor  # noqa: E402
from nostr import preview  # noqa: E402
from tests.app_process import run_window_script  # noqa: E402

BODY = b"ID3" + b"sound" * 50
SHA = hashlib.sha256(BODY).hexdigest()
URL = f"https://cdn.example/{SHA}"


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    return QApplication.instance() or QApplication([])


def test_types_come_from_one_table_whatever_the_computer_says():
    assert media_files.type_of("/x/Song.M4A") == "audio/mp4"
    assert media_files.type_of("clip.ogg") == "audio/ogg"
    assert media_files.type_of("film.mov") == "video/quicktime"
    assert media_files.type_of("https://cdn.example/a.flac?x=1") == "audio/flac"
    assert media_files.type_of("notes.txt") == "text/plain"
    assert not media_files.is_audio_or_video("notes.txt")
    assert media_files.extension_for("audio/mpeg") == ".mp3"


def test_the_preview_names_the_file_by_its_own_name():
    url = f"{URL}.mp3"
    named = {url: "Interview with Bob.mp3"}.get
    card = preview.player_card_name(url)
    assert f"[![▶ Audio: Interview with Bob.mp3]({card})]({url})" in preview.markdown_for_preview(
        f"Listen:\n\n{url}\n", media_names=named)
    assert 'alt="▶ Audio: Interview with Bob.mp3"' in preview.note_html(
        f"Listen {url}", media_names=named)


def test_the_preview_shows_the_player_where_readers_put_it():
    url = f"{URL}.mp3"
    markdown = preview.markdown_for_preview(f"Listen:\n\n{url}\n\nInline {url} stays.")
    assert f"[![▶ Audio: {SHA}.mp3]({preview.player_card_name(url)})]({url})" in markdown
    assert f"Inline {url} stays." in markdown
    clip = "https://cdn.example/clip.mp4"
    note = preview.note_html(f"See {clip}")
    assert f'<a href="{clip}"><img src="{preview.player_card_name(clip)}"' in note


def test_a_player_card_names_the_address_it_plays():
    url = "https://cdn.example/a b/clip.mp4?x=1&y=(2)"
    assert preview.player_card_address(preview.player_card_name(url)) == url
    assert preview.player_card_address("https://cdn.example/a.png") == ""
    assert preview.player_card_address(f"{preview.PLAYER_SCHEME}:%%%") == ""


def test_a_player_card_clicked_plays_in_the_app_and_other_links_open(monkeypatch):
    from PySide6.QtGui import QDesktopServices
    opened, played = [], []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url.toString()))
    view = preview.NostrPreview(play=played.append)
    view.anchorClicked.emit(QUrl(f"{URL}.mp4"))
    view.anchorClicked.emit(QUrl("https://x.example/page"))
    assert played == [f"{URL}.mp4"] and opened == ["https://x.example/page"]
    view.close()


def test_the_preview_asks_for_a_player_card_and_leaves_no_gap_below_it():
    asked = []
    view = preview.NostrPreview(images=lambda name: asked.append(name) or None)
    view.show_markdown(f"Before\n\n{URL}.mp4\n\nAfter")
    view.resize(900, 700)
    view.show()
    QApplication.processEvents()
    assert preview.player_card_name(f"{URL}.mp4") in asked
    card = next(block for block in iter_blocks(view.document()) if block.text() == "\ufffc")
    assert card.blockFormat().lineHeight() == 100
    view.close()


def test_a_pasted_audio_file_goes_in_as_a_dropped_one_only_with_an_account(tmp_path):
    song = tmp_path / "song.mp3"
    song.write_bytes(BODY)
    data = QMimeData()
    data.setUrls([QUrl.fromLocalFile(str(song))])
    data.setText("song.mp3")
    for account, expected in ((True, 1), (False, 0)):
        ed = HtmlEditor()
        ed.set_nostr_check(lambda: account)
        dropped = []
        ed.urls_dropped.connect(dropped.append)
        ed.insertFromMimeData(data)
        assert len(dropped) == expected
        assert ("song.mp3" in ed.toPlainText()) is (not account)


SCRIPT = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtCore import Qt
from PySide6.QtGui import QTextCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv[:1])
import main_window, recovery
from plain_writer import serialize_plain_with_images
from markdown_writer import document_to_markdown
from export_html import document_to_html
from nostr.blossom.store import MediaFile
from nostr.publisher import build_imeta_tags

path, big, sha = sys.argv[2], sys.argv[3], sys.argv[4]
told, asked = [], []
main_window.inform = lambda parent, **kw: told.append(kw.get("title", ""))
main_window.confirm_destructive = lambda parent, **kw: (asked.append(kw), False)[1]
main_window._ask_save_changes = lambda parent, title, **kw: (
    asked.append(dict(kw, title=title)), "cancel")[1]
w = main_window.MainWindow()
w.show()
sent = []
w._media_store.upload_bytes = lambda body, name, mime_type: sent.append((name, mime_type, len(body)))
w.nostr_state._active = True
w._update_nostr_commands()
w._upload_choice = lambda: "now"                    # Settings: Upload Right Away
ed = w.new_tab()
ed.setFocus()
r = {}
QTest.keyClicks(ed, "Listen:")
w._upload_limit = lambda: 100
w._add_media_file(ed, big)
r["too_large"] = told[-1] if told else ""
w._upload_limit = lambda: 10**9
w._add_media_file(ed, path)
doc = ed.document()
blocks = []
b = doc.begin()
while b.isValid():
    blocks.append(b.text()); b = b.next()
r["blocks"] = blocks
r["media_block"] = ed.is_media_block(doc.findBlockByNumber(1))
r["sent"] = sent
w._add_media_file(ed, path)                 # the same file again: one upload
r["sent_twice"] = len(sent)
ed.undo()
w._refresh_asset_in_documents(sha)
r["card_words"] = w._media_card(w._asset_manager.get(sha)).isNull() is False
# Typing in the media paragraph goes into a new one after it.
c = ed.textCursor(); c.setPosition(doc.findBlockByNumber(1).position() + 1); ed.setTextCursor(c)
QTest.keyClicks(ed, "Notes")
r["after_typing"] = [doc.findBlockByNumber(i).text() for i in range(doc.blockCount())]
# While it uploads it is written nowhere, and nothing is published.
r["md_pending"] = document_to_markdown(doc)
r["publish_pending"] = w._publish_text(ed, "markdown")
r["note_pending"] = w._publish_text(ed, "note")
r["plain_pending"] = serialize_plain_with_images(doc, lambda fmt: None)
r["html_pending"] = "Uploading" in document_to_html(
    doc, asset_resolver=w._asset_manager.export_view)
gate, told_before = [], len(told)
w._after_uploads(ed, lambda: gate.append(True))
r["gate_pending"] = bool(gate)
r["gate_said"] = told[told_before:]
snapshot = doc.toHtml()
r["crash_keeps_object"] = f"myeditor-asset:{sha}" in snapshot and "Uploading" not in snapshot
# Closing while it uploads asks, Cancel the default (the stand-in answers Cancel).
w.close_tab(w.tabs.currentIndex())
r["close_asked"] = asked[:]
asked.clear()
ed.document().setModified(False)                    # nothing to save: the upload alone
w.close_tab(w.tabs.currentIndex())
r["close_asked_saved"] = asked[:]
ed.document().setModified(True)
asked.clear()
event = __import__("PySide6.QtGui", fromlist=["QCloseEvent"]).QCloseEvent()
w.closeEvent(event)
r["quit_asked"] = asked[:]
r["quit_refused"] = not event.isAccepted()
r["still_open"] = w._editor_widget_index(ed) >= 0
# The upload finishes: nothing in the document changes, no step of Undo.
undo_steps = doc.availableUndoSteps()
media = MediaFile(hash=sha, url=f"https://cdn.example/{sha}", mime_type="audio/mpeg",
                  size=len(open(path, "rb").read()),
                  urls=[{"server": "https://cdn.example", "url": f"https://cdn.example/{sha}"}])
w._media_store.upload_status.emit(f"myeditor-asset:{sha}", "uploading")
w._media_store.upload_finished.emit(f"myeditor-asset:{sha}", media)
r["undo_steps_unchanged"] = doc.availableUndoSteps() == undo_steps
content, records = w._publish_payload(ed, "markdown")
r["published"] = content
r["published note"] = w._publish_text(ed, "note")
r["imeta"] = build_imeta_tags(records, content)
app.processEvents()                         # what waits is told from the event loop
r["gate_done"] = bool(gate)                 # publishing went on by itself
# Undo takes back the person's typing, not the finished upload.
ed.undo()
r["after_undo"] = [doc.findBlockByNumber(i).text() for i in range(doc.blockCount())]
# Return after it starts a clean paragraph; Backspace from below takes it whole.
c = ed.textCursor(); c.setPosition(doc.findBlockByNumber(1).position() + 1); ed.setTextCursor(c)
QTest.keyClick(ed, Qt.Key.Key_Return)
QTest.keyClicks(ed, "Below")
r["return_then_type"] = document_to_markdown(doc)
c = ed.textCursor(); c.setPosition(doc.findBlockByNumber(2).position()); ed.setTextCursor(c)
QTest.keyClick(ed, Qt.Key.Key_Backspace)
r["backspace_from_below"] = [doc.findBlockByNumber(i).text() for i in range(doc.blockCount())]
ed.undo()
r["undo_backspace_brings_it_back"] = ed.is_media_block(doc.findBlockByNumber(1))
for i in range(w.tabs.count()):
    e = w._editor_from_widget(w.tabs.widget(i))
    if e is not None:
        e.document().setModified(False)
w._media_not_uploaded = lambda doc: []
w.close()
print("RESULT " + json.dumps(r))
"""


def test_audio_in_the_window(tmp_path):
    song = tmp_path / "song.mp3"
    song.write_bytes(BODY)
    big = tmp_path / "big.mp3"
    big.write_bytes(b"x" * 200)
    r = run_window_script(SCRIPT, tmp_path, str(song), str(big), SHA)
    assert r["too_large"] == "“big.mp3” is too large"
    assert r["blocks"] == ["Listen:", "￼", ""] and r["media_block"] is True
    assert r["sent"] == [[f"myeditor-asset:{SHA}", "audio/mpeg", len(BODY)]]
    assert r["sent_twice"] == 1                                         # M1: one upload
    assert r["card_words"] is True
    assert r["after_typing"] == ["Listen:", "￼", "Notes", ""]      # H1
    for written in ("md_pending", "publish_pending", "note_pending", "plain_pending"):
        assert SHA not in r[written] and "Uploading" not in r[written]  # M3
        assert "Notes" in r[written]
    assert r["html_pending"] is False and r["crash_keeps_object"] is True
    # Publishing waits for the upload already on its way, asking nothing.
    assert r["gate_pending"] is False and r["gate_said"] == []
    # Closing a changed document while its audio uploads: one question,
    # the save one, saying what happens to the upload (L4, review).
    [close] = r["close_asked"]
    assert close["title"].startswith("Do you want to save the changes")
    assert "is still uploading" in close["message"] and r["still_open"] is True
    [saved] = r["close_asked_saved"]
    assert saved["action"] == "Close Anyway"           # nothing to save: the upload alone
    [quit_question] = r["quit_asked"]
    assert quit_question["title"].startswith("Do you want to save the changes")
    assert "Quitting now stops the upload" in quit_question["message"]
    assert r["quit_refused"] is True
    assert r["undo_steps_unchanged"] is True                            # M2
    assert f"Listen:\n\n{URL}.mp3\n\nNotes" in r["published"]
    # A line of its own in a note, without spaces around it.
    assert f"\n{URL}.mp3\n" in r["published note"] and f" {URL}" not in r["published note"]
    assert r["imeta"] == [["imeta", f"url {URL}.mp3", "m audio/mpeg", f"x {SHA}",
                           f"size {len(BODY)}"]]
    assert r["gate_done"] is True
    # Undo takes back typing, not the finished upload: the file stays.
    assert r["after_undo"][:2] == ["Listen:", "\ufffc"] and "Notes" not in r["after_undo"]
    assert "Below" in r["return_then_type"] and "￼" not in r["return_then_type"]
    assert r["backspace_from_below"][:2] == ["Listen:", "Below"]        # H1
    assert r["undo_backspace_brings_it_back"] is True


RMD_SCRIPT = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv[:1])
import main_window
from nostr.blossom.store import MediaFile

song, sha, folder = sys.argv[2], sys.argv[3], sys.argv[4]
w = main_window.MainWindow()
w.show()
w._media_store.upload_bytes = lambda body, name, mime_type: None
w.nostr_state._active = True
w._update_nostr_commands()
w._upload_choice = lambda: "now"                    # Settings: Upload Right Away
w._upload_limit = lambda: 10**9
r = {}
ed = w.new_tab()
ed.setFocus()
QTest.keyClicks(ed, "Before.")
w._add_media_file(ed, song)
QTest.keyClicks(ed, "After.")
w._save_to(os.path.join(folder, "during.rmd"))
r["during"] = open(os.path.join(folder, "during.rmd"), encoding="utf-8").read()
r["copied"] = os.path.exists(os.path.join(folder, "during_media", sha + ".mp3"))
url = f"https://cdn.example/{sha}"
w._media_store.upload_status.emit(f"myeditor-asset:{sha}", "uploading")
w._media_store.upload_finished.emit(f"myeditor-asset:{sha}", MediaFile(
    hash=sha, url=url, mime_type="audio/mpeg", size=1,
    urls=[{"server": "https://cdn.example", "url": url}]))
app.processEvents()
w._save_to(os.path.join(folder, "after.rmd"))
r["after"] = open(os.path.join(folder, "after.rmd"), encoding="utf-8").read()
ed.document().setModified(False)
w.close()
print("RESULT " + json.dumps(r))
"""


def test_r_markdown_writes_audio_as_a_link_during_and_after_the_upload(tmp_path):
    """Review N4: R Markdown links to the file, a copy beside the document
    while it uploads and its address once uploaded; never "image
    unavailable"."""
    song = tmp_path / "song.mp3"
    song.write_bytes(BODY)
    r = run_window_script(RMD_SCRIPT, tmp_path, str(song), SHA, str(tmp_path))
    assert f"Before.\n\n[song.mp3](during_media/{SHA}.mp3)\n\nAfter." in r["during"]
    assert r["copied"] is True
    assert f"Before.\n\n[song.mp3]({URL}.mp3)\n\nAfter." in r["after"]
    assert "unavailable" not in r["during"] + r["after"]


SAVED_DURING_SCRIPT = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv[:1])
import main_window
from nostr.blossom.store import MediaFile

song, sha, folder, ext = sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
asked = []
main_window._ask_save_changes = lambda parent, title, **kw: (asked.append(title), "cancel")[1]
w = main_window.MainWindow()
w.show()
w._media_store.upload_bytes = lambda body, name, mime_type: None
w.nostr_state._active = True
w._update_nostr_commands()
w._upload_choice = lambda: "now"                    # Settings: Upload Right Away
w._upload_limit = lambda: 10**9
r = {}
ed = w.new_tab()
ed.setFocus()
QTest.keyClicks(ed, "Before.")
w._add_media_file(ed, song)
QTest.keyClicks(ed, "After.")
path = os.path.join(folder, "doc" + ext)
w._save_to(path)
r["saved during"] = open(path, encoding="utf-8").read() if ext == ".md" else ""
r["modified after save"] = ed.document().isModified()
r["said at save"] = w.status.currentMessage()
url = f"https://cdn.example/{sha}"
w._media_store.upload_status.emit(f"myeditor-asset:{sha}", "uploading")
w._media_store.upload_finished.emit(f"myeditor-asset:{sha}", MediaFile(
    hash=sha, url=url, mime_type="audio/mpeg", size=1,
    urls=[{"server": "https://cdn.example", "url": url}]))
app.processEvents()
r["modified after upload"] = ed.document().isModified()
r["said after upload"] = w.status.currentMessage()
w.close_tab(w.tabs.currentIndex())
r["close asked"] = len(asked)
w._save_to(path)
r["saved after"] = open(path, encoding="utf-8").read() if ext == ".md" else ""
r["modified after second save"] = ed.document().isModified()
ed.document().setModified(False)
w.close()
print("RESULT " + json.dumps(r))
"""


def test_a_pdf_saved_while_its_audio_uploads_is_edited_again_when_it_is_done(tmp_path):
    """Review N1, as Pages does when an inserted item finishes loading after
    a save: a PDF links to the file once it is on a media server, so the
    save says it goes in once uploaded, and the document is edited again
    when it is, so Close and Quit ask."""
    song = tmp_path / "song.mp3"
    song.write_bytes(BODY)
    r = run_window_script(SAVED_DURING_SCRIPT, tmp_path, str(song), SHA, str(tmp_path), ".pdf")
    assert r["modified after save"] is False
    assert "“song.mp3” goes into the file once it is uploaded" in r["said at save"]
    assert r["modified after upload"] is True
    assert r["said after upload"] == "“song.mp3” is uploaded. Save the document to put it in."
    assert r["close asked"] == 1
    assert r["modified after second save"] is False


def test_a_markdown_file_saved_while_its_audio_uploads_holds_it_at_once(tmp_path):
    """A .md file carries the file as a copy beside itself, as it does a
    picture: nothing is left out, so nothing is to be saved again later."""
    song = tmp_path / "song.mp3"
    song.write_bytes(BODY)
    r = run_window_script(SAVED_DURING_SCRIPT, tmp_path, str(song), SHA, str(tmp_path), ".md")
    assert r["saved during"] == f"Before.\n\n[song.mp3](doc_media/{SHA}.mp3)\n\nAfter.\n"
    assert (tmp_path / "doc_media" / f"{SHA}.mp3").read_bytes() == BODY
    assert r["said at save"].startswith("Saved: ")
    assert r["modified after upload"] is False
    assert r["close asked"] == 0


def _exported(asset, alt="song.mp3"):
    from PySide6.QtGui import QTextDocument, QTextImageFormat
    from export_html import document_to_html
    doc = QTextDocument()
    image = QTextImageFormat()
    image.setName(f"myeditor-asset:{SHA}")
    image.setProperty(QTextImageFormat.ImageAltText, alt)
    doc.rootFrame().firstCursorPosition().insertImage(image)
    return document_to_html(doc, asset_resolver=lambda name: asset)


def test_an_html_file_plays_the_audio_it_carries():
    """Self-contained, as its pictures are: the page plays the file from
    its own bytes, and links to the uploaded file for a reader without a
    player (and for the editor reopening the page)."""
    import base64
    import types
    out = _exported(types.SimpleNamespace(mime="audio/mpeg", remote_url=URL, data=BODY))
    assert '<audio controls preload="metadata"' in out
    assert f'src="data:audio/mpeg;base64,{base64.b64encode(BODY).decode()}"' in out
    assert f'<a href="{URL}.mp3">▶ Audio: song.mp3</a></audio>' in out


def test_a_video_too_large_to_carry_plays_from_its_media_server(monkeypatch):
    import types
    import export_html
    monkeypatch.setattr(export_html, "MEDIA_EMBED_LIMIT", len(BODY) - 1)
    out = _exported(types.SimpleNamespace(mime="video/mp4", remote_url=URL, data=BODY),
                    alt="clip.mp4")
    assert f'<video controls preload="metadata" src="{URL}.mp4"' in out
    assert "base64" not in out


def test_audio_not_uploaded_yet_still_plays_from_the_page_and_none_at_all_is_left_out():
    import types
    pending = _exported(types.SimpleNamespace(mime="audio/mpeg", remote_url="", data=BODY))
    assert "<audio" in pending and "data:audio/mpeg;base64," in pending and "<a " not in pending
    gone = _exported(types.SimpleNamespace(mime="audio/mpeg", remote_url="", data=b""))
    assert "<audio" not in gone and "song.mp3" not in gone


@pytest.mark.parametrize("uploaded", [True, False])
def test_a_pdf_or_a_print_shows_audio_as_its_link_never_a_grey_box(uploaded, tmp_path):
    """A PDF and a printed page link to an uploaded audio or video file,
    as the HTML export does, and leave out one not uploaded yet: never the
    grey stand-in for a picture that can't be read."""
    import types
    from PySide6.QtGui import QPdfWriter, QTextDocument, QTextImageFormat
    from export_pdf import paginate
    doc = QTextDocument()
    cursor = doc.rootFrame().firstCursorPosition()
    cursor.insertText("Before.")
    cursor.insertBlock()
    image = QTextImageFormat()
    image.setName(f"myeditor-asset:{SHA}")
    image.setProperty(QTextImageFormat.ImageAltText, "song.mp3")
    cursor.insertImage(image)
    cursor.insertBlock()
    cursor.insertText("After.")
    asset = types.SimpleNamespace(mime="audio/mpeg", remote_url=URL if uploaded else "",
                                  data=BODY)
    writer = QPdfWriter(str(tmp_path / "out.pdf"))
    paged = paginate(doc, writer, writer.resolution(), asset_resolver=lambda name: asset)
    text = paged.clone.toPlainText()
    assert "￼" not in text and "Before." in text and "After." in text
    if uploaded:
        found = paged.clone.find("▶ Audio: song.mp3")
        assert found.charFormat().anchorHref() == f"{URL}.mp3"
    else:
        assert "song.mp3" not in text
    assert doc.toPlainText().count("￼") == 1          # the document is untouched


PASTE_SCRIPT = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtGui import QTextCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv[:1])
import main_window
from nostr.blossom.store import MediaFile

song, sha = sys.argv[2], sys.argv[3]
w = main_window.MainWindow()
w.show()
w._media_store.upload_bytes = lambda body, name, mime_type: None
w.nostr_state._active = True
w._update_nostr_commands()
w._upload_choice = lambda: "now"                    # Settings: Upload Right Away
w._upload_limit = lambda: 10**9
url = f"https://cdn.example/{sha}"
r = {}
source = w.new_tab()
source.setFocus()
w._add_media_file(source, song)
w._media_store.upload_status.emit(f"myeditor-asset:{sha}", "uploading")
w._media_store.upload_finished.emit(f"myeditor-asset:{sha}", MediaFile(
    hash=sha, url=url, mime_type="audio/mpeg", size=1,
    urls=[{"server": "https://cdn.example", "url": url}]))
app.processEvents()
c = source.textCursor()
c.setPosition(0)
c.setPosition(1, QTextCursor.MoveMode.KeepAnchor)
source.setTextCursor(c)
copied = source.createMimeDataFromSelection()
from paste import MARKDOWN_MIME
r["copied text"] = copied.text()
r["copied markdown"] = bytes(copied.data(MARKDOWN_MIME)).decode()

def blocks(ed):
    doc = ed.document()
    return [doc.findBlockByNumber(i).text() for i in range(doc.blockCount())]

for case, words, at in (("inside a word", "Head", 2), ("end of a paragraph", "Other doc", 9),
                        ("start of a paragraph", "Start", 0)):
    ed = w.new_tab()
    ed.setFocus()
    QTest.keyClicks(ed, words)
    c = ed.textCursor()
    c.setPosition(at)
    ed.setTextCursor(c)
    ed.insertFromMimeData(copied)
    r[case] = {"blocks": blocks(ed), "published": w._publish_text(ed, "markdown"),
               "note": w._publish_text(ed, "note")}
    ed.undo()
    r[case]["undone"] = blocks(ed)
for i in range(w.tabs.count()):
    e = w._editor_from_widget(w.tabs.widget(i))
    if e is not None:
        e.document().setModified(False)
w.close()
print("RESULT " + json.dumps(r))
"""


def test_a_pasted_or_dropped_file_is_a_paragraph_of_its_own_wherever_it_lands(tmp_path):
    """Review N2: pasted (or dragged, the same way in) inside a sentence,
    the file splits the paragraph and goes between its parts; its address
    is never glued to words."""
    song = tmp_path / "song.mp3"
    song.write_bytes(BODY)
    r = run_window_script(PASTE_SCRIPT, tmp_path, str(song), SHA)
    inside = r["inside a word"]
    assert inside["blocks"] == ["He", "￼", "ad"]
    assert inside["published"] == f"He\n\n{URL}.mp3\n\nad"
    assert inside["undone"] == ["Head"]
    end = r["end of a paragraph"]
    assert end["blocks"][:2] == ["Other doc", "￼"]
    assert end["published"].startswith(f"Other doc\n\n{URL}.mp3")
    assert end["undone"] == ["Other doc"]
    # Other apps get the address, never an internal name or U+FFFC.
    assert r["copied text"].strip() == f"{URL}.mp3"
    assert r["copied markdown"].strip() == f"{URL}.mp3"
    start = r["start of a paragraph"]
    assert start["blocks"] == ["\ufffc", "Start"]
    assert start["published"] == f"{URL}.mp3\n\nStart"


THEME_SCRIPT = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtCore import QUrl
from PySide6.QtGui import QTextDocument
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv[:1])
import main_window
from nostr.ui import media_card

song, sha = sys.argv[2], sys.argv[3]
w = main_window.MainWindow()
w.show()
w._media_store.upload_bytes = lambda body, name, mime_type: None
w.nostr_state._active = True
w._update_nostr_commands()
w._upload_limit = lambda: 10**9
ed = w.new_tab()
w._add_media_file(ed, song)

def fill():
    image = ed.document().resource(QTextDocument.ImageResource, QUrl(f"myeditor-asset:{sha}"))
    ratio = image.devicePixelRatio()
    return image.pixelColor(int((media_card.WIDTH - 8) * ratio),
                            int(media_card.HEIGHT / 2 * ratio)).lightness()

r = {}
w._set_theme(False)
r["light"] = fill()
w._set_theme(True)
r["dark"] = fill()
ed.document().setModified(False)
w._media_not_uploaded = lambda doc: []
w.close()
print("RESULT " + json.dumps(r))
"""


def test_the_card_follows_the_appearance(tmp_path):
    song = tmp_path / "song.mp3"
    song.write_bytes(BODY)
    r = run_window_script(THEME_SCRIPT, tmp_path, str(song), SHA)
    assert r["light"] > 200 and r["dark"] < 80


def test_the_card_shows_what_the_file_is_and_a_play_button_that_plays_it():
    """Audio is a strip, video a 16:9 frame, as players are before they
    start; each has its play button (the app plays it, media_viewer.py), and
    keeps a little room above and below, as a picture's paragraph does."""
    from nostr.ui import media_card
    audio = media_card.render("a.mp3", "", dark=False, mime="audio/mpeg")
    video = media_card.render("a.mp4", "", dark=False, mime="video/mp4")
    ratio = audio.devicePixelRatio()
    assert (audio.width() / ratio, audio.height() / ratio) == media_card.size_for("audio/mpeg")
    width, height = media_card.size_for("video/mp4")
    assert (video.width() / ratio, video.height() / ratio) == (width, height)
    assert round((height - 2 * media_card.MARGIN) / width * 16) == 9

    for image, mime in ((audio, "audio/mpeg"), (video, "video/mp4")):
        button = media_card.play_button_rect(mime)
        # The triangle: light on the video's dark disc, the card's own
        # fill on the audio's dark one.
        mark = image.pixelColor(int(button.center().x() * ratio), int(button.center().y() * ratio))
        rim = image.pixelColor(int((button.left() + 3) * ratio), int(button.center().y() * ratio))
        assert mark.lightness() != rim.lightness()
        middle = int(image.width() / 2)
        assert image.pixelColor(middle, int(media_card.MARGIN / 2 * ratio)).alpha() == 0
        assert image.pixelColor(middle, image.height() - int(media_card.MARGIN / 2 * ratio)).alpha() == 0


def test_a_video_card_shows_its_first_frame_and_how_long_it_plays():
    from PySide6.QtGui import QColor, QImage
    from nostr.ui import media_card
    frame = QImage(160, 90, QImage.Format.Format_RGB32)
    frame.fill(QColor("#22CC44"))
    card = media_card.render("a.mp4", "", dark=True, mime="video/mp4", poster=frame,
                             duration_ms=65_000)
    ratio = card.devicePixelRatio()
    corner = card.pixelColor(int(40 * ratio), int(40 * ratio))
    assert corner.green() > 150 and corner.red() < 80          # the frame, not a glyph
    assert media_card.duration_text(65_000) == "1:05"
    assert media_card.duration_text(3_725_000) == "1:02:05"
    assert media_card.duration_text(0) == ""


BEFORE_SCRIPT = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv[:1])
import main_window

song = sys.argv[2]
w = main_window.MainWindow()
w.show()
w._media_store.upload_bytes = lambda body, name, mime_type: None
w.nostr_state._active = True
w._update_nostr_commands()
w._upload_limit = lambda: 10**9
ed = w.new_tab()
ed.setFocus()
QTest.keyClicks(ed, "Top")
w._add_media_file(ed, song)
doc = ed.document()

def blocks():
    return [doc.findBlockByNumber(i).text() for i in range(doc.blockCount())]

def caret_before_object():
    c = ed.textCursor()
    c.setPosition(next(doc.findBlockByNumber(i).position() for i in range(doc.blockCount())
                       if ed.is_media_block(doc.findBlockByNumber(i))))
    ed.setTextCursor(c)

r = {}
caret_before_object()
QTest.keyClick(ed, Qt.Key.Key_Return)
r["return"] = blocks()
r["caret on the object"] = ed.is_media_block(ed.textCursor().block())
ed.undo()
r["undone"] = blocks()
caret_before_object()
QTest.keyClicks(ed, "Intro")
r["typed"] = blocks()
r["caret after the words"] = ed.textCursor().block().text() == "Intro"
ed.document().setModified(False)
w._media_not_uploaded = lambda doc: []
w.close()
print("RESULT " + json.dumps(r))
"""


def test_with_the_caret_before_the_file_return_and_typing_go_before_it(tmp_path):
    """As TextEdit and Pages do with any block: Return before it opens a
    paragraph above it, and typing there writes in a paragraph above."""
    song = tmp_path / "song.mp3"
    song.write_bytes(BODY)
    r = run_window_script(BEFORE_SCRIPT, tmp_path, str(song))
    assert r["return"] == ["Top", "", "￼", ""] and r["caret on the object"] is True
    assert r["undone"] == ["Top", "￼", ""]
    assert r["typed"] == ["Top", "Intro", "￼", ""] and r["caret after the words"] is True


FAILED_SCRIPT = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv[:1])
import main_window

song, sha = sys.argv[2], sys.argv[3]
w = main_window.MainWindow()
w.show()
w._media_store.upload_bytes = lambda body, name, mime_type: None
w.nostr_state._active = True
w._update_nostr_commands()
w._upload_limit = lambda: 10**9
ed = w.new_tab()
w._add_media_file(ed, song)
r = {}
for code in ("UPLOAD_FAILED", "NETWORK_UNAVAILABLE", "PAYMENT_REQUIRED"):
    w._on_asset_upload_failed(sha, code)
    r[code] = w.status.currentMessage()
ed.document().setModified(False)
w._media_not_uploaded = lambda doc: []
w.close()
print("RESULT " + json.dumps(r))
"""


def test_a_failed_audio_upload_does_not_speak_of_an_image(tmp_path):
    song = tmp_path / "song.mp3"
    song.write_bytes(BODY)
    r = run_window_script(FAILED_SCRIPT, tmp_path, str(song), SHA)
    for said in r.values():
        assert said and "image" not in said.lower()


FAILED_MANY_SCRIPT = r"""
import hashlib, json, os, sys
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv[:1])
import main_window

w = main_window.MainWindow()
w.show()
sent = []
w._media_store.upload_bytes = lambda body, name, mime_type: sent.append(name)
w.nostr_state._active = True
w._update_nostr_commands()
w._upload_limit = lambda: 10**9
ed = w.new_tab()
paths = sys.argv[2:]
for path in paths:
    w._add_media_file(ed, path)
shas = [hashlib.sha256(open(p, "rb").read()).hexdigest() for p in paths]
r = {}
# As a publish's wait does: the first goes, the others queue behind it.
for sha in shas[:int(os.environ["FAILING"])]:
    w._asset_manager.request_upload(sha)
app.processEvents()
w._media_store.upload_failed_code.emit(sent[0], "NETWORK_UNAVAILABLE")
app.processEvents()
r["said"] = w.status.currentMessage()
ed.document().setModified(False)
w._media_not_uploaded = lambda doc: []
w.close()
print("RESULT " + json.dumps(r))
"""


@pytest.mark.parametrize("failing, said", [
    (1, "Your file wasn’t uploaded. Couldn’t reach your media server: check your internet "
        "connection and try again."),
    (3, "Some files weren’t uploaded. Couldn’t reach your media server: check your internet "
        "connection and try again."),
])
def test_a_failed_upload_says_what_did_not_happen_never_how_many_wait(
        tmp_path, monkeypatch, failing, said):
    # Lessons 36: the stopped queue is counted for logic, never in words.
    paths = []
    for i in range(3):
        song = tmp_path / f"song{i}.mp3"
        song.write_bytes(BODY + bytes([i]) * 16)
        paths.append(str(song))
    monkeypatch.setenv("FAILING", str(failing))
    r = run_window_script(FAILED_MANY_SCRIPT, tmp_path, *paths)
    assert r["said"] == said


PREVIEW_SCRIPT = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv[:1])
import main_window
from nostr.blossom.store import MediaFile

song, sha = sys.argv[2], sys.argv[3]
w = main_window.MainWindow()
w.show()
w._media_store.upload_bytes = lambda body, name, mime_type: None
w.nostr_state._active = True
w._update_nostr_commands()
w._upload_choice = lambda: "now"                    # Settings: Upload Right Away
w._upload_limit = lambda: 10**9
ed = w.new_tab()
w._add_media_file(ed, song)
r = {"checks while uploading": list(w._preview_content(ed, "article").checks)}
url = f"https://cdn.example/{sha}"
w._media_store.upload_status.emit(f"myeditor-asset:{sha}", "uploading")
w._media_store.upload_finished.emit(f"myeditor-asset:{sha}", MediaFile(
    hash=sha, url=url, mime_type="audio/mpeg", size=1,
    urls=[{"server": "https://cdn.example", "url": url}]))
app.processEvents()
r["name"] = w._media_name(url + ".mp3")
w.nostr_state._active = False
r["document preview"] = w._preview_content(ed, "article").text
ed.document().setModified(False)
w.close()
print("RESULT " + json.dumps(r))
"""


def test_the_preview_shows_the_file_as_readers_do_and_says_while_it_uploads(tmp_path):
    song = tmp_path / "song.mp3"
    song.write_bytes(BODY)
    r = run_window_script(PREVIEW_SCRIPT, tmp_path, str(song), SHA)
    assert any("not on your media servers yet" in check for check in r["checks while uploading"])
    assert r["name"] == "song.mp3"
    assert r["document preview"] == f"{URL}.mp3"


def library_host(sent, tmp_path):
    """A media store whose uploads, once a file is read, are recorded in
    ``sent`` as (name, type) instead of going anywhere."""
    from nostr.blossom.settings import BlossomSettings
    from nostr.blossom.store import MediaStore
    host = MediaStore(session_pool=None, profile_provider=lambda: object(),
                      settings=BlossomSettings(tmp_path / "servers.json"))
    host._dispatch = lambda body, name, mime_type, added: sent.append((name, mime_type))
    # Kept for the whole run, as the window keeps its store: a store that
    # goes with the test can go on a worker's thread, with its network
    # objects, which ends the process.
    _STORES.append(host)
    return host


_STORES: list = []


def test_the_media_library_takes_types_from_the_same_table(tmp_path, monkeypatch):
    """One table of types for the whole app: the Media Library's own upload
    names a file's type from it too, whatever the computer's table says."""
    import mimetypes
    monkeypatch.setattr(mimetypes, "guess_type", lambda *a, **k: ("application/x-wrong", None))
    sent = []
    host = library_host(sent, tmp_path)
    for name in ("talk.m4a", "clip.mov", "photo.webp", "notes.pdf"):
        path = tmp_path / name
        path.write_bytes(b"data")
        host.upload_file(str(path))
    assert sent == [("talk.m4a", "audio/mp4"), ("clip.mov", "video/quicktime"),
                    ("photo.webp", "image/webp"), ("notes.pdf", "application/pdf")]
    assert media_files.type_of("photo.webp") == "image/webp"
    assert not media_files.is_audio_or_video("photo.webp")
    assert preview.player_label("https://cdn.example/a.png") is None


LARGE_SCRIPT = r"""
import json, os, sys, threading, pathlib
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv[:1])
from nostr.imports import workers
workers.BACKGROUND_FROM_BYTES = 0                 # every file counts as large here
import main_window

song, sha = sys.argv[2], sys.argv[3]
read_on = []
real_read = pathlib.Path.read_bytes
def read_bytes(self):
    read_on.append(threading.current_thread() is threading.main_thread())
    return real_read(self)
pathlib.Path.read_bytes = read_bytes

w = main_window.MainWindow()
w.show()
sent = []
w._media_store.upload_bytes = lambda body, *, name, mime_type, **kw: sent.append(name)
w.nostr_state._active = True
w._update_nostr_commands()
w._upload_choice = lambda: "now"                    # Settings: Upload Right Away
w._upload_limit = lambda: 10**9
ed = w.new_tab()
ed.setFocus()
ed.textCursor().insertText("Before.")
w._add_media_file(ed, song)
r = {"right away": ed.toPlainText()}
ed.textCursor().insertText(" Typed meanwhile.")          # the window answers meanwhile
for _ in range(200):
    app.processEvents()
    if sent:
        break
    threading.Event().wait(0.01)
r["after"] = [ed.document().findBlockByNumber(i).text()
              for i in range(ed.document().blockCount())]
r["sent"] = sent
r["read on the UI thread"] = [on for on in read_on if on]
ed.document().setModified(False)
w._media_not_uploaded = lambda doc: []
w.close()
print("RESULT " + json.dumps(r))
"""


def test_a_large_file_is_read_and_hashed_off_the_ui_thread(tmp_path):
    """The window stays responsive with a large video: the file is read and
    hashed, for the text and for its upload, on a worker thread; it goes in
    where the caret was."""
    song = tmp_path / "song.mp3"
    song.write_bytes(BODY)
    r = run_window_script(LARGE_SCRIPT, tmp_path, str(song), SHA)
    assert r["right away"] == "Before."
    # After the paragraph the caret was in, as a file inserted at once goes.
    assert r["after"][:2] == ["Before. Typed meanwhile.", "\ufffc"]
    assert r["sent"] == [f"myeditor-asset:{SHA}"]
    assert r["read on the UI thread"] == []


def test_the_media_library_reads_a_large_file_off_the_ui_thread(tmp_path, monkeypatch):
    import threading
    from PySide6.QtCore import QCoreApplication
    from nostr.blossom.store import MediaStore
    from nostr.imports import workers
    monkeypatch.setattr(workers, "BACKGROUND_FROM_BYTES", 0)
    main = []
    real = MediaStore._read_and_keep
    monkeypatch.setattr(MediaStore, "_read_and_keep", lambda self, path: (
        main.append(threading.current_thread() is threading.main_thread()), real(self, path))[1])
    sent = []
    host = library_host(sent, tmp_path)
    path = tmp_path / "talk.m4a"
    path.write_bytes(BODY)
    host.upload_file(str(path))
    for _ in range(200):
        QCoreApplication.processEvents()
        if sent:
            break
        threading.Event().wait(0.01)
    assert main == [False]
    assert sent == [("talk.m4a", "audio/mp4")]


UNDO_SCRIPT = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv[:1])
import main_window

song = sys.argv[2]
w = main_window.MainWindow()
w.show()
w._media_store.upload_bytes = lambda body, *, name, mime_type: None
w.nostr_state._active = True
w._update_nostr_commands()
w._upload_limit = lambda: 10**9
ed = w.new_tab()
ed.setFocus()
QTest.keyClicks(ed, "Top")
w._add_media_file(ed, song)
doc = ed.document()

def blocks():
    return [doc.findBlockByNumber(i).text() for i in range(doc.blockCount())]

def object_at():
    return next(doc.findBlockByNumber(i).position() for i in range(doc.blockCount())
                if ed.is_media_block(doc.findBlockByNumber(i)))

r = {"start": blocks()}
for where, offset in (("before", 0), ("after", 1)):
    c = ed.textCursor()
    c.setPosition(object_at() + offset)
    ed.setTextCursor(c)
    QTest.keyClicks(ed, "Words")
    r[where + " typed"] = blocks()
    QTest.keyClick(ed, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    r[where + " one undo"] = blocks()
    w.act_redo.trigger()
    r[where + " one redo"] = blocks()
    w.act_undo.trigger()
    r[where + " undone again"] = blocks()
# Return at the end of the file's paragraph, then words: one run too.
c = ed.textCursor()
c.setPosition(object_at() + 1)
ed.setTextCursor(c)
QTest.keyClick(ed, Qt.Key.Key_Return)
QTest.keyClicks(ed, "Below")
ed.undo()
r["return after, one undo"] = blocks()
ed.document().setModified(False)
w._media_not_uploaded = lambda doc: []
w.close()
print("RESULT " + json.dumps(r))
"""


def test_typing_at_a_file_is_one_step_of_undo(tmp_path):
    """As in Pages: the paragraph made for typing at a file and the words
    typed into it are one run of typing, one step of Undo; one Undo leaves
    the document exactly as it was, one Redo brings both back."""
    song = tmp_path / "song.mp3"
    song.write_bytes(BODY)
    r = run_window_script(UNDO_SCRIPT, tmp_path, str(song))
    start = r["start"]
    assert r["before typed"] == ["Top", "Words", "\ufffc", ""]
    assert r["after typed"] == ["Top", "\ufffc", "Words", ""]
    for where in ("before", "after"):
        assert r[where + " one undo"] == start
        assert r[where + " one redo"] == r[where + " typed"]
        assert r[where + " undone again"] == start
    assert r["return after, one undo"] == start


CLIPBOARD_SCRIPT = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QImage, QColor, QTextCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv[:1])
import main_window, paste
from nostr.blossom.store import MediaFile

song, sha = sys.argv[2], sys.argv[3]
w = main_window.MainWindow()
w.show()
w._media_store.upload_bytes = lambda body, *, name, mime_type: None
w.nostr_state._active = True
w._update_nostr_commands()
w._upload_choice = lambda: "now"                    # Settings: Upload Right Away
w._upload_limit = lambda: 10**9
ed = w.new_tab()
ed.setFocus()
QTest.keyClicks(ed, "Words")
w._add_media_file(ed, song)
url = f"https://cdn.example/{sha}"
w._media_store.upload_status.emit(f"myeditor-asset:{sha}", "uploading")
w._media_store.upload_finished.emit(f"myeditor-asset:{sha}", MediaFile(
    hash=sha, url=url, mime_type="audio/mpeg", size=1,
    urls=[{"server": "https://cdn.example", "url": url}]))
app.processEvents()
picture = QImage(4, 4, QImage.Format.Format_RGB32)
picture.fill(QColor("red"))
buffer = QBuffer()
buffer.open(QIODevice.OpenModeFlag.WriteOnly)
picture.save(buffer, "PNG")
asset = w._asset_manager.adopt_bytes(bytes(buffer.data()), alt="red")
c = ed.textCursor()
c.movePosition(QTextCursor.MoveOperation.End)
ed.setTextCursor(c)
w._insert_asset(ed, asset, alt="red")
ed.selectAll()
copied = ed.createMimeDataFromSelection()
private = (paste.LITERAL_MIME, paste.LITERAL_HTML_MIME)
r = {"formats": copied.formats(),
     "outside": {kind: bytes(copied.data(kind)).decode("utf-8", "replace")
                 for kind in copied.formats() if kind not in private},
     "private": {kind: bytes(copied.data(kind)).decode("utf-8", "replace")
                 for kind in private if copied.hasFormat(kind)}}
# Back into MyEditor, in a file that keeps its own formats (HTML): the
# objects themselves.
html_tab = w.new_tab()
html_tab._keeps_local_formats = lambda: True
html_tab.insertFromMimeData(copied)
doc = html_tab.document()
r["pasted back"] = sorted(set(main_window.iter_image_names(doc)))
for i in range(w.tabs.count()):
    e = w._editor_from_widget(w.tabs.widget(i))
    if e is not None:
        e.document().setModified(False)
w.close()
print("RESULT " + json.dumps(r))
"""


def test_no_internal_name_ever_leaves_the_app_on_the_clipboard(tmp_path):
    """Other apps get the real address in every format (a picture not
    uploaded yet: its bytes in HTML, its name in text); MyEditor's own
    private formats carry the objects, so a paste here brings them back."""
    song = tmp_path / "song.mp3"
    song.write_bytes(BODY)
    r = run_window_script(CLIPBOARD_SCRIPT, tmp_path, str(song), SHA)
    for kind, data in r["outside"].items():
        assert "myeditor-asset" not in data, kind
        assert "￼" not in data, kind
    assert f"{URL}.mp3" in r["outside"]["text/plain"]
    assert f'href="{URL}.mp3"' in r["outside"]["text/html"]
    assert "data:image/png;base64," in r["outside"]["text/html"]
    assert all("myeditor-asset" in data for data in r["private"].values())
    assert len(r["private"]) == 2
    assert len(r["pasted back"]) == 2 and all(n.startswith("myeditor-asset:")
                                              for n in r["pasted back"])


CLOSED_SCRIPT = r"""
import json, os, sys, threading
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv[:1])
from nostr.imports import workers
workers.BACKGROUND_FROM_BYTES = 0                 # every file is read in the background
import main_window

song, sha = sys.argv[2], sys.argv[3]
main_window._ask_save_changes = lambda parent, title, **kw: "discard"   # Don't Save
w = main_window.MainWindow()
w.show()
sent = []
w._media_store.upload_bytes = lambda body, *, name, mime_type: sent.append(name)
w.nostr_state._active = True
w._update_nostr_commands()
w._upload_limit = lambda: 10**9
r = {}
# Closed while the file is read: nothing goes into it, nothing is uploaded.
ed = w.new_tab()
ed.setFocus()
ed.textCursor().insertText("Abandoned.")
w._add_media_file(ed, song)
w.close_tab(w.tabs.currentIndex())
for _ in range(100):
    app.processEvents()
    threading.Event().wait(0.01)
r["closed: in the document"] = "￼" in ed.toPlainText()
r["closed: adopted"] = w._asset_manager.get(sha) is not None
r["closed: sent"] = list(sent)
r["closed: kept"] = w._media_image_loader.cache_path(sha).exists()
# Typed on while the file is read: it goes where the caret was.
ed = w.new_tab()
ed.setFocus()
ed.textCursor().insertText("Before.")
w._add_media_file(ed, song)
typing = ed.textCursor()
typing.insertText(" Typed meanwhile.")
typing.insertBlock()
typing.insertText("A paragraph typed meanwhile.")
for _ in range(100):
    app.processEvents()
    if sent:
        break
    threading.Event().wait(0.01)
r["typed: blocks"] = [ed.document().findBlockByNumber(i).text()
                      for i in range(ed.document().blockCount())]
ed.document().setModified(False)
w._media_not_uploaded = lambda doc: []
w.close()
print("RESULT " + json.dumps(r))
"""


def test_a_file_still_read_when_its_document_closes_goes_nowhere(tmp_path):
    """Review M2: closing the document (Don't Save) while a large file is
    read drops it: nothing goes into the closed document and nothing is
    uploaded. Typed on meanwhile, the file goes after the paragraph the
    caret was in, not after what was typed since."""
    song = tmp_path / "song.mp3"
    song.write_bytes(BODY)
    r = run_window_script(CLOSED_SCRIPT, tmp_path, str(song), SHA)
    assert r["closed: in the document"] is False
    assert r["closed: adopted"] is False and r["closed: sent"] == []
    assert r["closed: kept"] is False              # review N5: no copy left behind
    assert r["typed: blocks"][:2] == ["Before. Typed meanwhile.", "\ufffc"]
    assert r["typed: blocks"][-1] == "A paragraph typed meanwhile."


LOCAL_SCRIPT = r"""
import json, os, sys, threading
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv[:1])
import main_window
from doc_walk import iter_image_names

song, sha, folder = sys.argv[2], sys.argv[3], sys.argv[4]
w = main_window.MainWindow()
w.show()
sent = []
w._media_store.upload_bytes = lambda body, *, name, mime_type, **kw: sent.append(name)
w.nostr_state._active = True
w._update_nostr_commands()
w._upload_limit = lambda: 10**9
ed = w.new_tab()
ed.setFocus()
ed.textCursor().insertText("Listen:")
w._add_media_file(ed, song)
for _ in range(200):
    app.processEvents()
    if list(iter_image_names(ed.document())):
        break
    threading.Event().wait(0.01)
r = {"names": list(iter_image_names(ed.document())), "sent": sent,
     "state": w._asset_manager.get(sha).upload_state.value,
     "badge": w._media_badge(ed, f"myeditor-asset:{sha}").words}
path = os.path.join(folder, "My Trip.md")
w._save_to(path)
r["saved"] = open(path, encoding="utf-8").read()
r["copy"] = os.path.exists(os.path.join(folder, "My Trip_media", sha + ".mp3"))
w.close_tab(w.tabs.currentIndex())
w.open_path(path)
app.processEvents()
r["reopened"] = list(iter_image_names(w.current_editor().document()))
r["sent at last"] = sent
w.current_editor().document().setModified(False)
w.close()
print("RESULT " + json.dumps(r))
"""


def test_audio_added_stays_on_this_computer_and_survives_a_save(tmp_path):
    """Added, it stays on this computer, its badge says so, and nothing is
    sent; a .md file keeps it as a copy beside itself (a link to it, which
    a folder name with a space must not break) and reopens it as its card."""
    song = tmp_path / "song.mp3"
    song.write_bytes(BODY)
    folder = tmp_path / "docs"
    folder.mkdir()
    r = run_window_script(LOCAL_SCRIPT, tmp_path, str(song), SHA, str(folder))
    assert r["names"] == [f"myeditor-asset:{SHA}"]
    assert r["sent"] == [] and r["state"] == "local"
    assert r["badge"] == "On This Computer"
    assert f"[song.mp3](<My Trip_media/{SHA}.mp3>)" in r["saved"]
    assert r["copy"] is True
    assert r["reopened"] == [f"myeditor-asset:{SHA}"]
    assert r["sent at last"] == []


QUIT_READ_SCRIPT = r"""
import json, os, sys, threading
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv[:1])
from nostr.imports import workers
workers.BACKGROUND_FROM_BYTES = 0
import main_window

song, sha = sys.argv[2], sys.argv[3]
w = main_window.MainWindow()
w.show()
sent = []
w._media_store.upload_bytes = lambda body, *, name, mime_type: sent.append(name)
w.nostr_state._active = True
w._update_nostr_commands()
w._upload_limit = lambda: 10**9
ed = w.new_tab()
w._add_media_file(ed, song)
ed.document().setModified(False)
event = QCloseEvent()
w.closeEvent(event)                       # quitting while the file is read
for _ in range(100):
    app.processEvents()
    threading.Event().wait(0.01)
print("RESULT " + json.dumps({"quit": event.isAccepted(), "sent": sent,
                              "adopted": w._asset_manager.get(sha) is not None,
                              "kept": w._media_image_loader.cache_path(sha).exists()}))
"""


def test_a_file_still_read_when_the_app_quits_goes_nowhere(tmp_path):
    song = tmp_path / "song.mp3"
    song.write_bytes(BODY)
    r = run_window_script(QUIT_READ_SCRIPT, tmp_path, str(song), SHA)
    assert r == {"quit": True, "sent": [], "adopted": False, "kept": False}


QUIT_WRITING_SCRIPT = r"""
import json, os, sys, threading
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv[:1])
from nostr.imports import workers
workers.BACKGROUND_FROM_BYTES = 0
import main_window

song, sha, stage = sys.argv[2], sys.argv[3], sys.argv[4]
w = main_window.MainWindow()
w.show()
w._media_store.upload_bytes = lambda body, *, name, mime_type: None
w.nostr_state._active = True
w._update_nostr_commands()
w._upload_limit = lambda: 10**9
store = w._asset_manager._blob_store
real_put = store.put_bytes
writing, kept = threading.Event(), threading.Event()

def slow_put(data):
    writing.set()
    threading.Event().wait(0.5)            # a large file on its way to disk
    answer = real_put(data)
    kept.set()
    return answer

store.put_bytes = slow_put
ed = w.new_tab()
w._add_media_file(ed, song)
ed.document().setModified(False)
# Quit while the copy is being written, or just after it was kept but
# before its result was handled; no event is handled after that, as when
# the app goes.
(writing if stage == "writing" else kept).wait(10)
w.closeEvent(QCloseEvent())
threading.Event().wait(1.0)                # the worker ends meanwhile
print("RESULT " + json.dumps({"kept": store.cache_path(sha).exists()}))
"""


@pytest.mark.parametrize("stage", ["writing", "kept"])
def test_quitting_whatever_point_a_read_has_reached_leaves_no_copy(tmp_path, stage):
    """The N5 race: a quit right as a read keeps its copy left the copy,
    since its result was never handled once the app had gone."""
    song = tmp_path / "song.mp3"
    song.write_bytes(BODY)
    r = run_window_script(QUIT_WRITING_SCRIPT, tmp_path, str(song), SHA, stage)
    assert r == {"kept": False}


SHARED_READ_SCRIPT = r"""
import json, os, sys, threading
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv[:1])
from nostr.imports import workers
workers.BACKGROUND_FROM_BYTES = 0
import main_window

song, sha = sys.argv[2], sys.argv[3]
told = []
main_window.inform = lambda parent, **kw: told.append(kw.get("title", ""))
main_window._ask_save_changes = lambda parent, title, **kw: "discard"
w = main_window.MainWindow()
w.show()
w._media_store.upload_bytes = lambda body, *, name, mime_type: None
w.nostr_state._active = True
w._update_nostr_commands()
w._upload_limit = lambda: 10**9
store = w._asset_manager._blob_store
real_put = store.put_bytes
puts = threading.Semaphore(0)

def counted_put(data):
    answer = real_put(data)
    puts.release()
    return answer

store.put_bytes = counted_put
first = w.new_tab()
w._add_media_file(first, song)
second = w.new_tab()
w._add_media_file(second, song)
puts.acquire(timeout=10)                  # both reads have kept their copy
puts.acquire(timeout=10)
w.close_tab(w._editor_widget_index(first))
for _ in range(100):
    app.processEvents()
    threading.Event().wait(0.01)
print("RESULT " + json.dumps({
    "told": told,
    "second has it": "￼" in second.toPlainText(),
    "kept": store.cache_path(sha).exists()}))
second.document().setModified(False)
w._media_not_uploaded = lambda doc: []
"""


def test_closing_one_of_two_documents_reading_the_same_file_spares_the_other(tmp_path):
    """Round 5, L1: the closed document's read took the shared copy away,
    and the other document then refused the file."""
    song = tmp_path / "song.mp3"
    song.write_bytes(BODY)
    r = run_window_script(SHARED_READ_SCRIPT, tmp_path, str(song), SHA)
    assert r == {"told": [], "second has it": True, "kept": True}


FAILED_READ_SCRIPT = r"""
import json, os, sys, threading
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv[:1])
from nostr.imports import workers
workers.BACKGROUND_FROM_BYTES = 0
import main_window

song = sys.argv[2]
told = []
main_window.inform = lambda parent, **kw: told.append(kw.get("title", ""))
main_window._ask_save_changes = lambda parent, title, **kw: "discard"
w = main_window.MainWindow()
w.show()
w.nostr_state._active = True
w._update_nostr_commands()
w._upload_limit = lambda: 10**9
closed = threading.Event()

def failing_read(path, keeping=None, kept=None):
    closed.wait(10)
    raise OSError("the disk went away")

w._asset_manager.store_media_file = failing_read
r = {}
for case in ("closed", "open"):
    ed = w.new_tab()
    w._add_media_file(ed, song)
    if case == "closed":
        w.close_tab(w._editor_widget_index(ed))
    closed.set()
    for _ in range(100):
        app.processEvents()
        threading.Event().wait(0.01)
    r[case] = list(told)
    told.clear()
    closed.clear()
    if case == "open":
        ed.document().setModified(False)
print("RESULT " + json.dumps(r))
"""


def test_a_read_failing_for_a_closed_document_ends_quietly(tmp_path):
    """Round 5, L3: the document it was for is gone, as in Mail and the
    Finder after a cancelled operation; for an open one, it is said."""
    song = tmp_path / "song.mp3"
    song.write_bytes(BODY)
    r = run_window_script(FAILED_READ_SCRIPT, tmp_path, str(song))
    assert r == {"closed": [], "open": ["Couldn’t add “song.mp3”"]}


def test_keeping_a_large_copy_never_holds_up_another_file(tmp_path):
    """Round 6, M1: the lock that counts kept copies was held while a whole
    file was hashed and written, so the UI thread waited for another
    file's write (releasing its read, or keeping a small file)."""
    import threading
    import time
    from PySide6.QtCore import QObject, Signal
    from nostr.media.assets import AssetIndex
    from nostr.media.manager import AssetManager
    cache = tmp_path / "cache"
    cache.mkdir()
    writing = threading.Event()

    class Store(QObject):
        upload_status = Signal(str, str)
        upload_finished = Signal(str, object)
        upload_failed = Signal(str, str)
        upload_failed_code = Signal(str, str)
        ready = Signal(str, str, object)
        failed = Signal(str, str)
        url_ready = Signal(str, str, object)
        url_failed = Signal(str, str)
        files = {}

        def cache_path(self, sha):
            return cache / sha

        def has(self, sha):
            return (cache / sha).is_file()

        def put_bytes(self, data):
            sha = hashlib.sha256(data).hexdigest()
            if len(data) > 1000:                   # the large one writes slowly
                writing.set()
                time.sleep(1.5)
            (cache / sha).write_bytes(data)
            return sha

        def load(self, sha, url):
            pass

    store = Store()
    manager = AssetManager(blob_store=store, uploader=store, profile_provider=lambda: None,
                           index=AssetIndex(tmp_path / "index.json"))
    for name, size in (("big.mp4", 5000), ("small.mp3", 500), ("done.mp4", 600)):
        (tmp_path / name).write_bytes(os.urandom(size))
    done, _size = manager.store_media_file(str(tmp_path / "done.mp4"))
    big = threading.Thread(target=manager.store_media_file, args=(str(tmp_path / "big.mp4"),))
    big.start()
    writing.wait(5)
    started = time.monotonic()
    manager.release_stored(done)
    manager.store_media_file(str(tmp_path / "small.mp3"))
    waited = time.monotonic() - started
    big.join()
    assert waited < 0.5, waited
    assert not (cache / done).exists()             # released, nobody else held it


def test_a_copy_whose_write_failed_gives_its_count_back(tmp_path):
    """658d04e counts a copy before writing it; a write that fails gives
    the count back, so the copy another read of the same file keeps is
    taken out when that one lets it go, and only then."""
    from PySide6.QtCore import QObject, Signal
    from nostr.media.assets import AssetIndex
    from nostr.media.manager import AssetManager
    cache = tmp_path / "cache"
    cache.mkdir()
    failing = []

    class Store(QObject):
        upload_status = Signal(str, str)
        upload_finished = Signal(str, object)
        upload_failed = Signal(str, str)
        upload_failed_code = Signal(str, str)
        ready = Signal(str, str, object)
        failed = Signal(str, str)
        url_ready = Signal(str, str, object)
        url_failed = Signal(str, str)
        files = {}

        def cache_path(self, sha):
            return cache / sha

        def has(self, sha):
            return (cache / sha).is_file()

        def put_bytes(self, data):
            if failing:
                raise OSError("the disk is full")
            sha = hashlib.sha256(data).hexdigest()
            (cache / sha).write_bytes(data)
            return sha

        def load(self, sha, url):
            pass

    store = Store()
    manager = AssetManager(blob_store=store, uploader=store, profile_provider=lambda: None,
                           index=AssetIndex(tmp_path / "index.json"))
    (tmp_path / "talk.mp4").write_bytes(os.urandom(600))
    sha, _size = manager.store_media_file(str(tmp_path / "talk.mp4"))     # one read keeps it
    failing.append(True)
    with pytest.raises(OSError):
        manager.store_media_file(str(tmp_path / "talk.mp4"))             # another one's fails
    assert manager._kept_for == {sha: 1}
    assert (cache / sha).exists()
    manager.release_stored(sha)                                          # the first lets it go
    assert manager._kept_for == {} and not (cache / sha).exists()


REOPEN_SCRIPT = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv[:1])
import main_window
from doc_walk import iter_image_names

text = sys.argv[2]
w = main_window.MainWindow()
w.nostr_state._active = True
w._update_nostr_commands()
ed = w.new_tab()
w._load_markdown(ed, text)
names = list(iter_image_names(ed.document()))
asset = w._asset_manager.get(names[0].split(":", 1)[1]) if names else None
r = {"names": names, "media": bool(asset and asset.is_media),
     "from_address": bool(asset and asset.from_address),
     "published": w._publish_payload(ed, "markdown")[0]}
ed.document().setModified(False)
w.close()
print("RESULT " + json.dumps(r))
"""


def test_a_reopened_article_shows_its_audio_and_video_as_cards_again(tmp_path):
    """An article or draft reopened held its video as a bare address: it
    reads back as the card it was written from, and publishes the same."""
    text = f"Before\n\n{URL}.mp4\n\nAfter"
    r = run_window_script(REOPEN_SCRIPT, tmp_path, text)
    assert len(r["names"]) == 1 and r["media"] and r["from_address"]
    assert r["published"] == text
