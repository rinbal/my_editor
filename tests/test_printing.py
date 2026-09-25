# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins File > Print (printing.py).

A printed note must be the same pages as its PDF export, because both are
promised as the "formatted" version of the document: same page count, same
footer, and the footer counts the whole document even when only a range
prints. The print dialog's page ranges and copies must be honoured, PDF
tabs must print every page, and a printer that can't start must raise a
PrintError the window can explain, not crash.

No physical printer is needed: each test prints to a QPrinter in PDF output
mode and reads the result back with QPdfDocument.
"""

import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QPageRanges, QTextDocument  # noqa: E402
from PySide6.QtPdf import QPdfDocument  # noqa: E402
from PySide6.QtPrintSupport import QPrinter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import printing  # noqa: E402
from export_pdf import export_pdf  # noqa: E402

SETUP = {"page_size": "A4", "orientation": "portrait", "margins_mm": 20.0}


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def long_note(paragraphs: int = 120) -> QTextDocument:
    doc = QTextDocument()
    doc.setPlainText("\n".join(f"Paragraph {i}: the quick brown fox jumps over the lazy dog."
                               for i in range(paragraphs)))
    return doc


def pdf_printer(path) -> QPrinter:
    printer = printing.make_printer()
    printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
    printer.setOutputFileName(str(path))
    printing.prepare(printer, "Notes", SETUP)
    return printer


def read_pdf(path):
    pdf = QPdfDocument()
    assert pdf.load(str(path)) == QPdfDocument.Error.None_
    return pdf


def page_text(pdf, index: int) -> str:
    return pdf.getAllText(index).text()


def test_a_printed_note_has_the_same_pages_as_its_pdf_export(tmp_path):
    doc = long_note()
    export_pdf(doc, str(tmp_path / "export.pdf"), page_setup=SETUP)
    exported = read_pdf(tmp_path / "export.pdf").pageCount()
    assert exported > 1

    printed = printing.print_document(doc, pdf_printer(tmp_path / "print.pdf"))

    assert printed == exported
    pdf = read_pdf(tmp_path / "print.pdf")
    assert pdf.pageCount() == exported
    assert f"Page 2 of {exported}" in page_text(pdf, 1)


def test_printing_leaves_the_editors_document_alone(tmp_path):
    doc = long_note(10)
    doc.setModified(False)
    before = doc.toPlainText()
    printing.print_document(doc, pdf_printer(tmp_path / "print.pdf"))
    assert doc.toPlainText() == before
    assert not doc.isModified()


def test_a_page_range_prints_only_those_pages_with_the_full_count(tmp_path):
    doc = long_note()
    printer = pdf_printer(tmp_path / "print.pdf")
    ranges = QPageRanges()
    ranges.addPage(2)
    printer.setPageRanges(ranges)

    assert printing.print_document(doc, printer) == 1
    pdf = read_pdf(tmp_path / "print.pdf")
    assert pdf.pageCount() == 1
    total = read_pdf_page_count_of_export(doc, tmp_path)
    assert f"Page 2 of {total}" in page_text(pdf, 0)


def read_pdf_page_count_of_export(doc, tmp_path) -> int:
    export_pdf(doc, str(tmp_path / "count.pdf"), page_setup=SETUP)
    return read_pdf(tmp_path / "count.pdf").pageCount()


def test_selected_pages_follows_the_dialog():
    printer = printing.make_printer()
    assert printing.selected_pages(printer, 3) == [1, 2, 3]
    ranges = QPageRanges()
    ranges.addRange(2, 9)
    printer.setPageRanges(ranges)
    assert printing.selected_pages(printer, 3) == [2, 3], "pages past the end are skipped"


def test_a_range_outside_the_document_prints_nothing(tmp_path):
    printer = pdf_printer(tmp_path / "print.pdf")
    ranges = QPageRanges()
    ranges.addRange(50, 60)
    printer.setPageRanges(ranges)
    assert printing.print_document(long_note(5), printer) == 0


@pytest.mark.parametrize("collate, expected", [
    (True, [1, 2, 1, 2]),
    (False, [1, 1, 2, 2]),
])
def test_copies_are_painted_only_when_the_printer_cannot_make_them(collate, expected):
    printer = SimpleNamespace(supportsMultipleCopies=lambda: False,
                              copyCount=lambda: 2, collateCopies=lambda: collate)
    assert printing._copy_passes(printer, [1, 2]) == expected
    capable = SimpleNamespace(supportsMultipleCopies=lambda: True,
                              copyCount=lambda: 2, collateCopies=lambda: collate)
    assert printing._copy_passes(capable, [1, 2]) == [1, 2]


def test_a_pdf_tab_prints_every_page(tmp_path):
    export_pdf(long_note(), str(tmp_path / "source.pdf"), page_setup=SETUP)
    source = read_pdf(tmp_path / "source.pdf")

    printed = printing.print_pdf(source, pdf_printer(tmp_path / "print.pdf"))

    assert printed == source.pageCount()
    assert read_pdf(tmp_path / "print.pdf").pageCount() == source.pageCount()


def test_a_printer_that_cannot_start_raises_print_error(tmp_path):
    printer = pdf_printer(tmp_path / "missing-folder" / "print.pdf")
    with pytest.raises(printing.PrintError):
        printing.print_document(long_note(3), printer)


def test_the_job_is_named_after_the_document():
    printer = printing.make_printer()
    printing.prepare(printer, "Meeting notes", SETUP)
    assert printer.docName() == "Meeting notes"
    printing.prepare(printer, "", SETUP)
    assert printer.docName() == "Untitled"


def test_print_preview_is_offered_where_the_system_has_none():
    assert printing.OFFERS_PRINT_PREVIEW is (sys.platform != "darwin")
