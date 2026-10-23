"""Pins how a document's images survive save, reopen, publish and export.

The defect classes this file guards against:
- the asset key, an app-internal handle, reaching a file the user can
  open outside the app or an event signed to a relay,
- a foreign document's own image layout ("pics/dog.png" from another
  editor) being copied and rewritten by a text-only save,
- a .md saved offline reopening with a broken image because relative
  names were resolved against the process working directory,
- an image serializing as U+FFFC because a text-only path went back to
  toPlainText() or toMarkdown() lost the real image format,
- publishing a document whose images no reader can fetch,
- the .Rmd image copier reading any local path a document names, which
  a knit with self_contained then publishes.

The asset manager here is the real one over the blob-store and uploader
fakes, holding one uploaded asset and one local-only asset. A stub that
answers None for everything cannot reach the uploaded-to-remote_url or
the local-to-sidecar branch at all, which is how those branches went
unguarded.
"""

import hashlib
import os
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QUrl
from PySide6.QtGui import (
    QColor, QImage, QTextDocument, QTextImageFormat,
)
from PySide6.QtWidgets import QApplication

import alerts
import image_safety
import main_window as main_window_module
from doc_walk import (
    iter_block_runs, iter_blocks, iter_image_names, serialize_plain_with_images,
)
from editor import HtmlEditor
from export_html import document_to_html  # noqa: E402
from markdown_writer import document_to_markdown
from main_window import MainWindow
from nostr.media.assets import ASSET_SCHEME, AssetIndex, AssetState
from nostr.media.manager import AssetManager

from tests.media_fakes import (
    GIF_BYTES, PNG_BYTES, FakeBlobStore, FakeUploader, fake_alert,
)


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def _png(path, color="red", size=(4, 4)):
    img = QImage(*size, QImage.Format_RGB32)
    img.fill(QColor(color))
    assert img.save(str(path), "PNG")
    return str(path)


class _Window:
    """MainWindow's save-side media methods over a minimal stub."""

    _blob_cache_root = MainWindow._blob_cache_root
    _image_roots_for = MainWindow._image_roots_for
    _library_roots = MainWindow._library_roots
    _image_target_for_file_save = MainWindow._image_target_for_file_save
    _foreign_image_target = MainWindow._foreign_image_target
    _encode_document_image = MainWindow._encode_document_image
    _make_rmd_image_copier = MainWindow._make_rmd_image_copier
    _markdown_reference_for = MainWindow._markdown_reference_for
    _publish_payload = MainWindow._publish_payload
    _publish_text = MainWindow._publish_text
    _asset_named = MainWindow._asset_named
    _local_image_bytes = staticmethod(MainWindow._local_image_bytes)
    _local_objects = MainWindow._local_objects
    _uploads_needed = MainWindow._uploads_needed
    _after_uploads = MainWindow._after_uploads
    _agree_to_upload = MainWindow._agree_to_upload
    _file_label = staticmethod(MainWindow._file_label)
    _wait_for_uploads = MainWindow._wait_for_uploads
    _media_not_uploaded = MainWindow._media_not_uploaded
    _has_images = MainWindow._has_images
    _loses_content_on_save = MainWindow._loses_content_on_save
    _has_formatting = MainWindow._has_formatting
    _resolve_local_image = MainWindow._resolve_local_image

    def __init__(self, cache_dir, *, profile=None):
        cache_dir = Path(cache_dir)
        self._store = FakeBlobStore(cache_dir)
        # The blob cache is one object behind two names in production:
        # the loader the window reads its image root from, and the byte
        # store the asset manager resolves through.
        self._media_image_loader = self._store
        self._uploader = FakeUploader()
        self._asset_manager = AssetManager(
            blob_store=self._store,
            uploader=self._uploader,
            profile_provider=lambda: profile,
            decoder=image_safety.decode_image_bytes,
            index=AssetIndex(path=cache_dir.parent / "media_assets.json"),
        )
        self._upload_waits = {}
        self.status = types.SimpleNamespace(showMessage=lambda *a, **context: None, clearMessage=lambda: None)

    @staticmethod
    def _editor_widget_index(ed):
        return 0                     # still open

    def finish_upload(self):
        """The media server takes the file being sent."""
        call = self._uploader.calls[-1]
        sha = hashlib.sha256(call.body).hexdigest()
        self._uploader.finish(call.name, types.SimpleNamespace(
            url=f"https://cdn.example/{sha}.png", size=len(call.body), servers=()))
        QApplication.processEvents()         # what waits is told from the event loop

    def fail_upload(self, code="UPLOAD_FAILED"):
        self._uploader.fail(self._uploader.calls[-1].name, code)
        QApplication.processEvents()

    def uploaded_asset(self, data=PNG_BYTES, *, alt="a photo"):
        """An asset whose bytes are cached and whose blob is hosted."""
        adopted = self._asset_manager.adopt_bytes(data, alt=alt)
        assert adopted is not None
        asset = self._asset_manager.adopt_library_file(
            sha256=adopted.sha256,
            remote_url=f"https://cdn.example/{adopted.sha256}",
            mime=adopted.mime,
            alt=alt,
        )
        assert asset is not None and asset.is_uploaded
        return asset

    def local_asset(self, data=GIF_BYTES, *, alt="a sketch"):
        """An asset that exists only in this machine's cache."""
        asset = self._asset_manager.adopt_bytes(data, alt=alt)
        assert asset is not None and not asset.is_uploaded
        return asset


def _fmt(name, alt=""):
    fmt = QTextImageFormat()
    fmt.setName(name)
    if alt:
        fmt.setProperty(QTextImageFormat.ImageAltText, alt)
    return fmt


def _editor_with(*assets, text=""):
    """An editor holding a real image fragment per asset."""
    ed = HtmlEditor()
    cursor = ed.textCursor()
    if text:
        cursor.insertText(text)
    for asset in assets:
        cursor.insertImage(_fmt(asset.key, asset.alt or "image"))
    ed.setTextCursor(cursor)
    return ed


def _image_formats(doc):
    """Every image fragment's QTextImageFormat, in document order."""
    return [fmt.toImageFormat()
            for block in iter_blocks(doc)
            for _text, fmt in iter_block_runs(block)
            if fmt.isImageFormat()]


def _alt_of(fmt) -> str:
    return str(fmt.property(QTextImageFormat.ImageAltText) or "")


# --------------------------------------------------------------------------- #
# A foreign document keeps its own layout
# --------------------------------------------------------------------------- #

def test_foreign_relative_reference_survives_a_save(tmp_path):
    # Another editor wrote "pics/dog.png". Pressing Ctrl+S after a
    # text-only edit must not duplicate the file or rewrite the link.
    doc_dir = tmp_path / "doc"
    (doc_dir / "pics").mkdir(parents=True)
    _png(doc_dir / "pics" / "dog.png")
    win = _Window(tmp_path / "cache")

    target = win._image_target_for_file_save(QTextDocument(),
                                             str(doc_dir / "story.md"))

    assert target(_fmt("pics/dog.png")) == "pics/dog.png"
    assert not (doc_dir / "story_media").exists()


def test_foreign_urls_survive_a_save(tmp_path):
    win = _Window(tmp_path / "cache")
    target = win._image_target_for_file_save(QTextDocument(),
                                             str(tmp_path / "story.md"))

    for name in ("https://other.example/cat.png",
                 "http://plain.example/legacy.gif",
                 "data:image/png;base64,AAAA"):
        assert target(_fmt(name)) == name


def test_absolute_foreign_path_is_left_verbatim(tmp_path):
    # A path outside the cache belongs to the author's arrangement; the
    # exporters may read it, the save path must not rewrite it.
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    picture = _png(outside / "holiday.png")
    win = _Window(tmp_path / "cache")
    target = win._image_target_for_file_save(QTextDocument(),
                                             str(tmp_path / "story.md"))

    assert target(_fmt(picture)) == picture
    assert not (tmp_path / "story_media").exists()


def test_legacy_cache_path_still_moves_into_the_sidecar(tmp_path):
    # The one name that cannot travel with the document: older builds
    # wrote the machine-local cache path into the file.
    cache = tmp_path / "cache"
    win = _Window(cache)
    cached = _png(cache / ("c" * 64))
    target = win._image_target_for_file_save(QTextDocument(),
                                             str(tmp_path / "story.md"))

    destination = target(_fmt(cached))

    assert destination.startswith("story_media/")
    assert destination.endswith(".png")
    assert (tmp_path / "story_media" / os.path.basename(destination)).exists()


# --------------------------------------------------------------------------- #
# A reopened .md shows its pictures wherever the app was launched from
# --------------------------------------------------------------------------- #

def _md_editor(win, doc_dir, name):
    md = doc_dir / "note.md"
    md.write_text(f"![pic]({name})\n", encoding="utf-8")
    ed = HtmlEditor()
    ed._file_path = str(md)
    ed.set_local_image_resolver(lambda n, e=ed: win._resolve_local_image(e, n))
    ed.document().setMarkdown(md.read_text(encoding="utf-8"))
    return ed


def test_reopened_sidecar_image_renders_from_any_working_directory(tmp_path,
                                                                   monkeypatch):
    doc_dir = tmp_path / "doc"
    (doc_dir / "note_media").mkdir(parents=True)
    _png(doc_dir / "note_media" / "a.png", size=(6, 3))
    win = _Window(tmp_path / "cache")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    ed = _md_editor(win, doc_dir, "note_media/a.png")

    image = ed.document().resource(QTextDocument.ImageResource,
                                   QUrl("note_media/a.png"))
    assert isinstance(image, QImage)
    assert (image.width(), image.height()) == (6, 3)


def test_relative_name_outside_the_document_folder_reads_nothing(tmp_path,
                                                                 monkeypatch):
    # "../secret.png" is not beside the document, so it resolves to the
    # placeholder rather than to a file the reader never named.
    doc_dir = tmp_path / "doc"
    doc_dir.mkdir()
    secret = _png(tmp_path / "secret.png", color="red", size=(6, 3))
    win = _Window(tmp_path / "cache")
    monkeypatch.chdir(tmp_path)

    ed = _md_editor(win, doc_dir, "../secret.png")

    image = ed.document().resource(QTextDocument.ImageResource,
                                   QUrl("../secret.png"))
    assert isinstance(image, QImage)
    assert (image.width(), image.height()) != (6, 3)
    assert os.path.exists(secret)


def test_local_resolver_refuses_a_format_outside_the_allowlist(tmp_path):
    # An SVG beside the document is never handed to the Qt renderer,
    # which resolves xlink:href against the local filesystem.
    doc_dir = tmp_path / "doc"
    doc_dir.mkdir()
    (doc_dir / "vector.svg").write_bytes(
        b'<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8"></svg>')
    win = _Window(tmp_path / "cache")
    ed = HtmlEditor()
    ed._file_path = str(doc_dir / "note.md")

    assert win._resolve_local_image(ed, "vector.svg") is None


def test_local_resolver_answers_nothing_for_an_unsaved_document(tmp_path):
    win = _Window(tmp_path / "cache")
    ed = HtmlEditor()
    ed._file_path = None

    assert win._resolve_local_image(ed, "note_media/a.png") is None


# --------------------------------------------------------------------------- #
# The .Rmd image copier reads only what the exporters may read
# --------------------------------------------------------------------------- #

def test_rmd_copier_refuses_a_path_outside_the_roots(tmp_path):
    # Knitting emits self_contained HTML, so anything this copies into
    # the sidecar is published. A document may name any path.
    outside = tmp_path / "private"
    outside.mkdir()
    secret = _png(outside / "photo.png")
    export_dir = tmp_path / "shared"
    export_dir.mkdir()
    win = _Window(tmp_path / "cache")

    copy = win._make_rmd_image_copier(str(export_dir / "note.Rmd"))

    assert copy(secret) is None
    assert copy(f"file://{secret}") is None
    assert copy(str(outside / ".." / "private" / "photo.png")) is None
    assert not (export_dir / "note_media").exists()


def test_rmd_copier_still_copies_an_image_beside_the_document(tmp_path):
    export_dir = tmp_path / "shared"
    export_dir.mkdir()
    picture = _png(export_dir / "chart.png")
    win = _Window(tmp_path / "cache")

    copy = win._make_rmd_image_copier(str(export_dir / "note.Rmd"))
    reference = copy(picture)

    assert reference.startswith("note_media/")
    assert (export_dir / "note_media" / os.path.basename(reference)).exists()


def test_rmd_copier_passes_foreign_urls_through(tmp_path):
    export_dir = tmp_path / "shared"
    export_dir.mkdir()
    win = _Window(tmp_path / "cache")

    copy = win._make_rmd_image_copier(str(export_dir / "note.Rmd"))

    assert copy("https://other.example/cat.png") == "https://other.example/cat.png"
    assert not (export_dir / "note_media").exists()


# --------------------------------------------------------------------------- #
# The asset key never reaches a file the user can open
# --------------------------------------------------------------------------- #

def test_uploaded_asset_saves_as_its_remote_url(tmp_path):
    win = _Window(tmp_path / "cache")
    asset = win.uploaded_asset()
    target = win._image_target_for_file_save(QTextDocument(),
                                             str(tmp_path / "story.md"))

    assert target(_fmt(asset.key)) == asset.remote_url
    assert not (tmp_path / "story_media").exists(), "a hosted blob needs no copy"


def test_local_only_asset_saves_into_a_sidecar_that_exists(tmp_path):
    # Nothing has been uploaded, so the .md has to carry its own copy.
    win = _Window(tmp_path / "cache")
    asset = win.local_asset()
    target = win._image_target_for_file_save(QTextDocument(),
                                             str(tmp_path / "story.md"))

    destination = target(_fmt(asset.key))

    assert destination == f"story_media/{asset.sha256}.gif"
    sidecar = tmp_path / "story_media" / f"{asset.sha256}.gif"
    assert sidecar.read_bytes() == GIF_BYTES


def test_no_serializer_ever_writes_the_asset_key(tmp_path):
    # The key is an app-internal handle. It resolves for nobody outside
    # this process, so a document carrying one is a document with a
    # broken picture for every reader including the author on a second
    # machine.
    win = _Window(tmp_path / "cache")
    hosted = win.uploaded_asset()
    local = win.local_asset()
    ed = _editor_with(hosted, local, text="Look:")
    save_path = str(tmp_path / "story.md")
    target = win._image_target_for_file_save(ed.document(), save_path)
    copy_image = win._make_rmd_image_copier(str(tmp_path / "story.Rmd"))

    outputs = {
        "html": document_to_html(
            ed.document(), title="Story",
            image_roots=win._image_roots_for(save_path),
            asset_resolver=win._asset_manager.export_view,
        ),
        "markdown tab": document_to_markdown(ed.document(),
                                             win._markdown_reference_for(target)),
        "plain tab": serialize_plain_with_images(
            ed.document(), win._markdown_reference_for(target)
        ),
        "publish markdown": win._publish_text(ed, "markdown"),
        "publish note": win._publish_text(ed, "note"),
        "rmd": " ".join(str(copy_image(a.key)) for a in (hosted, local)),
    }

    for label, output in outputs.items():
        assert ASSET_SCHEME not in output, f"{label} leaked the asset key"

    for label in ("markdown tab", "plain tab", "publish markdown", "publish note"):
        assert hosted.remote_url in outputs[label], f"{label} lost the remote URL"

    sidecar = tmp_path / "story_media" / f"{local.sha256}.gif"
    assert sidecar.read_bytes() == GIF_BYTES


def test_markdown_save_keeps_images_out_of_the_replacement_character(tmp_path):
    # toMarkdown() on a document holding a real image format emits a
    # reference; toPlainText() emits U+FFFC and the picture is gone.
    win = _Window(tmp_path / "cache")
    asset = win.uploaded_asset(alt="a kitten")
    ed = _editor_with(asset)
    target = win._image_target_for_file_save(ed.document(),
                                             str(tmp_path / "story.md"))

    content = document_to_markdown(ed.document(), win._markdown_reference_for(target))

    assert f"![a kitten]({asset.remote_url})" in content
    assert "￼" not in content


def test_markdown_round_trip_keeps_the_image_name_and_alt(tmp_path):
    # The exact P0: a literal text insert of "![alt](url)" is escaped by
    # Qt's markdown writer into "\![alt](url)" and reopens as a link. A
    # real image format survives both directions.
    win = _Window(tmp_path / "cache")
    asset = win.uploaded_asset(alt="a kitten")
    ed = _editor_with(asset)

    markdown = ed.document().toMarkdown()
    assert f"![a kitten]({asset.key})" in markdown
    assert "\\!" not in markdown

    reopened = QTextDocument()
    reopened.setMarkdown(markdown)
    formats = _image_formats(reopened)

    assert [f.name() for f in formats] == [asset.key]
    assert [_alt_of(f) for f in formats] == ["a kitten"]


# --------------------------------------------------------------------------- #
# Publishing: what a reader actually receives
# --------------------------------------------------------------------------- #

def test_publish_markdown_emits_a_real_image_reference(tmp_path):
    win = _Window(tmp_path / "cache")
    asset = win.uploaded_asset(alt="a kitten")
    ed = _editor_with(asset)

    assert win._publish_text(ed, "markdown") == f"![a kitten]({asset.remote_url})"


def test_publish_note_emits_the_bare_url_padded_away_from_the_text(tmp_path):
    # A URL welded to the preceding word is neither readable nor
    # linkable in any client.
    win = _Window(tmp_path / "cache")
    asset = win.uploaded_asset(alt="a kitten")
    ed = _editor_with(asset, text="Look:")

    assert win._publish_text(ed, "note") == f"Look: {asset.remote_url} "


def test_an_article_carries_the_formatting_and_a_note_the_words(tmp_path):
    # An article (and its draft) is the Markdown a .md file would hold,
    # so bold reaches Nostr. A note is plain text: the words, no markup.
    win = _Window(tmp_path / "cache")
    ed = HtmlEditor()
    ed.setHtml("<p>first line</p><p>second <b>bold</b> line</p>")

    assert win._publish_text(ed, "markdown") == "first line\n\nsecond **bold** line"
    assert win._publish_text(ed, "note") == "first line\n\nsecond bold line"


def test_publish_text_never_emits_the_replacement_character(tmp_path):
    win = _Window(tmp_path / "cache")
    ed = _editor_with(win.uploaded_asset(), win.local_asset(), text="Look:")

    for flavor in ("markdown", "note"):
        assert "￼" not in win._publish_text(ed, flavor)


def test_local_objects_are_what_a_reader_cannot_fetch_yet(tmp_path):
    win = _Window(tmp_path / "cache")
    hosted = win.uploaded_asset()
    local = win.local_asset()
    ed = HtmlEditor()
    cursor = ed.textCursor()
    for name in (hosted.key, local.key, "https://other.example/cat.png",
                 "data:image/png;base64,AAAA", "pics/dog.png"):
        cursor.insertImage(_fmt(name, "x"))

    assets, missing = win._local_objects(ed)

    assert [asset.sha256 for asset in assets] == [local.sha256]
    assert missing == ["pics/dog.png"], "a file beside no document cannot be found"


def _publishing(win, ed, monkeypatch, click):
    box, shown = fake_alert(click=click)
    monkeypatch.setattr(alerts, "Alert", box)
    published = []
    win._after_uploads(ed, lambda: published.append(True))
    QApplication.processEvents()
    return shown, published


def test_publishing_asks_once_uploads_and_goes_on_by_itself(tmp_path, monkeypatch):
    # The irreversible step is where a file leaves the computer: asked
    # once, then the sheet opens by itself once the upload is done, as
    # Mail sends once its attachments are ready.
    win = _Window(tmp_path / "cache", profile=object())
    local = win.local_asset()
    ed = _editor_with(local)

    shown, published = _publishing(win, ed, monkeypatch, "Upload and Publish")

    assert [d.title for d in shown] == ["Upload 1 file to publish?"]
    assert shown[0].message == ("“a sketch” is on this computer only. Readers see it once it "
                                "is on your media servers, where anyone with its link can "
                                "view it.")
    assert published == [], "not before the file is there"
    win.finish_upload()
    assert published == [True]
    assert win._asset_manager.get(local.sha256).is_uploaded
    assert win._upload_waits == {}


def test_cancel_uploads_nothing_and_publishes_nothing(tmp_path, monkeypatch):
    win = _Window(tmp_path / "cache", profile=object())
    local = win.local_asset()

    _shown, published = _publishing(win, _editor_with(local), monkeypatch, "Cancel")

    assert published == []
    assert win._uploader.calls == []
    assert win._asset_manager.get(local.sha256).upload_state is AssetState.LOCAL


def test_a_document_on_the_servers_publishes_at_once(tmp_path, monkeypatch):
    win = _Window(tmp_path / "cache", profile=object())

    shown, published = _publishing(win, _editor_with(win.uploaded_asset()), monkeypatch, None)

    assert shown == [], "nothing to upload, so no question"
    assert published == [True]


def test_a_failed_upload_says_why_and_publishes_nothing(tmp_path, monkeypatch):
    win = _Window(tmp_path / "cache", profile=object())
    ed = _editor_with(win.local_asset())
    shown, published = _publishing(win, ed, monkeypatch, "Upload and Publish")

    win.fail_upload()

    assert [d.title for d in shown] == ["Upload 1 file to publish?",
                                        "Couldn’t upload “a sketch”"]
    assert shown[1].message.endswith("Nothing was published.")
    assert published == []


def test_try_again_uploads_again_and_then_publishes(tmp_path, monkeypatch):
    win = _Window(tmp_path / "cache", profile=object())
    ed = _editor_with(win.local_asset())
    box, shown = fake_alert(click="Upload and Publish")
    monkeypatch.setattr(alerts, "Alert", box)
    published = []
    win._after_uploads(ed, lambda: published.append(True))
    box, shown = fake_alert(click="Try Again")
    monkeypatch.setattr(alerts, "Alert", box)

    win.fail_upload()
    win.finish_upload()

    assert len(win._uploader.calls) == 2
    assert published == [True]


def test_a_picture_that_is_gone_stops_publishing_saying_which(tmp_path, monkeypatch):
    win = _Window(tmp_path / "cache", profile=object())
    ed = HtmlEditor()
    ed._file_path = str(tmp_path / "Trip.md")
    ed.textCursor().insertImage(_fmt("Trip_media/gone.png", "gone"))

    shown, published = _publishing(win, ed, monkeypatch, None)

    assert [d.title for d in shown] == ["A picture can’t be found"]
    assert "Trip_media/gone.png" in shown[0].message
    assert published == []


def test_a_picture_beside_the_document_is_published_by_its_address(tmp_path, monkeypatch):
    # A .md reopened names its pictures by their file beside it: publishing
    # used to ask to upload them, upload nothing and ask again, forever.
    win = _Window(tmp_path / "cache", profile=object())
    (tmp_path / "Trip_media").mkdir()
    (tmp_path / "Trip_media" / "sea.png").write_bytes(PNG_BYTES)
    ed = HtmlEditor()
    ed._file_path = str(tmp_path / "Trip.md")
    ed.textCursor().insertImage(_fmt("Trip_media/sea.png", "sea"))

    _shown, published = _publishing(win, ed, monkeypatch, "Upload and Publish")
    win.finish_upload()

    sha = hashlib.sha256(PNG_BYTES).hexdigest()
    assert published == [True]
    assert f"](https://cdn.example/{sha}.png)" in win._publish_text(ed, "markdown")
    assert list(iter_image_names(ed.document())) == ["Trip_media/sea.png"], (
        "the document names it as its file does: saving writes it back as it was")


def _two_local(win):
    first = win._asset_manager.adopt_bytes(GIF_BYTES, alt="sketch")
    second = win._asset_manager.adopt_bytes(PNG_BYTES, alt="photo")
    return first, second, _editor_with(first, second)


def _answer(monkeypatch, click):
    box, shown = fake_alert(click=click)
    monkeypatch.setattr(alerts, "Alert", box)
    return shown


def test_a_failure_names_the_file_that_failed_and_is_said_once(tmp_path, monkeypatch):
    # The manager fails the file queued behind a failing one first, with
    # no signal of its own: the alert named that one, opened inside the
    # manager's drain, and Try Again failed again at once, for ever.
    win = _Window(tmp_path / "cache", profile=object())
    first, second, ed = _two_local(win)
    _answer(monkeypatch, "Upload and Publish")
    published = []
    win._after_uploads(ed, lambda: published.append(True))
    QApplication.processEvents()
    sending = win._asset_manager.get(win._uploader.calls[-1].name.split(":", 1)[1])
    shown = _answer(monkeypatch, "Try Again")

    win.fail_upload()

    assert [d.title for d in shown] == [f"Couldn’t upload “{sending.alt}”"]
    assert published == []
    win.finish_upload()
    win.finish_upload()
    assert [d.title for d in shown] == [f"Couldn’t upload “{sending.alt}”"]
    assert published == [True]


def test_files_that_failed_before_are_sent_again_without_a_false_failure(tmp_path,
                                                                        monkeypatch):
    win = _Window(tmp_path / "cache", profile=object())
    first, second, ed = _two_local(win)
    win._uploader.fail_synchronously = "UPLOAD_FAILED"
    for asset in (first, second):
        win._asset_manager.request_upload(asset.sha256)
    win._uploader.fail_synchronously = None
    assert {a.upload_state for a in (first, second)} == {AssetState.FAILED}
    shown = _answer(monkeypatch, "Upload and Publish")
    published = []

    win._after_uploads(ed, lambda: published.append(True))
    QApplication.processEvents()
    win.finish_upload()
    win.finish_upload()

    assert [d.title for d in shown] == ["Upload 2 files to publish?"]
    assert published == [True]


def test_after_a_refusal_on_the_spot_and_cancel_nothing_more_is_sent(tmp_path, monkeypatch):
    # The person cancelled and was told nothing was published: the file
    # after the refused one must not leave the computer.
    win = _Window(tmp_path / "cache", profile=object())
    first, second, ed = _two_local(win)
    win._uploader.fail_synchronously = "UPLOAD_FAILED"
    _answer(monkeypatch, "Upload and Publish")
    win._after_uploads(ed, lambda: None)
    shown = _answer(monkeypatch, "Cancel")

    QApplication.processEvents()

    assert [d.title for d in shown][0].startswith("Couldn’t upload")
    assert len(win._uploader.calls) == 1
    assert AssetState.LOCAL in {first.upload_state, second.upload_state}


def test_publishing_while_a_draft_is_saved_leaves_the_draft_alone(tmp_path, monkeypatch):
    win = _Window(tmp_path / "cache", profile=object())
    ed = _editor_with(win.local_asset())
    _answer(monkeypatch, "Upload and Save")
    happened = []

    win._after_uploads(ed, lambda: happened.append("draft"), draft=True)
    win._after_uploads(ed, lambda: happened.append("publish"))
    QApplication.processEvents()
    win.finish_upload()

    assert sorted(happened) == ["draft", "publish"]


def test_a_failure_for_a_closed_document_says_nothing(tmp_path, monkeypatch):
    win = _Window(tmp_path / "cache", profile=object())
    ed = _editor_with(win.local_asset())
    _answer(monkeypatch, "Upload and Publish")
    win._after_uploads(ed, lambda: None)
    shown = _answer(monkeypatch, "Try Again")
    win._editor_widget_index = lambda _ed: -1           # its tab was closed

    win.fail_upload()

    assert shown == []


def test_a_draft_is_saved_only_under_the_account_it_was_asked_for(tmp_path):
    said, fired = [], []
    window = types.SimpleNamespace(
        _profile_store=types.SimpleNamespace(
            default=lambda: types.SimpleNamespace(user_pubkey="b" * 64)),
        status=types.SimpleNamespace(showMessage=lambda text, **context: said.append(text)),
        _fire_draft_publish_job=lambda *args: fired.append(args))

    MainWindow._save_draft(window, HtmlEditor(), types.SimpleNamespace(user_pubkey="a" * 64),
                           types.SimpleNamespace(kind=None))

    assert fired == []
    assert said == ["The draft was not saved: another account is in use."]


def test_a_markdown_save_that_cannot_copy_a_file_says_so(tmp_path):
    win = _Window(tmp_path / "cache")
    win._media_file_link = types.MethodType(MainWindow._media_file_link, win)
    song = win._asset_manager.adopt_media(b"ID3" + b"x" * 64, mime="audio/mpeg", name="song.mp3")
    ed = _editor_with(song)
    (tmp_path / "Doc_media").write_text("a file where the folder goes", encoding="utf-8")

    target = win._image_target_for_file_save(ed.document(), str(tmp_path / "Doc.md"))
    written = document_to_markdown(ed.document(), win._markdown_reference_for(target))

    assert "song" not in written
    assert target.left_out == [song]


def test_a_copy_already_beside_the_document_is_never_written_over(tmp_path):
    win = _Window(tmp_path / "cache")
    win._media_file_link = types.MethodType(MainWindow._media_file_link, win)
    song = win._asset_manager.adopt_media(b"ID3" + b"x" * 64, mime="audio/mpeg", name="song.mp3")
    (tmp_path / "Doc_media").mkdir()
    copy = tmp_path / "Doc_media" / f"{song.sha256}.mp3"
    copy.write_bytes(b"trimmed by the person since")

    link = win._media_file_link(song, str(tmp_path / "Doc.md"), "Our song")

    assert copy.read_bytes() == b"trimmed by the person since"
    assert link.markdown() == f"[Our song](Doc_media/{song.sha256}.mp3)"


def test_saving_a_draft_says_its_files_are_not_encrypted(tmp_path, monkeypatch):
    win = _Window(tmp_path / "cache", profile=object())
    box, shown = fake_alert(click="Cancel")
    monkeypatch.setattr(alerts, "Alert", box)

    win._after_uploads(_editor_with(win.local_asset()), lambda: None, draft=True)

    assert shown[0].title == "Upload 1 file to save the draft?"
    assert "Your draft is encrypted, the file on your media servers is not" in shown[0].message
    assert [b.label for b in shown[0].buttons] == ["Cancel", "Upload and Save"]


# --------------------------------------------------------------------------- #
# Formats that cannot carry an image warn before they drop it
# --------------------------------------------------------------------------- #

def test_image_only_document_warns_before_txt_and_rtf(tmp_path):
    # No colour, no bold, nothing _has_formatting can see: the image is
    # the only thing that would be lost.
    win = _Window(tmp_path / "cache")
    ed = _editor_with(win.uploaded_asset())

    assert win._loses_content_on_save(ed, "/tmp/note.txt") is True
    assert win._loses_content_on_save(ed, "/tmp/note.rtf") is True
    assert win._loses_content_on_save(ed, "/tmp/note.md") is False, (
        ".md keeps images now, so warning about them is a false alarm"
    )


def test_plain_document_warns_about_nothing(tmp_path):
    win = _Window(tmp_path / "cache")
    ed = HtmlEditor()
    ed.setPlainText("just words")

    for path in ("/tmp/note.txt", "/tmp/note.rtf", "/tmp/note.md"):
        assert win._loses_content_on_save(ed, path) is False


def test_a_picture_named_by_a_web_address_is_asked_for_not_left_blank():
    """Qt's own resolution never fetches: a reopened article's pictures were
    blank. The editor asks the window, and shows a stand-in meanwhile."""
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QTextDocument
    from editor import HtmlEditor
    asked = []
    ed = HtmlEditor()
    ed.set_web_image_resolver(lambda url: asked.append(url) or None)
    image = ed.loadResource(QTextDocument.ImageResource, QUrl("https://cdn.example/a.png"))
    assert asked == ["https://cdn.example/a.png"]
    assert image is not None and not image.isNull()
