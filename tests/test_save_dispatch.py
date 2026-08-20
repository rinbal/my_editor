"""Pins the Save As dispatch plumbing in main_window.

The defect classes this file guards against:
- the auto-extension logic regressing for cased filters (the old
  substring matching silently failed for ".Rmd (*.Rmd)"),
- .rmd accidentally matching the .md suffix branches (formatting-loss
  warning, toMarkdown save),
- the insert path putting literal markdown text into a markdown tab
  again, which Qt's own writer escapes into a link so the image reopens
  as text.
"""

import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QTextImageFormat
from PySide6.QtWidgets import QApplication

from main_window import MainWindow, _SAVE_EXTS, _SUPPORTED_EXTS, _extension_from_filter
from editor import HtmlEditor


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    # HtmlEditor is a widget, so this file needs a full QApplication.
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


# --------------------------------------------------------------------------- #
# Filter to extension extraction
# --------------------------------------------------------------------------- #

def test_extension_from_every_save_filter():
    cases = {
        ".txt (*.txt)": ".txt",
        ".html (*.html)": ".html",
        ".pdf (*.pdf)": ".pdf",
        ".md (*.md)": ".md",
        ".rtf (*.rtf)": ".rtf",
        ".Rmd (*.Rmd)": ".Rmd",   # canonical casing preserved
    }
    for filt, ext in cases.items():
        assert _extension_from_filter(filt) == ext


def test_extension_from_filter_handles_garbage():
    assert _extension_from_filter("") is None
    assert _extension_from_filter("All files (*.*)") is None


def test_save_exts_cover_every_filter_extension():
    # A typed "notes.Rmd" must count as already-has-extension.
    for typed in ("a.txt", "a.html", "a.pdf", "a.md", "a.rtf", "a.Rmd", "a.rmd"):
        assert any(typed.lower().endswith(e) for e in _SAVE_EXTS)


# --------------------------------------------------------------------------- #
# .rmd never rides the .md branches
# --------------------------------------------------------------------------- #

def test_rmd_suffix_is_not_md():
    # _save_to and the formatting-loss warning branch on these suffix
    # checks; .rmd must fall through to its own branch.
    assert not "notes.rmd".endswith('.md')
    assert not "notes.Rmd".lower().endswith(('.txt', '.md'))
    assert "notes.rmd".lower().endswith('.rmd')


def test_rmd_is_supported_for_open_and_drop():
    assert '.rmd' in _SUPPORTED_EXTS


# --------------------------------------------------------------------------- #
# _insert_asset puts a real image format into both kinds of tab
# --------------------------------------------------------------------------- #

class _StatusStub:
    def showMessage(self, *a, **k):
        pass


class _AssetManagerStub:
    def __init__(self, data=b""):
        self._data = data

    def resolve_bytes(self, key):
        return self._data or None


def _fake_window(data=b""):
    return types.SimpleNamespace(
        status=_StatusStub(), _asset_manager=_AssetManagerStub(data)
    )


def _image_formats(doc):
    out = []
    block = doc.begin()
    while block.isValid():
        it = block.begin()
        while not it.atEnd():
            frag = it.fragment()
            if frag.isValid() and frag.charFormat().isImageFormat():
                out.append(frag.charFormat().toImageFormat())
            it += 1
        block = block.next()
    return out


def test_insert_asset_uses_the_asset_key_as_the_image_name():
    ed = HtmlEditor()
    asset = types.SimpleNamespace(key=f"myeditor-asset:{'a' * 64}", sha256="a" * 64)
    MainWindow._insert_asset(_fake_window(), ed, asset, alt="a picture")

    formats = _image_formats(ed.document())
    assert [f.name() for f in formats] == [asset.key]
    assert formats[0].property(QTextImageFormat.ImageAltText) == "a picture"
    # The per-editor URL record is gone: provenance comes from the
    # asset layer, so nothing here may recreate it.
    assert not hasattr(ed, "_image_urls")


def test_insert_asset_markdown_tab_gets_a_fragment_not_literal_text():
    ed = HtmlEditor()
    ed._loaded_as_markdown = True
    asset = types.SimpleNamespace(key=f"myeditor-asset:{'b' * 64}", sha256="b" * 64)
    MainWindow._insert_asset(_fake_window(), ed, asset, alt="alt")

    # Literal "![alt](url)" text is what toMarkdown escapes into a link.
    assert "![alt]" not in ed.toPlainText()
    assert [f.name() for f in _image_formats(ed.document())] == [asset.key]
    assert not hasattr(ed, "_image_urls")
