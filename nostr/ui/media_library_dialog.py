# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Media Library: the person's images, videos, audio and PDFs.

One window for both uses: browsing (non-modal, from Nostr > Media
Library…) and picking (modal: Insert Image, an article's cover). It is
built the way a Finder window or Photos is:

- a toolbar with Add… (the system's file dialog), the kind of file to
  show, the order, a search field, and an action menu (⋯) with the
  selected files' commands, Refresh and Media Servers…;
- a scrolling grid of tiles (``media_grid``), several of them selectable;
- Quick Look on Space (``media_preview``); Return or a double-click
  inserts when picking and otherwise opens Quick Look;
- the same commands in a tile's context menu, in plain verbs: Insert,
  Quick Look, Copy Link, Open in Browser, Save As…, Delete;
- a file being added is a tile with its own progress and Cancel, and one
  that failed says why, with Retry: nothing waits in a modal;
- one quiet line at the bottom for what just happened, and how much of
  an allowance is used when a server gives one ("1.2 GB of 5 GB used").

What stays in the background: several servers, copies on each, a server
taking over when one cannot take a file, a server left out when it is
full. And privacy is handled rather than explained. Everything MyEditor
uploads can be opened by anyone with its link (a media server serves a
file to whoever has its address), so that is the normal case: it is said
once, in the quiet line at the foot of the window, and where a file is
added, and no tile is marked for it. The exception is marked: a private
file from another app, readable only by the person, has a small lock.
Choosing a private one to insert asks before it is shared
(``publish_copy_dialog``). When nobody
could check whether a file is private, the picker says so under the grid
and offers Try Again, because using such a file could share something
private; the library itself says nothing.

Private thumbnails are real thumbnails: the bytes are decrypted in
memory and handed straight to Qt, and nothing decrypted is ever written
to the shared blob cache, because that cache is what an export walks.
"""

from __future__ import annotations

import dataclasses
import os
import re
import unicodedata
from typing import Callable, Dict, List, Optional

from PySide6.QtCore import (
    QBuffer, QDateTime, QEvent, QIODevice, QSize, Qt, QTimer, QUrl, Signal,
)
from PySide6.QtGui import QAction, QCursor, QDesktopServices, QImageReader, QKeySequence, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

import i18n
import media_files
import theme
import url_safety
from alerts import confirm_destructive
from . import appearance
from i18n import _, ngettext
from media_files import format_size  # noqa: F401, imported from here too

from ..blossom.errors import ERROR_CODES, friendly_message
from ..blossom.store import MediaFile, MediaStore
from ..media.media_visibility import PRIVATE, PUBLISHED_COPY, MediaVisibility
from ..media.private_library import PrivateLibrary
from .media_grid import (
    FAILED,
    FINISHING,
    SENDING,
    THUMB,
    TILE_ROLE,
    WAITING,
    MediaGrid,
    MediaModel,
    Tile,
)
from .media_preview import QuickLook
from .media_source import PreviewResolver
from .media_thumbnails import MediaThumbnails
from .media_saver import MediaSaver, save_failure, saves_failure
from .thumbnail_loader import ThumbnailLoader

# Types a file being added is shown from its own bytes while it uploads:
# the ones the app decodes anyway.
_PREVIEWABLE = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp"}
_PREVIEW_MAX_BYTES = 40 * 1024 * 1024


def kind_of_mime(mime: str) -> str:
    return media_files.library_kind(mime)


class _DropPage(QWidget):
    """The page where the grid would be (empty, loading, no match): files
    dropped on it are added, as on the grid."""

    def __init__(self, add) -> None:
        super().__init__()
        self._add = add
        self.setAcceptDrops(True)

    @staticmethod
    def _paths(event) -> List[str]:
        data = event.mimeData()
        return [u.toLocalFile() for u in data.urls() if u.isLocalFile()] if data.hasUrls() else []

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if self._paths(event):
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if self._paths(event):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802
        paths = self._paths(event)
        if paths:
            event.acceptProposedAction()
            self._add(paths)


class MediaLibraryDialog(QDialog):
    """The Media Library window, browsing or picking."""

    # The picked file and its alternative text (empty without the field).
    file_picked = Signal(object, str)
    # Media Servers…: the window that owns the settings opens the sheet.
    servers_requested = Signal()

    def __init__(
        self,
        *,
        store: MediaStore,
        is_dark: bool = True,
        pick_mode: bool = False,
        pick_alt_text: bool = True,
        visibility: Optional[MediaVisibility] = None,
        saver: Optional[MediaSaver] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(_("Insert from Media Library") if pick_mode else _("Media Library"))
        self.setModal(pick_mode)
        self.resize(860, 560)
        # Deleted on close, so its connections to the store and the
        # loader go with it.
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.apply_theme(is_dark)
        appearance.bind(self)

        self._store = store
        self._is_dark = is_dark
        self._pick_mode = pick_mode
        # Browsing, a file goes into the document being written with Insert
        # (set_inserter): whether one can take it now, and how.
        self._inserter: Optional[Callable[[MediaFile, str], None]] = None
        self._can_take: Callable[[], bool] = lambda: False
        # Empty by default: with no private library wired there is
        # nothing to be private of, and every file is public.
        self._visibility = visibility or MediaVisibility()
        self._private_library: Optional[PrivateLibrary] = None
        self._loader = ThumbnailLoader(parent=self)
        self._previews = MediaThumbnails(self._loader, self._visibility, parent=self)
        self._previews.ready.connect(self._set_thumbnail)
        self._preview_asked: set[str] = set()
        self._thumbs: Dict[str, QPixmap] = {}
        self._asked: set = set()
        # Files added from this window, by job name, until they land.
        self._jobs: Dict[str, Tile] = {}
        # Redraws their indicators while any is in flight (about 30 times a
        # second, so a spinning or creeping ring moves smoothly).
        self._ticker = QTimer(self)
        self._ticker.setInterval(33)
        self._ticker.timeout.connect(self._tick)
        # What a finished upload has to say (a copy a server did not
        # keep), said once it is added.
        self._job_notes: Dict[str, str] = {}
        # What the store said about a job before its tile was made (a
        # small file starts, or is refused, inside upload_file): the
        # tile's state, and for a failure its reason, by job name.
        self._early: Dict[str, tuple] = {}
        self._adding = False
        # Shared copies being deleted, by the copy's sha256: the
        # original's name, and whether it stays (Stop Sharing) or goes
        # with them (Delete).
        self._unsharing: Dict[str, tuple] = {}
        self._deleting: Dict[str, int] = {}
        # Save As: the saver of what outlives this window (the main
        # window), so a save goes on when it closes; else its own, whose
        # save stops when it closes.
        self._owns_saver = saver is None
        self._saver = saver if saver is not None else MediaSaver(self._loader, parent=self)
        self._quick_look: Optional[QuickLook] = None
        self._fetching = False
        self._fetch_failed = ""

        self._build(pick_alt_text and pick_mode)
        self._rest_status()
        if self._saver.current:              # a save still running from a window before
            self._save_bar.setValue(self._saver.percent)
            self._show_saving(self._saver.current)
        self._adopt_running_uploads()
        # No minimum width of its own: the layout's is the smallest window,
        # every control at its own width in every language, and the grid
        # at least two columns.
        self.setMinimumHeight(400)
        self._connect()
        self._rebuild()
        self._refresh_storage()
        self._grid.setFocus(Qt.FocusReason.OtherFocusReason)
        store.fetch()

    # ------------------------------------------------------------------
    # Building
    # ------------------------------------------------------------------

    def apply_theme(self, is_dark: bool) -> None:
        self._is_dark = is_dark
        self.setStyleSheet(appearance.dialog_stylesheet(is_dark) + _extra_css(is_dark))
        appearance.surface(self, is_dark)
        if hasattr(self, "_grid"):
            self._grid.apply_theme(is_dark)
        if getattr(self, "_quick_look", None) is not None:
            self._quick_look.apply_theme(is_dark)

    def _build(self, alt_text: bool) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(self._build_toolbar())

        self._model = MediaModel(self)
        self._grid = MediaGrid(is_dark=self._is_dark)
        self._grid.setModel(self._model)
        self._grid.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._grid.customContextMenuRequested.connect(self._context_menu)
        self._grid.selectionModel().selectionChanged.connect(self._selection_changed)
        self._grid.open_requested.connect(self._open)
        self._grid.quick_look_requested.connect(self._toggle_quick_look)
        self._grid.delete_requested.connect(self._delete)
        self._grid.copy_requested.connect(self._copy_links)
        self._grid.save_requested.connect(self._save_as)
        self._grid.paste_requested.connect(self._paste)
        self._grid.files_dropped.connect(self._add_paths)
        self._grid.cancel_requested.connect(self._cancel_upload)
        self._grid.retry_requested.connect(self._retry_upload)
        self._grid.visible_changed.connect(self._load_visible)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._grid)
        self._stack.addWidget(self._build_empty_page())
        layout.addWidget(self._stack, 1)

        # The picker's one sentence about the file chosen, when it cannot
        # be used yet, with the way out.
        self._pick_notice = QWidget()
        notice = QHBoxLayout(self._pick_notice)
        notice.setContentsMargins(14, 6, 14, 0)
        self._pick_notice_text = QLabel()
        self._pick_notice_text.setWordWrap(True)
        self._pick_notice_text.setTextFormat(Qt.TextFormat.PlainText)
        self._pick_notice_button = QPushButton(_("Try Again"))
        self._pick_notice_button.setAutoDefault(False)
        self._pick_notice_button.clicked.connect(self._check_private_again)
        notice.addWidget(self._pick_notice_text, 1)
        notice.addWidget(self._pick_notice_button)
        self._pick_notice.hide()
        layout.addWidget(self._pick_notice)

        footer = QHBoxLayout()
        footer.setContentsMargins(14, 6, 14, 8)
        footer.setSpacing(12)
        self._status = QLabel()
        self._status.setObjectName("media_status")
        self._status.setWordWrap(True)
        self._status.setTextFormat(Qt.TextFormat.PlainText)
        self._status.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        # What is true of every file here, said once, the way a Finder or
        # Photos window says it at its foot rather than on each item. A
        # message about something that just happened takes its place for
        # a while; a problem stays until something else is said.
        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.setInterval(15_000)
        self._status_timer.timeout.connect(self._rest_status)
        self._storage = QLabel()
        self._storage.setObjectName("media_hint")
        self._storage.setTextFormat(Qt.TextFormat.PlainText)
        footer.addWidget(self._status, 1)
        self._save_bar = QProgressBar()
        self._save_bar.setRange(0, 100)
        self._save_bar.setTextVisible(False)
        self._save_bar.setFixedWidth(120)
        self._save_bar.setAccessibleName(_("Saving"))
        self._save_bar.hide()
        # "Stop", not "Cancel": the picker's Cancel, beside it, closes the
        # window.
        self._save_stop = QPushButton(_("Stop"))
        self._save_stop.setAccessibleName(_("Stop Saving"))
        self._save_stop.setAutoDefault(False)
        self._save_stop.clicked.connect(self._cancel_save)
        self._save_stop.hide()
        footer.addWidget(self._save_bar)
        footer.addWidget(self._save_stop)
        self._saver.started.connect(self._on_save_started)
        self._saver.progress.connect(self._save_bar.setValue)
        self._saver.finished.connect(self._on_save_finished)
        self._saver.batch_finished.connect(self._on_saves_finished)
        footer.addWidget(self._storage, 0,
                         Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(footer)

        self._alt_edit: Optional[QLineEdit] = None
        self._insert_button: Optional[QPushButton] = None
        if self._pick_mode:
            layout.addLayout(self._build_pick_bar(alt_text))

        find = QAction(self)
        find.setShortcut(QKeySequence.StandardKey.Find)
        find.triggered.connect(lambda: self._search.setFocus(Qt.FocusReason.ShortcutFocusReason))
        self.addAction(find)

    def _build_toolbar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setContentsMargins(14, 12, 14, 10)
        bar.setSpacing(8)
        self._add_button = QPushButton(_("Add…"))
        self._add_button.setToolTip(_("Add images, videos, audio or files from your computer"))
        self._add_button.setAutoDefault(False)
        self._add_button.clicked.connect(self._choose_files)
        self._add_button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        bar.addWidget(self._add_button)
        self._filter_combo = QComboBox()
        self._filter_combo.setAccessibleName(_("Show"))
        for kind, label in (("all", _("All Items")), ("image", _("Images")),
                            ("video", _("Videos")), ("audio", _("Audio")), ("file", _("Files"))):
            self._filter_combo.addItem(label, kind)
        self._filter_combo.currentIndexChanged.connect(self._rebuild)
        self._filter_combo.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        bar.addWidget(self._filter_combo)
        self._sort_combo = QComboBox()
        self._sort_combo.setAccessibleName(_("Sort By"))
        for key, label in (("newest", _("Newest First")), ("oldest", _("Oldest First")),
                           ("largest", _("Largest First")), ("smallest", _("Smallest First")),
                           ("name", _("Name"))):
            self._sort_combo.addItem(label, key)
        self._sort_combo.currentIndexChanged.connect(self._rebuild)
        self._sort_combo.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        bar.addWidget(self._sort_combo)
        bar.addStretch(1)
        self._search = QLineEdit()
        self._search.setPlaceholderText(_("Search"))
        self._search.setAccessibleName(_("Search Media"))
        self._search.setClearButtonEnabled(True)
        self._search.setMinimumWidth(90)
        self._search.setMaximumWidth(220)
        self._search.textChanged.connect(self._rebuild)
        # Escape clears it and Return goes to the results, as in the
        # Finder (see eventFilter).
        self._search.installEventFilter(self)
        bar.addWidget(self._search, 1)
        self._actions_button = QToolButton()
        self._actions_button.setObjectName("media_actions")
        self._actions_button.setText("⋯")
        self._actions_button.setAccessibleName(_("Actions"))
        self._actions_button.setToolTip(_("Actions"))
        self._actions_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._actions_menu = QMenu(self._actions_button)
        self._actions_menu.aboutToShow.connect(self._fill_actions_menu)
        self._actions_button.setMenu(self._actions_menu)
        bar.addWidget(self._actions_button)
        return bar

    def _build_empty_page(self) -> QWidget:
        page = _DropPage(self._add_paths)
        column = QVBoxLayout(page)
        column.addStretch(1)
        self._empty_text = QLabel()
        self._empty_text.setObjectName("media_empty")
        self._empty_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_text.setWordWrap(True)
        self._empty_text.setTextFormat(Qt.TextFormat.PlainText)
        column.addWidget(self._empty_text)
        self._empty_button = QPushButton()
        self._empty_button.setAutoDefault(False)
        self._empty_button.clicked.connect(self._empty_action)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self._empty_button)
        row.addStretch(1)
        column.addLayout(row)
        column.addStretch(2)
        self._empty = page
        return page

    def _build_pick_bar(self, alt_text: bool) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(14, 0, 14, 14)
        row.setSpacing(8)
        if alt_text:
            label = QLabel(_("Alt text:"))
            self._alt_edit = QLineEdit()
            label.setBuddy(self._alt_edit)
            self._alt_edit.setAccessibleName(_("Alt text"))
            self._alt_edit.setPlaceholderText(_("Describe the image for people who can’t see it"))
            self._alt_edit.setClearButtonEnabled(True)
            row.addWidget(label)
            row.addWidget(self._alt_edit, 1)
        else:
            row.addStretch(1)
        cancel = QPushButton(_("Cancel"))
        cancel.setAutoDefault(False)
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        self._insert_button = QPushButton(_("Insert"))
        self._insert_button.setDefault(True)
        self._insert_button.clicked.connect(self._insert)
        row.addWidget(self._insert_button)
        return row

    def _connect(self) -> None:
        store = self._store
        # Bound methods: Qt drops them when this window is deleted.
        store.library_changed.connect(self._rebuild)
        store.library_changed.connect(self._refresh_storage)
        store.listings_changed.connect(self._refresh_storage)
        store.fetch_started.connect(self._on_fetch_started)
        store.fetch_finished.connect(self._on_fetch_finished)
        store.fetch_error.connect(self._on_fetch_error)
        store.upload_started.connect(self._on_upload_started)
        store.upload_progress.connect(self._on_upload_progress)
        store.upload_status.connect(self._on_upload_status)
        store.upload_finished.connect(self._on_upload_finished)
        store.upload_failed.connect(self._on_upload_failed)
        store.upload_cancelled.connect(self._on_upload_cancelled)
        store.mirror_failed.connect(self._on_copy_missing)
        store.server_skipped.connect(self._on_copy_missing)
        store.file_deleted.connect(self._on_file_deleted)
        store.delete_failed.connect(self._on_delete_failed)
        self._loader.ready.connect(self._on_thumbnail_ready)
        self._loader.failed.connect(self._on_thumbnail_failed)

    # ------------------------------------------------------------------
    # The private library
    # ------------------------------------------------------------------

    def bind_private_library(self, library: PrivateLibrary) -> None:
        """Follow the private library: a file that turns out private gets
        its lock, and the picker learns when the check failed."""
        self._private_library = library
        library.library_changed.connect(self._on_private_library_changed)
        library.status_changed.connect(self._on_private_library_status)
        self._on_private_library_changed()

    def _on_private_library_changed(self) -> None:
        self._previews.cancel()
        # A decrypted thumbnail belongs to the account that opened it.
        # Rebuild from that account's current privacy records, rather
        # than retaining pixels across an account switch or key change.
        self._thumbs.clear()
        self._asked.clear()
        self._preview_asked.clear()
        if self._quick_look is not None:
            self._quick_look.invalidate()
        self._rebuild()

    def _on_private_library_status(self, _text: str) -> None:
        self._refresh_pick_notice()

    def _check_private_again(self) -> None:
        if self._private_library is not None:
            self._private_library.refresh()
        self._refresh_pick_notice()

    # ------------------------------------------------------------------
    # Tiles
    # ------------------------------------------------------------------

    def _rebuild(self, *_args) -> None:
        """Lay the grid out again from the store, keeping the selection."""
        selected = [tile.key for tile in self._grid.selected_tiles()]
        current = self._grid.currentIndex().data(TILE_ROLE)
        self._model.set_tiles(self._job_tiles() + self._file_tiles())
        selection = self._grid.selectionModel()
        for key in selected:
            row = self._model.row_of(key)
            if row >= 0:
                selection.select(self._model.index(row), selection.SelectionFlag.Select)
        if current is not None and self._model.row_of(current.key) >= 0:
            selection.setCurrentIndex(self._model.index(self._model.row_of(current.key)),
                                      selection.SelectionFlag.NoUpdate)
        self._show_page()
        self._selection_changed()

    def _matches(self, tile: Tile) -> bool:
        kind = self._filter_combo.currentData() or "all"
        text = self._search.text().strip().casefold()
        return (kind == "all" or tile.kind == kind) and (not text or text in tile.name.casefold())

    def _job_tiles(self) -> List[Tile]:
        return [tile for tile in self._jobs.values() if self._matches(tile)]

    def _file_tiles(self) -> List[Tile]:
        sort = self._sort_combo.currentData() or "newest"
        files = self._store.file_list(filter_type="all",
                                      sort_by="newest" if sort == "name" else sort)
        tiles = [self._file_tile(media) for media in files if not self._is_hidden_copy(media.hash)]
        tiles = [tile for tile in tiles if self._matches(tile)]
        if sort == "name":
            tiles.sort(key=lambda tile: tile.name.casefold())
        return tiles

    def _is_hidden_copy(self, sha: str) -> bool:
        """A public copy whose private original is in the library is not
        a tile of its own: one picture, one tile (and, shared, no lock)."""
        source = self._visibility.copy_source(sha)
        return bool(source) and source in self._store.files \
            and self._visibility.state_of(source) == PUBLISHED_COPY

    def _file_tile(self, media: MediaFile) -> Tile:
        name = self._store.name_of(media.hash) or self._visibility.name_of(media.hash)
        mime = (self._visibility.declared_mime(media.hash) or media.mime_type or "").lower().split(";", 1)[0].strip()
        if mime in ("", "application/octet-stream"):
            mime = media_files.type_of(name) or mime
        kind = kind_of_mime(mime)
        day = _day_of(media)
        name = name or day or kind_word_title(kind)
        detail = day if name != day else format_size(media.size)
        if kind == "file":
            detail = media_files.file_type_label(mime) + " · " + format_size(media.size)
        state = self._visibility.state_of(media.hash)
        return Tile(key=media.hash, name=name, detail=detail, kind=kind,
                    private=state == PRIVATE, media=media,
                    pixmap=self._thumbs.get(media.hash), mime=mime)

    def _show_page(self) -> None:
        """The grid, or one plain sentence where it would be."""
        if self._model.rowCount():
            self._stack.setCurrentWidget(self._grid)
            return
        text = self._search.text().strip()
        button = ""
        if self._fetch_failed and not self._store.files:
            message, button = self._fetch_failed, _("Try Again")
        elif text:
            message = _("No items match “{text}”.").format(text=text)
        elif self._fetching:
            message = _("Loading…")
        else:
            message = _("Images, videos, audio and files you add appear here. Anyone with "
                        "the link can see them.")
            button = _("Add…")
        self._empty_text.setText(message)
        self._empty_button.setText(button)
        self._empty_button.setVisible(bool(button))
        self._stack.setCurrentWidget(self._empty)

    def _empty_action(self) -> None:
        if self._fetch_failed:
            self._refresh()
        else:
            self._choose_files()

    # ------------------------------------------------------------------
    # Thumbnails, as tiles come into view
    # ------------------------------------------------------------------

    def _load_visible(self) -> None:
        for key in self._grid.visible_keys():
            tile = self._model.tile(key)
            if tile is None or not tile.is_file or key in self._thumbs or key in self._asked:
                continue
            if self._wants_picture(tile):
                self._asked.add(key)
                self._loader.load(key, tile.media.url)
            elif (tile.kind == "video" or tile.mime == "application/pdf") \
                    and key not in self._preview_asked and not self._visibility.is_unknown(key):
                self._preview_asked.add(key)
                self._previews.request(tile.media, tile.mime)

    def _wants_picture(self, tile: Tile) -> bool:
        """A picture, whatever the server calls it: a private picture is
        an envelope the server calls a byte stream, opened here when the
        decoder refuses it."""
        if self._visibility.is_private(tile.key):
            declared = self._visibility.declared_mime(tile.key)
            return tile.kind == "image" or (not declared and tile.kind == "file")
        return tile.kind == "image"

    def _on_thumbnail_ready(self, sha: str, _path: str, pix: QPixmap) -> None:
        media = self._store.files.get(sha)
        if media is not None and not media.width and not pix.isNull():
            media.width, media.height = pix.width(), pix.height()
        self._set_thumbnail(sha, pix)
        if self._quick_look_key() == sha:
            self._quick_look.set_picture(pix)

    def _on_thumbnail_failed(self, sha: str, reason: str) -> None:
        if reason == "not an image" and not self._visibility.is_private(sha):
            # A picture this system cannot draw (HEIC on most): the tile
            # shows its kind instead of an empty square.
            tile = self._model.tile(sha)
            if tile is not None:
                tile.no_picture = True
                self._model.changed(sha)
            return
        if reason == "not an image":
            # The bytes are in the cache. A private picture is sealed, not
            # broken: it is opened here, in memory.
            if self._visibility.is_private(sha):
                picture = self._private_picture(sha)
                if picture is not None:
                    self._set_thumbnail(sha, picture)
                    if self._quick_look_key() == sha:
                        self._quick_look.set_picture(picture)

    def _private_picture(self, sha: str) -> Optional[QPixmap]:
        try:
            envelope = self._loader.cache_path(sha).read_bytes()
        except OSError:
            return None
        outcome = self._visibility.preview(sha, envelope)
        del envelope
        return QPixmap.fromImage(outcome.image) if outcome.ok else None

    def _set_thumbnail(self, sha: str, pix: QPixmap) -> None:
        if sha not in self._store.files:
            return
        ratio = self.devicePixelRatioF()
        side = int(THUMB * ratio)
        tile = self._model.tile(sha)
        aspect = (Qt.AspectRatioMode.KeepAspectRatio if tile is not None and tile.kind == "file"
                  else Qt.AspectRatioMode.KeepAspectRatioByExpanding)
        scaled = pix.scaled(QSize(side, side), aspect,
                            Qt.TransformationMode.SmoothTransformation)
        scaled.setDevicePixelRatio(ratio)
        self._thumbs[sha] = scaled
        if tile is not None:
            tile.pixmap = scaled
            self._model.changed(sha)

    # ------------------------------------------------------------------
    # Selection and the commands on it
    # ------------------------------------------------------------------

    def _selected_files(self) -> List[Tile]:
        return [tile for tile in self._grid.selected_tiles() if tile.is_file]

    def _selection_changed(self, *_args) -> None:
        self._refresh_pick_notice()
        if self._alt_edit is not None:
            # Alternative text describes a picture; audio, video and files
            # go in by their names.
            files = self._selected_files()
            self._alt_edit.setEnabled(not files or files[0].kind == "image")
        if self._quick_look is not None and self._quick_look.isVisible():
            self._show_in_quick_look()

    def _fill_actions_menu(self) -> None:
        menu = self._actions_menu
        menu.clear()
        self._add_file_commands(menu, self._grid.selected_tiles())
        if menu.actions():
            menu.addSeparator()
        menu.addAction(_("Refresh"), self._refresh)
        if not self._pick_mode:
            # Servers are managed from the library, not while choosing.
            menu.addAction(_("Media Servers…"), self.servers_requested.emit)

    def _context_menu(self, point) -> None:
        index = self._grid.indexAt(point)
        if not index.isValid():
            return
        if not self._grid.selectionModel().isSelected(index):
            self._grid.setCurrentIndex(index)
        menu = QMenu(self)
        self._add_file_commands(menu, self._grid.selected_tiles())
        if menu.actions():
            menu.exec(QCursor.pos())

    def _add_file_commands(self, menu: QMenu, tiles: List[Tile]) -> None:
        """The commands for these tiles, as the context menu and the
        action menu both list them."""
        files = [tile for tile in tiles if tile.is_file]
        failed = [tile.key for tile in tiles if tile.state == FAILED]
        sending = [tile.key for tile in tiles if tile.state in (WAITING, SENDING)]
        if failed:
            menu.addAction(_("Retry"), lambda: [self._retry_upload(k) for k in failed])
            menu.addAction(_("Remove"), lambda: [self._dismiss_upload(k) for k in failed])
        if sending:
            menu.addAction(_("Cancel Upload"), lambda: [self._cancel_upload(k) for k in sending])
        if not files:
            return
        if failed or sending:
            menu.addSeparator()
        if self._pick_mode:
            menu.addAction(_("Insert"), self._insert).setEnabled(self._can_insert())
        elif self._inserter is not None:
            menu.addAction(_("Insert in Document"), self._insert).setEnabled(
                self._can_insert() and self._can_take())
            menu.addSeparator()
        menu.addAction(_("Quick Look"), self._toggle_quick_look)
        menu.addSeparator()
        linked = [tile for tile in files if self._link_of(tile)]
        menu.addAction(_("Copy Link") if len(files) == 1 else _("Copy Links"),
                       self._copy_links).setEnabled(bool(linked))
        if len(files) == 1:
            menu.addAction(_("Open in Browser"), self._open_in_browser).setEnabled(bool(linked))
        menu.addAction(_("Save As…") if len(files) == 1 else _("Save to Folder…"),
                       self._save_as)
        if not self._pick_mode:
            menu.addSeparator()
            if self._shared_copies(files):
                menu.addAction(_("Stop Sharing"), self._stop_sharing)
            menu.addAction(_("Delete"), self._delete)

    # -- open, insert, quick look ------------------------------------------

    def _open(self) -> None:
        if self._pick_mode:
            self._insert()
        else:
            self._toggle_quick_look()

    def _can_insert(self) -> bool:
        files = self._selected_files()
        return len(files) == 1 and not self._visibility.is_unknown(files[0].key)

    def set_inserter(self, insert: Callable[[MediaFile, str], None],
                     can_take: Callable[[], bool]) -> None:
        """Browsing, offer Insert in Document: ``insert(file, alt)`` puts a
        file into the document being written, while ``can_take()``."""
        self._inserter = insert
        self._can_take = can_take

    def _picked(self) -> MediaFile:
        """The chosen file, of the type its tile shows: a server that calls
        a video or a picture a byte stream does not make it one."""
        tile = self._selected_files()[0]
        media = tile.media
        if tile.mime and tile.mime != (media.mime_type or "").lower():
            media = dataclasses.replace(media, mime_type=tile.mime)
        return media

    def _insert(self) -> None:
        if not self._can_insert():
            return
        if not self._pick_mode:
            if self._inserter is not None and self._can_take():
                self._inserter(self._picked(), "")
            return
        alt = self._alt_edit.text().strip() if self._alt_edit is not None else ""
        self.file_picked.emit(self._picked(), alt)
        self.accept()

    def _refresh_pick_notice(self) -> None:
        """When the chosen file cannot be used, because nobody could
        check whether it is private, the picker says so where the eye
        is, and offers the way out."""
        if not self._pick_mode:
            return
        files = self._selected_files()
        blocked = len(files) == 1 and self._visibility.is_unknown(files[0].key)
        library = self._private_library
        checking = library is not None and library.loading
        if blocked and checking:
            self._pick_notice_text.setText(
                _("MyEditor is still checking whether this image is private. One moment."))
        elif blocked:
            self._pick_notice_text.setText(
                _("This image can’t be used until your signer app lets MyEditor check "
                  "whether it is private."))
        self._pick_notice_button.setVisible(blocked and not checking)
        self._pick_notice.setVisible(blocked)
        self._insert_button.setEnabled(self._can_insert())

    def _toggle_quick_look(self) -> None:
        if self._quick_look is not None and self._quick_look.isVisible():
            self._quick_look.close()
            return
        if not self._selected_files():
            return
        if self._quick_look is None:
            resolver = PreviewResolver(self._loader, self._visibility, parent=self)
            self._quick_look = QuickLook(grid=self._grid, is_dark=self._is_dark,
                                        resolver=resolver, parent=self)
            self._quick_look.closed.connect(self._grid.setFocus)
            self._quick_look.thumbnail_ready.connect(self._set_thumbnail)
            self._quick_look.save_requested.connect(self._save_preview)
        self._show_in_quick_look()
        self._quick_look.show()
        self._quick_look.raise_()

    def _quick_look_key(self) -> str:
        if self._quick_look is None or not self._quick_look.isVisible():
            return ""
        files = self._selected_files()
        return files[0].key if files else ""

    def _show_in_quick_look(self) -> None:
        files = self._selected_files()
        if not files:
            self._quick_look.close()
            return
        tile = files[0]
        glyph = "" if tile.kind == "image" else media_files.file_glyph(tile.mime)
        picture = None
        if self._wants_picture(tile):
            picture = self._full_picture(tile.key)
            if picture is None:
                self._loader.load(tile.key, tile.media.url)
        self._quick_look.show_file(name=tile.name, detail=_quick_look_detail(tile),
                                   url=self._link_of(tile), glyph=glyph, picture=picture,
                                   media=tile.media, mime=tile.mime)

    def _full_picture(self, sha: str) -> Optional[QPixmap]:
        if not self._loader.cache_path(sha).is_file():
            return None
        if self._visibility.is_private(sha):
            return self._private_picture(sha)
        picture = QPixmap(str(self._loader.cache_path(sha)))
        return None if picture.isNull() else picture

    # -- copy, open, save ---------------------------------------------------

    def _link_of(self, tile: Tile) -> str:
        """The address that shows someone this file, or "": a shared
        original's is its public copy's (its own serves sealed bytes), and
        a private file has none."""
        copy = self._visibility.public_copy_of(tile.key)
        if copy is not None:
            return copy.url
        if self._visibility.is_private(tile.key):
            return ""
        return tile.media.url

    def _copy_links(self) -> None:
        links = [link for link in map(self._link_of, self._selected_files()) if link]
        if links:
            QApplication.clipboard().setText("\n".join(links))

    def _open_in_browser(self) -> None:
        files = self._selected_files()
        link = self._link_of(files[0]) if files else ""
        if link and url_safety.is_safe_media_url(link):
            QDesktopServices.openUrl(QUrl(link))

    def _save_as(self) -> None:
        self._save_files(self._selected_files())

    def _save_preview(self) -> None:
        files = self._selected_files()
        self._save_files(files[:1])

    def _save_files(self, files: List[Tile]) -> None:
        if not files:
            return
        if len(files) == 1:
            target, _filter = QFileDialog.getSaveFileName(
                self, _("Save As"), _save_name(files[0]), _("All Files (*)"))
            targets = [(files[0], target)] if target else []
        else:
            folder = QFileDialog.getExistingDirectory(self, _("Choose a Folder"))
            targets = [(tile, os.path.join(folder, _save_name(tile))) for tile in files] \
                if folder else []
        saves = [(tile.media, target, self._opener(tile.key)) for tile, target in targets]
        if len(files) == 1:
            # The person named it, and was asked before it replaced a file.
            for media, target, opener in saves:
                self._saver.save(media, target, opener=opener)
        elif saves:
            # A folder's names are the library's, and replace nothing.
            self._saver.save_all(saves)

    def _opener(self, sha: str):
        """What opens a private file's envelope into the file; None for a
        public one."""
        visibility = self._visibility
        return ((lambda data: visibility.open_private(sha, data))
                if visibility.is_private(sha) else None)

    def _show_saving(self, target: str) -> None:
        self._show_status(_("Saving “{name}”…").format(name=os.path.basename(target)), "")
        self._status_timer.stop()
        self._save_bar.show()
        self._save_stop.show()

    def _on_save_started(self, target: str) -> None:
        self._save_bar.setValue(0)
        self._show_saving(target)

    def _on_save_finished(self, target: str, why: str) -> None:
        self._save_bar.hide()
        self._save_stop.hide()
        if why and why != "cancelled":
            self._say(save_failure(why, target), error=True)
        else:
            self._rest_status()

    def _on_saves_finished(self, total: int, failures: list) -> None:
        """A Save to Folder ended without every file: said at its end, each
        reason in the tooltip."""
        if failures:
            text, tip = saves_failure(total, failures)
            self._say(text, error=True)
            self._status.setToolTip(tip)

    def _cancel_save(self) -> None:
        self._saver.cancel()

    # -- delete ---------------------------------------------------------------

    def _delete(self) -> None:
        if self._pick_mode:
            return
        files = self._selected_files()
        if not files:
            return
        title = (_("Delete “{name}”?").format(name=files[0].name) if len(files) == 1
                 else ngettext("Delete {count} item?", "Delete {count} items?",
                               len(files)).format(count=len(files)))
        if all(self._visibility.state_of(tile.key) == PRIVATE for tile in files):
            # Another app's private files (Lotus, say): gone from it too.
            message = ngettext(
                "It’s also gone from the other app that keeps it privately. You can’t undo "
                "this action.",
                "They’re also gone from the other apps that keep them privately. You can’t "
                "undo this action.", len(files))
        else:
            message = ngettext(
                "Anything you published with it will no longer show it. You can’t undo "
                "this action.",
                "Anything you published with them will no longer show them. You can’t undo "
                "this action.", len(files))
        if not confirm_destructive(self, title=title, message=message, action=_("Delete"),
                                   is_dark=self._is_dark, nostr=True):
            return
        # A shared original goes with its copy, or the copy would stay
        # public with no tile left to reach it.
        self._delete_copies(files, stays=False)
        for tile in files:
            # How many servers had it, to tell a partial delete from none.
            self._deleting[tile.key] = len(tile.media.urls)
            self._store.delete_file(tile.key)

    def _shared_copies(self, files: List[Tile]) -> list:
        """(tile, its public copy) for the files shared through a copy:
        private originals someone published."""
        found = []
        for tile in files:
            copy = self._visibility.public_copy_of(tile.key)
            if copy is not None:
                found.append((tile, copy))
        return found

    def _stop_sharing(self) -> None:
        """Stop Sharing, as iCloud and Photos name it: the public copy is
        deleted from every server, the private original stays."""
        shared = self._shared_copies(self._selected_files())
        if not shared:
            return
        title = (_("Stop sharing “{name}”?").format(name=shared[0][0].name) if len(shared) == 1
                 else ngettext("Stop sharing {count} item?", "Stop sharing {count} items?",
                               len(shared)).format(count=len(shared)))
        message = ngettext(
            "Anyone with the link will no longer see it, and anything you published with it "
            "will no longer show it.",
            "Anyone with the link will no longer see them, and anything you published with "
            "them will no longer show them.", len(shared))
        if confirm_destructive(self, title=title, message=message, action=_("Stop Sharing"),
                               is_dark=self._is_dark, nostr=True):
            self._delete_copies([tile for tile, _copy in shared], stays=True)

    def _delete_copies(self, files: List[Tile], *, stays: bool) -> None:
        for tile, copy in self._shared_copies(files):
            self._unsharing[copy.sha256] = (tile.name, stays)
            # Everywhere it was put: the servers its record names (one
            # since removed from the list included) and its address. The
            # ones in use now are asked too, in case it is there
            # unrecorded; one that doesn't list it never had it, so it
            # can't keep the copy shared by being unreachable today.
            self._store.delete_file(copy.sha256, servers=list(copy.servers) + [copy.url],
                                    best_effort=self._store.target_servers())

    def _on_file_deleted(self, sha: str) -> None:
        # Deleting a public copy is what un-sharing it is, so the record
        # of what is shared follows.
        self._visibility.forget_public_copy(sha)
        name, stays = self._unsharing.pop(sha, ("", False))
        if stays:
            self._say(_("“{name}” is no longer shared.").format(name=name))

    def _on_delete_failed(self, sha: str, code: str) -> None:
        if sha in self._unsharing:
            name, _stays = self._unsharing.pop(sha)
            self._say(_("“{name}” is still shared. {reason}").format(
                name=name, reason=_delete_reason(code)), error=True)
            return
        name = self._name_of(sha)
        before = self._deleting.pop(sha, 0)
        media = self._store.files.get(sha)
        if media is not None and len(media.urls) < before:
            # Some servers deleted it, and one still has it.
            text = _("“{name}” is still on one of your media servers. {reason}")
        else:
            text = _("“{name}” wasn’t deleted. {reason}")
        self._say(text.format(name=name, reason=_delete_reason(code)), error=True)

    def _name_of(self, sha: str) -> str:
        tile = self._model.tile(sha)
        if tile is not None:
            return tile.name
        return self._store.name_of(sha) or self._visibility.name_of(sha) or _("The file")

    # ------------------------------------------------------------------
    # Adding files
    # ------------------------------------------------------------------

    def _choose_files(self) -> None:
        patterns = " ".join("*" + ext for ext in media_files.EXTENSIONS)
        # R Markdown's conventional casing matters to non-native Linux dialogs.
        patterns += " *.Rmd"
        paths, _filter = QFileDialog.getOpenFileNames(
            self, _("Add to Media Library"), "",
            _("Images, Videos, Audio and Files ({patterns})").format(patterns=patterns))
        self._add_paths(paths)

    def _add_paths(self, paths: List[str]) -> None:
        refused = []
        for path in paths:
            mime = media_files.type_of(path)
            if not mime or not os.path.isfile(path):
                refused.append(os.path.basename(path))
                continue
            self._adding = True
            try:
                name = self._store.upload_file(path)
            finally:
                self._adding = False
            self._add_job(name, mime, _local_preview(mime=mime, path=path))
        if refused:
            self._say(ngettext(
                "“{name}” wasn’t added. Choose a supported image, video, audio or document file.",
                "{count} files weren’t added. Choose supported images, videos, audio or document files.",
                len(refused)).format(name=refused[0], count=len(refused)),
                error=True)

    def _paste(self) -> None:
        """Command-V on the grid adds a pasted image."""
        clipboard = QApplication.clipboard()
        if not clipboard.mimeData().hasImage():
            return
        image = clipboard.image()
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if image.isNull() or not image.save(buffer, "PNG"):
            self._say(_("The pasted image couldn’t be read."), error=True)
            return
        body = bytes(buffer.data())
        self._adding = True
        try:
            name = self._store.add_bytes(body, label=_("Pasted Image") + ".png",
                                         mime_type="image/png")
        finally:
            self._adding = False
        self._add_job(name, "image/png", _local_preview(mime="image/png", data=body))

    def _add_job(self, name: str, mime: str, preview: Optional[QPixmap]) -> None:
        state, detail = self._early.pop(name, (WAITING, _("Preparing…")))
        if state == "cancelled":
            return
        self._jobs[name] = Tile(key=name, name=name, detail=detail, kind=kind_of_mime(mime),
                                state=state, pixmap=preview)
        self._follow(name)
        self._rebuild()
        if state == FAILED:
            self._say(_("“{name}” wasn’t added. {reason}").format(name=name, reason=detail),
                      error=True)

    def _adopt_running_uploads(self) -> None:
        """The files still being added when this window opens, from an
        earlier window or one that failed after it closed: the store
        owns them, so they are tiles here too."""
        states = {"waiting": WAITING, "sending": SENDING, "finishing": FINISHING,
                  "failed": FAILED}
        for added in self._store.added_uploads():
            state = states.get(added.state, WAITING)
            detail = added.error if state == FAILED else _("Preparing…")
            preview = _local_preview(mime=added.mime, path=added.path) if added.path else None
            self._jobs[added.name] = Tile(key=added.name, name=added.name, detail=detail,
                                          kind=kind_of_mime(added.mime), state=state,
                                          pixmap=preview)
            self._follow(added.name)

    def _cancel_upload(self, name: str) -> None:
        self._store.cancel_upload(name)

    def _retry_upload(self, name: str) -> None:
        tile = self._jobs.get(name)
        if tile is None or tile.state != FAILED:
            return
        tile.state, tile.detail, tile.meter = WAITING, _("Preparing…"), None
        self._model.changed(name)
        self._store.retry_upload(name)

    def _dismiss_upload(self, name: str) -> None:
        self._store.dismiss_upload(name)
        self._jobs.pop(name, None)
        self._rebuild()

    # -- the store's word on them -----------------------------------------

    def _on_upload_started(self, name: str) -> None:
        tile = self._jobs.get(name)
        if tile is not None:
            tile.state = SENDING
            self._follow(name)
        elif self._adding:
            self._early[name] = (SENDING, _("Preparing…"))

    def _on_upload_progress(self, name: str, _sent: int, _total: int) -> None:
        self._follow(name)

    def _on_upload_status(self, name: str, status: str) -> None:
        tile = self._jobs.get(name)
        if tile is not None and status == "mirroring":
            tile.state = FINISHING
        self._follow(name)

    # -- their indicators ----------------------------------------------------

    def _follow(self, name: str) -> None:
        """Show how far the file being added as ``name`` has got, from its
        upload's own indicator, and keep it moving while it is in flight."""
        tile = self._jobs.get(name)
        if tile is None or not tile.in_flight:
            return
        job = self._store.upload_job(name)
        if job is not None:
            tile.meter = job.meter
        tile.detail = tile.meter.words() if tile.meter is not None else _("Preparing…")
        self._model.changed(name)
        if not self._ticker.isActive():
            self._ticker.start()

    def _tick(self) -> None:
        moving = [name for name, tile in self._jobs.items() if tile.in_flight]
        if not moving:
            self._ticker.stop()
        for name in moving:
            self._follow(name)

    def _on_upload_finished(self, name: str, media: object) -> None:
        tile = self._jobs.pop(name, None)
        if tile is None:
            return
        sha = getattr(media, "hash", "")
        if tile.pixmap is not None and sha:
            self._thumbs.setdefault(sha, tile.pixmap)
        note = self._job_notes.pop(name, "")
        self._rebuild()
        if sha and self._model.row_of(sha) >= 0:
            self._grid.setCurrentIndex(self._model.index(self._model.row_of(sha)))
        if note:
            self._say(note)

    def _on_upload_failed(self, name: str, reason: str) -> None:
        tile = self._jobs.get(name)
        if tile is None:
            if self._adding:
                # Refused inside upload_file, before its tile was made.
                self._early[name] = (FAILED, reason)
            return
        self._job_notes.pop(name, None)
        tile.state, tile.detail = FAILED, reason
        self._model.changed(name)
        self._say(_("“{name}” wasn’t added. {reason}").format(name=name, reason=reason),
                  error=True)
        self._refresh_pick_notice()

    def _on_upload_cancelled(self, name: str) -> None:
        if self._jobs.pop(name, None) is not None:
            self._job_notes.pop(name, None)
            self._rebuild()
        elif self._adding:
            self._early[name] = ("cancelled", "")

    def _on_copy_missing(self, name: str, _host: str, _why: str) -> None:
        """A server did not take a copy, or was full. The file is on the
        others, so it is said once, when it is added, and plainly."""
        if name in self._jobs:
            self._job_notes[name] = _(
                "“{name}” is added, but one of your media servers didn’t keep a copy."
            ).format(name=name)

    # ------------------------------------------------------------------
    # Listing, storage, and the line at the bottom
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        self._fetch_failed = ""
        self._store.fetch(force=True)
        library = self._private_library
        if library is not None and not library.loading and not library.settled:
            library.refresh()
        self._show_page()

    def _on_fetch_started(self) -> None:
        self._fetching = True
        self._show_page()

    def _on_fetch_finished(self) -> None:
        self._fetching = False
        self._show_page()
        if any(listing.ok and listing.truncated
               for listing in self._store.server_listings().values()):
            self._say(_("Your media servers may not have listed every file. Refresh to "
                        "look again."))

    def _on_fetch_error(self, code: str) -> None:
        if code == ERROR_CODES.NETWORK_UNAVAILABLE:
            text = _("Couldn’t reach your media servers. Check your internet connection.")
        else:
            text = friendly_message(code)
        if self._store.files:
            self._say(text, error=True)
        else:
            self._fetch_failed = text
            self._show_page()

    def _refresh_storage(self) -> None:
        listings = self._store.server_listings()
        lines = []
        for server in self._store.target_servers():
            origin = url_safety.origin_of(server) or server
            quota = self._store.quota_for(origin)
            line = storage_text(self._store.bytes_on(origin), quota,
                                listings.get(origin)) if quota else ""
            if line:
                lines.append(line)
        self._storage.setText("  ·  ".join(lines))
        self._storage.setVisible(bool(lines))

    def _say(self, text: str, *, error: bool = False) -> None:
        self._show_status(text, "error" if error else "")
        if error:
            self._status_timer.stop()
        else:
            self._status_timer.start()

    def _rest_status(self) -> None:
        self._show_status(_("Anyone with the link can see the files you add here."), "rest")

    def _show_status(self, text: str, tone: str) -> None:
        self._status.setText(text)
        self._status.setToolTip("")
        self._status.setProperty("tone", tone)
        self._status.style().unpolish(self._status)
        self._status.style().polish(self._status)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if watched is self._search and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key == Qt.Key.Key_Escape and self._search.text():
                self._search.clear()
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                # To the results, not the default button: a search is
                # not a choice.
                if not self._grid.currentIndex().isValid() and self._model.rowCount():
                    self._grid.setCurrentIndex(self._model.index(0))
                self._grid.setFocus(Qt.FocusReason.OtherFocusReason)
                return True
        return super().eventFilter(watched, event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        # A Finder or Photos window does not close on Escape; a picker,
        # a question being answered, does (Cancel).
        if event.key() == Qt.Key.Key_Escape and not self._pick_mode:
            event.accept()
            return
        super().keyPressEvent(event)

    def showEvent(self, event) -> None:  # noqa: N802
        # The app's button style gives every push button a minimum width,
        # which a layout then takes as the button's minimum even when its
        # words need more (a longer translation). Its own width, measured
        # once styled, is the minimum here.
        for button in (self._add_button, self._actions_button):
            button.setMinimumWidth(button.sizeHint().width())
        super().showEvent(event)
        # The first layout is ready on the next turn. Initial previews
        # must not depend on the person scrolling or resizing the grid.
        QTimer.singleShot(0, self, self._load_visible)

    def done(self, result: int) -> None:
        self._previews.cancel()
        if self._quick_look is not None:
            self._quick_look.close()
        if self._owns_saver:
            # Nothing outlives this window to finish it: stopped, and
            # its part file removed. A shared saver goes on, and its
            # signals stop reaching this window when it is deleted.
            self._saver.cancel()
        super().done(result)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _extra_css(is_dark: bool) -> str:
    muted = appearance.roles(is_dark).muted
    return (f"QLabel#media_status[tone=\"error\"] {{ color: {appearance.roles(is_dark).error}; }}\n"
            f"QLabel#media_status[tone=\"rest\"], QLabel#media_hint {{ color: {muted}; }}\n"
            "QLabel#media_hint, QLabel#media_status { font-size: 12px; }\n"
            "QLabel#media_empty { font-size: 14px; }\n"
            "QToolButton#media_actions { font-size: 16px; padding: 0px 8px; }\n"
            "QToolButton#media_actions::menu-indicator { image: none; width: 0px; }\n")


def kind_word_title(kind: str) -> str:
    """A file's kind as a name, when it has no other."""
    return {"image": _("Image"), "video": _("Video"), "audio": _("Audio")}.get(kind, _("File"))


def _day_of(media: MediaFile) -> str:
    if media.uploaded_at_ms > 0:
        uploaded = QDateTime.fromMSecsSinceEpoch(int(media.uploaded_at_ms))
        if uploaded.isValid():
            return i18n.format_day(uploaded, short_month=True)
    return ""


def _quick_look_detail(tile: Tile) -> str:
    """One quiet line: kind, size, dimensions, the day it was added."""
    media = tile.media
    parts = [media_files.file_type_label(tile.mime or media.mime_type), format_size(media.size)]
    if media.width and media.height:
        parts.append(f"{media.width} × {media.height}")
    day = _day_of(media)
    if day:
        parts.append(day)
    return "  ·  ".join(parts)


def _delete_reason(code: str) -> str:
    """Why a server didn't delete a file. A refused account is the server
    the file is on refusing: choosing another server, the remedy when
    adding, can't delete it there."""
    if code == ERROR_CODES.AUTH_REJECTED:
        return _("A media server it’s on didn’t let your account delete it.")
    return friendly_message(code)


# What a file name may not hold on some system a file may be saved to
# (Windows, or a disk formatted for it): these characters, and these
# device names as the part before the first dot, in any case.
_UNSAVEABLE = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_DEVICE_NAMES = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
                 *(f"{port}{n}" for port in ("COM", "LPT") for n in "0123456789¹²³")}

# Most folders hold names of up to 255 bytes; a library name is kept
# shorter, so the number a copy gets ("Name 2.png") still fits.
_NAME_BYTES = 240


# Invisible characters a name keeps: the joiners some scripts and emoji
# need. Every other format character goes, as it hides text or turns it
# around ("invoice\u202egnp.exe" shows as "invoiceexe.png").
_KEPT_FORMAT = {"\u200c", "\u200d"}


def _save_name(tile: Tile) -> str:
    """The name a file is saved under: its own, with the extension of
    its kind when it has none, made one every system can save, as a
    system save dialog would: no character Windows refuses, none that
    hides or reorders what the name shows, nothing hidden (a leading
    dot), no device name, not too long."""
    name = "".join(c for c in tile.name
                   if c in _KEPT_FORMAT or unicodedata.category(c) != "Cf")
    name = _UNSAVEABLE.sub("-", name).strip().lstrip(".")
    stem, ext = os.path.splitext(name)
    ext = ext.rstrip(". ")
    if len(ext) > 16:                        # not an extension, a long name with a dot
        stem, ext = name, ""
    ext = ext or media_files.extension_for(tile.mime or tile.media.mime_type or "")
    stem = stem.rstrip(". ") or kind_word_title(tile.kind)
    first, dot, rest = stem.partition(".")
    if first.rstrip(" ").upper() in _DEVICE_NAMES:     # "CON.tar.gz" is CON too
        stem = first.rstrip(" ") + "-" + dot + rest
    while len((stem + ext).encode("utf-8")) > _NAME_BYTES:
        stem = stem[:-1].rstrip(". ")
    return stem + ext


def _local_preview(*, mime: str, path: str = "", data: bytes = b"") -> Optional[QPixmap]:
    """The picture of a file being added, from the computer's own copy,
    small. Only for the picture types the app decodes anyway."""
    if mime not in _PREVIEWABLE:
        return None
    buffer = None
    if path:
        try:
            if os.path.getsize(path) > _PREVIEW_MAX_BYTES:
                return None
        except OSError:
            return None
        reader = QImageReader(path)
    else:
        buffer = QBuffer()
        buffer.setData(data)
        buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        reader = QImageReader(buffer)
    reader.setAutoTransform(True)
    size = reader.size()
    if size.isValid():
        size.scale(QSize(THUMB * 2, THUMB * 2), Qt.AspectRatioMode.KeepAspectRatioByExpanding)
        reader.setScaledSize(size)
    image = reader.read()
    return None if image.isNull() else QPixmap.fromImage(image)


def storage_text(used: int, quota: int, listing) -> str:
    """The storage line for a server with a known allowance, or "" when
    its last listing did not succeed: a number from a failed listing is
    a guess, and saying nothing is better than "unavailable".

    "At least" when the listing stopped early; "Almost full" and "Full"
    in words from 90 percent, so the warning never rests on a colour.
    """
    if listing is None or not listing.ok:
        return ""
    fraction = min(1.0, used / quota) if quota else 0.0
    if listing.truncated:
        text = _("At least {used} of {quota} used")
    elif fraction >= 1.0:
        text = _("Full: {used} of {quota} used")
    elif fraction >= 0.9:
        text = _("Almost full: {used} of {quota} used")
    else:
        text = _("{used} of {quota} used")
    return text.format(used=media_files.format_storage(used),
                       quota=media_files.format_storage(quota))
