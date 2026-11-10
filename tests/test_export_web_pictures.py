# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Web pictures in the PDF export, in print and in the HTML export.

Every picture the document names by a web address is fetched once for
an export or a print (export_pictures.gather: held bytes first, the rest
through the guarded fetcher, capped and bounded in time, never an
address on this computer or its network, a redirect to one included),
and read through one helper (export_pictures.picture_bytes). A picture
that cannot be had is, on paper, one quiet stand-in with its
description; nothing else is said. No test here reaches the network.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from PySide6.QtCore import QRectF, QSize, QTimer
from PySide6.QtGui import QColor, QImage, QTextDocument
from PySide6.QtWidgets import QApplication

import export_pictures
from markdown_writer import read_markdown
from tests.photo_corners import CORNERS, PHOTOS

PLAN = (PHOTOS / "plan.png").read_bytes()
PHOTO = (PHOTOS / "photo.png").read_bytes()


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _doc(markdown: str) -> QTextDocument:
    doc = QTextDocument()
    read_markdown(doc, markdown)
    return doc


def _rendered(path) -> list:
    from PySide6.QtPdf import QPdfDocument
    pdf = QPdfDocument()
    assert pdf.load(str(path)) == QPdfDocument.Error.None_
    pages = []
    for page in range(pdf.pageCount()):
        size = pdf.pagePointSize(page)
        pages.append(pdf.render(page, QSize(int(size.width()), int(size.height()))))
    pdf.close()
    return pages


def _has(image: QImage, colour: QColor, tolerance: int = 24) -> bool:
    for y in range(0, image.height(), 2):
        for x in range(0, image.width(), 2):
            found = QColor(image.pixel(x, y))
            if (abs(found.red() - colour.red()) <= tolerance
                    and abs(found.green() - colour.green()) <= tolerance
                    and abs(found.blue() - colour.blue()) <= tolerance):
                return True
    return False


def _shows_the_picture(pages) -> bool:
    return any(all(_has(page, colour) for colour in CORNERS) for page in pages)


ALONE = "Words\n\n![Plan](https://pictures.example/plan.png)\n\nMore\n"
GROUPED = ("![Plan](https://pictures.example/plan.png#wide)\n\n"
           "![Bay](https://pictures.example/photo.png)\n")
FETCHED = {"https://pictures.example/plan.png": PLAN, "https://pictures.example/photo.png": PHOTO}


def test_every_web_picture_is_named_for_the_fetch_alone_or_in_a_group(app):
    doc = _doc("![a](https://e.org/1.png)\n\nText ![b](https://e.org/2.png#wide) text\n\n"
               "![c](https://e.org/3.png)\n\n![d](https://e.org/4.png)\n\n![e](local.png)\n\n"
               "![f](https://e.org/1.png)\n")
    assert export_pictures.web_picture_names(doc) == [
        "https://e.org/1.png", "https://e.org/2.png", "https://e.org/3.png",
        "https://e.org/4.png"]


@pytest.mark.parametrize("markdown", [ALONE, GROUPED], ids=["alone", "grouped"])
def test_the_pdf_carries_a_web_picture_it_was_given(app, tmp_path, markdown):
    from export_pdf import export_pdf
    path = tmp_path / "web.pdf"
    export_pdf(_doc(markdown), str(path), "Web", remote_pictures=FETCHED)
    assert _shows_the_picture(_rendered(path))


def test_a_printed_page_carries_a_web_picture_it_was_given(app, tmp_path):
    from PySide6.QtPrintSupport import QPrinter
    from export_pdf import default_page_setup, make_page_layout
    from printing import print_document
    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
    printer.setOutputFileName(str(tmp_path / "printed.pdf"))
    printer.setPageLayout(make_page_layout(default_page_setup()))
    assert print_document(_doc(ALONE), printer, remote_pictures=FETCHED) >= 1
    assert _shows_the_picture(_rendered(tmp_path / "printed.pdf"))


def test_a_picture_that_did_not_come_is_one_quiet_stand_in_with_its_description(app, tmp_path):
    from export_pdf import _placeholder_image, export_pdf
    import photo_group
    bare = _placeholder_image("")
    named = _placeholder_image("Floor plan of the bothy")
    assert photo_group.is_placeholder(bare) and photo_group.is_placeholder(named)
    assert named.size() == bare.size() and named != bare
    ink = QColor("#6b6760")
    assert _has(named, ink, 40) and not _has(bare, ink, 40)
    # In the PDF: the plate where the picture would be, no picture.
    path = tmp_path / "missing.pdf"
    export_pdf(_doc(ALONE), str(path), "Web", remote_pictures={})
    pages = _rendered(path)
    assert not _shows_the_picture(pages)
    assert _has(pages[0], QColor("#f1f0ec"), 3)


class _Fetcher:
    def __init__(self, answers):
        self.answers = answers
        self.asked = []

    def fetch(self, url, *, on_success, on_failure):
        self.asked.append(url)
        data = self.answers.get(url)
        if data is None:
            on_failure("no")
        else:
            on_success(data, "image/png")


def test_a_print_job_gathers_its_web_pictures_once_and_prints_them(app, monkeypatch):
    """File > Print hands the pictures to the print; a preview painting
    again does not fetch again."""
    import printing
    from main_window import MainWindow
    gathered, printed = [], []

    def gather(urls, *, held=None, **_):
        gathered.append(list(urls))
        return {url: PLAN for url in urls}
    monkeypatch.setattr(export_pictures, "gather", gather)
    monkeypatch.setattr(printing, "print_document",
                        lambda doc, printer, **kw: printed.append(kw["remote_pictures"]) or 1)
    editor = SimpleNamespace(document=lambda doc=_doc(ALONE): doc)
    window = SimpleNamespace(
        current_pdf_viewer=lambda: None, current_editor=lambda: editor,
        _blob_cache_root=lambda: "/nowhere", _held_picture_bytes=lambda url: None,
        _asset_manager=SimpleNamespace(export_view=None))
    window._web_pictures = MainWindow._web_pictures.__get__(window)
    title, paint = MainWindow._print_job(window)
    assert gathered == []                          # nothing fetched to enable Print
    paint(object())
    paint(object())
    assert gathered == [["https://pictures.example/plan.png"]]
    assert printed == [{"https://pictures.example/plan.png": PLAN}] * 2


def test_a_redirect_to_this_computers_network_is_not_followed(app):
    """The real fetcher: the guard checks every redirect before it is
    followed (nostr/imports/netguard.py)."""
    from nostr.imports.fetch import BlobFetcher
    from nostr.imports.netguard import NetGuard
    from tests.blossom_fakes import FakeNam, FakeReply

    moved = FakeReply(status=302, raw_headers={"Location": b"http://169.254.169.254/plan.png"})
    nam = FakeNam([moved])
    guard = NetGuard(resolver=lambda host, on_done: on_done(["93.184.216.34"]))
    fetcher = BlobFetcher(max_bytes=export_pictures.MAX_BYTES, nam=nam, guard=guard)
    QTimer.singleShot(0, moved.finish)
    got = export_pictures.gather(["https://pictures.example/plan.png"], fetcher=fetcher,
                                 timeout_ms=60_000)
    assert got == {}
    assert len(nam.calls) == 1                 # the redirect was never asked


def test_a_web_picture_is_read_through_the_one_helper(app):
    from image_safety import ImageRootPolicy
    policy = ImageRootPolicy([])
    assert export_pictures.picture_bytes("https://pictures.example/plan.png#wide", policy,
                                         remote_pictures=FETCHED) == (
        PLAN, "https://pictures.example/plan.png")
    assert export_pictures.picture_bytes("https://pictures.example/other.png", policy,
                                         remote_pictures=FETCHED) == (None, None)
    own = SimpleNamespace(data=PHOTO, remote_url="https://blossom.example/abc.png")
    assert export_pictures.picture_bytes("asset:abc", policy, asset_resolver=lambda n: own,
                                         remote_pictures=FETCHED) == (
        PHOTO, "https://blossom.example/abc.png")
