# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The card an audio or video file shows as, in the text and in its
preview, where readers show its player: what it is, its name, how long it
plays and how its upload is going.

It is the picture of the file's object in the document (an asset, as a
picture is), drawn again whenever something about it moves on: progress,
done, failed, its first frame found. Drawing it changes nothing in the
document, so it is never a step of Undo, and finishing an upload edits
nothing the person wrote.

A video shows as a player does before it starts: its first frame (a dark
frame until it is found), a play button in the middle and, along the
bottom, its name and how long it plays. Audio is a strip: a play button,
its name, how long it plays and a waveform. The play button plays it in
the app's player (``play_button_rect`` says where the button is, for a
click on it). Both keep a little room above and below them, as a
picture's paragraph does.

While it uploads, a thin bar along its bottom edge shows how far
(upload_progress.py): moving to and fro while nothing can be measured,
then filling, the one shape throughout.
"""

from __future__ import annotations

import time
from typing import Optional, Tuple

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor, QFont, QFontMetrics, QImage, QLinearGradient, QPainter, QPainterPath, QPolygonF,
)

import media_files
from . import appearance
from constants import (
    DARK_BORDER, DARK_FG, DARK_MENU_BG, DARK_MUTED_FG, LIGHT_BORDER, LIGHT_FG, LIGHT_MUTED_FG,
)

MARGIN = 6          # room above and below the card
WIDTH = 420         # audio, in the text
HEIGHT = 58 + 2 * MARGIN
VIDEO_WIDTH = 480   # video, in the text: a frame of 16:9
_SCALE = 2          # drawn at twice the size, so it is sharp on a high density screen
_PLAY = 56          # the play button on a video, across
_AUDIO_PLAY = 34    # the play button on audio, across
# A waveform's bars, as parts of the glyph's height.
_WAVE = (0.35, 0.7, 1.0, 0.55, 0.85, 0.4)
# The upload's bar: its height, and while indeterminate, the share of the
# track its moving part takes and how long it takes to cross and back.
_BAR = 3
_BAR_SEGMENT = 0.3
_BAR_SWEEP_SECONDS = 1.6


def size_for(mime: str, width: int = 0) -> Tuple[int, int]:
    """The card's size for ``mime``'s kind, ``width`` wide (its own width
    in the text when 0)."""
    if media_files.kind_of(mime) == media_files.VIDEO:
        width = width or VIDEO_WIDTH
        return width, round(width * 9 / 16) + 2 * MARGIN
    return width or WIDTH, HEIGHT


def play_button_rect(mime: str, width: int = 0) -> QRectF:
    """Where the play button is on a card ``width`` wide, from its top left."""
    w, h = size_for(mime, width)
    if media_files.kind_of(mime) == media_files.VIDEO:
        return QRectF(w / 2 - _PLAY / 2, h / 2 - _PLAY / 2, _PLAY, _PLAY)
    return QRectF(14, h / 2 - _AUDIO_PLAY / 2, _AUDIO_PLAY, _AUDIO_PLAY)


def duration_text(milliseconds: int) -> str:
    """"1:05", or "1:02:05" past an hour; "" when not known."""
    if milliseconds <= 0:
        return ""
    seconds = milliseconds // 1000
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def render(title: str, state: str, *, dark: bool, mime: str,
           poster: Optional[QImage] = None, duration_ms: int = 0, width: int = 0,
           uploading: bool = False, fraction: Optional[float] = None) -> QImage:
    """The card ``width`` wide (its own width in the text when 0): what
    ``mime`` is, ``title`` ("interview.mp3"), how long it plays and, when
    there is one, ``state`` ("Uploading… 42 %"). A video shows ``poster``,
    its first frame, when it has been found. ``uploading``: the upload's
    bar, filled to ``fraction``, or moving while that is None."""
    w, h = size_for(mime, width)
    image = QImage(w * _SCALE, h * _SCALE, QImage.Format.Format_ARGB32_Premultiplied)
    image.setDevicePixelRatio(_SCALE)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    if media_files.kind_of(mime) == media_files.VIDEO:
        _video(painter, w, h, title, state, poster, duration_ms, dark)
        track = QColor(255, 255, 255, 60)
    else:
        _audio(painter, w, h, title, state, duration_ms, dark)
        track = QColor(DARK_BORDER if dark else LIGHT_BORDER)
    if uploading:
        bar = QRectF(14, h - MARGIN - _BAR - 5, w - 28, _BAR)
        _upload_bar(painter, bar, fraction, track, QColor(appearance.roles(dark or media_files.kind_of(mime) == media_files.VIDEO).icon))
    painter.end()
    return image


def _upload_bar(painter: QPainter, bar: QRectF, fraction: Optional[float],
                track: QColor, ink: QColor) -> None:
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(track)
    painter.drawRoundedRect(bar, _BAR / 2, _BAR / 2)
    if fraction is None:
        # To and fro across the track, as an indeterminate bar does.
        phase = (time.monotonic() % _BAR_SWEEP_SECONDS) / _BAR_SWEEP_SECONDS
        travel = 1 - abs(2 * phase - 1)
        width = bar.width() * _BAR_SEGMENT
        filled = QRectF(bar.left() + (bar.width() - width) * travel, bar.top(), width,
                        bar.height())
    else:
        filled = QRectF(bar.left(), bar.top(), bar.width() * max(0.0, min(1.0, fraction)),
                        bar.height())
    painter.setBrush(ink)
    painter.drawRoundedRect(filled, _BAR / 2, _BAR / 2)


def _video(painter: QPainter, w: int, h: int, title: str, state: str,
           poster: Optional[QImage], duration_ms: int, dark: bool) -> None:
    frame = QRectF(0, MARGIN, w, h - 2 * MARGIN)
    path = QPainterPath()
    path.addRoundedRect(frame, 10, 10)
    painter.setClipPath(path)
    painter.fillRect(frame, QColor("#0B0B0C"))
    if poster is not None and not poster.isNull():
        # Fitted whole, as a player shows a frame, on black.
        size = poster.size().toSizeF()
        size.scale(frame.size(), Qt.AspectRatioMode.KeepAspectRatio)
        target = QRectF(frame.center().x() - size.width() / 2,
                        frame.center().y() - size.height() / 2, size.width(), size.height())
        painter.drawImage(target, poster)
    else:
        glow = QLinearGradient(frame.topLeft(), frame.bottomRight())
        glow.setColorAt(0, QColor("#2A2C31"))
        glow.setColorAt(1, QColor("#121316"))
        painter.fillRect(frame, glow)

    # A scrim along the bottom, so the name reads on any frame.
    scrim_height = 62 if state else 44
    scrim = QRectF(frame.left(), frame.bottom() - scrim_height, frame.width(), scrim_height)
    shade = QLinearGradient(scrim.topLeft(), scrim.bottomLeft())
    shade.setColorAt(0, QColor(0, 0, 0, 0))
    shade.setColorAt(1, QColor(0, 0, 0, 170))
    painter.fillRect(scrim, shade)

    _play_button(painter, play_button_rect("video/mp4", w),
                 QColor(0, 0, 0, 120), QColor(255, 255, 255, 235), ring=QColor(255, 255, 255, 90))

    white = QColor(255, 255, 255, 240)
    soft = QColor(255, 255, 255, 190)
    bold = QFont()
    bold.setBold(True)
    plain = QFont()
    left, right = frame.left() + 14, frame.right() - 14
    length = duration_text(duration_ms)
    length_width = QFontMetrics(plain).horizontalAdvance(length) + 12 if length else 0
    baseline = frame.bottom() - (36 if state else 14)
    painter.setFont(bold)
    painter.setPen(white)
    painter.drawText(QPointF(left, baseline), QFontMetrics(bold).elidedText(
        title, Qt.TextElideMode.ElideMiddle, int(right - left - length_width)))
    if length:
        painter.setFont(plain)
        painter.setPen(soft)
        painter.drawText(QPointF(right - length_width + 12, baseline), length)
    if state:
        painter.setFont(plain)
        painter.setPen(soft)
        painter.drawText(QPointF(left, frame.bottom() - 18), QFontMetrics(plain).elidedText(
            state, Qt.TextElideMode.ElideRight, int(right - left)))
    painter.setClipping(False)


def _audio(painter: QPainter, w: int, h: int, title: str, state: str, duration_ms: int,
           dark: bool) -> None:
    fill = QColor(DARK_MENU_BG if dark else "#F3F4F6")
    border = QColor(DARK_BORDER if dark else LIGHT_BORDER)
    ink = QColor(DARK_FG if dark else LIGHT_FG)
    muted = QColor(DARK_MUTED_FG if dark else LIGHT_MUTED_FG)
    card = QRectF(0.5, MARGIN + 0.5, w - 1, h - 2 * MARGIN - 1)
    path = QPainterPath()
    path.addRoundedRect(card, 8, 8)
    painter.fillPath(path, fill)
    painter.setPen(border)
    painter.drawPath(path)

    _play_button(painter, play_button_rect("audio/mpeg", w), ink, fill)

    wave = QRectF(w - 14 - 30, h / 2 - 10, 30, 20)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(muted)
    _waveform(painter, wave)

    left = 14 + _AUDIO_PLAY + 12
    room = int(wave.left() - 12 - left)
    bold = QFont()
    bold.setBold(True)
    plain = QFont()
    under = state or duration_text(duration_ms)
    middle = h / 2
    title_y = middle - 4 if under else middle + QFontMetrics(bold).ascent() / 2 - 2
    painter.setFont(bold)
    painter.setPen(ink)
    painter.drawText(QPointF(left, title_y),
                     QFontMetrics(bold).elidedText(title, Qt.TextElideMode.ElideMiddle, room))
    if under:
        painter.setFont(plain)
        painter.setPen(muted)
        painter.drawText(QPointF(left, middle + 14), QFontMetrics(plain).elidedText(
            under, Qt.TextElideMode.ElideRight, room))


def _play_button(painter: QPainter, area: QRectF, disc: QColor, mark: QColor,
                 ring: Optional[QColor] = None) -> None:
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(disc)
    painter.drawEllipse(area)
    if ring is not None:
        painter.setPen(ring)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(area.adjusted(0.5, 0.5, -0.5, -0.5))
        painter.setPen(Qt.PenStyle.NoPen)
    # The triangle sits a little right of the middle, where the eye puts it.
    side = area.width() * 0.36
    cx, cy = area.center().x() + side * 0.12, area.center().y()
    painter.setBrush(mark)
    painter.drawPolygon(QPolygonF([QPointF(cx - side / 2, cy - side / 1.7),
                                   QPointF(cx - side / 2, cy + side / 1.7),
                                   QPointF(cx + side / 1.6, cy)]))


def _waveform(painter: QPainter, area: QRectF) -> None:
    step = area.width() / len(_WAVE)
    for i, part in enumerate(_WAVE):
        height = area.height() * part
        painter.drawRoundedRect(QRectF(area.left() + i * step + 1, area.center().y() - height / 2,
                                       step - 2, height), 1.5, 1.5)
