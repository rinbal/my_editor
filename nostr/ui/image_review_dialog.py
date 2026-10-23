# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Choose which images an import copies to your media server.

A sheet with every image the chosen posts would copy, as thumbnails. A
checked image is copied, so the draft keeps working if the website
removes it (the default); an unchecked one stays where it is, linked
from the draft. The sheet returns the addresses left unchecked (the
*skip set*), which the import hands to the pipeline.

The list is the one the import works from (snapshots.images_to_copy:
each post's cover and the images of the Markdown it makes), so every
image that would be copied can be reviewed, and an unchecked one is
really left alone.

Thumbnails come from an image source that fetches them through the
importer's network guard (imports/remote_images.py); until one arrives,
its tile shows an empty frame. Space checks or unchecks the tile in
focus, as in any checklist.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Set

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QListView,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

import theme
from alerts import answer_return_as_the_system_does
from . import appearance
from i18n import _, ngettext

from ..imports.images import image_label

TILE = 96


class ImageReviewDialog(QDialog):
    """Thumbnails of image addresses; unchecked ones join the skip set."""

    def __init__(
        self,
        images: List[str],
        skip_urls: Iterable[str] = (),
        parent: Optional[QWidget] = None,
        *,
        image_source=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(_("Review Images"))
        self.setObjectName("image_review")
        # A sheet on the window it belongs to.
        self.setWindowModality(Qt.WindowModality.WindowModal)
        # The app's dialog look: the default button in the accent color.
        self.apply_theme(theme.is_dark_active())
        appearance.bind(self)
        self.resize(600, 460)
        if image_source is None:
            from ..imports.remote_images import RemoteImages
            image_source = RemoteImages(parent=self)
        self._images = image_source
        self._items: Dict[str, QListWidgetItem] = {}

        skip = set(skip_urls or ())
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(10)

        hint = QLabel(_(
            "Checked images are copied to your media server, so the draft keeps "
            "working if the website removes them. Uncheck any you'd rather leave "
            "where they are."))
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self._count = QLabel()
        self._count.setObjectName("image_review_count")
        layout.addWidget(self._count)

        self._list = QListWidget()
        self._list.setObjectName("image_review_list")
        self._list.setAccessibleName(_("Images to copy"))
        self._list.setViewMode(QListView.ViewMode.IconMode)
        self._list.setIconSize(QSize(TILE, TILE))
        self._list.setGridSize(QSize(TILE + 28, TILE + 44))
        self._list.setResizeMode(QListView.ResizeMode.Adjust)
        self._list.setMovement(QListView.Movement.Static)
        self._list.setWordWrap(True)
        self._list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        placeholder = self._placeholder()
        # Before any is asked for: one already at hand answers at once.
        self._images.ready.connect(self._set_thumbnail)
        for url in images:
            row = QListWidgetItem(placeholder, image_label(url))
            row.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled
                         | Qt.ItemFlag.ItemIsSelectable)
            row.setCheckState(Qt.CheckState.Unchecked if url in skip
                              else Qt.CheckState.Checked)
            row.setData(Qt.ItemDataRole.UserRole, url)
            row.setToolTip(url)
            row.setData(Qt.ItemDataRole.AccessibleTextRole, image_label(url))
            self._list.addItem(row)
            self._items[url] = row
            if self._images.image(url) is not None:
                self._set_thumbnail(url)
            # Decoded no larger than a tile needs (a sharper one when the
            # cache only has a smaller one).
            side = int(TILE * self.devicePixelRatioF())
            self._images.request(url, QSize(side, side))
        self._list.itemChanged.connect(self._update_count)
        if self._list.count():
            self._list.setCurrentRow(0)
        layout.addWidget(self._list, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.addButton(_("Done"), QDialogButtonBox.ButtonRole.AcceptRole)
        answer_return_as_the_system_does(buttons.buttons())
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._update_count()
        self.apply_theme(self._is_dark)

    def apply_theme(self, is_dark: bool) -> None:
        self._is_dark = is_dark
        self.setStyleSheet(appearance.dialog_stylesheet(is_dark))
        appearance.surface(self, is_dark)
        if hasattr(self, "_list"):
            r = appearance.roles(is_dark)
            self._list.setStyleSheet(f"""
                QListWidget {{ background: {r.list}; color: {r.text};
                    border: 1px solid {r.edge}; selection-background-color: {r.selection};
                    selection-color: #FFFFFF; }}
                QListWidget::item:selected:!active {{ background: {r.soft}; color: {r.text}; }}
            """)
            self._list.setPalette(appearance.palette(is_dark, surface="list"))

    def skip_urls(self) -> Set[str]:
        """Addresses left unchecked: these stay where they are."""
        skipped: Set[str] = set()
        for index in range(self._list.count()):
            row = self._list.item(index)
            if row.checkState() != Qt.CheckState.Checked:
                url = row.data(Qt.ItemDataRole.UserRole)
                if isinstance(url, str):
                    skipped.add(url)
        return skipped

    # -- internals ---------------------------------------------------------------

    def _update_count(self) -> None:
        total = self._list.count()
        kept = total - len(self.skip_urls())
        self._count.setText(ngettext("{kept} of {total} image will be copied.",
                                     "{kept} of {total} images will be copied.",
                                     total).format(kept=kept, total=total))

    def _set_thumbnail(self, url: str) -> None:
        row = self._items.get(url)
        image = self._images.image(url)
        if row is None or image is None or image.isNull():
            return
        tile = QPixmap(TILE, TILE)
        tile.fill(Qt.GlobalColor.transparent)
        painter = QPainter(tile)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        scaled = image.scaled(TILE, TILE, Qt.AspectRatioMode.KeepAspectRatio,
                              Qt.TransformationMode.SmoothTransformation)
        painter.drawImage((TILE - scaled.width()) // 2, (TILE - scaled.height()) // 2, scaled)
        painter.end()
        icon = QIcon(tile)
        # Qt synthesizes selected icons by tinting them with the system accent.
        # Selection belongs to the label/ring; the image itself stays truthful.
        icon.addPixmap(tile, QIcon.Mode.Selected)
        row.setIcon(icon)

    def _placeholder(self) -> QIcon:
        tile = QPixmap(TILE, TILE)
        tile.fill(Qt.GlobalColor.transparent)
        painter = QPainter(tile)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        fill = QColor(self.palette().color(QPalette.ColorRole.Mid))
        fill.setAlpha(70)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fill)
        painter.drawRoundedRect(4, 4, TILE - 8, TILE - 8, 6, 6)
        painter.end()
        return QIcon(tile)
