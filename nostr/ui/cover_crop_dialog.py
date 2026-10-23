# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A live cover crop with direct positioning and equivalent keyboard controls."""

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPalette, QPen
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel,
    QPushButton, QSlider, QVBoxLayout, QWidget,
)

import announce
from alerts import answer_return_as_the_system_does
from . import appearance
from i18n import _
from popup_placement import fitting_size
from ..media.cover_crop import WIDE_ASPECT, crop_rect, export_cover, output_size
from .flow_layout import FlowLayout
from .publish_sheet import make_sheet, scroll_area, sheet_stylesheet


class CropCanvas(QWidget):
    """Show the whole source with a crop frame; drag or use arrows to move it."""

    changed = Signal()

    def __init__(self, image: QImage, parent=None):
        super().__init__(parent)
        self.image = image
        self.aspect = WIDE_ASPECT
        self.focus = QPointF(0.5, 0.5)
        self.zoom = 1.0
        self._drag = None
        self.setMinimumSize(200, 140)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setAccessibleName(_("Cover framing"))
        self.setAccessibleDescription(_("Drag the frame to position the cover, or use the arrow keys."))

    def source_rect(self) -> QRectF:
        return crop_rect(self.image.size(), self.aspect, self.focus, self.zoom)

    def image_rect(self) -> QRectF:
        area = QRectF(self.rect()).adjusted(12, 12, -12, -12)
        size = self.image.size().scaled(area.size().toSize(), Qt.AspectRatioMode.KeepAspectRatio)
        return QRectF(area.center().x() - size.width() / 2,
                      area.center().y() - size.height() / 2, size.width(), size.height())

    def frame_rect(self) -> QRectF:
        area, source = self.image_rect(), self.source_rect()
        scale = area.width() / self.image.width()
        return QRectF(area.left() + source.left() * scale, area.top() + source.top() * scale,
                      source.width() * scale, source.height() * scale)

    def set_framing(self, *, aspect=None, zoom=None):
        if aspect is not None:
            self.aspect = aspect
        if zoom is not None:
            self.zoom = zoom
        self._move_focus(self.focus)

    def reset(self):
        self.zoom = 1.0
        self._move_focus(QPointF(0.5, 0.5))

    def _move_focus(self, point):
        rect = crop_rect(self.image.size(), self.aspect, point, self.zoom)
        self.focus = QPointF(rect.center().x() / self.image.width(),
                            rect.center().y() / self.image.height())
        self.update()
        self.changed.emit()

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.fillRect(self.rect(), self.palette().brush(QPalette.ColorRole.Base))
        area, frame = self.image_rect(), self.frame_rect()
        painter.drawImage(area, self.image)
        # Only excluded pixels are dimmed, so the selected crop remains true.
        shade = QColor(0, 0, 0, 150)
        for rect in (QRectF(area.left(), area.top(), area.width(), frame.top() - area.top()),
                     QRectF(area.left(), frame.bottom(), area.width(), area.bottom() - frame.bottom()),
                     QRectF(area.left(), frame.top(), frame.left() - area.left(), frame.height()),
                     QRectF(frame.right(), frame.top(), area.right() - frame.right(), frame.height())):
            painter.fillRect(rect, shade)
        painter.setPen(QPen(QColor(255, 255, 255, 110), 1))
        for fraction in (1 / 3, 2 / 3):
            x = frame.left() + frame.width() * fraction
            y = frame.top() + frame.height() * fraction
            painter.drawLine(QPointF(x, frame.top()), QPointF(x, frame.bottom()))
            painter.drawLine(QPointF(frame.left(), y), QPointF(frame.right(), y))
        painter.setPen(QPen(QColor("white"), 2))
        painter.drawRect(frame)
        if self.hasFocus():
            painter.setPen(QPen(self.palette().color(QPalette.ColorRole.Highlight), 2))
            painter.drawRect(QRectF(self.rect()).adjusted(2, 2, -2, -2))
        painter.end()

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or not self.image_rect().contains(event.position()):
            super().mousePressEvent(event)
            return
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        if not self.frame_rect().contains(event.position()):
            area = self.image_rect()
            self._move_focus(QPointF((event.position().x() - area.left()) / area.width(),
                                    (event.position().y() - area.top()) / area.height()))
        self._drag = (event.position(), QPointF(self.focus))
        self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, event):
        if self._drag is None:
            super().mouseMoveEvent(event)
            return
        start, focus = self._drag
        delta, area = event.position() - start, self.image_rect()
        self._move_focus(focus + QPointF(delta.x() / area.width(), delta.y() / area.height()))

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag = None
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        direction = {Qt.Key.Key_Left: (-1, 0), Qt.Key.Key_Right: (1, 0),
                     Qt.Key.Key_Up: (0, -1), Qt.Key.Key_Down: (0, 1)}.get(event.key())
        if direction is None:
            super().keyPressEvent(event)
            return
        step = 0.05 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 0.01
        self._move_focus(self.focus + QPointF(direction[0] * step, direction[1] * step))
        announce.announce(self, _("Cover center: {x}% across, {y}% down.").format(
            x=round(self.focus.x() * 100), y=round(self.focus.y() * 100)))
        event.accept()


class CoverCropDialog(QDialog):
    """Applying creates bytes only; the field owns upload and document changes."""

    def __init__(self, image: QImage, *, is_dark: bool, jpeg: bool = False, parent=None):
        super().__init__(parent)
        self.setWindowTitle(_("Adjust Cover Image"))
        make_sheet(self)
        self.apply_theme(is_dark)
        appearance.bind(self)
        self._jpeg = jpeg
        self.cover = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 14)
        page = QWidget()
        column = QVBoxLayout(page)
        column.setContentsMargins(0, 0, 0, 0)
        title = QLabel(_("Choose the part readers will see"))
        title.setWordWrap(True)
        column.addWidget(title)
        self.canvas = CropCanvas(image)
        column.addWidget(self.canvas, 1)
        hint = QLabel(_("Drag the frame to keep your subject in view. Arrow keys move it; Shift moves faster."))
        hint.setWordWrap(True)
        column.addWidget(hint)
        controls = QFormLayout()
        controls.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.format = QComboBox()
        self.format.addItem(_("Wide · 16:9 (recommended)"), WIDE_ASPECT)
        self.format.addItem(_("Landscape · 3:2"), 3 / 2)
        self.format.addItem(_("Square · 1:1"), 1.0)
        self.format.setAccessibleName(_("Cover format"))
        self.format.view().setAccessibleName(_("Cover format"))
        self.format.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.format.setMinimumContentsLength(14)
        self.format.currentIndexChanged.connect(self._format_changed)
        controls.addRow(_("Format:"), self.format)
        self.zoom = QSlider(Qt.Orientation.Horizontal)
        self.zoom.setRange(100, 300)
        self.zoom.setValue(100)
        self.zoom.setAccessibleName(_("Cover zoom"))
        self.zoom.valueChanged.connect(lambda value: self.canvas.set_framing(zoom=value / 100))
        zoom_row = QWidget()
        zoom_layout = QHBoxLayout(zoom_row)
        zoom_layout.setContentsMargins(0, 0, 0, 0)
        zoom_layout.addWidget(self.zoom, 1)
        self.zoom_value = QLabel("100 %")
        self.zoom.valueChanged.connect(lambda value: self.zoom_value.setText(f"{value} %"))
        zoom_layout.addWidget(self.zoom_value)
        controls.addRow(_("Zoom:"), zoom_row)
        column.addLayout(controls)
        self.dimensions = QLabel()
        self.dimensions.setWordWrap(True)
        column.addWidget(self.dimensions)
        self.note = QLabel()
        self.note.setWordWrap(True)
        muted = appearance.roles(is_dark).muted
        self.note.setStyleSheet(f"color: {muted};")
        column.addWidget(self.note)
        self.disclosure = disclosure = QLabel(_("This uploads a new still cover image. The original stays in your library. "
                              "Other Nostr apps may frame covers differently."))
        disclosure.setWordWrap(True)
        disclosure.setStyleSheet(f"color: {muted};")
        column.addWidget(disclosure)
        scroll = scroll_area(page)
        layout.addWidget(scroll, 1)
        row = FlowLayout(spacing=8, trailing=True)
        self.reset_button = QPushButton(_("Reset"))
        self.reset_button.setAutoDefault(False)
        self.reset_button.clicked.connect(self._reset)
        row.addWidget(self.reset_button)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.apply_button = self.buttons.addButton(_("Use Cover"), QDialogButtonBox.ButtonRole.AcceptRole)
        self.apply_button.setDefault(True)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        row.addWidget(self.buttons)
        layout.addLayout(row)
        answer_return_as_the_system_does(self.findChildren(QPushButton))
        for button in (self.reset_button, *self.buttons.buttons()):
            button.ensurePolished()
            button.setMinimumWidth(button.sizeHint().width())
        self.canvas.changed.connect(self._update_dimensions)
        self._update_dimensions()
        # An image editor needs more room than the compact publishing sheet.
        # Keep it on screen; its content scrolls when manually made smaller.
        self.resize(fitting_size(self, QSize(640, 680)))
        self.apply_theme(is_dark)

    def apply_theme(self, is_dark: bool) -> None:
        self._is_dark = is_dark
        self.setStyleSheet(sheet_stylesheet(is_dark))
        appearance.surface(self, is_dark)
        if hasattr(self, "note"):
            for label in (self.note, self.disclosure):
                label.setStyleSheet(f"color: {appearance.roles(is_dark).muted};")
            self.canvas.setPalette(appearance.palette(is_dark))
            self.canvas.update()

    def _format_changed(self, _index):
        self.canvas.set_framing(aspect=self.format.currentData())

    def _reset(self):
        self.format.setCurrentIndex(0)
        self.zoom.setValue(100)
        self.canvas.reset()

    def _update_dimensions(self):
        size = output_size(self.canvas.source_rect())
        self.dimensions.setText(_("New cover: {width} × {height} px").format(
            width=size.width(), height=size.height()))
        self.note.setText(_("A larger source will look sharper. For a wide cover, aim for 1600 × 900 px.")
                          if self.canvas.aspect == WIDE_ASPECT and size.width() < 1600 else "")
        self.note.setVisible(bool(self.note.text()))

    def accept(self):
        try:
            self.cover = export_cover(self.canvas.image, self.canvas.source_rect(), jpeg=self._jpeg)
        except ValueError:
            self.note.setText(_("This cover could not be prepared. Try another picture."))
            self.note.show()
            return
        super().accept()
