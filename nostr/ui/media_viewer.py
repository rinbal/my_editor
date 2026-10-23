# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The window audio or video of a document plays in.

A card's play button, in the text or in its preview, opens it: the file's
name as its title, the app's player (media_player.py) and one quiet line
while the file loads or when it can't be played. It plays as soon as the
file is here, since playing is what was asked for. Space plays and pauses,
Escape leaves full screen and then closes, as in Quick Look.

The bytes come the way the Media Library's Quick Look gets them
(PreviewResolver): from the local store when the file is there, else
downloaded through the guarded transport and checked against the file's
hash before anything plays it. One window serves every document; playing
another file replaces the one playing.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QDialog, QLabel, QPushButton, QVBoxLayout, QWidget

import media_files
import theme
from . import appearance
from i18n import _
from .media_player import MediaPlayer
from .media_source import PreviewResolver


class MediaViewer(QDialog):
    """Plays one audio or video file of a document."""

    def __init__(self, *, loader, visibility, is_dark: bool, parent=None) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self._nostr = False
        self._is_dark = is_dark
        self.setStyleSheet(theme.dialog_stylesheet(is_dark))
        appearance.bind(self)
        self._media = None
        self._mime = ""
        self._message_failed = False
        self._resolver = PreviewResolver(loader, visibility, parent=self)
        self._resolver.ready.connect(self._resolved)
        self._resolver.failed.connect(self._failed)
        self._resolver.progress.connect(self._progress)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 14)
        layout.setSpacing(8)
        self.player = MediaPlayer(self)
        self.player.failed.connect(self._failed)
        self.player.player.mediaStatusChanged.connect(self._status_changed)
        layout.addWidget(self.player, 1)
        self.message = QLabel()
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        self.message.setWordWrap(True)
        self.message.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.message.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        layout.addWidget(self.message)
        self.retry_button = QPushButton(_("Try Again"))
        self.retry_button.setAutoDefault(False)
        self.retry_button.clicked.connect(self._load)
        self.retry_button.hide()
        layout.addWidget(self.retry_button, alignment=Qt.AlignmentFlag.AlignHCenter)
        self._install_keys(self.player)
        self._play_when_loaded = False

    def apply_theme(self, dark: bool) -> None:
        self._is_dark = dark
        self.player.set_nostr(self._nostr, dark)
        if self._nostr:
            self.setStyleSheet(appearance.dialog_stylesheet(dark))
            appearance.surface(self, dark)
        else:
            self.setProperty("nightVioletSurface", None)
            from .native_appearance import apply_chrome
            apply_chrome(self, dark, nostr=False)
            self.setStyleSheet(theme.dialog_stylesheet(dark))
            appearance.neutral(self, dark)
            glow = getattr(self, "_night_violet_glow", None)
            if glow is not None:
                glow.hide()
        self._apply_message_color()

    def _apply_message_color(self) -> None:
        color = (appearance.roles(self._is_dark).error if self._message_failed
                 else appearance.roles(self._is_dark).muted)
        # QSS sets WindowText. PlaceholderText would blend that ink with the
        # background again, washing out errors in Light and dimming them in Dark.
        self.message.setForegroundRole(QPalette.WindowText if self._nostr else QPalette.PlaceholderText)
        self.message.setStyleSheet(f"color: {color};" if self._nostr else "")

    def play(self, media, mime: str, name: str, *, nostr: bool = False) -> None:
        """Play ``media`` (a MediaFile: hash, addresses, size), of type
        ``mime``, called ``name``."""
        self._nostr = nostr
        self.apply_theme(self._is_dark)
        self.setWindowTitle(name)
        if media_files.kind_of(mime) == media_files.VIDEO:
            self.resize(720, 470)
        else:
            self.resize(480, 320)
        self._media, self._mime = media, mime
        self._load()
        self.show()
        self.raise_()
        self.activateWindow()

    def _load(self) -> None:
        if self._media is None:
            return
        self.player.clear()
        self._message_failed = False
        self._apply_message_color()
        self.retry_button.hide()
        self.message.setText(_("Loading…"))
        self.message.show()
        self._play_when_loaded = True
        self._resolver.resolve(self._media)

    def _progress(self, received: int, total: int) -> None:
        if total > 0:
            self.message.setText(_("Loading… {percent} %").format(
                percent=min(100, received * 100 // total)))

    def _resolved(self, source) -> None:
        self.message.hide()
        self.player.set_source(source, self._mime)

    def _status_changed(self, status) -> None:
        # Loaded: it plays, once, as asked; after that the controls decide.
        if self._play_when_loaded and self.player.play_button.isEnabled():
            self._play_when_loaded = False
            self.player.player.play()

    def _failed(self, reason: str) -> None:
        self._play_when_loaded = False
        self._message_failed = True
        self._apply_message_color()
        self.message.setText(reason)
        self.message.show()
        self.retry_button.show()

    def _install_keys(self, widget: QWidget) -> None:
        widget.installEventFilter(self)
        for child in widget.findChildren(QWidget):
            child.installEventFilter(self)

    def eventFilter(self, watched, event):  # noqa: N802
        if event.type() == QEvent.Type.KeyPress and event.key() in (Qt.Key.Key_Space,
                                                                     Qt.Key.Key_Escape):
            self.keyPressEvent(event)
            return True
        return super().eventFilter(watched, event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        if key == Qt.Key.Key_Space:
            self.player.toggle_playback()
        elif key == Qt.Key.Key_Escape and self.player.video.isFullScreen():
            self.player.video.setFullScreen(False)
        elif key == Qt.Key.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802
        self._stop()
        super().closeEvent(event)

    def reject(self) -> None:
        self._stop()
        super().reject()

    def _stop(self) -> None:
        # Nothing plays on in a window that is gone.
        self._play_when_loaded = False
        self._resolver.cancel()
        self.player.clear()
