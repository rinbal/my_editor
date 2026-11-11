# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Crop…: a part of a picture, as a new file.

A sheet over the window shows the picture with a frame on it (as Photos'
crop does): drag inside the frame to move it, drag an edge or a corner to
size it, or choose a format (square, 3:2, ...). The arrow keys move the
frame, with Option (Alt) they size it. Crop makes a new file of the part
inside the frame; the picture's own file is never changed (SPEC 11 of the
photo grid design: a crop is a new address), so Undo brings the old one
back and every other document that shows it is untouched.

crop_bytes is the cut itself, without a view; CropSheet asks for the
frame and hands the new file's bytes to whoever opened it.
"""

from __future__ import annotations

import sys
from typing import List, Optional, Tuple

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QImage, QImageWriter, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QVBoxLayout, QWidget,
)

import image_safety
from i18n import _

# How the sheet shows the picture at most, and how close to an edge of the
# frame the pointer sizes it (points).
_VIEW = QSize(560, 400)
_GRIP = 8.0
# The smallest frame, in the picture's pixels.
_LEAST = 16
# Formats the new file keeps from the old one; anything else is written as PNG.
_KEPT = {"jpg": "jpg", "jpeg": "jpg", "png": "png", "webp": "webp"}
_JPEG_QUALITY = 92
# The pointer's shape over each grip of the frame (t, b, l, r: its edges).
_SHAPES = {"move": Qt.CursorShape.OpenHandCursor, "t": Qt.CursorShape.SizeVerCursor,
           "b": Qt.CursorShape.SizeVerCursor, "l": Qt.CursorShape.SizeHorCursor,
           "r": Qt.CursorShape.SizeHorCursor, "tl": Qt.CursorShape.SizeFDiagCursor,
           "br": Qt.CursorShape.SizeFDiagCursor, "tr": Qt.CursorShape.SizeBDiagCursor,
           "bl": Qt.CursorShape.SizeBDiagCursor}


def crop_bytes(data: bytes, area: QRect) -> Optional[Tuple[bytes, str]]:
    """The part ``area`` (the picture's pixels, upright) of the picture
    ``data`` as a new file's bytes, and its extension; None when the
    picture cannot be read (image_safety refuses it) or the part is empty."""
    image = image_safety.decode_image_bytes(data)
    if image is None:
        return None
    area = area.intersected(image.rect())
    if area.width() < 1 or area.height() < 1:
        return None
    kind = _KEPT.get((image_safety.sniff_image_ext(data) or "").lstrip(".").lower(), "png")
    if kind.encode() not in [bytes(f) for f in QImageWriter.supportedImageFormats()]:
        kind = "png"
    out = QByteArray()
    buffer = QBuffer(out)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    writer = QImageWriter(buffer, kind.encode())
    if kind == "jpg":
        writer.setQuality(_JPEG_QUALITY)
    if not writer.write(image.copy(area)):
        return None
    return bytes(out), kind


def _formats() -> List[Tuple[str, Optional[float]]]:
    """The frame's formats: free, the picture's own, then fixed ones."""
    return [(_("Free"), None), (_("Original"), 0.0), (_("Square"), 1.0), ("3:2", 1.5),
            ("4:3", 4 / 3), ("16:9", 16 / 9)]


class CropView(QWidget):
    """The picture, fitted, with the frame on it."""

    def __init__(self, image: QImage, parent: QWidget) -> None:
        super().__init__(parent)
        self._image = image
        self._pixmap = QPixmap.fromImage(image.scaled(
            _VIEW, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        self._scale = self._pixmap.width() / max(1, image.width())
        self.frame = QRectF(0, 0, image.width(), image.height())     # the picture's pixels
        self.ratio: Optional[float] = None
        self._drag: Optional[Tuple[str, QPointF, QRectF]] = None
        self.setFixedSize(self._pixmap.size())
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setAccessibleName(_("Crop area"))
        option = "Option" if sys.platform == "darwin" else "Alt"
        self.setAccessibleDescription(_("The arrow keys move the frame; with {key} they size "
                                        "it.").format(key=option))

    # ---- the frame ----

    def set_ratio(self, ratio: Optional[float]) -> None:
        """The frame's shape: None free, 0.0 the picture's own, else
        width over height; the largest such frame, centred."""
        width, height = self._image.width(), self._image.height()
        self.ratio = (width / height if ratio == 0.0 else ratio) if ratio is not None else None
        if self.ratio is None:
            self.frame = QRectF(0, 0, width, height)
        else:
            w = min(width, height * self.ratio)
            h = w / self.ratio
            self.frame = QRectF((width - w) / 2, (height - h) / 2, w, h)
        self.update()

    def area(self) -> QRect:
        """The frame in whole pixels of the picture."""
        return self.frame.toRect().intersected(self._image.rect())

    def _fit(self, frame: QRectF, anchor: str) -> QRectF:
        """``frame`` kept inside the picture, at least _LEAST, in its ratio."""
        bounds = QRectF(self._image.rect())
        w = max(_LEAST, min(frame.width(), bounds.width()))
        h = max(_LEAST, min(frame.height(), bounds.height()))
        if self.ratio is not None:
            if "l" in anchor or "r" in anchor or anchor == "move":
                h = w / self.ratio
            else:
                w = h * self.ratio
            if h > bounds.height():
                h = bounds.height()
                w = h * self.ratio
            if w > bounds.width():
                w = bounds.width()
                h = w / self.ratio
        left = frame.right() - w if "l" in anchor else frame.left()
        top = frame.bottom() - h if "t" in anchor else frame.top()
        left = min(max(bounds.left(), left), bounds.right() - w)
        top = min(max(bounds.top(), top), bounds.bottom() - h)
        return QRectF(left, top, w, h)

    def _shown(self) -> QRectF:
        s = self._scale
        f = self.frame
        return QRectF(f.x() * s, f.y() * s, f.width() * s, f.height() * s)

    def _grip_at(self, point: QPointF) -> str:
        shown = self._shown()
        near = shown.adjusted(-_GRIP, -_GRIP, _GRIP, _GRIP)
        if not near.contains(point):
            return ""
        grip = ""
        if abs(point.y() - shown.top()) <= _GRIP:
            grip += "t"
        elif abs(point.y() - shown.bottom()) <= _GRIP:
            grip += "b"
        if abs(point.x() - shown.left()) <= _GRIP:
            grip += "l"
        elif abs(point.x() - shown.right()) <= _GRIP:
            grip += "r"
        return grip or ("move" if shown.contains(point) else "")

    # ---- the pointer and the keys ----

    def mousePressEvent(self, event) -> None:
        grip = self._grip_at(event.position())
        if grip and event.button() == Qt.MouseButton.LeftButton:
            self._drag = (grip, event.position(), QRectF(self.frame))

    def mouseMoveEvent(self, event) -> None:
        if self._drag is None:
            self.setCursor(_SHAPES.get(self._grip_at(event.position()),
                                       Qt.CursorShape.ArrowCursor))
            return
        grip, start, frame = self._drag
        delta = (event.position() - start) / self._scale
        if grip == "move":
            self.frame = self._fit(frame.translated(delta), "move")
        else:
            moved = QRectF(frame)
            if "l" in grip:
                moved.setLeft(min(frame.right() - _LEAST, frame.left() + delta.x()))
            if "r" in grip:
                moved.setRight(max(frame.left() + _LEAST, frame.right() + delta.x()))
            if "t" in grip:
                moved.setTop(min(frame.bottom() - _LEAST, frame.top() + delta.y()))
            if "b" in grip:
                moved.setBottom(max(frame.top() + _LEAST, frame.bottom() + delta.y()))
            self.frame = self._fit(moved, grip)
        self.update()

    def mouseReleaseEvent(self, _event) -> None:
        self._drag = None

    def keyPressEvent(self, event) -> None:
        steps = {Qt.Key.Key_Left: (-1, 0), Qt.Key.Key_Right: (1, 0), Qt.Key.Key_Up: (0, -1),
                 Qt.Key.Key_Down: (0, 1)}
        if event.key() not in steps:
            super().keyPressEvent(event)
            return
        dx, dy = steps[event.key()]
        pace = (10 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1) / self._scale
        frame = QRectF(self.frame)
        if event.modifiers() & Qt.KeyboardModifier.AltModifier:
            frame.setWidth(frame.width() + dx * pace)
            frame.setHeight(frame.height() + dy * pace)
            self.frame = self._fit(frame, "br" if dx or dy else "move")
        else:
            self.frame = self._fit(frame.translated(dx * pace, dy * pace), "move")
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.drawPixmap(0, 0, self._pixmap)
        shown = self._shown()
        veil = QColor(0, 0, 0, 128)
        outside = QRectF(self.rect())
        for part in (QRectF(0, 0, outside.width(), shown.top()),
                     QRectF(0, shown.bottom(), outside.width(), outside.height() - shown.bottom()),
                     QRectF(0, shown.top(), shown.left(), shown.height()),
                     QRectF(shown.right(), shown.top(), outside.width() - shown.right(),
                            shown.height())):
            painter.fillRect(part, veil)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(255, 255, 255), 1.5))
        painter.drawRect(shown)
        if self.hasFocus():
            focus = self.palette().color(self.palette().ColorRole.Highlight)
            painter.setPen(QPen(focus, 3))
            painter.drawRect(shown.adjusted(-2, -2, 2, 2))
        painter.setPen(QPen(QColor(255, 255, 255), 3))
        for corner in (shown.topLeft(), shown.topRight(), shown.bottomLeft(), shown.bottomRight()):
            sx = 1 if corner.x() == shown.left() else -1
            sy = 1 if corner.y() == shown.top() else -1
            painter.drawLine(corner, corner + QPointF(12 * sx, 0))
            painter.drawLine(corner, corner + QPointF(0, 12 * sy))
        painter.end()


class CropSheet(QDialog):
    """Crop…, as a sheet of the window: the frame on the picture, its
    format, Cancel and Crop. ``cropped`` gets the new file's bytes and
    extension once Crop is chosen."""

    cropped = Signal(bytes, str)

    def __init__(self, parent: QWidget, data: bytes) -> None:
        super().__init__(parent)
        self._data = data
        self.setWindowTitle(_("Crop"))
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setWindowFlag(Qt.WindowType.Sheet, True)
        image = image_safety.decode_image_bytes(data)
        self.view = CropView(image if image is not None else QImage(), self)
        column = QVBoxLayout(self)
        column.setContentsMargins(20, 20, 20, 16)
        column.setSpacing(12)
        column.addWidget(self.view, 0, Qt.AlignmentFlag.AlignHCenter)
        row = QHBoxLayout()
        label = QLabel(_("Format:"))
        self.shape = QComboBox()
        for words, ratio in _formats():
            self.shape.addItem(words, ratio)
        label.setBuddy(self.shape)
        self.shape.currentIndexChanged.connect(
            lambda index: self.view.set_ratio(self.shape.itemData(index)))
        row.addWidget(label)
        row.addWidget(self.shape)
        row.addStretch(1)
        column.addLayout(row)
        note = QLabel(_("Makes a new file. The original stays as it is."))
        note.setWordWrap(True)
        faint = note.palette().color(note.palette().ColorRole.WindowText)
        faint.setAlphaF(0.65)
        note.setStyleSheet(f"color: {faint.name(QColor.NameFormat.HexArgb)};")
        column.addWidget(note)
        buttons = QDialogButtonBox()
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel).setText(_("Cancel"))
        self.crop_button = buttons.addButton(_("Crop"), QDialogButtonBox.ButtonRole.AcceptRole)
        self.crop_button.setDefault(True)
        buttons.accepted.connect(self._crop)
        buttons.rejected.connect(self.reject)
        column.addWidget(buttons)
        self.crop_button.setEnabled(image is not None)
        self.shape.setEnabled(image is not None)
        self.view.setFocus(Qt.FocusReason.OtherFocusReason)

    def _crop(self) -> None:
        found = crop_bytes(self._data, self.view.area())
        if found is None:
            self.reject()
            return
        self.accept()
        self.cropped.emit(*found)
