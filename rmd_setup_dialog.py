#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Setup dialog for the R Markdown toolchain.

Shown when the user knits without a working toolchain (and reachable via
File > R Markdown Toolchain...). Lists each component with its live
status, states exactly what would be downloaded and from where, and only
starts after an explicit Install click. Downloads and installs run on a
worker thread; output streams into the log pane. Closing the dialog
cancels cleanly.
"""

from __future__ import annotations

import threading

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

import rmd_toolchain
from rmd_toolchain import Cancelled, ToolchainError
from url_safety import is_safe_external_url

_DARK_CSS = """
QDialog { background: #1E1E1E; }
QLabel { color: #D4D4D4; font-size: 12px; }
QLabel#rmd_setup_title { color: #FFFFFF; font-size: 16px; font-weight: 600; }
QLabel#rmd_setup_note { color: #999999; font-size: 11px; }
QLabel[status="ok"] { color: #43A047; }
QLabel[status="missing"] { color: #E53935; }
QCheckBox { color: #B5B5B5; font-size: 12px; spacing: 6px; }
QPlainTextEdit {
    background: #252526; color: #CCCCCC;
    border: 1px solid #3C3C3C; border-radius: 4px;
    font-family: monospace; font-size: 11px;
}
QProgressBar {
    background: #252526; border: 1px solid #3C3C3C;
    border-radius: 4px; text-align: center; color: #D4D4D4;
}
QProgressBar::chunk { background: #007ACC; border-radius: 3px; }
QPushButton {
    background: #2D2D30; color: #D4D4D4;
    border: 1px solid #3C3C3C; padding: 6px 14px;
    border-radius: 4px; min-width: 86px;
}
QPushButton:hover { background: #3C3C3C; }
QPushButton:pressed { background: #1E1E1E; }
QPushButton:default { background: #007ACC; color: #FFFFFF; border-color: #1177C7; }
QPushButton:default:hover { background: #1177C7; }
QPushButton:disabled { color: #6E6E6E; }
"""

_LIGHT_CSS = """
QDialog { background: #FFFFFF; }
QLabel { color: #333333; font-size: 12px; }
QLabel#rmd_setup_title { color: #1A1A1A; font-size: 16px; font-weight: 600; }
QLabel#rmd_setup_note { color: #777777; font-size: 11px; }
QLabel[status="ok"] { color: #2E7D32; }
QLabel[status="missing"] { color: #C62828; }
QCheckBox { color: #555555; font-size: 12px; spacing: 6px; }
QPlainTextEdit {
    background: #F8F8F8; color: #333333;
    border: 1px solid #E1E1E1; border-radius: 4px;
    font-family: monospace; font-size: 11px;
}
QProgressBar {
    background: #F3F3F3; border: 1px solid #C5C5C5;
    border-radius: 4px; text-align: center; color: #333333;
}
QProgressBar::chunk { background: #0078D4; border-radius: 3px; }
QPushButton {
    background: #F3F3F3; color: #333333;
    border: 1px solid #C5C5C5; padding: 6px 14px;
    border-radius: 4px; min-width: 86px;
}
QPushButton:hover { background: #E5E5E5; }
QPushButton:pressed { background: #D5D5D5; }
QPushButton:default { background: #0078D4; color: #FFFFFF; border-color: #106EBE; }
QPushButton:default:hover { background: #106EBE; }
QPushButton:disabled { color: #AAAAAA; }
"""

_COMPONENTS = [
    ("r", "R interpreter", "from CRAN (cran.r-project.org)"),
    ("pandoc", "pandoc", "from the official pandoc releases (github.com/jgm/pandoc)"),
    ("rmarkdown", "rmarkdown package", "from CRAN into the app's private library"),
    ("latex", "LaTeX (optional, for PDF)", "TinyTeX via the official tinytex package"),
]


class _InstallWorker(QThread):
    """Runs the missing install steps in order on a background thread."""

    log_line = Signal(str)
    progress = Signal(int, int)
    succeeded = Signal()
    failed = Signal(str, str, str)   # message, instructions, page url
    cancelled = Signal()

    def __init__(self, want_tinytex: bool, parent=None):
        super().__init__(parent)
        self._want_tinytex = want_tinytex
        self.cancel_event = threading.Event()

    def run(self):
        log = self.log_line.emit
        prog = self.progress.emit
        cancel = self.cancel_event
        try:
            snapshot = rmd_toolchain.status(check_r_packages=True)
            if not snapshot["r"].present:
                rmd_toolchain.install_r(progress=prog, log=log, cancel=cancel)
            if not snapshot["pandoc"].present:
                rmd_toolchain.install_pandoc(progress=prog, log=log,
                                             cancel=cancel)
            # Re-check rmarkdown against whatever R we now have.
            if not rmd_toolchain.status(check_r_packages=True)["rmarkdown"].present:
                rmd_toolchain.install_rmarkdown(log=log, cancel=cancel)
            if self._want_tinytex and not snapshot["latex"].present:
                rmd_toolchain.install_tinytex(log=log, cancel=cancel)
        except Cancelled:
            self.cancelled.emit()
            return
        except ToolchainError as e:
            self.failed.emit(str(e), e.instructions, e.page)
            return
        except Exception as e:  # network failures, unexpected layouts
            self.failed.emit(str(e), "", "")
            return
        self.succeeded.emit()


class RmdSetupDialog(QDialog):
    """Consent-gated installer UI for the R Markdown toolchain."""

    def __init__(self, is_dark: bool = True, parent=None, want_pdf: bool = False):
        super().__init__(parent)
        self.setWindowTitle("R Markdown Toolchain")
        self.setModal(True)
        self.setMinimumWidth(560)
        self.setStyleSheet(_DARK_CSS if is_dark else _LIGHT_CSS)

        self._worker: _InstallWorker | None = None
        self._succeeded = False

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        root.setSpacing(10)

        title = QLabel("R Markdown Toolchain")
        title.setObjectName("rmd_setup_title")
        root.addWidget(title)

        intro = QLabel(
            "Knitting R Markdown needs the components below. Anything "
            "missing can be installed for you, each from its official "
            "source. Nothing is downloaded until you click Install.")
        intro.setWordWrap(True)
        root.addWidget(intro)

        self._status_labels: dict[str, QLabel] = {}
        for key, name, source in _COMPONENTS:
            row = QHBoxLayout()
            state = QLabel("checking...")
            state.setFixedWidth(90)
            self._status_labels[key] = state
            row.addWidget(state)
            label = QLabel(f"{name}  ({source})")
            label.setWordWrap(True)
            row.addWidget(label, 1)
            root.addLayout(row)

        self._tinytex_box = QCheckBox(
            "Also install TinyTeX to enable Knit to PDF (about 100 MB)")
        root.addWidget(self._tinytex_box)

        note = QLabel(
            "System installations are always preferred; the editor never "
            "replaces an R you already have. R packages go into the app's "
            "own library, not your system library.")
        note.setObjectName("rmd_setup_note")
        note.setWordWrap(True)
        root.addWidget(note)

        self._progress = QProgressBar()
        self._progress.setVisible(False)
        root.addWidget(self._progress)

        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setVisible(False)
        self._log.setMinimumHeight(140)
        root.addWidget(self._log)

        buttons = QHBoxLayout()
        self._page_btn = QPushButton("Open download page")
        self._page_btn.setVisible(False)
        buttons.addWidget(self._page_btn)
        buttons.addStretch(1)
        self._install_btn = QPushButton("Install")
        self._install_btn.setDefault(True)
        self._install_btn.clicked.connect(self._start_install)
        buttons.addWidget(self._install_btn)
        self._close_btn = QPushButton("Close")
        self._close_btn.clicked.connect(self.reject)
        buttons.addWidget(self._close_btn)
        root.addLayout(buttons)

        if want_pdf:
            self._tinytex_box.setChecked(True)

        self._refresh_status()

    # ------------------------------------------------------------------ #

    @property
    def succeeded(self) -> bool:
        return self._succeeded

    def _refresh_status(self):
        # Cheap checks inline; the Rscript package probe runs blocking but
        # completes in well under a second on any working install.
        snapshot = rmd_toolchain.status(check_r_packages=True)
        required_missing = False
        for key, _name, _source in _COMPONENTS:
            comp = snapshot[key]
            label = self._status_labels[key]
            if comp.present:
                label.setText("installed")
                label.setProperty("status", "ok")
            else:
                label.setText("missing")
                label.setProperty("status", "missing")
                if key != "latex":
                    required_missing = True
            label.style().unpolish(label)
            label.style().polish(label)
        self._install_btn.setEnabled(
            required_missing or (self._tinytex_box.isChecked()
                                 and not snapshot["latex"].present))
        return snapshot

    def _start_install(self):
        self._install_btn.setEnabled(False)
        self._tinytex_box.setEnabled(False)
        self._page_btn.setVisible(False)
        self._log.setVisible(True)
        self._log.clear()
        self._progress.setVisible(True)
        self._progress.setRange(0, 0)

        worker = _InstallWorker(self._tinytex_box.isChecked(), parent=self)
        worker.log_line.connect(self._append_log)
        worker.progress.connect(self._on_progress)
        worker.succeeded.connect(self._on_succeeded)
        worker.failed.connect(self._on_failed)
        worker.cancelled.connect(self._on_cancelled)
        self._worker = worker
        worker.start()

    def _append_log(self, line: str):
        self._log.appendPlainText(line)

    def _on_progress(self, done: int, total: int):
        if total > 0:
            self._progress.setRange(0, 100)
            self._progress.setValue(int(done * 100 / total))
        else:
            self._progress.setRange(0, 0)

    def _on_succeeded(self):
        self._progress.setVisible(False)
        self._append_log("\nAll components are ready.")
        self._succeeded = True
        self._refresh_status()
        self._tinytex_box.setEnabled(True)
        self.accept()

    def _on_failed(self, message: str, instructions: str, page: str):
        self._progress.setVisible(False)
        self._append_log(f"\nFAILED: {message}")
        if instructions:
            self._append_log(instructions)
        if page and is_safe_external_url(page):
            self._page_btn.setVisible(True)
            try:
                self._page_btn.clicked.disconnect()
            except (TypeError, RuntimeError):
                pass
            self._page_btn.clicked.connect(
                lambda: QDesktopServices.openUrl(QUrl(page)))
        self._tinytex_box.setEnabled(True)
        self._refresh_status()

    def _on_cancelled(self):
        self._progress.setVisible(False)
        self._append_log("\nInstall cancelled.")
        self._tinytex_box.setEnabled(True)
        self._refresh_status()

    def reject(self):
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel_event.set()
            self._worker.wait(3000)
        super().reject()
