# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins where a picture, audio or video of a document is, and that the
person can always tell: on this computer only until the document is
published, on its way, failed, or on the media servers.

The defect classes this file guards against:
- a picture on this computer looking just like one anyone can fetch,
- a badge drawn where the object is not, or on an object too small for it,
- a badge or a menu command showing without a Nostr account,
- publishing waiting forever, or going on before every file is there,
- a document whose file name has a space losing its pictures and files
  when it is reopened.
"""

import os
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt, QUrl
from PySide6.QtGui import QGuiApplication, QImage, QPainter, QTextDocument, QTextImageFormat
from PySide6.QtWidgets import QApplication, QMenu

import image_safety
import media_badge
from editor import HtmlEditor
from main_window import MainWindow
from markdown_writer import holds_faithfully, link_markdown
from nostr.media.assets import AssetIndex, AssetState
from nostr.media.manager import AssetManager
from nostr.media.upload_wait import UploadWait

from tests.media_fakes import GIF_BYTES, PNG_BYTES, FakeBlobStore, FakeUploader, sha_of


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def _manager(tmp_path, profile=object()):
    return AssetManager(blob_store=FakeBlobStore(Path(tmp_path) / "cache"),
                        uploader=FakeUploader(), profile_provider=lambda: profile,
                        decoder=image_safety.decode_image_bytes,
                        index=AssetIndex(path=Path(tmp_path) / "media_assets.json"))


def _uploaded(manager, data=PNG_BYTES):
    adopted = manager.adopt_bytes(data, alt="harbour")
    return manager.adopt_library_file(sha256=adopted.sha256, mime=adopted.mime, alt="harbour",
                                      remote_url=f"https://cdn.example/{adopted.sha256}.png")


def _finish(manager):
    uploader = manager._uploader
    call = uploader.calls[-1]
    uploader.finish(call.name, types.SimpleNamespace(
        url=f"https://cdn.example/{sha_of(call.body)}.png", size=len(call.body), servers=()))
    QApplication.processEvents()


def _fail(manager, name=None):
    manager._uploader.fail(name or manager._uploader.calls[-1].name, "UPLOAD_FAILED")
    QApplication.processEvents()


# --------------------------------------------------------------------------- #
# The badge, where the object is
# --------------------------------------------------------------------------- #

def _editor_with_picture(width, height, name="myeditor-asset:" + "a" * 64):
    ed = HtmlEditor()
    ed.resize(700, 500)
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(0x336699)
    ed.document().addResource(QTextDocument.ImageResource, QUrl(name), image)
    cursor = ed.textCursor()
    cursor.insertText("Above")
    cursor.insertBlock()
    fmt = QTextImageFormat()
    fmt.setName(name)
    cursor.insertImage(fmt)
    ed.show()
    QApplication.processEvents()
    return ed


def test_an_object_is_found_where_it_is_drawn():
    ed = _editor_with_picture(240, 120)
    [(name, box)] = list(media_badge.objects_in_sight(ed))
    at = ed.document().find("￼")
    at.setPosition(at.selectionStart())
    caret = ed.cursorRect(at)
    assert name == "myeditor-asset:" + "a" * 64
    assert (round(box.width()), round(box.height())) == (240, 120)
    assert (box.left(), box.top()) == (caret.left(), caret.top())
    assert media_badge.object_at(ed, box.center().toPoint()) == name
    assert media_badge.object_at(ed, QPoint(int(box.right()) + 20, int(box.center().y()))) is None
    ed.close()


def _painted(ed, badge_of):
    asked = []
    image = QImage(ed.viewport().size(), QImage.Format.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    media_badge.paint(ed, painter, lambda name: asked.append(name) or badge_of(name))
    painter.end()
    return asked, image


def test_a_badge_is_drawn_on_the_objects_top_left_corner():
    ed = _editor_with_picture(240, 120)
    [(_name, box)] = list(media_badge.objects_in_sight(ed))
    badge = media_badge.Badge(media_badge.LOCAL, "On This Computer")
    asked, image = _painted(ed, lambda name: badge)
    assert len(asked) == 1
    inside = box.topLeft().toPoint() + QPoint(14, 18)
    outside = box.bottomRight().toPoint() - QPoint(10, 10)
    assert image.pixelColor(inside).alpha() > 0
    assert image.pixelColor(outside).alpha() == 0
    ed.close()


def _image_fmt(name, width=0, height=0):
    fmt = QTextImageFormat()
    fmt.setName(name)
    if width:
        fmt.setWidth(width)
        fmt.setHeight(height)
    return fmt


def _show(ed):
    ed.resize(700, 400)
    ed.show()
    QApplication.processEvents()


def _drawn_at(ed, name):
    """Where Qt draws the object named ``name``: from the caret before it."""
    at = ed.document().find("\ufffc")
    at.setPosition(at.selectionStart())
    return ed.cursorRect(at)


def test_an_object_in_a_table_cell_is_found_where_it_is_drawn():
    # A cell's paragraph is laid out within its cell: the badge was drawn
    # at the corner of the view, over the text there.
    ed = HtmlEditor()
    cursor = ed.textCursor()
    cursor.insertText("Above")
    cursor.insertBlock()
    table = cursor.insertTable(2, 2)
    table.cellAt(1, 1).firstCursorPosition().insertImage(_image_fmt("pic", 200, 100))
    _show(ed)
    [(_name, box)] = list(media_badge.objects_in_sight(ed))
    caret = _drawn_at(ed, "pic")              # whole pixels: a cell's padding is not
    assert abs(box.left() - caret.left()) <= 1 and abs(box.top() - caret.top()) <= 1
    assert (box.width(), box.height()) == (200, 100)
    ed.close()


def test_an_object_beside_a_tall_cell_is_still_found():
    ed = HtmlEditor()
    table = ed.textCursor().insertTable(1, 2)
    table.cellAt(0, 0).firstCursorPosition().insertText("\n".join(["line"] * 80))
    table.cellAt(0, 1).firstCursorPosition().insertImage(_image_fmt("pic", 200, 100))
    _show(ed)
    assert [name for name, _box in media_badge.objects_in_sight(ed)] == ["pic"]
    ed.close()


def test_an_object_in_right_to_left_text_has_its_width():
    ed = HtmlEditor()
    cursor = ed.textCursor()
    block = cursor.blockFormat()
    block.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
    cursor.setBlockFormat(block)
    cursor.insertImage(_image_fmt("pic", 200, 100))
    _show(ed)
    [(_name, box)] = list(media_badge.objects_in_sight(ed))
    assert (box.width(), box.height()) == (200, 100)
    assert media_badge.object_at(ed, box.center().toPoint()) == "pic"
    ed.close()


def test_the_badge_keeps_its_words_inside_at_the_largest_text_size():
    ed = _editor_with_picture(400, 200)
    font = ed.font()
    font.setPointSizeF(40)
    ed.setFont(font)
    QApplication.processEvents()
    [(_name, box)] = list(media_badge.objects_in_sight(ed))
    _asked, image = _painted(ed, lambda name: media_badge.Badge(media_badge.LOCAL,
                                                                 "On This Computer"))
    # The capsule ends above the middle of the picture: its words are a
    # badge's size, whatever the text's.
    middle = box.topLeft().toPoint() + QPoint(14, 40)
    assert image.pixelColor(middle).alpha() == 0
    ed.close()


def test_an_object_too_small_for_a_badge_wears_none():
    ed = _editor_with_picture(40, 20)
    asked, _image = _painted(ed, lambda name: media_badge.Badge(media_badge.LOCAL, "x"))
    assert asked == []
    ed.close()


def test_the_editor_shows_the_badge_the_window_answers_as_its_tooltip():
    ed = _editor_with_picture(240, 120)
    ed.set_media_badges(lambda name: media_badge.Badge(media_badge.LOCAL, "On", "Only here."))
    [(_name, box)] = list(media_badge.objects_in_sight(ed))
    assert ed._badge_at(box.center().toPoint()).hint == "Only here."
    assert ed.media_object_at(box.center().toPoint()) == _name
    ed.close()


# --------------------------------------------------------------------------- #
# What the window answers
# --------------------------------------------------------------------------- #

class _Window:
    _asset_named = MainWindow._asset_named
    _local_image_bytes = staticmethod(MainWindow._local_image_bytes)
    _has_local_image = staticmethod(MainWindow._has_local_image)
    _media_badge = MainWindow._media_badge
    _media_context = MainWindow._media_context
    _upload_object = MainWindow._upload_object
    _copy_link = staticmethod(MainWindow._copy_link)

    def __init__(self, tmp_path, *, nostr=True):
        self._asset_manager = _manager(tmp_path)
        self.nostr_state = types.SimpleNamespace(active=nostr)
        self.status = types.SimpleNamespace(showMessage=lambda *a, **context: None)


def _places(win, ed, *names):
    return [getattr(win._media_badge(ed, name), "place", None) for name in names]


def test_each_object_wears_where_it_is(tmp_path):
    win = _Window(tmp_path)
    manager = win._asset_manager
    local = manager.adopt_bytes(GIF_BYTES, alt="sketch")
    hosted = _uploaded(manager)
    (tmp_path / "Trip_media").mkdir()
    (tmp_path / "Trip_media" / "sea.png").write_bytes(PNG_BYTES)
    ed = HtmlEditor()
    ed._file_path = str(tmp_path / "Trip.md")
    assert _places(win, ed, local.key, hosted.key, hosted.remote_url,
                   "https://elsewhere.example/cat.png", "Trip_media/sea.png") == [
        media_badge.LOCAL, media_badge.UPLOADED, media_badge.UPLOADED, None, media_badge.LOCAL]
    manager.request_upload(local.sha256)
    assert _places(win, ed, local.key) == [media_badge.UPLOADING]
    manager._uploader.fail(manager._uploader.calls[-1].name, "UPLOAD_FAILED")
    failed = win._media_badge(ed, local.key)
    assert failed.place == media_badge.FAILED and failed.hint.endswith("Publishing tries again.")


def test_what_is_not_on_this_computer_wears_no_badge(tmp_path):
    # A picture beside no file, a record with no bytes, a key with no
    # record: nothing to upload, so nothing says it is here.
    win = _Window(tmp_path)
    ed = HtmlEditor()
    ed._file_path = str(tmp_path / "Trip.md")
    gone = win._asset_manager.adopt_bytes(GIF_BYTES, alt="sketch")
    win._asset_manager._blob_store.cache_path(gone.sha256).unlink()
    assert _places(win, ed, "Trip_media/gone.png", gone.key, "myeditor-asset:" + "b" * 64) == [
        None, None, None]


def test_nothing_of_it_shows_without_a_nostr_account(tmp_path):
    win = _Window(tmp_path, nostr=False)
    local = win._asset_manager.adopt_bytes(GIF_BYTES, alt="sketch")
    assert win._media_badge(HtmlEditor(), local.key) is None


def _menu_on(win, ed, name):
    ed.media_object_at = lambda pos: name
    menu = QMenu()
    win._media_context(menu, ed, QPoint(0, 0))
    return menu


def test_a_local_object_can_be_uploaded_from_its_menu(tmp_path):
    win = _Window(tmp_path)
    local = win._asset_manager.adopt_bytes(GIF_BYTES, alt="sketch")
    menu = _menu_on(win, HtmlEditor(), local.key)
    [upload] = [a for a in menu.actions() if a.text() == "Upload to Media Servers"]
    upload.trigger()
    assert win._asset_manager.get(local.sha256).upload_state is AssetState.SIGNING


def test_an_uploaded_object_gives_its_link(tmp_path):
    win = _Window(tmp_path)
    hosted = _uploaded(win._asset_manager)
    menu = _menu_on(win, HtmlEditor(), hosted.key)
    # Named apart from Copy Link, which a link around the picture brings.
    [copy] = [a for a in menu.actions() if a.text() == "Copy Media Link"]
    copy.trigger()
    assert QGuiApplication.clipboard().text() == hosted.remote_url


def test_the_menu_offers_nothing_of_it_without_an_account(tmp_path):
    win = _Window(tmp_path, nostr=False)
    local = win._asset_manager.adopt_bytes(GIF_BYTES, alt="sketch")
    assert _menu_on(win, HtmlEditor(), local.key).actions() == []


# --------------------------------------------------------------------------- #
# Audio and video a .md file holds as a copy beside it
# --------------------------------------------------------------------------- #

class _Reopening(_Window):
    _media_from_addresses = MainWindow._media_from_addresses
    _media_copy_linked = MainWindow._media_copy_linked
    _adopt_media_copy = MainWindow._adopt_media_copy
    _editor_kind = MainWindow._editor_kind
    _may_insert_audio_video = MainWindow._may_insert_audio_video


SONG = b"ID3" + b"sound" * 40


def _reopened(tmp_path, link, data=SONG, name=None):
    """A .md beside a copy of ``data`` (called ``name``, else its hash),
    read back: the editor, and the window."""
    win = _Reopening(tmp_path)
    folder = tmp_path / "Doc_media"
    folder.mkdir(exist_ok=True)
    name = name or sha_of(data) + ".mp3"
    if data is not None:
        (folder / name).write_bytes(data)
    ed = HtmlEditor()
    ed._file_path = str(tmp_path / "Doc.md")
    from markdown_writer import read_markdown
    read_markdown(ed.document(), f"Listen:\n\n{link.format(name=name)}\n")
    win._media_from_addresses(ed)
    return ed, win


def _cards(ed):
    from doc_walk import iter_blocks
    found = []
    for block in iter_blocks(ed.document()):
        it = block.begin()
        while not it.atEnd():
            fmt = it.fragment().charFormat()
            if fmt.isImageFormat():
                image = fmt.toImageFormat()
                found.append((image.name(), image.property(QTextImageFormat.ImageAltText)))
            it += 1
    return found


def test_a_copy_beside_the_document_is_its_card_again_keeping_its_words(tmp_path):
    ed, _win = _reopened(tmp_path, "[Our wedding song](Doc_media/{name})")
    assert _cards(ed) == [(f"myeditor-asset:{sha_of(SONG)}", "Our wedding song")]


def test_a_copy_changed_since_is_taken_for_what_it_holds(tmp_path):
    # Its name is the hash of what was copied; the person trimmed it since.
    edited = b"ID3" + b"trimmed" * 10
    ed, win = _reopened(tmp_path, "[song.mp3](Doc_media/{name})", data=edited,
                        name=sha_of(SONG) + ".mp3")
    [(name, _words)] = _cards(ed)
    assert name == f"myeditor-asset:{sha_of(edited)}"
    assert (tmp_path / "Doc_media" / f"{sha_of(SONG)}.mp3").read_bytes() == edited


def test_a_link_the_person_wrote_to_a_file_of_their_own_stays_a_link(tmp_path):
    ed, _win = _reopened(tmp_path, "[my interview](Doc_media/{name})", name="interview.mp3")
    assert _cards(ed) == []


def test_a_copy_that_leads_outside_the_media_folder_stays_a_link(tmp_path):
    outside = tmp_path / "private.mp3"
    outside.write_bytes(SONG)
    (tmp_path / "Doc_media").mkdir()
    (tmp_path / "Doc_media" / (sha_of(SONG) + ".mp3")).symlink_to(outside)
    ed, _win = _reopened(tmp_path, "[song](Doc_media/{name})", data=None,
                         name=sha_of(SONG) + ".mp3")
    assert _cards(ed) == []


def test_a_copy_larger_than_a_media_server_takes_is_not_read(tmp_path, monkeypatch):
    import main_window as main_window_module
    monkeypatch.setattr(main_window_module, "MAX_MEDIA_BYTES", 10)
    ed, _win = _reopened(tmp_path, "[song](Doc_media/{name})")
    assert _cards(ed) == []


# --------------------------------------------------------------------------- #
# Waiting for uploads
# --------------------------------------------------------------------------- #

_WAITS = []                  # held as the window holds its wait


def _wait(manager, *assets):
    wait = UploadWait(manager, [asset.sha256 for asset in assets])
    _WAITS.append(wait)
    seen = []
    wait.done.connect(lambda: seen.append("done"))
    wait.failed.connect(lambda sha, code: seen.append(("failed", sha, code)))
    wait.start()
    QApplication.processEvents()
    return seen


def test_a_wait_for_files_already_there_is_done_at_once(tmp_path):
    manager = _manager(tmp_path)
    assert _wait(manager, _uploaded(manager)) == ["done"]


def test_a_wait_is_done_only_once_every_file_is_there(tmp_path):
    manager = _manager(tmp_path)
    first = manager.adopt_bytes(GIF_BYTES, alt="one")
    second = manager.adopt_bytes(PNG_BYTES, alt="two")
    seen = _wait(manager, first, second)
    _finish(manager)
    assert seen == []
    _finish(manager)
    assert seen == ["done"]


def test_a_wait_ends_at_the_first_failure_saying_which(tmp_path):
    manager = _manager(tmp_path)
    local = manager.adopt_bytes(GIF_BYTES, alt="one")
    seen = _wait(manager, local)
    _fail(manager)
    _finish(manager)
    assert seen == [("failed", local.sha256, "UPLOAD_FAILED")]


def test_a_wait_cancelled_says_nothing_more(tmp_path):
    manager = _manager(tmp_path)
    local = manager.adopt_bytes(GIF_BYTES, alt="one")
    wait = UploadWait(manager, [local.sha256])
    seen = []
    wait.done.connect(lambda: seen.append("done"))
    wait.start()
    wait.cancel()
    _finish(manager)
    assert seen == []


# --------------------------------------------------------------------------- #
# A document whose name has a space
# --------------------------------------------------------------------------- #

def test_an_address_with_a_space_is_written_so_it_reads_back():
    assert link_markdown("Walk [1].mp4", "My Trip_media/a.mp4") == (
        "[Walk \\[1\\].mp4](<My Trip_media/a.mp4>)")
    assert link_markdown("Walk.mp4", "Trip_media/a.mp4") == "[Walk.mp4](Trip_media/a.mp4)"


@pytest.mark.parametrize("markdown, faithful", [
    ("Look:\n\n![sea](<My Trip_media/a.png>)\n", True),
    ("Listen:\n\n[song.mp3](<My Trip_media/a.mp3>)\n", True),
    ("a <b>bold</b> word\n", False),
    ("[a](<b c>) and <span>x</span>\n", False),
])
def test_a_link_address_in_angle_brackets_is_not_raw_html(markdown, faithful):
    # It opened as its Markdown text, the pictures and files gone from view.
    assert holds_faithfully(markdown) is faithful
