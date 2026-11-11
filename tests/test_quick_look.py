# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Quick Look: a quiet preview in a floating panel. It says when it made
the widget it shows, so the window can wire it as it wires a tab, and a
spreadsheet in it shows nothing that edits."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtCore import QCoreApplication  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from quick_look import QuickLookPanel  # noqa: E402
from tests.office_samples import make_docx  # noqa: E402
from tests.sheet_fixtures import simple_xlsx  # noqa: E402


@pytest.fixture
def panel():
    QApplication.instance() or QApplication([])
    made = QuickLookPanel(is_dark=False)
    yield made
    made.close()
    made.deleteLater()
    QCoreApplication.processEvents()


def test_the_panel_says_when_it_made_what_it_shows(tmp_path, panel):
    from viewer_tabs import FileViewerTab
    letter = make_docx(tmp_path / "Letter.docx")
    made = []
    panel.content_created.connect(made.append)
    panel.show_path(letter)
    assert len(made) == 1 and made[0] is panel.content()
    assert isinstance(made[0], FileViewerTab)
    panel.show_path(letter)                      # the same file: nothing new
    assert len(made) == 1


def test_a_spreadsheet_is_a_quiet_preview(tmp_path, panel):
    path = tmp_path / "Budget.xlsx"
    simple_xlsx(path)
    panel.show_path(str(path))
    QCoreApplication.processEvents()
    sheet = panel.content()
    assert sheet.read_only
    assert sheet._formula_bar.isHidden() and sheet._add_button.isHidden()


def test_quick_look_says_why_a_picture_cannot_be_shown_and_what_to_do(tmp_path):
    """Gate 14: every case said "it hasn't arrived". Each has its own
    cause and remedy: a document not saved yet, a file moved away, an SVG
    drawing, no room for the temporary copy, and a picture still on its
    way from the web."""
    from types import SimpleNamespace
    from main_window import MainWindow, PictureNotShown
    from tests.photo_corners import PHOTOS
    QApplication.instance() or QApplication([])
    held = {}
    window = SimpleNamespace(
        _asset_manager=SimpleNamespace(resolve_bytes=lambda name: None),
        _held_picture_bytes=lambda url: held.get(url), _look_files=None)

    def said(editor, name) -> str:
        with pytest.raises(PictureNotShown) as problem:
            MainWindow._picture_file(window, editor, name)
        return str(problem.value)

    unsaved = SimpleNamespace(_file_path="")
    saved = SimpleNamespace(_file_path=str(tmp_path / "note.md"))
    (tmp_path / "here.png").write_bytes((PHOTOS / "square.png").read_bytes())
    assert MainWindow._picture_file(window, saved, "here.png") == str(tmp_path / "here.png")
    assert "Save this document first" in said(unsaved, "here.png")
    assert "“gone.png” is no longer beside this document" in said(saved, "pictures/gone.png")
    assert "hasn’t arrived" in said(saved, "https://example.org/late.png")
    held["https://example.org/drawing.svg"] = b'<svg xmlns="http://www.w3.org/2000/svg"/>'
    assert "SVG drawing" in said(saved, "https://example.org/drawing.svg")
    # A web picture it holds is shown from a temporary copy; without room
    # for one, that is what it says.
    held["https://example.org/photo.png"] = (PHOTOS / "square.png").read_bytes()
    copy = MainWindow._picture_file(window, saved, "https://example.org/photo.png")
    assert open(copy, "rb").read() == held["https://example.org/photo.png"]
    window._look_files = SimpleNamespace(isValid=lambda: False)
    assert "no room for a temporary copy" in said(saved, "https://example.org/photo.png")
