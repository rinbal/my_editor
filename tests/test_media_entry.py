# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins how an image enters a document and what leaves the machine after.

The defect classes this file guards against:
- a pasted screenshot, which exists nowhere else, being dropped because
  no signer is connected, an upload failed or the network is down,
- a paste or a drop putting a file on a public server, or asking about
  it, before the document is published (unless Upload Right Away is
  chosen in Settings),
- a drop refusing to insert until a signer is connected,
- an asset name falling through to Qt's default resolution, which reads
  it as a relative file path,
- an unresolved asset name being re-asked on every repaint, which turns
  one missing blob into a resolver storm.
"""

import os
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QMimeData, QPointF, Qt, QUrl
from PySide6.QtGui import (
    QColor, QDropEvent, QImage, QKeyEvent, QTextDocument, QTextImageFormat,
)
from PySide6.QtWidgets import QApplication

import alerts
import image_safety
import main_window as main_window_module
from doc_walk import iter_image_names
from editor import HtmlEditor
from main_window import MainWindow
from nostr.media.assets import ASSET_SCHEME, AssetIndex, AssetState, asset_key
from nostr.media.manager import AssetManager

from tests.media_fakes import (
    PNG_BYTES, TEXT_BYTES, FakeBlobStore, FakeUploader, fake_alert, sha_of,
)


TRAVERSAL_NAME = f"{ASSET_SCHEME}:../../etc/passwd"


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def _image_fmt(name, alt="image"):
    fmt = QTextImageFormat()
    fmt.setName(name)
    fmt.setProperty(QTextImageFormat.ImageAltText, alt)
    return fmt


def _clipboard_image(color="blue"):
    image = QImage(4, 4, QImage.Format_RGB32)
    image.fill(QColor(color))
    return image


# --------------------------------------------------------------------------- #
# The editor's resource seam
# --------------------------------------------------------------------------- #

class _CountingEditor(HtmlEditor):
    """Records its own paints, so a cached lookup is provably cached."""

    def __init__(self):
        super().__init__()
        self.paints = 0

    def paintEvent(self, event):
        self.paints += 1
        super().paintEvent(event)


def _resolver_calls_for(app, *, answer):
    """Show one asset image and repaint it, counting resolver calls."""
    calls = []
    ed = _CountingEditor()
    ed.set_resource_resolver(lambda name: calls.append(name) or answer,
                            ASSET_SCHEME)
    cursor = ed.textCursor()
    cursor.insertImage(_image_fmt(asset_key("a" * 64)))
    ed.show()
    app.processEvents()
    doc = ed.document()
    for _ in range(4):
        doc.markContentsDirty(0, doc.characterCount())
        ed.repaint()
        app.processEvents()
    assert ed.paints > 1, "the repaint loop has to actually repaint"
    return calls


def test_a_resolved_asset_is_asked_for_once(qt_app):
    image = QImage(5, 5, QImage.Format_RGB32)
    image.fill(QColor("red"))

    assert _resolver_calls_for(qt_app, answer=image) == [asset_key("a" * 64)]


def test_an_unresolved_asset_is_also_asked_for_once(qt_app):
    # Qt caches an image and re-asks for a null result, so the miss has
    # to answer with the placeholder or every repaint becomes a lookup.
    assert _resolver_calls_for(qt_app, answer=None) == [asset_key("a" * 64)]


def test_an_asset_name_never_falls_through_to_qt(tmp_path):
    # Qt's default resolution reads the name as a relative file path.
    ed = HtmlEditor()
    ed.set_resource_resolver(lambda name: None, ASSET_SCHEME)

    resolved = ed.loadResource(QTextDocument.ImageResource,
                               QUrl(asset_key("b" * 64)))

    assert isinstance(resolved, QImage)
    assert resolved is ed._placeholder_image()


def test_a_traversal_asset_name_is_inert(tmp_path):
    # myeditor-asset:../../etc/passwd reaches the resolver as a name and
    # must never become a path.
    class _TrapStore(FakeBlobStore):
        def cache_path(self, sha256):
            raise AssertionError("a document name reached the filesystem")

        def has(self, sha256):
            raise AssertionError("a document name reached the filesystem")

        def load(self, sha256, url):
            raise AssertionError("a document name reached the network")

    manager = AssetManager(
        blob_store=_TrapStore(tmp_path / "cache"),
        uploader=FakeUploader(),
        profile_provider=lambda: None,
        decoder=image_safety.decode_image_bytes,
        index=AssetIndex(path=tmp_path / "media_assets.json"),
    )
    ed = HtmlEditor()
    ed.set_resource_resolver(manager.resolve_image, ASSET_SCHEME)
    cursor = ed.textCursor()
    cursor.insertImage(_image_fmt(TRAVERSAL_NAME, "hostile"))

    assert manager.resolve_image(TRAVERSAL_NAME) is None
    assert ed.loadResource(QTextDocument.ImageResource,
                           QUrl(TRAVERSAL_NAME)) is ed._placeholder_image()
    assert TRAVERSAL_NAME in ed.document().toHtml(), "the fragment survives"


def test_a_late_arriving_image_does_not_dirty_the_document(qt_app):
    # The late fill repaints over the placeholder. Setting the modified
    # flag here would mark a saved document unsaved and trigger a
    # spurious crash backup.
    ed = HtmlEditor()
    ed.set_resource_resolver(lambda name: None, ASSET_SCHEME)
    key = asset_key("c" * 64)
    cursor = ed.textCursor()
    cursor.insertImage(_image_fmt(key))
    doc = ed.document()
    doc.setModified(False)
    image = QImage(5, 5, QImage.Format_RGB32)
    image.fill(QColor("green"))

    doc.addResource(QTextDocument.ImageResource, QUrl(key), image)
    doc.markContentsDirty(0, doc.characterCount())
    qt_app.processEvents()

    assert doc.isModified() is False


# --------------------------------------------------------------------------- #
# The editor reports the gesture only when somebody is listening
# --------------------------------------------------------------------------- #

def _ctrl_v(editor):
    editor.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_V, Qt.ControlModifier))


def test_ctrl_v_reports_a_clipboard_image_to_its_listener(qt_app):
    QApplication.clipboard().setImage(_clipboard_image())
    ed = HtmlEditor()
    seen = []
    pasted = []
    ed.paste_normalized = lambda: pasted.append(True)
    ed.image_pasted.connect(seen.append)

    _ctrl_v(ed)

    assert [img.width() for img in seen] == [4]
    assert pasted == [], "the paste was consumed, not handled twice"


def test_ctrl_v_pastes_normally_with_nobody_listening(qt_app):
    # The widget has to stay usable on its own: with nobody handling
    # pictures, a paste still brings in what text the clipboard holds.
    mime = QMimeData()
    mime.setImageData(_clipboard_image())
    mime.setText("caption")
    QApplication.clipboard().setMimeData(mime)
    ed = HtmlEditor()
    seen = []
    ed.image_pasted.connect(seen.append)
    ed.image_pasted.disconnect()

    _ctrl_v(ed)

    assert seen == []
    assert ed.toPlainText() == "caption"


def test_a_picture_copied_with_its_text_pastes_the_text(qt_app):
    # A web page selection carries a picture of itself next to its text:
    # the text is what was meant.
    mime = QMimeData()
    mime.setImageData(_clipboard_image())
    mime.setText("words")
    QApplication.clipboard().setMimeData(mime)
    ed = HtmlEditor()
    seen = []
    ed.image_pasted.connect(seen.append)

    _ctrl_v(ed)

    assert seen == [] and ed.toPlainText() == "words"


def _drop_urls(editor, *paths):
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(p) for p in paths])
    event = QDropEvent(QPointF(1, 1), Qt.CopyAction, mime,
                       Qt.LeftButton, Qt.NoModifier)
    editor.dropEvent(event)
    return event


def test_a_drop_reports_its_urls_to_its_listener(qt_app):
    ed = HtmlEditor()
    seen = []
    ed.urls_dropped.connect(seen.append)

    event = _drop_urls(ed, "/tmp/a.png")

    assert [u.toLocalFile() for batch in seen for u in batch] == ["/tmp/a.png"]
    assert event.isAccepted()


def test_a_drop_falls_back_to_text_with_nobody_listening(qt_app):
    ed = HtmlEditor()
    seen = []
    ed.urls_dropped.connect(seen.append)
    ed.urls_dropped.disconnect()

    _drop_urls(ed, "/tmp/a.png")

    assert seen == []
    assert ed.toPlainText() == "file:///tmp/a.png"


# --------------------------------------------------------------------------- #
# Paste and drop: the insert comes first, and stays on this computer
# --------------------------------------------------------------------------- #

class _Status:
    def __init__(self):
        self.messages = []

    def showMessage(self, text, timeout=0):
        self.messages.append(text)


class _Cannot:
    """What the window said it can't open (_say_cannot_open)."""

    def __init__(self):
        self.unsupported = []

    def __call__(self, names):
        self.unsupported.append(", ".join(names))


class _EntryWindow:
    """The slice of MainWindow that owns media entry."""

    _handle_pasted_image = MainWindow._handle_pasted_image
    _handle_dropped_images = MainWindow._handle_dropped_images
    _handle_dropped_urls = MainWindow._handle_dropped_urls
    _library_file_at = MainWindow._library_file_at
    _upload_added = MainWindow._upload_added
    _upload_choice = MainWindow._upload_choice
    _insert_asset = MainWindow._insert_asset
    _asset_image = MainWindow._asset_image

    def __init__(self, cache_dir, *, profile=None):
        cache_dir = Path(cache_dir)
        self._store = FakeBlobStore(cache_dir)
        self._uploader = FakeUploader()
        self._asset_manager = AssetManager(
            blob_store=self._store,
            uploader=self._uploader,
            profile_provider=lambda: profile,
            decoder=image_safety.decode_image_bytes,
            index=AssetIndex(path=cache_dir.parent / "media_assets.json"),
        )
        self._profile_store = types.SimpleNamespace(default=lambda: profile)
        self.nostr_state = types.SimpleNamespace(active=profile is not None)
        self.status = _Status()
        self.bar = _Cannot()
        self._say_cannot_open = self.bar
        self.editor = HtmlEditor()
        self.opened = []
        self.tabs = types.SimpleNamespace(currentWidget=lambda: None)

    def current_editor(self):
        return self.editor

    def _keeps_pictures(self, ed):
        return True                  # a document that keeps pictures (Markdown)

    def new_tab(self):
        return None

    def open_path(self, path):
        self.opened.append(path)


    # -- assertions ---------------------------------------------------------

    def image_names(self):
        return list(iter_image_names(self.editor.document()))

    def assets(self):
        return [self._asset_manager.get(sha)
                for sha in (n.split(":", 1)[1] for n in self.image_names())]


@pytest.fixture
def settings(monkeypatch):
    """Set what the settings file holds."""
    def hold(**values):
        monkeypatch.setattr(main_window_module, "load_settings", lambda: dict(values))
    return hold


@pytest.fixture
def no_alerts(monkeypatch):
    box, shown = fake_alert()
    monkeypatch.setattr(alerts, "Alert", box)
    yield
    assert shown == [], "adding a file asks nothing"


def _png_file(tmp_path, name="shot.png", data=PNG_BYTES):
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


def _speaks_of_nostr(messages) -> bool:
    words = ("signer", "upload", "nostr", "blossom", "server")
    return any(word in message.lower() for message in messages for word in words)


def test_a_paste_lands_in_the_document_with_no_signer(tmp_path):
    # A pasted screenshot exists nowhere else. Nothing about signers,
    # servers or networks may be a reason for it to vanish.
    win = _EntryWindow(tmp_path / "cache", profile=None)

    win._handle_pasted_image(win.editor, _clipboard_image())

    assert len(win.image_names()) == 1
    assert win.image_names()[0].startswith(f"{ASSET_SCHEME}:")
    assert win._uploader.calls == []
    # Without an account the editor says nothing of Nostr: a local image
    # is just an image (uploading is asked when publishing).
    assert not _speaks_of_nostr(win.status.messages)


def test_a_paste_that_cannot_encode_falls_back_to_a_normal_paste(tmp_path):
    win = _EntryWindow(tmp_path / "cache")
    pasted = []
    win.editor.paste_normalized = lambda: pasted.append(True)

    win._handle_pasted_image(win.editor, QImage())

    assert pasted == [True]
    assert win.image_names() == []


def test_a_paste_stays_on_this_computer_and_asks_nothing(tmp_path, settings, no_alerts):
    # Pasting is a quick, low-intent gesture: nothing leaves the machine
    # for it; publishing asks, once, for everything the document holds.
    settings()
    win = _EntryWindow(tmp_path / "cache", profile=object())

    win._handle_pasted_image(win.editor, _clipboard_image())

    assert len(win.image_names()) == 1
    assert win._uploader.calls == []
    assert win.assets()[0].upload_state is AssetState.LOCAL


def test_upload_right_away_sends_a_pasted_image_at_once(tmp_path, settings, no_alerts):
    settings(upload_added_media="now")
    win = _EntryWindow(tmp_path / "cache", profile=object())

    win._handle_pasted_image(win.editor, _clipboard_image())

    assert len(win._uploader.calls) == 1
    assert win.assets()[0].upload_state is AssetState.SIGNING


@pytest.mark.parametrize("earlier, choice", [
    ("always", "now"), ("never", "publishing"), ("ask", "publishing"), (None, "publishing")])
def test_the_earlier_answer_about_pasted_images_is_kept(tmp_path, settings, earlier, choice):
    # Who had pasted images uploaded always keeps uploading at once.
    settings(**({"upload_pasted_images": earlier} if earlier else {}))

    assert _EntryWindow(tmp_path / "cache")._upload_choice() == choice


def test_upload_right_away_does_nothing_of_nostr_without_an_account(tmp_path, settings):
    settings(upload_added_media="now")
    win = _EntryWindow(tmp_path / "cache", profile=None)

    win._handle_pasted_image(win.editor, _clipboard_image())

    assert win._uploader.calls == []


def test_a_failed_upload_leaves_the_image_in_the_document(tmp_path, settings):
    settings(upload_added_media="now")
    win = _EntryWindow(tmp_path / "cache", profile=object())
    win._uploader.fail_synchronously = "UPLOAD_FAILED"

    win._handle_pasted_image(win.editor, _clipboard_image())

    asset = win.assets()[0]
    assert asset.upload_state is AssetState.FAILED
    assert len(win.image_names()) == 1
    assert win._asset_manager.resolve_bytes(asset.key), "the bytes are still cached"


def test_a_drop_inserts_every_image_with_no_signer(tmp_path):
    win = _EntryWindow(tmp_path / "cache", profile=None)
    first = _png_file(tmp_path, "one.png")
    second = _png_file(tmp_path, "two.png", data=PNG_BYTES[:-1] + b"\x00")

    win._handle_dropped_images([first, second])

    assert len(win.image_names()) == 2
    assert win._uploader.calls == []
    assert not _speaks_of_nostr(win.status.messages)    # none without an account


def test_a_dropped_image_keeps_the_filename_as_its_alt_text(tmp_path):
    win = _EntryWindow(tmp_path / "cache", profile=None)

    win._handle_dropped_images([_png_file(tmp_path, "holiday.png")])

    assert win.assets()[0].alt == "holiday"


def test_a_drop_stays_on_this_computer_and_asks_nothing(tmp_path, settings, no_alerts):
    settings()
    win = _EntryWindow(tmp_path / "cache", profile=object())

    win._handle_dropped_images([_png_file(tmp_path)])

    assert win._uploader.calls == []
    assert len(win.image_names()) == 1


def test_upload_right_away_sends_each_dropped_image(tmp_path, settings, no_alerts):
    settings(upload_added_media="now")
    win = _EntryWindow(tmp_path / "cache", profile=object())
    first = _png_file(tmp_path, "one.png")
    second = _png_file(tmp_path, "two.png", data=PNG_BYTES[:-1] + b"\x00")

    win._handle_dropped_images([first, second])

    # Single flight: the second waits for the first to finish.
    assert len(win._uploader.calls) == 1
    assert {a.upload_state for a in win.assets()} == {AssetState.SIGNING,
                                                      AssetState.QUEUED}


def test_a_file_that_is_not_an_image_is_named_and_nothing_is_inserted(tmp_path):
    win = _EntryWindow(tmp_path / "cache", profile=None)
    prose = _png_file(tmp_path, "notes.png", data=TEXT_BYTES)

    win._handle_dropped_images([prose])

    assert win.image_names() == []
    assert any("notes.png" in m for m in win.status.messages)


def test_a_tile_dragged_from_the_media_library_is_inserted(tmp_path):
    """A tile dragged out of the library carries the file's address; the
    document takes it as Insert Image would, the private gate included."""
    win = _EntryWindow(tmp_path / "cache", profile=object())
    sha = "e" * 64
    record = types.SimpleNamespace(hash=sha, url=f"https://cdn.example/{sha}.png")
    win._media_store = types.SimpleNamespace(files={sha: record})
    inserted = []
    win._insert_media_at_cursor = lambda media, editor, alt="": inserted.append((media, editor))

    win._handle_dropped_urls([QUrl(record.url), QUrl("https://elsewhere.example/a.png")])

    assert inserted == [(record, win.editor)]
    assert win.bar.unsupported == []


def test_dropped_files_are_routed_by_extension(tmp_path):
    win = _EntryWindow(tmp_path / "cache", profile=None)
    picture = _png_file(tmp_path, "shot.png")
    vector = str(tmp_path / "logo.svg")
    document = str(tmp_path / "note.md")

    win._handle_dropped_urls([QUrl.fromLocalFile(p)
                              for p in (picture, vector, document)])
    QApplication.processEvents()                 # said after the drop

    assert len(win.image_names()) == 1
    assert win.opened == [document]
    assert win.bar.unsupported == ["logo.svg"], (
        "SVG is decoded nowhere in this process, so it is unsupported"
    )


# --------------------------------------------------------------------------- #
# Bytes arriving after the insert
# --------------------------------------------------------------------------- #

class _RefreshWindow:
    """The slice of MainWindow that repaints a late-arriving asset."""

    _refresh_asset_in_documents = MainWindow._refresh_asset_in_documents
    _editor_from_widget = staticmethod(lambda widget: widget)

    def __init__(self, cache_dir, editors):
        self._store = FakeBlobStore(Path(cache_dir))
        self._asset_manager = AssetManager(
            blob_store=self._store,
            uploader=FakeUploader(),
            profile_provider=lambda: None,
            decoder=image_safety.decode_image_bytes,
            index=AssetIndex(path=Path(cache_dir).parent / "media_assets.json"),
        )
        self.tabs = types.SimpleNamespace(
            count=lambda: len(editors),
            widget=lambda i: editors[i],
        )


def test_late_arriving_bytes_repaint_without_dirtying_the_document(tmp_path):
    # The image was inserted while its blob was still on a server. When
    # the bytes land, the placeholder is replaced in place: marking the
    # document modified here would unsave a saved file and write a
    # crash backup nobody asked for.
    sha = sha_of(PNG_BYTES)
    key = asset_key(sha)
    showing = HtmlEditor()
    showing.textCursor().insertImage(_image_fmt(key))
    elsewhere = HtmlEditor()
    elsewhere.textCursor().insertImage(_image_fmt(asset_key("d" * 64)))
    win = _RefreshWindow(tmp_path / "cache", [showing, elsewhere])
    win._store.put_bytes(PNG_BYTES)
    for ed in (showing, elsewhere):
        ed.document().setModified(False)

    win._refresh_asset_in_documents(sha)

    assert showing.document().resource(QTextDocument.ImageResource, QUrl(key))
    assert showing.document().isModified() is False
    assert elsewhere.document().isModified() is False


def test_a_refresh_for_bytes_that_never_arrived_changes_nothing(tmp_path):
    key = asset_key("e" * 64)
    ed = HtmlEditor()
    ed.textCursor().insertImage(_image_fmt(key))
    win = _RefreshWindow(tmp_path / "cache", [ed])
    ed.document().setModified(False)

    win._refresh_asset_in_documents("e" * 64)

    assert ed.document().isModified() is False


def test_a_drop_goes_where_it_is_dropped_not_at_the_caret(qt_app):
    """As in Pages and TextEdit: the caret moves to the drop point, and
    whoever takes the urls inserts there."""
    ed = HtmlEditor()
    ed.resize(400, 200)
    ed.show()
    ed.setPlainText("first line\nsecond line\nthird line")
    cursor = ed.textCursor()
    cursor.setPosition(0)
    ed.setTextCursor(cursor)
    from PySide6.QtGui import QTextCursor
    third = ed.document().findBlockByNumber(2)
    point = ed.cursorRect(QTextCursor(third)).center()
    at_drop = []
    ed.urls_dropped.connect(lambda urls: at_drop.append(ed.textCursor().position()))
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile("/tmp/a.png")])
    ed.dropEvent(QDropEvent(QPointF(point), Qt.CopyAction, mime,
                            Qt.LeftButton, Qt.NoModifier))
    assert at_drop and ed.document().findBlock(at_drop[0]).blockNumber() == 2


def test_a_library_audio_file_becomes_the_same_card_insert_audio_makes(tmp_path, monkeypatch):
    """Whichever way a file arrives (Insert Audio or Video, or a tile from
    the Media Library), the document holds the same object: a card named
    by the file's asset, already uploaded, nothing sent again."""
    from nostr.media.media_visibility import MediaVisibility
    from nostr.ui import media_card
    from nostr.ui.publish_copy_dialog import PickResolution
    sha = "a1" * 32
    url = f"https://one.example/{sha}.mp3"
    record = types.SimpleNamespace(hash=sha, url=url, mime_type="audio/mpeg", size=4096,
                                   urls=[{"server": "https://one.example", "url": url}],
                                   uploaded_at_ms=0)
    monkeypatch.setattr(main_window_module, "resolve_pick", lambda media, **kw: PickResolution(
        sha256=sha, url=url, mime="audio/mpeg", size=4096, ok=True))
    win = _EntryWindow(tmp_path / "cache", profile=object())
    win._uploader.files[sha] = record
    win._media_store = types.SimpleNamespace(files={sha: record},
                                             name_of=lambda s: "Podcast intro.mp3")
    win._media_visibility = MediaVisibility()
    win._copy_maker = None
    win.is_dark_theme = False
    win._may_insert_audio_video = lambda ed: True
    win._keeps_pictures = lambda ed: True
    win._want_poster = lambda media, mime: None
    win._poster_frames, win._media_lengths = {}, {}
    win._media_file_of = MainWindow._media_file_of
    for name in ("_insert_media_at_cursor", "_insert_media_card", "_media_card",
                 "_library_name", "_insert_url_as_text"):
        setattr(win, name, getattr(MainWindow, name).__get__(win))

    win._insert_media_at_cursor(record, win.editor)

    [asset] = win.assets()
    assert asset.is_media and asset.is_uploaded and asset.alt == "Podcast intro.mp3"
    assert asset.servers == ["https://one.example"]
    fmt = next(f for f in _image_formats(win.editor) if f.name() == asset.key)
    assert (fmt.width(), fmt.height()) == media_card.size_for(asset.mime)
    assert win._uploader.calls == []



def _library_window(tmp_path, monkeypatch, record, name, *, rich=True):
    """An _EntryWindow that inserts library picks the way MainWindow does,
    the pick resolving to ``record`` as the gate passes a public file."""
    from nostr.media.media_visibility import MediaVisibility
    from nostr.ui.publish_copy_dialog import PickResolution
    monkeypatch.setattr(main_window_module, "resolve_pick", lambda media, **kw: PickResolution(
        sha256=media.hash, url=media.url, mime=media.mime_type, size=media.size, ok=True))
    win = _EntryWindow(tmp_path / "cache", profile=object())
    win._uploader.files[record.hash] = record
    win._media_store = types.SimpleNamespace(files={record.hash: record}, name_of=lambda s: name)
    win._media_visibility = MediaVisibility()
    win._copy_maker = None
    win.is_dark_theme = False
    win._may_insert_audio_video = lambda ed: rich
    win._keeps_pictures = lambda ed: rich
    win._editor_kind = lambda ed: "rich" if rich else "plain"
    win._want_poster = lambda media, mime: None
    win._poster_frames, win._media_lengths = {}, {}
    win._media_file_of = MainWindow._media_file_of
    for method in ("_insert_media_at_cursor", "_insert_media_card", "_media_card",
                   "_library_name", "_insert_url_as_text", "_insert_file_link"):
        setattr(win, method, getattr(MainWindow, method).__get__(win))
    return win


def test_a_video_its_server_calls_a_byte_stream_goes_in_as_a_video(tmp_path, monkeypatch):
    """A server that cannot say what a file is (application/octet-stream)
    put a video in as its bare address; its name says what it is."""
    sha = "b2" * 32
    url = f"https://one.example/{sha}"
    record = types.SimpleNamespace(hash=sha, url=url, mime_type="application/octet-stream",
                                   size=4096, urls=[{"server": "https://one.example", "url": url}],
                                   uploaded_at_ms=0)
    win = _library_window(tmp_path, monkeypatch, record, "Talk.mp4")

    win._insert_media_at_cursor(record, win.editor)

    [asset] = win.assets()
    assert asset.mime == "video/mp4" and asset.is_media
    assert any(f.name() == asset.key for f in _image_formats(win.editor))


def test_a_file_from_the_library_goes_in_as_a_link_by_its_name(tmp_path, monkeypatch):
    sha = "c3" * 32
    url = f"https://one.example/{sha}.pdf"
    record = types.SimpleNamespace(hash=sha, url=url, mime_type="application/pdf", size=4096,
                                   urls=[{"server": "https://one.example", "url": url}],
                                   uploaded_at_ms=0)
    win = _library_window(tmp_path, monkeypatch, record, "Notes.pdf")

    win._insert_media_at_cursor(record, win.editor)

    block = win.editor.document().begin()
    fragment = block.begin().fragment()
    assert block.text() == "Notes.pdf"
    assert fragment.charFormat().isAnchor() and fragment.charFormat().anchorHref() == url
    assert win.assets() == []


def test_a_library_file_goes_into_plain_text_as_its_address(tmp_path, monkeypatch):
    """A plain-text file saves its text only: a picture would be lost."""
    sha = "d4" * 32
    url = f"https://one.example/{sha}.png"
    record = types.SimpleNamespace(hash=sha, url=url, mime_type="image/png", size=4096,
                                   urls=[{"server": "https://one.example", "url": url}],
                                   uploaded_at_ms=0)
    win = _library_window(tmp_path, monkeypatch, record, "Sunset.png", rich=False)

    win._insert_media_at_cursor(record, win.editor)

    assert win.editor.toPlainText() == url and _image_formats(win.editor) == []


def _image_formats(editor):
    """Every image format in the editor's text."""
    from PySide6.QtGui import QTextCharFormat  # noqa: F401
    found = []
    block = editor.document().begin()
    while block.isValid():
        it = block.begin()
        while not it.atEnd():
            fmt = it.fragment().charFormat()
            if fmt.isImageFormat():
                found.append(fmt.toImageFormat())
            it += 1
        block = block.next()
    return found


def test_a_library_file_with_no_name_is_called_by_its_day():
    from types import SimpleNamespace
    from PySide6.QtCore import QDateTime
    import i18n
    host = SimpleNamespace(_media_store=SimpleNamespace(name_of=lambda s: ""),
                           _media_visibility=SimpleNamespace(name_of=lambda s: ""))
    media = SimpleNamespace(hash="c" * 64, uploaded_at_ms=1_780_000_000_000)
    expected = i18n.format_day(QDateTime.fromMSecsSinceEpoch(1_780_000_000_000),
                               short_month=True)
    assert MainWindow._library_name(host, media) == expected
    assert MainWindow._library_name(host, SimpleNamespace(hash="d" * 64, uploaded_at_ms=0)) == ""


def test_an_uploading_card_is_drawn_again_where_it_stands_only():
    """Its bar moves many times a second: the card's picture is drawn
    again and only its place laid out again, never the whole text, and
    the document stays unmodified."""
    from types import SimpleNamespace
    sha = "e" * 64
    key = asset_key(sha)
    editor = HtmlEditor()
    cursor = editor.textCursor()
    cursor.insertText("Before\n")
    cursor.insertImage(_image_fmt(key))
    cursor.insertText("\nAfter")
    doc = editor.document()
    doc.setModified(False)
    dirty = []
    doc.markContentsDirty = lambda position, length: dirty.append((position, length))
    card = QImage(4, 4, QImage.Format.Format_ARGB32)
    host = SimpleNamespace(_all_editors=lambda: [editor], _media_card=lambda asset: card)
    MainWindow._redraw_card(host, SimpleNamespace(sha256=sha))
    assert dirty == [(len("Before\n"), 1)]
    assert doc.resource(QTextDocument.ImageResource, QUrl(key)) == card
    assert not doc.isModified()
