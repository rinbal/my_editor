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
    _ask_before_losing = MainWindow._ask_before_losing
    _warn_formatting_loss = MainWindow._warn_formatting_loss
    _becomes_rmd_source = MainWindow._becomes_rmd_source
    _set_editor_content = MainWindow._set_editor_content

    def _attach_highlighter(self, ed, path):
        pass

    def _update_editor_commands(self):
        pass

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
# Markdown: the question names what goes, and it goes from the document   #
# --------------------------------------------------------------------- #

def _colored(text="A red word"):
    from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor
    ed = _editor_with(text=text)
    word = QTextCursor(ed.document())
    word.setPosition(2)
    word.setPosition(5, QTextCursor.MoveMode.KeepAnchor)
    red = QTextCharFormat()
    red.setForeground(QColor("red"))
    word.mergeCharFormat(red)
    return ed


def test_the_markdown_save_question_names_only_what_is_lost(win, monkeypatch):
    import main_window
    seen = {}
    monkeypatch.setattr(main_window, "ask", lambda parent, **kw: seen.update(kw) or "cancel")
    win._ask_before_losing(_colored(), "note.md")
    assert seen["message"] == ("Markdown can’t keep colors, highlights and underlines. "
                               "Bold, italic, links and pictures stay.")
    assert [button.label for button in seen["buttons"]] == [
        "Save Without Colors", "Save as .html", "Save as .rtf", "Cancel"]


def test_the_rtf_save_question_names_only_what_is_lost(win, monkeypatch):
    import main_window
    seen = {}
    monkeypatch.setattr(main_window, "ask", lambda parent, **kw: seen.update(kw) or "cancel")
    win._ask_before_losing(_editor_with(win.local_asset()), "note.rtf")
    assert (seen["title"], seen["message"]) == (
        "Save as .rtf without pictures?", "RTF can’t keep pictures. Text, formatting and colors stay.")
    assert [button.label for button in seen["buttons"]] == [
        "Save as .rtf Anyway", "Save as .html", "Cancel"]


def test_saving_markdown_without_colors_takes_them_off_the_document(win, tmp_path,
                                                                    monkeypatch):
    import main_window
    from markdown_writer import has_local_only_formatting
    asked = []
    monkeypatch.setattr(main_window, "ask", lambda parent, **kw: asked.append(kw) or "anyway")
    win._ed = ed = _colored()
    path = str(tmp_path / "note.md")
    assert win._ask_before_losing(ed, path) == "without colors"
    assert win._save_to(path, without_colors=True)
    assert not has_local_only_formatting(ed.document()) and not ed.document().isModified()
    assert win._ask_before_losing(ed, path) == "save" and len(asked) == 1
    ed.undo()                                   # one step brings them back
    assert has_local_only_formatting(ed.document())


def test_a_markdown_save_that_fails_keeps_the_colors(win, tmp_path, monkeypatch):
    import main_window
    from markdown_writer import has_local_only_formatting
    monkeypatch.setattr(main_window, "inform", lambda *args, **kwargs: None)
    win._ed = ed = _colored()
    (tmp_path / "a file").write_text("", encoding="utf-8")
    assert not win._save_to(str(tmp_path / "a file" / "note.md"), without_colors=True)
    assert has_local_only_formatting(ed.document())


# --------------------------------------------------------------------- #
# R Markdown asks only when its source text leaves formatting out        #
# --------------------------------------------------------------------- #

def test_a_document_converted_to_rmd_is_saved_without_a_question(win):
    # The conversion keeps bold, italic and colors (rmarkdown.document_to_rmd).
    assert win._loses_content_on_save(_colored(), "paper.Rmd") is False


def test_a_document_saved_as_rmd_is_r_markdown_source_from_then_on(win, tmp_path):
    # It stayed a document that each save converted anew (or, before that,
    # one whose next save wrote its plain text): what was typed into it
    # since, R code too, reached the file escaped.
    ed = _colored()
    win._ed = ed
    path = str(tmp_path / "paper.Rmd")
    assert win._save_to(path) is True
    first = open(path, encoding="utf-8").read()
    assert ed.toPlainText() == first and win._editor_kind(ed) == "source"
    ed.setPlainText(first + "The mean is `r mean(x)`.\n")
    ed._file_path = path
    assert win._save_to(path) is True
    assert open(path, encoding="utf-8").read() == first + "The mean is `r mean(x)`.\n"


def test_r_markdown_saved_as_md_is_written_as_it_is(win, tmp_path):
    # Each line of the text was written as a paragraph of its own: a blank
    # line after every line, in the code chunks too.
    source = "---\ntitle: x\n---\n\n- one\n- two\n\n```{r}\nx <- 1\ny <- 2\n```\n"
    ed = _editor_with(text=source)
    ed._loaded_as_rmd_source = True
    win._ed = ed
    path = str(tmp_path / "notes.md")
    assert win._save_to(path) is True
    assert open(path, encoding="utf-8").read() == source


def test_markdown_open_as_its_text_is_saved_as_rmd_as_it_is(win, tmp_path):
    ed = _editor_with(text="# Title\n\n- # Heading in an item\n")
    ed._markdown_source = True
    win._ed = ed
    path = str(tmp_path / "notes.Rmd")
    assert win._save_to(path) is True
    assert open(path, encoding="utf-8").read() == "# Title\n\n- # Heading in an item\n"


def test_the_rmd_source_question_names_what_is_lost(win, monkeypatch):
    import main_window
    seen = {}
    monkeypatch.setattr(main_window, "ask", lambda parent, **kw: seen.update(kw) or "cancel")
    ed = _colored()
    ed._loaded_as_rmd_source = True
    assert win._ask_before_losing(ed, "paper.Rmd") == "cancel"
    assert seen["message"] == ("R Markdown is saved as its source text, so bold, italic, colors "
                               "and other formatting made here are left out. Pictures stay.")


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
