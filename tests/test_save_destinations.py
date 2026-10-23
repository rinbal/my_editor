# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins what every save destination does with an image, not just the
three destinations that happened to be wired up.

The defect this guards against: the image-loss check was applied to .rtf,
.md and .txt by name, so every other text destination fell through to a
silent drop. An .Rmd save was worse than silent, it wrote a bare U+FFFC
into R Markdown source that then went to pandoc.

These drive the real ``MainWindow._save_to`` dispatch rather than the
helpers underneath it. The helpers were already well covered; the wiring
between them was not, which is exactly where the defect lived.
"""

import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from main_window import MainWindow

from tests.test_media_document import _Window, _editor_with


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


class SaveWindow(_Window):
    """The stub plus the real save dispatch sitting on top of it."""

    _save_to = MainWindow._save_to
    _to_rmd_content = MainWindow._to_rmd_content
    _export_title_for = MainWindow._export_title_for
    _saving_without = MainWindow._saving_without
    _detail_label = MainWindow._detail_label
    _detail_names = MainWindow._detail_names
    _article_details_of = MainWindow._article_details_of
    _published_article_of = MainWindow._published_article_of
    _say_saved = MainWindow._say_saved
    _confirm_save_over_changes = MainWindow._confirm_save_over_changes
    _resolve_disk_change = MainWindow._resolve_disk_change

    def __init__(self, cache_dir):
        super().__init__(cache_dir)
        self._saved_stamps = {}
        self._ed = None
        self.status = types.SimpleNamespace(showMessage=lambda *a, **k: None)

    def current_editor(self):
        return self._ed

    def _update_tab_title(self):
        pass


@pytest.fixture
def win(tmp_path):
    return SaveWindow(tmp_path / "cache")


OBJECT_REPLACEMENT = "￼"


# --------------------------------------------------------------------- #
# The warning covers every destination that cannot carry an image        #
# --------------------------------------------------------------------- #

@pytest.mark.parametrize("name", [
    "notes.org", "todo.text", "README", "notes.tex", "data.csv", "a.markdown",
])
def test_unknown_text_destinations_warn_before_dropping_an_image(win, name):
    ed = _editor_with(win.local_asset())
    assert win._loses_content_on_save(ed, name) is True


@pytest.mark.parametrize("name", ["page.html", "page.htm", "paper.pdf"])
def test_destinations_that_keep_images_do_not_warn(win, name):
    ed = _editor_with(win.local_asset())
    assert win._loses_content_on_save(ed, name) is False


def test_markdown_destinations_do_not_warn_about_images(win):
    # .md and .Rmd both carry images as ![alt](sidecar), so an image alone
    # is not a reason to warn.
    ed = _editor_with(win.local_asset())
    assert win._loses_content_on_save(ed, "note.md") is False
    assert win._loses_content_on_save(ed, "paper.Rmd") is False


def test_rtf_still_warns_only_about_images(win):
    ed = _editor_with(win.local_asset())
    assert win._loses_content_on_save(ed, "note.rtf") is True


# --------------------------------------------------------------------- #
# R Markdown keeps its images                                            #
# --------------------------------------------------------------------- #

def test_rmd_source_tab_keeps_a_pasted_image(win, tmp_path):
    asset = win.local_asset()
    ed = _editor_with(asset, text="Some R source\n")
    ed._loaded_as_rmd_source = True
    win._ed = ed

    path = str(tmp_path / "paper.Rmd")
    assert win._save_to(path) is True

    written = open(path, encoding="utf-8").read()
    assert OBJECT_REPLACEMENT not in written
    assert f"![{asset.alt}](paper_media/{asset.sha256}" in written
    assert os.path.isdir(tmp_path / "paper_media")


def test_rmd_conversion_path_keeps_a_pasted_image(win, tmp_path):
    asset = win.local_asset()
    ed = _editor_with(asset, text="Plain prose\n")
    win._ed = ed

    path = str(tmp_path / "report.Rmd")
    assert win._save_to(path) is True

    written = open(path, encoding="utf-8").read()
    assert OBJECT_REPLACEMENT not in written
    assert asset.sha256 in written


def test_rmd_yaml_passthrough_keeps_a_pasted_image(win, tmp_path):
    asset = win.local_asset()
    ed = _editor_with(asset, text="---\ntitle: r\n---\n\nBody\n")
    win._ed = ed

    path = str(tmp_path / "front.Rmd")
    assert win._save_to(path) is True

    written = open(path, encoding="utf-8").read()
    assert OBJECT_REPLACEMENT not in written
    assert written.lstrip().startswith("---")
    assert asset.sha256 in written


def test_rmd_uploaded_image_uses_its_remote_url(win, tmp_path):
    asset = win.uploaded_asset()
    ed = _editor_with(asset)
    ed._loaded_as_rmd_source = True
    win._ed = ed

    path = str(tmp_path / "hosted.Rmd")
    assert win._save_to(path) is True

    written = open(path, encoding="utf-8").read()
    assert f"https://cdn.example/{asset.sha256}" in written
    # A machine-local cache path must never reach a portable document.
    assert str(tmp_path / "cache") not in written


# --------------------------------------------------------------------- #
# No destination ever writes the raw placeholder                         #
# --------------------------------------------------------------------- #

@pytest.mark.parametrize("name", ["notes.org", "plain.txt", "README", "note.md"])
def test_no_text_destination_writes_the_object_replacement(win, tmp_path, name):
    win._ed = _editor_with(win.local_asset(), text="before ")
    path = str(tmp_path / name)
    assert win._save_to(path) is True
    assert OBJECT_REPLACEMENT not in open(path, encoding="utf-8").read()


def test_a_document_without_images_is_unchanged_by_the_guard(win):
    ed = _editor_with(text="just words")
    for name in ("notes.org", "README", "paper.Rmd", "plain.txt"):
        assert win._loses_content_on_save(ed, name) is False


def test_a_saved_file_keeps_the_line_endings_it_was_read_with(win, tmp_path, monkeypatch):
    # On Windows a Markdown file from a Git repository must not turn
    # every line into CRLF the first time it is saved.
    import atomic_file
    from PySide6.QtWidgets import QTextEdit

    monkeypatch.setattr(atomic_file.os, "linesep", "\r\n")
    ed = QTextEdit()
    ed.setPlainText("Line one\nLine two")
    ed._newline = "\n"
    win._ed = ed
    path = str(tmp_path / "notes.txt")
    assert win._save_to(path) is True
    assert b"\r\n" not in open(path, "rb").read()

    ed._newline = None          # a new document: this system's ending
    assert win._save_to(path) is True
    assert b"\r\n" in open(path, "rb").read()


@pytest.mark.parametrize("path, said", [
    # A Markdown file holds the file itself, as a copy beside it.
    ("/notes/post.md", "Saved without what you typed for “Hashtags”: MyEditor can’t write it "
                       "into the file."),
    # A PDF links to it once it is on a media server.
    ("/notes/post.pdf", "Saved. “talk.mp4” goes into the file once it is uploaded."),
])
def test_a_save_says_what_it_left_out(path, said):
    """Round 8, L1: the message about the details replaced the one about
    audio or video still to be uploaded."""
    shown = []
    window = types.SimpleNamespace(
        status=types.SimpleNamespace(showMessage=lambda text, **context: shown.append(text)),
        _media_not_uploaded=lambda document: [types.SimpleNamespace(alt="talk.mp4", sha256="s",
                                                                    size=10)])
    window._detail_names = lambda details: MainWindow._detail_names(window, details)
    window._detail_label = MainWindow._detail_label
    ed = types.SimpleNamespace(document=lambda: None, _details_not_saved=frozenset({"tags"}))
    MainWindow._say_saved(window, ed, path)
    assert shown == [said]


@pytest.mark.parametrize("refused, title, button", [
    ({"tags"}, "“Hashtags” can’t be saved in the file", "Save Without It"),
    ({"title", "tags"}, "Some article details can’t be saved in the file", "Save Without Them"),
])
def test_saving_without_details_names_them_in_its_button(monkeypatch, refused, title, button):
    """Round 9, L5: "Save Without It" for several details."""
    import main_window as module
    asked = []
    monkeypatch.setattr(module, "ask", lambda parent, **kwargs: asked.append(kwargs) or "keep")
    window = types.SimpleNamespace(is_dark_theme=False,
                                   _article_details_of=lambda ed: module.ArticleDetails())
    window._detail_label = MainWindow._detail_label
    window._detail_names = lambda details: MainWindow._detail_names(window, details)
    front = types.SimpleNamespace(refused=frozenset(refused))
    assert MainWindow._saving_without(window, types.SimpleNamespace(), front, "/n/post.md") \
        is False
    assert asked[0]["title"] == title
    assert [b.label for b in asked[0]["buttons"]] == [button, "Keep Editing"]
