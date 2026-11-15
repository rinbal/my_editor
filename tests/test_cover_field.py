# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins the cover image field (nostr/ui/cover_field.py) that Publish as
Article and Article Details share.

What must hold:

  Choose… (with the real ellipsis) opens the Media Library's picker.
  Remove takes the cover away and shows only while there is one.

  A picture file dropped on the well is uploaded without what it reveals
  besides its pixels (the scrubbed bytes, never the file's own), and is
  the cover once its upload is done; until then the well says it is
  uploading. A file that is no picture, a failed upload, and a window
  without media servers each say why, and leave the cover as it was.

  A cover from the account's media library shows its picture; an
  address typed shows none (nothing typed is downloaded), and typing
  over a library cover takes its picture away.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QIODevice, QObject, Qt, Signal  # noqa: E402
from PySide6.QtGui import QColor, QImage, QKeySequence, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from nostr.blossom.store import MediaFile  # noqa: E402
from nostr.ui import cover_field  # noqa: E402

SHA = "ab" * 32
LIBRARY_URL = f"https://blossom.example/{SHA}.png"


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    return QApplication.instance() or QApplication([])


class FakeLoader(QObject):
    ready = Signal(str, str, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.asked = []

    def load(self, sha, url):
        self.asked.append((sha, url))


class FakeStore(QObject):
    upload_finished = Signal(str, object)
    upload_failed = Signal(str, str)
    upload_progress = Signal(str, int, int)

    def __init__(self):
        super().__init__()
        self.files = {SHA: MediaFile(hash=SHA, url=LIBRARY_URL, mime_type="image/png")}
        self.uploads = []

    def upload_bytes(self, body, *, name, mime_type="application/octet-stream"):
        self.uploads.append((body, name, mime_type))


@pytest.fixture
def field(monkeypatch):
    monkeypatch.setattr(cover_field, "ThumbnailLoader", FakeLoader)
    store = FakeStore()
    made = cover_field.CoverImageField(media_store=store)
    problems = []
    made.problem.connect(problems.append)
    return made, store, problems


def picture_file(tmp_path, name="cover.png"):
    image = QImage(4, 3, QImage.Format.Format_RGB32)
    image.fill(QColor("#336699"))
    image.setText("Location", "52.5200,13.4050")          # what must not be published
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    path = tmp_path / name
    path.write_bytes(bytes(buffer.data()))
    return path


def test_choose_and_remove_say_what_they_do(field):
    made, _store, _problems = field
    assert made.pick_button.text() == "Choose…"
    assert made.remove_button.isHidden()
    changed = []
    made.changed.connect(changed.append)
    made.setText("https://cdn.example/c.jpg")
    assert not made.remove_button.isHidden()
    made.remove_button.click()
    assert made.text() == "" and changed[-1] == "" and made.remove_button.isHidden()


def test_a_dropped_picture_is_uploaded_scrubbed_and_becomes_the_cover(field, tmp_path):
    made, store, problems = field
    path = picture_file(tmp_path)
    made.drop(str(path))
    [(body, name, mime)] = store.uploads
    assert body != path.read_bytes() and b"52.5200" not in body
    assert mime == "image/png" and made.text() == ""
    assert made.preview.text() == "Uploading…"
    store.upload_finished.emit("other.png", MediaFile(hash="cd" * 32, url="https://x/o.png"))
    assert made.text() == ""                                  # another upload: not ours
    url = f"https://blossom.example/{'ef' * 32}.png"
    store.upload_finished.emit(name, MediaFile(hash="ef" * 32, url=url, mime_type="image/png"))
    assert made.text() == url and problems == []
    assert made._loader.asked[-1] == ("ef" * 32, url)


def test_what_cannot_be_a_cover_is_said_and_changes_nothing(field, tmp_path):
    made, store, problems = field
    made.setText(LIBRARY_URL)
    notes = tmp_path / "notes.txt"
    notes.write_text("words", encoding="utf-8")
    made.drop(str(notes))
    assert problems == ["Only a picture can be a cover."] and store.uploads == []
    made.drop(str(picture_file(tmp_path)))
    store.upload_failed.emit(store.uploads[0][1],
                             "No media server is set up: add one in Media Servers.")
    # Lessons 36: what did not happen, then the store's one sentence.
    assert problems[-1] == ("The picture wasn’t uploaded. No media server is set up: add one "
                            "in Media Servers.")
    assert made.text() == LIBRARY_URL and made.preview.text() != "Uploading…"
    alone = cover_field.CoverImageField()
    said = []
    alone.problem.connect(said.append)
    alone.drop(str(picture_file(tmp_path, "other.png")))
    assert said and "web address" in said[0]


def test_a_library_cover_shows_its_picture_and_a_typed_one_none(field):
    made, _store, _problems = field
    pictures = []
    made.picture_changed.connect(lambda: pictures.append(made.picture().isNull()))
    made.setText(LIBRARY_URL)
    assert made._loader.asked == [(SHA, LIBRARY_URL)]
    made._loader.ready.emit(SHA, "", QPixmap(16, 9))
    assert not made.picture().isNull() and pictures == [False]
    made.setText("https://cdn.example/typed.jpg")
    assert made.picture().isNull() and pictures == [False, True]
    assert made.preview.text() == "Preview shown\nfor library picks"
    assert len(made._loader.asked) == 1                      # nothing typed is fetched


def test_a_read_only_cover_takes_no_drop_and_offers_no_change(field, tmp_path):
    made, store, _problems = field
    made.setReadOnly(True)
    made.drop(str(picture_file(tmp_path)))
    assert store.uploads == []
    assert not made.pick_button.isEnabled() and not made.remove_button.isEnabled()
    assert not made.preview.acceptDrops() or not made.preview.isEnabled()


def test_the_picker_is_titled_in_title_case_as_its_button_names_it(field, monkeypatch):
    made, _store, _problems = field
    titles = []

    class Picker:
        def __init__(self, **_kwargs):
            self._filter_combo = type("Combo", (), {"setCurrentIndex": lambda *_a: None})()
            self.file_picked = type("Signal", (), {"connect": lambda *_a: None})()

        def setWindowTitle(self, title):
            titles.append(title)

        def exec(self):
            return 0

    monkeypatch.setattr(cover_field, "MediaLibraryDialog", Picker)
    made.choose()
    assert titles == ["Choose Cover Image"] == [made.pick_button.accessibleName()]


def test_the_well_shows_how_far_the_upload_is_and_says_when_it_is_done(field, tmp_path):
    made, store, _problems = field
    busy = []
    made.uploading_changed.connect(busy.append)
    assert not made.uploading()
    made.drop(str(picture_file(tmp_path)))
    name = store.uploads[0][1]
    assert made.uploading() and busy == [True]
    store.upload_progress.emit("other.png", 50, 100)
    assert made.preview.text() == "Uploading\u2026"           # another upload
    store.upload_progress.emit(name, 42, 100)
    assert made.preview.text() == "Uploading\u2026\n42 %"
    store.upload_finished.emit(name, MediaFile(hash="ef" * 32, url="https://x/" + "ef" * 32))
    assert not made.uploading() and busy == [True, False]
    made.drop(str(picture_file(tmp_path, "b.png")))
    store.upload_failed.emit(store.uploads[1][1], "the server said no")
    assert not made.uploading() and busy == [True, False, True, False]


def test_a_cover_chosen_while_a_drop_uploads_wins(field, tmp_path):
    """Review round 2: a cover typed or picked while a dropped picture was
    still uploading was overwritten when the upload finished."""
    made, store, _problems = field
    made.drop(str(picture_file(tmp_path)))
    job = store.uploads[0][1]
    made.setText("https://cdn.example/typed.jpg")
    assert not made.uploading()
    store.upload_finished.emit(job, MediaFile(hash="ef" * 32, url="https://x/" + "ef" * 32))
    assert made.text() == "https://cdn.example/typed.jpg"


def test_the_drop_is_known_by_its_own_upload_not_by_its_file_name(field, tmp_path):
    """Review round 2: another upload of a file with the same name (from
    the Media Library, or a drop in the text) finishing first was taken
    as the cover."""
    made, store, _problems = field
    made.drop(str(picture_file(tmp_path, "cover.png")))
    job = store.uploads[0][1]
    assert job != "cover.png" and "cover" in job
    store.upload_finished.emit("cover.png", MediaFile(hash="cd" * 32, url="https://x/other"))
    assert made.text() == "" and made.uploading()
    store.upload_finished.emit(job, MediaFile(hash="ef" * 32, url="https://x/mine"))
    assert made.text() == "https://x/mine"


@pytest.mark.parametrize("language", ["en", "de"])
def test_the_upload_progress_fits_the_well_in_every_language(field, tmp_path, language):
    """Review round 2: "Wird hochgeladen … 100 %" was wider than the well
    on a Mac. The progress is two lines, as "No cover selected" is."""
    import i18n
    from PySide6.QtGui import QFontMetrics
    made, store, _problems = field
    i18n.install(language)
    try:
        made.drop(str(picture_file(tmp_path)))
        store.upload_progress.emit(store.uploads[0][1], 100, 100)
        lines = made.preview.text().split("\n")
        assert len(lines) == 2 and lines[1] == "100 %"
        metrics = QFontMetrics(made.preview.font())
        assert all(metrics.horizontalAdvance(line) < cover_field.THUMB.width() - 16
                   for line in lines)
    finally:
        i18n.install(i18n.ENGLISH)


def test_a_cover_dropped_in_article_details_lands_on_its_own_document(monkeypatch, tmp_path):
    """Review round 3, low 6: Article Details is one panel for every tab. A
    picture dropped for tab A, with tab B in front before its upload was
    done, became B's cover (or, B having a cover of its own, was dropped).
    The upload belongs to the document it was dropped for, and lands there."""
    from nostr.article_details import ArticleDetails
    from nostr.ui.article_details_panel import ArticleDetailsPanel
    monkeypatch.setattr(cover_field, "ThumbnailLoader", FakeLoader)
    store = FakeStore()
    panel = ArticleDetailsPanel(media_store=store)
    changed, landed = [], []
    panel.changed.connect(changed.append)
    panel.cover_uploaded.connect(lambda document, url: landed.append((document, url)))
    tab_a, tab_b, tab_c = object(), object(), object()
    url = f"https://blossom.example/{'ef' * 32}.png"

    def show(document, cover=""):
        panel.show_article(ArticleDetails(title="T", image=cover), fixed_identifier=False,
                           document=document)

    show(tab_a)
    panel.cover.drop(str(picture_file(tmp_path)))
    job = store.uploads[0][1]
    show(tab_b)                                         # no cover either
    assert not panel.cover.uploading() and "Uploading" not in panel.cover.preview.text()
    store.upload_finished.emit(job, MediaFile(hash="ef" * 32, url=url, mime_type="image/png"))
    assert panel.cover.text() == "" and changed == [] and landed == [(tab_a, url)]
    show(tab_a)
    panel.cover.drop(str(picture_file(tmp_path, "b.png")))
    show(tab_c, "https://cdn.example/c.jpg")            # a cover of its own: kept
    show(tab_a)                                         # back while it uploads
    assert panel.cover.uploading() and "Uploading" in panel.cover.preview.text()
    store.upload_finished.emit(store.uploads[1][1],
                               MediaFile(hash="ef" * 32, url=url, mime_type="image/png"))
    assert panel.cover.text() == url and changed[-1].image == url and len(landed) == 1


def test_the_window_gives_article_details_the_document_a_cover_lands_on():
    import inspect
    from main_window import MainWindow
    assert "document=ed" in inspect.getsource(MainWindow._fill_article_details_panel)
    assert "cover_uploaded.connect" in inspect.getsource(MainWindow)


def test_the_media_library_never_names_a_cover_upload_by_its_job(field, tmp_path,
                                                                    monkeypatch):
    """Review round 3 (publish-ux): a cover dropped on the well is uploaded
    under a name of its own ("cover-1a2b3c4d.png", so it is told from other
    uploads), and the Media Library, open meanwhile, said "Uploading
    cover-1a2b3c4d.png…". The library shows only the uploads it started
    now (media-ux): nothing of the cover's job, as for any upload of the
    editor's, whose file comes in once a server has it."""
    from tests.test_media_library import build, tiles
    made, store, _problems = field
    made.drop(str(picture_file(tmp_path, "cover.png")))
    job = store.uploads[0][1]
    assert job != "cover.png"
    world = build(tmp_path, monkeypatch)
    world.store.upload_started.emit(job)
    world.store.upload_progress.emit(job, 1, 2)
    world.store.mirror_failed.emit(job, "cdn.example.com", "refused")
    world.store.upload_failed.emit(job, "the server said no")
    assert tiles(world) == []
    assert job not in world.dialog._status.text()
    world.dialog.close()


def test_a_save_as_from_the_cover_picker_goes_on_when_the_picker_closes(tmp_path,
                                                                         monkeypatch):
    """Merge note (media-ux round 3, L1): the cover's picker saved with a
    Save As of its own, which went with the picker, or the sheet it opened
    from, when either closed. The window's saver (MainWindow._media_saver,
    which outlives both) is handed to it, through Publish as Article and
    Article Details alike."""
    import inspect
    from PySide6.QtCore import QObject
    from main_window import MainWindow
    from nostr.article_details import ArticleDetails
    from nostr.ui.article_details_panel import ArticleDetailsPanel
    from nostr.ui.media_saver import MediaSaver
    from tests.test_article_details import _dialog
    window = QObject()
    saver = MediaSaver(None, parent=window)
    handed = []

    class Picker:
        def __init__(self, **kwargs):
            handed.append(kwargs.get("saver"))
            self._filter_combo = type("Combo", (), {"setCurrentIndex": lambda *_a: None})()
            self.file_picked = type("Signal", (), {"connect": lambda *_a: None})()

        def setWindowTitle(self, _title):
            pass

        def exec(self):
            return 0

    monkeypatch.setattr(cover_field, "ThumbnailLoader", FakeLoader)
    monkeypatch.setattr(cover_field, "MediaLibraryDialog", Picker)
    made = cover_field.CoverImageField(media_store=FakeStore(), saver=saver)
    made.choose()
    panel = ArticleDetailsPanel(media_store=FakeStore(), media_saver=saver)
    panel.cover.choose()
    sheet = _dialog(tmp_path, ArticleDetails(title="T"), media_store=FakeStore(),
                    media_saver=saver)
    sheet._cover.choose()
    assert handed == [saver, saver, saver]
    # Without one, the picker keeps its own, as the Media Library does.
    cover_field.CoverImageField(media_store=FakeStore()).choose()
    assert handed[-1] is None
    # And the window hands its own to both.
    assert inspect.getsource(MainWindow).count("media_saver=self._media_saver") == 2
    sheet.close()
    panel.close()


# --------------------------------------------------------------------------- #
# The inspector's well                                                        #
# --------------------------------------------------------------------------- #

def stacked_field():
    from nostr.ui.cover_field import CoverImageField
    field = CoverImageField(media_store=None, stacked=True)
    field.resize(300, 400)
    field.show()
    QApplication.processEvents()
    return field


def test_the_inspectors_well_spans_its_column_in_the_cards_16_by_9():
    field = stacked_field()
    well = field.preview
    assert well.hasHeightForWidth() and well.heightForWidth(320) == 180
    assert well.focusPolicy() & Qt.FocusPolicy.TabFocus


def test_the_well_takes_delete_copy_and_return_as_an_image_well_does():
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtTest import QTest
    field = stacked_field()
    field.setText("https://cdn.example/cover.jpg")
    asked = []
    field.preview.activated.connect(lambda: asked.append(True))
    field.preview.setFocus()
    QTest.keySequence(field.preview, QKeySequence(QKeySequence.StandardKey.Copy))
    assert QGuiApplication.clipboard().text() == "https://cdn.example/cover.jpg"
    QTest.keyClick(field.preview, Qt.Key_Return)
    assert asked == [True]
    QTest.keyClick(field.preview, Qt.Key_Delete)
    assert field.text() == ""


def test_the_address_is_a_line_until_web_address_is_chosen(monkeypatch):
    from nostr.ui import cover_field
    monkeypatch.setattr(cover_field, "ThumbnailLoader", FakeLoader)
    made = cover_field.CoverImageField(media_store=FakeStore(), stacked=True)
    made.resize(300, 400)
    made.show()
    made.setText("https://cdn.example/cover.jpg")
    QApplication.processEvents()
    assert made.address_line.isVisible() and not made.url_edit.isVisible()
    assert [a.text() for a in made.pick_button.menu().actions()] == ["Media Library…",
                                                                     "Web Address…"]
    made.pick_button.menu().actions()[1].trigger()
    assert made.url_edit.isVisible() and not made.address_line.isVisible()
    made.url_edit.editingFinished.emit()
    assert made.address_line.isVisible() and not made.url_edit.isVisible()


def test_without_a_media_library_the_address_can_only_be_typed():
    made = stacked_field()
    made.setText("https://cdn.example/cover.jpg")
    QApplication.processEvents()
    assert made.url_edit.isVisible() and not made.address_line.isVisible()
