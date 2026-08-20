# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins how an image enters a document and what leaves the machine after.

The defect classes this file guards against:
- a pasted screenshot, which exists nowhere else, being dropped because
  no signer is connected, an upload failed or the network is down,
- Ctrl+V publishing to a public server with no confirmation, or the
  remembered preference being ignored in either direction,
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

import image_safety
import main_window as main_window_module
from doc_walk import iter_image_names
from editor import HtmlEditor
from main_window import MainWindow
from nostr.media.assets import ASSET_SCHEME, AssetIndex, AssetState, asset_key
from nostr.media.manager import AssetManager

from tests.media_fakes import (
    PNG_BYTES, TEXT_BYTES, FakeBlobStore, FakeUploader, fake_message_box, sha_of,
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
    # The widget has to stay usable on its own, so an unconsumed image
    # paste falls through to the plain-text path rather than vanishing.
    QApplication.clipboard().setImage(_clipboard_image())
    ed = HtmlEditor()
    seen = []
    pasted = []
    ed.paste_normalized = lambda: pasted.append(True)
    ed.image_pasted.connect(seen.append)
    ed.image_pasted.disconnect()

    _ctrl_v(ed)

    assert seen == []
    assert pasted == [True]


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
# Paste and drop: the insert comes first, consent comes after
# --------------------------------------------------------------------------- #

class _Status:
    def __init__(self):
        self.messages = []

    def showMessage(self, text, timeout=0):
        self.messages.append(text)


class _Bar:
    def __init__(self):
        self.unsupported = []

    def show_unsupported(self, names):
        self.unsupported.append(names)


class _EntryWindow:
    """The slice of MainWindow that owns media entry."""

    _handle_pasted_image = MainWindow._handle_pasted_image
    _handle_dropped_images = MainWindow._handle_dropped_images
    _handle_dropped_urls = MainWindow._handle_dropped_urls
    _confirm_paste_upload = MainWindow._confirm_paste_upload
    _insert_asset = MainWindow._insert_asset

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
        self.status = _Status()
        self.bar = _Bar()
        self.editor = HtmlEditor()
        self.opened = []
        self.tabs = types.SimpleNamespace(currentWidget=lambda: None)

    def current_editor(self):
        return self.editor

    def new_tab(self):
        return None

    def open_path(self, path):
        self.opened.append(path)

    def _bar_from_widget(self, widget):
        return self.bar

    # -- assertions ---------------------------------------------------------

    def image_names(self):
        return list(iter_image_names(self.editor.document()))

    def assets(self):
        return [self._asset_manager.get(sha)
                for sha in (n.split(":", 1)[1] for n in self.image_names())]


@pytest.fixture
def paste_choice(monkeypatch):
    """Set the persisted answer to the paste-upload prompt."""
    def choose(value):
        monkeypatch.setattr(main_window_module, "load_settings",
                            lambda: {"upload_pasted_images": value})
    return choose


def _png_file(tmp_path, name="shot.png", data=PNG_BYTES):
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


def test_a_paste_lands_in_the_document_with_no_signer(tmp_path):
    # A pasted screenshot exists nowhere else. Nothing about signers,
    # servers or networks may be a reason for it to vanish.
    win = _EntryWindow(tmp_path / "cache", profile=None)

    win._handle_pasted_image(win.editor, _clipboard_image())

    assert len(win.image_names()) == 1
    assert win.image_names()[0].startswith(f"{ASSET_SCHEME}:")
    assert win._uploader.calls == []
    assert any("Connect a signer" in m for m in win.status.messages)


def test_a_paste_that_cannot_encode_falls_back_to_a_normal_paste(tmp_path):
    win = _EntryWindow(tmp_path / "cache")
    pasted = []
    win.editor.paste_normalized = lambda: pasted.append(True)

    win._handle_pasted_image(win.editor, QImage())

    assert pasted == [True]
    assert win.image_names() == []


def test_never_keeps_a_pasted_image_off_every_server(tmp_path, paste_choice):
    paste_choice("never")
    win = _EntryWindow(tmp_path / "cache", profile=object())

    win._handle_pasted_image(win.editor, _clipboard_image())

    assert len(win.image_names()) == 1
    assert win._uploader.calls == []


def test_always_uploads_a_pasted_image_without_asking(tmp_path, paste_choice,
                                                      monkeypatch):
    paste_choice("always")
    box, shown = fake_message_box()
    monkeypatch.setattr(main_window_module, "QMessageBox", box)
    win = _EntryWindow(tmp_path / "cache", profile=object())

    win._handle_pasted_image(win.editor, _clipboard_image())

    assert shown == [], "the remembered answer replaces the prompt"
    assert len(win._uploader.calls) == 1
    assert win.assets()[0].upload_state is AssetState.SIGNING


def test_ask_consults_the_prompt_and_keeps_local_by_default(tmp_path,
                                                            paste_choice,
                                                            monkeypatch):
    # A paste is a high-frequency, low-intent gesture, so the outcome of
    # an accidental one has to be that nothing left the machine.
    paste_choice("ask")
    box, shown = fake_message_box()
    monkeypatch.setattr(main_window_module, "QMessageBox", box)
    monkeypatch.setattr(main_window_module, "save_setting",
                        lambda *a, **k: pytest.fail("nothing to remember"))
    monkeypatch.setattr(main_window_module, "BlossomSettings",
                        lambda: types.SimpleNamespace(
                            configured_servers=lambda: ["https://cdn.example"]))
    win = _EntryWindow(tmp_path / "cache", profile=object())

    win._handle_pasted_image(win.editor, _clipboard_image())

    assert [d.title for d in shown] == ["Upload pasted image"]
    assert shown[0].default.label == "Keep local"
    assert win._uploader.calls == []
    assert len(win.image_names()) == 1


def test_ask_uploads_when_the_prompt_is_answered_with_upload(tmp_path,
                                                             paste_choice,
                                                             monkeypatch):
    paste_choice("ask")
    box, shown = fake_message_box(click="Upload")
    remembered = []
    monkeypatch.setattr(main_window_module, "QMessageBox", box)
    monkeypatch.setattr(main_window_module, "save_setting",
                        lambda key, value: remembered.append((key, value)))
    monkeypatch.setattr(main_window_module, "BlossomSettings",
                        lambda: types.SimpleNamespace(
                            configured_servers=lambda: ["https://cdn.example"]))
    win = _EntryWindow(tmp_path / "cache", profile=object())

    win._handle_pasted_image(win.editor, _clipboard_image())

    assert len(win._uploader.calls) == 1
    assert remembered == [], "the box was never ticked"
    assert shown[0].checkbox.text() == "Remember this choice"


def test_a_failed_upload_leaves_the_image_in_the_document(tmp_path,
                                                          paste_choice):
    paste_choice("always")
    win = _EntryWindow(tmp_path / "cache", profile=object())
    win._uploader.fail_synchronously = "server said no"

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
    assert any("Connect a signer" in m for m in win.status.messages)


def test_a_dropped_image_keeps_the_filename_as_its_alt_text(tmp_path):
    win = _EntryWindow(tmp_path / "cache", profile=None)

    win._handle_dropped_images([_png_file(tmp_path, "holiday.png")])

    assert win.assets()[0].alt == "holiday"


def test_a_drop_uploads_only_when_the_prompt_is_accepted(tmp_path, monkeypatch):
    box, shown = fake_message_box(click="Keep local")
    monkeypatch.setattr(main_window_module, "QMessageBox", box)
    win = _EntryWindow(tmp_path / "cache", profile=object())

    win._handle_dropped_images([_png_file(tmp_path)])

    assert [d.title for d in shown] == ["Upload images"]
    assert shown[0].default.label == "Upload", "dropping is a deliberate act"
    assert win._uploader.calls == []
    assert len(win.image_names()) == 1


def test_a_drop_uploads_each_image_when_accepted(tmp_path, monkeypatch):
    box, _shown = fake_message_box(click="Upload")
    monkeypatch.setattr(main_window_module, "QMessageBox", box)
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


def test_dropped_files_are_routed_by_extension(tmp_path):
    win = _EntryWindow(tmp_path / "cache", profile=None)
    picture = _png_file(tmp_path, "shot.png")
    vector = str(tmp_path / "logo.svg")
    document = str(tmp_path / "note.md")

    win._handle_dropped_urls([QUrl.fromLocalFile(p)
                              for p in (picture, vector, document)])

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
