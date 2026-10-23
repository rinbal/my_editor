# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Quick Look for the Media Library: the selected file, larger.

Space opens it, as in the Finder, and Escape closes it. It
follows the grid's selection, so the arrow keys page through the
library while it is open (they are passed on to the grid). It shows the
picture fitted to the window, the file's name and one quiet line: what
kind of file, how large, its dimensions and the day it was added. A
video or audio file plays locally with Space to play or pause. PDFs and
text files are read-only previews; other types offer Save As.

The library owns selection and save actions. This view delegates byte
loading to PreviewResolver and presentation to small preview widgets.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QEvent, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QPalette, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

import media_files
import url_safety
from . import appearance
from i18n import _
from .media_picture import MediaPicture
from .file_icons import FileIcons


class QuickLook(QDialog):
    """The Quick Look window, beside the library and above it."""

    closed = Signal()
    save_requested = Signal()
    thumbnail_ready = Signal(str, object)

    def __init__(self, *, grid: QWidget, is_dark: bool, resolver=None, parent=None) -> None:
        super().__init__(parent, Qt.WindowType.Tool)
        self.setWindowTitle(_("Quick Look"))
        self.apply_theme(is_dark)
        appearance.bind(self)
        self._grid = grid
        self._url = ""
        self._key = ""
        self._mime = ""
        self._media = None
        self._resolver = resolver
        self._player = None
        self._file_preview = None
        self._loading = False
        self._file_icons = FileIcons()
        if resolver is not None:
            resolver.ready.connect(self._resolved)
            resolver.failed.connect(self._failed)
            resolver.progress.connect(self._progress)
        self.resize(560, 480)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 14)
        layout.setSpacing(8)
        self.picture = MediaPicture(self)
        self.pages = QStackedWidget()
        self.pages.addWidget(self.picture)
        layout.addWidget(self.pages, 1)
        self.message = QLabel()
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        self.message.setWordWrap(True)
        self.message.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.message.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        layout.addWidget(self.message)
        self.retry_button = QPushButton(_("Try Again"))
        self.retry_button.setAutoDefault(False)
        self.retry_button.clicked.connect(self._retry_or_stop)
        layout.addWidget(self.retry_button, alignment=Qt.AlignmentFlag.AlignHCenter)
        self.name = QLabel()
        self.name.setObjectName("quick_look_name")
        self.name.setTextFormat(Qt.TextFormat.PlainText)
        self.name.setWordWrap(True)
        self.name.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self.name)
        self.detail = QLabel()
        self.detail.setTextFormat(Qt.TextFormat.PlainText)
        self.detail.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.detail.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        layout.addWidget(self.detail)
        row = QHBoxLayout()
        row.addStretch(1)
        self.open_button = QPushButton(_("Open in Browser"))
        self.open_button.setAutoDefault(False)
        self.open_button.clicked.connect(self._open)
        row.addWidget(self.open_button)
        self.save_button = QPushButton(_("Save As…"))
        self.save_button.setAutoDefault(False)
        self.save_button.clicked.connect(self.save_requested)
        row.addWidget(self.save_button)
        row.addStretch(1)
        layout.addLayout(row)
        self.apply_theme(is_dark)

    def apply_theme(self, is_dark: bool) -> None:
        self._is_dark = is_dark
        self.setStyleSheet(appearance.dialog_stylesheet(is_dark)
                           + "QLabel#quick_look_name { font-weight: 600; }")
        appearance.surface(self, is_dark)
        for name in ("message", "detail"):
            label = getattr(self, name, None)
            if label is not None:
                label.setForegroundRole(QPalette.WindowText)
                label.setStyleSheet(f"color: {appearance.roles(is_dark).muted};")
        if getattr(self, "_player", None) is not None:
            self._player.set_nostr(True, is_dark)
        if getattr(self, "_file_preview", None) is not None:
            self._file_preview.apply_theme(is_dark)

    def show_file(self, *, name: str, detail: str, url: str, glyph: str,
                  picture: Optional[QPixmap], media=None, mime: str = "") -> None:
        """Show this file: its picture when there is one yet, else the
        glyph of its kind ("" while a picture is on its way)."""
        self.setWindowTitle(name)
        self.name.setText(name)
        self.detail.setText(detail)
        self._url = url
        key = media.hash if media is not None else ""
        if key and key == self._key and mime == self._mime:
            return                      # metadata updates must not restart playback
        self._clear()
        self._key, self._mime, self._media = key, mime, media
        self.pages.setCurrentWidget(self.picture)
        self.message.hide()
        self.retry_button.hide()
        self.picture.pixmap = picture
        self.picture.glyph = glyph
        if media_files.library_kind(mime) == "file":
            self.picture.icon = self._file_icons.icon(name, mime)
        self.picture.update()
        self.open_button.setVisible(bool(glyph)
                                    and url_safety.is_safe_media_url(url))
        self.save_button.setVisible(media is not None)
        if media is not None and self._resolver is not None and (
                media_files.kind_of(mime) is not None or mime == "application/pdf"
                or media_files.is_text(mime)):
            self._load()
        elif media_files.library_kind(mime) == "file" and picture is None:
            self.message.setText(_("Save this file to open it in an app on your computer."))
            self.message.show()

    def _load(self):
        if self._resolver is None or self._media is None:
            return
        if self._player is not None:
            self._player.clear()
        self._loading = True
        self.retry_button.setText(_("Stop"))
        self.retry_button.show()
        self.message.setText(_("Loading preview…"))
        self.message.show()
        self._resolver.resolve(self._media)

    def _retry_or_stop(self):
        if self._loading:
            self._resolver.cancel()
            self._failed(_("Preview loading stopped."))
        else:
            self._load()

    def _progress(self, received, total):
        if total > 0:
            self.message.setText(_("Loading preview… {percent}%").format(
                percent=min(100, received * 100 // total)))

    def _resolved(self, source):
        self._loading = False
        self.retry_button.hide()
        self.message.hide()
        if media_files.kind_of(self._mime) is not None:
            if self._player is None:
                from .media_player import MediaPlayer
                self._player = MediaPlayer(self, nostr=True)
                self._player.failed.connect(self._failed)
                self._player.poster_ready.connect(self._poster)
                self.pages.addWidget(self._player)
                self._install_keys(self._player)
            self.pages.setCurrentWidget(self._player)
            self._player.set_source(source, self._mime)
        else:
            if self._file_preview is None:
                from .file_preview import FilePreview
                self._file_preview = FilePreview(self)
                self._file_preview.apply_theme(self._is_dark)
                self._file_preview.failed.connect(self._failed)
                self.pages.addWidget(self._file_preview)
            self.pages.setCurrentWidget(self._file_preview)
            self._file_preview.show_source(source, self._mime)

    def _poster(self, pixmap):
        self.thumbnail_ready.emit(self._key, pixmap)

    def _failed(self, reason):
        self._loading = False
        self.message.setText(reason)
        self.message.show()
        self.retry_button.show()
        self.retry_button.setText(_("Try Again"))

    def _clear(self):
        if self._resolver is not None:
            self._resolver.cancel()
        if self._player is not None:
            self._player.clear()
        if self._file_preview is not None:
            self._file_preview.clear()
        self.picture.pixmap = None
        self.picture.icon = None
        self.picture.glyph = ""
        self.picture.update()
        self._key = ""
        self._media = None
        self._loading = False

    def invalidate(self):
        """Privacy metadata changed; the library will show the selection again."""
        self._clear()

    def _install_keys(self, widget):
        widget.installEventFilter(self)
        for child in widget.findChildren(QWidget):
            child.installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.KeyPress and event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Escape):
            self.keyPressEvent(event)
            return True
        return super().eventFilter(watched, event)

    def set_picture(self, picture: QPixmap) -> None:
        self.picture.pixmap = picture
        self.picture.glyph = ""
        self.picture.update()

    def _open(self) -> None:
        if url_safety.is_safe_media_url(self._url):
            QDesktopServices.openUrl(QUrl(self._url))

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        if key == Qt.Key.Key_Space and media_files.kind_of(self._mime) is not None:
            if self._player is not None:
                self._player.toggle_playback()
        elif key == Qt.Key.Key_Escape and self._player is not None and self._player.video.isFullScreen():
            self._player.video.setFullScreen(False)
        elif key in (Qt.Key.Key_Space, Qt.Key.Key_Escape):
            self.close()
        elif key in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down):
            QApplication.sendEvent(self._grid, event)
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802
        self._clear()
        super().closeEvent(event)
        self.closed.emit()

    def reject(self) -> None:
        self._clear()
        super().reject()
        self.closed.emit()
