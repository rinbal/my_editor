# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Second page of the drafts panel: import RSS / Atom / JSON feeds as
NIP-37 drafts.

Layout, top to bottom:

  +------------------------------------------+
  |  Feed URL  [______________________]      |
  |  Limit     [ 25 ▲▼ ]      [ Import ]     |
  +------------------------------------------+
  |  status line                             |
  +------------------------------------------+
  |  - First post              published     |
  |  - Second post             signing       |
  |  - Third post              pending       |
  |  ...                                     |
  +------------------------------------------+

Dependencies (relay pool, relay-list cache, bunker session pool) are
injected via :meth:`bind_runtime` so the panel can live in the same
``QStackedLayout`` as the existing drafts list without forcing
``DraftsPanel`` to grow new constructor args.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..bunker import BunkerSessionPool
from ..outbox import RelayListCache
from ..profiles import Profile
from ..relay import RelayPool
from ..rss.importer import FeedImportJob


_DEFAULT_LIMIT: int = 25
_MAX_LIMIT: int = 500


_STATUS_LABEL = {
    "pending": "pending",
    "resolving": "resolving from Nostr...",
    "signing": "signing",
    "published": "published",
    "failed": "failed",
}


_DARK_CSS = """
QFrame#feeds_panel { background: #1E1E1E; }
QLabel#feeds_panel_field { color: #858585; font-size: 10px;
    text-transform: uppercase; letter-spacing: 0.6px; }
QLabel#feeds_panel_hint { color: #6E6E6E; font-size: 11px; }
QLabel#feeds_panel_status { color: #D4D4D4; font-size: 11px; padding: 4px 8px; }
QLineEdit#feeds_panel_url, QSpinBox#feeds_panel_limit {
    background: #2A2A2A; color: #FFFFFF; border: 1px solid #3C3C3C;
    border-radius: 4px; padding: 6px 8px;
}
QLineEdit#feeds_panel_url:focus, QSpinBox#feeds_panel_limit:focus {
    border-color: #0E639C;
}
QPushButton#feeds_panel_primary {
    background: #0E639C; color: #FFFFFF; border: none;
    border-radius: 4px; padding: 7px 14px; font-weight: 600;
}
QPushButton#feeds_panel_primary:hover { background: #1177BB; }
QPushButton#feeds_panel_primary:disabled { background: #2A2A2A; color: #6E6E6E; }
QPushButton#feeds_panel_secondary {
    background: transparent; color: #D4D4D4; border: 1px solid #3C3C3C;
    border-radius: 4px; padding: 6px 12px;
}
QListWidget#feeds_panel_list {
    background: #1E1E1E; color: #D4D4D4; border: none;
    border-top: 1px solid #2A2A2A;
}
QListWidget#feeds_panel_list::item { padding: 6px 8px; }
QListWidget#feeds_panel_list::item:selected { background: #2A2D2E; }
"""

_LIGHT_CSS = """
QFrame#feeds_panel { background: #FFFFFF; }
QLabel#feeds_panel_field { color: #777777; font-size: 10px;
    text-transform: uppercase; letter-spacing: 0.6px; }
QLabel#feeds_panel_hint { color: #888888; font-size: 11px; }
QLabel#feeds_panel_status { color: #222222; font-size: 11px; padding: 4px 8px; }
QLineEdit#feeds_panel_url, QSpinBox#feeds_panel_limit {
    background: #FFFFFF; color: #222222; border: 1px solid #C8C8C8;
    border-radius: 4px; padding: 6px 8px;
}
QLineEdit#feeds_panel_url:focus, QSpinBox#feeds_panel_limit:focus {
    border-color: #0E639C;
}
QPushButton#feeds_panel_primary {
    background: #0E639C; color: #FFFFFF; border: none;
    border-radius: 4px; padding: 7px 14px; font-weight: 600;
}
QPushButton#feeds_panel_primary:hover { background: #1177BB; }
QPushButton#feeds_panel_primary:disabled { background: #E5E5E5; color: #999999; }
QPushButton#feeds_panel_secondary {
    background: transparent; color: #222222; border: 1px solid #C8C8C8;
    border-radius: 4px; padding: 6px 12px;
}
QListWidget#feeds_panel_list {
    background: #FFFFFF; color: #222222; border: none;
    border-top: 1px solid #ECECEC;
}
QListWidget#feeds_panel_list::item { padding: 6px 8px; }
QListWidget#feeds_panel_list::item:selected { background: #E5F2FB; }
"""


class FeedsPanel(QFrame):
    """RSS / Atom / JSON Feed importer.

    Public surface:
      bind_runtime(...)          inject relay pool, relay-list cache,
                                 session pool. Must be called before
                                 the user can run an import.
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
    ) -> None:
        super().__init__(parent)
        self.setObjectName("feeds_panel")
        self.setFrameShape(QFrame.NoFrame)

        self._is_dark = is_dark
        self._relay_pool: Optional[RelayPool] = None
        self._relay_list_cache: Optional[RelayListCache] = None
        self._session_pool: Optional[BunkerSessionPool] = None
        self._active_profile: Optional[Profile] = None
        self._job: Optional[FeedImportJob] = None
        self._rows: list[QListWidgetItem] = []

        self._build_ui()
        self.apply_theme(is_dark)
        self._refresh_button_state()

    # -- construction ------------------------------------------------------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        outer.addWidget(self._build_form())
        outer.addWidget(self._build_status())
        outer.addWidget(self._build_list(), 1)

    def _build_form(self) -> QWidget:
        frame = QFrame()
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(10, 10, 10, 6)
        layout.setSpacing(6)

        url_label = QLabel("Site or feed URL")
        url_label.setObjectName("feeds_panel_field")
        self._url_edit = QLineEdit()
        self._url_edit.setObjectName("feeds_panel_url")
        self._url_edit.setPlaceholderText("yourblog.example.com or paste any article URL")
        self._url_edit.setClearButtonEnabled(True)
        self._url_edit.textChanged.connect(self._refresh_button_state)
        self._url_edit.returnPressed.connect(self._on_import_clicked)
        layout.addWidget(url_label)
        layout.addWidget(self._url_edit)

        url_hint = QLabel(
            "We'll find the RSS, Atom, or JSON feed for you."
        )
        url_hint.setObjectName("feeds_panel_hint")
        url_hint.setWordWrap(True)
        layout.addWidget(url_hint)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        limit_label = QLabel("Limit")
        limit_label.setObjectName("feeds_panel_field")
        self._limit_spin = QSpinBox()
        self._limit_spin.setObjectName("feeds_panel_limit")
        self._limit_spin.setRange(1, _MAX_LIMIT)
        self._limit_spin.setValue(_DEFAULT_LIMIT)
        self._limit_spin.setFixedWidth(80)

        self._import_btn = QPushButton("Import")
        self._import_btn.setObjectName("feeds_panel_primary")
        self._import_btn.setCursor(Qt.PointingHandCursor)
        self._import_btn.clicked.connect(self._on_import_clicked)

        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.setObjectName("feeds_panel_secondary")
        self._cancel_btn.setCursor(Qt.PointingHandCursor)
        self._cancel_btn.clicked.connect(self._on_cancel_clicked)
        self._cancel_btn.setVisible(False)

        row.addWidget(limit_label)
        row.addWidget(self._limit_spin)
        row.addStretch(1)
        row.addWidget(self._cancel_btn)
        row.addWidget(self._import_btn)
        layout.addLayout(row)
        return frame

    def _build_status(self) -> QWidget:
        self._status_label = QLabel("")
        self._status_label.setObjectName("feeds_panel_status")
        self._status_label.setWordWrap(True)
        return self._status_label

    def _build_list(self) -> QWidget:
        self._list = QListWidget()
        self._list.setObjectName("feeds_panel_list")
        self._list.setUniformItemSizes(True)
        self._list.setSelectionMode(QListWidget.NoSelection)
        return self._list

    # -- public surface ----------------------------------------------------

    def bind_runtime(
        self,
        *,
        relay_pool: RelayPool,
        relay_list_cache: RelayListCache,
        session_pool: BunkerSessionPool,
    ) -> None:
        """Inject the runtime dependencies needed to publish drafts."""
        self._relay_pool = relay_pool
        self._relay_list_cache = relay_list_cache
        self._session_pool = session_pool
        self._refresh_button_state()

    def set_active_profile(self, profile: Optional[Profile]) -> None:
        """Track the active Nostr profile. Without one, imports are disabled."""
        if self._active_profile is profile:
            return
        # Cancel an in-flight import when the profile changes: the new
        # profile shouldn't inherit a job signed by the old key.
        if self._job is not None:
            self._job.cancel()
            self._job = None
            self._cancel_btn.setVisible(False)
        self._active_profile = profile
        self._refresh_button_state()

    def apply_theme(self, is_dark: bool) -> None:
        self._is_dark = is_dark
        self.setStyleSheet(_DARK_CSS if is_dark else _LIGHT_CSS)

    # -- button state -----------------------------------------------------

    def _refresh_button_state(self) -> None:
        runtime_ready = (
            self._relay_pool is not None
            and self._relay_list_cache is not None
            and self._session_pool is not None
        )
        url_ready = bool(self._url_edit.text().strip())
        profile_ready = self._active_profile is not None
        idle = self._job is None
        enabled = runtime_ready and url_ready and profile_ready and idle
        self._import_btn.setEnabled(enabled)
        if self._active_profile is None:
            self._set_status("Connect a Nostr profile to import feeds.")
        elif not runtime_ready:
            self._set_status("Importer is not wired up yet.")
        elif not url_ready and idle:
            self._set_status("")

    # -- import lifecycle --------------------------------------------------

    def _on_import_clicked(self) -> None:
        if not self._import_btn.isEnabled():
            return
        url = self._url_edit.text().strip()
        if not url:
            return
        if (
            self._relay_pool is None
            or self._relay_list_cache is None
            or self._session_pool is None
            or self._active_profile is None
        ):
            return

        self._list.clear()
        self._rows = []
        limit_value = int(self._limit_spin.value())

        self._job = FeedImportJob(
            feed_url=url,
            profile=self._active_profile,
            relay_pool=self._relay_pool,
            relay_list_cache=self._relay_list_cache,
            session_pool=self._session_pool,
            limit=limit_value,
            parent=self,
        )
        self._job.status_changed.connect(self._set_status)
        self._job.feed_loaded.connect(self._on_feed_loaded)
        self._job.item_started.connect(self._on_item_started)
        self._job.item_resolving_from_nostr.connect(self._on_item_resolving)
        self._job.item_succeeded.connect(self._on_item_succeeded)
        self._job.item_failed.connect(self._on_item_failed)
        self._job.completed.connect(self._on_completed)
        self._job.failed.connect(self._on_failed)

        self._cancel_btn.setVisible(True)
        self._import_btn.setVisible(False)
        self._set_status("Starting import...")
        self._job.start()
        self._refresh_button_state()

    def _on_cancel_clicked(self) -> None:
        if self._job is None:
            return
        self._job.cancel()
        self._set_status("Import cancelled.")
        self._finish_import()

    def _on_feed_loaded(self, feed_title: str, item_count: int) -> None:
        if item_count == 0:
            self._set_status(f"{feed_title}: no items to import.")
            return
        self._set_status(f"{feed_title}: importing {item_count} item(s)...")
        for _ in range(item_count):
            item = QListWidgetItem()
            self._list.addItem(item)
            self._rows.append(item)

    def _on_item_started(self, index: int, title: str) -> None:
        self._set_row(index, title, "signing")

    def _on_item_resolving(self, index: int, title: str) -> None:
        # The importer routes long-form items through Nostr before
        # signing; surface that state so the user knows why this row is
        # taking longer than the others.
        self._set_row(index, title, "resolving")

    def _on_item_succeeded(self, index: int, _identifier: str) -> None:
        title = self._row_title(index)
        self._set_row(index, title, "published")

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
        self._finish_import()

    def _on_failed(self, reason: str) -> None:
        self._set_status(f"Import failed: {reason}")
        self._finish_import()

    # -- helpers ----------------------------------------------------------

    def _set_status(self, text: str) -> None:
        self._status_label.setText(text)
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
        self._job = None
        self._cancel_btn.setVisible(False)
        self._import_btn.setVisible(True)
        self._refresh_button_state()


def _row_colour(status_key: str, is_dark: bool) -> Optional[QColor]:
    """Subtle colour cue per row state. Returns ``None`` for the default."""
    if status_key == "published":
        return QColor("#4EC9B0") if is_dark else QColor("#0A7B68")
    if status_key == "failed":
        return QColor("#F48771") if is_dark else QColor("#C8412A")
    if status_key in ("signing", "resolving"):
        return QColor("#DCDCAA") if is_dark else QColor("#85651E")
    return None
