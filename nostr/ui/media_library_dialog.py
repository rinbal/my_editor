# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Media Library dialog: browse, upload, preview, delete Blossom media.

One self-contained dialog. Toolbar with filter + sort + Upload + Refresh
above a thumbnail grid; an upload-progress strip pinned at the bottom
while uploads are running; a preview lightbox on double-click.

Designed to fit the existing publisher-dialog aesthetic: dark/light CSS
applied at construction, monospace headings, generous padding, no
animation. Picker variant is a thin subclass that adds a "Select" button.

The grid also says which files are private. A blob stored encrypted and
a blob anyone can open look identical from the server's side, both are
just bytes at a hash, so without a mark the user's only clue that they
picked a private picture would be the warning at the very end of a
publish. That is too late to be a choice. The mark is a word on the tile
and a chip on the thumbnail, not a colour alone: ``accessibility.md``
says to "offer visual indicators, like distinct shapes or icons, in
addition to color to help people perceive differences in function and
changes in state", and an item's text in this grid is painted in one
colour that the selection overrides anyway.

Private thumbnails are real thumbnails. The bytes are decrypted in
memory and handed straight to Qt; nothing decrypted is ever written to
the shared blob cache, because that cache is what an export walks. See
``media.private_preview``.
"""

from __future__ import annotations

from typing import Final, List, Optional

import itertools
import time
from pathlib import Path

from PySide6.QtCore import QBuffer, QIODevice, QRect, QSize, Qt, Signal, QUrl
from PySide6.QtGui import (
    QAction,
    QColor,
    QCursor,
    QDesktopServices,
    QFont,
    QIcon,
    QKeySequence,
    QPainter,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..blossom.store import MediaFile, MediaStore
from ..media.media_visibility import (
    PRIVATE,
    PUBLIC,
    PUBLISHED_COPY,
    UNKNOWN,
    MediaVisibility,
)
from ..media.private_library import PrivateLibrary
from .thumbnail_loader import ThumbnailLoader


# --------------------------------------------------------------------------- #
# Theme CSS: matches the publisher dialog family
# --------------------------------------------------------------------------- #

_DARK_CSS = """
QDialog { background: #1E1E1E; }
QLabel { color: #D4D4D4; font-size: 12px; }
QLabel#media_hint { color: #858585; }
QLabel#media_status { color: #FFB347; }
QLabel#media_empty { color: #6A6A6A; font-size: 13px; }
QLabel#media_notice {
    color: #FFB347;
    background: #2D2517;
    border: 1px solid #5A4620;
    border-radius: 4px;
    padding: 8px 10px;
}
/* Work in progress is not trouble. Same shape so the grid does not jump
   when one becomes the other, but the dialog's own grey rather than the
   amber it uses to mean "read this". 7.0:1 on its background. */
QLabel#media_notice[tone="working"] {
    color: #C8C8C8;
    background: #262626;
    border: 1px solid #3C3C3C;
}
QPushButton, QToolButton {
    background: #2D2D30;
    color: #D4D4D4;
    border: 1px solid #3C3C3C;
    padding: 6px 14px;
    border-radius: 4px;
    min-height: 22px;
}
QPushButton:hover, QToolButton:hover { background: #3C3C3C; }
QPushButton:pressed, QToolButton:pressed { background: #1E1E1E; }
QPushButton:disabled, QToolButton:disabled { background: #252526; color: #6A6A6A; border-color: #2D2D2D; }
QComboBox {
    background: #2D2D30;
    color: #D4D4D4;
    border: 1px solid #3C3C3C;
    border-radius: 4px;
    padding: 5px 30px 5px 12px;
    min-height: 22px;
    min-width: 130px;
}
QComboBox:hover { background: #3C3C3C; }
QComboBox:focus, QComboBox:on { border-color: #1177BB; }
QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: center right;
    width: 24px;
    border: none;
    background: transparent;
}
QComboBox::down-arrow {
    width: 10px;
    height: 6px;
    image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='10' height='6'><polygon points='0,0 10,0 5,6' fill='%23B0B0B0'/></svg>");
}
QComboBox::down-arrow:on {
    image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='10' height='6'><polygon points='0,6 10,6 5,0' fill='%23D4D4D4'/></svg>");
}
QComboBox QAbstractItemView {
    background: #252526;
    color: #D4D4D4;
    border: 1px solid #3C3C3C;
    border-radius: 4px;
    selection-background-color: #094771;
    outline: none;
    padding: 4px;
}
QComboBox QAbstractItemView::item {
    padding: 6px 12px;
    min-height: 22px;
    border-radius: 3px;
}
QComboBox QAbstractItemView::item:selected { background: #094771; color: #FFFFFF; }
QComboBox QAbstractItemView::item:hover { background: #2A2D2E; }
QPushButton#media_primary { background: #094771; border-color: #1177BB; color: #FFFFFF; }
QPushButton#media_primary:hover { background: #1177BB; }
QPushButton#media_destructive { background: #5A1D1D; border-color: #8B2F2F; color: #FFFFFF; }
QPushButton#media_destructive:hover { background: #8B2F2F; }
QListWidget {
    background: #1E1E1E;
    color: #D4D4D4;
    border: 1px solid #3C3C3C;
    border-radius: 4px;
    padding: 6px;
}
QListWidget::item {
    background: #252526;
    border: 1px solid #2D2D30;
    border-radius: 6px;
    padding: 6px;
    margin: 4px;
}
QListWidget::item:selected {
    background: #094771;
    border-color: #1177BB;
    color: #FFFFFF;
}
QListWidget::item:hover { background: #2A2D2E; }
QProgressBar {
    background: #252526;
    color: #D4D4D4;
    border: 1px solid #3C3C3C;
    border-radius: 3px;
    text-align: center;
    height: 14px;
}
QProgressBar::chunk { background: #1177BB; border-radius: 3px; }
QLineEdit {
    background: #252526;
    color: #D4D4D4;
    border: 1px solid #3C3C3C;
    border-radius: 4px;
    padding: 6px 10px;
    selection-background-color: #264F78;
}
QLineEdit:focus { border-color: #1177BB; }
QFrame#media_drop_zone {
    background: #252526;
    border: 1px dashed #3C3C3C;
    border-radius: 6px;
}
QFrame#media_drop_zone[drag_active="true"] {
    background: #094771;
    border-color: #1177BB;
}
"""

_LIGHT_CSS = """
QDialog { background: #FFFFFF; }
QLabel { color: #333333; font-size: 12px; }
QLabel#media_hint { color: #777777; }
QLabel#media_status { color: #A05000; }
QLabel#media_empty { color: #999999; font-size: 13px; }
QLabel#media_notice {
    color: #7A3E00;
    background: #FDF3E3;
    border: 1px solid #E0BE85;
    border-radius: 4px;
    padding: 8px 10px;
}
/* See the dark sheet. 8.6:1 on its background. */
QLabel#media_notice[tone="working"] {
    color: #4A4A4A;
    background: #F4F4F4;
    border: 1px solid #DCDCDC;
}
QPushButton, QToolButton {
    background: #ECECEC;
    color: #333333;
    border: 1px solid #CCCCCC;
    padding: 6px 14px;
    border-radius: 4px;
    min-height: 22px;
}
QPushButton:hover, QToolButton:hover { background: #E1E1E1; }
QPushButton:pressed, QToolButton:pressed { background: #D0D0D0; }
QPushButton:disabled, QToolButton:disabled { background: #F8F8F8; color: #BBBBBB; border-color: #EBEBEB; }
QComboBox {
    background: #FFFFFF;
    color: #333333;
    border: 1px solid #CCCCCC;
    border-radius: 4px;
    padding: 5px 30px 5px 12px;
    min-height: 22px;
    min-width: 130px;
}
QComboBox:hover { border-color: #999999; }
QComboBox:focus, QComboBox:on { border-color: #0066CC; }
QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: center right;
    width: 24px;
    border: none;
    background: transparent;
}
QComboBox::down-arrow {
    width: 10px;
    height: 6px;
    image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='10' height='6'><polygon points='0,0 10,0 5,6' fill='%23666666'/></svg>");
}
QComboBox::down-arrow:on {
    image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='10' height='6'><polygon points='0,6 10,6 5,0' fill='%23333333'/></svg>");
}
QComboBox QAbstractItemView {
    background: #FFFFFF;
    color: #333333;
    border: 1px solid #CCCCCC;
    border-radius: 4px;
    selection-background-color: #CCE4F7;
    outline: none;
    padding: 4px;
}
QComboBox QAbstractItemView::item {
    padding: 6px 12px;
    min-height: 22px;
    border-radius: 3px;
}
QComboBox QAbstractItemView::item:selected { background: #CCE4F7; color: #003366; }
QComboBox QAbstractItemView::item:hover { background: #F2F2F2; }
QPushButton#media_primary { background: #0066CC; border-color: #0055AA; color: #FFFFFF; }
QPushButton#media_primary:hover { background: #0077E0; }
QPushButton#media_destructive { background: #C0392B; border-color: #922B22; color: #FFFFFF; }
QPushButton#media_destructive:hover { background: #D54839; }
QListWidget {
    background: #FAFAFA;
    color: #333333;
    border: 1px solid #E1E1E1;
    border-radius: 4px;
    padding: 6px;
}
QListWidget::item {
    background: #FFFFFF;
    border: 1px solid #EAEAEA;
    border-radius: 6px;
    padding: 6px;
    margin: 4px;
}
QListWidget::item:selected {
    background: #CCE4F7;
    border-color: #0066CC;
    color: #003366;
}
QListWidget::item:hover { background: #F2F2F2; }
QProgressBar {
    background: #F0F0F0;
    color: #333333;
    border: 1px solid #CCCCCC;
    border-radius: 3px;
    text-align: center;
    height: 14px;
}
QProgressBar::chunk { background: #0066CC; border-radius: 3px; }
QLineEdit {
    background: #FFFFFF;
    color: #333333;
    border: 1px solid #CCCCCC;
    border-radius: 4px;
    padding: 6px 10px;
    selection-background-color: #CCE4F7;
}
QLineEdit:focus { border-color: #0066CC; }
QFrame#media_drop_zone {
    background: #F8F8F8;
    border: 1px dashed #CCCCCC;
    border-radius: 6px;
}
QFrame#media_drop_zone[drag_active="true"] {
    background: #E6F2FB;
    border-color: #0066CC;
}
"""


_THUMB_SIZE = 128

# The toolbar's type filter, as the store defines it. Applied here as
# well as there, because an encrypted blob's real type is not the one
# the server reports.
_FILTER_PREFIXES = {"image": "image/", "video": "video/", "audio": "audio/"}

# Tile text is three lines: size, hash prefix, and the visibility word.
# The third is blank for an ordinary public blob, so every cell is the
# same height whether or not it carries a state. Uniform cells are what
# let the grid keep ``setUniformItemSizes``.
_LABEL_LINES = 3

# The states whose bytes on the server are an encryption envelope. The
# grid treats these as one thing in several places, because what they
# share, that the server's type is the envelope's and the picture is
# sealed rather than broken, is what those places are reasoning about.
_ENCRYPTED_STATES = (PRIVATE, PUBLISHED_COPY)

# The chip drawn onto a thumbnail that is not plainly public. The same
# word for both private states: the file is private either way, and
# which of the two it is, is spelled out on the tile and in the tooltip
# rather than left to the colour. An unchecked file gets its own word,
# because "private" would be a claim and this is the absence of one.
_CHIP_TEXT = {
    PRIVATE: "PRIVATE",
    PUBLISHED_COPY: "PRIVATE",
    UNKNOWN: "NOT CHECKED",
}

# A verdict the library has not reached yet, as opposed to one it tried
# to reach and could not. Every rendering table here is read with
# ``.get(state, "")``, so this state draws no chip, prints no word and
# adds no tooltip line without any of them needing to know it exists.
#
# The distinction is the whole point. UNKNOWN is a finding and deserves
# to be said out loud, because it means publishing is blocked until
# somebody does something. A load that is simply still running is not a
# finding, and stamping NOT CHECKED across an entire grid while the
# answers are still arriving reports a verdict the app has not reached.
# What is softened here is the badge and never the permission: the
# publish gate keeps asking ``state_of``, which does not know this state.
_PENDING: Final[str] = "pending"
_CHIP_PAD_X = 6
_CHIP_PAD_Y = 3
_CHIP_MARGIN = 6

# Chip fill and ink, per theme. The fills are the two colours this
# dialog already uses, the status amber and the accent blue; the ink is
# chosen against the fill rather than against the tile, so the chip
# keeps its contrast whatever the thumbnail behind it looks like and
# whether or not the tile is selected. Amber means "private, nothing has
# left"; blue means "a public copy of this exists", which is the same
# blue this dialog already uses for the action a user is most likely to
# take. color.md: "Avoid using the same color to mean different things."
# Unchecked is neither of those colours: a state that means "no answer
# yet" must not borrow the colour of an answer. It gets the dialog's own
# grey, which reads as absent rather than as a third category of file.
_CHIP_COLORS = {
    True: {
        # 9.4:1 ink on fill.
        PRIVATE: ("#FFB347", "#1E1E1E"),
        # 4.8:1 ink on fill.
        PUBLISHED_COPY: ("#1177BB", "#FFFFFF"),
        # 5.4:1 ink on fill.
        UNKNOWN: ("#6A6A6A", "#FFFFFF"),
    },
    False: {
        # 5.8:1 ink on fill.
        PRIVATE: ("#A05000", "#FFFFFF"),
        # 5.6:1 ink on fill.
        PUBLISHED_COPY: ("#0066CC", "#FFFFFF"),
        # 6.9:1 ink on fill.
        UNKNOWN: ("#5A5A5A", "#FFFFFF"),
    },
}

# The word on the tile. Sentence case, as the rest of this dialog's
# labels are. writing.md: "Choose words that are easily understood and
# convey the right thing."
_STATE_LABELS = {
    PUBLIC: "",
    PRIVATE: "Private",
    PUBLISHED_COPY: "Private · copy published",
    UNKNOWN: "Not checked",
}

# The sentence in the tooltip. Longer, because a tooltip is where
# someone goes when the word alone was not enough.
_STATE_TOOLTIPS = {
    PRIVATE: "private, stored encrypted and readable only by you",
    PUBLISHED_COPY: "private, and a public copy of it exists",
    UNKNOWN: (
        "not checked, because your private library could not be read, so "
        "this app cannot say whether this file is private"
    ),
}

# What the picker says when the current selection is private. Shown
# inline next to the choice rather than as an alert: alerts.md says to
# "avoid using an alert merely to provide information" and to "prefer
# finding an alternative way to communicate it within the relevant
# context". The alert comes later, when there is actually a decision to
# make and something irreversible behind it.
_PICK_PRIVATE_NOTICE = (
    "This picture is private. Using it in something you publish creates a "
    "separate public copy that anyone can open. The original stays private, "
    "and you can confirm or cancel before anything is uploaded."
)
_PICK_PUBLISHED_NOTICE = (
    "This picture is private and already has a public copy. Using it here "
    "reuses that copy, so nothing new is uploaded."
)
# Said before the click rather than after it, for the same reason the two
# above are: the user is choosing, and this is the one state where the
# choice cannot be honoured. It says what is missing rather than what
# went wrong, because the file itself is fine.
_PICK_UNKNOWN_NOTICE = (
    "This picture has not been checked against your private library, so it "
    "cannot be used yet. Publishing it while it is unchecked could expose a "
    "file you keep private."
)

# What the banner says when the library has never been read at all, which
# is the state before the first load answers. The library's own status
# line replaces this as soon as there is one.
_LIBRARY_UNREAD = (
    "Your private library has not been read yet, so this app cannot tell "
    "which of these files are private."
)

# Shown while the load is running. Reading the library costs a signer
# round-trip per file, so this is the normal state of a large library for
# a few seconds, and it is progress rather than trouble: it says what is
# happening, not what is missing.
_LIBRARY_CHECKING = "Checking which of your files are private…"

# The picker equivalent, for the seconds before the answer for the
# selected file arrives. The publish gate still refuses a file in this
# state; what changes is that the user is told it is coming rather than
# told it cannot be used.
_PICK_PENDING_NOTICE = (
    "Checking whether this picture is private. This takes a moment the "
    "first time."
)

# Monotonic counter used to disambiguate paste-to-upload job names, two
# pastes within one wall-clock second must not collide in the upload
# queue, otherwise the auto-insert routes to the wrong editor.
_paste_counter = itertools.count(1)


def _format_size(byte_count: int) -> str:
    if byte_count >= 1024 * 1024:
        return f"{byte_count / (1024 * 1024):.1f} MiB"
    if byte_count >= 1024:
        return f"{byte_count / 1024:.1f} KiB"
    return f"{byte_count} B"


# --------------------------------------------------------------------------- #
# Drop zone
# --------------------------------------------------------------------------- #

class _DropZone(QFrame):
    """File-drop affordance. Emits ``files_dropped(list[str])``."""

    files_dropped = Signal(list)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("media_drop_zone")
        self.setAcceptDrops(True)
        self.setMinimumHeight(56)
        self.setProperty("drag_active", False)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        self._label = QLabel("Drop files here to upload, or use the Upload button.")
        self._label.setObjectName("media_hint")
        layout.addWidget(self._label)
        layout.addStretch(1)

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self._set_drag_active(True)
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:
        self._set_drag_active(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:
        self._set_drag_active(False)
        if not event.mimeData().hasUrls():
            event.ignore()
            return
        paths: List[str] = []
        for url in event.mimeData().urls():
            if url.isLocalFile():
                paths.append(url.toLocalFile())
        if paths:
            self.files_dropped.emit(paths)
            event.acceptProposedAction()

    def _set_drag_active(self, active: bool) -> None:
        self.setProperty("drag_active", "true" if active else "false")
        style = self.style()
        style.unpolish(self)
        style.polish(self)


# --------------------------------------------------------------------------- #
# Preview lightbox
# --------------------------------------------------------------------------- #

class _PreviewDialog(QDialog):
    """Full-pixmap preview: large image area, metadata strip, and the
    standard Copy URL / Download / Open in browser actions you'd expect
    from any media-library lightbox.

    Keyboard:
      ←/→  navigate · Esc close · Cmd/Ctrl+C copies URL · Cmd/Ctrl+S downloads
    """

    download_requested = Signal(object)   # MediaFile

    def __init__(
        self,
        files: List[MediaFile],
        start_index: int,
        loader: ThumbnailLoader,
        *,
        is_dark: bool,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Media preview")
        # Default to roughly half of a typical 1080p panel so the image
        # has real room. Minimum keeps the controls usable on small
        # laptops.
        self.resize(1024, 720)
        self.setMinimumSize(720, 540)
        # Without this the dialog stays alive (hidden) after the user
        # closes it, and its still-connected ``loader.ready`` slot fires
        # on every subsequent thumbnail decode. WA_DeleteOnClose makes
        # Qt release the C++ object on close so the connection is
        # cleaned up with it.
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setStyleSheet(_DARK_CSS if is_dark else _LIGHT_CSS)
        self._files = files
        self._loader = loader
        self._is_dark = is_dark
        self._index = max(0, min(start_index, len(files) - 1))
        self._current_pixmap: Optional[QPixmap] = None
        self._build_ui()
        loader.ready.connect(self._on_thumb_ready)
        self._refresh()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)

        # Massive image area, takes all the slack from the layout.
        self._image = QLabel()
        self._image.setAlignment(Qt.AlignCenter)
        self._image.setMinimumHeight(420)
        self._image.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout.addWidget(self._image, 1)

        # Metadata strip: URL, size, mime, server count.
        self._meta = QLabel("")
        self._meta.setObjectName("media_hint")
        self._meta.setWordWrap(True)
        self._meta.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self._meta)

        # Action row: Copy / Download / Open in browser / nav.
        action_row = QHBoxLayout()
        action_row.setSpacing(8)

        self._copy_btn = QPushButton("Copy URL")
        self._copy_btn.setShortcut(QKeySequence.Copy)
        self._copy_btn.clicked.connect(self._copy_url)
        action_row.addWidget(self._copy_btn)

        self._download_btn = QPushButton("Download")
        self._download_btn.setShortcut(QKeySequence.Save)
        self._download_btn.clicked.connect(self._download)
        action_row.addWidget(self._download_btn)

        self._open_btn = QPushButton("Open in browser")
        self._open_btn.clicked.connect(self._open_browser)
        action_row.addWidget(self._open_btn)

        action_row.addStretch(1)

        self._counter = QLabel("")
        self._counter.setObjectName("media_hint")
        action_row.addWidget(self._counter)

        self._prev_btn = QPushButton("◀ Previous")
        self._prev_btn.clicked.connect(self._prev)
        action_row.addWidget(self._prev_btn)

        self._next_btn = QPushButton("Next ▶")
        self._next_btn.clicked.connect(self._next)
        action_row.addWidget(self._next_btn)

        layout.addLayout(action_row)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Left:
            self._prev()
        elif event.key() == Qt.Key_Right:
            self._next()
        elif event.key() == Qt.Key_Escape:
            self.accept()
        else:
            super().keyPressEvent(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # Re-scale the current image to the new viewport so the preview
        # tracks the window. Skipped if no pixmap yet (still loading).
        self._rescale_current()

    def _current(self) -> MediaFile:
        return self._files[self._index]

    def _refresh(self) -> None:
        if not self._files:
            self.accept()
            return
        media = self._current()
        server_count = len(media.urls)
        dims = f"{media.width} × {media.height}  ·  " if media.width else ""
        self._meta.setText(
            f"{media.url}\n"
            f"{dims}{_format_size(media.size)}  ·  {media.mime_type}  ·  "
            f"mirrored on {server_count} server{'s' if server_count != 1 else ''}"
        )
        self._counter.setText(f"{self._index + 1} / {len(self._files)}")
        self._current_pixmap = None
        self._image.setPixmap(QPixmap())
        if (media.mime_type or "").startswith("image/"):
            self._image.setText("Loading…")
            self._loader.load(media.hash, media.url)
        else:
            self._image.setText(f"[{media.mime_type or 'binary'}, open in browser to inspect]")
        # Download / open-in-browser always make sense; copy URL always makes sense.
        # No need to enable/disable anything per item.

    def _rescale_current(self) -> None:
        if self._current_pixmap is None or self._current_pixmap.isNull():
            return
        target_w = max(1, self._image.width())
        target_h = max(1, self._image.height())
        scaled = self._current_pixmap.scaled(
            target_w, target_h,
            Qt.KeepAspectRatio, Qt.SmoothTransformation,
        )
        self._image.setPixmap(scaled)

    def _on_thumb_ready(self, sha: str, _path: str, pix: QPixmap) -> None:
        if self._index >= len(self._files):
            return
        if self._current().hash != sha:
            return
        # First decode for this file → refresh the meta line so the
        # dimensions appear in the strip below the image.
        media = self._current()
        if not media.width and not pix.isNull():
            media.width = pix.width()
            media.height = pix.height()
            self._refresh_meta_only()
        self._current_pixmap = pix
        self._rescale_current()

    def _refresh_meta_only(self) -> None:
        media = self._current()
        server_count = len(media.urls)
        dims = f"{media.width} × {media.height}  ·  " if media.width else ""
        self._meta.setText(
            f"{media.url}\n"
            f"{dims}{_format_size(media.size)}  ·  {media.mime_type}  ·  "
            f"mirrored on {server_count} server{'s' if server_count != 1 else ''}"
        )

    def _prev(self) -> None:
        if not self._files:
            return
        self._index = (self._index - 1) % len(self._files)
        self._refresh()

    def _next(self) -> None:
        if not self._files:
            return
        self._index = (self._index + 1) % len(self._files)
        self._refresh()

    def _copy_url(self) -> None:
        QApplication.clipboard().setText(self._current().url)

    def _open_browser(self) -> None:
        QDesktopServices.openUrl(QUrl(self._current().url))

    def _download(self) -> None:
        self.download_requested.emit(self._current())


# --------------------------------------------------------------------------- #
# Media Library dialog
# --------------------------------------------------------------------------- #

class MediaLibraryDialog(QDialog):
    """Browse / upload / delete Blossom media for the active profile.

    Picker mode (``pick_mode=True``) shows a "Select" button and emits
    ``file_picked(MediaFile)`` instead of providing in-place editor
    actions. The library itself works identically in both modes."""

    file_picked = Signal(object, str)   # MediaFile, alt_text (empty when alt row is hidden)

    def __init__(
        self,
        *,
        store: MediaStore,
        is_dark: bool = True,
        pick_mode: bool = False,
        pick_alt_text: bool = True,
        visibility: Optional[MediaVisibility] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Media library" if not pick_mode else "Insert image")
        self.setModal(False if not pick_mode else True)
        self.resize(820, 620)
        # Auto-delete on close so the dialog's connections to MediaStore
        # / ThumbnailLoader are released. Otherwise a parent-owned but
        # hidden dialog keeps firing every refresh / upload signal in
        # the background.
        self.setAttribute(Qt.WA_DeleteOnClose)

        self._store = store
        self._is_dark = is_dark
        self._pick_mode = pick_mode
        self._pick_alt_text = pick_alt_text and pick_mode
        self._loader = ThumbnailLoader(parent=self)
        # Empty by default: with no library and no ledger wired there is
        # nothing to be private of, and every blob is public, which is
        # the answer this dialog gave before any of this existed. That is
        # not the same as a library that exists and has not been read;
        # that one answers "not checked" and the grid marks it.
        self._visibility = visibility or MediaVisibility()
        # Set by ``bind_private_library``. Kept as well as the visibility
        # object because the two answer different questions: visibility
        # says what one file is, this says whether the answers can be
        # relied on at all, and the user needs to be told the second.
        self._private_library: Optional[PrivateLibrary] = None
        self._library_status = ""
        self._items_by_hash: dict[str, QListWidgetItem] = {}
        self._active_uploads: dict[str, QProgressBar] = {}
        # sha256 -> why its thumbnail could not be produced. Survives a
        # grid rebuild so the explanation is not lost on every refresh.
        self._preview_errors: dict[str, str] = {}

        self._build_ui()
        self._install_shortcuts()
        self.setStyleSheet(_DARK_CSS if is_dark else _LIGHT_CSS)

        # Wire store signals.
        store.library_changed.connect(self._refresh_grid)
        store.fetch_started.connect(lambda: self._set_status("Refreshing library…"))
        store.fetch_finished.connect(lambda: self._set_status(""))
        store.fetch_error.connect(lambda reason: self._set_status(reason, error=True))
        store.upload_started.connect(self._on_upload_started)
        store.upload_progress.connect(self._on_upload_progress)
        store.upload_status.connect(self._on_upload_status)
        store.upload_finished.connect(self._on_upload_finished)
        store.upload_failed.connect(self._on_upload_failed)
        store.upload_rerouted.connect(self._on_upload_rerouted)
        store.file_deleted.connect(self._on_file_deleted)
        store.delete_failed.connect(self._on_delete_failed)

        # Wire thumbnail loader signals to update existing items. The
        # failure side matters as much as the success side: without it a
        # blob that cannot be previewed is indistinguishable from one
        # still loading, forever.
        self._loader.ready.connect(self._on_thumbnail_ready)
        self._loader.failed.connect(self._on_thumbnail_failed)

        # Kick off an initial fetch.
        self._refresh_grid()
        store.fetch()

    # ------------------------------------------------------------------
    # The private library
    # ------------------------------------------------------------------

    def bind_private_library(self, library: PrivateLibrary) -> None:
        """Follow a private library, and say so when it cannot be read.

        Both of its signals matter and for different reasons.
        ``library_changed`` repaints the grid, so a file that turns out
        to be private stops being drawn as an ordinary blob. And
        ``status_changed`` is the only place the app ever learns that
        the library did not open: without it, "your private library
        stayed closed" is a sentence emitted into nothing while the user
        stands in front of a grid of files it cannot vouch for.

        Called rather than passed to the constructor because binding the
        profile is what starts the load, and that is the caller's
        decision: it is where the signer prompts come from.
        """
        self._private_library = library
        # Seeded rather than waited for. The library outlives this dialog,
        # so a load that finished before it was built emitted its status
        # into a widget that did not exist yet, and this reported a
        # library read minutes ago as one that had never been read.
        self._library_status = library.status
        library.library_changed.connect(self._on_private_library_changed)
        library.status_changed.connect(self._on_private_library_status)
        self._on_private_library_changed()

    def _on_private_library_status(self, text: str) -> None:
        self._library_status = text
        self._refresh_library_notice()

    def _on_private_library_changed(self) -> None:
        self._refresh_grid()
        self._refresh_library_notice()

    def _library_pending(self) -> bool:
        """Whether the answers are still on their way."""
        library = self._private_library
        return library is not None and library.loading

    def _library_failure_for(self, sha256: str) -> str:
        """Why this one file could not be accounted for, if it could not.

        Failures are filed under the record's ``d`` tag, which for a file
        record is its content hash, so a tile can find its own.
        """
        library = self._private_library
        if library is None or not sha256:
            return ""
        wanted = sha256.strip().lower()
        for failure in library.failures:
            if (failure.identifier or "").strip().lower() == wanted:
                return failure.reason
        return ""

    def _shown_state(self, sha256: str) -> str:
        """The state to draw, which is not always the state to act on.

        Only ever softens :data:`UNKNOWN`, and only while the load that
        would answer it is still running. Everything else is passed
        through untouched, so a file the library has actually failed to
        account for still gets its badge.
        """
        state = self._visibility.state_of(sha256)
        if state == UNKNOWN and self._library_pending():
            return _PENDING
        return state

    def _refresh_library_notice(self) -> None:
        """Say what is happening, and only when there is something to say.

        Three situations, and they used to share one amber banner and one
        run-on sentence assembled from whatever internal complaints were
        lying around. Reading the library is normal work that takes a
        signer round-trip per file, so most of the time this is a
        progress line, not a warning; a person who is told something is
        wrong every time they open a dialog stops reading the times it
        is.

        Settled is silence. Nothing is more reassuring than a dialog that
        has nothing to report.
        """
        library = self._private_library
        if library is None or library.settled:
            self._library_label.setVisible(False)
            return

        if library.loading:
            self._show_library_notice(_LIBRARY_CHECKING, working=True)
            return

        # Not loading and not settled: something is genuinely unresolved,
        # and the grid is showing files it cannot vouch for. One sentence
        # that says what it means for the user, rather than the load's
        # internal complaints concatenated. The per-record reasons stay
        # on the tiles they belong to, where they name a file.
        count = len(library.failures)
        if count:
            self._show_library_notice(
                f"{count} item{'' if count == 1 else 's'} in your private "
                "library could not be opened, so files marked NOT CHECKED "
                "cannot be used in published work. Refresh to try again.",
                working=False,
            )
            return
        self._show_library_notice(
            self._library_status or _LIBRARY_UNREAD, working=False,
        )

    def _show_library_notice(self, text: str, *, working: bool) -> None:
        self._library_label.setText(text)
        self._library_label.setProperty("tone", "working" if working else "")
        # A property that styling depends on does not repaint on its own.
        self._library_label.style().unpolish(self._library_label)
        self._library_label.style().polish(self._library_label)
        self._library_label.setVisible(True)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        header = QLabel("Your Nostr media (Blossom)")
        font = header.font()
        font.setPointSize(font.pointSize() + 1)
        font.setBold(True)
        header.setFont(font)
        layout.addWidget(header)

        # Filter / sort row + Upload / Refresh.
        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)

        self._filter_combo = _styled_combo()
        self._filter_combo.addItem("All files", "all")
        self._filter_combo.addItem("Images", "image")
        self._filter_combo.addItem("Videos", "video")
        self._filter_combo.addItem("Audio", "audio")
        self._filter_combo.currentIndexChanged.connect(self._refresh_grid)
        toolbar.addWidget(self._filter_combo)

        self._sort_combo = _styled_combo()
        self._sort_combo.addItem("Newest first", "newest")
        self._sort_combo.addItem("Oldest first", "oldest")
        self._sort_combo.addItem("Largest first", "largest")
        self._sort_combo.addItem("Smallest first", "smallest")
        self._sort_combo.currentIndexChanged.connect(self._refresh_grid)
        toolbar.addWidget(self._sort_combo)

        toolbar.addStretch(1)

        self._upload_btn = QPushButton("Upload")
        self._upload_btn.setObjectName("media_primary")
        self._upload_btn.clicked.connect(self._on_upload_clicked)
        toolbar.addWidget(self._upload_btn)

        self._refresh_btn = QPushButton("Refresh")
        self._refresh_btn.clicked.connect(lambda: self._store.fetch(force=True))
        toolbar.addWidget(self._refresh_btn)

        layout.addLayout(toolbar)

        # Drop zone.
        self._drop_zone = _DropZone(self)
        self._drop_zone.files_dropped.connect(self._on_files_dropped)
        layout.addWidget(self._drop_zone)

        # The private library's own trouble, above the grid it applies
        # to. Not in the status line at the bottom: that line is the
        # store's, it is overwritten by every fetch and upload, and a
        # sentence saying half these tiles cannot be trusted must not be
        # cleared by an unrelated refresh finishing.
        self._library_label = QLabel("")
        self._library_label.setObjectName("media_notice")
        self._library_label.setWordWrap(True)
        # The words in here come from a signer, which is somebody else's
        # program. QLabel guesses rich text when it is not told, so a
        # reason containing markup would be rendered as markup rather
        # than shown as what it is.
        self._library_label.setTextFormat(Qt.PlainText)
        self._library_label.setVisible(False)
        layout.addWidget(self._library_label)

        # Grid.
        self._grid = QListWidget()
        self._grid.setViewMode(QListWidget.IconMode)
        self._grid.setIconSize(QSize(_THUMB_SIZE, _THUMB_SIZE))
        self._grid.setResizeMode(QListWidget.Adjust)
        self._grid.setMovement(QListWidget.Static)
        self._grid.setSelectionMode(QListWidget.ExtendedSelection)
        self._grid.setUniformItemSizes(True)
        self._grid.setGridSize(
            QSize(_THUMB_SIZE + 24, _THUMB_SIZE + 28 + _LABEL_LINES * 16)
        )
        self._grid.setSpacing(4)
        self._grid.setContextMenuPolicy(Qt.CustomContextMenu)
        self._grid.customContextMenuRequested.connect(self._on_grid_context)
        self._grid.itemDoubleClicked.connect(self._on_item_activated)
        self._grid.itemSelectionChanged.connect(self._refresh_pick_notice)
        layout.addWidget(self._grid, 1)

        # Empty state placeholder (shown when grid is empty).
        self._empty_label = QLabel(
            "No media yet. Drop a file above or hit Upload."
        )
        self._empty_label.setObjectName("media_empty")
        self._empty_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self._empty_label)

        # Discoverability hint, surfaces the double-click / right-click /
        # paste / delete affordances that aren't obvious from the grid alone.
        hint_text = (
            "Double-click a tile to preview · Right-click for actions · "
            "Paste an image to upload"
        )
        if not self._pick_mode:
            hint_text += " · Del to remove selected"
        self._hint_label = QLabel(hint_text)
        self._hint_label.setObjectName("media_hint")
        layout.addWidget(self._hint_label)

        # The picker's early warning. It sits directly above the alt-text
        # row and the Insert button, which is where the eye already is
        # when the choice is being made, and it is hidden entirely when
        # the selection is an ordinary public file so it never becomes
        # furniture the user learns to skip.
        self._notice_label = QLabel("")
        self._notice_label.setObjectName("media_notice")
        self._notice_label.setWordWrap(True)
        self._notice_label.setVisible(False)
        layout.addWidget(self._notice_label)

        # Alt-text field, only when the embedding flow actually uses
        # alt text (the editor's "Insert image" picker does; the
        # article-cover picker does not, because Nostr's NIP-23
        # ``image`` tag is a URL with no alt sibling).
        if self._pick_alt_text:
            alt_row = QHBoxLayout()
            alt_row.setSpacing(8)
            alt_label = QLabel("Alt text (optional):")
            alt_row.addWidget(alt_label)
            self._alt_edit = QLineEdit()
            self._alt_edit.setPlaceholderText("Describe the image for screen readers")
            self._alt_edit.setClearButtonEnabled(True)
            alt_row.addWidget(self._alt_edit, 1)
            layout.addLayout(alt_row)
        else:
            self._alt_edit = None

        # Upload progress strip.
        self._upload_panel = QFrame()
        self._upload_panel_layout = QVBoxLayout(self._upload_panel)
        self._upload_panel_layout.setContentsMargins(0, 0, 0, 0)
        self._upload_panel_layout.setSpacing(4)
        self._upload_panel.setVisible(False)
        layout.addWidget(self._upload_panel)

        # Bottom row: status + actions.
        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        self._status_label = QLabel("")
        self._status_label.setObjectName("media_status")
        self._status_label.setWordWrap(True)
        # Carries a media server's own words, for the same reason.
        self._status_label.setTextFormat(Qt.PlainText)
        bottom.addWidget(self._status_label, 1)

        if self._pick_mode:
            self._cancel_btn = QPushButton("Cancel")
            self._cancel_btn.clicked.connect(self.reject)
            bottom.addWidget(self._cancel_btn)
            self._select_btn = QPushButton("Insert")
            self._select_btn.setObjectName("media_primary")
            self._select_btn.setDefault(True)
            self._select_btn.clicked.connect(self._on_select_clicked)
            bottom.addWidget(self._select_btn)
        else:
            self._copy_btn = QPushButton("Copy URL")
            self._copy_btn.clicked.connect(self._on_copy_clicked)
            bottom.addWidget(self._copy_btn)
            self._download_btn = QPushButton("Download")
            self._download_btn.clicked.connect(self._on_download_clicked)
            bottom.addWidget(self._download_btn)
            self._open_btn = QPushButton("Open in browser")
            self._open_btn.clicked.connect(self._on_open_browser_clicked)
            bottom.addWidget(self._open_btn)
            self._delete_btn = QPushButton("Delete")
            self._delete_btn.setObjectName("media_destructive")
            self._delete_btn.clicked.connect(self._on_delete_clicked)
            bottom.addWidget(self._delete_btn)
            self._close_btn = QPushButton("Close")
            self._close_btn.clicked.connect(self.accept)
            bottom.addWidget(self._close_btn)

        layout.addLayout(bottom)

    # ------------------------------------------------------------------
    # Keyboard shortcuts
    # ------------------------------------------------------------------

    def _install_shortcuts(self) -> None:
        """Wire grid-scoped Paste / Delete shortcuts.

        Scoping to the grid (Qt.WidgetShortcut) keeps the QLineEdit alt
        field free to do its own native Ctrl+V paste. Users invoke the
        upload-on-paste behaviour by clicking the grid first, which is
        how Notion / Slack / GitHub all behave.
        """
        paste_action = QAction(self._grid)
        paste_action.setShortcuts(QKeySequence.Paste)
        paste_action.setShortcutContext(Qt.WidgetShortcut)
        paste_action.triggered.connect(self._on_paste_image)
        self._grid.addAction(paste_action)

        # Delete is library-only, pick mode is read-only.
        if not self._pick_mode:
            delete_action = QAction(self._grid)
            delete_action.setShortcuts([QKeySequence.Delete, QKeySequence(Qt.Key_Backspace)])
            delete_action.setShortcutContext(Qt.WidgetShortcut)
            delete_action.triggered.connect(self._on_delete_clicked)
            self._grid.addAction(delete_action)

    def _on_paste_image(self) -> None:
        """Upload the clipboard image, if any. Falls through silently on
        a non-image clipboard so the user can keep pasting text into
        whatever widget actually has focus."""
        mime = QApplication.clipboard().mimeData()
        if not mime.hasImage():
            self._set_status("Clipboard has no image to upload.", error=True)
            return
        image = QApplication.clipboard().image()
        if image.isNull():
            self._set_status("Clipboard image could not be read.", error=True)
            return
        buf = QBuffer()
        buf.open(QIODevice.WriteOnly)
        if not image.save(buf, "PNG"):
            self._set_status("Could not encode clipboard image as PNG.", error=True)
            return
        body = bytes(buf.data())
        name = _paste_upload_name()
        self._store.upload_bytes(body, name=name, mime_type="image/png")

    # ------------------------------------------------------------------
    # Grid management
    # ------------------------------------------------------------------

    def _visible_files(self) -> List[MediaFile]:
        """The list the grid shows, filtered on what each file really is.

        The store filters on the type the server reports, which for an
        encrypted blob is the envelope's and not the picture's. Left to
        it, the image picker would hide every private photo the user
        owns, which is exactly the pick this whole flow exists to handle.
        So the sort stays with the store and the type test happens here,
        against the type the private record declares.
        """
        filter_type = self._filter_combo.currentData() or "all"
        sort_by = self._sort_combo.currentData() or "newest"
        files = self._store.file_list(filter_type="all", sort_by=sort_by)
        prefix = _FILTER_PREFIXES.get(filter_type)
        if not prefix:
            return files
        return [m for m in files if self._effective_mime(m).startswith(prefix)]

    def _effective_mime(self, media: MediaFile) -> str:
        return (
            self._visibility.declared_mime(media.hash) or (media.mime_type or "")
        )

    def _refresh_grid(self) -> None:
        files = self._visible_files()

        self._grid.clear()
        self._items_by_hash.clear()
        for media in files:
            self._add_grid_item(media)

        if files:
            self._grid.setVisible(True)
            self._empty_label.setVisible(False)
        else:
            self._grid.setVisible(False)
            self._empty_label.setVisible(True)
        # A rebuild drops the selection, so the notice it belonged to has
        # to go with it rather than describe a file that is no longer
        # picked.
        self._refresh_pick_notice()

    def _add_grid_item(self, media: MediaFile) -> None:
        state = self._shown_state(media.hash)
        item = QListWidgetItem()
        item.setText(_short_label(media, state))
        item.setTextAlignment(Qt.AlignHCenter)
        item.setToolTip(_tooltip_for(
            media,
            preview_error=self._preview_errors.get(media.hash, ""),
            state=state,
            public_copy=self._visibility.public_copy_of(media.hash),
            library_error=self._library_failure_for(media.hash),
        ))
        item.setData(Qt.UserRole, media.hash)

        # Default icon: a placeholder coloured square. Replaced when the
        # thumbnail loader finishes.
        item.setIcon(_placeholder_icon(media.mime_type, state, self._is_dark))
        self._grid.addItem(item)
        self._items_by_hash[media.hash] = item

        # A private blob is ciphertext, so the server calls it a byte
        # stream and the mime test below would never ask for it. Fetch it
        # anyway: the download, the hash check and the cache are all
        # already safe for opaque bytes, and the decrypt happens after,
        # in memory, on the failure the decoder is about to report.
        # Unless the record positively says it is something else: a
        # private video would otherwise be pulled down in full only to
        # learn that the image decoder refuses it. A record that names no
        # type at all is still worth trying, because the only other
        # source for that answer is the envelope's own byte stream.
        #
        # An unchecked blob is fetched on the server's word alone, like
        # any other. Nothing here knows better yet, and guessing would
        # mean downloading every opaque blob in the library on the chance
        # that one of them is a sealed picture.
        if state in _ENCRYPTED_STATES:
            declared = self._visibility.declared_mime(media.hash)
            wanted = not declared or declared.startswith("image/")
        else:
            wanted = (media.mime_type or "").startswith("image/")
        if wanted:
            self._loader.load(media.hash, media.url)

    def _on_thumbnail_failed(self, sha: str, reason: str) -> None:
        """Record why a preview is missing and say so on the item.

        For a private file this is the expected first answer, not a
        fault: the decoder was handed an encryption envelope and refused
        it, exactly as it should. The bytes are cached by now, so this is
        where the key gets used.
        """
        if self._visibility.is_private(sha) and reason == "not an image":
            reason = self._try_private_preview(sha)
            if not reason:
                return
        self._preview_errors[sha] = reason
        item = self._items_by_hash.get(sha)
        media = self._store.files.get(sha)
        if item is None or media is None:
            return
        item.setToolTip(_tooltip_for(
            media,
            preview_error=reason,
            state=self._shown_state(sha),
            public_copy=self._visibility.public_copy_of(sha),
            library_error=self._library_failure_for(sha),
        ))

    def _try_private_preview(self, sha: str) -> str:
        """Decrypt a private file's cached bytes and show them, or say why.

        Returns "" when the tile now holds a real picture, otherwise the
        reason for the placeholder.

        The bytes read here are the ciphertext the loader already cached,
        which is safe to keep on disk because it is ciphertext. What
        comes out of the decrypt goes to Qt and nowhere else: it is never
        handed to ``put_bytes``, never written to a file, and never
        offered to the export path, because the blob cache is exactly
        where an export would find it.
        """
        path = self._loader.cache_path(sha)
        try:
            envelope = path.read_bytes()
        except OSError:
            return "the downloaded bytes could not be read back"

        outcome = self._visibility.preview(sha, envelope)
        del envelope
        if not outcome.ok:
            return outcome.reason

        item = self._items_by_hash.get(sha)
        if item is None:
            return ""
        pix = QPixmap.fromImage(outcome.image).scaled(
            _THUMB_SIZE, _THUMB_SIZE,
            Qt.KeepAspectRatio, Qt.SmoothTransformation,
        )
        self._preview_errors.pop(sha, None)
        item.setIcon(_chipped_icon(pix, self._shown_state(sha), self._is_dark))
        media = self._store.files.get(sha)
        if media is not None:
            item.setToolTip(_tooltip_for(
                media,
                state=self._shown_state(sha),
                public_copy=self._visibility.public_copy_of(sha),
                library_error=self._library_failure_for(sha),
            ))
        return ""

    def _refresh_pick_notice(self) -> None:
        """Warn at the picker, not at the confirmation, that a pick is private.

        Only in pick mode: in library mode nothing is about to be
        published, so the same sentence would be a statement of fact with
        no decision attached to it.
        """
        if not self._pick_mode:
            return
        media = self._first_selected()
        # ``_shown_state`` rather than ``state_of``: this line is what the
        # user reads, and "cannot be used" is the wrong thing to tell
        # someone whose answer is two seconds away. The gate behind the
        # click is unchanged and still refuses.
        state = self._shown_state(media.hash) if media is not None else PUBLIC
        if state == PRIVATE:
            self._notice_label.setText(_PICK_PRIVATE_NOTICE)
        elif state == PUBLISHED_COPY:
            self._notice_label.setText(_PICK_PUBLISHED_NOTICE)
        elif state == _PENDING:
            self._notice_label.setText(_PICK_PENDING_NOTICE)
        elif state == UNKNOWN:
            self._notice_label.setText(_PICK_UNKNOWN_NOTICE)
        else:
            self._notice_label.setText("")
        self._notice_label.setVisible(bool(self._notice_label.text()))

    def _on_thumbnail_ready(self, sha: str, _path: str, pix: QPixmap) -> None:
        # Stash the decoded dimensions on the MediaFile the first time
        # we see them. Cheap (we have the pixmap), and powers the
        # tooltip / preview metadata without a second decode pass.
        media = self._store.files.get(sha)
        if media is not None and not media.width and not pix.isNull():
            media.width = pix.width()
            media.height = pix.height()
        # A later success supersedes an earlier failure, for instance
        # after a retry or a mirror that actually has the bytes.
        self._preview_errors.pop(sha, None)
        item = self._items_by_hash.get(sha)
        if item is None:
            return
        state = self._shown_state(sha)
        if media is not None:
            item.setToolTip(_tooltip_for(
                media, state=state,
                public_copy=self._visibility.public_copy_of(sha),
                library_error=self._library_failure_for(sha),
            ))
        scaled = pix.scaled(
            _THUMB_SIZE, _THUMB_SIZE,
            Qt.KeepAspectRatio, Qt.SmoothTransformation,
        )
        item.setIcon(_chipped_icon(scaled, state, self._is_dark))

    def _selected_media(self) -> List[MediaFile]:
        result: List[MediaFile] = []
        for item in self._grid.selectedItems():
            sha = item.data(Qt.UserRole)
            media = self._store.files.get(sha)
            if media is not None:
                result.append(media)
        return result

    def _first_selected(self) -> Optional[MediaFile]:
        media_list = self._selected_media()
        return media_list[0] if media_list else None

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _on_upload_clicked(self) -> None:
        # Use a single multi-select dialog so a user can upload several
        # files in one go. The store handles them sequentially.
        paths, _filter = QFileDialog.getOpenFileNames(
            self,
            "Upload to Blossom",
            "",
            "Media files (*.png *.jpg *.jpeg *.gif *.webp *.svg *.bmp *.mp4 *.webm *.mov *.mp3 *.wav *.ogg *.pdf);;All files (*)",
        )
        if not paths:
            return
        for path in paths:
            self._store.upload_file(path)

    def _on_files_dropped(self, paths: List[str]) -> None:
        for path in paths:
            self._store.upload_file(path)

    def _on_copy_clicked(self) -> None:
        media = self._first_selected()
        if media is None:
            self._set_status("Select a file first.", error=True)
            return
        QApplication.clipboard().setText(media.url)
        self._set_status(f"Copied URL: {media.url}", error=False)

    def _on_download_clicked(self) -> None:
        media = self._first_selected()
        if media is None:
            self._set_status("Select a file first.", error=True)
            return
        self._save_media_to_disk(media)

    def _on_open_browser_clicked(self) -> None:
        media = self._first_selected()
        if media is None:
            self._set_status("Select a file first.", error=True)
            return
        QDesktopServices.openUrl(QUrl(media.url))

    def _on_delete_clicked(self) -> None:
        targets = self._selected_media()
        if not targets:
            return
        msg = f"Delete {len(targets)} file{'s' if len(targets) != 1 else ''} from your Blossom servers?"
        confirm = QMessageBox(self)
        confirm.setWindowTitle("Delete media")
        confirm.setText(msg)
        confirm.setIcon(QMessageBox.Warning)
        confirm.setStandardButtons(QMessageBox.Cancel | QMessageBox.Yes)
        confirm.setDefaultButton(QMessageBox.Cancel)
        if confirm.exec() != QMessageBox.Yes:
            return
        for media in targets:
            self._store.delete_file(media.hash)

    def _on_select_clicked(self) -> None:
        media = self._first_selected()
        if media is None:
            return
        self.file_picked.emit(media, self._current_alt_text())
        self.accept()

    def _current_alt_text(self) -> str:
        if self._alt_edit is None:
            return ""
        return self._alt_edit.text().strip()

    def _on_item_activated(self, item: QListWidgetItem) -> None:
        if self._pick_mode:
            sha = item.data(Qt.UserRole)
            media = self._store.files.get(sha)
            if media is not None:
                self.file_picked.emit(media, self._current_alt_text())
                self.accept()
            return
        # Library mode: open the lightbox preview.
        files = self._visible_files()
        sha = item.data(Qt.UserRole)
        try:
            idx = next(i for i, f in enumerate(files) if f.hash == sha)
        except StopIteration:
            return
        dialog = _PreviewDialog(files, idx, self._loader, is_dark=self._is_dark, parent=self)
        # Surface the same Download action from inside the lightbox.
        dialog.download_requested.connect(self._save_media_to_disk)
        dialog.exec()

    def _on_grid_context(self, point) -> None:
        item = self._grid.itemAt(point)
        if item is None:
            return
        sha = item.data(Qt.UserRole)
        media = self._store.files.get(sha)
        if media is None:
            return
        menu = QMenu(self)
        act_preview = menu.addAction("Preview")
        menu.addSeparator()
        act_copy = menu.addAction("Copy URL")
        act_open = menu.addAction("Open in browser")
        act_download = menu.addAction("Download…")
        # Per-server copy submenu, useful when a file is mirrored on
        # several servers and the user wants a specific CDN.
        if len(media.urls) > 1:
            per_server = menu.addMenu("Copy URL from server")
            for entry in media.urls:
                server = entry.get("server", "")
                url = entry.get("url", "")
                if not url:
                    continue
                label = _hostname_from_url(server) or server or "(unknown)"
                act = per_server.addAction(label)
                act.setData(url)
        menu.addSeparator()
        if not self._pick_mode:
            act_delete = menu.addAction("Delete")
        else:
            act_delete = None

        chosen = menu.exec(QCursor.pos())
        if chosen is None:
            return
        if chosen == act_preview:
            self._on_item_activated(item)
        elif chosen == act_copy:
            QApplication.clipboard().setText(media.url)
            self._set_status(f"Copied URL: {media.url}")
        elif chosen == act_open:
            QDesktopServices.openUrl(QUrl(media.url))
        elif chosen == act_download:
            self._save_media_to_disk(media)
        elif act_delete is not None and chosen == act_delete:
            self._store.delete_file(media.hash)
        else:
            # Could be one of the per-server copy entries.
            data = chosen.data()
            if isinstance(data, str) and data:
                QApplication.clipboard().setText(data)
                self._set_status(f"Copied URL: {data}")

    def _save_media_to_disk(self, media: MediaFile) -> None:
        """Save a media file from the local Blossom cache (or fetch it
        on demand) to a user-chosen path. The download is async; we
        connect to ``ThumbnailLoader.ready`` until the bytes arrive,
        then copy them to the destination."""
        suggested = _suggested_save_name(media)
        target, _filter = QFileDialog.getSaveFileName(
            self,
            "Save media",
            suggested,
            "All files (*)",
        )
        if not target:
            return

        cache_path = self._loader.cache_path(media.hash)
        if cache_path.is_file():
            self._copy_to_target(cache_path, target)
            return

        # Not cached yet, kick off the loader and complete the copy on
        # arrival. Track the destination by hash so multiple parallel
        # downloads route to the right paths.
        if not hasattr(self, "_pending_downloads"):
            self._pending_downloads: dict = {}
            self._loader.ready.connect(self._on_download_loader_ready)
            self._loader.failed.connect(self._on_download_loader_failed)
        self._pending_downloads[media.hash] = target
        self._set_status(f"Downloading {media.url}…")
        self._loader.load(media.hash, media.url)

    def _on_download_loader_ready(self, sha: str, local_path: str, _pix) -> None:
        target = self._pending_downloads.pop(sha, None) if hasattr(self, "_pending_downloads") else None
        if not target:
            return
        self._copy_to_target(Path(local_path), target)

    def _on_download_loader_failed(self, sha: str, reason: str) -> None:
        target = self._pending_downloads.pop(sha, None) if hasattr(self, "_pending_downloads") else None
        if target is None:
            return
        self._set_status(f"Download failed: {reason}", error=True)

    def _copy_to_target(self, src, dest: str) -> None:
        """Copy a cached file to the user-chosen destination. ``src`` is
        a ``Path``-compatible source already in the Blossom cache."""
        try:
            data = src.read_bytes() if hasattr(src, "read_bytes") else open(src, "rb").read()
            with open(dest, "wb") as f:
                f.write(data)
        except OSError as exc:
            self._set_status(f"Could not save: {exc}", error=True)
            return
        self._set_status(f"Saved to {dest}")

    # ------------------------------------------------------------------
    # Status + upload progress
    # ------------------------------------------------------------------

    def _set_status(self, text: str, *, error: bool = False) -> None:
        self._status_label.setText(text)
        if error:
            color = "#FF6B6B" if self._is_dark else "#C0392B"
        else:
            color = "#FFB347" if self._is_dark else "#A05000"
        self._status_label.setStyleSheet(f"color: {color};")

    def _ensure_upload_row(self, name: str) -> QProgressBar:
        bar = self._active_uploads.get(name)
        if bar is not None:
            return bar
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        label = QLabel(name)
        row.addWidget(label, 1)
        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setValue(0)
        bar.setFixedWidth(220)
        row.addWidget(bar)

        wrapper = QWidget()
        wrapper.setLayout(row)
        self._upload_panel_layout.addWidget(wrapper)
        bar._wrapper = wrapper   # carry the wrapper for later removal
        self._active_uploads[name] = bar
        self._upload_panel.setVisible(True)
        return bar

    def _remove_upload_row(self, name: str) -> None:
        bar = self._active_uploads.pop(name, None)
        if bar is None:
            return
        wrapper = getattr(bar, "_wrapper", None)
        if wrapper is not None:
            self._upload_panel_layout.removeWidget(wrapper)
            wrapper.deleteLater()
        if not self._active_uploads:
            self._upload_panel.setVisible(False)

    def _on_upload_started(self, name: str) -> None:
        self._ensure_upload_row(name)
        self._set_status(f"Uploading {name}…")

    def _on_upload_progress(self, name: str, sent: int, total: int) -> None:
        bar = self._ensure_upload_row(name)
        pct = int(sent * 100 / total) if total > 0 else 0
        bar.setValue(pct)

    def _on_upload_status(self, name: str, status: str) -> None:
        bar = self._ensure_upload_row(name)
        if status == "signing":
            bar.setFormat("Signing…")
        elif status == "uploading":
            bar.setFormat("%p%")
        elif status == "mirroring":
            bar.setFormat("Mirroring…")
        elif status == "done":
            bar.setValue(100)
            bar.setFormat("Done")
        elif status == "failed":
            bar.setFormat("Failed")

    def _on_upload_finished(self, name: str, _media: object) -> None:
        self._remove_upload_row(name)
        self._set_status(f"Uploaded {name}.")
        self._refresh_grid()

    def _on_upload_failed(self, name: str, reason: str) -> None:
        self._remove_upload_row(name)
        self._set_status(f"Upload of {name} failed: {reason}", error=True)

    def _on_upload_rerouted(self, name: str, from_host: str, to_host: str) -> None:
        self._set_status(
            f"{from_host} can't take this file, routing {name} to {to_host} instead."
        )

    def _on_file_deleted(self, file_hash: str) -> None:
        """Deleting a public copy is what revoking one is, so record that.

        Without this the ledger goes on claiming the copy exists: the
        original keeps its published badge, and the next publish reuses
        an address that now returns nothing, which is a broken image in
        somebody's article. The grid repaints on the ``library_changed``
        that follows this signal.
        """
        if self._visibility.forget_public_copy(file_hash):
            self._set_status(
                "Removed the public copy. That picture is private again."
            )

    def _on_delete_failed(self, file_hash: str, reason: str) -> None:
        # The store already removed the file locally, so surface as a
        # neutral note rather than a red error, since "the file is gone
        # from your library" is exactly what the user asked for.
        short = file_hash[:8]
        self._set_status(f"Removed {short}… locally · {reason}", error=False)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _short_label(media: MediaFile, state: str = PUBLIC) -> str:
    """Card label: size, hash prefix, and the file's visibility.

    Filenames aren't carried by Blossom, so the hash prefix is our best
    stable identifier; we put the more useful info (size + mirror count)
    on the first line where users actually look.

    The third line is the visibility word, and an ordinary public blob
    simply does not have one: everything in a plain library is public, so
    saying so on every tile would be noise, and noise is what teaches
    people to stop reading. The cell is sized for three lines either way,
    so a mixed library does not reflow when a state appears.
    """
    server_count = len(media.urls)
    badge = f"· {server_count}×" if server_count > 1 else ""
    lines = [f"{_format_size(media.size)} {badge}", f"{media.hash[:8]}…"]
    state_label = _STATE_LABELS.get(state, "")
    if state_label:
        lines.append(state_label)
    return "\n".join(lines)


def _tooltip_for(
    media: MediaFile,
    *,
    preview_error: str = "",
    state: str = PUBLIC,
    public_copy=None,
    library_error: str = "",
) -> str:
    server_count = len(media.urls)
    lines = [
        f"sha256: {media.hash}",
        f"type:   {media.mime_type}",
        f"size:   {_format_size(media.size)}",
    ]
    if media.width and media.height:
        lines.append(f"dim:    {media.width} × {media.height}")
    # Why this particular file could not be accounted for. It belongs
    # here rather than in the banner above the grid: the reason names one
    # file, and a banner that concatenates every file's reason is a
    # diagnostic dump that says nothing about the tile under the pointer.
    if library_error:
        lines.append(f"library: {library_error}")
    # Which of the two states a private file is in matters more than the
    # colour of its chip can say, so the tooltip spells it out and names
    # the copy's address when there is one. The address is the thing a
    # user needs to revoke it.
    if state in _STATE_TOOLTIPS:
        lines.append(f"access: {_STATE_TOOLTIPS[state]}")
    if public_copy is not None:
        lines.append(f"copy:   {public_copy.url}")
    # A blob can be perfectly healthy on the server and still have no
    # preview, so the grid says which it is. A bare placeholder with no
    # explanation reads as a broken app.
    note = _no_preview_reason(media, preview_error, state)
    if note:
        lines.append(f"preview: {note}")
    lines.append(f"on {server_count} server{'s' if server_count != 1 else ''}:")
    for entry in media.urls:
        lines.append(f"  · {entry.get('url', '')}")
    lines.append("")
    lines.append("Double-click to preview · Right-click for actions")
    return "\n".join(lines)


def _no_preview_reason(
    media: MediaFile, preview_error: str = "", state: str = PUBLIC,
) -> str:
    """Plain-language reason this blob shows a placeholder, or "".

    Several different situations land on the same grey square. A
    non-image type is never even requested, while an image type that the
    decoder refuses has already been downloaded and checked.

    A private file is the third case and it needs its own sentence in
    both directions. Its bytes are an encryption envelope, so "the bytes
    are not a readable image" is true of what was downloaded and a lie
    about the file: the picture is fine, it is sealed. And when the
    decrypt itself fails, the reason is about the key, which is a
    different problem with a different fix.
    """
    mime = (media.mime_type or "").strip()
    if state in _ENCRYPTED_STATES:
        # The decoder's refusal is the expected step for an envelope, so
        # it is never worth repeating; anything else here is the private
        # preview's own reason and already reads as a sentence.
        if preview_error and preview_error != "not an image":
            return preview_error
        return ""
    if not mime.startswith("image/"):
        label = mime or "unknown type"
        return f"none, {label} is not an image"
    if preview_error == "not an image":
        return "downloaded, but the bytes are not a readable image"
    if preview_error:
        return preview_error
    return ""


def _placeholder_icon(
    mime_type: str, state: str = PUBLIC, is_dark: bool = True,
) -> QIcon:
    """Generate a coloured placeholder icon based on the media type."""
    pix = QPixmap(_THUMB_SIZE, _THUMB_SIZE)
    if state in _ENCRYPTED_STATES:
        # A private file's type on the server is the envelope's, not the
        # picture's, so the type-coloured squares above would tell the
        # user something false. Neutral until the decrypt says otherwise.
        pix.fill(Qt.GlobalColor.darkGray)
    elif (mime_type or "").startswith("image/"):
        pix.fill(Qt.GlobalColor.darkGray)
    elif (mime_type or "").startswith("video/"):
        pix.fill(Qt.GlobalColor.darkBlue)
    elif (mime_type or "").startswith("audio/"):
        pix.fill(Qt.GlobalColor.darkGreen)
    else:
        pix.fill(Qt.GlobalColor.gray)
    return _chipped_icon(pix, state, is_dark)


def _chipped_icon(pix: QPixmap, state: str, is_dark: bool) -> QIcon:
    """The thumbnail with its visibility chip drawn on, if it has one.

    The chip is painted onto the pixmap rather than styled onto the item
    because an item's text is one colour and the selection takes that
    colour over. A cue that disappears the moment a file is selected is
    a cue that is missing exactly when the user is about to act on it.
    """
    colors = _CHIP_COLORS[bool(is_dark)].get(state)
    text = _CHIP_TEXT.get(state, "")
    if colors is None or not text or pix.isNull():
        return QIcon(pix)
    fill, ink = colors

    stamped = QPixmap(pix)
    painter = QPainter(stamped)
    try:
        painter.setRenderHint(QPainter.Antialiasing, True)
        font = QFont(painter.font())
        font.setBold(True)
        font.setPointSize(max(7, font.pointSize() - 2))
        painter.setFont(font)

        metrics = painter.fontMetrics()
        text_w = metrics.horizontalAdvance(text)
        text_h = metrics.height()
        chip = QRect(
            _CHIP_MARGIN,
            stamped.height() - text_h - 2 * _CHIP_PAD_Y - _CHIP_MARGIN,
            text_w + 2 * _CHIP_PAD_X,
            text_h + 2 * _CHIP_PAD_Y,
        )
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(fill))
        painter.drawRoundedRect(chip, 3, 3)
        painter.setPen(QColor(ink))
        painter.drawText(chip, Qt.AlignCenter, text)
    finally:
        painter.end()
    return QIcon(stamped)


def _suggested_save_name(media: MediaFile) -> str:
    """Pick a sensible default filename for the Save dialog.

    Blossom doesn't carry filenames, the URL path is the sha256. We
    use the hash prefix + the mime-derived extension so the user gets
    something they can recognize and re-rename if they want.
    """
    import mimetypes
    ext = mimetypes.guess_extension(media.mime_type or "") or ""
    if not ext and media.mime_type:
        # mimetypes can return None for some common types we still want
        # to honor, so handle a couple by hand.
        ext_map = {
            "image/webp": ".webp",
            "image/svg+xml": ".svg",
            "video/quicktime": ".mov",
        }
        ext = ext_map.get(media.mime_type, "")
    return f"{media.hash[:12]}{ext}"


def _hostname_from_url(url: str) -> str:
    try:
        from urllib.parse import urlparse
        return urlparse(url).hostname or ""
    except (ValueError, AttributeError):
        return ""


def _paste_upload_name() -> str:
    """Unique-per-process display name for a clipboard upload job.

    ``int(time.time())`` collides when the user pastes twice inside one
    second; ``int(time.time() * 1000)`` is finer but still racy on fast
    hardware. A counter sourced from ``itertools.count`` is monotonic
    by construction and reads cleanly in the status bar.
    """
    return f"clipboard-{int(time.time())}-{next(_paste_counter):03d}.png"


def _styled_combo() -> QComboBox:
    """A QComboBox that respects the dialog's stylesheet on every platform.

    On macOS, an unstyled non-editable QComboBox can still pop up the
    *native* AppKit menu, which ignores QSS. Swapping the view for a
    QListView forces Qt to render its own styled popup, which means our
    padding / item-height / hover styles actually apply.
    """
    combo = QComboBox()
    combo.setView(QListView())
    return combo
