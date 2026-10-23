# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A local Qt player with familiar transport controls and no network access."""

from PySide6.QtCore import QBuffer, QIODevice, QSize, Qt, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoFrame
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QSlider, QStackedWidget, QStyle, QToolButton, QVBoxLayout, QWidget,
)

import media_files
import theme
from . import appearance
from i18n import _
from .media_picture import MediaPicture, frame_pixmap
from .media_source import PreviewSource


def time_label(milliseconds: int) -> str:
    seconds = max(0, milliseconds) // 1000
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


class MediaPlayer(QWidget):
    failed = Signal(str)
    poster_ready = Signal(object)

    def __init__(self, parent=None, *, nostr: bool = False):
        super().__init__(parent)
        self._buffer = None
        self._resume_after_seek = False
        self._poster_sent = False
        self._is_video = False
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.audio.setVolume(0.75)
        self.player.setAudioOutput(self.audio)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.video = QVideoWidget()
        self.video.setMinimumSize(280, 200)
        self.picture = MediaPicture(allow_upscale=True)
        self.display = QStackedWidget()
        self.display.addWidget(self.picture)
        self.display.addWidget(self.video)
        self.player.setVideoOutput(self.video)
        self.video.videoSink().videoFrameChanged.connect(self._frame)
        layout.addWidget(self.display, 1)
        row = QHBoxLayout()
        self.play_button = self._transport_button()
        self.play_button.clicked.connect(self.toggle_playback)
        row.addWidget(self.play_button)
        self.elapsed = QLabel("0:00 / 0:00")
        row.addWidget(self.elapsed)
        self.seek = QSlider(Qt.Orientation.Horizontal)
        self.seek.setAccessibleName(_("Playback position"))
        self.seek.setRange(0, 0)
        self.seek.setSingleStep(5000)
        self.seek.setPageStep(30000)
        self.seek.sliderPressed.connect(self._begin_seek)
        self.seek.sliderReleased.connect(self._end_seek)
        self.seek.sliderMoved.connect(self.player.setPosition)
        self.seek.valueChanged.connect(self._seek_changed)
        row.addWidget(self.seek, 1)
        self.mute_button = self._transport_button()
        self.mute_button.clicked.connect(self._toggle_mute)
        row.addWidget(self.mute_button)
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setAccessibleName(_("Volume"))
        self.volume.setRange(0, 100)
        self.volume.setValue(75)
        self.volume.setMaximumWidth(90)
        self.volume.valueChanged.connect(lambda value: self.audio.setVolume(value / 100))
        row.addWidget(self.volume)
        self.full_screen = self._transport_button()
        self.full_screen.setAccessibleName(_("Full Screen"))
        self.full_screen.setToolTip(_("Full Screen"))
        self.full_screen.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_TitleBarMaxButton))
        self.full_screen.clicked.connect(lambda: self.video.setFullScreen(not self.video.isFullScreen()))
        row.addWidget(self.full_screen)
        layout.addLayout(row)
        self.player.playbackStateChanged.connect(self._playback_changed)
        self.player.positionChanged.connect(self._position_changed)
        self.player.durationChanged.connect(self._duration_changed)
        self.player.errorOccurred.connect(self._failed)
        self.player.mediaStatusChanged.connect(self._status_changed)
        self.player.hasAudioChanged.connect(self._audio_available)
        self.audio.mutedChanged.connect(self._mute_changed)
        self._playback_changed(self.player.playbackState())
        self._mute_changed(False)
        self._audio_available(False)
        self.set_nostr(nostr, theme.is_dark_active())

    def set_nostr(self, nostr: bool, dark: bool) -> None:
        self._nostr = nostr
        self._appearance_dark = dark
        self.setStyleSheet(appearance.controls(dark) if nostr else "")
        self.setPalette(appearance.palette(dark) if nostr else theme._palette(dark))
        appearance.neutral(self.display, dark)
        playing = self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        self.play_button.setIcon(self._transport_icon(
            QStyle.StandardPixmap.SP_MediaPause if playing else QStyle.StandardPixmap.SP_MediaPlay))
        self._mute_changed(self.audio.isMuted())
        self.full_screen.setIcon(self._transport_icon(QStyle.StandardPixmap.SP_TitleBarMaxButton))

    def _transport_icon(self, standard):
        icon = self.style().standardIcon(standard)
        if getattr(self, "_nostr", False):
            return appearance.transport_icon(icon, self._appearance_dark)
        return icon

    def _transport_button(self):
        button = QToolButton()
        button.setIconSize(QSize(18, 18))
        button.setFixedSize(32, 32)
        return button

    def set_source(self, source: PreviewSource, mime: str) -> None:
        self.clear()
        self._poster_sent = False
        video = media_files.library_kind(mime) == "video"
        self._is_video = video
        self.display.setCurrentWidget(self.picture)
        self.picture.glyph = "video" if video else "audio"
        self.picture.update()
        self.full_screen.setVisible(video)
        self.play_button.setEnabled(False)
        if not self.player.isAvailable():
            self._failed(None, "")
            return
        if source.data is not None:
            self._buffer = QBuffer(self)
            self._buffer.setData(source.data)
            self._buffer.open(QIODevice.OpenModeFlag.ReadOnly)
            self.player.setSourceDevice(self._buffer, QUrl("file:///preview" + media_files.extension_for(mime)))
        else:
            self.player.setSource(QUrl.fromLocalFile(str(source.path)))
        # Loading shows the first frame; playback starts only on request.

    def clear(self) -> None:
        self.video.setFullScreen(False)
        self.player.stop()
        self.player.setSource(QUrl())
        self.video.videoSink().setVideoFrame(QVideoFrame())
        self.play_button.setEnabled(False)
        self.seek.setEnabled(False)
        self._audio_available(False)
        self.picture.pixmap = None
        self.picture.glyph = ""
        self.picture.update()
        self.display.setCurrentWidget(self.picture)
        if self._buffer is not None:
            self._buffer.close()
            self._buffer.deleteLater()
            self._buffer = None

    def toggle_playback(self) -> None:
        if not self.play_button.isEnabled():
            return
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def _begin_seek(self):
        self._resume_after_seek = self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        self.player.pause()

    def _end_seek(self):
        self.player.setPosition(self.seek.value())
        if self._resume_after_seek:
            self.player.play()

    def _seek_changed(self, value):
        if not self.seek.isSliderDown() and value != self.player.position():
            self.player.setPosition(value)

    def _position_changed(self, position):
        if not self.seek.isSliderDown():
            self.seek.blockSignals(True)
            self.seek.setValue(position)
            self.seek.blockSignals(False)
        self.elapsed.setText(f"{time_label(position)} / {time_label(self.player.duration())}")

    def _duration_changed(self, duration):
        self.seek.setRange(0, min(duration, 2_147_483_647))
        self._position_changed(self.player.position())

    def _playback_changed(self, state):
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        label = _("Pause") if playing else _("Play")
        self.play_button.setAccessibleName(label)
        self.play_button.setToolTip(label)
        self.play_button.setIcon(self._transport_icon(
            QStyle.StandardPixmap.SP_MediaPause if playing else QStyle.StandardPixmap.SP_MediaPlay))
        if self._is_video and playing:
            self.display.setCurrentWidget(self.video)
        else:
            self._show_frame(self.video.videoSink().videoFrame())
            self.display.setCurrentWidget(self.picture)

    def _toggle_mute(self):
        self.audio.setMuted(not self.audio.isMuted())

    def _mute_changed(self, muted):
        label = _("Unmute") if muted else _("Mute")
        self.mute_button.setAccessibleName(label)
        self.mute_button.setToolTip(label)
        self.mute_button.setIcon(self._transport_icon(
            QStyle.StandardPixmap.SP_MediaVolumeMuted if muted else QStyle.StandardPixmap.SP_MediaVolume))

    def _audio_available(self, available):
        self.mute_button.setEnabled(available)
        self.volume.setEnabled(available)

    def _status_changed(self, status):
        if status in (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia):
            self.play_button.setEnabled(True)
            self.seek.setEnabled(self.player.isSeekable())

    def _failed(self, _error, _detail):
        self.play_button.setEnabled(False)
        self.failed.emit(_("This audio or video format can’t be played on this computer. Use Save As to open it."))

    def _frame(self, frame):
        if not self._poster_sent or self.player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
            self._show_frame(frame)

    def _show_frame(self, frame):
        if self.player.source().isEmpty():
            return
        pixmap = frame_pixmap(frame)
        if pixmap is not None:
            self.picture.pixmap = pixmap
            self.picture.update()
            if not self._poster_sent:
                self._poster_sent = True
                self.poster_ready.emit(pixmap)
