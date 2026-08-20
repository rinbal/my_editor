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

import os
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QUrl
from PySide6.QtGui import (
    QColor, QImage, QTextCursor, QTextDocument, QTextImageFormat,
)
from PySide6.QtWidgets import QApplication

import image_safety
import main_window as main_window_module
from doc_walk import (
    iter_block_runs, iter_blocks, iter_image_names, serialize_plain_with_images,
)
from editor import HtmlEditor
from export_html import document_to_html
from main_window import MainWindow
from nostr.media.assets import ASSET_SCHEME, AssetIndex
from nostr.media.manager import AssetManager

from tests.media_fakes import (
    GIF_BYTES, PNG_BYTES, FakeBlobStore, FakeUploader, fake_message_box,
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
    _image_target_for_file_save = MainWindow._image_target_for_file_save
    _foreign_image_target = MainWindow._foreign_image_target
    _encode_document_image = MainWindow._encode_document_image
    _make_rmd_image_copier = MainWindow._make_rmd_image_copier
    _markdown_reference_for = MainWindow._markdown_reference_for
    _markdown_with_mapped_images = MainWindow._markdown_with_mapped_images
    _publish_payload = MainWindow._publish_payload
    _publish_text = MainWindow._publish_text
    _unpublishable_images = MainWindow._unpublishable_images
    _has_images = MainWindow._has_images
    _loses_content_on_save = MainWindow._loses_content_on_save
    _has_formatting = MainWindow._has_formatting
    _resolve_local_image = MainWindow._resolve_local_image

    def __init__(self, cache_dir):
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
            profile_provider=lambda: None,
            decoder=image_safety.decode_image_bytes,
            index=AssetIndex(path=cache_dir.parent / "media_assets.json"),
        )

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
        "markdown tab": win._markdown_with_mapped_images(ed, target),
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

    content = win._markdown_with_mapped_images(ed, target)

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


def test_publish_text_matches_plain_text_without_images(tmp_path):
    # The text half has to reproduce toPlainText exactly, which is what
    # lets it stand in for it at every publish site.
    win = _Window(tmp_path / "cache")
    ed = HtmlEditor()
    ed.setHtml("<p>first line</p><p>second <b>bold</b> line</p>")

    for flavor in ("markdown", "note"):
        assert win._publish_text(ed, flavor) == ed.toPlainText()


def test_publish_text_never_emits_the_replacement_character(tmp_path):
    win = _Window(tmp_path / "cache")
    ed = _editor_with(win.uploaded_asset(), win.local_asset(), text="Look:")

    for flavor in ("markdown", "note"):
        assert "￼" not in win._publish_text(ed, flavor)


def test_unpublishable_images_blocks_only_what_a_reader_cannot_fetch(tmp_path):
    win = _Window(tmp_path / "cache")
    hosted = win.uploaded_asset()
    local = win.local_asset()
    doc = QTextDocument()
    cursor = QTextCursor(doc)
    for name in (hosted.key, local.key, "https://other.example/cat.png",
                 "data:image/png;base64,AAAA", "pics/dog.png"):
        cursor.insertImage(_fmt(name, "x"))

    assert win._unpublishable_images(doc) == [local.key, "pics/dog.png"]


def test_publish_gate_stops_a_document_with_an_unuploaded_image(tmp_path,
                                                                monkeypatch):
    # A relay event cannot be recalled, so this is the one place where
    # waiting is the right answer.
    win = _Window(tmp_path / "cache")
    win._confirm_images_uploaded = types.MethodType(
        MainWindow._confirm_images_uploaded, win)
    local = win.local_asset()
    ed = _editor_with(local)
    box, shown = fake_message_box(click="Upload now")
    monkeypatch.setattr(main_window_module, "QMessageBox", box)

    assert win._confirm_images_uploaded(ed) is False
    assert [d.title for d in shown] == ["Images not uploaded yet"]
    assert win._asset_manager.get(local.sha256).upload_state.value != "local", (
        "Upload now has to actually queue the blocked asset"
    )


def test_publish_gate_passes_a_document_whose_images_are_all_reachable(tmp_path,
                                                                       monkeypatch):
    win = _Window(tmp_path / "cache")
    win._confirm_images_uploaded = types.MethodType(
        MainWindow._confirm_images_uploaded, win)
    ed = _editor_with(win.uploaded_asset())
    box, shown = fake_message_box()
    monkeypatch.setattr(main_window_module, "QMessageBox", box)

    assert win._confirm_images_uploaded(ed) is True
    assert shown == [], "nothing to warn about, so no modal"


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
