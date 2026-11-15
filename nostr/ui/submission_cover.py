# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A submission's cover, small (plan 10.1): in the list's rows, in the
detail's header and on the submit sheet's card.

- :func:`cover_pixmap`: the cover as a rounded square. Its picture is
  the revision's own small cover (``thumb``, read only from a verified,
  opened revision); before that, the kind's symbol (plan 9.4). A thumb
  is decoded once, no larger than it is drawn, and kept drawn: a row is
  painted again and again (RG-M3-a 8). One larger than
  :data:`MAX_THUMB_SIDE` a side, or of more than its bytes, is not
  decoded at all: the symbol stands in.
- :func:`monogram`: a card picture made of the title's first letters,
  for a cover that is only a web address and could not be fetched.
- :class:`CoverFetch`: such a cover fetched for the card and the
  revision's ``thumb``: through the importer's fetcher (the network
  guard: nothing on the local network, every redirect checked, no
  credentials), at most 5 MiB and 10 seconds, decoded only through the
  image allow-list. When it does not come, nothing is said: the card
  shows the monogram.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Callable, Optional

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QImage, QImageReader, QPainter, QPainterPath, QPixmap

import format_icons
from image_safety import decode_image_bytes

from ..imports.fetch import BlobFetcher
from .avatar import initials_for

MAX_BYTES = 5 * 1024 * 1024
TIMEOUT_MS = 10_000
# A revision's thumb is at most 24 KB (contract 5); more is not decoded,
# nor one larger a side than a thumb needs to be.
_MAX_THUMB_BYTES = 64 * 1024
MAX_THUMB_SIDE = 512
# Covers drawn, by their thumb, kind, size and colours: a list's worth.
_DRAWN: "OrderedDict[tuple, QPixmap]" = OrderedDict()
_KEEP = 256


def _rounded(size: int, radius: float) -> tuple:
    pixmap = QPixmap(size * 2, size * 2)
    pixmap.setDevicePixelRatio(2.0)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, size, size), radius, radius)
    painter.setClipPath(path)
    return pixmap, painter, path


def cover_pixmap(data: Optional[bytes], kind: str, size: int, fill: QColor,
                 ink: QColor) -> QPixmap:
    """The cover as a rounded square of ``size`` points: the picture in
    ``data`` (a revision's ``thumb``), cropped to fill it, or the symbol
    of ``kind`` on ``fill``. Drawn once for the same thumb and look."""
    key = (data or b"", kind, size, fill.rgba(), ink.rgba())
    drawn = _DRAWN.get(key)
    if drawn is not None:
        _DRAWN.move_to_end(key)
        return drawn
    drawn = _draw(data, kind, size, fill, ink)
    _DRAWN[key] = drawn
    while len(_DRAWN) > _KEEP:
        _DRAWN.popitem(last=False)
    return drawn


def _thumb(data: Optional[bytes], size: int) -> Optional[QImage]:
    """The thumb decoded no larger than ``size`` device pixels need, or
    None: too many bytes, too large a side, or no picture."""
    if not data or len(data) > _MAX_THUMB_BYTES:
        return None
    buffer = QBuffer()
    buffer.setData(QByteArray(data))
    if not buffer.open(QIODevice.OpenModeFlag.ReadOnly):
        return None
    declared = QImageReader(buffer).size()
    if not declared.isValid() or max(declared.width(), declared.height()) > MAX_THUMB_SIDE:
        return None
    return decode_image_bytes(data, at_least=QSize(size, size))


def _draw(data: Optional[bytes], kind: str, size: int, fill: QColor, ink: QColor) -> QPixmap:
    pixmap, painter, path = _rounded(size, size * 0.18)
    image = _thumb(data, size * 2)
    if image is not None and not image.isNull():
        scaled = image.scaled(size * 2, size * 2,
                              Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                              Qt.TransformationMode.SmoothTransformation)
        painter.drawImage(QRectF(0, 0, size, size), scaled,
                          QRectF((scaled.width() - size * 2) / 2,
                                 (scaled.height() - size * 2) / 2, size * 2, size * 2))
    else:
        painter.fillPath(path, fill)
        glyph = size * 0.5
        format_icons.paint("audio" if kind == "audio" else "article", painter,
                           QRectF((size - glyph) / 2, (size - glyph) / 2, glyph, glyph), ink)
    painter.end()
    return pixmap


def monogram(title: str, width: int, height: int, fill: QColor, ink: QColor) -> QPixmap:
    """A card picture of the title's first letters, as Contacts draws a
    person without a picture."""
    pixmap = QPixmap(width * 2, height * 2)
    pixmap.setDevicePixelRatio(2.0)
    pixmap.fill(fill)
    painter = QPainter(pixmap)
    font = QFont(painter.font())
    font.setBold(True)
    font.setPixelSize(max(12, int(height * 0.4)))
    painter.setFont(font)
    painter.setPen(ink)
    painter.drawText(QRectF(0, 0, width, height), Qt.AlignmentFlag.AlignCenter,
                     initials_for(title or "?"))
    painter.end()
    return pixmap


def png_bytes(image: QImage) -> bytes:
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(buffer.data())


class CoverFetch(QObject):
    """One cover from a web address (see the module docstring).
    ``fetcher`` and ``timer`` are seams."""

    def __init__(self, parent: Optional[QObject] = None, *, fetcher=None, timer=None) -> None:
        super().__init__(parent)
        self._fetcher = fetcher or BlobFetcher(self, max_bytes=MAX_BYTES)
        self._timer = timer if timer is not None else QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._give_up)
        self._waiting: Optional[Callable[[Optional[QImage]], None]] = None

    def fetch(self, url: str, on_done: Callable[[Optional[QImage]], None]) -> None:
        """``on_done(image)`` once: the cover decoded, or None when it did
        not come in time, is too large, or is no picture."""
        self.stop()
        self._waiting = on_done
        self._timer.start(TIMEOUT_MS)
        self._fetcher.fetch(url, on_success=lambda data, _mime, asked=on_done: self._got(
            asked, data), on_failure=lambda _why, asked=on_done: self._got(asked, None))

    def stop(self) -> None:
        """The account was let go, or the sheet closed: nothing answers."""
        self._waiting = None
        self._timer.stop()

    def _give_up(self) -> None:
        waiting, self._waiting = self._waiting, None
        if waiting is not None:
            waiting(None)

    def _got(self, asked, data: Optional[bytes]) -> None:
        if self._waiting is not asked:
            return                              # stopped, or it answered after its time
        self._timer.stop()
        self._waiting = None
        image = decode_image_bytes(data) if data and len(data) <= MAX_BYTES else None
        asked(image if image is not None and not image.isNull() else None)


__all__ = ["CoverFetch", "MAX_BYTES", "MAX_THUMB_SIDE", "TIMEOUT_MS", "cover_pixmap", "monogram",
           "png_bytes"]
