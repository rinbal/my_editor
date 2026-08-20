# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Second page of the drafts panel: preview-first feed imports.

Layout, top to bottom (visibility varies by state):

  +--------------------------------------------+
  |  SOURCE URL [_______________________]      |
  |  hint line                                 |
  |  [Import a file…]      [ Cancel ] [ Load ] |
  +--------------------------------------------+
  |  SUBSCRIBED SOURCES                  [ − ] |   idle/done only
  |    My Blog · last imported 2026-01-02      |
  +--------------------------------------------+
  |  (Newest 10)(Newest 25)(Newest 50)(All)    |   scope chips (preview)
  |  [x] Fetch full text …  [x] Mirror images  |   options (preview)
  |  (Subscribe)                               |
  +--------------------------------------------+
  |  ▂▂▂▂▂▂▂▂ progress bar (busy only)         |
  |  status / error line                       |
  +--------------------------------------------+
  |  [x] First post   · 2026-01-02 · 4 min     |   checkable preview rows
  |  [x] Second post  · 2026-01-01 · 2 min     |
  |                      [ Import 2 selected ] |
  +--------------------------------------------+

States: idle -> loading -> preview -> importing -> done. Loading and
importing are cancellable (button or Escape); the preview list is where
the user unticks items before anything is signed. During import the
same list shows per-item progress and the bar above the status line is
determinate (items done of total).

Platform conventions honoured deliberately: sentence-case labels with
verb-first actions and true ellipses on further-input buttons; no "we"
in user-facing copy; errors colored and adjacent to the status area;
lists select on single click and activate on double-click/Return, with
a remove control beside the list; blank idle states carry next-step
guidance; tooltips on every action.

Dependencies (relay pool, relay-list cache, bunker session pool, and
optionally the draft store for identifier migration) are injected via
:meth:`bind_runtime`. The ``fetcher`` and ``import_job_factory``
constructor seams exist so tests can drive the whole panel with fakes.
"""

from __future__ import annotations

import os
import time
from typing import Callable, List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from constants import (
    DARK_BG, DARK_BORDER, DARK_FG, DARK_MENU_BG, DARK_MUTED_FG,
    LIGHT_BG, LIGHT_BORDER, LIGHT_FG, LIGHT_MUTED_FG,
)

from ..bunker import BunkerSessionPool
from ..imports import workers
from ..imports.errors import SourceError, friendly_message
from ..imports.fetch import SourceFetcher
from ..imports.images import scan_html_images
from ..imports.pipeline import ImportItemsJob
from ..imports.preview import (
    SCOPE_PRESETS,
    default_scope_key,
    filter_items,
    read_minutes,
)
from ..imports.registry import (
    ResolveInput,
    ResolveResult,
    can_resolve_source,
    resolve_source,
)
from ..imports.subscriptions import FeedSubscriptionStore
from ..outbox import RelayListCache
from ..profiles import Profile
from ..relay import RelayPool
from ..rss.parser import FeedItem


_STATUS_LABEL = {
    "pending": "pending",
    "resolving": "resolving from Nostr…",
    # Covers both full-text recovery and podcast-chapter fetching.
    "extracting": "fetching content…",
    "mirroring": "mirroring images…",
    "signing": "signing",
    # "saved" fires at stash time: the draft is signed and exists, but
    # the relay publish is still in flight. "published" only once at
    # least one relay accepted the event (with the count as detail).
    "saved": "saved",
    "published": "published",
    "failed": "failed",
}


_URL_HINT_INVALID = (
    "That doesn't look like a fetchable URL. Paste a site or feed address."
)

# Guidance for the blank idle state: an empty screen must point at the
# next step rather than sit silent.
_IDLE_GUIDANCE = (
    "Paste a link above, or import an export file, to pull posts in "
    "as private drafts."
)

# Hard cap on an imported file (a WXR export of a large blog runs to a
# few tens of MB; anything bigger is almost certainly the wrong file).
_MAX_IMPORT_FILE_BYTES = 64 * 1024 * 1024

_FILE_DIALOG_FILTER = (
    "Content exports (*.xml *.json *.opml *.zip);;All files (*)"
)


# Panel accent. No app-wide token exists for it yet; defined once here
# and shared by both themes so it cannot drift between them.
_ACCENT = "#0E639C"
_ACCENT_HOVER = "#1177BB"

# Per-theme palette. App-wide colors come from ``constants.py`` (the
# same single source of truth ``theme.py`` uses); only values with no
# app-wide token stay literal, and each appears exactly once.
_THEME_TOKENS = {
    True: {
        "bg": DARK_BG,
        "fg": DARK_FG,
        "muted": DARK_MUTED_FG,
        "border": DARK_BORDER,
        "field_bg": DARK_MENU_BG,
        "field_fg": "#FFFFFF",
        "row_border": DARK_MENU_BG,
        "row_selected": "#2A2D2E",
        "disabled_bg": DARK_MENU_BG,
        "disabled_fg": DARK_MUTED_FG,
        "chip_checked_bg": "#094771",
        "error_fg": "#F48771",
    },
    False: {
        "bg": LIGHT_BG,
        "fg": LIGHT_FG,
        "muted": LIGHT_MUTED_FG,
        "border": LIGHT_BORDER,
        "field_bg": LIGHT_BG,
        "field_fg": LIGHT_FG,
        "row_border": "#ECECEC",
        "row_selected": "#E5F2FB",
        "disabled_bg": "#E5E5E5",
        "disabled_fg": "#999999",
        "chip_checked_bg": "#E5F2FB",
        "error_fg": "#C8412A",
    },
}


def _panel_css(is_dark: bool) -> str:
    """One QSS template for both themes, filled from the token table."""
    t = _THEME_TOKENS[is_dark]
    return f"""
QFrame#feeds_panel {{ background: {t["bg"]}; }}
QLabel#feeds_panel_field {{ color: {t["muted"]}; font-size: 10px;
    text-transform: uppercase; letter-spacing: 0.6px; }}
QLabel#feeds_panel_hint {{ color: {t["muted"]}; font-size: 11px; }}
QLabel#feeds_panel_status {{ color: {t["fg"]}; font-size: 11px; padding: 4px 8px; }}
QLineEdit#feeds_panel_url {{
    background: {t["field_bg"]}; color: {t["field_fg"]};
    border: 1px solid {t["border"]};
    border-radius: 4px; padding: 6px 8px;
}}
QLineEdit#feeds_panel_url:focus {{ border-color: {_ACCENT}; }}
QPushButton#feeds_panel_primary {{
    background: {_ACCENT}; color: #FFFFFF; border: none;
    border-radius: 4px; padding: 7px 14px; font-weight: 600;
}}
QPushButton#feeds_panel_primary:hover {{ background: {_ACCENT_HOVER}; }}
QPushButton#feeds_panel_primary:disabled {{
    background: {t["disabled_bg"]}; color: {t["disabled_fg"]}; }}
QPushButton#feeds_panel_secondary {{
    background: transparent; color: {t["fg"]};
    border: 1px solid {t["border"]};
    border-radius: 4px; padding: 6px 12px;
}}
QPushButton#feeds_panel_chip {{
    background: transparent; color: {t["muted"]};
    border: 1px solid {t["border"]};
    border-radius: 11px; padding: 3px 10px; font-size: 11px;
}}
QPushButton#feeds_panel_chip:checked {{
    background: {t["chip_checked_bg"]}; color: {t["fg"]};
    border-color: {_ACCENT};
}}
QListWidget#feeds_panel_list {{
    background: {t["bg"]}; color: {t["fg"]}; border: none;
    border-top: 1px solid {t["row_border"]};
}}
QListWidget#feeds_panel_list::item {{ padding: 6px 8px; }}
QListWidget#feeds_panel_list::item:selected {{ background: {t["row_selected"]}; }}
QLabel#feeds_panel_status[error="true"] {{ color: {t["error_fg"]}; }}
QProgressBar#feeds_panel_progress {{
    background: {t["field_bg"]}; border: none; border-radius: 2px;
    min-height: 4px; max-height: 4px;
}}
QProgressBar#feeds_panel_progress::chunk {{
    background: {_ACCENT}; border-radius: 2px;
}}
"""


def _fmt_date(ts: Optional[int]) -> str:
    if not ts:
        return ""
    try:
        return time.strftime("%Y-%m-%d", time.localtime(int(ts)))
    except (ValueError, OverflowError, OSError):
        return ""


class FeedsPanel(QFrame):
    """Preview-first feed importer.

    Public surface:
      bind_runtime(...)          inject relay pool, relay-list cache,
                                 session pool, and (optionally) the
                                 draft store used for identifier
                                 migration. Must be called before the
                                 user can run an import.
      set_active_profile(p)      track the currently bound profile.
      apply_theme(is_dark)       swap dark / light QSS.

    Signals:
      status_changed(str)        forwarded for parents that want their
                                 own status surface.
    """

    status_changed = Signal(str)

    def __init__(
        self,
        *,
        is_dark: bool = True,
        parent: Optional[QWidget] = None,
        fetcher: Optional[SourceFetcher] = None,
        import_job_factory: Optional[Callable[..., ImportItemsJob]] = None,
        run_blocking: Optional[Callable[..., None]] = None,
        subscription_store_factory: Optional[
            Callable[..., FeedSubscriptionStore]] = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("feeds_panel")
        self.setFrameShape(QFrame.NoFrame)

        self._is_dark = is_dark
        self._relay_pool: Optional[RelayPool] = None
        self._relay_list_cache: Optional[RelayListCache] = None
        self._session_pool: Optional[BunkerSessionPool] = None
        self._draft_store = None
        self._active_profile: Optional[Profile] = None

        self._fetcher = fetcher if fetcher is not None else SourceFetcher(self)
        self._import_job_factory = import_job_factory or ImportItemsJob
        # Executor for CPU-heavy resolution steps (feed parsing). The
        # default runs on the thread pool; tests inject an inline one.
        self._run_blocking = run_blocking or (
            lambda fn, ok, err: workers.run_blocking(fn, ok, err, parent=self)
        )

        self._state = "idle"
        # Bumped on every new load / cancel / profile switch; stale
        # resolution callbacks compare against it and drop themselves.
        self._load_generation = 0
        self._resolved_url = ""
        self._source_label = ""
        self._all_items: List[FeedItem] = []
        self._preview_items: List[FeedItem] = []
        self._populating = False
        self._job: Optional[ImportItemsJob] = None
        self._retired_jobs: List[ImportItemsJob] = []
        self._rows: List[QListWidgetItem] = []
        # Images the user unticked in the review dialog; kept at their
        # original URL. Reset on every new preview so one feed's
        # exclusions never bleed into another.
        self._skip_image_urls: set = set()
        self._blossom_settings = None
        self._nostr_query = None
        self._subscription_store_factory = (
            subscription_store_factory or FeedSubscriptionStore)
        self._subscriptions: Optional[FeedSubscriptionStore] = None
        self._feed_title = ""

        self._build_ui()
        # Escape cancels whatever is in flight, matching platform
        # expectations for interruptible operations.
        escape = QShortcut(QKeySequence(Qt.Key_Escape), self)
        escape.setContext(Qt.WidgetWithChildrenShortcut)
        escape.activated.connect(self._on_escape)
        self.apply_theme(is_dark)
        self._refresh_controls()

    # -- construction ------------------------------------------------------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        outer.addWidget(self._build_form())
        outer.addWidget(self._build_sources())
        outer.addWidget(self._build_scope_row())
        outer.addWidget(self._build_options_row())
        outer.addWidget(self._build_progress())
        outer.addWidget(self._build_status())
        outer.addWidget(self._build_list(), 1)
        outer.addWidget(self._build_import_row())

    def _build_form(self) -> QWidget:
        frame = QFrame()
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(10, 10, 10, 6)
        layout.setSpacing(6)

        url_label = QLabel("Source URL")
        url_label.setObjectName("feeds_panel_field")
        self._url_edit = QLineEdit()
        self._url_edit.setObjectName("feeds_panel_url")
        self._url_edit.setAccessibleName("Import source URL")
        self._url_edit.setPlaceholderText(
            "A blog, feed, npub, Bluesky thread, or Markdown URL")
        self._url_edit.setClearButtonEnabled(True)
        self._url_edit.textChanged.connect(self._refresh_controls)
        self._url_edit.returnPressed.connect(self._on_load_clicked)
        layout.addWidget(url_label)
        layout.addWidget(self._url_edit)

        url_hint = QLabel(
            "Accepts feeds (RSS / Atom / JSON), Nostr profiles and "
            "events, Bluesky threads, Markdown files, GitHub folders, "
            "and sitemaps."
        )
        url_hint.setObjectName("feeds_panel_hint")
        url_hint.setWordWrap(True)
        layout.addWidget(url_hint)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        self._load_btn = QPushButton("Load preview")
        self._load_btn.setObjectName("feeds_panel_primary")
        self._load_btn.setCursor(Qt.PointingHandCursor)
        self._load_btn.setToolTip(
            "Fetch the source and preview its items before importing")
        self._load_btn.clicked.connect(self._on_load_clicked)

        # Trailing ellipsis: the button opens a file dialog for
        # further input, per platform convention.
        self._file_btn = QPushButton("Import a file…")
        self._file_btn.setObjectName("feeds_panel_secondary")
        self._file_btn.setCursor(Qt.PointingHandCursor)
        self._file_btn.setToolTip(
            "A WordPress (WXR) or Ghost export, an OPML subscription "
            "list, or a Medium / Substack ZIP"
        )
        self._file_btn.clicked.connect(self._on_import_file_clicked)

        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.setObjectName("feeds_panel_secondary")
        self._cancel_btn.setCursor(Qt.PointingHandCursor)
        self._cancel_btn.setToolTip("Stop the current operation (Esc)")
        self._cancel_btn.clicked.connect(self._on_cancel_clicked)
        self._cancel_btn.setVisible(False)

        row.addWidget(self._file_btn)
        row.addStretch(1)
        row.addWidget(self._cancel_btn)
        row.addWidget(self._load_btn)
        layout.addLayout(row)
        return frame

    def _build_sources(self) -> QWidget:
        self._sources_frame = QFrame()
        layout = QVBoxLayout(self._sources_frame)
        layout.setContentsMargins(10, 0, 10, 6)
        layout.setSpacing(4)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(6)
        label = QLabel("Subscribed sources")
        label.setObjectName("feeds_panel_field")
        header.addWidget(label)
        header.addStretch(1)
        # The remove control sits with the list it affects (the native
        # add/remove-row pattern); no confirmation, unsubscribing is
        # freely reversible.
        self._remove_source_btn = QPushButton("−")
        self._remove_source_btn.setObjectName("feeds_panel_chip")
        self._remove_source_btn.setFixedWidth(26)
        self._remove_source_btn.setCursor(Qt.PointingHandCursor)
        self._remove_source_btn.setToolTip(
            "Unsubscribe from the selected source")
        self._remove_source_btn.setAccessibleName("Unsubscribe")
        self._remove_source_btn.setEnabled(False)
        self._remove_source_btn.clicked.connect(self._on_remove_source)
        header.addWidget(self._remove_source_btn)
        layout.addLayout(header)

        self._sources_list = QListWidget()
        self._sources_list.setObjectName("feeds_panel_list")
        self._sources_list.setAccessibleName("Subscribed sources")
        self._sources_list.setMaximumHeight(140)
        self._sources_list.setSelectionMode(QListWidget.SingleSelection)
        self._sources_list.setToolTip(
            "Double-click a source to load its latest items"
        )
        # Single click selects (so the remove control has a target);
        # double-click or Return loads, per platform list convention.
        self._sources_list.itemActivated.connect(self._on_source_activated)
        self._sources_list.itemSelectionChanged.connect(
            self._on_source_selection_changed)
        self._sources_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self._sources_list.customContextMenuRequested.connect(
            self._on_source_context_menu)
        layout.addWidget(self._sources_list)
        self._sources_frame.setVisible(False)
        return self._sources_frame

    def _build_scope_row(self) -> QWidget:
        self._scope_frame = QFrame()
        layout = QHBoxLayout(self._scope_frame)
        layout.setContentsMargins(10, 0, 10, 6)
        layout.setSpacing(6)

        self._scope_group = QButtonGroup(self)
        self._scope_group.setExclusive(True)
        self._scope_buttons = {}
        for preset in SCOPE_PRESETS:
            btn = QPushButton(preset.label)
            btn.setObjectName("feeds_panel_chip")
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setAccessibleName(f"Scope: {preset.label}")
            if preset.recommended:
                btn.setToolTip("Recommended")
            btn.clicked.connect(
                lambda _checked=False, key=preset.key: self._on_scope_selected(key)
            )
            self._scope_group.addButton(btn)
            self._scope_buttons[preset.key] = btn
            layout.addWidget(btn)
        layout.addStretch(1)
        self._scope_frame.setVisible(False)
        return self._scope_frame

    def _build_options_row(self) -> QWidget:
        self._options_frame = QFrame()
        layout = QVBoxLayout(self._options_frame)
        layout.setContentsMargins(10, 0, 10, 6)
        layout.setSpacing(4)

        self._fulltext_check = QCheckBox(
            "Fetch full text for teaser-only items")
        self._fulltext_check.setChecked(True)
        self._fulltext_check.setToolTip(
            "Some feeds only ship a summary. When on, the article page "
            "is fetched and its main text becomes the draft body."
        )
        layout.addWidget(self._fulltext_check)

        rehost_row = QHBoxLayout()
        rehost_row.setContentsMargins(0, 0, 0, 0)
        rehost_row.setSpacing(8)
        self._rehost_check = QCheckBox("Mirror images to Blossom")
        self._rehost_check.setChecked(True)
        self._rehost_check.setToolTip(
            "Copies every image to your Blossom server so imported "
            "drafts don't depend on the source site staying up."
        )
        self._rehost_check.toggled.connect(
            lambda _checked: self._update_import_button())
        self._review_images_btn = QPushButton("Review images")
        self._review_images_btn.setObjectName("feeds_panel_chip")
        self._review_images_btn.setCursor(Qt.PointingHandCursor)
        self._review_images_btn.clicked.connect(self._on_review_images)
        self._review_images_btn.setVisible(False)
        rehost_row.addWidget(self._rehost_check)
        rehost_row.addWidget(self._review_images_btn)
        rehost_row.addStretch(1)
        layout.addLayout(rehost_row)

        self._subscribe_btn = QPushButton("Subscribe")
        self._subscribe_btn.setObjectName("feeds_panel_chip")
        self._subscribe_btn.setCursor(Qt.PointingHandCursor)
        self._subscribe_btn.setToolTip(
            "Remember this source (synced privately via your relays) so "
            "you can re-import new items later."
        )
        self._subscribe_btn.clicked.connect(self._on_subscribe_clicked)
        self._subscribe_btn.setVisible(False)
        layout.addWidget(self._subscribe_btn)

        self._options_frame.setVisible(False)
        return self._options_frame

    def _build_progress(self) -> QWidget:
        # One slim bar in a consistent location for both waits:
        # indeterminate while loading a preview, determinate (done of
        # total items) during an import.
        frame = QFrame()
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(10, 0, 10, 0)
        layout.setSpacing(0)
        self._progress_bar = QProgressBar()
        self._progress_bar.setObjectName("feeds_panel_progress")
        self._progress_bar.setAccessibleName("Import progress")
        self._progress_bar.setTextVisible(False)
        self._progress_bar.setVisible(False)
        layout.addWidget(self._progress_bar)
        return frame

    def _build_status(self) -> QWidget:
        self._status_label = QLabel("")
        self._status_label.setObjectName("feeds_panel_status")
        self._status_label.setProperty("error", "false")
        self._status_label.setWordWrap(True)
        return self._status_label

    def _build_list(self) -> QWidget:
        self._list = QListWidget()
        self._list.setObjectName("feeds_panel_list")
        self._list.setAccessibleName("Items to import")
        self._list.setUniformItemSizes(True)
        self._list.setSelectionMode(QListWidget.NoSelection)
        self._list.itemChanged.connect(self._on_item_check_changed)
        return self._list

    def _build_import_row(self) -> QWidget:
        frame = QFrame()
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(10, 6, 10, 10)
        layout.setSpacing(8)

        self._import_btn = QPushButton("Import")
        self._import_btn.setObjectName("feeds_panel_primary")
        self._import_btn.setCursor(Qt.PointingHandCursor)
        self._import_btn.setToolTip(
            "Import the checked items as private drafts")
        self._import_btn.clicked.connect(self._on_import_clicked)
        self._import_btn.setVisible(False)

        layout.addStretch(1)
        layout.addWidget(self._import_btn)
        return frame

    # -- public surface ----------------------------------------------------

    def bind_runtime(
        self,
        *,
        relay_pool: RelayPool,
        relay_list_cache: RelayListCache,
        session_pool: BunkerSessionPool,
        draft_store=None,
        blossom_settings=None,
    ) -> None:
        """Inject the runtime dependencies needed to publish drafts.

        ``draft_store`` (anything supporting ``identifier in store``) is
        optional; when given, imports reuse a pre-prefix identifier that
        already exists locally instead of duplicating the draft under
        the new prefixed d-tag. ``blossom_settings`` (anything with a
        ``primary`` attribute) overrides where mirrored images land;
        the user's configured Blossom settings are read by default.
        """
        self._relay_pool = relay_pool
        self._relay_list_cache = relay_list_cache
        self._session_pool = session_pool
        self._draft_store = draft_store
        self._blossom_settings = blossom_settings
        # The relay-query surface Nostr-facing resolvers use (author
        # articles, single events, NostrHub NIPs).
        if relay_pool is not None:
            from ..imports.sources.nostr import RelayQueryAdapter
            self._nostr_query = RelayQueryAdapter(relay_pool, parent=self)
        else:
            self._nostr_query = None
        # Subscriptions: the user's remembered sources, synced privately
        # as an encrypted kind 30078 event.
        if self._subscriptions is None:
            self._subscriptions = self._subscription_store_factory(
                session_pool=session_pool,
                relay_pool=relay_pool,
                relay_list_cache=relay_list_cache,
                parent=self,
            )
            self._subscriptions.feeds_changed.connect(
                self._refresh_sources_list)
            self._subscriptions.sync_status.connect(self._on_sync_status)
            if self._active_profile is not None:
                self._subscriptions.bind_profile(self._active_profile)
        self._refresh_controls()

    def flush_subscriptions(self) -> None:
        """Publish pending subscription changes now (app quit / logout)."""
        if self._subscriptions is not None:
            self._subscriptions.flush()

    def set_active_profile(self, profile: Optional[Profile]) -> None:
        """Track the active Nostr profile. Without one, imports are disabled."""
        if self._active_profile is profile:
            return
        # Abandon any in-flight work: the new profile shouldn't inherit
        # a preview or a job signed by the old key.
        self._abort_activity()
        self._active_profile = profile
        if self._subscriptions is not None:
            self._subscriptions.bind_profile(profile)
        self._reset_to_idle()

    def apply_theme(self, is_dark: bool) -> None:
        self._is_dark = is_dark
        self.setStyleSheet(_panel_css(is_dark))

    # -- state handling ----------------------------------------------------

    def _abort_activity(self) -> None:
        self._load_generation += 1
        if self._job is not None:
            self._job.cancel()
            self._release_job()

    def _reset_to_idle(self) -> None:
        self._state = "idle"
        self._all_items = []
        self._preview_items = []
        self._resolved_url = ""
        self._rows = []
        self._list.clear()
        self._set_status("")
        self._refresh_controls()

    def _refresh_controls(self) -> None:
        runtime_ready = (
            self._relay_pool is not None
            and self._relay_list_cache is not None
            and self._session_pool is not None
        )
        # The registry is the single validation authority: registering a
        # new resolver automatically widens what this gate accepts.
        text = self._url_edit.text().strip()
        url_ready = bool(text) and can_resolve_source(text)
        profile_ready = self._active_profile is not None
        busy = self._state in ("loading", "importing")

        self._load_btn.setEnabled(
            runtime_ready and url_ready and profile_ready and not busy)
        self._load_btn.setVisible(self._state in ("idle", "preview", "done"))
        self._file_btn.setEnabled(runtime_ready and profile_ready and not busy)
        self._file_btn.setVisible(self._state in ("idle", "preview", "done"))
        self._cancel_btn.setVisible(busy)
        self._progress_bar.setVisible(busy)
        self._scope_frame.setVisible(self._state == "preview")
        self._options_frame.setVisible(self._state == "preview")
        self._import_btn.setVisible(self._state == "preview")
        self._sources_frame.setVisible(
            self._state in ("idle", "done")
            and profile_ready
            and self._sources_list.count() > 0
        )
        self._refresh_subscribe_button()

        if self._active_profile is None:
            self._set_status("Connect a Nostr profile to import feeds.")
        elif not runtime_ready:
            self._set_status("Importer is not wired up yet.")
        elif text and not url_ready and self._state == "idle":
            # Something was typed but it can't be a URL (an XML paste,
            # a bare word, an over-long blob). Say so instead of leaving
            # a silently disabled button.
            self._set_status(_URL_HINT_INVALID)
        elif not text and self._state == "idle":
            # A blank screen must still point at the next step.
            if self._sources_list.count() == 0:
                self._set_status(_IDLE_GUIDANCE)
            else:
                self._set_status("")

    # -- load / preview ----------------------------------------------------

    def _on_load_clicked(self) -> None:
        # State guard rather than visibility: widget visibility is
        # unreliable while the panel itself is hidden.
        if self._state in ("loading", "importing"):
            return
        if not self._load_btn.isEnabled():
            return
        url = self._url_edit.text().strip()
        if not url or self._active_profile is None:
            return
        generation = self._enter_loading_state(source_label=url)
        resolve_source(
            ResolveInput(url=url),
            fetcher=self._fetcher,
            on_success=lambda result, g=generation: self._on_preview_ready(g, result),
            on_failure=lambda error, g=generation: self._on_preview_failed(g, error),
            on_stage=lambda stage, g=generation: self._on_preview_stage(g, stage),
            is_cancelled=lambda g=generation: g != self._load_generation,
            run_blocking=self._run_blocking,
            nostr_query=self._nostr_query,
        )

    def _enter_loading_state(self, *, source_label: str) -> int:
        """Reset preview state and return the new load generation.

        A user-driven entry point: guaranteed outside any job's emit
        stack, so retired machinery can be freed for real here.
        """
        self._free_retired_jobs()
        self._load_generation += 1
        self._state = "loading"
        self._source_label = source_label
        self._all_items = []
        self._preview_items = []
        self._skip_image_urls = set()
        self._rows = []
        self._list.clear()
        # Preview loading has no known duration: indeterminate bar.
        self._progress_bar.setRange(0, 0)
        self._set_status("Loading preview…")
        self._refresh_controls()
        return self._load_generation

    # -- file imports ------------------------------------------------------

    def _on_import_file_clicked(self) -> None:
        if self._state in ("loading", "importing"):
            return
        if not self._file_btn.isEnabled():
            return
        path, _selected_filter = QFileDialog.getOpenFileName(
            self, "Import a content export", "", _FILE_DIALOG_FILTER)
        if path:
            self._import_file(path)

    def _import_file(self, path: str) -> None:
        """One-shot import of an export file (WXR / Ghost / ZIP / OPML).

        There is no URL to refresh, so the preview workspace opens
        directly with the parsed items and the import's ``source`` tag
        carries the file name.
        """
        label = os.path.basename(path)
        try:
            with open(path, "rb") as handle:
                data = handle.read(_MAX_IMPORT_FILE_BYTES + 1)
        except OSError as exc:
            self._set_status(f"Couldn't read that file: {exc}", error=True)
            return
        if len(data) > _MAX_IMPORT_FILE_BYTES:
            self._set_status(
                "That file is too large to import "
                f"(over {_MAX_IMPORT_FILE_BYTES // (1024 * 1024)} MB).",
                error=True,
            )
            return

        from ..imports.sources.archive import looks_like_zip
        if looks_like_zip(data):
            self._import_archive(data, label)
            return

        text = data.decode("utf-8", errors="replace")
        from ..imports.sources.opml import is_opml
        if is_opml(text):
            self._handle_opml(text, label)
            return

        generation = self._enter_loading_state(source_label=label)
        resolve_source(
            ResolveInput(url="", pasted_body=text),
            fetcher=self._fetcher,
            on_success=lambda result, g=generation: self._on_preview_ready(g, result),
            on_failure=lambda error, g=generation: self._on_preview_failed(g, error),
            on_stage=lambda stage, g=generation: self._on_preview_stage(g, stage),
            is_cancelled=lambda g=generation: g != self._load_generation,
            run_blocking=self._run_blocking,
            nostr_query=self._nostr_query,
        )

    def _import_archive(self, data: bytes, label: str) -> None:
        from ..imports.errors import ERROR_CODES as _EC
        from ..imports.sources.archive import ArchiveError, extract_archive
        from ..rss.parser import Feed

        generation = self._enter_loading_state(source_label=label)

        def _done(result) -> None:
            if generation != self._load_generation:
                return
            if result.platform is None or not result.items:
                self._state = "idle"
                self._refresh_controls()
                self._set_status(
                    "That ZIP doesn't look like a Medium or Substack "
                    "export, or it has no published posts.",
                    error=True,
                )
                return
            feed = Feed(format=result.platform, title=result.title,
                        link=None, description=None, items=result.items)
            self._on_preview_ready(
                generation, ResolveResult(url=label, feed=feed))

        def _failed(exc) -> None:
            if generation != self._load_generation:
                return
            self._state = "idle"
            self._refresh_controls()
            if isinstance(exc, ArchiveError):
                self._set_status(friendly_message(SourceError(
                    str(exc), _EC.ARCHIVE_UNREADABLE)), error=True)
            else:
                self._set_status(f"Couldn't read that archive: {exc}",
                                 error=True)

        self._run_blocking(lambda: extract_archive(data), _done, _failed)

    def _handle_opml(self, text: str, label: str) -> None:
        """Bulk-subscribe every feed in an OPML list, with one summary."""
        if self._subscriptions is None:
            self._set_status(
                "Feed subscriptions aren't available until the importer "
                "is fully wired up."
            )
            return
        from ..imports.sources.opml import parse_opml
        document = parse_opml(text)
        if not document.feeds:
            self._set_status(f"No feeds were found in {label}.")
            return
        added = 0
        skipped = 0
        for feed in document.feeds:
            result = self._subscriptions.add_feed(feed.xml_url, feed.title)
            if result.get("added"):
                added += 1
            else:
                skipped += 1
        if added and not skipped:
            self._set_status(f"Subscribed to {added} feed(s) from {label}.")
        elif added:
            self._set_status(
                f"Subscribed to {added} feed(s) from {label}; "
                f"{skipped} skipped (already subscribed or invalid)."
            )
        else:
            self._set_status(
                f"Nothing new in {label}: every feed was already "
                "subscribed or invalid."
            )

    def _on_preview_stage(self, generation: int, stage: dict) -> None:
        if generation != self._load_generation:
            return
        name = stage.get("name", "")
        url = stage.get("url", "")
        if name == "connecting":
            self._set_status(f"Fetching {url}")
        elif name == "discovering":
            self._set_status(f"Looking for a feed link at {url}")
        elif name == "parsing":
            self._set_status("Reading the feed…")

    def _on_preview_failed(self, generation: int, error: SourceError) -> None:
        if generation != self._load_generation:
            return
        self._state = "idle"
        # Refresh first: with an empty URL box it resets the status
        # line, which must not wipe the error text below.
        self._refresh_controls()
        self._set_status(friendly_message(error), error=True)

    def _on_preview_ready(self, generation: int, result: ResolveResult) -> None:
        if generation != self._load_generation:
            return
        self._resolved_url = result.url or self._source_label
        self._all_items = list(result.feed.items)
        if not self._all_items:
            self._state = "idle"
            self._refresh_controls()
            self._set_status(
                f"{result.feed.title or result.url}: no items to import.")
            return

        self._state = "preview"
        self._feed_title = result.feed.title or ""
        self._refresh_scope_chips()
        self._select_scope(default_scope_key(len(self._all_items)))
        self._apply_scope()
        title = result.feed.title or result.url
        self._set_status(
            f"{title}: {len(self._all_items)} item(s) found. "
            "Untick anything you don't want, then import."
        )
        self._refresh_controls()

    # -- subscriptions -----------------------------------------------------

    def _refresh_sources_list(self) -> None:
        self._sources_list.clear()
        if self._subscriptions is None:
            self._refresh_controls()
            return
        for feed in self._subscriptions.feeds:
            parts = [feed.title or feed.url]
            if feed.last_fetched_at:
                date = _fmt_date(feed.last_fetched_at)
                if date:
                    parts.append(f"last imported {date}")
            row = QListWidgetItem("  " + "   ·   ".join(parts))
            row.setData(Qt.UserRole, feed.url)
            row.setToolTip(feed.url)
            self._sources_list.addItem(row)
        self._refresh_controls()

    def _on_source_activated(self, row: QListWidgetItem) -> None:
        if self._state in ("loading", "importing"):
            return
        url = row.data(Qt.UserRole)
        if isinstance(url, str) and url:
            self._url_edit.setText(url)
            self._on_load_clicked()

    def _on_source_selection_changed(self) -> None:
        self._remove_source_btn.setEnabled(
            self._sources_list.currentItem() is not None)

    def _on_remove_source(self) -> None:
        row = self._sources_list.currentItem()
        if row is None or self._subscriptions is None:
            return
        url = row.data(Qt.UserRole)
        if isinstance(url, str) and url:
            self._subscriptions.remove_feed(url)

    def _on_source_context_menu(self, pos) -> None:
        row = self._sources_list.itemAt(pos)
        if row is None or self._subscriptions is None:
            return
        from PySide6.QtWidgets import QMenu
        url = row.data(Qt.UserRole)
        menu = QMenu(self._sources_list)
        unsubscribe = menu.addAction("Unsubscribe")
        chosen = menu.exec(self._sources_list.mapToGlobal(pos))
        if chosen is unsubscribe and isinstance(url, str):
            self._subscriptions.remove_feed(url)

    def _refresh_subscribe_button(self) -> None:
        show = (
            self._state == "preview"
            and self._subscriptions is not None
            and self._active_profile is not None
            # Only URL-shaped sources are re-resolvable later; a file
            # import has nothing to subscribe to.
            and can_resolve_source(self._source_label)
            and not self._subscriptions.has_feed(self._source_label)
        )
        self._subscribe_btn.setVisible(bool(show))

    def _on_subscribe_clicked(self) -> None:
        if self._subscriptions is None or self._state != "preview":
            return
        result = self._subscriptions.add_feed(
            self._source_label, title=self._feed_title)
        if result.get("added"):
            self._set_status(
                f"Subscribed to {self._feed_title or self._source_label}.")
        self._refresh_subscribe_button()

    def _on_sync_status(self, text: str) -> None:
        # Sync chatter must never stomp on an active preview or import.
        if text and self._state == "idle":
            self._set_status(text)

    def _active_subscription_since(self) -> int:
        if self._subscriptions is None:
            return 0
        feed = self._subscriptions.get(self._source_label)
        return feed.last_fetched_at if feed else 0

    # -- scope + selection -------------------------------------------------

    def _selected_scope_key(self) -> str:
        for key, btn in self._scope_buttons.items():
            if btn.isChecked():
                return key
        return "newest25"

    def _select_scope(self, key: str) -> None:
        btn = self._scope_buttons.get(key)
        if btn is not None:
            btn.setChecked(True)

    def _on_scope_selected(self, key: str) -> None:
        if self._state != "preview":
            return
        self._select_scope(key)
        self._apply_scope()

    def _selected_preset(self):
        key = self._selected_scope_key()
        for preset in SCOPE_PRESETS:
            if preset.key == key:
                return preset
        return SCOPE_PRESETS[1]

    def _apply_scope(self) -> None:
        preset = self._selected_preset()
        since = (
            self._active_subscription_since()
            if preset.since_last_visit else None
        )
        self._preview_items = filter_items(
            self._all_items, since=since or None, limit=preset.limit)
        self._populate_preview_list()
        self._update_import_button()

    def _refresh_scope_chips(self) -> None:
        """The since-visit chip only makes sense for a subscribed source
        with a recorded last import."""
        for preset in SCOPE_PRESETS:
            button = self._scope_buttons.get(preset.key)
            if button is None:
                continue
            if preset.since_last_visit:
                button.setVisible(self._active_subscription_since() > 0)

    def _populate_preview_list(self) -> None:
        self._populating = True
        try:
            self._list.clear()
            self._rows = []
            for index, item in enumerate(self._preview_items):
                row = QListWidgetItem(self._preview_row_text(item))
                row.setFlags(
                    row.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
                row.setCheckState(Qt.Checked)
                row.setData(Qt.UserRole, index)
                self._list.addItem(row)
                self._rows.append(row)
        finally:
            self._populating = False

    @staticmethod
    def _preview_row_text(item: FeedItem) -> str:
        parts = [item.title or item.link or "(untitled)"]
        date = _fmt_date(item.published_at)
        if date:
            parts.append(date)
        minutes = read_minutes(item.content_html)
        if minutes:
            parts.append(f"{minutes} min read")
        return "  " + "   ·   ".join(parts)

    def _on_item_check_changed(self, _row: QListWidgetItem) -> None:
        if self._populating or self._state != "preview":
            return
        self._update_import_button()

    def _checked_items(self) -> List[FeedItem]:
        selected: List[FeedItem] = []
        for row in self._rows:
            if row.checkState() == Qt.Checked:
                index = row.data(Qt.UserRole)
                if isinstance(index, int) and 0 <= index < len(self._preview_items):
                    selected.append(self._preview_items[index])
        return selected

    def _update_import_button(self) -> None:
        selected = self._checked_items()
        self._import_btn.setText(f"Import {len(selected)} selected")
        self._import_btn.setEnabled(len(selected) > 0)
        self._update_image_review_button(selected)

    def _selected_image_urls(self, selected: List[FeedItem]) -> List[str]:
        seen: List[str] = []
        for item in selected:
            for url in scan_html_images(item.content_html):
                if url not in seen:
                    seen.append(url)
        return seen

    def _update_image_review_button(self, selected: List[FeedItem]) -> None:
        images = self._selected_image_urls(selected)
        show = bool(images) and self._rehost_check.isChecked()
        self._review_images_btn.setVisible(show)
        if show:
            kept = sum(1 for u in images if u not in self._skip_image_urls)
            self._review_images_btn.setText(
                f"Review images ({kept}/{len(images)})…")

    def _on_review_images(self) -> None:
        if self._state != "preview":
            return
        from .image_review_dialog import ImageReviewDialog

        images = self._selected_image_urls(self._checked_items())
        if not images:
            return
        dialog = ImageReviewDialog(images, self._skip_image_urls, parent=self)
        if dialog.exec() == ImageReviewDialog.Accepted:
            self._skip_image_urls = dialog.skip_urls()
            self._update_import_button()

    def _blossom_primary(self) -> str:
        """The Blossom server mirrored images land on, or '' for none."""
        settings = self._blossom_settings
        if settings is None:
            try:
                from ..blossom.settings import BlossomSettings
                settings = self._blossom_settings = BlossomSettings()
            except Exception:  # noqa: BLE001, unreadable settings = no mirroring
                return ""
        try:
            return str(getattr(settings, "primary", "") or "")
        except Exception:  # noqa: BLE001
            return ""

    # -- import lifecycle --------------------------------------------------

    def _on_import_clicked(self) -> None:
        if self._state != "preview":
            return
        if (
            self._relay_pool is None
            or self._relay_list_cache is None
            or self._session_pool is None
            or self._active_profile is None
        ):
            return
        selected = self._checked_items()
        if not selected:
            return

        # Rebuild the list so row indices align with job item indices.
        self._populating = True
        try:
            self._list.clear()
            self._rows = []
            for item in selected:
                row = QListWidgetItem()
                self._list.addItem(row)
                self._rows.append(row)
                self._set_row(
                    len(self._rows) - 1,
                    item.title or item.link or "(untitled)",
                    "pending",
                )
        finally:
            self._populating = False

        store = self._draft_store
        identifier_exists = (
            (lambda identifier: identifier in store) if store is not None else None
        )
        self._job = self._import_job_factory(
            items=selected,
            feed_url=self._resolved_url or self._url_edit.text().strip(),
            profile=self._active_profile,
            relay_pool=self._relay_pool,
            relay_list_cache=self._relay_list_cache,
            session_pool=self._session_pool,
            identifier_exists=identifier_exists,
            fetch_full_text=self._fulltext_check.isChecked(),
            rehost_images=self._rehost_check.isChecked(),
            blossom_server=(
                self._blossom_primary()
                if self._rehost_check.isChecked() else ""
            ),
            skip_image_urls=set(self._skip_image_urls),
            parent=self,
        )
        self._job.status_changed.connect(self._set_status)
        self._job.item_started.connect(self._on_item_started)
        self._job.item_resolving_from_nostr.connect(self._on_item_resolving)
        self._job.item_extracting.connect(self._on_item_extracting)
        self._job.item_mirroring.connect(self._on_item_mirroring)
        self._job.item_succeeded.connect(self._on_item_succeeded)
        self._job.item_published.connect(self._on_item_published)
        self._job.item_failed.connect(self._on_item_failed)
        self._job.progress.connect(self._on_job_progress)
        self._job.completed.connect(self._on_completed)

        self._state = "importing"
        # The import has a known length: determinate bar, item by item.
        self._progress_bar.setRange(0, len(selected))
        self._progress_bar.setValue(0)
        self._set_status("Starting import…")
        self._refresh_controls()
        self._job.start()

    def _on_job_progress(self, done: int, total: int) -> None:
        if self._state != "importing":
            return
        self._progress_bar.setRange(0, max(1, total))
        self._progress_bar.setValue(done)

    def _on_cancel_clicked(self) -> None:
        if self._state == "loading":
            self._load_generation += 1
            self._state = "idle"
            # Refresh first so the idle-state defaults can't overwrite
            # the cancellation notice below.
            self._refresh_controls()
            self._set_status("Load cancelled.")
            return
        if self._state == "importing" and self._job is not None:
            self._job.cancel()
            self._finish_import()
            self._set_status("Import cancelled.")

    def _on_escape(self) -> None:
        if self._state in ("loading", "importing"):
            self._on_cancel_clicked()

    def _on_item_started(self, index: int, title: str) -> None:
        self._set_row(index, title, "signing")

    def _on_item_resolving(self, index: int, title: str) -> None:
        # The pipeline routes long-form items through Nostr before
        # signing; surface that state so the user knows why this row is
        # taking longer than the others.
        self._set_row(index, title, "resolving")

    def _on_item_extracting(self, index: int, title: str) -> None:
        self._set_row(index, title, "extracting")

    def _on_item_mirroring(
        self, index: int, mirrored: int, failed: int, total: int
    ) -> None:
        title = self._row_title(index)
        done = mirrored + failed
        detail = f"{done}/{total} images"
        if failed:
            detail += f", {failed} kept original"
        self._set_row(index, title, "mirroring", detail)

    def _on_item_succeeded(self, index: int, _identifier: str) -> None:
        # Stash time: signed and saved, relay publish still in flight.
        title = self._row_title(index)
        self._set_row(index, title, "saved")

    def _on_item_published(self, index: int, accepted: int, total: int) -> None:
        title = self._row_title(index)
        if accepted > 0:
            self._set_row(index, title, "published", f"{accepted}/{total} relays")
        else:
            # Signed and saved on the signer, but no relay took it. Not
            # a lie ("published") and not a failure (the draft exists).
            self._set_row(index, title, "saved", "no relay accepted it yet")

    def _on_item_failed(self, index: int, reason: str) -> None:
        title = self._row_title(index)
        self._set_row(index, title, "failed", reason)

    def _on_completed(self, succeeded: int, attempted: int) -> None:
        if attempted == 0:
            self._set_status("Done. Nothing to import.")
        else:
            self._set_status(
                f"Done. {succeeded}/{attempted} item(s) imported as drafts."
            )
        # Stamp the subscription so "New since last import" has a floor.
        if (
            succeeded > 0
            and self._subscriptions is not None
            and self._subscriptions.has_feed(self._source_label)
        ):
            self._subscriptions.mark_fetched(self._source_label)
            if self._feed_title:
                self._subscriptions.update_title(
                    self._source_label, self._feed_title)
        self._finish_import()

    # -- helpers ----------------------------------------------------------

    def _set_status(self, text: str, *, error: bool = False) -> None:
        self._status_label.setText(text)
        wanted = "true" if (error and text) else "false"
        if self._status_label.property("error") != wanted:
            self._status_label.setProperty("error", wanted)
            style = self._status_label.style()
            style.unpolish(self._status_label)
            style.polish(self._status_label)
        if text:
            self.status_changed.emit(text)

    def _row_title(self, index: int) -> str:
        if 0 <= index < len(self._rows):
            data = self._rows[index].data(Qt.UserRole)
            return str(data) if isinstance(data, str) else ""
        return ""

    def _set_row(
        self,
        index: int,
        title: str,
        status_key: str,
        detail: str = "",
    ) -> None:
        if not (0 <= index < len(self._rows)):
            return
        item = self._rows[index]
        item.setData(Qt.UserRole, title)
        status_text = _STATUS_LABEL.get(status_key, status_key)
        display_title = title or "(untitled)"
        if detail:
            item.setText(f"  {display_title}    {status_text}: {detail}")
        else:
            item.setText(f"  {display_title}    {status_text}")
        colour = _row_colour(status_key, self._is_dark)
        if colour is not None:
            item.setForeground(colour)

    def _finish_import(self) -> None:
        self._release_job()
        self._state = "done"
        self._refresh_controls()

    def _release_job(self) -> None:
        """Retire the finished/cancelled job.

        Called from inside the job's own signal handlers, where
        destroying the sender is unsafe; the job is parked in
        ``_retired_jobs`` instead and freed at the next safe point
        (:meth:`_free_retired_jobs`, invoked from user-driven entry
        points that can never sit inside a job's emit stack).
        """
        if self._job is not None:
            self._retired_jobs.append(self._job)
            self._job = None

    def _free_retired_jobs(self) -> None:
        """Actually free retired jobs (and their child publish jobs).

        Detaching from the Qt parent hands ownership to Python; with
        the panel's reference dropped the objects are reclaimed, so an
        arbitrarily long session never accumulates import machinery.
        """
        for job in self._retired_jobs:
            try:
                job.setParent(None)
            except RuntimeError:
                pass  # already gone with a torn-down parent
        self._retired_jobs = []


def _row_colour(status_key: str, is_dark: bool) -> Optional[QColor]:
    """Subtle colour cue per row state. Returns ``None`` for the default."""
    if status_key == "published":
        return QColor("#4EC9B0") if is_dark else QColor("#0A7B68")
    if status_key == "failed":
        return QColor("#F48771") if is_dark else QColor("#C8412A")
    if status_key in ("signing", "resolving", "extracting", "mirroring", "saved"):
        return QColor("#DCDCAA") if is_dark else QColor("#85651E")
    return None
