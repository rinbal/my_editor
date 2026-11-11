# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A Markdown file opened and saved without a change stays byte for byte
as it was, however the writer would spell what it holds (owner: "unchanged
unless edited"); once edited, the writer writes it."""

from __future__ import annotations

import shutil

import pytest
from PySide6.QtWidgets import QApplication

from tests.photo_corners import PHOTOS
from tests.window_fixture import close_window, make_window

# Spelled otherwise by the writer: an entity in a description, a group of
# pictures, "*" bullets, "__" bold, a hard break of two spaces, CRLF.
AS_WRITTEN = ("# Fish\r\n\r\n![Fish &amp; Chips](landscape.png)\r\n\r\n![Steg](portrait.png)\r\n\r\n"
              "* one\r\n* two\r\n\r\n__bold__ and a break  \r\nnext line\r\n")


@pytest.fixture
def window(tmp_path, monkeypatch):
    QApplication.instance() or QApplication([])
    w = make_window(tmp_path, monkeypatch)
    yield w
    close_window(w)


@pytest.fixture
def opened(window, tmp_path):
    for name in ("portrait.png", "landscape.png"):
        shutil.copy(PHOTOS / name, tmp_path / name)
    path = tmp_path / "fish.md"
    path.write_bytes(AS_WRITTEN.encode("utf-8"))
    ed = window.open_path(str(path))
    QApplication.processEvents()
    return ed, path


def test_a_file_saved_untouched_stays_byte_for_byte(window, opened):
    ed, path = opened
    assert window._save_to(str(path))
    assert path.read_bytes() == AS_WRITTEN.encode("utf-8")


def test_a_file_saved_after_an_edit_is_written_with_it(window, opened):
    ed, path = opened
    cursor = ed.textCursor()
    cursor.movePosition(cursor.MoveOperation.End)
    cursor.insertText("Added.")
    assert window._save_to(str(path))
    written = path.read_text(encoding="utf-8")
    # The edit, and the writer's spelling of the rest; a picture's
    # description as the file spelled it (gate 13).
    assert "Added." in written and "- one" in written and "Fish &amp; Chips" in written




def test_an_export_after_an_edit_never_brings_the_old_text_back(window, opened):
    """The first edit lets go of the file as read: a PDF export clears the
    document's modified mark, and a save after it still writes the edit."""
    ed, path = opened
    ed.textCursor().insertText("Edited. ")
    assert window._save_as_pdf(ed, str(path.with_suffix(".pdf")))
    assert not ed.document().isModified()
    assert window._save_to(str(path))
    assert "Edited." in path.read_text(encoding="utf-8")
