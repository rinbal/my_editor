#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later


import itertools
import json
import os
import time
from dataclasses import dataclass
from typing import Optional

from send2trash import send2trash
from PySide6.QtCore import QBuffer, QIODevice, Qt, QMarginsF, QTimer, QUrl, QFileSystemWatcher
from PySide6.QtNetwork import QLocalServer
from PySide6.QtGui import (
    QAction, QActionGroup, QKeySequence, QTextCursor, QTextDocument, QTextCharFormat, QColor,
    QPageLayout, QPageSize, QPixmap, QGuiApplication, QDesktopServices
)
from PySide6.QtWidgets import (
    QMainWindow, QFileDialog, QInputDialog, QMenu, QMessageBox, QWidget, QVBoxLayout,
    QTextEdit, QTabWidget, QToolButton, QHBoxLayout, QStatusBar, QPushButton, QTabBar,
    QDialogButtonBox, QWidgetAction, QLabel, QDialog, QSplitter, QProgressDialog
)

from constants import (
    DARK_BG, DARK_FG, LIGHT_BG, LIGHT_FG, DARK_SELECTION, LIGHT_SELECTION,
    DARK_MENU_BG, DARK_MENU_FG, LIGHT_MENU_BG, LIGHT_MENU_FG,
    DARK_BORDER, LIGHT_BORDER, MONO_FONT, APP_DISPLAY_NAME, APP_VERSION, APP_URL
)
from widgets import FindBar, HeaderWidget, LineNumberGutter, FileChangedBar, UpdateBar
from editor import HtmlEditor
from highlighter import SyntaxHighlighter, detect_language, detect_language_from_content, LANGUAGE_DISPLAY_NAMES
from settings import load_settings, save_setting
from welcome import welcome_html
from update_check import UpdateChecker
from updater import detect_install_kind, supports_in_app_update, select_asset, UpdateInstaller
import theme

# These extensions are loaded as rendered documents, not plain-text source code.
_RICH_DOC_EXTS = ('.html', '.htm', '.md', '.markdown')

# Extensions accepted for drag-and-drop and file open.
_SUPPORTED_EXTS = {'.md', '.html', '.htm', '.txt'}

# Image extensions we route through Blossom upload on drag-and-drop.
# These never overlap with _SUPPORTED_EXTS so the dispatcher stays simple.
_IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.svg'}
from recovery import EditorBackup, find_all_backups
from recent_files import load_recent, add_recent, clear_recent

from nostr.avatar_store import AvatarBatchLoader, AvatarStore
from nostr.bech32 import encode_note
from nostr.blossom.store import MediaFile, MediaStore
from nostr.bunker import BunkerSessionPool
from nostr.contacts import ContactListFetcher
from nostr.draft_store import DraftState, DraftStore
from nostr.draft_sync import DraftSync
from nostr.drafts import (
    INNER_KIND_LONG_FORM,
    INNER_KIND_SHORT_NOTE,
    MAX_INNER_PAYLOAD_BYTES,
    SUPPORTED_INNER_KINDS,
    build_inner_event,
    serialize_inner_event,
)
from nostr.known_people import KnownPeople, Person
from nostr.metadata import AvatarLoader, ProfileMetadataFetcher
from nostr.outbox import RelayListCache
from nostr.profiles import Profile, ProfileStore
from nostr.publisher import DraftDeleteJob, DraftPublishJob
from nostr.relay import RelayPool
from nostr.search import Nip50SearchClient
from nostr.ui.connect_dialog import ConnectDialog
from nostr.ui.draft_conflict_banner import DraftConflictBanner
from nostr.ui.drafts_panel import DEFAULT_PANEL_WIDTH, DraftsPanel
from nostr.ui.media_library_dialog import MediaLibraryDialog
from nostr.ui.publish_article_dialog import PublishArticleDialog
from nostr.ui.publish_note_dialog import PublishNoteDialog
from nostr.ui.save_destination_dialog import SaveDestination, SaveDestinationDialog
from nostr.ui.stash_kind_dialog import StashChoice, StashKind, StashKindDialog
from nostr.ui.thumbnail_loader import ThumbnailLoader

_IPC_SERVER_NAME = "minimal-texteditor-ipc"

# Monotonic counter for clipboard / drop upload job names. Pairs with
# the dialog-side helper but lives here too because main_window also
# originates paste-to-upload jobs (editor Ctrl+V).
_paste_job_counter = itertools.count(1)


# --------------------------------------------------------------------------- #
# Per-tab Nostr-draft binding                                                 #
# --------------------------------------------------------------------------- #

@dataclass
class DraftBinding:
    """Tracks a tab's link to a NIP-37 draft.

    Set on an editor whenever the user stashes a tab as a draft or
    opens an existing draft into a new tab. Used by the tab-title
    decoration (lock glyph + draft title), by the stash flow to
    preserve the addressable ``d``-tag across subsequent saves, and
    by the profile-mismatch guard to spot a binding whose signing
    identity no longer matches the active profile.
    """

    identifier: str            # the draft's d-tag
    inner_kind: int            # INNER_KIND_SHORT_NOTE or INNER_KIND_LONG_FORM
    event_id: str = ""         # newest wrap event id we've observed for this draft
    created_at: int = 0        # ``created_at`` of that wrap (last stash time)
    title: str = ""            # cached display title for the tab
    # pubkey of the profile this draft was last signed by. Used to
    # detect "tab from a different identity" after profile switches.
    profile_pubkey: str = ""


class MainWindow(QMainWindow):
    def __init__(self, initial_path: str | None = None):
        super().__init__()
        self.setWindowTitle("")
        self.resize(1100, 720)

        theme_pref = load_settings().get("theme")
        if theme_pref == "dark":
            self.is_dark_theme = True
        elif theme_pref == "light":
            self.is_dark_theme = False
        else:
            self.is_dark_theme = self._detect_os_dark_theme()
        # While True, the app follows OS color scheme changes live. Set to
        # False the moment the user picks a theme explicitly (checkbox or
        # Ctrl+Shift+T) so their choice sticks.
        self._follow_os_theme = theme_pref is None
        QGuiApplication.styleHints().colorSchemeChanged.connect(self._on_os_color_scheme_changed)

        self.show_line_numbers = False
        self.syntax_highlighting = True

        _s = load_settings()
        self.editor_background = _s.get("editor_background", "none")
        self.highlight_current_line = _s.get("highlight_current_line", False)
        self.paper_mode = _s.get("paper_mode", False)

        self._watcher = QFileSystemWatcher(self)
        self._watcher.fileChanged.connect(self._on_file_changed)
        self._saving_paths: set[str] = set()

        self.setAcceptDrops(True)

        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(False)
        self.tabs.tabBar().setExpanding(False)
        self.tabs.setMovable(True)
        self.tabs.tabBar().setContextMenuPolicy(Qt.CustomContextMenu)
        self.tabs.tabBar().customContextMenuRequested.connect(self._on_tab_context_menu)

        self.plus_btn = QToolButton()
        self.plus_btn.setText("+")
        self.plus_btn.setAutoRaise(True)
        self.plus_btn.clicked.connect(self.new_tab)
        self.tabs.setCornerWidget(self.plus_btn, Qt.TopRightCorner)

        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self._line_label = QLabel()
        self._line_label.setContentsMargins(0, 0, 8, 0)
        self.status.addPermanentWidget(self._line_label)

        self._apply_theme()

        self.header_widget = HeaderWidget()
        self.header_widget.theme_checkbox.toggled.connect(self._toggle_theme)
        self.header_widget.line_numbers_checkbox.toggled.connect(self._toggle_line_numbers)
        self.header_widget.syntax_highlight_checkbox.toggled.connect(self._toggle_syntax_highlighting)
        self.header_widget.undo_btn.clicked.connect(self._undo)
        self.header_widget.redo_btn.clicked.connect(self._redo)
        self.header_widget.bold_btn.clicked.connect(lambda: self._toggle_format('bold'))
        self.header_widget.italic_btn.clicked.connect(lambda: self._toggle_format('italic'))
        self.header_widget.underline_btn.clicked.connect(lambda: self._toggle_format('underline'))

        # Nostr publishing infrastructure. All pieces are process-wide
        # singletons living on the window; they are cheap to create and stay
        # alive for the lifetime of the editor.
        self._relay_pool = RelayPool(parent=self)
        self._profile_store = ProfileStore()
        self._relay_list_cache = RelayListCache(self._relay_pool, parent=self)
        self._session_pool = BunkerSessionPool(self._relay_pool, parent=self)
        self._metadata_fetcher = ProfileMetadataFetcher(
            self._relay_pool, self._profile_store, parent=self
        )
        self._metadata_fetcher.updated.connect(self._on_metadata_updated)
        self._avatar_loader = AvatarLoader(parent=self)
        # Throttled batcher feeds AvatarStore; widgets get repaint hints via
        # AvatarStore.avatar_added so newly-arrived pixmaps appear live.
        self._avatar_batcher = AvatarBatchLoader(self._avatar_loader, parent=self)
        self._avatars = AvatarStore(parent=self)
        self._avatar_batcher.ready.connect(self._avatars.put)
        # The own-profile chip refresh is still triggered explicitly so we can
        # also flip the menu if the active profile's avatar landed.
        self._avatars.avatar_added.connect(self._on_avatar_added)

        # Mentions infrastructure: cached known people, NIP-50 search,
        # background contact-list fetcher. All process-wide singletons.
        self._known_people = KnownPeople()
        self._search_client = Nip50SearchClient(
            self._relay_pool, self._known_people, parent=self
        )
        self._search_client.results.connect(self._on_search_results)
        self._contact_fetcher = ContactListFetcher(
            self._relay_pool, self._known_people, parent=self
        )
        self._contact_fetcher.person_updated.connect(self._on_person_updated)

        # NIP-37 draft infrastructure. The store is profile-scoped and
        # rebinds when the active profile changes; the sync orchestrator
        # owns the subscription + decryption pipeline; the panel is the
        # user-facing surface. All three stay alive for the session.
        self._draft_store = DraftStore(parent=self)
        self._draft_store.record_changed.connect(self._on_draft_record_changed)
        self._draft_sync = DraftSync(
            relay_pool=self._relay_pool,
            relay_list_cache=self._relay_list_cache,
            session_pool=self._session_pool,
            store=self._draft_store,
            parent=self,
        )
        self._draft_sync.status_changed.connect(self._on_draft_sync_status)
        self._draft_sync.bunker_error.connect(self._on_draft_sync_bunker_error)
        # Created lazily inside ``_build_findbar`` so its parent is the
        # central widget rather than ``self`` — keeps Qt's geometry
        # reasoning straightforward.
        self._drafts_panel: Optional[DraftsPanel] = None
        # Maps each open editor → its conflict banner widget so we can
        # avoid stacking duplicate banners on the same tab.
        self._tab_conflict_banners: dict = {}

        # Blossom media library — orchestrates uploads / list / delete
        # against the user's configured Blossom servers, signing each
        # auth event through the existing bunker pool.
        self._media_store = MediaStore(
            session_pool=self._session_pool,
            profile_provider=lambda: self._profile_store.default(),
            parent=self,
        )
        # Loader used by the editor's "Insert image" flow to materialize
        # a Blossom URL into a local file before calling QTextCursor.insertImage.
        self._media_image_loader = ThumbnailLoader(parent=self)
        self._media_image_loader.ready.connect(self._on_media_image_ready)
        self._media_image_loader.failed.connect(self._on_media_image_failed)
        # hash -> (editor_id, source_url, alt_text). Populated when we
        # pick or paste an image whose bytes aren't cached yet; drained
        # by _on_media_image_ready / _on_media_image_failed.
        self._pending_image_inserts: dict = {}
        # display_name -> editor_id. Tracks drag-/paste-originated
        # uploads so we can auto-insert into the originating editor
        # when MediaStore.upload_finished fires.
        self._pending_upload_inserts: dict = {}
        self._media_store.upload_finished.connect(self._on_upload_finished_for_insert)
        self._media_store.upload_failed.connect(self._on_upload_failed_for_insert)

        self._update_profile_chip()
        self._refresh_profile_chip_menu()
        # If a profile exists from a previous session, refresh its metadata
        # in the background and prime the mentions cache from the NIP-02
        # contact list.
        active = self._profile_store.default()
        if active is not None:
            self._metadata_fetcher.fetch(active)
            self._contact_fetcher.fetch(active.user_pubkey, active.bunker_relays)
            # Start the draft sync in the background. It's idempotent —
            # if the panel is never opened, this still keeps the store
            # warm so opening the panel later is instant.
            self._draft_sync.start_for(active)

        self._build_actions()
        self._build_menu()
        self._build_status_bar_view_toggle()
        self._build_findbar()

        # header_widget and findbar always construct themselves in dark
        # mode; bring them in line with a light theme detected above.
        if not self.is_dark_theme:
            self._set_theme(self.is_dark_theme, announce=False)

        if initial_path and os.path.isfile(initial_path):
            self.open_path(initial_path)

        restored = self._restore_backups()
        session = self._restore_session() if not initial_path and not restored else False
        if not initial_path and not restored and not session:
            if "welcome_shown" not in load_settings():
                self.show_welcome_tab()
                save_setting("welcome_shown", True)
            else:
                self.new_tab()

        self.tabs.currentChanged.connect(self._on_tab_changed)
        self._update_undo_redo_buttons()
        self._update_status_bar()
        self._start_ipc_server()
        QTimer.singleShot(3000, self._maybe_auto_check_for_updates)


    # ----------------------------------------------------------------------
    # IPC - SINGLE INSTANCE
    # ----------------------------------------------------------------------
    def _start_ipc_server(self):
        """Start a local socket server so that a second launch can forward a
        file path here instead of opening a new window."""
        QLocalServer.removeServer(_IPC_SERVER_NAME)  # remove stale socket if any
        self._ipc_server = QLocalServer(self)
        self._ipc_server.newConnection.connect(self._on_ipc_connection)
        self._ipc_server.listen(_IPC_SERVER_NAME)

    def _on_ipc_connection(self):
        conn = self._ipc_server.nextPendingConnection()
        conn.waitForReadyRead(300)
        path = conn.readAll().data().decode("utf-8").strip()
        conn.deleteLater()
        if path and os.path.isfile(path):
            self.open_path(path)
        self.raise_()
        self.activateWindow()

    # ----------------------------------------------------------------------
    # THEME APPLICATION
    # ----------------------------------------------------------------------
    @staticmethod
    def _detect_os_dark_theme() -> bool:
        """Best-effort read of the OS color scheme. Dark is the fallback
        default when the platform does not report a scheme."""
        scheme = QGuiApplication.styleHints().colorScheme()
        return scheme != Qt.ColorScheme.Light

    def _apply_theme(self):
        # Sync the application palette first so pop-ups the window stylesheet
        # never touches (message boxes, input dialogs, the tab scroller)
        # follow the theme. This is the single call site for both startup
        # and every later toggle, since _set_theme routes through here.
        theme.apply_app_theme(self.is_dark_theme)

        bg = DARK_BG if self.is_dark_theme else LIGHT_BG
        fg = DARK_FG if self.is_dark_theme else LIGHT_FG
        menu_bg = DARK_MENU_BG if self.is_dark_theme else LIGHT_MENU_BG
        menu_fg = DARK_MENU_FG if self.is_dark_theme else LIGHT_MENU_FG
        border = DARK_BORDER if self.is_dark_theme else LIGHT_BORDER

        self.setStyleSheet(f"""
            QMainWindow {{ background: {bg}; }}
            QTabWidget::pane {{
                border-top: 1px solid {border};
                background: {bg};
            }}
            QTabBar {{
                background: {menu_bg};
            }}
            QTabBar::tab {{
                background: {menu_bg};
                color: {menu_fg};
                padding: 8px 16px;
                border: 1px solid {border};
                border-bottom: none;
                border-top-left-radius: 6px;
                border-top-right-radius: 6px;
                margin-right: 2px;
            }}
            QTabBar::tab:selected {{
                background: {bg};
                border-bottom: 1px solid {bg};
            }}
            QTabBar::tab:hover {{ background: {menu_bg}; }}
            QTabBar QToolButton {{
                background: {menu_bg};
                color: {menu_fg};
                border: 1px solid {border};
            }}
            QTabBar QToolButton:hover {{ background: {bg}; }}
            QStatusBar {{
                background: {menu_bg};
                color: {menu_fg};
                border-top: 1px solid {border};
            }}
            QMenuBar {{
                background: {menu_bg};
                color: {menu_fg};
                border-bottom: 1px solid {border};
            }}
            QMenuBar::item:selected {{ background: {bg}; }}
            QMenu {{
                background: {menu_bg};
                color: {menu_fg};
                border: 1px solid {border};
            }}
            QMenu::item:selected {{ background: {bg}; }}
            QMenu::item:disabled {{ color: {border}; }}
            QScrollBar:vertical {{
                background: {menu_bg};
                width: 12px;
                border: none;
            }}
            QScrollBar::handle:vertical {{
                background: {border};
                min-height: 20px;
                border-radius: 6px;
                margin: 2px;
            }}
            QScrollBar::handle:vertical:hover {{ background: {fg}; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}
            QScrollBar:horizontal {{
                background: {menu_bg};
                height: 12px;
                border: none;
            }}
            QScrollBar::handle:horizontal {{
                background: {border};
                min-width: 20px;
                border-radius: 6px;
                margin: 2px;
            }}
            QScrollBar::handle:horizontal:hover {{ background: {fg}; }}
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0px; }}
        """)

        if self.is_dark_theme:
            self.plus_btn.setStyleSheet("""
                QToolButton {
                    background: #2D2D30;
                    color: #CCCCCC;
                    border: 1px solid #3C3C3C;
                    border-radius: 4px;
                    font-size: 14px;
                    font-weight: bold;
                    min-width: 20px;
                    min-height: 20px;
                }
                QToolButton:hover { background: #3C3C3C; }
            """)
        else:
            self.plus_btn.setStyleSheet("""
                QToolButton {
                    background: #DCDCDC;
                    color: #222222;
                    border: 1px solid #BBBBBB;
                    border-radius: 4px;
                    font-size: 14px;
                    font-weight: bold;
                    min-width: 20px;
                    min-height: 20px;
                }
                QToolButton:hover { background: #C8C8C8; }
            """)

        label_color = DARK_MENU_FG if self.is_dark_theme else LIGHT_FG
        self._line_label.setStyleSheet(f"color: {label_color}; font-size: 12px;")

        for i in range(self.tabs.count()):
            ed = self._editor_from_widget(self.tabs.widget(i))
            if ed:
                self._update_editor_theme(ed)


    def _auto_detect_language(self, editor):
        """Called via debounce timer; detects language from content for untitled tabs."""
        if getattr(editor, '_language', None):
            return  # already known
        lang = detect_language_from_content(editor.toPlainText())
        if lang and self.syntax_highlighting:
            editor._language = lang
            editor._highlighter = SyntaxHighlighter(
                editor.document(), lang, self.is_dark_theme
            )
            self._update_status_bar()

    def _attach_highlighter(self, editor, path: str | None):
        """Detect language from path and attach / replace syntax highlighter."""
        lang = detect_language(path)
        editor._language = lang
        # Remove existing highlighter if any
        if hasattr(editor, '_highlighter'):
            editor._highlighter.setDocument(None)
            del editor._highlighter
        # Only highlight plain-text source files, not rendered rich documents
        if lang and path and not path.lower().endswith(_RICH_DOC_EXTS) and self.syntax_highlighting:
            editor._highlighter = SyntaxHighlighter(
                editor.document(), lang, self.is_dark_theme
            )

    def _update_editor_theme(self, editor):
        bg = DARK_BG if self.is_dark_theme else LIGHT_BG
        fg = DARK_FG if self.is_dark_theme else LIGHT_FG
        selection = DARK_SELECTION if self.is_dark_theme else LIGHT_SELECTION
        menu_bg = DARK_MENU_BG if self.is_dark_theme else LIGHT_MENU_BG
        border = DARK_BORDER if self.is_dark_theme else LIGHT_BORDER

        editor.setStyleSheet(f"""
            QTextEdit {{
                background: transparent;
                color: {fg};
                border: none;
                selection-background-color: {selection};
                selection-color: {fg};
                font-family: {MONO_FONT};
                font-size: 14px;
                line-height: 1.5;
                padding: 8px;
            }}
            QScrollBar:vertical {{
                background: {menu_bg};
                width: 12px;
                border: none;
            }}
            QScrollBar::handle:vertical {{
                background: {border};
                min-height: 20px;
                border-radius: 6px;
                margin: 2px;
            }}
            QScrollBar::handle:vertical:hover {{ background: {fg}; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}
            QScrollBar:horizontal {{
                background: {menu_bg};
                height: 12px;
                border: none;
            }}
            QScrollBar::handle:horizontal {{
                background: {border};
                min-width: 20px;
                border-radius: 6px;
                margin: 2px;
            }}
            QScrollBar::handle:horizontal:hover {{ background: {fg}; }}
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0px; }}
        """)

        editor._theme_colors = {'bg': bg, 'fg': fg}

        if hasattr(editor, '_highlighter'):
            editor._highlighter.set_theme(self.is_dark_theme)

        if hasattr(editor, '_line_gutter'):
            editor._line_gutter._update_theme()

    def _apply_view_prefs_to_editor(self, editor):
        """Push the current View-menu preferences onto one editor. Called for
        every editor created (new_tab, open_path) so all tabs stay in sync."""
        editor.set_background_pattern(self.editor_background)
        editor.set_highlight_current_line(self.highlight_current_line)
        editor.set_paper_mode(self.paper_mode)


    # ----------------------------------------------------------------------
    # EDITOR / TAB HELPERS
    # ----------------------------------------------------------------------
    def current_editor(self) -> HtmlEditor | None:
        return self._editor_from_widget(self.tabs.currentWidget())

    def current_path(self) -> str | None:
        ed = self.current_editor()
        return getattr(ed, "_file_path", None) if ed else None

    def set_current_path(self, path: str | None):
        ed = self.current_editor()
        if ed is not None:
            ed._file_path = path
            self._update_tab_title()

    def _update_tab_title(self):
        ed = self.current_editor()
        if not ed:
            return
        self.tabs.setTabText(self.tabs.currentIndex(), self._compose_tab_title(ed))
        self._update_status_bar()

    def _compose_tab_title(self, ed) -> str:
        """Compute the display title for a tab.

        Precedence:
          - File path basename if the tab is backed by a local file.
          - Draft title if the tab is purely a draft (opened from the
            drafts panel without a local file).
          - "Untitled" as the final fallback for fresh blank tabs.

        Tabs with a draft binding get a leading lock glyph so the user
        can tell at a glance that contents are encrypted at rest on
        the relays.
        """
        path = getattr(ed, "_file_path", None)
        binding = getattr(ed, "_draft_binding", None)
        dirty = "*" if ed.document().isModified() else ""
        if path:
            base = os.path.basename(path)
        elif binding and binding.title:
            base = binding.title
        elif binding:
            base = "Untitled draft"
        else:
            base = "Untitled"
        prefix = "⚿ " if binding is not None else ""
        return f"{prefix}{base}{dirty}"

    def _update_status_bar(self):
        ed = self.current_editor()
        if not ed:
            self._line_label.setText("")
            return
        path = getattr(ed, "_file_path", None)
        file_info = path if path else "(Untitled)"
        lang = getattr(ed, "_language", None)
        lang_label = LANGUAGE_DISPLAY_NAMES.get(lang, '') if lang else ''
        current_line = ed.textCursor().blockNumber() + 1
        total_lines = ed.document().blockCount()
        parts = [file_info]
        if lang_label:
            parts.append(lang_label)
        page_count = ed.document().pageCount()
        if page_count > 1:
            parts.append(f"Page {page_count}")
        self.status.showMessage(" | ".join(parts))
        self._line_label.setText(f"Ln {current_line} / {total_lines}")
        self._update_format_buttons()

    def _update_window_title(self, *_):
        self.setWindowTitle("")

    def _on_tab_changed(self, index=None):
        self._update_window_title()
        self._update_undo_redo_buttons()
        self._update_status_bar()  # also calls _update_format_buttons
        if self.findbar.isVisible():
            # Clear stale highlights on every non-active tab
            for i in range(self.tabs.count()):
                if i != index:
                    ed = self._editor_from_widget(self.tabs.widget(i))
                    if ed:
                        ed.setExtraSelections([])
            self._search_matches = []
            self._current_match_index = -1
            self._last_search_text = ""
            self._on_search_text_changed()

    def _update_undo_redo_buttons(self):
        ed = self.current_editor()
        can_undo = ed.document().isUndoAvailable() if ed else False
        can_redo = ed.document().isRedoAvailable() if ed else False
        self.header_widget.undo_btn.setEnabled(can_undo)
        self.header_widget.redo_btn.setEnabled(can_redo)

    def _toggle_format(self, fmt: str):
        ed = self.current_editor()
        if ed:
            getattr(ed, f'toggle_{fmt}')()
            self._update_format_buttons()

    def _update_format_buttons(self):
        ed = self.current_editor()
        if not ed:
            self.header_widget.bold_btn.setChecked(False)
            self.header_widget.italic_btn.setChecked(False)
            self.header_widget.underline_btn.setChecked(False)
            return
        cursor = ed.textCursor()
        if cursor.hasSelection():
            bold = ed._all_in_selection(cursor, lambda f: f.fontWeight() > 400)
            italic = ed._all_in_selection(cursor, lambda f: f.fontItalic())
            underline = ed._all_in_selection(cursor, lambda f: f.fontUnderline())
        else:
            fmt = ed.currentCharFormat()
            bold = fmt.fontWeight() > 400
            italic = fmt.fontItalic()
            underline = fmt.fontUnderline()
        self.header_widget.bold_btn.setChecked(bold)
        self.header_widget.italic_btn.setChecked(italic)
        self.header_widget.underline_btn.setChecked(underline)


    # ----------------------------------------------------------------------
    # ACTIONS / MENU
    # ----------------------------------------------------------------------
    def _build_actions(self):
        self.act_new = QAction("New", self)
        self.act_new.setShortcut(QKeySequence.New)
        self.act_new.triggered.connect(self.new_tab)

        self.act_open = QAction("Open…", self)
        self.act_open.setShortcut(QKeySequence.Open)
        self.act_open.triggered.connect(self.open_dialog)

        self.act_save = QAction("Save", self)
        self.act_save.setShortcut(QKeySequence.Save)
        self.act_save.triggered.connect(self.save)

        self.act_save_as = QAction("Save As…", self)
        self.act_save_as.setShortcut(QKeySequence("Ctrl+Shift+S"))
        # Contextual: behaves as classic Save As when no Nostr profile
        # is connected; otherwise asks where to save (local file vs.
        # encrypted Nostr draft) and remembers the per-tab choice. See
        # ``_on_save_as_pressed`` for the full decision tree.
        self.act_save_as.triggered.connect(self._on_save_as_pressed)

        self.act_close_tab = QAction("Close Tab", self)
        self.act_close_tab.setShortcut(QKeySequence("Ctrl+W"))
        self.act_close_tab.triggered.connect(self._close_current_tab)

        self.act_quit = QAction("Quit", self)
        self.act_quit.setShortcut(QKeySequence("Ctrl+Q"))
        self.act_quit.triggered.connect(self._quit_application)

        # Formatting
        self.act_bold = QAction("Bold", self)
        self.act_bold.setShortcut(QKeySequence("Ctrl+B"))
        self.act_bold.triggered.connect(self._fmt_bold)

        self.act_italic = QAction("Italic", self)
        self.act_italic.setShortcut(QKeySequence("Ctrl+I"))
        self.act_italic.triggered.connect(self._fmt_italic)

        self.act_underline = QAction("Underline", self)
        self.act_underline.setShortcut(QKeySequence("Ctrl+U"))
        self.act_underline.triggered.connect(self._fmt_underline)

        self.act_reset_format = QAction("Reset Format", self)
        self.act_reset_format.setShortcut(QKeySequence("Ctrl+D"))
        self.act_reset_format.triggered.connect(self._reset_format)

        # Search
        self.act_find = QAction("Find", self)
        self.act_find.setShortcut(QKeySequence("Ctrl+F"))
        self.act_find.triggered.connect(self._toggle_findbar)

        self.act_find_next = QAction("Find Next", self)
        self.act_find_next.setShortcut(QKeySequence("F3"))
        self.act_find_next.triggered.connect(self._find_next)

        self.act_find_prev = QAction("Find Previous", self)
        self.act_find_prev.setShortcut(QKeySequence("Shift+F3"))
        self.act_find_prev.triggered.connect(self._find_prev)

        self._search_matches = []
        self._current_match_index = -1
        self._last_search_text = ""
        self._search_extra_selections = []

        # Theme + line numbers
        self.act_toggle_theme = QAction("Toggle Dark/Light Theme", self)
        self.act_toggle_theme.setShortcut(QKeySequence("Ctrl+Shift+T"))
        self.act_toggle_theme.triggered.connect(self._toggle_theme)

        self.act_toggle_line_numbers = QAction("Show Line Numbers", self)
        self.act_toggle_line_numbers.setShortcut(QKeySequence("Ctrl+Shift+L"))
        self.act_toggle_line_numbers.triggered.connect(self._toggle_line_numbers)
        self.act_toggle_line_numbers.setCheckable(True)

        self.act_toggle_syntax_hl = QAction("Syntax Highlighting", self)
        self.act_toggle_syntax_hl.setShortcut(QKeySequence("Ctrl+Shift+H"))
        self.act_toggle_syntax_hl.triggered.connect(self._toggle_syntax_highlighting)
        self.act_toggle_syntax_hl.setCheckable(True)
        self.act_toggle_syntax_hl.setChecked(True)

        # View menu: background viewing aids. All four toggles are independent
        # and composable except the background pattern, which is a radio pick.
        self.act_paper_mode = QAction("Paper Mode", self)
        self.act_paper_mode.setCheckable(True)
        self.act_paper_mode.setChecked(self.paper_mode)
        self.act_paper_mode.toggled.connect(self._toggle_paper_mode)

        self._bg_pattern_group = QActionGroup(self)
        self._bg_pattern_group.setExclusive(True)

        self.act_bg_none = QAction("None", self)
        self.act_bg_lines = QAction("Lines", self)
        self.act_bg_dashed = QAction("Dashed", self)
        self.act_bg_dots = QAction("Dots", self)
        self.act_bg_grid = QAction("Grid", self)

        for name, action in (
            ("none", self.act_bg_none),
            ("lines", self.act_bg_lines),
            ("dashed", self.act_bg_dashed),
            ("dots", self.act_bg_dots),
            ("grid", self.act_bg_grid),
        ):
            action.setCheckable(True)
            action.setChecked(self.editor_background == name)
            action.triggered.connect(lambda checked, n=name: self._set_background_pattern(n))
            self._bg_pattern_group.addAction(action)

        self.act_highlight_line = QAction("Highlight Current Line", self)
        self.act_highlight_line.setCheckable(True)
        self.act_highlight_line.setChecked(self.highlight_current_line)
        self.act_highlight_line.toggled.connect(self._toggle_highlight_line)

        self.addAction(self.act_bold)
        self.addAction(self.act_italic)
        self.addAction(self.act_underline)
        self.addAction(self.act_reset_format)
        self.addAction(self.act_toggle_theme)
        self.addAction(self.act_toggle_line_numbers)
        self.addAction(self.act_toggle_syntax_hl)

    def _build_menu(self):
        m_file = self.menuBar().addMenu("&File")
        m_file.addAction(self.act_new)
        m_file.addAction(self.act_open)
        m_file.addSeparator()
        self.m_recent = m_file.addMenu("Recent Files")
        self._populate_recent_menu()
        m_file.addSeparator()
        m_file.addAction(self.act_save)
        m_file.addAction(self.act_save_as)
        m_file.addSeparator()
        m_file.addAction(self.act_close_tab)
        m_file.addAction(self.act_quit)

        m_find = self.menuBar().addMenu("&Search")
        m_find.addAction(self.act_find)
        m_find.addAction(self.act_find_next)
        m_find.addAction(self.act_find_prev)

        m_view = self.menuBar().addMenu("&View")
        m_view.addAction(self.act_paper_mode)
        m_view.addSeparator()
        m_background = m_view.addMenu("Background")
        m_background.addAction(self.act_bg_none)
        m_background.addAction(self.act_bg_lines)
        m_background.addAction(self.act_bg_dashed)
        m_background.addAction(self.act_bg_dots)
        m_background.addAction(self.act_bg_grid)
        m_view.addAction(self.act_highlight_line)

        m_nostr = self.menuBar().addMenu("&Nostr")
        act_nostr_publish = QAction("Publish as Note…", self)
        act_nostr_publish.setShortcut(QKeySequence("Ctrl+Shift+P"))
        act_nostr_publish.triggered.connect(self._on_nostr_publish_note)
        m_nostr.addAction(act_nostr_publish)
        act_nostr_publish_article = QAction("Publish as Article…", self)
        act_nostr_publish_article.setShortcut(QKeySequence("Ctrl+Shift+A"))
        act_nostr_publish_article.triggered.connect(self._on_nostr_publish_article)
        m_nostr.addAction(act_nostr_publish_article)
        m_nostr.addSeparator()
        # Media (Blossom) — browse the user's uploaded blobs, upload new
        # ones, or insert one into the current document at the cursor.
        act_nostr_media = QAction("Media Library…", self)
        act_nostr_media.setShortcut(QKeySequence("Ctrl+Shift+M"))
        act_nostr_media.triggered.connect(self._on_nostr_media_library)
        m_nostr.addAction(act_nostr_media)
        self.addAction(act_nostr_media)
        act_nostr_insert_image = QAction("Insert image…", self)
        act_nostr_insert_image.setShortcut(QKeySequence("Ctrl+Shift+I"))
        act_nostr_insert_image.triggered.connect(self._on_nostr_insert_image)
        m_nostr.addAction(act_nostr_insert_image)
        self.addAction(act_nostr_insert_image)
        m_nostr.addSeparator()
        # Drafts surface — the side-docked panel. ``Ctrl+Shift+D`` (D
        # for Draft) toggles it, sitting alongside the other Ctrl+Shift
        # Nostr shortcuts.
        self.act_nostr_drafts = QAction("Drafts…", self)
        self.act_nostr_drafts.setShortcut(QKeySequence("Ctrl+Shift+D"))
        self.act_nostr_drafts.setCheckable(True)
        self.act_nostr_drafts.triggered.connect(self._on_toggle_drafts_panel)
        m_nostr.addAction(self.act_nostr_drafts)
        # Register globally so the shortcut works even when the menu
        # bar is hidden (Linux compact themes, full-screen mode).
        self.addAction(self.act_nostr_drafts)
        m_nostr.addSeparator()
        act_nostr_connect = QAction("Connect Signer…", self)
        act_nostr_connect.triggered.connect(self._on_nostr_connect)
        m_nostr.addAction(act_nostr_connect)
        act_nostr_sign_out = QAction("Sign Out Active Profile", self)
        act_nostr_sign_out.triggered.connect(self._on_nostr_sign_out)
        m_nostr.addAction(act_nostr_sign_out)

        help_menu = self.menuBar().addMenu("&Help")
        welcome_action = QAction("Welcome", self)
        welcome_action.triggered.connect(self.show_welcome_tab)
        help_menu.addAction(welcome_action)
        shortcuts_action = QAction("Keyboard Shortcuts", self)
        shortcuts_action.triggered.connect(self._show_shortcuts)
        help_menu.addAction(shortcuts_action)
        help_menu.addSeparator()
        check_updates_action = QAction("Check for Updates", self)
        check_updates_action.triggered.connect(self._check_for_updates_manual)
        help_menu.addAction(check_updates_action)
        help_menu.addSeparator()
        about_action = QAction("About", self)
        about_action.triggered.connect(self._show_about)
        help_menu.addAction(about_action)

    def _build_status_bar_view_toggle(self):
        """Quick status-bar toggle for Paper Mode, mirroring the View menu item."""
        self._paper_btn = QToolButton()
        self._paper_btn.setText("Paper")
        self._paper_btn.setToolTip("Toggle Paper Mode")
        self._paper_btn.setCheckable(True)
        self._paper_btn.setChecked(self.paper_mode)
        self._paper_btn.setAutoRaise(True)
        self._paper_btn.toggled.connect(self._toggle_paper_mode)
        self.status.addPermanentWidget(self._paper_btn)

    def _populate_recent_menu(self):
        self.m_recent.clear()
        self.m_recent.setToolTipsVisible(True)
        entries = [p for p in load_recent() if os.path.exists(p)]

        if not entries:
            empty = QAction("(empty)", self)
            empty.setEnabled(False)
            self.m_recent.addAction(empty)
        else:
            for path in entries:
                action = QAction(os.path.basename(path), self)
                action.setToolTip(path)
                action.triggered.connect(lambda checked, p=path: self.open_path(p))
                self.m_recent.addAction(action)

        self.m_recent.addSeparator()
        clear_widget = QLabel("  Clear Recent Files  ")
        clear_widget.setContentsMargins(4, 4, 4, 4)
        clear_widget.setStyleSheet("""
            QLabel {
                color: #CC4444;
                padding: 4px 8px;
            }
            QLabel:hover {
                background: rgba(180, 40, 40, 0.15);
                color: #FF6666;
            }
        """)
        clear_action = QWidgetAction(self)
        clear_action.setDefaultWidget(clear_widget)
        clear_action.triggered.connect(self._clear_recent_files)
        clear_widget.mousePressEvent = lambda e: (self.m_recent.close(), self._clear_recent_files())
        self.m_recent.addAction(clear_action)

    def _clear_recent_files(self):
        clear_recent()
        self._populate_recent_menu()

    def _build_findbar(self):
        self.findbar = FindBar(self._find_next, self._find_prev, self._toggle_findbar, self)
        self.findbar.setVisible(False)
        self.findbar.edit.textChanged.connect(self._on_search_text_changed)

        self.update_bar = UpdateBar()
        self.update_bar.update_theme(self.is_dark_theme)
        self.update_bar.download_requested.connect(self._on_update_bar_download)
        self.update_bar.dismissed.connect(self._on_update_bar_dismissed)

        # The editor area (header + findbar + tabs) lives on the left
        # of a horizontal splitter; the drafts panel docks on the right.
        # We always create the panel — keeping it always-present (just
        # hidden) preserves the layout's geometry across show/hide and
        # avoids re-parenting issues. When no Nostr profile is connected,
        # the panel itself renders the "Connect a Nostr profile" empty
        # state without taking visual space beyond a thin border.
        editor_side = QWidget()
        v = QVBoxLayout(editor_side)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        v.addWidget(self.header_widget, 0)
        v.addWidget(self.findbar, 0)
        v.addWidget(self.update_bar, 0)
        v.addWidget(self.tabs, 1)

        self._drafts_panel = DraftsPanel(is_dark=self.is_dark_theme)
        self._drafts_panel.bind_store(self._draft_store)
        self._drafts_panel.set_avatar_store(self._avatars)
        self._drafts_panel.feeds.bind_runtime(
            relay_pool=self._relay_pool,
            relay_list_cache=self._relay_list_cache,
            session_pool=self._session_pool,
        )
        self._drafts_panel.set_active_profile(self._profile_store.default())
        # The panel's outbound actions all route back through the host.
        self._drafts_panel.open_draft.connect(self._on_panel_open_draft)
        self._drafts_panel.publish_draft.connect(self._on_panel_publish_draft)
        self._drafts_panel.delete_draft.connect(self._on_panel_delete_draft)
        self._drafts_panel.retry_decrypt.connect(self._on_panel_retry_decrypt)
        self._drafts_panel.copy_event_id.connect(self._on_panel_copy_event_id)
        self._drafts_panel.switch_profile_requested.connect(
            self._on_panel_switch_profile
        )
        self._drafts_panel.refresh_requested.connect(self._draft_sync.refresh)
        self._drafts_panel.close_requested.connect(self._hide_drafts_panel)

        self._central_splitter = QSplitter(Qt.Horizontal)
        self._central_splitter.setObjectName("central_splitter")
        self._central_splitter.setHandleWidth(1)
        self._central_splitter.setChildrenCollapsible(False)
        self._central_splitter.addWidget(editor_side)
        self._central_splitter.addWidget(self._drafts_panel)
        # Editor takes all stretch; panel starts hidden.
        self._central_splitter.setStretchFactor(0, 1)
        self._central_splitter.setStretchFactor(1, 0)
        self._drafts_panel.hide()

        self.setCentralWidget(self._central_splitter)


    # ----------------------------------------------------------------------
    # CRASH RECOVERY
    # ----------------------------------------------------------------------
    def _restore_backups(self) -> bool:
        """Silently restore any backup files left over from a previous crash.

        Returns True if at least one backup was restored.
        """
        backups = find_all_backups()
        for backup in backups:
            original_path = backup.get("original_path")
            content = backup.get("content", "")
            backup_file = backup["_backup_file"]

            ed = HtmlEditor()
            ed.setPlainText(content)
            ed._file_path = original_path
            ed.document().setModified(True)
            ed.document().contentsChanged.connect(self._update_tab_title)
            ed.document().undoAvailable.connect(self._update_undo_redo_buttons)
            ed.document().redoAvailable.connect(self._update_undo_redo_buttons)
            ed.cursorPositionChanged.connect(self._update_status_bar)
            ed.currentCharFormatChanged.connect(self._update_format_buttons)
            ed.selectionChanged.connect(self._update_format_buttons)
            self._update_editor_theme(ed)

            container = QWidget()
            vbox = QVBoxLayout(container)
            vbox.setContentsMargins(0, 0, 0, 0)
            vbox.setSpacing(0)

            bar = FileChangedBar()
            bar.update_theme(self.is_dark_theme)
            bar.reload_requested.connect(lambda b=bar, e=ed: self._reload_from_disk(e, b))
            vbox.addWidget(bar)

            editor_area = QWidget()
            layout = QHBoxLayout(editor_area)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(0)

            if self.show_line_numbers:
                gutter = LineNumberGutter(ed)
                layout.addWidget(gutter)
                ed._line_gutter = gutter

            layout.addWidget(ed)
            vbox.addWidget(editor_area)

            base_name = os.path.basename(original_path) if original_path else "Untitled"
            idx = self.tabs.addTab(container, f"{base_name} (recovered)*")
            self._attach_close_button(idx, container)

            if original_path:
                self._watcher.addPath(original_path)

            # Replace the old backup file with a fresh one for the restored content
            os.remove(backup_file)
            ed._backup = EditorBackup(ed, original_path)
            ed._backup.write_now()

        return len(backups) > 0

    # ----------------------------------------------------------------------
    # FILE I/O
    # ----------------------------------------------------------------------
    def new_tab(self):
        ed = HtmlEditor()
        ed.document().contentsChanged.connect(self._update_tab_title)
        ed.document().undoAvailable.connect(self._update_undo_redo_buttons)
        ed.document().redoAvailable.connect(self._update_undo_redo_buttons)
        ed.cursorPositionChanged.connect(self._update_status_bar)
        ed.currentCharFormatChanged.connect(self._update_format_buttons)
        ed.selectionChanged.connect(self._update_format_buttons)
        self._update_editor_theme(ed)
        self._apply_view_prefs_to_editor(ed)

        container = QWidget()
        vbox = QVBoxLayout(container)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(0)

        bar = FileChangedBar()
        bar.update_theme(self.is_dark_theme)
        bar.reload_requested.connect(lambda b=bar, e=ed: self._reload_from_disk(e, b))
        vbox.addWidget(bar)

        editor_area = QWidget()
        layout = QHBoxLayout(editor_area)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        if self.show_line_numbers:
            gutter = LineNumberGutter(ed)
            layout.addWidget(gutter)
            ed._line_gutter = gutter

        layout.addWidget(ed)
        vbox.addWidget(editor_area)

        idx = self.tabs.addTab(container, "Untitled*")
        self.tabs.setCurrentIndex(idx)

        self._attach_close_button(idx, container)

        ed._file_path = None
        ed._language = None

        # Debounce timer: detect language from pasted/typed content
        _timer = QTimer(ed)
        _timer.setSingleShot(True)
        _timer.setInterval(600)
        _timer.timeout.connect(lambda: self._auto_detect_language(ed))
        ed.document().contentsChanged.connect(_timer.start)
        ed._lang_detect_timer = _timer

        ed.setHtml("<div></div>")
        ed._backup = EditorBackup(ed, None)
        ed.setFocus()
        self._update_window_title()

    def show_welcome_tab(self):
        """Open a friendly first-run tab introducing the app."""
        self.new_tab()
        ed = self.current_editor()
        ed.setHtml(welcome_html())
        ed.document().setModified(False)
        self.tabs.setTabText(self.tabs.currentIndex(), "Welcome")
        self._update_status_bar()

    def _on_tab_context_menu(self, pos):
        idx = self.tabs.tabBar().tabAt(pos)
        if idx < 0:
            return
        ed = self._editor_from_widget(self.tabs.widget(idx))
        has_path = bool(getattr(ed, '_file_path', None))

        menu = QMenu(self)
        rename_action = menu.addAction("Rename")
        rename_action.setEnabled(has_path)
        if not has_path:
            rename_action.setToolTip("Save the file first before renaming")

        menu.addSeparator()

        delete_action = menu.addAction("Delete File...")
        delete_action.setEnabled(has_path)
        if not has_path:
            delete_action.setToolTip("No file on disk to delete")

        action = menu.exec(self.tabs.tabBar().mapToGlobal(pos))
        if action == rename_action:
            self._rename_tab(idx, ed)
        elif action == delete_action:
            self._delete_tab_file(idx, ed)

    def _delete_tab_file(self, idx: int, ed):
        file_path = ed._file_path
        file_name = os.path.basename(file_path)

        msg = QMessageBox(self)
        msg.setWindowTitle("Delete file")
        msg.setIcon(QMessageBox.NoIcon)
        msg.setText(f'Are you sure you want to move "{file_name}" to trash?')
        msg.setStandardButtons(QMessageBox.Cancel)
        delete_btn = msg.addButton("Move to Trash", QMessageBox.DestructiveRole)
        delete_btn.setStyleSheet("""
            QPushButton {
                color: white;
                background-color: transparent;
                border: 1px solid #888;
                border-radius: 4px;
                padding: 4px 12px;
            }
            QPushButton:hover {
                background-color: #c0392b;
                border-color: #c0392b;
            }
        """)
        msg.setDefaultButton(QMessageBox.Cancel)
        msg.setWindowFlag(Qt.WindowCloseButtonHint, False)

        button_box = msg.findChild(QDialogButtonBox)
        if button_box:
            button_box.setCenterButtons(True)

        msg.exec()
        if msg.clickedButton() != delete_btn:
            return

        try:
            send2trash(file_path)
        except Exception as e:
            QMessageBox.critical(self, "Delete Failed", str(e))
            return

        # Close tab without prompting to save — file is gone
        if hasattr(ed, '_backup'):
            ed._backup.delete()
        self._watcher.removePath(file_path)
        self.tabs.removeTab(idx)

    def _rename_tab(self, idx: int, ed):
        old_path = ed._file_path
        old_name = os.path.basename(old_path)
        directory = os.path.dirname(old_path)

        new_name, ok = QInputDialog.getText(self, "Rename File", "New filename:", text=old_name)
        if not ok or not new_name.strip() or new_name.strip() == old_name:
            return

        new_name = new_name.strip()
        new_path = os.path.join(directory, new_name)

        if os.path.exists(new_path):
            QMessageBox.warning(self, "Rename Failed", f'A file named "{new_name}" already exists.')
            return

        try:
            os.rename(old_path, new_path)
        except OSError as e:
            QMessageBox.critical(self, "Rename Failed", str(e))
            return

        self._watcher.removePath(old_path)
        self._watcher.addPath(new_path)
        ed._file_path = new_path
        if hasattr(ed, '_backup'):
            ed._backup.update_file_path(new_path)
        self.tabs.setTabText(idx, new_name)
        self._update_window_title()

    def _attach_close_button(self, idx: int, container: QWidget):
        close_btn = QPushButton("×")
        close_btn.setFixedSize(24, 24)
        close_btn.setStyleSheet("""
            QPushButton {
                color: #A84444;
                background: transparent;
                border: none;
                border-radius: 4px;
                font-size: 18px;
                padding: 0px;
            }
            QPushButton:hover {
                color: #D06060;
                background: rgba(168, 68, 68, 0.15);
            }
            QPushButton:pressed {
                background: rgba(168, 68, 68, 0.30);
            }
        """)
        close_btn.setToolTip("Close tab")
        close_btn.clicked.connect(lambda: self.close_tab(self.tabs.indexOf(container)))
        self.tabs.tabBar().setTabButton(idx, QTabBar.RightSide, close_btn)

    def close_tab(self, index: int):
        w = self.tabs.widget(index)
        editor = self._editor_from_widget(w)

        if editor and editor.document().isModified():
            file_name = os.path.basename(editor._file_path) if getattr(editor, '_file_path', None) else "Untitled"
            r = QMessageBox.question(
                self, "Save Changes?",
                f"The document '{file_name}' has unsaved changes.\n\nDo you want to save before closing?",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
                QMessageBox.Yes
            )
            if r == QMessageBox.Cancel:
                return
            if r == QMessageBox.Yes:
                self.tabs.setCurrentIndex(index)
                if not self.save():
                    return
        if editor and hasattr(editor, '_backup'):
            editor._backup.delete()
        if editor:
            path = getattr(editor, '_file_path', None)
            if path:
                self._watcher.removePath(path)
            # A draft tab might own a conflict banner — drop the dict
            # entry so we don't accumulate references to dead editors
            # across the session.
            banner = self._tab_conflict_banners.pop(editor, None)
            if banner is not None:
                banner.setParent(None)
                banner.deleteLater()
            # Cancel any in-flight stash so its signal callbacks don't
            # fire against a destroyed editor. The signer round-trip
            # itself can't be recalled, but ``cancel()`` flips a flag
            # that suppresses all future signal emissions.
            active_job = getattr(editor, "_active_stash_job", None)
            if active_job is not None:
                active_job.cancel()
                editor._active_stash_job = None
        self.tabs.removeTab(index)

    def _close_current_tab(self):
        idx = self.tabs.currentIndex()
        if idx >= 0:
            self.close_tab(idx)

    def _quit_application(self):
        unsaved = []
        for i in range(self.tabs.count()):
            ed = self._editor_from_widget(self.tabs.widget(i))
            if ed and ed.document().isModified():
                unsaved.append(os.path.basename(ed._file_path) if getattr(ed, '_file_path', None) else "Untitled")

        if unsaved:
            msg = (f"The document '{unsaved[0]}' has unsaved changes.\n\nDo you want to save before quitting?"
                   if len(unsaved) == 1
                   else f"You have {len(unsaved)} documents with unsaved changes.\n\nDo you want to save all before quitting?")
            r = QMessageBox.question(self, "Save Changes?", msg,
                                     QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
                                     QMessageBox.Yes)
            if r == QMessageBox.Cancel:
                return
            if r == QMessageBox.Yes:
                for i in range(self.tabs.count()):
                    ed = self._editor_from_widget(self.tabs.widget(i))
                    if ed and ed.document().isModified():
                        self.tabs.setCurrentIndex(i)
                        if not self.save():
                            return
        for i in range(self.tabs.count()):
            ed = self._editor_from_widget(self.tabs.widget(i))
            if ed and hasattr(ed, '_backup'):
                ed._backup.delete()
        self.close()

    def _editor_from_widget(self, w) -> HtmlEditor | None:
        if isinstance(w, HtmlEditor):
            return w
        if isinstance(w, QWidget):
            return w.findChild(HtmlEditor)
        return None

    def _bar_from_widget(self, w) -> FileChangedBar | None:
        if isinstance(w, QWidget):
            return w.findChild(FileChangedBar)
        return None

    # ----------------------------------------------------------------------
    # DRAG AND DROP
    # ----------------------------------------------------------------------
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() or event.mimeData().hasText():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        if event.mimeData().hasUrls():
            self._handle_dropped_urls(event.mimeData().urls())
            event.acceptProposedAction()
        elif event.mimeData().hasText():
            self._handle_dropped_text(event.mimeData().text())
            event.acceptProposedAction()

    def _handle_dropped_urls(self, urls):
        unsupported = []
        image_paths = []
        for url in urls:
            if not url.isLocalFile():
                continue
            path = url.toLocalFile()
            ext = os.path.splitext(path)[1].lower()
            if ext in _SUPPORTED_EXTS:
                self.open_path(path)
            elif ext in _IMAGE_EXTS:
                image_paths.append(path)
            else:
                unsupported.append(os.path.basename(path))

        if image_paths:
            self._handle_dropped_images(image_paths)

        if unsupported:
            bar = self._bar_from_widget(self.tabs.currentWidget())
            if bar:
                bar.show_unsupported(", ".join(unsupported))

    def _handle_dropped_images(self, paths):
        """An image was dropped on the window. Offer to upload it to
        Blossom and insert the resulting URL at the current cursor.

        No-op with a single informational dialog when no profile is
        connected: the editor's no-image-support story is fine for that
        case (no key, no upload, no insert)."""
        active = self._profile_store.default()
        if active is None:
            QMessageBox.information(
                self,
                "Connect a signer first",
                "Connect a Nostr signer (Nostr → Connect Signer…) before "
                "uploading images to Blossom.",
            )
            return
        ed = self.current_editor()
        if ed is None:
            self.new_tab()
            ed = self.current_editor()
        if ed is None:
            return

        names = ", ".join(os.path.basename(p) for p in paths)
        prompt = QMessageBox(self)
        prompt.setWindowTitle("Upload image to Blossom")
        prompt.setText(
            f"Upload {len(paths)} image{'s' if len(paths) != 1 else ''} "
            f"({names}) to your Blossom servers and insert at the cursor?"
        )
        prompt.setStandardButtons(QMessageBox.Cancel | QMessageBox.Yes)
        prompt.setDefaultButton(QMessageBox.Yes)
        if prompt.exec() != QMessageBox.Yes:
            return

        # Match by basename — MediaStore emits upload_finished(name, ...)
        # using the file's basename as the display name.
        for path in paths:
            self._pending_upload_inserts[os.path.basename(path)] = id(ed)
            self._media_store.upload_file(path)
        self.status.showMessage(
            f"Uploading {len(paths)} image{'s' if len(paths) != 1 else ''} to Blossom…",
            5000,
        )

    def _handle_dropped_text(self, text: str):
        ed = self.current_editor()
        if ed is None:
            self.new_tab()
            ed = self.current_editor()
        cursor = ed.textCursor()
        fmt = QTextCharFormat()
        fmt.setFontWeight(400)
        fmt.setFontItalic(False)
        fmt.setFontUnderline(False)
        fmt.clearForeground()
        cursor.insertText(text, fmt)
        ed.setTextCursor(cursor)

    def open_dialog(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open", "",
            "Note files (*.md *.html *.txt);;All files (*.*)"
        )
        if path:
            self.open_path(path)

    def open_path(self, path: str):
        for i in range(self.tabs.count()):
            ed = self._editor_from_widget(self.tabs.widget(i))
            if ed and getattr(ed, '_file_path', None) == path:
                self.tabs.setCurrentIndex(i)
                bar = self._bar_from_widget(self.tabs.widget(i))
                if bar:
                    bar.show_already_open()
                return

        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"File could not be read:\n{e}")
            return

        ed = HtmlEditor()
        ext = path.lower()
        if ext.endswith(('.html', '.htm')):
            ed.setHtml(content)
        elif ext.endswith('.md'):
            ed.document().setMarkdown(content)
            ed._loaded_as_markdown = True
        else:
            ed.setPlainText(content)

        ed._file_path = path
        ed.document().setModified(False)
        ed.document().contentsChanged.connect(self._update_tab_title)
        ed.document().undoAvailable.connect(self._update_undo_redo_buttons)
        ed.document().redoAvailable.connect(self._update_undo_redo_buttons)
        ed.cursorPositionChanged.connect(self._update_status_bar)
        ed.currentCharFormatChanged.connect(self._update_format_buttons)
        ed.selectionChanged.connect(self._update_format_buttons)
        self._update_editor_theme(ed)
        self._apply_view_prefs_to_editor(ed)
        self._attach_highlighter(ed, path)

        container = QWidget()
        vbox = QVBoxLayout(container)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(0)

        bar = FileChangedBar()
        bar.update_theme(self.is_dark_theme)
        bar.reload_requested.connect(lambda b=bar, e=ed: self._reload_from_disk(e, b))
        vbox.addWidget(bar)

        editor_area = QWidget()
        layout = QHBoxLayout(editor_area)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        if self.show_line_numbers:
            gutter = LineNumberGutter(ed)
            layout.addWidget(gutter)
            ed._line_gutter = gutter

        layout.addWidget(ed)
        vbox.addWidget(editor_area)

        idx = self.tabs.addTab(container, os.path.basename(path))
        self.tabs.setCurrentIndex(idx)
        self._attach_close_button(idx, container)
        ed._backup = EditorBackup(ed, path)
        self._watcher.addPath(path)
        add_recent(path)
        self._populate_recent_menu()
        ed.setFocus()
        self._update_window_title()

    def _has_formatting(self, ed: HtmlEditor) -> bool:
        """Return True if the document contains any bold, italic, underline, or color formatting."""
        block = ed.document().begin()
        while block.isValid():
            it = block.begin()
            while not it.atEnd():
                fragment = it.fragment()
                if fragment.isValid():
                    fmt = fragment.charFormat()
                    if (fmt.fontWeight() > 400
                            or fmt.fontItalic()
                            or fmt.fontUnderline()
                            or fmt.hasProperty(QTextCharFormat.ForegroundBrush)):
                        return True
                it += 1
            block = block.next()
        return False

    def _warn_formatting_loss(self, ext_label: str) -> str:
        """Show warning dialog when saving to a format that loses formatting.
        Returns 'anyway', 'html', 'rtf', or 'cancel'."""
        msg = QMessageBox(self)
        msg.setWindowTitle("Formatting will be lost")
        msg.setIcon(QMessageBox.Warning)
        msg.setText(
            f"This document contains colors or text formatting\n"
            f"that cannot be stored in {ext_label}."
        )
        anyway_btn = msg.addButton(f"Save as {ext_label} anyway", QMessageBox.DestructiveRole)
        html_btn   = msg.addButton("Save as .html", QMessageBox.AcceptRole)
        rtf_btn    = msg.addButton("Save as .rtf", QMessageBox.AcceptRole)
        cancel_btn = msg.addButton(QMessageBox.Cancel)
        msg.setDefaultButton(cancel_btn)

        anyway_btn.setToolTip("Formatting and colors will be permanently removed from the saved file.")
        html_btn.setToolTip("Saves all colors, bold, italic and formatting.\nBest choice for editing in this editor.")
        rtf_btn.setToolTip("Saves all colors, bold, italic and formatting.\nCompatible with Word, LibreOffice and other apps.")

        _btn_base = """
            QPushButton {
                padding: 6px 14px;
                border-radius: 4px;
                border: 1px solid;
            }
        """
        anyway_btn.setStyleSheet(_btn_base + """
            QPushButton { background: transparent; color: #CC4444; border-color: #CC4444; }
            QPushButton:hover { background: rgba(180,40,40,0.15); color: #FF5555; border-color: #FF5555; }
        """)
        html_btn.setStyleSheet(_btn_base + """
            QPushButton { background: transparent; color: #4A9F4A; border-color: #4A9F4A; }
            QPushButton:hover { background: rgba(40,140,40,0.15); color: #5ABF5A; border-color: #5ABF5A; }
        """)
        rtf_btn.setStyleSheet(_btn_base + """
            QPushButton { background: transparent; color: #4A9F4A; border-color: #4A9F4A; }
            QPushButton:hover { background: rgba(40,140,40,0.15); color: #5ABF5A; border-color: #5ABF5A; }
        """)

        msg.exec()
        clicked = msg.clickedButton()
        if clicked == anyway_btn:
            return 'anyway'
        elif clicked == html_btn:
            return 'html'
        elif clicked == rtf_btn:
            return 'rtf'
        return 'cancel'

    def save(self) -> bool:
        path = self.current_path()
        if not path:
            # No local file backs this tab. If it's a draft tab (opened
            # from the drafts panel, or stashed and never disk-saved),
            # the user pressing Ctrl+S means "save what I'm working on"
            # — i.e. re-stash with the same kind + d-tag, no dialogs.
            # Otherwise fall through to the standard "Save As" prompt.
            ed = self.current_editor()
            binding = getattr(ed, "_draft_binding", None) if ed else None
            if binding is not None and self._has_active_nostr_profile():
                if self._is_stash_in_flight_for(ed):
                    self._stash_already_running_message()
                    return True
                # On Ctrl+S we never silently fork: if the binding
                # belongs to a different profile, the user has to make
                # an intentional choice. We don't offer "save a copy"
                # here because Ctrl+S is meant to mean "save what I
                # have" — silently changing identity violates that.
                mismatch_pk = self._draft_binding_profile_mismatch(ed)
                if mismatch_pk is not None:
                    outcome = self._resolve_mismatch(ed, mismatch_pk, allow_fork=False)
                    if outcome == "cancel":
                        return False
                    if outcome == "switch":
                        if not self._switch_active_profile_to(mismatch_pk):
                            QMessageBox.warning(
                                self, "Profile unavailable",
                                "That profile is no longer connected. "
                                "Re-pair it from Nostr → Connect Signer to save here.",
                            )
                            return False
                self._restash_with_binding(ed, binding)
                return True
            return self.save_as()
        ed = self.current_editor()
        if ed and path.lower().endswith(('.txt', '.md')) and self._has_formatting(ed):
            result = self._warn_formatting_loss(os.path.splitext(path)[1])
            if result == 'cancel':
                return False
            elif result == 'html':
                return self.save_as(self._suggest_path(path, '.html'), ".html (*.html)")
            elif result == 'rtf':
                return self.save_as(self._suggest_path(path, '.rtf'), ".rtf (*.rtf)")
        return self._save_to(path)

    def _suggest_path(self, current_path: str, new_ext: str) -> str:
        """Return current_path with its extension replaced by new_ext."""
        return os.path.splitext(current_path)[0] + new_ext

    def save_as(self, initial_path: str = "", initial_filter: str = "") -> bool:
        path, selected_filter = QFileDialog.getSaveFileName(
            self, "Save As", initial_path,
            ".txt (*.txt);; .html (*.html);; .pdf (*.pdf);; .md (*.md);; .rtf (*.rtf)",
            initial_filter
        )
        if not path:
            return False

        # Auto-add extension if the user didn't type one
        ext_map = {
            ".txt":  ".txt",
            ".html": ".html",
            ".md":   ".md",
            ".rtf":  ".rtf",
            ".pdf":  ".pdf",
        }
        for keyword, ext in ext_map.items():
            if keyword in selected_filter and not any(path.lower().endswith(e) for e in ext_map.values()):
                path += ext
                break

        # Warn if saving to a format that loses formatting
        ed = self.current_editor()
        if ed and path.lower().endswith(('.txt', '.md')) and self._has_formatting(ed):
            result = self._warn_formatting_loss(os.path.splitext(path)[1])
            if result == 'cancel':
                return False
            elif result == 'html':
                return self.save_as(self._suggest_path(path, '.html'), ".html (*.html)")
            elif result == 'rtf':
                return self.save_as(self._suggest_path(path, '.rtf'), ".rtf (*.rtf)")

        old_path = self.current_path()
        ok = self._save_to(path)
        if ok:
            if old_path and old_path != path:
                self._watcher.removePath(old_path)
            self._watcher.addPath(path)
            self.set_current_path(path)
            ed = self.current_editor()
            if ed:
                self._attach_highlighter(ed, path)
                self._update_status_bar()
            if ed and hasattr(ed, '_backup'):
                ed._backup.update_file_path(path)
            add_recent(path)
            self._populate_recent_menu()
        return ok

    def _save_to(self, path: str) -> bool:
        ed = self.current_editor()
        if not ed:
            return False

        # Suppress the watcher trigger caused by our own write
        self._saving_paths.add(path)
        QTimer.singleShot(500, lambda: self._saving_paths.discard(path))

        ext = path.lower()
        try:
            if ext.endswith('.pdf'):
                return self._save_as_pdf(ed, path)
            elif ext.endswith('.rtf'):
                return self._save_as_rtf(ed, path)

            if ext.endswith(('.html', '.htm')):
                content = ed.toHtml()
            elif ext.endswith('.md') and getattr(ed, '_loaded_as_markdown', False):
                content = ed.document().toMarkdown()
            else:
                content = ed.toPlainText()

            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            ed.document().setModified(False)
            self._update_tab_title()
            self.status.showMessage(f"Saved: {path}")
            return True

        except Exception as e:
            QMessageBox.critical(self, "Error", f"File could not be saved:\n{e}")
            return False

    def _on_file_changed(self, path: str):
        if path in self._saving_paths:
            return

        # Re-add path: some OS implementations remove it after a change event
        if os.path.exists(path):
            self._watcher.addPath(path)

        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            ed = self._editor_from_widget(w)
            if ed and getattr(ed, '_file_path', None) == path:
                bar = self._bar_from_widget(w)
                if bar:
                    if os.path.exists(path):
                        bar.show_changed(ed.document().isModified())
                    else:
                        bar.show_deleted()
                break

    def _reload_from_disk(self, ed, bar: FileChangedBar):
        path = getattr(ed, '_file_path', None)
        if not path or not os.path.exists(path):
            return
        try:
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Could not reload file:\n{e}")
            return

        ext = path.lower()
        if ext.endswith(('.html', '.htm')):
            ed.setHtml(content)
        elif ext.endswith('.md'):
            ed.document().setMarkdown(content)
            ed._loaded_as_markdown = True
        else:
            ed.setPlainText(content)

        ed.document().setModified(False)
        bar.hide()
        self._update_tab_title()

    def _save_as_rtf(self, editor, path: str) -> bool:
        try:
            content = self._to_rtf(editor)
            with open(path, "w", encoding="ascii") as f:
                f.write(content)
            editor.document().setModified(False)
            self._update_tab_title()
            self.status.showMessage(f"Saved: {path}")
            return True
        except Exception as e:
            QMessageBox.critical(self, "Error", f"RTF could not be saved:\n{e}")
            return False

    def _to_rtf(self, editor) -> str:
        """Convert QTextDocument to an RTF string.

        Walks the document block by block, fragment by fragment.
        Each fragment becomes an RTF group carrying its formatting codes.
        Non-ASCII characters are escaped as \\uN? (RTF Unicode escape).
        """
        doc = editor.document()

        # --- Pass 1: collect all unique foreground colors used in the document ---
        colors: list[tuple[int, int, int]] = []
        block = doc.begin()
        while block.isValid():
            it = block.begin()
            while not it.atEnd():
                fmt = it.fragment().charFormat()
                if fmt.hasProperty(QTextCharFormat.ForegroundBrush):
                    c = fmt.foreground().color()
                    rgb = (c.red(), c.green(), c.blue())
                    if rgb not in colors:
                        colors.append(rgb)
                it += 1
            block = block.next()

        # --- RTF header ---
        # \colortbl: entry 0 is implicit default; custom colors start at index 1.
        color_table = "{\\colortbl;" + "".join(
            f"\\red{r}\\green{g}\\blue{b};" for r, g, b in colors
        ) + "}"

        parts = [
            "{\\rtf1\\ansi\\deff0\n",
            "{\\fonttbl{\\f0\\fmodern\\fcharset0 Courier New;}}\n",
            color_table + "\n",
            "\\f0\\fs28\n",  # Courier New 14 pt  (RTF uses half-points)
        ]

        # --- Pass 2: emit content ---
        block = doc.begin()
        first_block = True
        while block.isValid():
            if not first_block:
                parts.append("\\par\n")
            first_block = False

            it = block.begin()
            while not it.atEnd():
                frag = it.fragment()
                fmt = frag.charFormat()

                # Escape RTF special chars and non-ASCII
                escaped = []
                for ch in frag.text():
                    if ch == "\\":
                        escaped.append("\\\\")
                    elif ch == "{":
                        escaped.append("\\{")
                    elif ch == "}":
                        escaped.append("\\}")
                    elif ord(ch) > 127:
                        escaped.append(f"\\u{ord(ch)}?")
                    else:
                        escaped.append(ch)
                text = "".join(escaped)

                # Build format prefix (all codes go inside a single RTF group
                # so they reset automatically at the closing brace)
                codes = ""
                if fmt.fontWeight() > 400:
                    codes += "\\b "
                if fmt.fontItalic():
                    codes += "\\i "
                if fmt.fontUnderline():
                    codes += "\\ul "
                if fmt.hasProperty(QTextCharFormat.ForegroundBrush):
                    c = fmt.foreground().color()
                    idx = colors.index((c.red(), c.green(), c.blue())) + 1
                    codes += f"\\cf{idx} "

                parts.append(f"{{{codes}{text}}}" if codes else text)
                it += 1

            block = block.next()

        parts.append("\n}")
        return "".join(parts)

    def _save_as_pdf(self, editor, path: str) -> bool:
        try:
            from PySide6.QtPrintSupport import QPrinter
            from PySide6.QtGui import QFont

            printer = QPrinter(QPrinter.HighResolution)
            printer.setOutputFormat(QPrinter.PdfFormat)
            printer.setOutputFileName(path)
            printer.setPageSize(QPageSize(QPageSize.A4))
            printer.setPageMargins(QMarginsF(10, 10, 10, 10), QPageLayout.Millimeter)

            # Clone document and force white background + black default text for print.
            # Custom text colors (red, blue, etc.) are mid-range and print well on white.
            doc = editor.document().clone()
            frame_fmt = doc.rootFrame().frameFormat()
            frame_fmt.setBackground(QColor("white"))
            doc.rootFrame().setFrameFormat(frame_fmt)
            doc.setDefaultStyleSheet("body { color: #000000; background: #ffffff; }")

            # Use point size (device-independent) so text scales correctly at 1200 DPI.
            # The editor uses "font-size: 14px" (screen pixels) which would appear tiny
            # at printer resolution without this conversion. 11pt ≈ standard document size.
            font = QFont(MONO_FONT, 11)
            doc.setDefaultFont(font)

            doc.print_(printer)

            editor.document().setModified(False)
            self._update_tab_title()
            self.status.showMessage(f"Saved: {path}")
            return True

        except Exception as e:
            QMessageBox.critical(self, "Error", f"PDF could not be saved:\n{e}")
            return False


    # ----------------------------------------------------------------------
    # SEARCH
    # ----------------------------------------------------------------------
    def _toggle_findbar(self):
        vis = not self.findbar.isVisible()
        self.findbar.setVisible(vis)
        if vis:
            self.findbar.focusIn()
            self._on_search_text_changed()
        else:
            self.findbar.set_match_info("")
            self._clear_search_highlights()
            ed = self.current_editor()
            if ed:
                cursor = ed.textCursor()
                cursor.setPosition(cursor.selectionStart())
                ed.setTextCursor(cursor)
                ed.setFocus()

    def _show_shortcuts(self):
        """Open the cheat-sheet style ``ShortcutsDialog``.

        Replaces the previous plain-text ``QMessageBox`` so we get a
        searchable, themed, category-grouped surface in keeping with
        how major desktop apps display their keyboard reference.
        """
        from shortcuts_dialog import ShortcutsDialog
        dlg = ShortcutsDialog(is_dark=self.is_dark_theme, parent=self)
        dlg.exec()

    def _show_about(self):
        QMessageBox.about(
            self,
            f"About {APP_DISPLAY_NAME}",
            f"<h3>{APP_DISPLAY_NAME}</h3>"
            f"<p>Version {APP_VERSION}</p>"
            "<p>A minimal distraction-free text editor.</p>"
            f'<p><a href="{APP_URL}">{APP_URL}</a></p>',
        )

    # ----------------------------------------------------------------------
    # UPDATE CHECK
    # ----------------------------------------------------------------------
    _UPDATE_CHECK_INTERVAL = 24 * 60 * 60  # seconds

    def _maybe_auto_check_for_updates(self):
        """Silent startup check, throttled to once every 24 hours."""
        settings = load_settings()
        last_check = settings.get("last_update_check", 0)
        if time.time() - last_check < self._UPDATE_CHECK_INTERVAL:
            return
        self._auto_update_checker = UpdateChecker(self)
        self._auto_update_checker.update_available.connect(self._on_auto_update_available)
        self._auto_update_checker.check()
        save_setting("last_update_check", time.time())

    def _on_auto_update_available(self, info):
        if load_settings().get("skipped_version") == info.version:
            return
        self._pending_release = info
        self.update_bar.show_update(info.version)

    def _on_update_bar_download(self):
        info = getattr(self, "_pending_release", None)
        if info is not None:
            self._start_update_or_open(info)

    def _on_update_bar_dismissed(self):
        info = getattr(self, "_pending_release", None)
        if info is not None:
            save_setting("skipped_version", info.version)

    def _start_update_or_open(self, info):
        """Update in place when this install supports it, otherwise open the
        release page in the browser (macOS, the .deb, and source runs)."""
        kind = detect_install_kind()
        asset = select_asset(kind, info.assets)
        if supports_in_app_update(kind) and asset is not None:
            self._run_in_app_update(kind, asset)
        else:
            QDesktopServices.openUrl(QUrl(info.page_url))

    def _run_in_app_update(self, kind, asset):
        self._update_installer = UpdateInstaller(kind, self)
        dlg = QProgressDialog("Downloading update…", "Cancel", 0, 100, self)
        dlg.setWindowTitle("Updating MyEditor")
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        self._update_progress = dlg

        self._update_installer.progress.connect(self._on_update_progress)
        self._update_installer.ready.connect(self._on_update_downloaded)
        self._update_installer.failed.connect(self._on_update_failed)
        dlg.canceled.connect(self._cancel_update)

        dlg.setValue(0)
        self._update_installer.start(asset)

    def _cancel_update(self):
        inst = getattr(self, "_update_installer", None)
        if inst is not None:
            inst.cancel()
        dlg = getattr(self, "_update_progress", None)
        if dlg is not None:
            dlg.close()
        # The update bar stays visible so the user can retry.

    def _on_update_progress(self, percent):
        dlg = getattr(self, "_update_progress", None)
        if dlg is not None and percent >= 0:
            dlg.setValue(percent)

    def _on_update_downloaded(self, path):
        dlg = getattr(self, "_update_progress", None)
        if dlg is not None:
            dlg.close()
        if not self._confirm_save_before_update():
            self._cleanup_update_file(path)
            return
        try:
            self._update_installer.apply(path)
        except Exception as e:
            self._cleanup_update_file(path)
            QMessageBox.critical(self, "Update Failed", f"Could not start the update.\n\n{e}")
            return
        # The installer (Windows) or the swap helper (AppImage) is now running
        # and will relaunch MyEditor; close so it can replace our files.
        self._closing_for_update = True
        self.close()

    def _on_update_failed(self, message):
        dlg = getattr(self, "_update_progress", None)
        if dlg is not None:
            dlg.close()
        info = getattr(self, "_pending_release", None)
        text = f"Could not download the update.\n\n{message}"
        if info is not None:
            r = QMessageBox.question(
                self, "Update Failed",
                text + "\n\nOpen the download page instead?",
                QMessageBox.Yes | QMessageBox.No)
            if r == QMessageBox.Yes:
                QDesktopServices.openUrl(QUrl(info.page_url))
        else:
            QMessageBox.warning(self, "Update Failed", text)

    def _confirm_save_before_update(self):
        """Handle unsaved work before the app relaunches for an update. Returns
        True if it is safe to proceed, False if the user cancelled."""
        has_unsaved = False
        for i in range(self.tabs.count()):
            ed = self._editor_from_widget(self.tabs.widget(i))
            if ed and ed.document().isModified():
                has_unsaved = True
                break
        if not has_unsaved:
            return True
        r = QMessageBox.question(
            self, "Save Changes?",
            "MyEditor will close to install the update.\n\nSave your changes first?",
            QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel, QMessageBox.Yes)
        if r == QMessageBox.Cancel:
            return False
        if r == QMessageBox.Yes:
            for i in range(self.tabs.count()):
                ed = self._editor_from_widget(self.tabs.widget(i))
                if ed and ed.document().isModified():
                    self.tabs.setCurrentIndex(i)
                    if not self.save():
                        return False
        return True

    def _cleanup_update_file(self, path):
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except OSError:
            pass

    def _check_for_updates_manual(self):
        """Help > Check for Updates - always checks, reports via QMessageBox."""
        self._manual_update_checker = UpdateChecker(self)
        self._manual_update_checker.update_available.connect(self._on_manual_update_available)
        self._manual_update_checker.up_to_date.connect(self._on_manual_up_to_date)
        self._manual_update_checker.failed.connect(self._on_manual_update_failed)
        self._manual_update_checker.check()
        save_setting("last_update_check", time.time())

    def _on_manual_update_available(self, info):
        self._pending_release = info
        kind = detect_install_kind()
        asset = select_asset(kind, info.assets)
        msg = QMessageBox(self)
        msg.setWindowTitle("Update Available")
        msg.setText(f"Version {info.version} is available.")
        if supports_in_app_update(kind) and asset is not None:
            msg.setInformativeText("Download and install it now?")
            action_btn = msg.addButton("Update Now", QMessageBox.ButtonRole.AcceptRole)
            msg.addButton(QMessageBox.StandardButton.Cancel)
            msg.exec()
            if msg.clickedButton() == action_btn:
                self._run_in_app_update(kind, asset)
        else:
            action_btn = msg.addButton("Open Release Page", QMessageBox.ButtonRole.AcceptRole)
            msg.addButton(QMessageBox.StandardButton.Close)
            msg.exec()
            if msg.clickedButton() == action_btn:
                QDesktopServices.openUrl(QUrl(info.page_url))

    def _on_manual_up_to_date(self):
        QMessageBox.information(self, "Check for Updates", "You are up to date.")

    def _on_manual_update_failed(self, error: str):
        QMessageBox.warning(self, "Check for Updates", "Could not reach GitHub to check for updates.")

    def _on_search_text_changed(self):
        needle = self.findbar.text()
        if needle != self._last_search_text:
            self._update_search_matches(needle)
            self._last_search_text = needle
        if needle:
            self._highlight_all_matches()
            self._update_match_display()
        else:
            self._clear_search_highlights()
            self.findbar.set_match_info("")

    def _update_search_matches(self, needle):
        ed = self.current_editor()
        if not ed:
            return
        self._search_matches = []
        if not needle:
            self._current_match_index = -1
            return
        cursor = QTextCursor(ed.document())
        cursor.movePosition(QTextCursor.Start)
        while True:
            cursor = ed.document().find(needle, cursor, QTextDocument.FindFlags())
            if cursor.isNull():
                break
            self._search_matches.append((cursor.selectionStart(), cursor.selectionEnd()))
        self._current_match_index = -1

    def _highlight_all_matches(self):
        ed = self.current_editor()
        if not ed:
            return
        if not self._search_matches:
            ed.setExtraSelections([])
            return

        highlight_fmt = QTextCharFormat()
        highlight_fmt.setBackground(QColor("#FFD700"))
        highlight_fmt.setForeground(QColor("#000000"))

        selections = []
        for start_pos, end_pos in self._search_matches:
            cursor = QTextCursor(ed.document())
            cursor.setPosition(start_pos)
            cursor.setPosition(end_pos, QTextCursor.KeepAnchor)
            sel = QTextEdit.ExtraSelection()
            sel.cursor = cursor
            sel.format = highlight_fmt
            selections.append(sel)

        if 0 <= self._current_match_index < len(self._search_matches):
            start_pos, end_pos = self._search_matches[self._current_match_index]
            cursor = QTextCursor(ed.document())
            cursor.setPosition(start_pos)
            cursor.setPosition(end_pos, QTextCursor.KeepAnchor)
            cur_sel = QTextEdit.ExtraSelection()
            cur_sel.cursor = cursor
            cur_fmt = QTextCharFormat()
            if self.is_dark_theme:
                cur_fmt.setBackground(QColor("#264F78"))
                cur_fmt.setForeground(QColor("#FFFFFF"))
            else:
                cur_fmt.setBackground(QColor("#0078D4"))
                cur_fmt.setForeground(QColor("#FFFFFF"))
            cur_sel.format = cur_fmt
            for i, sel in enumerate(selections):
                if sel.cursor.selectionStart() == start_pos:
                    selections[i] = cur_sel
                    break

        ed.setExtraSelections(selections)
        self._search_extra_selections = selections

    def _clear_search_highlights(self):
        ed = self.current_editor()
        if ed:
            ed.setExtraSelections([])
            self._search_extra_selections = []

    def _find_once(self, forward=True):
        ed = self.current_editor()
        if not ed:
            return
        needle = self.findbar.text()
        if not needle:
            return
        if needle != self._last_search_text:
            self._update_search_matches(needle)
            self._last_search_text = needle
        if not self._search_matches:
            self.findbar.set_match_info("No matches")
            return

        wrapped = False
        total = len(self._search_matches)
        if forward:
            if self._current_match_index == -1:
                self._current_match_index = 0
            else:
                next_idx = (self._current_match_index + 1) % total
                wrapped = (next_idx == 0)
                self._current_match_index = next_idx
        else:
            if self._current_match_index == -1:
                self._current_match_index = total - 1
            else:
                prev_idx = (self._current_match_index - 1) % total
                wrapped = (prev_idx == total - 1)
                self._current_match_index = prev_idx

        if 0 <= self._current_match_index < len(self._search_matches):
            start_pos, end_pos = self._search_matches[self._current_match_index]
            cursor = QTextCursor(ed.document())
            cursor.setPosition(start_pos)
            cursor.setPosition(end_pos, QTextCursor.KeepAnchor)
            ed.setTextCursor(cursor)
            ed.ensureCursorVisible()

        self._highlight_all_matches()
        self._update_match_display()
        if wrapped:
            self.findbar.set_match_info("Wrapped · " + self.findbar.match_info.text())
            QTimer.singleShot(1200, self._update_match_display)

    def _update_match_display(self):
        if not self._search_matches:
            self.findbar.set_match_info("No matches")
            return
        total = len(self._search_matches)
        current = self._current_match_index + 1
        self.findbar.set_match_info(f"{current} of {total}")

    def _find_next(self):
        self._find_once(True)

    def _find_prev(self):
        self._find_once(False)


    # ----------------------------------------------------------------------
    # FORMAT HANDLERS
    # ----------------------------------------------------------------------
    def _fmt_bold(self):
        ed = self.current_editor()
        if ed:
            ed.toggle_bold()

    def _fmt_italic(self):
        ed = self.current_editor()
        if ed:
            ed.toggle_italic()

    def _fmt_underline(self):
        ed = self.current_editor()
        if ed:
            ed.toggle_underline()

    def _reset_format(self):
        ed = self.current_editor()
        if ed:
            ed.reset_to_default()

    def _undo(self):
        ed = self.current_editor()
        if ed:
            ed.undo()

    def _redo(self):
        ed = self.current_editor()
        if ed:
            ed.redo()


    # ----------------------------------------------------------------------
    # THEME TOGGLE
    # ----------------------------------------------------------------------
    def _set_theme(self, is_dark: bool, announce: bool = True):
        """Apply is_dark to every theme-aware widget in the window.

        Shared by the manual toggle (checkbox / Ctrl+Shift+T) and the
        OS color-scheme-follow path, so the fan-out logic lives in one
        place instead of being duplicated.
        """
        self.is_dark_theme = is_dark
        if hasattr(self, 'header_widget'):
            self.header_widget.theme_checkbox.blockSignals(True)
            self.header_widget.theme_checkbox.setChecked(self.is_dark_theme)
            self.header_widget.theme_checkbox.blockSignals(False)
        self._apply_theme()
        if hasattr(self, 'findbar'):
            self.findbar.is_dark = self.is_dark_theme
            self.findbar._update_theme()
        if hasattr(self, 'header_widget'):
            self.header_widget.update_theme(self.is_dark_theme)
        if hasattr(self, 'update_bar'):
            self.update_bar.update_theme(self.is_dark_theme)
        for i in range(self.tabs.count()):
            bar = self._bar_from_widget(self.tabs.widget(i))
            if bar:
                bar.update_theme(self.is_dark_theme)
        # The drafts panel and any mounted conflict banners each carry
        # their own dark/light stylesheet pair. Update them in lockstep
        # with the rest of the window so a theme switch doesn't leave
        # half the editor on the old palette.
        if getattr(self, "_drafts_panel", None) is not None:
            self._drafts_panel.apply_theme(self.is_dark_theme)
        for banner in getattr(self, "_tab_conflict_banners", {}).values():
            banner.apply_theme(self.is_dark_theme)
        if announce:
            mode = "Dark" if self.is_dark_theme else "Light"
            self.status.showMessage(f"Switched to {mode} theme", 2000)

    def _toggle_theme(self):
        if hasattr(self, 'header_widget') and self.header_widget.theme_checkbox.isChecked() != self.is_dark_theme:
            target = self.header_widget.theme_checkbox.isChecked()
        else:
            target = not self.is_dark_theme
        self._set_theme(target)
        # The user made an explicit choice - stop following the OS scheme
        # and remember this choice across restarts.
        self._follow_os_theme = False
        save_setting("theme", "dark" if target else "light")

    def _on_os_color_scheme_changed(self, scheme):
        if not self._follow_os_theme:
            return
        self._set_theme(scheme != Qt.ColorScheme.Light)


    # ----------------------------------------------------------------------
    # LINE NUMBERS
    # ----------------------------------------------------------------------
    def _toggle_line_numbers(self):
        if hasattr(self, 'header_widget') and self.header_widget.line_numbers_checkbox.isChecked() != self.show_line_numbers:
            self.show_line_numbers = self.header_widget.line_numbers_checkbox.isChecked()
        else:
            self.show_line_numbers = not self.show_line_numbers
        if hasattr(self, 'header_widget'):
            self.header_widget.line_numbers_checkbox.blockSignals(True)
            self.header_widget.line_numbers_checkbox.setChecked(self.show_line_numbers)
            self.header_widget.line_numbers_checkbox.blockSignals(False)
        self.act_toggle_line_numbers.setChecked(self.show_line_numbers)

        for i in range(self.tabs.count()):
            container = self.tabs.widget(i)
            if isinstance(container, QWidget):
                editor = self._editor_from_widget(container)
                if editor:
                    if self.show_line_numbers and not hasattr(editor, '_line_gutter'):
                        gutter = LineNumberGutter(editor)
                        editor.parent().layout().insertWidget(0, gutter)
                        editor._line_gutter = gutter
                    elif not self.show_line_numbers and hasattr(editor, '_line_gutter'):
                        editor.parent().layout().removeWidget(editor._line_gutter)
                        editor._line_gutter.deleteLater()
                        delattr(editor, '_line_gutter')

        self.status.showMessage(f"Line numbers {'enabled' if self.show_line_numbers else 'disabled'}", 2000)


    # ----------------------------------------------------------------------
    # SYNTAX HIGHLIGHTING
    # ----------------------------------------------------------------------
    def _toggle_syntax_highlighting(self):
        if hasattr(self, 'header_widget') and self.header_widget.syntax_highlight_checkbox.isChecked() != self.syntax_highlighting:
            self.syntax_highlighting = self.header_widget.syntax_highlight_checkbox.isChecked()
        else:
            self.syntax_highlighting = not self.syntax_highlighting

        self.act_toggle_syntax_hl.setChecked(self.syntax_highlighting)
        self.header_widget.syntax_highlight_checkbox.blockSignals(True)
        self.header_widget.syntax_highlight_checkbox.setChecked(self.syntax_highlighting)
        self.header_widget.syntax_highlight_checkbox.blockSignals(False)

        for i in range(self.tabs.count()):
            container = self.tabs.widget(i)
            if isinstance(container, QWidget):
                editor = self._editor_from_widget(container)
                if editor:
                    if self.syntax_highlighting:
                        path = getattr(editor, '_file_path', None)
                        self._attach_highlighter(editor, path)
                    elif hasattr(editor, '_highlighter'):
                        editor._highlighter.setDocument(None)
                        del editor._highlighter

        self.status.showMessage(f"Syntax highlighting {'enabled' if self.syntax_highlighting else 'disabled'}", 2000)


    # ----------------------------------------------------------------------
    # VIEW MENU (background pattern, current-line highlight, paper mode)
    # ----------------------------------------------------------------------
    def _set_background_pattern(self, name):
        self.editor_background = name
        save_setting("editor_background", name)
        for i in range(self.tabs.count()):
            ed = self._editor_from_widget(self.tabs.widget(i))
            if ed:
                ed.set_background_pattern(name)
        self.status.showMessage(f"Background: {name.capitalize()}", 2000)

    def _toggle_paper_mode(self, on):
        on = bool(on)
        self.paper_mode = on
        save_setting("paper_mode", on)
        if hasattr(self, "act_paper_mode") and self.act_paper_mode.isChecked() != on:
            self.act_paper_mode.blockSignals(True)
            self.act_paper_mode.setChecked(on)
            self.act_paper_mode.blockSignals(False)
        if hasattr(self, "_paper_btn") and self._paper_btn.isChecked() != on:
            self._paper_btn.blockSignals(True)
            self._paper_btn.setChecked(on)
            self._paper_btn.blockSignals(False)
        for i in range(self.tabs.count()):
            ed = self._editor_from_widget(self.tabs.widget(i))
            if ed:
                ed.set_paper_mode(on)
        self.status.showMessage(f"Paper mode {'on' if on else 'off'}", 2000)

    def _toggle_highlight_line(self, on):
        on = bool(on)
        self.highlight_current_line = on
        save_setting("highlight_current_line", on)
        for i in range(self.tabs.count()):
            ed = self._editor_from_widget(self.tabs.widget(i))
            if ed:
                ed.set_highlight_current_line(on)
        self.status.showMessage(f"Highlight current line {'on' if on else 'off'}", 2000)


    # ----------------------------------------------------------------------
    # CLOSE EVENT
    # ----------------------------------------------------------------------
    _SESSION_FILE = os.path.join(os.path.expanduser("~"), ".cache", "my_editor", "session.json")

    def _save_session(self):
        paths = []
        for i in range(self.tabs.count()):
            ed = self._editor_from_widget(self.tabs.widget(i))
            if ed and getattr(ed, '_file_path', None):
                paths.append(ed._file_path)
        if not paths:
            return
        data = {"paths": paths, "active": self.tabs.currentIndex()}
        os.makedirs(os.path.dirname(self._SESSION_FILE), exist_ok=True)
        with open(self._SESSION_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)

    def _restore_session(self) -> bool:
        if not os.path.isfile(self._SESSION_FILE):
            return False
        try:
            with open(self._SESSION_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return False
        os.remove(self._SESSION_FILE)
        paths = [p for p in data.get("paths", []) if os.path.isfile(p)]
        if not paths:
            return False
        missing = len(data.get("paths", [])) - len(paths)
        for path in paths:
            self.open_path(path)
        active = data.get("active", 0)
        if 0 <= active < self.tabs.count():
            self.tabs.setCurrentIndex(active)
        if missing:
            self.status.showMessage(f"{missing} file(s) from last session could not be found.", 5000)
        return True

    def closeEvent(self, event):
        # An update relaunch already handled unsaved work, so skip the prompt.
        if not getattr(self, "_closing_for_update", False):
            for i in range(self.tabs.count()):
                ed = self._editor_from_widget(self.tabs.widget(i))
                if ed and ed.document().isModified():
                    r = QMessageBox.question(self, "Save Changes?",
                                             "There are unsaved changes. Close everything now?",
                                             QMessageBox.Yes | QMessageBox.No)
                    if r != QMessageBox.Yes:
                        event.ignore()
                        return
                    else:
                        break
        self._save_session()
        for i in range(self.tabs.count()):
            ed = self._editor_from_widget(self.tabs.widget(i))
            if ed and hasattr(ed, '_backup'):
                ed._backup.delete()
        # Close any warm relay sockets and bunker channels so the WebSocket
        # layer can flush close frames before the QApplication tears down.
        if hasattr(self, "_session_pool"):
            self._session_pool.close_all()
        if hasattr(self, "_relay_pool"):
            self._relay_pool.close_all()
        event.accept()

    # ----------------------------------------------------------------------
    # NOSTR — profile chip menu + connect flow
    # ----------------------------------------------------------------------

    def _update_profile_chip(self):
        """Re-render the chip icon from the current default profile.

        If we have a cached avatar pixmap for the active profile, it's
        passed in; otherwise the chip falls back to initials on a
        deterministic color disc.
        """
        chip = self.header_widget.profile_chip
        default = self._profile_store.default()
        if default is None:
            chip.set_disconnected()
            return
        chip.set_profile(
            display_name=default.display_name,
            user_pubkey_hex=default.user_pubkey,
            avatar_pixmap=self._avatars.get(default.user_pubkey),
        )

    def _refresh_profile_chip_menu(self):
        """Rebuild the chip's dropdown — fast and idempotent."""
        menu = self.header_widget.profile_chip.menu()
        menu.clear()

        profiles = self._profile_store.list()
        if not profiles:
            act = menu.addAction("Connect Nostr signer…")
            act.triggered.connect(self._on_nostr_connect)
            return

        active = self._profile_store.default()
        for profile in profiles:
            label = profile.display_name or profile.npub_short()
            act = menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(profile is active)
            act.triggered.connect(
                lambda _checked=False, p=profile: self._on_nostr_select_profile(p)
            )

        menu.addSeparator()
        act_add = menu.addAction("Add profile…")
        act_add.triggered.connect(self._on_nostr_connect)
        act_signout = menu.addAction("Sign out active profile")
        act_signout.triggered.connect(self._on_nostr_sign_out)

    def _on_nostr_connect(self):
        dialog = ConnectDialog(
            self._relay_pool,
            self._profile_store,
            parent=self,
            is_dark=self.is_dark_theme,
        )
        dialog.profile_connected.connect(self._on_nostr_profile_connected)
        dialog.exec()

    def _on_nostr_profile_connected(self, profile: Profile):
        # New (or re-connected) profile becomes the active one.
        self._profile_store.set_default(profile.user_pubkey)
        self._update_profile_chip()
        self._refresh_profile_chip_menu()
        self.status.showMessage(
            f"Connected as {profile.display_name or profile.npub_short()}", 5000
        )
        # Kick off metadata + avatar fetch in the background. The chip will
        # refresh itself when the fetcher signals back.
        self._metadata_fetcher.fetch(profile)
        # Also prime the mentions cache from this profile's NIP-02 contact list.
        self._contact_fetcher.fetch(profile.user_pubkey, profile.bunker_relays)
        # Bind the draft pipeline to the new profile so the panel
        # (visible or not) starts collecting wraps from the relays.
        self._draft_sync.start_for(profile)
        if self._drafts_panel is not None:
            self._drafts_panel.set_active_profile(profile)
            self._drafts_panel.set_signer_unsupported(False)

    def _on_nostr_select_profile(self, profile: Profile):
        self._profile_store.set_default(profile.user_pubkey)
        self._update_profile_chip()
        self._refresh_profile_chip_menu()
        # Switching identities — re-point the draft sync at the new
        # profile. ``DraftSync.start_for`` is idempotent if the same
        # profile is already active.
        self._draft_sync.start_for(profile)
        if self._drafts_panel is not None:
            self._drafts_panel.set_active_profile(profile)
            self._drafts_panel.set_signer_unsupported(False)

    def _on_nostr_sign_out(self):
        active = self._profile_store.default()
        if active is None:
            return
        confirm = QMessageBox.question(
            self,
            "Sign out?",
            (
                f"Remove the profile for {active.display_name or active.npub_short()}?\n\n"
                "Your real key stays in your signer. Only the local connection is forgotten."
            ),
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        self._session_pool.drop(active.user_pubkey)
        self._avatars.pop(active.user_pubkey, None)
        self._profile_store.remove(active.user_pubkey)
        self._update_profile_chip()
        self._refresh_profile_chip_menu()
        # Tear down the draft pipeline. If another profile remains, the
        # caller (or chip menu) can re-bind to it; otherwise the panel
        # falls back to its "Connect a Nostr profile" empty state.
        self._draft_sync.stop()
        remaining = self._profile_store.default()
        if self._drafts_panel is not None:
            self._drafts_panel.set_active_profile(remaining)
            self._drafts_panel.set_signer_unsupported(False)
        if remaining is not None:
            self._draft_sync.start_for(remaining)

    # -- metadata / avatar updates ----------------------------------------

    def _on_metadata_updated(self, profile: Profile):
        """Refreshed display name / picture URL landed — repaint the chip
        and queue the avatar download if one is available."""
        self._update_profile_chip()
        self._refresh_profile_chip_menu()
        if profile.picture:
            self._avatar_batcher.request(profile.user_pubkey, profile.picture)

    def _on_avatar_added(self, pubkey_hex: str, _pixmap: QPixmap):
        """A new pixmap landed in the store. Refresh the header chip only
        if it's for the currently-active profile; the picker/chip-row
        widgets subscribe to ``avatar_added`` themselves and don't need
        this signal."""
        active = self._profile_store.default()
        if active is not None and active.user_pubkey == pubkey_hex:
            self._update_profile_chip()

    def _on_person_updated(self, person: Person):
        """A kind 0 just resolved for someone in the contact list. Queue
        their avatar so the picker has it ready by the time the user
        searches for them."""
        if person.picture:
            self._avatar_batcher.request(person.pubkey, person.picture)

    def _on_search_results(self, _query: str, people: list):
        """NIP-50 search returned matches — queue their avatars too."""
        for person in people:
            if isinstance(person, Person) and person.picture:
                self._avatar_batcher.request(person.pubkey, person.picture)

    # -- publish ----------------------------------------------------------

    def _on_nostr_publish_note(self):
        active = self._profile_store.default()
        if active is None:
            QMessageBox.information(
                self,
                "Connect a signer first",
                "Connect a Nostr signer before publishing. Use the avatar "
                "chip in the header or Nostr → Connect Signer…",
            )
            return

        ed = self.current_editor()
        content = ed.toPlainText().strip() if ed is not None else ""
        if not content:
            QMessageBox.information(
                self,
                "Nothing to publish",
                "The current document is empty.",
            )
            return

        dialog = PublishNoteDialog(
            content=content,
            active_profile=active,
            store=self._profile_store,
            relay_pool=self._relay_pool,
            relay_list_cache=self._relay_list_cache,
            session_pool=self._session_pool,
            known_people=self._known_people,
            search_client=self._search_client,
            avatars=self._avatars,
            parent=self,
            is_dark=self.is_dark_theme,
        )
        dialog.published.connect(self._on_nostr_note_published)
        dialog.exec()

    def _on_nostr_note_published(self, event_id_hex: str, results):
        accepted = sum(1 for _, ok, _ in results if ok)
        note_id = encode_note(event_id_hex) if event_id_hex else ""
        if note_id:
            msg = (
                f"Published to Nostr: {accepted}/{len(results)} relays · "
                f"{note_id[:16]}…"
            )
        else:
            msg = f"Published to Nostr: {accepted}/{len(results)} relays"
        self.status.showMessage(msg, 8000)

    def _on_nostr_publish_article(self):
        active = self._profile_store.default()
        if active is None:
            QMessageBox.information(
                self,
                "Connect a signer first",
                "Connect a Nostr signer before publishing. Use the avatar "
                "chip in the header or Nostr → Connect Signer…",
            )
            return

        ed = self.current_editor()
        body = ed.toPlainText().rstrip() if ed is not None else ""
        if not body:
            QMessageBox.information(
                self,
                "Nothing to publish",
                "The current document is empty.",
            )
            return

        # Pre-fill metadata. Precedence:
        #   1. Draft binding (so a published article inherits the exact
        #      ``d`` and title from its draft, preserving the addressable
        #      coordinate so re-publishing replaces the draft in place).
        #   2. File path basename for disk-backed tabs.
        #   3. First non-blank line as a heuristic title for free-form tabs.
        binding = getattr(ed, "_draft_binding", None) if ed else None
        default_title = ""
        default_slug = ""
        if binding is not None and binding.inner_kind == INNER_KIND_LONG_FORM:
            default_title = binding.title
            default_slug = binding.identifier
        if not default_title:
            first_line = next((ln for ln in body.splitlines() if ln.strip()), "")
            default_title = first_line.lstrip("# ").strip()
        if not default_slug:
            path = self.current_path()
            default_slug = (
                os.path.splitext(os.path.basename(path))[0] if path else ""
            )

        dialog = PublishArticleDialog(
            body_markdown=body,
            active_profile=active,
            store=self._profile_store,
            relay_pool=self._relay_pool,
            relay_list_cache=self._relay_list_cache,
            session_pool=self._session_pool,
            known_people=self._known_people,
            search_client=self._search_client,
            avatars=self._avatars,
            media_store=self._media_store,
            default_title=default_title,
            default_slug=default_slug,
            parent=self,
            is_dark=self.is_dark_theme,
        )
        dialog.published.connect(self._on_nostr_article_published)
        dialog.exec()

    def _on_nostr_article_published(self, naddr: str, results):
        accepted = sum(1 for _, ok, _ in results if ok)
        if naddr:
            msg = (
                f"Published article to Nostr: {accepted}/{len(results)} relays · "
                f"{naddr[:18]}…"
            )
        else:
            msg = f"Published article to Nostr: {accepted}/{len(results)} relays"
        self.status.showMessage(msg, 8000)

    # -- media (Blossom) --------------------------------------------------

    def _on_nostr_media_library(self):
        """Open the Media Library dialog. Non-modal so the user can keep
        editing while uploads run in the background."""
        active = self._profile_store.default()
        if active is None:
            QMessageBox.information(
                self,
                "Connect a signer first",
                "Connect a Nostr signer (Nostr → Connect Signer…) before "
                "browsing your Blossom media library.",
            )
            return
        dialog = MediaLibraryDialog(
            store=self._media_store,
            is_dark=self.is_dark_theme,
            pick_mode=False,
            parent=self,
        )
        dialog.show()

    def _on_nostr_insert_image(self):
        """Open the Media Library in picker mode, restricted to images.
        On selection, materialize the blob to the local cache and insert
        it at the current cursor in the active editor."""
        active = self._profile_store.default()
        if active is None:
            QMessageBox.information(
                self,
                "Connect a signer first",
                "Connect a Nostr signer (Nostr → Connect Signer…) before "
                "inserting Blossom images.",
            )
            return
        ed = self.current_editor()
        if ed is None:
            self.new_tab()
            ed = self.current_editor()
        if ed is None:
            return
        dialog = MediaLibraryDialog(
            store=self._media_store,
            is_dark=self.is_dark_theme,
            pick_mode=True,
            parent=self,
        )
        # Pre-select images in the picker — videos / audio can't be
        # inserted as inline document objects.
        dialog._filter_combo.setCurrentIndex(1)
        dialog.file_picked.connect(
            lambda media, alt, e=ed: self._insert_media_at_cursor(media, e, alt)
        )
        dialog.exec()

    def _insert_media_at_cursor(self, media: MediaFile, editor, alt_text: str = "") -> None:
        """Resolve a media file and insert it at the editor's cursor.

        Three branches:
          - non-image  → insert the URL as plain text
          - markdown   → ``![alt](url)`` (no fetch needed, survives save)
          - rich text  → fetch into the cache, then insertImage(local path)
        """
        if not (media.mime_type or "").startswith("image/"):
            self.status.showMessage(
                f"Inserted URL only (non-image): {media.url}", 5000
            )
            cursor = editor.textCursor()
            cursor.insertText(media.url)
            return

        if getattr(editor, "_loaded_as_markdown", False):
            self._do_insert_image(editor, "", media.url, alt_text)
            return

        cache_path = self._media_image_loader.cache_path(media.hash)
        if cache_path.is_file():
            self._do_insert_image(editor, str(cache_path), media.url, alt_text)
            return
        # Queue and trigger an async load. ``ready`` will fire with the
        # local path so we can complete the insert.
        self._pending_image_inserts[media.hash] = (id(editor), media.url, alt_text)
        self._media_image_loader.load(media.hash, media.url)
        self.status.showMessage(f"Fetching {media.url}…", 5000)

    def _on_media_image_ready(self, sha: str, local_path: str, _pixmap) -> None:
        pending = self._pending_image_inserts.pop(sha, None)
        if pending is None:
            return
        editor_id, original_url, alt_text = pending
        editor = self._editor_by_id(editor_id)
        if editor is None:
            return
        self._do_insert_image(editor, local_path, original_url, alt_text)

    def _on_media_image_failed(self, sha: str, reason: str) -> None:
        """Loader failed for a hash we wanted to insert — drop the
        pending entry and surface a status message so the queue doesn't
        leak and the user sees why nothing was inserted."""
        if self._pending_image_inserts.pop(sha, None) is None:
            return
        self.status.showMessage(f"Image fetch failed: {reason}", 5000)

    def _do_insert_image(self, editor, local_path: str, source_url: str, alt_text: str = "") -> None:
        """Insert an image at the editor's current cursor.

        Markdown-loaded tabs (``_loaded_as_markdown``) get
        ``![alt](url)`` so the image survives a save round-trip. Rich-
        text tabs get an embedded ``QTextImageFormat`` pointing at the
        local cache file; the alt text rides along as the image's
        tooltip (the closest QTextDocument equivalent of alt).
        """
        is_md = getattr(editor, "_loaded_as_markdown", False)
        cursor = editor.textCursor()
        if is_md:
            cursor.insertText(f"![{alt_text}]({source_url})")
        else:
            cursor.insertImage(local_path)
        editor.setTextCursor(cursor)
        suffix = f" · alt: {alt_text}" if alt_text else ""
        self.status.showMessage(f"Inserted image · {source_url}{suffix}", 5000)

    def _editor_by_id(self, editor_id: int):
        for i in range(self.tabs.count()):
            ed = self._editor_from_widget(self.tabs.widget(i))
            if ed is not None and id(ed) == editor_id:
                return ed
        # Fall back to the current editor if the original tab is gone.
        return self.current_editor()

    def _on_upload_finished_for_insert(self, name: str, media: MediaFile) -> None:
        """When a dragged or pasted image finishes uploading, auto-insert
        it into the editor the user originated the drop / paste on.

        Uploads NOT initiated by drop/paste (e.g. the Library dialog's
        Upload button) won't be in the pending dict, so this is a no-op
        for those — no UI surprise."""
        editor_id = self._pending_upload_inserts.pop(name, None)
        if editor_id is None:
            return
        editor = self._editor_by_id(editor_id)
        if editor is None:
            return
        self._insert_media_at_cursor(media, editor)

    def _on_upload_failed_for_insert(self, name: str, reason: str) -> None:
        """An upload originated from a drop or paste failed — drop the
        pending entry so it doesn't leak. Failure status is already
        surfaced by MediaStore via upload_failed → status label."""
        self._pending_upload_inserts.pop(name, None)

    # -- Blossom: paste image from clipboard ------------------------------

    def _handle_pasted_image(self) -> bool:
        """Upload the clipboard image and queue an auto-insert at the
        editor's cursor. Returns True if the paste was consumed (so the
        editor skips its plain-text fallback). Returns False on any
        reason we couldn't handle it.
        """
        clipboard = QApplication.clipboard()
        if not clipboard.mimeData().hasImage():
            return False
        image = clipboard.image()
        if image.isNull():
            return False

        active = self._profile_store.default()
        if active is None:
            QMessageBox.information(
                self,
                "Connect a signer first",
                "You pasted an image, but no Nostr signer is connected. "
                "Connect one (Nostr → Connect Signer…) to upload images "
                "to Blossom and insert them into your notes.",
            )
            return True  # consumed — don't fall through to plain-text paste

        ed = self.current_editor()
        if ed is None:
            self.new_tab()
            ed = self.current_editor()
        if ed is None:
            return False

        buf = QBuffer()
        buf.open(QIODevice.WriteOnly)
        if not image.save(buf, "PNG"):
            self.status.showMessage(
                "Pasted image could not be encoded as PNG.", 5000
            )
            return True
        body = bytes(buf.data())

        # Monotonic counter + wall clock keeps the display name unique
        # even when two pastes land inside one second.
        name = f"clipboard-{int(time.time())}-{next(_paste_job_counter):03d}.png"
        self._pending_upload_inserts[name] = id(ed)
        self._media_store.upload_bytes(body, name=name, mime_type="image/png")
        self.status.showMessage("Uploading pasted image to Blossom…", 5000)
        return True

    # ----------------------------------------------------------------------
    # NOSTR — drafts panel toggling
    # ----------------------------------------------------------------------

    def _on_toggle_drafts_panel(self):
        """Menu action: show/hide the side-docked drafts panel.

        First time we show the panel we also size the splitter so the
        editor doesn't lose more than necessary. Subsequent toggles
        preserve the user's resize. The QSplitter naturally clamps the
        panel between its min and max widths.
        """
        if self._drafts_panel is None:
            return
        if self._drafts_panel.isVisible():
            self._hide_drafts_panel()
        else:
            self._show_drafts_panel()

    def _show_drafts_panel(self) -> None:
        if self._drafts_panel is None:
            return
        self._drafts_panel.show()
        # If the panel currently has zero width (its first appearance),
        # split the central area so the panel gets its default width
        # while the editor keeps the remainder.
        sizes = self._central_splitter.sizes()
        if len(sizes) == 2 and sizes[1] <= 0:
            total = max(sum(sizes), self._central_splitter.width())
            panel_w = min(DEFAULT_PANEL_WIDTH, max(total - 360, 240))
            self._central_splitter.setSizes([total - panel_w, panel_w])
        self.act_nostr_drafts.setChecked(True)
        self._draft_sync.refresh()

    def _hide_drafts_panel(self) -> None:
        if self._drafts_panel is None:
            return
        self._drafts_panel.hide()
        self.act_nostr_drafts.setChecked(False)

    # ----------------------------------------------------------------------
    # NOSTR — drafts panel signal handlers
    # ----------------------------------------------------------------------

    def _on_panel_switch_profile(self) -> None:
        """The panel's profile chip was clicked — defer to the existing
        chip menu so the user has one canonical place to switch."""
        chip = self.header_widget.profile_chip
        chip.showMenu()

    def _on_panel_copy_event_id(self, event_id: str) -> None:
        if event_id:
            self.status.showMessage(f"Copied event id {event_id[:10]}…", 3000)

    def _on_panel_open_draft(self, identifier: str) -> None:
        """Open a draft into a new editor tab.

        If a tab is already bound to this identifier, just activate it.
        Otherwise spin up a new untitled tab and load the decrypted body.
        """
        record = self._draft_store.get(identifier)
        if record is None or record.state is not DraftState.READY:
            self.status.showMessage(
                "Draft isn't ready to open yet — still decrypting.", 4000
            )
            return

        # Activate an already-open tab if it's bound to this draft.
        for i in range(self.tabs.count()):
            ed = self._editor_from_widget(self.tabs.widget(i))
            if ed is None:
                continue
            binding = getattr(ed, "_draft_binding", None)
            if binding is not None and binding.identifier == identifier:
                self.tabs.setCurrentIndex(i)
                return

        # Fresh tab. Reuse ``new_tab`` so every per-tab wiring (backup,
        # timers, theming) is consistent with disk-opened tabs.
        self.new_tab()
        ed = self.current_editor()
        if ed is None:
            return
        ed.setPlainText(record.content)
        ed.document().setModified(False)
        active = self._profile_store.default()
        ed._draft_binding = DraftBinding(
            identifier=record.identifier,
            inner_kind=record.inner_kind,
            event_id=record.event_id,
            created_at=record.created_at,
            title=record.title,
            profile_pubkey=active.user_pubkey.lower() if active else "",
        )
        # Deliberately NOT pre-setting ``_save_destination`` — the
        # destination dialog should still appear on first Ctrl+Shift+S
        # so the user can save the draft locally if they want. Silent
        # re-stashes go through Ctrl+S, which respects the binding via
        # ``_restash_with_binding``.
        self._update_tab_title()

    def _on_panel_publish_draft(self, identifier: str) -> None:
        """Promote a draft to a real (signed, public) Nostr publish.

        Routes through the existing publish dialogs. The simplest path:
        open the draft into a tab (so all current-tab-reading plumbing
        keeps working) then trigger the appropriate publish action.
        """
        record = self._draft_store.get(identifier)
        if record is None or record.state is not DraftState.READY:
            return
        self._on_panel_open_draft(identifier)
        if record.inner_kind == INNER_KIND_LONG_FORM:
            self._on_nostr_publish_article()
        else:
            self._on_nostr_publish_note()

    def _on_panel_retry_decrypt(self, identifier: str) -> None:
        """Re-attempt NIP-44 decryption for a previously-failed draft.

        Common trigger: the user missed the Amber approval prompt in
        time, the bunker request timed out, and the row is now sitting
        in FAILED state. ``DraftSync.retry_decrypt`` uses the
        ciphertext cached on the record, so this is a single fresh
        signer round-trip — no relay re-fetch.
        """
        self._draft_sync.retry_decrypt(identifier)
        self.status.showMessage("Retrying decryption — approve on your signer…", 6000)

    def _on_panel_delete_draft(self, identifier: str, inner_kind: int) -> None:
        profile = self._profile_store.default()
        if profile is None:
            return
        record = self._draft_store.get(identifier)
        title = record.title if (record and record.title) else "this draft"

        # Pre-flight: drafts created by other Nostr clients can use inner
        # kinds this editor doesn't speak (the most common case is NIP-23
        # kind 30024, used by Habla / Yakihonne for long-form drafts).
        # Tombstoning one of those from here could leave it visible in
        # the originating client, so we refuse early with a clear note
        # instead of asking the user to confirm a destructive action that
        # would then fail at the signer round-trip.
        if inner_kind not in SUPPORTED_INNER_KINDS:
            info = QMessageBox(self)
            info.setIcon(QMessageBox.Information)
            info.setWindowTitle("Can't remove this draft")
            info.setText(f"\"{title}\" was created by another Nostr client.")
            info.setInformativeText(
                "It uses a draft format this editor doesn't recognise, so "
                "removing it from here might leave it visible in the other "
                "client.\n\nTo remove it cleanly, open it in the app that "
                "created it and delete it there."
            )
            info.setDetailedText(f"Inner event kind: {inner_kind}")
            info.setStandardButtons(QMessageBox.Ok)
            info.exec()
            return

        confirm = QMessageBox(self)
        confirm.setIcon(QMessageBox.Warning)
        confirm.setWindowTitle("Delete draft?")
        confirm.setText(f"Delete \"{title}\" from your Nostr drafts?")
        confirm.setInformativeText(
            "A blank-content replacement will be published to your relays. "
            "Other clients (and your other devices) will treat the draft as "
            "removed. This action can't be undone."
        )
        confirm.setStandardButtons(QMessageBox.Cancel | QMessageBox.Yes)
        confirm.setDefaultButton(QMessageBox.Cancel)
        if confirm.exec() != QMessageBox.Yes:
            return

        # Safety net: any future validation that DraftDeleteJob adds
        # (or any new ValueError path) lands as a calm dialog instead of
        # a traceback, matching the pre-flight tone above.
        try:
            job = DraftDeleteJob(
                relay_pool=self._relay_pool,
                relay_list_cache=self._relay_list_cache,
                session_pool=self._session_pool,
                profile=profile,
                identifier=identifier,
                inner_kind=inner_kind,
                parent=self,
            )
        except ValueError as exc:
            QMessageBox.warning(
                self,
                "Couldn't start the deletion",
                f"This draft can't be removed from here.\n\n{exc}",
            )
            return

        job.status_changed.connect(lambda s: self.status.showMessage(s, 4000))
        job.tombstoned.connect(lambda d, _eid: self._draft_store.remove(d))
        job.failed.connect(
            lambda reason: QMessageBox.warning(
                self, "Couldn't delete draft", reason
            )
        )
        job.start()

    def _on_draft_sync_status(self, text: str) -> None:
        if self._drafts_panel is not None:
            self._drafts_panel.set_status(text)

    def _on_draft_sync_bunker_error(self, message: str) -> None:
        if self._drafts_panel is not None:
            self._drafts_panel.set_signer_unsupported(True)
        # Surface in the main window status bar too — the user may not
        # have the panel open.
        self.status.showMessage(message, 8000)

    # ----------------------------------------------------------------------
    # NOSTR — contextual Ctrl+Shift+S: disk vs. draft destination
    # ----------------------------------------------------------------------

    def _has_active_nostr_profile(self) -> bool:
        return self._profile_store.default() is not None

    def _on_save_as_pressed(self) -> None:
        """Handler for ``Ctrl+Shift+S``.

        Three-way decision:
          1. No Nostr profile connected → behave as classic ``Save As``.
          2. Nostr connected + this tab already chose a destination →
             use that destination silently.
          3. Otherwise → open the chooser dialog; optionally remember
             the choice for this tab.

        Two safety guards run before any dialog opens:
          - **Debounce**: if a stash is already in flight for this tab,
            we surface a status message and bail. Prevents rapid-fire
            shortcut presses from stacking up signer prompts.
          - **Profile mismatch**: if the tab is bound to a draft from a
            different identity, the user picks between switching back,
            saving a copy under the active profile, or cancelling.
        """
        ed = self.current_editor()
        if ed is None:
            return
        if self._is_stash_in_flight_for(ed):
            self._stash_already_running_message()
            return
        if not self._has_active_nostr_profile():
            self.save_as()
            return

        # Profile mismatch handling. On the Save-As path we allow
        # forking because the user has explicitly asked "save somewhere"
        # rather than the silent "save what I have" that Ctrl+S means.
        mismatch_pk = self._draft_binding_profile_mismatch(ed)
        if mismatch_pk is not None:
            outcome = self._resolve_mismatch(ed, mismatch_pk, allow_fork=True)
            if outcome == "cancel":
                return
            if outcome == "switch":
                if not self._switch_active_profile_to(mismatch_pk):
                    # Profile was removed between detection and
                    # confirmation — surface and bail rather than fall
                    # through to a save under the wrong identity.
                    QMessageBox.warning(
                        self, "Profile unavailable",
                        "That profile is no longer connected. "
                        "Re-pair it from Nostr → Connect Signer to save here.",
                    )
                    return
                # After the switch, the binding now matches active, so
                # we proceed normally below.
            elif outcome == "fork":
                self._fork_draft_binding(ed)

        remembered = getattr(ed, "_save_destination", None)
        if remembered is SaveDestination.LOCAL:
            self.save_as()
            return
        if remembered is SaveDestination.NOSTR_DRAFT:
            self._stash_current_tab_as_draft()
            return

        # Default the chooser to the option that matches the tab's
        # current identity: if it's a draft tab, pre-select "Save as
        # Nostr draft" — but still show the dialog so the user can
        # change their mind. Otherwise default to the safer disk option.
        is_draft_tab = getattr(ed, "_draft_binding", None) is not None
        default = (
            SaveDestination.NOSTR_DRAFT if is_draft_tab else SaveDestination.LOCAL
        )
        dlg = SaveDestinationDialog(
            default=default,
            is_dark=self.is_dark_theme,
            parent=self,
        )
        if dlg.exec() != QDialog.Accepted:
            return
        if dlg.remember:
            ed._save_destination = dlg.destination
        if dlg.destination is SaveDestination.LOCAL:
            self.save_as()
        else:
            self._stash_current_tab_as_draft()

    def _stash_current_tab_as_draft(self) -> None:
        """Open the kind picker and, on accept, fire a ``DraftPublishJob``.

        Per the product spec we ask for the kind on every Save-As stash
        — the prior binding (if any) is pre-selected so the common path
        is two clicks: open dialog → confirm.

        For a silent re-save of an already-bound tab (the ``Ctrl+S``
        path) see ``_restash_with_binding`` instead.
        """
        ed = self.current_editor()
        profile = self._profile_store.default()
        if ed is None or profile is None:
            return

        prior: Optional[DraftBinding] = getattr(ed, "_draft_binding", None)
        default_kind = (
            StashKind.ARTICLE
            if prior is not None and prior.inner_kind == INNER_KIND_LONG_FORM
            else StashKind.NOTE
        )
        title_hint, slug_hint, summary_hint, existing_note_id = (
            self._stash_defaults_for(ed, prior)
        )

        dlg = StashKindDialog(
            default=default_kind,
            suggested_title=title_hint,
            suggested_slug=slug_hint,
            suggested_summary=summary_hint,
            existing_note_identifier=existing_note_id,
            is_dark=self.is_dark_theme,
            parent=self,
        )
        if dlg.exec() != QDialog.Accepted or dlg.choice is None:
            return

        choice: StashChoice = dlg.choice
        inner = self._build_inner_for_choice(profile, choice, ed.toPlainText())
        if inner is None:
            return
        self._fire_draft_publish_job(ed, profile, choice, inner)

    def _restash_with_binding(self, ed, binding: DraftBinding) -> None:
        """Silent re-save of a draft-bound tab — no dialogs.

        Triggered by ``Ctrl+S`` when the tab has a draft binding but no
        local file path. The kind, identifier, and metadata are taken
        from the binding (and from the store for the cached summary),
        so the user gets the standard "save without asking" experience
        familiar from every editor.

        If the user wants to change the kind or move the draft to disk,
        they use ``Ctrl+Shift+S`` (the chooser dialog).
        """
        profile = self._profile_store.default()
        if profile is None:
            return

        # Pull summary off the cached record so re-stashes don't drop
        # metadata that's only stored in the inner event tags. Title is
        # already on the binding.
        summary = ""
        record = self._draft_store.get(binding.identifier)
        if record is not None:
            for tag in record.inner_tags:
                if len(tag) >= 2 and tag[0] == "summary" and not summary:
                    summary = tag[1]

        kind = (
            StashKind.ARTICLE
            if binding.inner_kind == INNER_KIND_LONG_FORM
            else StashKind.NOTE
        )
        choice = StashChoice(
            kind=kind,
            identifier=binding.identifier,
            title=binding.title,
            summary=summary,
        )
        inner = self._build_inner_for_choice(profile, choice, ed.toPlainText())
        if inner is None:
            return
        self._fire_draft_publish_job(ed, profile, choice, inner)

    def _fire_draft_publish_job(
        self,
        ed,
        profile: Profile,
        choice: StashChoice,
        inner: dict,
    ) -> None:
        """Validate size, fire ``DraftPublishJob``, wire the callbacks.

        Shared by the explicit-kind-picker path and the silent-restash
        path so both behave identically once the kind + identifier are
        decided.
        """
        # Final debounce check — the entry points (Ctrl+S, Ctrl+Shift+S)
        # also guard, but defense-in-depth here protects any future
        # call site we add (e.g. an auto-save timer).
        if self._is_stash_in_flight_for(ed):
            self._stash_already_running_message()
            return
        # Empty body: short-circuit with a friendly message before we
        # ask the signer to encrypt nothing. Matches the same guard the
        # publish-note and publish-article flows already use.
        if not str(inner.get("content", "")).strip():
            QMessageBox.information(
                self,
                "Nothing to stash",
                "The current document is empty. Add some content before "
                "saving it as a draft.",
            )
            return
        # Pre-flight the plaintext cap with a friendly message rather
        # than letting the publish job fail mid-pipeline. The job also
        # checks, but doing it here means no signer round-trip wasted.
        try:
            payload_bytes = len(serialize_inner_event(inner).encode("utf-8"))
        except (KeyError, TypeError, ValueError):
            QMessageBox.warning(
                self, "Couldn't prepare draft",
                "The draft contents could not be serialized for encryption.",
            )
            return
        if payload_bytes > MAX_INNER_PAYLOAD_BYTES:
            QMessageBox.warning(
                self, "Draft too large",
                f"This draft ({payload_bytes:,} bytes) exceeds the NIP-44 "
                f"encryption limit of {MAX_INNER_PAYLOAD_BYTES:,} bytes. "
                "Split it into smaller drafts or publish it directly.",
            )
            return

        job = DraftPublishJob(
            relay_pool=self._relay_pool,
            relay_list_cache=self._relay_list_cache,
            session_pool=self._session_pool,
            profile=profile,
            inner_event=inner,
            identifier=choice.identifier,
            parent=self,
        )
        # Pass the editor + inner through the closure so the stashed
        # handler can bind the tab and update the store optimistically.
        job.status_changed.connect(lambda s: self.status.showMessage(s, 5000))
        job.stashed.connect(
            lambda ident, eid, ts, _ed=ed, _inner=inner, _choice=choice:
            self._on_draft_stashed(_ed, _choice, eid, ts, _inner)
        )
        job.failed.connect(
            lambda reason: QMessageBox.warning(
                self, "Couldn't stash draft", reason
            )
        )
        # Register before starting so a synchronous failure path can't
        # leave the tab thinking nothing is in flight.
        self._attach_active_stash(ed, job)
        job.start()

    def _stash_defaults_for(
        self,
        ed,
        prior: Optional[DraftBinding],
    ) -> tuple[str, str, str, str]:
        """Compute title / slug / summary defaults for the kind dialog.

        Priority: prior draft binding → store metadata → file basename.
        Returns ``(title, slug, summary, existing_note_identifier)``.
        """
        title = ""
        slug = ""
        summary = ""
        existing_note_id = ""

        if prior is not None and prior.inner_kind == INNER_KIND_SHORT_NOTE:
            existing_note_id = prior.identifier

        if prior is not None:
            title = prior.title or ""
            if prior.inner_kind == INNER_KIND_LONG_FORM:
                slug = prior.identifier
            # Pull the cached summary off the store if available.
            record = self._draft_store.get(prior.identifier)
            if record is not None:
                for tag in record.inner_tags:
                    if len(tag) >= 2 and tag[0] == "summary" and not summary:
                        summary = tag[1]

        if not title:
            # Fall back to the file basename without extension.
            path = getattr(ed, "_file_path", None)
            if path:
                base = os.path.splitext(os.path.basename(path))[0]
                # Underscores/hyphens to spaces; title-case for a humane default.
                title = base.replace("_", " ").replace("-", " ").strip()
        return title, slug, summary, existing_note_id

    def _build_inner_for_choice(
        self,
        profile: Profile,
        choice: StashChoice,
        content: str,
    ) -> Optional[dict]:
        """Compose the inner unsigned event from the kind-picker choice.

        For articles we also seed the ``title`` / ``summary`` / ``d``
        tags so promoting the draft to a real NIP-23 publish later has
        the metadata it needs.
        """
        tags: list[list[str]] = []
        if choice.kind is StashKind.ARTICLE:
            tags.append(["d", choice.identifier])
            if choice.title:
                tags.append(["title", choice.title])
            if choice.summary:
                tags.append(["summary", choice.summary])
        try:
            return build_inner_event(
                kind=choice.kind.value,
                content=content,
                pubkey_hex=profile.user_pubkey,
                tags=tags,
            )
        except ValueError as exc:
            QMessageBox.warning(
                self, "Couldn't prepare draft", str(exc),
            )
            return None

    def _on_draft_stashed(
        self,
        ed,
        choice: StashChoice,
        event_id: str,
        created_at: int,
        inner: dict,
    ) -> None:
        """Job has signed the wrap. Bind the tab + update the store
        optimistically so the panel reflects the new state immediately
        rather than waiting for the relay echo to round-trip back
        through ``DraftSync``."""
        # The editor may have been closed mid-flight — bail gracefully.
        if ed is None or self._editor_widget_index(ed) < 0:
            return
        active = self._profile_store.default()
        ed._draft_binding = DraftBinding(
            identifier=choice.identifier,
            inner_kind=choice.kind.value,
            event_id=event_id,
            created_at=created_at,
            title=choice.title or (choice.identifier if choice.kind is StashKind.NOTE else ""),
            profile_pubkey=active.user_pubkey.lower() if active else "",
        )
        # A successful stash clears the modified flag — the tab's
        # contents now match the latest draft snapshot on the network.
        ed.document().setModified(False)
        self._update_tab_title()

        # Optimistic store update — by the time the relay echo arrives
        # via DraftSync, the panel already shows the row.
        self._draft_store.upsert_from_inner(
            identifier=choice.identifier,
            inner=inner,
            event_id=event_id,
            created_at=created_at,
            expiration=None,
        )

    def _editor_widget_index(self, ed) -> int:
        """Return the tab index that hosts ``ed``, or -1 if not found.

        Walks the tab widgets defensively — used by the post-job
        callback to verify the tab still exists before mutating it.
        """
        for i in range(self.tabs.count()):
            if self._editor_from_widget(self.tabs.widget(i)) is ed:
                return i
        return -1

    # ----------------------------------------------------------------------
    # NOSTR — stash debounce: at most one in-flight stash per tab
    # ----------------------------------------------------------------------

    def _is_stash_in_flight_for(self, ed) -> bool:
        """True if a ``DraftPublishJob`` is currently running for this tab.

        Prevents the rapid-Ctrl+Shift+S footgun where a user holding the
        shortcut would spawn N parallel stash jobs, each opening its own
        signer approval prompt. The guard sits at every entry point so
        the chooser + kind dialogs don't even appear while a stash is
        already underway.
        """
        return getattr(ed, "_active_stash_job", None) is not None

    def _stash_already_running_message(self) -> None:
        self.status.showMessage("Already saving this draft — wait for it to finish.", 4000)

    def _attach_active_stash(self, ed, job) -> None:
        ed._active_stash_job = job

        def _release(*_args) -> None:
            # Only clear if this is still the registered job — guards
            # against an out-of-order completed/failed pair from an
            # earlier cancelled job clobbering a newer one.
            if getattr(ed, "_active_stash_job", None) is job:
                ed._active_stash_job = None

        job.completed.connect(_release)
        job.failed.connect(_release)

    # ----------------------------------------------------------------------
    # NOSTR — draft / active-profile mismatch
    # ----------------------------------------------------------------------

    def _draft_binding_profile_mismatch(self, ed) -> Optional[str]:
        """Return the draft binding's pubkey if it doesn't match the active
        profile, or ``None`` when the binding either matches or is absent.

        The active profile drives signing and relay routing. Saving a
        draft tab under a different identity would silently fork the
        draft's addressable coordinate, which is rarely the user's
        intent — so this check funnels mismatches through a clear
        confirmation dialog rather than letting them happen by accident.
        """
        binding: Optional[DraftBinding] = getattr(ed, "_draft_binding", None)
        if binding is None or not binding.profile_pubkey:
            return None
        active = self._profile_store.default()
        if active is None:
            # No active profile: the caller already short-circuits to
            # disk save, so the mismatch doesn't matter here.
            return None
        if binding.profile_pubkey.lower() == active.user_pubkey.lower():
            return None
        return binding.profile_pubkey.lower()

    def _resolve_mismatch(
        self,
        ed,
        original_pubkey: str,
        *,
        allow_fork: bool,
    ) -> str:
        """Show the mismatch dialog and return one of ``switch`` /
        ``fork`` / ``cancel`` based on the user's choice.

        ``allow_fork`` is False on the Ctrl+S path because silent
        ``save`` should never quietly change which identity owns the
        draft; the user must explicitly Save-As to fork.
        """
        active = self._profile_store.default()
        original = self._profile_store.get(original_pubkey)

        def _label_for(p: Optional[Profile], pk: str) -> str:
            if p is not None:
                return p.display_name or p.npub_short()
            if pk:
                return f"{pk[:8]}…{pk[-4:]}"
            return "(unknown profile)"

        active_label = _label_for(active, active.user_pubkey if active else "")
        original_label = _label_for(original, original_pubkey)

        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Warning)
        msg.setWindowTitle("Different Nostr profile")
        msg.setText(
            f"This draft was last saved under {original_label}, "
            f"but you're now signed in as {active_label}."
        )
        msg.setInformativeText(
            "Saving under the current profile would create a separate "
            "draft. How would you like to handle it?"
        )

        switch_btn = None
        if original is not None:
            switch_btn = msg.addButton(
                f"Switch to {original_label} and save",
                QMessageBox.AcceptRole,
            )
        fork_btn = None
        if allow_fork:
            fork_btn = msg.addButton(
                f"Save a copy under {active_label}",
                QMessageBox.ActionRole,
            )
        cancel_btn = msg.addButton(QMessageBox.Cancel)
        msg.setDefaultButton(cancel_btn)
        msg.exec()

        clicked = msg.clickedButton()
        if switch_btn is not None and clicked is switch_btn:
            return "switch"
        if fork_btn is not None and clicked is fork_btn:
            return "fork"
        return "cancel"

    def _switch_active_profile_to(self, pubkey_hex: str) -> bool:
        """Switch the active Nostr profile by pubkey. Returns True on
        success, False if the requested profile is no longer in the
        store."""
        profile = self._profile_store.get(pubkey_hex)
        if profile is None:
            return False
        self._on_nostr_select_profile(profile)
        return True

    def _fork_draft_binding(self, ed) -> None:
        """Detach a tab from its current draft so the next stash mints
        a fresh identifier under the active profile."""
        ed._draft_binding = None
        ed._save_destination = None  # ensure the chooser re-appears
        self._update_tab_title()

    # ----------------------------------------------------------------------
    # NOSTR — conflict banner
    # ----------------------------------------------------------------------

    def _on_draft_record_changed(self, identifier: str) -> None:
        """Detect "newer version arrived from another device" conflicts.

        Fires whenever ``DraftStore.record_changed`` triggers. For each
        tab bound to ``identifier`` whose locally-cached event_id is
        older than the store's current one, surface the conflict banner.
        """
        record = self._draft_store.get(identifier)
        if record is None or record.state is not DraftState.READY:
            return
        for i in range(self.tabs.count()):
            container = self.tabs.widget(i)
            ed = self._editor_from_widget(container)
            if ed is None:
                continue
            binding = getattr(ed, "_draft_binding", None)
            if binding is None or binding.identifier != identifier:
                continue
            if not record.event_id or record.event_id == binding.event_id:
                continue  # No new server-side version.
            # If the tab isn't dirty, silently refresh — there's
            # nothing to conflict with.
            if not ed.document().isModified():
                ed.setPlainText(record.content)
                ed.document().setModified(False)
                binding.event_id = record.event_id
                binding.created_at = record.created_at
                binding.title = record.title
                self._update_tab_title()
                continue
            # Local edits + remote update → show the banner.
            self._show_conflict_banner(container, ed, record)

    def _show_conflict_banner(self, container, ed, record) -> None:
        """Insert (or update) the per-tab conflict banner."""
        if ed in self._tab_conflict_banners:
            # Already showing — refresh the message in case the remote
            # version has updated again.
            self._tab_conflict_banners[ed].show()
            return
        banner = DraftConflictBanner(is_dark=self.is_dark_theme)
        identifier = record.identifier
        banner.view_remote.connect(
            lambda i=identifier: self._on_conflict_view(i)
        )
        banner.reload.connect(
            lambda e=ed, i=identifier: self._on_conflict_reload(e, i)
        )
        banner.keep_mine.connect(lambda e=ed: self._dismiss_conflict_banner(e))
        banner.dismissed.connect(lambda e=ed: self._dismiss_conflict_banner(e))

        # The container's layout is a QVBoxLayout holding [bar, editor_area].
        # We insert the banner at the very top so it sits above both.
        layout = container.layout()
        layout.insertWidget(0, banner)
        self._tab_conflict_banners[ed] = banner

    def _dismiss_conflict_banner(self, ed) -> None:
        banner = self._tab_conflict_banners.pop(ed, None)
        if banner is None:
            return
        banner.setParent(None)
        banner.deleteLater()

    def _on_conflict_view(self, identifier: str) -> None:
        # View = open the remote version in a brand-new tab without
        # touching the conflicted one.
        self._on_panel_open_draft(identifier)

    def _on_conflict_reload(self, ed, identifier: str) -> None:
        record = self._draft_store.get(identifier)
        if record is None or record.state is not DraftState.READY:
            return
        ed.setPlainText(record.content)
        ed.document().setModified(False)
        binding = getattr(ed, "_draft_binding", None)
        if binding is not None:
            binding.event_id = record.event_id
            binding.created_at = record.created_at
            binding.title = record.title
        self._update_tab_title()
        self._dismiss_conflict_banner(ed)
