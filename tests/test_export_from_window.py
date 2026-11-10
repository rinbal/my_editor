# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Exports as the window runs them: File > Save As HTML, PDF and R
Markdown, and File > Print, on a real main window.

- **Where pictures are read from.** A document's pictures are read from
  where the document is (its folder, the sidebar folder it links pictures
  in, and the blob cache), never from where the export goes: a PDF saved
  to another folder carries the same pictures as one saved beside the
  document.
- **Waiting for web pictures.** A page or a PDF whose web pictures the
  window does not hold yet is written once they came, without a nested
  event loop: the export is of the document as it was asked for, closing
  the tab drops it quietly, a second request meanwhile is refused, and a
  sheet with Cancel shows only after a second.
"""

from __future__ import annotations

import shutil

import pytest
from PySide6.QtWidgets import QApplication, QProgressDialog, QPushButton

from tests.photo_corners import PHOTOS, pdf_pages, pages_show_a_picture
from tests.window_fixture import close_window, make_window

TRIP = ("# Trip\n\n![Steg](portrait.png)\n\n*The jetty*\n\n![Loch](landscape.png)\n\n"
        "Then the hut.\n\n![Hut](square.png)\n\nThe end.\n")


@pytest.fixture
def window(tmp_path, monkeypatch):
    QApplication.instance() or QApplication([])
    w = make_window(tmp_path, monkeypatch)
    yield w
    close_window(w)


@pytest.fixture
def trip(window, tmp_path):
    """trip.md open in the window, its pictures beside it in Docs/, and a
    folder Elsewhere/ to export to."""
    docs = tmp_path / "Docs"
    docs.mkdir()
    for name in ("portrait.png", "landscape.png", "square.png"):
        shutil.copy(PHOTOS / name, docs / name)
    (docs / "trip.md").write_text(TRIP, encoding="utf-8")
    elsewhere = tmp_path / "Elsewhere"
    elsewhere.mkdir()
    ed = window.open_path(str(docs / "trip.md"))
    QApplication.processEvents()
    return ed, docs, elsewhere


# --------------------------------------------------------------------------- #
# Where pictures are read from                                                 #
# --------------------------------------------------------------------------- #

def test_a_page_saved_to_another_folder_carries_the_documents_pictures(window, trip):
    ed, docs, elsewhere = trip
    for folder in (docs, elsewhere):
        assert window._save_to(str(folder / "trip.html"))
        page = (folder / "trip.html").read_text(encoding="utf-8")
        assert page.count('src="data:image/png;base64,') == 3, folder
        assert "[image unavailable]" not in page


def test_a_pdf_saved_to_another_folder_shows_the_documents_pictures(window, trip):
    ed, docs, elsewhere = trip
    for folder in (docs, elsewhere):
        assert window._save_as_pdf(ed, str(folder / "trip.pdf"))
        assert pages_show_a_picture(pdf_pages(folder / "trip.pdf")), folder


def test_r_markdown_saved_to_another_folder_takes_the_documents_pictures_along(window, trip):
    ed, docs, elsewhere = trip
    assert window._save_to(str(elsewhere / "trip.Rmd"))
    source = (elsewhere / "trip.Rmd").read_text(encoding="utf-8")
    copied = sorted(p.name for p in (elsewhere / "trip_media").iterdir())
    assert len(copied) == 3 and all(f"trip_media/{name}" in source for name in copied)


def test_an_export_never_reads_a_picture_from_the_folder_it_goes_to(window, trip):
    """A file of the same name in the export's folder is not the
    document's picture, and is never read for it."""
    ed, docs, elsewhere = trip
    stranger = elsewhere / "Stranger"
    stranger.mkdir()
    (docs / "square.png").unlink()
    shutil.copy(PHOTOS / "square.png", stranger / "square.png")
    assert window._save_to(str(stranger / "trip.html"))
    page = (stranger / "trip.html").read_text(encoding="utf-8")
    assert page.count('src="data:image/png;base64,') == 2


# --------------------------------------------------------------------------- #
# Waiting for web pictures                                                     #
# --------------------------------------------------------------------------- #

WEB = "# Away\n\n![Plan](https://pictures.example/plan.png)\n\nThe text.\n"


class _LateFetcher:
    """Answers only when the test says so; never the network."""
    made: list = []

    def __init__(self, *_a, **_k):
        self.waiting = {}
        _LateFetcher.made.append(self)

    def fetch(self, url, *, on_success, on_failure):
        self.waiting[url] = on_success

    def answer(self, data):
        for on_success in self.waiting.values():
            on_success(data, "image/png")


@pytest.fixture
def away(window, tmp_path, monkeypatch):
    """away.md open, its one picture on the web and not held yet."""
    import export_pictures
    _LateFetcher.made = []
    monkeypatch.setattr(export_pictures, "new_fetcher", _LateFetcher)
    monkeypatch.setattr(window, "_held_picture_bytes", lambda url: None)
    (tmp_path / "away.md").write_text(WEB, encoding="utf-8")
    ed = window.open_path(str(tmp_path / "away.md"))
    QApplication.processEvents()
    return ed, tmp_path


def _sheet(window):
    return next((d for d in window.findChildren(QProgressDialog) if d.labelText()), None)


def test_a_page_waits_for_its_web_pictures_without_a_nested_event_loop(window, away):
    ed, folder = away
    page = folder / "away.html"
    assert window._save_to(str(page)) is False      # under way, the app goes on
    assert not page.exists()
    _LateFetcher.made[0].answer((PHOTOS / "square.png").read_bytes())
    assert page.read_text(encoding="utf-8").count('src="data:image/png;base64,') == 1
    assert not ed.document().isModified()


def test_the_export_is_of_the_document_as_it_was_asked_for(window, away):
    ed, folder = away
    ed.document().setModified(True)
    window._save_to(str(folder / "away.html"))
    ed.selectAll()
    ed.insertPlainText("All gone")
    _LateFetcher.made[0].answer((PHOTOS / "square.png").read_bytes())
    page = (folder / "away.html").read_text(encoding="utf-8")
    assert "The text." in page and "All gone" not in page
    assert ed.document().isModified()               # the edit is not saved yet


def test_closing_the_tab_drops_the_export_quietly(window, away):
    ed, folder = away
    ed.document().setModified(False)
    window._save_as_pdf(ed, str(folder / "away.pdf"))
    window._discard_tab(window.tabs.currentIndex())
    assert window._gathering is None and _sheet(window) is None
    QApplication.processEvents()
    assert not (folder / "away.pdf").exists()


def test_a_second_export_while_one_waits_is_refused(window, away):
    ed, folder = away
    window._save_to(str(folder / "away.html"))
    assert window._save_as_pdf(ed, str(folder / "away.pdf")) is False
    assert len(_LateFetcher.made) == 1
    _LateFetcher.made[0].answer((PHOTOS / "square.png").read_bytes())
    assert (folder / "away.html").exists() and not (folder / "away.pdf").exists()


def test_the_sheet_shows_after_a_second_and_cancel_stops_the_export(window, away):
    ed, folder = away
    window._save_to(str(folder / "away.html"))
    sheet = _sheet(window)
    assert sheet.minimumDuration() == 1000 and not sheet.isVisible()
    cancel = sheet.findChild(QPushButton)
    assert cancel.text() == "Cancel"
    cancel.click()
    _LateFetcher.made[0].answer((PHOTOS / "square.png").read_bytes())
    assert not (folder / "away.html").exists() and window._gathering is None
