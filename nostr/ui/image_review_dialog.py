# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pick which images an import mirrors to Blossom.

A modal checklist over every image found in the current selection.
Checked images are mirrored (the default); unchecked ones stay at their
original source URL in the imported draft. The dialog returns the *skip
set*, which the panel hands to the import pipeline.
"""

from __future__ import annotations

from typing import Iterable, List, Optional, Set

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..imports.images import image_label


class ImageReviewDialog(QDialog):
    """Checklist of image URLs; unchecked rows join the skip set."""

    def __init__(
        self,
        images: List[str],
        skip_urls: Iterable[str] = (),
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Review images")
        self.setModal(True)
        self.resize(520, 380)

        skip = set(skip_urls or ())
        layout = QVBoxLayout(self)

        hint = QLabel(
            "Checked images are copied to your Blossom server so the "
            "draft doesn't depend on the source site. Untick any you "
            "want to keep at their original URL."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self._list = QListWidget()
        self._list.setAccessibleName("Images to mirror")
        self._list.setSelectionMode(QListWidget.NoSelection)
        for url in images:
            row = QListWidgetItem(f"{image_label(url)}    {url}")
            row.setFlags(row.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            row.setCheckState(Qt.Unchecked if url in skip else Qt.Checked)
            row.setData(Qt.UserRole, url)
            row.setToolTip(url)
            self._list.addItem(row)
        layout.addWidget(self._list, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def skip_urls(self) -> Set[str]:
        """URLs the user unticked: keep these at their original source."""
        skipped: Set[str] = set()
        for index in range(self._list.count()):
            row = self._list.item(index)
            if row.checkState() != Qt.Checked:
                url = row.data(Qt.UserRole)
                if isinstance(url, str):
                    skipped.add(url)
        return skipped
