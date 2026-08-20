"""Pins the native Qt PDF exporter against QPdfDocument (pdfium).

The defect classes this file guards against:
- pagination drift: content lost or duplicated across page boundaries,
- invisible text: dark-theme default text painting near-white on the
  white page (the ctx.palette override),
- page geometry not honoring the persisted page setup,
- oversized images overflowing the printable width,
- a cold clone losing every image, or reading files outside the
  caller's trusted roots,
- an image carried by a data: URI name (every document reopened from
  .html) printing as the unavailable placeholder,
- metadata (title/creator) not landing in the file.

Painting requires a QApplication (not QCoreApplication) plus the
offscreen platform.
"""

import base64
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QSize, QLocale
from PySide6.QtGui import (
    QColor,
    QImage,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QTextImageFormat,
)
from PySide6.QtPdf import QPdfDocument

import export_pdf
from export_pdf import (
    default_page_setup,
    export_pdf as export_pdf_file,
    load_page_setup,
    save_page_setup,
)


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


A4_SETUP = {"page_size": "A4", "orientation": "portrait", "margins_mm": 20.0}
LETTER_SETUP = {"page_size": "Letter", "orientation": "portrait", "margins_mm": 20.0}


def _long_doc(n=100):
    doc = QTextDocument()
    cur = QTextCursor(doc)
    plain = QTextCharFormat()
    for i in range(n):
        if i:
            cur.insertBlock()
        cur.insertText(f"Line {i:03d} sample body text", plain)
    return doc


def _load(path):
    pdf = QPdfDocument()
    assert pdf.load(path) == QPdfDocument.Error.None_
    return pdf


# --------------------------------------------------------------------------- #
# Geometry + metadata
# --------------------------------------------------------------------------- #

def test_a4_and_letter_page_sizes(tmp_path):
    for setup, (w, h) in ((A4_SETUP, (595, 842)), (LETTER_SETUP, (612, 792))):
        out = str(tmp_path / f"{setup['page_size']}.pdf")
        export_pdf_file(_long_doc(5), out, title="T", page_setup=setup)
        pdf = _load(out)
        size = pdf.pagePointSize(0)
        assert round(size.width()) == w
        assert round(size.height()) == h


def test_landscape_orientation(tmp_path):
    setup = dict(A4_SETUP, orientation="landscape")
    out = str(tmp_path / "land.pdf")
    export_pdf_file(_long_doc(5), out, title="T", page_setup=setup)
    size = _load(out).pagePointSize(0)
    assert round(size.width()) == 842
    assert round(size.height()) == 595


def test_metadata(tmp_path):
    out = str(tmp_path / "meta.pdf")
    export_pdf_file(_long_doc(3), out, title="My Notes", page_setup=A4_SETUP)
    pdf = _load(out)
    assert pdf.metaData(QPdfDocument.MetaDataField.Title) == "My Notes"
    assert pdf.metaData(QPdfDocument.MetaDataField.Creator) == "minimal texteditor"


# --------------------------------------------------------------------------- #
# Pagination + content integrity
# --------------------------------------------------------------------------- #

def test_multipage_no_loss_no_dupes_with_footers(tmp_path):
    out = str(tmp_path / "multi.pdf")
    export_pdf_file(_long_doc(100), out, title="T", page_setup=A4_SETUP)
    pdf = _load(out)
    n = pdf.pageCount()
    assert n >= 2
    all_text = " ".join(pdf.getAllText(p).text() for p in range(n))
    for i in range(100):
        assert all_text.count(f"Line {i:03d} ") == 1
    for p in range(n):
        assert f"Page {p + 1} of {n}" in pdf.getAllText(p).text()


def test_single_page_has_no_footer(tmp_path):
    out = str(tmp_path / "single.pdf")
    export_pdf_file(_long_doc(3), out, title="T", page_setup=A4_SETUP)
    pdf = _load(out)
    assert pdf.pageCount() == 1
    assert "Page 1 of 1" not in pdf.getAllText(0).text()


def test_unset_color_text_prints_black_not_theme_color(tmp_path):
    # A document whose text has NO explicit foreground must not inherit
    # the (dark) app palette when painted; it must come out black.
    out = str(tmp_path / "dark.pdf")
    export_pdf_file(_long_doc(3), out, title="T", page_setup=A4_SETUP)
    pdf = _load(out)
    assert "Line 000" in pdf.getAllText(0).text()
    im = pdf.render(0, QSize(595, 842))
    dark_pixels = sum(
        1 for y in range(0, 842, 3) for x in range(0, 595, 3)
        if im.pixelColor(x, y).lightness() < 96
    )
    assert dark_pixels > 20  # visible dark glyphs on the white page


def test_oversized_image_capped_to_printable_width(tmp_path):
    img = QImage(3000, 1000, QImage.Format_RGB32)
    img.fill(QColor("red"))
    ipath = str(tmp_path / "big.png")
    img.save(ipath, "PNG")
    doc = QTextDocument()
    QTextCursor(doc).insertImage(ipath)
    out = str(tmp_path / "img.pdf")
    export_pdf_file(doc, out, title="T", page_setup=A4_SETUP,
                    image_roots=(str(tmp_path),))
    pdf = _load(out)
    im = pdf.render(0, QSize(595, 842))
    xs = [x for y in range(0, 842, 2) for x in range(595)
          if im.pixelColor(x, y).red() > 200 and im.pixelColor(x, y).green() < 80]
    assert xs, "image did not render"
    width_pt = max(xs) - min(xs) + 1
    printable_pt = 595 - 2 * (20 / 25.4 * 72)  # A4 minus 20mm margins
    assert width_pt <= printable_pt + 3  # rasterization tolerance


def _count_pixels(pdf, predicate):
    im = pdf.render(0, QSize(595, 842))
    return sum(1 for y in range(0, 842, 4) for x in range(0, 595, 4)
               if predicate(im.pixelColor(x, y)))


def test_image_outside_the_roots_renders_a_placeholder(tmp_path):
    # The document names the file; only the caller's roots may be read.
    outside = tmp_path / "outside"
    outside.mkdir()
    img = QImage(400, 200, QImage.Format_RGB32)
    img.fill(QColor("red"))
    ipath = str(outside / "secret.png")
    img.save(ipath, "PNG")
    root = tmp_path / "root"
    root.mkdir()
    doc = QTextDocument()
    QTextCursor(doc).insertImage(ipath)
    out = str(tmp_path / "outside.pdf")
    export_pdf_file(doc, out, title="T", page_setup=A4_SETUP,
                    image_roots=(str(root),))
    pdf = _load(out)
    red = _count_pixels(pdf, lambda c: c.red() > 200 and c.green() < 80)
    grey = _count_pixels(
        pdf, lambda c: 180 < c.red() < 220 and abs(c.red() - c.blue()) < 12)
    assert red == 0
    assert grey > 0


def test_cold_document_renders_images_from_the_asset_resolver(tmp_path):
    # clone() is a plain QTextDocument, so a resolver installed on the
    # editor widget does not apply to it: without priming, a document
    # that was never shown exports with no image at all.
    img = QImage(400, 200, QImage.Format_RGB32)
    img.fill(QColor("red"))
    ipath = str(tmp_path / "asset.png")
    img.save(ipath, "PNG")
    data = open(ipath, "rb").read()
    key = "myeditor-asset:" + "a" * 64
    fmt = QTextImageFormat()
    fmt.setName(key)
    doc = QTextDocument()
    QTextCursor(doc).insertImage(fmt)
    resolved = SimpleNamespace(data=data, mime="image/png", remote_url="",
                               width=400, height=200, alt="", sha256="a" * 64)
    out = str(tmp_path / "cold.pdf")
    export_pdf_file(doc, out, title="T", page_setup=A4_SETUP,
                    asset_resolver=lambda name: resolved)
    pdf = _load(out)
    red = _count_pixels(pdf, lambda c: c.red() > 200 and c.green() < 80)
    assert red > 0


def test_data_uri_named_image_prints_its_own_pixels(tmp_path):
    # Every document reopened from .html names its images by the data
    # URI that carries them; printing a grey box instead was losing the
    # picture on the one path that cannot be undone.
    img = QImage(400, 200, QImage.Format_RGB32)
    img.fill(QColor("red"))
    ipath = str(tmp_path / "embedded.png")
    img.save(ipath, "PNG")
    uri = ("data:image/png;base64,"
           + base64.b64encode(open(ipath, "rb").read()).decode("ascii"))
    doc = QTextDocument()
    doc.setHtml(f'<p><img src="{uri}"></p>')
    out = str(tmp_path / "datauri.pdf")
    export_pdf_file(doc, out, title="T", page_setup=A4_SETUP)
    pdf = _load(out)
    red = _count_pixels(pdf, lambda c: c.red() > 200 and c.green() < 80)
    assert red > 0


def test_data_uri_carrying_a_refused_format_stays_a_placeholder(tmp_path):
    # SVG is never handed to a renderer, whichever way it arrives.
    svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="400" height="200">'
    svg += b'<rect width="400" height="200" fill="red"/></svg>'
    uri = "data:image/svg+xml;base64," + base64.b64encode(svg).decode("ascii")
    doc = QTextDocument()
    doc.setHtml(f'<p><img src="{uri}"></p>')
    out = str(tmp_path / "svg.pdf")
    export_pdf_file(doc, out, title="T", page_setup=A4_SETUP)
    pdf = _load(out)
    red = _count_pixels(pdf, lambda c: c.red() > 200 and c.green() < 80)
    assert red == 0


# --------------------------------------------------------------------------- #
# Page setup persistence
# --------------------------------------------------------------------------- #

def test_page_setup_roundtrip_and_validation(tmp_path, monkeypatch):
    monkeypatch.setattr(export_pdf, "_CONFIG_PATH",
                        str(tmp_path / "page_setup.json"))
    save_page_setup({"page_size": "Legal", "orientation": "landscape",
                     "margins_mm": 15.0})
    loaded = load_page_setup()
    assert loaded == {"page_size": "Legal", "orientation": "landscape",
                      "margins_mm": 15.0}
    # Invalid values fall back to defaults per-field.
    save_page_setup({"page_size": "Tabloid", "orientation": "diagonal",
                     "margins_mm": 500})
    loaded = load_page_setup()
    assert loaded == default_page_setup()


def test_default_page_setup_follows_locale():
    expected = ("Letter"
                if QLocale().measurementSystem() != QLocale.MetricSystem
                else "A4")
    setup = default_page_setup()
    assert setup["page_size"] == expected
    assert setup["orientation"] == "portrait"
    assert setup["margins_mm"] == 20.0


def test_missing_config_gives_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(export_pdf, "_CONFIG_PATH",
                        str(tmp_path / "nope" / "page_setup.json"))
    assert load_page_setup() == default_page_setup()
