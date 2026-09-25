#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""File > Print: the open document as formatted pages.

A note prints exactly as File > Save As .pdf lays it out: the same paper
size, orientation and margins (from File > Page Setup), the same fonts,
images capped to the page, and the same "Page N of M" footer, because both
go through export_pdf.paginate() and export_pdf.paint_pages(). A PDF tab
prints its own pages, each scaled to fit the printable area.

Following Apple's guidelines for printing, the platform's own print dialog
does the rest: printer choice, page ranges, copies and, on macOS, the
preview. Qt's dialog on Windows and Linux has no preview, so those two get
File > Print Preview; macOS doesn't, because its print panel already shows
one. One QPrinter lives as long as the window, so the chosen printer and
its options carry over to the next print.
"""

import sys

from PySide6.QtCore import QPointF, QRectF, QSize
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtPrintSupport import QPrinter

from export_pdf import make_page_layout, paginate, paint_pages

# PDF pages are rendered to images for printing. Past this resolution the
# images only grow, the output doesn't get any sharper on paper.
_PDF_MAX_DPI = 300

# macOS shows a preview in its print panel; offering a second one there
# would duplicate what the system already provides.
OFFERS_PRINT_PREVIEW = sys.platform != "darwin"


class PrintError(Exception):
    """Printing could not start (the printer or the output file refused)."""


def make_printer() -> QPrinter:
    """A printer at the device's full resolution, for one window's lifetime."""
    return QPrinter(QPrinter.PrinterMode.HighResolution)


def prepare(printer: QPrinter, title: str, page_setup: dict) -> None:
    """Name the job and start from the saved page setup."""
    printer.setDocName(title or "Untitled")
    printer.setCreator("MyEditor")
    printer.setPageLayout(make_page_layout(page_setup))


def selected_pages(printer: QPrinter, page_count: int) -> list:
    """The 1-based pages the print dialog asked for (all of them by default).

    Qt 6 dialogs accept ranges like "1-3, 5"; pages outside the document
    are ignored rather than printed blank.
    """
    ranges = printer.pageRanges()
    if ranges.isEmpty():
        return list(range(1, page_count + 1))
    return [page for page in range(1, page_count + 1) if ranges.contains(page)]


def _copy_passes(printer: QPrinter, pages: list) -> list:
    """The page sequence to paint, repeating it for copies only when the
    printer can't make copies itself."""
    if printer.supportsMultipleCopies() or printer.copyCount() <= 1:
        return pages
    copies = printer.copyCount()
    if printer.collateCopies():
        return pages * copies
    return [page for page in pages for _ in range(copies)]


def print_document(doc, printer: QPrinter, *, image_roots=(), asset_resolver=None) -> int:
    """Print a note (QTextDocument). Returns the number of pages printed.

    ``image_roots`` and ``asset_resolver`` mean what they mean for
    export_pdf(): only images from trusted places are read.
    """
    paged = paginate(doc, printer, printer.resolution(), image_roots=image_roots,
                     asset_resolver=asset_resolver)
    pages = selected_pages(printer, paged.page_count)
    if not pages:
        return 0
    painter = _begin(printer)
    try:
        paint_pages(painter, printer, paged, _copy_passes(printer, pages))
    finally:
        painter.end()
    return len(pages)


def print_pdf(document, printer: QPrinter) -> int:
    """Print a PDF (QPdfDocument), each page scaled to fit and centered.

    Returns the number of pages printed.
    """
    pages = selected_pages(printer, document.pageCount())
    if not pages:
        return 0
    painter = _begin(printer)
    try:
        area = QRectF(printer.pageLayout().paintRectPixels(printer.resolution()))
        area.moveTo(0, 0)
        dpi = min(printer.resolution(), _PDF_MAX_DPI)
        for n, number in enumerate(_copy_passes(printer, pages)):
            if n:
                printer.newPage()
            _paint_pdf_page(painter, document, number - 1, area, dpi)
    finally:
        painter.end()
    return len(pages)


def _paint_pdf_page(painter: QPainter, document, index: int, area: QRectF, dpi: int) -> None:
    points = document.pagePointSize(index)
    if points.isEmpty():
        return
    pixels = QSize(max(1, round(points.width() * dpi / 72)),
                   max(1, round(points.height() * dpi / 72)))
    rendered = document.render(index, pixels)
    # Rendered pages can have a transparent background; paper is white.
    page = QImage(pixels, QImage.Format_RGB32)
    page.fill(QColor("white"))
    with_background = QPainter(page)
    with_background.drawImage(0, 0, rendered)
    with_background.end()

    scale = min(area.width() / points.width(), area.height() / points.height())
    target = QRectF(0, 0, points.width() * scale, points.height() * scale)
    target.moveCenter(QPointF(area.center()))
    painter.setRenderHint(QPainter.SmoothPixmapTransform)
    painter.drawImage(target, page)


def _begin(printer: QPrinter) -> QPainter:
    painter = QPainter()
    if not painter.begin(printer):
        name = printer.outputFileName() or printer.printerName() or "the printer"
        raise PrintError(f"Couldn't start printing to {name}.")
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setRenderHint(QPainter.TextAntialiasing)
    return painter
