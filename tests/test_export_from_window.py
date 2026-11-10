# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Exports as the window runs them: File > Save As HTML, PDF and R
Markdown, and File > Print, on a real main window.

- **Where pictures are read from.** A document's pictures are read from
  where the document is (its folder, the sidebar folder it links pictures
  in, and the blob cache), never from where the export goes: a PDF saved
  to another folder carries the same pictures as one saved beside the
  document.
"""

from __future__ import annotations

import shutil

import pytest
from PySide6.QtWidgets import QApplication

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
