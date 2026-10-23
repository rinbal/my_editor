# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Media Library's grid: its tiles, how one is drawn, and the view.

A tile is a file in the library or a file being added to it. It shows
the picture (or the kind of file), the name, and one line under it: the
day it was added, or, for a file that did not upload, why. A private
file, readable only by the person, has a small lock in the thumbnail's
corner; the public ones, the normal case, have nothing. A file being added is dimmed under a ring that fills as it
uploads, with a × in the ring that cancels; one that failed has Retry.

The grid scrolls (no pages) and asks for thumbnails only for the tiles
in view, plus one screen ahead (``visible_keys``). Keyboard, as in the
Finder: arrows move, Space is Quick Look, Return opens, Delete or
Command-Backspace deletes, Command-C copies the link, Command-V adds a
pasted image. Files dragged onto the grid are added; a file's tile
dragged out carries its address, which a document takes as an image.

Colours come from the palette and ``theme``; the only fixed ones are the
scrims laid over a thumbnail, which sit on the picture and not on the
window, so they are the same in light and dark (as Photos draws them).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from PySide6.QtCore import (
    QAbstractListModel,
    QMimeData,
    QModelIndex,
    QPointF,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import QAbstractItemView, QListView, QStyle, QStyledItemDelegate

import format_icons
import media_files
from . import appearance
from i18n import _

from ..blossom.store import MediaFile
from ..blossom.upload_progress import UploadProgress
from .file_icons import FileIcons

# The thumbnail's side, and the cell around it.
THUMB = 128
_PAD = 6
_CAPTION_LINES = 3
CELL_WIDTH = THUMB + 2 * _PAD + 12

# A tile's states.
FILE = "file"
WAITING = "waiting"       # added, not sending yet (being read, or the signer)
SENDING = "sending"       # the bytes are on their way; Cancel stops it
FINISHING = "finishing"   # a server has it; copies to the others follow
FAILED = "failed"         # did not upload; Retry or Remove

# Scrims over a thumbnail, and what is drawn on them.
_SCRIM = QColor(0, 0, 0, 120)
_BADGE = QColor(0, 0, 0, 150)
_ON_SCRIM = QColor(255, 255, 255)
_ON_SCRIM_FAINT = QColor(255, 255, 255, 90)
# The indeterminate ring: an arc this long, round once in this many seconds.
_SPINNER_DEGREES = 90
_SPIN_SECONDS = 1.0
_PILL = QColor(255, 255, 255, 235)
_ON_PILL = QColor(0, 0, 0, 220)

TILE_ROLE = Qt.ItemDataRole.UserRole + 1


def kind_word(kind: str) -> str:
    """The kind of a file, as a screen reader says it."""
    return {"image": _("image"), "video": _("video"), "audio": _("audio")}.get(kind, _("file"))


@dataclass(eq=False)
class Tile:
    """One tile. ``key`` is a file's sha256, or the job name of a file
    being added."""

    key: str
    name: str
    detail: str = ""
    kind: str = "file"
    state: str = FILE
    private: bool = False
    # How far a file being added has got (its upload's own indicator);
    # None while there is nothing to measure yet.
    meter: Optional[UploadProgress] = None
    media: Optional[MediaFile] = None
    pixmap: Optional[QPixmap] = None
    # A picture that cannot be drawn here (HEIC on most systems).
    no_picture: bool = False
    mime: str = ""

    @property
    def is_file(self) -> bool:
        return self.state == FILE

    @property
    def in_flight(self) -> bool:
        """Whether it is being added (its indicator moves)."""
        return self.state in (WAITING, SENDING, FINISHING)

    def accessible_text(self) -> str:
        parts = [self.name, kind_word(self.kind)]
        if self.private:
            parts.append(_("private"))
        if self.in_flight:
            parts.append(_("uploading"))
        elif self.state == FAILED:
            parts.append(_("couldn’t upload: {reason}").format(reason=self.detail))
        return ", ".join(parts)

    def tooltip(self) -> str:
        lines = [self.name]
        if self.state == FAILED:
            lines.append(self.detail)
        elif self.private:
            lines.append(_("Only you can see this"))
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #

class MediaModel(QAbstractListModel):
    """The tiles, in the order shown."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._tiles: List[Tile] = []
        self._rows: Dict[str, int] = {}

    @property
    def tiles(self) -> List[Tile]:
        return list(self._tiles)

    def set_tiles(self, tiles: List[Tile]) -> None:
        self.beginResetModel()
        self._tiles = list(tiles)
        self._rows = {tile.key: row for row, tile in enumerate(self._tiles)}
        self.endResetModel()

    def tile(self, key: str) -> Optional[Tile]:
        row = self._rows.get(key)
        return self._tiles[row] if row is not None else None

    def row_of(self, key: str) -> int:
        return self._rows.get(key, -1)

    def changed(self, key: str) -> None:
        row = self._rows.get(key)
        if row is not None:
            index = self.index(row)
            self.dataChanged.emit(index, index)

    # -- the model's surface --------------------------------------------------

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._tiles)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._tiles):
            return None
        tile = self._tiles[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return tile.name
        if role == Qt.ItemDataRole.AccessibleTextRole:
            return tile.accessible_text()
        if role == Qt.ItemDataRole.ToolTipRole:
            return tile.tooltip()
        if role == TILE_ROLE:
            return tile
        return None

    def flags(self, index):
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if index.isValid() and self._tiles[index.row()].is_file:
            flags |= Qt.ItemFlag.ItemIsDragEnabled
        return flags

    def supportedDragActions(self):  # noqa: N802
        return Qt.DropAction.CopyAction

    def mimeTypes(self):  # noqa: N802
        return ["text/uri-list", "text/plain"]

    def mimeData(self, indexes):  # noqa: N802
        """A file dragged out is its address, as a link from a browser
        is: a document takes it as the image, any other app as a link."""
        urls = [self._tiles[i.row()].media.url for i in indexes
                if i.isValid() and self._tiles[i.row()].media is not None]
        data = QMimeData()
        data.setUrls([QUrl(url) for url in urls])
        data.setText("\n".join(urls))
        return data


# --------------------------------------------------------------------------- #
# How a tile is drawn
# --------------------------------------------------------------------------- #

class TileDelegate(QStyledItemDelegate):
    """Draws a tile; knows where its × and its Retry are."""

    def __init__(self, view: "MediaGrid", *, is_dark: bool) -> None:
        super().__init__(view)
        self._view = view
        self._is_dark = is_dark
        self._file_icons = FileIcons()

    def cell_size(self) -> QSize:
        line = QFontMetrics(self._view.font()).lineSpacing()
        return QSize(CELL_WIDTH, _PAD * 3 + THUMB + _CAPTION_LINES * line + 4)

    def sizeHint(self, _option, _index) -> QSize:  # noqa: N802
        return self.cell_size()

    # -- geometry ---------------------------------------------------------------

    @staticmethod
    def thumb_rect(cell: QRect) -> QRect:
        return QRect(cell.center().x() - THUMB // 2 + 1, cell.top() + _PAD, THUMB, THUMB)

    def cancel_rect(self, cell: QRect) -> QRect:
        """The ring of a file being sent; a click there cancels."""
        thumb = self.thumb_rect(cell)
        return QRect(thumb.center().x() - 20, thumb.center().y() - 20, 40, 40)

    def retry_rect(self, cell: QRect) -> QRect:
        """The Retry button on a file that did not upload."""
        return self._retry_in(self.thumb_rect(cell))

    def _retry_in(self, thumb: QRect) -> QRect:
        metrics = QFontMetrics(self._view.font())
        width = metrics.horizontalAdvance(_("Retry")) + 24
        height = metrics.height() + 8
        return QRect(thumb.center().x() - width // 2, thumb.bottom() - height - 10,
                     width, height)

    # -- painting ---------------------------------------------------------------

    def paint(self, painter: QPainter, option, index) -> None:
        tile: Tile = index.data(TILE_ROLE)
        if tile is None:
            return
        palette = option.palette
        cell = QRect(option.rect)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        if selected:
            tint = QColor(palette.color(QPalette.ColorRole.Highlight))
            tint.setAlpha(70 if self._view.hasFocus() else 40)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(tint)
            painter.drawRoundedRect(QRectF(cell).adjusted(2, 2, -2, -2), 8, 8)
        thumb = self.thumb_rect(cell)
        self._paint_thumb(painter, palette, thumb, tile)
        self._paint_caption(painter, palette, cell, thumb, tile)
        if option.state & QStyle.StateFlag.State_HasFocus and self._view.hasFocus():
            painter.setPen(QPen(QColor(appearance.roles(self._is_dark).focus), 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(QRectF(cell).adjusted(3, 3, -3, -3), 8, 8)
        painter.restore()

    def _paint_thumb(self, painter: QPainter, palette, thumb: QRect, tile: Tile) -> None:
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(thumb), 6, 6)
        painter.save()
        painter.setClipPath(clip)
        painter.fillRect(thumb, palette.color(QPalette.ColorRole.AlternateBase))
        if tile.pixmap is not None and not tile.pixmap.isNull():
            # Filled and cropped to the square, as Photos lays a grid out:
            # every tile the same shape, whatever the picture's.
            source = QRectF(tile.pixmap.rect())
            side = min(source.width(), source.height())
            source = QRectF(source.center().x() - side / 2, source.center().y() - side / 2,
                            side, side)
            if tile.kind == "file":
                source = QRectF(tile.pixmap.rect())
                size = tile.pixmap.size().scaled(thumb.size(), Qt.AspectRatioMode.KeepAspectRatio)
                target = QRect(thumb)
                target.setSize(size)
                target.moveCenter(thumb.center())
                painter.drawPixmap(QRectF(target), tile.pixmap, source)
            else:
                painter.drawPixmap(QRectF(thumb), tile.pixmap, source)
        elif tile.state == FILE and (tile.kind != "image" or tile.no_picture):
            glyph = media_files.file_glyph(tile.mime)
            side = 44
            rect = QRectF(thumb.center().x() - side / 2, thumb.center().y() - side / 2,
                          side, side)
            icon = self._file_icons.icon(tile.name, tile.mime) if tile.kind == "file" else None
            if icon is not None and not icon.isNull():
                icon.paint(painter, rect.toRect())
            else:
                format_icons.paint(glyph, painter, rect,
                                   palette.color(QPalette.ColorRole.PlaceholderText))
            if tile.kind == "file":
                suffix = media_files.file_extension(tile.name, tile.mime).lstrip(".").upper()
                if suffix:
                    painter.setPen(palette.color(QPalette.ColorRole.Text))
                    painter.drawText(thumb.adjusted(8, THUMB - 30, -8, -8),
                                     Qt.AlignmentFlag.AlignCenter, suffix)
        if tile.state != FILE:
            painter.fillRect(thumb, _SCRIM)
        painter.restore()

        if tile.in_flight:
            self._paint_ring(painter, thumb, tile)
        elif tile.state == FAILED:
            side = 30
            centre = QPointF(thumb.center().x(), thumb.top() + THUMB * 0.38)
            format_icons.paint("alert", painter,
                               QRectF(centre.x() - side / 2, centre.y() - side / 2, side, side),
                               _ON_SCRIM)
            pill = QRectF(self._retry_in(thumb))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(_PILL)
            painter.drawRoundedRect(pill, pill.height() / 2, pill.height() / 2)
            painter.setPen(_ON_PILL)
            painter.drawText(pill, Qt.AlignmentFlag.AlignCenter, _("Retry"))
        if tile.private and tile.state == FILE:
            badge = QRectF(thumb.left() + 6, thumb.bottom() - 6 - 22, 22, 22)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(_BADGE)
            painter.drawEllipse(badge)
            format_icons.paint("lock", painter, badge.adjusted(4, 4, -4, -4), _ON_SCRIM)

    def _paint_ring(self, painter: QPainter, thumb: QRect, tile: Tile) -> None:
        """The upload's indicator: a ring that fills clockwise as it goes,
        or, while nothing can be measured, a spinning arc (one shape for
        both, as Apple asks of an indicator that turns determinate)."""
        ring = QRectF(thumb.center().x() - 16, thumb.center().y() - 16, 32, 32)
        # A fixed dusk backing keeps the violet meter legible over any picture.
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(appearance.roles(True).dusk))
        painter.drawEllipse(ring.adjusted(-5, -5, 5, 5))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(_ON_SCRIM_FAINT, 3))
        painter.drawEllipse(ring)
        fraction = tile.meter.fraction() if tile.meter is not None else None
        pen = QPen(QColor(appearance.roles(True).icon), 3)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        if fraction is None:
            start = 90 - (time.monotonic() % _SPIN_SECONDS) / _SPIN_SECONDS * 360
            painter.drawArc(ring, int(start * 16), -_SPINNER_DEGREES * 16)
        elif fraction > 0:
            painter.drawArc(ring, 90 * 16, -int(fraction * 360 * 16))
        if tile.state != FINISHING:
            pen = QPen(_ON_SCRIM, 2)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            c = ring.center()
            painter.drawLine(QPointF(c.x() - 5, c.y() - 5), QPointF(c.x() + 5, c.y() + 5))
            painter.drawLine(QPointF(c.x() + 5, c.y() - 5), QPointF(c.x() - 5, c.y() + 5))

    def _paint_caption(self, painter: QPainter, palette, cell: QRect, thumb: QRect,
                       tile: Tile) -> None:
        font = self._view.font()
        metrics = QFontMetrics(font)
        line = metrics.lineSpacing()
        width = cell.width() - 2 * _PAD
        top = thumb.bottom() + _PAD
        painter.setFont(font)
        painter.setPen(palette.color(QPalette.ColorRole.Text))
        name = metrics.elidedText(tile.name, Qt.TextElideMode.ElideMiddle, width)
        painter.drawText(QRect(cell.left() + _PAD, top, width, line),
                         Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter, name)
        if not tile.detail:
            return
        small = QFont(font)
        small.setPointSizeF(max(8.0, font.pointSizeF() - 1))
        painter.setFont(small)
        small_metrics = QFontMetrics(small)
        painter.setPen(QColor(appearance.roles(self._is_dark).error) if tile.state == FAILED
                       else palette.color(QPalette.ColorRole.PlaceholderText))
        # A failure's first sentence says what happened; the rest (what
        # to do) is in the tooltip and in the line at the window's foot.
        detail = _first_sentence(tile.detail) if tile.state == FAILED else tile.detail
        lines = _wrap(detail, small_metrics, width, _CAPTION_LINES - 1)
        for number, text in enumerate(lines):
            painter.drawText(QRect(cell.left() + _PAD, top + line * (number + 1), width, line),
                             Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter, text)


def _first_sentence(text: str) -> str:
    head, dot, _rest = text.partition(". ")
    return head + "." if dot else text


def _wrap(text: str, metrics: QFontMetrics, width: int, most: int) -> List[str]:
    """``text`` in lines no wider than ``width``, at most ``most`` of
    them, the last elided."""
    words = text.split()
    lines: List[str] = []
    current = ""
    for word in words:
        trial = f"{current} {word}".strip()
        if metrics.horizontalAdvance(trial) <= width or not current:
            current = trial
            continue
        lines.append(current)
        current = word
    if current:
        lines.append(current)
    if len(lines) > most:
        rest = " ".join(lines[most - 1:])
        lines = lines[:most - 1] + [rest]
    if lines:
        lines[-1] = metrics.elidedText(lines[-1], Qt.TextElideMode.ElideRight, width)
    return lines


# --------------------------------------------------------------------------- #
# The view
# --------------------------------------------------------------------------- #

class MediaGrid(QListView):
    """The scrolling grid of tiles."""

    open_requested = Signal()
    quick_look_requested = Signal()
    delete_requested = Signal()
    copy_requested = Signal()
    save_requested = Signal()
    paste_requested = Signal()
    files_dropped = Signal(list)          # local paths
    cancel_requested = Signal(str)        # job name
    retry_requested = Signal(str)         # job name
    visible_changed = Signal()

    def apply_theme(self, is_dark: bool) -> None:
        self._is_dark = is_dark
        palette = appearance.palette(is_dark, surface="list")
        palette.setColor(QPalette.Base, QColor(appearance.roles(is_dark).list))
        self.setPalette(palette)
        self.delegate._is_dark = is_dark
        self.viewport().update()

    def __init__(self, *, is_dark: bool, parent=None) -> None:
        super().__init__(parent)
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setMovement(QListView.Movement.Static)
        self.setUniformItemSizes(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setMouseTracking(True)
        self.setFrameShape(QListView.Shape.NoFrame)
        self.setAccessibleName(_("Media"))
        self._is_dark = is_dark
        self._drop_active = False
        self.delegate = TileDelegate(self, is_dark=is_dark)
        self.apply_theme(is_dark)
        self.setItemDelegate(self.delegate)
        self.setGridSize(self.delegate.cell_size())
        self.setMinimumWidth(2 * CELL_WIDTH + self.verticalScrollBar().sizeHint().width() + 8)
        self.doubleClicked.connect(lambda _index: self.open_requested.emit())
        # Thumbnails are asked for as tiles come into view, once the
        # scrolling or resizing has settled.
        self._visible_timer = QTimer(self)
        self._visible_timer.setSingleShot(True)
        self._visible_timer.setInterval(30)
        self._visible_timer.timeout.connect(self.visible_changed)
        self.verticalScrollBar().valueChanged.connect(lambda _v: self._visible_timer.start())

    def setModel(self, model) -> None:  # noqa: N802
        super().setModel(model)
        model.modelReset.connect(self._visible_timer.start)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        # The columns share the width, so a window between two column
        # counts has no ragged gap at the right.
        cell = self.delegate.cell_size()
        width = self._row_width()
        columns = max(1, width // cell.width())
        size = QSize(max(cell.width(), width // columns), cell.height())
        if size != self.gridSize():
            self.setGridSize(size)
        self._visible_timer.start()

    def _row_width(self) -> int:
        """The width a row of tiles may fill: the width Qt lays them out in.

        Qt keeps the scroll bar's room whether or not the bar shows, and
        wraps a tile that would reach the last pixel. Filling the viewport
        instead made a full row wrap, the extra row brought the scroll bar,
        the bar narrowed the row until it fit, the bar went, and round
        again, thousands of times a second. This width is the same with
        and without the bar, so the layout settles."""
        bar = self.verticalScrollBar()
        overlaps = self.style().pixelMetric(QStyle.PixelMetric.PM_ScrollView_ScrollBarOverlap,
                                            None, bar)
        extent = 0 if overlaps else self.style().pixelMetric(
            QStyle.PixelMetric.PM_ScrollBarExtent, None, bar)
        return self.maximumViewportSize().width() - extent - 1

    # -- what is in view ----------------------------------------------------

    def visible_keys(self) -> List[str]:
        """The tiles in view and one screen below, top to bottom."""
        model = self.model()
        if model is None:
            return []
        area = self.viewport().rect().adjusted(0, 0, 0, self.viewport().height())
        keys = []
        for row in range(model.rowCount()):
            index = model.index(row)
            rect = self.visualRect(index)
            if rect.top() > area.bottom():
                break
            if rect.intersects(area):
                keys.append(index.data(TILE_ROLE).key)
        return keys

    def selected_tiles(self) -> List[Tile]:
        rows = sorted(index.row() for index in self.selectionModel().selectedIndexes())
        model = self.model()
        return [model.index(row).data(TILE_ROLE) for row in rows]

    # -- keys -----------------------------------------------------------------

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        if key == Qt.Key.Key_Space and not event.modifiers():
            self.quick_look_requested.emit()
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.open_requested.emit()
        elif key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            # Delete, or Backspace (the Mac's Delete key) with Command or
            # without, as MyEditor 3.3 had it; it asks first.
            self.delete_requested.emit()
        elif event.matches(QKeySequence.StandardKey.Copy):
            self.copy_requested.emit()
        elif event.matches(QKeySequence.StandardKey.Save):
            self.save_requested.emit()
        elif event.matches(QKeySequence.StandardKey.Paste):
            self.paste_requested.emit()
        else:
            super().keyPressEvent(event)

    # -- the ring's × and Retry -------------------------------------------------

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            index = self.indexAt(event.position().toPoint())
            tile = index.data(TILE_ROLE) if index.isValid() else None
            if tile is not None:
                cell = self.visualRect(index)
                point = event.position().toPoint()
                if tile.state in (WAITING, SENDING) and \
                        self.delegate.cancel_rect(cell).contains(point):
                    self.cancel_requested.emit(tile.key)
                    return
                if tile.state == FAILED and self.delegate.retry_rect(cell).contains(point):
                    self.retry_requested.emit(tile.key)
                    return
        super().mouseReleaseEvent(event)

    # -- files dropped in -------------------------------------------------------

    def _local_paths(self, event) -> List[str]:
        data = event.mimeData()
        if event.source() is self or not data.hasUrls():
            return []
        return [url.toLocalFile() for url in data.urls() if url.isLocalFile()]

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if self._local_paths(event):
            event.acceptProposedAction()
            self._set_drop_active(True)
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if self._local_paths(event):
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self._set_drop_active(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802
        paths = self._local_paths(event)
        self._set_drop_active(False)
        if not paths:
            super().dropEvent(event)
            return
        event.acceptProposedAction()
        self.files_dropped.emit(paths)

    def _set_drop_active(self, active: bool) -> None:
        if self._drop_active != active:
            self._drop_active = active
            self.viewport().update()

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        if self._drop_active:
            painter = QPainter(self.viewport())
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QPen(QColor(appearance.roles(self._is_dark).focus), 3))
            painter.drawRoundedRect(QRectF(self.viewport().rect()).adjusted(2, 2, -2, -2), 8, 8)
            painter.end()
