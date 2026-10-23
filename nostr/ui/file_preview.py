# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read-only PDF and bounded plain-text previews for library files."""

from PySide6.QtCore import QBuffer, QIODevice, QPointF, Signal
from PySide6.QtGui import QPalette
from PySide6.QtPdf import QPdfDocument
from PySide6.QtPdfWidgets import QPdfView
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QStackedWidget, QVBoxLayout, QWidget

import media_files
import theme
from . import appearance
from i18n import _
from .media_source import PreviewSource

TEXT_PREVIEW_BYTES = 256 * 1024


class FilePreview(QWidget):
    failed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._buffer = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.pages = QStackedWidget()
        layout.addWidget(self.pages, 1)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setAccessibleName(_("File preview"))
        self.pages.addWidget(self.text)
        self.document = QPdfDocument(self)
        self.document.statusChanged.connect(self._pdf_status)
        self.pdf = QPdfView()
        self.pdf.setDocument(self.document)
        self.pdf.setPageMode(QPdfView.PageMode.SinglePage)
        self.pdf.setZoomMode(QPdfView.ZoomMode.FitInView)
        self.pages.addWidget(self.pdf)
        self.pdf_controls = QWidget()
        row = QHBoxLayout(self.pdf_controls)
        row.setContentsMargins(0, 0, 0, 0)
        row.addStretch()
        self.previous = QPushButton(_("Previous Page"))
        self.previous.setAutoDefault(False)
        self.previous.clicked.connect(lambda: self._turn_page(-1))
        row.addWidget(self.previous)
        self.page_label = QLabel()
        row.addWidget(self.page_label)
        self.next = QPushButton(_("Next Page"))
        self.next.setAutoDefault(False)
        self.next.clicked.connect(lambda: self._turn_page(1))
        row.addWidget(self.next)
        row.addStretch()
        layout.addWidget(self.pdf_controls)
        self.note = QLabel(_("Showing the beginning of this file. Save As to read the whole file."))
        self.note.setWordWrap(True)
        self.note.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        layout.addWidget(self.note)
        self.pdf.pageNavigator().currentPageChanged.connect(self._update_page)
        self._update_page()

    def apply_theme(self, dark: bool) -> None:
        self.note.setForegroundRole(QPalette.WindowText)
        self.note.setStyleSheet(f"color: {appearance.roles(dark).muted};")
        appearance.neutral(self.text, dark)
        p = theme._palette(dark)
        self.text.setStyleSheet(f"QPlainTextEdit {{ background: {p.color(QPalette.Base).name()};"
                               f" color: {p.color(QPalette.Text).name()}; border: none; }}")

    def show_source(self, source: PreviewSource, mime: str) -> None:
        self.clear()
        pdf = mime == "application/pdf"
        self.pdf_controls.setVisible(pdf)
        self.note.hide()
        if pdf:
            self.pages.setCurrentWidget(self.pdf)
            if source.data is not None:
                self._buffer = QBuffer(self)
                self._buffer.setData(source.data)
                self._buffer.open(QIODevice.OpenModeFlag.ReadOnly)
                self.document.load(self._buffer)
            else:
                self.document.load(str(source.path))
        elif media_files.is_text(mime):
            self.pages.setCurrentWidget(self.text)
            try:
                body = source.read(TEXT_PREVIEW_BYTES + 1)
                # A UTF-8 BOM and UTF-16 BOM are common in exported files.
                encoding = "utf-16" if body.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
                text = body[:TEXT_PREVIEW_BYTES].decode(encoding, errors="replace")
                if "\x00" in text:
                    raise ValueError("binary")
                self.text.setPlainText(text)
                self.note.setVisible(len(body) > TEXT_PREVIEW_BYTES)
            except (OSError, ValueError):
                self.failed.emit(_("This file can’t be shown as text. Use Save As to open it."))

    def clear(self) -> None:
        self.document.close()
        self.text.clear()
        if self._buffer is not None:
            self._buffer.close()
            self._buffer.deleteLater()
            self._buffer = None

    def _pdf_status(self, status):
        if status == QPdfDocument.Status.Ready:
            self.pdf.pageNavigator().jump(0, QPointF())
            self._update_page()
        elif status == QPdfDocument.Status.Error:
            self.failed.emit(_("This PDF can’t be previewed. Use Save As to open it."))

    def _turn_page(self, direction):
        navigator = self.pdf.pageNavigator()
        page = navigator.currentPage() + direction
        if 0 <= page < self.document.pageCount():
            navigator.jump(page, QPointF())
            self._update_page()

    def _update_page(self):
        page = self.pdf.pageNavigator().currentPage()
        count = self.document.pageCount()
        if not count:
            self.page_label.clear()
            self.previous.setEnabled(False)
            self.next.setEnabled(False)
            return
        self.page_label.setText(_("Page {page} of {count}").format(page=page + 1, count=count))
        self.previous.setEnabled(page > 0)
        self.next.setEnabled(page + 1 < count)
